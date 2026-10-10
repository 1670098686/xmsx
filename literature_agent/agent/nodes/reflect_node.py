"""Reflect 节点：AI 主导解析的每轮单步编排器（队列驱动）。

编排权移交 agent.director，本节点只按当前状态选择阶段并做路由：
- 队列非空：直接放行执行，不做任何规划；
- 体检刚结束：首轮确定性播种（有原件通道第一步即 ai_read_original——
  AI 通读原件时在 function calling 内部循环中自主调用中间件取证；
  扫描件无通道逐页 OCR；其余先 extract_text；无模型纯本地链）；
- 原件读中循环成功：条件本地尾部——读中已取证的内容不重复，仅补扫描件
  缺页 OCR / 缺失全文提取 / AI 未给参考文献时的条件章节识别，最后始终
  本地 jieba 关键词；
- 维度齐全走信任校验；仍缺维度进入 Director 单步兜底循环；
- 文本/扫描基线到手（无原件通道/降级链路）：确定性结构化尾部
  （结构 → 关键词 → 图表页 OCR → 文本通道 AI 解读）；
- 无可安排动作：转 Verify，由三重校验 + Refine 兜底。
"""
from agent import director
from agent.nodes import emit
from agent.state import (
    ROUTE_ACT,
    ROUTE_VERIFY,
    TOOL_AI_READ_ORIGINAL,
    TOOL_DETECT_STRUCTURE,
    TOOL_EXTRACT_TEXT,
    TOOL_INSPECT_DOCUMENT,
    TOOL_OCR_PAGE,
)
from config import constants as C
from utils.logger import get_logger

logger = get_logger()


def reflect_node(state: dict) -> dict:
    """队列空时决定下一步单动作/动作链或收尾校验。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（planned_actions / lit_profile / max_steps /
        director_failures）；无增量时由路由转入 Verify。
    """
    # 致命失败（文件缺失/提取失败）→ 直接交 Verify/Finish 收尾
    if state.get("error"):
        return {}
    # 队列非空一律放行：Director 每轮只追加一个动作，护栏/尾部追加短链
    if state.get("planned_actions"):
        return {}
    if int(state.get("current_step", 0)) >= int(
            state.get("max_steps", C.AGENT_MAX_STEPS)):
        return {}

    layout = state.get("inspect_layout")
    if layout is None:
        return {}  # 防御：无布局无文本时 Verify 按致命失败收尾
    history = state.get("tool_history") or []
    original_ok = director._tool_ever_succeeded(state, TOOL_AI_READ_ORIGINAL)

    # ============ 原件读中循环成功：条件本地尾部（基线→关键词） ============
    if original_ok:
        update = _original_success_route(state, layout)
        if update is not None:
            return update
        # 路由返回 None 表示尾部无需补动作，落入信任校验/阶段5 单步兜底

    # 信任路径（维度齐全）：全部尾部动作跑完即转校验，只查完整性
    if state.get("ai_original_trusted"):
        return {}

    # 阶段 2：首轮播种（除体检外尚无任何工具执行）
    if not _post_inspect_started(history):
        return director.seed_after_inspect(state, layout)

    # 阶段 3：扫描件逐页 OCR 完成（仅降级路径——首轮无通道时 OCR 与尾部
    # 已一次性成链，不会走到这里）→ 接结构化尾部
    if (layout.get("is_scanned")
            and _ever_attempted(history, TOOL_OCR_PAGE)
            and not _ever_attempted(history, TOOL_DETECT_STRUCTURE)
            and not original_ok):
        return director.structure_tail_update(state, layout)

    # 阶段 4：全文提取完成（无原件通道/原件失败降级）→ 确定性结构化尾部
    if (director._tool_ever_succeeded(state, TOOL_EXTRACT_TEXT)
            and not _ever_attempted(history, TOOL_DETECT_STRUCTURE)
            and not original_ok):
        return director.structure_tail_update(state, layout)

    # 阶段 5：条件尾部结束后仍缺维度 → Director 单步兜底决策循环
    if original_ok:
        decision, failures = director.decide_next_action(state)
        update = {"director_failures": int(failures)}
        if decision.get("finalize"):
            logger.debug("Director 决定收尾：%s",
                         decision.get("reason", ""))
            return update
        update["planned_actions"] = [decision]
        logger.debug("Director 下一步：%s（非法决策累计 %s）",
                     decision.get("tool"), failures)
        return update

    # 其余情况（纯本地尾部执行完毕等）→ 转校验
    return {}


def _original_success_route(state: dict, layout: dict):
    """原件读中循环成功后的条件本地尾部路由。

    每次进入都依据当前状态实时计算缺失动作（读中 FC 循环已取到的全文/
    OCR/表格/章节由 Observe 写回状态，自动跳过）；无动作可补时返回 None
    交回主流程（维度齐全走信任校验；仍缺维度由阶段 5 单步兜底）。

    Args:
        state: Agent 共享状态。
        layout: 布局画像。
    Returns:
        状态增量；None 表示条件尾部已无动作。
    """
    actions = director.original_success_tail(state)
    if not actions:
        return None
    step = int(state.get("current_step", 0))
    # 一次性放宽步数：条件基线（扫描件可能补多页 OCR）+ 关键词
    # + 阶段5 单步兜底预留 + Refine 补救余量，防止中途被强制收尾
    new_max = max(
        int(state.get("max_steps", C.AGENT_MAX_STEPS)),
        step + len(actions)
        + C.AGENT_DIRECTOR_STEP_RESERVE + C.AGENT_REFINE_MAX_ATTEMPTS,
    )
    emit(state, C.AGENT_PCT_ORIGINAL_BASELINE,
         "AI 原件通读完成，正在补齐本地关键词与必要基线…")
    return {"planned_actions": actions, "max_steps": new_max}


def reflect_router(state: dict) -> str:
    """条件边：还有动作且未超步数 → Act；否则 → Verify。

    Args:
        state: 最新状态。
    Returns:
        ROUTE_ACT 或 ROUTE_VERIFY。
    """
    pending = state.get("planned_actions") or []
    if pending and int(state.get("current_step", 0)) < int(
            state.get("max_steps", C.AGENT_MAX_STEPS)):
        return ROUTE_ACT
    return ROUTE_VERIFY


def _ever_attempted(history: list, tool_name: str) -> bool:
    """历史中是否出现过该工具调用（不区分成功失败）。"""
    return any(item.get("tool") == tool_name for item in history)


def _post_inspect_started(history: list) -> bool:
    """体检之后是否已有任何工具被执行（用于识别首轮播种时机）。"""
    return any(
        item.get("tool") != TOOL_INSPECT_DOCUMENT for item in history
    )
