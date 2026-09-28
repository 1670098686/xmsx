"""解析规则配置业务：规则 CRUD、rule_detail JSON 读写、默认规则管理。"""
import json

from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.rule_dao import ParseRuleDao
from data_layer.db_connect import DatabaseManager
from utils.logger import get_logger

from business.service_common import exception_to_code, write_operation_log

logger = get_logger()

# 六个解析维度（与 text_analysis.detect_structure 对齐）
DIMENSION_KEYS = (
    "research_background", "core_view", "research_method",
    "innovation_point", "research_conclusion", "reference_list",
)
_DIMENSION_LABELS = {
    "research_background": "研究背景",
    "core_view": "核心观点",
    "research_method": "研究方法",
    "innovation_point": "创新点",
    "research_conclusion": "研究结论",
    "reference_list": "参考文献",
}
DEFAULT_WEIGHTS = {
    "research_background": 1.0,
    "core_view": 1.2,
    "research_method": 1.0,
    "innovation_point": 1.5,
    "research_conclusion": 1.5,
    "reference_list": 1.0,
}
_PRECISION_MIN, _PRECISION_MAX = 1, 5
# 内置预设规则 id（db_init 种子数据，适用学科等内置属性只读）
BUILTIN_RULE_IDS = (1, 2, 3, 4)
# 适用学科字段最大长度（与 parse_rule.subject_type VARCHAR(20) 对齐）
SUBJECT_TYPE_MAX_LENGTH = 20


class ParseRuleService:
    """解析规则配置业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.rule_dao = ParseRuleDao()
        self.config_dao = SystemConfigDao()

    # ================= 查询 =================

    def list_rules(self, subject_type: str = None) -> list:
        """列出规则（可按学科过滤），is_default 规则排在最前。"""
        rules = self.rule_dao.list_all()
        if subject_type:
            rules = [r for r in rules if r.get("subject_type") == subject_type]
        return rules

    def get_rule_detail(self, rule_id: int) -> dict:
        """获取规则详情（rule_detail JSON 反序列化并展开为平铺字段）。

        Returns:
            {id, rule_name, subject_type, precision_level, is_default,
             dimensions, weights}；规则不存在返回 None。
        """
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return None
        try:
            detail = json.loads(rule.get("rule_detail") or "{}")
        except (TypeError, ValueError):
            detail = {}
        dimensions = detail.get("dimensions") or {}
        dimensions = {
            key: bool(dimensions.get(key, True)) for key in DIMENSION_KEYS
        }
        weights = detail.get("weights") or dict(DEFAULT_WEIGHTS)
        return {
            "id": rule["id"],
            "rule_name": rule["rule_name"],
            "subject_type": rule["subject_type"],
            "precision_level": int(rule.get("precision_level", 3)),
            "is_default": int(rule.get("is_default", 0)),
            "dimensions": dimensions,
            "weights": weights,
        }

    def get_default_rule_id(self) -> int:
        """获取当前默认规则 id。"""
        default = self.rule_dao.get_default()
        if default:
            return default["id"]
        configured = self.config_dao.get(C.CFG_PARSE_DEFAULT_RULE, "1")
        return int(configured or 1)

    def get_default_dimensions(self) -> dict:
        """获取当前默认规则的六个解析维度开关。

        报告展示与导出统一按此开关隐藏停用维度（无需重新解析旧报告）；
        规则读取异常时退化为全部启用，保证报告可读。

        Returns:
            {维度key: bool}，键集合固定为 DIMENSION_KEYS。
        """
        all_on = {key: True for key in DIMENSION_KEYS}
        try:
            detail = self.get_rule_detail(self.get_default_rule_id())
            if not detail:
                return all_on
            dimensions = detail.get("dimensions") or {}
            return {
                key: bool(dimensions.get(key, True)) for key in DIMENSION_KEYS
            }
        except Exception as exc:
            logger.warning("读取默认规则维度失败，按全部启用处理：%s", exc)
            return all_on

    @staticmethod
    def is_builtin_rule(rule_id: int) -> bool:
        """判断规则是否为内置预设模板（预设模板的适用学科只读）。

        Args:
            rule_id: 规则 id。
        Returns:
            True 表示 db_init 写入的内置预设规则。
        """
        try:
            return int(rule_id) in BUILTIN_RULE_IDS
        except (TypeError, ValueError):
            return False

    # ================= 新增 / 修改 =================

    def create_rule(self, rule_name: str, subject_type: str,
                    dimensions: dict, weights: dict = None,
                    precision: int = 3) -> tuple:
        """创建自定义规则。

        Args:
            rule_name: 规则名称。
            subject_type: 学科类型（文科/理科/工科/通用等）。
            dimensions: {维度key: bool}。
            weights: {维度key: float}，缺省用默认权重。
            precision: 解析精度 1-5。
        Returns:
            (code, rule_id, msg)
        """
        rule_name = (rule_name or "").strip()
        if not rule_name:
            return C.CODE_FILE_INVALID, None, "规则名称不能为空"
        if any(r["rule_name"] == rule_name for r in self.rule_dao.list_all()):
            return C.CODE_DUPLICATE, None, "已存在同名规则"
        subject_type = (subject_type or "").strip() or "自定义"
        if len(subject_type) > SUBJECT_TYPE_MAX_LENGTH:
            return C.CODE_FILE_INVALID, None, \
                f"适用学科不能超过 {SUBJECT_TYPE_MAX_LENGTH} 个字"
        precision = self._clamp_precision(precision)
        detail = self._build_detail(dimensions, weights)
        try:
            rule_id = self.rule_dao.insert({
                "rule_name": rule_name,
                "subject_type": subject_type,
                "rule_detail": json.dumps(detail, ensure_ascii=False),
                "precision_level": precision,
                "is_default": 0,
            })
            write_operation_log(C.OP_CONFIG, f"新增解析规则：{rule_name}")
            return C.CODE_SUCCESS, rule_id, "规则创建成功"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def update_rule_detail(self, rule_id: int, dimensions: dict = None,
                           weights: dict = None,
                           precision: int = None,
                           subject_type: str = None) -> tuple:
        """更新规则维度/权重/精度/适用学科（JSON 合并写回）。

        内置预设规则的适用学科只读；自定义规则可自由编辑学科。
        规则修改只影响此后新发起的解析；已经解析完成并入库的报告不会被
        判定为过期，也不会被要求重新解析（用户可随时手动逐篇重新解析）。
        历史版本 rule_detail 中残留的 keyword_top_n 字段会在保存时自动清除。

        Args:
            rule_id: 规则 id。
            dimensions: 维度开关字典，None 表示不修改。
            weights: 维度权重字典，None 表示不修改。
            precision: 解析精度 1-5，None 表示不修改。
            subject_type: 适用学科，None 表示不修改。
        Returns:
            (code, None, msg)
        """
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return C.CODE_FILE_NOT_FOUND, None, "规则不存在"
        if subject_type is not None:
            if self.is_builtin_rule(rule_id):
                return C.CODE_FILE_INVALID, None, \
                    "预设模板的适用学科不可修改，可另存为新规则后自定义"
            subject_type = subject_type.strip()
            if not subject_type:
                return C.CODE_FILE_INVALID, None, "适用学科不能为空"
            if len(subject_type) > SUBJECT_TYPE_MAX_LENGTH:
                return C.CODE_FILE_INVALID, None, \
                    f"适用学科不能超过 {SUBJECT_TYPE_MAX_LENGTH} 个字"
        try:
            detail = json.loads(rule.get("rule_detail") or "{}")
        except (TypeError, ValueError):
            detail = {}
        # 兼容清理：旧版本写入的关键词数量字段已废弃，保存时移除
        detail.pop("keyword_top_n", None)
        if dimensions is not None:
            detail["dimensions"] = {
                key: bool(dimensions.get(key, True)) for key in DIMENSION_KEYS
            }
        if weights is not None:
            merged = dict(DEFAULT_WEIGHTS)
            merged.update({
                key: float(weights[key]) for key in weights
                if key in DIMENSION_KEYS
            })
            detail["weights"] = merged
        detail.setdefault("dimensions", {key: True for key in DIMENSION_KEYS})
        detail.setdefault("weights", dict(DEFAULT_WEIGHTS))

        fields = {"rule_detail": json.dumps(detail, ensure_ascii=False)}
        if precision is not None:
            fields["precision_level"] = self._clamp_precision(precision)
        if subject_type is not None:
            fields["subject_type"] = subject_type
        try:
            self.rule_dao.update_by_id(rule_id, fields)
            write_operation_log(C.OP_CONFIG, f"更新解析规则：{rule['rule_name']}")
            return C.CODE_SUCCESS, None, "规则已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def rename_rule(self, rule_id: int, new_name: str) -> tuple:
        """重命名规则。"""
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return C.CODE_FILE_NOT_FOUND, None, "规则不存在"
        new_name = (new_name or "").strip()
        if not new_name:
            return C.CODE_FILE_INVALID, None, "规则名称不能为空"
        try:
            self.rule_dao.update_by_id(rule_id, {"rule_name": new_name})
            return C.CODE_SUCCESS, None, "规则已重命名"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 默认 / 删除 / 重置 =================

    def set_default(self, rule_id: int) -> tuple:
        """设为默认规则（事务：旧默认置 0 → 目标置 1 → 同步系统配置）。"""
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return C.CODE_FILE_NOT_FOUND, None, "规则不存在"
        try:
            with DatabaseManager().write_lock:
                for item in self.rule_dao.list_all():
                    if item.get("is_default"):
                        self.rule_dao.update_by_id(item["id"], {"is_default": 0})
                self.rule_dao.update_by_id(rule_id, {"is_default": 1})
                self.config_dao.set(C.CFG_PARSE_DEFAULT_RULE, str(rule_id))
                self.config_dao.set(
                    C.CFG_PRECISION_DEFAULT,
                    str(rule.get("precision_level", 3)),
                )
            write_operation_log(C.OP_CONFIG, f"设置默认解析规则：{rule['rule_name']}")
            return C.CODE_SUCCESS, None, "已设为默认规则"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def delete_rule(self, rule_id: int) -> tuple:
        """删除规则（默认规则与仍被报告引用的规则禁止删除）。"""
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return C.CODE_FILE_NOT_FOUND, None, "规则不存在"
        if int(rule.get("is_default", 0)) == 1:
            return C.CODE_FILE_INVALID, None, "默认规则禁止删除"
        try:
            with DatabaseManager().write_lock:
                self.rule_dao.delete_by_id(rule_id)
            write_operation_log(C.OP_CONFIG, f"删除解析规则：{rule['rule_name']}")
            return C.CODE_SUCCESS, None, "规则已删除"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def reset_rule(self, rule_id: int) -> tuple:
        """将规则明细重置为全维度开启 + 默认权重 + 精度 3。"""
        rule = self.rule_dao.get_by_id(rule_id)
        if not rule:
            return C.CODE_FILE_NOT_FOUND, None, "规则不存在"
        detail = self._build_detail(
            {key: True for key in DIMENSION_KEYS}, None
        )
        try:
            self.rule_dao.update_by_id(rule_id, {
                "rule_detail": json.dumps(detail, ensure_ascii=False),
                "precision_level": 3,
            })
            write_operation_log(C.OP_CONFIG, f"重置解析规则：{rule['rule_name']}")
            return C.CODE_SUCCESS, None, "已重置为默认配置"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 内部辅助 =================

    @staticmethod
    def dimension_labels() -> dict:
        """返回维度 key → 中文标签映射（供 UI 使用）。"""
        return dict(_DIMENSION_LABELS)

    @staticmethod
    def _clamp_precision(precision: int) -> int:
        """精度钳制在 1-5 之间。"""
        try:
            value = int(precision)
        except (TypeError, ValueError):
            return 3
        return max(_PRECISION_MIN, min(_PRECISION_MAX, value))

    @staticmethod
    def _build_detail(dimensions: dict, weights: dict) -> dict:
        """组装写入 rule_detail 的 JSON 结构（仅维度开关与权重）。"""
        dims = {
            key: bool((dimensions or {}).get(key, True))
            for key in DIMENSION_KEYS
        }
        merged_weights = dict(DEFAULT_WEIGHTS)
        if weights:
            merged_weights.update({
                key: float(weights[key]) for key in weights
                if key in DIMENSION_KEYS
            })
        return {
            "dimensions": dims,
            "weights": merged_weights,
        }
