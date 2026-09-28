"""文献卡片：检索结果网格中的单篇文献展示卡片。"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from config import constants as C
from ui.widgets.tag_label import TagLabel


class DocCard(QFrame):
    """文献信息卡片。

    Signals:
        clicked(int): 卡片整体点击，传出文献 id。
        preview_requested(int): 预览按钮。
        parse_requested(int): “解析/查看报告”按钮，传出文献 id。
        tag_edit_requested(int): 修改标签按钮。
        delete_requested(int): 删除按钮。
    """

    clicked = pyqtSignal(int)
    preview_requested = pyqtSignal(int)
    parse_requested = pyqtSignal(int)
    tag_edit_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)

    def __init__(self, lit_info: dict, tags: list = None, parent=None):
        """
        Args:
            lit_info: literature_info 行字典（含 is_parsed 解析状态）。
            tags: 可选标签列表 [{"tag_name": ..., "tag_color": ...}, ...]。
        """
        super().__init__(parent)
        self.setObjectName("docCard")
        self.setMinimumWidth(260)
        self.lit_id = int(lit_info.get("id", 0))
        self.is_parsed = int(lit_info.get("is_parsed") or 0) == C.PARSE_DONE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题行：标题占满左侧，已解析文献右侧显示绿色“已解析”角标
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel(lit_info.get("literature_title", "未命名文献"))
        title.setObjectName("cardTitle")
        title.setWordWrap(True)
        title_row.addWidget(title, stretch=1)
        if self.is_parsed:
            parsed_badge = QLabel("已解析")
            parsed_badge.setObjectName("parsedLabel")
            parsed_badge.setToolTip("数据库中已有该文献的结构化解析报告")
            title_row.addWidget(parsed_badge, 0, Qt.AlignTop)
        layout.addLayout(title_row)

        meta = QLabel(
            f"作者：{lit_info.get('literature_author', '未知')}"
            f"｜{lit_info.get('publish_time', '')}"
        )
        meta.setProperty("level", "aux")
        layout.addWidget(meta)

        category = lit_info.get("category")
        if category or tags:
            tag_row = QHBoxLayout()
            tag_row.setSpacing(4)
            if category:
                category_label = QLabel(category.get("tag_name", ""))
                category_label.setObjectName("categoryLabel")
                category_label.setToolTip(
                    f"所属分类：{category.get('tag_name', '')}"
                )
                tag_row.addWidget(category_label)
            for tag in tags[:6]:
                tag_row.addWidget(
                    TagLabel(tag.get("tag_name", ""), tag.get("tag_color"))
                )
            tag_row.addStretch(1)
            tag_container = QWidget()
            tag_container.setObjectName("tagContainer")
            tag_container.setLayout(tag_row)
            layout.addWidget(tag_container)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self._btn_preview = QPushButton("预览")
        # 已解析文献按钮直接引导查看数据库中的报告；未解析的引导发起解析
        self._btn_parse = QPushButton("查看报告" if self.is_parsed else "解析")
        self._btn_parse.setToolTip(
            "查看数据库中已有的结构化解析报告" if self.is_parsed
            else "对该文献发起智能解析"
        )
        self._btn_tag = QPushButton("标签")
        self._btn_delete = QPushButton("删除")
        for btn in (self._btn_preview, self._btn_parse, self._btn_tag, self._btn_delete):
            btn.setCursor(Qt.PointingHandCursor)
            action_row.addWidget(btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._btn_preview.clicked.connect(
            lambda: self.preview_requested.emit(self.lit_id)
        )
        self._btn_parse.clicked.connect(
            lambda: self.parse_requested.emit(self.lit_id)
        )
        self._btn_tag.clicked.connect(
            lambda: self.tag_edit_requested.emit(self.lit_id)
        )
        self._btn_delete.clicked.connect(
            lambda: self.delete_requested.emit(self.lit_id)
        )

    def mousePressEvent(self, event):
        """卡片按下时记录点击并发出单击信号。"""
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.lit_id)
        super().mousePressEvent(event)
