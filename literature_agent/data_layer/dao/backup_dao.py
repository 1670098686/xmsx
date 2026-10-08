"""backup_record 备份记录表 DAO。"""
from data_layer.dao import BaseDao


class BackupRecordDao(BaseDao):
    """备份元信息 CRUD。"""

    def insert(self, record: dict) -> int:
        """新增一条备份记录。

        Args:
            record: 备份字段；backup_time 缺省时由表默认值（当前时间）填充。
        Returns:
            新记录主键 id。
        """
        fields = [
            "backup_name", "backup_path", "backup_size",
            "backup_type", "content_scope", "backup_status",
        ]
        values = [
            record.get("backup_name", ""),
            record.get("backup_path", ""),
            record.get("backup_size", 0),
            record.get("backup_type", "full"),
            record.get("content_scope", "all"),
            record.get("backup_status", 1),
        ]
        # 补登记历史备份时需要还原原始备份时间；不传则走 DEFAULT CURRENT_TIMESTAMP
        if record.get("backup_time"):
            fields.append("backup_time")
            values.append(record["backup_time"])
        placeholders = ", ".join("?" for _ in fields)
        cursor = self.conn.execute(
            f"""
            INSERT INTO backup_record
                ({", ".join(fields)})
            VALUES ({placeholders})
            """,
            values,
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_name(self, backup_name: str) -> dict:
        """按备份文件名查询记录（恢复对账按文件名匹配使用）。"""
        row = self.conn.execute(
            "SELECT * FROM backup_record WHERE backup_name = ?", (backup_name,)
        ).fetchone()
        return self.row_to_dict(row)

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

    def update_location(self, backup_id: int, backup_path: str,
                        backup_size: int, backup_time: str = None) -> None:
        """修正备份文件实际存放路径与尺寸（换机/目录迁移后重新挂接）。

        Args:
            backup_time: 同时校准备份时间（历史 UTC 记录），None 时不改时间。
        """
        if backup_time:
            sql = (
                "UPDATE backup_record SET backup_path = ?, backup_size = ?, "
                "backup_time = ? WHERE id = ?"
            )
            params = (backup_path, int(backup_size), backup_time, int(backup_id))
        else:
            sql = (
                "UPDATE backup_record SET backup_path = ?, backup_size = ? "
                "WHERE id = ?"
            )
            params = (backup_path, int(backup_size), int(backup_id))
        self.conn.execute(sql, params)
        self.conn.commit()

    def update_backup_time(self, backup_id: int, backup_time: str) -> None:
        """按 manifest 权威时间校准备份记录（修正历史 UTC 时间记录）。"""
        self.conn.execute(
            "UPDATE backup_record SET backup_time = ? WHERE id = ?",
            (backup_time, int(backup_id)),
        )
        self.conn.commit()
