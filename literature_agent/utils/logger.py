"""日志工具：统一记录操作日志与异常堆栈到本地文件。

日志文件位于用户数据目录 logs/app.log，按 2MB 轮转、保留 5 份。
控制台默认只显示 INFO 及以上的简洁日志；以 ``python main.py -v``
（或设置环境变量 LIT_AGENT_VERBOSE=1）启动时，控制台显示 DEBUG 全量
详细日志（含时间、代码位置），便于排查解析失败等问题根由。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".literature_agent", "logs")
_LOGGER_NAME = "literature_agent"
# 控制台详细模式开关：1/true/debug/on 视为开启（不区分大小写）
_VERBOSE_ENV = "LIT_AGENT_VERBOSE"
_max_bytes = 2 * 1024 * 1024
_backup_count = 5

_logger_cache = None


def console_verbose_enabled() -> bool:
    """读取控制台详细日志开关（环境变量 LIT_AGENT_VERBOSE）。

    Returns:
        True 表示控制台输出 DEBUG 级详细日志。
    """
    return str(os.environ.get(_VERBOSE_ENV, "")).strip().lower() in (
        "1", "true", "debug", "on", "yes")


def get_logger(log_dir: str = None) -> logging.Logger:
    """获取全局唯一 logger（带文件轮转 + 控制台输出）。

    Args:
        log_dir: 可选的日志目录，不传使用默认 ~/.literature_agent/logs。
    Returns:
        logging.Logger 单例。
    """
    global _logger_cache
    if _logger_cache is not None:
        return _logger_cache

    target_dir = log_dir or _DEFAULT_LOG_DIR
    os.makedirs(target_dir, exist_ok=True)
    log_path = os.path.join(target_dir, "app.log")

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        file_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(message)s"
        )
        file_handler = RotatingFileHandler(
            log_path, maxBytes=_max_bytes, backupCount=_backup_count, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_fmt)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        if console_verbose_enabled():
            # 详细模式：与文件一致的全量格式，排查时可直接定位代码位置
            console_handler.setLevel(logging.DEBUG)
            console_handler.setFormatter(file_fmt)
        else:
            # 默认模式：去掉 INFO/DEBUG 技术术语，改为用户友好的中文消息
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(
                logging.Formatter("%(message)s")
            )
        logger.addHandler(console_handler)

    _logger_cache = logger
    return logger


def log_exception(exception: Exception, context: str = "") -> None:
    """统一记录异常堆栈。

    Args:
        exception: 捕获到的异常对象。
        context: 异常发生的业务上下文描述。
    """
    logger = get_logger()
    if context:
        logger.error("%s: %s", context, exception, exc_info=True)
    else:
        logger.error("%s", exception, exc_info=True)
