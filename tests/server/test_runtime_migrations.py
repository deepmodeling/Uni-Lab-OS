"""runtime.db 的真实版本迁移：0.13 基线（v1）的库升级后保留业务数据。"""
from __future__ import annotations

import sqlite3

import pytest

from unilabos.server.database import DatabaseMigrationRequired, initialize_database
from unilabos.server.database.tables.runtime import RUNTIME_DATABASE

#: 0.13 基线（v1）的等待表与结构指纹，冻结在测试里。
V1_CHECKSUM = "68bbaa4dbedafb58de6a87d5f3a0153ce27165920c3797a9206237605a7e46ed"
V1_WAITER_DDL = """
    CREATE TABLE workflow_resource_waiter (
        job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
        task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
        lock_keys TEXT NOT NULL CHECK(json_valid(lock_keys) AND json_type(lock_keys)='array'),
        blockers TEXT NOT NULL CHECK(json_valid(blockers) AND json_type(blockers)='array')
    )
"""


def _as_v1(path, checksum: str = V1_CHECKSUM) -> None:
    connection = initialize_database(path, RUNTIME_DATABASE)
    connection.execute("PRAGMA foreign_keys = OFF")
    with connection:
        connection.execute("DROP TABLE workflow_resource_waiter")
        connection.execute(V1_WAITER_DDL)
        connection.execute("INSERT INTO workflow_resource_waiter VALUES ('job-1','task-1','[]','[]')")
        connection.execute("INSERT INTO workflow_operation_category(uuid, name, create_time, update_time) "
                           "VALUES ('keep-me', '保留', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')")
        connection.execute("UPDATE schema_identity SET checksum=? WHERE database_key='runtime'", (checksum,))
        connection.execute("PRAGMA user_version = 1")
    connection.close()


def test_v1_runtime_database_migrates_to_durable_waiter_order(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    _as_v1(path)

    connection = initialize_database(path, RUNTIME_DATABASE)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == RUNTIME_DATABASE.schema_version == 2
        columns = [row[1] for row in connection.execute("PRAGMA table_info(workflow_resource_waiter)")]
        assert "enqueued_at" in columns
        # 等待记录是瞬态事实，迁移时清空、由调度器重新排队；其余业务数据原样保留。
        assert connection.execute("SELECT count(*) FROM workflow_resource_waiter").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM workflow_operation_category WHERE uuid='keep-me'").fetchone()[0] == "保留"
        history = connection.execute("SELECT version FROM schema_migration").fetchall()
        assert [row[0] for row in history] == [2]
    finally:
        connection.close()
    assert list(tmp_path.glob("runtime.db.pre-v2-*.bak")), "迁移前必须先备份"
    sqlite3.connect(next(tmp_path.glob("runtime.db.pre-v2-*.bak"))).close()


def test_same_version_with_a_foreign_fingerprint_is_refused_not_half_migrated(tmp_path) -> None:
    """压平前的开发库也可能停在 user_version 1：指纹不是 0.13 基线就没有迁移路径，按 refuse 保留原样。"""
    path = tmp_path / "runtime.db"
    _as_v1(path, checksum="pre-baseline-dev")
    with pytest.raises(DatabaseMigrationRequired):
        initialize_database(path, RUNTIME_DATABASE).close()
    peek = sqlite3.connect(path)
    try:
        assert peek.execute("PRAGMA user_version").fetchone()[0] == 1
        assert peek.execute("SELECT count(*) FROM workflow_resource_waiter").fetchone()[0] == 1
    finally:
        peek.close()
    assert not list(tmp_path.glob("runtime.db.pre-v2-*.bak"))
