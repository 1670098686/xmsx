"""PDF 批注查看器：在 PDF 原貌页面上框选区域即可批注。

- 复用 PdfViewer 的 PyMuPDF 页面渲染、翻页、缩放；
- 在页面渲染图上叠加透明绘制层：已批注矩形+序号角标、待保存选区（虚线
  「新」框，保存前持续保留）、框选进行中的橡皮筋；
- 鼠标拖拽框选发出 region_selected（页码+PDF 点矩形），点击已有矩形
  发出 annotation_selected（锚点），Esc/翻页放弃选区发出
  region_cancelled，由 UI 层统一处理编辑态与落库；
- 批注锚点编解码统一走 business.note_manage，本组件不拼锚点字符串。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QWidget

from business.note_manage import parse_pdf_anchor
from config import constants as C
from ui.widgets.pdf_viewer import PdfViewer

# 批注角标半径（像素，按页面绘制尺寸固定，不随缩放变化）
_BADGE_RADIUS = 10
_BADGE_FONT_PX = 11


class _AnnotateCanvas(QWidget):
    """PDF 页面画布：绘制页面位图与批注覆盖层，处理框选/点击。

    选区三态（单一事实来源）：
    - _rubber_rect：按住拖拽中（屏幕像素）；
    - _pending_page/_pending_rect_points：已框选待保存（PDF 点，缩放跟随）；
    - annotations（查看器持有）：已保存批注。
    """

    def __init__(self, viewer: "PdfAnnotateViewer", parent=None):
        """初始化画布，绑定所属批注查看器以读取缩放与批注数据。"""
        super().__init__(parent)
        self._viewer = viewer
        self._pixmap = QPixmap()
        self._press_pos = None
        self._dragging = False
        self._mouse_grabbed = False
        self._rubber_rect = None        # 屏幕像素矩形（框选进行中）
        self._pending_page = None       # 待保存选区所在页（0 基）
        self._pending_rect_points = None  # 待保存选区 (x0,y0,x1,y1) PDF 点
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)  # 接收 Esc 取消选区
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(QCursor(Qt.CrossCursor))

    def set_page_pixmap(self, pixmap: QPixmap) -> None:
        """设置当前页渲染位图并按位图尺寸调整画布。"""
        self._pixmap = pixmap or QPixmap()
        if self._pixmap.isNull():
            self.resize(0, 0)
        else:
            self.resize(self._pixmap.size())
        self.update()

    def setPixmap(self, pixmap: QPixmap) -> None:
        """兼容基类 close_document 的清空调用。"""
        self.set_page_pixmap(pixmap)

    # ---------------- 待保存选区 ----------------

    def set_pending(self, page_index: int, rect_points: tuple) -> None:
        """登记一条待保存选区并立即重绘。"""
        self._pending_page = int(page_index)
        self._pending_rect_points = tuple(rect_points)
        self.update()

    def discard_pending(self) -> None:
        """静默清除待保存选区（保存成功/切换文献/选中已有批注时调用）。"""
        self._pending_page = None
        self._pending_rect_points = None
        self.update()

    def has_pending_on_page(self, page_index: int) -> bool:
        """判断指定页是否存在待保存选区。"""
        return self._pending_page == page_index and self._pending_rect_points

    # ---------------- 坐标换算 ----------------

    def _page_annotations(self) -> list:
        """返回当前页的批注项，按 id 升序（角标序号稳定）。"""
        page_index = self._viewer.page_index
        result = []
        for item in self._viewer.annotations:
            parsed = parse_pdf_anchor(item["anchor"])
            if parsed and parsed[0] == page_index:
                result.append((parsed[1], item))
        result.sort(key=lambda pair: pair[1]["note_id"])
        return result

    def _points_to_rect(self, rect_points) -> tuple:
        """PDF 点矩形转屏幕像素矩形（x, y, w, h）。"""
        scale = self._viewer.scale_factor
        x0, y0, x1, y1 = rect_points
        return (x0 * scale, y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale)

    def _hit_annotation(self, pos) -> str:
        """命中测试：返回点击位置最上层批注的锚点，未命中返回空串。"""
        hit = ""
        for rect_points, item in self._page_annotations():
            x, y, w, h = self._points_to_rect(rect_points)
            if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                hit = item["anchor"]  # 后者覆盖前者（id 大的在上层）
        return hit

    # ---------------- 键盘 / 鼠标事件 ----------------

    def keyPressEvent(self, event) -> None:
        """Esc 放弃当前待保存选区。"""
        if event.key() == Qt.Key_Escape and self._pending_rect_points is not None:
            self._viewer.cancel_pending()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        """按下记录起点并抓取鼠标（防止拖出画布后丢失释放事件）。"""
        if event.button() != Qt.LeftButton:
            return
        self.setFocus(Qt.MouseFocusReason)
        self._press_pos = event.pos()
        self._dragging = False
        self._rubber_rect = None
        self.grabMouse()
        self._mouse_grabbed = True

    def mouseMoveEvent(self, event) -> None:
        """按住移动更新橡皮筋；悬停时切换手型/十字光标。"""
        if self._press_pos is not None:
            delta = event.pos() - self._press_pos
            if not self._dragging and (
                abs(delta.x()) > C.PDF_ANNOT_MIN_DRAG_PX
                or abs(delta.y()) > C.PDF_ANNOT_MIN_DRAG_PX
            ):
                self._dragging = True
            if self._dragging:
                self._rubber_rect = self._clamp_rect(self._press_pos, event.pos())
                self.update()
        else:
            self.setCursor(
                QCursor(Qt.PointingHandCursor) if self._hit_annotation(event.pos())
                else QCursor(Qt.CrossCursor)
            )

    def mouseReleaseEvent(self, event) -> None:
        """释放：拖拽则把选区登记为待保存并通知 UI；点击则选中已有批注。"""
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return
        if self._mouse_grabbed:
            self.releaseMouse()
            self._mouse_grabbed = False
        if self._dragging and self._rubber_rect is not None:
            x, y, w, h = self._rubber_rect
            if w >= C.PDF_ANNOT_MIN_DRAG_PX and h >= C.PDF_ANNOT_MIN_DRAG_PX:
                self._viewer.submit_rubber(
                    self._viewer.page_index,
                    self._screen_rect_to_points(x, y, w, h),
                )
        else:
            anchor = self._hit_annotation(event.pos())
            if anchor:
                # 点击已有批注即放弃未保存选区，转入编辑已有批注
                self._viewer.discard_pending(notify=False)
                self._viewer.annotation_selected.emit(anchor)
        self._press_pos = None
        self._dragging = False
        self._rubber_rect = None
        self.update()

    def _screen_rect_to_points(self, x: float, y: float,
                               w: float, h: float) -> tuple:
        """屏幕像素矩形转 PDF 点矩形并夹在页面范围内。"""
        scale = self._viewer.scale_factor
        page_rect = self._viewer.current_page_rect()
        x0 = max(0.0, min(x / scale, page_rect.width))
        y0 = max(0.0, min(y / scale, page_rect.height))
        x1 = max(0.0, min((x + w) / scale, page_rect.width))
        y1 = max(0.0, min((y + h) / scale, page_rect.height))
        return x0, y0, x1, y1

    @staticmethod
    def _clamp_rect(start, end) -> tuple:
        """两点归一化为 (x, y, w, h)，并夹在画布位图范围内。"""
        x0 = max(0, min(start.x(), end.x()))
        y0 = max(0, min(start.y(), end.y()))
        x1 = max(start.x(), end.x())
        y1 = max(start.y(), end.y())
        return x0, y0, x1 - x0, y1 - y0

    # ---------------- 绘制 ----------------

    def paintEvent(self, _event) -> None:
        """绘制页面位图、批注矩形/角标、待保存选区、框选橡皮筋。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.drawPixmap(0, 0, self._pixmap)
        if self._pixmap.isNull():
            return
        self._paint_annotations(painter)
        if self._pending_rect_points is not None and self.has_pending_on_page(
            self._viewer.page_index
        ):
            self._paint_pending(painter)
        if self._rubber_rect is not None:
            self._paint_rubber(painter)

    def _paint_annotations(self, painter: QPainter) -> None:
        """绘制当前页全部批注矩形与序号角标。"""
        base = self.palette().highlight().color()
        text_color = self.palette().highlightedText().color()
        selected_anchor = self._viewer.selected_anchor
        for index, (rect_points, item) in enumerate(self._page_annotations(), start=1):
            x, y, w, h = self._points_to_rect(rect_points)
            is_selected = item["anchor"] == selected_anchor
            fill = QColor(base)
            fill.setAlpha(90 if is_selected else 55)
            painter.fillRect(int(x), int(y), int(w), int(h), fill)
            # drawRect 会用当前画刷填充内部，必须显式清空画刷避免实心覆盖
            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor(base), 3 if is_selected else 2)
            painter.setPen(pen)
            painter.drawRect(int(x), int(y), int(w), int(h))
            self._paint_badge(painter, int(x), int(y), str(index),
                              base, text_color)

    def _paint_pending(self, painter: QPainter) -> None:
        """绘制待保存选区：高亮色虚线框 + 「新」角标（保存前持续可见）。"""
        x, y, w, h = self._points_to_rect(self._pending_rect_points)
        x, y, w, h = int(x), int(y), int(w), int(h)
        color = QColor(self.palette().highlight().color())
        fill = QColor(color)
        fill.setAlpha(40)
        painter.fillRect(x, y, w, h, fill)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 2, Qt.DashLine))
        painter.drawRect(x, y, w, h)
        self._paint_badge(painter, x, y, "新", color,
                          self.palette().highlightedText().color())

    def _paint_badge(self, painter: QPainter, x: int, y: int,
                     text: str, base: QColor, text_color: QColor) -> None:
        """在矩形左上角绘制圆形序号角标。"""
        cx = x + C.PDF_ANNOT_BADGE_MARGIN + _BADGE_RADIUS
        cy = y + C.PDF_ANNOT_BADGE_MARGIN + _BADGE_RADIUS
        painter.setBrush(base)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(cx - _BADGE_RADIUS, cy - _BADGE_RADIUS,
                            _BADGE_RADIUS * 2, _BADGE_RADIUS * 2)
        painter.setPen(QPen(text_color))
        font = painter.font()
        font.setPixelSize(_BADGE_FONT_PX)
        font.setBold(True)
        painter.setFont(font)
        badge_rect = painter.fontMetrics().boundingRect(text)
        painter.drawText(
            cx - badge_rect.width() // 2,
            cy + badge_rect.height() // 2 - 2,
            text,
        )

    def _paint_rubber(self, painter: QPainter) -> None:
        """绘制正在框选的橡皮筋（虚线高亮色）。"""
        x, y, w, h = (int(v) for v in self._rubber_rect)
        color = QColor(self.palette().highlight().color())
        fill = QColor(color)
        fill.setAlpha(28)
        painter.fillRect(x, y, w, h, fill)
        painter.setBrush(Qt.NoBrush)
        pen = QPen(color, 2, Qt.DashLine)
        painter.setPen(pen)
        painter.drawRect(x, y, w, h)


class PdfAnnotateViewer(PdfViewer):
    """可在 PDF 页面上框选批注的查看器。

    Signals:
        region_selected(int, tuple): 框选完成，(页码0基, (x0,y0,x1,y1) PDF 点)。
        region_cancelled(): 待保存选区被放弃（Esc/翻页）。
        annotation_selected(str): 点击已有批注，携带批注锚点。
    """

    region_selected = pyqtSignal(int, tuple)
    region_cancelled = pyqtSignal()
    annotation_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        """初始化批注查看器，页面承载控件使用可交互画布。"""
        self.annotations = []        # [{"anchor","note_id","preview"}]
        self.selected_anchor = ""
        super().__init__(parent)
        self.canvas = self.page_label  # 基类工厂创建的正是 _AnnotateCanvas
        tip = QLabel("在页面上按住鼠标拖拽框选区域即可添加批注；保存前选区会保留在页面上，"
                     "按 Esc 或翻页可取消；点击序号区域可编辑已有批注")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        self.layout().insertWidget(self.layout().count() - 1, tip)

    def _create_page_widget(self):
        """覆写基类工厂：返回支持框选与覆盖层绘制的画布。"""
        return _AnnotateCanvas(self)

    # ---------------- 对外接口 ----------------

    def set_annotations(self, items: list) -> None:
        """设置当前文献的全部批注项并重绘。

        Args:
            items: [{"anchor": str, "note_id": int, "preview": str}, ...]
        """
        self.annotations = list(items or [])
        self.canvas.update()

    def set_selected(self, anchor: str) -> None:
        """高亮指定锚点的批注（不翻页）。"""
        self.selected_anchor = anchor or ""
        self.canvas.update()

    def focus_annotation(self, anchor: str) -> bool:
        """翻到批注所在页并高亮。

        Returns:
            True 表示锚点有效且已翻页。
        """
        parsed = parse_pdf_anchor(anchor)
        if not parsed:
            return False
        self._goto_page(parsed[0])
        self.selected_anchor = anchor
        self.canvas.update()
        return True

    def discard_pending(self, notify: bool = False) -> None:
        """清除待保存选区；notify 为真时同步发出 region_cancelled。

        Args:
            notify: True 用于用户主动取消（Esc/翻页），联动重置右侧编辑态；
                    False 用于保存成功/选中已有批注等静默场景。
        """
        existed = self.canvas._pending_rect_points is not None
        self.canvas.discard_pending()
        if notify and existed:
            self.region_cancelled.emit()

    def cancel_pending(self) -> None:
        """用户主动放弃选区（Esc），清除并通知 UI 重置编辑态。"""
        self.discard_pending(notify=True)

    def current_page_rect(self):
        """返回当前页的 PyMuPDF Rect（供画布做坐标夹取）。"""
        return self._doc[self._page_index].rect if self._doc else None

    def submit_rubber(self, page_index: int, rect_points: tuple) -> None:
        """画布框选完成：登记待保存选区（视觉保留）并通知 UI。"""
        self.selected_anchor = ""
        self.canvas.set_pending(page_index, rect_points)
        self.region_selected.emit(page_index, rect_points)

    # ---------------- 基类行为覆写 ----------------

    def _render_current(self) -> None:
        """渲染当前页到位图画布（批注覆盖层按新缩放自动重绘）。"""
        if not self._doc:
            self.canvas.set_page_pixmap(None)
            return
        self.canvas.set_page_pixmap(self._current_pixmap())
        total = self._doc.page_count
        self.lbl_page.setText(f"{self._page_index + 1} / {total}")
        self.btn_prev.setEnabled(self._page_index > 0)
        self.btn_next.setEnabled(self._page_index < total - 1)

    def _goto_page(self, index: int) -> None:
        """翻页：离开待保存选区所在页时按用户主动取消处理。"""
        pending_page = self.canvas._pending_page
        changed = self._doc and index != self._page_index
        super()._goto_page(index)
        if changed:
            self.selected_anchor = ""
            if pending_page is not None and pending_page != self._page_index:
                self.discard_pending(notify=True)

    def close_document(self) -> None:
        """关闭文档时同步清空批注数据与画布。"""
        self.annotations = []
        self.selected_anchor = ""
        self.discard_pending()
        super().close_document()
