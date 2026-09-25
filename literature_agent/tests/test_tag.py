"""阶段3 B3-5：标签分类管理业务测试（分类树/删除校验/拖拽布局/事务绑定/预设）。"""
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


def test_save_category_layout_reorder_and_reparent(mem_conn):
    """拖拽布局：同级重排与子分类↔根分类的层级调整都能落库。"""
    service = TagCategoryService()
    seed_roots = [c["id"] for c in service.tag_dao.get_children(0)]
    _, a, _ = service.create_category("根甲")
    _, b, _ = service.create_category("根乙")
    _, child, _ = service.create_category("子类", a)

    # 初始状态：种子根分类在前，随后根甲、根乙；子类挂在根甲下
    tree = service.get_categories_tree()
    root_a = next(node for node in tree if node["id"] == a)
    assert [node["id"] for node in tree] == seed_roots + [a, b]
    assert [node["id"] for node in root_a["children"]] == [child]

    # 布局1：根乙排到根甲前；子类提升为根分类（排末尾）；种子分类保持不变
    base = len(seed_roots)
    layout = [
        {"id": seed_id, "parent_id": 0, "sort_order": index + 1}
        for index, seed_id in enumerate(seed_roots)
    ]
    layout += [
        {"id": b, "parent_id": 0, "sort_order": base + 1},
        {"id": a, "parent_id": 0, "sort_order": base + 2},
        {"id": child, "parent_id": 0, "sort_order": base + 3},
    ]
    code, _d, msg = service.save_category_layout(child, layout)
    assert code == C.CODE_SUCCESS, msg
    tree = service.get_categories_tree()
    assert [node["id"] for node in tree] == seed_roots + [b, a, child]
    assert next(node for node in tree if node["id"] == a)["children"] == []

    # 布局2：把根分类 child 再挂回根乙下
    layout = [
        {"id": seed_id, "parent_id": 0, "sort_order": index + 1}
        for index, seed_id in enumerate(seed_roots)
    ]
    layout += [
        {"id": b, "parent_id": 0, "sort_order": base + 1},
        {"id": a, "parent_id": 0, "sort_order": base + 2},
        {"id": child, "parent_id": b, "sort_order": 1},
    ]
    assert service.save_category_layout(child, layout)[0] == C.CODE_SUCCESS
    tree = service.get_categories_tree()
    assert [node["id"] for node in tree] == seed_roots + [b, a]
    root_b = next(node for node in tree if node["id"] == b)
    assert [node["id"] for node in root_b["children"]] == [child]


def test_save_category_layout_rejects_invalid(mem_conn):
    """拖拽布局：挂到自身/不存在父级/同名冲突一律拒绝。"""
    service = TagCategoryService()
    _, a, _ = service.create_category("根甲X")
    _, b, _ = service.create_category("根乙X")
    _, child, _ = service.create_category("子类X", a)

    # 目标父级不存在
    bad_parent = [
        {"id": a, "parent_id": 9999, "sort_order": 1},
        {"id": b, "parent_id": 0, "sort_order": 1},
        {"id": child, "parent_id": a, "sort_order": 1},
    ]
    assert service.save_category_layout(a, bad_parent)[0] == C.CODE_FILE_NOT_FOUND

    # 挂到自身下
    self_parent = [
        {"id": a, "parent_id": a, "sort_order": 1},
        {"id": b, "parent_id": 0, "sort_order": 1},
        {"id": child, "parent_id": a, "sort_order": 1},
    ]
    assert service.save_category_layout(a, self_parent)[0] == C.CODE_FILE_INVALID

    # 与目标层级已有分类同名
    _, dup_child, _ = service.create_category("根乙X", a)
    same_name = [
        {"id": a, "parent_id": 0, "sort_order": 1},
        {"id": b, "parent_id": 0, "sort_order": 2},
        {"id": child, "parent_id": a, "sort_order": 1},
        {"id": dup_child, "parent_id": 0, "sort_order": 3},
    ]
    assert service.save_category_layout(dup_child, same_name)[0] == C.CODE_DUPLICATE

    # 被拖动节点不存在 / 布局缺少被拖动节点
    assert service.save_category_layout(9999, [])[0] == C.CODE_FILE_NOT_FOUND
    assert service.save_category_layout(
        a, [{"id": b, "parent_id": 0, "sort_order": 1}]
    )[0] == C.CODE_FILE_INVALID


def test_new_category_appends_sort_order(mem_conn):
    """新增分类自动排到同级末尾（种子根分类之后）。"""
    service = TagCategoryService()
    seed_count = len(service.tag_dao.get_children(0))
    _, first, _ = service.create_category("第一个根分类")
    _, second, _ = service.create_category("第二个根分类")
    _, sub, _ = service.create_category("子分类", first)
    dao = service.tag_dao
    assert dao.get_by_id(first)["sort_order"] == seed_count + 1
    assert dao.get_by_id(second)["sort_order"] == seed_count + 2
    assert dao.get_by_id(sub)["sort_order"] == 1
    # move_category 移动到新父级后排到同级末尾
    assert service.move_category(sub, 0)[0] == C.CODE_SUCCESS
    assert dao.get_by_id(sub)["parent_id"] == 0
    assert dao.get_by_id(sub)["sort_order"] == seed_count + 3


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
