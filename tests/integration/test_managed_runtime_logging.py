"""Supervisor 的 Worker/PLC 日志轮转与快速重启隔离验证。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from unilabos.managed_runtime.supervisor import ManagedRuntimeSupervisor
from unilabos.utils.log_storage import read_log_tail


@pytest.mark.skipif(os.name == "nt", reason="POSIX 测试可执行文件使用 shebang")
@pytest.mark.parametrize("component", ["worker", "simulator"])
def test_supervisor_rotates_real_output_and_preserves_distinct_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str,
) -> None:
    monkeypatch.setenv("UNILABOS_BASICCONFIG_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("UNILABOS_BASICCONFIG_LOG_BACKUP_COUNT", "3")
    prefix = tmp_path / "runtime"
    executable = prefix / "bin" / "unilab"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,time\n"
        "while True:\n"
        " os.write(1,b'STDOUT:' + b'x'*1000 + b'\\n')\n"
        " os.write(2,b'STDERR:device-error\\n');time.sleep(.01)\n"
    )
    executable.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    legacy = state / ("edge.log" if component == "worker" else "simulator.log")
    legacy.write_bytes(b"legacy-open-file")
    graph = tmp_path / "graph.json"
    graph.write_text("{}")
    config = tmp_path / "config.py"
    config.write_text("")
    supervisor = ManagedRuntimeSupervisor(prefix, state, "test-token")

    if component == "worker":
        start = lambda: supervisor.start_worker({
            "workspace_path": str(tmp_path), "graph_path": str(graph),
            "config_path": str(config), "working_dir": str(tmp_path), "backend": "simple",
        })
        stop = supervisor.stop_worker
        current_log = lambda: supervisor.status()["logPath"]
    else:
        start = lambda: supervisor.start_simulator({"executable_path": str(executable)})
        stop = supervisor.stop_simulator
        current_log = lambda: supervisor.status()["simulator"]["logPath"]

    first_path = None
    try:
        for _ in range(2):
            start()
            path = Path(current_log())
            assert path != legacy
            assert path != first_path
            assert path.parent == (tmp_path / "logs" if component == "worker" else state)
            deadline = time.monotonic() + 5.0
            while not path.with_name(path.name + ".3").exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
            stop()
            assert b"STDERR:device-error" in read_log_tail(path, 4096)
            segments = list(path.parent.glob(path.name + "*"))
            assert len(segments) <= 4
            assert all(segment.stat().st_size <= 1024 for segment in segments)
            if first_path is None:
                first_path = path
        assert legacy.read_bytes() == b"legacy-open-file"
    finally:
        stop()
