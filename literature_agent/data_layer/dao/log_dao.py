"""operation_log 操作日志表 DAO。"""
from data_layer.dao import BaseDao


class OperationLogDao(BaseDao):
    """用户操作行为日志 CRUD。"""

    def insert(self, log: dict) -> int:
        """写入一条操作日志。

        Args:
            log: 含 operate_type/operate_content/literature_id/operate_status 的字典。
        Returns:
            新日志 id。
        """
        cursor = self.conn.execute(
            """
            INSERT INTO operation_log
                (operate_type, operate_content, literature_id, operate_status)
            VALUES (?, ?, ?, ?)
            """,
            (
                log.get("operate_type", ""),
                log.get("operate_content", ""),
                log.get("literature_id", 0),
                log.get("operate_status", 1),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_recent(self, limit: int = 50) -> list:
        """查询最近 N 条操作日志。"""
        rows = self.conn.execute(
            "SELECT * FROM operation_log ORDER BY operate_time DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return self.rows_to_list(rows)

    def get_by_lit_id(self, literature_id: int) -> list:
        """查询某文献关联的全部日志。"""
        rows = self.conn.execute(
            "SELECT * FROM operation_log WHERE literature_id = ? "
            "ORDER BY operate_time DESC, id DESC",
            (literature_id,),
        ).fetchall()
        return self.rows_to_list(rows)

    def delete_before(self, time_str: str) -> int:
        """删除指定时间之前的历史日志（日志清理）。"""
        cursor = self.conn.execute(
            "DELETE FROM operation_log WHERE operate_time < ?", (time_str,)
        )
        self.conn.commit()
        return cursor.rowcount
