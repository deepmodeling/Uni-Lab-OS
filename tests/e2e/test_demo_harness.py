"""夹具自身的失败路径：不启动设备/不依赖网络，防止测试因静默降级而假绿。"""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tests.e2e import readme_demos as harness
from tests.e2e import test_readme_demos as runner
from tests.e2e import demo_contracts
from tests.e2e.demo_contracts import DemoContracts, demo_smoke


@pytest.mark.parametrize(("text", "expected"), [
    ("Start Port : 1024\nNumber of Ports : 13977", (1024, 15001)),
    ("开始端口：49152\n端口数：16384", (49152, 65536)),
])
def test_parse_windows_dynamic_port_range(text, expected):
    assert harness._parse_windows_tcp_range(text) == expected


@pytest.mark.parametrize("text", ["unknown", "Start: 65000\nCount: 1000", "Start: 1\nCount: 0"])
def test_invalid_windows_dynamic_port_range_is_not_silently_ignored(text):
    with pytest.raises(RuntimeError, match="动态端口"):
        harness._parse_windows_tcp_range(text)


def test_windows_test_ports_avoid_dynamic_range_and_probe_exclusive_binding(monkeypatch):
    monkeypatch.setattr(harness, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(harness, "_windows_tcp_range", lambda: (1024, 15001))

    def sample(candidates, count):
        assert count == 128
        assert all(port >= 15001 for port in candidates)
        assert 20002 not in candidates
        return [20000, 20001]

    monkeypatch.setattr(harness, "random", SimpleNamespace(
        SystemRandom=lambda: SimpleNamespace(sample=sample)))
    unavailable, available = MagicMock(), MagicMock()
    for sock in (unavailable, available):
        sock.__enter__.return_value = sock
    unavailable.bind.side_effect = PermissionError("reserved port")
    available.getsockname.return_value = ("0.0.0.0", 20001)
    monkeypatch.setattr(harness, "socket", SimpleNamespace(
        socket=Mock(side_effect=[unavailable, available]),
        AF_INET=2, SOCK_STREAM=1, SOL_SOCKET=65535, SO_EXCLUSIVEADDRUSE=-5,
    ))
    assert harness.free_port(exclude=(20002,)) == 20001
    available.setsockopt.assert_called_once_with(65535, -5, 1)
    available.bind.assert_called_once_with(("0.0.0.0", 20001))
    available.listen.assert_called_once_with(1)


def test_no_ports_outside_dynamic_range_fails_without_retry_loop(monkeypatch):
    monkeypatch.setattr(harness, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(harness, "_windows_tcp_range", lambda: (1024, 65536))
    with pytest.raises(RuntimeError, match="测试端口"):
        harness.free_port()


def test_sources_default_to_pinned_not_neighbor_checkout(monkeypatch, tmp_path):
    spec = harness.DEMOS[0]
    monkeypatch.delenv("UNILABOS_README_EXAMPLES_ROOT", raising=False)
    sibling = tmp_path / spec.repo / spec.package
    sibling.mkdir(parents=True)
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path / "core")
    clone = Mock(return_value=tmp_path / "pinned")
    monkeypatch.setattr(harness, "_clone_pinned", clone)
    root, source = harness.resolve_demo_source(spec, tmp_path / "cache")
    assert root == tmp_path / "pinned" and "pinned" in source
    clone.assert_called_once()


def test_invalid_explicit_source_must_not_fall_back(monkeypatch, tmp_path):
    monkeypatch.setenv("UNILABOS_README_EXAMPLES_ROOT", str(tmp_path))
    clone = Mock()
    monkeypatch.setattr(harness, "_clone_pinned", clone)
    with pytest.raises(RuntimeError, match="源码不完整"):
        harness.resolve_demo_source(harness.DEMOS[0])
    clone.assert_not_called()


def test_clone_failure_is_an_error_not_a_skip(monkeypatch, tmp_path):
    monkeypatch.delenv("UNILABOS_README_EXAMPLES_ROOT", raising=False)
    monkeypatch.setattr(harness, "_clone_pinned", Mock(side_effect=subprocess.CalledProcessError(1, "git fetch")))
    with pytest.raises(RuntimeError, match="无法取得"):
        harness.resolve_demo_source(harness.DEMOS[0], tmp_path)


def test_failed_fetch_does_not_poison_final_cache(monkeypatch, tmp_path):
    spec = harness.DEMOS[0]
    monkeypatch.setattr(harness, "_git", Mock(side_effect=subprocess.CalledProcessError(1, "git")))
    with pytest.raises(subprocess.CalledProcessError):
        harness._clone_pinned(spec, tmp_path)
    assert not (tmp_path / f"{spec.repo}-{spec.ref[:12]}").exists()


def test_dirty_cache_is_preserved_and_not_executed(monkeypatch, tmp_path):
    spec = harness.DEMOS[0]
    target = tmp_path / f"{spec.repo}-{spec.ref[:12]}"
    (target / ".git").mkdir(parents=True)
    changed = target / "driver.py"
    changed.write_text("user changes", encoding="utf-8")

    def git(*args, cwd):
        if args == ("rev-parse", "HEAD"):
            return spec.ref
        if args == ("remote", "get-url", "origin"):
            return spec.url
        if args == ("status", "--porcelain"):
            return " M driver.py"
        return ""

    monkeypatch.setattr(harness, "_git", git)
    result = harness._clone_pinned(spec, tmp_path)
    assert result != target and result.parent == tmp_path
    assert changed.read_text(encoding="utf-8") == "user changes"


def test_windows_cleanup_targets_only_test_process_tree(monkeypatch):
    process = Mock(pid=12345)
    process.poll.return_value = None
    run = Mock()
    monkeypatch.setattr(harness, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(harness.subprocess, "run", run)
    harness.stop_process(process)
    assert run.call_args.args[0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    process.wait.assert_called_once_with(timeout=10)


def test_user_environment_does_not_change_test_scenarios(monkeypatch):
    monkeypatch.setenv("UNILABOS_BASICCONFIG_PORT", "8003")
    monkeypatch.setenv("MATERIALS_DEMO_SKIP_AUTO_PREPARE", "1")
    monkeypatch.setenv("UNILABOS_E2E_BACKEND", "ros2")
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    environment = harness.subprocess_env({"LAN_DEMO_TERMINATE_AFTER": "3"})
    # 用户环境里的 UNILABOS_* 一律不透传；只有 harness 自己给运行时的控制面 token
    assert {key for key in environment if key.startswith("UNILABOS_")} == {"UNILABOS_BASICCONFIG_CONTROL_TOKEN"}
    assert environment["UNILABOS_BASICCONFIG_CONTROL_TOKEN"] == harness.E2E_CONTROL_TOKEN
    assert "MATERIALS_DEMO_SKIP_AUTO_PREPARE" not in environment
    assert environment["LAN_DEMO_TERMINATE_AFTER"] == "3"
    assert environment["ROS_DISTRO"] == "jazzy"


@pytest.mark.parametrize("error", [RuntimeError("bad API response"), KeyError("missing"), ValueError("bad JSON")])
def test_wait_does_not_hide_contract_failures(error):
    with pytest.raises(type(error)):
        harness.wait_until(Mock(side_effect=error), timeout=10)


def test_batch_instantiates_every_graph_before_starting_any_task(monkeypatch):
    batch = list(harness.DEMOS_BY_REPO["LabDeviceLockDemo"].workflows[1:5])
    events = []

    def instantiate(port, expectation, **kwargs):
        events.append("instantiate")
        return {"uuid": expectation.name}

    def request(port, path, payload):
        events.append("submit")
        return {"uuid": payload["workflow_uuid"]}

    monkeypatch.setattr(runner, "_instantiate_workflow", instantiate)
    monkeypatch.setattr(runner, "api_request", request)
    monkeypatch.setattr(runner, "_assert_task_queued", Mock(return_value={"blockers": ["holder"]}))
    monkeypatch.setattr(runner, "_await_workflow", Mock(return_value={"task_status": "succeeded"}))
    results = runner._run_workflow_batch(1, batch, timeout=10, abort=None)
    assert events == ["instantiate"] * 4 + ["submit"] * 4
    assert results[1]["queued"] == {"blockers": ["holder"]}


def test_smoke_import_is_version_scoped_and_restores_modules(tmp_path, monkeypatch):
    spec = SimpleNamespace(package="versioned_demo")
    package = tmp_path / spec.package
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "smoke.py").write_text("VERSION = 'pinned'", encoding="utf-8")
    old = SimpleNamespace(VERSION="stale")
    monkeypatch.setitem(sys.modules, "versioned_demo.smoke", old)
    with demo_smoke(spec, tmp_path) as smoke:
        assert smoke.VERSION == "pinned"
    assert sys.modules["versioned_demo.smoke"] is old
    assert "versioned_demo" not in sys.modules


@pytest.mark.parametrize("wrong", [None, "site", "occupant"])
def test_site_projection_checks_uuid_and_occupant_without_rewriting_evidence(monkeypatch, wrong):
    expectation = harness.WorkflowExpectation(name="materials", node_count=6)
    spec = SimpleNamespace(package="materials_demo", workflows=[None, expectation])
    smoke = SimpleNamespace(
        DECK_UUID="deck",
        assert_site_loop_workflow=Mock(),
        assert_material_loop_workflow=Mock(),
        assert_site_tour_workflow=Mock(),
        assert_material_flow_workflow=Mock(),
    )
    site = {"label": "T3", "site_uuid": "site-three", "occupied_material_uuid": "plate"}
    monkeypatch.setattr(demo_contracts, "api_request", Mock(return_value={"sites": [site]}))
    value = {
        "to_site": "other-site" if wrong == "site" else "site-three",
        "plate_uuid": "other-plate" if wrong == "occupant" else "plate",
    }
    proof = {"workflow_name": expectation.name, "jobs": [{}, {}, {}, {"return_info": {"return_value": value}}]}
    contracts = DemoContracts(spec, smoke, 1, "hostlink")
    if wrong:
        with pytest.raises(AssertionError):
            contracts.assert_batch([expectation], [proof])
        smoke.assert_material_loop_workflow.assert_not_called()
    else:
        contracts.assert_batch([expectation], [proof])
        projected = smoke.assert_material_loop_workflow.call_args.args[0]
        assert projected["jobs"][3]["return_info"]["return_value"]["to_site"] == "site-three"
        assert projected["site_bindings"] == {"T3": "site-three"}
        assert "site_bindings" not in proof
        assert value["to_site"] == "site-three"
