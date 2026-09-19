from unilabos.app.main import _can_start_without_cloud_auth, build_argparser


def test_cli_defaults_are_backward_compatible():
    args = build_argparser().parse_args([])
    assert args.mode == "real"
    assert args.sim_rate == 1.0
    assert args.sim_paused is False


def test_cli_accepts_sim_options():
    args = build_argparser().parse_args(["--mode", "sim", "--sim_rate", "20", "--sim_paused"])
    assert args.mode == "sim"
    assert args.sim_rate == 20.0
    assert args.sim_paused is True


def test_cli_can_disable_sim_ros_services():
    args = build_argparser().parse_args(["--mode", "sim", "--disable_sim_services"])
    assert args.disable_sim_services is True


def test_cli_physics_defaults_are_backward_compatible():
    args = build_argparser().parse_args([])

    assert args.physics == "none"
    assert args.physics_endpoint is None
    assert args.physics_scene is None
    assert args.physics_timeout == 120.0
    assert args.isaac_managed is False
    assert args.isaac_host == "127.0.0.1"
    assert args.isaac_port == 8091
    assert args.isaac_conda_env is None
    assert args.isaac_conda_executable == "conda"
    assert args.isaac_python == "python"
    assert args.isaac_log_path is None
    assert args.isaac_start_timeout == 120.0
    assert args.isaac_headless is True
    assert args.isaac_camera == "/World/Camera"
    assert args.isaac_rpc_timeout_s == 600.0
    assert args.isaac_joint_control_ui is False


def test_cli_accepts_isaac_physics_options():
    args = build_argparser().parse_args(
        [
            "--mode",
            "sim",
            "--physics",
            "isaac",
            "--physics_endpoint",
            "http://127.0.0.1:8091",
            "--physics_scene",
            "/tmp/lab.usd",
            "--physics_timeout",
            "180",
        ]
    )

    assert args.physics == "isaac"
    assert args.physics_endpoint == "http://127.0.0.1:8091"
    assert args.physics_scene == "/tmp/lab.usd"
    assert args.physics_timeout == 180.0


def test_cli_accepts_managed_isaac_worker_options():
    args = build_argparser().parse_args(
        [
            "--mode",
            "sim",
            "--physics",
            "isaac",
            "--isaac_managed",
            "--isaac_host",
            "0.0.0.0",
            "--isaac_port",
            "8092",
            "--isaac_conda_env",
            "matterix",
            "--isaac_conda_executable",
            "/home/ubuntu/miniforge3/bin/conda",
            "--isaac_python",
            "python",
            "--isaac_log_path",
            "/tmp/isaac_worker.log",
            "--isaac_start_timeout",
            "300",
            "--no_isaac_headless",
            "--isaac_camera",
            "/World/DemoCamera",
            "--isaac_rpc_timeout_s",
            "900",
            "--isaac_joint_control_ui",
        ]
    )

    assert args.isaac_managed is True
    assert args.isaac_host == "0.0.0.0"
    assert args.isaac_port == 8092
    assert args.isaac_conda_env == "matterix"
    assert args.isaac_conda_executable == "/home/ubuntu/miniforge3/bin/conda"
    assert args.isaac_python == "python"
    assert args.isaac_log_path == "/tmp/isaac_worker.log"
    assert args.isaac_start_timeout == 300.0
    assert args.isaac_headless is False
    assert args.isaac_camera == "/World/DemoCamera"
    assert args.isaac_rpc_timeout_s == 900.0
    assert args.isaac_joint_control_ui is True


def test_local_graph_fastapi_can_start_without_cloud_auth():
    args = {"app_bridges": ["fastapi"], "use_remote_resource": False}

    assert _can_start_without_cloud_auth(args, "/tmp/mock_all.json") is True


def test_websocket_or_remote_resource_still_requires_cloud_auth():
    assert _can_start_without_cloud_auth({"app_bridges": ["websocket"], "use_remote_resource": False}, "/tmp/g.json") is False
    assert _can_start_without_cloud_auth({"app_bridges": ["fastapi"], "use_remote_resource": True}, "/tmp/g.json") is False
    assert _can_start_without_cloud_auth({"app_bridges": ["fastapi"], "use_remote_resource": False}, None) is False
