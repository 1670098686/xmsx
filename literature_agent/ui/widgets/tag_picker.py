"""标签选择对话框：复选框列表 + 全选，供单篇改标签、批量打标使用。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ui.widgets.buttons import PrimaryButton


class TagPickerDialog(QDialog):
    """标签多选对话框。"""

    def __init__(self, tags: list, selected_ids: list = None,
                 title: str = "选择标签", parent=None):
        """
        Args:
            tags: [{"id":1,"tag_name":...,"tag_color":...}, ...]。
            selected_ids: 初始勾选的标签 id 列表。
            title: 对话框标题。
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumSize(320, 360)
        self._checkboxes = {}
        self._selected_ids = list(selected_ids or [])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)

        select_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_none = QPushButton("清空")
        btn_all.setCursor(Qt.PointingHandCursor)
        btn_none.setCursor(Qt.PointingHandCursor)
        select_row.addWidget(btn_all)
        select_row.addWidget(btn_none)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)

        for tag in tags:
            checkbox = QCheckBox(tag.get("tag_name", ""))
            checkbox.setProperty("tag_id", int(tag.get("id", 0)))
            if int(tag.get("id", 0)) in self._selected_ids:
                checkbox.setChecked(True)
            self._checkboxes[checkbox.property("tag_id")] = checkbox
            content_layout.addWidget(checkbox)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, stretch=1)

        if not tags:
            empty = QLabel("暂无可用标签，请先在管理中心创建")
            empty.setProperty("level", "aux")
            empty.setAlignment(Qt.AlignCenter)
            content_layout.addWidget(empty)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_ok = PrimaryButton("确定")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_ok)
        layout.addLayout(footer)

        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self._confirm)

    def _set_all(self, checked: bool) -> None:
        """全选或全不选标签列表。"""
        for checkbox in self._checkboxes.values():
            checkbox.setChecked(checked)

    def _confirm(self) -> None:
        """确认选择并回传选中的标签 id。"""
        self._selected_ids = [
            tag_id for tag_id, checkbox in self._checkboxes.items()
            if checkbox.isChecked()
        ]
        self.accept()

    @staticmethod
    def pick(tags: list, selected_ids: list = None,
             title: str = "选择标签", parent=None) -> tuple:
        """模态弹出标签选择框。

        Returns:
            (confirmed: bool, selected_ids: list)。
        """
        dialog = TagPickerDialog(tags, selected_ids, title, parent)
        confirmed = dialog.exec_() == QDialog.Accepted
        return confirmed, dialog._selected_ids
