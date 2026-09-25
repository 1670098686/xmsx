"""literature_report 文献解析报告表 DAO。"""
from data_layer.dao import BaseDao


class LiteratureReportDao(BaseDao):
    """结构化解析报告 CRUD。"""

    def insert(self, report: dict) -> int:
        """新增解析报告。"""
        cursor = self.conn.execute(
            """
            INSERT INTO literature_report
                (literature_id, research_background, core_view, research_method,
                 innovation_point, research_conclusion, reference_list, rule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.get("literature_id"),
                report.get("research_background", ""),
                report.get("core_view", ""),
                report.get("research_method", ""),
                report.get("innovation_point", ""),
                report.get("research_conclusion", ""),
                report.get("reference_list", ""),
                report.get("rule_id", 1),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_lit_id(self, literature_id: int) -> dict:
        """按文献 id 查询最新一篇报告。"""
        row = self.conn.execute(
            "SELECT * FROM literature_report WHERE literature_id = ? "
            "ORDER BY parse_time DESC, id DESC LIMIT 1",
            (literature_id,),
        ).fetchone()
        return self.row_to_dict(row)

    def update_by_lit_id(self, literature_id: int, fields: dict) -> int:
        """按文献 id 动态更新报告字段。"""
        allowed = {
            "research_background", "core_view", "research_method",
            "innovation_point", "research_conclusion", "reference_list", "rule_id",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        params.append(literature_id)
        cursor = self.conn.execute(
            f"UPDATE literature_report SET {', '.join(sets)} "
            "WHERE id = (SELECT id FROM literature_report WHERE literature_id = ? "
            "ORDER BY parse_time DESC, id DESC LIMIT 1)",
            tuple(params),
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_by_lit_id(self, literature_id: int) -> int:
        """删除某文献的全部报告记录。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_report WHERE literature_id = ?", (literature_id,)
        )
        self.conn.commit()
        return cursor.rowcount
