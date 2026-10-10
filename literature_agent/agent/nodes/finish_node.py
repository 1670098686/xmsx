"""Finish 节点：合并两套草稿产出最终报告（AI 优先、本地兜底）。"""
from agent.nodes import emit
from agent.prompts import DIMENSION_LABELS
from agent.state import (
    DIMENSION_KEYS,
    LOCAL_AUTHORITATIVE_DIMS,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_AI_READ_ORIGINAL,
)
from config import constants as C
from tool_layer import text_analysis
from utils.logger import get_logger

logger = get_logger()


def finish_node(state: dict) -> dict:
    """组装 final_report：平铺六维度 + 关键词 + 引擎标记 + 告警。

    合并策略：
    - 校验通过的维度优先采用 AI 深度解读，AI 缺失时用本地章节原文；
    - 校验最终仍未通过（unresolved_dims）的维度，AI 内容不可信，
      强制回退本地章节原文，本地也没有则留空并告警。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（final_report、engine、warnings）。
    """
    rule_detail = state.get("rule_detail") or {}
    dimensions_cfg = rule_detail.get("dimensions") or {}
    local_drafts = state.get("local_drafts") or {}
    ai_drafts = state.get("ai_drafts") or {}
    verify_result = state.get("verify_result") or {}
    unresolved = set(verify_result.get("unresolved_dims") or [])

    report: dict = {}
    used_ai = False
    missing: list[str] = []
    distrusted: list[str] = []

    def _finalize(key: str, text: str, from_ai: bool) -> str:
        """统一出口归一化：拼回 PDF/Word 硬换行、参考文献逐条成行。"""
        normalized = text_analysis.normalize_section_text(key, text or "")
        if from_ai and normalized:
            nonlocal used_ai
            used_ai = True
        return normalized

    for key in DIMENSION_KEYS:
        enabled = dimensions_cfg.get(key, True)
        if not enabled:
            report[key] = ""
            continue
        ai_text = (ai_drafts.get(key) or "").strip()
        local_text = (local_drafts.get(key) or "").strip()
        if key in unresolved:
            # 校验未通过：AI 内容降权，宁可采用可复现的本地基线
            report[key] = _finalize(key, local_text, False)
            if ai_text and local_text:
                distrusted.append(key)
            elif not local_text:
                missing.append(key)
        elif key in LOCAL_AUTHORITATIVE_DIMS:
            # 参考文献等确定性维度：本地提取为权威，AI 仅在本地缺失时兜底
            from_ai = bool(ai_text) and not local_text
            report[key] = _finalize(key, ai_text if from_ai else local_text,
                                    from_ai)
            if not report[key]:
                missing.append(key)
        elif ai_text:
            report[key] = _finalize(key, ai_text, True)
        elif local_text:
            report[key] = _finalize(key, local_text, False)
        else:
            report[key] = ""
            missing.append(key)

    engine = "agent_ai" if used_ai else "agent_local"
    # warnings 是追加 reducer，这里只能返回"本次新增"的告警
    new_warnings: list[str] = []
    if distrusted:
        names = "、".join(DIMENSION_LABELS.get(k, k) for k in distrusted)
        new_warnings.append(f"以下维度 AI 解读未通过校验，已改用本地章节原文：{names}")
    if missing and not verify_result.get("fatal"):
        names = "、".join(DIMENSION_LABELS.get(k, k) for k in missing)
        new_warnings.append(f"以下维度未解析到内容：{names}")
    if not verify_result.get("passed"):
        new_warnings.append(verify_result.get("message", "校验未通过"))

    existing_warnings = list(state.get("warnings") or [])
    all_warnings = existing_warnings + [
        w for w in new_warnings if w and w not in existing_warnings
    ]
    report["keywords"] = list(state.get("keywords") or [])
    report["engine"] = engine
    # 细粒度 AI 通道留痕：取首个成功的 AI 解读工具引擎
    # （agent_ai_file=原件直传；agent_ai=全文文本；空串=纯本地），
    # engine 字段仍只保留 agent_ai/agent_local 粗粒度，兼容业务层判断。
    ai_channel = ""
    for item in state.get("tool_history") or []:
        if item.get("tool") not in (TOOL_AI_READ_ORIGINAL, TOOL_AI_DEEP_ANALYZE):
            continue
        out = item.get("output") or {}
        if out.get("ok") and out.get("engine"):
            ai_channel = str(out["engine"])
            break
    report["ai_channel"] = ai_channel
    # 读中 function calling 留痕：取首个成功的 ai_read_original 输出
    # （fc_mode=cohabit 同框/staged 分段取证；fc_rounds 交互轮次；
    # fc_tools 模型通读中实际调用的本地中间件序列）
    fc_mode, fc_rounds, fc_tools = "", 0, []
    for item in state.get("tool_history") or []:
        if item.get("tool") != TOOL_AI_READ_ORIGINAL:
            continue
        out = item.get("output") or {}
        if out.get("ok"):
            fc_mode = str(out.get("fc_mode") or "")
            fc_rounds = int(out.get("fc_rounds") or 0)
            fc_tools = [
                {"name": str(trace.get("name", "")),
                 "args": trace.get("args") or {},
                 "ok": bool(trace.get("ok"))}
                for trace in (out.get("fc_tools") or [])
            ]
            break
    report["fc_mode"] = fc_mode
    report["fc_rounds"] = fc_rounds
    report["fc_tools"] = fc_tools
    # 首个致命 AI 错误原文随报告回传（网络/鉴权不可用、用户拒绝降级等），
    # 业务层据此弹窗向用户说明；无错误时为空串
    report["ai_error"] = str(state.get("ai_error") or "")
    report["warnings"] = all_warnings
    report["verified"] = bool(verify_result.get("passed"))
    # 校验模式留痕：original_trusted=原件直读成功只查完整性（AI 主导路径）；
    # full=完整三重校验（文本通道/本地链路/补救后路径）
    report["verification_mode"] = (
        "original_trusted" if verify_result.get("trusted_original") else "full"
    )

    emit(state, C.AGENT_PCT_FINISH,
         f"智能体解析完成（{engine}），正在写入解析报告…")

    # 用户友好总结：让控制台看到本次解析结果摘要
    engine_label = {
        "agent_ai": "AI 智能解析",
        "agent_local": "本地规则兜底",
        "local": "纯本地规则",
    }.get(engine, engine)
    verified = "已通过三重校验" if verify_result.get("passed") else "校验未通过"
    missing_label = ("（以下维度未解析：" + "、".join(
        DIMENSION_LABELS.get(k, k) for k in missing) + "）") if missing else ""
    logger.info("📝 解析完成：%s，%s%s", engine_label, verified, missing_label)
    if new_warnings:
        for w in new_warnings:
            logger.info("  ⚠️  提示：%s", w)
    return {"final_report": report, "engine": engine, "warnings": new_warnings}
