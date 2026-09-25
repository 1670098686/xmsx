"""左侧导航折叠菜单（管理中心）。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QButtonGroup, QFrame, QToolButton, QVBoxLayout


class FoldMenu(QFrame):
    """可折叠的二级导航菜单。

    Signals:
        item_clicked(str): 子项 key（页面标识）。
    """

    item_clicked = pyqtSignal(str)

    def __init__(self, title: str, items: list = None, parent=None):
        """
        Args:
            title: 菜单标题，如 "⚙️ 管理中心"。
            items: [(key, 文本), ...] 子菜单项。
        """
        super().__init__(parent)
        self.setObjectName("foldMenu")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = QToolButton()
        self._header.setObjectName("foldHeader")
        self._header.setText(f"{title} ▶")
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setCheckable(True)
        self._header.clicked.connect(self._toggle_body)

        self._body = QFrame()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body.hide()

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}

        root.addWidget(self._header)
        root.addWidget(self._body)

        if items:
            for key, text in items:
                self.add_item(key, text)

    def _toggle_body(self) -> None:
        """展开/收起子菜单。"""
        opened = not self._body.isVisible()
        self._body.setVisible(opened)
        arrow = "▼" if opened else "▶"
        # 保留标题 emoji 部分：去掉末尾箭头再拼
        title_text = self._header.text().rstrip("▼▶ ").strip()
        self._header.setText(f"{title_text} {arrow}")

    def add_item(self, key: str, text: str) -> None:
        """追加一个子菜单项。"""
        btn = QToolButton()
        btn.setObjectName("foldSubItem")
        btn.setText(text)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.item_clicked.emit(key))
        self._group.addButton(btn)
        self._buttons[key] = btn
        self._body_layout.addWidget(btn)

    def set_current(self, key: str) -> None:
        """高亮指定子项（并自动展开）。"""
        btn = self._buttons.get(key)
        if btn is not None:
            if not self._body.isVisible():
                self._toggle_body()
            btn.setChecked(True)

    def clear_current(self) -> None:
        """取消全部子项高亮。

        注意：QButtonGroup 在排他模式下会拦截 setChecked(False)，
        导致当前选中项无法取消（侧边栏出现上下两个选中态），
        这里临时关闭排他再取消，最后恢复排他。
        """
        checked = self._group.checkedButton()
        if checked is not None:
            self._group.setExclusive(False)
            checked.setChecked(False)
            self._group.setExclusive(True)
