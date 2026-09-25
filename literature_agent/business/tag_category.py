"""标签分类管理业务：分类树组装、分类/标签 CRUD、事务绑定、合并、预设模板。"""
from config import constants as C
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.lit_tag_dao import LiteratureTagDao
from data_layer.dao.tag_dao import CategoryTagDao
from data_layer.db_connect import DatabaseManager
from utils.logger import get_logger

from business.service_common import exception_to_code, write_operation_log

logger = get_logger()

# 标签预设模板（学术阅读常用，重复名称自动忽略）
# 注意：不得包含文献分类同名项（如“综述类”属于分类目录），
# 否则归类与标签会在卡片上出现两个同名标识。
PRESET_LABELS = [
    ("需精读", "#52C41A"),
    ("高创新性", "#FAAD14"),
    ("引用素材", "#4096bb"),
    ("待复现", "#722ED1"),
    ("存疑待查", "#FF7A45"),
    ("经典文献", "#EB2F96"),
    ("方法新颖", "#2F54EB"),
]

# 分类预设模板
PRESET_CATEGORIES = [
    ("综述类", "#E8F4F8"),
    ("期刊论文", "#E8F4F8"),
    ("学位论文", "#E8F4F8"),
    ("会议论文", "#E8F4F8"),
]


class TagCategoryService:
    """标签与分类管理业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.tag_dao = CategoryTagDao()
        self.lit_tag_dao = LiteratureTagDao()
        self.lit_dao = LiteratureInfoDao()

    # ================= 分类 =================

    def create_category(self, name: str, parent_id: int = 0,
                        color: str = C.DEFAULT_CATEGORY_COLOR) -> tuple:
        """创建分类目录。

        Args:
            name: 分类名称。
            parent_id: 父分类 id，0 为顶级。
            color: 分类颜色（十六进制）。
        Returns:
            (code, category_id, msg)
        """
        name = (name or "").strip()
        if not name:
            return C.CODE_FILE_INVALID, None, "分类名称不能为空"
        if parent_id and not self.tag_dao.get_by_id(parent_id):
            return C.CODE_FILE_NOT_FOUND, None, "父分类不存在"
        if self._find_same_name(C.TAG_TYPE_CATEGORY, name, parent_id):
            return C.CODE_DUPLICATE, None, "同级下已存在同名分类"
        if self._find_label_by_name(name):
            return C.CODE_DUPLICATE, None, f"已存在同名标签“{name}”，请先改名或删除该标签"
        try:
            category_id = self.tag_dao.insert({
                "tag_type": C.TAG_TYPE_CATEGORY,
                "tag_name": name,
                "tag_color": color,
                "parent_id": int(parent_id or 0),
            })
            write_operation_log(C.OP_CONFIG, f"新增分类：{name}")
            return C.CODE_SUCCESS, category_id, "分类创建成功"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def delete_category(self, cat_id: int) -> tuple:
        """删除分类（业务层校验子分类与关联文献，禁止非空删除）。

        Returns:
            (code, None, msg)
        """
        category = self.tag_dao.get_by_id(cat_id)
        if not category or category["tag_type"] != C.TAG_TYPE_CATEGORY:
            return C.CODE_FILE_NOT_FOUND, None, "分类不存在"
        children = self.tag_dao.get_children(cat_id)
        if children:
            return C.CODE_FILE_INVALID, None, "该分类下还有子分类，请先删除子分类"
        direct_count = self._count_lit_in_category(cat_id)
        if direct_count:
            return C.CODE_FILE_INVALID, None, f"该分类下还有 {direct_count} 篇文献，请先移出"
        try:
            with DatabaseManager().write_lock:
                self.tag_dao.delete_by_id(cat_id)
            write_operation_log(C.OP_CONFIG, f"删除分类：{category['tag_name']}")
            return C.CODE_SUCCESS, None, "分类已删除"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def move_category(self, cat_id: int, new_parent_id: int) -> tuple:
        """移动分类节点（禁止挂到自身或其子孙下）。"""
        category = self.tag_dao.get_by_id(cat_id)
        if not category or category["tag_type"] != C.TAG_TYPE_CATEGORY:
            return C.CODE_FILE_NOT_FOUND, None, "分类不存在"
        new_parent_id = int(new_parent_id or 0)
        if new_parent_id == cat_id:
            return C.CODE_FILE_INVALID, None, "不能将分类移动到自身下"
        if new_parent_id and not self.tag_dao.get_by_id(new_parent_id):
            return C.CODE_FILE_NOT_FOUND, None, "目标父分类不存在"
        if new_parent_id and self._is_descendant(cat_id, new_parent_id):
            return C.CODE_FILE_INVALID, None, "不能将分类移动到它自己的子分类下"
        if self._find_same_name(C.TAG_TYPE_CATEGORY, category["tag_name"],
                                new_parent_id):
            return C.CODE_DUPLICATE, None, "目标位置已存在同名分类"
        try:
            self.tag_dao.update_by_id(cat_id, {"parent_id": new_parent_id})
            write_operation_log(
                C.OP_CONFIG, f"移动分类：{category['tag_name']} → {new_parent_id}"
            )
            return C.CODE_SUCCESS, None, "分类已移动"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def get_categories_tree(self) -> list:
        """递归组装完整分类树。

        Returns:
            树节点列表，节点含 id/name/color/lit_count/children。
        """
        categories = self.tag_dao.get_all_categories()
        children_map = {}
        for category in categories:
            children_map.setdefault(category["parent_id"], []).append(category)

        def build(parent_id: int) -> list:
            """递归组装某父节点下的分类树。"""
            nodes = []
            for category in children_map.get(parent_id, []):
                nodes.append({
                    "id": category["id"],
                    "name": category["tag_name"],
                    "color": category["tag_color"],
                    "lit_count": self._count_lit_in_category(category["id"]),
                    "children": build(category["id"]),
                })
            return nodes

        return build(0)

    # ================= 标签 =================

    def create_tag(self, name: str, color: str = C.DEFAULT_LABEL_COLOR) -> tuple:
        """创建个性化标签。

        Returns:
            (code, tag_id, msg)
        """
        name = (name or "").strip()
        if not name:
            return C.CODE_FILE_INVALID, None, "标签名称不能为空"
        if self._find_same_name(C.TAG_TYPE_LABEL, name, 0):
            return C.CODE_DUPLICATE, None, "已存在同名标签"
        if self._find_category_by_name(name):
            return C.CODE_DUPLICATE, None, f"“{name}”已是文献分类名称，请换一个标签名或直接使用归类"
        try:
            tag_id = self.tag_dao.insert({
                "tag_type": C.TAG_TYPE_LABEL,
                "tag_name": name,
                "tag_color": color or C.DEFAULT_LABEL_COLOR,
                "parent_id": 0,
            })
            write_operation_log(C.OP_CONFIG, f"新增标签：{name}")
            return C.CODE_SUCCESS, tag_id, "标签创建成功"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def update_tag(self, tag_id: int, fields: dict) -> tuple:
        """更新标签名称/颜色。"""
        tag = self.tag_dao.get_by_id(tag_id)
        if not tag or tag["tag_type"] != C.TAG_TYPE_LABEL:
            return C.CODE_FILE_NOT_FOUND, None, "标签不存在"
        clean = {
            key: value for key, value in (fields or {}).items()
            if key in ("tag_name", "tag_color")
        }
        if "tag_name" in clean:
            clean["tag_name"] = clean["tag_name"].strip()
            if not clean["tag_name"]:
                return C.CODE_FILE_INVALID, None, "标签名称不能为空"
            other = self._find_same_name(C.TAG_TYPE_LABEL, clean["tag_name"], 0)
            if other and other["id"] != tag_id:
                return C.CODE_DUPLICATE, None, "已存在同名标签"
            if self._find_category_by_name(clean["tag_name"]):
                return C.CODE_DUPLICATE, None, (
                    f"“{clean['tag_name']}”已是文献分类名称，不能改为同名"
                )
        if not clean:
            return C.CODE_SUCCESS, None, "无需要更新的字段"
        try:
            self.tag_dao.update_by_id(tag_id, clean)
            write_operation_log(C.OP_CONFIG, f"修改标签：{tag['tag_name']}")
            return C.CODE_SUCCESS, None, "标签已更新"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def delete_tag(self, tag_id: int) -> tuple:
        """删除标签（literature_tag 绑定由外键级联清理）。"""
        tag = self.tag_dao.get_by_id(tag_id)
        if not tag or tag["tag_type"] != C.TAG_TYPE_LABEL:
            return C.CODE_FILE_NOT_FOUND, None, "标签不存在"
        try:
            with DatabaseManager().write_lock:
                self.tag_dao.delete_by_id(tag_id)
            write_operation_log(C.OP_CONFIG, f"删除标签：{tag['tag_name']}")
            return C.CODE_SUCCESS, None, "标签已删除"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def merge_tag(self, source_tag_id: int, target_tag_id: int) -> tuple:
        """合并标签：源标签的全部绑定迁移到目标标签后删除源标签（事务）。"""
        if int(source_tag_id) == int(target_tag_id):
            return C.CODE_FILE_INVALID, None, "源标签与目标标签相同"
        source = self.tag_dao.get_by_id(source_tag_id)
        target = self.tag_dao.get_by_id(target_tag_id)
        if not source or not target:
            return C.CODE_FILE_NOT_FOUND, None, "标签不存在"
        if source["tag_type"] != C.TAG_TYPE_LABEL or \
                target["tag_type"] != C.TAG_TYPE_LABEL:
            return C.CODE_FILE_INVALID, None, "仅支持个性化标签合并"
        try:
            with DatabaseManager().write_lock:
                source_lit_ids = self.lit_tag_dao.get_lit_ids_by_tag(source_tag_id)
                for lit_id in source_lit_ids:
                    self.lit_tag_dao.bind(lit_id, target_tag_id)
                self.tag_dao.delete_by_id(source_tag_id)
            write_operation_log(
                C.OP_CONFIG,
                f"合并标签：{source['tag_name']} → {target['tag_name']}",
            )
            return C.CODE_SUCCESS, None, "标签已合并"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def apply_preset_labels(self) -> tuple:
        """套用标签预设模板（已存在的同名标签跳过，与文献分类同名也跳过）。

        Returns:
            (code, 新增数量, msg)
        """
        inserted = 0
        try:
            for name, color in PRESET_LABELS:
                if self._find_same_name(C.TAG_TYPE_LABEL, name, 0):
                    continue
                if self._find_category_by_name(name):
                    # 分类已占用该名称，不能再建同名标签
                    continue
                self.tag_dao.insert({
                    "tag_type": C.TAG_TYPE_LABEL,
                    "tag_name": name,
                    "tag_color": color,
                    "parent_id": 0,
                })
                inserted += 1
            write_operation_log(C.OP_CONFIG, f"套用标签预设模板（新增 {inserted} 个）")
            return C.CODE_SUCCESS, inserted, f"已套用预设，新增 {inserted} 个标签"
        except Exception as exc:
            return exception_to_code(exc), 0, getattr(exc, "message", str(exc))

    def list_labels_with_count(self) -> list:
        """返回全部标签及其绑定文献数量。"""
        result = []
        for tag in self.tag_dao.get_all_labels():
            item = dict(tag)
            item["lit_count"] = self.lit_tag_dao.count_by_tag(tag["id"])
            result.append(item)
        return result

    # ================= 绑定 =================

    def bind_tags_to_lit(self, lit_id: int, tag_ids: list) -> tuple:
        """为文献全量替换标签绑定（事务先删后插，校验标签存在性）。"""
        if not self.lit_dao.select_by_id(lit_id):
            return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
        try:
            clean_ids = sorted({int(tid) for tid in (tag_ids or []) if tid is not None})
        except (TypeError, ValueError):
            return C.CODE_FILE_INVALID, None, "标签ID格式非法"
        for tag_id in clean_ids:
            tag = self.tag_dao.get_by_id(tag_id)
            if not tag or tag["tag_type"] != C.TAG_TYPE_LABEL:
                return C.CODE_FILE_INVALID, None, f"标签不存在：{tag_id}"
        try:
            with DatabaseManager().write_lock:
                self.lit_tag_dao.replace_tags(lit_id, clean_ids)
            write_operation_log(C.OP_CONFIG, f"文献 {lit_id} 绑定标签：{clean_ids}", lit_id)
            return C.CODE_SUCCESS, None, "标签绑定成功"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 内部辅助 =================

    def _find_same_name(self, tag_type: int, name: str, parent_id: int):
        """查找同类型、同层级下的同名记录。"""
        rows = self.tag_dao.get_all()
        for row in rows:
            if (row["tag_type"] == tag_type and row["tag_name"] == name
                    and row["parent_id"] == parent_id):
                return row
        return None

    def _find_category_by_name(self, name: str, exclude_id: int = 0):
        """按名称查找任意层级的文献分类（分类与标签禁止跨类型同名）。"""
        for row in self.tag_dao.get_all_categories():
            if row["tag_name"] == name and row["id"] != exclude_id:
                return row
        return None

    def _find_label_by_name(self, name: str, exclude_id: int = 0):
        """按名称查找个性化标签（分类与标签禁止跨类型同名）。"""
        for row in self.tag_dao.get_all_labels():
            if row["tag_name"] == name and row["id"] != exclude_id:
                return row
        return None

    def _count_lit_in_category(self, cat_id: int) -> int:
        """统计 category_id 直接指向该分类的文献数。"""
        rows = self.lit_dao.select_all()
        return sum(1 for lit in rows if lit.get("category_id") == cat_id)

    def _is_descendant(self, ancestor_id: int, candidate_id: int) -> bool:
        """判断 candidate 是否为 ancestor 的子孙节点。"""
        stack = [ancestor_id]
        while stack:
            current = stack.pop()
            for child in self.tag_dao.get_children(current):
                if child["id"] == candidate_id:
                    return True
                stack.append(child["id"])
        return False
