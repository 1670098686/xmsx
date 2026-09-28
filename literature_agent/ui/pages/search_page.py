"""分类检索页面：关键词 + 标签/时间/格式组合筛选，卡片网格、批量操作、删除确认。"""
import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from business.literature_search import LiteratureSearchService
from business.system_service import SystemService
from config import constants as C
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.cards import DocCard
from ui.widgets.dialogs import ConfirmDialog
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.tag_picker import TagPickerDialog
from ui.widgets.toast import show_toast
from ui.widgets.worker import ExportWorker

_CARD_MIN_WIDTH = 260
_GRID_SPACING = 12


class SearchPage(QWidget):
    """文献检索页。

    Signals:
        go_import_requested(): 库为空时引导去导入页。
        go_parse_requested(int): 卡片“解析”按钮 → 跳解析页并选中文献。
        go_note_requested(int): 卡片双击/整体点击 → 跳笔记页。
    """

    go_import_requested = pyqtSignal()
    go_parse_requested = pyqtSignal(int)
    go_note_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        """初始化文献检索页面并构建界面。"""
        super().__init__(parent)
        self._service = LiteratureSearchService()
        self._worker = None
        self._loading = None
        self._cards = []
        self._selected_ids = set()
        self._all_rows = []
        self._build_ui()

    # ================= UI =================

    def _build_ui(self) -> None:
        """构建筛选栏、卡片网格与批量操作栏。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("文献检索")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        # 筛选行
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.edit_keyword = QLineEdit()
        self.edit_keyword.setPlaceholderText("搜索标题 / 作者 / 来源 / 正文关键词，回车检索")
        self.edit_keyword.setClearButtonEnabled(True)
        self.combo_tag = QComboBox()
        self.combo_category_filter = QComboBox()
        self.combo_parsed = QComboBox()
        self.combo_time = QComboBox()
        self.combo_type = QComboBox()
        self.combo_tag.setMinimumWidth(130)
        self.combo_category_filter.setMinimumWidth(120)
        self.combo_parsed.setMinimumWidth(110)
        self.combo_time.setMinimumWidth(120)
        self.combo_type.setMinimumWidth(150)
        # 解析状态 / 入库时间段 / 文件格式为固定选项（不随库内容变化）
        self.combo_parsed.addItem("全部状态", C.PARSE_FILTER_ALL)
        self.combo_parsed.addItem("已解析", C.PARSE_FILTER_DONE)
        self.combo_parsed.addItem("未解析", C.PARSE_FILTER_TODO)
        for value, text in (
            (C.TIME_RANGE_ALL, "全部时间"),
            (C.TIME_RANGE_OLDER_THAN_YEAR, "一年以前"),
            (C.TIME_RANGE_THIS_YEAR, "今年"),
            (C.TIME_RANGE_THIS_MONTH, "本月"),
            (C.TIME_RANGE_THIS_WEEK, "本周"),
        ):
            self.combo_time.addItem(text, value)
        self.combo_type.addItem("全部格式", None)
        for lit_type in C.LIT_TYPE_OPTIONS:
            self.combo_type.addItem(C.LIT_TYPE_LABELS[lit_type], lit_type)
        self.btn_search = PrimaryButton("搜索")
        self.btn_reset = GhostButton("重置")
        filter_row.addWidget(self.edit_keyword, stretch=1)
        filter_row.addWidget(self.combo_tag)
        filter_row.addWidget(self.combo_category_filter)
        filter_row.addWidget(self.combo_parsed)
        filter_row.addWidget(self.combo_time)
        filter_row.addWidget(self.combo_type)
        filter_row.addWidget(self.btn_search)
        filter_row.addWidget(self.btn_reset)
        root.addLayout(filter_row)

        # 批量操作行
        self.batch_bar = QFrame()
        self.batch_bar.setObjectName("batchBar")
        batch_layout = QHBoxLayout(self.batch_bar)
        batch_layout.setContentsMargins(10, 6, 10, 6)
        self.label_selected = QLabel("已选 0 篇")
        self.btn_select_all = GhostButton("全选")
        self.btn_clear_select = GhostButton("取消选择")
        self.combo_category = QComboBox()
        self.btn_assign = QPushButton("归类")
        self.btn_batch_tag = QPushButton("批量打标")
        self.btn_batch_export = QPushButton("批量导出")
        self.btn_batch_delete = DangerButton("批量删除")
        export_menu = QMenu(self)
        for fmt, text in (("word", "Word"), ("txt", "TXT"), ("pdf", "PDF")):
            export_menu.addAction(text).triggered.connect(
                lambda checked, f=fmt: self._batch_export(f)
            )
        self.btn_batch_export.setMenu(export_menu)
        batch_layout.addWidget(self.label_selected)
        batch_layout.addSpacing(8)
        batch_layout.addWidget(self.btn_select_all)
        batch_layout.addWidget(self.btn_clear_select)
        batch_layout.addSpacing(12)
        batch_layout.addWidget(QLabel("分类："))
        batch_layout.addWidget(self.combo_category)
        batch_layout.addWidget(self.btn_assign)
        batch_layout.addWidget(self.btn_batch_tag)
        batch_layout.addWidget(self.btn_batch_export)
        batch_layout.addStretch(1)
        batch_layout.addWidget(self.btn_batch_delete)
        for btn in (self.btn_select_all, self.btn_clear_select, self.btn_assign,
                    self.btn_batch_tag, self.btn_batch_export,
                    self.btn_batch_delete):
            btn.setCursor(Qt.PointingHandCursor)
        root.addWidget(self.batch_bar)

        # 网格 / 空状态
        self.stack = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(_GRID_SPACING)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        self.empty_state = EmptyState(icon="📚", text="暂无文献")
        self.empty_state.action_clicked.connect(self._on_empty_action)
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.empty_state)
        root.addWidget(self.stack, stretch=1)

        # 信号
        self.btn_search.clicked.connect(self._do_search)
        self.btn_reset.clicked.connect(self._reset_filters)
        self.edit_keyword.returnPressed.connect(self._do_search)
        self.combo_tag.currentIndexChanged.connect(self._do_search)
        self.combo_category_filter.currentIndexChanged.connect(self._do_search)
        self.combo_parsed.currentIndexChanged.connect(self._do_search)
        self.combo_time.currentIndexChanged.connect(self._do_search)
        self.combo_type.currentIndexChanged.connect(self._do_search)
        self.btn_assign.clicked.connect(self._assign_category)
        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_clear_select.clicked.connect(self._clear_selection)
        self.btn_batch_tag.clicked.connect(self._batch_tag)
        self.btn_batch_delete.clicked.connect(self._batch_delete)

        self._update_batch_bar()

    # ================= 刷新/筛选 =================

    def refresh(self) -> None:
        """页面显示：刷新筛选项并执行当前检索。"""
        self._reload_filter_combos()
        self._do_search()

    def _reload_filter_combos(self) -> None:
        """重新加载标签/分类筛选下拉项并保留选择。

        解析状态、入库时间段、文件格式为固定选项，构建时已填充。
        """
        tags = self._service.get_all_tags()
        category_options = self._service.get_category_tree_options()

        self.combo_tag.blockSignals(True)
        self.combo_tag.clear()
        self.combo_tag.addItem("全部标签", None)
        for tag in tags:
            self.combo_tag.addItem(tag.get("tag_name", ""), tag["id"])
        self.combo_tag.blockSignals(False)

        self.combo_category_filter.blockSignals(True)
        self.combo_category_filter.clear()
        self.combo_category_filter.addItem("全部分类", None)
        for category in category_options:
            self.combo_category_filter.addItem(category["text"], category["id"])
        self.combo_category_filter.blockSignals(False)

        self.combo_category.clear()
        self.combo_category.addItem("选择分类…", None)
        for category in category_options:
            self.combo_category.addItem(category["text"], category["id"])

    def _current_filters(self) -> dict:
        """收集当前筛选条件为查询字典（业务层统一翻译为 SQL 条件）。"""
        filters = {}
        tag_id = self.combo_tag.currentData()
        category_id = self.combo_category_filter.currentData()
        parsed_status = self.combo_parsed.currentData()
        time_range = self.combo_time.currentData()
        lit_type = self.combo_type.currentData()
        if tag_id:
            filters["tag_id"] = tag_id
        if category_id:
            filters["category_id"] = category_id
        if parsed_status and parsed_status != C.PARSE_FILTER_ALL:
            filters["parsed_status"] = parsed_status
        if time_range and time_range != C.TIME_RANGE_ALL:
            filters["time_range"] = time_range
        if lit_type:
            filters["literature_type"] = lit_type
        return filters

    def _do_search(self) -> None:
        """执行检索并渲染卡片。"""
        keyword = self.edit_keyword.text().strip()
        code, rows, msg = self._service.full_text_search(keyword, self._current_filters())
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            rows = []
        self._all_rows = rows
        self._selected_ids &= {row["id"] for row in rows}
        self._render_cards(rows)
        self._update_batch_bar()

    def _reset_filters(self) -> None:
        """重置全部筛选条件并重新检索。"""
        self.edit_keyword.clear()
        self.combo_tag.setCurrentIndex(0)
        self.combo_category_filter.setCurrentIndex(0)
        self.combo_parsed.setCurrentIndex(0)
        self.combo_time.setCurrentIndex(0)
        self.combo_type.setCurrentIndex(0)
        self._do_search()

    # ================= 会话状态恢复（阶段4）=================

    def capture_state(self) -> dict:
        """捕获当前检索条件，供关闭时持久化。

        Returns:
            {"keyword","tag_id","category_id","parsed_status",
             "time_range","literature_type"} 纯数据字典。
        """
        return {
            "keyword": self.edit_keyword.text().strip(),
            "tag_id": self.combo_tag.currentData(),
            "category_id": self.combo_category_filter.currentData(),
            "parsed_status": self.combo_parsed.currentData(),
            "time_range": self.combo_time.currentData(),
            "literature_type": self.combo_type.currentData(),
        }

    def restore_state(self, state: dict) -> None:
        """按持久化的检索条件还原控件并执行检索（数据已失效时静默回退全部）。"""
        if not isinstance(state, dict):
            return
        self.edit_keyword.setText(str(state.get("keyword") or ""))
        self._select_combo_by_data(self.combo_tag, state.get("tag_id"))
        self._select_combo_by_data(self.combo_category_filter,
                                   state.get("category_id"))
        self._select_combo_by_data(self.combo_parsed,
                                   state.get("parsed_status", C.PARSE_FILTER_ALL))
        self._select_combo_by_data(self.combo_time,
                                   state.get("time_range", C.TIME_RANGE_ALL))
        self._select_combo_by_data(self.combo_type, state.get("literature_type"))
        self._do_search()

    @staticmethod
    def _select_combo_by_data(combo, value) -> None:
        """按 itemData 选中下拉项；找不到时回退第 0 项。"""
        if value is None:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # ================= 卡片网格 =================

    def _render_cards(self, rows: list) -> None:
        """渲染文献卡片网格。"""
        self._clear_grid()
        self._cards = []
        for lit in rows:
            card = DocCard(lit, lit.get("tags", []), parent=self.grid_host)
            card.clicked.connect(self._toggle_select)
            card.preview_requested.connect(self._preview)
            card.parse_requested.connect(self.go_parse_requested.emit)
            card.tag_edit_requested.connect(self._edit_tags)
            card.delete_requested.connect(self._delete_one)
            if card.lit_id in self._selected_ids:
                self._apply_selected(card, True)
            self._cards.append(card)
        self._relayout_cards()

        if not rows:
            has_any = self._service.lit_dao.count_all() > 0
            if has_any:
                self.empty_state.set_text("未找到匹配的文献，试试调整筛选条件")
                self.empty_state.set_icon("🔍")
                self.empty_state.set_action_text("重置筛选条件")
            else:
                self.empty_state.set_text("文献库还是空的，先去导入第一篇文献吧")
                self.empty_state.set_icon("📥")
                self.empty_state.set_action_text("前往导入文献")
            self.stack.setCurrentIndex(1)
        else:
            self.stack.setCurrentIndex(0)

    def _clear_grid(self) -> None:
        """清空卡片网格。"""
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def _relayout_cards(self) -> None:
        """按视口宽度自适应列数重排卡片。"""
        if not self._cards:
            return
        viewport_width = self.scroll.viewport().width() or 560
        columns = max(1, viewport_width // (_CARD_MIN_WIDTH + _GRID_SPACING))
        self._clear_grid()
        for index, card in enumerate(self._cards):
            self.grid.addWidget(card, index // columns, index % columns)
        for col in range(columns):
            self.grid.setColumnStretch(col, 1)

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化时重新排布卡片网格。"""
        super().resizeEvent(event)
        self._relayout_cards()

    def _on_empty_action(self) -> None:
        """空态引导按钮动作（跳转导入或解析页）。"""
        if self._service.lit_dao.count_all() > 0:
            self._reset_filters()
        else:
            self.go_import_requested.emit()

    # ================= 选择/批量操作 =================

    def _toggle_select(self, lit_id: int) -> None:
        """切换某张卡片的选中状态。"""
        if lit_id in self._selected_ids:
            self._selected_ids.discard(lit_id)
        else:
            self._selected_ids.add(lit_id)
        for card in self._cards:
            if card.lit_id == lit_id:
                self._apply_selected(card, lit_id in self._selected_ids)
                break
        self._update_batch_bar()

    @staticmethod
    def _apply_selected(card: DocCard, selected: bool) -> None:
        """根据勾选结果更新批量操作栏。"""
        card.setProperty("selected", "true" if selected else False)
        # 刷新样式
        card.style().unpolish(card)
        card.style().polish(card)

    def _update_batch_bar(self) -> None:
        """刷新批量操作栏的选中数与按钮可用性。"""
        count = len(self._selected_ids)
        self.label_selected.setText(f"已选 {count} 篇")
        enabled = count > 0
        for widget in (self.combo_category, self.btn_assign, self.btn_batch_tag,
                       self.btn_batch_export, self.btn_batch_delete):
            widget.setEnabled(enabled)
        # 全选：当前结果中存在未选中的卡片时可用
        current_ids = {row["id"] for row in self._all_rows}
        self.btn_select_all.setEnabled(
            bool(current_ids) and not current_ids.issubset(self._selected_ids)
        )
        # 取消选择：有任意选中项时可用
        self.btn_clear_select.setEnabled(enabled)

    def _select_all(self) -> None:
        """全选当前筛选结果中的全部文献卡片。"""
        current_ids = {row["id"] for row in self._all_rows}
        self._selected_ids |= current_ids
        for card in self._cards:
            self._apply_selected(card, card.lit_id in self._selected_ids)
        self._update_batch_bar()

    def _clear_selection(self) -> None:
        """取消全部已选文献。"""
        self._selected_ids.clear()
        for card in self._cards:
            self._apply_selected(card, False)
        self._update_batch_bar()

    def _selected_list(self) -> list:
        """返回当前选中的文献 id 列表。"""
        return sorted(self._selected_ids)

    def _assign_category(self) -> None:
        """为选中文献批量分配分类。"""
        category_id = self.combo_category.currentData()
        if not category_id:
            show_toast("请先选择要归入的分类", "warn", parent=self.window())
            return
        ids = self._selected_list()
        code, _data, msg = self._service.assign_category(ids, category_id)
        show_toast(msg, "success" if code == C.CODE_SUCCESS else "error",
                   parent=self.window())
        if code == C.CODE_SUCCESS:
            # 复位批量分类下拉，重检刷新卡片上的分类角标（保留当前筛选条件）
            self.combo_category.setCurrentIndex(0)
            self._do_search()

    def _batch_tag(self) -> None:
        """为选中文献批量绑定标签。"""
        tags = self._service.get_all_tags()
        confirmed, selected_tag_ids = TagPickerDialog.pick(
            tags, title="批量添加标签（保留已有标签）", parent=self
        )
        if not confirmed or not selected_tag_ids:
            if confirmed:
                show_toast("未选择任何标签", "warn", parent=self.window())
            return
        ids = self._selected_list()
        code, _data, msg = self._service.batch_bind_tags(ids, selected_tag_ids)
        show_toast(msg, "success" if code == C.CODE_SUCCESS else "error",
                   parent=self.window())
        if code == C.CODE_SUCCESS:
            self._do_search()

    def _batch_delete(self) -> None:
        """批量删除选中文献（按是否存有数据库报告给出明确二次确认）。"""
        ids = self._selected_list()
        report_count = len(self._service.get_lit_ids_with_report(ids))
        if report_count:
            content = (
                f"确定删除选中的 {len(ids)} 篇文献吗？\n"
                f"其中 {report_count} 篇在数据库中存有解析报告，删除后文献"
                "原件、解析报告及全部笔记将一并清除，且不可恢复。"
            )
        else:
            content = (
                f"确定删除选中的 {len(ids)} 篇文献吗？"
                "删除后文献原件、文献记录及全部笔记将一并清除，且不可恢复。"
            )
        if ConfirmDialog.confirm(
            self, title="批量删除文献", content=content,
            confirm_text="全部删除", danger=True,
        ):
            code, data, msg = self._service.batch_delete(ids)
            self._selected_ids.clear()
            self._do_search()
            result = data or {}
            failed = result.get("failed", 0)
            show_toast(
                f"已删除 {result.get('success', 0)} 篇"
                + (f"，{failed} 篇失败" if failed else ""),
                "success" if code == C.CODE_SUCCESS else "warn",
                parent=self.window(),
            )

    def _batch_export(self, fmt: str) -> None:
        """批量导出选中文献的解析报告（默认定位到配置的报告存储目录）。"""
        ids = self._selected_list()
        start_dir = SystemService().get_storage_paths().get("report", "")
        output_dir = QFileDialog.getExistingDirectory(
            self, "选择导出目录", start_dir
        )
        if not output_dir:
            return
        self._loading = LoadingMask(self)
        self._loading.show_progress(self, f"批量导出 {len(ids)} 篇报告...")
        self._worker = ExportWorker(ids, fmt, output_dir, parent=self)
        self._worker.finished_all.connect(self._on_export_finished)
        self._worker.start()

    def _on_export_finished(self, result: dict) -> None:
        """批量导出结束后提示结果。"""
        if self._loading:
            self._loading.hide_mask()
        success = len(result.get("success", []))
        failed = len(result.get("failed", []))
        level = "success" if not failed else "warn"
        show_toast(f"导出完成：成功 {success} 篇，失败 {failed} 篇", level,
                   parent=self.window())

    # ================= 单篇操作 =================

    def _preview(self, lit_id: int) -> None:
        """跳转预览或解析指定文献。"""
        code, abs_path, msg = self._service.get_lit_file_path(lit_id)
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            return
        try:
            os.startfile(abs_path)
        except OSError as exc:
            show_toast(f"无法打开文件：{exc}", "error", parent=self.window())

    def _edit_tags(self, lit_id: int) -> None:
        """打开标签选择弹窗编辑文献标签。"""
        tags = self._service.get_all_tags()
        current = [tag["id"] for tag in self._service.get_lit_tags(lit_id)]
        confirmed, selected_tag_ids = TagPickerDialog.pick(
            tags, current, title="修改文献标签", parent=self
        )
        if not confirmed:
            return
        code, _data, msg = self._service.bind_tags(lit_id, selected_tag_ids)
        show_toast(msg, "success" if code == C.CODE_SUCCESS else "error",
                   parent=self.window())
        if code == C.CODE_SUCCESS:
            self._do_search()

    def _delete_one(self, lit_id: int) -> None:
        """删除单篇文献；数据库中已有解析报告时必须明确告知并二次确认。"""
        has_report = lit_id in self._service.get_lit_ids_with_report([lit_id])
        if has_report:
            content = (
                "该文献在数据库中已有解析报告。删除后，文献原件、文献记录、"
                "数据库中的解析报告及笔记将一并删除，且不可恢复。确定继续吗？"
            )
        else:
            content = (
                "确定删除该文献吗？删除后文献原件、文献记录及笔记不可恢复。"
            )
        if ConfirmDialog.confirm(
            self, title="删除文献",
            content=content, confirm_text="一并删除", danger=True,
        ):
            code, _data, msg = self._service.delete_literature(lit_id)
            show_toast(
                msg, "success" if code == C.CODE_SUCCESS else "error",
                parent=self.window(),
            )
            if code == C.CODE_SUCCESS:
                self._selected_ids.discard(lit_id)
                self._do_search()
