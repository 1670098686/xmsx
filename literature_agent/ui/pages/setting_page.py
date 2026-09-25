"""管理中心容器页：QStackedWidget 承载 5 个设置子页面，由左侧折叠菜单驱动切换。"""
from PyQt5.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from ui.pages.setting_pages.ai_page import SettingAIPage
from ui.pages.setting_pages.backup_page import SettingBackupPage
from ui.pages.setting_pages.rules_page import SettingRulesPage
from ui.pages.setting_pages.storage_page import SettingStoragePage
from ui.pages.setting_pages.tag_page import SettingTagPage

# 子模块 key → 堆栈索引
MODULE_RULES = "rules"
MODULE_STORAGE = "storage"
MODULE_AI = "ai"
MODULE_BACKUP = "backup"
MODULE_TAG = "tag"
_MODULE_KEYS = (
    MODULE_RULES, MODULE_STORAGE, MODULE_AI, MODULE_BACKUP, MODULE_TAG,
)


class SettingPage(QWidget):
    """管理中心容器：内部堆栈切换 5 个子页面。"""

    def __init__(self, parent=None):
        """初始化管理中心容器页面并注册各子模块。"""
        super().__init__(parent)
        self._current_key = MODULE_RULES
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.page_rules = SettingRulesPage()
        self.page_storage = SettingStoragePage()
        self.page_ai = SettingAIPage()
        self.page_backup = SettingBackupPage()
        self.page_tag = SettingTagPage()
        self._pages = {
            MODULE_RULES: self.page_rules,
            MODULE_STORAGE: self.page_storage,
            MODULE_AI: self.page_ai,
            MODULE_BACKUP: self.page_backup,
            MODULE_TAG: self.page_tag,
        }
        for key in _MODULE_KEYS:
            self.stack.addWidget(self._pages[key])
        layout.addWidget(self.stack)

    def show_module(self, key: str) -> None:
        """切换到指定管理子页面并刷新其数据。

        Args:
            key: rules/storage/ai/backup/tag。
        """
        page = self._pages.get(key)
        if page is None:
            return
        self._current_key = key
        self.stack.setCurrentWidget(page)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def set_module_name(self, key: str) -> None:
        """兼容阶段1调用方：按子模块 key 切换页面。"""
        self.show_module(key)

    def refresh(self) -> None:
        """主窗口页面切换时刷新当前子页面。"""
        page = self._pages.get(self._current_key)
        if page is not None and callable(getattr(page, "refresh", None)):
            page.refresh()

    def current_module(self) -> str:
        """返回当前管理子模块 key。"""
        return self._current_key
