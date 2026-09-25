"""管理中心 - 解析规则配置页：模板切换、六维度开关、解析精度、默认规则管理。"""
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QSpinBox,
    QWidget,
)

from business.parse_rule_service import (
    DEFAULT_KEYWORD_TOP_N, DIMENSION_KEYS, ParseRuleService,
)
from ui.pages.setting_pages import build_scroll_content, build_section_card
from ui.widgets.buttons import DangerButton, GhostButton, PrimaryButton
from ui.widgets.dialogs import ConfirmDialog, InputDialog
from ui.widgets.toast import show_toast

# 精度档位说明
_PRECISION_HINTS = {
    1: "1 · 最快速度（仅提取基础结构）",
    2: "2 · 较快（轻量化分析）",
    3: "3 · 均衡（推荐）",
    4: "4 · 较精细（更全面的语义提取）",
    5: "5 · 最精细（耗时最长）",
}


class SettingRulesPage(QWidget):
    """解析规则配置子页面。"""

    def __init__(self, parent=None):
        """初始化解析规则配置子页面。"""
        super().__init__(parent)
        self._service = ParseRuleService()
        self._loading = False
        self._dim_checks = {}
        self._build_ui()

    def _build_ui(self) -> None:
        """构建解析规则配置界面。"""
        content_layout = build_scroll_content(self)

        # ---- 规则模板卡片 ----
        card, layout = build_section_card("规则模板")
        row = QHBoxLayout()
        row.addWidget(QLabel("当前规则："))
        self.combo_rules = QComboBox()
        self.combo_rules.setMinimumWidth(260)
        self.lbl_default = QLabel("")
        self.lbl_default.setProperty("level", "aux")
        self.btn_set_default = PrimaryButton("设为默认")
        row.addWidget(self.combo_rules)
        row.addWidget(self.lbl_default)
        row.addStretch(1)
        row.addWidget(self.btn_set_default)
        layout.addLayout(row)

        form = QFormLayout()
        form.setSpacing(8)
        self.lbl_subject = QLabel("-")
        form.addRow("适用学科：", self.lbl_subject)

        dim_frame = QFrame()
        dim_row = QHBoxLayout(dim_frame)
        dim_row.setContentsMargins(0, 0, 0, 0)
        dim_row.setSpacing(14)
        labels = self._service.dimension_labels()
        for key in DIMENSION_KEYS:
            check = QCheckBox(labels[key])
            self._dim_checks[key] = check
            dim_row.addWidget(check)
        dim_row.addStretch(1)
        form.addRow("解析维度：", dim_frame)

        self.combo_precision = QComboBox()
        for level in sorted(_PRECISION_HINTS):
            self.combo_precision.addItem(_PRECISION_HINTS[level], level)
        self.lbl_precision = QLabel("")
        self.lbl_precision.setProperty("level", "aux")
        prec_row = QHBoxLayout()
        prec_row.addWidget(self.combo_precision)
        prec_row.addWidget(self.lbl_precision)
        prec_row.addStretch(1)
        form.addRow("解析精度：", prec_row)

        self.spin_keyword = QSpinBox()
        self.spin_keyword.setRange(5, 100)
        self.spin_keyword.setValue(DEFAULT_KEYWORD_TOP_N)
        form.addRow("关键词数量：", self.spin_keyword)
        layout.addLayout(form)
        content_layout.addWidget(card)

        # ---- 操作卡片 ----
        card2, layout2 = build_section_card("规则管理")
        tip = QLabel("修改当前规则后点击「保存修改」；需要保留当前内置规则时可"
                     "「另存为新规则」。默认规则不可删除。")
        tip.setProperty("level", "aux")
        tip.setWordWrap(True)
        layout2.addWidget(tip)
        btn_row = QHBoxLayout()
        self.btn_save = PrimaryButton("保存修改")
        self.btn_save_as = GhostButton("另存为新规则")
        self.btn_reset = GhostButton("恢复默认维度")
        self.btn_delete = DangerButton("删除规则")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_save_as)
        btn_row.addWidget(self.btn_reset)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_delete)
        layout2.addLayout(btn_row)
        content_layout.addWidget(card2)
        content_layout.addStretch(1)

        self.combo_rules.currentIndexChanged.connect(self._on_rule_changed)
        self.combo_precision.currentIndexChanged.connect(self._update_precision_hint)
        self.btn_save.clicked.connect(self._save_current)
        self.btn_save_as.clicked.connect(self._save_as_new)
        self.btn_reset.clicked.connect(self._reset_current)
        self.btn_set_default.clicked.connect(self._set_default)
        self.btn_delete.clicked.connect(self._delete_current)

    # ================= 数据加载 =================

    def refresh(self) -> None:
        """重新加载规则下拉并还原当前默认规则选中。"""
        self._loading = True
        self.combo_rules.blockSignals(True)
        self.combo_rules.clear()
        rules = self._service.list_rules()
        default_id = self._service.get_default_rule_id()
        select_index = 0
        for index, rule in enumerate(rules):
            label = rule["rule_name"]
            if int(rule.get("is_default", 0)) == 1:
                label += "（默认）"
            self.combo_rules.addItem(label, rule["id"])
            if rule["id"] == default_id:
                select_index = index
        self.combo_rules.blockSignals(False)
        if rules:
            self.combo_rules.setCurrentIndex(select_index)
            self._load_detail(self.combo_rules.currentData())
        self._loading = False

    def _on_rule_changed(self, _index: int) -> None:
        """下拉切换规则时刷新维度表单。"""
        if self._loading:
            return
        rule_id = self.combo_rules.currentData()
        if rule_id is not None:
            self._load_detail(rule_id)

    def _load_detail(self, rule_id: int) -> None:
        """把规则详情填入维度/精度/关键词控件。"""
        detail = self._service.get_rule_detail(rule_id)
        if not detail:
            return
        self.lbl_subject.setText(detail.get("subject_type") or "-")
        for key, check in self._dim_checks.items():
            check.setChecked(bool(detail["dimensions"].get(key, True)))
        precision = int(detail.get("precision_level", 3))
        index = self.combo_precision.findData(precision)
        self.combo_precision.setCurrentIndex(max(index, 0))
        self.spin_keyword.setValue(int(detail.get("keyword_top_n",
                                                  DEFAULT_KEYWORD_TOP_N)))
        is_default = int(detail.get("is_default", 0)) == 1
        self.btn_delete.setEnabled(not is_default)
        self.btn_set_default.setEnabled(not is_default)
        self.lbl_default.setText("当前默认规则" if is_default else "")
        self._update_precision_hint()

    def _update_precision_hint(self) -> None:
        """精度下拉旁实时说明。"""
        level = self.combo_precision.currentData()
        self.lbl_precision.setText("档位越高，解析耗时越长" if level else "")

    def _collect_dimensions(self) -> dict:
        """收集六个维度勾选状态。"""
        return {key: check.isChecked() for key, check in self._dim_checks.items()}

    # ================= 操作 =================

    def _current_rule_id(self):
        """返回当前选中的解析规则 id。"""
        return self.combo_rules.currentData()

    def _save_current(self) -> None:
        """保存对当前规则维度/精度/关键词的修改。"""
        rule_id = self._current_rule_id()
        if rule_id is None:
            show_toast("请先选择规则", level="warn")
            return
        code, _data, msg = self._service.update_rule_detail(
            rule_id,
            dimensions=self._collect_dimensions(),
            keyword_top_n=self.spin_keyword.value(),
            precision=self.combo_precision.currentData(),
        )
        show_toast(msg, level="success" if code == 0 else "error")

    def _save_as_new(self) -> None:
        """把当前维度配置另存为一条自定义新规则。"""
        name = InputDialog.prompt(
            self, "另存为新规则", "规则名称：", placeholder="例如：医学文献精细规则"
        )
        if not name:
            return
        code, _rule_id, msg = self._service.create_rule(
            name, "自定义", self._collect_dimensions(),
            precision=self.combo_precision.currentData(),
            keyword_top_n=self.spin_keyword.value(),
        )
        if code == 0:
            show_toast(msg, level="success")
            self.refresh()
        else:
            show_toast(msg, level="error")

    def _reset_current(self) -> None:
        """恢复当前规则为全维度开启 + 默认权重。"""
        rule_id = self._current_rule_id()
        if rule_id is None:
            return
        if not ConfirmDialog.confirm(
            self, "恢复默认维度", "确定将当前规则恢复为全部维度开启、默认权重？"
        ):
            return
        code, _data, msg = self._service.reset_rule(rule_id)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self._load_detail(rule_id)

    def _set_default(self) -> None:
        """把当前规则设为默认解析规则。"""
        rule_id = self._current_rule_id()
        if rule_id is None:
            return
        code, _data, msg = self._service.set_default(rule_id)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()

    def _delete_current(self) -> None:
        """删除当前自定义规则（默认规则服务端禁删）。"""
        rule_id = self._current_rule_id()
        if rule_id is None:
            return
        rule_name = self.combo_rules.currentText()
        if not ConfirmDialog.confirm(
            self, "删除规则", f"确定删除规则「{rule_name}」？删除后不可恢复。",
            confirm_text="删除", danger=True,
        ):
            return
        code, _data, msg = self._service.delete_rule(rule_id)
        show_toast(msg, level="success" if code == 0 else "error")
        if code == 0:
            self.refresh()
