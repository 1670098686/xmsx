"""管理中心 - 标签管理页：左侧分类树（拖拽排序/调级）、右侧标签卡片（改色/删除/预设）。"""
from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QColorDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from business.tag_category import TagCategoryService
from config import constants as C
from config.global_config import THEME_LIGHT, THEME_MAP
from ui.pages.setting_pages import build_section_card
from ui.theme_manager import ThemeManager
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.dialogs import ConfirmDialog, InputDialog
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast

_DEFAULT_TAG_COLOR = C.DEFAULT_LABEL_COLOR
_DEFAULT_CATEGORY_COLOR = C.DEFAULT_CATEGORY_COLOR
_CARD_COLUMNS = 3


class SettingTagPage(QWidget):
    """标签管理子页面。"""

    def __init__(self, parent=None):
        """初始化标签管理子页面。"""
        super().__init__(parent)
        self._service = TagCategoryService()
        self._labels_by_id = {}
        self._card_row_count = 0
        self._build_ui()

    def _build_ui(self) -> None:
        """构建标签分类管理界面。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("标签管理")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_category_panel())
        splitter.addWidget(self._build_label_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

    # ================= 左侧分类树 =================

    def _build_category_panel(self) -> QWidget:
        """构造左侧分类面板。"""
        card, layout = build_section_card("文献分类")

        btn_row = QHBoxLayout()
        self.btn_add_root = GhostButton("新增根分类")
        self.btn_add_child = GhostButton("新增子分类")
        self.btn_del_category = DangerButton("删除分类")
        btn_row.addWidget(self.btn_add_root)
        btn_row.addWidget(self.btn_add_child)
        layout.addLayout(btn_row)
        layout.addWidget(self.btn_del_category)

        self.tree = CategoryTreeWidget()
        self.tree.setHeaderLabels(["分类名称（关联文献数）"])
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        # 文件夹式内部拖拽：同级拖动排序，跨级拖动调整父子层级
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDragDropMode(QTreeWidget.InternalMove)
        self.tree.setDefaultDropAction(Qt.MoveAction)
        self.tree.setDropIndicatorShown(True)
        self.tree.category_dropped.connect(self._on_category_dropped)
        layout.addWidget(self.tree, stretch=1)

        tip = QLabel("拖拽分类可调整排序与层级（子分类可拖为根分类，根分类也可拖入其它分类）；"
                     "含子分类或仍关联文献的分类不可删除")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.btn_add_root.clicked.connect(lambda: self._add_category(0))
        self.btn_add_child.clicked.connect(self._add_child_category)
        self.btn_del_category.clicked.connect(self._delete_category)
        return card

    def _selected_category_id(self):
        """返回树中当前选中分类 id，无选中返回 None。"""
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _add_category(self, parent_id: int) -> None:
        """新增分类：输入名称后选择颜色（取消选色使用默认色）。"""
        name = InputDialog.prompt(self, "新增分类", "分类名称：",
                                  placeholder="例如：研究方向")
        if not name:
            return
        color = self._pick_color(_DEFAULT_CATEGORY_COLOR, "选择分类颜色")
        code, _cat_id, msg = self._service.create_category(name, parent_id, color)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _add_child_category(self) -> None:
        """在选中分类下新增子分类。"""
        parent_id = self._selected_category_id()
        if parent_id is None:
            show_toast("请先在左侧选择父分类", level="warn")
            return
        self._add_category(parent_id)

    def _delete_category(self) -> None:
        """删除选中分类（业务层二次校验子分类/关联文献）。"""
        cat_id = self._selected_category_id()
        if cat_id is None:
            show_toast("请先选择要删除的分类", level="warn")
            return
        if not ConfirmDialog.confirm(
            self, "删除分类", "确定删除选中分类？该分类下不能有子分类或关联文献。",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._service.delete_category(cat_id)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _load_tree(self, nodes: list, parent_item=None) -> None:
        """递归填充分类树。

        分类颜色作为文本前景色会导致浅色系（如默认浅蓝 #E8F4F8）在白底上完全看不清，
        这里用 QIcon 画一个小圆点色块作为 item 的 icon（颜色就是分类色），
        分类名称保持主题深色前景，保证无论什么分类色都清晰可读。
        """
        for node in nodes:
            color_hex = node.get("color") or _DEFAULT_CATEGORY_COLOR
            text = f"{node['name']}（{node['lit_count']} 篇）"
            item = (
                QTreeWidgetItem(parent_item, [text])
                if parent_item is not None
                else QTreeWidgetItem(self.tree, [text])
            )
            item.setData(0, Qt.UserRole, node["id"])
            item.setIcon(0, _color_dot_icon(color_hex))
            # 文本前景色始终跟随主题（深色/浅色），保证可读
            theme = THEME_MAP.get(ThemeManager().current_theme(), THEME_LIGHT)
            item.setForeground(0, QColor(theme["text_title"]))
            self._load_tree(node.get("children", []), item)
            item.setExpanded(True)

    # ================= 右侧标签区 =================

    def _build_label_panel(self) -> QWidget:
        """构造右侧标签面板。"""
        card, layout = build_section_card("个性化标签")

        toolbar = QHBoxLayout()
        self.btn_add_tag = PrimaryButton("新增标签")
        self.btn_preset = GhostButton("套用预设模板")
        toolbar.addWidget(self.btn_add_tag)
        toolbar.addWidget(self.btn_preset)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        self.scroll.setWidget(self.grid_host)
        layout.addWidget(self.scroll, stretch=1)

        self.empty_state = EmptyState(icon="🏷️", text="还没有标签，点击「新增标签」或"
                                      "「套用预设模板」")
        layout.addWidget(self.empty_state)

        self.btn_add_tag.clicked.connect(self._add_tag)
        self.btn_preset.clicked.connect(self._apply_preset)
        return card

    def _add_tag(self) -> None:
        """新增标签：名称 + 颜色。"""
        name = InputDialog.prompt(self, "新增标签", "标签名称：",
                                  placeholder="例如：待精读")
        if not name:
            return
        color = self._pick_color(_DEFAULT_TAG_COLOR, "选择标签颜色")
        code, _tag_id, msg = self._service.create_tag(name, color)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _change_tag_color(self, tag_id: int) -> None:
        """打开取色器修改标签颜色。"""
        tag = self._labels_by_id.get(tag_id)
        initial = QColor(tag.get("tag_color") or _DEFAULT_TAG_COLOR) if tag \
            else QColor(_DEFAULT_TAG_COLOR)
        color = QColorDialog.getColor(initial, self, "选择标签颜色")
        if not color.isValid():
            return
        code, _data, msg = self._service.update_tag(
            tag_id, {"tag_color": color.name().upper()}
        )
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _delete_tag(self, tag_id: int, tag_name: str) -> None:
        """删除标签（级联解除全部文献绑定，需二次确认）。"""
        if not ConfirmDialog.confirm(
            self, "删除标签",
            f"确定删除标签「{tag_name}」？该标签与文献的全部绑定将一并解除。",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._service.delete_tag(tag_id)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _apply_preset(self) -> None:
        """套用预设标签模板。"""
        code, inserted, msg = self._service.apply_preset_labels()
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0 and inserted:
            self.refresh()

    # ================= 分类树拖拽 =================

    def _on_category_dropped(self, moved_id: int) -> None:
        """拖拽放置后收集新布局交业务层校验落库；失败时按数据库数据回滚视图。"""
        code, _data, msg = self._service.save_category_layout(
            moved_id, self._collect_tree_layout()
        )
        show_toast(msg, level="success" if code == 0 else "error")
        self.refresh(select_id=moved_id if code == 0 else None)

    def _collect_tree_layout(self) -> list:
        """按树当前显示顺序收集全量节点布局。

        Returns:
            [{"id": 分类id, "parent_id": 父分类id（0为根）, "sort_order": 同级序号}, ...]
        """
        layout = []

        def walk(item: QTreeWidgetItem, parent_id: int) -> None:
            """递归收集某节点下各子节点的父级与排序。"""
            for order in range(item.childCount()):
                child = item.child(order)
                cat_id = child.data(0, Qt.UserRole)
                layout.append({
                    "id": cat_id, "parent_id": parent_id,
                    "sort_order": order + 1,
                })
                walk(child, cat_id)

        walk(self.tree.invisibleRootItem(), 0)
        return layout

    # ================= 渲染 =================

    def refresh(self, select_id=None) -> None:
        """重新加载分类树与标签卡片，可选选中指定分类。

        Args:
            select_id: 刷新后需要重新选中并展开的分类 id。
        """
        self.tree.clear()
        self._load_tree(self._service.get_categories_tree())
        for index in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(index)
        if select_id is not None:
            self._select_category(select_id)

        labels = self._service.list_labels_with_count()
        self._labels_by_id = {tag["id"]: tag for tag in labels}
        self._rebuild_grid(labels)
        self.empty_state.setVisible(not labels)
        self.scroll.setVisible(bool(labels))

    def _select_category(self, cat_id: int) -> None:
        """在树中选中指定分类并展开其所在路径。"""
        target = self._find_category_item(
            self.tree.invisibleRootItem(), int(cat_id)
        )
        if target is None:
            return
        parent = target.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        target.setExpanded(True)
        self.tree.setCurrentItem(target)

    def _find_category_item(self, parent_item: QTreeWidgetItem,
                            cat_id: int):
        """递归查找指定 id 的树节点，找不到返回 None。"""
        for index in range(parent_item.childCount()):
            child = parent_item.child(index)
            if child.data(0, Qt.UserRole) == cat_id:
                return child
            found = self._find_category_item(child, cat_id)
            if found is not None:
                return found
        return None

    def _rebuild_grid(self, labels: list) -> None:
        """重建右侧标签卡片网格。"""
        while self.grid.count():
            old = self.grid.takeAt(0)
            if old.widget():
                old.widget().deleteLater()
        for index, tag in enumerate(labels):
            row, col = divmod(index, _CARD_COLUMNS)
            self.grid.addWidget(self._build_tag_card(tag), row, col)
        # 先复位旧的伸缩行，再把最后一张卡片之后的行设为弹簧，保持顶对齐
        for row in range(self._card_row_count + 1):
            self.grid.setRowStretch(row, 0)
        self._card_row_count = (len(labels) + _CARD_COLUMNS - 1) // _CARD_COLUMNS
        self.grid.setRowStretch(self._card_row_count, 1)
        for col in range(_CARD_COLUMNS):
            self.grid.setColumnStretch(col, 1)

    def _build_tag_card(self, tag: dict) -> QFrame:
        """构造单个标签卡片（色块 + 名称 + 绑定数 + 删除）。"""
        card = QFrame()
        card.setObjectName("settingCard")
        card.setMinimumWidth(180)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        color_btn = QPushButton()
        color = tag.get("tag_color") or _DEFAULT_TAG_COLOR
        # 标签色为用户数据，实例级动态样式
        color_btn.setStyleSheet(
            f"background-color: {color}; border: 1px solid {color};"
            "border-radius: 6px; min-width: 14px; max-width: 14px;"
            "min-height: 14px; max-height: 14px;"
        )
        color_btn.setToolTip("点击修改颜色")
        color_btn.setCursor(Qt.PointingHandCursor)
        name_label = QLabel(tag["tag_name"])
        name_label.setObjectName("cardTitle")
        btn_delete = QPushButton("✕")
        btn_delete.setObjectName("tagCardDel")
        btn_delete.setToolTip("删除标签")
        btn_delete.setCursor(Qt.PointingHandCursor)
        top_row.addWidget(color_btn)
        top_row.addWidget(name_label, stretch=1)
        top_row.addWidget(btn_delete)
        layout.addLayout(top_row)

        count_label = QLabel(f"关联文献 {tag.get('lit_count', 0)} 篇")
        count_label.setProperty("level", "aux")
        layout.addWidget(count_label)

        tag_id = tag["id"]
        color_btn.clicked.connect(lambda _checked, tid=tag_id: self._change_tag_color(tid))
        btn_delete.clicked.connect(
            lambda _checked, tid=tag_id, n=tag["tag_name"]: self._delete_tag(tid, n)
        )
        return card

    @staticmethod
    def _pick_color(default_color: str, title: str) -> str:
        """打开取色器，用户取消时返回默认色。"""
        color = QColorDialog.getColor(QColor(default_color), None, title)
        if not color.isValid():
            return default_color
        return color.name().upper()


class CategoryTreeWidget(QTreeWidget):
    """支持内部拖拽的分类树。

    Signals:
        category_dropped(int): 拖拽放置完成，传出被拖动分类的 id；
            最终是否合法由业务层校验，界面以数据库结果重建树（非法拖放自动回滚）。
    """

    category_dropped = pyqtSignal(int)

    def dropEvent(self, event) -> None:
        """记录被拖动节点后执行默认放置，放置被接受时通知上层持久化。"""
        current = self.currentItem()
        moved_id = current.data(0, Qt.UserRole) if current else None
        super().dropEvent(event)
        if event.isAccepted() and moved_id is not None:
            self.category_dropped.emit(moved_id)


def _color_dot_icon(color_hex: str) -> QIcon:
    """绘制指定颜色的小圆点 QIcon，用于分类树每行前面的彩色装饰。

    Args:
        color_hex: 分类颜色，如 "#E8F4F8"。
    Returns:
        14x14 的圆形色块 QIcon。
    """
    size = 14
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color_hex))
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pix)
