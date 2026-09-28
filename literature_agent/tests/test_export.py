"""阶段3 B3-5：报告导出 + 全量/增量备份 + 恢复业务测试。"""
import os
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
    """报告默认保存到文献资料库（与原件同目录）；同秒重复保存保留历史版本不覆盖。"""
    import time
    import tempfile
    from pathlib import Path
    from business.literature_parse import LiteratureParseService
    tmp_path = Path(tempfile.mkdtemp())
    lit = _import_one(tmp_path)
    lit_dir, _backup_dir, _report_dir = storage
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

    # 默认保存位置是文献资料库目录（上传的 paper.txt 也在其中），不是报告目录
    assert norm_path(os.path.dirname(path1)) == norm_path(lit_dir)
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
    # 文献资料库内：1 个原件 paper.txt + 3 个报告文件
    assert len(os.listdir(lit_dir)) == 4


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
    """批量保存到资料库：全部成功并回调进度。"""
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

    lit_dir, _backup_dir, _report_dir = storage
    progress = []
    result = ExportBackupService().save_batch_to_library(
        [lit1["id"], lit2["id"]], "word",
        progress_callback=lambda p, m: progress.append(p),
    )
    assert result["failed"] == []
    assert len(result["success"]) == 2
    assert all(os.path.isfile(p) and p.endswith(".docx") for p in result["success"])
    assert progress and progress[-1] == 100
    # 报告与文献原件同目录：2 个原件（p1/p2.txt）+ 2 个 docx 报告
    assert len(os.listdir(lit_dir)) == 4


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
