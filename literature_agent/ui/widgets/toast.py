"""全局轻量 Toast 提示：顶部居中、自动消失，不遮挡操作界面。"""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QWidget,
)

from config.global_config import THEME_LIGHT, THEME_MAP
from ui.theme_manager import ThemeManager

# 提示级别 → 主题变量名（颜色全部取自 config 主题字典，禁止硬编码）
_LEVEL_THEME_KEYS = {
    "info": "info",
    "success": "success",
    "warn": "warn",
    "error": "error",
}


def _level_color(level: str) -> str:
    """按当前主题解析提示级别对应的语义色。"""
    theme = THEME_MAP.get(ThemeManager().current_theme(), THEME_LIGHT)
    return theme.get(_LEVEL_THEME_KEYS.get(level, "info"), THEME_LIGHT["info"])


class ToastOverlay(QFrame):
    """单条 Toast 浮层（内部使用，外部请调用 ToastOverlay.show）。"""

    def __init__(self, host: QWidget, message: str, level: str, duration: int):
        """初始化控件。"""
        super().__init__(host)
        self.setObjectName("toastItem")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._host = host
        self._duration = duration

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        bar = QLabel()
        bar.setFixedSize(4, 16)
        bar.setStyleSheet(
            f"background-color: {_level_color(level)}; border-radius: 2px;"
        )
        text = QLabel(message)
        text.setObjectName("toastText")
        layout.addWidget(bar)
        layout.addWidget(text)

        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)
        self._timer.start(duration)

    def _reposition(self) -> None:
        """定位到宿主窗口顶部居中，多条 Toast 依次向下堆叠。"""
        host_rect = self._host.rect()
        x = (host_rect.width() - self.width()) // 2
        existing = [
            c for c in self._host.children()
            if isinstance(c, ToastOverlay) and c is not self and c.isVisible()
        ]
        y = 50 + len(existing) * (self.height() + 6)
        self.move(max(x, 8), y)


def show_toast(message: str, level: str = "info", duration: int = 2200,
               parent: QWidget = None) -> None:
    """弹出一条 Toast 提示。

    Args:
        message: 提示文本。
        level: info / success / warn / error。
        duration: 显示时长（毫秒）。
        parent: 宿主控件；不传则使用当前活动窗口。
    """
    host = parent or QApplication.activeWindow()
    if host is None:
        return
    ToastOverlay(host, message, level, duration)
