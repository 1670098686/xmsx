"""阶段2 B2-9：智能解析业务 + 结构识别 + 报告导出测试。"""
import json
import os

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.parse_rule_service import DIMENSION_KEYS, ParseRuleService
from business.system_service import SystemService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.dao.rule_dao import ParseRuleDao
from tool_layer import export_generator, text_analysis
from utils.exceptions import FileInvalidError

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


def test_extract_txt_normalizes_crlf_newlines(tmp_path):
    """Windows 记事本保存的 CRLF 文献提取后换行统一为 LF，

    保证 Agent 溯源校验按空行分段时不会把整篇误判为一段（锚点全部越界）。
    """
    from tool_layer import file_parser

    crlf_path = tmp_path / "crlf.txt"
    crlf_path.write_bytes("第一段正文。\r\n\r\n第二段正文。\r\n".encode("utf-8"))
    text, _meta = file_parser.extract_txt_text(str(crlf_path))
    assert "\r" not in text
    assert text == "第一段正文。\n\n第二段正文。"
    # 与溯源校验一致的按空行切分应得到两个段落
    assert len([p for p in text.split("\n\n") if p.strip()]) == 2


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
    from agent import prompts
    prompt = prompts.build_ai_analyze_prompt(precision=3)
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


def test_rule_change_keeps_existing_report(mem_conn, imported_lit):
    """默认规则修改后，已有报告保持不变、仍可正常读取，不触发重新解析。"""
    parse_service = LiteratureParseService()
    assert parse_service.parse_single(imported_lit)[0] == 0
    report_before = LiteratureReportDao().get_by_lit_id(imported_lit)
    assert report_before is not None

    rule_service = ParseRuleService()
    all_dims = {key: True for key in DIMENSION_KEYS}
    assert rule_service.update_rule_detail(
        1, dimensions=all_dims, precision=4
    )[0] == 0

    # 改规则不写任何规则版本戳，旧报告原样保留
    assert SystemConfigDao().get("parse_rule_version", "") == ""
    report_after = LiteratureReportDao().get_by_lit_id(imported_lit)
    assert report_after == report_before

    # 报告仍可正常读取，文献仍标记为已解析；手动重新解析不受影响
    assert parse_service.get_report(imported_lit) is not None
    assert LiteratureInfoDao().select_by_id(imported_lit)["is_parsed"] == C.PARSE_DONE
    assert parse_service.reparse(imported_lit)[0] == 0
    assert LiteratureInfoDao().select_by_id(imported_lit)["is_parsed"] == C.PARSE_DONE


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


def test_default_rule_dimension_filters_view_and_export(
        mem_conn, imported_lit, tmp_path):
    """默认规则停用维度：报告视图与 TXT 导出均隐藏该章节；库内原文保留，
    重新勾选后旧报告内容恢复显示（无需重新解析）。"""
    parse_service = LiteratureParseService()
    assert parse_service.parse_single(imported_lit)[0] == 0
    rule_service = ParseRuleService()
    default_id = rule_service.get_default_rule_id()
    assert rule_service.get_default_dimensions()["innovation_point"] is True
    assert parse_service.get_report(imported_lit)["innovation_point"]

    dims = {key: True for key in DIMENSION_KEYS}
    dims["innovation_point"] = False
    code, _d, msg = rule_service.update_rule_detail(default_id, dimensions=dims)
    assert code == 0, msg
    assert rule_service.get_default_dimensions()["innovation_point"] is False

    # 视图层隐藏，数据库原文保留
    assert parse_service.get_report(imported_lit)["innovation_point"] == ""
    assert LiteratureReportDao().get_by_lit_id(imported_lit)["innovation_point"]

    # TXT 导出不含停用章节
    txt_path = str(tmp_path / "hidden.txt")
    code, _out, msg = ExportBackupService().export_report(
        imported_lit, "txt", txt_path
    )
    assert code == 0, msg
    assert "四、创新点" not in tmp_path.joinpath("hidden.txt").read_text(
        encoding="utf-8"
    )

    # 重新勾选：旧报告内容原样恢复
    dims["innovation_point"] = True
    assert rule_service.update_rule_detail(
        default_id, dimensions=dims
    )[0] == 0
    assert parse_service.get_report(imported_lit)["innovation_point"]
    txt_path2 = str(tmp_path / "shown.txt")
    assert ExportBackupService().export_report(
        imported_lit, "txt", txt_path2
    )[0] == 0
    assert "四、创新点" in tmp_path.joinpath("shown.txt").read_text(
        encoding="utf-8"
    )


def test_clear_report_keeps_literature_and_resets_status(mem_conn, imported_lit):
    """删除解析报告：报告清空、状态回未解析，文献保留且可重新解析。"""
    from data_layer.dao.log_dao import OperationLogDao
    from data_layer.dao.note_dao import LiteratureNoteDao

    service = LiteratureParseService()
    assert service.parse_single(imported_lit)[0] == 0
    lit_dao = LiteratureInfoDao()
    assert lit_dao.select_by_id(imported_lit)["is_parsed"] == C.PARSE_DONE
    assert LiteratureReportDao().get_by_lit_id(imported_lit)

    # 附带一条笔记，验证删除报告不影响笔记批注
    note_id = LiteratureNoteDao().insert({
        "literature_id": imported_lit,
        "paragraph_pos": "txh:0-10",
        "note_content": "删报告不应删笔记",
        "note_type": "paragraph",
        "highlight_style": "highlight",
    })

    code, _data, msg = service.clear_report(imported_lit)
    assert code == C.CODE_SUCCESS, msg
    # 文献记录保留、状态回到未解析
    lit = lit_dao.select_by_id(imported_lit)
    assert lit is not None
    assert lit["is_parsed"] == C.PARSE_NOT_STARTED
    # 报告已清空，get_report 回到 None
    assert LiteratureReportDao().get_by_lit_id(imported_lit) is None
    assert service.get_report(imported_lit) is None
    # 笔记保留
    assert LiteratureNoteDao().get_by_id(note_id)["note_content"] == "删报告不应删笔记"
    # 操作日志记录
    logs = OperationLogDao().get_recent(limit=5)
    assert any("删除解析报告" in (row.get("operate_content") or "")
               for row in logs)

    # 可立即重新解析
    code, _report, msg = service.parse_single(imported_lit)
    assert code == C.CODE_SUCCESS, msg
    assert lit_dao.select_by_id(imported_lit)["is_parsed"] == C.PARSE_DONE


def test_clear_report_without_report_returns_error(mem_conn, imported_lit):
    """未解析文献没有报告：删除返回业务错误且不误改文献状态。"""
    service = LiteratureParseService()
    code, _data, msg = service.clear_report(imported_lit)
    assert code != C.CODE_SUCCESS
    assert "暂无解析报告" in msg
    assert (LiteratureInfoDao().select_by_id(imported_lit)["is_parsed"]
            == C.PARSE_NOT_STARTED)


def test_clear_report_missing_literature(mem_conn):
    """文献不存在：返回文件未找到错误码。"""
    code, _data, msg = LiteratureParseService().clear_report(999999)
    assert code == C.CODE_FILE_NOT_FOUND
    assert msg


# ================= 阶段3：ReAct Agent 与业务层集成 =================

def test_parse_default_runs_agent_local_engine(mem_conn, imported_lit):
    """Agent 是唯一解析路径：未配置 AI 模型时离线走 agent_local 引擎。"""
    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "agent_local"
    assert report["agent_warnings"] == []
    assert "ReAct" in msg


def test_parse_agent_exception_falls_back_to_local(
        mem_conn, imported_lit, monkeypatch):
    """Agent 运行期抛异常时自动回退纯本地规则兜底，解析不中断、报告正常落库。"""
    def boom(*args, **kwargs):
        raise RuntimeError("langgraph 模拟崩溃")

    monkeypatch.setattr(
        "business.literature_parse.run_parse_agent", boom)

    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "local"
    assert "多尺度" in report["innovation_point"]
    # 回退动作写入了操作日志
    logs = SystemService().get_recent_logs(20)
    assert any("Agent 异常已回退" in row.get("operate_content", "") for row in logs)


def test_parse_agent_warnings_propagate_to_view(
        mem_conn, imported_lit, monkeypatch):
    """Agent 带三重校验告警完成时，warnings 透传到报告视图且报告仍成功。"""
    def fake_agent(*args, **kwargs):
        return {
            "final_report": {
                "research_background": "本地或AI产出的背景内容",
                "core_view": "核心观点内容",
                "research_method": "研究方法内容",
                "innovation_point": "创新点内容",
                "research_conclusion": "结论内容",
                "reference_list": "[1] 参考文献",
                "keywords": ["深度学习", "模块化"],
                "engine": "agent_ai",
                "verified": False,
                "warnings": ["创新点 AI 解读未通过校验，已改用本地章节原文"],
            },
            "error": None,
        }

    monkeypatch.setattr(
        "business.literature_parse.run_parse_agent", fake_agent)

    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "agent_ai"
    assert report["agent_warnings"]
    assert "校验告警" in msg
    assert report["keywords"] == ["深度学习", "模块化"]


def test_parse_agent_fatal_failure_falls_back_and_marks_failed(
        mem_conn, imported_lit, monkeypatch):
    """Agent 判定致命失败（final_report 为空）→ 回退本地规则兜底 →
    本地兜底同样因源文件缺失抛错 → 文献标记解析失败。"""
    def fatal_agent(*args, **kwargs):
        return {"final_report": None, "error": "全文提取失败：文件损坏"}

    monkeypatch.setattr(
        "business.literature_parse.run_parse_agent", fatal_agent)
    # 同时让本地兜底的文本提取也失败，模拟源文件不可读
    monkeypatch.setattr(
        "business.literature_parse.LiteratureParseService._extract_lit_text",
        lambda self, lit: (_ for _ in ()).throw(
            RuntimeError("文件无法读取")))

    code, _report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code != 0
    assert "文件无法读取" in msg
    lit = LiteratureInfoDao().select_by_id(imported_lit)
    assert lit["is_parsed"] == C.PARSE_FAILED


def test_parse_verify_fatal_empty_report_not_saved(
        mem_conn, imported_lit, monkeypatch):
    """Agent 三重校验判 fatal（全文为空，如 OCR 全部失败）：final_report
    虽是非空字典但六维度全空——business 必须回退本地兜底，本地同样提取
    失败时返回错误码、标记解析失败，绝不落库"成功的空报告"。"""
    empty_report = {key: "" for key in (
        "research_background", "core_view", "research_method",
        "innovation_point", "research_conclusion", "reference_list")}
    empty_report.update({
        "keywords": [], "engine": "agent_local", "ai_channel": "",
        "ai_error": "", "warnings": ["文献全文为空，无法解析"],
        "verified": False,
    })

    def fatal_agent(*args, **kwargs):
        return {
            "final_report": empty_report,
            "error": None,  # OCR 页失败按设计不写 state.error
            "verify_result": {"fatal": True, "passed": False,
                              "message": "文献全文为空，无法解析"},
        }

    monkeypatch.setattr(
        "business.literature_parse.run_parse_agent", fatal_agent)
    monkeypatch.setattr(
        "business.literature_parse.LiteratureParseService._extract_lit_text",
        lambda self, lit: (_ for _ in ()).throw(
            FileInvalidError("PDF 无文本层（可能是纯扫描件），无法提取文字")))

    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code != 0
    assert "无文本层" in msg
    # 全空报告不得落库，文献标记解析失败供用户重试
    assert LiteratureReportDao().get_by_lit_id(imported_lit) is None
    lit = LiteratureInfoDao().select_by_id(imported_lit)
    assert lit["is_parsed"] == C.PARSE_FAILED


def test_batch_parse_each_lit_runs_independent_agent(
        mem_conn, imported_lit, monkeypatch, tmp_path):
    """批量解析时每篇文献独立调用一次 Agent。"""
    # 再导入第二篇（内容必须不同，否则导入去重拦截）
    second = tmp_path / "paper2.txt"
    second.write_text(PAPER_TEXT + "\n补充材料：本文另含消融实验与误差分析附录。\n",
                      encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(second))
    assert code == 0, msg

    call_lit_ids = []

    def recording_agent(lit_path, rule_detail, precision=3, model=None,
                        lit_id=0, progress_callback=None, max_steps=None,
                        degradation_callback=None):
        call_lit_ids.append(lit_id)
        return {
            "final_report": {
                "research_background": "背景", "core_view": "观点",
                "research_method": "方法", "innovation_point": "创新",
                "research_conclusion": "结论", "reference_list": "[1] 文献",
                "keywords": ["深度学习"], "engine": "agent_local",
                "verified": True, "warnings": [],
            },
            "error": None,
        }

    monkeypatch.setattr(
        "business.literature_parse.run_parse_agent", recording_agent)

    result = LiteratureParseService().parse_batch(
        [imported_lit, data["id"]])
    assert result["failed"] == []
    assert len(result["success"]) == 2
    assert sorted(call_lit_ids) == sorted([imported_lit, data["id"]])
