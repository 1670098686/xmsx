"""system_config 系统配置表 DAO（键值对）。"""
from data_layer.dao import BaseDao


class SystemConfigDao(BaseDao):
    """系统配置读写，UPSERT 语义。"""

    def get(self, config_key: str, default: str = None) -> str:
        """读取单个配置值；不存在时返回 default。"""
        row = self.conn.execute(
            "SELECT config_value FROM system_config WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        return row[0] if row is not None else default

    def get_desc(self, config_key: str) -> str:
        """读取配置项描述。"""
        row = self.conn.execute(
            "SELECT config_desc FROM system_config WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        return row[0] if row is not None else ""

    def set(self, config_key: str, config_value: str, config_desc: str = None) -> None:
        """写入配置（存在则更新，不存在则插入）。

        Args:
            config_key: 配置键。
            config_value: 配置值。
            config_desc: 可选描述，仅插入时生效。
        """
        self.conn.execute(
            """
            INSERT INTO system_config (config_key, config_value, config_desc)
            VALUES (?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET
                config_value = excluded.config_value,
                update_time = CURRENT_TIMESTAMP
            """,
            (config_key, str(config_value), config_desc or ""),
        )
        self.conn.commit()

    def get_all(self) -> dict:
        """读取全部配置，返回 {key: value} 字典。"""
        rows = self.conn.execute(
            "SELECT config_key, config_value FROM system_config"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def bulk_set(self, configs: dict) -> None:
        """事务内批量更新配置。

        Args:
            configs: {config_key: config_value} 字典。
        """
        try:
            for key, value in configs.items():
                self.conn.execute(
                    """
                    INSERT INTO system_config (config_key, config_value)
                    VALUES (?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_value = excluded.config_value,
                        update_time = CURRENT_TIMESTAMP
                    """,
                    (key, str(value)),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def delete(self, config_key: str) -> int:
        """删除单个配置项。"""
        cursor = self.conn.execute(
            "DELETE FROM system_config WHERE config_key = ?", (config_key,)
        )
        self.conn.commit()
        return cursor.rowcount
