"""文献拖拽上传区：支持拖拽文件与点击选择两种交互。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout

from ui.widgets.buttons import PrimaryButton


class UploadBox(QFrame):
    """虚线圆角上传框。

    Signals:
        files_dropped(list[str]): 拖拽放下的本地文件绝对路径列表。
        click_requested(): 点击上传框/按钮（由页面打开 QFileDialog）。
    """

    files_dropped = pyqtSignal(list)
    click_requested = pyqtSignal()

    def __init__(self, hint: str = "拖拽PDF/TXT/Word文献至此导入",
                 button_text: str = "点击选择文件", parent=None):
        """初始化控件。"""
        super().__init__(parent)
        self.setObjectName("uploadBox")
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self.setProperty("dragActive", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(10)

        icon = QLabel("📂")
        icon.setObjectName("uploadIcon")
        icon.setAlignment(Qt.AlignCenter)
        tip = QLabel(hint)
        tip.setProperty("level", "aux")
        tip.setAlignment(Qt.AlignCenter)

        self._btn = PrimaryButton(button_text)
        layout.addStretch(1)
        layout.addWidget(icon)
        layout.addWidget(tip)
        layout.addWidget(self._btn, alignment=Qt.AlignCenter)
        layout.addStretch(1)

        self._btn.clicked.connect(self.click_requested.emit)

    # ---- 点击空白区域同样触发选择 ----
    def mousePressEvent(self, event):
        """点击上传区时打开文件选择对话框。"""
        if event.button() == Qt.LeftButton:
            self.click_requested.emit()
        super().mousePressEvent(event)

    # ---- 拖拽事件 ----
    def dragEnterEvent(self, event):
        """拖拽进入时校验是否包含文件。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drag_active(True)

    def dragMoveEvent(self, event):
        """拖拽移动时保持接受放置状态。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        """拖拽离开时取消高亮。"""
        self._set_drag_active(False)

    def dropEvent(self, event):
        """放下文件时发出文件接收信号。"""
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile()
        ]
        self._set_drag_active(False)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)

    def _set_drag_active(self, active: bool) -> None:
        """切换拖拽悬停高亮样式。"""
        self.setProperty("dragActive", active)
        # 重新应用样式使属性选择器生效
        self.style().unpolish(self)
        self.style().polish(self)
