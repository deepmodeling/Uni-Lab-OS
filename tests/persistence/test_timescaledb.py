"""PostgreSQL 设备状态历史的 TimescaleDB 集成测试。"""

from __future__ import annotations

import pytest

from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.config.config import BasicConfig


@pytest.fixture(scope="module")
def timescaledb_test_url():
    """为 TimescaleDB 测试创建独立数据库，避免污染共享 PG 测试库。"""

    import os
    from urllib.parse import urlsplit, urlunsplit
    from uuid import uuid4

    import psycopg
    from psycopg import sql

    admin_url = os.environ.get("UNILABOS_TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("设置 UNILABOS_TEST_POSTGRES_URL 后运行真实 PostgreSQL 集成测试")

    database_name = "unilab_timescale_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    database_url = urlunsplit(
        urlsplit(admin_url)._replace(path="/" + database_name)
    )
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            available = connection.execute(
                "SELECT 1 FROM pg_available_extensions "
                "WHERE name = 'timescaledb'"
            ).fetchone()
            if available is None:
                pytest.skip("测试 PostgreSQL 未安装 TimescaleDB 扩展")
            connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        yield database_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


@pytest.fixture
def timescaledb_settings(timescaledb_test_url, monkeypatch):
    """返回已启用 TimescaleDB 的独立 PostgreSQL 测试库。"""

    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(BasicConfig, "database_url", timescaledb_test_url)
    return timescaledb_test_url


def test_device_property_history_is_timescale_hypertable(
    timescaledb_settings,
):
    store = DeviceStateStore(":memory:")
    try:
        hypertable = store.db._connection.execute(
            """
            SELECT hypertable_schema, hypertable_name
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'public'
              AND hypertable_name = 'device_property_history'
            """
        ).fetchone()
        assert hypertable == ("public", "device_property_history")

        interval = store.db._connection.execute(
            """
            SELECT integer_interval
            FROM timescaledb_information.dimensions
            WHERE hypertable_schema = 'public'
              AND hypertable_name = 'device_property_history'
              AND column_name = 'recorded_at'
            """
        ).fetchone()[0]
        assert interval == 86_400_000

        primary_key = store.db._connection.execute(
            """
            SELECT pg_get_constraintdef(constraint_info.oid)
            FROM pg_constraint AS constraint_info
            WHERE constraint_info.conrelid =
                'public.device_property_history'::regclass
              AND constraint_info.contype = 'p'
            """
        ).fetchone()[0]
        assert primary_key == "PRIMARY KEY (recorded_at, id)"

        time_index = store.db._connection.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'device_property_history'
              AND indexname = 'idx_dph_device_property_recorded_at'
            """
        ).fetchone()
        assert time_index == (1,)

        assert store.set("reactor-1", "temperature", 25.0, now_ms=1) is True
        assert store.set("reactor-1", "temperature", 25.0, now_ms=2) is False
        assert store.db.query_one(
            "SELECT COUNT(*) AS count FROM device_property_history"
        )["count"] == 1
    finally:
        store.close()


def test_device_property_history_timescale_initialization_is_idempotent(
    timescaledb_settings,
):
    first = DeviceStateStore(":memory:")
    first.close()

    second = DeviceStateStore(":memory:")
    try:
        hypertable_count = second.db._connection.execute(
            """
            SELECT COUNT(*)
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'public'
              AND hypertable_name = 'device_property_history'
            """
        ).fetchone()[0]
        assert hypertable_count == 1
    finally:
        second.close()
