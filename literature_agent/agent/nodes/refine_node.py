"""Refine 节点：根据三重校验结果生成补救动作（反幻觉回环的修证环节）。

补救策略：
- 缺失维度：参考文献等确定性维度提高精度重跑本地结构识别；
  其余维度有模型时并入 AI 重解读目标，无模型则同样提精度重采；
- 一致性/溯源问题：有模型时以"本地章节原文"为事实约束让 AI 重解读并重新标锚点；
  无模型时本地规则即事实基线，无法补救，带 unresolved 告警直接收尾。
提速：同一轮所有需 AI 补救的维度"合并为一次调用"——单维度走 local_section
（只喂该维度章节原文），多维度走 mixed_section（按维度拼接章节/全文），
把原来"N 个维度串行 N 次 AI 调用（每次 20-120 秒）"压缩为 1 次。
防死循环：同一"(维度, 问题类型)"补救次数超过 AGENT_REFINE_MAX_ATTEMPTS 后放弃。
"""
from agent.nodes import emit, remediation_percent
from agent.state import (
    LOCAL_AUTHORITATIVE_DIMS,
    ROUTE_ACT,
    ROUTE_FINISH,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_BASIS_LOCAL_SECTION,
    TOOL_BASIS_MIXED_SECTION,
    TOOL_DETECT_STRUCTURE,
)
from agent.verifiers import (
    ISSUE_CONSISTENCY,
    ISSUE_MISSING,
    ISSUE_TRACEABILITY,
)
from config import constants as C
from utils.logger import get_logger

logger = get_logger()


def refine_node(state: dict) -> dict:
    """按校验结果在补救预算内生成动作并入队。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（planned_actions、refine_attempts、warnings）。
    """
    result = state.get("verify_result") or {}
    attempts = dict(state.get("refine_attempts") or {})
    # AI 通道已被判定致命不可用（网络/鉴权等，或用户拒绝降级）时，
    # 与"未配置模型"同路径：只做本地提精度重采，不再排队必然失败的 AI 补救
    model = None if state.get("ai_unavailable") else state.get("model")
    precision = max(1, min(5, int(state.get("precision", 3))))

    actions: list[dict] = []
    exhausted: list[str] = []
    # 本轮需 AI 补救的维度（保持出现顺序、去重；预算仍按"维度+问题"分别计数）
    ai_targets: list[str] = []

    def _within_budget(key: str) -> bool:
        """该(维度,问题)是否还有补救次数。"""
        return attempts.get(key, 0) < C.AGENT_REFINE_MAX_ATTEMPTS

    def _bump(key: str) -> None:
        attempts[key] = attempts.get(key, 0) + 1

    def _reserve_ai_target(dim: str, budget_key: str) -> None:
        """登记一个待 AI 补救维度；预算耗尽则只记录 exhausted，不产生动作。"""
        if not _within_budget(budget_key):
            exhausted.append(budget_key)
            return
        _bump(budget_key)
        if dim not in ai_targets:
            ai_targets.append(dim)

    # 1) 缺失维度：确定性维度（参考文献）走本地提精度重采；其余维度有模型则
    #    并入 AI 重解读目标，无模型则统一提精度重采
    missing = result.get("missing") or []
    local_authority_missing = [
        dim for dim in missing if dim in LOCAL_AUTHORITATIVE_DIMS
    ]
    ai_missing = [
        dim for dim in missing
        if dim not in LOCAL_AUTHORITATIVE_DIMS
    ]

    if local_authority_missing:
        key = f"ALL:{ISSUE_MISSING}"
        if _within_budget(key):
            _bump(key)
            actions.append({
                "tool": TOOL_DETECT_STRUCTURE,
                "args": {"precision": min(5, precision + 1)},
                "reason": "参考文献等确定性维度缺失：提高本地识别精度重新采集",
            })
        else:
            exhausted.append(key)

    if ai_missing:
        if model:
            for dim in ai_missing:
                _reserve_ai_target(dim, f"{dim}:{ISSUE_MISSING}")
        else:
            key = f"ALL:{ISSUE_MISSING}"
            if _within_budget(key):
                _bump(key)
                actions.append({
                    "tool": TOOL_DETECT_STRUCTURE,
                    "args": {"precision": min(5, precision + 1)},
                    "reason": "完整性存在缺失：提高本地识别精度重新采集",
                })
            else:
                exhausted.append(key)

    # 2) 一致性 / 溯源问题：有模型时并入同一批 AI 重解读目标
    if model:
        issue_items = (result.get("problems") or []) + \
                      (result.get("invalid_citations") or [])
        for item in issue_items:
            dim = item.get("dimension")
            issue = item.get("issue", ISSUE_CONSISTENCY)
            if not dim or dim in LOCAL_AUTHORITATIVE_DIMS:
                continue
            _reserve_ai_target(dim, f"{dim}:{issue}")

    # 3) AI 补救合并为一次调用：单维度走最精准的章节通道；多维度走合并通道
    if model and ai_targets:
        local_drafts = state.get("local_drafts") or {}
        if len(ai_targets) == 1:
            dim = ai_targets[0]
            args = {"focus": dim}
            if (local_drafts.get(dim) or "").strip():
                # 校验问题维度：本地章节在，以章节原文为事实约束重新解读
                args["basis"] = TOOL_BASIS_LOCAL_SECTION
                reason = f"校验未通过：以本地章节原文为事实约束重新解读 {dim}"
            else:
                # 完整性缺失维度：本地无章节，只能基于全文补解读
                reason = f"完整性缺失 {dim}：基于全文让 AI 单独补解读"
            actions.append({
                "tool": TOOL_AI_DEEP_ANALYZE, "args": args, "reason": reason,
            })
        else:
            actions.append({
                "tool": TOOL_AI_DEEP_ANALYZE,
                "args": {"focus": ",".join(ai_targets),
                         "basis": TOOL_BASIS_MIXED_SECTION},
                "reason": (f"合并重解读 {len(ai_targets)} 个校验未通过/缺失维度"
                           "（一次 AI 调用完成补救）"),
            })

    update = {"refine_attempts": attempts}
    # 补救阶段百分比随已执行步数在 83-89 间推进，且不低于最近一次校验锚点
    refine_percent = remediation_percent(
        state, int(state.get("current_step", 0)))
    if actions:
        emit(state, refine_percent,
             f"校验未通过，正在执行 {len(actions)} 个补救动作…")
        update["planned_actions"] = actions
        logger.debug(
            "Refine 入队补救动作：%s；AI 目标维度=%s；预算内已耗尽=%s",
            [f"{a.get('tool')}({a.get('args')})" for a in actions],
            ai_targets, exhausted)
    else:
        emit(state, refine_percent, "补救预算用尽或无可行补救，带告警收尾")
        logger.debug("Refine 无可行补救动作；预算内已耗尽=%s", exhausted)
    return update


def refine_router(state: dict) -> str:
    """条件边：有补救动作 → Act；队列仍空 → Finish（防 Refine↔Verify 死循环）。

    Args:
        state: 最新状态。
    Returns:
        ROUTE_ACT 或 ROUTE_FINISH。
    """
    if state.get("planned_actions"):
        return ROUTE_ACT
    return ROUTE_FINISH
