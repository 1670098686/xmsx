"""PDF 批注查看器：WPS 式文字标记（高亮/下划线/删除线）与区域框批注。

- 复用 PdfViewer 的 PyMuPDF 页面渲染、翻页、缩放；
- 文字标记基于 PyMuPDF ``page.get_text("words")`` 的词坐标：拖拽时把
  屏幕选区换算为 PDF 点，找出与选区相交的词序号区间，锚点以
  “pdfh:页:起词-止词”形式落库（见 business.note_manage），缩放/翻页
  后仍能按词重绘，不依赖位图；
- 区域框批注保留原矩形锚点“pdf:页:x0,y0,x1,y1”；
- 工具模式：box 区域框 / highlight 荧光笔 / underline 下划线 /
  strikeout 删除线；颜色由 UI 工具条设置；
- 鼠标拖拽发出 region_selected（框）或 markup_selected（文字标记），
  点击已有标记发出 annotation_selected（锚点），Esc/翻页放弃未保存
  框选区发出 region_cancelled，由 UI 层统一处理编辑态与落库。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QWidget

from business.note_manage import parse_pdf_note_anchor
from config import constants as C
from ui.widgets.pdf_viewer import PdfViewer

# 批注角标半径（像素，按页面绘制尺寸固定，不随缩放变化）
_BADGE_RADIUS = 10
_BADGE_FONT_PX = 11

# fitz get_text("words") 元组下标
_W_X0, _W_Y0, _W_X1, _W_Y1 = 0, 1, 2, 3
_W_TEXT, _W_BLOCK, _W_LINE, _W_NO = 4, 5, 6, 7


def _mark_default_color(kind: str) -> str:
    """按标记类型返回默认色值。"""
    if kind == C.MARK_KIND_HIGHLIGHT:
        return C.NOTE_DEFAULT_MARK_COLOR
    return C.NOTE_DEFAULT_LINE_COLOR


class _AnnotateCanvas(QWidget):
    """PDF 页面画布：绘制页面位图与批注覆盖层，处理框选/文字选取/点击。

    选区状态：
    - _rubber_rect：按住拖拽中的屏幕像素矩形；
    - _live_span：文字工具拖拽中实时命中的词区间 (w0, w1)；
    - _pending_page/_pending_rect_points：框批注已选待保存（PDF 点）；
    - annotations（查看器持有）：已保存批注。
    """

    def __init__(self, viewer: "PdfAnnotateViewer", parent=None):
        """初始化画布，绑定所属批注查看器以读取缩放、工具与批注数据。"""
        super().__init__(parent)
        self._viewer = viewer
        self._pixmap = QPixmap()
        self._press_pos = None
        self._dragging = False
        self._mouse_grabbed = False
        self._rubber_rect = None        # 屏幕像素矩形（拖拽进行中）
        self._live_span = None          # 文字工具拖拽中命中的词区间
        self._pending_page = None       # 待保存框选区所在页（0 基）
        self._pending_rect_points = None  # 待保存框选区 (x0,y0,x1,y1) PDF 点
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)  # 接收 Esc 取消选区
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

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

    # ---------------- 待保存框选区 ----------------

    def set_pending(self, page_index: int, rect_points: tuple) -> None:
        """登记一条待保存框选区并立即重绘。"""
        self._pending_page = int(page_index)
        self._pending_rect_points = tuple(rect_points)
        self.update()

    def discard_pending(self) -> None:
        """静默清除待保存框选区（保存成功/切换文献/选中已有批注时调用）。"""
        self._pending_page = None
        self._pending_rect_points = None
        self.update()

    def has_pending_on_page(self, page_index: int) -> bool:
        """判断指定页是否存在待保存框选区。"""
        return self._pending_page == page_index and self._pending_rect_points

    # ---------------- 坐标与词命中 ----------------

    def _page_items(self) -> list:
        """返回当前页全部批注项 [(info, item), ...]，按 note_id 升序。"""
        page_index = self._viewer.page_index
        result = []
        for item in self._viewer.annotations:
            info = parse_pdf_note_anchor(item["anchor"])
            if info and info["page"] == page_index:
                result.append((info, item))
        result.sort(key=lambda pair: pair[1]["note_id"])
        return result

    def _scale(self) -> float:
        """当前 PDF 点→屏幕像素缩放系数。"""
        return self._viewer.scale_factor

    def _points_to_rect(self, rect_points) -> tuple:
        """PDF 点矩形转屏幕像素矩形 (x, y, w, h)。"""
        scale = self._scale()
        x0, y0, x1, y1 = rect_points
        return (x0 * scale, y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale)

    def _screen_to_points(self, pos) -> tuple:
        """屏幕像素位置转夹在页面内的 PDF 点坐标。"""
        scale = self._scale()
        page_rect = self._viewer.current_page_rect()
        px = max(0.0, min(pos.x() / scale, page_rect.width))
        py = max(0.0, min(pos.y() / scale, page_rect.height))
        return px, py

    @staticmethod
    def _rects_overlap(a, b) -> bool:
        """两个 (x0,y0,x1,y1) 矩形是否相交（含边接触）。"""
        return not (a[_W_X1] < b[_W_X0] or b[_W_X1] < a[_W_X0]
                    or a[_W_Y1] < b[_W_Y0] or b[_W_Y1] < a[_W_Y0])

    def _span_in_rect(self, rect_points) -> tuple:
        """计算与 PDF 点选区相交的词序号区间。

        Returns:
            (w0, w1) 均含；无文字层或未命中任何词时返回 None。
        """
        words = self._viewer.page_words()
        if not words:
            return None
        hit = [
            index for index, word in enumerate(words)
            if self._rects_overlap(rect_points, word)
        ]
        if hit:
            return min(hit), max(hit)
        # 拖在词行间隙时退化为最近点吸附，避免“选不中”
        return None

    def _word_line_rects(self, span: tuple) -> list:
        """把词区间按 (block,line) 合并成行矩形列表（屏幕像素整数矩形）。"""
        w0, w1 = span
        words = self._viewer.page_words()
        scale = self._scale()
        lines = {}
        order = []
        for index in range(max(0, w0), min(w1 + 1, len(words))):
            word = words[index]
            key = (word[_W_BLOCK], word[_W_LINE])
            if key not in lines:
                lines[key] = [word[_W_X0], word[_W_Y0],
                              word[_W_X1], word[_W_Y1]]
                order.append(key)
            else:
                rect = lines[key]
                rect[0] = min(rect[0], word[_W_X0])
                rect[1] = min(rect[1], word[_W_Y0])
                rect[2] = max(rect[2], word[_W_X1])
                rect[3] = max(rect[3], word[_W_Y1])
        return [
            (int(rect[0] * scale), int(rect[1] * scale),
             int(rect[2] * scale), int(rect[3] * scale))
            for key in order for rect in [lines[key]]
        ]

    def _hit_annotation(self, pos) -> str:
        """命中测试：返回点击位置最上层批注的锚点，未命中返回空串。"""
        hit = ""
        px, py = self._screen_to_points(pos)
        words = self._viewer.page_words()
        for info, item in self._page_items():
            if info["kind"] == C.MARK_KIND_BOX:
                x0, y0, x1, y1 = info["rect"]
                if x0 <= px <= x1 and y0 <= py <= y1:
                    hit = item["anchor"]
                continue
            w0, w1 = info["span"]
            for index in range(max(0, w0), min(w1 + 1, len(words))):
                word = words[index]
                if (word[_W_X0] <= px <= word[_W_X1]
                        and word[_W_Y0] <= py <= word[_W_Y1]):
                    hit = item["anchor"]
                    break
        return hit

    # ---------------- 键盘 / 鼠标事件 ----------------

    def _cursor_for_tool(self):
        """按当前工具返回光标形状。"""
        if self._viewer.tool == C.MARK_KIND_BOX:
            return QCursor(Qt.CrossCursor)
        return QCursor(Qt.IBeamCursor)

    def keyPressEvent(self, event) -> None:
        """Esc 放弃当前待保存框选区。"""
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
        self._live_span = None
        self.grabMouse()
        self._mouse_grabbed = True

    def mouseMoveEvent(self, event) -> None:
        """按住移动更新橡皮筋（文字工具同步高亮候选词）；悬停切换光标。"""
        if self._press_pos is not None:
            delta = event.pos() - self._press_pos
            if not self._dragging and (
                abs(delta.x()) > C.PDF_ANNOT_MIN_DRAG_PX
                or abs(delta.y()) > C.PDF_ANNOT_MIN_DRAG_PX
            ):
                self._dragging = True
            if self._dragging:
                self._rubber_rect = self._clamp_rect(self._press_pos, event.pos())
                if self._viewer.tool != C.MARK_KIND_BOX:
                    x, y, w, h = self._rubber_rect
                    points = self._screen_rect_to_points(x, y, w, h)
                    self._live_span = self._span_in_rect(points)
                self.update()
        else:
            anchor = self._hit_annotation(event.pos())
            if anchor:
                self.setCursor(QCursor(Qt.PointingHandCursor))
            else:
                self.setCursor(self._cursor_for_tool())

    def mouseReleaseEvent(self, event) -> None:
        """释放：拖拽按工具提交选区；点击则选中已有批注。"""
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return
        if self._mouse_grabbed:
            self.releaseMouse()
            self._mouse_grabbed = False
        if self._dragging and self._rubber_rect is not None:
            # 以释放点为准重新归一化，避免最后一次 move 事件缺失导致少选词
            x, y, w, h = self._clamp_rect(self._press_pos, event.pos())
            self._rubber_rect = (x, y, w, h)
            if w >= C.PDF_ANNOT_MIN_DRAG_PX and h >= C.PDF_ANNOT_MIN_DRAG_PX:
                points = self._screen_rect_to_points(x, y, w, h)
                if self._viewer.tool == C.MARK_KIND_BOX:
                    self._viewer.submit_rubber(self._viewer.page_index, points)
                else:
                    span = self._span_in_rect(points)
                    if span is None:
                        self._viewer.markup_hint.emit(
                            "该页未检测到可选文字（可能是扫描图片），"
                            "请切换为区域框批注工具"
                        )
                    else:
                        self._viewer.submit_markup(
                            self._viewer.tool, self._viewer.page_index,
                            span[0], span[1],
                        )
        else:
            anchor = self._hit_annotation(event.pos())
            if anchor:
                # 点击已有批注即放弃未保存框选区，转入编辑已有批注
                self._viewer.discard_pending(notify=False)
                self._viewer.annotation_selected.emit(anchor)
        self._press_pos = None
        self._dragging = False
        self._rubber_rect = None
        self._live_span = None
        self.update()

    def _screen_rect_to_points(self, x: float, y: float,
                               w: float, h: float) -> tuple:
        """屏幕像素矩形转 PDF 点矩形并夹在页面范围内。"""
        scale = self._scale()
        page_rect = self._viewer.current_page_rect()
        x0 = max(0.0, min(x / scale, page_rect.width))
        y0 = max(0.0, min(y / scale, page_rect.height))
        x1 = max(0.0, min((x + w) / scale, page_rect.width))
        y1 = max(0.0, min((y + h) / scale, page_rect.height))
        return x0, y0, x1, y1

    def _clamp_rect(self, start, end) -> tuple:
        """两点归一化为 (x, y, w, h)，并夹在画布位图范围内。

        终点坐标取两点的较大值（此前误用 min，导致向右下拖拽时
        宽高恒为 0，框选/高亮/划线全部无法提交）。
        """
        bound_w = max(0, self._pixmap.width())
        bound_h = max(0, self._pixmap.height())
        x0 = max(0, min(start.x(), end.x()))
        y0 = max(0, min(start.y(), end.y()))
        x1 = min(bound_w, max(start.x(), end.x()))
        y1 = min(bound_h, max(start.y(), end.y()))
        return x0, y0, x1 - x0, y1 - y0

    # ---------------- 绘制 ----------------

    def paintEvent(self, _event) -> None:
        """绘制页面位图、批注覆盖层、待保存框选区与拖拽橡皮筋。"""
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
        """绘制当前页全部批注：框批注矩形+角标，文字标记按词重绘。"""
        base = self.palette().highlight().color()
        text_color = self.palette().highlightedText().color()
        selected_anchor = self._viewer.selected_anchor
        box_index = 0
        for info, item in self._page_items():
            is_selected = item["anchor"] == selected_anchor
            if info["kind"] == C.MARK_KIND_BOX:
                box_index += 1
                self._paint_box(painter, info["rect"], str(box_index),
                                base, text_color, is_selected)
            else:
                self._paint_text_mark(painter, info, item, is_selected)

    def _paint_box(self, painter, rect_points, badge_text,
                   base: QColor, text_color: QColor, is_selected: bool) -> None:
        """绘制区域框批注：半透明填充 + 描边 + 序号角标。"""
        x, y, w, h = self._points_to_rect(rect_points)
        fill = QColor(base)
        fill.setAlpha(90 if is_selected else 55)
        painter.fillRect(int(x), int(y), int(w), int(h), fill)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(base), 3 if is_selected else 2))
        painter.drawRect(int(x), int(y), int(w), int(h))
        self._paint_badge(painter, int(x), int(y), badge_text,
                          base, text_color)

    def _paint_text_mark(self, painter, info: dict, item: dict,
                         is_selected: bool) -> None:
        """按行矩形绘制高亮/下划线/删除线。"""
        color = QColor(item.get("color") or _mark_default_color(info["kind"]))
        line_rects = self._word_line_rects(info["span"])
        painter.setBrush(Qt.NoBrush)
        if info["kind"] == C.MARK_KIND_HIGHLIGHT:
            fill = QColor(color)
            fill.setAlpha(C.MARK_FILL_ALPHA)
            for x0, y0, x1, y1 in line_rects:
                painter.fillRect(x0, y0, x1 - x0, y1 - y0, fill)
            if is_selected:
                border = QColor(color)
                border.setAlpha(C.MARK_SELECTED_BORDER_ALPHA)
                painter.setPen(QPen(border, 1.5))
                for x0, y0, x1, y1 in line_rects:
                    painter.drawRect(x0, y0, x1 - x0, y1 - y0)
            return

        line_color = QColor(color)
        line_color.setAlpha(C.MARK_LINE_ALPHA)
        width = C.MARK_LINE_WIDTH_PX + (1 if is_selected else 0)
        painter.setPen(QPen(line_color, width))
        for x0, y0, x1, y1 in line_rects:
            if info["kind"] == C.MARK_KIND_UNDERLINE:
                line_y = y1 - max(1, width // 2)
            else:  # 删除线：行高中部
                line_y = (y0 + y1) // 2
            painter.drawLine(x0, line_y, x1, line_y)

    def _paint_pending(self, painter: QPainter) -> None:
        """绘制待保存框选区：高亮色虚线框 + 「新」角标。"""
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

    def _paint_badge(self, painter, x: int, y: int, text: str,
                     base: QColor, text_color: QColor) -> None:
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
        """绘制拖拽中的橡皮筋；文字工具同步高亮候选词。"""
        x, y, w, h = (int(v) for v in self._rubber_rect)
        if self._viewer.tool != C.MARK_KIND_BOX and self._live_span:
            color = QColor(self._viewer.mark_color)
            fill = QColor(color)
            fill.setAlpha(C.MARK_FILL_ALPHA)
            for lx0, ly0, lx1, ly1 in self._word_line_rects(self._live_span):
                painter.fillRect(lx0, ly0, lx1 - lx0, ly1 - ly0, fill)
        else:
            color = QColor(self.palette().highlight().color())
            fill = QColor(color)
            fill.setAlpha(28)
            painter.fillRect(x, y, w, h, fill)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 2, Qt.DashLine))
        painter.drawRect(x, y, w, h)


class PdfAnnotateViewer(PdfViewer):
    """PDF 页面批注查看器：文字标记 + 区域框批注。

    Signals:
        region_selected(int, tuple): 框选完成，(页码0基, (x0,y0,x1,y1) PDF 点)。
        markup_selected(str, int, int, int): 文字标记完成，
            (kind, 页码0基, 起词序号, 止词序号)。
        region_cancelled(): 待保存框选区被放弃（Esc/翻页）。
        annotation_selected(str): 点击已有批注，携带批注锚点。
        markup_hint(str): 文字选取失败等轻提示文案。
    """

    region_selected = pyqtSignal(int, tuple)
    markup_selected = pyqtSignal(str, int, int, int)
    region_cancelled = pyqtSignal()
    annotation_selected = pyqtSignal(str)
    markup_hint = pyqtSignal(str)

    def __init__(self, parent=None):
        """初始化批注查看器：默认区域框工具与默认标记色。"""
        self.annotations = []        # [{"anchor","note_id","preview","color"}]
        self.selected_anchor = ""
        self.tool = C.MARK_KIND_BOX
        self.mark_color = C.NOTE_DEFAULT_MARK_COLOR
        self._words_cache = {}       # 页码 -> fitz 词元组列表
        super().__init__(parent)
        self.canvas = self.page_label  # 基类工厂创建的正是 _AnnotateCanvas
        self.canvas.setCursor(QCursor(Qt.CrossCursor))
        tip = QLabel("工具条可切换：区域框批注 / 荧光高亮 / 下划线 / 删除线；"
                     "在文字上按住拖拽即可标记，点击已有标记可补写批注；"
                     "按 Esc 或翻页取消未保存的框选区")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        self.layout().insertWidget(self.layout().count() - 1, tip)

    def _create_page_widget(self):
        """覆写基类工厂：返回支持文字选取与覆盖层绘制的画布。"""
        return _AnnotateCanvas(self)

    # ---------------- 对外接口 ----------------

    def load_pdf(self, file_path: str) -> tuple:
        """打开 PDF 并清空词缓存与工具状态。"""
        self._words_cache = {}
        self.selected_anchor = ""
        return super().load_pdf(file_path)

    def close_document(self) -> None:
        """关闭文档时同步清空词缓存、批注数据与画布。"""
        self._words_cache = {}
        self.annotations = []
        self.selected_anchor = ""
        self.discard_pending()
        super().close_document()

    def page_words(self, page_index: int = None) -> list:
        """惰性获取指定页的词列表（PyMuPDF words 元组，按阅读顺序）。

        Args:
            page_index: 页码；None 取当前页。
        Returns:
            [(x0,y0,x1,y1,text,block,line,word_no), ...]；无文档返回空表。
        """
        if self._doc is None:
            return []
        if page_index is None:
            page_index = self._page_index
        if page_index in self._words_cache:
            return self._words_cache[page_index]
        try:
            words = list(self._doc[page_index].get_text("words"))
        except Exception:  # 个别异常页不应导致整个批注功能不可用
            words = []
        self._words_cache[page_index] = words
        return words

    def set_tool(self, kind: str) -> None:
        """切换批注工具并更新光标。

        Args:
            kind: box/highlight/underline/strikeout。
        """
        if kind not in (C.MARK_KIND_BOX, C.MARK_KIND_HIGHLIGHT,
                        C.MARK_KIND_UNDERLINE, C.MARK_KIND_STRIKEOUT):
            return
        self.tool = kind
        self.canvas.setCursor(
            QCursor(Qt.CrossCursor if kind == C.MARK_KIND_BOX
                    else Qt.IBeamCursor)
        )
        self.canvas.update()

    def set_mark_color(self, color_hex: str) -> None:
        """设置文字标记当前颜色（十六进制）。"""
        if color_hex:
            self.mark_color = color_hex

    def set_annotations(self, items: list) -> None:
        """设置当前文献的全部批注项并重绘。

        Args:
            items: [{"anchor", "note_id", "preview", "color"}, ...]
        """
        self.annotations = list(items or [])
        self.canvas.update()

    def set_selected(self, anchor: str) -> None:
        """高亮指定锚点的批注（不翻页）。"""
        self.selected_anchor = anchor or ""
        self.canvas.update()

    def focus_annotation(self, anchor: str) -> bool:
        """翻到批注所在页并高亮（框/文字标记通用）。

        Returns:
            True 表示锚点有效且已翻页。
        """
        info = parse_pdf_note_anchor(anchor)
        if not info:
            return False
        self._goto_page(info["page"])
        self.selected_anchor = anchor
        self.canvas.update()
        return True

    def discard_pending(self, notify: bool = False) -> None:
        """清除待保存框选区；notify 为真时同步发出 region_cancelled。

        Args:
            notify: True 用于用户主动取消（Esc/翻页），联动重置右侧编辑态；
                    False 用于保存成功/选中已有批注等静默场景。
        """
        existed = self.canvas._pending_rect_points is not None
        self.canvas.discard_pending()
        if notify and existed:
            self.region_cancelled.emit()

    def cancel_pending(self) -> None:
        """用户主动放弃框选区（Esc），清除并通知 UI 重置编辑态。"""
        self.discard_pending(notify=True)

    def current_page_rect(self):
        """返回当前页的 PyMuPDF Rect（供画布做坐标夹取）。"""
        return self._doc[self._page_index].rect if self._doc else None

    def submit_rubber(self, page_index: int, rect_points: tuple) -> None:
        """画布框选完成：登记待保存框选区（视觉保留）并通知 UI。"""
        self.selected_anchor = ""
        self.canvas.set_pending(page_index, rect_points)
        self.region_selected.emit(page_index, rect_points)

    def submit_markup(self, kind: str, page_index: int,
                      word_start: int, word_end: int) -> None:
        """文字标记选区完成：通知 UI 立即落库（纯标记无需编辑框）。"""
        self.selected_anchor = ""
        self.markup_selected.emit(kind, page_index, word_start, word_end)

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
        """翻页：离开待保存框选区所在页时按用户主动取消处理。"""
        pending_page = self.canvas._pending_page
        changed = self._doc and index != self._page_index
        super()._goto_page(index)
        if changed:
            self.selected_anchor = ""
            if pending_page is not None and pending_page != self._page_index:
                self.discard_pending(notify=True)
