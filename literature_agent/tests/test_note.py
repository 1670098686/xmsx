"""阶段2 B2-9：笔记批注业务单元测试（含 1 秒防抖）。"""
import time

import pytest

from business.literature_import import LiteratureImportService
from business.note_manage import (
    NoteManageService, encode_pdf_anchor, is_pdf_anchor, parse_pdf_anchor,
)
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.note_dao import LiteratureNoteDao


@pytest.fixture
def lit_id(tmp_path):
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    path = tmp_path / "paper.txt"
    path.write_text("摘要\n测试文献内容。\n\n结论\n结论内容。", encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg
    return data["id"]


def test_add_paragraph_note(mem_conn, lit_id):
    service = NoteManageService()
    code, note, msg = service.add_paragraph_note(lit_id, "3", "这句是重点", "#E8F4F8")
    assert code == 0, msg
    assert note["note_type"] == C.NOTE_TYPE_PARAGRAPH
    assert note["paragraph_pos"] == "3"
    assert note["highlight_style"] == "#E8F4F8"
    assert len(service.get_notes_by_lit(lit_id)) == 1


def test_add_note_validation(mem_conn, lit_id):
    service = NoteManageService()
    code, _n, _m = service.add_paragraph_note(lit_id, "1", "   ")
    assert code == C.CODE_FILE_INVALID
    code, _n, _m = service.add_paragraph_note(9999, "1", "x")
    assert code == C.CODE_FILE_NOT_FOUND


def test_global_note_upsert(mem_conn, lit_id):
    service = NoteManageService()
    code, first, _ = service.add_global_note(lit_id, "全局笔记初版")
    assert code == 0
    code, second, _ = service.add_global_note(lit_id, "全局笔记修订版")
    assert code == 0
    assert first["id"] == second["id"]
    assert service.get_global_note(lit_id)["note_content"] == "全局笔记修订版"
    # 全局笔记只有一条
    globals_ = [
        n for n in service.get_notes_by_lit(lit_id)
        if n["note_type"] == C.NOTE_TYPE_GLOBAL
    ]
    assert len(globals_) == 1


def test_update_note_debounce_flush(mem_conn, lit_id):
    """连续更新只挂起一次；flush 立即落库最后内容。"""
    service = NoteManageService()
    code, note, _ = service.add_paragraph_note(lit_id, "1", "A")
    note_id = note["id"]

    service.update_note(note_id, {"note_content": "B"})
    service.update_note(note_id, {"note_content": "C"})
    # 防抖窗口内数据库仍旧
    assert LiteratureNoteDao().get_by_id(note_id)["note_content"] == "A"
    service.flush_note(note_id)
    assert LiteratureNoteDao().get_by_id(note_id)["note_content"] == "C"


def test_update_note_debounce_timer(mem_conn, lit_id):
    """1 秒后 Timer 自动落库。"""
    service = NoteManageService()
    code, note, _ = service.add_paragraph_note(lit_id, "1", "origin")
    service.update_note(note["id"], {"note_content": "timed"})
    time.sleep(1.3)
    assert LiteratureNoteDao().get_by_id(note["id"])["note_content"] == "timed"


def test_update_missing_note(mem_conn, lit_id):
    code, _n, msg = NoteManageService().update_note(9999, {"note_content": "x"})
    assert code == C.CODE_FILE_NOT_FOUND


def test_delete_note(mem_conn, lit_id):
    service = NoteManageService()
    code, note, _ = service.add_paragraph_note(lit_id, "1", "x")
    code, _n, msg = service.delete_note(note["id"])
    assert code == 0 and msg == "已删除"
    assert service.get_notes_by_lit(lit_id) == []


def test_clear_all_notes(mem_conn, lit_id):
    service = NoteManageService()
    service.add_paragraph_note(lit_id, "1", "a")
    service.add_paragraph_note(lit_id, "2", "b")
    service.add_global_note(lit_id, "g")
    code, count, msg = service.clear_all_notes(lit_id)
    assert code == 0 and count == 3 and msg == "已清空"
    assert service.get_notes_by_lit(lit_id) == []

    # 文献不存在时拒绝清空（业务层二次校验）
    code, count, _ = service.clear_all_notes(9999)
    assert code == C.CODE_FILE_NOT_FOUND


def test_clear_cancels_pending_timer(mem_conn, lit_id):
    """清空时挂起的防抖更新不得再复活已删记录。"""
    service = NoteManageService()
    code, note, _ = service.add_paragraph_note(lit_id, "1", "x")
    service.update_note(note["id"], {"note_content": "late"})
    service.clear_all_notes(lit_id)
    time.sleep(1.3)
    assert LiteratureNoteDao().get_by_id(note["id"]) is None


# ================= PDF 区域批注 =================

def test_pdf_anchor_codec_roundtrip():
    """锚点编码→解析往返一致，且坐标取整、页序 0 基。"""
    anchor = encode_pdf_anchor(2, (90.4, 170.7, 506.2, 590.9))
    assert anchor == "pdf:2:90,171,506,591"
    assert len(anchor) <= 50  # paragraph_pos VARCHAR(50)
    assert is_pdf_anchor(anchor)
    page_index, rect = parse_pdf_anchor(anchor)
    assert page_index == 2
    assert rect == (90.0, 171.0, 506.0, 591.0)


def test_parse_pdf_anchor_rejects_invalid():
    """普通段落序号、损坏锚点、退化矩形均判为非 PDF 锚点。"""
    assert parse_pdf_anchor("3") is None
    assert is_pdf_anchor("3") is False
    assert parse_pdf_anchor("pdf:0:10,10,10,80") is None   # x0==x1
    assert parse_pdf_anchor("pdf:a:1,2,3,4") is None       # 页码非数字
    assert parse_pdf_anchor("pdf:0:1,2,3") is None         # 坐标不足
    assert parse_pdf_anchor("") is None


def test_encode_pdf_anchor_rejects_bad_geometry():
    """负页码/反向矩形抛 ValueError。"""
    with pytest.raises(ValueError):
        encode_pdf_anchor(-1, (0, 0, 10, 10))
    with pytest.raises(ValueError):
        encode_pdf_anchor(0, (10, 10, 5, 5))
    with pytest.raises(ValueError):
        encode_pdf_anchor(0, "not-a-rect")


def test_add_pdf_annotation(mem_conn, lit_id):
    """PDF 区域批注落库为 paragraph 类型，锚点可解析回页面与矩形。"""
    service = NoteManageService()
    code, note, msg = service.add_pdf_annotation(
        lit_id, 1, (72.0, 144.0, 300.6, 260.0), "此处论证有问题"
    )
    assert code == 0, msg
    assert note["note_type"] == C.NOTE_TYPE_PARAGRAPH
    page_index, rect = parse_pdf_anchor(note["paragraph_pos"])
    assert page_index == 1
    assert rect == (72.0, 144.0, 301.0, 260.0)
    # 与普通段落批注共存且均可查询
    service.add_paragraph_note(lit_id, "0", "段首批注")
    notes = service.get_notes_by_lit(lit_id)
    anchors = [is_pdf_anchor(n["paragraph_pos"]) for n in notes]
    assert anchors == [True, False]  # 同为 paragraph 类型，锚点格式可区分


def test_add_pdf_annotation_validation(mem_conn, lit_id):
    """空内容、文献不存在、非法矩形均被拒绝。"""
    service = NoteManageService()
    code, _n, _m = service.add_pdf_annotation(lit_id, 0, (0, 0, 10, 10), "  ")
    assert code == C.CODE_FILE_INVALID
    code, _n, _m = service.add_pdf_annotation(9999, 0, (0, 0, 10, 10), "x")
    assert code == C.CODE_FILE_NOT_FOUND
    code, _n, msg = service.add_pdf_annotation(lit_id, 0, (5, 5, 1, 1), "x")
    assert code == C.CODE_FILE_INVALID and "非法" in msg
