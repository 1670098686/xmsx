"""Act 节点：从动作队列取出第一个动作并调用对应工具（ReAct 的 Action）。"""
import re
import time

from agent import director
from agent.nodes import (
    emit,
    ocr_progress_percent,
    remediation_percent,
    tool_progress_pair,
)
from agent.prompts import DIMENSION_LABELS
from agent.state import (
    DIMENSION_KEYS,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_AI_READ_ORIGINAL,
    TOOL_BASIS_LOCAL_SECTION,
    TOOL_BASIS_MIXED_SECTION,
    TOOL_DETECT_STRUCTURE,
    TOOL_EXTRACT_KEYWORDS,
    TOOL_EXTRACT_TABLES,
    TOOL_EXTRACT_TEXT,
    TOOL_INSPECT_DOCUMENT,
    TOOL_OCR_PAGE,
)
from agent.tools import (
    build_tool_registry,
    global_paragraphs,
    render_marked_paragraphs,
    summarize_observation,
)
from config import constants as C
from utils.logger import get_logger

logger = get_logger()


def act_node(state: dict) -> dict:
    """执行队首工具动作，把完整输出留给 Observe 节点入状态。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量：队列出队、步数 +1、追加一条 tool_history。
    """
    actions = list(state.get("planned_actions") or [])
    if not actions:
        return {}
    action = actions.pop(0)
    step = int(state.get("current_step", 0)) + 1
    tool_name = action.get("tool", "")
    tool_args = action.get("args") or {}

    # 提速改动：确定性结构化尾部的首次整体 AI 解读（无 basis）前，按本地
    # 草稿充实度裁剪 focus，本地章节已充实的维度不再交 AI 复述；全部充实
    # 则跳过本次 AI 调用。Director 单步决策的补解读（缺维度本身就是目标）
    # 与 Refine 补救（带 basis）不裁剪。
    skip_reason = ""
    if (tool_name == TOOL_AI_DEEP_ANALYZE
            and not tool_args.get("basis")
            and action.get("shrink_focus")):
        tool_args, skip_reason = _shrink_overall_ai_focus(tool_args, state)

    retry = _is_retry_call(state, tool_name)
    percent = _acting_percent(state, action, tool_name, tool_args, step, retry)
    if skip_reason:
        emit(state, percent,
             "本地章节内容已充实，跳过 AI 重复解读以节省解析时间…")
    else:
        emit(state, percent,
             f"第 {step} 步：{_acting_message(tool_name, tool_args, retry)}")

    registry = build_tool_registry(
        state.get("model"),
        on_progress=lambda message: emit(
            state,
            _tool_progress_percent(state, message, percent, action),
            message))
    tool = registry.get(tool_name)
    if tool is None:
        output = {"ok": False, "error": f"未知工具：{tool_name}"}
    elif skip_reason:
        # 不调用模型：记一次"跳过"观测，后续节点按无 AI 草稿处理
        output = {"ok": True, "skipped": True, "drafts": {}, "citations": {},
                  "engine": "", "reason": skip_reason}
    else:
        tool_args = _inject_state_args(tool, tool_args, state)
        # 全文为空（文本提取/逐页 OCR 均未获得内容）时，需要 text 形参的
        # 工具必然无法执行：直接给结构化失败观测，避免缺参触发 pydantic
        # 英文校验异常污染告警，Verify 随后按"全文为空"致命失败收尾。
        if ("text" not in tool_args and _tool_requires_text(tool)
                and not state.get("abs_text")):
            output = {
                "ok": False,
                "error": "文献全文为空（未提取或识别到任何文字），跳过该步骤",
            }
        else:
            tool_started = time.monotonic()
            try:
                output = tool.invoke(tool_args)
                elapsed = time.monotonic() - tool_started
                if isinstance(output, dict):
                    detail = ("" if output.get("ok")
                              else f"，error={str(output.get('error'))[:200]}")
                    logger.debug("工具 %s 执行完成：ok=%s，耗时 %.2f 秒%s",
                                 tool_name, output.get("ok"), elapsed, detail)
                else:
                    logger.debug("工具 %s 执行完成，耗时 %.2f 秒",
                                 tool_name, elapsed)
            except Exception as exc:  # 工具异常转为观测，由后续节点决策，不炸图
                logger.warning("Agent 工具 %s 执行异常：%s", tool_name, exc,
                               exc_info=True)
                output = {"ok": False, "error": str(exc)}

    history_item = {
        "step": step,
        "tool": tool_name,
        "args": _brief_args(tool_args),
        "reason": action.get("reason", ""),
        "output": output,
        "observation": summarize_observation(tool_name, output),
        # Director/阶段5 显式打标的进度锚点（可能为 None，走泳道默认锚点）
        "pct_start": action.get("_pct_start"),
        "pct_end": action.get("_pct_end"),
    }
    update = {
        "planned_actions": actions,
        "current_step": step,
        "tool_history": [history_item],
    }
    # 通道级降级与本地基线：新架构下原件直读排在第一步，此刻本地尚无全文。
    # - 协议不支持/PDF 超限（degradable=True）且用户授权回调同意：置
    #   original_read_failed，随后走"全文文本通道"结构化尾部；
    # - 用户拒绝/网络/鉴权/超时：置 ai_unavailable，只走本地基线；
    # 两种情形都必须先补本地解析素材：扫描件逐页 ocr_page，其余先
    # extract_text；结构化尾部动作由 Reflect 在基线完成后统一入队。
    if (tool_name == TOOL_AI_READ_ORIGINAL
            and output.get("ok") is False and state.get("model")):
        error_message = str(output.get("error") or "原件直传失败")
        degrade = False
        if output.get("degradable"):
            callback = state.get("degradation_callback")
            if callback is not None:
                try:
                    degrade = bool(callback(error_message))
                except Exception:
                    # 授权回调本身异常（UI 未就绪等）：保守按不允许处理
                    degrade = False
        if degrade:
            update["original_read_failed"] = True
        else:
            update["ai_unavailable"] = True
            update["ai_error"] = error_message

        layout = state.get("inspect_layout") or {}
        if layout.get("is_scanned"):
            baseline = director.scanned_ocr_chain_for_degrade(layout)
        else:
            baseline = [{
                "tool": TOOL_EXTRACT_TEXT, "args": {},
                "reason": "原件直传未成功：先补取文献全文作为解析基线",
            }]
        actions.extend(baseline)
        # 步数放宽：基线（OCR 可能很多页）+ 结构化尾部 + 补救余量
        new_max = max(
            int(state.get("max_steps", C.AGENT_MAX_STEPS)),
            step + len(baseline) + C.AGENT_SCANNED_STEP_TAIL,
        )
        update["max_steps"] = new_max
    return update


def _tool_progress_percent(state: dict, message: str, acting_percent: int,
                           action: dict = None) -> int:
    """工具内部 on_progress 消息对应的进度百分比。

    - 读中 function calling 循环：ai_read_original 段内按"轮次 N/M"
      在 12→69 间推进（70 留给通读完成锚点）；
    - 超长文献分块解读：map 各块在段内推进、reduce 停在段末；打标动作
      （阶段5）在动作自带的 [start,end] 小区间内推进，绝不越过 end；
    - 其余消息（整体解读/补救/OCR 的"仍在思考"心跳）：保持该步进行中
      的锚点百分比不动，避免心跳期间进度条倒退或跳变。

    Args:
        state: Agent 共享状态。
        message: 工具内 on_progress 回调的中文进度消息。
        acting_percent: 当前步骤"进行中"的锚点百分比。
        action: 当前执行的规划动作（可能含 _pct_end 自带锚点）。
    Returns:
        进度百分比。
    """
    tagged_end = None
    if action is not None and action.get("_pct_end") is not None:
        tagged_end = int(action["_pct_end"])
    # 读中 FC 循环轮次：在 ai_read_original 锚点段内按轮次推进
    fc_match = re.search(r"读中取证（轮次\s*(\d+)\s*/\s*(\d+)）",
                         message or "")
    if fc_match and (action or {}).get("tool") == TOOL_AI_READ_ORIGINAL:
        pair = tool_progress_pair(state, TOOL_AI_READ_ORIGINAL)
        if pair:
            start, end = pair
            cap = max(start, end - 1)  # 70 留给通读完成
            index = int(fc_match.group(1))
            total = max(1, int(fc_match.group(2)))
            return min(cap, start + int((cap - start) * index / total))
    match = re.search(r"片段\s*(\d+)\s*/\s*(\d+)", message or "")
    if match:
        index, total = int(match.group(1)), max(1, int(match.group(2)))
        if tagged_end is not None:
            # 打标动作：map 推进封顶 end-1，给 reduce 的 end 留位
            cap = max(acting_percent, tagged_end - 1)
            if cap <= acting_percent:
                return acting_percent
            return min(cap, acting_percent
                       + int((cap - acting_percent) * index / total))
        # 非打标动作：取当前泳道 ai_deep_analyze 锚点段
        # （早段 62→75；原件泳道降级链 76→81）
        pair = tool_progress_pair(state, TOOL_AI_DEEP_ANALYZE)
        if pair:
            map_start, map_cap = pair
            map_cap = max(map_start, map_cap - 1)
            return min(map_cap,
                       map_start + int((map_cap - map_start) * index / total))
        return acting_percent
    if "综合" in (message or ""):
        if tagged_end is not None:
            return tagged_end
        pair = tool_progress_pair(state, TOOL_AI_DEEP_ANALYZE)
        if pair:
            return pair[1]
        return acting_percent
    return acting_percent


# ================= 进度条文案 =================

def _is_retry_call(state: dict, tool_name: str) -> bool:
    """该工具此前是否已成功/尝试调用过（再次出现即 Verify 驱动的补救调用）。"""
    return any(
        item.get("tool") == tool_name
        for item in (state.get("tool_history") or [])
    )


def _acting_percent(state: dict, action: dict, tool_name: str, args: dict,
                    step: int, retry: bool) -> int:
    """进行中百分比：打标动作取自带锚点；OCR 按页/泳道推进；其余取泳道锚点。

    Director 预规划动作与阶段 5 补救动作由编排层显式打标（_pct_start），
    多动作在各自小区间内均分，优先级最高；首轮（非 retry）其余动作按泳道
    锚点：早段 10-75、原件段 62-81；Refine 补救动作（retry）走 83-89 段，
    且不低于最近一次校验锚点。
    """
    if action.get("_pct_start") is not None:
        return int(action["_pct_start"])
    if tool_name == TOOL_OCR_PAGE:
        return ocr_progress_percent(state, args.get("page_index", 0), done=False)
    if not retry:
        pair = tool_progress_pair(state, tool_name)
        if pair:
            return pair[0]
    return remediation_percent(state, step)


def _acting_message(tool_name: str, args: dict, retry: bool) -> str:
    """生成用户可读的"工具调用中"文案（禁止把英文工具名直接抛给用户）。"""
    if tool_name == TOOL_INSPECT_DOCUMENT:
        return "正在抽样检查原件布局（扫描件 / 双栏 / 图表页）…"
    if tool_name == TOOL_OCR_PAGE:
        page_index = int(args.get("page_index", 0)) + 1
        if args.get("mode") == "ai":
            channel = "AI 视觉"
        elif args.get("mode") == "local":
            channel = "本地 OCR"
        else:
            channel = "OCR"
        return f"正在识别第 {page_index} 页文字（{channel}）…"
    if tool_name == TOOL_EXTRACT_TEXT:
        return "正在提取文献全文（PDF / Word / TXT 自动识别）…"
    if tool_name == TOOL_EXTRACT_TABLES:
        page_index = args.get("page_index", -1)
        try:
            page_index = int(page_index)
        except (TypeError, ValueError):
            page_index = -1
        if page_index >= 0:
            return f"正在结构化提取第 {page_index + 1} 页表格（转 Markdown）…"
        return "正在结构化提取文献中的全部表格（转 Markdown）…"
    if tool_name == TOOL_DETECT_STRUCTURE:
        precision = int(args.get("precision", 3))
        if retry:
            return (f"校验发现内容缺失，正在以更高精度（{precision}/5）"
                    "重新识别章节结构…")
        return f"正在按章节标题识别文献结构（本地规则，精度 {precision}/5）…"
    if tool_name == TOOL_EXTRACT_KEYWORDS:
        top_n = int(args.get("top_n", 20))
        return f"正在提取关键词（本地分词，Top {top_n}）…"
    if tool_name == TOOL_AI_DEEP_ANALYZE:
        return _ai_acting_message(args, retry)
    if tool_name == TOOL_AI_READ_ORIGINAL:
        return _original_acting_message(args)
    return f"正在执行解析动作：{tool_name}…"


def _original_acting_message(args: dict) -> str:
    """AI 原件直读的进度文案：模型直接收到 PDF/Word 原件（非提取文本）。"""
    focus = str(args.get("focus", "") or "")
    dims = [part.strip() for part in focus.split(",") if part.strip()]
    if dims:
        names = "、".join(DIMENSION_LABELS.get(key, key) for key in dims)
        return (f"AI 正在通读文献原件并深度解读 {len(dims)} 个维度"
                f"（{names}，原件直传）…")
    return "AI 正在通读文献原件并深度解读全部维度（原件直传）…"


def _ai_acting_message(args: dict, retry: bool) -> str:
    """AI 深度解读的进度文案：区分整体解读 / 章节重解读 / 单维度补解读。"""
    focus = str(args.get("focus", "") or "")
    dims = [part.strip() for part in focus.split(",") if part.strip()]
    single = len(dims) == 1 and "," not in focus
    if single:
        label = DIMENSION_LABELS.get(dims[0], dims[0])
        if args.get("basis") == "local_section":
            return f"校验未通过，AI 正依据章节原文重新解读「{label}」…"
        return f"AI 正在补充解读「{label}」…"
    if dims:
        names = "、".join(DIMENSION_LABELS.get(key, key) for key in dims)
        if args.get("basis") == TOOL_BASIS_MIXED_SECTION:
            return (f"校验未通过，AI 正依据章节原文合并重新解读 {len(dims)} 个维度"
                    f"（{names}）…")
        return f"AI 正在通读全文并深度解读 {len(dims)} 个维度（{names}）…"
    return "AI 正在通读全文并深度解读六个维度…"


def _tool_requires_text(tool) -> bool:
    """工具形参是否声明 text（全文为空时这些工具应短路而非缺参调用）。"""
    try:
        return "text" in tool.args_schema.model_fields
    except Exception:
        return False


def _inject_state_args(tool, tool_args: dict, state: dict) -> dict:
    """把状态中的全文自动绑定到声明了 text 形参的工具。

    规划动作只携带元参数（precision/focus/top_n），全文不进 LLM 对话历史；
    Act 执行前在此统一注入，保证 detect_structure / extract_keywords /
    ai_deep_analyze 的 text 参数始终来自已提取的 state.abs_text。

    Args:
        tool: LangChain BaseTool 实例。
        tool_args: 规划动作给出的原始参数。
        state: Agent 共享状态。
    Returns:
        注入后的参数字典。
    """
    merged = dict(tool_args or {})
    try:
        fields = tool.args_schema.model_fields
    except Exception:
        return merged
    # 原件路径统一由状态注入，规划动作不携带路径（历史记录只留文件名）
    if ("file_path" in fields and "file_path" not in merged
            and state.get("lit_path")):
        merged["file_path"] = state["lit_path"]
    # 解析精度与停用维度统一来自解析规则状态，规划器不参与决策，
    # 避免 LLM 自行更改精度或解读已被规则关闭的维度
    if "precision" in fields and "precision" not in merged:
        merged["precision"] = int(state.get("precision", 3))
    if "disabled_dims" in fields and "disabled_dims" not in merged:
        merged["disabled_dims"] = _disabled_dims_from_state(state)
    # ai_read_original 的读中 function calling 循环需要体检摘要：
    # 总页数用于页码越界校验、扫描件/表格页/图表页提示模型自主取证
    if getattr(tool, "name", "") == TOOL_AI_READ_ORIGINAL:
        layout = state.get("inspect_layout") or {}
        if "page_count" not in merged:
            merged["page_count"] = int(layout.get("page_count") or 0)
        if "is_scanned" not in merged:
            merged["is_scanned"] = bool(layout.get("is_scanned"))
        if "table_pages" not in merged:
            merged["table_pages"] = ",".join(
                str(index) for index in (layout.get("table_pages") or []))
        if "figure_pages" not in merged:
            merged["figure_pages"] = ",".join(
                str(index) for index in (layout.get("pages_with_images") or []))
    if "text" not in fields or "text" in merged or not state.get("abs_text"):
        return merged
    # Refine 补救调用的事实约束（统一渲染【段落n】全局标记，保证模型给出的
    # 锚点与校验器的全文段落编号空间一致，避免章节内局部编号导致溯源必败）：
    # - local_section：重解读单维度，只喂该维度章节覆盖的全局段落（小 payload）；
    # - mixed_section：合并重解读多维度，喂各维度段落并集；
    #   存在本地缺失维度（完整性补救）时退回全文标记版，保证有上下文可补。
    # 映射缺失（旧检查点/识别异常）时一律退回全文标记版。
    focus = str(merged.get("focus", "") or "")
    single_dim = focus and "," not in focus
    basis = merged.get("basis", "")
    if basis == TOOL_BASIS_LOCAL_SECTION and single_dim:
        merged["text"] = _refine_material_for_dims([focus], state)
        return merged
    if basis == TOOL_BASIS_MIXED_SECTION:
        dims = [part.strip() for part in focus.split(",") if part.strip()]
        merged["text"] = _refine_material_for_dims(dims, state)
        return merged
    merged["text"] = state["abs_text"]
    return merged


def _refine_material_for_dims(dims: list, state: dict) -> str:
    """为 Refine 补救构造带【段落n】全局标记的事实材料。

    Args:
        dims: 需补救的维度 key 列表。
        state: Agent 共享状态（取 abs_text / section_paragraphs / local_drafts）。
    Returns:
        标记文本：全部维度都有精确章节映射时只含相关段落（省 token、更快）；
        存在本地缺失维度或映射缺失时回退为全文标记版。
    """
    full_text = state.get("abs_text") or ""
    section_map = state.get("section_paragraphs") or {}
    drafts = state.get("local_drafts") or {}
    indices: list = []
    all_mapped = bool(dims)
    for dim in dims:
        dim_indices = section_map.get(dim) or []
        has_local = bool((drafts.get(dim) or "").strip())
        # 本地有章节却没有段落映射（异常/旧状态）：映射不可信，退回全文
        if has_local and not dim_indices:
            all_mapped = False
            break
        # 本地缺失维度（完整性补救）：章节材料无法覆盖，需要全文上下文
        if not has_local:
            all_mapped = False
            break
        indices.extend(dim_indices)
    if all_mapped:
        marked = render_marked_paragraphs(full_text, indices)
        if marked:
            return marked
    # 退回全文标记版（段落号仍与校验器一致，只是 payload 更大）
    return render_marked_paragraphs(
        full_text, range(len(global_paragraphs(full_text))))


def _disabled_dims_from_state(state: dict) -> str:
    """从解析规则中生成停用维度逗号串（供 AI 工具 disabled_dims 形参）。

    Args:
        state: Agent 共享状态，rule_detail.dimensions 为 {维度key: bool}。
    Returns:
        停用维度 key 的逗号串；全部启用或规则缺失时返回空串。
    """
    dimensions = ((state.get("rule_detail") or {}).get("dimensions")) or {}
    return ",".join(
        key for key in DIMENSION_KEYS
        if not bool(dimensions.get(key, True))
    )


def _shrink_overall_ai_focus(args: dict, state: dict) -> tuple:
    """首次整体 AI 解读提速：把本地章节已充实的维度从 focus 中剔除。

    本地结构识别先于 AI 执行，轮到 AI 动作时本地草稿已就绪：本地章节达到
    AGENT_LOCAL_DIM_SELF_SUFFICIENT_CHARS 字的维度直接采用本地原文，AI 只补
    本地缺失/过短的维度，缩短 prompt 与输出、提高一次校验通过率；若所有维度
    本地都已充实，则整体跳过本次 AI 调用。Refine 补救动作（带 basis）不裁剪。

    Args:
        args: AI 动作原始参数。
        state: Agent 共享状态。
    Returns:
        (args, skip_reason)：skip_reason 非空表示应跳过 AI 调用。
    """
    focus = str(args.get("focus", "") or "")
    dims = [part.strip() for part in focus.split(",") if part.strip()]
    if not dims:
        return args, ""  # 无 focus（六维度全量解读）保持原行为，不做激进裁剪
    drafts = state.get("local_drafts") or {}
    need_ai = [
        dim for dim in dims
        if len((drafts.get(dim) or "").strip()) < C.AGENT_LOCAL_DIM_SELF_SUFFICIENT_CHARS
    ]
    if len(need_ai) == len(dims):
        return args, ""  # 所有维度本地都不足：原样解读
    if not need_ai:
        return args, "all_dimensions_covered_by_local"
    shrunk = dict(args)
    shrunk["focus"] = ",".join(need_ai)
    return shrunk, ""


def _brief_args(args: dict) -> dict:
    """参数进历史前去敏/瘦身：路径只留文件名，长文本只记长度。"""
    brief = {}
    for key, value in (args or {}).items():
        if key == "file_path":
            brief[key] = str(value).rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        elif isinstance(value, str) and len(value) > 200:
            brief[key] = f"<文本 {len(value)} 字>"
        else:
            brief[key] = value
    return brief
