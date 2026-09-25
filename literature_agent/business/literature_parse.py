"""智能解析业务：优先将文献原件直传 AI 模型阅读解析，未配置/失败时回退本地规则解析。"""
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
from tool_layer import ai_client, file_helper, file_parser, text_analysis
from utils.exceptions import AIServiceError
from utils.logger import get_logger

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

# AI 系统提示词：要求直接阅读原始文件并严格输出 JSON
_AI_SYSTEM_PROMPT = (
    "你是专业的中文学术文献分析助手。用户会直接提供文献的原始文件（PDF/Word/TXT），"
    "你必须阅读原件全文内容后作答，不得依赖文件名猜测，不得要求用户粘贴文本。"
    "严格只输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码围栏。"
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
                     progress_callback=None) -> tuple:
        """解析单篇文献并写入结构化报告。

        Args:
            lit_id: 文献 id。
            rule_id: 指定解析规则 id；不传则取系统默认规则。
            progress_callback: 可选回调 callback(percent:int, message:str)。
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

            # 优先 AI 原件直传解析；未配置模型或调用失败时回退本地解析
            precision = int(rule.get("precision_level", 3))
            ai_result = self._try_ai_parse(
                lit, rule_detail, active_rule_id, precision, emit
            )
            if ai_result is not None:
                report, keywords = ai_result
                used_ai = True
            else:
                used_ai = False
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
                report = self._build_report(lit, structure,
                                            rule_detail, active_rule_id)

            emit(95, "写入解析报告")
            self._save_report(lit_id, report)

            with DatabaseManager().write_lock:
                self.lit_dao.update_by_id(lit_id, {"is_parsed": C.PARSE_DONE})

            saved = self.report_dao.get_by_lit_id(lit_id)
            report_view = self._compose_report_view(lit, saved, keywords)

            write_operation_log(
                C.OP_PARSE, f"解析完成：{lit.get('literature_title', lit_id)}", lit_id
            )
            emit(100, "解析完成")
            msg = "AI 原件解析完成" if used_ai else "解析完成（本地规则）"
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
                progress_callback=None) -> tuple:
        """重新解析（覆盖历史报告，先删旧报告再解析）。

        Returns:
            (code, report_dict, msg)
        """
        try:
            with DatabaseManager().write_lock:
                self.report_dao.delete_by_lit_id(lit_id)
        except Exception as exc:
            logger.warning("清理旧报告失败：%s", exc)
        return self.parse_single(lit_id, rule_id, progress_callback)

    def parse_batch(self, lit_ids: list, rule_id: int = None,
                    progress_callback=None) -> dict:
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
                lit_id, rule_id, item_progress(index, lit_id)
            )
            if code == C.CODE_SUCCESS:
                result["success"].append(data)
            else:
                result["failed"].append({"lit_id": lit_id, "msg": msg})
        return result

    # ================= 读取 =================

    def get_report(self, lit_id: int) -> dict:
        """获取文献最新报告（未解析返回 None）。"""
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return None
        saved = self.report_dao.get_by_lit_id(lit_id)
        if not saved:
            return None
        return self._compose_report_view(lit, saved, [])

    def list_parsed_lit_ids(self) -> list:
        """返回已完成解析的文献 id 列表（规则更新后批量重解析用）。"""
        return [
            lit["id"] for lit in self.lit_dao.select_all()
            if lit.get("is_parsed") == C.PARSE_DONE
        ]

    def is_report_stale(self, lit_id: int) -> bool:
        """判断某文献的解析报告是否早于默认规则的最近修改时间。

        Args:
            lit_id: 文献 id。
        Returns:
            True 表示默认规则模板更新于报告生成之后，建议重新解析；
            无报告、未记录规则版本或时间戳异常时返回 False。
        """
        version = SystemConfigDao().get(C.CFG_PARSE_RULE_VERSION, "")
        if not version:
            return False
        report = self.report_dao.get_by_lit_id(lit_id)
        if not report or not report.get("parse_time"):
            return False
        try:
            # parse_time 与规则版本均为 UTC "YYYY-MM-DD HH:MM:SS" 定长文本，可按字典序比较
            return str(report["parse_time"]).strip() < str(version).strip()
        except Exception:
            logger.warning("报告/规则时间戳比较失败：lit_id=%s", lit_id)
            return False

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

    # ================= AI 原件直传解析 =================

    def _try_ai_parse(self, lit: dict, rule_detail: dict, rule_id: int,
                      precision: int, emit):
        """尝试将文献原件直传当前 AI 模型解析。

        原件（PDF/Word/TXT 二进制文件）整体上传给模型，由模型直接阅读，
        不做任何本地文本提取。未配置模型或调用失败时返回 None，
        由调用方回退本地规则解析，保证离线/弱网环境仍可使用。

        Args:
            lit: 文献记录字典。
            rule_detail: 解析规则详情（维度开关等）。
            rule_id: 生效规则 id。
            precision: 解析精度 1-5。
            emit: 进度回调 emit(percent, message)。
        Returns:
            (report_dict, keywords_list)：各维度均为成段完整语句，
            keywords 为附加关键词列表；未走 AI 时返回 None。
        """
        model = SystemService().get_current_ai_model(decrypt=True)
        if not model or not model.get("base_url") or not model.get("api_key"):
            return None

        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        if not os.path.isfile(abs_path):
            # 文件缺失交给本地路径统一抛业务异常
            return None

        try:
            emit(15, "上传文献原件至 AI 模型")
            file_id = ai_client.upload_file(
                model["base_url"], model["api_key"], abs_path,
            )

            emit(55, "AI 正在阅读原件并生成结构化报告")
            prompt = self._build_ai_prompt(rule_detail, int(precision))
            content = ai_client.chat_with_file(
                model["base_url"], model["api_key"], model["name"],
                file_id, prompt, _AI_SYSTEM_PROMPT,
            )
            data = ai_client.extract_json_block(content)

            emit(85, "整理 AI 解析结果")
            raw_keywords = data.get("keywords") or []
            if isinstance(raw_keywords, str):
                raw_keywords = [
                    k.strip() for k in raw_keywords.replace("，", ",").split(",")
                ]
            keywords = [
                str(k).strip() for k in raw_keywords if str(k).strip()
            ][:C.PARSE_KEYWORD_TOP_N]

            dimensions = rule_detail.get("dimensions", {})
            report = {"literature_id": lit["id"], "rule_id": rule_id}
            for field in _DIMENSION_FIELDS:
                if dimensions.get(field, True):
                    value = data.get(field, "")
                    raw = value.strip() if isinstance(value, str) else str(value).strip()
                    # 与本地解析同一出口：合并硬换行、整理为成段语句/逐条文献
                    report[field] = text_analysis.normalize_section_text(field, raw)
                else:
                    report[field] = ""
            return report, keywords
        except AIServiceError as exc:
            logger.warning("AI 原件解析失败，回退本地解析：%s", exc.message)
            write_operation_log(
                C.OP_PARSE,
                f"AI 解析不可用已回退本地：{exc.message}",
                lit.get("id"), C.OP_STATUS_SUCCESS,
            )
            return None

    @staticmethod
    def _build_ai_prompt(rule_detail: dict, precision: int = 3) -> str:
        """按启用维度与解析精度构造 AI 解析指令（要求严格 JSON 输出）。

        六个内容维度一律要求输出完整陈述句组成的段落，禁止以短语/要点
        罗列替代语句；关键词只允许出现在独立的 keywords 附加字段中；
        参考文献维度例外，按原件逐条列出。

        Args:
            rule_detail: 规则明细（维度开关等）。
            precision: 解析精度 1-5，低档求快、高档求详尽。
        Returns:
            拼装完成的提示词字符串。
        """
        dimensions = rule_detail.get("dimensions", {})
        field_specs = {
            "research_background": "研究背景：用完整语句陈述选题背景与要解决的问题，150-300字",
            "core_view": "核心观点：用完整语句陈述作者的主要论点与依据，150-300字",
            "research_method": "研究方法：用完整语句说明采用的研究/分析方法与过程，100-200字",
            "innovation_point": "创新点：用完整语句陈述文章的创新之处，100-200字",
            "research_conclusion": "研究结论：用完整语句总结最终结论与建议，150-300字",
            "reference_list": "参考文献：按原件文末原样逐条列出，每条单独一行",
        }
        precision_guides = {
            1: "解析精度要求：最快档，每项用 1-2 句完整句子概括最核心内容。",
            2: "解析精度要求：快速档，每项用 2-4 句完整句子简明陈述。",
            3: "解析精度要求：均衡档，按各项标注字数用完整句子成段作答。",
            4: "解析精度要求：精细档，在忠于原件的前提下用完整句子展开，每项 300-400 字。",
            5: "解析精度要求：最精细档，用完整句子全面覆盖论证细节与数据，每项 400-600 字。",
        }
        lines = [
            "请直接阅读我上传的文献原始文件全文，依据原件内容完成结构化解析。要求：",
            precision_guides.get(precision, precision_guides[3]),
            "除参考文献与 keywords 外，每个维度字段都必须是一段或多段意思"
            "完整的陈述句（句子主谓宾完整、句末使用句号），严禁用短语、"
            "词组罗列或编号要点清单代替完整句子。",
        ]
        for field in _DIMENSION_FIELDS:
            if dimensions.get(field, True):
                lines.append(f'- "{field}": {field_specs[field]}')
            else:
                lines.append(f'- "{field}": 该维度未启用，填空字符串 ""')
        lines.append('- "keywords": 附加信息，从原件中提炼 5-15 个关键词的字符串数组，'
                     '该字段独立存在，不得用它替代上面任何维度的语句段落')
        lines.append("所有内容必须使用与原件一致的语言；未在原件中出现的信息不要编造。")
        lines.append("只输出 JSON 对象，示例："
                     '{"research_background":"...","core_view":"...",'
                     '"research_method":"...","innovation_point":"...",'
                     '"research_conclusion":"...","reference_list":"...",'
                     '"keywords":["..."]}')
        return "\n".join(lines)

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
    def _compose_report_view(lit: dict, saved_report: dict,
                             keywords: list = None) -> dict:
        """合并文献元信息、报告维度字段与附加关键词，供 UI 直接渲染。

        关键词仅作附加信息（不入库、不参与维度内容），因此读取历史报告
        时传入空列表。
        """
        view = dict(saved_report or {})
        view.update({
            "literature_id": lit["id"],
            "literature_title": lit.get("literature_title", ""),
            "literature_author": lit.get("literature_author", ""),
            "publish_time": lit.get("publish_time", ""),
            "journal_source": lit.get("journal_source", ""),
            "literature_type": lit.get("literature_type", ""),
            "keywords": keywords or [],
        })
        return view
