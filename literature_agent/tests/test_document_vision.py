# -*- coding: utf-8 -*-
"""document_vision（原件布局探测 + 页面渲染 + 本地 OCR 封装）单元测试。

用 PyMuPDF 在临时目录实时构造单栏/双栏/扫描/图表 PDF，不依赖任何外部样例。
本地 OCR 用例对环境自适应：未装 Tesseract 时验证友好报错，已装时验证返回文本。
"""
import sys

import pytest

# document_vision 导入时会把内置 .vendor 加入 sys.path，之后可直接 import pymupdf
from tool_layer import document_vision
from utils.exceptions import FileInvalidError, LiteratureAgentError

import pymupdf

_SINGLE_TEXT = (
    "模块化设计是软件工程中的重要方法，通过高内聚低耦合的原则组织功能单元，"
    "可以显著提升团队的迭代效率并降低长期维护成本。" * 3
)
_COLUMN_TEXT = (
    "本栏文字用于布局探测标定，双栏页面中缝附近不应出现词块覆盖，"
    "左右两栏各有足够数量的词语分布。" * 4
)


def _build_single_pdf(path, pages=1):
    """构造普通单栏文本 PDF。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 800),
                            _SINGLE_TEXT, fontsize=11)
    doc.save(path)
    doc.close()


def _build_double_pdf(path):
    """构造双栏文本 PDF（左右两个文本框）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 50, 285, 800),
                        _COLUMN_TEXT, fontsize=10)
    page.insert_textbox(pymupdf.Rect(310, 50, 555, 800),
                        _COLUMN_TEXT, fontsize=10)
    doc.save(path)
    doc.close()


def _build_scanned_pdf(path, pages=1):
    """构造无文本层的扫描件 PDF（整页位图）。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB,
                                pymupdf.IRect(0, 0, 500, 700), 1)
        page.insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pixmap)
    doc.save(path)
    doc.close()


def _build_figure_pdf(path):
    """构造上半部分文字 + 下半部分整幅大图的图文混合 PDF。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 200),
                        _SINGLE_TEXT, fontsize=11)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB,
                            pymupdf.IRect(0, 0, 400, 300), 2)
    page.insert_image(pymupdf.Rect(90, 240, 500, 620), pixmap=pixmap)
    doc.save(path)
    doc.close()


# ================= inspect_document_layout =================

def test_inspect_txt_returns_plain_layout(tmp_path):
    """TXT 不做视觉探测，直接返回普通文本布局。"""
    path = tmp_path / "a.txt"
    path.write_text("普通文本内容", encoding="utf-8")
    layout = document_vision.inspect_document_layout(str(path))
    assert layout["format"] == "txt"
    assert layout["is_scanned"] is False
    assert layout["has_two_columns"] is False
    assert layout["pages_with_images"] == []
    assert layout["page_samples"] == []


def test_inspect_single_column_pdf(tmp_path):
    path = str(tmp_path / "single.pdf")
    _build_single_pdf(path)
    layout = document_vision.inspect_document_layout(path)
    assert layout["format"] == "pdf"
    assert layout["page_count"] == 1
    assert layout["is_scanned"] is False
    assert layout["has_two_columns"] is False
    assert layout["pages_with_images"] == []
    sample = layout["page_samples"][0]
    assert sample["estimated_layout"] == "single"
    assert sample["has_text_layer"] is True


def test_inspect_double_column_pdf(tmp_path):
    path = str(tmp_path / "double.pdf")
    _build_double_pdf(path)
    layout = document_vision.inspect_document_layout(path)
    assert layout["is_scanned"] is False
    assert layout["has_two_columns"] is True
    # 中缝应在页面中央（0.5）附近
    assert 0.4 <= layout["column_gutter_estimate"] <= 0.6
    assert layout["page_samples"][0]["estimated_layout"] == "double"


def test_inspect_scanned_pdf(tmp_path):
    path = str(tmp_path / "scanned.pdf")
    _build_scanned_pdf(path, pages=2)
    layout = document_vision.inspect_document_layout(path, sample_pages=5)
    assert layout["page_count"] == 2
    assert layout["is_scanned"] is True
    assert layout["has_two_columns"] is False
    # 扫描件整篇走 OCR，不再单列图表页
    assert layout["pages_with_images"] == []
    assert all(s["estimated_layout"] == "image-only"
               and s["has_text_layer"] is False
               for s in layout["page_samples"])


def test_inspect_figure_pdf_lists_image_page(tmp_path):
    path = str(tmp_path / "figure.pdf")
    _build_figure_pdf(path)
    layout = document_vision.inspect_document_layout(path)
    assert layout["is_scanned"] is False
    assert layout["pages_with_images"] == [0]
    assert layout["page_samples"][0]["has_image"] is True


def test_inspect_missing_file_raises(tmp_path):
    with pytest.raises(FileInvalidError):
        document_vision.inspect_document_layout(str(tmp_path / "nope.pdf"))


def test_inspect_unsupported_suffix_raises(tmp_path):
    path = tmp_path / "a.xyz"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(FileInvalidError):
        document_vision.inspect_document_layout(str(path))


# ================= render_page_png =================

def test_render_page_png_returns_png_bytes(tmp_path):
    path = str(tmp_path / "p.pdf")
    _build_single_pdf(path)
    png = document_vision.render_page_png(path, 0, dpi=120)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_page_out_of_range_raises(tmp_path):
    path = str(tmp_path / "p.pdf")
    _build_single_pdf(path)
    with pytest.raises(FileInvalidError):
        document_vision.render_page_png(path, 5)


def test_render_page_rejects_txt(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("普通文本", encoding="utf-8")
    with pytest.raises(LiteratureAgentError):
        document_vision.render_page_png(str(path), 0)


# ================= 本地 OCR 降级 =================

def test_local_ocr_missing_raises_friendly_error(tmp_path):
    """无 Tesseract 环境（本机如此）必须返回可指导用户安装的明确错误。"""
    path = str(tmp_path / "p.pdf")
    _build_scanned_pdf(path)
    png = document_vision.render_page_png(path, 0)
    if document_vision.is_local_ocr_available():
        text, confidence = document_vision.ocr_image_local(png)
        assert isinstance(text, str)
        assert 0.0 <= confidence <= 1.0
    else:
        with pytest.raises(LiteratureAgentError) as exc_info:
            document_vision.ocr_image_local(png)
        assert "Tesseract" in exc_info.value.message
