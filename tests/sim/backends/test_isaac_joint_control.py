from __future__ import annotations

import pytest

from unilabos.sim.backends.isaac.joint_control import (
    DEFAULT_MVPPKUSHENGKE_JOINTS,
    DEFAULT_STABLE_DRIVE_SETTINGS,
    JointControlError,
    JointControlService,
    PlanOptions,
)


class FakeJointAdapter:
    def __init__(self, positions=None, unsafe_waypoint=None, baseline_contacts=None):
        self.positions = dict(positions or {})
        self.unsafe_waypoint = unsafe_waypoint
        self.baseline_contacts = [dict(contact) for contact in (baseline_contacts or [])]
        self.applied_targets = []
        self.step_calls = []
        self.contact_reads = 0
        self.drive_settings = {}
        self.captured = None
        self.restored = None

    def list_joint_specs(self):
        return DEFAULT_MVPPKUSHENGKE_JOINTS

    def get_joint_positions(self):
        return dict(self.positions)

    def capture_state(self):
        self.captured = dict(self.positions)
        return dict(self.positions)

    def restore_state(self, state):
        self.restored = dict(state)
        self.positions = dict(state)

    def set_joint_targets(self, targets):
        self.applied_targets.append(dict(targets))
        self.positions.update({str(key): float(value) for key, value in targets.items()})

    def step_simulation(self, steps):
        self.step_calls.append(int(steps))

    def get_disallowed_contacts(self):
        self.contact_reads += 1
        waypoint_index = len(self.applied_targets) - 1
        contacts = [dict(contact) for contact in self.baseline_contacts]
        if self.unsafe_waypoint == waypoint_index:
            contacts.append({"body0": "/World/arm3_Link", "body1": "/World/base_link"})
        return contacts

    def apply_drive_settings(self, settings):
        self.drive_settings = {str(name): dict(values) for name, values in settings.items()}
        return self.drive_settings


def test_default_mvppkushengke_joint_specs_have_expected_units_and_limits():
    specs = {spec.name: spec for spec in DEFAULT_MVPPKUSHENGKE_JOINTS}

    assert specs["arm1_joint"].unit == "deg"
    assert specs["arm1_joint"].drive_axis == "angular"
    assert specs["arm1_joint"].lower_limit == pytest.approx(-114.591552734375)
    assert specs["arm1_joint"].upper_limit == pytest.approx(114.591552734375)
    assert specs["arm0_joint"].unit == "m"
    assert specs["arm0_joint"].drive_axis == "linear"
    assert specs["arm0_joint"].upper_limit == pytest.approx(0.4300000071525574)


def test_plan_joint_targets_interpolates_by_largest_joint_step():
    adapter = FakeJointAdapter({"arm1_joint": 0.0, "arm0_joint": 0.0})
    service = JointControlService(adapter)

    plan = service.plan_joint_targets(
        {"arm1_joint": 12.0, "arm0_joint": 0.05},
        PlanOptions(max_revolute_step_deg=5.0, max_prismatic_step_m=0.02),
    )

    assert len(plan.waypoints) == 3
    assert plan.waypoints[0]["arm1_joint"] == pytest.approx(4.0)
    assert plan.waypoints[0]["arm0_joint"] == pytest.approx(0.0166666667)
    assert plan.waypoints[-1] == {"arm1_joint": 12.0, "arm0_joint": 0.05}
    assert service.get_joint_control_state()["last_plan"]["plan_id"] == plan.plan_id


def test_plan_joint_targets_has_no_waypoints_for_noop_targets():
    adapter = FakeJointAdapter({"arm1_joint": 0.0, "arm0_joint": 0.0})
    service = JointControlService(adapter)

    plan = service.plan_joint_targets({"arm1_joint": 0.0, "arm0_joint": 0.0})

    assert plan.waypoints == []


def test_plan_joint_targets_rejects_unknown_or_out_of_limit_targets():
    service = JointControlService(FakeJointAdapter({"arm1_joint": 0.0}))

    with pytest.raises(JointControlError, match="unknown_joint"):
        service.plan_joint_targets({"unknown_joint": 1.0})

    with pytest.raises(JointControlError, match="joint_limit_exceeded"):
        service.plan_joint_targets({"arm1_joint": 200.0})


def test_execute_requires_checked_safe_plan():
    adapter = FakeJointAdapter({"arm1_joint": 0.0})
    service = JointControlService(adapter)
    plan = service.plan_joint_targets({"arm1_joint": 5.0})

    with pytest.raises(JointControlError, match="plan_not_checked"):
        service.execute_joint_plan(plan.plan_id)

    check = service.check_joint_plan(plan.plan_id)
    result = service.execute_joint_plan(plan.plan_id)

    assert check.safe is True
    assert result.ok is True
    assert adapter.applied_targets[-1] == {"arm1_joint": 5.0}


def test_collision_check_rejects_unsafe_plan_and_restores_state():
    adapter = FakeJointAdapter({"arm1_joint": 0.0}, unsafe_waypoint=1)
    service = JointControlService(adapter)
    plan = service.plan_joint_targets({"arm1_joint": 12.0}, PlanOptions(max_revolute_step_deg=5.0))

    check = service.check_joint_plan(plan.plan_id)

    assert check.safe is False
    assert check.code == "collision_detected"
    assert check.waypoint_index == 1
    assert check.contacts == [{"body0": "/World/arm3_Link", "body1": "/World/base_link"}]
    assert adapter.restored == {"arm1_joint": 0.0}
    with pytest.raises(JointControlError, match="plan_not_safe"):
        service.execute_joint_plan(plan.plan_id)


def test_collision_check_ignores_contacts_present_before_plan():
    baseline_contact = {"body0": "/World/base_link", "body1": "/World/table"}
    adapter = FakeJointAdapter({"arm1_joint": 0.0}, baseline_contacts=[baseline_contact])
    service = JointControlService(adapter)
    plan = service.plan_joint_targets({"arm1_joint": 8.0}, PlanOptions(max_revolute_step_deg=4.0))

    check = service.check_joint_plan(plan.plan_id)

    assert check.safe is True
    assert check.code == "safe"
    assert check.contacts == []
    assert adapter.restored == {"arm1_joint": 0.0}


def test_collision_check_reports_only_new_contacts_after_baseline():
    baseline_contact = {"body0": "/World/base_link", "body1": "/World/table"}
    adapter = FakeJointAdapter(
        {"arm1_joint": 0.0},
        unsafe_waypoint=1,
        baseline_contacts=[baseline_contact],
    )
    service = JointControlService(adapter)
    plan = service.plan_joint_targets({"arm1_joint": 12.0}, PlanOptions(max_revolute_step_deg=5.0))

    check = service.check_joint_plan(plan.plan_id)

    assert check.safe is False
    assert check.code == "collision_detected"
    assert check.waypoint_index == 1
    assert check.contacts == [{"body0": "/World/arm3_Link", "body1": "/World/base_link"}]


def test_collision_check_can_be_disabled_and_reports_mode_in_state():
    adapter = FakeJointAdapter({"arm1_joint": 0.0}, unsafe_waypoint=0)
    service = JointControlService(adapter)
    service.set_collision_check_enabled(False)
    plan = service.plan_joint_targets({"arm1_joint": 5.0}, PlanOptions(max_revolute_step_deg=5.0))

    check = service.check_joint_plan(plan.plan_id)

    assert service.get_joint_control_state()["collision_check_enabled"] is False
    assert check.safe is True
    assert check.code == "collision_check_disabled"
    assert check.message == "Collision check is disabled"
    assert adapter.contact_reads == 0
    assert adapter.applied_targets == []


def test_apply_stable_drive_settings_updates_adapter_and_state():
    adapter = FakeJointAdapter({"arm1_joint": 0.0})
    service = JointControlService(adapter)

    result = service.apply_stable_drive_settings()

    assert result["ok"] is True
    assert result["code"] == "stable_drive_applied"
    assert result["settings"]["arm1_joint"] == DEFAULT_STABLE_DRIVE_SETTINGS["arm1_joint"]
    assert adapter.drive_settings["arm0_joint"] == DEFAULT_STABLE_DRIVE_SETTINGS["arm0_joint"]
    state = service.get_joint_control_state()
    assert state["drive_tuning"]["mode"] == "stable"
    assert state["drive_tuning"]["settings"]["arm3_joint"] == DEFAULT_STABLE_DRIVE_SETTINGS["arm3_joint"]


def test_execute_allows_unchecked_plan_when_collision_check_disabled():
    adapter = FakeJointAdapter({"arm1_joint": 0.0}, unsafe_waypoint=0)
    service = JointControlService(adapter)
    service.set_collision_check_enabled(False)
    plan = service.plan_joint_targets({"arm1_joint": 5.0}, PlanOptions(max_revolute_step_deg=5.0))

    result = service.execute_joint_plan(plan.plan_id)

    assert result.ok is True
    assert result.code == "executed"
    assert adapter.contact_reads == 0
    assert adapter.applied_targets[-1] == {"arm1_joint": 5.0}


def test_stop_joint_motion_prevents_running_execution():
    adapter = FakeJointAdapter({"arm1_joint": 0.0})
    service = JointControlService(adapter)

    service.stop_joint_motion()

    assert service.get_joint_control_state()["status"] == "stopped"
