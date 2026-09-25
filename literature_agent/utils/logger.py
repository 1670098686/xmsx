"""日志工具：统一记录操作日志与异常堆栈到本地文件。

日志文件位于用户数据目录 logs/app.log，按 2MB 轮转、保留 5 份。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".literature_agent", "logs")
_LOGGER_NAME = "literature_agent"
_max_bytes = 2 * 1024 * 1024
_backup_count = 5

_logger_cache = None


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
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(
            logging.Formatter("%(levelname)-7s | %(message)s")
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
