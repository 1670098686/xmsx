"""PDF 应用内查看器：使用 PyMuPDF 将 PDF 页面渲染为图像，支持翻页与缩放。

设计说明：
- 不提取 PDF 文本，直接按页面原貌渲染（含排版、图表、公式）；
- PyMuPDF 随项目内置在 .vendor 目录，本模块导入前自行补 sys.path；
- 渲染仅针对当前页，翻页惰性加载，避免大文档一次性占满内存。
"""
import os
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

# 补项目内置 vendor 目录（PyMuPDF 免全局安装）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, ".vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

try:
    import pymupdf  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时降级提示
    pymupdf = None
    _PYMUPDF_AVAILABLE = False

_BASE_DPI = 96
_ZOOM_STEP = 0.2
_MIN_ZOOM = 0.6
_MAX_ZOOM = 3.0

# ========== 打开文档句柄注册表 ==========
# Windows 上 PyMuPDF 打开 PDF 期间会持有文件句柄，未关闭时 os.remove 报
# WinError 32（另一个程序正在使用此文件）。删除文献原件前，业务层通过
# service_common 的删除前钩子回调 close_viewers_for_path，按路径关闭
# 所有正在预览该文件的查看器（注册表仅在 UI 层内部使用，不跨层）。
_OPEN_VIEWERS_BY_PATH = {}


def _normalize_path(file_path: str) -> str:
    """把路径规范化为注册表键（realpath + 大小写归一）。"""
    try:
        return os.path.normcase(os.path.realpath(file_path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(file_path))


def _register_viewer(viewer: "PdfViewer") -> None:
    """按当前打开路径登记查看器。"""
    key = getattr(viewer, "_open_path", "")
    if key:
        _OPEN_VIEWERS_BY_PATH.setdefault(key, set()).add(viewer)


def _unregister_viewer(viewer: "PdfViewer") -> None:
    """按当前打开路径注销查看器，集合清空后移除键。"""
    key = getattr(viewer, "_open_path", "")
    if not key:
        return
    viewers = _OPEN_VIEWERS_BY_PATH.get(key)
    if viewers is not None:
        viewers.discard(viewer)
        if not viewers:
            _OPEN_VIEWERS_BY_PATH.pop(key, None)


def close_viewers_for_path(file_path: str) -> int:
    """关闭所有正在预览指定 PDF 的查看器，释放底层文件句柄。

    供删除受管文献原件前的钩子调用；未被任何查看器打开时为空操作。

    Args:
        file_path: 待删除文件绝对路径。
    Returns:
        实际被关闭的查看器数量。
    """
    if not file_path:
        return 0
    key = _normalize_path(file_path)
    # 复制后遍历：close_document 会在遍历过程中修改注册表集合
    viewers = list(_OPEN_VIEWERS_BY_PATH.get(key, ()))
    for viewer in viewers:
        try:
            viewer.close_document()
        except Exception:  # 单个查看器关闭失败不阻断删除流程
            pass
    return len(viewers)


class PdfViewer(QWidget):
    """单 PDF 文档查看器：页面原貌渲染 + 翻页 + 缩放。"""

    def __init__(self, parent=None):
        """初始化查看器界面与空文档状态。"""
        super().__init__(parent)
        self._doc = None
        self._open_path = ""
        self._page_index = 0
        self._zoom = 1.0
        self._build_ui()
        if not _PYMUPDF_AVAILABLE:
            self._show_message("PDF 渲染组件（PyMuPDF）未安装，无法在应用内预览 PDF")

    def _build_ui(self) -> None:
        """构建顶部翻页/缩放工具条与滚动页面区。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 0, 4, 0)
        self.btn_prev = QPushButton("‹ 上一页")
        self.btn_next = QPushButton("下一页 ›")
        self.btn_zoom_out = QPushButton("－")
        self.btn_zoom_in = QPushButton("＋")
        self.btn_fit = QPushButton("适应宽度")
        self.lbl_page = QLabel("0 / 0")
        self.lbl_page.setMinimumWidth(70)
        self.lbl_page.setAlignment(Qt.AlignCenter)
        for btn in (self.btn_zoom_out, self.btn_zoom_in):
            btn.setFixedWidth(36)
        toolbar.addWidget(self.btn_prev)
        toolbar.addWidget(self.lbl_page)
        toolbar.addWidget(self.btn_next)
        toolbar.addSpacing(16)
        toolbar.addWidget(self.btn_zoom_out)
        toolbar.addWidget(self.btn_zoom_in)
        toolbar.addWidget(self.btn_fit)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.page_label = self._create_page_widget()
        self.scroll.setWidget(self.page_label)
        layout.addWidget(self.scroll, stretch=1)

        self.msg_label = QLabel("")
        self.msg_label.setAlignment(Qt.AlignCenter)
        self.msg_label.setWordWrap(True)
        self.msg_label.hide()
        layout.addWidget(self.msg_label)

        self.btn_prev.clicked.connect(lambda: self._goto_page(self._page_index - 1))
        self.btn_next.clicked.connect(lambda: self._goto_page(self._page_index + 1))
        self.btn_zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom - _ZOOM_STEP))
        self.btn_zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom + _ZOOM_STEP))
        self.btn_fit.clicked.connect(self._fit_width)

    def _create_page_widget(self) -> QLabel:
        """创建页面承载控件（子类可覆写为可交互画布）。"""
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        return label

    def load_pdf(self, file_path: str) -> tuple:
        """打开 PDF 并渲染第一页。

        Args:
            file_path: PDF 绝对路径。
        Returns:
            (code: int, msg: str)，0 成功。
        """
        if not _PYMUPDF_AVAILABLE:
            return 1, "PDF 渲染组件未安装"
        if self._doc is not None:
            self._doc.close()
            self._doc = None
            _unregister_viewer(self)
            self._open_path = ""
        try:
            self._doc = pymupdf.open(file_path)
        except Exception as exc:  # 文件损坏/加密等打开失败
            self._show_message(f"PDF 打开失败：{exc}")
            return 1, str(exc)
        if self._doc.needs_pass:
            self._doc.close()
            self._doc = None
            self._show_message("该 PDF 已加密，暂不支持在应用内预览")
            return 1, "加密 PDF"
        # 打开成功：登记句柄，供删除文献原件前按路径统一释放（WinError 32 兜底）
        self._open_path = _normalize_path(file_path)
        _register_viewer(self)
        self.msg_label.hide()
        self.scroll.show()
        self._page_index = 0
        self._zoom = 1.0
        self._render_current()
        return 0, ""

    def close_document(self) -> None:
        """关闭当前文档并释放资源（同时从句柄注册表注销）。"""
        if self._doc is not None:
            self._doc.close()
            self._doc = None
        _unregister_viewer(self)
        self._open_path = ""
        self.page_label.setPixmap(QPixmap())
        self.lbl_page.setText("0 / 0")

    def _goto_page(self, index: int) -> None:
        """跳转到指定页（自动夹在合法范围内）。"""
        if not self._doc:
            return
        index = max(0, min(index, self._doc.page_count - 1))
        if index == self._page_index:
            return
        self._page_index = index
        self._render_current()
        self.scroll.verticalScrollBar().setValue(0)

    def _set_zoom(self, zoom: float) -> None:
        """设置缩放倍率并重渲染当前页。"""
        if not self._doc:
            return
        self._zoom = round(max(_MIN_ZOOM, min(_MAX_ZOOM, zoom)), 2)
        self._render_current()

    def _fit_width(self) -> None:
        """按滚动视口宽度自适应当前页缩放。"""
        if not self._doc:
            return
        page = self._doc[self._page_index]
        viewport_w = max(200, self.scroll.viewport().width() - 24)
        self._zoom = round(viewport_w / page.rect.width * 72.0 / _BASE_DPI, 2)
        self._zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, self._zoom))
        self._render_current()

    def _render_current(self) -> None:
        """渲染当前页为 QPixmap 并刷新页码与按钮状态。"""
        if not self._doc:
            return
        pixmap = self._current_pixmap()
        if pixmap is None:
            return
        self.page_label.setPixmap(pixmap)
        self.page_label.resize(pixmap.size())
        total = self._doc.page_count
        self.lbl_page.setText(f"{self._page_index + 1} / {total}")
        self.btn_prev.setEnabled(self._page_index > 0)
        self.btn_next.setEnabled(self._page_index < total - 1)

    def _current_pixmap(self):
        """把当前页按当前缩放渲染为 QPixmap（供只读查看器与批注查看器复用）。

        Returns:
            QPixmap；无文档时返回 None。
        """
        if not self._doc:
            return None
        page = self._doc[self._page_index]
        dpi = int(round(_BASE_DPI * self._zoom))
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        image = QImage(
            pixmap.samples, pixmap.width, pixmap.height,
            pixmap.stride, QImage.Format_RGB888,
        )
        # copy() 使 QImage 脱离 PyMuPDF 托管的 samples 缓冲区
        return QPixmap.fromImage(image.copy())

    @property
    def page_index(self) -> int:
        """返回当前页码序号（从 0 开始）。"""
        return self._page_index

    @property
    def zoom(self) -> float:
        """返回当前缩放倍率。"""
        return self._zoom

    @property
    def scale_factor(self) -> float:
        """返回当前 PDF 点→屏幕像素的缩放系数。"""
        return _BASE_DPI * self._zoom / 72.0

    def _show_message(self, text: str) -> None:
        """显示降级/错误提示，隐藏页面区。"""
        self.scroll.hide()
        self.msg_label.setText(text)
        self.msg_label.show()
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)
