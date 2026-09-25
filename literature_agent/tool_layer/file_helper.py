"""文件操作工具：校验、哈希、安全复制/移动、磁盘占用、路径安全与相对路径转换。

本模块不访问数据库；基准目录由 business 层从 system_config 读取后传入。
"""
import hashlib
import os
import shutil

from config import constants as C
from utils.exceptions import (
    FileInvalidError, LiteratureFileNotFoundError, PermissionDeniedError,
    StorageNotEnoughError,
)
from utils.logger import get_logger

logger = get_logger()

# 各存储区的默认基准目录（system_config 缺省时使用）
_DEFAULT_BASE_DIRS = {
    "literature": os.path.join(
        os.path.expanduser("~"), ".literature_agent", "data", "literature"
    ),
    "report": os.path.join(
        os.path.expanduser("~"), ".literature_agent", "data", "report"
    ),
    "note": os.path.join(
        os.path.expanduser("~"), ".literature_agent", "data", "note"
    ),
    "backup": os.path.join(
        os.path.expanduser("~"), ".literature_agent", "backup"
    ),
}

_HASH_CHUNK_SIZE = 1024 * 1024  # 1MB 分块读取，避免大文件占满内存


def validate_file(file_path: str) -> tuple:
    """校验文件存在性、后缀、大小与可读性。

    Args:
        file_path: 文件绝对路径。
    Returns:
        (是否合法, 说明信息)。
    """
    if not file_path:
        return False, "文件路径为空"
    if not os.path.exists(file_path):
        return False, "文件不存在"
    if not os.path.isfile(file_path):
        return False, "路径不是有效文件"
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix not in C.SUPPORTED_SUFFIX:
        return False, f"不支持的文件格式：{suffix}（仅支持 PDF/TXT/DOCX/DOC）"
    if os.path.getsize(file_path) <= 0:
        return False, "文件大小为 0，文件可能已损坏"
    if not os.access(file_path, os.R_OK):
        return False, "文件无读取权限"
    return True, "校验通过"


def calc_file_hash(file_path: str) -> str:
    """计算文件 SHA256 摘要（分块读取）。

    Args:
        file_path: 文件绝对路径。
    Returns:
        64 位十六进制摘要字符串。
    Raises:
        LiteratureFileNotFoundError: 文件不存在。
    """
    if not os.path.isfile(file_path):
        raise LiteratureFileNotFoundError(f"文件不存在：{file_path}")
    sha = hashlib.sha256()
    with open(file_path, "rb") as fp:
        for chunk in iter(lambda: fp.read(_HASH_CHUNK_SIZE), b""):
            sha.update(chunk)
    return sha.hexdigest()


def sanitize_path(path: str, base_dir: str = None) -> str:
    """路径安全校验，禁止目录穿越。

    Args:
        path: 待校验路径（相对或绝对）。
        base_dir: 提供相对路径时的基准目录；绝对路径做规范化即可。
    Returns:
        规范化后的绝对路径。
    Raises:
        FileInvalidError: 路径为空或解析后逃逸出基准目录。
    """
    if not path:
        raise FileInvalidError("路径为空")
    if base_dir:
        base_real = os.path.realpath(base_dir)
        target = os.path.realpath(
            path if os.path.isabs(path) else os.path.join(base_real, path)
        )
        if target != base_real and not target.startswith(base_real + os.sep):
            raise FileInvalidError(f"非法路径（疑似目录穿越）：{path}")
        return target
    return os.path.realpath(path)


def ensure_dir(dir_path: str) -> str:
    """确保目录存在。

    Args:
        dir_path: 目录绝对路径。
    Returns:
        规范化后的目录路径。
    Raises:
        PermissionDeniedError: 无法创建目录。
    """
    try:
        os.makedirs(dir_path, exist_ok=True)
        return os.path.realpath(dir_path)
    except OSError as exc:
        raise PermissionDeniedError(f"无法创建目录 {dir_path}：{exc}") from exc


def safe_copy(src: str, dst: str) -> None:
    """安全复制文件（自动建目录、防穿越、校验磁盘空间）。

    Args:
        src: 源文件绝对路径。
        dst: 目标文件绝对路径。
    Raises:
        LiteratureFileNotFoundError: 源文件不存在。
        StorageNotEnoughError: 目标盘剩余空间不足。
    """
    if not os.path.isfile(src):
        raise LiteratureFileNotFoundError(f"源文件不存在：{src}")
    dst_dir = sanitize_path(os.path.dirname(dst))
    ensure_dir(dst_dir)
    file_size = os.path.getsize(src)
    usage = get_disk_usage(dst_dir)
    if usage["free"] < file_size:
        raise StorageNotEnoughError(
            f"磁盘剩余空间不足（需要 {file_size} 字节，可用 {usage['free']} 字节）"
        )
    try:
        shutil.copy2(src, dst)
        logger.info("文件已复制：%s -> %s", src, dst)
    except OSError as exc:
        raise PermissionDeniedError(f"文件复制失败：{exc}") from exc


def safe_move(src: str, dst: str) -> None:
    """安全移动文件（跨盘自动退化为复制+删除）。

    Args:
        src: 源文件绝对路径。
        dst: 目标文件绝对路径。
    """
    if not os.path.isfile(src):
        raise LiteratureFileNotFoundError(f"源文件不存在：{src}")
    dst_dir = sanitize_path(os.path.dirname(dst))
    ensure_dir(dst_dir)
    try:
        shutil.move(src, dst)
        logger.info("文件已移动：%s -> %s", src, dst)
    except OSError as exc:
        raise PermissionDeniedError(f"文件移动失败：{exc}") from exc


def get_disk_usage(path: str = None) -> dict:
    """获取指定路径所在磁盘的占用情况。

    Args:
        path: 任意路径（不存在时取其父目录，最终回退到用户主目录）。
    Returns:
        {"total": 字节, "used": 字节, "free": 字节}。
    """
    target = path or os.path.expanduser("~")
    while target and not os.path.exists(target):
        target = os.path.dirname(target)
    usage = shutil.disk_usage(target or os.path.expanduser("~"))
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def get_default_base_dir(path_key: str) -> str:
    """返回存储区默认基准目录（不读数据库）。

    Args:
        path_key: literature/report/note/backup。
    Returns:
        默认目录绝对路径。
    """
    return _DEFAULT_BASE_DIRS.get(path_key, _DEFAULT_BASE_DIRS["literature"])


def to_relative(abs_path: str, base_dir: str) -> str:
    """绝对路径转库内相对路径（入库前调用）。

    Args:
        abs_path: 文件绝对路径。
        base_dir: 存储区基准目录。
    Returns:
        相对路径（如 "202609/xxx.pdf"）。
    """
    base_real = ensure_dir(base_dir)
    target_real = sanitize_path(abs_path, base_real)
    return os.path.relpath(target_real, base_real).replace(os.sep, "/")


def resolve_path(rel_path: str, base_dir: str = None,
                 path_key: str = "literature") -> str:
    """相对路径解析为安全的绝对路径（读取文件前调用）。

    Args:
        rel_path: 库内相对路径。
        base_dir: business 层从 system_config 读取的基准目录；
                  不传时使用默认目录。
        path_key: literature/report/note/backup。
    Returns:
        校验后的绝对路径。
    """
    base = base_dir or get_default_base_dir(path_key)
    base_real = ensure_dir(base)
    return sanitize_path(rel_path, base_real)


def unique_dst_path(dst_dir: str, filename: str) -> str:
    """目标文件已存在时自动追加序号，返回不冲突的目标路径。

    Args:
        dst_dir: 目标目录。
        filename: 原始文件名。
    Returns:
        不与现有文件冲突的绝对路径。
    """
    base_dir = ensure_dir(dst_dir)
    stem, suffix = os.path.splitext(filename)
    candidate = os.path.join(base_dir, filename)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(base_dir, f"{stem}_{index}{suffix}")
        index += 1
    return candidate
