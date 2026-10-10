"""Agent 节点子包：Plan / Act / Observe / Reflect / Verify / Refine / Finish。"""
from agent.state import TOOL_AI_READ_ORIGINAL
from config import constants as C


def original_phase(state: dict) -> bool:
    """是否已进入"原件泳道"——曾经尝试过 ai_read_original（成功或失败）。

    原件泳道下的护栏/降级基线/Director 动作统一使用 61-79 的后段锚点；
    从未尝试直读的链路（离线、扫描件无通道）使用 10-75 的早段锚点。

    Args:
        state: Agent 共享状态。
    Returns:
        True 表示工具进度应取原件泳道锚点。
    """
    return any(
        item.get("tool") == TOOL_AI_READ_ORIGINAL
        for item in state.get("tool_history") or []
    )


def tool_progress_pair(state: dict, tool_name: str) -> tuple | None:
    """按当前泳道返回工具的 (进行中, 完成) 进度锚点。

    inspect_document / ai_read_original 全链路共享；其余工具按是否已尝试
    原件直读区分早段/原件泳道，保证任何链路下百分比单调不减。

    Args:
        state: Agent 共享状态。
        tool_name: 即将执行或刚完成的工具名。
    Returns:
        (start_percent, end_percent)；无锚点配置时返回 None。
    """
    shared = C.AGENT_TOOL_PROGRESS.get(tool_name)
    if tool_name in ("inspect_document", TOOL_AI_READ_ORIGINAL):
        return shared
    table = (C.AGENT_TOOL_PROGRESS_LATE if original_phase(state)
             else C.AGENT_TOOL_PROGRESS_EARLY)
    return table.get(tool_name)


def remediation_percent(state: dict, step: int) -> int:
    """Refine 补救动作（无显式打标、重试类工具）的进度百分比。

    基础值随步数在 85-89 段推进；同时不低于最近一次校验锚点——阶段5
    单步补救（80-84）后的再次校验取 88，其后的 Refine 补救动作不能因
    步数较小而跌回该锚点之下，保证百分比单调不倒挂。

    Args:
        state: Agent 共享状态（取 last_verify_percent）。
        step: 当前步数。
    Returns:
        进度百分比（85-89）。
    """
    percent = C.AGENT_PCT_REFINE_BASE + max(0, int(step) - 3)
    floor = state.get("last_verify_percent")
    if floor is not None:
        percent = max(percent, int(floor))
    return min(C.AGENT_PCT_REFINE_ACTION_CAP, percent)


def emit(state: dict, percent: int, message: str) -> None:
    """安全调用进度回调；回调异常不影响图执行。

    Args:
        state: 当前节点状态（取 progress_callback）。
        percent: 进度百分比 0-100。
        message: 进度描述。
    """
    callback = state.get("progress_callback")
    if callback is None:
        return
    try:
        callback(int(percent), str(message))
    except Exception:  # 进度回调属于旁路能力，失败只忽略不中断解析
        pass


def ocr_progress_percent(state: dict, page_index: int, done: bool) -> int:
    """OCR 动作的进度百分比（按泳道与场景分四段，均随页码单调递增）。

    - 扫描件 + 早段泳道（离线/无原件通道首轮）：10→28；
    - 扫描件 + 原件泳道（直读失败后的降级基线）：62→66；
    - 图表页 + 早段泳道（文本链结构化尾部）：56→61；
    - 图表页 + 原件泳道（降级结构化尾部补识别）：72→75。
      （预规划排定的图表页 OCR 由动作自带 _pct_start/_pct_end 打标，
      不会走到本函数。）

    Args:
        state: Agent 共享状态（取布局画像与工具历史）。
        page_index: 当前 OCR 页码（从 0）。
        done: False=识别进行中，True=本页完成（完成值比进行中高 1 个百分点）。
    Returns:
        进度百分比。
    """
    layout = state.get("inspect_layout") or {}
    index = max(0, int(page_index))
    late = original_phase(state)
    if layout.get("is_scanned"):
        total = max(1, int(layout.get("page_count") or 1))
        if late:
            start, end = (C.AGENT_PCT_OCR_LATE_SCAN_START,
                          C.AGENT_PCT_OCR_LATE_SCAN_END)
        else:
            start, end = C.AGENT_PCT_OCR_SCAN_START, C.AGENT_PCT_OCR_SCAN_END
        cap = end - 1
        percent = start + int((cap - start) * (index + 1) / total)
        return min(end, percent + (1 if done else 0))
    figure_pages = layout.get("pages_with_images") or []
    position = figure_pages.index(index) + 1 if index in figure_pages else 1
    total = max(1, len(figure_pages))
    if late:
        start, end = (C.AGENT_PCT_OCR_LATE_FIGURE_START,
                      C.AGENT_PCT_OCR_LATE_FIGURE_END)
    else:
        start, end = C.AGENT_PCT_OCR_FIGURE_START, C.AGENT_PCT_OCR_FIGURE_END
    cap = end - 1
    percent = start + int((cap - start) * position / total)
    return min(end, percent + (1 if done else 0))
