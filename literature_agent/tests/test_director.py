# -*- coding: utf-8 -*-
"""Director 编排器的单元测试（无网络、无数据库）。

覆盖：
- 维度覆盖/通道判定（enabled_dims/missing_ai_dims/original_read_tool/
  is_original_trusted）；
- 首轮确定性播种 seed_after_inspect（原件通道 / 扫描件 OCR / 文本提取，
  原件动作 focus 含全部启用维度以便读中 FC 循环直出参考文献）；
- 原件读中循环成功后的条件本地尾部 original_success_tail / fc_tools_used
  （扫描件补页 OCR 含 FC 失败页、全文已存/读中已取跳过、参考文献缺失才
  条件触发章节识别、关键词始终本地、基线统一打 71 锚点）；
- Director 单步决策 decide_next_action（覆盖即 finalize / 合法动作 /
  非法 JSON 回退 / 被护栏拒绝 / 连续非法转恢复，阶段5动作 80-84 打标）；
- apply_director_guard 白名单、focus 收敛、页码越界、max_tables 钳制、
  同签名去重（含读中 FC trace）；
- 确定性恢复 _recovery_action 与扫描件降级 OCR 链。
"""
from agent import director, llm
from agent.state import (
    DIMENSION_KEYS,
    OCR_MODE_AUTO,
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

FAKE_MODEL = {"name": "fake-model", "base_url": "http://fake.local/v1",
              "api_key": "fake-key"}
FILE_ID_MODEL = dict(FAKE_MODEL, file_channel="file_id")
FILE_DATA_MODEL = dict(FAKE_MODEL, file_channel="file_data")

# 交给 AI 的五个非本地权威维度（参考文献 reference_list 始终走本地权威）
NON_AUTH_DIMS = [d for d in DIMENSION_KEYS if d != "reference_list"]


def _state(**overrides) -> dict:
    """构造一个带 PDF 布局、全文非空、默认全开维度的 Director 输入状态。"""
    state = {
        "lit_path": r"C:\literature\paper.pdf",
        "rule_detail": {},
        "precision": 3,
        "model": dict(FAKE_MODEL),
        "inspect_layout": {
            "format": "pdf", "is_scanned": False, "has_two_columns": False,
            "page_count": 5, "pages_with_images": [], "table_pages": [],
        },
        "abs_text": "这是一段足以通过非空校验的文献正文内容，用于驱动编排测试。",
        "ai_drafts": {},
        "local_drafts": {},
        "tool_history": [],
        "current_step": 3,
        "max_steps": C.AGENT_MAX_STEPS,
        "director_failures": 0,
        "table_texts": {},
        "ocr_pages": {},
    }
    state.update(overrides)
    return state


def _fill_ai_dims(dims) -> dict:
    """把给定维度全部填成非空 AI 草稿。"""
    return {dim: f"{dim} 的非空解读内容，足以判定该维度已覆盖。" for dim in dims}


def _force_channel(monkeypatch, channel):
    """显式指定原件通道，避免 auto 通道触发任何网络探测。"""
    monkeypatch.setattr(ai_client, "resolve_file_channel",
                        lambda model: channel)


def _fc_history(fc_tools, *, ok=True, drafts=None):
    """构造一条带读中 FC 留痕的 ai_read_original 工具历史。"""
    return {
        "tool": TOOL_AI_READ_ORIGINAL,
        "output": {"ok": ok, "fc_tools": fc_tools, "drafts": drafts or {}},
    }


# ================= 维度覆盖与通道判定 =================

def test_enabled_and_missing_dims_respect_rule_and_authority():
    # 关闭创新点维度：既不在启用列表，也不在缺失列表
    state = _state(rule_detail={"dimensions": {"innovation_point": False}})
    enabled = director.enabled_dims(state)
    assert "innovation_point" not in enabled
    # reference_list 是本地权威维度，永远不进入"待 AI 补全"集合
    assert "reference_list" not in director.ai_focus_dims(state)
    missing = director.missing_ai_dims(state)
    assert "reference_list" not in missing
    assert set(missing) == set(NON_AUTH_DIMS) - {"innovation_point"}


def test_original_read_tool_routes_by_channel(monkeypatch):
    _force_channel(monkeypatch, ai_client.FILE_CHANNEL_ID)
    assert director.original_read_tool(_state(model=FILE_ID_MODEL)) \
        == TOOL_AI_READ_ORIGINAL

    _force_channel(monkeypatch, ai_client.FILE_CHANNEL_DATA)
    assert director.original_read_tool(_state(model=FILE_DATA_MODEL)) \
        == TOOL_AI_READ_ORIGINAL  # PDF 可 Base64 内联
    assert director.original_read_tool(
        _state(model=FILE_DATA_MODEL, lit_path=r"C:\lit\a.txt")) \
        == TOOL_AI_DEEP_ANALYZE  # 非 PDF 退回文本通道

    _force_channel(monkeypatch, "text")
    assert director.original_read_tool(_state()) == TOOL_AI_DEEP_ANALYZE

    assert director.original_read_tool(_state(model=None)) == ""


def test_is_original_trusted_requires_ok_original_and_full_dims():
    # 无原件直读历史
    assert director.is_original_trusted(_state()) is False

    # 有成功直读但维度缺失
    missing = _state(tool_history=[
        {"tool": TOOL_AI_READ_ORIGINAL, "output": {"ok": True}}])
    assert director.is_original_trusted(missing) is False

    # 成功直读且非本地权威维度齐全
    full = _state(
        ai_drafts=_fill_ai_dims(NON_AUTH_DIMS),
        tool_history=[
            {"tool": TOOL_AI_READ_ORIGINAL, "output": {"ok": True}}])
    assert director.is_original_trusted(full) is True

    # 直读失败的输出不算成功
    failed = _state(
        ai_drafts=_fill_ai_dims(NON_AUTH_DIMS),
        tool_history=[
            {"tool": TOOL_AI_READ_ORIGINAL, "output": {"ok": False}}])
    assert director.is_original_trusted(failed) is False

    # 发生过通道降级：即便后来补齐维度也必须回退完整校验
    full["original_read_failed"] = True
    assert director.is_original_trusted(full) is False


# ================= 首轮确定性播种 =================

def test_seed_prefers_original_read_for_file_id(monkeypatch):
    _force_channel(monkeypatch, ai_client.FILE_CHANNEL_ID)
    update = director.seed_after_inspect(
        _state(model=FILE_ID_MODEL,
               inspect_layout={"is_scanned": True, "page_count": 3}),
        {"is_scanned": True, "page_count": 3})
    actions = update["planned_actions"]
    # 即便扫描件，只要原件通道可用，第一步也只排 ai_read_original
    assert len(actions) == 1
    assert actions[0]["tool"] == TOOL_AI_READ_ORIGINAL
    # 读中 FC 循环输出全部启用维度（含参考文献）：AI 给出参考文献后本地
    # 不再跑章节识别，仅在 AI 缺失时本地条件兜底
    assert actions[0]["args"]["focus"] == ",".join(DIMENSION_KEYS)


def test_seed_scanned_pdf_without_channel_plans_per_page_ocr():
    layout = {"is_scanned": True, "page_count": 3, "pages_with_images": []}
    update = director.seed_after_inspect(
        _state(model=None, inspect_layout=layout), layout)
    tools = [a["tool"] for a in update["planned_actions"]]
    # 逐页 OCR 在前，结构化尾部（无 AI，因模型为空）收尾
    assert tools == [
        TOOL_OCR_PAGE, TOOL_OCR_PAGE, TOOL_OCR_PAGE,
        TOOL_DETECT_STRUCTURE, TOOL_EXTRACT_KEYWORDS]
    ocr_actions = [a for a in update["planned_actions"]
                   if a["tool"] == TOOL_OCR_PAGE]
    assert [a["args"]["page_index"] for a in ocr_actions] == [0, 1, 2]
    assert all(a["args"]["mode"] == OCR_MODE_AUTO for a in ocr_actions)
    assert update["lit_profile"]["kind"] == "scanned_paper"
    assert update["max_steps"] >= 3 + C.AGENT_SCANNED_STEP_TAIL


def test_seed_text_file_uses_extract_text(monkeypatch):
    _force_channel(monkeypatch, ai_client.FILE_CHANNEL_DATA)
    # file_data 通道但 TXT 非 PDF：不排原件直读，先取全文
    update = director.seed_after_inspect(
        _state(model=FILE_DATA_MODEL, lit_path=r"C:\lit\a.txt",
               inspect_layout={"is_scanned": False, "page_count": 0}),
        {"is_scanned": False, "page_count": 0})
    assert update["planned_actions"] == [
        {"tool": TOOL_EXTRACT_TEXT, "args": {},
         "reason": update["planned_actions"][0]["reason"]}]


# ================= fc_tools_used：读中循环留痕汇总 =================

def test_fc_tools_used_collects_names_and_ocr_pages():
    state = _state(tool_history=[
        # 失败的 ai_read_original 留痕一律忽略
        _fc_history([{"name": TOOL_EXTRACT_TABLES,
                      "args": {"page_index": 0}, "ok": True}], ok=False),
        _fc_history([
            {"name": TOOL_EXTRACT_TEXT, "args": {}, "ok": True},
            {"name": TOOL_OCR_PAGE,
             "args": {"page_index": 2}, "ok": True},
            # 失败的 OCR 页也计入"已尝试"，条件尾部不重复排单
            {"name": TOOL_OCR_PAGE,
             "args": {"page_index": 4}, "ok": False},
            # 非法页码不进集合
            {"name": TOOL_OCR_PAGE,
             "args": {"page_index": "x"}, "ok": False},
        ]),
    ])
    used = director.fc_tools_used(state)
    assert used["names"] == {
        TOOL_EXTRACT_TEXT, TOOL_OCR_PAGE}
    assert used["ocr_pages"] == {2, 4}


def test_fc_tools_used_empty_without_original_history():
    used = director.fc_tools_used(_state())
    assert used == {"names": set(), "ocr_pages": set()}


# ================= 原件读中循环成功后的条件本地尾部 =================

def test_tail_non_scanned_empty_text_adds_extract_detect_keywords():
    # 非扫描件 + 无全文 + AI/本地均无参考文献：补提取、条件章节识别、关键词
    actions = director.original_success_tail(_state(abs_text=""))
    assert [a["tool"] for a in actions] == [
        TOOL_EXTRACT_TEXT, TOOL_DETECT_STRUCTURE, TOOL_EXTRACT_KEYWORDS]
    # 基线动作统一打 71 锚点（首尾同点），关键词打 72-74
    assert actions[0]["_pct_start"] == C.AGENT_PCT_ORIGINAL_BASELINE
    assert actions[0]["_pct_end"] == C.AGENT_PCT_ORIGINAL_BASELINE
    assert actions[1]["_pct_start"] == C.AGENT_PCT_ORIGINAL_BASELINE
    assert actions[2]["_pct_start"] == C.AGENT_PCT_KEYWORD_TAIL_START
    assert actions[2]["_pct_end"] == C.AGENT_PCT_KEYWORD_TAIL_END


def test_tail_skips_extract_when_text_exists_and_ai_has_references():
    # 全文已在手 + AI 直出参考文献：只跑关键词，不重复提取、不跑章节识别
    state = _state(
        abs_text="已有全文内容",
        ai_drafts={"reference_list": "[1] 张三. 软件工程研究. 2022."})
    actions = director.original_success_tail(state)
    assert [a["tool"] for a in actions] == [TOOL_EXTRACT_KEYWORDS]


def test_tail_skips_extract_when_fc_loop_already_extracted():
    # 读中循环已提取过全文（state 尚未合并时也以 FC 留痕为准），不重复提取
    state = _state(
        abs_text="",
        ai_drafts={"reference_list": "[1] 李四. 模块化研究. 2023."},
        tool_history=[_fc_history(
            [{"name": TOOL_EXTRACT_TEXT, "args": {}, "ok": True}])])
    actions = director.original_success_tail(state)
    assert TOOL_EXTRACT_TEXT not in [a["tool"] for a in actions]
    assert TOOL_DETECT_STRUCTURE not in [a["tool"] for a in actions]
    assert [a["tool"] for a in actions] == [TOOL_EXTRACT_KEYWORDS]


def test_tail_scanned_completes_only_missing_pages():
    # 扫描件 3 页：state 已有第 0 页、读中 OCR 过第 2 页（含失败）→ 只补第 1 页
    state = _state(
        abs_text="",
        inspect_layout={"is_scanned": True, "page_count": 3,
                        "pages_with_images": []},
        ocr_pages={0: "第零页文字"},
        tool_history=[_fc_history([
            {"name": TOOL_OCR_PAGE, "args": {"page_index": 2}, "ok": False}])])
    actions = director.original_success_tail(state)
    baseline = [a for a in actions if a["tool"] == TOOL_OCR_PAGE]
    assert len(baseline) == 1
    assert baseline[0]["args"]["page_index"] == 1
    assert baseline[0]["args"]["mode"] == OCR_MODE_AUTO
    # 扫描件基线绝不排 extract_text
    assert all(a["tool"] != TOOL_EXTRACT_TEXT for a in actions)


def test_tail_scanned_all_pages_attempted_skips_ocr():
    state = _state(
        abs_text="已有 OCR 全文",
        inspect_layout={"is_scanned": True, "page_count": 2},
        ocr_pages={0: "a", 1: "b"},
        ai_drafts={"reference_list": "[1] 作者. 文献. 2020."})
    actions = director.original_success_tail(state)
    assert [a["tool"] for a in actions] == [TOOL_EXTRACT_KEYWORDS]


def test_tail_detect_skipped_when_local_or_rule_covers_references():
    # 本地草稿已有参考文献：不跑章节识别
    state_local = _state(local_drafts={"reference_list": "[1] 本地命中"})
    assert TOOL_DETECT_STRUCTURE not in [
        a["tool"] for a in director.original_success_tail(state_local)]

    # 规则关闭参考文献维度：即便 AI/本地都空也不跑章节识别
    state_off = _state(
        rule_detail={"dimensions": {"reference_list": False}})
    actions = director.original_success_tail(state_off)
    assert TOOL_DETECT_STRUCTURE not in [a["tool"] for a in actions]


def test_tail_detect_skipped_when_fc_or_history_already_ran():
    state_fc = _state(tool_history=[_fc_history(
        [{"name": TOOL_DETECT_STRUCTURE, "args": {"precision": 3},
          "ok": True}])])
    assert TOOL_DETECT_STRUCTURE not in [
        a["tool"] for a in director.original_success_tail(state_fc)]

    state_hist = _state(tool_history=[
        {"tool": TOOL_DETECT_STRUCTURE,
         "args": {"precision": 3}, "output": {"ok": True}}])
    assert TOOL_DETECT_STRUCTURE not in [
        a["tool"] for a in director.original_success_tail(state_hist)]


def test_tail_detect_clamps_precision():
    actions = director.original_success_tail(_state(precision=99))
    detect = next(a for a in actions if a["tool"] == TOOL_DETECT_STRUCTURE)
    assert detect["args"]["precision"] == 5


def test_tail_keywords_skipped_when_already_available():
    # state 已有关键词
    state = _state(
        keywords=["模块化", "记账APP"],
        ai_drafts={"reference_list": "[1] x. 2021."})
    assert director.original_success_tail(state) == []
    # 外层历史已成功执行过关键词提取
    state_hist = _state(
        ai_drafts={"reference_list": "[1] x. 2021."},
        tool_history=[{"tool": TOOL_EXTRACT_KEYWORDS,
                       "output": {"ok": True}}])
    assert director.original_success_tail(state_hist) == []


# ================= 动作进度打标 =================

def test_keyword_tail_action_tagged_72_74():
    action = director.keyword_tail_action()
    assert action["tool"] == TOOL_EXTRACT_KEYWORDS
    assert action["_pct_start"] == C.AGENT_PCT_KEYWORD_TAIL_START
    assert action["_pct_end"] == C.AGENT_PCT_KEYWORD_TAIL_END


def test_tag_action_span_evenly_covers_range():
    actions = [
        {"tool": TOOL_EXTRACT_TABLES, "args": {"page_index": -1}},
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 1}},
        {"tool": TOOL_AI_DEEP_ANALYZE, "args": {"focus": "innovation_point"}},
    ]
    tagged = director.tag_action_span(actions, 70, 78)
    starts = [a["_pct_start"] for a in tagged]
    ends = [a["_pct_end"] for a in tagged]
    # 首起 70、末止 78、首尾相接、严格单调不减
    assert starts[0] == 70
    assert ends[-1] == 78
    assert ends[:-1] == [s - 1 for s in starts[1:]]


def test_tag_action_span_empty_returns_empty():
    assert director.tag_action_span([], 70, 78) == []


def test_scanned_ocr_chain_for_degrade():
    chain = director.scanned_ocr_chain_for_degrade({"page_count": 3})
    assert [a["args"]["page_index"] for a in chain] == [0, 1, 2]
    # 布局缺失时至少排 1 页，不返回空链
    assert len(director.scanned_ocr_chain_for_degrade(None)) == 1


# ================= Director 单步决策 =================

def test_decide_finalizes_immediately_when_dims_covered(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("维度已覆盖时不应再调用 Director LLM")

    monkeypatch.setattr(llm, "plan_next_action", _boom)
    state = _state(ai_drafts=_fill_ai_dims(NON_AUTH_DIMS))
    action, failures = director.decide_next_action(state)
    assert action.get("finalize") is True
    assert failures == 0


def test_decide_returns_guarded_llm_action(monkeypatch):
    state = _state(ai_drafts=_fill_ai_dims(
        [d for d in NON_AUTH_DIMS if d != "innovation_point"]))

    monkeypatch.setattr(llm, "plan_next_action",
                        lambda model, prompt, page_count=0: {
                            "tool": TOOL_AI_DEEP_ANALYZE,
                            "args": {"focus": "innovation_point"},
                            "reason": "补创新点"})
    action, failures = director.decide_next_action(state)
    assert failures == 0
    assert action["tool"] == TOOL_AI_DEEP_ANALYZE
    assert action["args"]["focus"] == "innovation_point"
    # 阶段5动作统一打 80-84 锚点（Verify 77 之后不倒挂）
    assert action["_pct_start"] == C.AGENT_PCT_STAGE5_START
    assert action["_pct_end"] == C.AGENT_PCT_STAGE5_END


def test_decide_invalid_json_falls_back_to_recovery(monkeypatch):
    state = _state(ai_drafts=_fill_ai_dims(
        [d for d in NON_AUTH_DIMS if d != "innovation_point"]))
    monkeypatch.setattr(llm, "plan_next_action",
                        lambda *a, **k: None)  # 非法 JSON / 调用失败
    action, failures = director.decide_next_action(state)
    assert failures == 1
    # 全文已在手：恢复动作为按缺失维度补一次 AI 解读
    assert action["tool"] == TOOL_AI_DEEP_ANALYZE
    assert action["args"]["focus"] == "innovation_point"


def test_decide_rejected_action_increments_failures_then_recovery(monkeypatch):
    state = _state(ai_drafts=_fill_ai_dims(
        [d for d in NON_AUTH_DIMS if d != "innovation_point"]))
    # 工具虽在 LLM 白名单，但 focus 含本地权威维度/已填充维度，护栏必须拒绝
    monkeypatch.setattr(llm, "plan_next_action",
                        lambda *a, **k: {
                            "tool": TOOL_AI_DEEP_ANALYZE,
                            "args": {"focus": "reference_list"},
                            "reason": "越权解读参考文献"})
    action, failures = director.decide_next_action(state)
    assert failures == 1
    assert action["tool"] == TOOL_AI_DEEP_ANALYZE
    assert action["args"]["focus"] == "innovation_point"


def test_decide_skips_llm_after_failure_threshold(monkeypatch):
    state = _state(
        director_failures=C.AGENT_DIRECTOR_MAX_LLM_FAILURES,
        ai_drafts=_fill_ai_dims(
            [d for d in NON_AUTH_DIMS if d != "innovation_point"]))

    def _boom(*args, **kwargs):
        raise AssertionError("达到非法决策上限后不应再询问 LLM")

    monkeypatch.setattr(llm, "plan_next_action", _boom)
    action, failures = director.decide_next_action(state)
    # 该轮未再询问 LLM，失败计数累加一次，直接落确定性恢复
    assert failures == C.AGENT_DIRECTOR_MAX_LLM_FAILURES + 1
    assert action["tool"] == TOOL_AI_DEEP_ANALYZE


# ================= apply_director_guard 护栏 =================

def test_guard_rejects_hidden_tools():
    state = _state()
    for hidden in (TOOL_INSPECT_DOCUMENT, TOOL_AI_READ_ORIGINAL,
                   TOOL_EXTRACT_KEYWORDS):
        assert director.apply_director_guard(
            {"tool": hidden, "args": {}}, state) is None
    assert director.apply_director_guard("not-a-dict", state) is None


def test_guard_extract_text_normalizes_args():
    action = director.apply_director_guard(
        {"tool": TOOL_EXTRACT_TEXT, "args": {"unexpected": 1}}, _state())
    assert action == {"tool": TOOL_EXTRACT_TEXT, "args": {},
                      "reason": ""}


def test_guard_detect_structure_requires_text_and_clamps_precision():
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 99}},
        _state())["args"]["precision"] == 5
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 0}},
        _state())["args"]["precision"] == 1
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": "x"}},
        _state()) is None
    # 全文为空不允许结构识别
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 3}},
        _state(abs_text="")) is None


def test_guard_extract_tables_validates_page_and_clamps_count():
    guarded = director.apply_director_guard(
        {"tool": TOOL_EXTRACT_TABLES,
         "args": {"page_index": 2, "max_tables": 999}}, _state())
    assert guarded["args"] == {"page_index": 2,
                               "max_tables": C.AGENT_TABLE_MAX_DEFAULT}
    # 全部页 -1 合法
    assert director.apply_director_guard(
        {"tool": TOOL_EXTRACT_TABLES, "args": {"page_index": -1}},
        _state())["args"]["page_index"] == -1
    # 页码越界拒绝
    assert director.apply_director_guard(
        {"tool": TOOL_EXTRACT_TABLES, "args": {"page_index": 9}},
        _state()) is None
    # max_tables 非整数退回默认值
    assert director.apply_director_guard(
        {"tool": TOOL_EXTRACT_TABLES,
         "args": {"page_index": -1, "max_tables": "abc"}},
        _state())["args"]["max_tables"] == C.AGENT_TABLE_MAX_DEFAULT


def test_guard_ocr_page_validates_pdf_and_page():
    guarded = director.apply_director_guard(
        {"tool": TOOL_OCR_PAGE,
         "args": {"page_index": 2, "mode": "bogus"}}, _state())
    assert guarded["args"] == {"page_index": 2, "mode": OCR_MODE_AUTO}
    # 页码越界
    assert director.apply_director_guard(
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 5}},
        _state()) is None
    # 缺页码
    assert director.apply_director_guard(
        {"tool": TOOL_OCR_PAGE, "args": {}}, _state()) is None
    # 非 PDF（page_count=0）不能 OCR
    assert director.apply_director_guard(
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 0}},
        _state(inspect_layout={"page_count": 0})) is None


def test_guard_ai_deep_analyze_focus_intersects_missing_dims():
    # 仅缺创新点：focus 中的已填充维度与参考文献维度一律剔除
    state = _state(ai_drafts=_fill_ai_dims(
        [d for d in NON_AUTH_DIMS if d != "innovation_point"]))
    guarded = director.apply_director_guard(
        {"tool": TOOL_AI_DEEP_ANALYZE,
         "args": {"focus": "innovation_point,core_view,reference_list"}},
        state)
    assert guarded["args"]["focus"] == "innovation_point"

    # focus 全部不在缺失集合 → 拒绝（单步兜底不允许重写已有维度）
    assert director.apply_director_guard(
        {"tool": TOOL_AI_DEEP_ANALYZE,
         "args": {"focus": "core_view"}}, state) is None
    assert director.apply_director_guard(
        {"tool": TOOL_AI_DEEP_ANALYZE,
         "args": {"focus": "reference_list"}}, state) is None
    # 全文为空 / AI 不可用 → 拒绝
    assert director.apply_director_guard(
        {"tool": TOOL_AI_DEEP_ANALYZE,
         "args": {"focus": "innovation_point"}},
        _state(abs_text="")) is None
    assert director.apply_director_guard(
        {"tool": TOOL_AI_DEEP_ANALYZE,
         "args": {"focus": "innovation_point"}},
        _state(ai_unavailable=True)) is None


def test_guard_rejects_duplicate_signature():
    state = _state(ai_drafts=_fill_ai_dims(
        [d for d in NON_AUTH_DIMS if d != "innovation_point"]))
    action = {"tool": TOOL_AI_DEEP_ANALYZE,
              "args": {"focus": "innovation_point"}}
    first = director.apply_director_guard(action, state)
    assert first is not None
    # 同签名动作已执行：拒绝原地打转（不同 focus 仍允许）
    state["tool_history"].append(
        {"tool": TOOL_AI_DEEP_ANALYZE, "args": {"focus": "innovation_point"}})
    assert director.apply_director_guard(action, state) is None


def test_guard_rejects_duplicate_signature_from_fc_trace():
    # 读中 FC 循环已执行过同签名取证工具：阶段5 不得重复调度
    state = _state(tool_history=[_fc_history([
        {"name": TOOL_DETECT_STRUCTURE, "args": {"precision": 3},
         "ok": True}])])
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 3}},
        state) is None
    # 不同精度仍允许
    assert director.apply_director_guard(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 4}},
        state) is not None


# ================= 确定性恢复 =================

def test_recovery_scanned_pdf_continues_next_ocr_page():
    state = _state(
        abs_text="",
        inspect_layout={"is_scanned": True, "page_count": 2},
        ocr_pages={0: "第一页文字"})
    action = director._recovery_action(state, ["innovation_point"])
    assert action["tool"] == TOOL_OCR_PAGE
    assert action["args"]["page_index"] == 1

    # 全部页 OCR 完成且仍无文本：无恢复动作
    state["ocr_pages"] = {0: "a", 1: "b"}
    state["abs_text"] = ""
    assert director._recovery_action(state, ["innovation_point"]) is None


def test_recovery_text_pdf_extracts_before_ai():
    state = _state(abs_text="")
    action = director._recovery_action(state, ["innovation_point"])
    assert action["tool"] == TOOL_EXTRACT_TEXT
    # extract_text 已成功过则不再重复（此时无文本基线，无计可施）
    state["tool_history"].append(
        {"tool": TOOL_EXTRACT_TEXT, "output": {"ok": True}})
    assert director._recovery_action(state, ["innovation_point"]) is None


def test_recovery_with_text_uses_ai_unless_unavailable():
    state = _state(abs_text="已有全文",
                   ai_drafts=_fill_ai_dims(
                       [d for d in NON_AUTH_DIMS if d != "innovation_point"]))
    action = director._recovery_action(state, ["innovation_point"])
    assert action["tool"] == TOOL_AI_DEEP_ANALYZE
    assert action["args"]["focus"] == "innovation_point"

    state["ai_unavailable"] = True
    assert director._recovery_action(state, ["innovation_point"]) is None


# ================= llm.sanitize_tool_call：FC 工具调用消毒（共享协议） =================

def test_sanitize_tool_call_fc_whitelist_and_pages():
    # FC 白名单四工具可用：抽表全部页、OCR 指定页、提取全文、章节识别
    assert llm.sanitize_tool_call(
        {"tool": TOOL_EXTRACT_TABLES, "args": {"page_index": -1}},
        page_count=5)["args"]["page_index"] == -1
    assert llm.sanitize_tool_call(
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 4}},
        page_count=5)["args"]["mode"] == OCR_MODE_AUTO
    assert llm.sanitize_tool_call(
        {"tool": TOOL_EXTRACT_TEXT, "args": {}}, page_count=5)["args"] == {}
    # detect_structure 必须显式带 precision（FC 执行器会在消毒前补默认值）
    assert llm.sanitize_tool_call(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {}}, page_count=5) is None
    assert llm.sanitize_tool_call(
        {"tool": TOOL_DETECT_STRUCTURE, "args": {"precision": 9}},
        page_count=5)["args"]["precision"] == 5
    # 非白名单工具 / 页码越界 / 非 PDF 按页 OCR 一律拒绝
    assert llm.sanitize_tool_call(
        {"tool": "unknown_tool", "args": {}}, page_count=5) is None
    assert llm.sanitize_tool_call(
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 5}},
        page_count=5) is None
    assert llm.sanitize_tool_call(
        {"tool": TOOL_OCR_PAGE, "args": {"page_index": 0}},
        page_count=0) is None
    assert llm.sanitize_tool_call("bad", page_count=5) is None
    # 非 PDF（DOCX）仅允许全部页 -1 抽表；args 缺省按 -1 处理
    assert llm.sanitize_tool_call(
        {"tool": TOOL_EXTRACT_TABLES}, page_count=0)["args"]["page_index"] == -1
    assert llm.sanitize_tool_call(
        {"tool": TOOL_EXTRACT_TABLES, "args": {"page_index": 0}},
        page_count=0) is None
