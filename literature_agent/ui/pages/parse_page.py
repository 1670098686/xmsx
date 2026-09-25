"""文献解析页面：选文献 → QThread 解析 → 左原文右 7 折叠块报告 → 导出。"""
import os
import re

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from business.export_backup import ExportBackupService
from business.literature_parse import LiteratureParseService
from business.literature_search import LiteratureSearchService
from business.system_service import SystemService
from config import constants as C
from ui.widgets.buttons import GhostButton, PrimaryButton
from ui.widgets.collapse import CollapseBlock
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.pdf_viewer import PdfViewer
from ui.widgets.toast import show_toast
from ui.widgets.worker import ParseWorker, RenderPdfWorker

# 报告 7 个折叠块（字段, 标题, 是否高亮）
BLOCKS = [
    ("basic", "文献基础信息", False),
    ("research_background", "研究背景", False),
    ("core_view", "核心观点", False),
    ("research_method", "研究方法", False),
    ("innovation_point", "创新点", True),
    ("research_conclusion", "研究结论", True),
    ("reference_list", "参考文献", False),
]

_EXPORT_TITLES = {"word": "Word 文档 (*.docx)", "txt": "文本文件 (*.txt)",
                  "pdf": "PDF 文件 (*.pdf)"}
_EXPORT_SUFFIX = {"word": ".docx", "txt": ".txt", "pdf": ".pdf"}


class ParsePage(QWidget):
    """智能解析页。

    Signals:
        go_import_requested(): 空状态引导按钮 → 跳转导入页。
    """

    go_import_requested = pyqtSignal()

    def __init__(self, parent=None):
        """初始化文献解析页面并构建界面。"""
        super().__init__(parent)
        self._parse_service = LiteratureParseService()
        self._search_service = LiteratureSearchService()
        self._export_service = ExportBackupService()
        self._worker = None
        self._loading = None
        self._render_worker = None
        self._render_loading = None
        self._lit_list = []
        self._has_report = False
        self._build_ui()

    # ================= UI =================

    def _build_ui(self) -> None:
        """构建页面整体布局。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("文献解析")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("选择文献："))
        self.combo_lit = QComboBox()
        self.combo_lit.setMinimumWidth(280)
        self.btn_parse = PrimaryButton("开始解析")
        self.btn_stop = GhostButton("停止解析")
        self.btn_reparse = GhostButton("重新解析")
        self.btn_export = GhostButton("导出报告")
        export_menu = QMenu(self)
        for fmt, text in (("word", "导出为 Word"), ("txt", "导出为 TXT"),
                          ("pdf", "导出为 PDF")):
            action = export_menu.addAction(text)
            action.triggered.connect(lambda checked, f=fmt: self._export_report(f))
        self.btn_export.setMenu(export_menu)
        self.btn_stop.setEnabled(False)
        self.btn_reparse.setEnabled(False)
        self.btn_export.setEnabled(False)
        toolbar.addWidget(self.combo_lit, stretch=1)
        toolbar.addWidget(self.btn_parse)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.btn_reparse)
        toolbar.addWidget(self.btn_export)
        root.addLayout(toolbar)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress_label = QLabel("就绪")
        self.progress_label.setProperty("level", "aux")
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.progress_label)
        root.addLayout(progress_row)

        self.stack = QStackedWidget()
        self.splitter = self._build_splitter()
        self.empty_state = EmptyState(
            icon="🔍", text="暂无已导入文献，请先在「文献导入」页面添加",
            action_text="前往导入文献",
        )
        self.empty_state.action_clicked.connect(self.go_import_requested.emit)
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.empty_state)
        root.addWidget(self.stack, stretch=1)

        self.combo_lit.currentIndexChanged.connect(self._on_lit_changed)
        self.btn_parse.clicked.connect(self._start_parse)
        self.btn_stop.clicked.connect(self._stop_parse)
        self.btn_reparse.clicked.connect(lambda: self._start_parse(reparse=True))

    def _build_splitter(self) -> QSplitter:
        """构建文献选择、原文与报告分栏。"""
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左：原文预览（PDF 应用内原貌浏览；TXT/DOC/DOCX 提取文本展示）
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)
        left_label = QLabel("文献原文")
        left_label.setObjectName("blockTitle")
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setPlaceholderText("选择文献后在此查看原文")
        self.pdf_viewer = PdfViewer()
        # 阅读器堆栈：0=文本阅读器，1=PDF 阅读器
        self.reader_stack = QStackedWidget()
        self.reader_stack.addWidget(self.viewer)
        self.reader_stack.addWidget(self.pdf_viewer)
        left_layout.addWidget(left_label)
        left_layout.addWidget(self.reader_stack)

        # 右：结构化报告
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_label = QLabel("结构化解析报告")
        right_label.setObjectName("blockTitle")
        right_layout.addWidget(right_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        report_host = QWidget()
        report_layout = QVBoxLayout(report_host)
        report_layout.setContentsMargins(0, 0, 0, 0)
        report_layout.setSpacing(8)
        self.blocks = {}
        for field, block_title, highlight in BLOCKS:
            block = CollapseBlock(block_title, icon="📌", highlight=highlight)
            block.set_text("（暂无内容）")
            self.blocks[field] = block
            report_layout.addWidget(block)
        report_layout.addStretch(1)
        scroll.setWidget(report_host)
        right_layout.addWidget(scroll)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 100)
        splitter.setStretchFactor(1, 100)
        return splitter

    # ================= 刷新/选文献 =================

    def refresh(self) -> None:
        """页面显示时刷新文献下拉。"""
        code, rows, _ = self._search_service.get_all_literature()
        self._lit_list = rows if code == C.CODE_SUCCESS else []
        self.combo_lit.blockSignals(True)
        current_id = self.current_lit_id()
        self.combo_lit.clear()
        select_index = -1
        for index, lit in enumerate(self._lit_list):
            parsed_flag = "✅ " if lit.get("is_parsed") == C.PARSE_DONE else ""
            self.combo_lit.addItem(
                f"{parsed_flag}{lit.get('literature_title', '未命名文献')}",
                lit["id"],
            )
            if lit["id"] == current_id:
                select_index = index
        self.combo_lit.blockSignals(False)

        if self._lit_list:
            self.stack.setCurrentIndex(0)
            self.combo_lit.setCurrentIndex(select_index if select_index >= 0 else 0)
            if select_index < 0:
                self._on_lit_changed(0)
        else:
            self.stack.setCurrentIndex(1)
            self.viewer.setPlainText("")
            self.pdf_viewer.close_document()
            self._clear_blocks()

    def select_literature(self, lit_id: int) -> None:
        """外部（检索页卡片/全局状态）请求选中某文献。"""
        for index in range(self.combo_lit.count()):
            if self.combo_lit.itemData(index) == lit_id:
                self.combo_lit.setCurrentIndex(index)
                return
        # 目标不在列表（可能刚导入未刷新），刷新后再选
        self.refresh()
        for index in range(self.combo_lit.count()):
            if self.combo_lit.itemData(index) == lit_id:
                self.combo_lit.setCurrentIndex(index)
                break

    def current_lit_id(self):
        """返回当前选中的文献 id。"""
        return self.combo_lit.currentData()

    def _on_lit_changed(self, index: int) -> None:
        """切换文献时加载原文与已有解析报告。"""
        lit_id = self.combo_lit.itemData(index)
        if not lit_id:
            return
        self.progress.setValue(0)
        self.progress_label.setText("就绪")
        self._load_original(lit_id)
        # 加载已有报告
        report = self._parse_service.get_report(lit_id)
        if report:
            self._fill_blocks(report)
            self._has_report = True
            self.btn_reparse.setEnabled(True)
            self.btn_export.setEnabled(True)
        else:
            self._clear_blocks()
            self._has_report = False
            self.btn_reparse.setEnabled(False)
            self.btn_export.setEnabled(False)

    def _load_original(self, lit_id: int) -> None:
        """按上传格式加载原文：子线程准备 PDF（PDF 直出、Word 转 PDF），TXT 走文本。"""
        if self._render_worker and self._render_worker.isRunning():
            # COM 转换不可强制中断，旧任务结果到达后按当前 lit_id 自动丢弃
            self._render_worker = None
        self._render_worker = RenderPdfWorker(lit_id, parent=self)
        self._render_worker.ready.connect(self._on_render_ready)
        if self._render_loading is None:
            self._render_loading = LoadingMask(self)
        QTimer.singleShot(C.RENDER_MASK_DELAY_MS, self._show_render_mask)
        self._render_worker.start()

    def _show_render_mask(self) -> None:
        """原文渲染仍在进行时才显示遮罩（快速返回的 PDF/TXT 不闪遮罩）。"""
        if self._render_worker and self._render_worker.isRunning():
            self._render_loading.show_progress(self, "正在准备原版式预览...")

    def _on_render_ready(self, lit_id: int, code: int, info, msg: str) -> None:
        """接收子线程渲染结果并切换阅读器，过期文献的结果直接丢弃。"""
        if lit_id != self.current_lit_id():
            return
        self._render_loading.hide_mask()
        if code == C.CODE_SUCCESS:
            self.reader_stack.setCurrentWidget(self.pdf_viewer)
            pdf_code, pdf_msg = self.pdf_viewer.load_pdf(info["path"])
            if pdf_code != 0:
                # 渲染失败（加密/损坏）时降级文本提取
                self._fallback_text(
                    lit_id, f"（原版式预览失败：{pdf_msg}，以下为提取文本）\n\n")
        else:
            # TXT 等不支持原版式渲染，或转换后端不可用：提取文本展示
            self.pdf_viewer.close_document()
            self.reader_stack.setCurrentWidget(self.viewer)
            self._fallback_text(lit_id, "")

    def _fallback_text(self, lit_id: int, prefix: str) -> None:
        """降级通道：提取文本填充文本阅读器。"""
        code, text, msg = self._parse_service.preview_text(lit_id)
        self.viewer.setPlainText(
            prefix + (text if code == C.CODE_SUCCESS else f"原文加载失败：{msg}")
        )

    # ================= 解析 =================

    def _start_parse(self, reparse: bool = False) -> None:
        """启动解析子线程。"""
        lit_id = self.current_lit_id()
        if not lit_id:
            show_toast("请先选择文献", "warn", parent=self.window())
            return
        if self._worker and self._worker.isRunning():
            show_toast("解析进行中，请稍候", "info", parent=self.window())
            return

        self._worker = ParseWorker(lit_id, reparse=reparse, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.report_ready.connect(self._on_report_ready)
        self._worker.finished_all.connect(self._on_finished_all)
        self._worker.canceled.connect(self._on_canceled)

        self._loading = LoadingMask(self)
        self._loading.show_progress(self, reparse and "重新解析中..." or "解析中...")
        self._set_running(True)

    def _stop_parse(self) -> None:
        """请求中断解析任务。"""
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self.btn_stop.setEnabled(False)

    def _on_progress(self, percent: int, message: str) -> None:
        """接收解析进度并更新进度条。"""
        self.progress.setValue(percent)
        self.progress_label.setText(message)
        if self._loading:
            self._loading.show_percent(percent, message)

    def _on_report_ready(self, lit_id: int, report: dict) -> None:
        """接收单篇报告并填充展示块。"""
        if lit_id == self.current_lit_id():
            self._fill_blocks(report)

    def _on_finished_all(self, success: list, failed: list) -> None:
        """批量解析结束后汇总成功与失败。"""
        if self._loading:
            self._loading.hide_mask()
        self._set_running(False)
        self._worker = None
        if failed:
            show_toast(f"解析失败：{failed[0]['msg']}", "error", parent=self.window())
            self.progress_label.setText("解析失败")
        elif success:
            self.progress.setValue(100)
            self.progress_label.setText("解析完成")
            self.btn_reparse.setEnabled(True)
            self.btn_export.setEnabled(True)
            show_toast("解析完成", "success", parent=self.window())
        self.refresh()

    def _on_canceled(self) -> None:
        """解析被用户停止时恢复界面状态。"""
        if self._loading:
            self._loading.hide_mask()
        self._set_running(False)
        self.progress_label.setText("已停止")
        show_toast("已停止解析", "warn", parent=self.window())

    def _set_running(self, running: bool) -> None:
        """按运行状态切换按钮与进度条展示。"""
        self.btn_parse.setEnabled(not running)
        self.btn_reparse.setEnabled(not running and self._has_report)
        self.btn_stop.setEnabled(running)
        self.combo_lit.setEnabled(not running)

    # ================= 报告块渲染 =================

    def _fill_blocks(self, report: dict) -> None:
        """用报告数据填充各结构化展示块。"""
        keywords = report.get("keywords") or []
        if isinstance(keywords, (list, tuple)):
            keywords = "、".join(str(k) for k in keywords)
        basic = (
            f"标题：{report.get('literature_title', '')}\n"
            f"作者：{report.get('literature_author', '') or '未知'}\n"
            f"发表时间：{report.get('publish_time', '') or '未知'}\n"
            f"期刊/来源：{report.get('journal_source', '') or '未知'}\n"
            f"文件格式：{report.get('literature_type', '')}\n"
            f"关键词：{keywords or '未提取'}\n"
            f"解析时间：{report.get('parse_time', '')}"
        )
        self.blocks["basic"].set_text(basic)
        for field, _title, _highlight in BLOCKS[1:]:
            content = (report.get(field) or "").strip()
            self.blocks[field].set_text(content or "（未识别到该部分内容）")
            if content:
                self.blocks[field].expand()

    def _clear_blocks(self) -> None:
        """清空全部结构化展示块内容。"""
        self.blocks["basic"].set_text("选择文献并点击“开始解析”")
        for field, _title, _highlight in BLOCKS[1:]:
            self.blocks[field].set_text("（暂无内容）")

    # ================= 导出 =================

    def export_current(self, fmt: str = None) -> bool:
        """Ctrl+E 快捷键入口：导出当前文献报告。

        Args:
            fmt: word/txt/pdf；不传时取系统默认导出格式配置。
        Returns:
            True 表示已进入导出流程（含用户取消弹窗的情况）。
        """
        if not self.current_lit_id():
            show_toast("请先选择并解析文献", "warn", parent=self.window())
            return False
        if fmt is None:
            from config.settings import get_setting
            fmt = (get_setting(C.CFG_EXPORT_DEFAULT_TYPE, "txt") or "txt").lower()
            if fmt not in _EXPORT_SUFFIX:
                fmt = "txt"
        self._export_report(fmt)
        return True

    def _export_report(self, fmt: str) -> None:
        """弹保存对话框并导出当前解析报告（默认定位到配置的报告存储目录）。"""
        lit_id = self.current_lit_id()
        if not lit_id:
            return
        suffix = _EXPORT_SUFFIX[fmt]
        title = (self.combo_lit.currentText() or "").strip() or f"文献{lit_id}"
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
        default_name = f"解析报告_{safe_title}{suffix}"
        # 默认打开配置的报告存储目录，用户可改到任意位置
        report_dir = SystemService().get_storage_paths().get("report", "")
        default_path = (
            os.path.join(report_dir, default_name) if report_dir else default_name
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出解析报告", default_path, _EXPORT_TITLES[fmt]
        )
        if not path:
            return
        if not path.lower().endswith(suffix):
            path += suffix
        code, out, msg = self._export_service.export_report(lit_id, fmt, path)
        if code == C.CODE_SUCCESS:
            show_toast(f"已导出：{os.path.basename(out)}", "success",
                       parent=self.window())
        else:
            show_toast(msg, "error", parent=self.window())
