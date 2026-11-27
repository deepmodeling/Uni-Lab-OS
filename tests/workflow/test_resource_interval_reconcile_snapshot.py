"""批量锁结算只读取一次事务快照，不混入历史失败 attempt。"""

import json
import sqlite3

from unilabos.workflow import resource_lock_plan
from unilabos.workflow import task_runtime_projection as projection


def test_terminal_reconciliation_reuses_transaction_snapshot(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE workflow_task (uuid TEXT, execution_plan TEXT, deleted_at TEXT);
        CREATE TABLE workflow_node_job (
            uuid TEXT, workflow_task_uuid TEXT, workflow_node_uuid TEXT,
            status TEXT, meta_data TEXT, uncertainty_reason TEXT,
            current_attempt INTEGER, deleted_at TEXT, control_data TEXT);
        CREATE TABLE execution_lock_lease (
            workflow_node_job_uuid TEXT, state TEXT, deleted_at TEXT);
        CREATE TABLE execution_claim (workflow_node_job_uuid TEXT, state TEXT);
    """)
    connection.execute(
        "INSERT INTO workflow_task VALUES (?, ?, NULL)", ("task", '{"nodes":[]}')
    )
    for identity, status, current in [
        ("a", "succeeded", 1),
        ("b", "succeeded", 1),
        ("old", "failed", 0),
        ("next", "pending", 1),
    ]:
        connection.execute(
            "INSERT INTO workflow_node_job VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                identity,
                "task",
                identity,
                status,
                "{}",
                "",
                current,
                json.dumps({"resource_interval_ids": ["shared"]}),
            ),
        )
        # 终态作业仍持有活跃租约, 才是对账要考察的对象。
        connection.execute(
            "INSERT INTO execution_lock_lease VALUES (?, 'running', NULL)", (identity,)
        )
    policies = []
    released = []
    queries = []
    connection.set_trace_callback(queries.append)

    def policy(plan, ids, job, jobs):
        policies.append((plan, jobs))
        assert [row["workflow_node_uuid"] for row in jobs] == ["a", "b", "next"]
        return ("shared",)

    monkeypatch.setattr(resource_lock_plan, "retained_resource_interval_ids", policy)
    monkeypatch.setattr(
        projection,
        "release_execution_locks",
        lambda connection, **kwargs: released.append(kwargs),
    )
    projection._reconcile_terminal_resource_intervals(
        connection, task_uuid="task", now="now"
    )
    assert len(policies) == 3
    assert all(
        plan is policies[0][0] and jobs is policies[0][1] for plan, jobs in policies
    )
    assert sum("current_attempt=1" in query for query in queries) == 1
    assert [row["job_uuid"] for row in released] == ["a", "b", "old"]
    assert all(row["keep_interval_ids"] == ("shared",) for row in released)
    # 下一次事务必须重新观察后继完成，不能使用进程级缓存。
    connection.execute(
        "UPDATE workflow_node_job SET status='succeeded' WHERE uuid='next'"
    )
    policies.clear()
    projection._reconcile_terminal_resource_intervals(
        connection, task_uuid="task", now="later"
    )
    assert policies[0][1][-1]["status"] == "succeeded"
    assert sum("current_attempt=1" in query for query in queries) == 2


def test_terminal_reconciliation_skips_jobs_without_active_locks(monkeypatch):
    """已全部释放的终态作业不再逐个重算保留区间。

    release_execution_locks 只改 reserved/running/uncertain 状态的租约与 claim,
    对无活跃租约的作业是空操作; 但每个作业完成都要走一遍终态对账, 若扫全部
    终态作业, 单次代价随已完成作业数线性增长 (pTLC 5383 节点任务实测 6.8 s)。
    """

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE workflow_task (uuid TEXT, execution_plan TEXT, deleted_at TEXT);
        CREATE TABLE workflow_node_job (
            uuid TEXT, workflow_task_uuid TEXT, workflow_node_uuid TEXT,
            status TEXT, meta_data TEXT, uncertainty_reason TEXT,
            current_attempt INTEGER, deleted_at TEXT, control_data TEXT);
        CREATE TABLE execution_lock_lease (
            workflow_node_job_uuid TEXT, state TEXT, deleted_at TEXT);
        CREATE TABLE execution_claim (workflow_node_job_uuid TEXT, state TEXT);
    """)
    connection.execute(
        "INSERT INTO workflow_task VALUES (?, ?, NULL)", ("task", '{"nodes":[]}')
    )
    control = json.dumps({"resource_interval_ids": ["shared"]})
    for identity in ("released", "leased", "claimed", "soft_deleted_lease"):
        connection.execute(
            "INSERT INTO workflow_node_job VALUES (?, ?, ?, 'succeeded', '{}', '', 1, NULL, ?)",
            (identity, "task", identity, control),
        )
    connection.execute(
        "INSERT INTO execution_lock_lease VALUES ('released', 'released', NULL)"
    )
    connection.execute(
        "INSERT INTO execution_lock_lease VALUES ('leased', 'reserved', NULL)"
    )
    connection.execute("INSERT INTO execution_claim VALUES ('claimed', 'running')")
    connection.execute(
        "INSERT INTO execution_lock_lease VALUES ('soft_deleted_lease', 'running', 'gone')"
    )
    considered = []
    released = []
    monkeypatch.setattr(
        resource_lock_plan,
        "retained_resource_interval_ids",
        lambda plan, ids, job, jobs: considered.append(job["workflow_node_uuid"]) or ("shared",),
    )
    monkeypatch.setattr(
        projection,
        "release_execution_locks",
        lambda connection, **kwargs: released.append(kwargs["job_uuid"]),
    )
    projection._reconcile_terminal_resource_intervals(
        connection, task_uuid="task", now="now"
    )
    assert considered == ["leased", "claimed"]
    assert released == ["leased", "claimed"]


def test_retention_reads_modern_plan_without_copying_graph():
    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError(
                "Read-only resource policy must not copy unrelated graph data"
            )

    plan = {
        "nodes": [NoCopy()],
        "resource_plan": {
            "intervals": [
                {"interval_id": "i", "node_uuids": ["a", "b"], "resource_id": "robot"}
            ],
            "resources": [{"resource_id": "robot", "alias": "robot"}],
        },
    }
    jobs = [
        {"workflow_node_uuid": "a", "status": "succeeded"},
        {"workflow_node_uuid": "b", "status": "pending"},
    ]
    assert resource_lock_plan.retained_resource_interval_ids(
        plan, ["i"], jobs[0], jobs
    ) == ("i",)
    jobs[1]["status"] = "succeeded"
    assert (
        resource_lock_plan.retained_resource_interval_ids(plan, ["i"], jobs[0], jobs)
        == ()
    )
    assert plan["resource_plan"]["intervals"][0]["node_uuids"] == ["a", "b"]
