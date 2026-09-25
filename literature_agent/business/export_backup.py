"""导出与备份业务。

阶段 2：解析报告单篇/批量导出（word/txt/pdf）。
阶段 3：全量/增量备份（zip 打包 DB 快照 + 文献源文件 + manifest）、
        从备份恢复（sqlite backup API 在线覆盖，重启后完全生效）、备份记录管理。
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile

from config import constants as C
from data_layer.dao.backup_dao import BackupRecordDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.db_connect import DatabaseManager
from tool_layer import export_generator, file_helper
from utils.common_helper import format_file_size, now_str
from utils.logger import get_logger

from business.literature_parse import LiteratureParseService
from business.service_common import exception_to_code, get_base_dir, write_operation_log

logger = get_logger()

_BACKUP_MANIFEST = "manifest.json"
_BACKUP_DB_ENTRY = "database/literature_agent.db"
_BACKUP_FILE_PREFIX = "literature/"
_BACKUP_VERSION = 1


class ExportBackupService:
    """导出与备份业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.lit_dao = LiteratureInfoDao()
        self.backup_dao = BackupRecordDao()
        self.parse_service = LiteratureParseService()

    def export_report(self, lit_id: int, fmt: str, output_path: str) -> tuple:
        """导出单篇文献的结构化解析报告。

        Args:
            lit_id: 文献 id。
            fmt: word/txt/pdf。
            output_path: 目标文件绝对路径。
        Returns:
            (code, output_path, msg)
        """
        try:
            report_data = self.parse_service.get_report(lit_id)
            if not report_data:
                return C.CODE_PARSE_FAILED, None, "该文献尚未解析，请先解析后再导出"
            export_generator.export_report(report_data, output_path, fmt)
            write_operation_log(
                C.OP_EXPORT,
                f"导出解析报告：{report_data.get('literature_title', lit_id)}（{fmt}）",
                lit_id,
            )
            return C.CODE_SUCCESS, output_path, "导出成功"
        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("导出报告失败 lit_id=%s：%s", lit_id, msg, exc_info=True)
            write_operation_log(
                C.OP_EXPORT, f"导出报告失败[id={lit_id}]：{msg}",
                lit_id, C.OP_STATUS_FAILED,
            )
            return code, None, msg

    def export_batch(self, lit_ids: list, fmt: str, output_dir: str) -> dict:
        """批量导出解析报告。

        Args:
            lit_ids: 文献 id 列表。
            fmt: word/txt/pdf。
            output_dir: 输出目录。
        Returns:
            {"success": [path,...], "failed": [{"lit_id","msg"},...]}
        """
        result = {"success": [], "failed": []}
        suffix = self._fmt_suffix(fmt)
        os.makedirs(output_dir, exist_ok=True)
        for lit_id in lit_ids:
            lit = self.lit_dao.select_by_id(lit_id)
            if not lit:
                result["failed"].append({"lit_id": lit_id, "msg": "文献不存在"})
                continue
            filename = self._safe_filename(
                f"{lit.get('literature_title', lit_id)}_{lit_id}{suffix}"
            )
            output_path = os.path.join(output_dir, filename)
            code, path, msg = self.export_report(lit_id, fmt, output_path)
            if code == C.CODE_SUCCESS:
                result["success"].append(path)
            else:
                result["failed"].append({"lit_id": lit_id, "msg": msg})
        return result

    @staticmethod
    def _fmt_suffix(fmt: str) -> str:
        """格式名转文件后缀。"""
        mapping = {"word": ".docx", "txt": ".txt", "pdf": ".pdf"}
        return mapping.get((fmt or "").lower(), ".txt")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """清洗文件名中的非法字符。"""
        return re.sub(r'[\\/:*?"<>|]', "_", filename).strip() or "report.txt"

    # ================= 备份 =================

    def full_backup(self, output_path: str = None,
                    progress_callback=None) -> tuple:
        """全量备份：DB 快照 + 全部文献源文件打包为 zip。

        Args:
            output_path: 可选 zip 绝对路径；不传自动放入备份目录。
            progress_callback: 可选回调 (percent:int, message:str)。
        Returns:
            (code, backup_record:dict, msg)
        """
        return self._do_backup(
            C.BACKUP_TYPE_FULL, output_path, progress_callback
        )

    def incremental_backup(self, output_path: str = None,
                           progress_callback=None) -> tuple:
        """增量备份：DB 快照 + 相对上次备份新增/变化的文献文件。"""
        return self._do_backup(
            C.BACKUP_TYPE_INCREMENTAL, output_path, progress_callback
        )

    def _do_backup(self, backup_type: str, output_path: str,
                   progress_callback) -> tuple:
        """备份主流程（全量/增量共用）。"""
        def emit(percent, message):
            """安全转发进度回调（未提供回调时忽略）。"""
            if progress_callback:
                progress_callback(percent, message)

        zip_path = None
        record_id = None
        try:
            emit(5, "准备备份目录")
            backup_dir = get_base_dir("backup")
            file_helper.ensure_dir(backup_dir)
            timestamp = now_str("%Y%m%d_%H%M%S")
            if not output_path:
                output_path = os.path.join(
                    backup_dir, f"backup_{timestamp}_{backup_type}.zip"
                )
            output_path = file_helper.sanitize_path(output_path)
            zip_path = output_path

            # 1. 收集要打包的文献文件
            emit(15, "收集文献清单")
            all_files = self._collect_lit_files()
            if backup_type == C.BACKUP_TYPE_FULL:
                files_to_pack = all_files
            else:
                files_to_pack = self._diff_incremental(all_files)

            # 2. 备份记录先落库（尺寸暂为 0），保证随后的数据库快照包含本记录，
            #    这样“恢复该备份”后备份列表不会丢失该备份
            name = os.path.basename(zip_path)
            record_id = self.backup_dao.insert({
                "backup_name": name,
                "backup_path": zip_path,
                "backup_size": 0,
                "backup_type": backup_type,
                "content_scope": "all",
                "backup_status": C.OP_STATUS_SUCCESS,
            })

            # 3. 快照数据库到临时文件
            emit(40, "快照数据库")
            tmp_dir = tempfile.mkdtemp(prefix="lit_backup_")
            db_snapshot = os.path.join(tmp_dir, "literature_agent.db")
            self._snapshot_database(db_snapshot)

            # 4. 写 zip
            emit(55, f"写入备份文件（{len(files_to_pack)} 个文献文件）")
            manifest = {
                "version": _BACKUP_VERSION,
                "backup_type": backup_type,
                "create_time": now_str(),
                "db_entry": _BACKUP_DB_ENTRY,
                "file_prefix": _BACKUP_FILE_PREFIX,
                "file_count": len(files_to_pack),
                # files 为该时点累积清单（增量 zip 只含变化文件，
                # 下一次增量据此比对，避免旧文件被重复判定为新增）
                "files": [
                    {"path": rel, "size": size, "sha256": digest}
                    for rel, _abs, size, digest in all_files
                ],
            }
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_snapshot, _BACKUP_DB_ENTRY)
                total = len(files_to_pack) or 1
                for index, (rel, abs_path, _size, _digest) in enumerate(files_to_pack):
                    zf.write(abs_path, _BACKUP_FILE_PREFIX + rel)
                    if progress_callback:
                        percent = 60 + int(30 * (index + 1) / total)
                        progress_callback(percent, f"打包文献 {index + 1}/{total}")
                zf.writestr(
                    _BACKUP_MANIFEST,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )

            # 5. 回填实际文件大小
            emit(90, "写入备份记录")
            backup_size = os.path.getsize(zip_path)
            self.backup_dao.update_size(record_id, backup_size)

            # 6. 用含最终备份记录（含正确尺寸）的快照替换 zip 内 db 条目，
            #    保证恢复后该备份记录完整存在；随后回填重写产生的尺寸微差
            db_snapshot_final = os.path.join(tmp_dir, "literature_agent_final.db")
            self._snapshot_database(db_snapshot_final)
            self._replace_zip_entry(zip_path, _BACKUP_DB_ENTRY, db_snapshot_final)
            self.backup_dao.update_size(record_id, os.path.getsize(zip_path))

            write_operation_log(
                C.OP_BACKUP,
                f"{backup_type} 备份成功：{name}（{format_file_size(backup_size)}，"
                f"{len(files_to_pack)} 个文献文件）",
            )
            emit(100, "备份完成")
            return C.CODE_SUCCESS, self.backup_dao.get_by_id(record_id), "备份成功"
        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("备份失败：%s", msg, exc_info=True)
            write_operation_log(C.OP_BACKUP, f"{backup_type} 备份失败：{msg}",
                                status=C.OP_STATUS_FAILED)
            # 失败半成品清理：zip 与预写记录一并回滚
            if record_id is not None:
                try:
                    self.backup_dao.delete_by_id(record_id)
                except Exception:
                    logger.warning("清理失败备份记录失败：id=%s", record_id)
            if zip_path and os.path.isfile(zip_path):
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
            return code, None, f"备份失败：{msg}"

    def restore(self, backup_file_path: str,
                progress_callback=None) -> tuple:
        """从备份 zip 恢复：校验 → 还原文献文件 → 在线覆盖数据库。

        Args:
            backup_file_path: 备份 zip 绝对路径。
            progress_callback: 可选回调 (percent, message)。
        Returns:
            (code, None, msg)
        """
        def emit(percent, message):
            """安全转发进度回调（未提供回调时忽略）。"""
            if progress_callback:
                progress_callback(percent, message)

        tmp_dir = None
        try:
            if not backup_file_path or not os.path.isfile(backup_file_path):
                return C.CODE_FILE_NOT_FOUND, None, "备份文件不存在"
            if not zipfile.is_zipfile(backup_file_path):
                return C.CODE_FILE_INVALID, None, "备份文件已损坏（非有效 zip）"

            emit(10, "校验备份清单")
            with zipfile.ZipFile(backup_file_path, "r") as zf:
                names = zf.namelist()
                if _BACKUP_MANIFEST not in names or _BACKUP_DB_ENTRY not in names:
                    return C.CODE_FILE_INVALID, None, "备份内容不完整（缺少清单或数据库）"
                manifest = json.loads(zf.read(_BACKUP_MANIFEST).decode("utf-8"))
                if manifest.get("version") != _BACKUP_VERSION:
                    return C.CODE_FILE_INVALID, None, "备份版本不受支持"

                emit(30, "校验数据库快照")
                tmp_dir = tempfile.mkdtemp(prefix="lit_restore_")
                db_restore_path = os.path.join(tmp_dir, "literature_agent.db")
                with zf.open(_BACKUP_DB_ENTRY) as src, \
                        open(db_restore_path, "wb") as dst:
                    dst.write(src.read())

                # 打开快照库做完整性与表结构校验
                check_conn = sqlite3.connect(db_restore_path)
                try:
                    integrity = check_conn.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                    if integrity != "ok":
                        return C.CODE_FILE_INVALID, None, "备份数据库完整性校验失败"
                    tables = {
                        row[0] for row in check_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "literature_info" not in tables:
                        return C.CODE_FILE_INVALID, None, "备份数据库缺少核心数据表"
                finally:
                    check_conn.close()

                # 还原文献文件到当前受管目录
                emit(55, "还原文献文件")
                literature_dir = file_helper.ensure_dir(get_base_dir("literature"))
                file_entries = [
                    item for item in names
                    if item.startswith(_BACKUP_FILE_PREFIX) and not item.endswith("/")
                ]
                total = len(file_entries) or 1
                for index, entry in enumerate(file_entries):
                    rel = entry[len(_BACKUP_FILE_PREFIX):]
                    # 防 zip slip：规范化后必须仍在受管目录内
                    target = file_helper.sanitize_path(rel, literature_dir)
                    file_helper.ensure_dir(os.path.dirname(target))
                    with zf.open(entry) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    if progress_callback:
                        percent = 60 + int(25 * (index + 1) / total)
                        progress_callback(percent, f"还原文献 {index + 1}/{total}")

            # 在线覆盖当前数据库（sqlite backup API，无需替换文件）
            emit(88, "覆盖当前数据库")
            self._restore_database(db_restore_path)

            write_operation_log(
                C.OP_RESTORE,
                f"从备份恢复：{os.path.basename(backup_file_path)}，重启应用后完全生效",
            )
            emit(100, "恢复完成")
            return C.CODE_SUCCESS, None, "恢复成功，请重启应用使全部数据生效"
        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("恢复失败：%s", msg, exc_info=True)
            write_operation_log(C.OP_RESTORE, f"恢复失败：{msg}",
                                status=C.OP_STATUS_FAILED)
            return code, None, f"恢复失败：{msg}"

    def get_backup_list(self) -> list:
        """获取全部备份记录（附加 file_exists 与 size_text 供 UI 展示）。"""
        records = self.backup_dao.get_all()
        for record in records:
            path = record.get("backup_path", "")
            record["file_exists"] = bool(path) and os.path.isfile(path)
            record["size_text"] = format_file_size(record.get("backup_size", 0))
            type_text = {
                C.BACKUP_TYPE_FULL: "全量备份",
                C.BACKUP_TYPE_INCREMENTAL: "增量备份",
            }
            record["type_text"] = type_text.get(
                record.get("backup_type"), record.get("backup_type", "")
            )
        return records

    def delete_backup(self, backup_id: int) -> tuple:
        """删除备份（同时删除 zip 文件与备份记录，调用方需二次确认）。"""
        record = self.backup_dao.get_by_id(backup_id)
        if not record:
            return C.CODE_FILE_NOT_FOUND, None, "备份记录不存在"
        try:
            path = record.get("backup_path", "")
            if path and os.path.isfile(path):
                os.remove(path)
            self.backup_dao.delete_by_id(backup_id)
            write_operation_log(C.OP_BACKUP, f"删除备份：{record['backup_name']}")
            return C.CODE_SUCCESS, None, "备份已删除"
        except OSError as exc:
            return exception_to_code(exc), None, f"备份文件删除失败：{exc}"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 备份内部辅助 =================

    def _collect_lit_files(self) -> list:
        """收集受管目录中现存的全部文献文件。

        Returns:
            [(相对路径, 绝对路径, 字节数, sha256), ...]，缺失文件自动跳过。
        """
        base_dir = get_base_dir("literature")
        result = []
        for lit in self.lit_dao.select_all():
            rel = (lit.get("file_path") or "").replace("\\", "/")
            if not rel:
                continue
            abs_path = file_helper.resolve_path(rel, base_dir, "literature")
            if not os.path.isfile(abs_path):
                logger.warning("备份跳过缺失文献：id=%s path=%s", lit["id"], rel)
                continue
            result.append((
                rel, abs_path, os.path.getsize(abs_path),
                file_helper.calc_file_hash(abs_path),
            ))
        return result

    def _diff_incremental(self, current_files: list) -> list:
        """与最近一次备份的 manifest 对比，返回新增/变化文件（路径或大小不同）。"""
        latest = self.backup_dao.get_latest()
        if not latest or not os.path.isfile(latest.get("backup_path", "")):
            return current_files
        try:
            with zipfile.ZipFile(latest["backup_path"], "r") as zf:
                manifest = json.loads(
                    zf.read(_BACKUP_MANIFEST).decode("utf-8")
                )
            old_map = {
                item["path"]: item.get("size", -1)
                for item in manifest.get("files", [])
            }
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            logger.warning("旧备份清单读取失败，按全量处理：%s", exc)
            return current_files
        return [
            item for item in current_files
            if item[0] not in old_map or item[2] != old_map[item[0]]
        ]

    @staticmethod
    def _snapshot_database(snapshot_path: str) -> None:
        """用 sqlite backup API 生成当前数据库的一致性快照（持写锁）。"""
        manager = DatabaseManager()
        with manager.write_lock:
            manager.get_conn().commit()
            dst_conn = sqlite3.connect(snapshot_path)
            try:
                manager.get_conn().backup(dst_conn)
            finally:
                dst_conn.close()

    @staticmethod
    def _replace_zip_entry(zip_path: str, entry_name: str,
                           replacement_file: str) -> None:
        """流式重写 zip，仅替换指定条目（其余条目原样复制，不整体读入内存）。"""
        tmp_zip = zip_path + ".tmp"
        with zipfile.ZipFile(zip_path, "r") as src, \
                zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == entry_name:
                    dst.write(replacement_file, entry_name)
                    continue
                with src.open(item, "r") as rf, dst.open(item, "w") as wf:
                    shutil.copyfileobj(rf, wf)
        os.replace(tmp_zip, zip_path)

    @staticmethod
    def _restore_database(snapshot_path: str) -> None:
        """将快照库通过 backup API 覆盖到当前活动连接（Windows 也可在线执行）。"""
        manager = DatabaseManager()
        src_conn = sqlite3.connect(snapshot_path)
        try:
            with manager.write_lock:
                src_conn.backup(manager.get_conn())
                manager.get_conn().commit()
        finally:
            src_conn.close()
