"""笔记批注页面：左侧原文（PDF 原貌框选批注 / 纯文本段落批注），右侧批注/全局笔记双 Tab。"""

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QSplitter, QStackedWidget, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from business.literature_parse import LiteratureParseService
from business.literature_search import LiteratureSearchService
from business.note_manage import NoteManageService, parse_pdf_anchor
from config import constants as C
from ui.widgets.buttons import DangerButton, PrimaryButton
from ui.widgets.dialogs import ConfirmDialog
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.pdf_annotation_viewer import PdfAnnotateViewer
from ui.widgets.toast import show_toast
from ui.widgets.worker import RenderPdfWorker
from utils.common_helper import now_str


class AnnotatedTextView(QTextBrowser):
    """原文阅读器：点击段落发出 paragraph_clicked 信号。

    Signals:
        paragraph_clicked(int): 段落序号（从 0 开始）。
    """

    paragraph_clicked = pyqtSignal(int)

    def mouseReleaseEvent(self, event):
        """鼠标释放时记录所点击段落并发出段落点击信号。"""
        super().mouseReleaseEvent(event)
        cursor = self.cursorForPosition(event.pos())
        block = cursor.block()
        if block.isValid():
            self.paragraph_clicked.emit(block.blockNumber())


class NotePage(QWidget):
    """笔记批注页。

    Signals:
        go_import_requested(): 空状态引导跳转导入页。
    """

    go_import_requested = pyqtSignal()

    def __init__(self, parent=None):
        """初始化笔记批注页面并构建界面。"""
        super().__init__(parent)
        self._search_service = LiteratureSearchService()
        self._parse_service = LiteratureParseService()
        self._note_service = NoteManageService()
        self._lit_list = []
        self._paragraphs = []
        self._paragraph_blocks = {}   # 段落序号 -> 文档 blockNumber
        self._notes = []              # 当前文献全部笔记
        self._is_pdf_mode = False     # 当前文献是否以 PDF 原貌批注模式展示
        self._current_note_id = None  # 正在编辑的批注 id
        self._current_pos = None      # 新批注锚点：纯文本段落序号
        self._pending_pdf_region = None  # 新批注锚点：(页码, PDF 点矩形)
        self._global_note_id = None
        self._loading_lit = False
        self._render_worker = None
        self._render_loading = None
        # UI 层 1 秒防抖计时（业务层另有防抖落库，双保险提示）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._show_saved)
        self._build_ui()

    # ================= UI =================

    def _build_ui(self) -> None:
        """构建页面整体布局。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("笔记批注")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("选择文献："))
        self.combo_lit = QComboBox()
        self.combo_lit.setMinimumWidth(280)
        self.btn_save_all = PrimaryButton("保存本篇全部笔记")
        self.btn_clear_all = DangerButton("清空本篇全部笔记")
        toolbar.addWidget(self.combo_lit, stretch=1)
        toolbar.addSpacing(12)
        toolbar.addWidget(self.btn_save_all)
        toolbar.addWidget(self.btn_clear_all)
        root.addLayout(toolbar)

        self.stack = QStackedWidget()
        self.splitter = self._build_splitter()
        self.empty_state = EmptyState(
            icon="📝", text="暂无已导入文献，请先导入并解析文献",
            action_text="前往导入文献",
        )
        self.empty_state.action_clicked.connect(self.go_import_requested.emit)
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.empty_state)
        root.addWidget(self.stack, stretch=1)

        self.combo_lit.currentIndexChanged.connect(self._on_lit_changed)
        self.btn_save_all.clicked.connect(self._save_all_notes)
        self.btn_clear_all.clicked.connect(self._clear_all_notes)

    def _build_splitter(self) -> QSplitter:
        """构建原文与笔记的左右分栏。"""
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左：原文（PDF 原貌批注 / 纯文本段落批注 双态切换）
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)
        self.left_tip = QLabel("文献原文（点击段落即可批注，已批注段落自动高亮）")
        self.left_tip.setObjectName("blockTitle")
        self.viewer = AnnotatedTextView()
        self.viewer.setReadOnly(True)
        if hasattr(self.viewer, "setPlaceholderText"):
            self.viewer.setPlaceholderText("选择文献后在此查看原文")
        self.viewer.paragraph_clicked.connect(self._on_paragraph_clicked)

        self.pdf_viewer = PdfAnnotateViewer()
        self.pdf_viewer.region_selected.connect(self._on_pdf_region_selected)
        self.pdf_viewer.region_cancelled.connect(self._on_pdf_region_cancelled)
        self.pdf_viewer.annotation_selected.connect(self._on_pdf_annotation_clicked)

        self.source_stack = QStackedWidget()
        self.source_stack.addWidget(self.viewer)
        self.source_stack.addWidget(self.pdf_viewer)
        left_layout.addWidget(self.left_tip)
        left_layout.addWidget(self.source_stack)

        # 右：双 Tab
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_paragraph_tab(), "原文批注")
        self.tabs.addTab(self._build_global_tab(), "全局笔记")
        right_layout.addWidget(self.tabs)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 100)
        splitter.setStretchFactor(1, 85)
        return splitter

    def _build_paragraph_tab(self) -> QWidget:
        """构建段落批注标签页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        self.list_notes = QListWidget()
        self.list_notes.setMinimumHeight(120)

        self.para_title = QLabel("选中原文段落后，在此书写批注")
        self.para_title.setObjectName("blockTitle")
        self.edit_para = QPlainTextEdit()
        self.edit_para.setPlaceholderText("点击左侧任意段落，然后在此输入批注内容")

        btn_row = QHBoxLayout()
        self.btn_save_new = PrimaryButton("保存新批注")
        self.btn_delete_note = DangerButton("删除该批注")
        self.btn_save_new.setEnabled(False)
        self.btn_delete_note.setEnabled(False)
        btn_row.addWidget(self.btn_save_new)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_delete_note)
        self.para_status = QLabel("")
        self.para_status.setProperty("level", "aux")

        layout.addWidget(self.list_notes, stretch=2)
        layout.addWidget(self.para_title)
        layout.addWidget(self.edit_para, stretch=3)
        layout.addLayout(btn_row)
        layout.addWidget(self.para_status)

        self.list_notes.currentItemChanged.connect(self._on_note_selected)
        self.edit_para.textChanged.connect(self._on_para_text_changed)
        self.btn_save_new.clicked.connect(self._save_new_note)
        self.btn_delete_note.clicked.connect(self._delete_current_note)
        return page

    def _build_global_tab(self) -> QWidget:
        """构建全局笔记标签页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        tip = QLabel("全局总结与读后感（每篇文献一条，自动保存）")
        tip.setObjectName("blockTitle")
        self.edit_global = QPlainTextEdit()
        self.edit_global.setPlaceholderText("在此记录这篇文献的整体理解、阅读计划……")
        self.global_status = QLabel("")
        self.global_status.setProperty("level", "aux")
        layout.addWidget(tip)
        layout.addWidget(self.edit_global, stretch=1)
        layout.addWidget(self.global_status)

        self.edit_global.textChanged.connect(self._on_global_text_changed)
        return page

    # ================= 刷新/切换 =================

    def refresh(self) -> None:
        """页面显示：刷新文献列表并加载当前文献笔记。"""
        code, rows, _ = self._search_service.get_all_literature()
        self._lit_list = rows if code == C.CODE_SUCCESS else []
        self.combo_lit.blockSignals(True)
        current_id = self.current_lit_id()
        self.combo_lit.clear()
        select_index = -1
        for index, lit in enumerate(self._lit_list):
            self.combo_lit.addItem(
                lit.get("literature_title", "未命名文献"), lit["id"]
            )
            if lit["id"] == current_id:
                select_index = index
        self.combo_lit.blockSignals(False)

        if self._lit_list:
            self.stack.setCurrentIndex(0)
            self.btn_save_all.setEnabled(True)
            self.btn_clear_all.setEnabled(True)
            self.combo_lit.setCurrentIndex(select_index if select_index >= 0 else 0)
            if select_index < 0:
                self._on_lit_changed(0)
        else:
            self.stack.setCurrentIndex(1)
            self.btn_save_all.setEnabled(False)
            self.btn_clear_all.setEnabled(False)
            self.pdf_viewer.close_document()

    def current_lit_id(self):
        """返回当前选中的文献 id。"""
        return self.combo_lit.currentData()

    def _on_lit_changed(self, index: int) -> None:
        """切换文献时先落库旧笔记再加载新文献。"""
        lit_id = self.combo_lit.itemData(index)
        if not lit_id:
            return
        self._loading_lit = True
        self._flush_pending()
        self._load_literature(lit_id)
        self._loading_lit = False

    def _load_literature(self, lit_id: int) -> None:
        """加载批注数据并异步准备原文（PDF 原貌 / 纯文本段落）。"""
        self._notes = self._note_service.get_notes_by_lit(lit_id)
        self._render_note_list()
        # 原文准备为子线程（Word 转 PDF 可能数秒），完成回调中再按模式渲染锚点
        self._load_source(lit_id)

        global_note = self._note_service.get_global_note(lit_id)
        self._global_note_id = global_note["id"] if global_note else None
        self.edit_global.blockSignals(True)
        self.edit_global.setPlainText(
            global_note["note_content"] if global_note else ""
        )
        self.edit_global.blockSignals(False)
        self.global_status.setText("")

        self._current_note_id = None
        self._current_pos = None
        self._pending_pdf_region = None
        self.btn_save_new.setEnabled(False)
        self.btn_delete_note.setEnabled(False)
        self.para_title.setText("选中原文区域后，在此书写批注")
        self.edit_para.blockSignals(True)
        self.edit_para.clear()
        self.edit_para.blockSignals(False)
        self.para_status.setText("")

    def _load_source(self, lit_id: int) -> None:
        """子线程准备原文：PDF/Word 走原版式 PDF 批注查看器，TXT 走文本段落。"""
        self._is_pdf_mode = False
        if self._render_worker and self._render_worker.isRunning():
            # COM 转换不可强制中断，旧任务结果到达后按当前 lit_id 自动丢弃
            self._render_worker = None
        self._render_worker = RenderPdfWorker(lit_id, parent=self)
        self._render_worker.ready.connect(self._on_source_ready)
        if self._render_loading is None:
            self._render_loading = LoadingMask(self)
        QTimer.singleShot(C.RENDER_MASK_DELAY_MS, self._show_render_mask)
        self._render_worker.start()

    def _show_render_mask(self) -> None:
        """原文准备仍在进行时才显示遮罩（快速返回的 PDF/TXT 不闪遮罩）。"""
        if self._render_worker and self._render_worker.isRunning():
            self._render_loading.show_progress(self, "正在准备原文...")

    def _on_source_ready(self, lit_id: int, code: int, info, _msg: str) -> None:
        """接收子线程渲染结果并切换批注视图，过期文献的结果直接丢弃。"""
        if lit_id != self.current_lit_id():
            return
        self._render_loading.hide_mask()
        if code == C.CODE_SUCCESS:
            pdf_code, _pdf_msg = self.pdf_viewer.load_pdf(info["path"])
            if pdf_code == C.CODE_SUCCESS:
                self._is_pdf_mode = True
                self.source_stack.setCurrentWidget(self.pdf_viewer)
                if info.get("converted"):
                    self.left_tip.setText(
                        "文献原文（已按上传的 Word 文档原版式渲染：在页面上拖拽框选区域即可批注，"
                        "点击序号区域编辑）"
                    )
                else:
                    self.left_tip.setText(
                        "文献原文（PDF 原貌浏览：在页面上拖拽框选区域即可批注，点击序号区域编辑）"
                    )
        if not self._is_pdf_mode:
            # TXT 或原版式渲染失败（无 Office/加密等）：回退为提取文本，保证仍可批注
            self.pdf_viewer.close_document()
            text_code, text, _text_msg = self._parse_service.preview_text(lit_id)
            self._paragraphs = self._split_paragraphs(
                text if text_code == C.CODE_SUCCESS else ""
            )
            self._render_source()
            self.source_stack.setCurrentWidget(self.viewer)
            self.left_tip.setText("文献原文（点击段落即可批注，已批注段落自动高亮）")
        # 批注数据在切换时已加载，按最终模式渲染 PDF 锚点或段落高亮
        if self._is_pdf_mode:
            self.pdf_viewer.set_annotations(self._pdf_annotation_items())
        else:
            self._apply_highlights()

    def _pdf_annotation_items(self) -> list:
        """从当前文献笔记中提取 PDF 区域批注，供批注查看器绘制。"""
        items = []
        for note in self._notes:
            if note["note_type"] != C.NOTE_TYPE_PARAGRAPH:
                continue
            anchor = note.get("paragraph_pos", "")
            if parse_pdf_anchor(anchor):
                items.append({
                    "anchor": anchor,
                    "note_id": note["id"],
                    "preview": self._note_preview(note),
                })
        return items

    @staticmethod
    def _note_preview(note: dict) -> str:
        """生成批注列表用单行摘要。"""
        preview = (note.get("note_content") or "").replace("\n", " ").strip()
        return preview[:24] + "…" if len(preview) > 24 else preview

    @staticmethod
    def _split_paragraphs(text: str) -> list:
        """纯展示用段落切分：按原始换行逐行成段（与解析页原文展示保持一致）。"""
        if not text:
            return []
        lines = text.replace("\r\n", "\n").split("\n")
        return [line.strip() for line in lines if line.strip()]

    def _render_source(self) -> None:
        """将段落数组写入文档，并建立 段落序号→blockNumber 映射。"""
        self.viewer.clear()
        self._paragraph_blocks = {}
        if not self._paragraphs:
            return
        cursor = self.viewer.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for index, paragraph in enumerate(self._paragraphs):
            cursor.insertText(paragraph)
            if index < len(self._paragraphs) - 1:
                cursor.insertText("\n")
        # 以实际 block 校准映射（空行不占段落号）
        block = self.viewer.document().firstBlock()
        paragraph_index = 0
        while block.isValid():
            if block.text().strip():
                self._paragraph_blocks[paragraph_index] = block.blockNumber()
                paragraph_index += 1
            block = block.next()
        self.viewer.verticalScrollBar().setValue(0)

    def _apply_highlights(self) -> None:
        """为已有段落批注的原文块绘制高亮底色（颜色取主题高亮派生色）。"""
        default_color = self.viewer.palette().highlight().color()
        default_color.setAlpha(70)
        for note in self._notes:
            if note["note_type"] != C.NOTE_TYPE_PARAGRAPH:
                continue
            try:
                pos = int(note.get("paragraph_pos", "-1"))
            except (TypeError, ValueError):
                continue
            block_number = self._paragraph_blocks.get(pos)
            if block_number is None:
                continue
            color = default_color
            style = (note.get("highlight_style") or "").strip()
            if style.startswith("#"):
                color = QColor(style)
                color.setAlpha(90)
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            block = self.viewer.document().findBlockByNumber(block_number)
            if not block.isValid():
                continue
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.BlockUnderCursor)
            cursor.mergeCharFormat(fmt)

    def _render_note_list(self) -> None:
        """渲染当前文献的批注列表（PDF 区域 / 纯文本段落 各自标注位置）。"""
        self.list_notes.clear()
        for note in self._notes:
            if note["note_type"] != C.NOTE_TYPE_PARAGRAPH:
                continue
            preview = self._note_preview(note)
            parsed_anchor = parse_pdf_anchor(note.get("paragraph_pos", ""))
            if parsed_anchor:
                label = f"第 {parsed_anchor[0] + 1} 页区域：{preview}"
            else:
                label = f"段落 {int(note['paragraph_pos']) + 1}：{preview}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, note["id"])
            self.list_notes.addItem(item)

    # ================= 段落批注联动 =================

    def _on_paragraph_clicked(self, block_number: int) -> None:
        """点击原文：定位对应批注；无批注则准备新建。"""
        pos = None
        for paragraph_index, mapped_block in self._paragraph_blocks.items():
            if mapped_block == block_number:
                pos = paragraph_index
                break
        if pos is None:
            return
        self.tabs.setCurrentIndex(0)
        self._current_pos = pos
        self._pending_pdf_region = None

        # 查找该段已有批注（一个段落仅一条批注）
        existing = next(
            (n for n in self._notes
             if n["note_type"] == C.NOTE_TYPE_PARAGRAPH
             and not parse_pdf_anchor(n.get("paragraph_pos", ""))
             and int(n["paragraph_pos"]) == pos),
            None,
        )
        if existing is not None:
            self._select_note_in_list(existing["id"])
            return

        # 无批注：进入新建态
        self.list_notes.clearSelection()
        self._current_note_id = None
        self.btn_delete_note.setEnabled(False)
        self.para_title.setText(f"正在批注：第 {pos + 1} 段")
        self.edit_para.blockSignals(True)
        self.edit_para.clear()
        self.edit_para.blockSignals(False)
        self._refresh_save_button()
        self._scroll_to_paragraph(pos)

    def _on_note_selected(self, current: QListWidgetItem, _previous) -> None:
        """列表选中项变化时把对应批注载入编辑器。"""
        if current is None:
            return
        note_id = current.data(Qt.UserRole)
        note = next((n for n in self._notes if n["id"] == note_id), None)
        if note:
            self._activate_note(note)

    def _activate_note(self, note: dict) -> None:
        """把指定批注载入编辑器并联动定位原文（列表与页面点击共用）。"""
        note_id = note["id"]
        self._current_note_id = note_id
        self._current_pos = None
        self._pending_pdf_region = None
        # 选中已有批注即放弃未保存选区（静默，不清空编辑框，随后载入批注内容）
        self.pdf_viewer.discard_pending()
        parsed_anchor = parse_pdf_anchor(note.get("paragraph_pos", ""))
        if parsed_anchor:
            page_index = parsed_anchor[0]
            self.para_title.setText(f"正在批注：第 {page_index + 1} 页区域")
            self.pdf_viewer.focus_annotation(note["paragraph_pos"])
        elif self._is_pdf_mode:
            # 早期在“提取文本”模式下创建的段落批注，无 PDF 坐标，无法在原版式定位
            self.para_title.setText("正在批注：文本模式批注（原版式页面无法定位）")
            show_toast(
                "该批注创建于提取文本模式，无法在原版式页面定位，内容仍可查看和编辑",
                "info", parent=self.window(),
            )
        else:
            pos = int(note["paragraph_pos"])
            self._current_pos = pos
            self.para_title.setText(f"正在批注：第 {pos + 1} 段")
            self._scroll_to_paragraph(pos)
        self.edit_para.blockSignals(True)
        self.edit_para.setPlainText(note["note_content"])
        self.edit_para.blockSignals(False)
        self.btn_delete_note.setEnabled(True)
        self._refresh_save_button()

    # ================= PDF 区域批注联动 =================

    def _on_pdf_region_selected(self, page_index: int, rect_points: tuple) -> None:
        """PDF 页面框选新区域：进入新建批注状态。"""
        self.tabs.setCurrentIndex(0)
        self._pending_pdf_region = (page_index, rect_points)
        self._current_pos = None
        self._current_note_id = None
        self.list_notes.clearSelection()
        self.btn_delete_note.setEnabled(False)
        self.para_title.setText(f"正在批注：第 {page_index + 1} 页新区域")
        self.edit_para.blockSignals(True)
        self.edit_para.clear()
        self.edit_para.blockSignals(False)
        self._refresh_save_button()
        self.edit_para.setFocus()

    def _on_pdf_annotation_clicked(self, anchor: str) -> None:
        """点击页面上已有批注区域：直接载入该批注并同步右侧列表选中。"""
        for row in range(self.list_notes.count()):
            item = self.list_notes.item(row)
            note = next(
                (n for n in self._notes if n["id"] == item.data(Qt.UserRole)),
                None,
            )
            if note and note.get("paragraph_pos") == anchor:
                # 直接激活（当前行可能本来就选中，setCurrentRow 不会发变化信号）
                self.list_notes.setCurrentRow(row)
                self._activate_note(note)
                return

    def _on_pdf_region_cancelled(self) -> None:
        """待保存选区被放弃（Esc/翻页）：退出新建批注状态。"""
        if self._current_note_id is not None:
            return  # 正在编辑已有批注，不受选区取消影响
        self._pending_pdf_region = None
        self._current_pos = None
        self.list_notes.clearSelection()
        self.btn_save_new.setEnabled(False)
        self.btn_delete_note.setEnabled(False)
        self.para_title.setText("在左侧页面拖拽框选区域后，在此书写批注")
        self.edit_para.blockSignals(True)
        self.edit_para.clear()
        self.edit_para.blockSignals(False)
        self.para_status.setText("已取消本次框选")

    def _scroll_to_paragraph(self, pos: int) -> None:
        """将原文视图滚动到指定段落。"""
        block_number = self._paragraph_blocks.get(pos)
        if block_number is None:
            return
        block = self.viewer.document().findBlockByNumber(block_number)
        cursor = QTextCursor(block)
        self.viewer.setTextCursor(cursor)
        self.viewer.ensureCursorVisible()

    def _refresh_save_button(self) -> None:
        """根据挂起的防抖更新刷新保存按钮状态。"""
        has_text = bool(self.edit_para.toPlainText().strip())
        has_anchor = (
            self._current_pos is not None
            or self._pending_pdf_region is not None
        )
        self.btn_save_new.setEnabled(
            self._current_note_id is None and has_text and has_anchor
        )

    def _on_para_text_changed(self) -> None:
        """批注编辑框内容变化时安排防抖保存。"""
        if self._loading_lit:
            return
        if self._current_note_id is not None:
            content = self.edit_para.toPlainText()
            code, _data, msg = self._note_service.update_note(
                self._current_note_id, {"note_content": content}
            )
            if code != C.CODE_SUCCESS:
                show_toast(msg, "error", parent=self.window())
                return
            self.para_status.setText("编辑中…停止输入 1 秒后自动保存")
            self._save_timer.start(C.NOTE_SAVE_DEBOUNCE_MS)
        else:
            self._refresh_save_button()

    def _save_new_note(self) -> None:
        """保存新批注到数据库并刷新列表（PDF 区域 / 文本段落两种锚点）。"""
        lit_id = self.current_lit_id()
        content = self.edit_para.toPlainText().strip()
        if not lit_id:
            show_toast("请先选择文献", "warn", parent=self.window())
            return
        if self._pending_pdf_region is not None:
            page_index, rect_points = self._pending_pdf_region
            code, note, msg = self._note_service.add_pdf_annotation(
                lit_id, page_index, rect_points, content
            )
            anchor_desc = f"第 {page_index + 1} 页区域"
        else:
            pos = self._current_pos
            if pos is None:
                show_toast("请先在左侧原文选中批注位置", "warn", parent=self.window())
                return
            code, note, msg = self._note_service.add_paragraph_note(
                lit_id, pos, content
            )
            anchor_desc = f"第 {pos + 1} 段"
        if not content:
            show_toast("批注内容不能为空", "warn", parent=self.window())
            return
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            return
        self._notes.append(note)
        self._current_note_id = note["id"]
        self._pending_pdf_region = None
        if self._is_pdf_mode:
            # 选区转为正式批注，页面上的虚线「新」框移除、出现带序号的正式框
            self.pdf_viewer.discard_pending()
            self.pdf_viewer.set_annotations(self._pdf_annotation_items())
        else:
            self._apply_highlights()
        self.btn_save_new.setEnabled(False)
        self.btn_delete_note.setEnabled(True)
        self._render_note_list()
        self._select_note_in_list(note["id"])
        self.para_title.setText(f"正在批注：{anchor_desc}")
        show_toast("批注已保存", "success", parent=self.window())

    def _select_note_in_list(self, note_id: int) -> None:
        """在批注列表中选中指定批注。"""
        for row in range(self.list_notes.count()):
            if self.list_notes.item(row).data(Qt.UserRole) == note_id:
                self.list_notes.setCurrentRow(row)
                break

    def _delete_current_note(self) -> None:
        """删除当前选中批注（二次确认后执行）。"""
        if self._current_note_id is None:
            return
        if not ConfirmDialog.confirm(
            self, title="删除批注", content="确定删除这条段落批注吗？",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._note_service.delete_note(self._current_note_id)
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            return
        self._load_literature(self.current_lit_id())  # 重建列表与高亮
        show_toast("批注已删除", "success", parent=self.window())

    # ================= 全局笔记 =================

    def _on_global_text_changed(self) -> None:
        """全局笔记变化时安排防抖保存。"""
        if self._loading_lit:
            return
        lit_id = self.current_lit_id()
        content = self.edit_global.toPlainText()
        if not lit_id:
            return
        if self._global_note_id is None:
            if not content.strip():
                return
            code, note, msg = self._note_service.add_global_note(lit_id, content)
            if code != C.CODE_SUCCESS:
                show_toast(msg, "error", parent=self.window())
                return
            self._global_note_id = note["id"]
            self._notes = self._note_service.get_notes_by_lit(lit_id)
        else:
            code, _data, msg = self._note_service.update_note(
                self._global_note_id, {"note_content": content}
            )
            if code != C.CODE_SUCCESS:
                show_toast(msg, "error", parent=self.window())
                return
        self.global_status.setText("编辑中…停止输入 1 秒后自动保存")
        self._save_timer.start(C.NOTE_SAVE_DEBOUNCE_MS)

    def _show_saved(self) -> None:
        """展示“已保存”轻提示。"""
        stamp = now_str()
        self.para_status.setText(f"已自动保存（{stamp}）")
        self.global_status.setText(f"已自动保存（{stamp}）")

    # ================= 保存全部/清空/退出 =================

    def _save_all_notes(self) -> None:
        """一键保存本篇全部笔记。

        依次处理：正在书写但尚未落库的新批注、防抖等待中的段落批注与全局笔记
        编辑，保证点击后本篇笔记全部写入数据库。
        """
        lit_id = self.current_lit_id()
        if not lit_id:
            show_toast("请先选择文献", "warn", parent=self.window())
            return
        saved_new_note = False
        # 1. 正在新建、尚未点击“保存新批注”的批注
        if (self._current_note_id is None
                and (self._current_pos is not None
                     or self._pending_pdf_region is not None)):
            if self.edit_para.toPlainText().strip():
                self._save_new_note()
                saved_new_note = self._current_note_id is not None
            else:
                show_toast("有一条批注内容为空，未保存该批注", "warn",
                           parent=self.window())
        # 2. 强制落库所有防抖中的编辑（段落批注 + 全局笔记）
        self._note_service.flush_all()
        self._save_timer.stop()
        stamp = now_str()
        self.para_status.setText(f"已保存（{stamp}）")
        self.global_status.setText(f"已保存（{stamp}）")
        if not saved_new_note:
            show_toast("本篇全部笔记已保存", "success", parent=self.window())

    def _clear_all_notes(self) -> None:
        """清空当前文献全部笔记（二次确认后执行）。"""
        lit_id = self.current_lit_id()
        if not lit_id:
            return
        if not ConfirmDialog.confirm(
            self, title="清空全部笔记",
            content="将删除该文献的全部段落批注与全局笔记，且不可恢复，确定继续吗？",
            confirm_text="全部清空", danger=True,
        ):
            return
        self._note_service.flush_all()
        code, count, msg = self._note_service.clear_all_notes(lit_id)
        if code != C.CODE_SUCCESS:
            show_toast(msg, "error", parent=self.window())
            return
        self._load_literature(lit_id)
        show_toast(f"已清空 {count} 条笔记", "success", parent=self.window())

    def _flush_pending(self) -> None:
        """切换文献前立即落库挂起的防抖更新。"""
        self._note_service.flush_all()

    def flush_before_exit(self) -> None:
        """主窗口关闭时调用，保证防抖笔记全部落库。"""
        self._note_service.flush_all()

    def save_now(self) -> bool:
        """Ctrl+S 快捷键入口：立即落库挂起的防抖笔记。

        Returns:
            True 表示执行了保存流程。
        """
        if not self.current_lit_id():
            show_toast("请先选择文献", "warn", parent=self.window())
            return False
        self._note_service.flush_all()
        show_toast("笔记已保存", "success", parent=self.window())
        return True
