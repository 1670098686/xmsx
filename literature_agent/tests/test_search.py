"""阶段2 B2-9：分类检索业务单元测试。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from business.literature_import import LiteratureImportService
from business.literature_search import LiteratureSearchService
from business.note_manage import NoteManageService
import business.literature_search as search_module
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.tag_dao import CategoryTagDao
from data_layer.db_connect import DatabaseManager


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


def test_get_category_tree_options_indented(mem_conn):
    """分类下拉选项按分类树层级缩进，且父分类紧邻子分类之前。"""
    from business.tag_category import TagCategoryService
    tag_svc = TagCategoryService()
    roots = {c["tag_name"]: c["id"] for c in tag_svc.tag_dao.get_all_categories()}
    code, child_id, msg = tag_svc.create_category("综述子类Z", roots["综述类"])
    assert code == 0, msg

    options = LiteratureSearchService().get_category_tree_options()
    by_id = {opt["id"]: opt["text"] for opt in options}
    order = [opt["id"] for opt in options]
    # 根分类不缩进，子分类带缩进与层级连接线
    assert by_id[roots["期刊论文"]] == "期刊论文"
    assert by_id[child_id].startswith("　└ ")
    # 子分类紧跟父分类
    assert order.index(child_id) == order.index(roots["综述类"]) + 1


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


def test_get_lit_ids_with_report(mem_conn, library):
    """删除前预览：仅返回数据库中已存有解析报告的文献 id。"""
    from business.literature_parse import LiteratureParseService
    from data_layer.dao.report_dao import LiteratureReportDao

    service = LiteratureSearchService()
    all_ids = [library["dl"], library["nlp"], library["bio"]]
    assert service.get_lit_ids_with_report(all_ids) == []
    assert service.get_lit_ids_with_report([]) == []

    assert LiteratureParseService().parse_single(library["bio"])[0] == 0
    assert service.get_lit_ids_with_report(all_ids) == [library["bio"]]
    # 重复 id 去重并保持入参顺序
    assert service.get_lit_ids_with_report(
        [library["bio"], library["dl"], library["bio"]]
    ) == [library["bio"]]

    # 文献删除后报告随级联清除
    service.delete_literature(library["bio"])
    assert LiteratureReportDao().get_by_lit_id(library["bio"]) is None
    assert service.get_lit_ids_with_report(all_ids) == []


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


# ================= 解析状态 / 入库时间段 / DOC 格式筛选 =================

def test_filter_by_parsed_status(mem_conn, library):
    """已解析/未解析分类筛选：按 is_parsed 是否等于 PARSE_DONE 划分。"""
    from business.literature_parse import LiteratureParseService

    assert LiteratureParseService().parse_single(library["bio"])[0] == 0
    service = LiteratureSearchService()

    code, rows, _ = service.full_text_search(
        "", {"parsed_status": C.PARSE_FILTER_DONE}
    )
    assert [r["id"] for r in rows] == [library["bio"]]

    code, rows, _ = service.full_text_search(
        "", {"parsed_status": C.PARSE_FILTER_TODO}
    )
    assert {r["id"] for r in rows} == {library["dl"], library["nlp"]}

    # all 与缺省等价，返回全部
    code, rows_all, _ = service.full_text_search(
        "", {"parsed_status": C.PARSE_FILTER_ALL}
    )
    assert len(rows_all) == 3


def test_filter_doc_type_distinct_from_docx(mem_conn, library):
    """DOC 与 DOCX 为独立格式筛选值，互不串档。"""
    LiteratureInfoDao().update_by_id(
        library["bio"], {"literature_type": C.LIT_TYPE_DOC}
    )
    service = LiteratureSearchService()

    code, rows, _ = service.full_text_search(
        "", {"literature_type": C.LIT_TYPE_DOC}
    )
    assert [r["id"] for r in rows] == [library["bio"]]

    code, rows, _ = service.full_text_search(
        "", {"literature_type": C.LIT_TYPE_DOCX}
    )
    assert rows == []


def test_filter_by_time_range(mem_conn, library):
    """入库时间段：400 天前的文献只命中“一年以前”，不命中“今年/本月/本周”。"""
    conn = DatabaseManager().get_conn()
    old_text = (
        datetime.now(timezone.utc) - timedelta(days=400)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE literature_info SET create_time = ? WHERE id = ?",
        (old_text, library["bio"]),
    )
    conn.commit()

    service = LiteratureSearchService()
    recent_ids = {library["dl"], library["nlp"]}

    code, rows, _ = service.full_text_search(
        "", {"time_range": C.TIME_RANGE_OLDER_THAN_YEAR}
    )
    assert [r["id"] for r in rows] == [library["bio"]]

    for range_key in (C.TIME_RANGE_THIS_YEAR, C.TIME_RANGE_THIS_MONTH,
                      C.TIME_RANGE_THIS_WEEK):
        code, rows, _ = service.full_text_search("", {"time_range": range_key})
        assert {r["id"] for r in rows} == recent_ids

    # 全部时间不受限
    code, rows, _ = service.full_text_search(
        "", {"time_range": C.TIME_RANGE_ALL}
    )
    assert len(rows) == 3


def test_resolve_time_range_bounds():
    """时间段边界：本周以周一零点为起点；今年/本月起点为 1 日；一年以前为
    当前时刻回溯 365 天的开区间上界；全部/未知值不施加边界。"""
    monday = date(2026, 9, 28)  # 周一
    start, end = search_module._resolve_time_range_bounds(
        C.TIME_RANGE_THIS_WEEK, monday
    )
    assert end is None
    assert start == search_module._local_to_utc_text(
        datetime(2026, 9, 28, 0, 0)
    )

    start, _ = search_module._resolve_time_range_bounds(
        C.TIME_RANGE_THIS_MONTH, monday
    )
    assert start == search_module._local_to_utc_text(
        datetime(2026, 9, 1, 0, 0)
    )

    start, _ = search_module._resolve_time_range_bounds(
        C.TIME_RANGE_THIS_YEAR, monday
    )
    assert start == search_module._local_to_utc_text(
        datetime(2026, 1, 1, 0, 0)
    )

    # 周中（周日）回退到本周一
    sunday = date(2026, 10, 4)
    start, _ = search_module._resolve_time_range_bounds(
        C.TIME_RANGE_THIS_WEEK, sunday
    )
    assert start == search_module._local_to_utc_text(
        datetime(2026, 9, 28, 0, 0)
    )

    start, end = search_module._resolve_time_range_bounds(
        C.TIME_RANGE_OLDER_THAN_YEAR, monday
    )
    assert start is None
    end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    assert timedelta(days=364) < datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - end_dt < timedelta(days=366)

    assert search_module._resolve_time_range_bounds(
        C.TIME_RANGE_ALL, monday
    ) is None
    assert search_module._resolve_time_range_bounds("unknown", monday) is None
