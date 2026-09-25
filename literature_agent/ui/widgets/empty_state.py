"""空状态占位组件：图标 + 提示文案 + 可选引导按钮。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout

from ui.widgets.buttons import PrimaryButton


class EmptyState(QFrame):
    """列表/检索无数据时的统一空状态。"""

    action_clicked = pyqtSignal()

    def __init__(self, icon: str = "📭", text: str = "暂无数据",
                 action_text: str = None, parent=None):
        """初始化控件。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)

        self._icon = QLabel(icon)
        self._icon.setObjectName("emptyIcon")
        self._icon.setAlignment(Qt.AlignCenter)
        self._title = QLabel(text)
        self._title.setObjectName("emptyTitle")
        self._title.setAlignment(Qt.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(self._icon)
        layout.addWidget(self._title)

        self._btn = None
        if action_text:
            self._build_button(action_text)

        layout.addStretch(1)

    def _build_button(self, action_text: str) -> None:
        """创建引导按钮（插入到末尾弹性区之前）。"""
        self._btn = PrimaryButton(action_text)
        self.layout().insertWidget(3, self._btn, alignment=Qt.AlignCenter)
        self._btn.clicked.connect(self.action_clicked.emit)

    def set_text(self, text: str) -> None:
        """更新提示文案。"""
        self._title.setText(text)

    def set_icon(self, icon: str) -> None:
        """更新图标（emoji）。"""
        self._icon.setText(icon)

    def set_action_text(self, action_text: str) -> None:
        """更新或新建引导按钮文案。"""
        if self._btn is None:
            self._build_button(action_text)
        else:
            self._btn.setText(action_text)
            self._btn.setVisible(bool(action_text))
