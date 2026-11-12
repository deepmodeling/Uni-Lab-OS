"""runtime 命令复用 dev 生命周期 API，不创建独立工作区守护进程。"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import envelope
from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import run_cli_command
from unilabos.client.http import HTTPClient
from unilabos.config.config import BasicConfig
from unilabos.server.api.app import ControlTokenMiddleware
from unilabos.server.api.runtime.diagnostics import create_backend_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


def _run(tmp_path, capsys, *argv):
    parser = build_parser()
    args = parser.parse_args([
        "--working-dir", str(tmp_path), "runtime", *argv,
        "--address", "http://testserver/api/v1", "--json",
    ])
    assert run_cli_command(args, parser)
    return json.loads(capsys.readouterr().out)


def test_status_drain_resume_use_existing_scheduler(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(BasicConfig, "control_token", "runtime-cli-test")
    monkeypatch.setattr("unilabos.server.backend.edge_control.get_edge_control_service", lambda: None)
    # 调度器启动即按 runtime.db 做库存/执行对账：用真实（空的）WorkflowService，
    # MagicMock 的查询结果恒为真值，会被当成遗留派发而暂停派发。
    workflow = WorkflowService(":memory:")
    scheduler = BackendScheduler(workflow, MagicMock())
    backend = SimpleNamespace(device_manager=SimpleNamespace(get_active_jobs=lambda: []))
    app = create_backend_app(lambda: scheduler, lambda: backend)
    app.add_middleware(ControlTokenMiddleware)
    original = HTTPClient.__init__

    def connect(self, config, get_auth_secret=None):
        original(self, config, get_auth_secret)
        self._client.close()
        self._client = TestClient(app, base_url=config.base_url)

    monkeypatch.setattr(HTTPClient, "__init__", connect)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("runtime 查询和排空不得启动进程"))
    try:
        assert _run(tmp_path, capsys, "status")["drain"]["phase"] == "running"
        assert _run(tmp_path, capsys, "drain", "--wait")["phase"] == "drained"
        status = _run(tmp_path, capsys, "status")
        assert status["readiness"]["status"] == "not_ready"
        assert scheduler.dispatch_paused
        assert _run(tmp_path, capsys, "resume")["phase"] == "running"
        assert not scheduler.dispatch_paused
    finally:
        scheduler.stop()
        workflow.close()


def test_restart_targets_edge_and_reset_defaults_to_preview(tmp_path, monkeypatch, capsys):
    calls = []
    original = HTTPClient.__init__

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/reset"):
            return httpx.Response(200, json=envelope({
                "supported": True, "pending": False, "confirmation_token": "preview-token",
                "backup_path": "", "detail": "预览",
            }))
        return httpx.Response(200, json=envelope({
            "pending": True, "mode": "quiescent", "scope": "edge", "effective_scope": "edge",
            "requested_at": 1.0, "restarting": False, "safe_restart": True,
            "active_jobs": [], "dispatch_paused": True,
        }))

    def connect(self, config, get_auth_secret=None):
        original(self, config, get_auth_secret)
        self._client.close()
        self._client = httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(HTTPClient, "__init__", connect)
    _run(tmp_path, capsys, "restart")
    assert calls[-1].method == "POST"
    assert json.loads(calls[-1].content) == {"mode": "quiescent", "scope": "edge"}
    _run(tmp_path, capsys, "reset")
    assert calls[-1].method == "GET"
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, capsys, "reset", "--confirmation-token", "preview-token")
    assert exc.value.code == 1
    assert len(calls) == 2


def test_offline_runtime_status_fails_without_starting_daemon(tmp_path, monkeypatch, capsys):
    calls = []

    def offline(self, *args, **kwargs):
        calls.append(args)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(HTTPClient, "_request", offline)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("离线查询不得拉起 daemon"))
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, capsys, "status")
    assert exc.value.code == 1
    assert len(calls) == 1


@pytest.mark.parametrize("group", ["runtime", "package"])
def test_invalid_wait_budget_rejected_before_any_side_effect(group):
    parser = build_parser()
    args = ["runtime", "drain"] if group == "runtime" else ["package", "install", "/driver"]
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*args, "--wait", "--timeout", "nan"])
    assert exc.value.code == 2
