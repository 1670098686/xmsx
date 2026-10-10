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

# 详细日志开关必须在任何项目模块 import 之前读取：各模块导入时即会
# 初始化全局 logger 单例。支持 -v/--verbose，等价于 LIT_AGENT_VERBOSE=1。
# 开启后启动窗口（命令行）实时输出 DEBUG 全量日志，便于排查解析问题。
if "--verbose" in sys.argv or "-v" in sys.argv:
    os.environ["LIT_AGENT_VERBOSE"] = "1"
    sys.argv = [arg for arg in sys.argv if arg not in ("--verbose", "-v")]

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from config import constants as C
from config.global_config import FONT_BODY
from config.settings import GlobalState, load_settings
from data_layer.db_connect import DatabaseManager
from data_layer.db_init import init_db
from ui.main_window import MainWindow, install_activation_click_filter
from ui.theme_manager import ThemeManager
from utils.logger import console_verbose_enabled, get_logger


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
    logger.info("=" * 50)
    logger.info("文献阅读整理Agent 启动中…")
    if console_verbose_enabled():
        logger.info("（详细日志模式已开启）")

    start_at = time.perf_counter()

    # 1. 数据库初始化（首次启动自动建 9 张表 + 种子数据）
    logger.info("[1/5] 正在初始化本地数据库…")
    manager = bootstrap_database()
    logger.info("      ✓ 数据库就绪（9 张数据表已加载）")

    # 2. 加载系统配置并恢复全局状态
    logger.info("[2/5] 正在加载系统配置…")
    from business.system_service import SystemService
    system_service = SystemService()
    system_service.ensure_default_config()
    # 预创建四类存储目录，避免"目录尚未创建"与实际打开为空目录的展示矛盾
    dirs_ok = system_service.ensure_storage_dirs()
    settings = load_settings()
    theme_name = settings.get(C.CFG_UI_STYLE, C.THEME_LIGHT_NAME)
    GlobalState().current_theme = theme_name
    logger.info("      ✓ 系统配置就绪")
    logger.info("      • 界面主题：%s", "深色" if theme_name == C.THEME_DARK_NAME else "浅色")
    if dirs_ok:
        logger.info("      • 已检查 %d 个存储目录", dirs_ok)

    # 2.1 打印 AI 模型就绪状态（如果有配置）
    try:
        model = system_service.get_current_ai_model()
        if model and model.get("name"):
            channel = model.get("file_channel", C.AI_FILE_CHANNEL_AUTO)
            channel_label = {
                C.AI_FILE_CHANNEL_FILE_ID: "file_id 文件托管通道",
                C.AI_FILE_CHANNEL_FILE_DATA: "PDF Base64 内联通道",
                C.AI_FILE_CHANNEL_NONE: "仅全文文本通道",
            }.get(channel, "自动识别通道")
            logger.info("      • AI 模型已就绪：%s（%s）", model["name"], channel_label)
        else:
            logger.info("      • 提示：尚未配置 AI 模型，可在「管理中心 → AI 模型」中设置以启用智能解析")
    except Exception:
        logger.info("      • AI 模型：暂未配置")

    # 3. 创建 Qt 应用
    logger.info("[3/5] 正在创建应用窗口…")
    app = QApplication(sys.argv)
    app.setApplicationName("文献阅读整理Agent")
    font = QFont(FONT_BODY[0], FONT_BODY[1], FONT_BODY[2])
    app.setFont(font)

    # 4. 应用上次保存的主题
    ThemeManager().apply_theme(app, theme_name)

    # 4.1 Windows：点击非活动窗口只激活窗口、不触发被点中的控件，
    #     避免从其他软件切回时误触侧边栏导致页面/管理子页被切换
    install_activation_click_filter(app)

    # 5. 创建并显示主窗口
    logger.info("[4/5] 正在创建主窗口…")
    window = MainWindow()
    window.show()

    elapsed_ms = int((time.perf_counter() - start_at) * 1000)
    logger.info("[5/5] 启动完成！")
    logger.info("      • 本次启动耗时：%d ms", elapsed_ms)
    logger.info("      • 主窗口已显示，等待您的操作")
    logger.info("=" * 50)

    exit_code = app.exec_()

    # 6. 退出前强制落库防抖笔记，再关闭数据库连接
    logger.info("文献阅读整理Agent 正在退出…")
    try:
        from business.note_manage import NoteManageService
        NoteManageService().flush_all()
        logger.info("      ✓ 笔记数据已保存")
    except Exception as exc:
        logger.error("      ✗ 退出前笔记落库失败：%s", exc)
    manager.close()
    logger.info("      ✓ 数据库连接已关闭")
    logger.info("退出完成（code=%s），再见！", exit_code)
    logger.info("=" * 50)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
