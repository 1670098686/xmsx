"""管理中心 - AI 模型配置页：多模型切换、API 密钥加密保存（默认掩码，可明文查看）。

密钥状态模型（杜绝查看/保存误清空）：
- 加载模型时一次性解密，真实明文始终保存在输入框中，默认 Password 回显（圆点）；
- 「查看 / 隐藏」只切换回显模式，绝不替换或重新拉取文本；
- 保存时只有用户实际编辑过密钥才提交新密钥；未编辑则传 None，
  业务层完全不动已存密文。
"""
from PyQt5.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QWidget,
)

from business.system_service import SystemService
from config import constants as C
from ui.pages.setting_pages import build_scroll_content, build_section_card
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.dialogs import (
    AIProbeResultDialog, ConfirmDialog, InputDialog,
)
from ui.widgets.empty_state import EmptyState
from ui.widgets.loading import LoadingMask
from ui.widgets.toast import show_toast
from ui.widgets.worker import AIProbeWorker


class SettingAIPage(QWidget):
    """AI 模型配置子页面。"""

    def __init__(self, parent=None):
        """初始化 AI 模型配置子页面。"""
        super().__init__(parent)
        self._service = SystemService()
        self._loading = False
        self._key_dirty = False
        self._probe_worker = None
        self._probe_mask = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建 AI 模型配置界面。"""
        content_layout = build_scroll_content(self)

        card, layout = build_section_card("AI 模型")
        row = QHBoxLayout()
        row.addWidget(QLabel("当前模型："))
        self.combo_models = QComboBox()
        self.combo_models.setMinimumWidth(240)
        self.lbl_current = QLabel("")
        self.lbl_current.setProperty("level", "aux")
        self.btn_add = GhostButton("新增模型")
        self.btn_delete = DangerButton("删除模型")
        row.addWidget(self.combo_models)
        row.addWidget(self.lbl_current)
        row.addStretch(1)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_delete)
        layout.addLayout(row)

        form = QFormLayout()
        form.setSpacing(10)
        self.edit_base_url = QLineEdit()
        self.edit_base_url.setPlaceholderText("例如：https://dashscope.aliyuncs.com/compatible-mode/v1")
        form.addRow("接口地址：", self.edit_base_url)

        key_row_widget = QWidget()
        key_row = QHBoxLayout(key_row_widget)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.edit_api_key = QLineEdit()
        # 默认密码回显；框内始终是真实明文，查看只切换回显模式
        self.edit_api_key.setEchoMode(QLineEdit.Password)
        self.edit_api_key.setPlaceholderText("输入 API 密钥，将加密后保存在本地数据库")
        self.btn_reveal = QPushButton("👁 查看")
        self.btn_reveal.setCheckable(True)
        key_row.addWidget(self.edit_api_key, stretch=1)
        key_row.addWidget(self.btn_reveal)
        form.addRow("API 密钥：", key_row_widget)

        self.combo_channel = QComboBox()
        for channel in C.AI_FILE_CHANNELS:
            self.combo_channel.addItem(
                C.AI_FILE_CHANNEL_LABELS.get(channel, channel), channel
            )
        self.combo_channel.setMinimumWidth(420)
        form.addRow("原件通道：", self.combo_channel)
        layout.addLayout(form)

        tip = QLabel("密钥使用 Fernet 对称加密后写入本地数据库，仅保存在本机，"
                     "不会随任何网络请求上传；点击「查看」临时明文显示。")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        channel_tip = QLabel(
            "原件通道说明：qwen3.8 等模型支持 PDF Base64 直传（本地文件内联，"
            "无需托管）；qwen-long、Kimi 等使用文件 ID 托管直传；其他模型可先选"
            "「自动识别」并点击「校验配置」，实测通过后按提示保存。"
            "选择「仅全文文本」则任何模型都用本地提取的全文解析。"
        )
        channel_tip.setProperty("level", "aux")
        channel_tip.setWordWrap(True)
        layout.addWidget(channel_tip)

        action_row = QHBoxLayout()
        self.btn_save = PrimaryButton("保存配置")
        self.btn_probe = GhostButton("校验配置")
        action_row.addWidget(self.btn_save)
        action_row.addWidget(self.btn_probe)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        content_layout.addWidget(card)

        self.empty_state = EmptyState(
            icon="🤖", text="尚未配置 AI 模型，点击「新增模型」开始",
        )
        content_layout.addWidget(self.empty_state, stretch=1)
        content_layout.addStretch(1)

        self.combo_models.currentIndexChanged.connect(self._on_model_changed)
        self.btn_reveal.toggled.connect(self._toggle_reveal)
        self.edit_api_key.textEdited.connect(self._on_key_edited)
        self.btn_save.clicked.connect(self._save)
        self.btn_probe.clicked.connect(self._probe_config)
        self.btn_add.clicked.connect(self._add_model)
        self.btn_delete.clicked.connect(self._delete_model)

    # ================= 数据加载 =================

    def refresh(self, select_name: str = None) -> None:
        """重新加载模型列表并选中指定（或当前）模型。

        Args:
            select_name: 需要强制选中的模型名（新增模型后传入）；
                         None 时选中业务层标记的当前模型。
        """
        self._loading = True
        self.combo_models.blockSignals(True)
        self.combo_models.clear()
        models = self._service.list_ai_models()
        current_name = None
        for model in models:
            self.combo_models.addItem(model["name"], model["name"])
            if model["is_current"]:
                current_name = model["name"]

        target_name = select_name or current_name
        if target_name is not None:
            index = self.combo_models.findData(target_name)
            if index >= 0:
                self.combo_models.setCurrentIndex(index)
        self.combo_models.blockSignals(False)

        has_models = bool(models)
        self.empty_state.setVisible(not has_models)
        self.combo_models.setEnabled(has_models)
        self.edit_base_url.setEnabled(has_models)
        self.edit_api_key.setEnabled(has_models)
        self.combo_channel.setEnabled(has_models)
        self.btn_save.setEnabled(has_models)
        self.btn_probe.setEnabled(has_models)
        self.btn_delete.setEnabled(has_models)
        self.btn_reveal.setEnabled(has_models)

        if has_models:
            # 新增模型场景：把选中模型同步为业务层当前模型
            if select_name and select_name != current_name:
                code, _d, msg = self._service.set_ai_model(select_name)
                if code != C.CODE_SUCCESS:
                    show_toast(msg, level="error")
            self._load_current_model()
        else:
            self._clear_form()
        self._loading = False

    def _current_model_name(self):
        """返回当前选中的模型名称。"""
        return self.combo_models.currentData()

    def _find_model(self, name: str, decrypt: bool = False):
        """从业务层获取指定模型配置。"""
        for model in self._service.list_ai_models(decrypt):
            if model["name"] == name:
                return model
        return None

    def _reset_reveal(self) -> None:
        """复位密钥回显为密码态并复位查看按钮（信号阻塞，避免触发切换）。"""
        btn = self.btn_reveal
        btn.blockSignals(True)
        btn.setChecked(False)
        btn.setText("👁 查看")
        btn.blockSignals(False)
        self.edit_api_key.setEchoMode(QLineEdit.Password)

    def _clear_form(self) -> None:
        """清空全部表单（模型删光时调用，避免残留被删模型的数据）。"""
        self.edit_base_url.clear()
        self.edit_api_key.clear()
        self.combo_channel.setCurrentIndex(0)
        self.lbl_current.setText("")
        self._key_dirty = False
        self._reset_reveal()

    def _load_current_model(self) -> None:
        """把当前选中模型信息填入表单。

        密钥一次性解密为明文装入输入框（密码回显），查看仅切换回显模式，
        因此查看/隐藏过程不可能改动或清空密钥。
        """
        name = self._current_model_name()
        model = self._find_model(name, decrypt=True) if name else None
        self._key_dirty = False
        if not model:
            self._clear_form()
            return
        self.edit_base_url.setText(model["base_url"])
        self.edit_api_key.setText(model["api_key"])
        channel = model.get("file_channel") or C.AI_FILE_CHANNEL_AUTO
        channel_index = self.combo_channel.findData(channel)
        self.combo_channel.setCurrentIndex(
            channel_index if channel_index >= 0 else 0
        )
        self._reset_reveal()
        self.lbl_current.setText("✓ 当前使用" if model["is_current"] else "")

    def _on_model_changed(self, _index: int) -> None:
        """用户切换下拉：通知业务层切换当前模型并加载其配置。"""
        if self._loading:
            return
        name = self._current_model_name()
        if not name:
            return
        code, _data, msg = self._service.set_ai_model(name)
        if code == C.CODE_SUCCESS:
            self._load_current_model()
            show_toast(f"已切换到模型：{name}", level="success")
        else:
            show_toast(msg, level="error")

    # ================= 交互 =================

    def _toggle_reveal(self, checked: bool) -> None:
        """眼睛按钮：仅切换明文/密码回显，不改动文本内容。"""
        if checked:
            self.edit_api_key.setEchoMode(QLineEdit.Normal)
            self.btn_reveal.setText("🙈 隐藏")
        else:
            self.edit_api_key.setEchoMode(QLineEdit.Password)
            self.btn_reveal.setText("👁 查看")

    def _on_key_edited(self, _text: str) -> None:
        """用户手动修改密钥后标记脏状态，保存时才覆盖原密钥。"""
        self._key_dirty = True

    def _save(self) -> None:
        """保存接口地址与原件通道；密钥仅在被编辑过时更新（否则保持原密文）。"""
        name = self._current_model_name()
        if not name:
            show_toast("请先选择模型", level="warn")
            return
        base_url = self.edit_base_url.text().strip()
        channel = self.combo_channel.currentData() or C.AI_FILE_CHANNEL_AUTO
        if self._key_dirty:
            api_key = self.edit_api_key.text().strip()
            if not api_key:
                show_toast("API 密钥不能为空", level="warn")
                return
        else:
            # 未编辑密钥：传 None，业务层完全不动已存密文
            api_key = None
        code, _data, msg = self._service.set_ai_key(
            name, api_key, base_url, file_channel=channel
        )
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            # 重新装载；若用户正处于明文查看态，装载后保持明文
            revealed = self.btn_reveal.isChecked()
            self._load_current_model()
            if revealed:
                self.btn_reveal.blockSignals(True)
                self.btn_reveal.setChecked(True)
                self.btn_reveal.blockSignals(False)
                self._toggle_reveal(True)

    def _add_model(self) -> None:
        """新增一个空白模型并立即选中，接口地址/密钥输入框初始化为空。"""
        name = InputDialog.prompt(
            self, "新增 AI 模型", "模型名称：", placeholder="例如：通义千问 / GPT-4o"
        )
        if not name:
            return
        code, _data, msg = self._service.add_ai_model(name)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            # 选中新模型：refresh 内部会把它置为当前模型，并装入空白地址/密钥
            self.refresh(select_name=name)

    def _delete_model(self) -> None:
        """删除当前选中模型；剩余模型自动接管，无剩余时清空表单。"""
        name = self._current_model_name()
        if not name:
            return
        if not ConfirmDialog.confirm(
            self, "删除模型",
            f"确定删除模型「{name}」及其本地保存的密钥？删除后不可恢复。",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._service.delete_ai_model(name)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            # refresh 会选中剩余的第一个；一个不剩时清空接口地址与密钥输入框
            self.refresh()

    # ================= 配置校验 =================

    def _probe_config(self) -> None:
        """启动子线程校验当前表单中的 AI 配置（无需先保存）。

        校验三项：①连通与鉴权；②模型能否正常返回内容；
        ③模型是否支持 PDF 原件直传解析。
        """
        if self._probe_worker and self._probe_worker.isRunning():
            show_toast("正在校验中，请稍候", level="info")
            return
        name = self._current_model_name()
        if not name:
            show_toast("请先选择或新增模型", level="warn")
            return
        base_url = self.edit_base_url.text().strip()
        if not base_url:
            show_toast("请先填写接口地址", level="warn")
            return
        # 密钥框为空（如新增模型尚未填写）时传 None，业务层改读已存密钥
        api_key = self.edit_api_key.text().strip() or None
        channel = self.combo_channel.currentData() or C.AI_FILE_CHANNEL_AUTO

        self._set_form_enabled(False)
        self._probe_worker = AIProbeWorker(
            name, base_url, api_key, file_channel=channel, parent=self
        )
        self._probe_worker.finished_all.connect(self._on_probe_finished)
        self._probe_mask = LoadingMask(self)
        self._probe_mask.show_progress(
            self, "正在校验 AI 配置（连通测试 / PDF 原件探测，可能需要数十秒）..."
        )
        self._probe_worker.start()

    def _on_probe_finished(self, code: int, result, msg: str) -> None:
        """校验子线程结束：关闭遮罩、展示结果；自动识别命中时询问是否保存通道。"""
        if self._probe_mask:
            self._probe_mask.hide_mask()
            self._probe_mask = None
        self._probe_worker = None
        self._set_form_enabled(True)
        AIProbeResultDialog.show_result(self.window(), result, msg)
        self._maybe_save_detected_channel(code, result)

    def _maybe_save_detected_channel(self, code: int, result) -> None:
        """自动识别模式实测到可用通道时，经用户确认后保存到模型配置。

        仅当：校验成功、表单选的是自动识别、实测通道明确、用户未在期间切换模型
        时才询问；任何情况都不静默改动配置。
        """
        if code != C.CODE_SUCCESS or not isinstance(result, dict):
            return
        detected = result.get("detected_channel") or ""
        selected = result.get("file_channel") or C.AI_FILE_CHANNEL_AUTO
        name = result.get("name") or ""
        if selected != C.AI_FILE_CHANNEL_AUTO or not name:
            return
        if detected not in (
            C.AI_FILE_CHANNEL_FILE_DATA, C.AI_FILE_CHANNEL_FILE_ID,
        ):
            return
        if name != self._current_model_name():
            return
        label = C.AI_FILE_CHANNEL_SHORT_LABELS.get(detected, detected)
        if not ConfirmDialog.confirm(
            self.window(), "保存识别到的原件通道",
            f"自动识别确认该模型可通过「{label}」直接读取本地 PDF 原件，"
            "是否立即把该通道保存到模型配置？保存后解析文献将直传 PDF 原件。",
            confirm_text="保存通道",
        ):
            return
        save_code, _d, save_msg = self._service.set_ai_key(
            name, None, None, file_channel=detected
        )
        if save_code == C.CODE_SUCCESS:
            self._load_current_model()
            show_toast("原件通道已保存，解析时将直传 PDF 原件", level="success")
        else:
            show_toast(save_msg, level="error")

    def _set_form_enabled(self, enabled: bool) -> None:
        """校验期间统一禁用/恢复表单与按钮，避免并发操作。"""
        self.combo_models.setEnabled(enabled)
        self.edit_base_url.setEnabled(enabled)
        self.edit_api_key.setEnabled(enabled)
        self.combo_channel.setEnabled(enabled)
        self.btn_save.setEnabled(enabled)
        self.btn_probe.setEnabled(enabled)
        self.btn_add.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)
        self.btn_reveal.setEnabled(enabled)
