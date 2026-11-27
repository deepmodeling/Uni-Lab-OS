"""边缘执行镜像（EdgeExecutionMirror）的版本、事务和退役生命周期测试。"""

from __future__ import annotations

import sqlite3
import stat
import uuid
from pathlib import Path

import pytest

from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.config.config import BasicConfig


def _claim_fields() -> dict[str, object]:
    """构造 Edge 执行镜像测试所需的稳定 Claim/Fence 载荷。"""

    return {
        "claim_uuid": str(uuid.uuid4()),
        "attempt": 1,
        "fences": [
            {"lock_key": "/devices/test-device", "fencing_token": 1},
        ],
    }


def test_acknowledged_protocol_state_is_retired_without_losing_highwater(
    tmp_path: Path,
) -> None:
    """验证 Backend ACK 后退役命令、发件箱和终态作业，同时保留投递高水位。

    ``tmp_path`` 提供隔离 SQLite 路径；返回为空。测试关闭并重开数据库，证明
    高水位是持久事实而不是从尚未清理的命令行临时推导。
    """

    database_path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(database_path))
    if BasicConfig.database_type == "sqlite":
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    # 下行命令 UUID 是投递游标身份，作业 UUID 是运行镜像稳定身份。
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 17,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        command_uuid,
    )
    command_event_uuid = store.enqueue_event(
        "command.ack", {"command_uuid": command_uuid}
    )
    store.mark_command_completed(command_uuid)

    assert store.last_ack_command_sequence() == 17
    store.acknowledge_event(command_event_uuid)
    assert store.command_status(command_uuid) == ""

    assert store.save_pending_outcome(job_uuid, "failed", {}, [])
    outcome_event_uuid = store.complete_pending_outcome(
        job_uuid, {"job_uuid": job_uuid}
    )
    terminal_job = store.get_job(job_uuid)
    assert terminal_job is not None
    assert terminal_job.job_access_token == ""
    store.acknowledge_event(outcome_event_uuid)
    assert store.get_job(job_uuid) is None
    assert store.pending_events(float("inf")) == []
    store.close()

    reopened = EdgeControlStore(str(database_path))
    assert reopened.last_ack_command_sequence() == 17
    assert reopened.db.index_exists("idx_edge_job_status_updated")
    reopened.close()

    if BasicConfig.database_type == "sqlite":
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == EdgeControlStore.SCHEMA_VERSION


def test_failed_serialization_rolls_back_immediate_transaction(tmp_path: Path) -> None:
    """验证待提交结果编码失败不会遗留占用连接的半开事务。

    ``tmp_path`` 提供隔离数据库；返回为空。不可 JSON 编码的结果触发异常后，
    同一 ``EdgeControlStore`` 必须仍能提交新 meta 事实。
    """

    store = EdgeControlStore(str(tmp_path / "edge-control.db"))
    # 作业/任务/节点 UUID 定位待提交结果，命令 UUID 定位 job.start。
    job_uuid = str(uuid.uuid4())
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        str(uuid.uuid4()),
    )
    with pytest.raises(TypeError):
        store.save_pending_outcome(
            job_uuid,
            "failed",
            {"not_json": object()},
            [],
        )

    store.set_meta("transaction_recovered", "yes")
    assert store.get_meta("transaction_recovered") == "yes"
    store.close()


def test_unknown_outcome_waits_for_final_physical_settlement(tmp_path: Path) -> None:
    """执行未知结果 ACK 后必须保留作业镜像直到最终对账 ACK。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。结果持久化只确定工作流
    节点作业（WorkflowNodeJob）业务结果，最后一条 UNKNOWN 处置 ACK 才证明
    物理结算（PhysicalSettlement）完成并允许退役运行行。
    """

    database_path = tmp_path / "unknown-edge-control.db"
    store = EdgeControlStore(str(database_path))
    # 作业/任务/节点/job.start 命令 UUID 绑定运行镜像，
    # 设备子命令身份绑定待对账物理效果。
    job_uuid = str(uuid.uuid4())
    unknown_command_id = f"workflow-node-job:{job_uuid}:place"
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        str(uuid.uuid4()),
    )
    assert store.save_pending_outcome(
        job_uuid,
        "failed",
        {},
        [{"message": "物理结果未知"}],
        [unknown_command_id],
    )
    # 结果事件 UUID 是 Backend 结果 ACK 的精确关联身份。
    outcome_event_uuid = store.complete_pending_outcome(
        job_uuid,
        {"job_uuid": job_uuid},
    )

    store.acknowledge_event(outcome_event_uuid)
    unresolved_job = store.get_job(job_uuid)
    assert unresolved_job is not None
    assert unresolved_job.status == "outcome_committed_unknown"
    assert unresolved_job.job_access_token == ""

    # 对账命令 UUID 保证人工处置幂等，处置事件 UUID 定位最终 ACK。
    resolution_command_uuid = str(uuid.uuid4())
    store.complete_unknown_resolution(
        resolution_command_uuid,
        {
            "job_uuid": job_uuid,
            "command_uuid": resolution_command_uuid,
            "device_command_id": unknown_command_id,
            "resolution": "canceled",
            "previous_state": "UNKNOWN",
            "current_state": "CANCELED",
            "dispatch_block_reason": "",
        },
        remaining_unknown_command_ids=[],
    )
    resolution_event_uuid = next(
        event.event_uuid
        for event in store.pending_events(float("inf"))
        if event.event_type == "job.unknown_resolution_committed"
    )
    store.acknowledge_event(resolution_event_uuid)
    assert store.get_job(job_uuid) is None
    store.close()


@pytest.mark.parametrize(
    "settlement_order",
    ["unauthorized_before_ack", "ack_before_unauthorized", "outcome_before_ack"],
)
def test_final_unknown_marker_survives_pending_outcome_races(
    tmp_path: Path,
    settlement_order: str,
) -> None:
    """最终 UNKNOWN 事件与待提交结果的三种顺序都必须保留精确 ACK 标记。

    ``tmp_path`` 提供隔离数据库，``settlement_order`` 指定 Backend 先拒绝结果、
    先 ACK 处置或先接受结果；返回为空。根设备命令所依赖的最终事件身份不得
    被 ``outcome_pending`` 覆盖或在事件 ACK 前删除。
    """

    store = EdgeControlStore(
        str(tmp_path / f"pending-outcome-race-{settlement_order}.db")
    )
    # 作业 UUID 同时关联本地 outcome 待办和最终物理处置事件。
    job_uuid = str(uuid.uuid4())
    device_command_id = f"workflow-node-job:{job_uuid}:place"
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        str(uuid.uuid4()),
    )
    assert store.save_pending_outcome(
        job_uuid,
        "canceled",
        {},
        [{"message": "取消完成回调与人工处置重叠"}],
        [device_command_id],
    )
    resolution_command_uuid = str(uuid.uuid4())
    store.complete_unknown_resolution(
        resolution_command_uuid,
        {
            "job_uuid": job_uuid,
            "command_uuid": resolution_command_uuid,
            "device_command_id": device_command_id,
        },
        remaining_unknown_command_ids=[],
    )
    resolution_event_uuid = next(
        event.event_uuid
        for event in store.pending_events(float("inf"))
        if event.event_type == "job.unknown_resolution_committed"
    )
    expected_status = f"outcome_resolution_pending_final:{resolution_event_uuid}"
    pending_final_job = store.get_job(job_uuid)
    assert pending_final_job is not None
    assert pending_final_job.status == expected_status

    if settlement_order == "unauthorized_before_ack":
        assert store.retire_pending_outcome(job_uuid)
        pending_final_job = store.get_job(job_uuid)
        assert pending_final_job is not None
        assert pending_final_job.status == expected_status
        store.acknowledge_event(resolution_event_uuid)
    elif settlement_order == "ack_before_unauthorized":
        store.acknowledge_event(resolution_event_uuid)
        pending_final_job = store.get_job(job_uuid)
        assert pending_final_job is not None
        assert pending_final_job.status == expected_status
        assert store.retire_pending_outcome(job_uuid)
    else:
        store.complete_pending_outcome(job_uuid, {"job_uuid": job_uuid})
        pending_final_job = store.get_job(job_uuid)
        assert pending_final_job is not None
        assert pending_final_job.status == expected_status
        store.acknowledge_event(resolution_event_uuid)

    assert store.get_job(job_uuid) is None
    store.close()


def test_only_final_unknown_resolution_ack_retires_job_mirror(tmp_path: Path) -> None:
    """多条 UNKNOWN 处置中只有最终事件 ACK 能退役作业镜像。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。即使最后一条
    处置已在本地落账，先到的旧处置 ACK 也不得冒充最终物理结算。
    """

    database_path = tmp_path / "multiple-unknown-edge-control.db"
    store = EdgeControlStore(str(database_path))
    # 作业/任务/节点/job.start 命令 UUID 绑定运行镜像，
    # 两条设备子命令用于验证最终事件精确性。
    job_uuid = str(uuid.uuid4())
    first_command_id = f"workflow-node-job:{job_uuid}:pick"
    final_command_id = f"workflow-node-job:{job_uuid}:place"
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        str(uuid.uuid4()),
    )
    assert store.save_pending_outcome(
        job_uuid,
        "failed",
        {},
        [{"message": "两条物理结果未知"}],
        [first_command_id, final_command_id],
    )
    # 结果事件 UUID 定位含两条 UNKNOWN 事实的 Backend ACK。
    outcome_event_uuid = store.complete_pending_outcome(
        job_uuid, {"job_uuid": job_uuid}
    )
    store.acknowledge_event(outcome_event_uuid)

    # 两个对账命令 UUID 分别定位非最终与最终人工处置。
    first_resolution_command_uuid = str(uuid.uuid4())
    final_resolution_command_uuid = str(uuid.uuid4())
    store.complete_unknown_resolution(
        first_resolution_command_uuid,
        {"job_uuid": job_uuid, "device_command_id": first_command_id},
        remaining_unknown_command_ids=[final_command_id],
    )
    first_resolution_event = next(
        event
        for event in store.pending_events(float("inf"))
        if event.event_type == "job.unknown_resolution_committed"
    )
    store.complete_unknown_resolution(
        final_resolution_command_uuid,
        {"job_uuid": job_uuid, "device_command_id": final_command_id},
        remaining_unknown_command_ids=[],
    )
    resolution_events = [
        event
        for event in store.pending_events(float("inf"))
        if event.event_type == "job.unknown_resolution_committed"
    ]
    final_resolution_event = resolution_events[-1]

    store.acknowledge_event(first_resolution_event.event_uuid)
    pending_final_job = store.get_job(job_uuid)
    assert pending_final_job is not None
    assert pending_final_job.status == (
        f"outcome_resolution_pending_final:{final_resolution_event.event_uuid}"
    )

    store.acknowledge_event(final_resolution_event.event_uuid)
    assert store.get_job(job_uuid) is None
    store.close()


def test_settled_job_rejects_late_terminal_callback(tmp_path: Path) -> None:
    """结果已持久化后的迟到设备回调不得重建待办。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。已清空作业
    Token 的运行镜像必须拒绝后续终态写入，否则会形成无法提交的孤儿行。
    """

    store = EdgeControlStore(str(tmp_path / "late-callback.db"))
    # 作业/任务/节点 UUID 绑定运行镜像，job.start 命令 UUID 绑定原始下发。
    job_uuid = str(uuid.uuid4())
    start_command_uuid = str(uuid.uuid4())
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        start_command_uuid,
    )
    assert store.save_pending_outcome(job_uuid, "succeeded", {}, [])
    # 结果事件 UUID 定位 Backend 已持久化的首个终态。
    store.complete_pending_outcome(job_uuid, {"job_uuid": job_uuid})

    assert not store.save_pending_outcome(job_uuid, "failed", {}, [])
    assert store.get_pending_outcome(job_uuid) is None
    settled_job = store.get_job(job_uuid)
    assert settled_job is not None
    assert settled_job.status == "outcome_committed"
    store.close()


def test_store_rejects_newer_schema(tmp_path: Path) -> None:
    """旧 Edge 进程不得打开更高版本状态库继续写入。

    ``tmp_path`` 提供隔离数据库；返回为空。版本门禁必须在任何迁移前关闭失败。
    """

    if BasicConfig.database_type == "postgresql":
        pytest.skip("PostgreSQL 不使用 SQLite PRAGMA user_version")

    database_path = tmp_path / "future-edge-control.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {EdgeControlStore.SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        EdgeControlStore(str(database_path))
