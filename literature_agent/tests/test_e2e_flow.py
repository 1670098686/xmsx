"""阶段4 T4-1：全链路端到端测试（业务层，不依赖 Qt）。

链路：导入 → 批量解析 → 全文检索 → 分类/标签绑定与筛选
     → 段落/全局笔记（防抖落库）→ 单篇/批量导出 → 全量备份
     → 删除数据 → 恢复 → 全数据校验 → 会话配置持久化 → 操作日志。
"""
import json
import os

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.literature_search import LiteratureSearchService
from business.note_manage import NoteManageService
from business.tag_category import TagCategoryService
from config import constants as C
from config.settings import get_setting, save_setting
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.log_dao import OperationLogDao

PAPER_A = (
    "摘要\n本文研究深度学习在图像识别中的应用。\n\n"
    "研究背景：神经网络近年来快速发展。\n\n"
    "结论：深度学习模型在实验中表现优异。\n"
)
PAPER_B = (
    "摘要\n本文讨论自然语言处理的关键词抽取方法。\n\n"
    "研究背景：文本数据规模持续增长。\n\n"
    "结论：分词与词频统计能有效提取关键词。\n"
)


@pytest.fixture
def storage(tmp_path):
    """重定向全部受管目录到临时目录。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "lit_store"))
    SystemConfigDao().set(C.CFG_REPORT_PATH, str(tmp_path / "report_store"))
    SystemConfigDao().set(C.CFG_NOTE_PATH, str(tmp_path / "note_store"))
    SystemConfigDao().set(C.CFG_BACKUP_PATH, str(tmp_path / "backup_store"))
    return tmp_path


def _write_paper(tmp_path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_full_workflow_end_to_end(mem_conn, storage, tmp_path):
    """T4-1：导入→解析→检索→批注→导出→备份→恢复全链路无断点。"""
    import_service = LiteratureImportService()
    parse_service = LiteratureParseService()
    search_service = LiteratureSearchService()
    tag_service = TagCategoryService()
    note_service = NoteManageService()
    backup_service = ExportBackupService()

    # ---------- 1. 导入两篇 ----------
    path_a = _write_paper(tmp_path, "paper_a.txt", PAPER_A)
    path_b = _write_paper(tmp_path, "paper_b.txt", PAPER_B)
    code, lit_a, msg = import_service.import_single(path_a)
    assert code == C.CODE_SUCCESS, msg
    code, lit_b, msg = import_service.import_single(path_b)
    assert code == C.CODE_SUCCESS, msg
    id_a, id_b = lit_a["id"], lit_b["id"]

    # 重复导入必须命中去重
    code, _data, _msg = import_service.import_single(path_a)
    assert code == C.CODE_DUPLICATE

    # ---------- 2. 批量解析 ----------
    result = parse_service.parse_batch([id_a, id_b])
    assert len(result["failed"]) == 0, result["failed"]
    assert len(result["success"]) == 2
    lit_dao = LiteratureInfoDao()
    assert lit_dao.select_by_id(id_a)["is_parsed"] == C.PARSE_DONE

    # ---------- 3. 全文检索 ----------
    code, rows, msg = search_service.full_text_search("深度学习")
    assert code == C.CODE_SUCCESS, msg
    hit_ids = {row["id"] for row in rows}
    assert id_a in hit_ids

    code, rows, _msg = search_service.full_text_search("关键词")
    assert id_b in {row["id"] for row in rows}
    assert id_a not in {row["id"] for row in rows}

    # ---------- 4. 分类 + 标签 + 绑定 + 筛选 ----------
    code, cat_id, msg = tag_service.create_category("E2E分类X")
    assert code == C.CODE_SUCCESS, msg
    code, tag_id, msg = tag_service.create_tag("E2E标签X")
    assert code == C.CODE_SUCCESS, msg

    code, _d, msg = search_service.assign_category([id_a], cat_id)
    assert code == C.CODE_SUCCESS, msg
    code, _d, msg = tag_service.bind_tags_to_lit(id_a, [tag_id])
    assert code == C.CODE_SUCCESS, msg

    code, rows, _msg = search_service.filter_by_tag(tag_id)
    assert {row["id"] for row in rows} == {id_a}
    code, rows, _msg = search_service.filter_by_category(cat_id)
    assert {row["id"] for row in rows} == {id_a}

    # ---------- 5. 笔记：段落批注 + 全局笔记 + 防抖落库 ----------
    code, note, msg = note_service.add_paragraph_note(id_a, "3", "重点段落批注E2E")
    assert code == C.CODE_SUCCESS, msg
    code, global_note, msg = note_service.add_global_note(id_a, "全局总结E2E")
    assert code == C.CODE_SUCCESS, msg
    note_service.flush_all()
    notes = note_service.get_notes_by_lit(id_a)
    assert any(n["note_content"] == "重点段落批注E2E" for n in notes)
    assert note_service.get_global_note(id_a)["note_content"] == "全局总结E2E"

    # ---------- 6. 单篇导出 + 批量导出 ----------
    out_txt = str(tmp_path / "a.txt")
    code, _p, msg = backup_service.export_report(id_a, "txt", out_txt)
    assert code == C.CODE_SUCCESS, msg and os.path.isfile(out_txt)
    out_docx = str(tmp_path / "a.docx")
    code, _p, msg = backup_service.export_report(id_a, "word", out_docx)
    assert code == C.CODE_SUCCESS, msg and os.path.isfile(out_docx)
    batch_dir = str(tmp_path / "batch_export")
    os.makedirs(batch_dir, exist_ok=True)
    batch_result = backup_service.export_batch([id_a, id_b], "txt", batch_dir)
    assert len(batch_result["failed"]) == 0, batch_result["failed"]
    assert len([f for f in os.listdir(batch_dir) if f.endswith(".txt")]) == 2

    # ---------- 7. 全量备份 ----------
    code, record, msg = backup_service.full_backup()
    assert code == C.CODE_SUCCESS, msg
    backup_zip = record["backup_path"]
    assert os.path.isfile(backup_zip)

    # ---------- 8. 模拟数据丢失：删库行 + 删受管文件 ----------
    assert lit_dao.delete_by_id(id_a) == 1
    assert lit_dao.delete_by_id(id_b) == 1
    assert LiteratureInfoDao().select_all() == []
    for root, _dirs, files in os.walk(str(tmp_path / "lit_store")):
        for name in files:
            os.remove(os.path.join(root, name))

    # ---------- 9. 恢复并全量校验 ----------
    code, _d, msg = backup_service.restore(backup_zip)
    assert code == C.CODE_SUCCESS, msg
    assert lit_dao.select_by_id(id_a) is not None
    assert lit_dao.select_by_id(id_b) is not None
    # 标签绑定关系恢复
    code, rows, _msg = search_service.filter_by_tag(tag_id)
    assert {row["id"] for row in rows} == {id_a}
    # 笔记恢复
    note_service2 = NoteManageService()
    note_service2.flush_all()
    contents = {n["note_content"] for n in note_service2.get_notes_by_lit(id_a)}
    assert "重点段落批注E2E" in contents
    assert note_service2.get_global_note(id_a)["note_content"] == "全局总结E2E"
    # 备份记录自身也在恢复后的库中
    from data_layer.dao.backup_dao import BackupRecordDao
    assert BackupRecordDao().get_by_id(record["id"]) is not None

    # ---------- 10. 会话状态配置持久化 ----------
    save_setting(C.CFG_LAST_PAGE, "2")
    search_state = {"keyword": "深度学习", "tag_id": tag_id,
                    "year": None, "literature_type": C.LIT_TYPE_TXT}
    save_setting(C.CFG_SEARCH_STATE, json.dumps(search_state, ensure_ascii=False))
    assert get_setting(C.CFG_LAST_PAGE) == "2"
    restored = json.loads(get_setting(C.CFG_SEARCH_STATE))
    assert restored["keyword"] == "深度学习"

    # ---------- 11. 重要操作均已写操作日志 ----------
    log_titles = " ".join(
        str(row.get("operate_content", ""))
        for row in OperationLogDao().get_recent(500)
    )
    assert "备份" in log_titles
