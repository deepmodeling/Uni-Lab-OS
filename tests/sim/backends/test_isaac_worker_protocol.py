import json
from types import MethodType

import pytest

from unilabos.sim.backends.isaac import worker
from unilabos.sim.backends.isaac.worker import IsaacWorkerState
from unilabos.sim.backends.isaac.protocol import decode_request, encode_error, encode_response


def test_decode_request_reads_operation_and_args():
    op, args = decode_request(b'{"op":"step","args":{"dt":0.05}}')

    assert op == "step"
    assert args == {"dt": 0.05}


def test_decode_request_rejects_missing_operation():
    with pytest.raises(ValueError, match="RPC request missing op"):
        decode_request(b'{"args":{}}')


def test_decode_request_rejects_non_object_args():
    with pytest.raises(ValueError, match="RPC request args must be an object"):
        decode_request(b'{"op":"step","args":[1,2]}')


def test_encode_response_matches_client_decode_shape():
    body = encode_response({"ok_value": 1})

    assert json.loads(body.decode("utf-8")) == {"ok": True, "result": {"ok_value": 1}}


def test_encode_error_matches_client_decode_shape():
    body = encode_error("bad scene")

    assert json.loads(body.decode("utf-8")) == {"ok": False, "error": "bad scene"}


class FakeJointController:
    def list_joint_controls(self):
        return [{"name": "arm1_joint"}]

    def get_joint_control_state(self):
        return {"status": "ready"}

    def plan_joint_targets(self, targets, options=None):
        return {"plan_id": "plan_0001", "targets": targets, "options": options or {}}

    def check_joint_plan(self, plan_id):
        return {"plan_id": plan_id, "safe": True}

    def execute_joint_plan(self, plan_id):
        return {"plan_id": plan_id, "ok": True}

    def stop_joint_motion(self):
        return {"ok": True, "code": "stopped"}

    def set_collision_check_enabled(self, enabled):
        return {"collision_check_enabled": bool(enabled)}

    def apply_stable_drive_settings(self):
        return {"ok": True, "code": "stable_drive_applied"}


class FakeJointControlService:
    def __init__(self):
        self.enabled = True
        self.drive_applied = False

    def get_joint_control_state(self):
        return {"collision_check_enabled": self.enabled}

    def set_collision_check_enabled(self, enabled):
        self.enabled = bool(enabled)
        return {"collision_check_enabled": self.enabled}

    def apply_stable_drive_settings(self):
        self.drive_applied = True
        return {"ok": True, "code": "stable_drive_applied"}


class FakeApp:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


class FakeStringModel:
    def __init__(self, value=""):
        self.value = value

    def set_value(self, value):
        self.value = str(value)


def test_worker_dispatches_joint_control_operations():
    state = IsaacWorkerState(FakeJointController())

    assert state.dispatch("list_joint_controls", {}) == [{"name": "arm1_joint"}]
    assert state.dispatch("get_joint_control_state", {}) == {"status": "ready"}
    assert state.dispatch("plan_joint_targets", {"targets": {"arm1_joint": 10.0}}) == {
        "plan_id": "plan_0001",
        "targets": {"arm1_joint": 10.0},
        "options": {},
    }
    assert state.dispatch("check_joint_plan", {"plan_id": "plan_0001"}) == {
        "plan_id": "plan_0001",
        "safe": True,
    }
    assert state.dispatch("execute_joint_plan", {"plan_id": "plan_0001"}) == {
        "plan_id": "plan_0001",
        "ok": True,
    }
    assert state.dispatch("stop_joint_motion", {}) == {"ok": True, "code": "stopped"}
    assert state.dispatch("set_collision_check_enabled", {"enabled": False}) == {"collision_check_enabled": False}
    assert state.dispatch("apply_stable_drive_settings", {}) == {"ok": True, "code": "stable_drive_applied"}


def test_isaac_controller_runs_queued_ui_actions_before_idle_update():
    controller = object.__new__(worker.IsaacController)
    controller.app = FakeApp()
    controller._joint_control_status_model = None
    controller._init_ui_action_queue()
    events = []

    controller._enqueue_ui_action("Check", lambda: events.append(("action", controller.app.updates)))

    assert events == []

    controller.idle()

    assert events == [("action", 0)]
    assert controller.app.updates == 1


def test_isaac_controller_toggles_collision_check_from_ui_action():
    controller = object.__new__(worker.IsaacController)
    controller._joint_control_status_model = None
    controller._collision_mode_model = FakeStringModel()
    service = FakeJointControlService()
    controller._get_joint_control_service = MethodType(lambda _self: service, controller)

    controller._ui_toggle_collision_check_now()

    assert service.enabled is False
    assert "OFF" in controller._collision_mode_model.value
    assert "bypasses" in controller._collision_mode_model.value


def test_isaac_controller_applies_stable_drive_from_ui_action():
    controller = object.__new__(worker.IsaacController)
    controller._joint_control_status_model = FakeStringModel()
    controller._collision_mode_model = FakeStringModel()
    service = FakeJointControlService()
    controller._get_joint_control_service = MethodType(lambda _self: service, controller)

    controller._ui_apply_stable_drive_now()

    assert service.drive_applied is True
    assert "Stable drive applied" in controller._joint_control_status_model.value
