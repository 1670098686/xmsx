"""智能解析业务：统一由 ReAct 文献解析智能体主导解析过程。

Agent 自主编排"原件直传 → 全文文本 → 本地规则"三级能力：优先把文献原件
交给模型通读，原件通道因协议不支持/超限失败时经用户授权降级全文文本，
AI 整体不可用或 Agent 自身异常时回退本地规则解析，保证解析不中断。

Agent 全部提示词由 agent.prompts 统一管理（外置模板位于
resources/prompts/，业务层不再直接加载提示词）。
"""
import json
import os
import tempfile
import threading

from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.dao.rule_dao import ParseRuleDao
from data_layer.db_connect import DatabaseManager
from tool_layer import file_helper, file_parser, text_analysis
from utils.logger import get_logger

from agent import run_parse_agent
from business.parse_rule_service import DIMENSION_KEYS, ParseRuleService
from business.service_common import (
    exception_to_code, get_base_dir, write_operation_log,
)
from business.system_service import SystemService

logger = get_logger()

# 规则维度开关 → 报告字段
_DIMENSION_FIELDS = (
    "research_background", "core_view", "research_method",
    "innovation_point", "research_conclusion", "reference_list",
)

# Word→PDF 渲染缓存创建锁：解析页/笔记页可能同时请求同一文献，
# 串行化“检查缓存→转换→改名”，避免并发写同名产物造成 PDF 损坏
_render_cache_lock = threading.Lock()


class LiteratureParseService:
    """文献智能解析业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.lit_dao = LiteratureInfoDao()
        self.report_dao = LiteratureReportDao()
        self.rule_dao = ParseRuleDao()

    # ================= 单篇解析 =================

    def parse_single(self, lit_id: int, rule_id: int = None,
                     progress_callback=None, degradation_callback=None) -> tuple:
        """解析单篇文献并写入结构化报告。

        Args:
            lit_id: 文献 id。
            rule_id: 指定解析规则 id；不传则取系统默认规则。
            progress_callback: 可选回调 callback(percent:int, message:str)。
            degradation_callback: 可选回调 callback(error_message:str)->bool；
                原件通道不被模型支持时调用，返回 True 表示用户允许改用
                全文文本通道重试，False 表示不允许（直接回退本地解析）。
                不传则不自动降级。
        Returns:
            (code, report_dict, msg)
        """
        def emit(percent, message):
            """安全转发批量进度回调（未提供回调时忽略）。"""
            if progress_callback:
                progress_callback(percent, message)

        try:
            lit = self.lit_dao.select_by_id(lit_id)
            if not lit:
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"

            emit(5, "加载解析规则")
            rule, rule_detail = self._load_rule(rule_id)
            active_rule_id = rule["id"]

            with DatabaseManager().write_lock:
                self.lit_dao.update_by_id(lit_id, {"is_parsed": C.PARSE_IN_PROGRESS})

            # ReAct + LangGraph 智能体是唯一的 AI 解析路径：自主编排
            # 原件直传/全文文本/本地工具并做三重校验；Agent 自身异常时
            # 回退纯本地规则兜底，保证解析不中断
            precision = int(rule.get("precision_level", 3))
            agent_warnings: list = []
            ai_error = None

            try:
                agent_output = self._try_agent_parse(
                    lit, rule_detail, active_rule_id, precision, emit,
                    degradation_callback,
                )
            except Exception as exc:
                # 编排层任何异常都必须回退本地兜底，绝不中断解析
                logger.warning(
                    "ReAct Agent 解析异常，回退本地规则解析：lit_id=%s | %s",
                    lit_id, getattr(exc, "message", str(exc)), exc_info=True)
                write_operation_log(
                    C.OP_PARSE,
                    f"Agent 异常已回退本地规则："
                    f"{getattr(exc, 'message', str(exc))}",
                    lit_id, C.OP_STATUS_SUCCESS,
                )
                agent_output = None

            if agent_output is not None:
                report, keywords, engine, agent_warnings, ai_error = agent_output
            else:
                report, keywords, engine = self._run_local_fallback(
                    lit, rule_detail, active_rule_id, precision, emit)

            # 关键词随报告持久化（JSON 数组字符串），刷新/重新导出不丢失
            report["keywords"] = json.dumps(keywords, ensure_ascii=False)

            emit(95, "写入解析报告")
            self._save_report(lit_id, report)

            with DatabaseManager().write_lock:
                self.lit_dao.update_by_id(lit_id, {"is_parsed": C.PARSE_DONE})

            saved = self.report_dao.get_by_lit_id(lit_id)
            report_view = self._compose_report_view(lit, saved)
            report_view["parse_engine"] = engine
            # AI 尝试过但失败时回传面向用户的错误信息（未配置模型时为 None）
            report_view["ai_error"] = ai_error
            # Agent 三重校验未完全通过时的告警（如某维度已改用本地基线）
            report_view["agent_warnings"] = agent_warnings

            log_detail = f"解析完成（{engine}）：{lit.get('literature_title', lit_id)}"
            if agent_warnings:
                log_detail += "；" + "；".join(agent_warnings)
            write_operation_log(C.OP_PARSE, log_detail, lit_id)
            emit(100, "解析完成")
            if engine in ("agent_ai", "agent_local"):
                msg = "解析完成（ReAct 智能体）"
                if agent_warnings:
                    msg += f"，{len(agent_warnings)} 项校验告警已在报告中标注"
            else:
                msg = "解析完成（本地规则兜底）"
            return C.CODE_SUCCESS, report_view, msg

        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("解析文献 %s 失败：%s", lit_id, msg, exc_info=True)
            self._mark_failed(lit_id)
            write_operation_log(
                C.OP_PARSE, f"解析失败[id={lit_id}]：{msg}", lit_id, C.OP_STATUS_FAILED
            )
            return code, None, msg

    def reparse(self, lit_id: int, rule_id: int = None,
                progress_callback=None, degradation_callback=None) -> tuple:
        """重新解析（覆盖历史报告，先删旧报告再解析）。

        Returns:
            (code, report_dict, msg)
        """
        try:
            with DatabaseManager().write_lock:
                self.report_dao.delete_by_lit_id(lit_id)
        except Exception as exc:
            logger.warning("清理旧报告失败：%s", exc)
        return self.parse_single(
            lit_id, rule_id, progress_callback,
            degradation_callback=degradation_callback,
        )

    def clear_report(self, lit_id: int) -> tuple:
        """仅删除指定文献的解析报告，文献保留并回到未解析状态。

        与“删除文献”相区别：本操作只清空结构化解析结果（literature_report）
        并把 is_parsed 回退为未解析，literature_info 记录、源文件与段落笔记
        均保留，用户随后可在解析页重新选中该文献重新解析。

        Args:
            lit_id: 文献 id。
        Returns:
            (code, None, msg)：code=0 成功；文献不存在返回
            CODE_FILE_NOT_FOUND；本就没有报告返回 CODE_ERROR。
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
        report = self.report_dao.get_by_lit_id(lit_id)
        if not report:
            return C.CODE_ERROR, None, "该文献暂无解析报告"
        try:
            with DatabaseManager().write_lock:
                self.report_dao.delete_by_lit_id(lit_id)
                self.lit_dao.update_by_id(
                    lit_id, {"is_parsed": C.PARSE_NOT_STARTED}
                )
            write_operation_log(
                C.OP_PARSE,
                f"删除解析报告（文献保留）：{lit.get('literature_title', lit_id)}",
                lit_id,
            )
            return C.CODE_SUCCESS, None, "解析报告已删除，文献状态回到未解析"
        except Exception as exc:
            msg = getattr(exc, "message", str(exc))
            logger.error("删除文献 %s 的解析报告失败：%s", lit_id, msg,
                         exc_info=True)
            write_operation_log(
                C.OP_PARSE, f"删除解析报告失败[id={lit_id}]：{msg}",
                lit_id, C.OP_STATUS_FAILED
            )
            return exception_to_code(exc), None, msg

    def parse_batch(self, lit_ids: list, rule_id: int = None,
                    progress_callback=None, degradation_callback=None) -> dict:
        """批量解析。

        Returns:
            {"success": [report_dict,...], "failed": [{"lit_id","msg"},...]}
        """
        result = {"success": [], "failed": []}
        total = len(lit_ids)

        def item_progress(index, lit_id):
            """适配单篇解析进度为批量整体进度。"""
            def callback(percent, message):
                """将单篇解析进度透传给上层回调。"""
                if progress_callback:
                    overall = int(((index + percent / 100) / total) * 100)
                    progress_callback(overall, f"[{index + 1}/{total}] {message}")
            return callback

        for index, lit_id in enumerate(lit_ids):
            code, data, msg = self.parse_single(
                lit_id, rule_id, item_progress(index, lit_id),
                degradation_callback=degradation_callback,
            )
            if code == C.CODE_SUCCESS:
                result["success"].append(data)
            else:
                result["failed"].append({"lit_id": lit_id, "msg": msg})
        return result

    # ================= 读取 =================

    def get_report(self, lit_id: int) -> dict:
        """获取文献最新报告（未解析返回 None）。

        返回视图按“当前默认解析规则”的维度开关过滤：规则中停用的维度
        字段统一置空，使界面折叠块与导出文件都不展示该维度；数据库中的
        原始报告内容保留，重新启用维度后可再次显示，无需重新解析。
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return None
        saved = self.report_dao.get_by_lit_id(lit_id)
        if not saved:
            return None
        view = self._compose_report_view(lit, saved)
        self._apply_default_dimensions(view)
        return view

    def get_default_dimensions(self) -> dict:
        """获取当前默认规则的维度开关（供解析页决定折叠块显隐）。"""
        return ParseRuleService().get_default_dimensions()

    @staticmethod
    def _apply_default_dimensions(report_view: dict) -> None:
        """按当前默认规则把停用维度在报告视图中置空（原地修改）。"""
        try:
            dimensions = ParseRuleService().get_default_dimensions()
        except Exception as exc:
            logger.warning("应用维度开关失败，按全部启用展示：%s", exc)
            return
        for field in DIMENSION_KEYS:
            if not dimensions.get(field, True):
                report_view[field] = ""

    def preview_text(self, lit_id: int) -> tuple:
        """提取文献原文供阅读区展示（UI 禁止直接调用 tool_layer）。

        Returns:
            (code, text, msg)
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return C.CODE_FILE_NOT_FOUND, "", "文献不存在"
        try:
            return C.CODE_SUCCESS, self._extract_lit_text(lit), ""
        except Exception as exc:
            return exception_to_code(exc), "", getattr(exc, "message", str(exc))

    def preview_file(self, lit_id: int) -> tuple:
        """返回文献原始文件的绝对路径与后缀，供 UI 按类型选择阅读器。

        Returns:
            (code, {"path": str, "suffix": str}, msg)
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        if not os.path.isfile(abs_path):
            from utils.exceptions import LiteratureFileNotFoundError
            exc = LiteratureFileNotFoundError("文献源文件已被移动或删除")
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))
        suffix = os.path.splitext(abs_path)[1].lower().lstrip(".")
        return C.CODE_SUCCESS, {"path": abs_path, "suffix": suffix}, ""

    def prepare_render_pdf(self, lit_id: int) -> tuple:
        """获取文献的 PDF 原貌渲染路径（PDF 直出，Word 本地转 PDF）。

        - 原始 PDF：直接返回受管存储中的绝对路径；
        - .doc/.docx：调用本机 Word/WPS/LibreOffice 转为 PDF，按文件哈希缓存
          到系统临时目录，同一文件再次打开不重复转换；
        - .txt 等不支持原版式渲染的格式返回 CODE_FILE_INVALID，由 UI 回退文本。

        Args:
            lit_id: 文献 id。
        Returns:
            (code, {"path": str, "suffix": str, "converted": bool}, msg)
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        if not os.path.isfile(abs_path):
            from utils.exceptions import LiteratureFileNotFoundError
            exc = LiteratureFileNotFoundError("文献源文件已被移动或删除")
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

        suffix = os.path.splitext(abs_path)[1].lower()
        if suffix == ".pdf":
            return C.CODE_SUCCESS, {
                "path": abs_path, "suffix": "pdf", "converted": False,
            }, ""
        if suffix not in C.WORD_RENDER_SUFFIX:
            return C.CODE_FILE_INVALID, None, f"{suffix} 格式不支持原版式渲染"

        try:
            file_hash = file_helper.calc_file_hash(abs_path)
            cache_dir = os.path.join(
                tempfile.gettempdir(), C.PDF_RENDER_CACHE_DIR)
            cached_pdf = os.path.join(cache_dir, f"{file_hash}.pdf")
            if os.path.isfile(cached_pdf) and os.path.getsize(cached_pdf) > 0:
                logger.info("命中 Word→PDF 渲染缓存：lit_id=%s", lit_id)
                return C.CODE_SUCCESS, {
                    "path": cached_pdf,
                    "suffix": suffix.lstrip("."),
                    "converted": True,
                }, ""

            # 全局串行转换；拿锁后二次检查缓存（另一线程可能刚转换完成）
            with _render_cache_lock:
                if os.path.isfile(cached_pdf) and os.path.getsize(cached_pdf) > 0:
                    logger.info("命中 Word→PDF 渲染缓存（锁内）：lit_id=%s", lit_id)
                    return C.CODE_SUCCESS, {
                        "path": cached_pdf,
                        "suffix": suffix.lstrip("."),
                        "converted": True,
                    }, ""
                os.makedirs(cache_dir, exist_ok=True)
                converted_path = file_parser.convert_document_to_pdf(
                    abs_path, cache_dir)
                if os.path.abspath(converted_path) != os.path.abspath(cached_pdf):
                    # 转换产物按原文件名生成，统一改名为哈希名避免中文/重名问题
                    os.replace(converted_path, cached_pdf)
            logger.info("Word→PDF 渲染完成并缓存：lit_id=%s", lit_id)
            return C.CODE_SUCCESS, {
                "path": cached_pdf,
                "suffix": suffix.lstrip("."),
                "converted": True,
            }, ""
        except Exception as exc:
            return (
                exception_to_code(exc),
                None,
                getattr(exc, "message", str(exc)),
            )

    # ================= 内部方法 =================

    def _try_agent_parse(self, lit: dict, rule_detail: dict, rule_id: int,
                         precision: int, emit, degradation_callback=None):
        """运行 ReAct + LangGraph 智能体完成单篇解析。

        - 存在可用 AI 模型（含已解密密钥）时传入，Agent 自主编排原件直传、
          全文文本与本地工具；未配置模型时 Agent 自动退化为纯本地工具链；
        - Agent 对致命失败（源文件缺失、提取不出文本）返回 error 状态，
          此处转成 None 交回本地兜底，沿用"标记解析失败"行为，
          绝不持久化一份空报告；
        - 正常完成（即使部分维度校验未通过但有本地基线）返回报告五元组，
          ai_error 携带首个致命 AI 错误原文（无错误时为 None）。

        Args:
            lit: 文献记录字典。
            rule_detail: 解析规则详情（维度开关等）。
            rule_id: 生效规则 id。
            precision: 解析精度 1-5。
            emit: 进度回调 emit(percent, message)。
            degradation_callback: 原件→文本降级前的用户授权回调。
        Returns:
            (report_dict, keywords_list, engine_str, warnings_list,
            ai_error_or_None)；致命失败时返回 None。
        """
        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(
            lit["file_path"], base_dir, "literature")

        model = SystemService().get_current_ai_model(decrypt=True)
        if not model or not model.get("base_url") or not model.get("api_key"):
            model = None  # 离线模式：Agent 只编排本地工具

        state = run_parse_agent(
            abs_path, rule_detail, precision=precision,
            model=model, lit_id=lit.get("id", 0), progress_callback=emit,
            degradation_callback=degradation_callback,
        )
        final_report = state.get("final_report")
        # 致命失败除显式 error 外，还包括三重校验的 fatal 判定（全文为空：
        # 如扫描件 OCR 全部失败且无文本层）。此时 final_report 仍是六维度
        # 全空的字典，不能据此落库一份"成功的空报告"，必须交回本地兜底
        # （本地同样提取失败时抛错 → 文献标记解析失败并提示用户）。
        verify_fatal = bool((state.get("verify_result") or {}).get("fatal"))
        if not final_report or state.get("error") or verify_fatal:
            return None

        dimensions_cfg = rule_detail.get("dimensions") or {}
        report = {"literature_id": lit["id"], "rule_id": rule_id}
        for field in _DIMENSION_FIELDS:
            report[field] = (
                final_report.get(field, "") if dimensions_cfg.get(field, True)
                else ""
            )
        keywords = [
            str(word).strip()
            for word in (final_report.get("keywords") or [])
            if str(word).strip()
        ][:C.PARSE_KEYWORD_TOP_N]
        engine = final_report.get("engine", "agent_local")
        warnings = list(final_report.get("warnings") or [])
        ai_error = str(final_report.get("ai_error") or "") or None
        return report, keywords, engine, warnings, ai_error

    def _run_local_fallback(self, lit: dict, rule_detail: dict, rule_id: int,
                            precision: int, emit):
        """纯本地规则兜底解析（Agent 致命异常时使用，不发起任何 AI 调用）。

        Args:
            lit: 文献记录字典。
            rule_detail: 解析规则详情（维度开关等）。
            rule_id: 生效规则 id。
            precision: 解析精度 1-5。
            emit: 进度回调 emit(percent, message)。
        Returns:
            (report_dict, keywords_list, "local")。
        """
        emit(15, "提取文献原文")
        text = self._extract_lit_text(lit)

        emit(55, "识别文献结构并整合成段语句")
        structure = text_analysis.detect_structure(text, precision)

        emit(75, "提取关键词")
        keywords = [
            word for word, _weight
            in text_analysis.extract_keywords(text, C.PARSE_KEYWORD_TOP_N)
        ]

        emit(85, "生成结构化报告")
        report = self._build_report(lit, structure, rule_detail, rule_id)
        return report, keywords, "local"

    def _load_rule(self, rule_id: int) -> tuple:
        """加载规则及其 detail JSON。

        Returns:
            (rule_dict, detail_dict)
        """
        rule = None
        if rule_id:
            rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            default_id = SystemConfigDao().get(C.CFG_PARSE_DEFAULT_RULE, "1")
            rule = self.rule_dao.get_by_id(int(default_id or 1))
        if not rule:
            rule = self.rule_dao.get_default()
        try:
            detail = json.loads(rule.get("rule_detail") or "{}")
        except (TypeError, ValueError):
            detail = {}
        detail.setdefault("dimensions", {})
        # 兼容清理：忽略历史规则中残留的关键词数量配置（该能力已下线）
        detail.pop("keyword_top_n", None)
        return rule, detail

    def _extract_lit_text(self, lit: dict) -> str:
        """根据库内相对路径还原文件并提取全文。"""
        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        if not os.path.isfile(abs_path):
            from utils.exceptions import LiteratureFileNotFoundError
            raise LiteratureFileNotFoundError("文献源文件已被移动或删除")
        text, _meta, _lit_type = file_parser.auto_extract(abs_path)
        return text

    @staticmethod
    def _build_report(lit: dict, structure: dict,
                      rule_detail: dict, rule_id: int) -> dict:
        """按规则维度开关组装报告字段（各维度均为成段完整语句）。"""
        dimensions = rule_detail.get("dimensions", {})
        report = {"literature_id": lit["id"], "rule_id": rule_id}
        for field in _DIMENSION_FIELDS:
            enabled = dimensions.get(field, True)
            report[field] = structure.get(field, "") if enabled else ""
        return report

    def _save_report(self, lit_id: int, report: dict) -> None:
        """存在报告则更新，否则插入（调用方持有写锁的语义由各 DAO 提交保证）。"""
        existing = self.report_dao.get_by_lit_id(lit_id)
        if existing:
            self.report_dao.update_by_lit_id(lit_id, report)
        else:
            self.report_dao.insert(report)

    def _mark_failed(self, lit_id: int) -> None:
        """解析失败时回写状态，异常不外抛。"""
        try:
            with DatabaseManager().write_lock:
                self.lit_dao.update_by_id(lit_id, {"is_parsed": C.PARSE_FAILED})
        except Exception as exc:
            logger.error("回写解析失败状态异常：%s", exc)

    @staticmethod
    def _compose_report_view(lit: dict, saved_report: dict) -> dict:
        """合并文献元信息、报告维度字段与附加关键词，供 UI 直接渲染。

        关键词随报告表以 JSON 数组字符串持久化，此处统一反序列化为
        list；历史脏数据/解析失败时退化为空列表，保证 UI 类型稳定。
        """
        view = dict(saved_report or {})
        raw_keywords = (saved_report or {}).get("keywords") or ""
        keywords = []
        if isinstance(raw_keywords, list):
            keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        elif isinstance(raw_keywords, str) and raw_keywords.strip():
            try:
                parsed = json.loads(raw_keywords)
                if isinstance(parsed, list):
                    keywords = [
                        str(k).strip() for k in parsed if str(k).strip()
                    ]
            except (ValueError, TypeError):
                logger.warning("报告关键词字段不是合法 JSON，已忽略：lit_id=%s",
                               lit.get("id"))
        view.update({
            "literature_id": lit["id"],
            "literature_title": lit.get("literature_title", ""),
            "literature_author": lit.get("literature_author", ""),
            "publish_time": lit.get("publish_time", ""),
            "journal_source": lit.get("journal_source", ""),
            "literature_type": lit.get("literature_type", ""),
            "keywords": keywords,
        })
        return view
