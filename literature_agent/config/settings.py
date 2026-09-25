"""系统设置读写与全局 UI 状态。

- GlobalState：当前主题、当前选中文献等全局唯一状态，各页面禁止私存副本；
- load_settings / save_ui_style：从 system_config 表读写持久化配置。
"""
from config import constants as C
from utils.logger import get_logger


class GlobalState:
    """全局运行时状态（单例，存放不直接落库的临时会话状态）。"""

    _instance = None

    def __new__(cls):
        """单例实例化入口。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._reset()
        return cls._instance

    def _reset(self) -> None:
        """初始化默认状态。"""
        self.current_theme: str = C.THEME_LIGHT_NAME
        self.selected_lit_id: int = 0
        self.selected_category_id: int = 0
        self.selected_tag_ids: list = []

    def set_selected_lit(self, lit_id: int) -> None:
        """设置当前选中文献 id（全局唯一）。"""
        self.selected_lit_id = int(lit_id or 0)


def load_settings() -> dict:
    """启动时从 system_config 表加载全部配置。

    Returns:
        {config_key: config_value} 字典；数据库不可用时返回空字典并记日志。
    """
    try:
        # 延迟导入避免 config → data_layer 的顶层循环依赖
        from data_layer.dao.config_dao import SystemConfigDao
        return SystemConfigDao().get_all()
    except Exception as exc:  # 数据库尚未初始化等场景兜底
        get_logger().warning("加载系统配置失败，使用默认值: %s", exc)
        return {}


def get_setting(config_key: str, default: str = "") -> str:
    """读取单个系统配置。"""
    try:
        from data_layer.dao.config_dao import SystemConfigDao
        value = SystemConfigDao().get(config_key)
        return value if value is not None else default
    except Exception as exc:
        get_logger().warning("读取配置 %s 失败: %s", config_key, exc)
        return default


def save_setting(config_key: str, config_value: str) -> bool:
    """写入单个系统配置。

    Returns:
        True 表示成功，False 表示失败（已记日志，不向 UI 抛堆栈）。
    """
    try:
        from data_layer.dao.config_dao import SystemConfigDao
        SystemConfigDao().set(config_key, config_value)
        return True
    except Exception as exc:
        get_logger().error("保存配置 %s 失败: %s", config_key, exc, exc_info=True)
        return False


def save_ui_style(theme_name: str) -> bool:
    """持久化当前界面主题。"""
    return save_setting(C.CFG_UI_STYLE, theme_name)
