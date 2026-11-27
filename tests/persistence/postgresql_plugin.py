"""将选定的现有业务测试同时运行在两种数据库上。

示例：pytest -p tests.persistence.postgresql_plugin tests/scheduler_core/test_task_lifecycle.py
SQLite 文件降版测试不适用于 PostgreSQL，应单独运行。
"""

import pytest

from tests.persistence.conftest import database_mode, postgres_test_url  # noqa: F401


@pytest.fixture(autouse=True)
def selected_database(database_mode):
    yield database_mode
