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
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.collapse import CollapseBlock
from ui.widgets.dialogs import (
    ConfirmDialog, DEGRADE_ALLOW_ALL, DEGRADE_ALLOW_ONCE, AlertDialog,
    DegradeConfirmDialog, ExportFormatDialog,
)
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.pdf_viewer import PdfViewer
from ui.widgets.toast import show_toast
from ui.widgets.worker import ExportWorker, ParseWorker, RenderPdfWorker

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
        # 解析进行中不使用全屏遮罩：进度只走页面内进度条，不遮挡工具区
        self._library_worker = None
        self._render_worker = None
        self._render_loading = None
        self._lit_list = []
        self._has_report = False
        self._enabled_dimensions = {field for field, _t, _h in BLOCKS[1:]}
        self._pending_ai_notices = []
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
        self.btn_reparse = GhostButton("重新解析")
        self.btn_export = GhostButton("导出报告")
        self.btn_delete_report = DangerButton("删除报告")
        export_menu = QMenu(self)
        action_save_lib = export_menu.addAction("保存到资料库…")
        action_save_lib.triggered.connect(self._save_current_to_library)
        export_menu.addSeparator()
        for text, fmt in (("导出为 Word（另存为…）", "word"),
                          ("导出为 TXT（另存为…）", "txt"),
                          ("导出为 PDF（另存为…）", "pdf")):
            action = export_menu.addAction(text)
            action.triggered.connect(
                lambda checked, f=fmt: self._export_report(f)
            )
        self.btn_export.setMenu(export_menu)
        self.btn_reparse.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_delete_report.setEnabled(False)
        toolbar.addWidget(self.combo_lit, stretch=1)
        toolbar.addWidget(self.btn_parse)
        toolbar.addWidget(self.btn_reparse)
        toolbar.addWidget(self.btn_export)
        toolbar.addWidget(self.btn_delete_report)
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
        self.btn_reparse.clicked.connect(lambda: self._start_parse(reparse=True))
        self.btn_delete_report.clicked.connect(self._delete_report)

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
        """页面显示：刷新文献下拉，并按当前默认规则维度显隐报告折叠块。"""
        code, rows, _ = self._search_service.get_all_literature()
        self._lit_list = rows if code == C.CODE_SUCCESS else []
        self._apply_dimension_visibility()
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
            target_index = select_index if select_index >= 0 else 0
            # clear()+addItem 后 Qt 已自动选中第 0 项（信号被阻塞），若目标
            # 恰为第 0 项，setCurrentIndex 不再发射切换信号，需手动同步
            # 原文/报告区，保证删除报告等操作后界面状态正确
            already_on_target = self.combo_lit.currentIndex() == target_index
            self.combo_lit.setCurrentIndex(target_index)
            if select_index < 0 or already_on_target:
                self._on_lit_changed(target_index)
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
            self.btn_delete_report.setEnabled(True)
        else:
            self._clear_blocks()
            self._has_report = False
            self.btn_reparse.setEnabled(False)
            self.btn_export.setEnabled(False)
            self.btn_delete_report.setEnabled(False)

    def _apply_dimension_visibility(self) -> None:
        """按当前默认解析规则的维度勾选显示/隐藏报告折叠块。

        停用维度的折叠块直接隐藏（含其旧报告内容），重新勾选后无需重新
        解析即可恢复显示；“文献基础信息”块始终展示。
        """
        dimensions = self._parse_service.get_default_dimensions()
        self._enabled_dimensions = {
            field for field, _title, _highlight in BLOCKS[1:]
            if dimensions.get(field, True)
        }
        self.blocks["basic"].setVisible(True)
        for field, _title, _highlight in BLOCKS[1:]:
            self.blocks[field].setVisible(field in self._enabled_dimensions)

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

    def _launch_parse(self, lit_ids, reparse: bool, mask_text: str) -> bool:
        """创建并启动解析子线程。

        Args:
            lit_ids: 单个文献 id 或 id 列表。
            reparse: 是否覆盖旧报告重新解析。
            mask_text: 遮罩上显示的进度文案。
        Returns:
            True 表示已启动；已有解析任务运行时返回 False。
        """
        if self._worker and self._worker.isRunning():
            show_toast("解析进行中，请稍候", "info", parent=self.window())
            return False
        self._pending_ai_notices = []
        is_batch = not isinstance(lit_ids, int) and len(lit_ids) > 1
        self._worker = ParseWorker(lit_ids, reparse=reparse, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.report_ready.connect(self._on_report_ready)
        self._worker.finished_all.connect(self._on_finished_all)
        self._worker.confirm_degradation.connect(
            lambda msg: self._on_confirm_degradation(msg, is_batch)
        )
        self._worker.ai_notices.connect(self._on_ai_notices)

        # 解析不弹全屏遮罩：仅用工具条下方的页面内进度条展示进度
        self._set_running(True)
        self.progress.setValue(0)
        self.progress_label.setText(mask_text)
        self._worker.start()
        return True

    def _start_parse(self, reparse: bool = False) -> None:
        """启动当前选中文献的解析子线程。"""
        lit_id = self.current_lit_id()
        if not lit_id:
            show_toast("请先选择文献", "warn", parent=self.window())
            return
        self._launch_parse(lit_id, reparse,
                           "重新解析中..." if reparse else "解析中...")

    def _on_progress(self, percent: int, message: str) -> None:
        """接收解析进度并更新页面内进度条（不使用遮罩弹窗）。"""
        self.progress.setValue(percent)
        self.progress_label.setText(message)

    def _on_report_ready(self, lit_id: int, report: dict) -> None:
        """接收单篇报告并填充展示块。"""
        if lit_id == self.current_lit_id():
            self._fill_blocks(report)

    def _on_confirm_degradation(self, error_message: str, is_batch: bool) -> None:
        """AI 原件通道失败：弹窗把模型错误原文告知用户并征询降级授权。

        Args:
            error_message: AI 返回的错误原文。
            is_batch: 本次是否为批量解析（提供"本批次全部生效"选项）。
        """
        choice = DegradeConfirmDialog.ask(
            self.window(), error_message, batch=is_batch
        )
        self._worker.provide_degradation_answer(
            allowed=choice in (DEGRADE_ALLOW_ONCE, DEGRADE_ALLOW_ALL),
            apply_to_batch=(choice == DEGRADE_ALLOW_ALL),
        )

    def _on_ai_notices(self, notices: list) -> None:
        """暂存本次解析产生的 AI 失败/降级提示，结束时统一弹窗一次。"""
        self._pending_ai_notices = list(notices or [])

    def _show_ai_notices(self) -> None:
        """把本次解析中 AI 通道的错误聚合为一个弹窗告知用户。"""
        if not self._pending_ai_notices:
            return
        lines = [f"· {text}" for text in self._pending_ai_notices]
        AlertDialog.show_info(
            self.window(),
            "AI 解析提示（已安全处理）",
            "以下文献的 AI 解析过程中出现问题，系统已自动改用本地规则完成解析，"
            "报告内容不受影响；请根据错误说明检查 AI 模型配置：\n\n"
            + "\n".join(lines),
        )
        self._pending_ai_notices = []

    def _on_finished_all(self, success: list, failed: list) -> None:
        """批量解析结束后汇总成功与失败，并引导保存报告到资料库。"""
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
            engines = {item.get("parse_engine", "local") for item in success}
            if len(success) == 1:
                toast_msg = {
                    "ai_file": "解析完成：AI 已通读原件",
                    "ai_text": "解析完成：AI 已通读全文文本",
                    "local": "解析完成（本地规则，AI 不可用）",
                }.get(next(iter(engines)), "解析完成")
            elif engines <= {"ai_file", "ai_text"}:
                toast_msg = f"解析完成：AI 已通读 {len(success)} 篇文献"
            else:
                toast_msg = f"解析完成 {len(success)} 篇（部分使用本地规则）"
            show_toast(toast_msg, "success", parent=self.window())
        self.refresh()
        # AI 错误必须显式弹窗告知（聚合一次，避免批量时弹窗轰炸）
        self._show_ai_notices()
        # 解析流程最后一步：选择格式并保存到项目资料库
        if success:
            self._prompt_save_to_library(success)

    # ================= 删除解析报告（保留文献） =================

    def _delete_report(self) -> None:
        """二次确认后仅删除当前文献的解析报告。

        只清空结构化解析结果并把文献状态回到“未解析”，文献记录、源文件
        与笔记批注保留，文献仍在选择下拉中，可立即重新解析。
        """
        lit_id = self.current_lit_id()
        if not lit_id or self._worker is not None:
            return
        title = (self.combo_lit.currentText() or "").strip() or f"文献{lit_id}"
        confirmed = ConfirmDialog.confirm(
            self,
            title="删除解析报告",
            content=(
                f"确定删除「{title}」的解析报告吗？\n\n"
                "· 仅清空结构化解析结果，文献记录、源文件与笔记批注保留；\n"
                "· 文献状态将回到「未解析」，仍可在上方下拉中选中并重新解析；\n"
                "· 此操作不可恢复。"
            ),
            confirm_text="删除报告",
            danger=True,
        )
        if not confirmed:
            return
        code, _data, msg = self._parse_service.clear_report(lit_id)
        if code != C.CODE_SUCCESS:
            show_toast(msg or "删除解析报告失败", "error",
                       parent=self.window())
            return
        self.progress_label.setText("报告已删除，等待重新解析")
        show_toast("解析报告已删除，文献已保留", "success",
                   parent=self.window())
        self.refresh()

    # ================= 保存报告到资料库 =================

    def _save_current_to_library(self) -> None:
        """手动把当前文献已解析的报告选择格式后保存到资料库。"""
        lit_id = self.current_lit_id()
        if not lit_id:
            show_toast("请先选择并解析文献", "warn", parent=self.window())
            return
        fmt = ExportFormatDialog.choose(self, 1)
        if not fmt:
            return
        if self._library_worker and self._library_worker.isRunning():
            show_toast("报告保存中，请稍候", "info", parent=self.window())
            return
        # 手动保存不传当次报告，由业务层从数据库读取最新已存报告
        self._library_worker = ExportWorker(
            [lit_id], fmt, to_library=True, parent=self
        )
        self._library_worker.progress.connect(self._on_library_progress)
        self._library_worker.finished_all.connect(self._on_library_saved)
        self._loading = LoadingMask(self)
        self._loading.show_progress(self, "正在保存解析报告到资料库...")
        self._library_worker.start()

    def _prompt_save_to_library(self, success_reports: list) -> None:
        """解析完成后弹窗选择格式，并在子线程把报告保存到资料库。

        Args:
            success_reports: 本次解析成功的报告字典列表（含关键词附加信息）。
        """
        if not success_reports:
            return
        fmt = ExportFormatDialog.choose(self, len(success_reports))
        if not fmt:
            return  # 用户选择"暂不保存"
        lit_ids = [int(report["literature_id"]) for report in success_reports]
        report_map = {
            int(report["literature_id"]): report for report in success_reports
        }
        if self._library_worker and self._library_worker.isRunning():
            show_toast("报告保存中，请稍候", "info", parent=self.window())
            return
        self._library_worker = ExportWorker(
            lit_ids, fmt, to_library=True, report_map=report_map, parent=self
        )
        self._library_worker.progress.connect(self._on_library_progress)
        self._library_worker.finished_all.connect(self._on_library_saved)
        self._loading = LoadingMask(self)
        self._loading.show_progress(self, "正在保存解析报告到资料库...")
        self._library_worker.start()

    def _on_library_progress(self, percent: int, message: str) -> None:
        """资料库保存进度更新。"""
        if self._loading:
            self._loading.show_percent(percent, message)

    def _on_library_saved(self, result: dict) -> None:
        """资料库批量保存结束后提示结果。"""
        if self._loading:
            self._loading.hide_mask()
        self._library_worker = None
        success_count = len(result.get("success", []))
        failed = result.get("failed", [])
        if success_count and not failed:
            show_toast(f"已保存 {success_count} 篇解析报告到资料库", "success",
                       parent=self.window())
        elif success_count:
            show_toast(
                f"{success_count} 篇已保存，{len(failed)} 篇保存失败："
                f"{failed[0]['msg']}", "warn", parent=self.window(),
            )
        else:
            show_toast(f"保存失败：{failed[0]['msg'] if failed else '未知错误'}",
                       "error", parent=self.window())

    def _set_running(self, running: bool) -> None:
        """按运行状态切换按钮与进度条展示。"""
        self.btn_parse.setEnabled(not running)
        self.btn_reparse.setEnabled(not running and self._has_report)
        self.btn_delete_report.setEnabled(not running and self._has_report)
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
            if field not in self._enabled_dimensions:
                # 该维度在当前默认规则中已停用：不填充也不展开
                continue
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
