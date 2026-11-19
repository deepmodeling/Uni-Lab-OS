"""同一个驱动包的 YAML 注册表，运行时（``--devices``）、``package inspect`` 与 ``package install``
台账必须看到同一组设备：都认 ``[tool.unilabos.registry].paths``、包内 ``registry/``，并与 ``@device``
合并，而不是二选一。"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from unilabos.app.cli.package import inspect_package
from unilabos.registry.registry import Registry
from unilabos.server.services import driver_packages as dp

_YAML_DEVICE = """
{device_id}:
  category: [{category}]
  class:
    module: yamlpkg.driver:PlainDriver
    type: python
    action_value_mappings: {{}}
    status_types: {{}}
"""

EXPECTED = {"yamlpkg_ast", "yamlpkg.pump-a", "yamlpkg.extra"}


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture()
def yaml_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "yaml-repo"
    _write(
        repo / "pyproject.toml",
        """
        [project]
        name = "yaml-pkg"
        version = "1.0.0"
        dependencies = []

        [tool.setuptools.packages.find]
        include = ["yamlpkg*"]

        [tool.unilabos.registry]
        paths = ["extra_registry"]
        """,
    )
    _write(repo / "yamlpkg" / "__init__.py", "")
    _write(
        repo / "yamlpkg" / "driver.py",
        '''
        from unilabos.registry.decorators import action, device


        class PlainDriver:
            def __init__(self, **kwargs):
                pass


        @device(id="yamlpkg_ast", category=["yaml"])
        class AstDriver:
            @action(description="ping")
            def ping(self) -> bool:
                return True
        ''',
    )
    _write(repo / "yamlpkg" / "registry" / "devices" / "pumps" / "pumps.yaml", _YAML_DEVICE.format(device_id="yamlpkg.pump-a", category="pump"))
    _write(repo / "extra_registry" / "devices" / "extra.yaml", _YAML_DEVICE.format(device_id="yamlpkg.extra", category="extra"))
    return repo


def test_runtime_mount_sees_declared_and_package_registries(yaml_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    registry = Registry.__wrapped__()  # 独立实例，不碰全局单例
    registry.setup(devices_dirs=[str(yaml_repo / "yamlpkg")], external_only=True)
    assert EXPECTED <= set(registry.device_type_registry)


def test_inspect_lists_yaml_and_decorator_devices(yaml_repo: Path, tmp_path: Path) -> None:
    info = inspect_package(str(yaml_repo), out_dir=str(tmp_path / "out"))
    assert set(info["devices"]) == EXPECTED
    by_id = {item["id"]: item for item in info["resources"]}
    assert by_id["yamlpkg.pump-a"]["class"]["module"] == "yamlpkg.driver:PlainDriver"
    assert by_id["yamlpkg_ast"]["class"]["module"] == "yamlpkg.driver:AstDriver"


def test_install_ledger_records_yaml_devices(yaml_repo: Path, tmp_path: Path, monkeypatch) -> None:
    service = dp.DriverPackageService(tmp_path / "host")
    monkeypatch.setattr(service, "_install_dependencies", lambda deps, upgrade, log: "uv")
    operation_id = service.start_install(str(yaml_repo))["operation_id"]
    deadline = time.monotonic() + 60
    while service.operation(operation_id)["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.operation(operation_id)["status"] == "succeeded", service.operation(operation_id)
    (entry,) = dp.load_ledger(service.working_dir).values()
    assert set(entry.device_ids) == EXPECTED
