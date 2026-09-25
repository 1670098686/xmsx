"""parse_rule 解析规则配置表 DAO。"""
from data_layer.dao import BaseDao


class ParseRuleDao(BaseDao):
    """解析规则 CRUD；rule_detail 以 JSON 字符串存储。"""

    def insert(self, rule: dict) -> int:
        """新增解析规则。"""
        cursor = self.conn.execute(
            """
            INSERT INTO parse_rule
                (rule_name, subject_type, rule_detail, precision_level, is_default)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rule.get("rule_name", ""),
                rule.get("subject_type", "通用"),
                rule.get("rule_detail", "{}"),
                rule.get("precision_level", 3),
                rule.get("is_default", 0),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_id(self, rule_id: int) -> dict:
        """按主键查询规则。"""
        row = self.conn.execute(
            "SELECT * FROM parse_rule WHERE id = ?", (rule_id,)
        ).fetchone()
        return self.row_to_dict(row)

    def get_default(self) -> dict:
        """查询当前默认规则。"""
        row = self.conn.execute(
            "SELECT * FROM parse_rule WHERE is_default = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        return self.row_to_dict(row)

    def get_by_subject(self, subject_type: str) -> list:
        """按学科类型查询规则列表。"""
        rows = self.conn.execute(
            "SELECT * FROM parse_rule WHERE subject_type = ? ORDER BY id",
            (subject_type,),
        ).fetchall()
        return self.rows_to_list(rows)

    def list_all(self) -> list:
        """查询全部规则。"""
        rows = self.conn.execute(
            "SELECT * FROM parse_rule ORDER BY is_default DESC, id"
        ).fetchall()
        return self.rows_to_list(rows)

    def update_by_id(self, rule_id: int, fields: dict) -> int:
        """按主键动态更新规则字段。"""
        allowed = {"rule_name", "subject_type", "rule_detail", "precision_level", "is_default"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        params.append(rule_id)
        cursor = self.conn.execute(
            f"UPDATE parse_rule SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        self.conn.commit()
        return cursor.rowcount

    def update_detail(self, rule_id: int, rule_detail_json: str) -> int:
        """仅更新规则明细 JSON。"""
        return self.update_by_id(rule_id, {"rule_detail": rule_detail_json})

    def delete_by_id(self, rule_id: int) -> int:
        """按主键删除规则（默认规则是否可删由业务层校验）。"""
        cursor = self.conn.execute(
            "DELETE FROM parse_rule WHERE id = ?", (rule_id,)
        )
        self.conn.commit()
        return cursor.rowcount
