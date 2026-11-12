"""实时日志：有界游标、并发读者、文件切换、HostLink 下行与 HTTP 路由。"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.backend.hostlink.protocol import ActionType, RemoteError
from unilabos.backend.hostlink.server import HostLinkServer
from unilabos.server.api.runtime import logs as api_logs
from unilabos.server.services.runtime.logs import LogSourceNotFound, LogSourceUnavailable, RuntimeLogService
from unilabos.utils import log as log_config
from unilabos.utils import runtime_logs as reader


class Processes:
    def __init__(self, workdir: Path, items: list[dict] | None = None):
        self.working_dir = workdir
        self.items = items or []

    def list(self):
        return self.items


def test_tail_incremental_two_readers_and_utf8(tmp_path: Path):
    path = tmp_path / "host.log"
    path.write_text("开始\n第二行\n第三行\n", encoding="utf-8")
    first = reader.read_log_file(path, source_id="host", limit=2)
    assert [line.text for line in first.lines] == ["第二行", "第三行"]
    assert first.truncated and not first.has_more
    appended = "[ERROR] 中文异常\nTraceback:\n  错误位置\n".encode()
    with path.open("ab") as handle:
        handle.write(appended[:-2])
    batch = reader.read_log_file(path, source_id="host", cursor=first.cursor, limit=1)
    assert [line.text for line in batch.lines] == ["[ERROR] 中文异常"]
    assert batch.has_more
    with ThreadPoolExecutor(max_workers=2) as executor:
        reads = list(executor.map(lambda _: reader.read_log_file(path, source_id="host", cursor=batch.cursor), range(2)))
    assert reads[0] == reads[1]
    assert [line.text for line in reads[0].lines] == ["Traceback:"]
    with path.open("ab") as handle:
        handle.write(appended[-2:])
    last = reader.read_log_file(path, source_id="host", cursor=reads[0].cursor)
    assert [line.text for line in last.lines] == ["  错误位置"]
    assert reader.read_log_file(path, source_id="host", cursor=last.cursor).lines == []


@pytest.mark.parametrize("change", ["replace", "truncate", "source"])
def test_reset_on_file_or_source_change(tmp_path: Path, change: str):
    path = tmp_path / "host.log"
    path.write_text("old line\n" * 20)
    old = reader.read_log_file(path, source_id="host")
    source = "host"
    if change == "replace":
        path.rename(tmp_path / "old.log")
    if change == "source":
        source = "slave:a"
    path.write_text("新进程\n", encoding="utf-8")
    new = reader.read_log_file(path, source_id=source, cursor=old.cursor)
    assert new.reset and [line.text for line in new.lines] == ["新进程"]


def test_bounded_long_line_and_partial_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader, "MAX_READ_BYTES", 64)
    monkeypatch.setattr(reader, "MAX_LINE_CHARS", 16)
    path = tmp_path / "log"
    path.write_bytes(b"x" * 200)
    batch = reader.read_log_file(path, source_id="host")
    assert batch.truncated and len(batch.lines[0].text) < 40
    with path.open("ab") as handle:
        handle.write(b"\nprogress\rcomplete\r\n")
    last = reader.read_log_file(path, source_id="host", cursor=batch.cursor)
    assert last.lines[-1].text == "progress\rcomplet … [该行已截断]"
    assert not last.has_more


def test_tail_bound_and_ansi_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader, "MAX_READ_BYTES", 100)
    path = tmp_path / "log"
    path.write_bytes(b"old\n" * 200 + b"\x1b[31m[ERROR]\x1b[0m fail\n")
    batch = reader.read_log_file(path, source_id="host", limit=2)
    assert batch.truncated and len(batch.lines) <= 2
    assert batch.lines[-1].text == "[ERROR] fail"
    assert batch.lines[0].offset > 0


@pytest.fixture()
def local_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "host.log"
    path.write_text("[INFO] host ready\n", encoding="utf-8")
    monkeypatch.setattr(log_config, "_log_file_path", str(path))
    process = {"id": "test-slave", "name": "测试 Slave", "pid": 123, "device_ids": ["pump"], "status": "running"}
    process_path = tmp_path / "device_processes" / "test-slave" / "process.log"
    process_path.parent.mkdir(parents=True)
    process_path.write_text("[INFO] slave ready\n", encoding="utf-8")
    return RuntimeLogService(Processes(tmp_path, [process]), None, machine_name="host-machine")


def test_local_sources_and_persisted_managed_logs(local_service):
    sources = local_service.sources().sources
    assert [item.source_id for item in sources] == ["host", "managed:test-slave"]
    assert sources[0].pid == os.getpid()
    assert sources[1].managed and sources[1].device_ids == ["pump"]
    assert local_service.read("host").lines[0].text == "[INFO] host ready"
    # 即便 Host 的内存 tail 缓存为空，旧的受管日志仍从已登记路径读取。
    local_service.processes.items[0]["status"] = "stopped"
    local_service.processes.items[0]["pid"] = None
    batch = local_service.read("managed:test-slave")
    assert batch.lines[0].text == "[INFO] slave ready" and batch.pid is None


@pytest.mark.parametrize("override", [
    ["--machine_name", "custom"], ["--machine-name", "custom"],
    ["--machine_name=custom"], ["--machine-name=custom"],
])
def test_managed_identity_honors_last_cli_override(local_service, override: list[str]):
    local_service.processes.items[0]["command"] = ["python", "--machine_name", "generated", *override]
    assert local_service.sources().sources[1].node_id == "custom"


def test_path_cannot_be_supplied_or_escape_process_root(local_service):
    with pytest.raises(LogSourceNotFound):
        local_service.read("../../private.txt")
    local_service.processes.items.append({"id": "../../escape", "name": "bad"})
    with pytest.raises(LogSourceNotFound):
        local_service.read("managed:../../escape")


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "cursor=garbage", "cursor=" + "a" * 200])
def test_http_validation(local_service, monkeypatch: pytest.MonkeyPatch, query: str):
    monkeypatch.setattr(api_logs, "get_runtime_log_service", lambda: local_service)
    app = FastAPI()
    app.include_router(api_logs.create_runtime_logs_router())
    with TestClient(app) as client:
        error_of(client.get(f"/api/v1/hostlink/logs?source_id=host&{query}"), 1000)


def test_http_shape_cache_and_errors(local_service, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_logs, "get_runtime_log_service", lambda: local_service)
    app = FastAPI()
    app.include_router(api_logs.create_runtime_logs_router())
    with TestClient(app) as client:
        sources = client.get("/api/v1/hostlink/log-sources")
        assert sources.status_code == 200 and sources.headers["cache-control"] == "no-store"
        result = client.get("/api/v1/hostlink/logs", params={"source_id": "host", "limit": 10})
        assert result.status_code == 200 and ok_data(result)["pid"] == os.getpid()
        assert ok_data(result)["lines"][0]["offset"] == 0
        error_of(client.get("/api/v1/hostlink/logs?source_id=missing"), 4001)
        monkeypatch.setattr(log_config, "_log_file_path", None)
        error_of(client.get("/api/v1/hostlink/logs?source_id=host"), 5001)


def test_hostlink_logs_roundtrip_without_optional_capability_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "remote.log"
    path.write_text("[INFO] 远端驱动\n", encoding="utf-8")
    monkeypatch.setattr(log_config, "_log_file_path", str(path))
    server = HostLinkServer("127.0.0.1", 0).start()
    slave = HostLinkClient("127.0.0.1", server.port, machine_name="remote-a", device_ids=["sensor"])
    peer_without_flags = HostLinkClient("127.0.0.1", server.port, machine_name="standard-slave")
    peer_without_flags.capabilities.remove("process-logs")
    service = RuntimeLogService(Processes(tmp_path), server, machine_name="host")
    try:
        assert slave.connect_blocking(2) and peer_without_flags.connect_blocking(2)
        batch = service.read("slave:remote-a")
        assert batch.source_id == "slave:remote-a" and batch.lines[0].text == "[INFO] 远端驱动"
        assert batch.pid == os.getpid()
        assert service.read("slave:remote-a", cursor=batch.cursor).lines == []
        # 当前协议的日志方法是必备项，不再靠可选能力标志探测旧版本。
        assert service.read("slave:standard-slave").lines[0].text == "[INFO] 远端驱动"
        peer = next(peer for peer in server.peers() if peer["node_id"] == "remote-a")
        # RPC 入参不允许绕开读取上限，任意 path 参数也不能切换文件。
        with pytest.raises(RemoteError):
            server.request_peer(peer["addr"], ActionType.LOG_READ, {"limit": 1001})
        with pytest.raises(RemoteError):
            server.request_peer(peer["addr"], ActionType.LOG_READ, {"path": str(tmp_path / "secret")})
        service.processes.items.append({"id": "m", "name": "managed", "command": ["--machine_name", "remote-a"]})
        assert "slave:remote-a" not in [item.source_id for item in service.sources().sources]
    finally:
        slave.close()
        peer_without_flags.close()
        server.stop()
