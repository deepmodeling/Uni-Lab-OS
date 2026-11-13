from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.backend.reset import ResetController, ResetConflict, configure_reset
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.api.runtime.diagnostics import create_backend_router


def controller(tmp_path, preflight=lambda: None):
    root = tmp_path / "data"
    root.mkdir()
    return ResetController(ServerDatabasePaths.resolve(root), str(tmp_path), preflight)


def test_confirmation_and_idempotency(tmp_path):
    calls = []
    reset = controller(tmp_path, lambda: calls.append(1))
    with pytest.raises(ResetConflict):
        reset.request("wrong", "清空全部数据")
    assert not reset.pending
    reset.request(reset.token, "清空全部数据")
    reset.request(reset.token, "清空全部数据")
    assert calls == [1]


def test_preflight_failure_never_moves_data(tmp_path):
    def reject():
        raise ResetConflict("busy")
    reset = controller(tmp_path, reject)
    reset.paths.runtime_db.write_bytes(b"original")
    with pytest.raises(ResetConflict):
        reset.request(reset.token, "清空全部数据")
    reset.finish()
    assert reset.paths.runtime_db.read_bytes() == b"original"


def test_archives_only_explicit_targets_and_keeps_packages(tmp_path):
    reset = controller(tmp_path)
    reset.paths.runtime_db.write_bytes(b"authority")
    edge = reset.paths.root / "edge"
    edge.mkdir()
    (edge / "history.db").write_bytes(b"edge-history")
    (tmp_path / "device_processes.json").write_text("{}")
    packages = tmp_path / "driver_packages.json"
    packages.write_text("keep")
    reset.request(reset.token, "清空全部数据")
    assert reset.paths.runtime_db.exists()  # 请求只登记；必须停机后才执行。
    reset.finish()
    assert not reset.paths.runtime_db.exists()
    assert not (edge / "history.db").exists()
    assert packages.read_text() == "keep"
    assert (reset.backup / "manifest.json").exists()
    assert not (reset.paths.root / "reset-pending.json").exists()


def test_reject_custom_database_path(tmp_path):
    paths = ServerDatabasePaths.resolve(tmp_path / "data", {"runtime": tmp_path / "outside.db"})
    reset = ResetController(paths, str(tmp_path), lambda: None)
    with pytest.raises(ResetConflict):
        reset.preview()


def test_partial_move_failure_leaves_recovery_marker(tmp_path, monkeypatch):
    reset = controller(tmp_path)
    reset.paths.runtime_db.write_bytes(b"a")
    reset.request(reset.token, "清空全部数据")
    def fail(*args):
        raise OSError("locked")
    monkeypatch.setattr("unilabos.server.backend.reset.os.replace", fail)
    with pytest.raises(OSError):
        reset.finish()
    assert (reset.paths.root / "reset-pending.json").exists()
    assert reset.paths.runtime_db.exists()


def test_http_preview_confirm_and_shutdown(tmp_path, monkeypatch):
    reset = controller(tmp_path)
    stopped = []
    monkeypatch.setattr("unilabos.server.api.app.request_server_shutdown", lambda: stopped.append(True))
    configure_reset(reset)
    app = FastAPI()
    app.include_router(create_backend_router(lambda: None, lambda: None))
    try:
        with TestClient(app) as client:
            preview = ok_data(client.get("/api/v1/reset"))
            assert preview["supported"]
            # 确认令牌不对：状态冲突 3003（HTTP 200 信封），不停机
            error_of(client.post("/api/v1/reset", json={"confirmation_token": "x", "confirmation": "清空全部数据"}), 3003)
            assert not stopped
            reply = client.post("/api/v1/reset", json={"confirmation_token": preview["confirmation_token"], "confirmation": "清空全部数据"})
            assert ok_data(reply, status=202)["pending"]
            assert stopped == [True]
            assert not reset.backup.exists()
    finally:
        configure_reset(None)


def test_full_reset_real_split_runtime(tmp_path):
    """真实进程测试只能用 pytest 的独立目录，绝不连接既有实验室端口。"""
    import json
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    if os.environ.get("UNILABOS_TEST_FULL_RESET") != "1":
        pytest.skip("设置 UNILABOS_TEST_FULL_RESET=1 执行独立临时部署验证")
    def port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    management, hostlink = port(), port()
    root = tmp_path / "db"
    work = tmp_path / "work"
    package = tmp_path / "empty_devices"
    package.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("UNILABOS_")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    command = [sys.executable, "-m", "unilabos.app.main", "--backend", "hostlink", "--skip_env_check",
               "--config", str(Path(__file__).resolve().parents[2] / "unilabos/config/example_config.py"),
               "--disable_browser", "--external_devices_only", "--devices", str(package),
               "--port", str(management), "--hostlink_port", str(hostlink),
               "--working_dir", str(work), "--server_database_root", str(root)]
    if os.environ.get("OPENLAB_PROTOCOL_SMOKE"):
        # 通用 --write smoke 需要可实例化资源类；空设备图仍不会连接硬件。
        command.remove("--external_devices_only")
    log = tmp_path / "runtime.log"
    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        token_path = work / "control_token"
        if token_path.exists():
            headers["X-UniLab-Token"] = token_path.read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{management}/api/v1{path}", data=data,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(log.read_text(encoding="utf-8"))
                try:
                    if request("/health")["execution"] == "ready":
                        request("/driver-packages")
                        break
                except Exception:
                    pass
                time.sleep(0.3)
            else:
                pytest.fail(log.read_text(encoding="utf-8"))
            assert request("/scheduler/drain", {})["phase"] == "drained"
            assert request("/scheduler/drain/resume", {})["phase"] == "running"
            # reset 预检在已有显式 drain 时也必须取得自己的暂停来源。
            assert request("/scheduler/drain", {})["requested"]
            preview = request("/reset")
            assert preview["supported"]
            smoke = os.environ.get("OPENLAB_PROTOCOL_SMOKE")
            if smoke:
                result = subprocess.run(["node", smoke, f"http://127.0.0.1:{management}", "--write"],
                                        env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
                assert result.returncode == 0, result.stdout + result.stderr
            assert request("/reset", {"confirmation_token": preview["confirmation_token"], "confirmation": "清空全部数据"})["pending"]
            assert process.wait(timeout=60) == 0, log.read_text(encoding="utf-8")
            manifest = json.loads((Path(preview["backup_path"]) / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["state"] == "completed"
            assert any(Path(item["source"]).name == "runtime.db" for item in manifest["moved"])
            assert not (root / "runtime.db").exists()
            assert not (root / "edge" / "runtime.db").exists()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
