"""pytest 公共夹具：每个测试函数获得全新的内存 SQLite 数据库。"""
import os
import sys

import pytest

# 确保从项目根目录（literature_agent/）可直接导入各包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data_layer.db_connect import DatabaseManager  # noqa: E402
from data_layer.db_init import init_db  # noqa: E402


@pytest.fixture
def mem_conn():
    """提供已完成建表与种子数据的内存数据库连接。

    Yields:
        sqlite3.Connection: 内存库连接（外键约束已开启）。
    """
    DatabaseManager.reset_instance()
    manager = DatabaseManager(":memory:")
    init_db(manager)
    yield manager.get_conn()
    DatabaseManager.reset_instance()
