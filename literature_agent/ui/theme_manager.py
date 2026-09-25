"""浅色/深色主题切换管理器。

QSS 模板位于 resources/qss/{theme}.qss，颜色占位符由 config.global_config
中的主题字典替换，切换时整体重设 qApp.styleSheet。
"""
import os
import threading

from PyQt5.QtWidgets import QApplication

from config.global_config import THEME_MAP, THEME_LIGHT, FONT_FAMILY
from config import constants as C
from config.settings import save_ui_style
from utils.logger import get_logger

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QSS_DIR = os.path.join(_PROJECT_ROOT, "resources", "qss")


class ThemeManager:
    """主题管理器（单例）。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例实例化入口。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._current = C.THEME_LIGHT_NAME
                    cls._instance = instance
        return cls._instance

    def _build_qss(self, theme_name: str) -> str:
        """读取 QSS 模板并用主题变量替换占位符。

        Args:
            theme_name: light / dark。
        Returns:
            替换完成的完整 QSS 字符串。
        """
        qss_path = os.path.join(_QSS_DIR, f"{theme_name}.qss")
        with open(qss_path, "r", encoding="utf-8") as fp:
            template = fp.read()
        variables = dict(THEME_MAP.get(theme_name, THEME_LIGHT))
        variables["font_family"] = FONT_FAMILY
        return template % variables

    def apply_theme(self, app: QApplication, theme_name: str) -> bool:
        """应用指定主题。

        Args:
            app: QApplication 实例。
            theme_name: light / dark。
        Returns:
            True 成功，False 失败（已记日志，保持原主题）。
        """
        if theme_name not in THEME_MAP:
            get_logger().warning("未知主题 %s，回退浅色", theme_name)
            theme_name = C.THEME_LIGHT_NAME
        try:
            app.setStyleSheet(self._build_qss(theme_name))
            self._current = theme_name
            save_ui_style(theme_name)
            get_logger().info("主题已切换: %s", theme_name)
            return True
        except Exception as exc:
            get_logger().error("应用主题失败 %s: %s", theme_name, exc, exc_info=True)
            return False

    def toggle_theme(self, app: QApplication) -> str:
        """在浅色/深色之间切换。

        Returns:
            切换后的主题名。
        """
        new_theme = (
            C.THEME_DARK_NAME if self._current == C.THEME_LIGHT_NAME
            else C.THEME_LIGHT_NAME
        )
        self.apply_theme(app, new_theme)
        return new_theme

    def current_theme(self) -> str:
        """返回当前主题名。"""
        return self._current
