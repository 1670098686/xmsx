"""阶段1 DAO 层单元测试。

覆盖：
- 9 张表建表与种子数据
- 各 DAO 基础 CRUD
- 多对多绑定/替换/级联删除
- 唯一约束、外键级联
- 系统配置 UPSERT
"""
import pytest

from config import constants as C
from data_layer.dao.backup_dao import BackupRecordDao
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.lit_tag_dao import LiteratureTagDao
from data_layer.dao.log_dao import OperationLogDao
from data_layer.dao.note_dao import LiteratureNoteDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.dao.rule_dao import ParseRuleDao
from data_layer.dao.tag_dao import CategoryTagDao


# ---------- 种子数据 ----------

def test_seed_parse_rules(mem_conn):
    """默认通用规则及文/理/工预设规则共 4 条，且有且仅有 1 条默认规则。"""
    rules = ParseRuleDao(mem_conn).list_all()
    assert len(rules) == 4
    defaults = [r for r in rules if r["is_default"] == 1]
    assert len(defaults) == 1
    assert defaults[0]["id"] == 1
    assert '"innovation_point": true' in defaults[0]["rule_detail"]


def test_seed_category_tags(mem_conn):
    """预设 3 个分类 + 3 个标签。"""
    tag_dao = CategoryTagDao(mem_conn)
    assert len(tag_dao.get_all_categories()) == 3
    assert len(tag_dao.get_all_labels()) == 3


def test_seed_system_config(mem_conn):
    """系统配置种子写入且可读。"""
    config_dao = SystemConfigDao(mem_conn)
    assert config_dao.get(C.CFG_UI_STYLE) == "light"
    assert config_dao.get(C.CFG_AUTO_SAVE_TIME) == "30"
    assert config_dao.get(C.CFG_BACKUP_CYCLE) == "7"
    assert len(config_dao.get_all()) >= 12


# ---------- literature_info ----------

def _sample_lit(title="测试文献", file_hash="hash001"):
    return {
        "literature_title": title,
        "literature_author": "张三",
        "publish_time": "2026",
        "journal_source": "CVPR",
        "literature_type": C.LIT_TYPE_PDF,
        "file_path": "2026/test.pdf",
        "file_size": 1024,
        "file_hash": file_hash,
        "category_id": 0,
        "is_parsed": C.PARSE_NOT_STARTED,
    }


def test_lit_info_crud(mem_conn):
    dao = LiteratureInfoDao(mem_conn)
    lit_id = dao.insert(_sample_lit())
    assert lit_id > 0

    row = dao.select_by_id(lit_id)
    assert row["literature_title"] == "测试文献"
    assert row["is_parsed"] == 0

    assert dao.update_by_id(lit_id, {"is_parsed": C.PARSE_DONE}) == 1
    assert dao.select_by_id(lit_id)["is_parsed"] == 2

    assert dao.select_by_hash("hash001")["id"] == lit_id
    assert dao.select_by_hash("not-exist") is None
    assert dao.count_all() == 1


def test_lit_info_search(mem_conn):
    dao = LiteratureInfoDao(mem_conn)
    dao.insert(_sample_lit("深度学习综述", "h1"))
    dao.insert(_sample_lit("机器学习导论", "h2"))
    results = dao.search_by_title("深度")
    assert len(results) == 1
    assert results[0]["literature_title"] == "深度学习综述"


# ---------- literature_tag 多对多 ----------

def test_lit_tag_bind_and_replace(mem_conn):
    lit_dao = LiteratureInfoDao(mem_conn)
    tag_dao = CategoryTagDao(mem_conn)
    lt_dao = LiteratureTagDao(mem_conn)

    lit_id = lit_dao.insert(_sample_lit())
    t1 = tag_dao.insert({"tag_type": 2, "tag_name": "新标签A", "tag_color": "#fff"})
    t2 = tag_dao.insert({"tag_type": 2, "tag_name": "新标签B", "tag_color": "#eee"})

    lt_dao.bind(lit_id, t1)
    lt_dao.bind(lit_id, t1)  # 重复绑定应被 UNIQUE 忽略
    assert lt_dao.get_tag_ids_by_lit(lit_id) == [t1]
    assert lt_dao.get_lit_ids_by_tag(t1) == [lit_id]
    assert lt_dao.count_by_tag(t1) == 1

    # 全量替换为 t2
    lt_dao.replace_tags(lit_id, [t2])
    assert lt_dao.get_tag_ids_by_lit(lit_id) == [t2]
    assert lt_dao.count_by_tag(t1) == 0


def test_tag_unique_constraint(mem_conn):
    """同类型+同名+同父级的分类/标签唯一。"""
    import sqlite3
    tag_dao = CategoryTagDao(mem_conn)
    tag_dao.insert({"tag_type": 1, "tag_name": "重名", "parent_id": 0})
    with pytest.raises(sqlite3.IntegrityError):
        tag_dao.insert({"tag_type": 1, "tag_name": "重名", "parent_id": 0})


# ---------- 外键级联删除 ----------

def test_cascade_delete_literature(mem_conn):
    """删除文献时，报告、笔记、标签绑定应级联清理。"""
    lit_dao = LiteratureInfoDao(mem_conn)
    report_dao = LiteratureReportDao(mem_conn)
    note_dao = LiteratureNoteDao(mem_conn)
    tag_dao = CategoryTagDao(mem_conn)
    lt_dao = LiteratureTagDao(mem_conn)

    lit_id = lit_dao.insert(_sample_lit())
    tag_id = tag_dao.insert({"tag_type": 2, "tag_name": "级联标签"})
    lt_dao.bind(lit_id, tag_id)
    report_dao.insert({"literature_id": lit_id, "core_view": "观点"})
    note_dao.insert({"literature_id": lit_id, "note_content": "批注"})

    assert report_dao.get_by_lit_id(lit_id) is not None
    assert len(note_dao.get_by_lit_id(lit_id)) == 1
    assert lt_dao.count_by_tag(tag_id) == 1

    lit_dao.delete_by_id(lit_id)
    assert report_dao.get_by_lit_id(lit_id) is None
    assert note_dao.get_by_lit_id(lit_id) == []
    assert lt_dao.get_tag_ids_by_lit(lit_id) == []
    # 标签本体仍存在，只是绑定被清理
    assert tag_dao.get_by_id(tag_id) is not None


def test_report_rule_set_null(mem_conn):
    """删除解析规则后，报告的 rule_id 置 NULL 而非连带删除。"""
    lit_dao = LiteratureInfoDao(mem_conn)
    rule_dao = ParseRuleDao(mem_conn)
    report_dao = LiteratureReportDao(mem_conn)

    lit_id = lit_dao.insert(_sample_lit(file_hash="h-rule"))
    rule_id = rule_dao.insert({"rule_name": "临时规则", "is_default": 0})
    report_id = report_dao.insert({"literature_id": lit_id, "rule_id": rule_id})
    rule_dao.delete_by_id(rule_id)

    row = mem_conn.execute(
        "SELECT rule_id FROM literature_report WHERE id = ?", (report_id,)
    ).fetchone()
    assert row[0] is None


# ---------- note ----------

def test_note_crud(mem_conn):
    lit_dao = LiteratureInfoDao(mem_conn)
    note_dao = LiteratureNoteDao(mem_conn)
    lit_id = lit_dao.insert(_sample_lit(file_hash="h-note"))

    note_id = note_dao.insert({
        "literature_id": lit_id,
        "paragraph_pos": "p3",
        "note_content": "原文批注",
        "note_type": C.NOTE_TYPE_PARAGRAPH,
        "highlight_style": "#FAAD14",
    })
    assert note_dao.get_by_id(note_id)["note_content"] == "原文批注"
    note_dao.update_by_id(note_id, {"note_content": "修改后批注"})
    assert note_dao.get_by_id(note_id)["note_content"] == "修改后批注"
    assert note_dao.delete_by_id(note_id) == 1
    assert note_dao.get_by_id(note_id) is None


# ---------- parse_rule ----------

def test_rule_default_switch(mem_conn):
    rule_dao = ParseRuleDao(mem_conn)
    new_id = rule_dao.insert({"rule_name": "自定义", "is_default": 0})
    # 模拟业务层切换默认：先清旧默认再设新默认
    rule_dao.update_by_id(1, {"is_default": 0})
    rule_dao.update_by_id(new_id, {"is_default": 1})
    assert rule_dao.get_default()["id"] == new_id


# ---------- system_config ----------

def test_config_upsert(mem_conn):
    config_dao = SystemConfigDao(mem_conn)
    # 新键插入
    config_dao.set("custom_key", "v1", "自定义")
    assert config_dao.get("custom_key") == "v1"
    assert config_dao.get_desc("custom_key") == "自定义"
    # 同键更新，不新增行
    config_dao.set("custom_key", "v2")
    assert config_dao.get("custom_key") == "v2"
    rows = mem_conn.execute(
        "SELECT COUNT(1) FROM system_config WHERE config_key = 'custom_key'"
    ).fetchone()[0]
    assert rows == 1


def test_config_bulk_set(mem_conn):
    config_dao = SystemConfigDao(mem_conn)
    config_dao.bulk_set({"a": "1", "b": "2"})
    all_cfg = config_dao.get_all()
    assert all_cfg["a"] == "1" and all_cfg["b"] == "2"


# ---------- backup_record ----------

def test_backup_record_crud(mem_conn):
    dao = BackupRecordDao(mem_conn)
    backup_id = dao.insert({
        "backup_name": "20260924_full",
        "backup_path": "/backup/1.zip",
        "backup_size": 2048,
        "backup_type": C.BACKUP_TYPE_FULL,
    })
    assert dao.get_by_id(backup_id)["backup_name"] == "20260924_full"
    assert dao.get_latest()["id"] == backup_id
    assert len(dao.get_all()) == 1
    assert dao.delete_by_id(backup_id) == 1
    assert dao.get_latest() is None


# ---------- operation_log ----------

def test_operation_log(mem_conn):
    dao = OperationLogDao(mem_conn)
    dao.insert({
        "operate_type": C.OP_IMPORT,
        "operate_content": "导入 测试文献.pdf",
        "literature_id": 0,
        "operate_status": C.OP_STATUS_SUCCESS,
    })
    logs = dao.get_recent(10)
    assert len(logs) == 1
    assert logs[0]["operate_type"] == "导入"
    assert logs[0]["operate_status"] == 1


# ---------- 防 SQL 注入（参数化）----------

def test_parameterized_query_injection(mem_conn):
    dao = LiteratureInfoDao(mem_conn)
    dao.insert(_sample_lit(title="正常文献", file_hash="h-ok"))
    # 恶意输入只会作为普通字符串参与 LIKE，不会破坏查询
    results = dao.search_by_title("' OR 1=1 --")
    assert results == []


# ---------- schema v2 迁移：分类同名标签 ----------

def test_migrate_v2_collision_tags(mem_conn):
    """旧版“综述类”同名标签并入分类：未归类文献提升，已归类不覆盖，绑定级联清理。"""
    from data_layer.db_init import _migrate_v1_to_v2_collision_tags

    lit_dao = LiteratureInfoDao(mem_conn)
    tag_dao = CategoryTagDao(mem_conn)
    lt_dao = LiteratureTagDao(mem_conn)

    cat_review = next(
        c["id"] for c in tag_dao.get_all_categories() if c["tag_name"] == "综述类"
    )
    cat_journal = next(
        c["id"] for c in tag_dao.get_all_categories() if c["tag_name"] == "期刊论文"
    )
    # 模拟旧版漏洞数据：与分类同名的个性化标签
    collision_tag = tag_dao.insert(
        {"tag_type": 2, "tag_name": "综述类", "tag_color": "#13C2C2"}
    )
    normal_tag = tag_dao.insert(
        {"tag_type": 2, "tag_name": "自定义正常标签", "tag_color": "#52C41A"}
    )

    lit_unclassified = lit_dao.insert(_sample_lit("未归类文献", "h-u"))
    lit_classified = lit_dao.insert(_sample_lit("已归期刊论文", "h-j"))
    lit_dao.update_by_id(lit_classified, {"category_id": cat_journal})
    lt_dao.bind(lit_unclassified, collision_tag)
    lt_dao.bind(lit_classified, collision_tag)
    lt_dao.bind(lit_unclassified, normal_tag)

    removed, promoted = _migrate_v1_to_v2_collision_tags(mem_conn)
    assert (removed, promoted) == (1, 1)
    # 未归类文献提升到同名分类；已显式归类的不被覆盖
    assert lit_dao.select_by_id(lit_unclassified)["category_id"] == cat_review
    assert lit_dao.select_by_id(lit_classified)["category_id"] == cat_journal
    # 冗余标签删除、绑定级联清理；正常标签保留
    assert tag_dao.get_by_id(collision_tag) is None
    assert lt_dao.get_tag_ids_by_lit(lit_unclassified) == [normal_tag]
    assert lt_dao.get_tag_ids_by_lit(lit_classified) == []
    # 幂等：再次执行无操作
    assert _migrate_v1_to_v2_collision_tags(mem_conn) == (0, 0)
