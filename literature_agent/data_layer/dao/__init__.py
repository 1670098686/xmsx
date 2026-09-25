"""DAO 子包：各数据表原子 CRUD 封装。

DAO 层禁止编写业务判断逻辑与多表业务组装，复杂查询由 business 层编排。
"""
from data_layer.db_connect import DatabaseManager


class BaseDao:
    """DAO 基类：统一获取连接与 Row→dict 转换。"""

    def __init__(self, conn=None):
        """初始化 DAO。

        Args:
            conn: 可选 sqlite3.Connection；不传则使用 DatabaseManager 单例连接。
        """
        self.conn = conn or DatabaseManager().get_conn()

    @staticmethod
    def row_to_dict(row) -> dict:
        """将 sqlite3.Row 转为普通字典；None 原样返回。"""
        return dict(row) if row is not None else None

    @staticmethod
    def rows_to_list(rows) -> list:
        """将 Row 列表转为字典列表。"""
        return [dict(r) for r in rows]
