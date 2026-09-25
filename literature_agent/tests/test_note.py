"""阶段2 B2-9：笔记批注业务单元测试（含 1 秒防抖）。"""
import time

import pytest

from business.literature_import import LiteratureImportService
from business.note_manage import (
    NoteManageService, encode_pdf_anchor, encode_pdf_mark_anchor,
    encode_text_anchor, is_pdf_anchor, parse_pdf_anchor,
    parse_pdf_mark_anchor, parse_pdf_note_anchor, parse_text_anchor,
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


# ================= PDF 文字标记（高亮/下划线/删除线） =================

@pytest.mark.parametrize("kind,prefix", [
    (C.MARK_KIND_HIGHLIGHT, C.NOTE_PDF_HIGHLIGHT_PREFIX),
    (C.MARK_KIND_UNDERLINE, C.NOTE_PDF_UNDERLINE_PREFIX),
    (C.MARK_KIND_STRIKEOUT, C.NOTE_PDF_STRIKEOUT_PREFIX),
])
def test_pdf_mark_anchor_roundtrip(kind, prefix):
    """词区间锚点编码→解析往返一致，长度满足 VARCHAR(50)。"""
    anchor = encode_pdf_mark_anchor(kind, 2, 12, 38)
    assert anchor == f"{prefix}2:12-38"
    assert len(anchor) <= 50
    assert is_pdf_anchor(anchor)
    assert parse_pdf_anchor(anchor) is None       # 不属于区域框锚点
    assert parse_pdf_mark_anchor(anchor) == (kind, 2, 12, 38)


def test_encode_pdf_mark_anchor_rejects_invalid():
    """非法类型/负页码/反向词区间均抛 ValueError。"""
    with pytest.raises(ValueError):
        encode_pdf_mark_anchor(C.MARK_KIND_BOX, 0, 0, 1)
    with pytest.raises(ValueError):
        encode_pdf_mark_anchor(C.MARK_KIND_HIGHLIGHT, -1, 0, 1)
    with pytest.raises(ValueError):
        encode_pdf_mark_anchor(C.MARK_KIND_HIGHLIGHT, 0, 9, 3)
    assert parse_pdf_mark_anchor("pdfh:0:x-3") is None
    assert parse_pdf_mark_anchor("pdfh:0:3-1") is None
    assert parse_pdf_mark_anchor("3") is None


def test_parse_pdf_note_anchor_dispatch():
    """统一解析：区域框返回 rect，文字标记返回 span，互不串台。"""
    boxed = parse_pdf_note_anchor(encode_pdf_anchor(1, (1.0, 2.0, 30.0, 40.0)))
    assert boxed["kind"] == C.MARK_KIND_BOX
    assert boxed["page"] == 1
    assert boxed["rect"] == (1.0, 2.0, 30.0, 40.0)
    assert boxed["span"] is None

    marked = parse_pdf_note_anchor(
        encode_pdf_mark_anchor(C.MARK_KIND_UNDERLINE, 4, 7, 10))
    assert marked["kind"] == C.MARK_KIND_UNDERLINE
    assert marked["page"] == 4
    assert marked["span"] == (7, 10)
    assert marked["rect"] is None

    assert parse_pdf_note_anchor("3") is None
    assert parse_pdf_note_anchor("txh:0-10") is None


def test_add_pdf_markup_empty_content_and_default_color(mem_conn, lit_id):
    """纯高亮允许无批注文字，未指定颜色时按类型补默认色。"""
    service = NoteManageService()
    code, note, msg = service.add_pdf_markup(
        lit_id, C.MARK_KIND_HIGHLIGHT, 0, 3, 8)
    assert code == 0, msg
    assert note["note_content"] == ""
    assert note["highlight_style"] == C.NOTE_DEFAULT_MARK_COLOR
    assert parse_pdf_mark_anchor(note["paragraph_pos"]) == (
        C.MARK_KIND_HIGHLIGHT, 0, 3, 8)


def test_add_pdf_markup_with_content_and_custom_color(mem_conn, lit_id):
    """文字标记可携带批注文字与自定义颜色。"""
    service = NoteManageService()
    code, note, msg = service.add_pdf_markup(
        lit_id, C.MARK_KIND_UNDERLINE, 2, 0, 2, "关键结论", "#9AD0FF")
    assert code == 0, msg
    assert note["note_content"] == "关键结论"
    assert note["highlight_style"] == "#9AD0FF"
    # 批注文字后续可通过 update_note 补写
    code, _u, msg = service.update_note(note["id"], {"note_content": "修订"})
    assert code == 0, msg
    service.flush_note(note["id"])
    assert LiteratureNoteDao().get_by_id(note["id"])["note_content"] == "修订"


def test_add_pdf_markup_validation(mem_conn, lit_id):
    """非法类型/文献不存在/反向词区间被拒绝。"""
    service = NoteManageService()
    code, _n, _m = service.add_pdf_markup(
        lit_id, C.MARK_KIND_COMMENT, 0, 0, 1)   # PDF 标记不支持 comment
    assert code == C.CODE_FILE_INVALID
    code, _n, _m = service.add_pdf_markup(
        9999, C.MARK_KIND_HIGHLIGHT, 0, 0, 1)
    assert code == C.CODE_FILE_NOT_FOUND
    code, _n, msg = service.add_pdf_markup(
        lit_id, C.MARK_KIND_STRIKEOUT, 0, 6, 2)
    assert code == C.CODE_FILE_INVALID and "非法" in msg


# ================= TXT 字符区间标记 =================

@pytest.mark.parametrize("kind,prefix", [
    (C.MARK_KIND_COMMENT, C.NOTE_TEXT_COMMENT_PREFIX),
    (C.MARK_KIND_HIGHLIGHT, C.NOTE_TEXT_HIGHLIGHT_PREFIX),
    (C.MARK_KIND_UNDERLINE, C.NOTE_TEXT_UNDERLINE_PREFIX),
    (C.MARK_KIND_STRIKEOUT, C.NOTE_TEXT_STRIKEOUT_PREFIX),
])
def test_text_anchor_roundtrip(kind, prefix):
    """字符区间锚点编码→解析往返一致（半开区间）。"""
    anchor = encode_text_anchor(kind, 120, 186)
    assert anchor == f"{prefix}120-186"
    assert len(anchor) <= 50
    assert parse_text_anchor(anchor) == (kind, 120, 186)
    assert is_pdf_anchor(anchor) is False


def test_encode_text_anchor_rejects_invalid():
    """非法类型/负偏移/空区间/闭端不大于开端均拒绝。"""
    with pytest.raises(ValueError):
        encode_text_anchor(C.MARK_KIND_BOX, 0, 1)
    with pytest.raises(ValueError):
        encode_text_anchor(C.MARK_KIND_HIGHLIGHT, -1, 1)
    with pytest.raises(ValueError):
        encode_text_anchor(C.MARK_KIND_HIGHLIGHT, 5, 5)
    with pytest.raises(ValueError):
        encode_text_anchor(C.MARK_KIND_HIGHLIGHT, 9, 3)
    assert parse_text_anchor("txh:a-3") is None
    assert parse_text_anchor("pdfh:0:1-3") is None
    assert parse_text_anchor("") is None


def test_add_text_mark_comment_requires_content(mem_conn, lit_id):
    """选中批注必须有文字，纯划线则允许空内容。"""
    service = NoteManageService()
    code, _n, msg = service.add_text_mark(
        lit_id, C.MARK_KIND_COMMENT, 0, 5, "   ")
    assert code == C.CODE_FILE_INVALID and "不能为空" in msg

    code, note, msg = service.add_text_mark(
        lit_id, C.MARK_KIND_COMMENT, 0, 5, "此处存疑")
    assert code == 0, msg
    assert parse_text_anchor(note["paragraph_pos"]) == (
        C.MARK_KIND_COMMENT, 0, 5)

    for kind in (C.MARK_KIND_HIGHLIGHT, C.MARK_KIND_UNDERLINE,
                 C.MARK_KIND_STRIKEOUT):
        code, note, msg = service.add_text_mark(lit_id, kind, 10, 20)
        assert code == 0, msg
        assert note["note_content"] == ""


def test_add_text_mark_colors(mem_conn, lit_id):
    """自定义颜色落库；空颜色按类型补默认（高亮黄、划线红）。"""
    service = NoteManageService()
    code, highlight, _ = service.add_text_mark(
        lit_id, C.MARK_KIND_HIGHLIGHT, 0, 4)
    assert highlight["highlight_style"] == C.NOTE_DEFAULT_MARK_COLOR
    code, underline, _ = service.add_text_mark(
        lit_id, C.MARK_KIND_UNDERLINE, 5, 9, highlight_style="#FFB3C7")
    assert underline["highlight_style"] == "#FFB3C7"
    code, strike, _ = service.add_text_mark(
        lit_id, C.MARK_KIND_STRIKEOUT, 10, 14)
    assert strike["highlight_style"] == C.NOTE_DEFAULT_LINE_COLOR


def test_add_text_mark_validation(mem_conn, lit_id):
    """非法类型/文献不存在/非法区间被拒绝。"""
    service = NoteManageService()
    code, _n, _m = service.add_text_mark(
        lit_id, C.MARK_KIND_BOX, 0, 1, "x")
    assert code == C.CODE_FILE_INVALID
    code, _n, _m = service.add_text_mark(
        9999, C.MARK_KIND_HIGHLIGHT, 0, 1)
    assert code == C.CODE_FILE_NOT_FOUND
    code, _n, msg = service.add_text_mark(
        lit_id, C.MARK_KIND_HIGHLIGHT, 8, 2)
    assert code == C.CODE_FILE_INVALID and "非法" in msg


def test_mixed_anchors_coexist_in_order(mem_conn, lit_id):
    """区域框/词标记/字符区间/旧段落锚点共存，按插入顺序返回且可分类。"""
    service = NoteManageService()
    service.add_pdf_annotation(lit_id, 0, (0.0, 0.0, 10.0, 10.0), "框")
    service.add_paragraph_note(lit_id, "0", "段")
    service.add_pdf_markup(lit_id, C.MARK_KIND_HIGHLIGHT, 1, 0, 2)
    service.add_text_mark(lit_id, C.MARK_KIND_COMMENT, 0, 3, "选区批注")

    notes = service.get_notes_by_lit(lit_id)
    assert len(notes) == 4
    kinds = []
    for note in notes:
        info = parse_pdf_note_anchor(note["paragraph_pos"])
        if info is not None:
            kinds.append(info["kind"])
        elif parse_text_anchor(note["paragraph_pos"]):
            kinds.append(C.MARK_KIND_COMMENT)
        else:
            kinds.append("paragraph")
    assert kinds == [
        C.MARK_KIND_BOX, "paragraph", C.MARK_KIND_HIGHLIGHT,
        C.MARK_KIND_COMMENT,
    ]
