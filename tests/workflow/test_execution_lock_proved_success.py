"""明确结果提交后，UNKNOWN 期间标成 uncertain 的续持资源必须可被后继继承。"""

import json
import sqlite3

from unilabos.workflow.execution_lock_lease import (
    mark_execution_locks_uncertain,
    release_execution_locks,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE execution_lock_lease (
            uuid TEXT, workflow_node_job_uuid TEXT, lock_key TEXT, state TEXT,
            meta_data TEXT, released_at TEXT, update_time TEXT, deleted_at TEXT);
        CREATE TABLE execution_claim (
            workflow_node_job_uuid TEXT, state TEXT, released_at TEXT,
            update_time TEXT);
    """)
    for uuid, lock_key, interval_ids in [
        ("kept", "/devices/pipette", ["shared"]),
        ("temp", "/devices/other", []),
    ]:
        connection.execute(
            "INSERT INTO execution_lock_lease VALUES (?, 'job', ?, 'running', ?, NULL, NULL, NULL)",
            (uuid, lock_key, json.dumps({"resource_interval_ids": interval_ids})),
        )
    connection.execute("INSERT INTO execution_claim VALUES ('job', 'running', NULL, NULL)")
    return connection


def _states(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT uuid, state FROM execution_lock_lease").fetchall()
    return {row["uuid"]: row["state"] for row in rows}


def test_proved_success_restores_retained_lease_from_uncertain_to_running():
    connection = _connection()
    mark_execution_locks_uncertain(connection, job_uuid="job", now="t1")
    assert _states(connection) == {"kept": "uncertain", "temp": "uncertain"}

    release_execution_locks(connection, job_uuid="job", now="t2", keep_interval_ids=("shared",))

    assert _states(connection) == {"kept": "running", "temp": "released"}
    claim = connection.execute("SELECT state FROM execution_claim").fetchone()
    assert claim["state"] == "running"


def test_release_without_retained_interval_still_releases_uncertain_claim():
    connection = _connection()
    mark_execution_locks_uncertain(connection, job_uuid="job", now="t1")

    release_execution_locks(connection, job_uuid="job", now="t2", keep_interval_ids=("other",))

    assert _states(connection) == {"kept": "released", "temp": "released"}
    claim = connection.execute("SELECT state FROM execution_claim").fetchone()
    assert claim["state"] == "released"
