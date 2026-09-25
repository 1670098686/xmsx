"""存储占用环形图组件：按各目录真实字节数绘制占比分段。

纯 QPainter 实现，不引入第三方图表库；颜色由调用方传入（常量统一定义），
中心镂空、中心文字与空态底色跟随当前主题。

几何约定（以控件中心为圆心，R 为环外半径，ring_width 为环厚）：
- 分段 drawPie 的外边缘 = R；空态 drawArc 画笔中线半径 = R - ring_width/2，
  两者环带外边缘严格一致且均不超出控件边界，杜绝宽笔被裁成直边。
- 镂空内圆半径 r = R - ring_width。
"""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from config.global_config import THEME_LIGHT, THEME_MAP
from ui.theme_manager import ThemeManager
from utils.common_helper import format_file_size

# 环形厚度占外圆半径的比例
_RING_WIDTH_RATIO = 0.30
# 环外边缘距控件边界的安全留白（像素），避免抗锯齿像素被裁切
_EDGE_MARGIN = 4.0
_CENTER_FONT_SIZE = 13
_CENTER_SUB_FONT_SIZE = 10


class DonutChart(QWidget):
    """存储占用环形图。

    用法::

        chart.set_segments([("文献", 1024, "#4A90D9"), ...])

    分段按字节数占总字节数的比例绘制；总占用为 0 时展示灰色空态环。
    """

    def __init__(self, diameter: int = 200, parent=None):
        """初始化环形图。

        Args:
            diameter: 控件直径（像素），正方形。
            parent: 父控件。
        """
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        # [(名称, 字节数, 十六进制颜色), ...]
        self._segments = []
        self._total = 0

    def set_segments(self, segments: list) -> None:
        """更新分段数据并触发重绘。

        Args:
            segments: [(名称 str, 字节数 int, 颜色 hex str), ...]，
                      字节数为 0 的分段仍会出现在图例，但不绘制扇区。
        """
        self._segments = list(segments)
        self._total = sum(int(item[1]) for item in self._segments)
        self.update()

    def _geometry(self) -> tuple:
        """计算统一几何参数。

        Returns:
            (R 外半径, ring_width 环厚, center 中心点)。
        """
        center_x = center_y = self._diameter / 2.0
        outer_radius = self._diameter / 2.0 - _EDGE_MARGIN
        ring_width = outer_radius * _RING_WIDTH_RATIO
        return outer_radius, ring_width, center_x, center_y

    def paintEvent(self, event) -> None:
        """绘制环形分段、镂空内圆与中心总占用文字。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        theme = THEME_MAP.get(ThemeManager().current_theme(), THEME_LIGHT)
        outer_radius, ring_width, cx, cy = self._geometry()

        if self._total <= 0:
            self._paint_empty(painter, outer_radius, ring_width, cx, cy, theme)
        else:
            pie_rect = self._square_rect(cx, cy, outer_radius)
            self._paint_segments(painter, pie_rect)

        inner_radius = outer_radius - ring_width
        self._paint_center(painter, inner_radius, cx, cy, theme)
        painter.end()

    @staticmethod
    def _square_rect(cx: float, cy: float, radius: float) -> QRectF:
        """以 (cx, cy) 为中心、radius 为半边长构造正方形矩形。"""
        return QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

    def _paint_empty(self, painter: QPainter, outer_radius: float,
                     ring_width: float, cx: float, cy: float,
                     theme: dict) -> None:
        """总占用为 0 时绘制灰色空态环（画笔中线位于环带中央）。"""
        mid_radius = outer_radius - ring_width / 2.0
        pen = QPen(QColor(theme["border"]))
        pen.setWidthF(ring_width)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(self._square_rect(cx, cy, mid_radius), 0, 360 * 16)

    def _paint_segments(self, painter: QPainter, outer: QRectF) -> None:
        """按字节占比顺时针绘制各分段扇区（从 12 点钟方向起）。"""
        painter.setPen(Qt.NoPen)
        start_angle = 90 * 16  # Qt 角度以 3 点为 0°、逆时针为正；90° 即 12 点
        for _name, size, color_hex in self._segments:
            if size <= 0:
                continue
            span = -int(round(size * 360.0 * 16 / self._total))
            painter.setBrush(QColor(color_hex))
            painter.drawPie(outer, start_angle, span)
            start_angle += span

    def _paint_center(self, painter: QPainter, inner_radius: float,
                      cx: float, cy: float, theme: dict) -> None:
        """绘制镂空内圆（卡片底色）与中心总占用文字。"""
        inner = self._square_rect(cx, cy, inner_radius)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme["bg_card"]))
        painter.drawEllipse(inner)

        if self._total > 0:
            total_text = format_file_size(self._total)
            sub_text = "总占用"
        else:
            total_text = "0 B"
            sub_text = "暂无占用"

        painter.setPen(QColor(theme["text_title"]))
        font = QFont()
        font.setPointSize(_CENTER_FONT_SIZE)
        font.setBold(True)
        painter.setFont(font)
        total_rect = QRectF(inner.x(), inner.y() + inner.height() * 0.26,
                            inner.width(), inner.height() * 0.32)
        painter.drawText(total_rect, Qt.AlignHCenter | Qt.AlignVCenter, total_text)

        painter.setPen(QColor(theme["text_aux"]))
        sub_font = QFont()
        sub_font.setPointSize(_CENTER_SUB_FONT_SIZE)
        painter.setFont(sub_font)
        sub_rect = QRectF(inner.x(), inner.y() + inner.height() * 0.58,
                          inner.width(), inner.height() * 0.24)
        painter.drawText(sub_rect, Qt.AlignHCenter | Qt.AlignVCenter, sub_text)
