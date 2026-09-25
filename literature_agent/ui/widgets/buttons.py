"""全局统一样式按钮。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QPushButton


class PrimaryButton(QPushButton):
    """主操作按钮（主色背景）。"""

    def __init__(self, text: str = "", parent=None):
        """初始化控件。"""
        super().__init__(text, parent)
        self.setObjectName("btnPrimary")
        self.setCursor(Qt.PointingHandCursor)


class DangerButton(QPushButton):
    """高危操作按钮（删除/清空/覆盖等，红色描边）。"""

    def __init__(self, text: str = "", parent=None):
        """初始化控件。"""
        super().__init__(text, parent)
        self.setObjectName("btnDanger")
        self.setCursor(Qt.PointingHandCursor)


class GhostButton(QPushButton):
    """次级幽灵按钮（使用默认 QPushButton 样式，语义化别名）。"""

    def __init__(self, text: str = "", parent=None):
        """初始化控件。"""
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
