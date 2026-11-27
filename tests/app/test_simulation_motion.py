"""TrajectoryReceipt 的公开合同：OS 只校验回执，不接管领域规划。"""
import pytest

from unilabos.simulation.motion import (
    CollisionStatus,
    EvidenceLevel,
    ReceiptRejected,
    TrajectoryReceipt,
    verify_trajectory_receipt,
)

SHA = "sha256:" + "a" * 64


def receipt(**overrides):
    values = dict(
        action_id="action-1",
        trajectory_digest=SHA,
        scene_digest=SHA,
        units="m",
        frames=("world", "tcp"),
        topology="external_axis",
        planned="planner:rrt-connect",
        executed="exec:1",
        observed="readback:1",
        settled="settle:1",
        evidence_level=EvidenceLevel.PHYSICS_OBSERVED,
        collision_status=CollisionStatus.CLEAR,
    )
    values.update(overrides)
    return TrajectoryReceipt(**values)


def test_complete_receipt_is_accepted():
    verify_trajectory_receipt(receipt())


def test_missing_planned_stage_is_rejected():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(receipt(planned=""))


def test_malformed_digest_is_rejected():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(receipt(scene_digest="sha256:not-a-digest"))


def test_physical_qualified_requires_clear_collision():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(
            receipt(
                evidence_level=EvidenceLevel.PHYSICAL_QUALIFIED,
                collision_status=CollisionStatus.COLLISION,
            )
        )


def test_observed_requires_executed_stage():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(receipt(executed=None))


def test_replay_cannot_claim_physics_observed():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(
            receipt(replay=True, evidence_level=EvidenceLevel.PHYSICS_OBSERVED)
        )


def test_collision_checked_requires_checked_status():
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(
            receipt(
                evidence_level=EvidenceLevel.COLLISION_CHECKED,
                collision_status=CollisionStatus.NOT_CHECKED,
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [{"units": ""}, {"frames": ()}, {"topology": ""}, {"frames": ("world", "world")}],
)
def test_units_frames_and_topology_are_required_and_unique(overrides):
    with pytest.raises(ReceiptRejected):
        verify_trajectory_receipt(receipt(**overrides))
