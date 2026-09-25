"""阶段2 B2-9：分类检索业务单元测试。"""
import pytest

from business.literature_import import LiteratureImportService
from business.literature_search import LiteratureSearchService
from business.note_manage import NoteManageService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.tag_dao import CategoryTagDao


def _import_one(tmp_path, filename, title, content):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg
    # 导入标题取文件名，这里直接更新成测试标题便于断言
    from data_layer.dao.lit_info_dao import LiteratureInfoDao
    LiteratureInfoDao().update_by_id(
        data["id"], {"literature_title": title, "publish_time": "2025-03"}
    )
    return data["id"]


@pytest.fixture
def library(tmp_path):
    """构造 3 篇文献的小资料库。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    ids = {
        "dl": _import_one(tmp_path, "dl.txt", "深度学习综述",
                          "卷积神经网络与图像识别研究。"),
        "nlp": _import_one(tmp_path, "nlp.txt", "自然语言处理进展",
                           "Transformer 大模型与机器翻译。"),
        "bio": _import_one(tmp_path, "bio.txt", "基因编辑技术观察",
                           "CRISPR 基因治疗临床研究。"),
    }
    return ids


def test_get_all_literature(mem_conn, library):
    code, rows, _ = LiteratureSearchService().get_all_literature()
    assert code == 0 and len(rows) == 3
    assert all("tags" in row for row in rows)


def test_keyword_search_title_and_content(mem_conn, library):
    service = LiteratureSearchService()
    code, rows, _ = service.full_text_search("深度学习")
    assert code == 0 and [r["id"] for r in rows] == [library["dl"]]

    # 命中正文（已解析报告中的核心段落）
    from business.literature_parse import LiteratureParseService
    LiteratureParseService().parse_single(library["bio"])
    code, rows, _ = service.full_text_search("CRISPR")
    assert [r["id"] for r in rows] == [library["bio"]]

    # 空结果
    code, rows, _ = service.full_text_search("量子引力弦理论")
    assert rows == []


def test_search_matches_note_content(mem_conn, library):
    """全文检索可命中笔记内容。"""
    code, _n, _ = NoteManageService().add_global_note(
        library["dl"], "课堂汇报用的重点文献"
    )
    assert code == 0
    code, rows, _ = LiteratureSearchService().full_text_search("课堂汇报")
    assert [r["id"] for r in rows] == [library["dl"]]


def test_filter_by_tag_and_category(mem_conn, library):
    service = LiteratureSearchService()
    tag = CategoryTagDao().insert({
        "tag_type": C.TAG_TYPE_LABEL, "tag_name": "自定义标签X",
        "tag_color": "#52C41A", "parent_id": 0, "sort_order": 9,
    })
    category = CategoryTagDao().insert({
        "tag_type": C.TAG_TYPE_CATEGORY, "tag_name": "自定义分类X",
        "tag_color": "#000000", "parent_id": 0, "sort_order": 9,
    })
    service.bind_tags(library["nlp"], [tag])
    service.assign_category([library["nlp"], library["dl"]], category)

    code, rows, _ = service.filter_by_tag(tag)
    assert [r["id"] for r in rows] == [library["nlp"]]
    assert rows[0]["tags"][0]["tag_name"] == "自定义标签X"

    code, rows, _ = service.filter_by_category(category)
    assert {r["id"] for r in rows} == {library["nlp"], library["dl"]}


def test_combined_filters(mem_conn, library):
    service = LiteratureSearchService()
    code, rows, _ = service.full_text_search(
        "综述", {"literature_type": C.LIT_TYPE_TXT, "publish_time": "2025"}
    )
    assert [r["id"] for r in rows] == [library["dl"]]

    # 格式不匹配 → 空
    code, rows, _ = service.full_text_search("综述", {"literature_type": "PDF"})
    assert rows == []


def test_bind_tags_replace_semantics(mem_conn, library):
    service = LiteratureSearchService()
    t1 = CategoryTagDao().insert({"tag_type": 2, "tag_name": "T1", "tag_color": "#fff"})
    t2 = CategoryTagDao().insert({"tag_type": 2, "tag_name": "T2", "tag_color": "#fff"})
    service.bind_tags(library["dl"], [t1])
    service.bind_tags(library["dl"], [t2])
    tags = service.get_lit_tags(library["dl"])
    assert [t["id"] for t in tags] == [t2]

    # 批量追加保留并集
    service.batch_bind_tags([library["dl"]], [t1])
    assert {t["id"] for t in service.get_lit_tags(library["dl"])} == {t1, t2}


def test_delete_cascades(mem_conn, library):
    service = LiteratureSearchService()
    NoteManageService().add_global_note(library["dl"], "待删笔记")
    code, _d, msg = service.delete_literature(library["dl"])
    assert code == 0 and msg == "已删除"
    code, rows, _ = service.get_all_literature()
    assert len(rows) == 2
    assert NoteManageService().get_notes_by_lit(library["dl"]) == []


def test_batch_delete(mem_conn, library):
    code, data, _ = LiteratureSearchService().batch_delete(
        [library["dl"], library["bio"]]
    )
    assert code == 0 and data["success"] == 2 and data["failed"] == 0
    code, rows, _ = LiteratureSearchService().get_all_literature()
    assert len(rows) == 1


def test_filter_options(mem_conn, library):
    options = LiteratureSearchService().get_filter_options()
    assert "2025" in options["years"]
    assert C.LIT_TYPE_TXT in options["types"]
    # 有发表时间的年份显示为“YYYY 年”
    assert options["year_labels"]["2025"] == "2025 年"


def test_assign_category_attached_to_rows(mem_conn, library):
    """归类后检索结果携带 category 信息，未归类为 None。"""
    service = LiteratureSearchService()
    category = CategoryTagDao().insert({
        "tag_type": C.TAG_TYPE_CATEGORY, "tag_name": "测试分类",
        "tag_color": "#E8F4F8", "parent_id": 0, "sort_order": 9,
    })
    code, _d, msg = service.assign_category([library["dl"]], category)
    assert code == 0 and msg == "归类成功"

    code, rows, _ = service.get_all_literature()
    row_map = {row["id"]: row for row in rows}
    assert row_map[library["dl"]]["category"]["tag_name"] == "测试分类"
    assert row_map[library["nlp"]]["category"] is None

    # 顶部分类筛选可用
    code, rows, _ = service.full_text_search("", {"category_id": category})
    assert [r["id"] for r in rows] == [library["dl"]]


def test_year_filter_falls_back_to_import_year(mem_conn, tmp_path):
    """无发表时间的文献按入库年份出现在时间选项与筛选结果中。"""
    import datetime

    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    path = tmp_path / "nodate.txt"
    path.write_text("没有发表年份的文献内容。", encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg

    service = LiteratureSearchService()
    current_year = str(datetime.datetime.now().year)
    options = service.get_filter_options()
    assert current_year in options["years"]
    assert options["year_labels"][current_year] == f"{current_year} 年（入库）"

    code, rows, _ = service.full_text_search("", {"publish_time": current_year})
    assert [r["id"] for r in rows] == [data["id"]]

    # 有发表年份的文献即使今年入库，也不出现在“入库年份”桶中
    from data_layer.dao.lit_info_dao import LiteratureInfoDao
    dated = tmp_path / "dated.txt"
    dated.write_text("有年份", encoding="utf-8")
    code, dated_info, _ = LiteratureImportService().import_single(str(dated))
    assert code == 0
    LiteratureInfoDao().update_by_id(dated_info["id"], {"publish_time": "2024"})
    code, rows, _ = service.full_text_search("", {"publish_time": current_year})
    assert [r["id"] for r in rows] == [data["id"]]
    code, rows, _ = service.full_text_search("", {"publish_time": "2024"})
    assert [r["id"] for r in rows] == [dated_info["id"]]

    code, rows, _ = service.full_text_search("", {"publish_time": "1999"})
    assert rows == []


def test_attach_tags_hides_category_duplicate(mem_conn, library):
    """旧数据残留的“与分类同名标签”不在卡片上重复展示。"""
    from business.tag_category import TagCategoryService

    service = LiteratureSearchService()
    cat_review = next(
        c["id"] for c in service.get_all_categories() if c["tag_name"] == "综述类"
    )
    # DAO 直插绕过业务校验，模拟旧版产生的同名标签
    collision_tag = CategoryTagDao().insert({
        "tag_type": C.TAG_TYPE_LABEL, "tag_name": "综述类",
        "tag_color": "#13C2C2", "parent_id": 0, "sort_order": 9,
    })
    other_tag = CategoryTagDao().insert({
        "tag_type": C.TAG_TYPE_LABEL, "tag_name": "自定义精读X",
        "tag_color": "#52C41A", "parent_id": 0, "sort_order": 9,
    })
    code_a, _d_a, msg_a = service.assign_category([library["dl"]], cat_review)
    assert code_a == 0, msg_a
    code, _d, msg = TagCategoryService().bind_tags_to_lit(
        library["dl"], [collision_tag, other_tag]
    )
    assert code == 0, msg

    code, rows, _ = service.get_all_literature()
    row = next(r for r in rows if r["id"] == library["dl"])
    assert row["category"]["tag_name"] == "综述类"
    assert [t["tag_name"] for t in row["tags"]] == ["自定义精读X"]

    # 未归类文献不受去重影响，同名标签仍正常显示
    code, rows, _ = service.get_all_literature()
    row_nlp = next(r for r in rows if r["id"] == library["nlp"])
    assert row_nlp["category"] is None
