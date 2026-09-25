"""通用辅助函数：时间格式化、参数校验、数据分片等纯工具函数。"""
from datetime import datetime
from typing import Any, Iterator


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回当前时间的格式化字符串。

    Args:
        fmt: strftime 格式串。
    Returns:
        格式化后的时间字符串。
    """
    return datetime.now().strftime(fmt)


def format_time(dt_value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """将 datetime 对象格式化为字符串。

    Args:
        dt_value: datetime 实例；None 或非 datetime 返回空串。
        fmt: strftime 格式串。
    Returns:
        格式化字符串，无法格式化时返回空字符串。
    """
    if isinstance(dt_value, datetime):
        return dt_value.strftime(fmt)
    return ""


def is_blank(text: Any) -> bool:
    """判断字符串是否为空白（None、空串、纯空格）。"""
    return text is None or (isinstance(text, str) and text.strip() == "")


def safe_int(value: Any, default: int = 0) -> int:
    """安全转换为 int，失败时返回默认值，不抛异常。

    Args:
        value: 任意待转换值。
        default: 转换失败时的兜底值。
    Returns:
        转换后的整数。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def chunk_list(items: list, size: int) -> Iterator[list]:
    """将列表按固定大小分片（用于批量处理）。

    Args:
        items: 原始列表。
        size: 每片大小，必须大于 0。
    Yields:
        分片后的子列表。
    Raises:
        ValueError: size <= 0 时抛出。
    """
    if size <= 0:
        raise ValueError("chunk size 必须大于 0")
    for i in range(0, len(items), size):
        yield items[i:i + size]


def format_file_size(num_bytes: int) -> str:
    """将字节数格式化为人类可读的文件大小。

    Args:
        num_bytes: 字节数。
    Returns:
        形如 "2.4 MB" 的字符串。
    """
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
