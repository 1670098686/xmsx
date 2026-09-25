"""文献阅读整理Agent 程序入口。

启动顺序：初始化单例数据库 → 自动建表/种子数据 → 加载系统配置
        → 应用持久化主题 → 创建主窗口 → 事件循环。
"""
import os
import sys
import time

# 确保无论从哪个工作目录启动，项目根目录都在 sys.path 中
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 项目内置第三方库目录（PDF 渲染用 PyMuPDF，随项目分发免全局安装）
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, ".vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from config import constants as C
from config.global_config import FONT_BODY
from config.settings import GlobalState, load_settings
from data_layer.db_connect import DatabaseManager
from data_layer.db_init import init_db
from ui.main_window import MainWindow
from ui.theme_manager import ThemeManager
from utils.logger import get_logger


def bootstrap_database() -> DatabaseManager:
    """初始化数据库单例并执行首次建表。"""
    manager = DatabaseManager()
    init_db(manager)
    return manager


def main() -> int:
    """应用主入口。

    Returns:
        进程退出码。
    """
    logger = get_logger()
    start_at = time.perf_counter()

    # 1. 数据库初始化（首次启动自动建 9 张表 + 种子数据）
    manager = bootstrap_database()

    # 2. 加载系统配置并恢复全局状态
    from business.system_service import SystemService
    system_service = SystemService()
    system_service.ensure_default_config()
    # 预创建四类存储目录，避免“目录尚未创建”与实际打开为空目录的展示矛盾
    system_service.ensure_storage_dirs()
    settings = load_settings()
    theme_name = settings.get(C.CFG_UI_STYLE, C.THEME_LIGHT_NAME)
    GlobalState().current_theme = theme_name

    # 3. 创建 Qt 应用
    app = QApplication(sys.argv)
    app.setApplicationName("文献阅读整理Agent")
    font = QFont(FONT_BODY[0], FONT_BODY[1], FONT_BODY[2])
    app.setFont(font)

    # 4. 应用上次保存的主题
    ThemeManager().apply_theme(app, theme_name)

    # 5. 创建并显示主窗口
    window = MainWindow()
    window.show()
    elapsed_ms = int((time.perf_counter() - start_at) * 1000)
    logger.info("应用启动完成，耗时 %s ms（验收基线 3000ms）", elapsed_ms)

    exit_code = app.exec_()

    # 6. 退出前强制落库防抖笔记，再关闭数据库连接
    try:
        from business.note_manage import NoteManageService
        NoteManageService().flush_all()
    except Exception as exc:
        logger.error("退出前笔记落库失败：%s", exc, exc_info=True)
    manager.close()
    logger.info("应用退出，code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
