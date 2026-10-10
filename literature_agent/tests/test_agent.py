# -*- coding: utf-8 -*-
"""ReAct 文献解析 Agent 的单元测试。

不依赖数据库：Agent 是 tool_layer 之上的编排层。
- verifiers 三校验为纯函数，直接构造输入断言；
- 端到端用本地临时 txt；
- AI 闭环通过 monkeypatch ai_client.chat_with_text 注入假模型，无真实网络。
"""
import json

import pytest

from config import constants as C
from agent import run_parse_agent
from agent.tools import split_citation_marks
from agent.verifiers import (
    ISSUE_CONSISTENCY,
    ISSUE_TRACEABILITY,
    verify_completeness,
    verify_consistency,
    verify_traceability,
)

SAMPLE_TEXT = """摘要

本文研究记账类APP模块化设计方法，提出可复用的模块划分框架。

一、引言（研究背景）

随着移动支付普及，个人记账需求激增，但记账类APP功能冗余、迭代缓慢。
现有设计方法缺乏模块复用机制，导致重复开发。本文聚焦模块化设计。

二、相关技术与核心观点

核心观点是模块化能显著提升迭代效率并降低维护成本。
模块化设计通过高内聚低耦合原则组织功能单元。

三、研究方法

我们采用多案例研究法，选取三款主流记账APP进行功能拆解与模块映射，
并通过用户任务完成时间对比模块化前后版本。

四、创新点

提出了一种面向记账场景的模块依赖图建模方法，
以及基于业务稳定性的模块边界划分算法。

五、实验与结论

实验表明模块化版本任务完成时间平均缩短18%，崩溃率下降。
研究结论：模块化方法适用于中小团队的记账类产品迭代。

参考文献

[1] 张三, 李四. 软件模块化设计研究. 计算机学报, 2021.
[2] 王五. 移动应用架构实践. 软件工程, 2022.
"""

ALL_DIMS = [
    "research_background", "core_view", "research_method",
    "innovation_point", "research_conclusion", "reference_list",
]
FAKE_MODEL = {"name": "fake-model", "base_url": "http://fake.local/v1",
              "api_key": "fake-key"}

# 长于短篇阈值（1500 字）的版本：供需要完整 Refine 预算的 AI 闭环用例使用，
# 中性填充放在首章之前，不影响章节识别与参考文献提取
_LONG_PADDING = (
    "补充背景材料：研究团队调研了行业现状、用户访谈记录与同类产品公开文档，"
    "这些材料为后续模块划分提供了事实依据与需求来源。" * 20
)
LONG_SAMPLE_TEXT = SAMPLE_TEXT.replace(
    "一、引言（研究背景）", _LONG_PADDING + "\n\n一、引言（研究背景）")

# 标题全部标准命中的样本：除创新点（36 字）外各章正文均 >=50 字，
# 用于验证首次整体 AI 解读的 focus 裁剪与单维度 local_section 补救
SINGLE_FAIL_TEXT = """摘要

本文研究记账类APP的模块化设计方法，提出一套可复用的模块划分框架与分层解耦的工程实践路径。

一、引言

随着移动支付在全社会快速普及，个人记账需求持续激增，然而主流记账类APP普遍存在功能冗余、迭代缓慢的问题，现有设计方法缺乏模块复用机制，导致重复开发严重。本文聚焦模块化设计方法研究。

二、核心观点

核心观点是模块化设计能够显著提升软件产品的迭代效率，并且可以有效降低系统在长期演进过程中的维护成本与跨团队沟通成本。

三、研究方法

我们采用多案例研究法，选取三款主流记账APP进行功能拆解与模块映射，并通过用户任务完成时间对比模块化前后两个版本的差异表现。

四、创新点

提出面向记账场景的模块依赖图建模方法，以及基于业务稳定性的边界划分算法。

五、研究结论

实验表明模块化版本任务完成时间平均缩短百分之十八，崩溃率明显下降，模块化方法适用于中小团队记账类产品的持续迭代与推广落地。

参考文献

[1] 张三, 李四. 软件模块化设计研究. 计算机学报, 2021.
[2] 王五. 移动应用架构实践. 软件工程, 2022.
"""

# 核心观点（31 字）与创新点（36 字）同时短于本地自足阈值，
# 用于验证多维度校验失败时合并为一次 mixed_section AI 调用
MERGE_FAIL_TEXT = SINGLE_FAIL_TEXT.replace(
    "核心观点是模块化设计能够显著提升软件产品的迭代效率，并且可以有效降低"
    "系统在长期演进过程中的维护成本与跨团队沟通成本。",
    "核心观点是模块化设计能够提升迭代效率，并降低系统长期维护成本。")

# 六个维度本地章节均达到自足阈值（创新点加长到 53 字），
# 用于验证首次整体 AI 解读被整体跳过、零模型调用完成解析
ALL_SUFFICIENT_TEXT = SINGLE_FAIL_TEXT.replace(
    "提出面向记账场景的模块依赖图建模方法，以及基于业务稳定性的边界划分算法。",
    "提出面向记账场景的模块依赖图建模方法，以及基于业务稳定性综合评估的"
    "模块边界划分迭代算法与配套工程落地规范。")


@pytest.fixture
def sample_txt(tmp_path):
    """在临时目录写一篇含标准章节的 txt 文献，返回路径。"""
    path = tmp_path / "sample.txt"
    path.write_text(SAMPLE_TEXT, encoding="utf-8")
    return str(path)


@pytest.fixture
def long_sample_txt(tmp_path):
    """超过短篇阈值的 txt 文献（保留同样的章节结构）。"""
    path = tmp_path / "long_sample.txt"
    path.write_text(LONG_SAMPLE_TEXT, encoding="utf-8")
    return str(path)


@pytest.fixture
def single_fail_txt(tmp_path):
    """仅创新点章节短于自足阈值的短篇（单维度 AI 补救场景）。"""
    path = tmp_path / "single_fail.txt"
    path.write_text(SINGLE_FAIL_TEXT, encoding="utf-8")
    return str(path)


@pytest.fixture
def merge_fail_txt(tmp_path):
    """核心观点与创新点两章都短于自足阈值的短篇（多维度合并补救场景）。"""
    path = tmp_path / "merge_fail.txt"
    path.write_text(MERGE_FAIL_TEXT, encoding="utf-8")
    return str(path)


@pytest.fixture
def all_sufficient_txt(tmp_path):
    """六个维度本地章节均达到自足阈值的短篇（整体跳过 AI 场景）。"""
    path = tmp_path / "all_sufficient.txt"
    path.write_text(ALL_SUFFICIENT_TEXT, encoding="utf-8")
    return str(path)


# ================= 锚点解析 =================

def test_split_citation_marks_extracts_indices():
    text = "模块化提升迭代效率。\n[原文位置：3, 7]"
    clean, anchors = split_citation_marks(text)
    assert clean == "模块化提升迭代效率。"
    assert anchors == [3, 7]


def test_split_citation_marks_tolerates_fullwidth_and_duplicates():
    clean, anchors = split_citation_marks("结论内容[原文位置: 2，2,5 ]")
    assert clean == "结论内容"
    assert anchors == [2, 5]


def test_split_citation_marks_without_marker():
    clean, anchors = split_citation_marks("普通结论，没有标记")
    assert clean == "普通结论，没有标记"
    assert anchors == []


def test_split_citation_marks_strips_paragraph_transport_marks():
    """模型把 Refine 材料前缀【段落n】照抄进正文时必须剥除，锚点以[原文位置]为准。"""
    clean, anchors = split_citation_marks(
        "【段落9】提出面向记账场景的模块依赖图建模方法。[原文位置：9]")
    assert clean == "提出面向记账场景的模块依赖图建模方法。"
    assert anchors == [9]


def test_split_citation_marks_strips_chunk_transport_marks():
    """map-reduce 材料的【片段i/N】标记同样不得泄漏进最终报告。"""
    clean, anchors = split_citation_marks("【片段 2/5】跨片段综合出的结论内容。")
    assert clean == "跨片段综合出的结论内容。"
    assert anchors == []


# ================= 章节 → 全局段落号映射 =================

def test_global_paragraphs_index_matches_blank_line_split():
    from agent.tools import global_paragraphs, render_marked_paragraphs
    text = "标题块\n\n第一段正文。\n\n第二段正文。"
    paragraphs = global_paragraphs(text)
    assert paragraphs == ["标题块", "第一段正文。", "第二段正文。"]
    # 只渲染指定全局段落号，且越界号被忽略
    marked = render_marked_paragraphs(text, [1, 99])
    assert marked == "【段落1】第一段正文。"
    assert render_marked_paragraphs(text, [99]) == ""


def test_detect_structure_with_anchors_maps_global_paragraphs():
    from tool_layer.text_analysis import detect_structure_with_anchors
    structure, anchors = detect_structure_with_anchors(SINGLE_FAIL_TEXT, 3)
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]

    method_idx = next(i for i, p in enumerate(paragraphs) if "多案例研究法" in p)
    heading_idx = next(
        i for i, p in enumerate(paragraphs) if p.strip() == "三、研究方法")
    # 章节正文所在全局段落被映射
    assert method_idx in anchors["research_method"]
    # 标题单独成块的段落号不混入映射（它不支撑维度正文）
    assert heading_idx not in anchors["research_method"]
    # 六个维度键齐全；创新点章节正文同样可映射
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)
    assert inno_idx in anchors["innovation_point"]
    assert set(anchors.keys()) == set(ALL_DIMS)
    # structure 与原 detect_structure 输出一致
    assert "多案例研究法" in structure["research_method"]


# ================= 本地兜底：期刊双栏排版取材 =================

_JOURNAL_TEXT = """《某期刊》2024 年第 01 期 ●财会经济
记账类 APP 模块化设计方法与应用研究

●胡雪晴

摘要：对记账类 APP 分析与研究，得出一套针对记账类
APP 的模块化设计方案。研究表明，模块化可以显著降低开发
成本，覆盖更多用户，企业成本降低，用户体验提升。
关键词：记账应用 模块化 信息可视化
中图分类号：F233 文献标识码：A

随着移动支付普及，个人记账需求激增，市面上记账类 APP 数量
非常多，受用户喜爱的记账应用并不多，记账软件以鲨鱼记账
与随手记为龙头，还有多款有特色的记账软件可供选择。

三、APP“FLPE 账本”设计实践
从名称来说，“FLPE”并没有内在涵义，是一种现代流行
概念。从功能来说，“FLPE 账本”以模块化概念来设计。

场，很多产品在理论支撑不足的情况下投入市场也取得了成功。
但是作为设计领域研究者，我们需要深入挖掘设计背后的逻辑，
本文以记账 APP 研究实践，分析了可视化设计方法与策略，
能为社会带来相当大的价值。
[基金项目：某省教育厅项目（编号：23C908）]

参考文献：
[1] 张三. 量化自我与记账应用研究[J].装饰,2020(5):1-5.
[2] 李四. 交互设计提升理财应用可用性[J].包装工程,2021(8):6-9.
四、结语（作者单位：某学院财务处某城 100000）
从 1999 年末某款手机开始到现 [作者简介：王五（1990—），某城人，
在，移动智能终端从出现到普及不过短短 20 年，所以在细分财务处讲师，主要研究方向：经济管理。]
的软件设计领域相关的理论研究较缺乏，但由于其庞大的市"""


def test_fallback_abstract_block_used_as_background_title_excluded():
    """期刊摘要块成为背景兜底；论文标题/作者行不被误选。"""
    from tool_layer.text_analysis import detect_structure_with_anchors
    structure, _anchors = detect_structure_with_anchors(_JOURNAL_TEXT, 3)
    bg = structure["research_background"]
    assert bg.startswith("对记账类 APP 分析与研究")
    assert "用户体验提升" in bg
    # 标题行与作者行不得进入背景
    assert "记账类 APP 模块化设计方法与应用研究" not in bg
    assert "胡雪晴" not in bg
    # 本文无创新章节且无创新指示词：诚实留空，不拿标题凑数
    assert structure["innovation_point"] == ""
    # 方法章节兜底命中"设计实践"段
    assert "FLPE" in structure["research_method"]


def test_fallback_inline_conclusion_reconstructed_across_columns():
    """行内"结语"标题：剥作者简介/职称，拼回双栏前后两段正文。"""
    from tool_layer.text_analysis import detect_structure_with_anchors
    structure, anchors = detect_structure_with_anchors(_JOURNAL_TEXT, 3)
    conclusion = structure["research_conclusion"]
    # 标题后左栏底部正文（作者简介被剥除，拼接处"到现/在"连成完整句）
    assert "从 1999 年末某款手机开始到现在" in conclusion
    assert "所以在细分" in conclusion
    # 标题前右栏顶部的结语后半段被拼回（"市/场"跨栏连成完整词）
    assert "场，很多产品在理论支撑不足" in conclusion
    assert "能为社会带来相当大的价值" in conclusion
    # 脚注、参考文献一律不混入
    for noise in ("作者简介", "讲师", "主要研究方向", "基金项目",
                  "23C908", "[1]", "参考文献", "责编"):
        assert noise not in conclusion, noise
    # 拼回的两段全局段落都映射到结论锚点
    paragraphs = [p for p in _JOURNAL_TEXT.split("\n\n") if p.strip()]
    tail_idx = next(i for i, p in enumerate(paragraphs)
                    if "从 1999 年末" in p)
    body_idx = next(i for i, p in enumerate(paragraphs)
                     if "能为社会带来相当大的价值" in p)
    assert tail_idx in anchors["research_conclusion"]
    assert body_idx in anchors["research_conclusion"]


# ================= 校验一：完整性 =================

def test_verify_completeness_detects_missing():
    missing = verify_completeness(
        ["core_view", "innovation_point"],
        {"core_view": "有内容", "innovation_point": "   "},
    )
    assert missing == ["innovation_point"]


def test_verify_completeness_all_present():
    assert verify_completeness(ALL_DIMS, {d: "内容" for d in ALL_DIMS}) == []


# ================= 校验二：一致性 =================

def test_verify_consistency_flags_irrelevant_long_text():
    local = "模块化设计通过高内聚低耦合原则组织功能单元，显著提升记账APP的迭代效率"
    ai = "量子纠缠与区块链元宇宙脑机接口核聚变泛在计算 " * 10
    problems = verify_consistency(
        {"core_view": ai}, {"core_view": local}, ["core_view"])
    assert len(problems) == 1
    assert problems[0]["issue"] == ISSUE_CONSISTENCY
    assert problems[0]["dimension"] == "core_view"


def test_verify_consistency_passes_matching_summary():
    local = "模块化设计通过高内聚低耦合原则组织功能单元，提升迭代效率"
    ai = "本文核心观点为模块化设计，依靠高内聚低耦合提升迭代效率"
    assert verify_consistency(
        {"core_view": ai}, {"core_view": local}, ["core_view"]) == []


def test_verify_consistency_skips_short_local_section():
    # 本地章节过短（疑似误切）时不比对，避免误杀
    assert verify_consistency(
        {"core_view": "一段合理长度的解读文字" * 5},
        {"core_view": "短"}, ["core_view"]) == []


# ================= 校验三：溯源 =================

def test_verify_traceability_requires_anchor():
    problems = verify_traceability(
        {"core_view": "没有标注锚点的解读内容"},
        {"core_view": []}, SAMPLE_TEXT, ["core_view"])
    assert len(problems) == 1
    assert problems[0]["issue"] == ISSUE_TRACEABILITY
    assert "未标注" in problems[0]["reason"]


def test_verify_traceability_rejects_out_of_range():
    problems = verify_traceability(
        {"innovation_point": "模块依赖图建模与边界划分算法"},
        {"innovation_point": [999]}, SAMPLE_TEXT, ["innovation_point"])
    assert problems and "越界" in problems[0]["reason"]


def test_verify_traceability_accepts_valid_anchor():
    # SAMPLE_TEXT 按空行分段，创新点章节（含"模块依赖图"）是其中一段
    paragraphs = [p for p in SAMPLE_TEXT.split("\n\n") if p.strip()]
    target = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)
    problems = verify_traceability(
        {"innovation_point": "提出模块依赖图建模方法和模块边界划分算法"},
        {"innovation_point": [target]}, SAMPLE_TEXT, ["innovation_point"])
    assert problems == []


def test_verify_traceability_ai_side_cover_passes_large_paragraph():
    # PDF 重建出的大段落：原文侧口径数学上不可达，但 AI 侧实词覆盖率达标
    big_para = ("记账应用模块化设计通过高内聚低耦合的方式组织各个功能单元，"
                 "显著提升开发迭代效率并降低长期维护成本，" * 3)
    text = f"首段引言内容，交代研究背景与行业现状。\n\n{big_para}"
    ai_text = "记账应用模块化设计组织功能单元，提升开发迭代效率，降低维护成本"
    problems = verify_traceability(
        {"core_view": ai_text},
        {"core_view": [1]}, text, ["core_view"])
    assert problems == []


def test_verify_traceability_still_rejects_unrelated_ai_on_large_paragraph():
    # 双向覆盖率任一达标才算通过：与大段落完全无关的解读两个口径都不达标
    big_para = ("记账应用模块化设计通过高内聚低耦合的方式组织各个功能单元，"
                 "显著提升开发迭代效率并降低长期维护成本，" * 3)
    text = f"首段引言内容，交代研究背景与行业现状。\n\n{big_para}"
    problems = verify_traceability(
        {"core_view": "量子纠缠区块链元宇宙脑机接口核聚变泛在计算石墨烯"},
        {"core_view": [1]}, text, ["core_view"])
    assert len(problems) == 1
    assert problems[0]["issue"] == ISSUE_TRACEABILITY


def test_verify_traceability_pure_local_mode_skipped():
    # 无 AI 解读的维度不做溯源要求
    assert verify_traceability({}, {}, SAMPLE_TEXT, ALL_DIMS) == []


# ================= 端到端：离线本地链路 =================

def test_offline_agent_end_to_end(sample_txt):
    state = run_parse_agent(sample_txt, rule_detail={}, model=None)
    report = state["final_report"]
    assert report["verified"] is True
    assert report["engine"] == "agent_local"
    for dim in ALL_DIMS:
        assert report[dim].strip(), f"离线模式 {dim} 应有本地章节内容"
    assert len(report["keywords"]) > 0
    # AI 主导改造后所有格式统一先 inspect（非 PDF 毫秒级固定布局），
    # 离线链路：inspect → extract_text → detect_structure → extract_keywords
    assert [h["tool"] for h in state["tool_history"]] == [
        "inspect_document", "extract_text",
        "detect_structure", "extract_keywords"]


def test_agent_respects_disabled_dimensions(sample_txt):
    rule = {"dimensions": {"reference_list": False}}
    state = run_parse_agent(sample_txt, rule_detail=rule, model=None)
    report = state["final_report"]
    assert report["reference_list"] == ""
    assert report["verified"] is True


def test_agent_fatal_failure_on_missing_file(tmp_path):
    state = run_parse_agent(str(tmp_path / "nope.txt"), rule_detail={}, model=None)
    report = state["final_report"]
    assert report["verified"] is False
    assert any("提取失败" in w for w in report["warnings"])


# ================= 端到端：假 AI 驱动的 Refine 反幻觉闭环 =================
# 注：阶段 1.5 提速后默认即本地规划（不调 LLM 规划器），无需再 monkeypatch

def test_agent_refine_loop_repairs_hallucinated_ai(
        single_fail_txt, monkeypatch):
    """首次 AI 对唯一待补维度（创新点）给出无关长文+越界锚点 → 校验失败
    → 单维度 local_section 重解读 → 通过（1 次整体 + 1 次重解读）。"""
    call_count = {"n": 0}
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 与原文无关的长篇内容 + 越界锚点
            return json.dumps({
                "innovation_point":
                    "量子纠缠区块链元宇宙脑机接口核聚变泛在计算数字孪生 " * 8
                    + "[原文位置：999]",
            }, ensure_ascii=False)
        # Refine：复述注入的创新点章节原文并标注其在全文中的真实段落锚点
        return json.dumps({
            "innovation_point": full_text.strip() + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)

    state = run_parse_agent(single_fail_txt, rule_detail={}, model=dict(FAKE_MODEL))
    report = state["final_report"]

    assert call_count["n"] == 2, "应经历一次整体解读 + 一次重解读"
    assert report["verified"] is True
    assert report["engine"] == "agent_ai"
    # 重解读后内容回到本地章节事实（含原文术语），且不含锚点标记残留
    assert "模块依赖图" in report["innovation_point"]
    assert "[原文位置" not in report["innovation_point"]
    # Refine 动作确实走了 local_section 通道
    refine_calls = [
        h for h in state["tool_history"]
        if h["tool"] == "ai_deep_analyze" and h["args"].get("basis") == "local_section"
    ]
    assert len(refine_calls) == 1


def test_agent_refine_material_uses_global_paragraph_markers(
        single_fail_txt, monkeypatch):
    """Refine 材料带【段落n】全局编号：模型照抄材料（含前缀标记）并引用材料中
    的全局段落号时，溯源一次通过，且【段落n】不泄漏进最终报告。"""
    import re
    call_count = {"n": 0}
    refine_material = {}

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return json.dumps({
                "innovation_point":
                    "量子纠缠区块链元宇宙脑机接口核聚变泛在计算数字孪生 " * 8
                    + "[原文位置：999]",
            }, ensure_ascii=False)
        # Refine：材料应只含创新点章节的全局标记段落
        assert "【段落" in full_text, "补救材料必须携带【段落n】全局编号标记"
        refine_material["text"] = full_text
        marker = re.search(r"【段落\s*(\d+)\s*】", full_text)
        global_idx = int(marker.group(1))
        # 模拟模型直接照抄材料（前缀标记也抄回），并用材料中的全局号做锚点
        return json.dumps({
            "innovation_point": full_text.strip() + f"[原文位置：{global_idx}]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)

    state = run_parse_agent(single_fail_txt, rule_detail={}, model=dict(FAKE_MODEL))
    report = state["final_report"]

    assert call_count["n"] == 2, "全局锚点对齐后应一次补救通过，不再有第二次重解读"
    assert report["verified"] is True
    assert report["engine"] == "agent_ai"
    assert "模块依赖图" in report["innovation_point"]
    # 内部传输标记与锚点标记都不得残留在最终报告中
    assert "【段落" not in report["innovation_point"]
    assert "【片段" not in report["innovation_point"]
    assert "[原文位置" not in report["innovation_point"]
    # 模型引用的全局段落号确实是创新点正文段落
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)
    assert f"【段落{inno_idx}】" in refine_material["text"]


def test_agent_refine_budget_exhausts_without_deadloop(
        long_sample_txt, monkeypatch):
    """AI 持续幻觉：补救预算耗尽后带告警收尾，该维度回退本地基线，图不死循环。

    用长样本：短篇 max_steps=5 放不下"1 次整体 + 2 次重解读"共 3 次 AI 调用；
    长样本中仅研究结论（29 字）弱于本地自足阈值，首次 focus 被裁剪为该维度。
    """
    call_count = {"n": 0}

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        return json.dumps({
            "research_conclusion":
                "量子纠缠区块链元宇宙脑机接口核聚变泛在计算数字孪生 " * 8
                + "[原文位置：999]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)

    state = run_parse_agent(long_sample_txt, rule_detail={}, model=dict(FAKE_MODEL))
    report = state["final_report"]

    # 1 次整体解读 + 预算内 2 次重解读（单维度均走 local_section 通道）
    assert call_count["n"] == 3
    local_section_calls = [
        h for h in state["tool_history"]
        if h["tool"] == "ai_deep_analyze"
        and h["args"].get("basis") == "local_section"
    ]
    assert len(local_section_calls) == 2
    assert report["verified"] is False
    # 幻觉文本被丢弃，回退本地章节
    assert report["research_conclusion"].strip()
    assert "量子纠缠" not in report["research_conclusion"]
    assert any("改用本地章节原文" in w for w in report["warnings"])
    # 图正常到达 Finish（final_report 存在即说明未死循环）
    assert state.get("final_report")


def test_agent_without_model_ai_tool_returns_safe_error():
    from agent.tools import build_tool_registry
    registry = build_tool_registry(None)
    out = registry["ai_deep_analyze"].invoke({"text": "任意文本"})
    assert out["ok"] is False
    assert out["citations"] == {}
    assert "未配置" in out["error"]


# ================= 阶段4：文献画像与自适应规划 =================

from agent.profiler import describe_profile, profile_text  # noqa: E402
from agent.prompts import build_ai_analyze_prompt  # noqa: E402
from agent import get_agent_state, resume_parse_agent  # noqa: E402


def test_profile_classifies_short_paper():
    profile = profile_text("摘要\n\n一段很短的摘要正文。")
    assert profile["kind"] == "short_paper"
    assert profile["keyword_top_n"] == C.AGENT_SHORT_KEYWORD_TOP_N


def test_profile_classifies_survey_and_experimental():
    survey = "深度学习研究综述：本文系统梳理研究进展。" + "补充内容。" * 300
    assert profile_text(survey)["kind"] == "survey"
    exp = "基于消融实验的实证研究：我们构建数据集并对比。" + "补充内容。" * 300
    assert profile_text(exp)["kind"] == "experimental"


def test_profile_counts_section_headings():
    text = "一、引言\n背景\n2.1 方法\n设计\n第3章 实验\n结果\n结论\n总结"
    profile = profile_text(text + "补充" * 400)
    assert profile["heading_count"] >= 3
    assert "章节标题数" in describe_profile(profile)


def test_short_paper_clip_steps_and_keywords(sample_txt):
    """短篇文献：循环步数在默认值之上收紧（恰好覆盖剩余动作+补救余量）。"""
    state = run_parse_agent(sample_txt, rule_detail={}, model=None)
    assert state["lit_profile"]["kind"] == "short_paper"
    # 统一 inspect 后：尾部规划时已用 2 步（inspect/extract），结构+关键词 2 步，
    # 补救余量 AGENT_REFINE_MAX_ATTEMPTS+1=3 → 收紧到 7（低于默认 8）
    assert state["max_steps"] == 2 + 2 + C.AGENT_REFINE_MAX_ATTEMPTS + 1
    kw_action = [h for h in state["tool_history"]
                 if h["tool"] == "extract_keywords"][0]
    assert kw_action["args"]["top_n"] == C.AGENT_SHORT_KEYWORD_TOP_N


def test_ai_prompt_multi_dimension_focus():
    """多维度 focus 追加解读范围行；停用维度字段行被替换；全量无范围行。"""
    multi = build_ai_analyze_prompt("core_view,innovation_point")
    assert '"core_view"' in multi and '"innovation_point"' in multi
    assert "只输出以下维度：核心观点、创新点" in multi
    # 外置模板始终保留完整 JSON 字段说明，focus 靠范围行 + 工具侧字段过滤收口
    assert '"reference_list"' in multi
    single = build_ai_analyze_prompt("core_view")
    assert '"core_view"' in single
    assert "只输出以下维度：核心观点" in single
    full = build_ai_analyze_prompt("")
    assert "【本次解读范围】" not in full
    assert '"reference_list"' in full and '"innovation_point"' in full
    # 规则停用维度：模板字段行替换为"填空字符串"指令
    disabled = build_ai_analyze_prompt("", disabled_dims="reference_list")
    assert '- "reference_list": 该维度未启用，填空字符串 ""' in disabled


def test_dimension_strategy_reference_local_first(
        long_sample_txt, monkeypatch):
    """分维度策略：AI 动作不覆盖参考文献；AI 即便返回了参考文献也以本地为准。"""
    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        # 五维度正常内容 + 故意夹带一条编造的参考文献
        return json.dumps({
            "research_background": "本文研究记账APP模块化设计方法与复用框架" * 2
                                   + "[原文位置：2]",
            "core_view": "模块化通过高内聚低耦合提升记账APP迭代效率" * 2
                         + "[原文位置：4]",
            "research_method": "采用多案例研究法对三款记账APP做功能拆解对比" * 2
                               + "[原文位置：6]",
            "innovation_point": "提出模块依赖图建模与模块边界划分算法" * 2
                                + "[原文位置：8]",
            "research_conclusion": "模块化版本任务完成时间缩短百分之十八" * 2
                                   + "[原文位置：10]",
            "reference_list": "[99] 编造的不存在文献. 虚假期刊, 2099.",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)
    state = run_parse_agent(long_sample_txt, rule_detail={},
                            model=dict(FAKE_MODEL))
    report = state["final_report"]

    # 整体 AI 动作的 focus 不含 reference_list
    overall = [h for h in state["tool_history"] if h["tool"] == "ai_deep_analyze"]
    assert overall
    assert "reference_list" not in overall[0]["args"]["focus"]
    # 最终参考文献采用本地提取结果（张三/王五），AI 编造条目被丢弃
    assert "张三" in report["reference_list"]
    assert "编造的不存在文献" not in report["reference_list"]


def test_disabled_dimensions_excluded_from_ai_focus(
        long_sample_txt, monkeypatch):
    """规则停用的维度不进入 AI focus。"""
    captured = {}

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        captured["prompt"] = prompt
        return json.dumps({
            "research_background": "背景内容" * 6 + "[原文位置：2]",
            "core_view": "观点内容" * 6 + "[原文位置：4]",
            "research_method": "方法内容" * 6 + "[原文位置：6]",
            "innovation_point": "创新内容" * 6 + "[原文位置：8]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)
    rule = {"dimensions": {"research_conclusion": False, "reference_list": False}}
    state = run_parse_agent(long_sample_txt, rule_detail=rule,
                            model=dict(FAKE_MODEL))
    overall = [h for h in state["tool_history"] if h["tool"] == "ai_deep_analyze"]
    focus = overall[0]["args"]["focus"].split(",")
    assert "research_conclusion" not in focus
    assert "reference_list" not in focus
    assert set(focus) == {
        "research_background", "core_view", "research_method", "innovation_point"}
    assert state["final_report"]["research_conclusion"] == ""


# ================= 阶段4：checkpoint 中断恢复 =================

def test_checkpoint_persists_and_resume(sample_txt):
    """带 thread_id 运行后检查点可查；对已结束线程续跑取回同一报告。"""
    state = run_parse_agent(sample_txt, rule_detail={}, model=None,
                            thread_id="parse-test-001")
    assert state["final_report"]

    snapshot = get_agent_state("parse-test-001")
    assert snapshot["final_report"]["engine"] == "agent_local"
    # 统一 inspect 后离线链路 4 步（inspect / extract / detect / keywords）
    assert len(snapshot["tool_history"]) == 4

    # 已结束线程续跑：直接返回检查点中的最终状态
    resumed = resume_parse_agent("parse-test-001")
    assert resumed["final_report"] == state["final_report"]


def test_threads_are_isolated(sample_txt):
    """不同 thread_id 的检查点互不干扰。"""
    run_parse_agent(sample_txt, rule_detail={}, model=None,
                    thread_id="parse-thread-a")
    assert get_agent_state("parse-thread-a")["final_report"]
    assert get_agent_state("parse-never-used") == {}


def test_no_checkpoint_without_thread_id(sample_txt):
    """不传 thread_id 时走无持久化轻量图，运行结果不受影响。"""
    state = run_parse_agent(sample_txt, rule_detail={}, model=None)
    assert state["final_report"]["verified"] is True
    # 无线程 id 的运行不留检查点
    assert get_agent_state("parse-should-not-exist") == {}


# ================= 阶段4增强：工具调用中文进度与提示词约束 =================

def test_progress_callbacks_show_chinese_tool_status(sample_txt):
    """每次工具调用都推送中文进度文案；百分比单调不减且 Agent 段不超过 90。"""
    events = []
    state = run_parse_agent(
        sample_txt, rule_detail={}, model=None,
        progress_callback=lambda percent, msg: events.append((percent, msg)),
    )
    assert state["final_report"]["verified"] is True
    percents = [p for p, _ in events]
    messages = [m for _, m in events]

    # 百分比单调不减（允许持平），Agent 段止于 90，落库由业务层推进到 100
    assert percents == sorted(percents)
    assert percents[0] == 8
    assert percents[-1] == 90
    assert max(percents) <= 90

    # 各工具的"进行中/完成"文案均可在进度条看到，且不直接暴露英文工具名
    joined = " | ".join(messages)
    assert "ReAct 智能体已启动" in joined
    assert "正在提取文献全文" in joined
    assert "全文提取完成" in joined
    assert "识别文献结构" in joined
    assert "正在提取关键词" in joined
    assert "三重校验通过" in joined
    assert "正在写入解析报告" in joined
    assert "调用工具 extract_text" not in joined


def test_progress_shows_ai_retry_message(
        single_fail_txt, monkeypatch):
    """Verify 驱动的 AI 重解读在进度条显示"重新解读「维度」"中文文案。"""
    call_count = {"n": 0}
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return json.dumps({
                "innovation_point":
                    "量子纠缠区块链元宇宙脑机接口核聚变泛在计算数字孪生 " * 8
                    + "[原文位置：999]",
            }, ensure_ascii=False)
        return json.dumps({
            "innovation_point": full_text.strip() + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)
    messages = []
    run_parse_agent(
        single_fail_txt, rule_detail={}, model=dict(FAKE_MODEL),
        progress_callback=lambda percent, msg: messages.append(msg),
    )
    assert call_count["n"] == 2
    joined = " | ".join(messages)
    # 提速后首次整体解读被裁剪：仅本地章节过短的创新点交 AI，走单维度补充文案
    assert "AI 正在补充解读「创新点」" in joined
    assert "重新解读「创新点」" in joined


def test_verify_and_remediation_percent_monotonic_around_stage5():
    """阶段5补救（80-84）→校验→Refine 的百分比严格不倒挂。"""
    from agent.nodes import remediation_percent
    from agent.nodes.verify_node import _verify_percent

    # 阶段5动作完成于 84：首轮校验按 pct_end+1 钳到 85（实际流程中阶段5
    # 动作只出现在首次校验之后，届时为非首轮，直接取 88）
    after_stage5 = {"tool_history": [
        {"tool": "ai_deep_analyze", "pct_end": C.AGENT_PCT_STAGE5_END}]}
    assert _verify_percent(after_stage5, first_round=True) == 85
    assert _verify_percent(after_stage5, first_round=False) \
        == C.AGENT_PCT_VERIFY_RETRY

    # 关键词收尾（74）后的信任/完整校验锚点不受钳制影响
    # （原件泳道历史中必有成功的 ai_read_original）
    after_keywords = {
        "ai_original_trusted": True,
        "tool_history": [
            {"tool": "ai_read_original", "output": {"ok": True}},
            {"tool": "extract_keywords",
             "pct_end": C.AGENT_PCT_KEYWORD_TAIL_END}]}
    assert _verify_percent(after_keywords, first_round=True) \
        == C.AGENT_PCT_VERIFY_TRUSTED
    after_keywords["ai_original_trusted"] = False
    assert _verify_percent(after_keywords, first_round=True) \
        == C.AGENT_PCT_VERIFY_FIRST

    # 补救动作即使步数较小，也不低于刚完成的校验锚点 88（取小步数暴露下限）
    assert remediation_percent(
        {"last_verify_percent": C.AGENT_PCT_VERIFY_RETRY}, step=3) \
        == C.AGENT_PCT_VERIFY_RETRY
    # 无校验锚点时退回步数公式（REFINE_BASE=85，step=7 → 89 封顶）
    assert remediation_percent({}, step=7) == 89


def test_ai_prompts_enforce_paragraph_and_json_safety():
    """解读/重解读提示词包含完整段落、禁编造与 JSON 安全等反幻觉约束。"""
    from agent.prompts import build_refine_prompt

    prompt = build_ai_analyze_prompt("")
    # 整体解读指令主体取自外置模板 resources/prompts/ai_parse_user_prompt.txt
    assert "完整句子" in prompt
    assert "不要编造" in prompt
    assert "中文引号“”" in prompt
    assert "原文位置" in prompt

    refine = build_refine_prompt("core_view")
    assert "未通过校验" in refine
    assert "核心观点" in refine
    assert "完整中文语句" in refine
    # 锚点必须引用材料中的【段落n】全局段落号，严禁章节内自行从 0 编号
    assert "【段落n】" in refine
    assert "严禁自行从 0 编号" in refine


def test_director_prompt_carries_layout_dims_and_tool_pool():
    """Director 单步决策提示词携带布局/缺失维度/步数/工具池与工具使用纪律。"""
    from agent.prompts import (
        DIRECTOR_SYSTEM_PROMPT, build_director_user_prompt,
    )

    user = build_director_user_prompt(
        layout={
            "format": "pdf", "is_scanned": False,
            "has_two_columns": True, "page_count": 10,
            "pages_with_images": [7], "table_pages": [2],
        },
        enabled_dims=["core_view", "innovation_point", "reference_list"],
        missing_dims=["innovation_point"],
        executed=[{"step": 2, "tool": "ai_read_original", "ok": True,
                   "observation": "返回 4 个维度"}],
        tool_descriptions=[
            "extract_tables(page_index, max_tables)：提取表格转 Markdown"],
        remaining_steps=4, char_count=3200, has_abs_text=True,
        table_texts_count=1,
    )
    # 布局行：双栏 + 图表页 + 体检疑似表格页（索引 2 → 第 3 页）
    assert "双栏排版" in user
    assert "图表页：第 8 页" in user
    assert "体检疑似含表格页：第 3 页" in user
    # 覆盖状态与剩余步数
    assert "当前仍缺内容的维度：创新点(innovation_point)" in user
    assert "剩余可用步数：4" in user
    assert "已结构化表格数：1" in user
    assert "extract_tables" in user
    # 系统提示纪律：只输出单动作 JSON / finalize / 参考文献不交 AI
    assert "参考文献" in DIRECTOR_SYSTEM_PROMPT
    assert "finalize" in DIRECTOR_SYSTEM_PROMPT
    assert "JSON" in DIRECTOR_SYSTEM_PROMPT


# ================= 阶段1：三项提速改造专项测试 =================

def _single_dim_correct_chat(source_text):
    """构造"只返回创新点正确解读"的假模型：复述创新点章节并标注真实全文锚点。"""
    paragraphs = [p for p in source_text.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        return json.dumps({
            "innovation_point": paragraphs[inno_idx] + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False)

    return fake_chat


def test_text_track_never_invokes_director_llm(single_fail_txt, monkeypatch):
    """文本通道链路（无原件通道）全程确定性编排，不调用 Director LLM 规划。"""

    def _boom(*args, **kwargs):
        raise AssertionError("文本通道确定性尾部不应调用 Director LLM")

    monkeypatch.setattr("agent.llm.plan_next_action", _boom)
    monkeypatch.setattr(
        "tool_layer.ai_client.chat_with_text",
        _single_dim_correct_chat(SINGLE_FAIL_TEXT))

    state = run_parse_agent(single_fail_txt, rule_detail={}, model=dict(FAKE_MODEL))
    assert state["final_report"]["verified"] is True
    assert state["final_report"]["engine"] == "agent_ai"


def test_stage5_fills_missing_dim_after_fc_original_read(
        single_fail_txt, monkeypatch):
    """AI 主导：FC 通读原件只产出四维度（缺创新点）→ 条件本地尾部 →
    首次完整校验后由阶段5 Director 单步决策排 1 个补解读动作补齐。

    读中循环（chat_round）只发生 1 次且模型未发起工具调用；条件尾部按
    extract_text → detect_structure → keywords 执行；补解读走文本通道。
    """
    planned = {"n": 0}
    rounds = {"n": 0}
    four_dims = [d for d in NON_AUTH_DIMS if d != "innovation_point"]
    payload4, _chosen = _original_payload(SINGLE_FAIL_TEXT, four_dims)
    payload_inno, _chosen2 = _original_payload(
        SINGLE_FAIL_TEXT, ["innovation_point"])

    monkeypatch.setattr(
        ai_client, "resolve_file_channel",
        lambda model: ai_client.FILE_CHANNEL_ID)
    monkeypatch.setattr(ai_client, "upload_file", lambda *a, **k: "file-xyz")

    def fake_round(base_url, api_key, model_name, messages, tools=None,
                   timeout=None, on_waiting=None, **kwargs):
        rounds["n"] += 1
        # 首轮（也是唯一一轮）即原子产出四维度 JSON，不发起读中工具调用
        return {"content": payload4, "tool_calls": [],
                "finish_reason": "stop"}

    monkeypatch.setattr(ai_client, "chat_round", fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text",
                        lambda *a, **k: payload_inno)

    def fake_single_step(model, user_prompt, page_count=0):
        planned["n"] += 1
        return {"tool": "ai_deep_analyze",
                "args": {"focus": "innovation_point"},
                "reason": "创新点缺失，结合全文补解读"}

    monkeypatch.setattr("agent.llm.plan_next_action", fake_single_step)

    state = run_parse_agent(single_fail_txt, rule_detail={},
                            model=dict(FILE_ID_MODEL))
    assert rounds["n"] == 1
    assert planned["n"] == 1
    names = [h["tool"] for h in state["tool_history"]]
    # 条件本地尾部（全文/章节/关键词）在前，阶段5 补解读在首次校验之后
    assert names == [
        "inspect_document", "ai_read_original", "extract_text",
        "detect_structure", "extract_keywords", "ai_deep_analyze"]
    fallback = next(
        h for h in state["tool_history"]
        if h["tool"] == "ai_deep_analyze")
    assert fallback["args"]["focus"] == "innovation_point"
    assert state["final_report"]["verified"] is True
    # 信任标记只在原件通读当刻判定：缺维度未获信任，全程走完整三重校验
    assert state["final_report"]["verification_mode"] == "full"


def test_first_ai_focus_shrunk_to_weak_local_dims(
        single_fail_txt, monkeypatch):
    """改动3：首次整体 AI 解读的 focus 裁剪为本地章节过短的创新点，且一次通过。"""
    call_count = {"n": 0}

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        return _single_dim_correct_chat(SINGLE_FAIL_TEXT)(
            base_url, api_key, model_name, full_text, prompt,
            system_prompt=system_prompt, max_tokens=max_tokens)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)
    state = run_parse_agent(single_fail_txt, rule_detail={}, model=dict(FAKE_MODEL))

    ai_calls = [h for h in state["tool_history"]
                if h["tool"] == "ai_deep_analyze"]
    assert len(ai_calls) == 1
    # 五维度 focus 被裁剪为唯一本地不充实的创新点；整体解读不带 basis
    assert ai_calls[0]["args"]["focus"] == "innovation_point"
    assert not ai_calls[0]["args"].get("basis")
    assert call_count["n"] == 1
    assert state["final_report"]["verified"] is True


def test_refine_merges_multiple_dims_into_one_mixed_call(
        merge_fail_txt, monkeypatch):
    """改动2：核心观点与创新点同时校验失败 → 合并为一次 mixed_section 调用补救。"""
    call_count = {"n": 0}
    paragraphs = [p for p in MERGE_FAIL_TEXT.split("\n\n") if p.strip()]
    core_idx = next(i for i, p in enumerate(paragraphs)
                    if "模块化设计能够提升迭代效率" in p)
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 两维度均返回无关长文 + 越界锚点：一致性与溯源同时失败
            hallucinated = (
                "量子纠缠区块链元宇宙脑机接口核聚变泛在计算数字孪生 " * 8
                + "[原文位置：999]")
            return json.dumps({
                "core_view": hallucinated,
                "innovation_point": hallucinated,
            }, ensure_ascii=False)
        # 合并重解读：两维度回到各自章节事实，锚点按全文段落编号标注
        return json.dumps({
            "core_view": paragraphs[core_idx] + f"[原文位置：{core_idx}]",
            "innovation_point": paragraphs[inno_idx]
                                + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)
    state = run_parse_agent(merge_fail_txt, rule_detail={}, model=dict(FAKE_MODEL))

    # 1 次整体解读（同样被裁剪为两个弱维度）+ 1 次合并重解读
    assert call_count["n"] == 2
    mixed_calls = [
        h for h in state["tool_history"]
        if h["tool"] == "ai_deep_analyze"
        and h["args"].get("basis") == "mixed_section"
    ]
    assert len(mixed_calls) == 1
    focus = mixed_calls[0]["args"]["focus"].split(",")
    assert set(focus) == {"core_view", "innovation_point"}
    # 合并后不再为每个维度单独发 local_section 调用
    assert not [
        h for h in state["tool_history"]
        if h["tool"] == "ai_deep_analyze"
        and h["args"].get("basis") == "local_section"
    ]
    report = state["final_report"]
    assert report["verified"] is True
    assert "模块化" in report["core_view"]
    assert "模块依赖图" in report["innovation_point"]


def test_overall_ai_skipped_when_all_local_sections_sufficient(
        all_sufficient_txt, monkeypatch):
    """改动3：本地章节全部充实时整体跳过 AI，零模型调用也能通过三重校验。"""

    def _boom(*args, **kwargs):
        raise AssertionError("本地章节已全部充实，不应发起任何模型调用")

    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", _boom)
    state = run_parse_agent(all_sufficient_txt, rule_detail={},
                            model=dict(FAKE_MODEL))

    ai_calls = [h for h in state["tool_history"]
                if h["tool"] == "ai_deep_analyze"]
    assert len(ai_calls) == 1
    assert ai_calls[0]["output"].get("skipped") is True
    assert ai_calls[0]["output"].get("reason") == "all_dimensions_covered_by_local"
    report = state["final_report"]
    assert report["verified"] is True
    assert report["engine"] == "agent_local"
    for dim in ALL_DIMS:
        assert report[dim].strip(), f"{dim} 应由本地章节兜底填充"


def test_refine_prompt_multi_only_lists_target_dims():
    """改动2配套：合并重解读提示词只声明/示例目标维度键，非法输入返回空串。"""
    from agent.prompts import build_refine_prompt_multi

    prompt = build_refine_prompt_multi(["core_view", "innovation_point"])
    assert '"core_view"' in prompt
    assert '"innovation_point"' in prompt
    assert "核心观点" in prompt and "创新点" in prompt
    assert '"reference_list"' not in prompt
    assert '"research_method"' not in prompt
    # 单维度也走合并提示词构造（Refine 实际按是否 2+ 维度选通道）
    single = build_refine_prompt_multi(["core_view"])
    assert '"core_view"' in single and '"innovation_point"' not in single
    assert build_refine_prompt_multi([]) == ""
    assert build_refine_prompt_multi(["not_a_dim"]) == ""


# ================= 阶段2：视觉感知（扫描件 / 双栏 / 图文混合）集成测试 =================
#
# 用 PyMuPDF 实时构造三类 PDF：
# - 双栏 PDF：左栏放标准章节文本、右栏放填充文字（china-s 内置中文字体，
#   保证 pdfplumber 也能提取文本层）；
# - 扫描件 PDF：整页位图无文本层，OCR 结果用 monkeypatch 注入，无真实网络；
# - 图文混合 PDF：上半页章节文字 + 下半页整幅大图（覆盖面积超阈值）。

from tool_layer import (  # noqa: E402
    ai_client, document_vision, file_parser, text_analysis,
)
from utils.exceptions import (  # noqa: E402
    AIServiceError,
    FileInvalidError,
    LiteratureAgentError,
)
from tests.test_document_vision import _build_scanned_pdf  # noqa: E402
from agent.state import DIMENSION_KEYS  # noqa: E402

import pymupdf  # noqa: E402
import re  # noqa: E402

VISION_MODEL = dict(FAKE_MODEL, vision_enabled=True)
# 原件直传通道模型（file_channel 持久化字段驱动 Agent 选路）
FILE_ID_MODEL = dict(FAKE_MODEL, file_channel="file_id")
FILE_DATA_MODEL = dict(FAKE_MODEL, file_channel="file_data")
# 参考文献由本地权威提取，AI 整体解读覆盖其余五个维度
NON_AUTH_DIMS = [d for d in DIMENSION_KEYS if d != "reference_list"]
_DOUBLE_FILLER = (
    "右栏填充文字用于保证双栏探测时页面两侧均有足量的词块与字符分布。" * 12
)
_FIGURE_OCR_TEXT = "图1 模块化架构示意图：展示模块依赖关系与模块边界划分流程。"


def _build_double_column_pdf(path):
    """左栏标准章节 + 右栏填充文字的双栏中文 PDF（中文字体可被 pdfplumber 提取）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 50, 285, 800),
                        SAMPLE_TEXT, fontname="china-s", fontsize=8)
    page.insert_textbox(pymupdf.Rect(310, 50, 555, 800),
                        _DOUBLE_FILLER, fontname="china-s", fontsize=8)
    doc.save(path)
    doc.close()


def _build_figure_chinese_pdf(path):
    """上半页章节文字 + 下半页整幅大图（图片覆盖比约 0.30，超过图表页阈值）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 45, 545, 420),
                        SAMPLE_TEXT, fontname="china-s", fontsize=7)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), 2)
    page.insert_image(pymupdf.Rect(90, 440, 505, 770), pixmap=pixmap)
    doc.save(path)
    doc.close()


@pytest.fixture
def double_pdf(tmp_path):
    path = tmp_path / "double.pdf"
    _build_double_column_pdf(str(path))
    return str(path)


@pytest.fixture
def scanned_pdf(tmp_path):
    """2 页无文本层扫描件。"""
    path = tmp_path / "scanned.pdf"
    _build_scanned_pdf(str(path), pages=2)
    return str(path)


@pytest.fixture
def scanned_pdf_one(tmp_path):
    """1 页无文本层扫描件。"""
    path = tmp_path / "scanned1.pdf"
    _build_scanned_pdf(str(path), pages=1)
    return str(path)


@pytest.fixture
def figure_pdf(tmp_path):
    path = tmp_path / "figure.pdf"
    _build_figure_chinese_pdf(str(path))
    return str(path)


def test_double_column_pdf_uses_standard_extract_chain(double_pdf):
    """场景一：双栏 PDF 被识别后仍走标准文本提取链路，不触发 OCR。"""
    state = run_parse_agent(double_pdf, rule_detail={}, model=None)
    layout = state["inspect_layout"]
    assert layout["has_two_columns"] is True
    assert 0.4 <= layout["column_gutter_estimate"] <= 0.6

    tool_names = [h["tool"] for h in state["tool_history"]]
    assert tool_names == [
        "inspect_document", "extract_text",
        "detect_structure", "extract_keywords"]
    assert "ocr_page" not in tool_names
    assert len(state["abs_text"]) > 100

    report = state["final_report"]
    assert report["verified"] is True
    assert report["engine"] == "agent_local"
    assert "张三" in report["reference_list"]


def test_scanned_pdf_runs_per_page_ocr_without_extract(
        scanned_pdf, monkeypatch):
    """场景二：扫描件自动逐页 OCR（本地轨），全程不调用 extract_text。"""
    def fake_local_ocr(image_bytes, lang=None):
        return SAMPLE_TEXT, 0.95

    monkeypatch.setattr(document_vision, "ocr_image_local", fake_local_ocr)
    state = run_parse_agent(scanned_pdf, rule_detail={}, model=None)

    assert state["inspect_layout"]["is_scanned"] is True
    tool_names = [h["tool"] for h in state["tool_history"]]
    assert tool_names == [
        "inspect_document", "ocr_page", "ocr_page",
        "detect_structure", "extract_keywords"]
    assert "extract_text" not in tool_names

    ocr_calls = [h for h in state["tool_history"] if h["tool"] == "ocr_page"]
    assert [h["args"]["page_index"] for h in ocr_calls] == [0, 1]
    assert all(h["output"]["ok"] and h["output"]["engine"] == "tesseract"
               for h in ocr_calls)
    # 全文完全来自两页 OCR 拼接
    assert "模块化" in state["abs_text"]
    assert state["abs_text"].count("摘要") == 2

    report = state["final_report"]
    assert report["verified"] is True
    assert len(report["keywords"]) > 0
    assert not any("识别失败" in w for w in report["warnings"])


def test_scanned_pdf_uses_ai_vision_track(scanned_pdf_one, monkeypatch):
    """视觉轨：勾选视觉能力的模型自动走多模态识别，引擎标记 ai_vision。"""
    vision_calls = {"n": 0}
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def fake_vision(base_url, api_key, model_name, image_bytes, prompt,
                    image_mime="image/png", system_prompt=None,
                    on_waiting=None):
        vision_calls["n"] += 1
        assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert "这是文献第 1 页。" in prompt  # 提示词带页码定位
        return SINGLE_FAIL_TEXT

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        return json.dumps({
            "innovation_point": paragraphs[inno_idx]
                                + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False)

    monkeypatch.setattr("tool_layer.ai_client.chat_with_vision", fake_vision)
    monkeypatch.setattr("tool_layer.ai_client.chat_with_text", fake_chat)

    state = run_parse_agent(scanned_pdf_one, rule_detail={},
                            model=dict(VISION_MODEL))

    ocr_calls = [h for h in state["tool_history"] if h["tool"] == "ocr_page"]
    assert len(ocr_calls) == 1
    assert ocr_calls[0]["output"]["engine"] == "ai_vision"
    assert vision_calls["n"] == 1
    assert "extract_text" not in [h["tool"] for h in state["tool_history"]]

    report = state["final_report"]
    assert report["verified"] is True
    assert report["engine"] == "agent_ai"
    assert "模块依赖图" in report["innovation_point"]


def test_figure_pdf_ocr_appended_between_keywords_and_ai(
        figure_pdf, monkeypatch):
    """场景三：图文混合先提文本做结构识别，图表页在关键词后单独 OCR 并追加全文。"""
    monkeypatch.setattr(
        document_vision, "ocr_image_local",
        lambda image_bytes, lang=None: (_FIGURE_OCR_TEXT, 0.9))

    state = run_parse_agent(figure_pdf, rule_detail={}, model=None)

    layout = state["inspect_layout"]
    assert layout["is_scanned"] is False
    assert layout["pages_with_images"] == [0]

    tool_names = [h["tool"] for h in state["tool_history"]]
    assert tool_names == [
        "inspect_document", "extract_text",
        "detect_structure", "extract_keywords", "ocr_page"]

    ocr_hist = [h for h in state["tool_history"] if h["tool"] == "ocr_page"]
    assert ocr_hist[0]["args"]["page_index"] == 0
    # OCR 文本带页码标记追加在提取全文之后，且不抢占章节切分
    assert state["abs_text"].rstrip().endswith(_FIGURE_OCR_TEXT)
    assert f"【{C.AGENT_FIGURE_OCR_BLOCK_TITLE}·第1页】" in state["abs_text"]

    report = state["final_report"]
    assert report["verified"] is True
    assert "张三" in report["reference_list"]
    assert not any("识别失败" in w for w in report["warnings"])


def test_auto_mode_falls_back_to_local_when_vision_fails(
        scanned_pdf_one, monkeypatch):
    """auto 模式：AI 视觉调用失败时自动降级本地 OCR，解析不中断。"""
    paragraphs = [p for p in SINGLE_FAIL_TEXT.split("\n\n") if p.strip()]
    inno_idx = next(i for i, p in enumerate(paragraphs) if "模块依赖图" in p)

    def vision_boom(*args, **kwargs):
        raise LiteratureAgentError("视觉服务超时")

    monkeypatch.setattr("tool_layer.ai_client.chat_with_vision", vision_boom)
    monkeypatch.setattr(
        document_vision, "ocr_image_local",
        lambda image_bytes, lang=None: (SINGLE_FAIL_TEXT, 0.9))
    monkeypatch.setattr(
        "tool_layer.ai_client.chat_with_text",
        lambda *a, **k: json.dumps({
            "innovation_point": paragraphs[inno_idx]
                                + f"[原文位置：{inno_idx}]",
        }, ensure_ascii=False))

    state = run_parse_agent(scanned_pdf_one, rule_detail={},
                            model=dict(VISION_MODEL))
    ocr_hist = [h for h in state["tool_history"] if h["tool"] == "ocr_page"]
    assert len(ocr_hist) == 1
    assert ocr_hist[0]["output"]["ok"] is True
    assert ocr_hist[0]["output"]["engine"] == "tesseract"
    assert state["final_report"]["verified"] is True


def test_ocr_unavailable_on_both_tracks_finishes_with_warning(
        scanned_pdf_one, monkeypatch):
    """两轨都不可用（无视觉模型 + 本机无 Tesseract）：跳页带告警收尾，不闪退。"""
    monkeypatch.setattr(document_vision, "is_local_ocr_available",
                        lambda: False)

    state = run_parse_agent(scanned_pdf_one, rule_detail={}, model=None)
    report = state["final_report"]

    tool_names = [h["tool"] for h in state["tool_history"]]
    assert tool_names == [
        "inspect_document", "ocr_page",
        "detect_structure", "extract_keywords"]
    ocr_hist = [h for h in state["tool_history"] if h["tool"] == "ocr_page"][0]
    assert ocr_hist["output"]["ok"] is False
    assert report["verified"] is False
    assert any("识别失败" in w for w in report["warnings"])

    # 全文为空时三重校验判致命失败：业务层据此回退本地兜底，
    # 绝不允许六维度全空的"成功空报告"落库
    assert (state.get("verify_result") or {}).get("fatal") is True

    # 全文为空后需要 text 形参的工具必须短路为中文结构化错误，
    # 不能让 pydantic 英文校验异常进入告警/日志
    outputs = {h["tool"]: h["output"] for h in state["tool_history"]}
    for tool_name in ("detect_structure", "extract_keywords"):
        assert outputs[tool_name]["ok"] is False
        assert "文献全文为空" in outputs[tool_name]["error"]
    joined_warnings = " ".join(report["warnings"])
    assert "validation error" not in joined_warnings
    assert "Field required" not in joined_warnings


def test_inspect_failure_falls_back_to_plain_extract(
        double_pdf, monkeypatch):
    """布局探测本身异常时不致命：按普通文本兜底走标准提取链路。

    用双栏 PDF fixture（带文本层）——PDF 链路会先调 inspect，
    inspect 炸了后 Reflect 防御分支 layout=None 直接入队 extract_text，
    普通文本提取仍能成功，整链路非致命收尾。
    """
    def inspect_boom(*args, **kwargs):
        raise FileInvalidError("模拟布局检查组件异常")

    monkeypatch.setattr(document_vision, "inspect_document_layout",
                        inspect_boom)
    state = run_parse_agent(double_pdf, rule_detail={}, model=None)

    names = [h["tool"] for h in state["tool_history"]]
    # inspect 被调且炸了，后续仍走完普通提取链路，不致命
    assert names[0] == "inspect_document"
    assert "extract_text" in names
    assert "detect_structure" in names
    assert state.get("error") is None
    report = state["final_report"]
    assert report is not None
    assert any("布局检查失败" in w for w in report["warnings"])


def test_scanned_pdf_progress_messages(scanned_pdf_one, monkeypatch):
    """扫描件链路的中文进度文案与单调百分比。"""
    monkeypatch.setattr(
        document_vision, "ocr_image_local",
        lambda image_bytes, lang=None: (SAMPLE_TEXT, 0.9))

    events = []
    state = run_parse_agent(
        scanned_pdf_one, rule_detail={}, model=None,
        progress_callback=lambda percent, msg: events.append((percent, msg)))
    assert state["final_report"]["verified"] is True

    percents = [p for p, _ in events]
    assert percents == sorted(percents)
    joined = " | ".join(m for _, m in events)
    assert "正在抽样检查原件布局" in joined
    assert "正在识别第 1 页文字" in joined
    assert "扫描件第 1/1 页识别完成" in joined


# ================= 阶段3：AI 原件直传通道（优先原件，失败降级文本） =================

def _original_payload(text: str, dims: list):
    """构造"模型通读原件后返回的合法六维度 JSON"。

    每个维度选取与本地结构识别章节词汇重叠最高的全局段落作为解读内容（模拟
    模型真实锚点：解读内容应来自所引段落），并标注其真实段落索引；本地
    章节缺失时退回最长段落。段落切分与 verifiers 完全一致（split("\\n\\n")
    后过滤空段），溯源校验必过。

    Returns:
        (payload_json, {维度key: (段落原文, 段落索引)})
    """
    paragraphs = [p for p in (text or "").strip().split("\n\n") if p.strip()]
    structure, _anchors = text_analysis.detect_structure_with_anchors(text or "")
    from agent.verifiers import _content_words

    longest = max(paragraphs, key=len)
    chosen = {}
    payload_obj = {}
    for dim in dims:
        target = structure.get(dim) or ""
        best_idx, best_score = None, -1.0
        if target:
            target_words = _content_words(target)
            for idx, paragraph in enumerate(paragraphs):
                para_words = _content_words(paragraph)
                score = (len(target_words & para_words)
                         / max(len(target_words | para_words), 1))
                if score > best_score:
                    best_score, best_idx = score, idx
        if best_idx is None or best_score <= 0:
            best_idx = paragraphs.index(longest)
        paragraph = paragraphs[best_idx]
        chosen[dim] = (paragraph, best_idx)
        payload_obj[dim] = f"{paragraph}[原文位置：{best_idx}]"
    payload = json.dumps(payload_obj, ensure_ascii=False)
    return payload, chosen


def test_file_id_channel_reads_original_and_skips_figure_ocr(
        figure_pdf, monkeypatch):
    """file_id 通道：直接上传原件经读中 FC 循环通读；图文混合不再 OCR 图表页；
    全程不调文本通道。"""
    calls = {"upload": 0, "round": 0, "chat_text": 0, "local_pdf": 0}
    extracted, _, _ = file_parser.auto_extract(figure_pdf)
    payload, chosen = _original_payload(extracted, NON_AUTH_DIMS)
    candidate = chosen["innovation_point"][0]

    def fake_upload(*args, **kwargs):
        calls["upload"] += 1
        return "file-abc-123"

    def fake_round(base_url, api_key, model_name, messages, tools=None,
                   timeout=None, on_waiting=None, **kwargs):
        calls["round"] += 1
        # 首轮消息必须引用上传得到的 file_id 部件并同框携带 tools schema
        first_user = messages[1]["content"]
        file_part = first_user[0]
        assert file_part["type"] == "file"
        assert file_part.get("file_id") == "file-abc-123"
        assert tools  # 原件与 tools 同框（cohabit）
        return {"content": payload, "tool_calls": [], "finish_reason": "stop"}

    def reject_chat_text(*args, **kwargs):
        calls["chat_text"] += 1
        raise AssertionError("原件通道可用时不应调用全文文本通道")

    def reject_local_pdf(*args, **kwargs):
        calls["local_pdf"] += 1
        raise AssertionError("file_id 通道不应走 PDF Base64 内联")

    monkeypatch.setattr(ai_client, "upload_file", fake_upload)
    monkeypatch.setattr(ai_client, "chat_round", fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", reject_chat_text)
    monkeypatch.setattr(ai_client, "chat_with_local_pdf", reject_local_pdf)

    state = run_parse_agent(figure_pdf, rule_detail={},
                            model=dict(FILE_ID_MODEL))
    names = [h["tool"] for h in state["tool_history"]]
    # AI 主导：第一步即直读含图表的原件，图表页不再单独 OCR；
    # 读中循环未调工具 → 条件尾部补全文/参考文献章节识别 → 关键词
    assert names == [
        "inspect_document", "ai_read_original", "extract_text",
        "detect_structure", "extract_keywords"]
    assert "ocr_page" not in names
    assert calls["upload"] == 1 and calls["round"] == 1
    assert calls["chat_text"] == 0
    assert state["inspect_layout"]["pages_with_images"] == [0]
    assert state["final_report"]["verification_mode"] == "original_trusted"

    original = next(h for h in state["tool_history"]
                    if h["tool"] == "ai_read_original")
    # FC 留痕：同框模式、1 轮、未调用读中中间件
    assert original["output"]["fc_mode"] == "cohabit"
    assert original["output"]["fc_rounds"] == 1
    assert original["output"]["fc_tools"] == []

    report = state["final_report"]
    assert report["verified"] is True
    assert report["engine"] == "agent_ai"
    assert report["ai_channel"] == "agent_ai_file"
    # Finish 出口统一做章节归一化（拼回 PDF 提取的硬换行），按归一化文本比对
    expected = text_analysis.normalize_section_text(
        "innovation_point", candidate)
    assert expected in report["innovation_point"]


def test_file_data_channel_reads_pdf_inline(double_pdf, monkeypatch):
    """file_data 通道：PDF 经 Base64 内联进入读中 FC 循环，不走 /files 与文本通道。"""
    calls = {"upload": 0, "round": 0, "chat_text": 0}
    extracted, _, _ = file_parser.auto_extract(double_pdf)
    payload, _chosen = _original_payload(extracted, NON_AUTH_DIMS)

    def reject_upload(*args, **kwargs):
        calls["upload"] += 1
        raise AssertionError("file_data 通道不应调用 /files 上传")

    def fake_round(base_url, api_key, model_name, messages, tools=None,
                   timeout=None, on_waiting=None, **kwargs):
        calls["round"] += 1
        # 首轮用户部件：内联 PDF（data URI）+ 文本指令，且 tools 同框
        file_part = messages[1]["content"][0]
        assert file_part["type"] == "file"
        data_uri = file_part["file"]["file_data"]
        assert data_uri.startswith("data:application/pdf;base64,")
        assert file_part["file"]["filename"].endswith(".pdf")
        assert "file_id" not in file_part
        assert tools
        return {"content": payload, "tool_calls": [], "finish_reason": "stop"}

    def reject_chat_text(*args, **kwargs):
        calls["chat_text"] += 1
        raise AssertionError("PDF 内联通道可用时不应退化为全文文本")

    monkeypatch.setattr(ai_client, "upload_file", reject_upload)
    monkeypatch.setattr(ai_client, "chat_round", fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", reject_chat_text)

    state = run_parse_agent(double_pdf, rule_detail={},
                            model=dict(FILE_DATA_MODEL))
    names = [h["tool"] for h in state["tool_history"]]
    # AI 主导：双栏 PDF 第一步即 Base64 内联直读，随后条件尾部补全文/
    # 参考文献章节识别 → 关键词收尾
    assert names == [
        "inspect_document", "ai_read_original", "extract_text",
        "detect_structure", "extract_keywords"]
    assert state["inspect_layout"]["has_two_columns"] is True
    assert calls["round"] == 1 and calls["upload"] == 0
    assert calls["chat_text"] == 0
    assert state["final_report"]["verified"] is True
    assert state["final_report"]["verification_mode"] == "original_trusted"
    assert state["final_report"]["ai_channel"] == "agent_ai_file"


def test_file_data_channel_on_txt_uses_text_track(sample_txt, monkeypatch):
    """file_data 通道仅支持 PDF：TXT 文献自动改用全文文本解读工具。"""
    calls = {"local_pdf": 0, "chat_text": 0}
    payload, _chosen = _original_payload(SAMPLE_TEXT, NON_AUTH_DIMS)

    def reject_local_pdf(*args, **kwargs):
        calls["local_pdf"] += 1
        raise AssertionError("非 PDF 不应走 PDF Base64 内联")

    def fake_chat_text(*args, **kwargs):
        calls["chat_text"] += 1
        return payload

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", reject_local_pdf)
    monkeypatch.setattr(ai_client, "chat_with_text", fake_chat_text)

    state = run_parse_agent(sample_txt, rule_detail={},
                            model=dict(FILE_DATA_MODEL))
    names = [h["tool"] for h in state["tool_history"]]
    # 统一 inspect；非 PDF 不能 Base64 内联 → 文本链 extract/detect/keywords
    # 后由确定性尾部排 ai_deep_analyze，全程不出现 ai_read_original
    assert names == [
        "inspect_document", "extract_text", "detect_structure",
        "extract_keywords", "ai_deep_analyze"]
    assert "ai_read_original" not in names
    assert calls["chat_text"] == 1 and calls["local_pdf"] == 0
    assert state["final_report"]["verified"] is True


def test_original_read_failure_falls_back_to_text_track(
        single_fail_txt, monkeypatch):
    """读中 FC 首轮即被网关以协议理由拒绝（非 tools 协议错误）：
    ai_read_original 整体失败，用户授权后 Act 通道降级排队一次全文解读。"""
    calls = {"upload": 0, "round": 0, "chat_text": 0}
    asked = []
    payload, _chosen = _original_payload(SINGLE_FAIL_TEXT, NON_AUTH_DIMS)

    def fake_upload(*args, **kwargs):
        calls["upload"] += 1
        return "file-xyz"

    def round_boom(*args, **kwargs):
        calls["round"] += 1
        raise AIServiceError("模拟该模型不支持文件对话协议")

    def fake_chat_text(*args, **kwargs):
        calls["chat_text"] += 1
        return payload

    monkeypatch.setattr(ai_client, "upload_file", fake_upload)
    monkeypatch.setattr(ai_client, "chat_round", round_boom)
    monkeypatch.setattr(ai_client, "chat_with_text", fake_chat_text)

    state = run_parse_agent(
        single_fail_txt, rule_detail={}, model=dict(FILE_ID_MODEL),
        degradation_callback=lambda error: asked.append(error) or True)
    history = state["tool_history"]
    names = [h["tool"] for h in history]
    # AI 主导：inspect 后第一步即 ai_read_original；失败经授权后先补全文基线，
    # 再接确定性尾部（结构/关键词/一次全文文本解读）
    assert names == [
        "inspect_document", "ai_read_original", "extract_text",
        "detect_structure", "extract_keywords", "ai_deep_analyze"]
    original = next(h for h in history if h["tool"] == "ai_read_original")
    assert original["output"]["ok"] is False
    fallback = next(h for h in history if h["tool"] == "ai_deep_analyze")
    # 通道降级是"整体全文解读"换轨，不是 Refine 的章节重解读（无 basis）
    assert fallback["output"].get("ok") is True
    assert not fallback["args"].get("basis")
    assert "innovation_point" in fallback["args"].get("focus", "")
    assert calls["upload"] == 1 and calls["round"] == 1
    assert calls["chat_text"] == 1
    assert state.get("original_read_failed") is True
    # 授权回调收到 AI 原始错误信息
    assert asked and "文件对话协议" in asked[0]

    report = state["final_report"]
    assert report["verified"] is True
    # 主通道尝试过原件但成功的是文本通道
    assert report["ai_channel"] == "agent_ai"
    assert any("AI 原件通读失败" in w for w in report["warnings"])


def test_original_read_failure_without_callback_does_not_degrade(
        single_fail_txt, monkeypatch):
    """读中 FC 失败且无授权回调：不得静默降级，标记 ai_unavailable 转本地基线。"""
    def round_boom(*args, **kwargs):
        raise AIServiceError("模拟该模型不支持文件对话协议")

    def reject_text(*args, **kwargs):
        raise AssertionError("未获用户授权时不应改走全文文本通道")

    monkeypatch.setattr(ai_client, "upload_file", lambda *a, **k: "file-xyz")
    monkeypatch.setattr(ai_client, "chat_round", round_boom)
    monkeypatch.setattr(ai_client, "chat_with_text", reject_text)

    state = run_parse_agent(single_fail_txt, rule_detail={},
                            model=dict(FILE_ID_MODEL))
    names = [h["tool"] for h in state["tool_history"]]
    # AI 主导：inspect → ai_read_original 失败被拒 → 补全文基线 + 本地结构尾部，
    # 不再排任何 AI 文本解读
    assert names == [
        "inspect_document", "ai_read_original", "extract_text",
        "detect_structure", "extract_keywords"]
    assert "ai_deep_analyze" not in names
    assert state["ai_unavailable"] is True
    report = state["final_report"]
    assert "文件对话协议" in report["ai_error"]
    assert report["engine"] == "agent_local"


def test_scanned_pdf_with_file_id_reads_original(scanned_pdf_one, monkeypatch):
    """扫描件 + file_id：第一步直读扫描原件（读中 FC 未调工具）；条件尾部
    逐页 OCR 取本地基线供完整性校验与参考文献。"""
    calls = {"upload": 0, "round": 0, "chat_text": 0}
    payload, chosen = _original_payload(SAMPLE_TEXT, NON_AUTH_DIMS)
    candidate = chosen["innovation_point"][0]

    monkeypatch.setattr(
        document_vision, "ocr_image_local",
        lambda image_bytes, lang=None: (SAMPLE_TEXT, 0.9))
    monkeypatch.setattr(
        ai_client, "upload_file",
        lambda *a, **k: (calls.__setitem__("upload", calls["upload"] + 1),
                         "file-scan")[1])

    def fake_round(base_url, api_key, model_name, messages, tools=None,
                   timeout=None, on_waiting=None, **kwargs):
        calls["round"] += 1
        return {"content": payload, "tool_calls": [], "finish_reason": "stop"}

    def reject_text(*args, **kwargs):
        calls["chat_text"] += 1
        raise AssertionError("原件直读成功时不应再调用全文文本通道")

    monkeypatch.setattr(ai_client, "chat_round", fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", reject_text)

    state = run_parse_agent(scanned_pdf_one, rule_detail={},
                            model=dict(FILE_ID_MODEL))
    names = [h["tool"] for h in state["tool_history"]]
    # 直读成功在前；扫描件本地无文本层，条件尾部以 1 次逐页 OCR 补全文，
    # 再本地识别参考文献；关键词收尾；全程无文本通道 AI
    assert names == [
        "inspect_document", "ai_read_original", "ocr_page",
        "detect_structure", "extract_keywords"]
    assert state["inspect_layout"]["is_scanned"] is True
    assert calls["upload"] == 1 and calls["round"] == 1
    assert calls["chat_text"] == 0
    assert state["final_report"]["verification_mode"] == "original_trusted"
    assert "张三" in state["final_report"]["reference_list"]
    report = state["final_report"]
    assert report["verified"] is True
    assert candidate[:8] in report["innovation_point"]


# ================= 阶段4：超长文本 map-reduce、精度档位与不可降级错误 =================

def test_ai_deep_analyze_long_text_uses_map_reduce(monkeypatch):
    """超长全文：工具内自动 map（逐块保留【段落n】）+ reduce（一次综合）。

    全局段落号锚点契约：map 块文本带【段落n】全局编号，reduce 综合结果标注
    的段落锚点与 verifiers 的空行切分一致，可直接通过溯源校验。
    """
    from agent.tools import build_tool_registry
    from agent import tools as agent_tools

    # 全文超短阈值触发 map-reduce；split_text_chunks 的块宽为函数默认参数
    # （定义时绑定），monkeypatch 模块函数本身强制小尺寸切多块
    monkeypatch.setattr(C, "AI_INLINE_FULL_CHARS", 50)
    _orig_split = agent_tools.split_text_chunks
    monkeypatch.setattr(
        agent_tools, "split_text_chunks",
        lambda full_text, *args, **kwargs:
            _orig_split(full_text, size=120, overlap=20))

    paragraphs = [p for p in SAMPLE_TEXT.split("\n\n") if p.strip()]
    idx = {key: next(i for i, p in enumerate(paragraphs)
                     if key in p) for key in ("记账类APP模块化", "随着移动支付",
                                              "多案例研究法", "模块依赖图",
                                              "任务完成时间平均缩短")}

    calls = {"map": 0, "reduce": 0}
    progress_msgs = []

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        if "其中一个片段" in prompt:
            calls["map"] += 1
            # 素材照录块内首条带全局段落号的原文行
            first_marker = next(
                line for line in full_text.splitlines() if line.startswith("【段落"))
            return json.dumps({
                "research_background": [], "core_view": [first_marker],
                "research_method": [], "innovation_point": [],
                "research_conclusion": [], "reference_list": [],
                "keywords": [],
            }, ensure_ascii=False)
        calls["reduce"] += 1
        assert "综合生成最终解析结果" in prompt
        return json.dumps({
            "research_background":
                paragraphs[idx["随着移动支付"]]
                + f"[原文位置：{idx['随着移动支付']}]",
            "core_view":
                paragraphs[idx["记账类APP模块化"]]
                + f"[原文位置：{idx['记账类APP模块化']}]",
            "research_method":
                paragraphs[idx["多案例研究法"]]
                + f"[原文位置：{idx['多案例研究法']}]",
            "innovation_point":
                paragraphs[idx["模块依赖图"]]
                + f"[原文位置：{idx['模块依赖图']}]",
            "research_conclusion":
                paragraphs[idx["任务完成时间平均缩短"]]
                + f"[原文位置：{idx['任务完成时间平均缩短']}]",
            "reference_list": paragraphs[-1] + f"[原文位置：{len(paragraphs) - 1}]",
            "keywords": ["模块化", "记账APP"],
        }, ensure_ascii=False)

    monkeypatch.setattr(ai_client, "chat_with_text", fake_chat)
    registry = build_tool_registry(
        dict(FAKE_MODEL),
        on_progress=lambda message: progress_msgs.append(message))
    out = registry["ai_deep_analyze"].invoke({
        "text": SAMPLE_TEXT, "focus": "", "precision": 3, "disabled_dims": ""})

    assert out["ok"] is True, out.get("error")
    assert calls["map"] >= 2, "全文应切成至少两个片段逐块提炼"
    assert calls["reduce"] == 1
    assert any("片段 1/" in msg and "超长文献分块解读" in msg
               for msg in progress_msgs)
    # reduce 综合的五维度与参考文献均保留，锚点被剥离为 citation_map
    assert "模块划分框架" in out["drafts"]["core_view"]
    assert out["citations"]["core_view"] == [idx["记账类APP模块化"]]
    assert out["citations"]["innovation_point"] == [idx["模块依赖图"]]
    assert out["citations"]["reference_list"] == [len(paragraphs) - 1]
    assert "[原文位置" not in out["drafts"]["innovation_point"]


def test_precision_level_passes_through_to_ai_prompt(
        sample_txt, monkeypatch):
    """端到端：run_parse_agent 的 precision 档位透传进首次 AI 解读提示词。"""
    captured_prompts = []

    paragraphs = [p for p in SAMPLE_TEXT.split("\n\n") if p.strip()]

    def _anchor(keyword):
        """取含关键词段落的 (原文, 全局段落号)。"""
        index = next(i for i, p in enumerate(paragraphs) if keyword in p)
        return paragraphs[index], index

    bg, bg_i = _anchor("随着移动支付")
    view, view_i = _anchor("记账类APP模块化")
    method, method_i = _anchor("多案例研究法")
    inno, inno_i = _anchor("模块依赖图")
    concl, concl_i = _anchor("任务完成时间平均缩短")

    def fake_chat(base_url, api_key, model_name, full_text, prompt,
                  system_prompt=None, max_tokens=None, on_waiting=None):
        captured_prompts.append(prompt)
        return json.dumps({
            "research_background": bg + f"[原文位置：{bg_i}]",
            "core_view": view + f"[原文位置：{view_i}]",
            "research_method": method + f"[原文位置：{method_i}]",
            "innovation_point": inno + f"[原文位置：{inno_i}]",
            "research_conclusion": concl + f"[原文位置：{concl_i}]",
        }, ensure_ascii=False)

    monkeypatch.setattr(ai_client, "chat_with_text", fake_chat)
    state = run_parse_agent(sample_txt, rule_detail={}, precision=1,
                            model=dict(FAKE_MODEL))
    assert state["final_report"]["verified"] is True
    # 首次整体解读提示词携带精度 1 档位说明（Refine 提示词不带档位）
    assert any("最快档，每项用 1-2 句" in p for p in captured_prompts)


def test_network_error_does_not_degrade_and_marks_ai_unavailable(
        sample_txt, monkeypatch):
    """文本通道遇网络错误（不可降级）：立即标记 ai_unavailable，不再重试 AI。"""
    chat_calls = []

    def fake_chat(*args, **kwargs):
        chat_calls.append(1)
        raise AIServiceError("无法连接模型服务：connection refused")

    monkeypatch.setattr(ai_client, "chat_with_text", fake_chat)
    state = run_parse_agent(sample_txt, rule_detail={}, model=dict(FAKE_MODEL))

    assert state["ai_unavailable"] is True
    assert "connection refused" in state["final_report"]["ai_error"]
    assert state["final_report"]["engine"] == "agent_local"
    # 首次整体解读失败后 Refine 不再发起 AI 调用
    assert len(chat_calls) == 1
