"""文献导入业务：校验 → hash 去重 → 文本提取 → 复制入库 → 写日志。"""
import os

from config import constants as C
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.db_connect import DatabaseManager
from tool_layer import file_helper, file_parser
from utils.logger import get_logger

from business.service_common import (
    exception_to_code, get_base_dir, notify_file_will_delete,
    write_operation_log,
)

logger = get_logger()

# 重复文件处理策略
DUP_SKIP = "skip"
DUP_OVERWRITE = "overwrite"


class LiteratureImportService:
    """文献导入业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.lit_dao = LiteratureInfoDao()

    # ================= 单篇导入 =================

    def import_single(self, file_path: str,
                      duplicate_policy: str = DUP_SKIP) -> tuple:
        """导入单篇文献。

        Args:
            file_path: 文献文件绝对路径。
            duplicate_policy: skip=重复时返回 CODE_DUPLICATE；
                              overwrite=删除旧记录后重新导入。
        Returns:
            (code, data, msg)：成功时 data 为文献字典；
            重复时 data 含 existing 旧记录。
        """
        filename = os.path.basename(file_path or "")
        try:
            # 1. 文件校验
            valid, reason = file_helper.validate_file(file_path)
            if not valid:
                code = (
                    C.CODE_FILE_NOT_FOUND
                    if "不存在" in reason else C.CODE_FILE_INVALID
                )
                write_operation_log(
                    C.OP_IMPORT, f"导入失败[{filename}]：{reason}", status=C.OP_STATUS_FAILED
                )
                return code, None, reason

            safe_path = file_helper.sanitize_path(file_path)
            file_size = os.path.getsize(safe_path)

            # 2. hash 去重
            file_hash = file_helper.calc_file_hash(safe_path)
            existing = self.lit_dao.select_by_hash(file_hash)
            old_rel_path = ""
            if existing:
                old_rel_path = existing.get("file_path") or ""
                if duplicate_policy == DUP_OVERWRITE:
                    # 覆盖：删除旧记录（报告/笔记级联清理）后继续导入
                    with DatabaseManager().write_lock:
                        self.lit_dao.delete_by_id(existing["id"])
                    logger.info("重复文献执行覆盖导入：id=%s", existing["id"])
                else:
                    return (
                        C.CODE_DUPLICATE,
                        {"existing": existing},
                        f"文件已存在（{existing.get('literature_title', filename)}）",
                    )

            # 3. 文本提取（同时验证文件未损坏/有文本层）
            text, meta, lit_type = file_parser.auto_extract(safe_path)

            # 4. 复制到受管存储目录，保存相对路径
            base_dir = get_base_dir("literature")
            dst_path = file_helper.unique_dst_path(base_dir, filename)
            file_helper.safe_copy(safe_path, dst_path)
            rel_path = file_helper.to_relative(dst_path, base_dir)

            # 5. 入库
            lit_info = {
                "literature_title": self._pick_title(meta, filename),
                "literature_author": meta.get("author", "") or "未知",
                "publish_time": meta.get("publish_time", "") or "",
                "journal_source": meta.get("journal_source", "") or "",
                "literature_type": lit_type,
                "file_path": rel_path,
                "file_size": file_size,
                "file_hash": file_hash,
                "category_id": 0,
                "is_parsed": C.PARSE_NOT_STARTED,
            }
            lit_id = self.lit_dao.insert(lit_info)
            lit_info["id"] = lit_id
            # 附带提取文本长度，供业务判断（不落库）
            lit_info["text_length"] = len(text)

            write_operation_log(
                C.OP_IMPORT, f"导入文献：{lit_info['literature_title']}", lit_id
            )
            # 覆盖导入成功后清理旧原件（新原件已用不冲突文件名落盘）；
            # 清理失败不影响导入结果，仅记录警告避免产生孤儿文件无感知
            if old_rel_path and old_rel_path != rel_path:
                try:
                    old_abs = file_helper.resolve_path(
                        old_rel_path, base_dir, "literature"
                    )
                    # 先释放 UI 预览器对旧原件的句柄，避免 WinError 32
                    notify_file_will_delete(old_abs)
                    file_helper.safe_delete_file(old_abs, base_dir)
                except Exception as exc:
                    logger.warning("覆盖导入后旧原件清理失败：%s", exc)
            return C.CODE_SUCCESS, lit_info, "导入成功"

        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("导入文献失败 %s：%s", filename, msg, exc_info=True)
            write_operation_log(
                C.OP_IMPORT, f"导入失败[{filename}]：{msg}", status=C.OP_STATUS_FAILED
            )
            return code, None, msg

    @staticmethod
    def _pick_title(meta: dict, filename: str) -> str:
        """从元数据中挑选标题，缺失时退回文件名。"""
        title = (meta.get("title") or "").strip()
        return title or os.path.splitext(filename)[0]

    # ================= 批量导入 =================

    def import_batch(self, file_paths: list, progress_callback=None,
                     duplicate_policy: str = DUP_SKIP) -> dict:
        """批量导入。

        Args:
            file_paths: 文件绝对路径列表。
            progress_callback: 可选回调 callback(current, total, path, code, msg)。
            duplicate_policy: skip/overwrite，作用于每一篇。
        Returns:
            {"success": [lit_dict,...], "failed": [{"path","msg"},...],
             "duplicate": [{"path","existing"},...]}
        """
        result = {"success": [], "failed": [], "duplicate": []}
        total = len(file_paths)
        for index, path in enumerate(file_paths):
            code, data, msg = self.import_single(path, duplicate_policy)
            if code == C.CODE_SUCCESS:
                result["success"].append(data)
            elif code == C.CODE_DUPLICATE:
                result["duplicate"].append({
                    "path": path,
                    "existing": (data or {}).get("existing"),
                })
            else:
                result["failed"].append({"path": path, "msg": msg})
            if progress_callback:
                progress_callback(index + 1, total, path, code, msg)
        return result

    # ================= 查询/去重/删除 =================

    def check_duplicate(self, file_path: str):
        """根据 SHA256 判断文件是否已在库。

        Args:
            file_path: 文件绝对路径。
        Returns:
            命中时返回旧文献字典，否则返回 None；文件不可读也返回 None。
        """
        try:
            valid, _ = file_helper.validate_file(file_path)
            if not valid:
                return None
            file_hash = file_helper.calc_file_hash(
                file_helper.sanitize_path(file_path)
            )
            return self.lit_dao.select_by_hash(file_hash)
        except Exception as exc:
            logger.warning("去重检查失败 %s：%s", file_path, exc)
            return None

    def get_literature_list(self) -> list:
        """获取全部文献（导入页表格/解析页下拉使用）。"""
        return self.lit_dao.select_all()

    def get_lit_file_path(self, lit_id: int):
        """把库内相对路径还原为绝对路径。

        Returns:
            (code, abs_path, msg)
        """
        lit = self.lit_dao.select_by_id(lit_id)
        if not lit:
            return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
        base_dir = get_base_dir("literature")
        try:
            abs_path = file_helper.resolve_path(lit["file_path"], base_dir, "literature")
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))
        if not os.path.isfile(abs_path):
            return C.CODE_FILE_NOT_FOUND, abs_path, "文献源文件已被移动或删除"
        return C.CODE_SUCCESS, abs_path, ""

    def delete_literature(self, lit_id: int) -> tuple:
        """删除单篇文献：先删受管原件，再删数据库记录（报告/笔记级联清理）。

        原件被占用等无法删除时中止操作并保留数据库记录，保证库与目录一致，
        用户关闭占用程序后可重试；原件已缺失视为可继续删除记录。

        Returns:
            (code, None, msg)
        """
        try:
            lit = self.lit_dao.select_by_id(lit_id)
            if not lit:
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
            code, msg = self._delete_stored_file(lit)
            if code != C.CODE_SUCCESS:
                return code, None, msg
            with DatabaseManager().write_lock:
                self.lit_dao.delete_by_id(lit_id)
            write_operation_log(
                C.OP_DELETE,
                f"删除文献（含原件）：{lit.get('literature_title', lit_id)}",
                lit_id,
            )
            return C.CODE_SUCCESS, None, "已删除"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    @staticmethod
    def _delete_stored_file(lit: dict) -> tuple:
        """删除文献在受管存储目录中的原件（路径穿越校验，缺失容忍）。

        Returns:
            (code, msg)：code=0 表示文件已删除或本就不存在。
        """
        rel_path = lit.get("file_path") or ""
        if not rel_path:
            return C.CODE_SUCCESS, ""
        try:
            base_dir = get_base_dir("literature")
            abs_path = file_helper.resolve_path(rel_path, base_dir, "literature")
            # 删除前释放 UI 预览器（PyMuPDF）对该文件的句柄，
            # 否则 Windows 上 os.remove 会报 WinError 32
            notify_file_will_delete(abs_path)
            file_helper.safe_delete_file(abs_path, base_dir)
            return C.CODE_SUCCESS, ""
        except Exception as exc:
            msg = getattr(exc, "message", str(exc))
            logger.warning("删除文献原件失败 id=%s：%s", lit.get("id"), msg)
            return exception_to_code(exc), msg

    def clear_all_records(self) -> tuple:
        """清空全部导入记录（高危操作，UI/调用方必须先完成二次确认）。

        先逐篇删除受管目录中的原件：任一文件被占用则中止并保留全部数据库
        记录，避免出现“记录没了、原件还在”的不一致；全部原件清理完成后
        再清空数据库记录（解析报告、笔记随外键级联清除）。

        Returns:
            (code, deleted_count, msg)
        """
        try:
            lit_list = self.lit_dao.select_all()
            for lit in lit_list:
                code, msg = self._delete_stored_file(lit)
                if code != C.CODE_SUCCESS:
                    return code, 0, f"部分原件无法删除，已中止清空：{msg}"
            with DatabaseManager().write_lock:
                count = self.lit_dao.delete_all()
            write_operation_log(
                C.OP_DELETE, f"清空全部文献记录及原件（共 {count} 条）"
            )
            return C.CODE_SUCCESS, count, "清空成功"
        except Exception as exc:
            return exception_to_code(exc), 0, getattr(exc, "message", str(exc))
