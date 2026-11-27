"""旧数据库升级到 v4：保留业务数据、回填资源锁归属且允许重复启动。"""

from uuid import uuid4

from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore
from unilabos.app.edge_control.store import EdgeControlStore


def test_edge_fence_upgrade_preserves_and_backfills_task(database_mode, tmp_path):
    path = str(tmp_path / "edge.db")
    task_uuid, job_uuid = str(uuid4()), str(uuid4())
    store = EdgeControlStore(path)
    try:
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": str(uuid4()),
                "job_access_token": "test-token",
                "claim_uuid": str(uuid4()),
                "attempt": 1,
                "fences": [{"lock_key": "/device/one", "fencing_token": 7}],
            },
            str(uuid4()),
        )
        with store.db.transaction():
            store.db.execute("ALTER TABLE edge_resource_fence DROP COLUMN task_uuid")
            store.db.execute(
                "INSERT INTO edge_resource_fence VALUES (?, ?, ?, ?, ?)",
                ("/device/retired", 9, str(uuid4()), str(uuid4()), 1.0),
            )
            if database_mode == "sqlite":
                store.db.execute("PRAGMA user_version = 3")
    finally:
        store.close()

    for _ in range(2):
        reopened = EdgeControlStore(path)
        try:
            rows = reopened.db.query_all(
                "SELECT lock_key, last_fencing_token, task_uuid "
                "FROM edge_resource_fence ORDER BY lock_key"
            )
            assert [tuple(row) for row in rows] == [
                ("/device/one", 7, task_uuid), ("/device/retired", 9, "")
            ]
            assert reopened.get_job(job_uuid).task_uuid == task_uuid
        finally:
            reopened.close()


def test_authority_upgrade_preserves_jobs_and_resolution(database_mode, tmp_path):
    from tests.networking.test_local_edge_authority import _payload

    path = tmp_path / "authority.db"
    payload = _payload()
    store = LocalEdgeAuthorityStore(path)
    try:
        store.dispatch(payload)
        with store.db.transaction():
            store.db.execute("ALTER TABLE local_edge_job DROP COLUMN resolution_outcome_json")
    finally:
        store.close()

    for expected in (None, '{"outcome":"verified"}'):
        reopened = LocalEdgeAuthorityStore(path)
        try:
            row = reopened.db.query_one(
                "SELECT task_uuid, resolution_outcome_json FROM local_edge_job WHERE job_uuid=?",
                (payload["job_id"],),
            )
            assert row["task_uuid"] == payload["task_id"]
            assert row["resolution_outcome_json"] == expected
            with reopened.db.transaction():
                reopened.db.execute(
                    "UPDATE local_edge_job SET resolution_outcome_json=? WHERE job_uuid=?",
                    ('{"outcome":"verified"}', payload["job_id"]),
                )
        finally:
            reopened.close()
