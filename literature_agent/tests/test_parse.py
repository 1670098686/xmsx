"""阶段2 B2-9：智能解析业务 + 结构识别 + 报告导出测试。"""
import json
import os

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.parse_rule_service import DIMENSION_KEYS, ParseRuleService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.dao.rule_dao import ParseRuleDao
from tool_layer import export_generator, text_analysis

PAPER_TEXT = """深度学习研究综述

张三

摘要
本文系统梳理了深度学习在计算机视觉领域的研究背景与发展脉络。

引言
近年来，以卷积神经网络为代表的深度学习方法取得显著进展，研究背景值得关注。

研究方法
本文采用文献计量法与实验对比法，收集近五年顶会论文进行统计分析。

创新点
提出一种多尺度特征融合模块，并构建了统一评测基准。

结论
深度学习方法在视觉任务上效果显著，未来将向小样本学习方向发展。

参考文献
[1] LeCun Y. Gradient-based learning applied to document recognition. 1998.
[2] Krizhevsky A. ImageNet classification with deep CNN. 2012.
"""


@pytest.fixture
def imported_lit(tmp_path):
    """导入一篇结构化 TXT 文献，返回 lit_id。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    path = tmp_path / "paper.txt"
    path.write_text(PAPER_TEXT, encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg
    return data["id"]


def test_detect_structure_sections():
    result = text_analysis.detect_structure(PAPER_TEXT)
    assert "文献计量" in result["research_method"]
    assert "多尺度特征融合" in result["innovation_point"]
    assert "小样本学习" in result["research_conclusion"]
    assert "LeCun" in result["reference_list"]


def test_extract_keywords():
    """关键词作为附加信息仍正常提取，条数受 top_n 限制。"""
    keywords = text_analysis.extract_keywords(PAPER_TEXT, top_n=10)
    assert 0 < len(keywords) <= 10
    assert all(isinstance(word, str) and weight > 0 for word, weight in keywords)
    assert text_analysis.extract_keywords("", top_n=10) == []


def test_normalize_section_text_merges_wrapped_sentences():
    """章节归一化：PDF/Word 硬换行拆散的同一句话必须拼回完整段落。"""
    raw = (
        "本文研究深度学习在计算机视觉\n"
        "领域的应用，并提出统一评测基准。\n"
        "实验结果表明该方法效果显著。"
    )
    merged = text_analysis.normalize_section_text("core_view", raw)
    assert merged == (
        "本文研究深度学习在计算机视觉领域的应用，并提出统一评测基准。\n"
        "实验结果表明该方法效果显著。"
    )
    # 空内容安全返回空串
    assert text_analysis.normalize_section_text("core_view", "  \n ") == ""


def test_normalize_section_text_keeps_reference_items():
    """参考文献维度：逐条独立成行，不做跨行拼接。"""
    raw = "[1] LeCun Y. Gradient-based learning.\n[2] Krizhevsky A. ImageNet."
    refs = text_analysis.normalize_section_text("reference_list", raw)
    assert refs == "[1] LeCun Y. Gradient-based learning.\n[2] Krizhevsky A. ImageNet."


def test_truncate_at_sentence_boundary():
    """超长兜底内容只能在句子边界截断，不允许留下半句话。"""
    text = "首句较短。第二句话长度适中恰好落在截断窗口之内。第三句保留不到。"
    clipped = text_analysis.truncate_at_sentence_boundary(text, 26)
    assert clipped.endswith("。")
    assert "第三句" not in clipped
    assert "第二句话" in clipped
    # 未超限时原样返回
    assert text_analysis.truncate_at_sentence_boundary("短句。", 50) == "短句。"


def test_split_paragraphs():
    paragraphs = text_analysis.split_into_paragraphs(PAPER_TEXT)
    assert paragraphs[0] == {"pos": "0", "content": paragraphs[0]["content"]}
    assert all(item["pos"] == str(index) for index, item in enumerate(paragraphs))


def test_parse_single_success(mem_conn, imported_lit):
    service = LiteratureParseService()
    progress = []
    code, report, msg = service.parse_single(
        imported_lit, progress_callback=lambda p, m: progress.append(p)
    )
    assert code == 0, msg
    assert report["literature_id"] == imported_lit
    assert "深度学习" in report["research_background"]
    assert "文献计量" in report["research_method"]
    assert "多尺度" in report["innovation_point"]
    # 关键词作为附加信息仍输出（固定数量上限），但各维度内容必须是完整句子
    assert isinstance(report["keywords"], list) and report["keywords"]
    assert len(report["keywords"]) <= C.PARSE_KEYWORD_TOP_N
    for field in ("research_background", "research_method",
                  "innovation_point", "research_conclusion"):
        sentence = report[field].strip()
        assert sentence, f"{field} 不应为空"
        assert sentence.endswith(("。", "！", "？")), \
            f"{field} 应以句末标点结束，实际：{sentence[:30]}"
        # 维度内容是多字段句子而非关键词罗列：长度明显大于单个词条
        assert len(sentence) >= 10, f"{field} 不能只是关键词/短语：{sentence}"
    # 进度从 5 递增到 100
    assert progress[0] < progress[-1] == 100
    # 文献状态回写为已解析
    assert LiteratureInfoDao().select_by_id(imported_lit)["is_parsed"] == C.PARSE_DONE
    # 报告表有记录
    assert LiteratureReportDao().get_by_lit_id(imported_lit) is not None


def test_parse_missing_literature(mem_conn):
    code, _data, msg = LiteratureParseService().parse_single(9999)
    assert code == C.CODE_FILE_NOT_FOUND
    assert msg == "文献不存在"


def test_reparse_replaces_report(mem_conn, imported_lit):
    service = LiteratureParseService()
    service.parse_single(imported_lit)
    code, report, msg = service.reparse(imported_lit)
    assert code == 0, msg
    # 重新解析后仍只有一篇报告（更新而非无限追加）
    rows = service.report_dao.conn.execute(
        "SELECT COUNT(1) FROM literature_report WHERE literature_id = ?",
        (imported_lit,),
    ).fetchone()
    assert rows[0] == 1


def test_parse_respects_dimension_switch(mem_conn, imported_lit):
    """关闭某维度开关后，报告对应字段应为空。"""
    rule_id = ParseRuleDao().insert({
        "rule_name": "仅背景规则",
        "subject_type": "通用",
        "rule_detail": json.dumps({
            "dimensions": {
                "research_background": True,
                "core_view": False,
                "research_method": False,
                "innovation_point": False,
                "research_conclusion": False,
                "reference_list": False,
            },
            "weights": {},
        }, ensure_ascii=False),
        "precision_level": 3,
        "is_default": 0,
    })
    code, report, msg = LiteratureParseService().parse_single(imported_lit, rule_id)
    assert code == 0, msg
    assert report["research_background"]
    assert report["innovation_point"] == ""
    assert report["reference_list"] == ""


def test_detect_structure_precision_levels():
    """精度 1-2 严格档不做段落猜测兜底；精度 3 均衡档启用首段兜底。"""
    loose_text = "这是一段没有任何标准章节标题的自由文本。\n\n第二段补充说明内容。\n"
    strict = text_analysis.detect_structure(loose_text, precision=1)
    assert strict["research_background"] == ""
    assert strict["core_view"] == ""
    assert strict["research_conclusion"] == ""

    balanced = text_analysis.detect_structure(loose_text, precision=3)
    assert balanced["research_background"]
    assert balanced["core_view"]


def test_custom_rule_subject_editable_builtin_protected(mem_conn):
    """自定义模板学科可编辑，预设模板学科只读，空/超长学科被拒绝。"""
    service = ParseRuleService()
    all_dims = {key: True for key in DIMENSION_KEYS}
    code, rule_id, msg = service.create_rule("学科测试规则", "计算机", all_dims)
    assert code == 0, msg
    assert service.is_builtin_rule(rule_id) is False
    assert service.is_builtin_rule(1) is True

    assert service.update_rule_detail(rule_id, subject_type="医学")[0] == 0
    assert service.get_rule_detail(rule_id)["subject_type"] == "医学"
    assert service.update_rule_detail(rule_id, subject_type="   ")[0] == \
        C.CODE_FILE_INVALID
    assert service.update_rule_detail(
        rule_id, subject_type="学" * 21
    )[0] == C.CODE_FILE_INVALID

    # 预设模板禁止修改适用学科，但维度/精度仍可修改
    code, _d, msg = service.update_rule_detail(1, subject_type="数学")
    assert code == C.CODE_FILE_INVALID and "预设" in msg
    assert service.update_rule_detail(1, precision=2)[0] == 0


def test_rule_detail_no_longer_has_keyword_count(mem_conn):
    """规则明细中不再存在 keyword_top_n；保存时清理历史残留字段。"""
    import json as _json
    service = ParseRuleService()
    all_dims = {key: True for key in DIMENSION_KEYS}
    code, rule_id, _ = service.create_rule("无关键词数量规则", "通用", all_dims)
    assert code == 0
    detail = service.get_rule_detail(rule_id)
    assert "keyword_top_n" not in detail
    raw = ParseRuleDao().get_by_id(rule_id)["rule_detail"]
    assert "keyword_top_n" not in _json.loads(raw)

    # 手工写入历史残留字段后保存，应被自动清除
    ParseRuleDao().update_by_id(rule_id, {"rule_detail": _json.dumps({
        "dimensions": all_dims, "keyword_top_n": 99,
    }, ensure_ascii=False)})
    assert service.update_rule_detail(rule_id, precision=2)[0] == 0
    raw = ParseRuleDao().get_by_id(rule_id)["rule_detail"]
    assert "keyword_top_n" not in _json.loads(raw)


def test_ai_prompt_requires_sentence_dimensions_and_keyword_field():
    """AI 提示词：维度字段必须是完整语句段落，keywords 仅作独立附加字段。"""
    prompt = LiteratureParseService._build_ai_prompt(
        {"dimensions": {key: True for key in DIMENSION_KEYS}}, precision=3
    )
    # 维度语句化强约束仍在
    assert "完整" in prompt and "句号" in prompt
    # keywords 作为独立附加字段被要求，且明确不得替代维度语句
    assert '"keywords"' in prompt
    assert "不得用它替代上面任何维度的语句段落" in prompt
    # 每个内容维度的描述都要求"完整语句"
    for field in ("research_background", "core_view", "research_method",
                  "innovation_point", "research_conclusion"):
        line = next(line for line in prompt.splitlines() if f'"{field}"' in line)
        assert "完整语句" in line


def test_stale_report_after_default_rule_changed(mem_conn, imported_lit):
    """默认规则修改后，此前生成的报告应判定为过期；重新解析后恢复最新。"""
    parse_service = LiteratureParseService()
    assert parse_service.parse_single(imported_lit)[0] == 0
    assert parse_service.is_report_stale(imported_lit) is False

    # SQLite 时间戳为秒级精度，间隔 1.2 秒保证规则版本严格晚于报告时间
    import time
    time.sleep(1.2)
    rule_service = ParseRuleService()
    all_dims = {key: True for key in DIMENSION_KEYS}
    assert rule_service.update_rule_detail(
        1, dimensions=all_dims, precision=4
    )[0] == 0
    assert parse_service.is_report_stale(imported_lit) is True
    assert imported_lit in parse_service.list_parsed_lit_ids()

    # 修改非默认规则不影响过期判定
    code, other_id, _ = rule_service.create_rule("另一条规则", "自定义", all_dims)
    assert code == 0
    rule_service.update_rule_detail(other_id, precision=2)
    assert parse_service.is_report_stale(imported_lit) is True

    # 重新解析后报告时间更新，不再提示过期
    time.sleep(1.2)
    assert parse_service.reparse(imported_lit)[0] == 0
    assert parse_service.is_report_stale(imported_lit) is False


def test_parse_batch(mem_conn, tmp_path):
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    ids = []
    for name, body in (("p1.txt", PAPER_TEXT),
                       ("p2.txt", "摘要\n另一篇完全不同的传感器网络研究正文。\n")):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        code, data, _ = LiteratureImportService().import_single(str(path))
        assert code == 0
        ids.append(data["id"])
    result = LiteratureParseService().parse_batch(ids)
    assert len(result["success"]) == 2
    assert result["failed"] == []


def test_export_report_word_and_txt(mem_conn, imported_lit, tmp_path):
    parse_service = LiteratureParseService()
    code, _r, msg = parse_service.parse_single(imported_lit)
    assert code == 0, msg

    export_service = ExportBackupService()
    txt_path = str(tmp_path / "report.txt")
    code, out, msg = export_service.export_report(imported_lit, "txt", txt_path)
    assert code == 0 and out == txt_path
    assert os.path.isfile(txt_path)
    assert "研究方法" in tmp_path.joinpath("report.txt").read_text(encoding="utf-8")

    docx_path = str(tmp_path / "report.docx")
    code, _out, msg = export_service.export_report(imported_lit, "word", docx_path)
    assert code == 0, msg
    assert os.path.isfile(docx_path)


def test_export_before_parse(mem_conn, imported_lit, tmp_path):
    code, _out, msg = ExportBackupService().export_report(
        imported_lit, "txt", str(tmp_path / "r.txt")
    )
    assert code == C.CODE_PARSE_FAILED
    assert "尚未解析" in msg


def test_export_generator_bad_format(tmp_path):
    with pytest.raises(Exception):
        export_generator.export_report({"literature_title": "x"},
                                       str(tmp_path / "x.md"), "md")
