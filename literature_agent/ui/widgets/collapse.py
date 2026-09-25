"""可折叠内容块：解析报告的各结构模块（研究背景/创新点/结论等）。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame, QLabel, QToolButton, QVBoxLayout, QWidget,
)


class CollapseBlock(QFrame):
    """标题栏可点击、内容区可展开/收起的卡片块。"""

    def __init__(self, title: str, icon: str = "", highlight: bool = False,
                 opened: bool = True, parent=None):
        """
        Args:
            title: 折叠块标题。
            icon: 标题前的 emoji 图标。
            highlight: True 时内容区使用主色高亮（创新点/结论）。
            opened: 初始是否展开。
        """
        super().__init__(parent)
        self.setObjectName("collapseBlock")
        self._highlight = highlight
        self._opened = opened

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._head = QToolButton()
        self._head.setObjectName("collapseHead")
        self._head.setCheckable(True)
        self._head.setChecked(opened)
        self._head.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._title_base = f"{icon} {title}".strip()
        self._head.setText(f"{self._title_base} {'▼' if opened else '▶'}")
        self._head.setCursor(Qt.PointingHandCursor)
        self._head.clicked.connect(self._toggle)

        self._body = QFrame()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(12, 10, 12, 10)

        self._content_label = QLabel("")
        self._content_label.setWordWrap(True)
        self._content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if highlight:
            self._content_label.setObjectName("highlightContent")
        body_layout.addWidget(self._content_label)

        root.addWidget(self._head)
        root.addWidget(self._body)
        self._body.setVisible(opened)

    def _toggle(self) -> None:
        """展开/收起内容区并更新箭头。"""
        self._opened = not self._opened
        self._body.setVisible(self._opened)
        self._head.setChecked(self._opened)
        arrow = "▼" if self._opened else "▶"
        self._head.setText(f"{self._title_base} {arrow}")

    def set_text(self, text: str) -> None:
        """设置折叠块文本内容。"""
        self._content_label.setText(text)

    def set_widget(self, widget: QWidget) -> None:
        """用自定义控件替换默认文本区（复杂报告内容场景）。"""
        layout = self._body.layout()
        layout.removeWidget(self._content_label)
        self._content_label.hide()
        layout.addWidget(widget)

    def is_opened(self) -> bool:
        """当前是否处于展开状态。"""
        return self._opened

    def expand(self) -> None:
        """强制展开。"""
        if not self._opened:
            self._toggle()

    def collapse(self) -> None:
        """强制收起。"""
        if self._opened:
            self._toggle()
