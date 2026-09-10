"""F08 单节点调试复用冻结执行计划（ExecutionPlan）的合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.workflow.test_f05_workflow_spec_compiler import (
    FIRST_NODE_UUID,
    SOURCE_NODE_UUID,
    _task_snapshot,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

HTTP_NODE_A_UUID = "81000000-0000-4000-8000-000000000001"
HTTP_NODE_B_UUID = "81000000-0000-4000-8000-000000000002"
UNKNOWN_NODE_UUID = "81000000-0000-4000-8000-000000000099"
MIXED_SOURCE_UUID = "81000000-0000-4000-8000-000000000003"
MIXED_MATERIAL_UUID = "81000000-0000-4000-8000-000000000004"


def _http_runtime(database: Path) -> tuple[TestClient, WorkflowStore]:
    """创建 F08 公共 HTTP 合同使用的隔离运行时。

    参数：``database`` 是本测试独占的工作流 SQLite 路径。返回：公共客户端与
    可检查物理写入的存储。异常：初始化失败时保留底层异常。
    """

    store = WorkflowStore(database)
    return TestClient(create_workflow_app(WorkflowService(store))), store


def _http_node(identity: str, *, disabled: bool = False) -> dict[str, Any]:
    """构造无端口依赖的可执行手工确认节点。

    参数：``identity`` 是稳定节点 UUID，``disabled`` 控制是否排除执行。返回：
    公共图写接口接受的节点对象。异常：无，非法身份由真实接口失败关闭。
    """

    return {
        "uuid": identity,
        "name": f"manual-{identity[-1]}",
        "type": "manual_confirm",
        "pose": {},
        "param": {},
        "execution_policy": {},
        "disabled": disabled,
        "minimized": False,
        "meta_data": {},
    }


def _create_http_workflow(
    client: TestClient,
    *,
    disable_second: bool = False,
) -> str:
    """通过公共接口创建并应用一个双节点工作流（Workflow）。

    参数：``client`` 是隔离 HTTP 客户端，``disable_second`` 控制第二节点状态。
    返回：服务端生成的工作流 UUID。异常：任何 HTTP/业务失败由断言暴露。
    """

    created = client.post(
        "/api/v1/workflows",
        json={"name": "F08 single node", "tags": [], "meta_data": {}},
    )
    assert created.status_code == 201
    workflow_uuid = str(created.json()["data"]["uuid"])
    saved = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                _http_node(HTTP_NODE_A_UUID),
                _http_node(HTTP_NODE_B_UUID, disabled=disable_second),
            ],
            "edges": [],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["code"] == 0
    return workflow_uuid


def _write_counts(store: WorkflowStore) -> tuple[int, int]:
    """读取任务与作业物理行数。

    参数：``store`` 是当前隔离存储。返回：工作流任务（WorkflowTask）和工作流
    节点作业（WorkflowNodeJob）的行数。异常：保留 SQLite 查询异常。
    """

    tasks = store._conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0]
    jobs = store._conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0]
    return int(tasks), int(jobs)


def test_single_action_target_excludes_material_source_and_all_other_jobs() -> None:
    """目标动作必须是单节点计划中唯一正式作业。

    参数：无。返回：无；断言固定物料来源（MaterialSource）和其他动作都不创建
    工作流节点作业（WorkflowNodeJob），计划图只保留目标节点且不产生 skipped
    作业。异常：计划构建失败或范围回归由断言暴露。
    """

    task, _normal_jobs = _task_snapshot()
    graph = task["workflow_snapshot"]
    original = deepcopy(graph)

    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="single_node",
        target_node_uuid=FIRST_NODE_UUID,
    )

    assert plan["run_mode"] == "single_node"
    assert plan["target_node_uuid"] == FIRST_NODE_UUID
    assert [node["uuid"] for node in plan["nodes"]] == [FIRST_NODE_UUID]
    assert [job["workflow_node_uuid"] for job in jobs] == [FIRST_NODE_UUID]
    assert all(job.get("status") != "skipped" for job in jobs)
    assert SOURCE_NODE_UUID not in {job["workflow_node_uuid"] for job in jobs}
    assert plan["edges"] == []
    assert {handle["node_uuid"] for handle in plan["handles"]} == {FIRST_NODE_UUID}
    assert graph == original


def test_single_material_source_target_is_the_only_coordinator_job() -> None:
    """目标为物料来源时也只建立一个协调器作业。

    参数：无。返回：无；断言物料来源（MaterialSource）可作为显式单节点目标，
    且普通动作不创建作业。异常：计划构建失败或额外作业由断言暴露。
    """

    task, _normal_jobs = _task_snapshot()

    plan, jobs = ExecutionPlanBuilder().build(
        task["workflow_snapshot"],
        run_mode="single_node",
        target_node_uuid=SOURCE_NODE_UUID,
    )

    assert [node["uuid"] for node in plan["nodes"]] == [SOURCE_NODE_UUID]
    assert [job["workflow_node_uuid"] for job in jobs] == [SOURCE_NODE_UUID]
    assert plan["edges"] == []
    assert {handle["node_uuid"] for handle in plan["handles"]} == {SOURCE_NODE_UUID}


def test_http_single_node_keeps_full_snapshot_but_only_one_target_job(
    tmp_path: Path,
) -> None:
    """公共单节点任务保留完整快照并只持久化目标作业。

    参数：``tmp_path`` 是 pytest 隔离目录。返回：无；断言工作流任务
    （WorkflowTask）快照含全部已应用节点，执行计划（ExecutionPlan）及作业仅含
    显式目标。异常：HTTP、持久化或范围回归由断言暴露。
    """

    client, store = _http_runtime(tmp_path / "f08-http.db")
    try:
        workflow_uuid = _create_http_workflow(client)
        created = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "single_node",
                "target_node_uuid": HTTP_NODE_B_UUID,
                "input": {},
            },
        )

        assert created.status_code == 201
        task = created.json()["data"]
        assert task["run_mode"] == "single_node"
        assert task["target_node_uuid"] == HTTP_NODE_B_UUID
        assert {node["uuid"] for node in task["workflow_snapshot"]["nodes"]} == {
            HTTP_NODE_A_UUID,
            HTTP_NODE_B_UUID,
        }
        assert [node["uuid"] for node in task["execution_plan"]["nodes"]] == [
            HTTP_NODE_B_UUID
        ]
        jobs = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
        assert [job["workflow_node_uuid"] for job in jobs] == [HTTP_NODE_B_UUID]
        assert all(job["status"] != "skipped" for job in jobs)
        assert _write_counts(store) == (1, 1)
    finally:
        client.close()
        store.close()


def test_single_node_without_target_selects_first_stable_root(
    tmp_path: Path,
) -> None:
    """省略目标时必须确定性选择首个拓扑根节点。

    参数：``tmp_path`` 是 pytest 隔离目录。返回：无；断言任务、计划和唯一作业
    使用同一稳定根身份。异常：HTTP 或排序回归由断言暴露。
    """

    client, store = _http_runtime(tmp_path / "f08-default-target.db")
    try:
        workflow_uuid = _create_http_workflow(client)
        created = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": workflow_uuid, "run_mode": "single_node"},
        )

        assert created.status_code == 201
        task = created.json()["data"]
        assert task["target_node_uuid"] == HTTP_NODE_A_UUID
        assert task["execution_plan"]["target_node_uuid"] == HTTP_NODE_A_UUID
        assert [node["uuid"] for node in task["execution_plan"]["nodes"]] == [
            HTTP_NODE_A_UUID
        ]
        assert _write_counts(store) == (1, 1)
    finally:
        client.close()
        store.close()


def test_single_node_without_target_keeps_mixed_root_topological_order() -> None:
    """省略目标时不得把较晚物料来源提升到较早普通根之前。

    参数：无。返回：无；断言混合普通动作与物料来源（MaterialSource）的稳定
    拓扑根顺序不会因执行种类分组而改变。异常：计划构建或默认目标回归由断言
    暴露。
    """

    graph = {
        "nodes": [
            {
                **_http_node(HTTP_NODE_A_UUID),
                "create_time": "2026-08-05T00:00:00Z",
            },
            {
                "uuid": MIXED_SOURCE_UUID,
                "name": "later-source",
                "type": "material_source",
                "create_time": "2026-08-05T00:00:01Z",
                "param": {
                    "mode": "existing",
                    "material_uuid": MIXED_MATERIAL_UUID,
                },
                "disabled": False,
            },
        ],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }

    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="single_node",
        target_node_uuid=None,
    )

    assert plan["target_node_uuid"] == HTTP_NODE_A_UUID
    assert [node["uuid"] for node in plan["nodes"]] == [HTTP_NODE_A_UUID]
    assert [job["workflow_node_uuid"] for job in jobs] == [HTTP_NODE_A_UUID]


def test_unknown_and_disabled_single_node_targets_write_nothing(
    tmp_path: Path,
) -> None:
    """未知或禁用目标必须在首次持久写入前失败关闭。

    参数：``tmp_path`` 是 pytest 隔离目录。返回：无；断言两类非法目标都返回
    稳定错误且任务/作业零写入。异常：HTTP 或事务原子性回归由断言暴露。
    """

    client, store = _http_runtime(tmp_path / "f08-invalid-target.db")
    try:
        workflow_uuid = _create_http_workflow(client, disable_second=True)
        for target in (UNKNOWN_NODE_UUID, HTTP_NODE_B_UUID):
            response = client.post(
                "/api/v1/workflow-tasks",
                json={
                    "workflow_uuid": workflow_uuid,
                    "run_mode": "single_node",
                    "target_node_uuid": target,
                },
            )
            assert response.status_code == 200
            assert response.json()["code"] == 1000
            assert _write_counts(store) == (0, 0)
    finally:
        client.close()
        store.close()
