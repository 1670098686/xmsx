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
                 innovation_point, research_conclusion, reference_list,
                 keywords, rule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.get("literature_id"),
                report.get("research_background", ""),
                report.get("core_view", ""),
                report.get("research_method", ""),
                report.get("innovation_point", ""),
                report.get("research_conclusion", ""),
                report.get("reference_list", ""),
                report.get("keywords", ""),
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
            "innovation_point", "research_conclusion", "reference_list",
            "keywords", "rule_id",
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

    def select_lit_ids_with_report(self, literature_ids: list) -> list:
        """从给定文献 id 集合中筛出数据库中已存有解析报告的 id。

        Args:
            literature_ids: 文献 id 列表。
        Returns:
            存在报告记录的文献 id 列表（保持入参去重后的顺序）。
        """
        ids = [int(lit_id) for lit_id in literature_ids if lit_id is not None]
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT literature_id FROM literature_report "
            f"WHERE literature_id IN ({placeholders})",
            tuple(unique_ids),
        ).fetchall()
        found = {row[0] for row in rows}
        return [lit_id for lit_id in unique_ids if lit_id in found]
