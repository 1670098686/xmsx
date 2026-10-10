"""Observe 节点：把工具输出写入对应状态槽位（ReAct 的 Observation/写入）。"""
import re

from agent.nodes import (
    emit,
    ocr_progress_percent,
    remediation_percent,
    tool_progress_pair,
)
from agent.state import (
    DIMENSION_KEYS,
    LOCAL_AUTHORITATIVE_DIMS,
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
from tool_layer import ai_client


def observe_node(state: dict) -> dict:
    """根据最近一次工具调用，将结果归入布局/全文/两套草稿/关键词。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（可能含 inspect_layout、ocr_pages、abs_text、local_drafts、
        ai_drafts、keywords、error、warnings）。
    """
    history = state.get("tool_history") or []
    if not history:
        return {}
    last = history[-1]
    tool_name = last.get("tool")
    output = last.get("output") or {}
    retry = len([item for item in history if item.get("tool") == tool_name]) > 1
    percent = _done_percent(state, last, tool_name, last.get("args") or {},
                            int(last.get("step", 0)), retry)
    update = {}

    if tool_name == TOOL_INSPECT_DOCUMENT:
        if output.get("ok"):
            update["inspect_layout"] = output.get("layout") or {}
            emit(state, percent, _layout_done_message(update["inspect_layout"]))
        else:
            # 布局探测失败不致命：按普通文本链路兜底提取（pdfplumber 仍可能成功）
            message = f"原件布局检查失败：{output.get('error')}，按普通文本处理"
            update["inspect_layout"] = {}
            update["warnings"] = [message]
            emit(state, percent, message)

    elif tool_name == TOOL_OCR_PAGE:
        page_index = int(output.get("page_index",
                                    last.get("args", {}).get("page_index", 0)))
        page_total = int((state.get("inspect_layout") or {}).get("page_count") or 0)
        if output.get("ok") and (output.get("text") or "").strip():
            ocr_pages = {**(state.get("ocr_pages") or {}),
                         page_index: output["text"].strip()}
            update["ocr_pages"] = ocr_pages
            update["abs_text"] = _merge_ocr_text(state, ocr_pages)
            engine_label = "AI 视觉" if output.get("engine") == "ai_vision" \
                else "本地 OCR"
            page_label = (f"第 {page_index + 1}/{page_total} 页"
                          if page_total else f"第 {page_index + 1} 页")
            scanned = (state.get("inspect_layout") or {}).get("is_scanned")
            scene = "扫描件" if scanned else "图表页"
            emit(state, percent, f"{scene}{page_label}识别完成（{engine_label}）")
        else:
            # 单页 OCR 失败不致命：跳过该页，其余页继续；最终无文本时 Verify 兜底
            message = f"第 {page_index + 1} 页识别失败：{output.get('error', '未知错误')}"
            update["warnings"] = [message]
            emit(state, percent, message)

    elif tool_name == TOOL_EXTRACT_TEXT:
        if output.get("ok"):
            update["abs_text"] = output.get("text", "")
            emit(state, percent,
                 f"全文提取完成：{output.get('char_count', 0)} 字"
                 f"（{output.get('file_type') or '未知类型'}）")
        elif state.get("ai_original_trusted"):
            # 信任路径的全文提取只是后处理护栏（供参考文献/关键词/兜底）：
            # 扫描件等场景提取失败不致命、不置 error，AI 原件结果照常收尾
            message = (f"原件已直读成功；本地全文提取失败（不影响 AI 解析结果）："
                       f"{output.get('error', '未知错误')}")
            update["warnings"] = [message]
            emit(state, percent, message)
        else:
            update["error"] = f"全文提取失败：{output.get('error', '未知错误')}"
            update["warnings"] = [update["error"]]
            emit(state, percent, update["error"])

    elif tool_name == TOOL_EXTRACT_TABLES:
        if output.get("ok"):
            table_texts = dict(state.get("table_texts") or {})
            for item in output.get("tables") or []:
                key = _table_item_key(item)
                if not key:
                    continue
                table_texts[key] = {
                    "page": item.get("page"),
                    "index": int(item.get("index", 0)),
                    "markdown": str(item.get("markdown") or "").strip(),
                }
            update["table_texts"] = table_texts
            update["abs_text"] = _merge_table_text(state, table_texts)
            new_count = len(output.get("tables") or [])
            emit(state, percent,
                 f"表格提取完成：本次 {new_count} 个，"
                 f"累计 {len(table_texts)} 个（Markdown 结构化）")
        else:
            # 抽表失败不致命：该维度仍可依据正文/AI 原件通读结果继续
            message = f"表格提取失败：{output.get('error', '未知错误')}"
            update["warnings"] = [message]
            emit(state, percent, message)

    elif tool_name == TOOL_DETECT_STRUCTURE:
        if output.get("ok"):
            update["local_drafts"] = {
                **(state.get("local_drafts") or {}),
                **(output.get("structure") or {}),
            }
            # 章节→全局段落号映射；提精度重采时各维度独立覆盖
            update["section_paragraphs"] = {
                **(state.get("section_paragraphs") or {}),
                **(output.get("section_paragraphs") or {}),
            }
            hit = [k for k, v in update["local_drafts"].items() if v]
            prefix = "章节结构重新识别" if retry else "本地结构识别"
            emit(state, percent, f"{prefix}完成：命中 {len(hit)} 个维度")
        else:
            message = f"本地结构识别失败：{output.get('error')}"
            update["warnings"] = [message]
            emit(state, percent, message)

    elif tool_name == TOOL_EXTRACT_KEYWORDS:
        if output.get("ok"):
            update["keywords"] = output.get("keywords", [])
            emit(state, percent,
                 f"关键词提取完成：{len(update['keywords'])} 个")
        else:
            message = f"本地关键词提取失败：{output.get('error')}"
            update["warnings"] = [message]
            emit(state, percent, message)

    elif tool_name in (TOOL_AI_DEEP_ANALYZE, TOOL_AI_READ_ORIGINAL):
        is_original = tool_name == TOOL_AI_READ_ORIGINAL
        if output.get("ok"):
            update["ai_drafts"] = {
                **(state.get("ai_drafts") or {}),
                **(output.get("drafts") or {}),
            }
            # 重解读只返回部分维度：旧锚点以本次为准、整体合并
            update["citation_map"] = {
                **(state.get("citation_map") or {}),
                **(output.get("citations") or {}),
            }
            if output.get("engine"):
                update["engine"] = output["engine"]
            # 读中 FC 循环取得的本地副产物写回共享状态（全文/OCR 页/表格/
            # 章节结构），作为三重校验、参考文献与关键词的本地基线
            if is_original:
                _merge_fc_byproducts(state, output, update)
            # 原件直读成功且启用的非本地维度齐全：置信任标记，Verify 只查
            # 完整性，跳过由线性本地文本裁判的一致性/溯源校验
            if is_original:
                trusted = _is_original_trusted(state, update["ai_drafts"])
                if trusted:
                    update["ai_original_trusted"] = True
            hit = [k for k, v in update["ai_drafts"].items() if v]
            if output.get("skipped"):
                emit(state, percent,
                     "本地章节已覆盖全部待解读维度，本次未调用 AI（提速）")
            elif is_original:
                # 原件通读只在首轮整体解读出现，Refine 补救统一走文本通道
                mode_label = "同框" if output.get("fc_mode") == "cohabit" \
                    else "分段取证"
                fc_count = len(output.get("fc_tools") or [])
                emit(state, percent,
                     f"AI 原件通读完成（读中{mode_label}，"
                     f"{output.get('fc_rounds', 0)} 轮/调用 {fc_count} 个"
                     f"中间件）：累计 {len(hit)} 个维度")
            else:
                prefix = "AI 重新解读" if retry else "AI 深度解读"
                emit(state, percent, f"{prefix}完成：累计 {len(hit)} 个维度")
        else:
            error_message = str(output.get("error") or "AI 调用失败")
            # 原件直传失败：是否换轨全文文本已由 Act 按错误类型 + 用户授权决定
            if is_original:
                if state.get("ai_unavailable"):
                    warning = (f"AI 原件通读失败：{error_message}"
                               "（已停止 AI 尝试，改用本地规则解析）")
                    emit(state, percent,
                         "AI 通道不可用，改用本地规则解析…")
                else:
                    warning = (f"AI 原件通读失败：{error_message}"
                               "（已改用全文文本通道重试）")
                    emit(state, percent,
                         "AI 原件通读不可用，改用全文文本通道重试…")
            else:
                warning = f"AI 深度解读不可用：{error_message}"
                # 文本通道是最后一条 AI 通道：网络/鉴权/超时等致命错误
                # 直接标记 AI 不可用，Refine 不再排队必败的 AI 补救；
                # 仅"输入超长"类错误保留章节级短文本补救的可能
                if not ai_client.is_context_length_error(error_message):
                    update["ai_unavailable"] = True
                    update["ai_error"] = error_message
                    emit(state, percent,
                         f"{warning}（AI 通道不可用，将以本地结果继续）")
                else:
                    emit(state, percent,
                         f"{warning}（将以本地结果继续）")
            update["warnings"] = [warning]

    return update


def _merge_ocr_text(state: dict, ocr_pages: dict) -> str:
    """把 OCR 识别结果聚合为全文。

    - 扫描件：全文完全来自逐页 OCR，按页码顺序拼接；
    - 图文混合（文本 PDF 补充图表页）：以提取文本为底本，图表页 OCR 文本
      带页码标记追加在末尾，供关键词与 AI 解读参考（结构识别已先完成，
      不会污染章节/参考文献切分）。

    Args:
        state: Agent 共享状态。
        ocr_pages: {页码索引: 识别文本}。
    Returns:
        聚合后的全文。
    """
    ordered = [
        ocr_pages[index].strip()
        for index in sorted(ocr_pages) if (ocr_pages.get(index) or "").strip()
    ]
    if (state.get("inspect_layout") or {}).get("is_scanned"):
        return "\n\n".join(ordered)
    base = (state.get("abs_text") or "").strip()
    blocks = [
        f"【{C.AGENT_FIGURE_OCR_BLOCK_TITLE}·第{index + 1}页】\n"
        f"{ocr_pages[index].strip()}"
        for index in sorted(ocr_pages)
        if (ocr_pages.get(index) or "").strip()
    ]
    if not blocks:
        return state.get("abs_text") or ""
    if base:
        return base + "\n\n" + "\n\n".join(blocks)
    return "\n\n".join(blocks)


# 已并入全文尾部的表格块标记：PDF「【表格数据·第N页】」、DOCX「【Word表N】」
_TABLE_BLOCK_MARK_RE = re.compile(
    r"\n*【(?:"
    r"表格数据·第\d+页"
    r"|Word表\d+"
    r")】"
)


def _table_item_key(item: dict) -> str:
    """生成表格全局去重键：PDF 按 (页码, 表序号)，DOCX 按表序号。

    Args:
        item: extract_tables 返回的单个表格元素。
    Returns:
        稳定键字符串；无法识别时返回空串。
    """
    page = item.get("page")
    try:
        index = int(item.get("index", 0))
    except (TypeError, ValueError):
        return ""
    if isinstance(page, int):
        return f"pdf:{page}:{index}"
    if str(page).lower() == "docx":
        return f"docx:{index}"
    return ""


def _strip_table_blocks(text: str) -> str:
    """删除全文尾部已追加的全部表格块，返回不含表格块的正文底本。

    表格块始终只追加在全文末尾（与图表 OCR 块同策略），截断到首个表格块
    标记之前即可，保证重复抽表/多次合并不会产生重复块。

    Args:
        text: 当前全文（可能含历史表格块）。
    Returns:
        剔除表格块后的正文。
    """
    match = _TABLE_BLOCK_MARK_RE.search(text or "")
    return (text[:match.start()] if match else (text or "")).rstrip()


def _merge_table_text(state: dict, table_texts: dict) -> str:
    """把全部已提取表格按块标题去重追加到全文末尾。

    - PDF：同页多个表格合并进一个「【表格数据·第N页】」块，按页码、表序排列；
    - DOCX：每个表格一个「【Word表N】」块，按表序排列；
    - 表格块不参与正文段落重建（追加在 detect_structure 之后或文本通道
      AI 解读之前），仅向 AI 与关键词提供表格数据。

    Args:
        state: Agent 共享状态。
        table_texts: {去重键: {"page","index","markdown"}}。
    Returns:
        合并表格块后的全文；无表格时返回原全文。
    """
    base = _strip_table_blocks(state.get("abs_text") or "")
    pdf_pages: dict = {}
    docx_items: list = []
    for item in (table_texts or {}).values():
        markdown = str((item or {}).get("markdown") or "").strip()
        if not markdown:
            continue
        page = (item or {}).get("page")
        index = int((item or {}).get("index", 0))
        if isinstance(page, int):
            pdf_pages.setdefault(page, []).append((index, markdown))
        elif str(page).lower() == "docx":
            docx_items.append((index, markdown))

    blocks: list = []
    for page in sorted(pdf_pages):
        body = "\n\n".join(
            markdown for _idx, markdown in sorted(pdf_pages[page]))
        blocks.append(
            f"【{C.AGENT_TABLE_BLOCK_TITLE}·第{page + 1}页】\n{body}")
    for index, markdown in sorted(docx_items):
        blocks.append(f"【Word表{index + 1}】\n{markdown}")

    if not blocks:
        return base
    if base:
        return base + "\n\n" + "\n\n".join(blocks)
    return "\n\n".join(blocks)


def _merge_fc_byproducts(state: dict, output: dict, update: dict) -> None:
    """把读中 FC 循环的本地取证副产物合并进 Observe 状态增量。

    合并顺序与各独立工具的 Observe 分支保持一致：
    1. detect_structure → local_drafts / section_paragraphs；
    2. extract_text 全文 → 仅在当前尚无全文时作为底本；
    3. ocr_page 页 → ocr_pages 聚合并重建全文（扫描件全文来自 OCR，
       文本件图表页 OCR 以块追加）；
    4. extract_tables → table_texts 去重并把表格块追加到全文末尾。

    Args:
        state: Agent 共享状态（不含本次观测增量）。
        output: ai_read_original 的成功输出（含 fc_byproducts）。
        update: 本次 Observe 状态增量（就地写入）。
    """
    byproducts = output.get("fc_byproducts") or {}

    # 1) 章节结构（六维度本地候选 + 章节→全局段落锚点）
    structure_pack = byproducts.get("structure") or {}
    structure = structure_pack.get("structure") or {}
    if structure:
        update["local_drafts"] = {
            **(state.get("local_drafts") or {}), **structure}
    section_paragraphs = structure_pack.get("section_paragraphs") or {}
    if section_paragraphs:
        update["section_paragraphs"] = {
            **(state.get("section_paragraphs") or {}), **section_paragraphs}

    # 后续全文/OCR/表格合并需要在"已含本次增量"的临时状态上进行
    current_state = dict(state)
    if update.get("local_drafts"):
        current_state["local_drafts"] = update["local_drafts"]

    # 2) 读中提取的全文：仅在尚无全文底本时采用
    fc_text = str(byproducts.get("text") or "")
    if fc_text.strip() and not (current_state.get("abs_text") or "").strip():
        update["abs_text"] = fc_text
        current_state["abs_text"] = fc_text

    # 3) 读中 OCR 页（可能是扫描件部分页/全部页，或文本件图表页）
    fc_ocr = byproducts.get("ocr_pages") or {}
    if fc_ocr:
        merged_ocr = dict(state.get("ocr_pages") or {})
        for raw_index, text in fc_ocr.items():
            text = str(text or "").strip()
            if text:
                merged_ocr[int(raw_index)] = text
        update["ocr_pages"] = merged_ocr
        current_state["ocr_pages"] = merged_ocr
        merged_text = _merge_ocr_text(current_state, merged_ocr)
        update["abs_text"] = merged_text
        current_state["abs_text"] = merged_text

    # 4) 读中提取的表格（去重后以块追加全文末尾）
    fc_tables = byproducts.get("tables") or []
    if fc_tables:
        table_texts = dict(state.get("table_texts") or {})
        for item in fc_tables:
            key = _table_item_key(item)
            if not key:
                continue
            table_texts[key] = {
                "page": item.get("page"),
                "index": int(item.get("index", 0)),
                "markdown": str(item.get("markdown") or "").strip(),
            }
        update["table_texts"] = table_texts
        current_state["table_texts"] = table_texts
        update["abs_text"] = _merge_table_text(current_state, table_texts)


def _is_original_trusted(state: dict, merged_ai_drafts: dict) -> bool:
    """判定原件直读结果是否可信任（只查完整性、跳过本地裁判的前置条件）。

    严格三条件，任一不满足即回退完整三重校验：
    1. tool_history 中存在成功的 ai_read_original；
    2. 未发生原件通道失败（original_read_failed 为假）；
    3. 规则启用的非本地权威维度在合并后 AI 草稿中均非空。

    Args:
        state: Agent 共享状态（不含本次观测增量）。
        merged_ai_drafts: 合并本次产出后的 AI 草稿。
    Returns:
        True 表示可走原件信任旁路。
    """
    if state.get("original_read_failed"):
        return False
    has_ok_original = any(
        item.get("tool") == TOOL_AI_READ_ORIGINAL
        and (item.get("output") or {}).get("ok")
        for item in state.get("tool_history") or []
    )
    if not has_ok_original:
        return False
    dims_cfg = (state.get("rule_detail") or {}).get("dimensions") or {}
    for dim in DIMENSION_KEYS:
        if not dims_cfg.get(dim, True):
            continue
        if dim in LOCAL_AUTHORITATIVE_DIMS:
            continue  # 参考文献等由本地确定性提取，不要求 AI 覆盖
        if not str((merged_ai_drafts or {}).get(dim) or "").strip():
            return False
    return True


def _layout_done_message(layout: dict) -> str:
    """把布局摘要压成一句中文进度文案。"""
    if layout.get("is_scanned"):
        return (f"原件布局检查完成：扫描件（共 {layout.get('page_count', 0)} 页），"
                "将逐页识别文字…")
    features = []
    if layout.get("has_two_columns"):
        features.append("双栏文本排版")
    figure_pages = layout.get("pages_with_images") or []
    if figure_pages:
        features.append(f"含 {len(figure_pages)} 个图表页")
    if features:
        return "原件布局检查完成：" + "、".join(features)
    return f"原件布局检查完成：普通文本（{str(layout.get('format', '')).upper()}）"


def _done_percent(state: dict, last: dict, tool_name: str, args: dict,
                  step: int, retry: bool) -> int:
    """完成百分比：动作自带锚点优先；OCR 按页码/泳道推进；其余取泳道锚点。

    优先级：
    1. 动作入队时显式打标 pct_end（预规划动作/关键词尾部/阶段5补救）；
    2. ocr_page 按四象限页码进度（未显式打标的扫描件/图表页 OCR）；
    3. 非重试动作按当前泳道的工具锚点表取终点；
    4. Refine 补救动作随步数在 83-89 段推进，且不低于最近一次校验锚点。

    Args:
        state: Agent 共享状态。
        last: 最近一条工具历史项（含动作自带的 pct_start/pct_end）。
        tool_name: 工具名。
        args: 本次调用参数。
        step: 当前步数。
        retry: 是否为同名工具重试。
    Returns:
        0-100 的完成百分比。
    """
    tagged_end = last.get("pct_end")
    if tagged_end is not None:
        return int(tagged_end)
    if tool_name == TOOL_OCR_PAGE:
        return ocr_progress_percent(state, args.get("page_index", 0), done=True)
    if not retry:
        pair = tool_progress_pair(state, tool_name)
        if pair:
            return pair[1]
    return remediation_percent(state, step)
