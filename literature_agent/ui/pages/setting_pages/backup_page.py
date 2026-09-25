"""管理中心 - 资料导出备份页：全量/增量备份、备份列表、恢复、删除（QThread + 遮罩）。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QWidget,
)

from business.export_backup import ExportBackupService
from config import constants as C
from ui.pages.setting_pages import build_scroll_content, build_section_card
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.dialogs import ConfirmDialog
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.toast import show_toast
from ui.widgets.worker import BackupWorker

_COLUMNS = ["备份时间", "类型", "大小", "状态", "备份文件"]


class SettingBackupPage(QWidget):
    """资料导出备份子页面。"""

    def __init__(self, parent=None):
        """初始化资料导出备份子页面。"""
        super().__init__(parent)
        self._service = ExportBackupService()
        self._worker = None
        self._records = []
        self._build_ui()
        self._mask = LoadingMask(self)

    def _build_ui(self) -> None:
        """构建备份管理界面。"""
        content_layout = build_scroll_content(self)

        card, layout = build_section_card("备份与恢复")
        tip = QLabel("全量备份包含全部文献文件与数据库快照；增量备份仅包含相对"
                     "上次备份新增或变化的文件。恢复将覆盖当前数据库与文献文件。")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        btn_row = QHBoxLayout()
        self.btn_full = PrimaryButton("全量备份")
        self.btn_inc = GhostButton("增量备份")
        self.btn_restore = GhostButton("恢复选中")
        self.btn_delete = DangerButton("删除选中")
        self.btn_refresh = GhostButton("刷新列表")
        btn_row.addWidget(self.btn_full)
        btn_row.addWidget(self.btn_inc)
        btn_row.addWidget(self.btn_restore)
        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_refresh)
        layout.addLayout(btn_row)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table)

        self.empty_state = EmptyState(
            icon="🗄️", text="还没有备份记录，点击「全量备份」创建第一份备份",
        )
        layout.addWidget(self.empty_state)
        content_layout.addWidget(card, stretch=1)

        self.btn_full.clicked.connect(lambda: self._start(BackupWorker.MODE_FULL))
        self.btn_inc.clicked.connect(
            lambda: self._start(BackupWorker.MODE_INCREMENTAL)
        )
        self.btn_restore.clicked.connect(self._restore_selected)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_refresh.clicked.connect(self.refresh)

    # ================= 数据加载 =================

    def refresh(self) -> None:
        """加载备份记录列表。"""
        self._records = self._service.get_backup_list()
        self.table.setRowCount(0)
        for row_index, record in enumerate(self._records):
            self.table.insertRow(row_index)
            time_item = QTableWidgetItem(str(record.get("backup_time", "")))
            time_item.setData(Qt.UserRole, record["id"])
            status = int(record.get("backup_status", C.OP_STATUS_SUCCESS))
            exists = record.get("file_exists", False)
            status_text = "成功" if status == C.OP_STATUS_SUCCESS else "失败"
            if not exists:
                status_text = "文件缺失"
            items = [
                time_item,
                QTableWidgetItem(record.get("type_text", "")),
                QTableWidgetItem(record.get("size_text", "")),
                QTableWidgetItem(status_text),
                QTableWidgetItem(record.get("backup_name", "")),
            ]
            tooltip = record.get("backup_path", "")
            for col, item in enumerate(items):
                if col == 0:
                    item.setData(Qt.UserRole, record["id"])
                item.setToolTip(tooltip)
                self.table.setItem(row_index, col, item)
        has_records = bool(self._records)
        self.empty_state.setVisible(not has_records)
        self.table.setVisible(has_records)
        self.btn_restore.setEnabled(has_records)
        self.btn_delete.setEnabled(has_records)

    def _selected_record(self):
        """返回当前选中行对应的备份记录 dict，无选中返回 None。"""
        row = self.table.currentRow()
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    # ================= 备份 / 恢复 =================

    def _start(self, mode: str) -> None:
        """启动全量/增量备份子线程。"""
        title = "全量备份" if mode == BackupWorker.MODE_FULL else "增量备份"
        if mode == BackupWorker.MODE_INCREMENTAL and not self._records:
            show_toast("请先创建一次全量备份", level="warn")
            return
        self._set_busy(True, f"正在{title}...")
        self._worker = BackupWorker(mode, "", self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_all.connect(
            lambda code, msg, t=title: self._on_finished(code, msg, t)
        )
        self._worker.start()

    def _restore_selected(self) -> None:
        """二次确认后恢复选中备份。"""
        record = self._selected_record()
        if not record:
            show_toast("请先选择一条备份记录", level="warn")
            return
        if not record.get("file_exists"):
            show_toast("备份文件已缺失，无法恢复", level="error")
            return
        if not ConfirmDialog.confirm(
            self, "恢复备份",
            f"将用备份「{record.get('backup_name', '')}」覆盖当前全部文献数据"
            "与数据库，当前未备份的数据将丢失，确定继续？",
            confirm_text="确认恢复", danger=True,
        ):
            return
        self._set_busy(True, "正在恢复...")
        self._worker = BackupWorker(
            BackupWorker.MODE_RESTORE, record["backup_path"], self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_all.connect(self._on_restore_finished)
        self._worker.start()

    def _delete_selected(self) -> None:
        """二次确认后删除选中备份（zip + 记录）。"""
        record = self._selected_record()
        if not record:
            show_toast("请先选择一条备份记录", level="warn")
            return
        if not ConfirmDialog.confirm(
            self, "删除备份",
            f"确定删除备份文件「{record.get('backup_name', '')}」？删除后不可恢复。",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._service.delete_backup(record["id"])
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _on_progress(self, percent: int, message: str) -> None:
        """备份/恢复进度回传。"""
        self._mask.show_percent(percent, message)

    def _on_finished(self, code: int, msg: str, title: str) -> None:
        """备份结束回调。"""
        self._set_busy(False)
        show_toast(msg, level="success" if code == 0 else "error")
        self.refresh()

    def _on_restore_finished(self, code: int, msg: str) -> None:
        """恢复结束回调：成功时提示重启。"""
        self._set_busy(False)
        if code == 0:
            show_toast(f"{msg}，请重启应用使全部数据生效", level="success")
        else:
            show_toast(msg, level="error")
        self.refresh()

    def _set_busy(self, busy: bool, title: str = "处理中...") -> None:
        """备份期间禁用操作按钮并显示遮罩。"""
        for btn in (self.btn_full, self.btn_inc, self.btn_restore,
                    self.btn_delete, self.btn_refresh):
            btn.setEnabled(not busy)
        if busy:
            self._mask.show_percent(0, title)
        else:
            self._mask.hide_mask()
