"""文献导入页面：拖拽/点选 → QThread 批量导入 → 表格四态（待导入/导入中/成功/失败/重复）。"""
import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from business.literature_import import DUP_OVERWRITE, DUP_SKIP, LiteratureImportService
from config import constants as C
from ui.widgets.buttons import DangerButton, GhostButton
from ui.widgets.dialogs import (
    CHOICE_OVERWRITE, CHOICE_SKIP_ALL, ChoiceDialog, ConfirmDialog,
)
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.toast import show_toast
from ui.widgets.upload_box import UploadBox
from ui.widgets.worker import ImportWorker
from utils.common_helper import now_str

# 表格列
COL_NAME, COL_TYPE, COL_TIME, COL_STATUS, COL_ACTION = range(5)

# 行态
ROW_PENDING = "待导入"
ROW_RUNNING = "导入中"
ROW_SUCCESS = "成功"
ROW_FAILED = "失败"
ROW_DUPLICATE = "重复"


class ImportPage(QWidget):
    """文献导入页。

    Signals:
        files_dropped(list): 拖拽文件（转发自 UploadBox）。
        go_library_requested(): 点击“加入资料库”，请求主窗口跳转检索页。
    """

    files_dropped = pyqtSignal(list)
    go_library_requested = pyqtSignal()

    def __init__(self, parent=None):
        """初始化导入页面并构建界面。"""
        super().__init__(parent)
        self._service = LiteratureImportService()
        self._worker = None
        self._loading = None
        self._rows = {}  # 路径 → 行号
        self._failed_paths = []
        self._build_ui()

        self.upload_box.files_dropped.connect(self._on_files_received)
        self.upload_box.click_requested.connect(self._open_file_dialog)

    # ================= UI =================

    def _build_ui(self) -> None:
        """构建页面整体布局。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("文献导入")
        title.setObjectName("pageTitle")
        tip = QLabel("支持 PDF / TXT / DOC / DOCX，支持拖拽上传与批量导入")
        tip.setProperty("level", "aux")
        root.addWidget(title)
        root.addWidget(tip)

        self.upload_box = UploadBox()
        root.addWidget(self.upload_box)

        action_row = QHBoxLayout()
        self.btn_stop = GhostButton("停止导入")
        self.btn_retry = GhostButton("重新导入失败项")
        self.btn_clear_failed = GhostButton("清空失败记录")
        self.btn_clear_all = DangerButton("清空全部导入记录")
        self.btn_stop.setEnabled(False)
        action_row.addWidget(self.btn_stop)
        action_row.addWidget(self.btn_retry)
        action_row.addWidget(self.btn_clear_failed)
        action_row.addStretch(1)
        action_row.addWidget(self.btn_clear_all)
        root.addLayout(action_row)

        # 表格 / 空状态 堆栈
        self.stack = QStackedWidget()
        self.table = self._build_table()
        self.empty_state = EmptyState(
            icon="📥", text="暂无导入记录，拖拽文件到上方区域或点击选择文件"
        )
        table_wrap = QFrame()
        table_wrap.setObjectName("tableWrap")
        wrap_layout = QVBoxLayout(table_wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.addWidget(self.table)
        self.stack.addWidget(table_wrap)
        self.stack.addWidget(self.empty_state)
        root.addWidget(self.stack, stretch=1)

        self.btn_stop.clicked.connect(self._stop_import)
        self.btn_retry.clicked.connect(self._retry_failed)
        self.btn_clear_failed.clicked.connect(self._clear_failed_rows)
        self.btn_clear_all.clicked.connect(self._clear_all_records)

        self._switch_empty()

    def _build_table(self) -> QTableWidget:
        """构建导入记录表格。"""
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["文件名", "格式", "导入时间", "状态", "操作"])
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        # 全部列 Interactive：每一列边界都可由用户拖拽调整宽度；
        # 最后一列（操作列）自动占满剩余空间，按钮在列内居中。
        header = table.horizontalHeader()
        for col, width in enumerate(C.IMPORT_COL_WIDTHS):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            table.setColumnWidth(col, width)
        # 允许拖动表头调整列的显示顺序（逻辑列不变，仅视觉换位）
        header.setSectionsMovable(True)
        header.setStretchLastSection(True)
        return table

    # ================= 刷新入口 =================

    def refresh(self) -> None:
        """页面被切换显示时，从数据库载入已导入文献。"""
        if self._worker is not None and self._worker.isRunning():
            return
        self._rebuild_table_from_db()

    def _rebuild_table_from_db(self) -> None:
        """从数据库重建文献导入记录表格。"""
        records = self._service.get_literature_list()
        self.table.setRowCount(0)
        self._rows = {}
        self._failed_paths = []
        for lit in records:
            self._append_record_row(lit)
        self._switch_empty()

    def _switch_empty(self) -> None:
        """在空态提示与内容表格之间切换显示。"""
        has_rows = self.table.rowCount() > 0
        self.stack.setCurrentIndex(0 if has_rows else 1)
        self.btn_retry.setEnabled(bool(self._failed_paths))
        self.btn_clear_failed.setEnabled(bool(self._failed_paths))

    # ================= 选文件/去重决策 =================

    def _open_file_dialog(self) -> None:
        """打开系统文件选择对话框添加待导入文件。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文献文件", "",
            "文献文件 (*.pdf *.txt *.docx *.doc);;所有文件 (*)",
        )
        if paths:
            self._on_files_received(paths)

    def _on_files_received(self, paths: list) -> None:
        """接收拖拽或对话框文件并入队待导入。"""
        self.files_dropped.emit(paths)
        paths = [p for p in paths if p not in self._rows]
        if not paths:
            show_toast("这些文件已在列表中", "warn", parent=self.window())
            return
        if self._worker is not None and self._worker.isRunning():
            show_toast("请等待当前导入完成或先停止", "warn", parent=self.window())
            return

        duplicate_policy = self._decide_duplicate_policy(paths)
        if duplicate_policy is None:
            return
        self._start_import(paths, duplicate_policy)

    def _decide_duplicate_policy(self, paths: list):
        """导入前快速 hash 预检重复文件，弹窗一次性决策。"""
        duplicates = [p for p in paths if self._service.check_duplicate(p)]
        if not duplicates:
            return DUP_SKIP
        sample = os.path.basename(duplicates[0])
        more = f"等 {len(duplicates)} 个文件" if len(duplicates) > 1 else ""
        choice = ChoiceDialog.choose(
            self,
            title="文件重复",
            content=f"“{sample}”{more}已在文献库中，是否覆盖导入？",
        )
        if choice == CHOICE_OVERWRITE:
            return DUP_OVERWRITE
        if choice == CHOICE_SKIP_ALL:
            return DUP_SKIP
        return DUP_SKIP  # 跳过本次：本批次按跳过处理

    # ================= 导入执行 =================

    def _start_import(self, paths: list, policy: str) -> None:
        """启动导入子线程。"""
        for path in paths:
            self._add_session_row(path)
        self._switch_empty()

        self._worker = ImportWorker(paths, policy, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.finished_all.connect(self._on_finished_all)

        self._loading = LoadingMask(self)
        self._loading.show_percent(0, "批量导入中...")
        self.btn_stop.setEnabled(True)
        self._set_action_buttons_running(True)
        self._worker.start()

    def _stop_import(self) -> None:
        """请求中断正在执行的导入任务。"""
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self.btn_stop.setEnabled(False)
            show_toast("正在停止导入...", "info", parent=self.window())

    def _on_progress(self, percent: int, path: str) -> None:
        """接收导入进度信号并刷新进度条。"""
        if self._loading:
            self._loading.show_percent(percent, f"批量导入中 {percent}%")
        row = self._rows.get(path)
        if row is not None:
            self._set_status(row, ROW_RUNNING, f"导入中 {percent}%", "pending")

    def _on_item_done(self, path: str, code: int, msg: str) -> None:
        """接收单文件导入结果并更新对应表格行。"""
        row = self._rows.get(path)
        if row is None:
            return
        filename = os.path.basename(path)
        if code == C.CODE_SUCCESS:
            self._set_status(row, f"✅ {ROW_SUCCESS}", "", "success")
            self._set_row_actions(row)
            self._set_cell(row, COL_TIME, now_str())
        elif code == C.CODE_DUPLICATE:
            self._set_status(row, f"⚠️ {ROW_DUPLICATE}（已跳过）", msg, "fail")
        else:
            self._failed_paths.append(path)
            self._set_status(row, f"❌ {ROW_FAILED}", msg, "fail")

    def _on_finished_all(self, success: list, failed: list, duplicate: list) -> None:
        """全部导入结束后汇总结果并恢复按钮状态。"""
        if self._loading:
            self._loading.hide_mask()
        self.btn_stop.setEnabled(False)
        self._set_action_buttons_running(False)
        self._worker = None
        # 以数据库为准重建，保证导入时间/格式与库一致
        self._rebuild_table_from_db()
        parts = [f"成功 {len(success)}"]
        if failed:
            parts.append(f"失败 {len(failed)}")
        if duplicate:
            parts.append(f"重复跳过 {len(duplicate)}")
        level = "success" if not failed else "warn"
        show_toast("导入完成：" + " / ".join(parts), level, parent=self.window())

    # ================= 表格行操作 =================

    def _add_session_row(self, path: str) -> None:
        """向表格追加一条本次会话的文件记录。"""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[path] = row
        filename = os.path.basename(path)
        suffix = os.path.splitext(filename)[1].lower().lstrip(".").upper()
        self._set_cell(row, COL_NAME, filename)
        self._set_cell(row, COL_TYPE, suffix or "-")
        self._set_cell(row, COL_TIME, now_str())
        self._set_status(row, ROW_PENDING, "等待导入", "pending")
        self.table.setCellWidget(row, COL_ACTION, QWidget())

    def _append_record_row(self, lit: dict) -> None:
        """向表格追加一条历史导入记录。"""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._set_cell(row, COL_NAME, lit.get("literature_title", "未命名文献"))
        self._set_cell(row, COL_TYPE, lit.get("literature_type", ""))
        self._set_cell(row, COL_TIME, lit.get("create_time", ""))
        self._set_status(row, f"✅ {ROW_SUCCESS}", "", "success")
        self._set_row_actions(row, lit.get("id"))
        # 记录已存在行，避免重复导入同一文件时重复建行
        self._rows[f"db:{lit.get('id')}"] = row

    def _set_row_actions(self, row: int, lit_id: int = None) -> None:
        """成功行放“预览/加入资料库”按钮。"""
        if lit_id is None:
            # 会话行：按文件名查最新记录 id
            name_item = self.table.item(row, COL_NAME)
            lit_id = self._find_lit_id_by_name(name_item.text() if name_item else "")

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)
        btn_preview = QPushButton("预览")
        btn_library = QPushButton("前往资料库")
        btn_preview.setCursor(Qt.PointingHandCursor)
        btn_library.setCursor(Qt.PointingHandCursor)
        # 显式最小高度：不依赖 QSS 抛光时序，高 DPI 下也完整可读
        btn_preview.setMinimumHeight(32)
        btn_library.setMinimumHeight(32)
        if lit_id:
            btn_preview.clicked.connect(lambda: self._preview_lit(lit_id))
            btn_library.clicked.connect(self.go_library_requested.emit)
        else:
            btn_preview.setEnabled(False)
            btn_library.setEnabled(False)
        # 两侧弹性：按钮在列宽内居中
        layout.addStretch(1)
        layout.addWidget(btn_preview)
        layout.addWidget(btn_library)
        layout.addStretch(1)
        self.table.setCellWidget(row, COL_ACTION, container)
        # 行高按实际字体度量动态计算（自动适配系统 DPI 缩放），
        # 并保证不小于统一最小行高，彻底杜绝按钮文字被行高裁切
        row_height = max(
            C.TABLE_ROW_MIN_HEIGHT,
            self.table.fontMetrics().height() + 30,
        )
        self.table.setRowHeight(row, row_height)

    def _find_lit_id_by_name(self, name: str):
        """按文件名在已导入文献中查找文献 id。"""
        for lit in self._service.get_literature_list():
            if lit.get("literature_title") == name:
                return lit["id"]
        return None

    def _preview_lit(self, lit_id: int) -> None:
        """预览指定文献的基本信息。"""
        code, abs_path, msg = self._service.get_lit_file_path(lit_id)
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            return
        try:
            os.startfile(abs_path)  # Windows 外部阅读器打开
        except OSError as exc:
            show_toast(f"无法打开文件：{exc}", "error", parent=self.window())

    def _set_cell(self, row: int, col: int, text: str) -> None:
        """设置表格指定单元格的展示文本。"""
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        self.table.setItem(row, col, item)

    def _set_status(self, row: int, text: str, tooltip: str = "",
                    level: str = "pending") -> None:
        """更新表格行的导入状态展示。"""
        label = QLabel(text)
        label.setProperty("rowStatus", level)
        if tooltip:
            label.setToolTip(tooltip)
        self.table.setCellWidget(row, COL_STATUS, label)
        self.table.setRowHeight(
            row,
            max(C.TABLE_ROW_MIN_HEIGHT, self.table.fontMetrics().height() + 30),
        )

    # ================= 失败重试/清空 =================

    def _retry_failed(self) -> None:
        """对失败文件重新发起导入。"""
        paths = [p for p in self._failed_paths if os.path.isfile(p)]
        if not paths:
            show_toast("没有可重试的失败文件", "info", parent=self.window())
            return
        self._failed_paths.clear()
        self._start_import(paths, DUP_SKIP)

    def _clear_failed_rows(self) -> None:
        """纯 UI 操作：失败/重复行不在库中，按数据库重建即等于清除。"""
        self._failed_paths.clear()
        self._rebuild_table_from_db()

    def _clear_all_records(self) -> None:
        """清空当前导入记录（二次确认后执行）。"""
        if ConfirmDialog.confirm(
            self, title="清空全部导入记录",
            content="将删除全部文献原件及其解析报告、笔记（不可恢复），确定继续吗？",
            confirm_text="全部清空", danger=True,
        ):
            code, count, msg = self._service.clear_all_records()
            if code == C.CODE_SUCCESS:
                self._rebuild_table_from_db()
                show_toast(f"已清空 {count} 条记录", "success", parent=self.window())
            else:
                show_toast(msg, "error", parent=self.window())

    def _set_action_buttons_running(self, running: bool) -> None:
        """按任务运行状态启停导入/停止按钮。"""
        self.btn_retry.setEnabled(not running and bool(self._failed_paths))
        self.btn_clear_failed.setEnabled(not running)
        self.btn_clear_all.setEnabled(not running)
        self.upload_box.setEnabled(not running)
