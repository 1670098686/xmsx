"""管理中心 - 存储路径配置页：存储占用环形图、四类存储目录、历史文件迁移。"""
import os
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from business.system_service import SystemService
from config import constants as C
from ui.pages.setting_pages import build_scroll_content, build_section_card
from ui.widgets.buttons import GhostButton, PrimaryButton
from ui.widgets.donut_chart import DonutChart
from ui.widgets.loading import LoadingMask
from ui.widgets.toast import show_toast
from ui.widgets.worker import PathMigrateWorker
from utils.common_helper import format_file_size

# path_key → (显示名, 选择对话框提示)
_PATH_ITEMS = [
    ("literature", "文献存储目录", "选择文献存储目录"),
    ("report", "解析报告存储目录", "选择解析报告存储目录"),
    ("note", "笔记存储目录", "选择笔记存储目录"),
    ("backup", "备份存储目录", "选择备份存储目录"),
]
# 环形图末段：数据库（解析报告/笔记/标签/配置等结构化数据）
_DB_LEGEND = ("database", "数据库")


class SettingStoragePage(QWidget):
    """存储路径配置子页面。"""

    def __init__(self, parent=None):
        """初始化存储路径配置子页面。"""
        super().__init__(parent)
        self._service = SystemService()
        self._worker = None
        self._path_edits = {}
        self._size_labels = {}
        self._legend_size_labels = {}
        self._legend_percent_labels = {}
        self._build_ui()
        self._mask = LoadingMask(self)

    def _build_ui(self) -> None:
        """构建存储路径配置界面。"""
        content_layout = build_scroll_content(self)

        # ---- 存储占用环形图 ----
        chart_card, chart_layout = build_section_card("存储占用分布")
        # 卡片预留足够高度，环形上下留白，杜绝任何方向被裁切
        chart_card.setMinimumHeight(C.STORAGE_CHART_CARD_MIN_HEIGHT)
        chart_row = QHBoxLayout()
        chart_row.setContentsMargins(8, 8, 8, 8)
        chart_row.setSpacing(36)

        # 环形图列：上下弹性留白，图始终完整居中
        chart_col = QVBoxLayout()
        chart_col.addStretch(1)
        self.chart = DonutChart(C.STORAGE_CHART_SIZE)
        chart_col.addWidget(self.chart, 0, Qt.AlignCenter)
        chart_col.addStretch(1)
        chart_row.addLayout(chart_col, 0)

        legend_box = QVBoxLayout()
        legend_box.setSpacing(0)
        legend_box.addStretch(1)
        for index, (path_key, title, _caption) in enumerate(_PATH_ITEMS):
            legend_box.addWidget(self._build_legend_row(index, path_key, title))
        legend_box.addWidget(
            self._build_legend_row(len(_PATH_ITEMS), _DB_LEGEND[0], _DB_LEGEND[1])
        )
        legend_box.addStretch(1)
        chart_row.addLayout(legend_box, stretch=1)

        chart_layout.addLayout(chart_row, stretch=1)
        content_layout.addWidget(chart_card)

        # ---- 四类路径 ----
        for path_key, title, dialog_caption in _PATH_ITEMS:
            card, layout = build_section_card(title)
            edit = QLineEdit()
            edit.setReadOnly(True)
            btn_browse = QPushButton("选择…")
            btn_open = GhostButton("打开文件夹")
            btn_save = PrimaryButton("保存并迁移")
            row = QHBoxLayout()
            row.addWidget(edit, stretch=1)
            row.addWidget(btn_browse)
            row.addWidget(btn_open)
            row.addWidget(btn_save)
            layout.addLayout(row)
            size_label = QLabel("")
            size_label.setProperty("level", "aux")
            layout.addWidget(size_label)
            hint_label = QLabel(C.STORAGE_DIR_HINTS.get(path_key, ""))
            hint_label.setProperty("level", "aux")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
            content_layout.addWidget(card)

            self._path_edits[path_key] = edit
            self._size_labels[path_key] = size_label
            btn_browse.clicked.connect(
                lambda _checked, k=path_key, cap=dialog_caption: self._browse(k, cap)
            )
            btn_open.clicked.connect(lambda _checked, k=path_key: self._open_dir(k))
            btn_save.clicked.connect(lambda _checked, k=path_key: self._save(k))

        content_layout.addStretch(1)

    def _build_legend_row(self, color_index: int, path_key: str,
                          title: str) -> QWidget:
        """构造环形图图例一行：色块 + 目录名 + 真实占用 + 占比。

        Args:
            color_index: 配色常量下标（与环形图分段顺序一致）。
            path_key: 存储区 key，同时作为图例控件索引。
            title: 目录显示名。
        Returns:
            该行控件（固定行高，数字列固定宽度，避免跳动与挤压）。
        """
        row_widget = QWidget()
        row_widget.setFixedHeight(40)
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        dot = QLabel()
        dot.setFixedSize(12, 12)
        color = C.STORAGE_CHART_COLORS[color_index]
        # 数据可视化色为固定常量，实例级动态样式
        dot.setStyleSheet(
            f"background-color: {color}; border-radius: 6px;"
            "min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px;"
        )
        name_label = QLabel(title.replace("存储目录", ""))
        name_label.setFixedWidth(96)
        size_label = QLabel("0 B")
        size_label.setFixedWidth(110)
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        percent_label = QLabel("0.0%")
        percent_label.setProperty("level", "aux")
        percent_label.setFixedWidth(56)
        percent_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row.addWidget(dot)
        row.addWidget(name_label)
        row.addStretch(1)
        row.addWidget(size_label)
        row.addWidget(percent_label)

        self._legend_size_labels[path_key] = size_label
        self._legend_percent_labels[path_key] = percent_label
        return row_widget

    # ================= 数据加载 =================

    def refresh(self) -> None:
        """刷新路径文本，并按四个目录与数据库的真实字节数重绘环形图与图例。"""
        info = self._service.get_disk_info()
        dirs = info["dirs"]
        db_size = info["db_size"]
        segments = []
        for index, (path_key, _title, _caption) in enumerate(_PATH_ITEMS):
            item = dirs[path_key]
            self._path_edits[path_key].setText(item["path"])
            if item["exists"]:
                state_text = "" if item["dir_size"] > 0 else "（空目录）"
            else:
                state_text = "（目录尚未创建，保存时自动创建）"
            self._size_labels[path_key].setText(
                f"目录占用：{item['dir_size_text']}{state_text}"
            )
            color = C.STORAGE_CHART_COLORS[index]
            segments.append((_title, item["dir_size"], color))
        # 末段：数据库文件（解析报告、笔记、标签、配置等结构化数据）
        segments.append((_DB_LEGEND[1], db_size, C.STORAGE_DB_COLOR))

        total = sum(item[1] for item in segments)
        self.chart.set_segments(segments)

        # 图例：真实占用 + 占总存储量百分比
        for path_key, _title, _caption in _PATH_ITEMS:
            dir_size = dirs[path_key]["dir_size"]
            percent = (dir_size * 100.0 / total) if total else 0.0
            self._legend_size_labels[path_key].setText(
                format_file_size(dir_size)
            )
            self._legend_percent_labels[path_key].setText(f"{percent:.1f}%")
        db_percent = (db_size * 100.0 / total) if total else 0.0
        self._legend_size_labels[_DB_LEGEND[0]].setText(
            info["db_size_text"]
        )
        self._legend_percent_labels[_DB_LEGEND[0]].setText(f"{db_percent:.1f}%")

    # ================= 交互 =================

    def _browse(self, path_key: str, caption: str) -> None:
        """选择新目录并回填输入框（不落库）。"""
        current = self._path_edits[path_key].text()
        start = current if current and os.path.isdir(current) else ""
        chosen = QFileDialog.getExistingDirectory(self, caption, start)
        if chosen:
            self._path_edits[path_key].setText(os.path.normpath(chosen))

    def _open_dir(self, path_key: str) -> None:
        """在系统文件管理器中打开存储目录。"""
        path = self._path_edits[path_key].text()
        if not path:
            show_toast("路径为空", level="warn")
            return
        try:
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606 - 本地资源管理器打开用户自选目录
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except OSError as exc:
            show_toast(f"打开失败：{exc}", level="error")

    def _save(self, path_key: str) -> None:
        """保存路径并通过子线程迁移历史文件。"""
        new_path = self._path_edits[path_key].text().strip()
        if not new_path:
            show_toast("请先选择目录", level="warn")
            return
        if not ConfirmGuard.confirm_migrate(self, path_key):
            return
        self._set_controls_enabled(False)
        self._mask.show_percent(0, "正在迁移历史文件...")
        self._worker = PathMigrateWorker(path_key, new_path, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        """迁移进度回传。"""
        self._mask.show_percent(percent, message)

    def _on_finished(self, code: int, migrated: int, msg: str) -> None:
        """迁移结束：关闭遮罩、刷新展示。"""
        self._mask.hide_mask()
        self._set_controls_enabled(True)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _set_controls_enabled(self, enabled: bool) -> None:
        """迁移期间统一禁用路径操作按钮，避免并发。"""
        for edit in self._path_edits.values():
            edit.setEnabled(enabled)


class ConfirmGuard:
    """路径迁移二次确认（避免与业务无关逻辑散落，静态封装在本模块）。"""

    @staticmethod
    def confirm_migrate(parent, path_key: str) -> bool:
        """迁移前高危确认。"""
        from ui.widgets.dialogs import ConfirmDialog
        title = dict(_PATH_ITEMS)[path_key][1]
        return ConfirmDialog.confirm(
            parent,
            "保存并迁移",
            f"将修改「{title}」并把该目录下的全部历史文件移动到新目录，"
            "移动期间请勿关闭程序，是否继续？",
            confirm_text="保存并迁移",
        )
