"""小圆角标签：文献卡片上的分类/标签色块，支持自定义颜色。"""
from PyQt5.QtWidgets import QLabel

from config import constants as C


class TagLabel(QLabel):
    """标签色块（字号 11px，圆角 4px）。"""

    def __init__(self, text: str, color: str = None, parent=None):
        """
        Args:
            text: 标签名称。
            color: 可选自定义底色（十六进制）；不传使用主题主色。
        """
        super().__init__(text, parent)
        self.setObjectName("tagLabel")
        if color:
            self.set_color(color)

    def set_color(self, color: str) -> None:
        """设置标签底色。

        Args:
            color: 十六进制颜色值。
        """
        # 用户数据颜色属于动态样式，直接作用于本实例而非写入全局 QSS
        self.setStyleSheet(
            f"background-color: {color}; border-radius: 4px; padding: 2px 6px; "
            f"font-size: 11px; color: {C.TAG_TEXT_COLOR};"
        )

    def set_text(self, text: str) -> None:
        """更新标签文字。"""
        self.setText(text)
