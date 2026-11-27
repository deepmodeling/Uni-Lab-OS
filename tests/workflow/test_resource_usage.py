"""资源页面必须分离任务物料绑定与执行锁，且读取不改变状态。"""

import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.error_handling_api import create_error_handling_router
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.store import WorkflowStore, utc_now


def test_resource_usage_reads_bindings_and_preserves_terminal_owners(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    workflow, task, node, job, material, binding = (str(uuid4()) for _ in range(6))
    store.create_workflow(workflow_uuid=workflow, name="资源使用", tags=[], description=None, meta_data={})
    now = utc_now()
    with store.transaction() as conn:
        conn.execute("""INSERT INTO workflow_task(uuid,create_time,update_time,workflow_uuid,status,
            workflow_snapshot,execution_plan,run_mode,control_status,cleanup_status,trace_context,input,output,error_info,meta_data)
            VALUES(?,?,?,?,'running','{}','{}','normal','active','none','{}','{}','{}','[]','{}')""", (task, now, now, workflow))
        conn.execute("""INSERT INTO workflow_node_job(uuid,create_time,update_time,workflow_task_uuid,
            workflow_node_uuid,topological_index,executor_kind,execution_policy,status,attempt,param,feedback_data,
            return_info,control_data,error_info,expected_change_set,meta_data,feedback_sequence,execution_timeout_seconds)
            VALUES(?,?,?,?,?,0,'device_action','{}','running',1,'{}','{}','{}','{}','[]',?,'{}',0,0)""",
            (job, now, now, task, node, '{"kind":"material_transfer","material_uuid":"sample","target_site_uuid":"target"}'))
    control = StationExecutionControl(store)
    initial = control.resource_usage()
    assert initial["material_bindings"] == []
    assert initial["occupancies"] == []
    assert len(initial["jobs"]) == 1  # 无锁在途动作也不能被丢弃。
    with store.transaction() as conn:
        conn.execute("""INSERT INTO workflow_task_material_claim(uuid,create_time,update_time,
            workflow_task_uuid,workflow_node_uuid,workflow_node_job_uuid,material_uuid,status,revision,acquired_at)
            VALUES(?,?,?,?,?,?,?,'active',1,?)""", (binding, now, now, task, node, job, material, now))
        conn.execute("UPDATE workflow_node_job SET status='failed' WHERE uuid=?", (job,))
        conn.execute("UPDATE workflow_task SET status='canceled',cleanup_status='requires_attention' WHERE uuid=?", (task,))
    with store.read() as conn:
        before_changes = getattr(conn, "total_changes", None)
        tables = ("workflow_task", "workflow_node_job", "workflow_task_material_claim")

        def snapshot():
            return {
                table: sorted(
                    (dict(row) for row in conn.execute(f"SELECT * FROM {table}")),
                    key=lambda row: json.dumps(row, sort_keys=True),
                )
                for table in tables
            }

        before = snapshot()
    service = ErrorHandlingService(store, SimpleNamespace(error_control=control), None)
    app = FastAPI()
    app.include_router(create_error_handling_router(service), prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/stations/local/resource-usage")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["material_bindings"][0]["material_uuid"] == material
        assert data["jobs"][0]["task_status"] == "canceled"
        assert data["jobs"][0]["expected_change_set"]["target_site_uuid"] == "target"
        assert client.get("/api/v1/stations/other/resource-usage").status_code == 404
    with store.read() as conn:
        assert snapshot() == before
        if before_changes is not None:
            assert conn.total_changes == before_changes
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_task SET cleanup_status='settled' WHERE uuid=?", (task,))
    assert control.resource_usage()["material_bindings"] == []
    assert control.resource_usage()["jobs"] == []
    store.close()
