"""统一二次确认弹窗：删除、覆盖、清空、恢复等高危操作必须使用本组件。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from ui.widgets.buttons import DangerButton, PrimaryButton

# 重复文件三选结果
CHOICE_OVERWRITE = "overwrite"
CHOICE_SKIP = "skip"
CHOICE_SKIP_ALL = "skip_all"


class ConfirmDialog(QDialog):
    """高危操作二次确认对话框。"""

    def __init__(self, parent=None, title: str = "确认操作", content: str = "",
                 confirm_text: str = "确认", cancel_text: str = "取消",
                 danger: bool = False):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setProperty("level", "aux")
        layout.addWidget(title_label)
        layout.addWidget(content_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton(cancel_text)
        btn_confirm = DangerButton(confirm_text) if danger else PrimaryButton(confirm_text)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_confirm)
        layout.addLayout(footer)

        btn_cancel.clicked.connect(self.reject)
        btn_confirm.clicked.connect(self.accept)

    @staticmethod
    def confirm(parent=None, title: str = "确认操作", content: str = "",
                confirm_text: str = "确认", cancel_text: str = "取消",
                danger: bool = False) -> bool:
        """模态弹出确认框。

        Args:
            parent: 父窗口。
            title: 弹窗标题。
            content: 风险说明文案。
            confirm_text: 确认按钮文字。
            cancel_text: 取消按钮文字。
            danger: True 时确认按钮使用危险红色样式。
        Returns:
            True 表示用户确认，False 表示取消。
        """
        dialog = ConfirmDialog(parent, title, content, confirm_text, cancel_text, danger)
        return dialog.exec_() == QDialog.Accepted


class ChoiceDialog(QDialog):
    """三选一对话框（重复文件：覆盖导入 / 跳过本次 / 全部跳过）。"""

    def __init__(self, parent=None, title: str = "文件重复", content: str = "",
                 primary_text: str = "覆盖导入", secondary_text: str = "跳过本次",
                 cancel_text: str = "全部跳过"):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(400)
        self.choice = CHOICE_SKIP_ALL

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setProperty("level", "aux")
        layout.addWidget(title_label)
        layout.addWidget(content_label)

        footer = QHBoxLayout()
        btn_cancel = QPushButton(cancel_text)
        btn_secondary = QPushButton(secondary_text)
        btn_primary = DangerButton(primary_text)
        footer.addStretch(1)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_secondary)
        footer.addWidget(btn_primary)
        layout.addLayout(footer)

        btn_primary.clicked.connect(lambda: self._choose(CHOICE_OVERWRITE))
        btn_secondary.clicked.connect(lambda: self._choose(CHOICE_SKIP))
        btn_cancel.clicked.connect(lambda: self._choose(CHOICE_SKIP_ALL))

    def _choose(self, choice: str) -> None:
        """内部统一的弹窗选择执行入口。"""
        self.choice = choice
        self.accept()

    @staticmethod
    def choose(parent=None, title: str = "文件重复", content: str = "",
               primary_text: str = "覆盖导入", secondary_text: str = "跳过本次",
               cancel_text: str = "全部跳过") -> str:
        """模态弹出三选框。

        Returns:
            overwrite / skip / skip_all。
        """
        dialog = ChoiceDialog(
            parent, title, content, primary_text, secondary_text, cancel_text
        )
        dialog.exec_()
        return dialog.choice


class InputDialog(QDialog):
    """统一单行文本输入弹窗（新增分类/标签/规则/模型等场景使用）。"""

    def __init__(self, parent=None, title: str = "请输入", label_text: str = "名称：",
                 default_text: str = "", placeholder: str = ""):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel(title))
        self.input = QLineEdit(default_text)
        self.input.setPlaceholderText(placeholder)
        label = QLabel(label_text)
        label.setProperty("level", "aux")
        layout.addWidget(label)
        layout.addWidget(self.input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("formError")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_confirm = PrimaryButton("确定")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_confirm)
        layout.addLayout(footer)

        btn_cancel.clicked.connect(self.reject)
        btn_confirm.clicked.connect(self._on_confirm)
        self.input.returnPressed.connect(self._on_confirm)

    def _on_confirm(self) -> None:
        """空内容不允许提交，行内红字提示。"""
        if not self.input.text().strip():
            self.error_label.setText("内容不能为空")
            self.error_label.show()
            return
        self.accept()

    @staticmethod
    def prompt(parent=None, title: str = "请输入", label_text: str = "名称：",
               default_text: str = "", placeholder: str = ""):
        """模态弹出输入框。

        Returns:
            用户输入的去空格字符串；取消返回 None。
        """
        dialog = InputDialog(parent, title, label_text, default_text, placeholder)
        if dialog.exec_() != QDialog.Accepted:
            return None
        return dialog.input.text().strip()
