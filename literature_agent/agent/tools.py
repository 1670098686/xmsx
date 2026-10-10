"""Agent 工具池：把 tool_layer 的成熟能力包装为 LangChain BaseTool。

- 工具为纯函数，不读取业务状态、不访问数据库；
- AI 工具所需的模型配置（含已解密密钥）通过 build_tool_registry 闭包注入，
  避免 agent 层反向依赖 business 层；
- 返回值全部为 JSON 可序列化的 dict/list，便于写入 tool_history 与喂给 LLM。
"""
import json
import os
import re

from langchain_core.tools import BaseTool, tool

from agent import prompts
from agent.llm import sanitize_tool_call
from agent.state import (
    DIMENSION_KEYS,
    OCR_MODE_AUTO,
    OCR_MODE_AI,
    OCR_MODE_LOCAL,
    OCR_ENGINE_AI,
    OCR_ENGINE_LOCAL,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_AI_READ_ORIGINAL,
    TOOL_DETECT_STRUCTURE,
    TOOL_EXTRACT_KEYWORDS,
    TOOL_EXTRACT_TABLES,
    TOOL_EXTRACT_TEXT,
    TOOL_INSPECT_DOCUMENT,
    TOOL_OCR_PAGE,
)
from config import constants as C
from tool_layer import ai_client, document_vision, file_parser, text_analysis
from utils.exceptions import LiteratureAgentError
from utils.logger import get_logger

logger = get_logger()


def build_tool_registry(model: dict = None, on_progress=None) -> dict[str, BaseTool]:
    """构造工具名 → BaseTool 映射。

    Args:
        model: 当前 AI 模型配置 {"name","base_url","api_key",...}；None 时
            AI 工具仍可被调用，但会返回 ok=False（规划器据此跳过 AI）。
        on_progress: 可选进度回调 on_progress(message:str)，超长文献
            map-reduce 分块解读时逐块回调，由 Act 节点转发到进度条。
    Returns:
        {工具名: StructuredTool}
    """

    def _progress(message: str) -> None:
        """安全转发分块进度；回调异常不影响解析。"""
        if on_progress is None:
            return
        try:
            on_progress(str(message))
        except Exception:
            pass

    def _wait_hint(scene: str):
        """构造 AI 请求等待心跳回调；未配置进度回调时返回 None（不启心跳）。

        Args:
            scene: 当前等待场景描述（如"AI 正在通读文献原件"）。
        Returns:
            (elapsed_seconds:int)->None 回调，周期提示"仍在思考中"，
            让长文档 1-5 分钟的同步等待在进度条上可见、不被误认为卡死。
        """
        if on_progress is None:
            return None

        def _on_waiting(seconds: int) -> None:
            try:
                on_progress(
                    f"{scene}（已等待 {int(seconds)} 秒，"
                    "长文档 AI 解读通常需要 1-5 分钟，请耐心等待）…")
            except Exception:
                pass

        return _on_waiting

    @tool(TOOL_INSPECT_DOCUMENT)
    def inspect_document(file_path: str,
                         sample_pages: int = C.AGENT_INSPECT_SAMPLE_PAGES) -> dict:
        """抽样检查原件布局（毫秒级、非整页 OCR），感知扫描件/双栏/图表页，
        让后续提取策略由布局画像自适应决定。仅 PDF 做坐标分析；
        TXT/DOCX/DOC 直接返回普通文本布局。

        Args:
            file_path: 文献原件的本地绝对路径。
            sample_pages: 抽样页数上限（默认前 5 页）。
        Returns:
            {"ok":bool,"layout":{布局摘要},"error":str}
        """
        try:
            layout = document_vision.inspect_document_layout(
                file_path, int(sample_pages))
            return {"ok": True, "layout": layout}
        except LiteratureAgentError as exc:
            return {"ok": False, "layout": {}, "error": exc.message}

    @tool(TOOL_OCR_PAGE)
    def ocr_page(file_path: str, page_index: int,
                 mode: str = OCR_MODE_AUTO) -> dict:
        """对 PDF 单页渲染截图后做文字识别（扫描件/图表页通道）。
        双轨：配置了视觉模型时走 AI 多模态，否则走本地 Tesseract；
        auto 模式首选通道失败时自动尝试另一轨，两轨都不可用返回 ok=False。

        Args:
            file_path: PDF 原件的本地绝对路径。
            page_index: 页码索引（从 0 开始）。
            mode: auto（默认，自动选轨）| local（强制 Tesseract）| ai（强制视觉模型）。
        Returns:
            {"ok":bool,"text":str,"engine":str,"confidence":float,
             "page_index":int,"error":str}
        """
        try:
            image_bytes = document_vision.render_page_png(file_path, int(page_index))
        except LiteratureAgentError as exc:
            return {"ok": False, "text": "", "engine": "", "confidence": 0.0,
                    "page_index": int(page_index), "error": exc.message}

        use_mode = mode if mode in (OCR_MODE_AUTO, OCR_MODE_LOCAL,
                                    OCR_MODE_AI) else OCR_MODE_AUTO
        vision_ready = bool(
            model and model.get("base_url") and model.get("api_key")
            and (model.get("vision_enabled") or model.get("supports_vision"))
        )
        prompt = prompts.build_vision_ocr_prompt(page_index=int(page_index))

        # auto：视觉模型可用时优先 AI（中文准确率更高），失败再降级本地
        if use_mode in (OCR_MODE_AI, OCR_MODE_AUTO) and vision_ready:
            try:
                content = ai_client.chat_with_vision(
                    model["base_url"], model["api_key"], model.get("name", ""),
                    image_bytes, prompt,
                    on_waiting=_wait_hint(
                        f"AI 视觉模型正在识别第 {int(page_index) + 1} 页内容"),
                )
                return {"ok": True, "text": (content or "").strip(),
                        "engine": OCR_ENGINE_AI, "confidence": 0.0,
                        "page_index": int(page_index), "error": ""}
            except LiteratureAgentError as exc:
                if use_mode == OCR_MODE_AI:
                    return {"ok": False, "text": "", "engine": "",
                            "confidence": 0.0, "page_index": int(page_index),
                            "error": exc.message}
                logger.info("AI 视觉识别第 %s 页失败，降级本地 OCR：%s",
                            page_index, exc.message)

        if use_mode in (OCR_MODE_LOCAL, OCR_MODE_AUTO):
            try:
                text, confidence = document_vision.ocr_image_local(image_bytes)
                return {"ok": True, "text": text, "engine": OCR_ENGINE_LOCAL,
                        "confidence": confidence, "page_index": int(page_index),
                        "error": ""}
            except LiteratureAgentError as exc:
                return {"ok": False, "text": "", "engine": "",
                        "confidence": 0.0, "page_index": int(page_index),
                        "error": exc.message}

        # mode=ai 但模型未配置/未启用视觉
        return {"ok": False, "text": "", "engine": "", "confidence": 0.0,
                "page_index": int(page_index),
                "error": "当前 AI 模型未启用视觉解析，无法进行页面识别"}

    @tool(TOOL_EXTRACT_TEXT)
    def extract_text(file_path: str) -> dict:
        """提取文献全文。支持 PDF/TXT/DOCX/DOC，自动清理 PDF 抽取噪声。

        Args:
            file_path: 文献原件的本地绝对路径。
        Returns:
            {"ok":bool,"text":str,"char_count":int,"file_type":str,"error":str}
        """
        try:
            text, _meta, lit_type = file_parser.auto_extract(file_path)
            return {
                "ok": True,
                "text": text or "",
                "char_count": len(text or ""),
                "file_type": lit_type,
            }
        except LiteratureAgentError as exc:
            return {"ok": False, "text": "", "char_count": 0,
                    "file_type": "", "error": exc.message}

    @tool(TOOL_EXTRACT_TABLES)
    def extract_tables(file_path: str, page_index: int = -1,
                       max_tables: int = C.AGENT_TABLE_MAX_DEFAULT) -> dict:
        """提取文献中的表格并转换为 Markdown，保留行列结构与单元格数据。

        PDF 按页识别线框/无框表格，DOCX 提取文档内嵌表格；通读原件后若某
        维度依赖具体表格数据（实验数据、指标对比、调查结果等），调用本工具
        取得准确的行列内容再补全该维度。TXT 与旧版 DOC 无表格返回空结果。

        Args:
            file_path: 文献原件的本地绝对路径（由 Agent 状态自动注入）。
            page_index: PDF 指定页码索引（从 0 开始）；-1 表示全部页；
                对 DOCX/TXT 无效。
            max_tables: 返回表格数量上限。
        Returns:
            {"ok":bool,"tables":[{"page":int|"docx","index":int,
            "markdown":str}],"table_pages":[int],"error":str}
        """
        try:
            result = file_parser.extract_tables_markdown(
                file_path, int(page_index), int(max_tables))
            return {"ok": True,
                    "tables": result.get("tables", []),
                    "table_pages": result.get("table_pages", [])}
        except LiteratureAgentError as exc:
            return {"ok": False, "tables": [], "table_pages": [],
                    "error": exc.message}

    @tool(TOOL_DETECT_STRUCTURE)
    def detect_structure(text: str, precision: int = 3) -> dict:
        """用本地规则按章节标题识别六个维度（研究背景/核心观点/研究方法/
        创新点/研究结论/参考文献），不调用大模型、结果确定可复现。

        Args:
            text: 文献全文。
            precision: 解析精度 1-5，越高兜底补全越充分、耗时越长。
        Returns:
            {"ok":bool,"structure":{六个维度:原文段落},
             "section_paragraphs":{维度:[全局段落号,...]},"error":str}
        """
        try:
            structure, section_paragraphs = \
                text_analysis.detect_structure_with_anchors(
                    text or "", int(precision))
            return {"ok": True, "structure": structure,
                    "section_paragraphs": section_paragraphs}
        except Exception as exc:  # 工具层兜底：异常转为观测结果而非中断图
            logger.warning("Agent 工具 detect_structure 失败：%s", exc)
            return {"ok": False, "structure": {},
                    "section_paragraphs": {}, "error": str(exc)}

    @tool(TOOL_EXTRACT_KEYWORDS)
    def extract_keywords(text: str, top_n: int = 20) -> dict:
        """用 jieba 从全文提取关键词（本地 TF-IDF），用于校验 AI 关键词真实性。

        Args:
            text: 文献全文。
            top_n: 返回关键词数量上限。
        Returns:
            {"ok":bool,"keywords":[str,...],"error":str}
        """
        try:
            words = text_analysis.extract_keywords(text or "", int(top_n))
            return {"ok": True, "keywords": [word for word, _w in words]}
        except Exception as exc:
            logger.warning("Agent 工具 extract_keywords 失败：%s", exc)
            return {"ok": False, "keywords": [], "error": str(exc)}

    @tool(TOOL_AI_DEEP_ANALYZE)
    def ai_deep_analyze(text: str, focus: str = "", basis: str = "",
                        precision: int = 3, disabled_dims: str = "") -> dict:
        """调用当前配置的大模型对文献文本做深度结构化解读，输出六个维度，
        每段解读须标注原文段落锚点用于溯源校验。
        整体解读（无 basis）时若全文超过上下文安全阈值，自动按段落分块
        逐段提炼（map）后统一综合（reduce），保证模型覆盖全文而非开头片段。
        未配置 AI 模型或调用失败时返回 ok=False，由调用方改用本地结果。

        Args:
            text: 要解读的文献文本（basis=local_section 时为单章节原文）。
            focus: 仅解读指定维度 key（如 innovation_point），空串表示六维度整体解读；
                多维度用英文逗号分隔（如 "core_view,innovation_point"）。
            basis: Refine 补救通道标记，由系统内部动作传入：
                local_section 表示单维度基于章节原文重解读；
                mixed_section 表示多维度合并重解读（一次调用补救多个维度）。
            precision: 解析精度 1-5（由 Agent 状态注入，选择外置档位说明）。
            disabled_dims: 规则停用维度（英文逗号分隔，由 Agent 状态注入）。
        Returns:
            {"ok":bool,"drafts":{维度:成段解读（不含锚点标记）},
             "citations":{维度:[段落索引,...]},"engine":str,"error":str}
        """
        if not model or not model.get("base_url") or not model.get("api_key"):
            return {"ok": False, "drafts": {}, "citations": {}, "engine": "",
                    "error": "未配置可用的 AI 模型"}
        system_prompt = prompts.load_system_prompt()
        try:
            single_focus = focus and "," not in focus
            if basis == "mixed_section":
                multi_dims = [part.strip() for part in focus.split(",")
                              if part.strip()]
                prompt = prompts.build_refine_prompt_multi(multi_dims)
                content = ai_client.chat_with_text(
                    model["base_url"], model["api_key"], model.get("name", ""),
                    text, prompt, system_prompt,
                    on_waiting=_wait_hint(
                        "AI 正依据章节原文合并重新解读多个维度"),
                )
            elif basis == "local_section" and single_focus:
                label = prompts.DIMENSION_LABELS.get(focus, focus)
                prompt = prompts.build_refine_prompt(focus)
                content = ai_client.chat_with_text(
                    model["base_url"], model["api_key"], model.get("name", ""),
                    text, prompt, system_prompt,
                    on_waiting=_wait_hint(
                        f"AI 正依据章节原文重新解读「{label}」"),
                )
            elif len(text or "") > C.AI_INLINE_FULL_CHARS:
                # 超长全文：按全局段落分块 → 逐块 map 提炼 → 一次 reduce 综合
                content = _map_reduce_analyze(
                    model, text, focus, precision, disabled_dims,
                    _progress, _wait_hint)
            else:
                prompt = prompts.build_ai_analyze_prompt(
                    focus, from_original=False, precision=int(precision),
                    disabled_dims=disabled_dims)
                content = ai_client.chat_with_text(
                    model["base_url"], model["api_key"], model.get("name", ""),
                    text, prompt, system_prompt,
                    on_waiting=_wait_hint("AI 正在通读全文并深度解读"),
                )
            parsed = ai_client.extract_json_block(content)
            drafts, citations = parse_dimension_drafts(parsed, focus)
            return {"ok": True, "drafts": drafts, "citations": citations,
                    "engine": "agent_ai"}
        except LiteratureAgentError as exc:
            logger.warning("Agent 工具 ai_deep_analyze 失败：%s", exc.message)
            return {"ok": False, "drafts": {}, "citations": {}, "engine": "",
                    "error": exc.message}

    @tool(TOOL_AI_READ_ORIGINAL)
    def ai_read_original(file_path: str, focus: str = "",
                         precision: int = 3, disabled_dims: str = "",
                         page_count: int = 0, is_scanned: bool = False,
                         table_pages: str = "",
                         figure_pages: str = "") -> dict:
        """把文献**原件**（PDF/DOCX/TXT）经 file_id/file_data 通道交给大模型
        通读，并在通读过程中通过 function calling 自主调用本地中间件
        （extract_tables/ocr_page/extract_text/detect_structure）辅助取证，
        工具结果在本地执行后回灌对话，由模型产出最终六维度 JSON。
        首选「原件消息 + tools 同框」（cohabit）；网关拒绝同框时自动降级
        「先文本取证、再带取证材料原子通读」（staged）。未配置通道/文件类型
        不符/调用失败返回 ok=False；degradable 表示是否可换全文文本通道。

        Args:
            file_path: 文献原件本地绝对路径（由 Agent 状态自动注入）。
            focus: 仅解读指定维度 key（英文逗号分隔），空串表示全部维度。
            precision: 解析精度 1-5（由 Agent 状态注入）。
            disabled_dims: 规则停用维度（英文逗号分隔，由 Agent 状态注入）。
            page_count: 体检总页数（由 Agent 状态注入，用于页码越界校验）。
            is_scanned: 是否疑似扫描件（由 Agent 状态注入）。
            table_pages: 体检疑似表格页（英文逗号分隔的页码索引，状态注入）。
            figure_pages: 体检疑似图表页（同上格式，状态注入）。
        Returns:
            {"ok","drafts","citations","engine","fc_mode","fc_rounds",
             "fc_tools","fc_byproducts","degradable","error"}；
            fc_byproducts 携带读中工具取到的全文/OCR 页/表格/章节结构，
            由 Observe 写回共享状态作为本地基线。
        """
        if not model or not model.get("base_url") or not model.get("api_key"):
            fail = _ai_tool_fail("未配置可用的 AI 模型")
            fail["degradable"] = False
            return fail
        channel = ai_client.resolve_file_channel(model)
        if channel not in (ai_client.FILE_CHANNEL_ID,
                           ai_client.FILE_CHANNEL_DATA):
            fail = _ai_tool_fail(
                "当前 AI 模型未启用原件直传通道，改用全文文本通道")
            fail["degradable"] = True
            return fail
        if (channel == ai_client.FILE_CHANNEL_DATA
                and os.path.splitext(file_path)[1].lower() != ".pdf"):
            fail = _ai_tool_fail(
                "PDF Base64 直传通道仅支持 PDF，改用全文文本通道")
            fail["degradable"] = True
            return fail

        def _invoke_local(tool_name: str, args: dict, text_cache: dict):
            """在本地执行一个读中工具，返回 (output, 给模型看的文本)。"""
            if tool_name == TOOL_EXTRACT_TABLES:
                output = extract_tables.invoke({
                    "file_path": file_path,
                    "page_index": int(args.get("page_index", -1)),
                    "max_tables": int(args.get(
                        "max_tables", C.AGENT_TABLE_MAX_DEFAULT)),
                })
                return output, render_tables_observation(output)
            if tool_name == TOOL_OCR_PAGE:
                page_index = int(args["page_index"])
                output = ocr_page.invoke({
                    "file_path": file_path, "page_index": page_index,
                    "mode": str(args.get("mode", OCR_MODE_AUTO)),
                })
                if output.get("ok"):
                    body = f"第 {page_index + 1} 页 OCR 文字" \
                           f"（引擎 {output.get('engine', '?')}）：\n" \
                           f"{output.get('text', '')}"
                else:
                    body = f"第 {page_index + 1} 页 OCR 失败：" \
                           f"{output.get('error', '')}"
                return output, body
            if tool_name == TOOL_EXTRACT_TEXT:
                output = extract_text.invoke({"file_path": file_path})
                if output.get("ok"):
                    text_cache["text"] = output.get("text", "")
                return output, render_numbered_text_observation(output)
            if tool_name == TOOL_DETECT_STRUCTURE:
                if text_cache.get("text") is None:
                    fetched = extract_text.invoke({"file_path": file_path})
                    text_cache["text"] = fetched.get("text", "") \
                        if fetched.get("ok") else ""
                    if not fetched.get("ok"):
                        return fetched, (
                            f"本地章节识别需要先提取全文，但提取失败："
                            f"{fetched.get('error', '')}")
                output = detect_structure.invoke({
                    "text": text_cache["text"],
                    "precision": int(args.get("precision", precision)),
                })
                return output, render_structure_observation(output)
            return {"ok": False}, f"未注册的读中工具：{tool_name}"

        try:
            table_pages_list = _parse_page_list(table_pages)
            figure_pages_list = _parse_page_list(figure_pages)
            loop = _FCLoop(
                model=model,
                invoke_local=_invoke_local,
                file_path=file_path,
                channel=channel,
                page_count=int(page_count or 0),
                focus=focus,
                precision=int(precision),
                disabled_dims=disabled_dims,
                is_scanned=bool(is_scanned),
                table_pages=table_pages_list,
                figure_pages=figure_pages_list,
                progress=_progress,
                wait_hint=_wait_hint,
            )
            result = loop.run()
            parsed = ai_client.extract_json_block(result["content"])
            drafts, citations = parse_dimension_drafts(parsed, focus)
            return {
                "ok": True,
                "drafts": drafts,
                "citations": citations,
                "engine": "agent_ai_file",
                "fc_mode": result["mode"],
                "fc_rounds": result["rounds"],
                "fc_tools": result["trace"],
                "fc_byproducts": result["byproducts"],
            }
        except LiteratureAgentError as exc:
            logger.warning("Agent 工具 ai_read_original 失败：%s", exc.message)
            fail = _ai_tool_fail(exc.message)
            # 协议不支持（模型不接受 file/tools 消息）或 PDF 超限：文本通道
            # 仍可工作，适合自动降级；网络/鉴权/超时不自动降级，转本地基线。
            fail["degradable"] = ai_client.is_file_channel_degradable(
                exc.message)
            return fail

    registry = {
        TOOL_INSPECT_DOCUMENT: inspect_document,
        TOOL_OCR_PAGE: ocr_page,
        TOOL_EXTRACT_TEXT: extract_text,
        TOOL_EXTRACT_TABLES: extract_tables,
        TOOL_DETECT_STRUCTURE: detect_structure,
        TOOL_EXTRACT_KEYWORDS: extract_keywords,
        TOOL_AI_DEEP_ANALYZE: ai_deep_analyze,
        TOOL_AI_READ_ORIGINAL: ai_read_original,
    }
    return registry


def _ai_tool_fail(message: str) -> dict:
    """AI 类工具统一的失败返回结构。"""
    return {"ok": False, "drafts": {}, "citations": {}, "engine": "",
            "error": message}


# ================= 读中 function calling 多轮循环 =================

# 读中可暴露给模型的工具中文名（进度文案用）
FC_TOOL_LABELS = {
    TOOL_EXTRACT_TABLES: "提取表格数据",
    TOOL_OCR_PAGE: "识别页面文字",
    TOOL_EXTRACT_TEXT: "提取可引用全文",
    TOOL_DETECT_STRUCTURE: "本地章节结构识别",
}

# 暴露给模型的 OpenAI function calling 工具清单（白名单；
# inspect/ai_read_original/ai_deep_analyze/extract_keywords 等内部工具不暴露）
FC_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_EXTRACT_TABLES,
            "description": (
                "从文献指定页码提取表格并转换为 Markdown，保留行列结构与"
                "单元格原始数据。当解析结论需要引用表格中的精确数值"
                "（实验数据、指标对比、调查统计等）时调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_index": {
                        "type": "integer",
                        "description": "PDF 页码索引，从 0 开始；-1 表示全部页。"
                                       "Word 文档传 -1。",
                    },
                    "max_tables": {
                        "type": "integer",
                        "description": "最多返回的表格数量，默认 20。",
                    },
                },
                "required": ["page_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_OCR_PAGE,
            "description": (
                "对 PDF 指定页做视觉 OCR 取字。仅当该页是扫描图像、复杂"
                "图表或公式密集页、你无法从原件直接看清文字时调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_index": {
                        "type": "integer",
                        "description": "PDF 页码索引，从 0 开始，不得越界。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "local", "ai"],
                        "description": "识别方式：auto 自动选择（默认）、"
                                       "local 本地 OCR、ai 视觉模型。",
                    },
                },
                "required": ["page_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_EXTRACT_TEXT,
            "description": (
                "提取文献纯文本全文，按空行切分段落并标注【段落n】全局编号。"
                "当你需要精确引用原文段落号（填写[原文位置]锚点）时调用。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_DETECT_STRUCTURE,
            "description": (
                "用本地可复现规则识别章节结构与六个维度的原文候选段落，"
                "用于交叉验证你的章节判断与参考文献线索。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "precision": {
                        "type": "integer",
                        "description": "识别精度 1-5，默认按系统当前精度。",
                    },
                },
            },
        },
    },
]

# 工具调用预算耗尽后的强制收尾指令
_FC_BUDGET_USED_HINT = (
    "工具调用预算已用尽。请立即基于文献原件与已经取得的全部工具结果，"
    "不要再调用任何工具，直接输出最终的六维度 JSON 对象。"
)
# 模型收尾文本不是合法 JSON 时的一次修复指令
_FC_JSON_REPAIR_HINT = (
    "你刚才的回复不是可被 json.loads 解析的合法 JSON 对象。"
    "请只重新输出最终结果本身：一个 JSON 对象，不要代码围栏、不要解释文字，"
    "值内部引用一律使用中文引号。"
)


class _StagedFallback(Exception):
    """内部信号：Plan A（原件+tools 同框）被网关拒绝，切换 Plan B。"""


def _is_valid_json_content(content: str) -> bool:
    """收尾文本是否能提取出合法 JSON 对象（容错解析与正式解析同一入口）。"""
    try:
        ai_client.extract_json_block(content)
        return True
    except (LiteratureAgentError, ValueError):
        return False


def _truncate_tool_result(text: str) -> str:
    """工具结果回灌前按常量截断，头部保留主体、尾部保留结论行。"""
    text = str(text or "")
    limit = C.AGENT_TOOL_RESULT_MAX_CHARS
    if len(text) <= limit:
        return text
    tail = C.AGENT_TOOL_RESULT_TAIL_CHARS
    skipped = len(text) - limit
    return (text[:limit - tail]
            + f"\n……（工具结果过长，中间省略 {skipped} 字）……\n"
            + text[-tail:])


def _parse_page_list(raw) -> list:
    """把注入的页码列表/逗号串解析为 int 列表，非法项忽略。"""
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw or "").split(",")
    pages = []
    for item in items:
        part = str(item).strip()
        if part.lstrip("-").isdigit():
            pages.append(int(part))
    return pages


def render_tables_observation(output: dict) -> str:
    """extract_tables 输出 → 回灌给模型的 Markdown 文本。"""
    if not output.get("ok"):
        return f"表格提取失败：{output.get('error', '')}"
    tables = output.get("tables") or []
    if not tables:
        return "未在指定页面检测到表格。"
    parts = []
    for index, item in enumerate(tables):
        page = item.get("page")
        location = f"第 {int(page) + 1} 页" if isinstance(page, int) \
            else "Word 文档内嵌表格"
        parts.append(f"【表 {index + 1}·{location}】\n{item.get('markdown', '')}")
    return "已提取到以下 Markdown 表格：\n\n" + "\n\n".join(parts)


def render_numbered_text_observation(output: dict) -> str:
    """extract_text 输出 → 带【段落n】全局编号的全文文本（统一出口截断）。"""
    if not output.get("ok"):
        return f"全文提取失败：{output.get('error', '')}"
    paragraphs = global_paragraphs(output.get("text", ""))
    numbered = "\n\n".join(
        f"【段落{index}】{paragraph}"
        for index, paragraph in enumerate(paragraphs))
    return (
        f"全文共 {output.get('char_count', 0)} 字，按空行切分为 "
        f"{len(paragraphs)} 个段落（标注[原文位置]时只能使用方括号中的"
        f"全局段落号）：\n{numbered}"
    )


def render_structure_observation(output: dict) -> str:
    """detect_structure 输出 → 六维度章节候选与段落锚点摘要。"""
    if not output.get("ok"):
        return f"本地章节结构识别失败：{output.get('error', '')}"
    structure = output.get("structure") or {}
    section_paragraphs = output.get("section_paragraphs") or {}
    lines = ["本地规则识别的章节候选如下（仅供交叉验证，不要照抄噪声）："]
    for key in DIMENSION_KEYS:
        text = str(structure.get(key, "") or "").strip()
        anchors = section_paragraphs.get(key) or []
        if not text:
            lines.append(f"- {key}：未命中；锚点段落：{anchors}")
            continue
        preview = text[:300] + ("……" if len(text) > 300 else "")
        lines.append(
            f"- {key}（锚点段落 {anchors}）：{preview}")
    return "\n".join(lines)


class _FCLoop:
    """ai_read_original 内部的 function calling 多轮循环执行器。

    两种网关模式自适应：
    - cohabit（Plan A）：原件消息与 tools 同框，模型边读边调工具；
    - staged（Plan B）：同框被协议拒绝后，先纯文本取证、再带证据原子通读。

    安全边界：工具轮数 AGENT_TOOL_MAX_ROUNDS、总调用数
    AGENT_TOOL_MAX_CALLS、同签名去重、结果长度截断，全部在此收口。
    """

    def __init__(self, *, model: dict, invoke_local, file_path: str,
                 channel: str, page_count: int, focus: str, precision: int,
                 disabled_dims: str, is_scanned: bool,
                 table_pages: list, figure_pages: list,
                 progress, wait_hint):
        """初始化多轮循环。

        Args:
            model: 已解密模型配置。
            invoke_local: 本地工具执行回调 (tool_name, args, text_cache)
                -> (output:dict, observation:str)。
            file_path: 文献原件绝对路径。
            channel: 原件通道（file_id/file_data）。
            page_count: PDF 总页数（0 为非 PDF）。
            focus/precision/disabled_dims: 解析范围/精度/停用维度。
            is_scanned/table_pages/figure_pages: 体检结果。
            progress: 进度文案回调。
            wait_hint: 心跳工厂 wait_hint(scene)->on_waiting 回调。
        """
        self.model = model
        self.invoke_local = invoke_local
        self.file_path = file_path
        self.channel = channel
        self.page_count = max(0, int(page_count))
        self.focus = focus
        self.precision = max(1, min(5, int(precision)))
        self.disabled_dims = disabled_dims
        self.is_scanned = is_scanned
        self.table_pages = table_pages
        self.figure_pages = figure_pages
        self.progress = progress
        self.wait_hint = wait_hint
        self.base_url = model["base_url"]
        self.api_key = model["api_key"]
        self.model_name = model.get("name", "")
        self.system_prompt = prompts.load_system_prompt()
        self.signatures: set = set()
        self.trace: list = []
        self.evidence: list = []
        self.text_cache = {"text": None}
        # 读中工具的本地副产物：成功后由 ai_read_original 带出，Observe
        # 统一写回 state（abs_text/ocr_pages/table_texts/local_drafts），
        # 作为三重校验与参考文献/关键词的本地基线，避免再跑一遍相同工具
        self.byproducts = {
            "text": None,
            "ocr_pages": {},
            "tables": [],
            "structure": None,
        }
        self.calls = 0

    def run(self) -> dict:
        """执行完整读中循环。

        Returns:
            {"content": 最终 JSON 文本, "mode": cohabit/staged,
             "rounds": 模型交互总轮数, "trace": 工具调用留痕}
        Raises:
            LiteratureAgentError: 两种模式均失败或最终结果无法解析。
        """
        file_item = self._prepare_file_item()
        channel_label = {
            ai_client.FILE_CHANNEL_ID: "文件托管",
            ai_client.FILE_CHANNEL_DATA: "Base64 内联",
        }.get(self.channel, self.channel)
        logger.info("🤖 让 AI 通读文献原件（%s 通道）…", channel_label)
        try:
            content, rounds = self._run_cohabit(file_item)
            mode = "cohabit"
        except _StagedFallback:
            logger.info("  ⚠️ 当前模型不支持原件+工具同框，改用分段取证+原子通读")
            content, rounds = self._run_staged(file_item)
            mode = "staged"
        logger.info("  ✓ AI 通读完成（共 %d 轮交互，调用了 %d 个工具）",
                    rounds, self.calls)
        return {"content": content, "mode": mode, "rounds": rounds,
                "trace": self.trace, "byproducts": self._collect_byproducts()}

    def _prepare_file_item(self) -> dict:
        """上传原件（file_id）或编码 PDF（file_data），构造首轮 file 部件。"""
        if self.channel == ai_client.FILE_CHANNEL_ID:
            file_id = ai_client.upload_file(
                self.base_url, self.api_key, self.file_path,
                on_waiting=self.wait_hint("文献原件正在上传到模型服务"))
            return ai_client.build_file_content_item(file_id=file_id)
        file_size = os.path.getsize(self.file_path)
        if file_size > C.AI_PDF_INLINE_MAX_BYTES:
            limit_mb = C.AI_PDF_INLINE_MAX_BYTES // (1024 * 1024)
            raise LiteratureAgentError(
                f"PDF 原件过大（约 {file_size // (1024 * 1024)}MB），"
                f"超过内联发送上限 {limit_mb}MB，无法以原件模式解析")
        return ai_client.build_file_content_item(file_path=self.file_path)

    def _chat(self, messages: list, tools, timeout: int, scene: str) -> dict:
        """单轮模型交互（统一心跳与超时）。"""
        return ai_client.chat_round(
            self.base_url, self.api_key, self.model_name,
            messages, tools=tools, timeout=timeout,
            on_waiting=self.wait_hint(scene))

    def _execute_calls(self, messages: list, tool_calls: list,
                       round_index: int) -> int:
        """把模型请求的工具调用在本地全部执行并回灌。

        Args:
            messages: 多轮消息历史（追加 assistant/tool 消息）。
            tool_calls: 模型本轮返回的 tool_calls 原始结构。
            round_index: 当前轮次（进度文案用）。
        Returns:
            实际执行的合法工具调用数。
        """
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        })
        executed = 0
        for call in tool_calls:
            call_id = str(call.get("id") or f"call_{round_index}_{executed}")
            ok, observation = self._execute_one(call, round_index)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _truncate_tool_result(observation),
            })
            if ok:
                executed += 1
        return executed

    def _execute_one(self, call: dict, round_index: int) -> tuple:
        """校验并执行单个工具调用，返回 (是否真实执行, 回灌文本)。"""
        function = call.get("function") or {}
        name = function.get("name")
        try:
            raw_args = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            return False, "工具参数不是合法 JSON，请重新发起调用"
        if not isinstance(raw_args, dict):
            return False, "工具参数必须是 JSON 对象"
        if name == TOOL_DETECT_STRUCTURE:
            raw_args.setdefault("precision", self.precision)
        decision = sanitize_tool_call(
            {"tool": name, "args": raw_args}, self.page_count)
        if decision is None:
            return False, (
                f"工具调用非法：{name} 不在白名单或页码越界"
                f"（本文献共 {self.page_count} 页，页码从 0 开始），"
                "请更换工具或参数后重试")
        tool_name = decision["tool"]
        args = decision["args"]
        signature = (tool_name, json.dumps(
            args, sort_keys=True, ensure_ascii=False))
        if signature in self.signatures:
            return True, (
                f"工具 {tool_name} 使用相同参数已经执行过，结果已在对话"
                "上下文中，请勿重复调用；如需更多信息请更换页码/参数")
        self.signatures.add(signature)
        label = FC_TOOL_LABELS.get(tool_name, tool_name)
        self.progress(
            f"AI 读中取证（轮次 {round_index}/{C.AGENT_TOOL_MAX_ROUNDS}）："
            f"{label}")
        try:
            output, observation = self.invoke_local(
                tool_name, args, self.text_cache)
        except Exception as exc:  # 工具异常不炸循环：错误回灌让模型改道
            logger.warning("读中工具 %s 执行异常：%s", tool_name, exc)
            output, observation = (
                {"ok": False}, f"工具 {tool_name} 本地执行异常：{exc}")
        ok = bool(output.get("ok"))
        self.trace.append({"name": tool_name, "args": args, "ok": ok})
        self.calls += 1
        # 用户友好日志：让控制台明确展示 AI 正在调用哪个中间件
        if ok:
            # 组装简短参数摘要，避免过长
            param_hint = []
            if "page_index" in args:
                pi = args["page_index"]
                param_hint.append(
                    f"第 {pi} 页" if pi != -1 else "全部页")
            if "max_tables" in args:
                param_hint.append(f"最多 {args['max_tables']} 张")
            if "precision" in args:
                param_hint.append(f"精度 {args['precision']}")
            hint_str = "（" + "，".join(param_hint) + "）" if param_hint else ""
            logger.info("  · AI 调用了 %s%s", label, hint_str)
        else:
            logger.info("  ⚠️ AI 调用了 %s，但执行失败：%s",
                        label, output.get("message", ""))
        self.evidence.append(
            f"### 取证 {len(self.evidence) + 1}：{label}\n{observation}")
        if ok:
            self._collect_one_byproduct(tool_name, args, output)
        return ok, observation

    def _collect_one_byproduct(self, tool_name: str, args: dict,
                               output: dict) -> None:
        """把单个读中工具的成功输出收进副产物槽位（供 Observe 写回状态）。"""
        if tool_name == TOOL_EXTRACT_TEXT:
            self.byproducts["text"] = output.get("text", "")
        elif tool_name == TOOL_OCR_PAGE:
            page_index = int(args.get("page_index", 0))
            text = str(output.get("text") or "").strip()
            if text:
                self.byproducts["ocr_pages"][page_index] = text
        elif tool_name == TOOL_EXTRACT_TABLES:
            for item in output.get("tables") or []:
                self.byproducts["tables"].append(item)
        elif tool_name == TOOL_DETECT_STRUCTURE:
            self.byproducts["structure"] = {
                "structure": output.get("structure") or {},
                "section_paragraphs": output.get("section_paragraphs") or {},
            }

    def _collect_byproducts(self) -> dict:
        """汇总读中循环副产物；detect_structure 内部隐式提取的全文也带出。"""
        if self.text_cache.get("text") is not None \
                and not self.byproducts.get("text"):
            self.byproducts["text"] = self.text_cache["text"]
        return self.byproducts

    def _budget_exhausted(self, rounds: int) -> bool:
        """工具轮数或总调用数是否已达上限。"""
        return (rounds >= C.AGENT_TOOL_MAX_ROUNDS
                or self.calls >= C.AGENT_TOOL_MAX_CALLS)

    def _run_cohabit(self, file_item: dict) -> tuple:
        """Plan A：原件消息与 tools 同框的多轮循环。

        Returns:
            (最终 JSON 文本, 交互轮数)
        Raises:
            _StagedFallback: 首轮即被网关以协议理由拒绝。
        """
        prompt = prompts.build_original_fc_prompt(
            self.focus, precision=self.precision,
            disabled_dims=self.disabled_dims,
            page_count=self.page_count, is_scanned=self.is_scanned,
            table_pages=self.table_pages, figure_pages=self.figure_pages)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": [
                file_item, {"type": "text", "text": prompt}]},
        ]
        tools_on = True
        rounds = 0
        while True:
            rounds += 1
            if tools_on and self._budget_exhausted(rounds):
                tools_on = False
                messages.append(
                    {"role": "user", "content": _FC_BUDGET_USED_HINT})
            scene = (f"AI 正在通读文献原件（读中工具轮次 "
                     f"{rounds}/{C.AGENT_TOOL_MAX_ROUNDS}）")
            try:
                response = self._chat(
                    messages,
                    FC_TOOL_SCHEMAS if tools_on else None,
                    C.AI_TIMEOUT_PDF_CHAT, scene)
            except LiteratureAgentError as exc:
                if rounds == 1 and ai_client.is_tools_unsupported(exc.message):
                    raise _StagedFallback() from exc
                raise
            tool_calls = response.get("tool_calls") if tools_on else None
            if tool_calls:
                self._execute_calls(messages, tool_calls, rounds)
                continue
            content = response.get("content") or ""
            if _is_valid_json_content(content):
                return content, rounds
            if rounds >= C.AGENT_TOOL_MAX_ROUNDS + 2:
                raise LiteratureAgentError(
                    "模型通读完成但返回的结构化结果无法解析")
            if tools_on:
                # 模型未调工具但输出非法：关闭工具后给修复机会
                tools_on = False
            messages.append(
                {"role": "user", "content": _FC_JSON_REPAIR_HINT})

    def _run_staged(self, file_item: dict) -> tuple:
        """Plan B：先纯文本 function calling 取证，再带证据原子通读。

        Returns:
            (最终 JSON 文本, 交互总轮数)
        """
        probe_prompt = prompts.build_staged_probe_prompt(
            page_count=self.page_count, is_scanned=self.is_scanned,
            table_pages=self.table_pages, figure_pages=self.figure_pages)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": probe_prompt},
        ]
        rounds = 0
        tools_supported = True
        while True:
            rounds += 1
            if self._budget_exhausted(rounds):
                break
            try:
                response = self._chat(
                    messages, FC_TOOL_SCHEMAS, C.AI_TIMEOUT_CHAT,
                    f"AI 正在规划读前取证（轮次 {rounds}/"
                    f"{C.AGENT_TOOL_MAX_ROUNDS}）")
            except LiteratureAgentError as exc:
                # 纯文本 tools 也不被接受：放弃取证，直接做无工具原子通读
                # （等价于改造前的一次性原件通读，不会因此解析失败）
                if ai_client.is_tools_unsupported(exc.message):
                    tools_supported = False
                    break
                raise
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                break
            self._execute_calls(messages, tool_calls, rounds)
        if not tools_supported:
            self.evidence = []

        evidence_text = "\n\n".join(self.evidence)
        final_prompt = prompts.build_staged_final_prompt(
            evidence_text, focus=self.focus, precision=self.precision,
            disabled_dims=self.disabled_dims)
        final_messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": [
                file_item, {"type": "text", "text": final_prompt}]},
        ]
        rounds += 1
        response = self._chat(
            final_messages, None, C.AI_TIMEOUT_PDF_CHAT,
            "AI 正结合读前取证材料通读文献原件")
        content = response.get("content") or ""
        if _is_valid_json_content(content):
            return content, rounds
        # 原子通读后仅给一次 JSON 修复机会
        final_messages.append(
            {"role": "assistant", "content": content})
        final_messages.append(
            {"role": "user", "content": _FC_JSON_REPAIR_HINT})
        rounds += 1
        response = self._chat(
            final_messages, None, C.AI_TIMEOUT_PDF_CHAT,
            "AI 正在重新输出结构化结果")
        content = response.get("content") or ""
        if _is_valid_json_content(content):
            return content, rounds
        raise LiteratureAgentError(
            "模型通读完成但返回的结构化结果无法解析")


def split_text_chunks(full_text: str,
                      size: int = C.AI_INLINE_CHUNK_CHARS,
                      overlap: int = C.AI_INLINE_CHUNK_OVERLAP,
                      max_chunks: int = C.AI_INLINE_MAX_CHUNKS) -> list:
    """按空行把全文切成非空段落，再按字符上限聚合成带重叠的连续块。

    段落全局编号与 verifiers._split_paragraphs 完全一致（split("\\n\\n")
    后过滤空段），保证 map-reduce 最终标注的「段落n」锚点能通过溯源校验。
    相邻块之间通过尾部段落重叠，避免章节论证在切点处断裂；单段超过块
    上限时该段独占一块（不再硬切句子）。

    Args:
        full_text: 文献全文。
        size: 单块字符数上限。
        overlap: 相邻块重叠字符数上限（按整个段落重叠，不拆句）。
        max_chunks: 块数上限，超过抛业务异常（提示换长文本模型）。
    Returns:
        [[(全局段落号, 段落原文), ...], ...]：每个元素是一个块的段落列表。
    """
    paragraphs = [
        (index, part.strip())
        for index, part in enumerate((full_text or "").split("\n\n"))
        if part.strip()
    ]
    chunks: list[list] = []
    current: list = []
    current_len = 0
    for index, paragraph in paragraphs:
        plen = len(paragraph)
        if current and current_len + plen + 2 > size:
            chunks.append(current)
            tail: list = []
            tail_len = 0
            for _idx, text in reversed(current):
                if tail and tail_len + len(text) > overlap:
                    break
                tail.insert(0, (_idx, text))
                tail_len += len(text)
            current = tail
            current_len = tail_len
        current.append((index, paragraph))
        current_len += plen + 2
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise LiteratureAgentError(
            f"文献全文过长（切分为 {len(chunks)} 个片段，超过单次解析上限 "
            f"{max_chunks}），请换用支持原件直传的长文本模型")
    return chunks


def _render_chunk_with_markers(chunk: list) -> str:
    """把块内段落渲染为带【段落n】全局编号的文本。"""
    return "\n\n".join(f"【段落{index}】{text}" for index, text in chunk)


def global_paragraphs(full_text: str) -> list:
    """全文按空行切分后的非空段落列表（与 verifiers._split_paragraphs 同构）。

    Args:
        full_text: 文献全文。
    Returns:
        段落文本列表，列表下标即溯源校验使用的全局段落号。
    """
    return [
        part.strip()
        for part in (full_text or "").split("\n\n")
        if part.strip()
    ]


def render_marked_paragraphs(full_text: str, indices) -> str:
    """按全局段落号选取段落，渲染为【段落n】标记文本。

    Refine 章节补救时用它替代裸章节原文：模型只能引用材料里的全局段落号，
    产出的锚点与校验器的全文段落编号空间一致，避免章节内局部编号错位。

    Args:
        full_text: 文献全文。
        indices: 全局段落号可迭代对象（越界号自动忽略）。
    Returns:
        带标记的拼接文本；无有效段落号时返回空串。
    """
    paragraphs = global_paragraphs(full_text)
    picked = [
        (index, paragraphs[index])
        for index in sorted(set(int(i) for i in (indices or [])))
        if 0 <= index < len(paragraphs)
    ]
    return _render_chunk_with_markers(picked)


def _map_reduce_analyze(model: dict, full_text: str, focus: str,
                        precision: int, disabled_dims: str,
                        on_progress, wait_hint=None) -> str:
    """超长全文的 map-reduce 两阶段解读，返回模型最终 JSON 文本。

    1. map：每个带【段落n】编号的块单独提炼完整句子素材（保留编号）；
    2. reduce：把全部块素材合并后一次综合成六维度 JSON，锚点只能引用
       素材中出现过的段落号（与全文全局段落号一致，可过溯源校验）。

    Args:
        model: 模型配置。
        full_text: 文献全文。
        focus: 维度范围（逗号分隔，空串为全部维度）。
        precision: 解析精度 1-5。
        disabled_dims: 停用维度。
        on_progress: 进度回调 on_progress(message)。
        wait_hint: 可选心跳工厂 wait_hint(scene)->回调，单次模型调用
            等待期间周期提示（每个 map 块与 reduce 各自独立计时）。
    Returns:
        reduce 阶段模型返回的 JSON 文本。
    """
    chunks = split_text_chunks(full_text)
    base_url = model["base_url"]
    api_key = model["api_key"]
    model_name = model.get("name", "")
    system_prompt = prompts.load_system_prompt()
    map_prompt = prompts.build_map_prompt(disabled_dims)
    total = len(chunks)
    materials: list[str] = []
    for index, chunk in enumerate(chunks):
        scene = f"AI 通读全文片段 {index + 1}/{total}（超长文献分块解读）"
        on_progress(scene)
        waiting = wait_hint(scene) if wait_hint else None
        part = ai_client.chat_with_text(
            base_url, api_key, model_name,
            _render_chunk_with_markers(chunk), map_prompt, system_prompt,
            on_waiting=waiting,
        )
        materials.append(f"【片段{index + 1}/{total}】\n{(part or '').strip()}")

    reduce_scene = "AI 正在综合全部片段生成结构化报告…"
    on_progress(reduce_scene)
    reduce_prompt = prompts.build_reduce_prompt(
        precision=int(precision), disabled_dims=disabled_dims)
    return ai_client.chat_with_text(
        base_url, api_key, model_name,
        "\n\n".join(materials), reduce_prompt, system_prompt,
        on_waiting=wait_hint(reduce_scene) if wait_hint else None,
    )


def parse_dimension_drafts(parsed: dict, focus: str = "") -> tuple:
    """把模型返回的 JSON 解析为 (drafts, citations)。

    统一处理两件事：
    - focus 契约：指定维度时模型越界返回的维度一律忽略；
    - 锚点剥离：用 split_citation_marks 拆出正文与原文段落索引。

    Args:
        parsed: extract_json_block 解析出的模型 JSON 字典。
        focus: 逗号分隔的维度 key；空串表示接收全部维度。
    Returns:
        (drafts, citations)：仅含有正文内容的维度。
    """
    focus_keys = None
    if (focus or "").strip():
        focus_keys = [part.strip() for part in focus.split(",") if part.strip()]
    drafts: dict = {}
    citations: dict = {}
    for key in DIMENSION_KEYS:
        if focus_keys and key not in focus_keys:
            continue
        clean_text, anchors = split_citation_marks(
            str(parsed.get(key, "") or ""))
        if clean_text:
            drafts[key] = clean_text
            citations[key] = anchors
    return drafts, citations


def split_citation_marks(content: str) -> tuple:
    """从 AI 维度正文中剥离溯源锚点与系统传输标记。

    处理两类标记：
    - 「[原文位置：n1, n2]」：溯源锚点，剥除同时收集段落索引用于校验；
    - 「【段落n】」「【片段i/N】」：仅用于喂给模型的材料前缀标记
      （Refine 全局段落标记、map-reduce 片段标记），模型若照抄进正文，
      必须剥除，避免内部标记泄漏到最终解析报告。

    Args:
        content: AI 返回的单维度原始文本，可能带一个或多个锚点标记。
    Returns:
        (去除标记后的成段正文, 段落索引列表)；无标记时索引列表为空。
    """
    anchors: list[int] = []

    def _collect(match: "re.Match") -> str:
        for part in re.split(r"[,，]", match.group(1)):
            part = part.strip()
            if part.isdigit():
                idx = int(part)
                if idx not in anchors:
                    anchors.append(idx)
        return ""

    cleaned = _CITATION_PATTERN.sub(_collect, content or "")
    # 传输标记只剥除、不参与锚点收集（权威锚点以[原文位置]为准）
    cleaned = _TRANSPORT_MARK_PATTERN.sub("", cleaned)
    return cleaned.strip(), anchors


# [原文位置：0, 3] / [原文位置:0,3]，容忍全角逗号与空格
_CITATION_PATTERN = re.compile(r"\[原文位置[：:]\s*([0-9,，\s]*)\]")
# 喂给模型的材料内部标记：【段落12】、【片段 2/5】，容忍空格
_TRANSPORT_MARK_PATTERN = re.compile(
    r"【\s*(?:段落\s*\d+|片段\s*\d+\s*/\s*\d+)\s*】")


def summarize_observation(tool_name: str, output: dict) -> str:
    """把工具完整输出压缩成给 LLM 看的短观测（全文不进对话历史，防 token 膨胀）。

    Args:
        tool_name: 工具名。
        output: 工具返回字典。
    Returns:
        简短观测文本。
    """
    head_limit = C.AGENT_OBSERVATION_TEXT_HEAD
    if not isinstance(output, dict):
        return str(output)[:head_limit]
    if not output.get("ok", True) and output.get("error"):
        return f"{tool_name} 执行失败：{output['error']}"
    if tool_name == TOOL_INSPECT_DOCUMENT:
        layout = output.get("layout") or {}
        if layout.get("is_scanned"):
            feature = f"扫描件（共 {layout.get('page_count', 0)} 页，需逐页 OCR）"
        elif layout.get("has_two_columns"):
            feature = "双栏文本排版"
            if layout.get("pages_with_images"):
                feature += f"，{len(layout['pages_with_images'])} 个图表页"
        elif layout.get("pages_with_images"):
            feature = f"图文混合（{len(layout['pages_with_images'])} 个图表页）"
        else:
            feature = f"普通文本（{str(layout.get('format', '')).upper()}）"
        return f"原件布局检查完成：{feature}"
    if tool_name == TOOL_OCR_PAGE:
        return (f"第 {output.get('page_index', 0) + 1} 页 OCR 完成"
                f"（{output.get('engine', '?')}，{len(output.get('text', ''))} 字）")
    if tool_name == TOOL_EXTRACT_TEXT:
        head = (output.get("text") or "")[:head_limit]
        return (f"已提取全文，共 {output.get('char_count', 0)} 字，"
                f"类型 {output.get('file_type', '?')}。开头：\n{head}")
    if tool_name == TOOL_EXTRACT_TABLES:
        tables = output.get("tables") or []
        pages = sorted({
            int(item.get("page")) for item in tables
            if isinstance(item.get("page"), int)
        })
        location = ""
        if pages:
            location = "，位于第 " + "、".join(str(p + 1) for p in pages) + " 页"
        elif tables:
            location = "（Word 文档内嵌表格）"
        return f"表格提取完成：共 {len(tables)} 个 Markdown 表格{location}"
    if tool_name == TOOL_DETECT_STRUCTURE:
        structure = output.get("structure") or {}
        hit = [k for k, v in structure.items() if v]
        return f"本地结构识别命中维度：{hit or '无'}"
    if tool_name == TOOL_EXTRACT_KEYWORDS:
        return f"本地关键词：{output.get('keywords', [])}"
    if tool_name == TOOL_AI_DEEP_ANALYZE:
        drafts = output.get("drafts") or {}
        citations = output.get("citations") or {}
        hit = [k for k, v in drafts.items() if v]
        anchored = [k for k, v in citations.items() if v]
        return (f"AI 深度解读返回维度：{hit or '无'}；"
                f"已标注原文锚点的维度：{anchored or '无'}")
    if tool_name == TOOL_AI_READ_ORIGINAL:
        if not output.get("ok"):
            track = "将改用全文文本通道" if output.get("degradable") \
                else "AI 通道不可用，将转本地规则解析"
            return (f"AI 原件通读失败（{track}）："
                    f"{output.get('error', '')[:80]}")
        drafts = output.get("drafts") or {}
        citations = output.get("citations") or {}
        hit = [k for k, v in drafts.items() if v]
        anchored = [k for k, v in citations.items() if v]
        mode_label = "同框" if output.get("fc_mode") == "cohabit" else "分段取证"
        fc_tools = output.get("fc_tools") or []
        tool_seq = "、".join(
            FC_TOOL_LABELS.get(item.get("name"), item.get("name", "?"))
            for item in fc_tools) or "无"
        return (f"AI 原件通读返回维度：{hit or '无'}；"
                f"已标注原文锚点的维度：{anchored or '无'}；"
                f"读中模式：{mode_label}，{output.get('fc_rounds', 0)} 轮，"
                f"读中工具：{tool_seq}")
    return str(output)[:head_limit]

