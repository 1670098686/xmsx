"""Director：AI 主导解析的编排器（原件优先、读中 function calling 自主取证）。

职责（被 reflect_node 调用，自身不直接执行工具）：
- seed_after_inspect：体检后的首轮确定性播种——能用原件通道就第一步直传
  ai_read_original（常规路径全程仅 1 次通读 AI 调用，AI 在通读中通过
  function calling 自主调中间件取证）；扫描件无通道走逐页 OCR；其余先
  extract_text；无模型走纯本地链；
- original_success_tail：读中循环成功后的**条件**本地尾部——读中已取证的
  内容不重复：扫描件补未识别页 OCR、无全文时补 extract_text、AI 未给出
  参考文献时才条件触发 detect_structure，最后始终本地 jieba 关键词；
- decide_next_action：尾部结束后仍缺维度时的单步兜底循环；连续非法
  输出转确定性恢复动作；
- is_original_trusted：信任旁路判定（成功原件直读 + 非本地权威维度齐全）。

设计约束：本模块只编排、不访问数据库；工具实际执行仍走 Act 节点。
"""
import os

from agent import llm, prompts
from agent.nodes import emit, original_phase
from agent.profiler import describe_profile, profile_text
from agent.state import (
    DIMENSION_KEYS,
    LOCAL_AUTHORITATIVE_DIMS,
    OCR_MODE_AUTO,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_AI_READ_ORIGINAL,
    TOOL_DETECT_STRUCTURE,
    TOOL_EXTRACT_KEYWORDS,
    TOOL_EXTRACT_TABLES,
    TOOL_EXTRACT_TEXT,
    TOOL_OCR_PAGE,
)
from agent.tools import build_tool_registry
from config import constants as C
from tool_layer import ai_client
from utils.logger import get_logger

logger = get_logger()

# Director 可调度的工具白名单（inspect/ai_read_original/extract_keywords
# 由系统确定性掌握，不对 Director 暴露）
DIRECTOR_TOOLS = (
    TOOL_EXTRACT_TEXT,
    TOOL_EXTRACT_TABLES,
    TOOL_OCR_PAGE,
    TOOL_DETECT_STRUCTURE,
    TOOL_AI_DEEP_ANALYZE,
)


# ================= 维度覆盖与通道判定 =================

def enabled_dims(state: dict) -> list:
    """规则启用的全部维度 key（默认全开）。"""
    dims_cfg = (state.get("rule_detail") or {}).get("dimensions") or {}
    return [dim for dim in DIMENSION_KEYS if dims_cfg.get(dim, True)]


def ai_focus_dims(state: dict) -> list:
    """启用维度中交给 AI 的部分（剔除参考文献等本地权威维度）。"""
    return [dim for dim in enabled_dims(state)
            if dim not in LOCAL_AUTHORITATIVE_DIMS]


def missing_ai_dims(state: dict) -> list:
    """已启用且应交给 AI、但当前 AI 草稿仍为空的维度（保持启用顺序）。"""
    ai_drafts = state.get("ai_drafts") or {}
    return [dim for dim in ai_focus_dims(state)
            if not str(ai_drafts.get(dim) or "").strip()]


def original_read_tool(state: dict) -> str:
    """决定"原件直读"是否可用及对应工具（移植自旧 Reflect）。

    - file_id 托管通道：PDF/DOCX/TXT 均可，走 ai_read_original；
    - file_data 内联通道：仅 PDF 走 ai_read_original，非 PDF 退回文本通道；
    - 仅文本/自动判定无通道：ai_deep_analyze；未配置模型返回空串。

    Args:
        state: Agent 共享状态。
    Returns:
        TOOL_AI_READ_ORIGINAL / TOOL_AI_DEEP_ANALYZE / ""
    """
    model = state.get("model")
    if not model:
        return ""
    channel = ai_client.resolve_file_channel(model)
    if channel == ai_client.FILE_CHANNEL_ID:
        return TOOL_AI_READ_ORIGINAL
    if channel == ai_client.FILE_CHANNEL_DATA:
        is_pdf = os.path.splitext(
            state.get("lit_path", ""))[1].lower() == ".pdf"
        return TOOL_AI_READ_ORIGINAL if is_pdf else TOOL_AI_DEEP_ANALYZE
    return TOOL_AI_DEEP_ANALYZE


def is_original_trusted(state: dict) -> bool:
    """原件直读结果是否可走信任旁路（只查完整性）。

    三条件同时成立：存在成功的 ai_read_original；未发生通道失败降级；
    规则启用的非本地权威维度 AI 草稿均非空。任一不满足即回退完整三重校验。

    Args:
        state: Agent 共享状态。
    Returns:
        True 表示信任旁路可用。
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
    return not missing_ai_dims(state)


# ================= 首轮确定性播种 =================

def seed_after_inspect(state: dict, layout: dict) -> dict:
    """体检完成后的首轮播种（不调 LLM）。

    - 模型原件通道可用（含扫描件，由服务端视觉读版式）：仅排
      ai_read_original，focus 为启用维度剔除本地权威维度；
    - 扫描件但无原件通道：逐页 ocr_page 链 + 结构化尾部动作；
    - 其余：仅排 extract_text，文本到手后由结构化尾部接续；
    - 无模型：同上两条链路，但尾部不含任何 AI 动作。

    Args:
        state: Agent 共享状态。
        layout: inspect_document 布局摘要。
    Returns:
        状态增量（planned_actions，扫描件另含 lit_profile/max_steps）。
    """
    focus = enabled_dims(state)
    model = state.get("model")

    if model and original_read_tool(state) == TOOL_AI_READ_ORIGINAL:
        emit(state, C.AGENT_PCT_ORIGINAL_READ_START,
             "布局检查完成，AI 正在通读文献原件（原件直传，优先保留版式/"
             "表格/图表信息；通读中可自主调用中间件取证）…")
        return {"planned_actions": [{
            "tool": TOOL_AI_READ_ORIGINAL,
            # FC 读中循环输出全部启用维度（含参考文献）：AI 给出参考文献
            # 后本地不再跑 detect_structure，仅 AI 缺失时本地条件兜底
            "args": {"focus": ",".join(focus)},
            "reason": "第一步直接把文献原件交给大模型通读并结构化解读"
                      "（读中可自主调表格/OCR/全文/章节中间件）",
        }]}

    if layout.get("is_scanned"):
        return _scanned_ocr_seed(state, layout)

    emit(state, C.AGENT_PCT_OCR_SCAN_START,
         "布局检查完成：文本型原件，开始提取全文…")
    return {"planned_actions": [{
        "tool": TOOL_EXTRACT_TEXT, "args": {},
        "reason": "无原件直传通道：先取得文献全文，再走结构化解析链路",
    }]}


def _scanned_ocr_seed(state: dict, layout: dict) -> dict:
    """扫描件且无原件通道：逐页 OCR 链 + 结构化尾部（确定性规划）。"""
    precision = max(1, min(5, int(state.get("precision", 3))))
    focus = ai_focus_dims(state)
    page_count = max(1, int(layout.get("page_count") or 1))
    actions = _ocr_chain_actions(page_count)
    actions.extend(_structure_tail_actions(
        state, precision, C.PARSE_KEYWORD_TOP_N, focus,
        figure_pages=(),
    ))
    emit(state, C.AGENT_PCT_OCR_SCAN_START,
         f"布局检查完成：扫描件（共 {page_count} 页），规划 {len(actions)} 个动作"
         "（逐页识别文字 → 结构化解析）")
    return {
        "planned_actions": actions,
        "lit_profile": {
            "kind": "scanned_paper", "kind_label": "扫描件文献",
            "char_count": 0, "heading_count": 0, "dense_headings": False,
            "keyword_top_n": C.PARSE_KEYWORD_TOP_N,
        },
        "max_steps": max(
            int(state.get("max_steps", C.AGENT_MAX_STEPS)),
            page_count + C.AGENT_SCANNED_STEP_TAIL,
        ),
    }


def _ocr_chain_actions(page_count: int) -> list:
    """构造扫描件逐页 OCR 动作列表。"""
    return [
        {
            "tool": TOOL_OCR_PAGE,
            "args": {"page_index": index, "mode": OCR_MODE_AUTO},
            "reason": f"扫描件第 {index + 1}/{page_count} 页逐页识别文字",
        }
        for index in range(max(1, int(page_count)))
    ]


def scanned_ocr_chain_for_degrade(layout: dict) -> list:
    """原件直传失败后的扫描件基线：逐页 OCR 动作（供 Act 降级入队）。"""
    return _ocr_chain_actions(max(1, int((layout or {}).get("page_count") or 1)))


# ================= 结构化尾部（文本/扫描件基线链路） =================

def structure_tail_update(state: dict, layout: dict) -> dict:
    """全文/逐页 OCR 文本到手后的确定性结构化尾部。

    顺序：本地章节结构 → 本地关键词 →（图表页 OCR）→ 文本通道 AI 解读。
    AI 已被判不可用（ai_unavailable）时尾部不含 AI 动作。文献画像与步数
    调整沿用旧策略（短篇收紧、含图表页放宽）。

    Args:
        state: Agent 共享状态。
        layout: 布局摘要（取图表页）。
    Returns:
        状态增量（planned_actions、lit_profile、max_steps）。
    """
    precision = max(1, min(5, int(state.get("precision", 3))))
    focus = ai_focus_dims(state)
    profile = profile_text(state.get("abs_text") or "")
    actions = _structure_tail_actions(
        state, precision, profile["keyword_top_n"], focus,
        figure_pages=tuple((layout or {}).get("pages_with_images") or []),
    )

    update = {"planned_actions": actions, "lit_profile": profile}
    current_max = int(state.get("max_steps", C.AGENT_MAX_STEPS))
    needed_steps = int(state.get("current_step", 0)) + len(actions) \
        + C.AGENT_REFINE_MAX_ATTEMPTS + 1
    figure_count = len((layout or {}).get("pages_with_images") or [])
    if profile["kind"] == C.LIT_KIND_SHORT:
        update["max_steps"] = min(
            current_max, max(5, needed_steps + figure_count))
    elif needed_steps > current_max:
        update["max_steps"] = needed_steps

    if state.get("model") and not state.get("ai_unavailable") and focus:
        suffix = "含 AI 全文文本深度解读"
    else:
        suffix = "纯本地工具链"
    figure_pages = (layout or {}).get("pages_with_images") or []
    if figure_pages:
        suffix += f"，含 {len(figure_pages)} 页图表识别"
    # 原件泳道（直读失败降级后的结构化尾部）：规划提示须不低于降级基线段
    # 终点（OCR/extract 完成 66），取护栏 detect 起点 67；早段泳道仍用 24
    plan_percent = (C.AGENT_TOOL_PROGRESS_LATE[TOOL_DETECT_STRUCTURE][0]
                    if original_phase(state) else C.AGENT_PCT_REFLECT)
    emit(state, plan_percent,
         f"文献画像：{describe_profile(profile)}；规划 {len(actions)} 个动作（{suffix}）")
    logger.debug(
        "结构化尾部动作链（%s，%s）：%s",
        profile.get("kind"), suffix,
        " → ".join(str(action.get("tool")) for action in actions))
    return update


def _structure_tail_actions(state: dict, precision: int, keyword_top_n: int,
                            focus: list, figure_pages: tuple) -> list:
    """结构 → 关键词 → 图表页 OCR →（可选）文本通道 AI 解读。"""
    actions = [
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": precision},
         "reason": "先用可复现的本地规则锁定章节与六维度原文"},
        {"tool": TOOL_EXTRACT_KEYWORDS, "args": {"top_n": int(keyword_top_n)},
         "reason": "本地 TF-IDF 关键词，作为反幻觉交叉校验基线"},
    ]
    for page_index in figure_pages:
        actions.append({
            "tool": TOOL_OCR_PAGE,
            "args": {"page_index": int(page_index), "mode": OCR_MODE_AUTO},
            "reason": f"图表页第 {int(page_index) + 1} 页补充识别图内文字",
        })
    # ai_unavailable（网络/鉴权致命错误、用户拒绝降级）时不再排必败的 AI
    if (state.get("model") and not state.get("ai_unavailable") and focus):
        actions.append({
            "tool": TOOL_AI_DEEP_ANALYZE,
            "args": {"focus": ",".join(focus)},
            "reason": "在本地基线之上让 AI 通读全文文本深度解读"
                      "（参考文献以本地提取为准）",
            # 仅确定性尾部的整体解读允许按本地章节充实度裁剪 focus；
            # Director 单步决策的补解读不裁剪（缺失维度本身就是目标）
            "shrink_focus": True,
        })
    return actions


# ================= 原件读中循环成功后的条件本地尾部 =================

def fc_tools_used(state: dict) -> dict:
    """汇总成功 ai_read_original 读中循环的工具留痕。

    Args:
        state: Agent 共享状态。
    Returns:
        {"names": set(读中执行过的工具名), "ocr_pages": set(读中 OCR 过的
        页码索引，含失败页)}，供条件尾部跳过已取证内容。
    """
    names: set = set()
    ocr_pages: set = set()
    for item in state.get("tool_history") or []:
        if item.get("tool") != TOOL_AI_READ_ORIGINAL:
            continue
        output = item.get("output") or {}
        if not output.get("ok"):
            continue
        for trace in output.get("fc_tools") or []:
            name = trace.get("name")
            if not name:
                continue
            names.add(name)
            if name == TOOL_OCR_PAGE:
                try:
                    ocr_pages.add(int((trace.get("args") or {}).get(
                        "page_index")))
                except (TypeError, ValueError):
                    pass
    return {"names": names, "ocr_pages": ocr_pages}


def original_success_tail(state: dict) -> list:
    """原件读中循环成功后的条件本地尾部（本地参与最小化）。

    读中循环（含 staged 分段取证）已在 FC 内部执行过的取证一律不重复：
    - 扫描件：补齐读中循环与外层均未识别的剩余页 OCR（逐页基线缺失会被
      Verify 判致命，不能省）；
    - 其余格式：state 尚无全文且读中/外层都没提取过时，补一次
      extract_text（供 jieba 关键词与三重校验）；
    - detect_structure 条件触发：仅当参考文献维度启用、AI 草稿与本地草稿
      都没有参考文献、且读中/外层都未跑过章节识别时，补一次；
    - extract_keywords 始终本地执行（尚未有结果时），关键词以本地 jieba
      入库。

    条件基线动作统一打 AGENT_PCT_ORIGINAL_BASELINE 锚点；关键词打
    72-74 段锚点。无需任何动作时返回空列表（直接转校验/阶段5）。

    Args:
        state: Agent 共享状态。
    Returns:
        已打进度锚点的有序动作列表（可能为空）。
    """
    layout = state.get("inspect_layout") or {}
    history = state.get("tool_history") or []
    fc = fc_tools_used(state)
    actions: list = []

    # 1) 本地基线文本：扫描件补页 OCR / 其余补全文提取
    if layout.get("is_scanned"):
        page_count = max(1, int(layout.get("page_count") or 1))
        attempted = {
            int(index) for index in (state.get("ocr_pages") or {}).keys()
        }
        for item in history:
            if item.get("tool") == TOOL_OCR_PAGE:
                index = (item.get("args") or {}).get("page_index")
                if index is not None:
                    attempted.add(int(index))
        attempted |= fc["ocr_pages"]
        for page_index in range(page_count):
            if page_index in attempted:
                continue
            actions.append({
                "tool": TOOL_OCR_PAGE,
                "args": {"page_index": page_index, "mode": OCR_MODE_AUTO},
                "reason": f"本地基线：补识别扫描件第 {page_index + 1}/"
                          f"{page_count} 页",
            })
    elif (not (state.get("abs_text") or "").strip()
            and not _ever_attempted(history, TOOL_EXTRACT_TEXT)
            and TOOL_EXTRACT_TEXT not in fc["names"]):
        actions.append({
            "tool": TOOL_EXTRACT_TEXT, "args": {},
            "reason": "本地基线：补取全文供关键词与三重校验",
        })

    # 2) 条件章节识别：仅 AI/本地都没有参考文献时触发
    dims_cfg = (state.get("rule_detail") or {}).get("dimensions") or {}
    refs_enabled = dims_cfg.get("reference_list", True)
    ai_has_ref = bool(str(
        (state.get("ai_drafts") or {}).get("reference_list") or "").strip())
    local_has_ref = bool(str(
        (state.get("local_drafts") or {}).get("reference_list") or "").strip())
    detect_done = (_ever_attempted(history, TOOL_DETECT_STRUCTURE)
                   or TOOL_DETECT_STRUCTURE in fc["names"])
    if (refs_enabled and not ai_has_ref and not local_has_ref
            and not detect_done):
        precision = max(1, min(5, int(state.get("precision", 3))))
        actions.append({
            "tool": TOOL_DETECT_STRUCTURE,
            "args": {"precision": precision},
            "reason": "AI 未给出参考文献：本地规则条件触发章节结构识别",
        })

    tagged = tag_action_span(
        actions, C.AGENT_PCT_ORIGINAL_BASELINE,
        C.AGENT_PCT_ORIGINAL_BASELINE) if actions else []

    # 3) 本地关键词始终执行（含表格/图表补充文本，以本地结果入库）
    if (not state.get("keywords")
            and not _tool_ever_succeeded(state, TOOL_EXTRACT_KEYWORDS)):
        tagged.append(keyword_tail_action())
    return tagged


def keyword_tail_action() -> dict:
    """构造关键词收尾动作并打 72-74 锚点（条件基线动作之后执行）。

    关键词始终以本地 jieba 结果入库；此时全文已含读中取证/条件基线取得
    的表格、OCR 补充文本，关键词覆盖更全。

    Returns:
        带进度锚点的 extract_keywords 动作。
    """
    action = {
        "tool": TOOL_EXTRACT_KEYWORDS,
        "args": {"top_n": C.PARSE_KEYWORD_TOP_N},
        "reason": "本地 TF-IDF 关键词（含读中取证补充文本，以本地结果入库）",
    }
    return tag_action_pct(action, C.AGENT_PCT_KEYWORD_TAIL_START,
                          C.AGENT_PCT_KEYWORD_TAIL_END)


def _ever_attempted(history: list, tool_name: str) -> bool:
    """历史中是否出现过该工具调用（不区分成功失败）。"""
    return any(item.get("tool") == tool_name for item in (history or []))


# ================= 动作进度打标 =================

def tag_action_pct(action: dict, start: int, end: int) -> dict:
    """给动作复制并打上进度锚点；Act/Observe 优先读取以保证多动作不倒挂。

    Args:
        action: 原始规划动作。
        start: 该动作"进行中"百分比。
        end: 该动作"完成"百分比。
    Returns:
        含 _pct_start/_pct_end 的新动作（不修改入参）。
    """
    tagged = dict(action)
    tagged["_pct_start"] = int(start)
    tagged["_pct_end"] = int(end)
    return tagged


def tag_action_span(actions: list, lo: int, hi: int) -> list:
    """把一组动作在 [lo, hi] 段内按数量均分打标。

    首动作起点恰为 lo、末动作终点恰为 hi，相邻动作首尾相接，
    多个连续动作执行时百分比严格单调不减。

    Args:
        actions: 有序动作列表。
        lo: 段起点。
        hi: 段终点。
    Returns:
        打标后的新动作列表（顺序不变）。
    """
    if not actions:
        return []
    total = hi - lo + 1
    count = len(actions)
    tagged = []
    for index, action in enumerate(actions):
        start = lo + total * index // count
        end = lo + total * (index + 1) // count - 1
        # 段宽不足动作数（如条件基线多个动作共用 71 单点）时，均分公式会
        # 让前序动作 end 低于自身 start（进度倒挂）；钳到不低于 start
        tagged.append(tag_action_pct(
            action, start, min(hi, max(start, end))))
    return tagged


# ================= Director 单步决策（LLM + 确定性恢复） =================

def _executed_summary(history: list) -> list:
    """把工具历史压成 Director 提示词需要的已执行动作摘要。"""
    return [
        {
            "step": item.get("step"),
            "tool": item.get("tool"),
            "ok": bool((item.get("output") or {}).get("ok")),
            "observation": str(item.get("observation") or "")[:200],
        }
        for item in history
    ]


def decide_next_action(state: dict) -> tuple:
    """让 Director LLM 单步决定下一个工具动作。

    覆盖已完整时返回 (finalize, 0)（不经 LLM）；模型不可用/非法输出/
    被护栏拒绝时返回确定性恢复动作（或 finalize），并累加 director_failures。

    Args:
        state: Agent 共享状态。
    Returns:
        (动作, 累计非法决策次数)：动作为 {"tool","args","reason"} 或
        {"finalize": True}。第二个元素用于 Reflect 持久化 director_failures。
    """
    missing = missing_ai_dims(state)
    if not missing:
        return {"finalize": True, "reason": "启用维度已全部覆盖"}, 0

    failures = int(state.get("director_failures") or 0)
    action = None
    if failures < C.AGENT_DIRECTOR_MAX_LLM_FAILURES and state.get("model") \
            and not state.get("ai_unavailable"):
        action = _ask_llm_director(state, missing)

    if action is not None:
        guarded = apply_director_guard(action, state)
        if guarded is not None:
            return _tag_stage5(guarded), failures
        logger.info("Director 决策被护栏拒绝：%s", action)
        failures += 1
    else:
        failures += 1

    recovery = _recovery_action(state, missing)
    if recovery is None:
        return {"finalize": True,
                "reason": "无可行恢复动作，交校验/本地结果收尾"}, failures
    return _tag_stage5(recovery), failures


def _tag_stage5(action: dict) -> dict:
    """阶段5 单步补救动作统一打 80-84 段锚点（完整校验 77 之后，不倒挂）。"""
    return tag_action_pct(action, C.AGENT_PCT_STAGE5_START,
                          C.AGENT_PCT_STAGE5_END)


def _ask_llm_director(state: dict, missing: list) -> dict | None:
    """组装上下文调用 LLM 单步规划；调用/解析失败返回 None。"""
    model = state.get("model")
    layout = state.get("inspect_layout") or {}
    page_count = int(layout.get("page_count") or 0)
    tool_descriptions = _director_tool_descriptions(model)
    user_prompt = prompts.build_director_user_prompt(
        layout=layout,
        enabled_dims=enabled_dims(state),
        missing_dims=missing,
        executed=_executed_summary(state.get("tool_history") or []),
        tool_descriptions=tool_descriptions,
        remaining_steps=max(
            0, int(state.get("max_steps", C.AGENT_MAX_STEPS))
            - int(state.get("current_step", 0))),
        char_count=len(state.get("abs_text") or ""),
        has_abs_text=bool((state.get("abs_text") or "").strip()),
        table_texts_count=len(state.get("table_texts") or {}),
    )
    try:
        return llm.plan_next_action(model, user_prompt, page_count)
    except Exception as exc:  # 规划属旁路能力：任何异常转确定性恢复
        logger.warning("Director LLM 决策异常，转确定性恢复：%s", exc)
        return None


def _director_tool_descriptions(model: dict) -> list[str]:
    """构造 Director 工具池说明（仅白名单工具）。"""
    registry = build_tool_registry(model)
    descriptions = []
    for name in DIRECTOR_TOOLS:
        tool = registry.get(name)
        if tool is None:
            continue
        try:
            fields = tool.args_schema.model_fields
            hint = "(" + ", ".join(
                key for key in fields if key not in ("file_path",)) + ")"
        except Exception:
            hint = ""
        first_line = (tool.description or "").strip().splitlines()[0] \
            if tool.description else ""
        descriptions.append(f"{tool.name}{hint}：{first_line}")
    return descriptions


def apply_director_guard(action: dict, state: dict) -> dict | None:
    """校验 Director 给出的单动作；非法/重复/越界一律拒绝（返回 None）。

    - 只允许白名单工具（inspect/ai_read_original/extract_keywords 隐藏）；
    - 同签名动作已在外层历史或读中 FC 循环中执行过则拒绝（防原地打转）；
    - ai_deep_analyze：focus 只允许"当前缺失的非本地权威维度"；全文为空/
      AI 不可用时拒绝；
    - detect_structure：全文为空拒绝，精度钳制 1-5；
    - ocr_page：仅 PDF、页码不越界；
    - extract_tables：页码 -1 或不越界，max_tables 钳制 1~上限。

    Args:
        action: LLM 给出的 {"tool","args","reason"}。
        state: Agent 共享状态。
    Returns:
        规范化后的动作；拒绝时返回 None。
    """
    if not isinstance(action, dict):
        return None
    tool_name = action.get("tool")
    if tool_name not in DIRECTOR_TOOLS:
        return None
    args = dict(action.get("args") or {})
    layout = state.get("inspect_layout") or {}
    page_count = int(layout.get("page_count") or 0)

    if tool_name == TOOL_EXTRACT_TEXT:
        safe_args = {}
    elif tool_name == TOOL_DETECT_STRUCTURE:
        if not (state.get("abs_text") or "").strip():
            return None
        try:
            precision = int(args.get("precision", state.get("precision", 3)))
        except (TypeError, ValueError):
            return None
        safe_args = {"precision": max(1, min(5, precision))}
    elif tool_name == TOOL_EXTRACT_TABLES:
        try:
            page_index = int(args.get("page_index", -1))
        except (TypeError, ValueError):
            return None
        if page_count > 0 and not (page_index == -1 or 0 <= page_index < page_count):
            return None
        try:
            max_tables = int(args.get("max_tables", C.AGENT_TABLE_MAX_DEFAULT))
        except (TypeError, ValueError):
            max_tables = C.AGENT_TABLE_MAX_DEFAULT
        safe_args = {"page_index": page_index,
                     "max_tables": max(1, min(C.AGENT_TABLE_MAX_DEFAULT,
                                             max_tables))}
    elif tool_name == TOOL_OCR_PAGE:
        if page_count <= 0:
            return None  # 仅 PDF 可 OCR
        try:
            page_index = int(args.get("page_index"))
        except (TypeError, ValueError):
            return None
        if not 0 <= page_index < page_count:
            return None
        mode = str(args.get("mode", OCR_MODE_AUTO))
        if mode not in ("auto", "local", "ai"):
            mode = OCR_MODE_AUTO
        safe_args = {"page_index": page_index, "mode": mode}
    else:  # TOOL_AI_DEEP_ANALYZE
        if not (state.get("abs_text") or "").strip():
            return None
        if state.get("ai_unavailable"):
            return None
        # 阶段5 单步兜底只允许补当前缺失的非本地权威维度
        allowed = set(missing_ai_dims(state))
        focus_dims = [
            part.strip() for part in str(args.get("focus", "")).split(",")
            if part.strip() in allowed
        ]
        if not focus_dims:
            return None
        safe_args = {"focus": ",".join(focus_dims)}

    if _signature_executed(state, tool_name, safe_args):
        return None
    return {
        "tool": tool_name,
        "args": safe_args,
        "reason": str(action.get("reason", ""))[:120],
    }


def _signature_executed(state: dict, tool_name: str, args: dict) -> bool:
    """同签名动作是否已执行过（页码/精度/focus 等可比参数）。

    同时检查外层工具历史与 ai_read_original 读中 FC 循环的工具留痕，
    避免阶段5 重复调度模型在通读中已经调用过的取证工具。
    """
    signature = (tool_name, tuple(sorted(
        (key, value) for key, value in args.items()
        if key in ("page_index", "mode", "precision", "focus", "max_tables")
    )))

    def _same_signature(history_args: dict) -> bool:
        history_sig = (tool_name, tuple(sorted(
            (key, value) for key, value in (history_args or {}).items()
            if key in ("page_index", "mode", "precision", "focus", "max_tables")
        )))
        return history_sig == signature

    for item in state.get("tool_history") or []:
        if item.get("tool") == tool_name and _same_signature(
                item.get("args") or {}):
            return True
        # 读中 FC 循环内执行过的同名同参工具
        if item.get("tool") == TOOL_AI_READ_ORIGINAL \
                and (item.get("output") or {}).get("ok"):
            for trace in (item.get("output") or {}).get("fc_tools") or []:
                if trace.get("name") == tool_name \
                        and _same_signature(trace.get("args") or {}):
                    return True
    return False


def _recovery_action(state: dict, missing: list) -> dict | None:
    """Director 不可用时的确定性恢复：先补全文基线，再按缺失维度 AI 补解读。

    Args:
        state: Agent 共享状态。
        missing: 待补 AI 维度。
    Returns:
        单动作；无可行动作（AI 不可用等）返回 None。
    """
    layout = state.get("inspect_layout") or {}
    if not (state.get("abs_text") or "").strip():
        if layout.get("is_scanned"):
            page_count = max(1, int(layout.get("page_count") or 1))
            done_pages = {
                int(index) for index in (state.get("ocr_pages") or {}).keys()
            }
            next_page = next(
                (index for index in range(page_count)
                 if index not in done_pages), None)
            if next_page is not None:
                return {
                    "tool": TOOL_OCR_PAGE,
                    "args": {"page_index": next_page, "mode": OCR_MODE_AUTO},
                    "reason": f"确定性恢复：继续逐页识别第 {next_page + 1} 页",
                }
        elif not _tool_ever_succeeded(state, TOOL_EXTRACT_TEXT):
            return {
                "tool": TOOL_EXTRACT_TEXT, "args": {},
                "reason": "确定性恢复：先补取文献全文再补维度",
            }
        return None
    if state.get("model") and not state.get("ai_unavailable") and missing:
        return {
            "tool": TOOL_AI_DEEP_ANALYZE,
            "args": {"focus": ",".join(missing)},
            "reason": "确定性恢复：基于全文让 AI 补齐缺失维度",
        }
    return None


def _tool_ever_succeeded(state: dict, tool_name: str) -> bool:
    """历史中该工具是否成功执行过。"""
    return any(
        item.get("tool") == tool_name and (item.get("output") or {}).get("ok")
        for item in state.get("tool_history") or []
    )
