import sys
import threading

from unilabos.sim.backends.isaac import worker


def test_worker_import_does_not_import_isaac_modules():
    assert "isaacsim" not in sys.modules
    assert "omni.usd" not in sys.modules


def test_encode_png_rgb_writes_complete_png():
    image = [
        [[255, 0, 0], [0, 255, 0]],
        [[0, 0, 255], [255, 255, 255]],
    ]
    payload = worker.encode_png_rgb(image)

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload.endswith(b"IEND\xaeB`\x82")


def test_worker_parse_args_defaults():
    args = worker.parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8091
    assert args.headless is True
    assert args.scene is None
    assert args.camera == "/World/Camera"
    assert args.rpc_timeout_s == 600.0
    assert args.joint_control_ui is False


def test_worker_parse_args_accepts_joint_control_ui():
    args = worker.parse_args(["--joint-control-ui"])

    assert args.joint_control_ui is True


def test_worker_state_dispatches_to_controller():
    class FakeController:
        def __init__(self):
            self.calls = []

        def load_scene(self, scene_path):
            self.calls.append(("load_scene", scene_path))

        def step(self, dt):
            self.calls.append(("step", dt))

        def get_observation(self, entity_id):
            return {"entity_id": entity_id}

    controller = FakeController()
    state = worker.IsaacWorkerState(controller)

    state.dispatch("load_scene", {"scene_path": "/tmp/lab.usd"})
    state.dispatch("step", {"dt": 0.1})
    assert state.dispatch("get_observation", {"entity_id": "arm"}) == {"entity_id": "arm"}
    assert controller.calls == [("load_scene", "/tmp/lab.usd"), ("step", 0.1)]


def test_worker_state_can_dispatch_controller_calls_on_main_thread():
    class FakeController:
        def __init__(self):
            self.calls = []

        def step(self, dt):
            self.calls.append(("step", dt))
            return {"stepped": dt}

    controller = FakeController()
    state = worker.IsaacWorkerState(controller, dispatch_on_main_thread=True, rpc_timeout_s=1.0)
    result = []

    thread = threading.Thread(target=lambda: result.append(state.dispatch("step", {"dt": 0.25})))
    thread.start()

    assert state.process_next(timeout=1.0) is True
    thread.join(timeout=1.0)

    assert result == [{"stepped": 0.25}]
    assert controller.calls == [("step", 0.25)]
