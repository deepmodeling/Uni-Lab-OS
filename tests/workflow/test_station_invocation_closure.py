"""单站闭环的去重合同：同一次中心调用绝不被启动两次。

验收断言是"断网、重启、重复投递之后不重复启动同一调用"。中心侧靠调用身份去重，但
**中心去重无法阻止工站自己再执行一次**：如果工站每次都用新生成的本地身份，本地就会
把它当成一次新调用。因此这一层的规则是：

- 本地身份必须从中心调用身份**推导**，重启后仍然相同；
- 已经落地的调用，重投时返回既有身份并且**不**执行；
- 同一个调用身份携带不同任务包必须被拒绝，而不是当成重放接受；
- 任务被软删除也不释放身份——"不再执行"不等于"可以再执行一次"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from unilabos.workflow.station_invocation import (
    InvocationFingerprintMismatch,
    StationInvocationError,
    begin_station_invocation,
    invocation_key,
    local_task_uuid,
    prepare_plan_builder,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

CENTER_INVOCATION = "5a2116f9-dde4-50fb-aaa5-314ab980d1ca"
CENTER_TASK = "66666666-6666-4666-8666-666666666666"
DIGEST = "sha256:" + "ab" * 32
WORKFLOW = str(uuid5(NAMESPACE_URL, "workflow:single-station-loop"))


def _stable_uuid(value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"station-invocation-closure:{value}"))


def _prepared(node_uuid: str, job_uuid: str) -> PreparedTaskInput:
    plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": node_uuid,
                "kind": "device_action",
                "device_id": "simulated_device",
                "action_name": "run",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {"goal": {"type": "object", "additionalProperties": True}},
                    "additionalProperties": False,
                },
                "material_requirements": [],
            }
        ],
        "handles": [],
        "edges": [],
    }
    return PreparedTaskInput(
        workflow_snapshot={},
        resolved_input={},
        execution_plan=plan,
        jobs=[
            {
                "uuid": job_uuid,
                "workflow_node_uuid": node_uuid,
                "topological_index": 0,
                "executor_kind": "device_action",
                "execution_policy": {},
                "execution_timeout_seconds": 0,
                "param": {},
            }
        ],
    )


def _start(
    store: WorkflowStore,
    *,
    invocation: str = CENTER_INVOCATION,
    digest: str = DIGEST,
) -> Any:
    node_uuid = _stable_uuid(f"node:{invocation}")
    # job 身份按调用区分：同一个 job uuid 重复落库会撞唯一约束，而那与去重无关。
    job_uuid = _stable_uuid(f"job:{invocation}")
    return begin_station_invocation(
        store,
        workflow_uuid=WORKFLOW,
        station_invocation_uuid=invocation,
        global_task_uuid=CENTER_TASK,
        payload_digest=digest,
        run_mode="normal",
        target_node_uuid=None,
        description="单站闭环",
        meta_data={"source": "center"},
        plan_builder=prepare_plan_builder(_prepared(node_uuid, job_uuid)),
        applied_graph={},
    )


def test_local_identity_is_derived_from_the_center_invocation() -> None:
    """本地身份必须确定：重启后算出不同的身份会让中心的事件去重失效。"""

    assert local_task_uuid(CENTER_INVOCATION) == local_task_uuid(CENTER_INVOCATION)
    assert invocation_key(CENTER_INVOCATION) == f"station-invocation-identity:{CENTER_INVOCATION}"
    other = "77777777-7777-4777-8777-777777777777"
    assert local_task_uuid(CENTER_INVOCATION) != local_task_uuid(other)
    with pytest.raises(StationInvocationError):
        local_task_uuid("")
    with pytest.raises(StationInvocationError):
        invocation_key("")


def test_first_delivery_starts_the_invocation(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    result = _start(store)

    assert result.created is True
    assert result.should_execute is True
    assert result.local_task_uuid == local_task_uuid(CENTER_INVOCATION)
    assert result.task["uuid"] == result.local_task_uuid
    store.close()


def test_redelivery_does_not_start_the_same_invocation_twice(tmp_path: Path) -> None:
    """验收的核心断言：重复投递必须取回既有任务，而不是再执行一次。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    first = _start(store)
    second = _start(store)
    third = _start(store)

    assert first.created is True
    assert second.created is False and second.should_execute is False
    assert third.created is False
    # 三次递送必须是同一个本地任务：不同身份意味着同一次调用被执行了多次。
    assert {first.local_task_uuid, second.local_task_uuid, third.local_task_uuid} == {
        first.local_task_uuid
    }
    assert second.task["uuid"] == first.local_task_uuid
    store.close()


def test_restart_keeps_the_identity_so_redelivery_is_still_recognised(tmp_path: Path) -> None:
    """重启后必须仍然认出"这是同一次调用"。

    这是断网/重启场景的关键：新进程若换一个本地身份，本地就会把它当成新调用。
    """

    database = tmp_path / "workflow.db"
    store = WorkflowStore(database)
    first = _start(store)
    store.close()

    # 模拟进程重启：重新打开同一个运行事实库。
    reopened = WorkflowStore(database)
    second = _start(reopened)

    assert second.created is False, "重启后重投被当成了新调用，会重复执行"
    assert second.local_task_uuid == first.local_task_uuid
    reopened.close()


def test_changed_payload_for_the_same_invocation_is_refused(tmp_path: Path) -> None:
    """同一调用身份配不同任务包必须拒绝：继续执行等于执行中心不认可的输入。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    _start(store)

    with pytest.raises(InvocationFingerprintMismatch):
        _start(store, digest="sha256:" + "cd" * 32)
    store.close()


def test_soft_deleted_task_still_occupies_the_identity(tmp_path: Path) -> None:
    """软删除只表示"不再执行"，不表示"可以再执行一次"。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    first = _start(store)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET deleted_at = ? WHERE uuid = ?",
            ("2026-10-14T00:00:00Z", first.local_task_uuid),
        )

    second = _start(store)
    assert second.created is False, "软删除释放了调用身份，同一次调用会被再执行一次"
    assert second.local_task_uuid == first.local_task_uuid
    store.close()


def test_distinct_invocations_get_distinct_local_tasks(tmp_path: Path) -> None:
    """不同调用必须各自落地，不能被去重误伤。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    other = "88888888-8888-4888-8888-888888888888"
    first = _start(store)
    second = _start(store, invocation=other)

    assert first.created is True and second.created is True
    assert first.local_task_uuid != second.local_task_uuid
    store.close()


def test_missing_center_identity_is_refused(tmp_path: Path) -> None:
    """缺少中心身份或摘要时必须拒绝，而不是退化成"每次都是新调用"。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    with pytest.raises(StationInvocationError, match="任务包摘要"):
        _start(store, digest="")
    with pytest.raises(StationInvocationError):
        begin_station_invocation(
            store,
            workflow_uuid=WORKFLOW,
            station_invocation_uuid=CENTER_INVOCATION,
            global_task_uuid="",
            payload_digest=DIGEST,
            run_mode="normal",
            target_node_uuid=None,
            description=None,
            meta_data={},
            plan_builder=prepare_plan_builder(
                _prepared(_stable_uuid("node:x"), _stable_uuid("job:x"))
            ),
            applied_graph={},
        )
    with pytest.raises(StationInvocationError, match="中心调用身份"):
        _start(store, invocation="")
    store.close()
