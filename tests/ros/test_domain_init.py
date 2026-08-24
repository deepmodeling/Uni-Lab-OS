import pytest

from unilabos.ros import main_slave_run


class _StopRuntime(RuntimeError):
    pass


def test_rclpy_receives_explicit_domain_id(monkeypatch) -> None:
    calls = []
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    monkeypatch.setattr(main_slave_run.rclpy, "ok", lambda: False)
    monkeypatch.setattr(
        main_slave_run.rclpy,
        "init",
        lambda **kwargs: calls.append(kwargs),
    )

    main_slave_run._init_rclpy(["--log-level", "info"], 47)

    assert calls == [{"args": ["--log-level", "info"], "domain_id": 47}]
    assert main_slave_run.os.environ["ROS_DOMAIN_ID"] == "47"


def test_old_rclpy_falls_back_to_environment_path(monkeypatch) -> None:
    calls = []
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    monkeypatch.setattr(main_slave_run.rclpy, "ok", lambda: False)

    def fake_init(**kwargs):
        calls.append(kwargs)
        if "domain_id" in kwargs:
            raise TypeError("unsupported domain_id")

    monkeypatch.setattr(main_slave_run.rclpy, "init", fake_init)

    main_slave_run._init_rclpy([], 9)

    assert calls == [
        {"args": [], "domain_id": 9},
        {"args": []},
    ]


def test_ros_host_starts_microbackend_network_before_rclpy(monkeypatch) -> None:
    from unilabos.server.scheduler import host_network

    events = []

    def setup_network(material_gateway=None):
        events.append(("hostlink", material_gateway))
        return object()

    class HostNode:
        def __init__(self, *_args, **_kwargs):
            self.resources_config = "live-resource-tree"
            self.resource_tracker = object()
            events.append(("host-node", None))

    class Executor:
        def __init__(self, **_kwargs):
            pass

        def spin(self):
            pass

        def add_node(self, _node):
            pass

    class Thread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            events.append(("executor", None))

    monkeypatch.setattr(
        host_network,
        "setup_host_network_service",
        setup_network,
    )
    monkeypatch.setattr(
        main_slave_run,
        "_init_rclpy",
        lambda _args, _domain: events.append(("rclpy", None)),
    )
    monkeypatch.setattr(main_slave_run, "HostNode", HostNode)
    monkeypatch.setattr(main_slave_run, "MultiThreadedExecutor", Executor)
    monkeypatch.setattr(main_slave_run.threading, "Thread", Thread)
    monkeypatch.setattr(
        main_slave_run.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(_StopRuntime()),
    )

    with pytest.raises(_StopRuntime):
        main_slave_run.main(object(), object())

    assert events[:2] == [("hostlink", None), ("rclpy", None)]
    assert events.count(("hostlink", None)) == 1


def test_ros_slave_gets_hostlink_policy_before_rclpy(monkeypatch) -> None:
    from unilabos.server.scheduler import host_network

    events = []
    device_config = object()

    def setup_slave(*, device_ids, wait_for_host=None):
        events.append(("hostlink", device_ids, wait_for_host))
        return object(), 57

    monkeypatch.setattr(
        host_network,
        "require_slave_startup_device_ids",
        lambda value: ["pump-1"] if value is device_config else [],
    )
    monkeypatch.setattr(
        host_network,
        "setup_slave_network_client",
        setup_slave,
    )

    def stop_after_rclpy(_args, domain_id):
        events.append(("rclpy", domain_id))
        raise _StopRuntime()

    monkeypatch.setattr(main_slave_run, "_init_rclpy", stop_after_rclpy)

    with pytest.raises(_StopRuntime):
        main_slave_run.slave(device_config, object())

    assert events == [
        ("hostlink", ["pump-1"], None),
        ("rclpy", 57),
    ]
