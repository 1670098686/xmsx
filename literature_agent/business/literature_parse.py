"""智能解析业务：支持原件的模型直传 AI 阅读解析，其余模型发送完整全文，AI 均不可用时回退本地规则解析。

提示词外置在 resources/prompts/ 目录（可直接打开检查/修改）：
- ai_system_prompt.txt：系统提示词；
- ai_parse_user_prompt.txt：单轮解析用户提示词模板；
- ai_map_user_prompt.txt / ai_reduce_user_prompt.txt：超长文献分块提炼与综合；
- ai_openings.json / ai_precision_guides.json：开场白与各精度档位说明。
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
from tool_layer import ai_client, file_helper, file_parser, text_analysis
from utils.exceptions import AIServiceError
from utils.logger import get_logger

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

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROMPT_DIR = os.path.join(_PROJECT_ROOT, "resources", "prompts")
_prompt_cache = {}

# 资源文件缺失时的兜底系统提示词（与 ai_system_prompt.txt 保持一致）
_SYSTEM_PROMPT_FALLBACK = (
    "你是一名严谨、专业的中文学术文献结构化分析助手。用户会向你提供文献的"
    "原始文件（PDF/DOC/DOCX/TXT），或从原件完整提取的全文文本；无论哪种形式，"
    "你都必须逐页、逐节通读全部内容（包括摘要、关键词、引言、方法、实验、"
    "讨论、结论与参考文献，以及表格、图注中的有效信息）后再作答，严禁仅凭"
    "文件名、标题或开头片段臆测。\n"
    "你的输出必须严格遵守以下规则：\n"
    "1. 只输出一个合法的 JSON 对象，不要输出任何解释、前后缀、注释或 "
    "Markdown 代码围栏；\n"
    "2. 忠于原件：所有事实、数据、年份、方法名、结论与数字必须以原件为唯一"
    "依据，严禁编造、补充或脑补原件中不存在的信息；\n"
    "3. 除参考文献与 keywords 外，每个字段都输出由意思完整、句末带句号的"
    "陈述句组成的段落，禁止用短语、词组或编号要点罗列代替；\n"
    "4. 输出语言与原件正文保持一致；原件为中文时一律使用简体中文。"
)


def load_system_prompt() -> str:
    """加载系统提示词（resources/prompts/ai_system_prompt.txt）。"""
    return _load_prompt_text("ai_system_prompt.txt", _SYSTEM_PROMPT_FALLBACK)


def _load_prompt_text(filename: str, fallback: str) -> str:
    """读取提示词文本文件并缓存；文件缺失/损坏时使用内置兜底文本。"""
    if filename not in _prompt_cache:
        path = os.path.join(_PROMPT_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                content = fp.read().strip()
            _prompt_cache[filename] = content or fallback
        except OSError as exc:
            logger.warning("提示词文件读取失败，使用内置兜底：%s | %s", path, exc)
            _prompt_cache[filename] = fallback
    return _prompt_cache[filename]


def _load_prompt_json(filename: str) -> dict:
    """读取提示词配套 JSON（开场白/精度档位），失败时返回空字典由调用方兜底。"""
    cache_key = f"json:{filename}"
    if cache_key not in _prompt_cache:
        path = os.path.join(_PROMPT_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            _prompt_cache[cache_key] = data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning("提示词配置读取失败：%s | %s", path, exc)
            _prompt_cache[cache_key] = {}
    return _prompt_cache[cache_key]


def _apply_dimension_switches(template: str, dimensions: dict,
                              disabled_text: str) -> str:
    """按规则维度开关替换提示词模板中被停用维度的字段行。

    Args:
        template: 含 ``- "field": ...`` 字段行的模板文本。
        dimensions: {field: bool} 维度开关。
        disabled_text: 维度停用时字段说明的替换文案（不含字段名前缀）。
    Returns:
        替换后的提示词文本。
    """
    lines = []
    for line in template.splitlines():
        replaced = False
        for field in _DIMENSION_FIELDS:
            if line.startswith(f'- "{field}":') and not dimensions.get(field, True):
                lines.append(f'- "{field}": {disabled_text}')
                replaced = True
                break
        if not replaced:
            lines.append(line)
    return "\n".join(lines)


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

            # 优先 AI 解析（支持文件的模型走原件直传，其余走全文文本；
            # 文件协议被拒且经用户允许后降级全文文本）；均失败才回退本地规则
            precision = int(rule.get("precision_level", 3))
            ai_result, ai_error = self._try_ai_parse(
                lit, rule_detail, active_rule_id, precision, emit,
                degradation_callback=degradation_callback,
            )
            if ai_result is not None:
                report, keywords, engine = ai_result
            else:
                engine = "local"
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

            write_operation_log(
                C.OP_PARSE, f"解析完成（{engine}）：{lit.get('literature_title', lit_id)}",
                lit_id
            )
            emit(100, "解析完成")
            msg = {
                "ai_file": "AI 原件解析完成",
                "ai_text": "AI 全文解析完成",
                "local": "解析完成（本地规则）",
            }.get(engine, "解析完成")
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

    # ================= AI 解析（原件直传 / 全文文本） =================

    @classmethod
    def _model_supports_file(cls, model_name: str, base_url: str) -> bool:
        """判断当前模型是否支持原件直传阅读（file_id 或 PDF Base64 内联）。

        Args:
            model_name: 模型名称。
            base_url: 接口根地址。
        Returns:
            True 表示具备任一原件通道；False 只能走全文文本模式。
        """
        return ai_client.model_supports_file(model_name, base_url)

    @staticmethod
    def _is_file_protocol_error(exc: AIServiceError) -> bool:
        """识别"模型不支持 file 类型消息"的 400 协议错误（委托工具层判定）。"""
        return ai_client.is_file_message_rejected(getattr(exc, "message", str(exc)))

    def _allow_text_fallback(self, lit: dict, error_message: str,
                             degradation_callback) -> bool:
        """就"原件通道失败、是否改用全文文本重试"征得用户允许并记录日志。

        Args:
            lit: 文献记录（用于日志关联 lit_id）。
            error_message: AI 返回的错误原文（弹窗中原样展示）。
            degradation_callback: UI 注入的授权回调；为空视为不允许。
        Returns:
            True 用户允许降级全文文本；False 拒绝（应回退本地解析）。
        """
        allowed = bool(degradation_callback) and \
            bool(degradation_callback(error_message))
        if allowed:
            logger.warning("原件通道失败（%s），经用户允许改用全文文本解析",
                           error_message)
            write_operation_log(
                C.OP_PARSE,
                f"原件通道失败，经用户允许改用全文文本：{error_message}",
                lit.get("id"), C.OP_STATUS_SUCCESS,
            )
        else:
            logger.warning("用户未允许全文文本降级，回退本地解析：%s",
                           error_message)
            write_operation_log(
                C.OP_PARSE,
                f"用户未允许降级全文文本，回退本地：{error_message}",
                lit.get("id"), C.OP_STATUS_SUCCESS,
            )
        return allowed

    def _try_ai_parse(self, lit: dict, rule_detail: dict, rule_id: int,
                      precision: int, emit, degradation_callback=None):
        """按模型配置的原件通道选择 AI 解析方式，失败时安全回退。

        通道取自当前模型持久化配置 file_channel（auto/file_id/file_data/none），
        因此任意支持相应协议的 PDF 模型都可直传本地原件，不再依赖模型名硬编码：
        - file_id：原件经 /files 上传，模型通读原件（qwen-long/Kimi 等）；
        - file_data：PDF 原件本地 Base64 内联直传，无需托管/公网 URL
          （qwen3.8 系列及兼容该格式的模型）；非 PDF（docx/txt）走全文文本；
        - none：本地完整提取全文后作为文本消息发送；
        - auto：按模型名关键字预判通道；未知模型保守走全文文本
          （用户可在 AI 配置页校验自动识别后保存为显式通道）；
        - 原件通道失败（协议不支持、PDF 过大等）时，必须经
          degradation_callback 征得用户同意才改用全文文本通道；
        - 未配置模型或 AI 通道均失败时回退本地解析，
          并返回面向用户的 AI 错误信息供界面弹窗说明。

        Args:
            lit: 文献记录字典。
            rule_detail: 解析规则详情（维度开关等）。
            rule_id: 生效规则 id。
            precision: 解析精度 1-5。
            emit: 进度回调 emit(percent, message)。
            degradation_callback: 可选授权回调 callback(error_message)->bool。
        Returns:
            ((report_dict, keywords_list, engine) 或 None, ai_error 或 None)。
        """
        model = SystemService().get_current_ai_model(decrypt=True)
        if not model or not model.get("base_url") or not model.get("api_key"):
            return None, None

        base_dir = get_base_dir("literature")
        abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        if not os.path.isfile(abs_path):
            # 文件缺失交给本地路径统一抛业务异常
            return None, None

        model_name = model.get("name") or ""
        base_url = model["base_url"]
        api_key = model["api_key"]
        configured = model.get("file_channel") or C.AI_FILE_CHANNEL_AUTO
        # 显式通道按用户配置走（其他 PDF 模型由此获得支持）；
        # auto 仍按名称启发式预判，解析流程中不额外发起探测网络请求
        if configured == C.AI_FILE_CHANNEL_AUTO:
            mode = ai_client.file_channel_mode(model_name, base_url)
        else:
            mode = configured
        try:
            if mode == ai_client.FILE_CHANNEL_ID:
                try:
                    result = self._ai_parse_with_file(
                        lit, rule_detail, rule_id, precision, emit,
                        base_url, api_key, model_name, abs_path,
                    )
                    return result, None
                except AIServiceError as exc:
                    if not self._is_file_protocol_error(exc):
                        raise
                    if not self._allow_text_fallback(
                        lit, exc.message, degradation_callback
                    ):
                        return None, (
                            f"AI 无法直接读取文献原件（{exc.message}），"
                            "且未获得改用全文文本重试的许可，已使用本地规则完成解析。"
                        )
            elif mode == ai_client.FILE_CHANNEL_DATA:
                # PDF Base64 内联原件通道（qwen3.8 或其他兼容模型）：
                # 仅 PDF 走直传；docx/txt 没有服务端 PDF 版式需求，走全文文本
                if os.path.splitext(abs_path)[1].lower() == ".pdf":
                    try:
                        result = self._ai_parse_with_pdf_inline(
                            lit, rule_detail, rule_id, precision, emit,
                            base_url, api_key, model_name, abs_path,
                        )
                        return result, None
                    except AIServiceError as exc:
                        if not self._allow_text_fallback(
                            lit, exc.message, degradation_callback
                        ):
                            return None, (
                                f"AI 无法直接读取 PDF 原件（{exc.message}），"
                                "且未获得改用全文文本重试的许可，已使用本地规则完成解析。"
                            )
            result = self._ai_parse_with_text(
                lit, rule_detail, rule_id, precision, emit,
                base_url, api_key, model_name, abs_path,
            )
            return result, None
        except AIServiceError as exc:
            logger.warning("AI 解析失败，回退本地解析：%s", exc.message)
            write_operation_log(
                C.OP_PARSE,
                f"AI 解析不可用已回退本地：{exc.message}",
                lit.get("id"), C.OP_STATUS_SUCCESS,
            )
            return None, f"AI 模型调用失败：{exc.message}"

    def _ai_parse_with_file(self, lit, rule_detail, rule_id, precision, emit,
                            base_url, api_key, model_name, abs_path):
        """file_id 通道：/files 上传原件 → 模型通读原件 → 结构化 JSON。

        Returns:
            (report_dict, keywords_list, "ai_file")
        """
        emit(15, "上传文献原件至 AI 模型")
        file_id = ai_client.upload_file(base_url, api_key, abs_path)

        emit(55, "AI 正在通读文献原件并分维度解析")
        prompt = self._build_ai_prompt(rule_detail, precision, file_mode=True)
        content = ai_client.chat_with_file(
            base_url, api_key, model_name, file_id, prompt, load_system_prompt(),
        )
        emit(85, "整理 AI 解析结果")
        report, keywords = self._assemble_ai_report(
            ai_client.extract_json_block(content), lit, rule_detail, rule_id
        )
        return report, keywords, "ai_file"

    def _ai_parse_with_pdf_inline(self, lit, rule_detail, rule_id, precision, emit,
                                  base_url, api_key, model_name, abs_path):
        """file_data 通道（PDF Base64 内联理解）：本地 PDF Base64 内联直传。

        文件只随本次请求发送给用户配置的接口，不经过 /files 文件托管、
        不需要公网 URL；模型通读 PDF 原件后返回结构化 JSON。

        Returns:
            (report_dict, keywords_list, "ai_file")
        """
        emit(15, "读取 PDF 原件并本地编码（不上传文件托管）")
        emit(55, "AI 正在通读 PDF 原件并分维度解析")
        prompt = self._build_ai_prompt(rule_detail, precision, file_mode=True)
        content = ai_client.chat_with_local_pdf(
            base_url, api_key, model_name, abs_path, prompt, load_system_prompt(),
        )
        emit(85, "整理 AI 解析结果")
        report, keywords = self._assemble_ai_report(
            ai_client.extract_json_block(content), lit, rule_detail, rule_id
        )
        return report, keywords, "ai_file"

    def _ai_parse_with_text(self, lit, rule_detail, rule_id, precision, emit,
                            base_url, api_key, model_name, abs_path):
        """全文文本通道：本地完整提取全文 → 模型通读全文 → 结构化 JSON。

        全文较短时单轮完成；超过上下文安全阈值时按重叠分块逐段提炼，
        再统一综合，保证模型覆盖文献全文而非只看开头。

        Returns:
            (report_dict, keywords_list, "ai_text")
        """
        emit(15, "提取文献完整原文")
        full_text = self._extract_lit_text(lit)
        if not full_text or not full_text.strip():
            raise AIServiceError("文献未能提取出文本（可能是无文本层的扫描件）")

        if len(full_text) <= C.AI_INLINE_FULL_CHARS:
            emit(55, "AI 正在通读文献全文并分维度解析")
            prompt = self._build_ai_prompt(rule_detail, precision, file_mode=False)
            content = ai_client.chat_with_text(
                base_url, api_key, model_name, full_text, prompt,
                load_system_prompt(),
            )
            emit(85, "整理 AI 解析结果")
            report, keywords = self._assemble_ai_report(
                ai_client.extract_json_block(content), lit, rule_detail, rule_id
            )
            return report, keywords, "ai_text"

        # 超长文献：分块提炼 → 汇总综合
        chunks = self._split_text_chunks(full_text)
        if len(chunks) > C.AI_INLINE_MAX_CHUNKS:
            raise AIServiceError(
                f"文献全文过长（{len(chunks)} 个片段），超出单次解析上限，"
                "请换用支持原件直传的长文本模型（如 qwen-long）"
            )
        materials = []
        map_prompt = self._build_ai_map_prompt(rule_detail)
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            percent = 20 + int(55 * (index + 1) / total)
            emit(percent, f"AI 通读全文片段 {index + 1}/{total}")
            part = ai_client.chat_with_text(
                base_url, api_key, model_name, chunk, map_prompt,
                load_system_prompt(),
            )
            materials.append(f"【片段{index + 1}/{total}】\n{part.strip()}")

        emit(80, "AI 综合全文片段生成结构化报告")
        reduce_prompt = self._build_ai_reduce_prompt(rule_detail, precision)
        merged = "\n\n".join(materials)
        content = ai_client.chat_with_text(
            base_url, api_key, model_name, merged, reduce_prompt,
            load_system_prompt(),
        )
        emit(85, "整理 AI 解析结果")
        report, keywords = self._assemble_ai_report(
            ai_client.extract_json_block(content), lit, rule_detail, rule_id
        )
        return report, keywords, "ai_text"

    @staticmethod
    def _split_text_chunks(full_text: str) -> list:
        """按字符上限把全文切成带重叠的连续片段（尽量在换行处切分）。

        Returns:
            文本片段列表。
        """
        size = C.AI_INLINE_CHUNK_CHARS
        overlap = C.AI_INLINE_CHUNK_OVERLAP
        chunks = []
        start = 0
        total = len(full_text)
        while start < total:
            end = min(start + size, total)
            # 非末尾片段：在切点附近找换行，避免把一句话切成两半
            if end < total:
                window = full_text[start:end]
                break_pos = window.rfind("\n")
                if break_pos >= int(size * 0.7):
                    end = start + break_pos + 1
            chunks.append(full_text[start:end].strip())
            if end >= total:
                break
            start = max(0, end - overlap)
        return [chunk for chunk in chunks if chunk]

    def _assemble_ai_report(self, data: dict, lit: dict,
                            rule_detail: dict, rule_id: int) -> tuple:
        """把 AI 返回的 JSON 归一化为 (report_dict, keywords_list)。

        各维度统一过章节语句归一化（合并硬换行、参考文献逐条保留），
        与本地解析共用同一出口。
        """
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
                report[field] = text_analysis.normalize_section_text(field, raw)
            else:
                report[field] = ""
        return report, keywords

    # 开场白/精度档位 JSON 缺失时的内置兜底（与 resources/prompts 中保持一致）
    _OPENING_FALLBACK = {
        "file": "请完整通读我上传的文献原始文件（含封面、摘要、引言、正文各节、"
                "结论与参考文献，逐页阅读不得跳读），依据原件全文完成结构化解析。要求：",
        "text": "以下提供了从文献原件中完整提取的全文文本。请先通读全文"
                "（含摘要、引言、正文各节、结论与参考文献，不得只看开头），"
                "再依据全文内容完成结构化解析。要求：",
    }
    _PRECISION_FALLBACK = {
        "1": "解析精度要求：最快档，每项用 1-2 句完整句子概括最核心内容。",
        "2": "解析精度要求：快速档，每项用 2-4 句完整句子简明陈述。",
        "3": "解析精度要求：均衡档，按各项标注字数用完整句子成段作答。",
        "4": "解析精度要求：精细档，在忠于原件的前提下用完整句子展开，每项 300-400 字。",
        "5": "解析精度要求：最精细档，用完整句子全面覆盖论证细节与数据，每项 400-600 字。",
    }

    @classmethod
    def _build_ai_prompt(cls, rule_detail: dict, precision: int = 3,
                         file_mode: bool = True) -> str:
        """按启用维度与解析精度构造 AI 解析指令（模板：resources/prompts/ai_parse_user_prompt.txt）。

        六个内容维度一律要求输出完整陈述句组成的段落，禁止以短语/要点
        罗列替代语句；关键词只允许出现在独立的 keywords 附加字段中；
        参考文献维度例外，按原件逐条列出。

        Args:
            rule_detail: 规则明细（维度开关等）。
            precision: 解析精度 1-5，低档求快、高档求详尽。
            file_mode: True 为原件直传通道开场白；False 为全文文本通道。
        Returns:
            拼装完成的提示词字符串。
        """
        dimensions = rule_detail.get("dimensions", {})
        openings = _load_prompt_json("ai_openings.json") or cls._OPENING_FALLBACK
        opening = openings.get(
            "file" if file_mode else "text", cls._OPENING_FALLBACK["file"]
            if file_mode else cls._OPENING_FALLBACK["text"]
        )
        guides = _load_prompt_json("ai_precision_guides.json") \
            or cls._PRECISION_FALLBACK
        precision_guide = guides.get(
            str(precision), cls._PRECISION_FALLBACK["3"]
        )

        template = _load_prompt_text("ai_parse_user_prompt.txt", "")
        if not template:
            # 模板文件缺失：用开场白+精度说明组成最小可用提示词
            template = "【开场白】\n【精度要求】"
        lines = []
        for line in template.splitlines():
            if line.strip() == "【开场白】":
                lines.append(opening)
            elif line.strip() == "【精度要求】":
                lines.append(precision_guide)
            else:
                lines.append(line)
        return _apply_dimension_switches(
            "\n".join(lines), dimensions, '该维度未启用，填空字符串 ""'
        )

    @classmethod
    def _build_ai_map_prompt(cls, rule_detail: dict) -> str:
        """超长文献分块提炼阶段提示词（模板：resources/prompts/ai_map_user_prompt.txt）。"""
        dimensions = rule_detail.get("dimensions", {})
        template = _load_prompt_text(
            "ai_map_user_prompt.txt",
            "以下是文献全文的其中一个片段。请只依据本片段内容摘录完整句子素材。\n"
            "严格只输出 JSON 对象，字段如下：",
        )
        return _apply_dimension_switches(
            template, dimensions, "该维度未启用，固定给 []"
        )

    @classmethod
    def _build_ai_reduce_prompt(cls, rule_detail: dict, precision: int) -> str:
        """超长文献综合阶段提示词（模板：resources/prompts/ai_reduce_user_prompt.txt）。"""
        dimensions = rule_detail.get("dimensions", {})
        template = _load_prompt_text(
            "ai_reduce_user_prompt.txt",
            "以下是同一篇文献各全文片段的结构化素材。请综合全部素材整合成最终解析结果。\n"
            "严格只输出 JSON 对象，字段如下：",
        )
        # 模板中字段说明行按维度开关替换（precision 参数保留用于未来档位扩展）
        _ = precision
        return _apply_dimension_switches(
            template, dimensions, '该维度未启用，填空字符串 ""'
        )

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
