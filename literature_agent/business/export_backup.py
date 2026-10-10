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

    def save_report_to_library(self, lit_id: int, fmt: str,
                               report_data: dict = None) -> tuple:
        """解析完成后把报告保存到解析报告存储目录（system_config 配置的
        report_path，与文献原件目录分离）。

        文件名含时间戳，同一篇文献多次保存会保留全部历史版本文件；
        同一秒内重复保存时自动追加序号，绝不覆盖旧文件。
        用户主动"另存为"可导出到任意自选目录，与本方法无关。

        Args:
            lit_id: 文献 id。
            fmt: word/txt/pdf。
            report_data: 可选报告字典（解析当次传入可保留关键词附加信息）；
                         不传时从数据库读取已存报告。
        Returns:
            (code, output_path, msg)
        """
        try:
            if report_data is None:
                report_data = self.parse_service.get_report(lit_id)
            if not report_data:
                return C.CODE_PARSE_FAILED, None, "该文献尚未解析，请先解析后再保存"
            if fmt not in ("word", "txt", "pdf"):
                return C.CODE_FILE_INVALID, None, \
                    f"不支持的导出格式：{fmt}（仅支持 word/txt/pdf）"
            suffix = self._fmt_suffix(fmt)

            title = report_data.get("literature_title") or f"文献{lit_id}"
            safe_title = self._safe_filename(str(title))
            # 保存位置：配置的解析报告存储目录（report_path），与文献原件目录分离
            report_dir = file_helper.ensure_dir(get_base_dir("report"))
            timestamp = now_str("%Y%m%d_%H%M%S")
            base_name = f"解析报告_{safe_title}_{lit_id}_{timestamp}"
            output_path = self._unique_library_path(report_dir, base_name, suffix)

            export_generator.export_report(report_data, output_path, fmt)
            write_operation_log(
                C.OP_EXPORT,
                f"解析报告已保存到报告存储目录：{title}（{fmt}）", lit_id,
            )
            return C.CODE_SUCCESS, output_path, "已保存到解析报告存储目录"
        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("解析报告保存失败 lit_id=%s：%s", lit_id, msg,
                         exc_info=True)
            write_operation_log(
                C.OP_EXPORT, f"解析报告保存失败[id={lit_id}]：{msg}",
                lit_id, C.OP_STATUS_FAILED,
            )
            return code, None, msg

    def save_batch_to_library(self, lit_ids: list, fmt: str,
                              report_map: dict = None,
                              progress_callback=None) -> dict:
        """批量把解析报告保存到配置的解析报告存储目录（report_path）。

        Args:
            lit_ids: 文献 id 列表。
            fmt: word/txt/pdf。
            report_map: {lit_id: report_dict}，解析当次的报告可保留关键词。
            progress_callback: 可选回调 (percent:int, message:str)。
        Returns:
            {"success": [path,...], "failed": [{"lit_id","msg"},...]}
        """
        report_map = report_map or {}
        result = {"success": [], "failed": []}
        total = len(lit_ids) or 1
        for index, lit_id in enumerate(lit_ids):
            code, path, msg = self.save_report_to_library(
                lit_id, fmt, report_map.get(lit_id)
            )
            if code == C.CODE_SUCCESS:
                result["success"].append(path)
            else:
                result["failed"].append({"lit_id": lit_id, "msg": msg})
            if progress_callback:
                percent = int((index + 1) / total * 100)
                progress_callback(
                    percent, f"保存解析报告 {index + 1}/{total}"
                )
        return result

    @staticmethod
    def _unique_library_path(report_dir: str, base_name: str,
                             suffix: str) -> str:
        """生成资料库内不冲突的报告文件路径（重名追加 _2/_3... 保留历史）。"""
        candidate = os.path.join(report_dir, base_name + suffix)
        if not os.path.exists(candidate):
            return candidate
        for seq in range(2, 10000):
            candidate = os.path.join(report_dir, f"{base_name}_{seq}{suffix}")
            if not os.path.exists(candidate):
                return candidate
        # 理论不可达：兜底加毫秒，保证不覆盖历史文件
        return os.path.join(
            report_dir, f"{base_name}_{now_str('%H%M%S_%f')}{suffix}"
        )

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
        tmp_dir = None
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
            # 显式写入本地时间：列默认值 CURRENT_TIMESTAMP 是 UTC，会导致备份列表
            # 显示时间比本地少 8 小时；与 manifest.create_time 取同一时刻保证一致
            backup_time = now_str()
            record_id = self.backup_dao.insert({
                "backup_name": name,
                "backup_path": zip_path,
                "backup_size": 0,
                "backup_type": backup_type,
                "content_scope": "all",
                "backup_status": C.OP_STATUS_SUCCESS,
                "backup_time": backup_time,
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
                "create_time": backup_time,
                "db_entry": _BACKUP_DB_ENTRY,
                "file_prefix": _BACKUP_FILE_PREFIX,
                "file_count": len(files_to_pack),
                # packed_files：本次 zip 实际打包的文献（恢复完整性校验依据）；
                # cumulative_files：该时点受管目录现存全部文献的累积清单
                # （下次增量差异比对依据）。两者必须分离：若只留"累积清单"，
                # 裸恢复增量备份时 DB 有记录、zip 无原件会静默丢失文件。
                "packed_files": [
                    {"path": rel, "size": size, "sha256": digest}
                    for rel, _abs, size, digest in files_to_pack
                ],
                "cumulative_files": [
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
            # 重写 zip 条目失败时可能残留同名 .tmp 半成品，一并清理
            if zip_path and os.path.isfile(zip_path + ".tmp"):
                try:
                    os.remove(zip_path + ".tmp")
                except OSError:
                    pass
            return code, None, f"备份失败：{msg}"
        finally:
            # 无论成功失败都清理 DB 快照临时目录，避免系统临时目录残留
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

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
                emit(50, "校验文献文件完整性")
                literature_dir = file_helper.ensure_dir(get_base_dir("literature"))
                # 覆盖 DB 前的完整性闸门：DB 快照引用且备份时点存在的文献，
                # 原件要么在本备份 zip 内、要么已存在于目标目录。
                # 裸恢复增量备份（无全量基线、磁盘又没有旧文件）在此被拒绝，
                # 避免出现"库有记录、磁盘无原件"的不一致
                missing = self._find_missing_restore_files(
                    names, db_restore_path, manifest, literature_dir
                )
                if missing:
                    preview = "、".join(missing[:3])
                    suffix = f" 等 {len(missing)} 篇" if len(missing) > 3 else ""
                    if manifest.get("backup_type") == C.BACKUP_TYPE_INCREMENTAL:
                        hint = (
                            f"该增量备份不包含 {len(missing)} 篇文献的原件"
                            f"（{preview}{suffix}）。增量备份只保存相对上次备份"
                            f"新增/变化的文件，请先恢复最近一次全量备份，"
                            f"再恢复本增量备份"
                        )
                    else:
                        hint = (
                            f"备份内容不完整，缺少 {len(missing)} 篇文献原件"
                            f"（{preview}{suffix}），无法完整恢复"
                        )
                    return C.CODE_FILE_INVALID, None, hint

                emit(55, "还原文献文件")
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

            # 快照把 backup_record 表"倒回"到备份时点：晚于该时点创建的备份
            # （典型：先全量后增量，从全量恢复时增量记录不在快照中）zip 仍在磁盘，
            # 记录却随覆盖消失。扫描备份目录对账，补登记缺失记录、修正失效路径
            emit(92, "核对备份列表")
            recon = self._reconcile_backup_records()
            if recon["added"] or recon["relinked"] or recon["time_fixed"]:
                logger.info(
                    "恢复对账：补登记备份记录 %s 条，重新挂接 %s 条，时间校准 %s 条",
                    recon["added"], recon["relinked"], recon["time_fixed"],
                )

            write_operation_log(
                C.OP_RESTORE,
                f"从备份恢复：{os.path.basename(backup_file_path)}，重启应用后完全生效",
            )
            emit(100, "恢复完成")
            extra = ""
            if recon["added"]:
                extra = f"（已自动找回 {recon['added']} 个备份记录，可继续按时间顺序恢复）"
            return C.CODE_SUCCESS, None, f"恢复成功，请重启应用使全部数据生效{extra}"
        except Exception as exc:
            code = exception_to_code(exc)
            msg = getattr(exc, "message", str(exc))
            logger.error("恢复失败：%s", msg, exc_info=True)
            write_operation_log(C.OP_RESTORE, f"恢复失败：{msg}",
                                status=C.OP_STATUS_FAILED)
            return code, None, f"恢复失败：{msg}"
        finally:
            # 无论成功、失败还是完整性校验拒绝，都清理解压 DB 快照临时目录
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

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
        """与最近一次备份的累积清单对比，返回新增/变化文件（路径或大小不同）。"""
        latest = self.backup_dao.get_latest()
        if not latest or not os.path.isfile(latest.get("backup_path", "")):
            return current_files
        try:
            with zipfile.ZipFile(latest["backup_path"], "r") as zf:
                manifest = json.loads(
                    zf.read(_BACKUP_MANIFEST).decode("utf-8")
                )
            # 新格式读 cumulative_files；旧版备份只有 files（语义同为累积清单）
            cumulative = manifest.get("cumulative_files")
            if cumulative is None:
                cumulative = manifest.get("files", [])
            old_map = {
                item["path"]: item.get("size", -1)
                for item in cumulative
            }
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            logger.warning("旧备份清单读取失败，按全量处理：%s", exc)
            return current_files
        return [
            item for item in current_files
            if item[0] not in old_map or item[2] != old_map[item[0]]
        ]

    @staticmethod
    def _find_missing_restore_files(zip_names: list, db_snapshot_path: str,
                                    manifest: dict, literature_dir: str) -> list:
        """找出恢复后仍会缺失原件的文献相对路径（覆盖 DB 前的完整性闸门）。

        判定规则：DB 快照中引用、且备份时点实际存在（manifest 累积清单）
        的每一篇文献，其原件必须「包含在本备份 zip 中」或「已存在于恢复
        目标目录」，两者都不满足则列入缺失。备份时点本就缺失原件的孤儿
        记录（累积清单中没有）不参与要求，避免误报。

        Args:
            zip_names: 备份 zip 内全部条目名。
            db_snapshot_path: 已解压的数据库快照文件绝对路径。
            manifest: 备份清单 dict（新格式含 cumulative_files，
                      旧格式只有 files，语义相同）。
            literature_dir: 恢复目标文献受管目录。
        Returns:
            缺失文件的库内相对路径列表（统一正斜杠）。
        """
        existed = manifest.get("cumulative_files")
        if existed is None:  # 旧版备份回退字段
            existed = manifest.get("files", [])
        existed_rels = {
            str(item.get("path", "")).replace("\\", "/")
            for item in existed if item.get("path")
        }
        packed_rels = {
            name[len(_BACKUP_FILE_PREFIX):].replace("\\", "/")
            for name in zip_names
            if name.startswith(_BACKUP_FILE_PREFIX) and not name.endswith("/")
        }
        check_conn = sqlite3.connect(db_snapshot_path)
        try:
            rows = check_conn.execute(
                "SELECT file_path FROM literature_info "
                "WHERE file_path IS NOT NULL AND file_path <> ''"
            ).fetchall()
        finally:
            check_conn.close()
        missing = []
        for (raw_rel,) in rows:
            rel = (raw_rel or "").replace("\\", "/")
            # 备份时点就已缺失的孤儿记录不纳入完整性要求
            if existed_rels and rel not in existed_rels:
                continue
            if rel in packed_rels:
                continue
            # 仅做存在性检查（只读，不写文件），同机恢复时保留磁盘现存原件
            if not os.path.isfile(os.path.join(literature_dir, rel)):
                missing.append(rel)
        return missing

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

    def _reconcile_backup_records(self) -> dict:
        """恢复覆盖数据库后，对账备份目录与 backup_record 表。

        恢复用的 DB 快照只含备份时点之前的备份记录；晚于该时点创建的备份
        zip 仍在磁盘（恢复流程不清理备份目录），记录却随快照覆盖丢失，
        导致其在备份列表消失、无法被继续恢复。本方法按文件名扫描当前备份
        目录：缺记录的 zip 用 manifest 信息补登记（还原原始备份时间），
        记录路径失效（换机/备份目录迁移）的重新挂接。

        Returns:
            dict: {"added": 补登记条数, "relinked": 重新挂接条数,
                   "time_fixed": 时间校准条数}
        """
        result = {"added": 0, "relinked": 0, "time_fixed": 0}
        backup_dir = get_base_dir("backup")
        if not os.path.isdir(backup_dir):
            return result
        for name in sorted(os.listdir(backup_dir)):
            if not name.lower().endswith(".zip"):
                continue
            abs_path = os.path.join(backup_dir, name)
            if not os.path.isfile(abs_path):
                continue
            # 只认本系统备份：必须含 manifest 且可解析
            backup_type = C.BACKUP_TYPE_FULL
            create_time = None
            try:
                with zipfile.ZipFile(abs_path, "r") as zf:
                    if _BACKUP_MANIFEST not in zf.namelist():
                        continue
                    manifest = json.loads(
                        zf.read(_BACKUP_MANIFEST).decode("utf-8")
                    )
                backup_type = manifest.get("backup_type", C.BACKUP_TYPE_FULL)
                create_time = manifest.get("create_time")
            except (OSError, zipfile.BadZipFile, KeyError, ValueError):
                logger.warning("恢复对账跳过无法解析的 zip：%s", name)
                continue
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue

            record = self.backup_dao.get_by_name(name)
            if record is None:
                # 备份晚于快照时点，记录被覆盖 → 补登记
                self.backup_dao.insert({
                    "backup_name": name,
                    "backup_path": abs_path,
                    "backup_size": size,
                    "backup_type": backup_type,
                    "content_scope": "all",
                    "backup_status": 1,
                    "backup_time": create_time,
                })
                result["added"] += 1
                logger.info("恢复对账补登记备份：%s（%s）", name, backup_type)
            elif not os.path.isfile(record.get("backup_path", "")):
                # 记录还在但路径失效，当前目录同名 zip 即实际位置；同时校准时间
                self.backup_dao.update_location(
                    record["id"], abs_path, size, create_time
                )
                result["relinked"] += 1
                logger.info("恢复对账重新挂接备份记录 id=%s：%s",
                            record["id"], abs_path)
            elif create_time and record.get("backup_time") != create_time:
                # 路径有效但时间与 manifest 不符（早期版本记录落的是 UTC）→ 校准
                self.backup_dao.update_backup_time(record["id"], create_time)
                result["time_fixed"] += 1
                logger.info("恢复对账校准备份时间 id=%s：%s -> %s",
                            record["id"], record.get("backup_time"), create_time)
        return result

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
