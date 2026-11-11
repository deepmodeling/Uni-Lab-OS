"""CLI 经真实管理路由登记源码包，与 Host 启动和受管 slave 共用台账。"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import run_cli_command
from unilabos.client.http import HTTPClient
from unilabos.config.config import BasicConfig
from unilabos.server.api.app import ControlTokenMiddleware
from unilabos.server.api.driver_packages import create_driver_packages_router
from unilabos.server.services import driver_packages as dp


@pytest.fixture
def package_stack(tmp_path, monkeypatch):
    service = dp.DriverPackageService(tmp_path / "host")
    monkeypatch.setattr(dp, "_service", service)
    monkeypatch.setattr(BasicConfig, "control_token", "package-cli-test")
    installed = []
    monkeypatch.setattr(service, "_install_dependencies", lambda deps, upgrade, log: installed.append(deps) or "uv")
    app = FastAPI()
    app.include_router(create_driver_packages_router())
    app.add_middleware(ControlTokenMiddleware)
    original = HTTPClient.__init__

    def connect(self, config, get_auth_secret=None):
        original(self, config, get_auth_secret)
        self._client.close()
        self._client = TestClient(app, base_url=config.base_url)

    monkeypatch.setattr(HTTPClient, "__init__", connect)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("管理 CLI 不得安装包体或启动设备进程"))
    return service, installed


def _run(tmp_path, capsys, *arguments):
    parser = build_parser()
    args = parser.parse_args([
        "--working-dir", str(tmp_path / "cli"), "package", *arguments,
        "--address", "http://testserver/api/v1/", "--json",
    ])
    assert run_cli_command(args, parser)
    return json.loads(capsys.readouterr().out)


def test_source_install_toggle_and_uninstall_share_host_ledger(package_stack, tmp_path, capsys):
    service, installed = package_stack
    source = tmp_path / "source"
    package = source / "demo_driver"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname="demo-driver"\nversion="1.0"\ndependencies=["pyserial", "unilabos"]\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text('''
raise AssertionError("只允许 AST 扫描，不得导入驱动")
from unilabos.registry.decorators import device
@device(id="demo", display_name="测试设备", category=["virtual_device"], supported_backends=["hostlink"])
class Device:
    def __init__(self, **kwargs): pass
''', encoding="utf-8")
    result = _run(tmp_path, capsys, "install", str(source), "--wait")
    assert result["status"] == "succeeded", result
    assert installed == [["pyserial"]]
    assert dp.enabled_package_dirs(service.working_dir) == [str(package)]
    inventory = _run(tmp_path, capsys, "list")
    assert inventory["packages"][0]["package_root"] == str(source)
    assert inventory["packages"][0]["device_ids"] == ["demo"]
    assert inventory["restart_required"] is True
    _run(tmp_path, capsys, "disable", "demo-driver")
    assert dp.enabled_package_dirs(service.working_dir) == []
    _run(tmp_path, capsys, "enable", "demo-driver")
    assert dp.enabled_package_dirs(service.working_dir) == [str(package)]
    assert _run(tmp_path, capsys, "uninstall", "demo-driver", "--wait")["status"] == "succeeded"
    assert source.is_dir()
    assert dp.load_ledger(service.working_dir) == {}
    assert not dp.ledger_path(tmp_path / "cli").exists()


def test_pypi_spec_fails_without_running_local_installer(package_stack, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, capsys, "install", "example-package==1.0")
    assert exc.value.code == 1
    assert package_stack[0].inventory()["packages"] == []


def test_changes_cli_returns_process_and_changed_file(package_stack, tmp_path, capsys):
    service, _ = package_stack
    source = tmp_path / "external"
    source.mkdir()
    code = source / "driver.py"
    code.write_text("old")
    service.configure([str(source)], external_only=True)
    code.write_text("new")
    service.source_changes.poll()
    service.source_changes.poll()
    capsys.readouterr()
    result = _run(tmp_path, capsys, "changes")
    assert result["automatic_restart"] is False
    assert result["notices"][0]["target"] == "host"
    assert result["notices"][0]["changed_files"] == [{"path": str(code), "change": "modified"}]


def test_server_operation_failure_returns_nonzero(package_stack, tmp_path, capsys):
    service, _ = package_stack
    service._operations["failed-op"] = dp.DriverPackageOperation(
        operation_id="failed-op", kind="install", spec="source", status="failed", error="依赖安装失败",
    )
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, capsys, "operation", "failed-op", "--wait")
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "failed"
    assert "依赖安装失败" in output.err
