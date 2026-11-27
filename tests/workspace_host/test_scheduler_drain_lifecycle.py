"""Workspace Host 与调度器排空接口的公开生命周期合同测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspacePaths
from unilabos.workspace_host.scheduler_lifecycle import (
    SchedulerLifecycleClient,
    SchedulerLifecycleError,
)


class _DrainHandler(BaseHTTPRequestHandler):
    """模拟调度器公开排空接口，只维护测试所需的外部状态。"""

    phase = "running"
    requests: ClassVar[list[tuple[str, str]]] = []

    def do_POST(self) -> None:
        """处理开始或恢复排空请求，并返回公开状态对象。"""

        type(self).requests.append(("POST", self.path))
        if self.path == "/api/v1/scheduler/drain":
            type(self).phase = "drained"
        elif self.path == "/api/v1/scheduler/drain/resume":
            type(self).phase = "running"
        self._send_state()

    def do_GET(self) -> None:
        """返回当前排空阶段，供 Host 轮询安全停止条件。"""

        type(self).requests.append(("GET", self.path))
        self._send_state()

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭测试 HTTP 服务的标准错误日志。"""

    def _send_state(self) -> None:
        """把当前阶段编码为调度器排空接口的 JSON 形状。"""

        payload = json.dumps(
            {
                "phase": type(self).phase,
                "accepting_new_dispatches": type(self).phase == "running",
                "active_device_job_count": 0,
                "active_device_job_ids": [],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _NeverDrainedHandler(_DrainHandler):
    """模拟始终存在设备在途作业的调度器。"""

    phase = "draining"

    def do_POST(self) -> None:
        """接受排空命令，但持续报告仍有一个设备作业。"""

        type(self).requests.append(("POST", self.path))
        self._send_state()

    def _send_state(self) -> None:
        """返回不会收敛的排空状态，用于验证 Host 关闭式失败。"""

        payload = json.dumps(
            {
                "phase": "draining",
                "accepting_new_dispatches": False,
                "active_device_job_count": 1,
                "active_device_job_ids": ["job-running"],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _running_host(
    workspace: Path,
    scheduler_address: str,
    *,
    timeout: float,
    domain_mode: str = "local",
) -> tuple[WorkspaceHost, dict[str, subprocess.Popen[bytes]]]:
    """创建由两个真实子进程组成的 Host 测试现场。

    参数：``workspace`` 是隔离工作区，``scheduler_address`` 是公开调度接口，
    ``timeout`` 是排空总预算。返回：Host 与测试拥有的两个子进程。异常：子进程
    启动失败时由 ``subprocess.Popen`` 原样抛出，调用方负责最终清理。
    """

    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=timeout)
    processes = {
        name: subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for name in ("backend", "edge")
    }
    for name, process in processes.items():
        host._processes[name] = process
        component = host._components[name]
        component["phase"] = "ready"
        component["pid"] = process.pid
        component["generation"] = f"test-{name}"
    host._components["backend"]["address"] = scheduler_address
    host._components["backend"]["metadata"] = {"domainMode": domain_mode}
    return host, processes


def _wait_operation(host: WorkspaceHost, operation_id: str) -> dict[str, object]:
    """等待公开 Host 操作进入终态并返回持久操作记录。"""

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        operation = host.operation(operation_id)
        if operation["phase"] in {"succeeded", "failed"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"操作未在测试预算内结束：{operation_id}")


def _cleanup(
    host: WorkspaceHost,
    processes: dict[str, subprocess.Popen[bytes]],
    server: ThreadingHTTPServer,
) -> None:
    """回收测试拥有的 Host、子进程和外部 HTTP 服务。"""

    host.close()
    for process in processes.values():
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    server.shutdown()
    server.server_close()


def test_workspace_stop_drains_before_stopping_two_processes(tmp_path: Path) -> None:
    """统一停止先经公开接口排空，再终止 Edge 和 Backend 两个真实进程。

    参数：``tmp_path`` 提供隔离状态目录。返回：无；断言公开 Host 操作成功、
    排空请求已发生且两个 PID 均退出。异常：任一步骤顺序或收敛失败则测试失败。
    """

    _DrainHandler.phase = "running"
    _DrainHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DrainHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    host, processes = _running_host(tmp_path, address, timeout=1.0)
    try:
        submitted = host.submit(
            {
                "operationId": "workspace-stop-safe",
                "command": "workspace.stop",
                "parameters": {},
            }
        )
        operation = _wait_operation(host, str(submitted["operationId"]))

        assert operation["phase"] == "succeeded"
        assert _DrainHandler.requests[0] == (
            "POST",
            "/api/v1/scheduler/drain",
        )
        assert all(process.poll() is not None for process in processes.values())
    finally:
        _cleanup(host, processes, server)


def test_workspace_stop_keeps_processes_when_drain_times_out(tmp_path: Path) -> None:
    """设备作业未收敛时统一停止失败关闭，不得杀死任一进程。

    参数：``tmp_path`` 提供隔离状态目录。返回：无；断言操作失败且两个真实 PID
    仍存活。异常：Host 误杀设备或调度进程，或未返回稳定错误时测试失败。
    """

    _NeverDrainedHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverDrainedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    host, processes = _running_host(tmp_path, address, timeout=0.15)
    try:
        submitted = host.submit(
            {
                "operationId": "workspace-stop-blocked",
                "command": "workspace.stop",
                "parameters": {},
            }
        )
        operation = _wait_operation(host, str(submitted["operationId"]))

        assert operation["phase"] == "failed"
        assert operation["error"]["code"] == "workspace_drain_timeout"
        assert all(process.poll() is None for process in processes.values())
        assert all(os.kill(process.pid, 0) is None for process in processes.values())
    finally:
        _cleanup(host, processes, server)


@pytest.mark.parametrize("adopted", [False, True])
@pytest.mark.parametrize("cleanup_fails_once", [False, True])
def test_workspace_stop_cleans_up_after_backend_exit(tmp_path: Path, adopted: bool,
                                                   cleanup_fails_once: bool, monkeypatch) -> None:
    """已退出的自有或接管 Backend 不再排空；停止保留设备状态未知的告警。"""
    _DrainHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DrainHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, processes = _running_host(tmp_path, f"http://127.0.0.1:{server.server_port}", timeout=0.15)
    try:
        processes["backend"].kill()
        processes["backend"].wait(timeout=3)
        if adopted:
            host._processes.pop("backend")
        host.snapshot()
        assert host._components["backend"]["phase"] == "failed"
        if cleanup_fails_once:
            from unilabos.workspace_host.model import WorkspaceHostError
            original_stop = host._stop_component

            def fail_stop(name):
                raise WorkspaceHostError("stop_failed", "模拟一次进程清理失败")

            monkeypatch.setattr(host, "_stop_component", fail_stop)
            first = host.submit({"operationId": "stop-retryable", "command": "workspace.stop"})
            assert _wait_operation(host, str(first["operationId"]))["phase"] == "failed"
            monkeypatch.setattr(host, "_stop_component", original_stop)
        submitted = host.submit({"operationId": "stop-after-crash", "command": "workspace.stop"})
        operation = _wait_operation(host, str(submitted["operationId"]))
        assert operation["phase"] == "succeeded"
        assert not _DrainHandler.requests
        assert all(process.poll() is not None for process in processes.values())
        assert operation["result"]["warnings"][0]["code"] == "scheduler_exited_before_drain"
        assert "无法确认设备作业状态" in operation["result"]["warnings"][0]["message"]
        assert host.snapshot()["components"]["backend"]["phase"] == "idle"
        again = host.submit({"operationId": "stop-again", "command": "workspace.stop"})
        assert _wait_operation(host, str(again["operationId"]))["phase"] == "succeeded"
    finally:
        _cleanup(host, processes, server)


def test_failed_phase_with_live_backend_still_requires_drain(tmp_path: Path) -> None:
    """仅有 failed 状态不能证明进程退出，仍必须等待设备排空。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverDrainedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, processes = _running_host(tmp_path, f"http://127.0.0.1:{server.server_port}", timeout=0.15)
    try:
        host._components["backend"]["phase"] = "failed"
        submitted = host.submit({"operationId": "stop-live-failed", "command": "workspace.stop"})
        operation = _wait_operation(host, str(submitted["operationId"]))
        assert operation["phase"] == "failed"
        assert operation["error"]["code"] == "workspace_drain_timeout"
        assert all(process.poll() is None for process in processes.values())
    finally:
        _cleanup(host, processes, server)


def test_workspace_stop_drains_backend_even_when_edge_was_stopped(
    tmp_path: Path,
) -> None:
    """Edge 已单独停止时，统一停止仍要先排空 Backend 调度器。

    参数：``tmp_path`` 提供隔离状态目录。返回：无；断言公开 Host 操作仍访问
    排空接口并停止 Backend。异常：若只根据 Edge 当前状态跳过排空，测试失败。
    """

    _DrainHandler.phase = "running"
    _DrainHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DrainHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    host, processes = _running_host(tmp_path, address, timeout=1.0)
    try:
        edge_stop = host.submit(
            {
                "operationId": "edge-stop-before-workspace-stop",
                "command": "os.stop",
                "parameters": {},
            }
        )
        edge_operation = _wait_operation(host, str(edge_stop["operationId"]))
        assert edge_operation["phase"] == "succeeded"

        submitted = host.submit(
            {
                "operationId": "workspace-stop-backend-only",
                "command": "workspace.stop",
                "parameters": {},
            }
        )
        operation = _wait_operation(host, str(submitted["operationId"]))

        assert operation["phase"] == "succeeded"
        assert _DrainHandler.requests[0] == (
            "POST",
            "/api/v1/scheduler/drain",
        )
        assert processes["backend"].poll() is not None
    finally:
        _cleanup(host, processes, server)


def test_workspace_start_backend_upstream_resumes_local_scheduler(
    tmp_path: Path,
) -> None:
    """Backend 上游模式启动动作进程后仍恢复本地工站调度器。

    参数：``tmp_path`` 提供隔离状态目录。返回：无；断言统一启动操作成功且不
    调用本地恢复接口。异常：若绕过本地工站调度权威，测试失败。
    """

    _DrainHandler.phase = "running"
    _DrainHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DrainHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    host, processes = _running_host(
        tmp_path,
        address,
        timeout=1.0,
        domain_mode="backend",
    )
    try:
        submitted = host.submit(
            {
                "operationId": "workspace-start-remote-authority",
                "command": "workspace.start",
                "parameters": {},
            }
        )
        operation = _wait_operation(host, str(submitted["operationId"]))

        assert operation["phase"] == "succeeded"
        assert _DrainHandler.requests == [
            ("POST", "/api/v1/scheduler/drain/resume")
        ]
    finally:
        _cleanup(host, processes, server)


def test_workspace_start_caps_scheduler_resume_request_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复派发请求不能把统一启动拖过容器等待窗口。

    参数：``tmp_path`` 提供隔离工作区，``monkeypatch`` 替换公开调度客户端。
    返回无；断言即使组件 readiness 预算为 600 秒，单次恢复请求仍固定封顶 10 秒。
    异常：预算再次随 readiness 放大时断言失败。
    """

    paths = WorkspacePaths.resolve(tmp_path)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=600.0)
    captured: dict[str, object] = {}

    def resume(address: str, *, timeout: float) -> dict[str, object]:
        """记录恢复请求；参数是 Backend 地址和超时，返回合法运行状态，异常无。"""

        captured.update(address=address, timeout=timeout)
        return {
            "phase": "running",
            "active_device_job_count": 0,
            "active_device_job_ids": [],
        }

    host._components["backend"].update(
        phase="ready",
        address="http://127.0.0.1:18003",
    )
    monkeypatch.setattr(host._scheduler_lifecycle, "resume", resume)
    try:
        host._resume_local_scheduler()
    finally:
        host.close()

    assert captured == {
        "address": "http://127.0.0.1:18003",
        "timeout": 10.0,
    }


def test_drain_deadline_does_not_issue_post_deadline_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排空总预算耗尽后应稳定报告超时，不再发起最小 socket 请求。

    参数：``monkeypatch`` 注入单调时钟和调度器响应。返回无；断言只发送初始
    POST，sleep 跨过 deadline 后直接抛 ``workspace_drain_timeout``。异常：实现
    在过期后继续 GET 或改变稳定错误码时断言失败。
    """

    lifecycle = SchedulerLifecycleClient()
    moments = iter((100.0, 100.0, 100.0, 100.2))
    requests: list[tuple[str, str]] = []

    def request(
        _address: str,
        *,
        method: str,
        path: str,
        timeout: float,
    ) -> dict[str, object]:
        """记录公开请求；参数是请求元数据，返回持续 draining 状态，异常无。"""

        assert timeout > 0
        requests.append((method, path))
        return {
            "phase": "draining",
            "active_device_job_count": 1,
            "active_device_job_ids": ["job-running"],
        }

    monkeypatch.setattr(
        "unilabos.workspace_host.scheduler_lifecycle.time.monotonic",
        lambda: next(moments),
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.scheduler_lifecycle.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(lifecycle, "_request", request)

    with pytest.raises(SchedulerLifecycleError) as captured:
        lifecycle.begin_and_wait("http://127.0.0.1:18003", timeout=0.1)

    assert captured.value.code == "workspace_drain_timeout"
    assert requests == [("POST", "/api/v1/scheduler/drain")]
