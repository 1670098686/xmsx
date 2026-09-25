"""阶段3 B3-5：标签分类管理业务测试（分类树/删除校验/事务绑定/合并/预设）。"""
import pytest

from business.literature_import import LiteratureImportService
from business.tag_category import TagCategoryService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_tag_dao import LiteratureTagDao


@pytest.fixture
def storage(tmp_path):
    """重定向文献存储目录。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "lit_store"))


def _import_one(tmp_path, name="paper.txt", content="研究背景：标签管理测试。\n"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    code, lit, msg = LiteratureImportService().import_single(str(path))
    assert code == C.CODE_SUCCESS, msg
    return lit


def test_create_category_and_tree(mem_conn):
    """创建父子分类并递归组装分类树。"""
    service = TagCategoryService()
    code, parent_id, msg = service.create_category("计算机")
    assert code == C.CODE_SUCCESS, msg
    code, child_id, msg = service.create_category("机器学习", parent_id)
    assert code == C.CODE_SUCCESS, msg

    tree = service.get_categories_tree()
    root = next(node for node in tree if node["id"] == parent_id)
    assert root["name"] == "计算机"
    assert root["children"][0]["id"] == child_id

    # 重名 / 空名校验
    code, _i, _m = service.create_category("计算机")
    assert code == C.CODE_DUPLICATE
    code, _i, _m = service.create_category("  ")
    assert code == C.CODE_FILE_INVALID
    code, _i, _m = service.create_category("孤儿", parent_id=9999)
    assert code == C.CODE_FILE_NOT_FOUND


def test_delete_category_guards(mem_conn, storage, tmp_path):
    """有子分类或有关联文献的分类禁止删除；空分类可删。"""
    service = TagCategoryService()
    code, parent_id, _ = service.create_category("父类")
    code, child_id, _ = service.create_category("子类", parent_id)

    code, _d, msg = service.delete_category(parent_id)
    assert code == C.CODE_FILE_INVALID
    assert "子分类" in msg

    lit = _import_one(tmp_path)
    from business.literature_search import LiteratureSearchService
    code, _d, _m = LiteratureSearchService().assign_category([lit["id"]], child_id)
    assert code == C.CODE_SUCCESS
    code, _d, msg = service.delete_category(child_id)
    assert code == C.CODE_FILE_INVALID
    assert "篇文献" in msg

    # 移出文献、删除子分类后父分类可删
    from data_layer.dao.lit_info_dao import LiteratureInfoDao
    LiteratureInfoDao().update_by_id(lit["id"], {"category_id": 0})
    assert service.delete_category(child_id)[0] == C.CODE_SUCCESS
    assert service.delete_category(parent_id)[0] == C.CODE_SUCCESS
    assert service.delete_category(9999)[0] == C.CODE_FILE_NOT_FOUND


def test_move_category_cycle_guard(mem_conn):
    """禁止移动到自身或自己的子孙下。"""
    service = TagCategoryService()
    _, a, _ = service.create_category("A")
    _, b, _ = service.create_category("B", a)
    _, c, _ = service.create_category("C", b)

    assert service.move_category(a, a)[0] == C.CODE_FILE_INVALID
    assert service.move_category(a, b)[0] == C.CODE_FILE_INVALID
    assert service.move_category(a, c)[0] == C.CODE_FILE_INVALID
    assert service.move_category(c, 0)[0] == C.CODE_SUCCESS


def test_tag_crud_bind_and_count(mem_conn, storage, tmp_path):
    """标签 CRUD、文献绑定事务替换、绑定数统计。"""
    service = TagCategoryService()
    code, tag1, msg = service.create_tag("待精读", "#52C41A")
    assert code == C.CODE_SUCCESS, msg
    code, tag2, _ = service.create_tag("方法参考")
    assert code == C.CODE_SUCCESS
    assert service.create_tag("待精读")[0] == C.CODE_DUPLICATE

    lit = _import_one(tmp_path)
    code, _d, msg = service.bind_tags_to_lit(lit["id"], [tag1, tag2])
    assert code == C.CODE_SUCCESS, msg
    assert LiteratureTagDao().get_tag_ids_by_lit(lit["id"]) == sorted([tag1, tag2])

    labels = {item["id"]: item for item in service.list_labels_with_count()}
    assert labels[tag1]["lit_count"] == 1

    # 全量替换：去掉 tag1
    assert service.bind_tags_to_lit(lit["id"], [tag2])[0] == C.CODE_SUCCESS
    labels = {item["id"]: item for item in service.list_labels_with_count()}
    assert labels[tag1]["lit_count"] == 0
    assert labels[tag2]["lit_count"] == 1

    # 非法标签 / 不存在文献
    assert service.bind_tags_to_lit(lit["id"], [9999])[0] == C.CODE_FILE_INVALID
    assert service.bind_tags_to_lit(9999, [tag2])[0] == C.CODE_FILE_NOT_FOUND

    # 改名与改色（避开种子预设标签名）
    assert service.update_tag(tag1, {"tag_name": "重点精读X"})[0] == C.CODE_SUCCESS
    assert service.update_tag(tag1, {"tag_name": "  "})[0] == C.CODE_FILE_INVALID


def test_delete_tag_cascades_bindings(mem_conn, storage, tmp_path):
    """删除标签后 literature_tag 绑定由外键级联清理。"""
    service = TagCategoryService()
    _, tag_id, _ = service.create_tag("临时标签")
    lit = _import_one(tmp_path)
    service.bind_tags_to_lit(lit["id"], [tag_id])

    assert service.delete_tag(tag_id)[0] == C.CODE_SUCCESS
    assert LiteratureTagDao().count_by_tag(tag_id) == 0
    assert service.delete_tag(tag_id)[0] == C.CODE_FILE_NOT_FOUND


def test_merge_tag_migrates_bindings(mem_conn, storage, tmp_path):
    """合并标签：绑定迁移到目标标签，源标签删除。"""
    service = TagCategoryService()
    _, src, _ = service.create_tag("合并源标签X")
    _, target, _ = service.create_tag("合并目标标签Y")
    lit1 = _import_one(tmp_path, name="a.txt", content="研究背景：第一篇。\n")
    lit2 = _import_one(tmp_path, name="b.txt", content="研究背景：第二篇。\n")
    service.bind_tags_to_lit(lit1["id"], [src])
    service.bind_tags_to_lit(lit2["id"], [src, target])

    code, _d, msg = service.merge_tag(src, target)
    assert code == C.CODE_SUCCESS, msg
    lit_tag_dao = LiteratureTagDao()
    assert lit_tag_dao.count_by_tag(target) == 2
    assert lit_tag_dao.count_by_tag(src) == 0

    assert service.merge_tag(target, target)[0] == C.CODE_FILE_INVALID
    assert service.merge_tag(9998, 9999)[0] == C.CODE_FILE_NOT_FOUND


def test_apply_preset_labels_idempotent(mem_conn):
    """套用预设模板写入预设标签，再次套用不重复插入。"""
    service = TagCategoryService()
    code, inserted, msg = service.apply_preset_labels()
    assert code == C.CODE_SUCCESS, msg
    assert inserted >= 4
    names_before = {t["tag_name"] for t in service.tag_dao.get_all_labels()}
    # 预设不得包含种子分类同名项“综述类”
    assert "综述类" not in names_before

    code, inserted2, _ = service.apply_preset_labels()
    assert code == C.CODE_SUCCESS
    assert inserted2 == 0
    names_after = {t["tag_name"] for t in service.tag_dao.get_all_labels()}
    assert names_before == names_after


def test_tag_category_cross_type_same_name_rejected(mem_conn):
    """标签与文献分类禁止跨类型同名（新建/改名/预设三个入口拦截）。"""
    service = TagCategoryService()
    # 种子已含分类“综述类”，不能再建同名标签
    code, _i, msg = service.create_tag("综述类")
    assert code == C.CODE_DUPLICATE and "分类" in msg

    # 先建标签，再建同名分类也要拒绝
    code, tag_y, _ = service.create_tag("研究方向Y")
    assert code == C.CODE_SUCCESS
    code, _i, msg = service.create_category("研究方向Y")
    assert code == C.CODE_DUPLICATE and "标签" in msg

    # 标签改名为已有分类名被拒
    code, _i, msg = service.update_tag(tag_y, {"tag_name": "期刊论文"})
    assert code == C.CODE_DUPLICATE and "分类" in msg

    # 套用预设同样不会产生分类同名标签
    service.apply_preset_labels()
    label_names = {t["tag_name"] for t in service.tag_dao.get_all_labels()}
    assert label_names.isdisjoint(
        {c["tag_name"] for c in service.tag_dao.get_all_categories()}
    )
