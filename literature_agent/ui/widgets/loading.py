"""耗时操作遮罩：半透明遮罩 + 进度条，覆盖在目标控件之上。"""
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget,
)


class LoadingMask(QFrame):
    """覆盖父控件的加载遮罩，随父控件尺寸自动调整。"""

    def __init__(self, parent: QWidget):
        """初始化控件。"""
        super().__init__(parent)
        self.setObjectName("loadingMask")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()

        outer = QVBoxLayout(self)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)

        self._box = QFrame()
        self._box.setObjectName("loadingBox")
        self._box.setMinimumWidth(260)
        box_layout = QVBoxLayout(self._box)
        box_layout.setContentsMargins(20, 16, 20, 16)
        box_layout.setSpacing(10)

        self._title = QLabel("处理中，请稍候...")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        box_layout.addWidget(self._title)
        box_layout.addWidget(self._progress)

        row.addWidget(self._box)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        parent.installEventFilter(self)

    def eventFilter(self, obj, event):
        """父控件尺寸变化时同步遮罩大小。"""
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.resize(self.parent().size())
        return super().eventFilter(obj, event)

    def show_progress(self, parent: QWidget = None, title: str = "处理中...") -> None:
        """显示不确定进度遮罩。

        Args:
            parent: 兼容参数（遮罩在构造时已绑定父控件）。
            title: 遮罩标题。
        """
        self._title.setText(title)
        self._progress.setRange(0, 0)  # 不确定进度（滚动动画）
        self.resize(self.parent().size())
        self.show()
        self.raise_()

    def show_percent(self, value: int, title: str = "处理中...") -> None:
        """显示确定百分比进度。

        Args:
            value: 0~100。
            title: 遮罩标题。
        """
        self._title.setText(title)
        if self._progress.maximum() == 0:
            self._progress.setRange(0, 100)
        self._progress.setValue(int(value))
        self.show()
        self.raise_()

    def hide_mask(self) -> None:
        """隐藏遮罩并复位进度。"""
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self.hide()
