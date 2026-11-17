"""conda 包 ROS 2 冒烟测试的超时与本机发现约定；不需要 ROS。"""

from unilabos.backend.ros2 import conda_smoke


def test_environment_limits_discovery_to_localhost_without_overriding_explicit_values():
    env = conda_smoke.smoke_environment({"ROS_LOCALHOST_ONLY": "0", "PATH": "/bin"})
    assert env["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
    assert env["ROS_LOCALHOST_ONLY"] == "0"
    assert "<AllowMulticast>false</AllowMulticast>" in env["CYCLONEDDS_URI"]
    assert env["PATH"] == "/bin"


def test_environment_uses_only_the_variable_of_the_active_distro():
    jazzy = conda_smoke.smoke_environment({"ROS_DISTRO": "jazzy"})
    assert jazzy["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST" and "ROS_LOCALHOST_ONLY" not in jazzy
    humble = conda_smoke.smoke_environment({"ROS_DISTRO": "humble"})
    assert humble["ROS_LOCALHOST_ONLY"] == "1" and "ROS_AUTOMATIC_DISCOVERY_RANGE" not in humble


def test_hanging_child_fails_with_timeout_instead_of_blocking():
    assert conda_smoke.main("import time; time.sleep(30)", timeout=1) == conda_smoke.TIMEOUT_EXIT_CODE


def test_restart_exit_code_runs_a_second_time(tmp_path):
    marker = tmp_path / "first-run"
    code = (
        "import pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n"
        "    marker.touch()\n"
        "    sys.exit(75)\n"
    )
    assert conda_smoke.main(code, timeout=30) == 0


def test_failure_is_reported_without_retry():
    assert conda_smoke.main("import sys; sys.exit(3)", timeout=30) == 3
