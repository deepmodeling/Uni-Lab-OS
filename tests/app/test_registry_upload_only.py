"""调度权威不扫描注册表：目录只来自 Host 上报；默认拓扑（权威 + Host 子进程）靠规则 B 自动接受。

前两个用例在子进程里跑 ``unilab`` 的启动入口，把 ``build_registry`` 换成「一调用就失败」、把
``run_backend_process`` 换成只打印标记，验证权威路径根本不碰注册表扫描。最后一个用例起真实的默认
拓扑，经管理端口确认 Host 子进程的上报已全部生效（单机，无需人工确认）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_process_lifetime import EXAMPLE_CONFIG, _env, _free_port, _Unilab

_LAUNCHER = textwrap.dedent(
    """
    import sys

    from unilabos.registry import registry as registry_module


    def no_scan(*args, **kwargs):
        print("REGISTRY-SCANNED", flush=True)
        raise SystemExit(9)


    registry_module.build_registry = no_scan

    from unilabos.app import backend_main


    def fake_authority(args_dict, working_dir, *, launch_host=False):
        print(f"AUTHORITY launch_host={launch_host}", flush=True)


    backend_main.run_backend_process = fake_authority

    from unilabos.app import main

    sys.argv = ["unilab", *sys.argv[1:]]
    main.main()
    """
)


def _launch(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "launch.py"
    script.write_text(_LAUNCHER, encoding="utf-8")
    command = [
        sys.executable, str(script), "--backend", "hostlink", "--skip_env_check", "--config", str(EXAMPLE_CONFIG),
        "--disable_browser", "--working_dir", str(tmp_path / "work"),
        "--server_database_root", str(tmp_path / "db"), "--port", str(_free_port()),
        "--hostlink_port", str(_free_port()), *extra,
    ]
    return subprocess.run(command, env=_env(), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


@pytest.mark.parametrize(("extra", "launch_host"), [((), True), (("--role", "backend"), False)])
def test_authority_starts_without_scanning_the_registry(tmp_path, extra, launch_host) -> None:
    result = _launch(tmp_path, *extra)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert f"AUTHORITY launch_host={launch_host}" in output
    assert "REGISTRY-SCANNED" not in output


def test_single_process_host_still_scans(tmp_path) -> None:
    """``--no-safe-restart``：权威与 Host 同进程，Host 照常扫描（再进程内上报）。"""

    result = _launch(tmp_path, "--no_safe_restart")
    output = result.stdout + result.stderr
    assert "REGISTRY-SCANNED" in output, output[-4000:]
    assert "AUTHORITY" not in output


def _api(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1{path}", timeout=10) as response:
        body = json.load(response)
    assert body.get("code") == 0, body
    return body.get("data")


@pytest.mark.slow
def test_default_topology_catalog_comes_from_the_host_child_without_manual_acceptance(tmp_path) -> None:
    authority = _Unilab(tmp_path, "authority", [], root=tmp_path / "db")
    try:
        authority.host_child_pid()
        status = authority.wait_for(
            lambda: (data := _api(authority.port, "/registry/status"))["reported"] and data,
            "Host 子进程上报注册表",
        )
        assert status["pending_count"] == 0, status
        assert status["acceptance"]["single_machine"] is True and status["remote_slaves"] == []
        assert status["last_report"]["acceptance"]["single_machine"] is True
        entries = {entry["name"]: entry for entry in _api(authority.port, "/registry/entries")["entries"]}
        assert entries["host_node"]["status"] == ["active"]
        assert entries["host_node"]["package"] == "unilabos"
        assert not [name for name, entry in entries.items() if "pending" in entry["status"]]
        # 结构模板与 host_node 的动作模板都在
        templates = _api(authority.port, "/workflow-node-templates?page_size=100&node_type=condition")["items"]
        assert [item["name"] for item in templates] == ["condition"]
    finally:
        authority.close()
