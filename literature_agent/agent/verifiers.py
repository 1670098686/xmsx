"""Verify 节点的三重校验（反幻觉核心）：

1. 完整性 verify_completeness：规则启用的维度必须有内容（AI 或本地任一）；
2. 一致性 verify_consistency：AI 解读与本地章节词集交叉比对，
   "长度悬殊 + 关键词几乎不重叠"同时成立才判矛盾，避免把 AI 合理扩写误杀；
3. 溯源 verify_traceability：AI 每段解读标注的段落锚点必须真实存在，
   且所引段落确实与解读内容词汇相关（乱标锚点也算幻觉）。

全部为无副作用纯函数，段落切分复用 tool_layer.text_analysis，便于单测。
"""
import jieba

from agent.state import DIMENSION_KEYS, LOCAL_AUTHORITATIVE_DIMS
from config import constants as C

# 三类校验问题的稳定标识（写入 refine_attempts 的键，也供 UI/日志识别）
ISSUE_MISSING = "missing"
ISSUE_CONSISTENCY = "consistency"
ISSUE_TRACEABILITY = "traceability"


def _content_words(text: str) -> set:
    """中文按 jieba 切词，保留长度 >=2 的词与连续英文/数字 token。

    Args:
        text: 任意中文/英文混合文本。
    Returns:
        词集合（用于 Jaccard / 覆盖率计算）。
    """
    words = set()
    for token in jieba.lcut(text or ""):
        token = token.strip()
        if len(token) >= 2:
            words.add(token.lower())
    return words


def _jaccard(left: set, right: set) -> float:
    """两个词集的 Jaccard 相似度；任一为空时返回 0。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def verify_completeness(enabled_dimensions: list, merged_drafts: dict) -> list:
    """校验一：启用维度是否都解析到了内容。

    Args:
        enabled_dimensions: 规则中启用的维度 key 列表。
        merged_drafts: 维度 → 最终文本（AI 与本地合并后的视图）。
    Returns:
        缺失维度 key 列表。
    """
    return [
        dim for dim in enabled_dimensions
        if not (merged_drafts.get(dim) or "").strip()
    ]


def verify_consistency(ai_drafts: dict, local_drafts: dict,
                       enabled_dimensions: list) -> list:
    """校验二：AI 解读不得与本地章节明显矛盾。

    判据（两条件同时成立）：
    - AI 文本长度超过本地章节 N 倍（疑似脱离原文自由发挥）；
    - 词集 Jaccard 低于阈值（几乎没有共同关键词）。

    Args:
        ai_drafts: AI 六维度解读。
        local_drafts: 本地规则识别的章节原文。
        enabled_dimensions: 启用维度。
    Returns:
        [{"dimension":key,"issue":ISSUE_CONSISTENCY,"reason":中文说明}]
    """
    problems = []
    for dim in enabled_dimensions:
        ai_text = (ai_drafts or {}).get(dim, "")
        local_text = (local_drafts or {}).get(dim, "")
        if not ai_text or not local_text:
            continue
        if len(local_text) < C.AGENT_VERIFY_MIN_LOCAL_CHARS:
            continue  # 本地章节疑似误切/过短，不具备比对基线价值
        overlap = _jaccard(_content_words(ai_text), _content_words(local_text))
        length_ratio = len(ai_text) / max(len(local_text), 1)
        if (length_ratio > C.AGENT_VERIFY_AI_LOCAL_LEN_RATIO
                and overlap < C.AGENT_VERIFY_OVERLAP_MIN):
            problems.append({
                "dimension": dim,
                "issue": ISSUE_CONSISTENCY,
                "reason": (f"AI 解读长度为本地章节 {length_ratio:.1f} 倍，"
                           f"关键词重叠仅 {overlap:.2f}，疑似脱离原文"),
            })
    return problems


def verify_traceability(ai_drafts: dict, citation_map: dict,
                        abs_text: str, enabled_dimensions: list) -> list:
    """校验三：AI 每段解读的原文锚点必须存在且内容相关。

    Args:
        ai_drafts: AI 六维度解读。
        citation_map: 维度 → AI 标注的段落索引列表。
        abs_text: 文献全文（按空行切分为段落）。
        enabled_dimensions: 启用维度。
    Returns:
        [{"dimension":key,"issue":ISSUE_TRACEABILITY,"reason":中文说明}]
    """
    paragraphs = _split_paragraphs(abs_text)
    problems = []
    for dim in enabled_dimensions:
        ai_text = (ai_drafts or {}).get(dim, "")
        if not ai_text:
            continue  # 无 AI 内容（纯本地模式）不做溯源要求
        anchors = (citation_map or {}).get(dim) or []
        if not anchors:
            problems.append({
                "dimension": dim,
                "issue": ISSUE_TRACEABILITY,
                "reason": "AI 解读未标注任何原文位置锚点",
            })
            continue
        invalid = [idx for idx in anchors if idx < 0 or idx >= len(paragraphs)]
        if invalid:
            problems.append({
                "dimension": dim,
                "issue": ISSUE_TRACEABILITY,
                "reason": f"原文位置锚点越界：{invalid}（全文共 {len(paragraphs)} 段）",
            })
            continue
        ai_words = _content_words(ai_text)
        # 口径一（源段落侧）：单个锚点段落词被 AI 解读命中的比例，
        # 对小段落严格有效；口径二（AI 侧）：AI 实词在全部所引段落
        # 并集中有据的比例，对 PDF 重建出的大段落必不可少——否则
        # 一段数百词时 AI 再忠实也无法在数学上达标。任一通过即可。
        best_source_cover = 0.0
        anchor_union = set()
        for idx in anchors:
            para_words = _content_words(paragraphs[idx])
            anchor_union |= para_words
            if para_words:
                best_source_cover = max(
                    best_source_cover,
                    len(ai_words & para_words) / len(para_words),
                )
        common_words = ai_words & anchor_union
        ai_side_cover = (
            len(common_words) / len(ai_words) if ai_words else 0.0)
        source_ok = best_source_cover >= C.AGENT_VERIFY_CITATION_COVER_MIN
        ai_side_ok = (
            ai_side_cover >= C.AGENT_VERIFY_CITATION_AI_COVER_MIN
            and len(common_words) >= C.AGENT_VERIFY_CITATION_COMMON_WORDS_MIN
        )
        if not (source_ok or ai_side_ok):
            problems.append({
                "dimension": dim,
                "issue": ISSUE_TRACEABILITY,
                "reason": (f"所引段落与解读词汇相关度过低"
                           f"（AI 侧 {ai_side_cover:.2f} / 原文侧 "
                           f"{best_source_cover:.2f}），锚点疑似随意标注"),
            })
    return problems


def run_all_verifications(state: dict) -> dict:
    """汇总三重校验，产出 Verify 节点统一结果。

    原件信任旁路（AI 主导解析）：ai_read_original 成功且启用的非本地维度
    齐全（state.ai_original_trusted）时，AI 直接通读了含版式/表格的原件，
    线性本地文本既无能力也无资格裁判它，故只跑完整性校验——非本地维度
    取 AI 草稿、参考文献等本地权威维度取本地草稿；一致性/溯源一律跳过。
    信任路径下静默全文提取失败（如扫描件无文本层）不判致命。

    Args:
        state: Agent 共享状态。
    Returns:
        {"passed","fatal","missing","problems","invalid_citations",
         "unresolved_dims","message","trusted_original"}
    """
    rule_detail = state.get("rule_detail") or {}
    enabled = [
        dim for dim in DIMENSION_KEYS
        if rule_detail.get("dimensions", {}).get(dim, True)
    ]
    ai_drafts = state.get("ai_drafts") or {}
    local_drafts = state.get("local_drafts") or {}
    trusted = bool(state.get("ai_original_trusted"))

    # 信任路径允许 abs_text 为空（扫描件原件直读成功、本地提不出文本层）；
    # 非信任路径全文为空是致命失败；state.error 始终致命
    if state.get("error") or (not state.get("abs_text") and not trusted):
        return {
            "passed": False, "fatal": True, "missing": enabled,
            "problems": [], "invalid_citations": [],
            "unresolved_dims": list(enabled),
            "message": state.get("error") or "文献全文为空，无法解析",
            "trusted_original": False,
        }

    if trusted:
        # 规则硬约束：扫描件经原件通道成功也必须有逐页 OCR 基线可供校验，
        # 所有页 OCR 均失败（本地无文本层）时判致命，不落空报告，
        # 交由业务层走本地兜底/报错
        layout = state.get("inspect_layout") or {}
        if layout.get("is_scanned") and not (state.get("abs_text") or "").strip():
            return {
                "passed": False, "fatal": True, "missing": enabled,
                "problems": [], "invalid_citations": [],
                "unresolved_dims": list(enabled),
                "message": "扫描件逐页 OCR 基线缺失，无法完成本地校验",
                "trusted_original": False,
            }
        # 非本地权威维度以 AI 原件通读为准；参考文献等本地权威维度优先取
        # 本地结构识别，AI 已在通读中给出而本地未条件触发识别时以 AI 兜底
        merged = {
            dim: ((local_drafts.get(dim) or ai_drafts.get(dim) or "")
                  if dim in LOCAL_AUTHORITATIVE_DIMS
                  else (ai_drafts.get(dim) or ""))
            for dim in enabled
        }
        missing = verify_completeness(enabled, merged)
        return {
            "passed": not missing,
            "fatal": False,
            "missing": missing,
            "problems": [],
            "invalid_citations": [],
            "unresolved_dims": sorted(set(missing)),
            "message": "" if not missing
                       else _summarize_failures(missing, [], []),
            "trusted_original": True,
        }

    merged = {
        dim: (ai_drafts.get(dim) or local_drafts.get(dim) or "")
        for dim in enabled
    }
    missing = verify_completeness(enabled, merged)
    problems = verify_consistency(ai_drafts, local_drafts, enabled)
    invalid_citations = verify_traceability(
        ai_drafts, state.get("citation_map") or {},
        state.get("abs_text", ""), enabled,
    )
    unresolved = set(missing)
    unresolved.update(p["dimension"] for p in problems)
    unresolved.update(p["dimension"] for p in invalid_citations)
    passed = not missing and not problems and not invalid_citations
    return {
        "passed": passed,
        "fatal": False,
        "missing": missing,
        "problems": problems,
        "invalid_citations": invalid_citations,
        "unresolved_dims": sorted(unresolved),
        "message": "" if passed else _summarize_failures(
            missing, problems, invalid_citations),
        "trusted_original": False,
    }


def _split_paragraphs(abs_text: str) -> list:
    """按空行切分原文段落，与 AI Prompt 中"按空行分段"的约定保持一致。

    Args:
        abs_text: 文献全文。
    Returns:
        非空段落列表。
    """
    return [part.strip() for part in (abs_text or "").split("\n\n") if part.strip()]


def _summarize_failures(missing: list, problems: list, invalid: list) -> str:
    """把三类问题拼成一条面向用户/日志的中文摘要。"""
    parts = []
    if missing:
        parts.append(f"缺失维度 {len(missing)} 个")
    if problems:
        parts.append(f"一致性疑点 {len(problems)} 处")
    if invalid:
        parts.append(f"溯源失效 {len(invalid)} 处")
    return "解析校验未通过：" + "，".join(parts)
