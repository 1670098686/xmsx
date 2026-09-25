"""SQLite 单例数据库连接管理。

全局共用一个连接，禁止频繁打开关闭；开启外键约束、WAL 模式与忙等待。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable

from utils.exceptions import DatabaseError
from utils.logger import get_logger

DEFAULT_DB_DIR = os.path.join(os.path.expanduser("~"), ".literature_agent")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "literature_agent.db")


class DatabaseManager:
    """SQLite 数据库单例管理器（线程安全双重检查锁）。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = None):
        """单例实例化入口。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialize(db_path or DEFAULT_DB_PATH)
                    cls._instance = instance
        return cls._instance

    def _initialize(self, db_path: str) -> None:
        """实际初始化连接（仅单例首次创建时执行一次）。

        Args:
            db_path: 数据库文件路径；:memory: 用于单元测试。
        """
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # QThread 工作线程与主线程共享同一连接，写操作加锁串行化
        self._write_lock = threading.RLock()
        self._apply_pragmas()
        get_logger().info("数据库连接已建立: %s", db_path)

    def _apply_pragmas(self) -> None:
        """应用外键、WAL、忙等待等性能与完整性 PRAGMA。"""
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.commit()

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅供单元测试切换 :memory: 数据库时使用）。"""
        with cls._lock:
            if cls._instance is not None:
                try:
                    cls._instance.close()
                except sqlite3.Error:
                    pass
            cls._instance = None

    def get_conn(self) -> sqlite3.Connection:
        """返回全局唯一连接。"""
        return self.conn

    @property
    def write_lock(self):
        """写操作重入锁（业务层跨多 DAO 的事务编排可持有）。"""
        return self._write_lock

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """执行单条参数化写操作并自动提交。

        Args:
            sql: SQL 语句，参数位必须使用 ? 占位。
            params: 与占位符对应的参数元组/列表。
        Returns:
            sqlite3.Cursor。
        Raises:
            DatabaseError: 包装底层 sqlite3 异常。
        """
        try:
            with self._write_lock:
                cursor = self.conn.cursor()
                cursor.execute(sql, tuple(params))
                self.conn.commit()
            return cursor
        except sqlite3.Error as exc:
            self.conn.rollback()
            get_logger().error("SQL执行失败: %s | %s", sql, exc, exc_info=True)
            raise DatabaseError(f"SQL执行失败: {exc}") from exc

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        """批量参数化执行。"""
        try:
            with self._write_lock:
                cursor = self.conn.cursor()
                cursor.executemany(sql, [tuple(p) for p in seq_of_params])
                self.conn.commit()
            return cursor
        except sqlite3.Error as exc:
            self.conn.rollback()
            get_logger().error("SQL批量执行失败: %s | %s", sql, exc, exc_info=True)
            raise DatabaseError(f"SQL批量执行失败: {exc}") from exc

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row:
        """参数化查询单条记录。"""
        cursor = self.conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor.fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """参数化查询多条记录。"""
        cursor = self.conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor.fetchall()

    @contextmanager
    def transaction(self):
        """事务上下文管理器：异常自动回滚，正常退出自动提交。"""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        """关闭连接（程序退出时调用）。"""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
