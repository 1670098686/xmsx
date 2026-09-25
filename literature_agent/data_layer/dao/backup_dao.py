"""backup_record 备份记录表 DAO。"""
from data_layer.dao import BaseDao


class BackupRecordDao(BaseDao):
    """备份元信息 CRUD。"""

    def insert(self, record: dict) -> int:
        """新增一条备份记录。"""
        cursor = self.conn.execute(
            """
            INSERT INTO backup_record
                (backup_name, backup_path, backup_size, backup_type,
                 content_scope, backup_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("backup_name", ""),
                record.get("backup_path", ""),
                record.get("backup_size", 0),
                record.get("backup_type", "full"),
                record.get("content_scope", "all"),
                record.get("backup_status", 1),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_id(self, backup_id: int) -> dict:
        """按主键查询备份记录。"""
        row = self.conn.execute(
            "SELECT * FROM backup_record WHERE id = ?", (backup_id,)
        ).fetchone()
        return self.row_to_dict(row)

    def get_all(self) -> list:
        """查询全部备份记录（最新在前）。"""
        rows = self.conn.execute(
            "SELECT * FROM backup_record ORDER BY backup_time DESC, id DESC"
        ).fetchall()
        return self.rows_to_list(rows)

    def get_latest(self) -> dict:
        """查询最近一次备份记录。"""
        row = self.conn.execute(
            "SELECT * FROM backup_record ORDER BY backup_time DESC, id DESC LIMIT 1"
        ).fetchone()
        return self.row_to_dict(row)

    def delete_by_id(self, backup_id: int) -> int:
        """按主键删除备份记录。"""
        cursor = self.conn.execute(
            "DELETE FROM backup_record WHERE id = ?", (backup_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def update_size(self, backup_id: int, backup_size: int) -> None:
        """回填备份文件实际字节数（备份记录先于 zip 落库场景使用）。"""
        self.conn.execute(
            "UPDATE backup_record SET backup_size = ? WHERE id = ?",
            (int(backup_size), int(backup_id)),
        )
        self.conn.commit()
