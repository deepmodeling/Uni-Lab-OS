"""驱动包台账 / API：源码树落 unilabos_data、按目录挂载、依赖用 uv 预装（网络与安装器用桩）。"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.driver_packages import create_driver_packages_router
from unilabos.server.services import driver_packages as dp

DEVICE_SOURCE = '''
from unilabos.registry.decorators import action, device


@device(id="{device_id}", display_name="虚拟泵", category=["virtual_device"], supported_backends=["hostlink"])
class Pump:
    run_in_test_mode = True

    def __init__(self, device_id=None, **kwargs):
        self.device_id = device_id

    @action(display_name="分液", always_free=True)
    def dispense(self, volume_ml: float = 1.0):
        return {{"volume_ml": volume_ml}}
'''


def _seed_managed_package(service: dp.DriverPackageService) -> Path:
    root = dp.packages_root(service.working_dir) / "demo" / "1.0"
    _write_source_tree(root, name="demo", version="1.0", package="demo", device_id="old_device", deps=[])
    dp.save_ledger(service.working_dir, {"demo": dp.DriverPackageRecord(
        name="demo", version="1.0", source_kind="archive", package_root=str(root),
        package_dirs=[str(root / "demo")], device_ids=["old_device"],
    )})
    return root


def _fake_remote_source(service, tmp_path, monkeypatch, *, name="demo", version="2.0", valid=True):
    staging = tmp_path / "download"
    root = staging / "source"
    if valid:
        _write_source_tree(root, name=name, version=version, package="demo", device_id="new_device", deps=[])
    else:
        root.mkdir(parents=True)
        (root / "pyproject.toml").write_text(f'[project]\nname="{name}"\nversion="{version}"\n', encoding="utf-8")
    monkeypatch.setattr(service, "_fetch_source_tree", lambda source, log: (staging, root, "digest"))
    monkeypatch.setattr(service, "_install_dependencies", lambda deps, upgrade, log: "")


def test_busy_mutations_return_conflict_and_reads_stay_available(service, tmp_path, monkeypatch):
    _seed_managed_package(service)
    source = tmp_path / "local"
    _write_source_tree(source, name="another", version="1.0", package="another", device_id="another", deps=["pyserial"])
    entered = threading.Event()
    release = threading.Event()

    def slow_dependencies(deps, upgrade, log):
        entered.set()
        assert release.wait(5)
        return "uv"

    monkeypatch.setattr(service, "_install_dependencies", slow_dependencies)
    app = FastAPI()
    app.include_router(create_driver_packages_router())
    with TestClient(app) as client:
        started = client.post("/api/v1/driver-packages/install", json={"spec": str(source)})
        assert started.status_code == 202
        try:
            assert entered.wait(3)
            error_of(client.put("/api/v1/driver-packages/demo/enabled", json={"enabled": False}), 3003)
            error_of(client.delete("/api/v1/driver-packages/demo"), 3003)
            error_of(client.post("/api/v1/driver-packages/install", json={"spec": str(source)}), 3003)
            ok_data(client.get("/api/v1/driver-packages"))
            assert len(service.operations()) == 1
        finally:
            release.set()
        assert _wait(service, ok_data(started)["operation_id"])["status"] == "succeeded"
        ok_data(client.put("/api/v1/driver-packages/demo/enabled", json={"enabled": False}))
    assert set(dp.load_ledger(service.working_dir)) == {"demo", "another"}


def test_terminal_operation_status_means_the_write_lock_is_free(service, tmp_path, monkeypatch):
    """读到终态就能发起下一次变更：终态落盘与释放写锁对读者是原子的。"""
    source = tmp_path / "local"
    _write_source_tree(source, name="demo", version="1.0", package="demo", device_id="demo_pump", deps=[])
    monkeypatch.setattr(service, "_install_dependencies", lambda *args: "")
    original_mutation = service.source_changes.mutation

    def slow_release(active: bool) -> None:
        if not active:
            time.sleep(0.3)  # 放大"终态已落盘、写锁未释放"的窗口（满载时线程恰好在这里被挂起）
        original_mutation(active)

    monkeypatch.setattr(service.source_changes, "mutation", slow_release)
    done = _wait(service, service.start_install(str(source))["operation_id"])
    assert done["status"] == "succeeded", done
    again = _wait(service, service.start_install(str(source), upgrade=True)["operation_id"])
    assert again["status"] == "succeeded", again


def test_concurrent_enable_writes_cannot_lose_a_successful_update(service, monkeypatch):
    dp.save_ledger(service.working_dir, {n: dp.DriverPackageRecord(name=n) for n in ("a", "b")})
    entered = threading.Event()
    release = threading.Event()
    original_save = dp.save_ledger

    def paused_save(path, records):
        entered.set()
        assert release.wait(5)
        original_save(path, records)

    monkeypatch.setattr(dp, "save_ledger", paused_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.set_enabled, "a", False)
        try:
            assert entered.wait(3)
            with pytest.raises(dp.DriverPackageBusyError):
                service.set_enabled("b", False)
        finally:
            release.set()
        assert first.result(timeout=3)["enabled"] is False
    assert service.set_enabled("b", False)["enabled"] is False
    assert all(not item.enabled for item in dp.load_ledger(service.working_dir).values())


def test_invalid_upgrade_keeps_previous_source_and_ledger(service, tmp_path, monkeypatch):
    old = _seed_managed_package(service)
    original = (old / "demo" / "pump.py").read_bytes()
    _fake_remote_source(service, tmp_path, monkeypatch, valid=False)
    done = _wait(service, service.start_install("https://example.invalid/demo.zip", upgrade=True)["operation_id"])
    assert done["status"] == "failed"
    assert dp.load_ledger(service.working_dir)["demo"].version == "1.0"
    assert (old / "demo" / "pump.py").read_bytes() == original


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_ledger_failure_rolls_back_source_replacement(service, tmp_path, monkeypatch, version):
    old = _seed_managed_package(service)
    original = (old / "demo" / "pump.py").read_bytes()
    _fake_remote_source(service, tmp_path, monkeypatch, version=version)

    def cannot_save(*args):
        raise OSError("模拟台账写入失败")

    monkeypatch.setattr(dp, "save_ledger", cannot_save)
    done = _wait(service, service.start_install("https://example.invalid/demo.zip", upgrade=True)["operation_id"])
    assert done["status"] == "failed" and "台账写入失败" in done["error"]
    assert (old / "demo" / "pump.py").read_bytes() == original
    assert dp.load_ledger(service.working_dir)["demo"].device_ids == ["old_device"]
    assert list(old.parent.iterdir()) == [old]
    assert list(service._staging_root().iterdir()) == []


def test_uninstall_ledger_failure_restores_managed_source(service, monkeypatch):
    old = _seed_managed_package(service)

    def cannot_save(*args):
        raise OSError("模拟台账写入失败")

    monkeypatch.setattr(dp, "save_ledger", cannot_save)
    done = _wait(service, service.start_uninstall("demo")["operation_id"])
    assert done["status"] == "failed"
    assert (old / "demo" / "pump.py").is_file()
    assert "demo" in dp.load_ledger(service.working_dir)


@pytest.mark.parametrize("name,version", [
    (".", "1.0"), ("..", "1.0"), ("demo", "."), ("demo", ".."), (".staging", "1.0"),
])
def test_bad_package_metadata_cannot_escape_cache(service, tmp_path, monkeypatch, name, version):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("保留", encoding="utf-8")
    _fake_remote_source(service, tmp_path, monkeypatch, name=name, version=version)
    dependencies = []
    monkeypatch.setattr(service, "_install_dependencies", lambda *a: dependencies.append(True))
    done = _wait(service, service.start_install("https://example.invalid/demo.zip")["operation_id"])
    assert done["status"] == "failed" and "不能用作源码目录" in done["error"]
    assert not dependencies
    assert sentinel.read_text(encoding="utf-8") == "保留"
    assert dp.load_ledger(service.working_dir) == {}


def test_package_storage_symlink_is_rejected(service, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = dp.packages_root(service.working_dir)
    home.mkdir(parents=True)
    try:
        (home / "demo").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    _fake_remote_source(service, tmp_path, monkeypatch)
    done = _wait(service, service.start_install("https://example.invalid/demo.zip")["operation_id"])
    assert done["status"] == "failed" and "符号链接" in done["error"]
    assert list(outside.iterdir()) == []


def test_local_package_keeps_explicit_symlink_mount(service, tmp_path, monkeypatch):
    outside = tmp_path / "shared"
    _write_source_tree(outside, name="demo", version="1.0", package="demo", device_id="linked_device", deps=[])
    local = tmp_path / "checkout"
    local.mkdir()
    (local / "pyproject.toml").write_text('[project]\nname="demo"\nversion="1.0"\n', encoding="utf-8")
    try:
        (local / "demo").symlink_to(outside / "demo", target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    monkeypatch.setattr(service, "_install_dependencies", lambda *a: "")
    done = _wait(service, service.start_install(str(local))["operation_id"])
    assert done["status"] == "succeeded", done
    record = dp.load_ledger(service.working_dir)["demo"]
    assert record.package_root == str(local)
    assert record.package_dirs == [str(outside / "demo")]


def _write_source_tree(root: Path, *, name: str, version: str, package: str, device_id: str, deps: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{name}"\nversion = "{version}"\n'
        "dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]\n"
        "[tool.setuptools.packages.find]\n"
        f'include = ["{package}*"]\n',
        encoding="utf-8",
    )
    (root / package).mkdir()
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / package / "pump.py").write_text(DEVICE_SOURCE.format(device_id=device_id), encoding="utf-8")
    (root / "graph").mkdir()
    (root / "graph" / "demo.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dp.DriverPackageService:
    svc = dp.DriverPackageService(tmp_path / "unilabos_data")
    svc.configure(scan_dirs=[str(tmp_path / "site_demo")], external_only=True)
    monkeypatch.setattr(dp, "_service", svc)
    return svc


def _wait(svc: dp.DriverPackageService, operation_id: str) -> dict:
    for _ in range(600):
        record = svc.operation(operation_id)
        if record["status"] != "running":
            return record
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def test_ledger_roundtrip_and_enabled_dirs(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "pkg_a"
    pkg_dir.mkdir()
    records = {
        "pkg_a": dp.DriverPackageRecord(name="pkg-a", package_dirs=[str(pkg_dir)], device_ids=["dev_a"], enabled=True),
        "pkg_b": dp.DriverPackageRecord(name="pkg-b", package_dirs=[str(tmp_path / "missing")], enabled=True),
        "pkg_c": dp.DriverPackageRecord(name="pkg-c", package_dirs=[str(pkg_dir)], enabled=False),
    }
    dp.save_ledger(tmp_path, records)
    loaded = dp.load_ledger(tmp_path)
    assert set(loaded) == {"pkg_a", "pkg_b", "pkg_c"}
    assert loaded["pkg_a"].device_ids == ["dev_a"]
    # 缺失目录与未启用的包不参与挂载
    assert dp.enabled_package_dirs(tmp_path) == [str(pkg_dir)]


def test_resolve_source_forms(tmp_path: Path) -> None:
    github = dp.resolve_source("https://github.com/Xuwznln/LabDeviceLockDemo")
    assert github.kind == "github" and github.fallback_name == "LabDeviceLockDemo"
    assert github.urls == (
        "https://codeload.github.com/Xuwznln/LabDeviceLockDemo/zip/refs/heads/main",
        "https://codeload.github.com/Xuwznln/LabDeviceLockDemo/zip/refs/heads/master",
    )
    pinned = dp.resolve_source("git+https://github.com/Xuwznln/LabDeviceLockDemo.git@v0.1.0")
    assert pinned.kind == "github" and pinned.ref == "v0.1.0"
    assert pinned.urls[0].endswith("/zip/refs/heads/v0.1.0") and pinned.urls[1].endswith("/zip/refs/tags/v0.1.0")
    tree = dp.resolve_source("https://github.com/Xuwznln/LabDeviceLockDemo/tree/dev")
    assert tree.ref == "dev"

    archive = dp.resolve_source("https://example.com/dl/acme-devices-0.1.0.tar.gz")
    assert archive.kind == "archive" and archive.fallback_name == "acme-devices-0.1.0"

    local = dp.resolve_source(str(tmp_path))
    assert local.kind == "local" and local.path == tmp_path.resolve()

    for bad in ("", "https://example.com/not-an-archive", "git+ssh://git@github.com/x/y.git", str(tmp_path / "nope")):
        with pytest.raises(dp.DriverPackageError):
            dp.resolve_source(bad)


def test_install_local_directory_registers_in_place(service: dp.DriverPackageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "acme_src"
    _write_source_tree(source, name="acme-devices", version="0.1.0", package="acme_devices", device_id="acme_pump_demo", deps=["pyserial>=3.5", "unilabos"])
    installed: list[tuple[list[str], bool]] = []

    def fake_deps(dependencies, upgrade, log):
        installed.append((list(dependencies), upgrade))
        log("[deps] stub")
        return "uv"

    monkeypatch.setattr(service, "_install_dependencies", fake_deps)

    started = service.start_install(str(source), enable=True)
    assert started["status"] == "running"
    done = _wait(service, started["operation_id"])
    assert done["status"] == "succeeded", done
    assert installed == [(["pyserial>=3.5"], False)]  # unilabos 本体不算依赖

    (package,) = service.inventory()["packages"]
    assert package["name"] == "acme-devices" and package["version"] == "0.1.0"
    assert package["source_kind"] == "local" and package["installer"] == "uv"
    assert package["package_root"] == str(source.resolve())
    assert package["package_dirs"] == [str((source / "acme_devices").resolve())]  # tests/ 与 graph/ 不是包
    assert package["device_ids"] == ["acme_pump_demo"]
    assert package["dependencies"] == ["pyserial>=3.5"]
    assert package["mounted"] is False and service.inventory()["restart_required"] is True
    assert dp.enabled_package_dirs(service.working_dir) == package["package_dirs"]
    # 源码树原地登记：unilabos_data 下没有拷贝
    assert not (dp.packages_root(service.working_dir) / "acme-devices").exists()

    # 卸载：本机目录不删文件
    done = _wait(service, service.start_uninstall("acme-devices")["operation_id"])
    assert done["status"] == "succeeded", done
    assert source.is_dir() and service.inventory()["packages"] == []


def test_install_from_github_archive_lands_in_unilabos_data(service: dp.DriverPackageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 造一个 GitHub codeload 风格的 zip：顶层是 <repo>-<branch>/ 目录
    tree = tmp_path / "LabDeviceLockDemo-main"
    _write_source_tree(tree, name="lock_demo", version="0.1.0", package="lock_demo", device_id="lock_probe_demo", deps=[])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path in tree.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(tmp_path))
    archive_bytes = buffer.getvalue()

    downloads: list[str] = []

    def fake_download(url: str, destination: Path, log) -> None:
        downloads.append(url)
        if url.endswith("/refs/heads/main"):
            raise dp.DriverPackageError("下载失败 HTTP 404")  # 仓库默认分支是 master
        destination.write_bytes(archive_bytes)

    monkeypatch.setattr(service, "_download", fake_download)
    monkeypatch.setattr(service, "_install_dependencies", lambda deps, upgrade, log: "")

    done = _wait(service, service.start_install("https://github.com/Xuwznln/LabDeviceLockDemo", name="lock_demo")["operation_id"])
    assert done["status"] == "succeeded", done
    assert downloads == [
        "https://codeload.github.com/Xuwznln/LabDeviceLockDemo/zip/refs/heads/main",
        "https://codeload.github.com/Xuwznln/LabDeviceLockDemo/zip/refs/heads/master",
    ]
    (package,) = service.inventory()["packages"]
    root = Path(package["package_root"])
    assert root == dp.packages_root(service.working_dir) / "lock_demo" / "0.1.0"
    assert (root / "pyproject.toml").is_file() and (root / "graph" / "demo.json").is_file()
    assert package["package_dirs"] == [str((root / "lock_demo").resolve())]
    assert package["device_ids"] == ["lock_probe_demo"]
    assert package["source_kind"] == "github" and len(package["sha256"]) == 64
    assert list((dp.packages_root(service.working_dir) / ".staging").glob("driver-package-*")) == [], "临时目录应清理"

    # 重装同版本：覆盖同一目录，不留第二份
    done = _wait(service, service.start_install("https://github.com/Xuwznln/LabDeviceLockDemo", upgrade=True)["operation_id"])
    assert done["status"] == "succeeded", done
    assert [item.name for item in (dp.packages_root(service.working_dir) / "lock_demo").iterdir()] == ["0.1.0"]

    # 卸载：删掉 unilabos_data 里的源码树
    done = _wait(service, service.start_uninstall("lock_demo")["operation_id"])
    assert done["status"] == "succeeded", done
    assert not (dp.packages_root(service.working_dir) / "lock_demo").exists()


def test_dependency_install_failure_and_toggle(service: dp.DriverPackageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "needs_deps"
    _write_source_tree(source, name="needs-deps", version="1.0", package="needs_deps", device_id="dep_dev", deps=["nonexistent-lib==9.9"])

    def failing(deps, upgrade, log):
        raise dp.DriverPackageError("依赖安装失败（uv 退出码 1）：nonexistent-lib==9.9")

    monkeypatch.setattr(service, "_install_dependencies", failing)
    done = _wait(service, service.start_install(str(source))["operation_id"])
    assert done["status"] == "failed" and "依赖安装失败" in done["error"]
    assert service.inventory()["packages"] == []  # 失败不登记
    assert done['requires_confirmation']
    with pytest.raises(dp.DriverPackageBusyError):
        service.start_install(str(source))
    service.confirm_operation(done['operation_id'], done['version'], '已修复并核实共享依赖环境')

    dp.save_ledger(service.working_dir, {"x": dp.DriverPackageRecord(name="x", package_dirs=[], enabled=True)})
    record = service.set_enabled("x", False)
    assert record["enabled"] is False
    with pytest.raises(dp.DriverPackageError):
        service.set_enabled("missing", True)


def test_dependency_command_shapes() -> None:
    uv = dp._dependency_install_command("uv", ["pyserial>=3.5", "pymodbus"], upgrade=True)
    assert uv[:5] == ["uv", "pip", "install", "--python", uv[4]] and "--upgrade" in uv and "pymodbus" in uv
    pip = dp._dependency_install_command("pip", ["pyserial>=3.5"], upgrade=False)
    assert pip[1:4] == ["-m", "pip", "install"] and "--upgrade" not in pip
    assert dp._dependency_filter(["Uni-Lab-OS>=0.12", "pyserial ; sys_platform == 'win32'", "acme_devices"], "acme-devices") == [
        "pyserial ; sys_platform == 'win32'"
    ]


def test_catalog_merges_remote_and_local(service: dp.DriverPackageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    working = service.working_dir
    dp.save_ledger(working, {"acme_devices": dp.DriverPackageRecord(name="acme-devices", version="0.1.0")})
    monkeypatch.setattr(dp.DriverPackageService, "_index_url", staticmethod(lambda: ""))
    empty = service.catalog()
    assert empty["packages"] == []
    assert empty["sources"] == [{"kind": "local", "location": str(dp.catalog_path(working)), "ok": True, "count": 0, "missing": True}]

    dp.catalog_path(working).parent.mkdir(parents=True, exist_ok=True)
    dp.catalog_path(working).write_text(
        '{"packages": [{"name": "lab-plates", "spec": "https://github.com/lab/plates", "devices": ["plate_reader"]},'
        ' {"name": "acme-devices", "spec": "https://github.com/acme/devices", "version": "9.9"}, {"name": ""}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(dp.DriverPackageService, "_index_url", staticmethod(lambda: "https://index.example/packages.json"))
    monkeypatch.setattr(
        dp.DriverPackageService,
        "_fetch_json",
        staticmethod(lambda url: [{"name": "acme-devices", "spec": "https://github.com/acme/devices@v0.2.0", "tags": ["pump"]}]),
    )
    merged = service.catalog()
    assert [item["kind"] for item in merged["sources"]] == ["remote", "local"]
    by_name = {item["name"]: item for item in merged["packages"]}
    assert by_name["acme-devices"]["spec"] == "https://github.com/acme/devices@v0.2.0"
    assert by_name["acme-devices"]["official"] is True and by_name["acme-devices"]["installed"] is True
    assert by_name["lab-plates"]["official"] is False and by_name["lab-plates"]["installed"] is False


def test_router_endpoints(service: dp.DriverPackageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(create_driver_packages_router())
    client = TestClient(app)

    response = client.get("/api/v1/driver-packages")
    assert response.status_code == 200
    body = ok_data(response)
    assert body["scan_dirs"] and body["external_only"] is True
    assert body["packages"] == [] and body["packages_root"].endswith("driver_packages")

    monkeypatch.setattr(dp.DriverPackageService, "_index_url", staticmethod(lambda: ""))
    assert ok_data(client.get("/api/v1/driver-packages/catalog"))["packages"] == []
    error_of(client.post("/api/v1/driver-packages/install", json={"spec": ""}), 1000)
    error_of(client.post("/api/v1/driver-packages/install", json={"spec": str(tmp_path / "missing")}), 1000)
    error_of(client.post("/api/v1/driver-packages/install", json={"spec": "https://example.com/page"}), 1000)
    error_of(client.get("/api/v1/driver-packages/operations/unknown"), 4001)
    error_of(client.put("/api/v1/driver-packages/ghost/enabled", json={"enabled": True}), 4001)

    source = tmp_path / "via_api"
    _write_source_tree(source, name="via-api", version="0.0.1", package="via_api", device_id="api_dev", deps=[])
    monkeypatch.setattr(service, "_install_dependencies", lambda deps, upgrade, log: "")
    accepted = client.post("/api/v1/driver-packages/install", json={"spec": str(source)})
    assert accepted.status_code == 202
    done = _wait(service, ok_data(accepted)["operation_id"])
    assert done["status"] == "succeeded", done
    assert ok_data(client.get("/api/v1/driver-packages/operations"))[0]["operation_id"] == ok_data(accepted)["operation_id"]
    ok_data(client.delete("/api/v1/driver-packages/via-api"), status=202)
