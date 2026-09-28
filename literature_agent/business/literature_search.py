"""分类检索业务：全文模糊检索、标签/分类/时间/格式组合筛选、批量操作。"""
import re
from datetime import date, datetime, timedelta

from config import constants as C
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.lit_tag_dao import LiteratureTagDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.dao.tag_dao import CategoryTagDao
from data_layer.db_connect import DatabaseManager
from utils.logger import get_logger

from business.literature_import import LiteratureImportService
from business.service_common import exception_to_code, write_operation_log

logger = get_logger()

# 合法四位年份（发表时间/入库时间提取用）
_YEAR_PATTERN = re.compile(r"^(\d{4})")

# 一年以前的回溯天数（滚动一周年）
_ONE_YEAR_DAYS = 365
_TIME_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _local_to_utc_text(local_dt: datetime) -> str:
    """把本地时区的朴素日期时间换算为 UTC 定长字符串（create_time 同格式）。"""
    utc_offset = datetime.now().astimezone().utcoffset()
    return (local_dt - utc_offset).strftime(_TIME_TEXT_FORMAT)


def _resolve_time_range_bounds(range_key: str,
                               today: date = None) -> tuple:
    """计算入库时间段的 UTC 闭开区间边界。

    Args:
        range_key: constants.TIME_RANGE_* 之一。
        today: 可选“今天”（本地日期），默认取系统当前日期，测试可注入。
    Returns:
        (start_utc, end_utc)：闭开区间，None 表示该侧不限；
        全部时间或无法识别的选项返回 None。
    """
    if range_key == C.TIME_RANGE_ALL:
        return None
    today = today or date.today()
    if range_key == C.TIME_RANGE_THIS_WEEK:
        # 周一为一周起点
        start = datetime.combine(
            today - timedelta(days=today.weekday()), datetime.min.time()
        )
        return _local_to_utc_text(start), None
    if range_key == C.TIME_RANGE_THIS_MONTH:
        start = datetime.combine(today.replace(day=1), datetime.min.time())
        return _local_to_utc_text(start), None
    if range_key == C.TIME_RANGE_THIS_YEAR:
        start = datetime.combine(today.replace(month=1, day=1),
                                 datetime.min.time())
        return _local_to_utc_text(start), None
    if range_key == C.TIME_RANGE_OLDER_THAN_YEAR:
        end = datetime.now() - timedelta(days=_ONE_YEAR_DAYS)
        return None, _local_to_utc_text(end)
    return None


class LiteratureSearchService:
    """文献检索业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.lit_dao = LiteratureInfoDao()
        self.tag_dao = CategoryTagDao()
        self.lit_tag_dao = LiteratureTagDao()
        self.report_dao = LiteratureReportDao()
        self._import_service = LiteratureImportService()

    # ================= 检索 =================

    def get_all_literature(self, sort_by: str = "create_time",
                           order: str = "desc") -> tuple:
        """获取全部文献（卡片数据含标签）。

        Returns:
            (code, [lit_dict,...], msg)
        """
        try:
            rows = self.lit_dao.search({"sort_by": sort_by, "order": order})
            return C.CODE_SUCCESS, self._attach_tags(rows), ""
        except Exception as exc:
            return exception_to_code(exc), [], getattr(exc, "message", str(exc))

    def full_text_search(self, keyword: str, filters: dict = None) -> tuple:
        """全文模糊检索（标题/作者/来源/笔记/报告内容）+ 组合条件。

        Args:
            keyword: 关键词（可为空）。
            filters: tag_id/category_id/literature_type/publish_time/
                parsed_status(all/done/todo)/time_range(all/older_than_year/
                this_year/this_month/this_week)/sort_by/order。
        Returns:
            (code, [lit_dict,...], msg)
        """
        try:
            query = dict(filters or {})
            query["keyword"] = keyword or ""
            self._apply_parsed_status(query)
            self._apply_time_range(query)
            rows = self.lit_dao.search(query)
            return C.CODE_SUCCESS, self._attach_tags(rows), ""
        except Exception as exc:
            return exception_to_code(exc), [], getattr(exc, "message", str(exc))

    @staticmethod
    def _apply_parsed_status(query: dict) -> None:
        """把 UI 的解析状态选项翻译为 DAO 状态条件（原地写入并移除原键）。"""
        status = query.pop("parsed_status", C.PARSE_FILTER_ALL)
        if status == C.PARSE_FILTER_DONE:
            query["is_parsed_eq"] = C.PARSE_DONE
        elif status == C.PARSE_FILTER_TODO:
            query["is_parsed_neq"] = C.PARSE_DONE

    @staticmethod
    def _apply_time_range(query: dict) -> None:
        """把入库时间段选项翻译为 create_time 闭开区间（原地写入并移除原键）。

        create_time 以 UTC 定长字符串存储（CURRENT_TIMESTAMP），边界按用户
        本地自然日计算后换算为 UTC，字符串可直接字典序比较。
        """
        range_key = query.pop("time_range", C.TIME_RANGE_ALL)
        bounds = _resolve_time_range_bounds(range_key)
        if bounds is None:
            return
        start_utc, end_utc = bounds
        if start_utc:
            query["create_time_from"] = start_utc
        if end_utc:
            query["create_time_to"] = end_utc

    def filter_by_category(self, category_id: int) -> tuple:
        """按分类筛选。

        Returns:
            (code, [lit_dict,...], msg)
        """
        try:
            rows = self.lit_dao.search({"category_id": category_id})
            return C.CODE_SUCCESS, self._attach_tags(rows), ""
        except Exception as exc:
            return exception_to_code(exc), [], getattr(exc, "message", str(exc))

    def filter_by_tag(self, tag_id: int) -> tuple:
        """按标签筛选。

        Returns:
            (code, [lit_dict,...], msg)
        """
        try:
            rows = self.lit_dao.search({"tag_id": tag_id})
            return C.CODE_SUCCESS, self._attach_tags(rows), ""
        except Exception as exc:
            return exception_to_code(exc), [], getattr(exc, "message", str(exc))

    # ================= 筛选项/标签 =================

    def get_all_tags(self) -> list:
        """返回全部个性化标签（tag_type=2）。"""
        return self.tag_dao.get_all_labels()

    def get_all_categories(self) -> list:
        """返回全部分类目录（tag_type=1）。"""
        return self.tag_dao.get_all_categories()

    def get_category_tree_options(self) -> list:
        """返回按分类树层级缩进的下拉选项（父节点在前、子节点紧随其后）。

        Returns:
            [{"id": 分类id, "text": 带全角空格缩进的显示文本}, ...]，
            根分类文本为分类原名，子分类按深度加缩进与层级连接线。
        """
        categories = self.tag_dao.get_all_categories()
        children_map = {}
        for category in categories:
            children_map.setdefault(category["parent_id"], []).append(category)

        options = []

        def walk(parent_id: int, depth: int) -> None:
            """深度优先追加某父级下的分类选项。"""
            for category in children_map.get(parent_id, []):
                name = category["tag_name"]
                text = name if depth == 0 else f"{'　' * depth}└ {name}"
                options.append({"id": category["id"], "text": text})
                walk(category["id"], depth + 1)

        walk(0, 0)
        return options

    def get_filter_options(self) -> dict:
        """组装检索页下拉筛选项（时间年份、文件格式）。

        年份优先取发表时间（publish_time）；未填写发表时间的文献回退取
        入库年份（create_time），并在 year_labels 中以“（入库）”标注，
        保证任何文献库的时间下拉都有可选项。

        Returns:
            {"years": [str,...], "year_labels": {year: 显示文本},
             "types": [str,...]}
        """
        publish_years, import_years, types = set(), set(), set()
        for lit in self.lit_dao.select_all():
            pub_match = _YEAR_PATTERN.match((lit.get("publish_time") or "").strip())
            if pub_match:
                publish_years.add(pub_match.group(1))
            else:
                in_match = _YEAR_PATTERN.match((lit.get("create_time") or "").strip())
                if in_match:
                    import_years.add(in_match.group(1))
            if lit.get("literature_type"):
                types.add(lit["literature_type"])
        years = sorted(publish_years, reverse=True)
        year_labels = {year: f"{year} 年" for year in years}
        for year in sorted(import_years, reverse=True):
            if year not in year_labels:
                years.append(year)
                year_labels[year] = f"{year} 年（入库）"
        return {"years": years, "year_labels": year_labels,
                "types": sorted(types)}

    def get_lit_tags(self, lit_id: int) -> list:
        """获取某篇文献已绑定的标签详情列表。"""
        tag_map = {tag["id"]: tag for tag in self.tag_dao.get_all()}
        return [
            tag_map[tag_id]
            for tag_id in self.lit_tag_dao.get_tag_ids_by_lit(lit_id)
            if tag_id in tag_map
        ]

    def _attach_tags(self, rows: list) -> list:
        """为文献列表批量附加 tags 与 category 字段（供卡片直接渲染）。

        - tags：literature_tag 绑定的个性化标签（tag_type=2 或其它已绑定项）；
        - category：category_id 指向的分类目录字典，未归类为 None。
        """
        all_tags = self.tag_dao.get_all()
        tag_map = {tag["id"]: tag for tag in all_tags}
        category_map = {
            cat["id"]: cat for cat in self.tag_dao.get_all_categories()
        }
        for lit in rows:
            tag_ids = self.lit_tag_dao.get_tag_ids_by_lit(lit["id"])
            category = category_map.get(int(lit.get("category_id") or 0))
            lit["category"] = category
            # 与所属分类同名的标签属于冗余标识（旧版数据可能残留），不再重复展示
            category_name = category["tag_name"] if category else None
            lit["tags"] = [
                tag_map[tid] for tid in tag_ids
                if tid in tag_map
                and tag_map[tid].get("tag_name") != category_name
            ]
        return rows

    # ================= 删除/归类/绑定 =================

    def get_lit_ids_with_report(self, lit_ids: list) -> list:
        """返回给定文献中数据库里已存有解析报告的文献 id。

        供删除前确认弹窗统计“将一并删除多少份解析报告”。

        Args:
            lit_ids: 待判定的文献 id 列表。
        Returns:
            存在解析报告的文献 id 列表（去重，保持入参顺序）。
        """
        try:
            return self.report_dao.select_lit_ids_with_report(lit_ids or [])
        except Exception as exc:
            logger.warning("查询文献解析报告存在性失败：%s", exc)
            return []

    def delete_literature(self, lit_id: int) -> tuple:
        """删除单篇（委托导入服务，保证级联与日志一致）。

        数据库中的解析报告、笔记由外键 ON DELETE CASCADE 随文献一并清除；
        调用方（UI）必须先完成“是否一并删除解析报告”的二次确认。
        """
        return self._import_service.delete_literature(lit_id)

    def batch_delete(self, lit_ids: list) -> tuple:
        """批量删除。

        Returns:
            (code, {"success": int, "failed": int}, msg)
        """
        success, failed = 0, 0
        with DatabaseManager().write_lock:
            for lit_id in lit_ids:
                code, _data, _msg = self._import_service.delete_literature(lit_id)
                if code == C.CODE_SUCCESS:
                    success += 1
                else:
                    failed += 1
        if failed and not success:
            return C.CODE_ERROR, {"success": 0, "failed": failed}, "批量删除失败"
        return C.CODE_SUCCESS, {"success": success, "failed": failed}, "批量删除完成"

    def assign_category(self, lit_ids: list, category_id: int) -> tuple:
        """批量归类（修改 literature_info.category_id）。

        Returns:
            (code, None, msg)
        """
        try:
            with DatabaseManager().write_lock:
                for lit_id in lit_ids:
                    self.lit_dao.update_by_id(lit_id, {"category_id": int(category_id)})
            write_operation_log(
                C.OP_CONFIG, f"批量归类 {len(lit_ids)} 篇文献 → 分类 {category_id}"
            )
            return C.CODE_SUCCESS, None, "归类成功"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def bind_tags(self, lit_id: int, tag_ids: list) -> tuple:
        """为单篇文献全量替换标签绑定（事务先删后插）。

        Returns:
            (code, None, msg)
        """
        try:
            if not self.lit_dao.select_by_id(lit_id):
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
            with DatabaseManager().write_lock:
                self.lit_tag_dao.replace_tags(lit_id, [int(t) for t in tag_ids])
            write_operation_log(C.OP_CONFIG, f"修改文献 {lit_id} 的标签绑定", lit_id)
            return C.CODE_SUCCESS, None, "标签已更新"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def batch_bind_tags(self, lit_ids: list, tag_ids: list) -> tuple:
        """批量追加标签（保留每篇已有绑定）。

        Returns:
            (code, None, msg)
        """
        try:
            with DatabaseManager().write_lock:
                for lit_id in lit_ids:
                    existing = set(self.lit_tag_dao.get_tag_ids_by_lit(lit_id))
                    merged = sorted(existing | {int(t) for t in tag_ids})
                    self.lit_tag_dao.replace_tags(lit_id, merged)
            write_operation_log(
                C.OP_CONFIG, f"批量添加标签 {len(lit_ids)} 篇 → {tag_ids}"
            )
            return C.CODE_SUCCESS, None, "批量打标完成"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def get_lit_file_path(self, lit_id: int):
        """解析文献源文件绝对路径（供预览使用）。"""
        return self._import_service.get_lit_file_path(lit_id)
