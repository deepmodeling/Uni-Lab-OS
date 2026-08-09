from unilabos.ros import main_slave_run


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
