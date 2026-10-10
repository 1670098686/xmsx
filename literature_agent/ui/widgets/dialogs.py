"""统一二次确认弹窗：删除、覆盖、清空、恢复等高危操作必须使用本组件。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from config.global_config import THEME_LIGHT, THEME_MAP
from config import constants as C
from ui.theme_manager import ThemeManager
from ui.widgets.buttons import GhostButton, DangerButton, PrimaryButton

# 重复文件三选结果
CHOICE_OVERWRITE = "overwrite"
CHOICE_SKIP = "skip"
CHOICE_SKIP_ALL = "skip_all"

# 解析报告保存格式选择结果
REPORT_FMT_WORD = "word"
REPORT_FMT_TXT = "txt"
REPORT_FMT_PDF = "pdf"

# AI 原件→全文降级用户选择
DEGRADE_ALLOW_ONCE = "allow_once"
DEGRADE_ALLOW_ALL = "allow_all"
DEGRADE_DENY = "deny"


class ConfirmDialog(QDialog):
    """高危操作二次确认对话框。"""

    def __init__(self, parent=None, title: str = "确认操作", content: str = "",
                 confirm_text: str = "确认", cancel_text: str = "取消",
                 danger: bool = False):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setProperty("level", "aux")
        layout.addWidget(title_label)
        layout.addWidget(content_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton(cancel_text)
        btn_confirm = DangerButton(confirm_text) if danger else PrimaryButton(confirm_text)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_confirm)
        layout.addLayout(footer)

        btn_cancel.clicked.connect(self.reject)
        btn_confirm.clicked.connect(self.accept)

    @staticmethod
    def confirm(parent=None, title: str = "确认操作", content: str = "",
                confirm_text: str = "确认", cancel_text: str = "取消",
                danger: bool = False) -> bool:
        """模态弹出确认框。

        Args:
            parent: 父窗口。
            title: 弹窗标题。
            content: 风险说明文案。
            confirm_text: 确认按钮文字。
            cancel_text: 取消按钮文字。
            danger: True 时确认按钮使用危险红色样式。
        Returns:
            True 表示用户确认，False 表示取消。
        """
        dialog = ConfirmDialog(parent, title, content, confirm_text, cancel_text, danger)
        return dialog.exec_() == QDialog.Accepted


class ChoiceDialog(QDialog):
    """三选一对话框（重复文件：覆盖导入 / 跳过本次 / 全部跳过）。"""

    def __init__(self, parent=None, title: str = "文件重复", content: str = "",
                 primary_text: str = "覆盖导入", secondary_text: str = "跳过本次",
                 cancel_text: str = "全部跳过"):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(400)
        self.choice = CHOICE_SKIP_ALL

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setProperty("level", "aux")
        layout.addWidget(title_label)
        layout.addWidget(content_label)

        footer = QHBoxLayout()
        btn_cancel = QPushButton(cancel_text)
        btn_secondary = QPushButton(secondary_text)
        btn_primary = DangerButton(primary_text)
        footer.addStretch(1)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_secondary)
        footer.addWidget(btn_primary)
        layout.addLayout(footer)

        btn_primary.clicked.connect(lambda: self._choose(CHOICE_OVERWRITE))
        btn_secondary.clicked.connect(lambda: self._choose(CHOICE_SKIP))
        btn_cancel.clicked.connect(lambda: self._choose(CHOICE_SKIP_ALL))

    def _choose(self, choice: str) -> None:
        """内部统一的弹窗选择执行入口。"""
        self.choice = choice
        self.accept()

    @staticmethod
    def choose(parent=None, title: str = "文件重复", content: str = "",
               primary_text: str = "覆盖导入", secondary_text: str = "跳过本次",
               cancel_text: str = "全部跳过") -> str:
        """模态弹出三选框。

        Returns:
            overwrite / skip / skip_all。
        """
        dialog = ChoiceDialog(
            parent, title, content, primary_text, secondary_text, cancel_text
        )
        dialog.exec_()
        return dialog.choice


class InputDialog(QDialog):
    """统一单行文本输入弹窗（新增分类/标签/规则/模型等场景使用）。"""

    def __init__(self, parent=None, title: str = "请输入", label_text: str = "名称：",
                 default_text: str = "", placeholder: str = ""):
        """初始化控件。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel(title))
        self.input = QLineEdit(default_text)
        self.input.setPlaceholderText(placeholder)
        label = QLabel(label_text)
        label.setProperty("level", "aux")
        layout.addWidget(label)
        layout.addWidget(self.input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("formError")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_confirm = PrimaryButton("确定")
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_confirm)
        layout.addLayout(footer)

        btn_cancel.clicked.connect(self.reject)
        btn_confirm.clicked.connect(self._on_confirm)
        self.input.returnPressed.connect(self._on_confirm)

    def _on_confirm(self) -> None:
        """空内容不允许提交，行内红字提示。"""
        if not self.input.text().strip():
            self.error_label.setText("内容不能为空")
            self.error_label.show()
            return
        self.accept()

    @staticmethod
    def prompt(parent=None, title: str = "请输入", label_text: str = "名称：",
               default_text: str = "", placeholder: str = ""):
        """模态弹出输入框。

        Returns:
            用户输入的去空格字符串；取消返回 None。
        """
        dialog = InputDialog(parent, title, label_text, default_text, placeholder)
        if dialog.exec_() != QDialog.Accepted:
            return None
        return dialog.input.text().strip()


class ExportFormatDialog(QDialog):
    """解析完成后选择报告文件格式并保存到解析报告存储目录的对话框。"""

    def __init__(self, parent=None, lit_count: int = 1):
        """初始化格式选择控件。

        Args:
            lit_count: 本次解析成功的文献数量（用于文案单复数）。
        """
        super().__init__(parent)
        self.setWindowTitle("保存解析报告")
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.selected_fmt = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel("保存解析报告")
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)
        target = "该篇文献的解析报告" if lit_count <= 1 else \
            f"本次解析成功的 {lit_count} 篇文献报告"
        content = QLabel(
            f"{target}将以所选格式保存到解析报告存储目录"
            "（管理中心可配置，与上传的文献原件目录分开），"
            "历史版本会自动保留。"
        )
        content.setWordWrap(True)
        content.setProperty("level", "aux")
        layout.addWidget(content)

        footer = QHBoxLayout()
        btn_skip = QPushButton("暂不保存")
        btn_word = QPushButton("Word 文档")
        btn_txt = QPushButton("TXT 文本")
        btn_pdf = PrimaryButton("PDF 文档")
        footer.addStretch(1)
        footer.addWidget(btn_skip)
        footer.addWidget(btn_word)
        footer.addWidget(btn_txt)
        footer.addWidget(btn_pdf)
        layout.addLayout(footer)

        btn_skip.clicked.connect(self.reject)
        btn_word.clicked.connect(lambda: self._select(REPORT_FMT_WORD))
        btn_txt.clicked.connect(lambda: self._select(REPORT_FMT_TXT))
        btn_pdf.clicked.connect(lambda: self._select(REPORT_FMT_PDF))

    def _select(self, fmt: str) -> None:
        """记录所选格式并关闭弹窗。"""
        self.selected_fmt = fmt
        self.accept()

    @staticmethod
    def choose(parent=None, lit_count: int = 1):
        """模态弹出格式选择框。

        Returns:
            word/txt/pdf；用户选择"暂不保存"或关闭时返回 None。
        """
        dialog = ExportFormatDialog(parent, lit_count)
        if dialog.exec_() != QDialog.Accepted:
            return None
        return dialog.selected_fmt


class AlertDialog(QDialog):
    """只读信息弹窗（展示 AI 返回的错误原文等较长文本）。"""

    def __init__(self, parent=None, title: str = "提示", message: str = "",
                 confirm_text: str = "我知道了", danger: bool = False):
        """初始化控件。

        Args:
            title: 弹窗标题。
            message: 完整提示内容（支持长文本、自动换行、可选中复制）。
            confirm_text: 确认按钮文案。
            danger: True 时确认按钮使用危险红色样式。
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        content = QLabel(message)
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content.setProperty("level", "aux")
        body_layout.addWidget(content)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll)

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_confirm = DangerButton(confirm_text) if danger else PrimaryButton(confirm_text)
        footer.addWidget(btn_confirm)
        layout.addLayout(footer)
        btn_confirm.clicked.connect(self.accept)

    @staticmethod
    def show_info(parent=None, title: str = "提示", message: str = "",
                  confirm_text: str = "我知道了", danger: bool = False) -> None:
        """模态弹出只读信息框（无返回值）。"""
        dialog = AlertDialog(parent, title, message, confirm_text, danger)
        dialog.exec_()


class DegradeConfirmDialog(QDialog):
    """AI 原件通道失败时，征询用户是否允许改用全文文本通道重试。"""

    def __init__(self, parent=None, error_message: str = "", batch: bool = False):
        """初始化控件。

        Args:
            error_message: AI 返回的错误原文。
            batch: 是否为批量解析（显示"对本批次全部生效"按钮）。
        """
        super().__init__(parent)
        self.setWindowTitle("AI 原件解析失败")
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.choice = DEGRADE_DENY

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title_label = QLabel("该模型无法直接读取文献原件")
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)

        content = QLabel(
            "AI 模型返回了以下错误：\n"
            f"{error_message}\n\n"
            "是否允许改用「全文文本模式」重试？\n"
            "该模式会先在本地完整提取文献全文，再发送给模型通读解析，"
            "解析效果与原件模式基本一致（文献不会上传到任何第三方，"
            "全文仅发送给您配置的 AI 接口）。"
        )
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content.setProperty("level", "aux")
        layout.addWidget(content)

        footer = QHBoxLayout()
        btn_deny = QPushButton("不允许，回退本地解析")
        btn_allow_all = GhostButton("允许（本批次全部生效）") if batch else None
        btn_allow_once = PrimaryButton("允许，改用全文文本")
        footer.addStretch(1)
        footer.addWidget(btn_deny)
        if btn_allow_all is not None:
            footer.addWidget(btn_allow_all)
        footer.addWidget(btn_allow_once)
        layout.addLayout(footer)

        btn_deny.clicked.connect(lambda: self._choose(DEGRADE_DENY))
        if btn_allow_all is not None:
            btn_allow_all.clicked.connect(lambda: self._choose(DEGRADE_ALLOW_ALL))
        btn_allow_once.clicked.connect(lambda: self._choose(DEGRADE_ALLOW_ONCE))

    def _choose(self, choice: str) -> None:
        """记录用户选择并关闭弹窗。"""
        self.choice = choice
        self.accept()

    @staticmethod
    def ask(parent=None, error_message: str = "", batch: bool = False) -> str:
        """模态弹出降级授权框。

        Returns:
            allow_once / allow_all / deny。
        """
        dialog = DegradeConfirmDialog(parent, error_message, batch)
        dialog.exec_()
        return dialog.choice


class AIProbeResultDialog(QDialog):
    """AI 模型配置校验结果弹窗（连通性/输出返回/PDF 原件解析三项）。"""

    def __init__(self, parent=None, result: dict = None, msg: str = ""):
        """初始化控件。

        Args:
            result: SystemService.validate_ai_config 返回的结果字典。
            msg: 整体错误提示（鉴权/连通失败时）。
        """
        super().__init__(parent)
        result = result or {}
        self.setWindowTitle("AI 配置校验结果")
        self.setWindowFlags(self.windowFlags() | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("modalBox")
        root.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title_label = QLabel("AI 配置校验结果")
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)

        model_name = result.get("name") or "（未选择模型）"
        base_url = result.get("base_url") or ""
        layout.addWidget(self._plain_label(f"模型：{model_name}"))
        layout.addWidget(self._plain_label(f"接口地址：{base_url}"))

        selected_channel = result.get("file_channel") or C.AI_FILE_CHANNEL_AUTO
        selected_text = C.AI_FILE_CHANNEL_SHORT_LABELS.get(
            selected_channel, selected_channel
        )
        detected_channel = result.get("detected_channel") or ""
        if detected_channel and detected_channel != selected_channel:
            selected_text += (
                "；实测可用通道："
                + C.AI_FILE_CHANNEL_SHORT_LABELS.get(
                    detected_channel, detected_channel
                )
            )
        layout.addWidget(self._plain_label(
            f"原件解析通道：{selected_text}"
        ))

        chat_ok = bool(result.get("chat_ok"))
        layout.addWidget(self._status_row(
            "① 连通与鉴权",
            chat_ok,
            "配置可用，模型已正常响应" if chat_ok else "配置不可用",
        ))
        if chat_ok and result.get("chat_reply"):
            layout.addWidget(self._detail_label(
                f"模型回复：{result['chat_reply']}"
            ))
        if not chat_ok:
            detail = result.get("chat_error") or msg or "未知错误"
            layout.addWidget(self._detail_label(f"错误信息：{detail}"))

        file_ok = result.get("file_ok")
        used_channel = detected_channel or ""
        if file_ok is True:
            if used_channel == C.AI_FILE_CHANNEL_FILE_DATA:
                file_text = "支持：本地 PDF 已通过 Base64 直传并读到文件正文内容"
            else:
                file_text = "支持：原件已成功上传托管并读取到文件正文内容"
        elif selected_channel == C.AI_FILE_CHANNEL_NONE:
            file_text = (
                "未检测：当前选择为“仅全文文本”，解析文献时将使用"
                "本地提取的全文文本模式，不影响正常使用。"
            )
        elif file_ok is False:
            file_text = (
                "不支持：该模型无法通过所选通道读取 PDF 原件（"
                f"{result.get('file_error') or '未通过探测'}）。"
                "可切换其他原件通道重新校验，或直接使用全文文本模式。"
            )
        else:
            file_text = "未检测（连通性校验未通过）"
        layout.addWidget(self._status_row(
            "② PDF 原件解析",
            file_ok is True,
            file_text,
            skipped=file_ok is None,
        ))
        if file_ok is True and result.get("file_reply"):
            layout.addWidget(self._detail_label(
                f"模型读到的正文：{result['file_reply']}"
            ))

        hint = result.get("name_hint_file")
        if hint and file_ok is not True:
            layout.addWidget(self._detail_label(
                "提示：模型名称包含长文档模型特征，若探测不支持，"
                "请确认接口网关是否开通了文件消息能力。"
            ))

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_close = PrimaryButton("关闭")
        footer.addWidget(btn_close)
        layout.addLayout(footer)
        btn_close.clicked.connect(self.accept)

    @staticmethod
    def _plain_label(text: str) -> QLabel:
        """构造普通可选中信息行。"""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    @staticmethod
    def _detail_label(text: str) -> QLabel:
        """构造次要样式的长文本明细行。"""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setProperty("level", "aux")
        return label

    @staticmethod
    def _status_row(title: str, ok: bool, detail: str,
                    skipped: bool = False) -> QLabel:
        """构造「✓/✗ + 标题 + 说明」状态行（语义色取自当前主题）。"""
        icon = "✓" if ok else ("—" if skipped else "✗")
        label = QLabel(f"{icon} {title}：{detail}")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        theme = THEME_MAP.get(ThemeManager().current_theme(), THEME_LIGHT)
        if ok:
            label.setStyleSheet(f"color: {theme['success']};")
        elif not skipped:
            label.setStyleSheet(f"color: {theme['error']};")
        return label

    @staticmethod
    def show_result(parent=None, result: dict = None, msg: str = "") -> None:
        """模态弹出校验结果。"""
        dialog = AIProbeResultDialog(parent, result, msg)
        dialog.exec_()
