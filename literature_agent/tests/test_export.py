"""阶段3 B3-5：报告导出 + 全量/增量备份 + 恢复业务测试。"""
import glob
import json
import os
import sqlite3
import tempfile
import zipfile

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao

SAMPLE_TEXT = (
    "研究背景：备份恢复功能测试。\n\n"
    "核心观点：数据安全第一。\n\n"
    "研究结论：备份可用。\n"
)


@pytest.fixture
def storage(tmp_path):
    """重定向文献与备份目录到临时目录。"""
    lit_dir = tmp_path / "lit_store"
    backup_dir = tmp_path / "backup_store"
    report_dir = tmp_path / "report_store"
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(lit_dir))
    SystemConfigDao().set(C.CFG_BACKUP_PATH, str(backup_dir))
    SystemConfigDao().set(C.CFG_REPORT_PATH, str(report_dir))
    return str(lit_dir), str(backup_dir), str(report_dir)


def _import_one(tmp_path, name="paper.txt", content=SAMPLE_TEXT):
    """导入一篇临时 TXT 文献，返回文献字典。"""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    code, lit, msg = LiteratureImportService().import_single(str(path))
    assert code == C.CODE_SUCCESS, msg
    return lit


def test_export_report_txt(mem_conn, storage, tmp_path):
    """解析后可导出 TXT/Word 报告。"""
    lit = _import_one(tmp_path)
    from business.literature_parse import LiteratureParseService
    code, _report, msg = LiteratureParseService().parse_single(lit["id"])
    assert code == C.CODE_SUCCESS, msg

    service = ExportBackupService()
    out_txt = str(tmp_path / "r.txt")
    code, _p, msg = service.export_report(lit["id"], "txt", out_txt)
    assert code == C.CODE_SUCCESS, msg
    assert os.path.isfile(out_txt)

    out_docx = str(tmp_path / "r.docx")
    code, _p, msg = service.export_report(lit["id"], "word", out_docx)
    assert code == C.CODE_SUCCESS, msg
    assert os.path.isfile(out_docx)


def test_save_report_to_library_location_and_history(mem_conn, storage):
    """报告默认保存到配置的解析报告存储目录（与原件目录分离）；同秒重复保存保留历史版本不覆盖。"""
    import time
    import tempfile
    from pathlib import Path
    from business.literature_parse import LiteratureParseService
    tmp_path = Path(tempfile.mkdtemp())
    lit = _import_one(tmp_path)
    lit_dir, _backup_dir, report_dir = storage
    code, report, msg = LiteratureParseService().parse_single(lit["id"])
    assert code == C.CODE_SUCCESS, msg

    service = ExportBackupService()
    code, path1, msg = service.save_report_to_library(
        lit["id"], "txt", report_data=report
    )
    assert code == C.CODE_SUCCESS, msg
    def norm_path(value):
        """归一化路径（解析 8.3 短路径、统一大小写）后比较。"""
        return os.path.normcase(os.path.realpath(value))

    # 默认保存位置是解析报告存储目录（report_path），不是文献原件目录
    assert norm_path(os.path.dirname(path1)) == norm_path(report_dir)
    assert norm_path(os.path.dirname(path1)) != norm_path(lit_dir)
    assert path1.endswith(".txt")
    assert os.path.isfile(path1)

    # 同秒再次保存：追加序号生成不同文件，历史版本保留
    code, path2, msg = service.save_report_to_library(
        lit["id"], "txt", report_data=report
    )
    assert code == C.CODE_SUCCESS, msg
    assert path2 != path1
    assert os.path.isfile(path1) and os.path.isfile(path2)

    # 时间戳不同（隔 1.2 秒）时文件名也不同
    time.sleep(1.2)
    code, path3, msg = service.save_report_to_library(
        lit["id"], "pdf", report_data=report
    )
    assert code == C.CODE_SUCCESS, msg
    assert path3.endswith(".pdf") and path3 != path2
    # 报告目录内：3 个报告文件；文献原件目录只有 1 个原件，不混入报告
    assert len(os.listdir(report_dir)) == 3
    assert len(os.listdir(lit_dir)) == 1


def test_save_report_to_library_keeps_keywords(mem_conn, storage):
    """解析当次传入的报告含关键词附加信息，保存的 TXT 中应保留关键词行。"""
    import tempfile
    from pathlib import Path
    from business.literature_parse import LiteratureParseService
    tmp_path = Path(tempfile.mkdtemp())
    lit = _import_one(tmp_path)
    code, report, msg = LiteratureParseService().parse_single(lit["id"])
    assert code == C.CODE_SUCCESS, msg
    assert report.get("keywords"), "解析报告应携带关键词附加信息"

    service = ExportBackupService()
    code, path, msg = service.save_report_to_library(
        lit["id"], "txt", report_data=report
    )
    assert code == C.CODE_SUCCESS, msg
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    assert "关键词：" in content


def test_save_report_to_library_rejects_invalid(mem_conn, storage):
    """未解析文献与非法格式被拒绝。"""
    import tempfile
    from pathlib import Path
    lit = _import_one(Path(tempfile.mkdtemp()))
    service = ExportBackupService()
    assert service.save_report_to_library(lit["id"], "txt")[0] == C.CODE_PARSE_FAILED

    from business.literature_parse import LiteratureParseService
    assert LiteratureParseService().parse_single(lit["id"])[0] == C.CODE_SUCCESS
    code, _p, msg = service.save_report_to_library(lit["id"], "excel")
    assert code == C.CODE_FILE_INVALID and "word/txt/pdf" in msg


def test_save_batch_to_library(mem_conn, storage):
    """批量保存到解析报告存储目录：全部成功并回调进度。"""
    import tempfile
    from pathlib import Path
    from business.literature_parse import LiteratureParseService
    tmp_path = Path(tempfile.mkdtemp())
    lit1 = _import_one(tmp_path, name="p1.txt")
    lit2 = _import_one(tmp_path, name="p2.txt",
                       content="研究背景：批量保存第二篇。\n\n结论：第二篇结论。\n")
    parse_service = LiteratureParseService()
    assert parse_service.parse_single(lit1["id"])[0] == C.CODE_SUCCESS
    assert parse_service.parse_single(lit2["id"])[0] == C.CODE_SUCCESS

    lit_dir, _backup_dir, report_dir = storage
    progress = []
    result = ExportBackupService().save_batch_to_library(
        [lit1["id"], lit2["id"]], "word",
        progress_callback=lambda p, m: progress.append(p),
    )
    assert result["failed"] == []
    assert len(result["success"]) == 2
    assert all(os.path.isfile(p) and p.endswith(".docx") for p in result["success"])
    assert progress and progress[-1] == 100
    # 报告进报告存储目录（2 个 docx），文献原件目录只留 2 个原件
    assert len(os.listdir(report_dir)) == 2
    assert len(os.listdir(lit_dir)) == 2


def test_full_backup_creates_zip_with_db_and_files(mem_conn, storage, tmp_path):
    """全量备份：zip 内含 manifest、db 快照、文献源文件，并落备份记录。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    progress = []
    code, record, msg = service.full_backup(
        progress_callback=lambda p, m: progress.append(p)
    )
    assert code == C.CODE_SUCCESS, msg
    assert os.path.isfile(record["backup_path"])
    assert record["backup_type"] == C.BACKUP_TYPE_FULL
    assert progress and progress[-1] == 100

    with zipfile.ZipFile(record["backup_path"]) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "database/literature_agent.db" in names
        lit_entries = [n for n in names if n.startswith("literature/")]
        assert len(lit_entries) == 1


def test_restore_overwrites_current_database(mem_conn, storage, tmp_path):
    """备份后删除文献，恢复后数据回来且文献文件还原。"""
    lit = _import_one(tmp_path)
    lit_id = lit["id"]
    service = ExportBackupService()
    code, record, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg

    # 删除文献（级联清理），并手动移除受管文件模拟数据丢失
    lit_dao = LiteratureInfoDao()
    assert lit_dao.delete_by_id(lit_id) == 1
    lit_dir, _backup, _report = storage
    for root, _dirs, files in os.walk(lit_dir):
        for name in files:
            os.remove(os.path.join(root, name))

    code, _data, msg = service.restore(record["backup_path"])
    assert code == C.CODE_SUCCESS, msg
    assert lit_dao.select_by_id(lit_id) is not None
    restored_files = []
    for root, _dirs, files in os.walk(lit_dir):
        restored_files.extend(files)
    assert restored_files
    # 阶段4 P1：备份记录先于快照落库，恢复后该备份自身的记录不能丢失
    from data_layer.dao.backup_dao import BackupRecordDao
    restored_record = BackupRecordDao().get_by_id(record["id"])
    assert restored_record is not None
    assert restored_record["backup_size"] > 0


def test_restore_rejects_invalid_zip(mem_conn, storage, tmp_path):
    """非 zip 文件恢复必须被拒绝。"""
    fake = tmp_path / "broken.zip"
    fake.write_text("not a zip", encoding="utf-8")
    code, _data, msg = ExportBackupService().restore(str(fake))
    assert code == C.CODE_FILE_INVALID


def test_incremental_backup_only_packs_changed(mem_conn, storage, tmp_path):
    """增量备份：无变化 0 个文献文件；新增文献后只打包新文件。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, record1, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg

    code, record2, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg
    assert record2["backup_type"] == C.BACKUP_TYPE_INCREMENTAL
    with zipfile.ZipFile(record2["backup_path"]) as zf:
        inc_entries = [n for n in zf.namelist() if n.startswith("literature/")]
    assert inc_entries == []

    _import_one(tmp_path, name="paper2.txt",
                content="研究背景：第二篇新增文献。\n\n结论：不同内容。\n")
    code, record3, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg
    with zipfile.ZipFile(record3["backup_path"]) as zf:
        inc_entries = [n for n in zf.namelist() if n.startswith("literature/")]
    assert len(inc_entries) == 1


def test_backup_list_and_delete(mem_conn, storage, tmp_path):
    """备份列表标记文件存在；删除备份同时删除 zip 与记录。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, record, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg

    records = service.get_backup_list()
    assert len(records) == 1
    assert records[0]["file_exists"] is True
    assert records[0]["type_text"] == "全量备份"

    code, _data, msg = service.delete_backup(record["id"])
    assert code == C.CODE_SUCCESS, msg
    assert not os.path.isfile(record["backup_path"])
    assert service.get_backup_list() == []

    code, _data, msg = service.delete_backup(9999)
    assert code == C.CODE_FILE_NOT_FOUND


def test_backup_missing_record_file_is_marked(mem_conn, storage, tmp_path):
    """记录存在但 zip 丢失时，列表标记 file_exists=False。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, record, _ = service.full_backup()
    os.remove(record["backup_path"])
    records = service.get_backup_list()
    assert records[0]["file_exists"] is False


# ================= 增量备份完整性修复回归 =================

def _read_manifest(backup_path):
    """读取备份 zip 内 manifest.json。"""
    with zipfile.ZipFile(backup_path) as zf:
        return json.loads(zf.read("manifest.json").decode("utf-8"))


def _lit_entries(backup_path):
    """返回备份 zip 内 literature/ 文献条目名列表。"""
    with zipfile.ZipFile(backup_path) as zf:
        return [n for n in zf.namelist() if n.startswith("literature/")]


def test_incremental_manifest_splits_packed_and_cumulative(
        mem_conn, storage, tmp_path):
    """增量 manifest 拆双字段：packed=本次变化文件，cumulative=现存全量。"""
    _import_one(tmp_path, name="paper1.txt", content="paper1 content\n")
    service = ExportBackupService()
    code, _full, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg

    _import_one(tmp_path, name="paper2.txt", content="paper2 content\n")
    code, inc, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg

    manifest = _read_manifest(inc["backup_path"])
    packed = {item["path"] for item in manifest["packed_files"]}
    cumulative = {item["path"] for item in manifest["cumulative_files"]}
    assert manifest["file_count"] == 1
    assert packed == {"paper2.txt"}
    assert cumulative == {"paper1.txt", "paper2.txt"}
    # 旧的歧义字段 files 不再写入新备份
    assert "files" not in manifest
    # zip 实际条目与 packed_files 一致
    assert _lit_entries(inc["backup_path"]) == ["literature/paper2.txt"]


def test_repeated_incremental_does_not_degrade(mem_conn, storage, tmp_path):
    """连续无变化增量：靠 cumulative_files 比对，不把旧文件重复打包（不退化为全量）。"""
    _import_one(tmp_path, name="paper1.txt")
    service = ExportBackupService()
    assert service.full_backup()[0] == C.CODE_SUCCESS
    _import_one(tmp_path, name="paper2.txt",
                content="研究背景：第二篇新增文献。\n")
    assert service.incremental_backup()[0] == C.CODE_SUCCESS

    # 再做一次增量：无任何新增/变化 → 本次打包 0 个，但累积清单仍为全量
    code, inc2, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg
    manifest = _read_manifest(inc2["backup_path"])
    assert manifest["file_count"] == 0
    assert manifest["packed_files"] == []
    assert {item["path"] for item in manifest["cumulative_files"]} == {
        "paper1.txt", "paper2.txt",
    }
    assert _lit_entries(inc2["backup_path"]) == []


def test_restore_incremental_without_base_is_rejected(
        mem_conn, storage, tmp_path):
    """磁盘无旧文件时裸恢复增量备份：拒绝并提示先恢复全量，且不覆盖当前库。"""
    lit1 = _import_one(tmp_path, name="paper1.txt", content="paper1\n")
    lit2 = _import_one(tmp_path, name="paper2.txt", content="paper2\n")
    service = ExportBackupService()
    assert service.full_backup()[0] == C.CODE_SUCCESS
    code, inc, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg

    # 模拟换机器/磁盘丢失：删库记录并清空文献目录
    lit_dao = LiteratureInfoDao()
    assert lit_dao.delete_by_id(lit1["id"]) == 1
    assert lit_dao.delete_by_id(lit2["id"]) == 1
    lit_dir, _backup, _report = storage
    for root, _dirs, files in os.walk(lit_dir):
        for name in files:
            os.remove(os.path.join(root, name))

    code, _data, msg = service.restore(inc["backup_path"])
    assert code == C.CODE_FILE_INVALID
    assert "全量备份" in msg
    # 完整性闸门在覆盖 DB/还原文件之前关闭：当前库保持删除后状态
    assert lit_dao.select_by_id(lit1["id"]) is None
    # 没有任何文献文件被写入
    restored = []
    for _root, _dirs, files in os.walk(lit_dir):
        restored.extend(files)
    assert restored == []


def test_restore_incremental_same_machine_keeps_existing_files(
        mem_conn, storage, tmp_path):
    """同机恢复增量（旧原件仍在磁盘）：允许恢复，未变化文件保留、变化文件还原。"""
    _import_one(tmp_path, name="paper1.txt", content="paper1\n")
    service = ExportBackupService()
    assert service.full_backup()[0] == C.CODE_SUCCESS
    lit2 = _import_one(tmp_path, name="paper2.txt", content="paper2\n")
    code, inc, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg

    # 不清空磁盘，直接恢复：paper1 已在磁盘、paper2 在 zip 内，完整性校验通过
    code, _data, msg = service.restore(inc["backup_path"])
    assert code == C.CODE_SUCCESS, msg

    lit_dir, _backup, _report = storage
    disk_files = []
    for _root, _dirs, files in os.walk(lit_dir):
        disk_files.extend(files)
    assert set(disk_files) == {"paper1.txt", "paper2.txt"}
    assert LiteratureInfoDao().select_by_id(lit2["id"]) is not None


def test_diff_reads_legacy_files_field(mem_conn, storage, tmp_path):
    """旧版备份 manifest 只有 files（累积清单）：差异比对正确回退，不误判旧文件。"""
    from data_layer.dao.backup_dao import BackupRecordDao
    legacy_zip = tmp_path / "legacy_inc.zip"
    with zipfile.ZipFile(legacy_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "version": 1,
            "backup_type": "incremental",
            "file_count": 1,
            "files": [{"path": "paper1.txt", "size": 100, "sha256": "old"}],
        }, ensure_ascii=False))
    BackupRecordDao().insert({
        "backup_name": "legacy_inc.zip",
        "backup_path": str(legacy_zip),
        "backup_size": 1,
        "backup_type": C.BACKUP_TYPE_INCREMENTAL,
        "content_scope": "all",
        "backup_status": C.OP_STATUS_SUCCESS,
    })

    current = [
        ("paper1.txt", os.path.join(str(tmp_path), "paper1.txt"), 100, "old"),
        ("paper2.txt", os.path.join(str(tmp_path), "paper2.txt"), 50, "new"),
    ]
    diffed = ExportBackupService()._diff_incremental(current)
    # paper1 大小无变化不重复打包，paper2 被判定为新增
    assert [item[0] for item in diffed] == ["paper2.txt"]


def test_find_missing_uses_legacy_files_field(mem_conn, storage, tmp_path):
    """完整性校验对旧版 files 字段回退：zip 无原件且磁盘无文件时判缺失。"""
    from business.export_backup import _BACKUP_FILE_PREFIX
    lit_dir, _backup, _report = storage
    snapshot_db = tmp_path / "snap.db"
    conn = sqlite3.connect(str(snapshot_db))
    conn.execute("CREATE TABLE literature_info (file_path TEXT)")
    conn.execute("INSERT INTO literature_info VALUES (?)", ("paper1.txt",))
    conn.commit()
    conn.close()

    legacy_manifest = {
        "version": 1,
        "backup_type": "incremental",
        # 旧格式：只有 files（备份时点 paper1 存在）
        "files": [{"path": "paper1.txt", "size": 8}],
    }
    zip_names = ["manifest.json", "database/literature_agent.db",
                 f"{_BACKUP_FILE_PREFIX}paper2.txt"]

    # zip 没带 paper1、磁盘也没有 → 缺失
    missing = ExportBackupService._find_missing_restore_files(
        zip_names, str(snapshot_db), legacy_manifest, lit_dir
    )
    assert missing == ["paper1.txt"]

    # 磁盘已有 paper1（同机恢复）→ 不再缺失
    os.makedirs(lit_dir, exist_ok=True)
    with open(os.path.join(lit_dir, "paper1.txt"), "wb") as fh:
        fh.write(b"paper1\n")
    missing = ExportBackupService._find_missing_restore_files(
        zip_names, str(snapshot_db), legacy_manifest, lit_dir
    )
    assert missing == []


# ================= 临时目录清理回归 =================

def _temp_dirs(prefix):
    """列出系统临时目录中当前存在的指定前缀临时目录。"""
    return glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*"))


def test_backup_cleans_temp_snapshot_dir(mem_conn, storage, tmp_path):
    """备份成功后 lit_backup_* 临时快照目录被 finally 清理，无残留。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    before = set(_temp_dirs("lit_backup_"))
    code, _record, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg
    after = set(_temp_dirs("lit_backup_"))
    assert not (after - before), f"备份临时目录残留：{after - before}"


def test_restore_cleans_temp_snapshot_dir(mem_conn, storage, tmp_path):
    """恢复成功后 lit_restore_* 临时解压目录被 finally 清理，无残留。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, record, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg
    before = set(_temp_dirs("lit_restore_"))
    code, _data, msg = service.restore(record["backup_path"])
    assert code == C.CODE_SUCCESS, msg
    after = set(_temp_dirs("lit_restore_"))
    assert not (after - before), f"恢复临时目录残留：{after - before}"


def test_rejected_restore_cleans_temp_snapshot_dir(mem_conn, storage, tmp_path):
    """完整性校验拒绝裸恢复增量时，临时解压目录同样被清理。"""
    lit1 = _import_one(tmp_path, name="paper1.txt", content="paper1\n")
    _import_one(tmp_path, name="paper2.txt", content="paper2\n")
    service = ExportBackupService()
    assert service.full_backup()[0] == C.CODE_SUCCESS
    code, inc, _msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg

    lit_dao = LiteratureInfoDao()
    lit_dao.delete_by_id(lit1["id"])
    lit_dir, _backup, _report = storage
    for root, _dirs, files in os.walk(lit_dir):
        for name in files:
            os.remove(os.path.join(root, name))

    before = set(_temp_dirs("lit_restore_"))
    code, _data, msg = service.restore(inc["backup_path"])
    assert code == C.CODE_FILE_INVALID
    after = set(_temp_dirs("lit_restore_"))
    assert not (after - before), f"拒绝恢复后临时目录残留：{after - before}"


# ================= 恢复后备份记录对账（全量→增量恢复链） =================

def _clear_literature_dir(lit_dir):
    """模拟磁盘损坏：清空受管文献目录下全部文件。"""
    for root, _dirs, files in os.walk(lit_dir):
        for name in files:
            os.remove(os.path.join(root, name))


def _manifest_create_time(zip_path):
    """读取备份 zip 内 manifest 的 create_time。"""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return json.loads(zf.read("manifest.json").decode("utf-8"))["create_time"]


def test_restore_full_then_incremental_chain(mem_conn, storage, tmp_path):
    """先全量 A 后增量 B：从 A 恢复后 B 记录自动找回，并可继续恢复 B。"""
    _import_one(tmp_path, name="paper1.txt", content="paper1\n")
    service = ExportBackupService()
    code, full_rec, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg
    full_time = _manifest_create_time(full_rec["backup_path"])

    _import_one(tmp_path, name="paper2.txt", content="paper2\n")
    code, inc_rec, msg = service.incremental_backup()
    assert code == C.CODE_SUCCESS, msg
    inc_time = _manifest_create_time(inc_rec["backup_path"])

    # 灾难场景：磁盘文献全部丢失后，从全量 A 恢复
    lit_dir, _backup_dir, _report_dir = storage
    _clear_literature_dir(lit_dir)
    code, _data, msg = service.restore(full_rec["backup_path"])
    assert code == C.CODE_SUCCESS, msg

    # A 恢复后：备份列表必须同时包含 A、B，且 B 文件状态正常、类型为增量
    records = {r["backup_name"]: r for r in service.get_backup_list()}
    assert full_rec["backup_name"] in records
    assert inc_rec["backup_name"] in records, "增量备份记录在全量恢复后丢失"
    recovered_inc = records[inc_rec["backup_name"]]
    assert recovered_inc["backup_type"] == C.BACKUP_TYPE_INCREMENTAL
    assert recovered_inc["file_exists"] is True
    # 补登记保留原始备份时间（不是恢复操作的当前时间）
    assert recovered_inc["backup_time"] == inc_time
    assert records[full_rec["backup_name"]]["backup_time"] == full_time

    # 恢复 A 后 DB 与磁盘都回到 A 时点：只有 paper1
    lit_dao = LiteratureInfoDao()
    assert lit_dao.count_all() == 1
    assert os.path.isfile(os.path.join(lit_dir, "paper1.txt"))

    # 继续按顺序恢复增量 B：paper1 已在磁盘（A 还原）、paper2 在 B zip，闸门应放行
    code, _data, msg = service.restore(recovered_inc["backup_path"])
    assert code == C.CODE_SUCCESS, msg
    titles = {item["literature_title"] for item in lit_dao.select_all()}
    assert titles == {"paper1", "paper2"}
    assert os.path.isfile(os.path.join(lit_dir, "paper1.txt"))
    assert os.path.isfile(os.path.join(lit_dir, "paper2.txt"))


def test_reconcile_relinks_invalid_record_path(mem_conn, storage, tmp_path):
    """备份记录路径失效（换机/目录迁移）时，对账按同名 zip 重新挂接。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, rec, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg

    # 人为把记录路径改成已失效路径，模拟换机后绝对路径漂移
    from data_layer.dao.backup_dao import BackupRecordDao
    dao = BackupRecordDao()
    dao.update_location(rec["id"], os.path.join(tmp_path, "gone", "x.zip"), 0)
    listed = {r["id"]: r for r in service.get_backup_list()}[rec["id"]]
    assert listed["file_exists"] is False

    result = service._reconcile_backup_records()
    assert result["relinked"] == 1
    fixed = dao.get_by_id(rec["id"])
    assert os.path.isfile(fixed["backup_path"])
    assert fixed["backup_size"] > 0
    # 重新挂接同时按 manifest 校准时间（本地时间）
    assert fixed["backup_time"] == _manifest_create_time(rec["backup_path"])


def test_reconcile_fixes_legacy_utc_backup_time(mem_conn, storage, tmp_path):
    """早期版本备份记录落的是 UTC 时间，对账按 manifest 校准为本地时间且幂等。"""
    _import_one(tmp_path)
    service = ExportBackupService()
    code, rec, msg = service.full_backup()
    assert code == C.CODE_SUCCESS, msg
    manifest_time = _manifest_create_time(rec["backup_path"])

    from data_layer.dao.backup_dao import BackupRecordDao
    dao = BackupRecordDao()
    # 模拟旧记录：时间整体早 8 小时（UTC）
    from datetime import datetime, timedelta
    utc_like = (
        datetime.strptime(manifest_time, "%Y-%m-%d %H:%M:%S") - timedelta(hours=8)
    ).strftime("%Y-%m-%d %H:%M:%S")
    dao.conn.execute(
        "UPDATE backup_record SET backup_time=? WHERE id=?",
        (utc_like, rec["id"]),
    )
    dao.conn.commit()

    result = service._reconcile_backup_records()
    assert result["time_fixed"] == 1
    assert dao.get_by_id(rec["id"])["backup_time"] == manifest_time
    # 校准后再次对账无动作（幂等）
    assert service._reconcile_backup_records() == {
        "added": 0, "relinked": 0, "time_fixed": 0
    }


def test_reconcile_is_idempotent(mem_conn, storage, tmp_path):
    """对账重复执行不产生重复备份记录。"""
    _import_one(tmp_path, name="paper1.txt", content="paper1\n")
    service = ExportBackupService()
    assert service.full_backup()[0] == C.CODE_SUCCESS
    _import_one(tmp_path, name="paper2.txt", content="paper2\n")
    assert service.incremental_backup()[0] == C.CODE_SUCCESS

    first = service._reconcile_backup_records()
    second = service._reconcile_backup_records()
    assert second == {"added": 0, "relinked": 0, "time_fixed": 0}
    names = [r["backup_name"] for r in service.get_backup_list()]
    assert len(names) == len(set(names))
    # 首次对账在记录完好时也不应重复插入或校准
    assert first == {"added": 0, "relinked": 0, "time_fixed": 0}
