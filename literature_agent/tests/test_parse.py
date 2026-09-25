"""阶段2 B2-9：智能解析业务 + 结构识别 + 报告导出测试。"""
import json
import os

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
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
    keywords = text_analysis.extract_keywords(PAPER_TEXT, top_n=10)
    assert 0 < len(keywords) <= 10
    assert all(isinstance(word, str) and weight > 0 for word, weight in keywords)


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
    assert isinstance(report["keywords"], list) and report["keywords"]
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
            "keyword_top_n": 5,
        }, ensure_ascii=False),
        "precision_level": 3,
        "is_default": 0,
    })
    code, report, msg = LiteratureParseService().parse_single(imported_lit, rule_id)
    assert code == 0, msg
    assert report["research_background"]
    assert report["innovation_point"] == ""
    assert report["reference_list"] == ""


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
