from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.backend.scheduler import (
    ActionLockClaim,
    InvalidLockHandoff,
    MaterialHandleTransferProof,
    MaterialLockClaim,
    ResourceLockIdentifier,
    SchedulerLockHandoffRequest,
    SchedulerResourceManager,
    SchedulerResourceRequest,
)


def action(device_id: str, action_name: str) -> ActionLockClaim:
    return ActionLockClaim(device_id=device_id, action_name=action_name)


def material(material_uuid: str) -> MaterialLockClaim:
    return MaterialLockClaim(material_uuid=material_uuid)


def request(
    owner_uuid: str,
    *,
    current_action: ActionLockClaim | None = None,
    always_free: bool = False,
    actions: list[ActionLockClaim] | None = None,
    materials: list[MaterialLockClaim] | None = None,
) -> SchedulerResourceRequest:
    return SchedulerResourceRequest(
        request_uuid=f"request-{owner_uuid}",
        owner_uuid=owner_uuid,
        task_uuid=f"task-{owner_uuid}",
        current_action=current_action,
        always_free=always_free,
        action_lock_claims=actions or [],
        material_lock_claims=materials or [],
    )


def material_proof(
    material_uuid: str,
    *,
    source_owner_uuid: str = "job-a",
    target_owner_uuid: str = "job-b",
) -> MaterialHandleTransferProof:
    return MaterialHandleTransferProof(
        material_uuid=material_uuid,
        workflow_edge_uuid=f"edge-{material_uuid}",
        source_task_uuid=f"task-{source_owner_uuid}",
        source_output_handle_uuid=f"output-{material_uuid}",
        target_task_uuid=f"task-{target_owner_uuid}",
        target_input_handle_uuid=f"input-{material_uuid}",
    )


def test_same_device_action_is_serialized_until_release() -> None:
    manager = SchedulerResourceManager()
    claim = action("arm-1", "move")

    first = manager.acquire(request("job-1", current_action=claim))
    second = manager.acquire(request("job-2", current_action=claim))

    assert first.status == "held"
    assert second.status == "waiting"
    assert second.blockers == ["job-1"]

    manager.release("job-1")

    assert manager.request_for_owner("job-2").status == "held"
    assert manager.owner_of(ResourceLockIdentifier.action(claim)) == "job-2"


def test_always_free_only_skips_the_implicit_current_action_lock() -> None:
    manager = SchedulerResourceManager()
    current = action("reader-1", "read")
    shared_gate = action("arm-1", "workspace")

    first = manager.acquire(
        request(
            "job-1",
            current_action=current,
            always_free=True,
            actions=[shared_gate],
        )
    )
    second = manager.acquire(
        request(
            "job-2",
            current_action=current,
            always_free=True,
            actions=[shared_gate],
        )
    )

    assert [item.canonical_key for item in first.identifiers] == [
        ("action", "arm-1", "workspace")
    ]
    assert first.status == "held"
    assert second.status == "waiting"
    assert manager.owner_of(ResourceLockIdentifier.action(current)) is None


def test_complete_claim_set_is_atomic_and_fifo_only_blocks_overlap() -> None:
    manager = SchedulerResourceManager()
    arm = action("arm-1", "move")
    m1 = material("material-1")
    m2 = material("material-2")

    assert (
        manager.acquire(request("job-a", current_action=arm, materials=[m1])).status
        == "held"
    )
    assert (
        manager.acquire(request("job-b", current_action=arm, materials=[m2])).status
        == "waiting"
    )
    # m2 虽然还空闲，但 job-b 不能只拿一部分。
    assert manager.owner_of(ResourceLockIdentifier.material(m2)) is None
    # job-c 与更早的 job-b 重叠，不能越过它抢 m2。
    assert (
        manager.acquire(request("job-c", always_free=True, materials=[m2])).status
        == "waiting"
    )
    # 完全不相交的申请可以并行。
    assert (
        manager.acquire(
            request("job-d", current_action=action("heater-1", "heat"))
        ).status
        == "held"
    )

    manager.release("job-a")

    assert manager.request_for_owner("job-b").status == "held"
    assert manager.request_for_owner("job-c").status == "waiting"
    assert manager.owner_of(ResourceLockIdentifier.material(m2)) == "job-b"


def test_prelocked_future_action_is_transferred_without_a_free_gap() -> None:
    manager = SchedulerResourceManager()
    prepare = action("loader-1", "prepare")
    robot = action("arm-1", "move")

    manager.acquire(request("job-a", current_action=prepare, actions=[robot]))
    assert manager.acquire(request("job-b", current_action=robot)).status == "waiting"

    handoff = manager.begin_handoff(
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-action",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            action_lock_claims=[robot],
        )
    )

    assert handoff.status == "completed"
    assert manager.owner_of(ResourceLockIdentifier.action(robot)) == "job-b"
    assert manager.owner_of(ResourceLockIdentifier.action(prepare)) == "job-a"
    manager.release("job-a")
    assert manager.request_for_owner("job-b").status == "held"


def test_material_handoff_requires_direct_handle_proof() -> None:
    with pytest.raises(ValidationError, match="Handle proof"):
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-material",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            material_lock_claims=[material("material-1")],
        )


def test_material_handle_proof_must_connect_the_registered_tasks() -> None:
    manager = SchedulerResourceManager()
    material_claim = material("material-1")
    manager.acquire(request("job-a", always_free=True, materials=[material_claim]))
    manager.acquire(request("job-b", always_free=True, materials=[material_claim]))

    with pytest.raises(
        InvalidLockHandoff,
        match="proof source does not match source task",
    ):
        manager.begin_handoff(
            SchedulerLockHandoffRequest(
                handoff_uuid="handoff-material",
                from_owner_uuid="job-a",
                to_owner_uuid="job-b",
                material_lock_claims=[material_claim],
                material_handle_proofs=[
                    material_proof(
                        "material-1",
                        source_owner_uuid="another-job",
                    )
                ],
            )
        )


def test_pending_material_handoff_has_priority_and_source_keeps_lock() -> None:
    manager = SchedulerResourceManager()
    target_action = action("heater-1", "heat")
    material_claim = material("material-1")
    material_identifier = ResourceLockIdentifier.material(material_claim)

    manager.acquire(request("job-x", current_action=target_action))
    manager.acquire(request("job-a", always_free=True, materials=[material_claim]))
    manager.acquire(
        request(
            "job-b",
            current_action=target_action,
            materials=[material_claim],
        )
    )
    manager.acquire(request("job-c", always_free=True, materials=[material_claim]))

    handoff = manager.begin_handoff(
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-material",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            material_lock_claims=[material_claim],
            material_handle_proofs=[material_proof("material-1")],
        )
    )
    assert handoff.status == "pending"
    assert manager.owner_of(material_identifier) == "job-a"

    # 前序终态不会释放已进入转移态的物料。
    assert manager.release("job-a").status == "held"
    assert manager.owner_of(material_identifier) == "job-a"
    assert manager.request_for_owner("job-c").status == "waiting"

    # 后继的剩余动作锁释放后，完整集合一次性交给后继；job-c 不可插队。
    manager.release("job-x")
    assert manager.handoff("handoff-material").status == "completed"
    assert manager.request_for_owner("job-b").status == "held"
    assert manager.owner_of(material_identifier) == "job-b"
    assert manager.request_for_owner("job-c").status == "waiting"

    manager.release("job-b")
    assert manager.request_for_owner("job-c").status == "held"
    assert manager.owner_of(material_identifier) == "job-c"


def test_canceling_target_releases_pending_material_for_next_waiter() -> None:
    manager = SchedulerResourceManager()
    target_action = action("heater-1", "heat")
    material_claim = material("material-1")
    identifier = ResourceLockIdentifier.material(material_claim)

    manager.acquire(request("job-x", current_action=target_action))
    manager.acquire(request("job-a", always_free=True, materials=[material_claim]))
    manager.acquire(
        request(
            "job-b",
            current_action=target_action,
            materials=[material_claim],
        )
    )
    manager.acquire(request("job-c", always_free=True, materials=[material_claim]))
    manager.begin_handoff(
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-material",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            material_lock_claims=[material_claim],
            material_handle_proofs=[material_proof("material-1")],
        )
    )
    manager.release("job-a")

    canceled = manager.cancel_owner("job-b", reason="downstream canceled")

    assert canceled.status == "canceled"
    assert manager.handoff("handoff-material").status == "canceled"
    assert manager.request_for_owner("job-c").status == "held"
    assert manager.owner_of(identifier) == "job-c"


def test_canceling_handoff_before_source_terminal_keeps_source_ownership() -> None:
    manager = SchedulerResourceManager()
    target_action = action("heater-1", "heat")
    material_claim = material("material-1")
    identifier = ResourceLockIdentifier.material(material_claim)

    manager.acquire(request("job-x", current_action=target_action))
    manager.acquire(request("job-a", always_free=True, materials=[material_claim]))
    manager.acquire(
        request(
            "job-b",
            current_action=target_action,
            materials=[material_claim],
        )
    )
    manager.acquire(request("job-c", always_free=True, materials=[material_claim]))
    manager.begin_handoff(
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-material",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            material_lock_claims=[material_claim],
            material_handle_proofs=[material_proof("material-1")],
        )
    )

    manager.cancel_owner("job-b", reason="downstream canceled early")

    assert manager.owner_of(identifier) == "job-a"
    assert manager.request_for_owner("job-c").status == "waiting"
    manager.release("job-a")
    assert manager.owner_of(identifier) == "job-c"


def test_snapshot_and_event_feed_expose_handoff_state() -> None:
    manager = SchedulerResourceManager()
    robot = action("arm-1", "move")
    manager.acquire(request("job-a", actions=[robot], always_free=True))
    manager.acquire(request("job-b", current_action=robot))
    manager.begin_handoff(
        SchedulerLockHandoffRequest(
            handoff_uuid="handoff-action",
            from_owner_uuid="job-a",
            to_owner_uuid="job-b",
            action_lock_claims=[robot],
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.sequence > 0
    assert snapshot.handoffs[0].status == "completed"
    assert snapshot.ownerships[0].owner_uuid == "job-b"
    event_types = [item.event_type for item in manager.events()]
    assert "lock.handoff_pending" in event_types
    assert "lock.transferred" in event_types
    assert [item.sequence for item in manager.events(after_sequence=2)] == list(
        range(3, snapshot.sequence + 1)
    )
