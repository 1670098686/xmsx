"""Verify 节点：三重校验（完整性 / 一致性 / 溯源）+ 条件路由。"""
from agent.nodes import emit, original_phase
from agent.state import (
    ROUTE_FINISH, ROUTE_REFINE, TOOL_AI_DEEP_ANALYZE, TOOL_AI_READ_ORIGINAL,
)
from agent.verifiers import run_all_verifications
from config import constants as C
from utils.logger import get_logger

logger = get_logger()


def _verify_percent(state: dict, first_round: bool) -> int:
    """按链路选择校验锚点，保证相对上一个工具完成值不倒挂。

    - 补救回环（非首轮）：统一用 88；
    - 原件信任旁路（维度齐全）：用 76（紧跟关键词收尾 72-74）；
    - 原件泳道完整校验（条件尾部后阶段5补维度 / 直读失败降级后）：用 77；
    - 早段泳道最后工具是 AI 解读（扫描件文本通道等）：用 77；
    - 早段泳道纯本地工具之后（离线）：用 62（紧跟关键词 55 / 图表 OCR 61）。

    额外钳制：上一个动作自带 pct_end 锚点（阶段5补救 80-84）时，首轮
    校验至少取 pct_end+1（封顶 88），避免 84→77 倒挂。实际流程中阶段5
    动作只可能出现在首次校验之后，届时 first_round=False 直接取 88。
    """
    if not first_round:
        percent = C.AGENT_PCT_VERIFY_RETRY
    elif state.get("ai_original_trusted"):
        percent = C.AGENT_PCT_VERIFY_TRUSTED
    else:
        history = state.get("tool_history") or []
        if original_phase(state) or not history:
            percent = C.AGENT_PCT_VERIFY_FIRST
        else:
            last_tool = (history[-1].get("tool") or "")
            if last_tool in (TOOL_AI_DEEP_ANALYZE, TOOL_AI_READ_ORIGINAL):
                percent = C.AGENT_PCT_VERIFY_FIRST
            else:
                # 纯本地工具（inspect / extract / detect / keywords / 图表 OCR）之后
                percent = C.AGENT_PCT_VERIFY_FIRST_NO_AI
    if first_round:
        history = state.get("tool_history") or []
        last_end = history[-1].get("pct_end") if history else None
        if last_end is not None:
            percent = max(
                percent,
                min(C.AGENT_PCT_VERIFY_RETRY, int(last_end) + 1))
    return percent


def verify_node(state: dict) -> dict:
    """执行三重校验并写入 verify_result。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（verify_result）。
    """
    result = run_all_verifications(state)
    first_round = not bool(state.get("verify_result"))
    percent = _verify_percent(state, first_round)
    # 用户友好日志 + 完整进度消息（UI 进度条仍需完整描述）
    if result["passed"]:
        if result.get("trusted_original"):
            logger.info("✅ 校验通过（原件信任旁路）：所有关键维度已确认")
            emit(state, percent, "原件直读校验通过：维度完整性确认")
        else:
            missing = result.get("missing") or []
            if missing:
                logger.info("✅ 校验通过（缺失维度：%s，可接受）", "、".join(missing))
                emit(state, percent,
                     f"三重校验通过（缺失：{'、'.join(missing)}，可接受）")
            else:
                logger.info("✅ 三重校验通过（完整性 / 一致性 / 原文溯源）")
                emit(state, percent, "三重校验通过：完整性 / 一致性 / 原文溯源")
    elif result["fatal"]:
        logger.info("❌ 校验致命失败，终止解析：%s", result.get("message", ""))
        emit(state, percent, f"解析终止：{result['message']}")
    else:
        missing = result.get("missing") or []
        problems = result.get("problems") or []
        missing_info = f" 缺失：{'、'.join(missing)}" if missing else ""
        problem_info = (f" 问题：{'、'.join(p.get('dimension','') for p in problems[:3])}"
                        if problems else "")
        logger.info("⚠️  校验未通过，进入补救阶段%s%s", missing_info, problem_info)
        emit(state, percent, f"校验未通过：{result['message']}")
    # DEBUG 级详细日志（默认控制台不显示）
    logger.debug(
        "三重校验结果：passed=%s fatal=%s；缺失维度=%s；问题=%s；无效锚点=%s",
        result.get("passed"), result.get("fatal"),
        result.get("missing") or [],
        [f"{p.get('dimension')}:{p.get('issue')}"
         for p in (result.get("problems") or [])],
        [f"{p.get('dimension')}:{p.get('issue')}"
         for p in (result.get("invalid_citations") or [])])
    return {"verify_result": result, "last_verify_percent": percent}


def verify_router(state: dict) -> str:
    """条件边：通过 → Finish；非致命失败且仍有补救预算 → Refine；否则 Finish。

    步数与单维度补救次数的双重上限在 Refine 节点内判定，这里只区分
    "是否还有补救资格"：达到 max_steps 后一律收尾，避免无效动作。

    Args:
        state: 最新状态。
    Returns:
        ROUTE_FINISH 或 ROUTE_REFINE。
    """
    result = state.get("verify_result") or {}
    if result.get("passed") or result.get("fatal"):
        return ROUTE_FINISH
    if int(state.get("current_step", 0)) >= int(
            state.get("max_steps", C.AGENT_MAX_STEPS)):
        return ROUTE_FINISH
    return ROUTE_REFINE
