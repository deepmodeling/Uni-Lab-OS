from unilabos.app import backend as backend_mod
from unilabos.sim.backends.fake_physics import FakePhysicsBackend


class _DummyPhysics:
    name = "dummy"


def test_initialize_runtime_for_backend_builds_fake_physics():
    services = backend_mod._initialize_runtime_for_backend(
        backend="ros",
        kwargs={
            "mode": "sim",
            "sim_rate": 10.0,
            "sim_paused": True,
            "physics": "fake",
            "physics_endpoint": None,
            "physics_scene": "/tmp/lab.usd",
            "physics_timeout": 77.0,
            "disable_sim_services": False,
            "disable_query_api": False,
            "query_grpc_port": 50051,
        },
    )

    assert isinstance(services.context.physics, FakePhysicsBackend)
    assert services.context.physics_backend_name == "fake"
    assert services.context.physics_scene == "/tmp/lab.usd"
    assert services.context.physics_timeout == 77.0
    assert services.context.clock.scale == 10.0
    assert services.context.clock.paused is True
    assert services.context.sim_services_enabled is True
    assert services.context.query_api_enabled is True


def test_initialize_runtime_for_non_ros_keeps_query_api_off():
    services = backend_mod._initialize_runtime_for_backend(
        backend="simple",
        kwargs={"mode": "sim", "physics": "none", "disable_query_api": False},
    )

    assert services.context.physics is None
    assert services.context.query_api_enabled is False


def test_initialize_runtime_for_backend_starts_managed_isaac(monkeypatch):
    started_configs = []
    build_calls = []
    dummy_physics = _DummyPhysics()

    class FakeManagedWorker:
        def __init__(self, config):
            self.config = config
            self.endpoint = config.endpoint
            self.stopped = False

        def start(self):
            started_configs.append(self.config)

        def stop(self):
            self.stopped = True

    def fake_build_physics_backend(name, endpoint=None, scene=None, timeout=120.0):
        build_calls.append({"name": name, "endpoint": endpoint, "scene": scene, "timeout": timeout})
        return dummy_physics

    monkeypatch.setattr(backend_mod, "ManagedIsaacWorker", FakeManagedWorker)
    monkeypatch.setattr(backend_mod, "build_physics_backend", fake_build_physics_backend)
    monkeypatch.setattr(backend_mod.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(backend_mod.signal, "getsignal", lambda _signum: backend_mod.signal.SIG_DFL)
    monkeypatch.setattr(backend_mod.signal, "signal", lambda _signum, _handler: None)
    monkeypatch.setattr(backend_mod, "_managed_isaac_shutdown_registered", False)

    services = backend_mod._initialize_runtime_for_backend(
        backend="ros",
        kwargs={
            "mode": "sim",
            "physics": "isaac",
            "physics_endpoint": None,
            "physics_scene": "/home/ubuntu/labsim/LabUtopia_repro/assets/chemistry_lab/lab_001/lab_001.usd",
            "physics_timeout": 300.0,
            "isaac_managed": True,
            "isaac_host": "127.0.0.1",
            "isaac_port": 8092,
            "isaac_conda_env": "matterix",
            "isaac_conda_executable": "/home/ubuntu/miniforge3/bin/conda",
            "isaac_python": "python",
            "isaac_log_path": "/tmp/isaac_worker.log",
            "isaac_start_timeout": 300.0,
            "isaac_headless": True,
            "isaac_camera": "/World/Camera",
            "isaac_rpc_timeout_s": 600.0,
            "isaac_joint_control_ui": True,
        },
    )

    assert len(started_configs) == 1
    assert started_configs[0].conda_env == "matterix"
    assert started_configs[0].port == 8092
    assert started_configs[0].joint_control_ui is True
    assert build_calls == [
        {
            "name": "isaac",
            "endpoint": "http://127.0.0.1:8092",
            "scene": "/home/ubuntu/labsim/LabUtopia_repro/assets/chemistry_lab/lab_001/lab_001.usd",
            "timeout": 300.0,
        }
    ]
    assert services.context.physics is dummy_physics
    assert services.context.physics_endpoint == "http://127.0.0.1:8092"


def test_managed_isaac_requires_isaac_physics():
    try:
        backend_mod._initialize_runtime_for_backend(
            backend="ros",
            kwargs={"mode": "sim", "physics": "fake", "isaac_managed": True},
        )
    except ValueError as exc:
        assert "--isaac_managed requires --physics isaac" in str(exc)
    else:
        raise AssertionError("managed Isaac must reject non-Isaac physics")


def test_managed_isaac_signal_handler_stops_worker_and_chains_previous(monkeypatch):
    events = []
    registered = {}

    class FakeWorker:
        def stop(self):
            events.append("stop")

    def previous_handler(signum, frame):
        events.append(("previous", signum, frame))

    monkeypatch.setattr(backend_mod.atexit, "register", lambda callback: events.append(("atexit", callback)))
    monkeypatch.setattr(backend_mod.signal, "getsignal", lambda _signum: previous_handler)
    monkeypatch.setattr(
        backend_mod.signal,
        "signal",
        lambda signum, handler: registered.setdefault(signum, handler),
    )
    monkeypatch.setattr(backend_mod, "_managed_isaac_shutdown_registered", False)
    monkeypatch.setattr(backend_mod, "_managed_isaac_worker", FakeWorker())

    backend_mod._register_managed_isaac_shutdown()
    registered[backend_mod.signal.SIGTERM](backend_mod.signal.SIGTERM, None)

    assert events[0][0] == "atexit"
    assert "stop" in events
    assert ("previous", backend_mod.signal.SIGTERM, None) in events
