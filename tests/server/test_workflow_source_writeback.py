"""作者源码回写：Linux lease 强路径与 macOS / Windows 可移植路径的同一外部契约。

覆盖后端选择（缺 lease / sigtimedwait / O_DIRECTORY / dir_fd 时退到可移植路径）、
内容 CAS、目标缺失、符号链接拒绝、sidecar 锁串行化、失败不留临时文件、
PermissionError → draft_hash_conflict，以及在“没有 dir_fd、没有 lease”的模拟平台上
走完整的 import-python → 草稿 → 应用。
"""

from __future__ import annotations

import hashlib
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.workflows import DeviceCatalog
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow import service as workflow_service
from unilabos.server.services.runtime.workflow.authoring_compiler import WorkflowAuthoringCompiler
from unilabos.server.services.runtime.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)

PORTABLE = workflow_service._SOURCE_WRITE_PORTABLE
LEASE = workflow_service._SOURCE_WRITE_LEASE
PUMP_UUID = "11111111-0000-4000-8000-000000000001"
SOURCE = '''from unilabos.registry.workflows import workflow


@workflow("加样流程", tags=["demo"])
def flow(ctx, *, amount: float = 2.0):
    first = ctx.run("pump-1/dose", {"volume": amount}, name="加样")
    ctx.run("pump-1/dose", {"volume": first.volume}, name="复加")
'''


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@pytest.fixture
def portable(monkeypatch) -> None:
    monkeypatch.setattr(workflow_service, "_SOURCE_WRITE_BACKEND", PORTABLE)


@pytest.fixture
def source(tmp_path) -> Iterator[Tuple[WorkflowService, Dict[str, Any], Path]]:
    """一个已登记托管源码的工作流：返回服务、登记信息与目标文件路径。"""

    service = WorkflowService(tmp_path / "runtime.db")
    service.configure_authoring(compiler=None, source_root=tmp_path / "sources")
    workflow = service.create_workflow(name="回写", tags=[], description=None, meta_data={})
    registration = service._registration(workflow["uuid"])
    target = Path(registration["package_root"]) / registration["relative_path"]
    try:
        yield service, registration, target
    finally:
        service.close()


def _leftovers(directory: Path) -> list[str]:
    """回写不应留下的半成品：临时文件与 CAS 备份。"""

    return sorted(path.name for path in directory.iterdir() if path.suffix in {".tmp", ".cas"})


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:  # Windows 无创建符号链接权限
        pytest.skip(f"当前环境不能创建符号链接：{exc}")


# 后端选择 -------------------------------------------------------------------


def test_backend_selection_env_forces_portable() -> None:
    assert workflow_service._select_source_write_backend({"UNILABOS_SOURCE_WRITE_BACKEND": "portable"}) == PORTABLE
    assert workflow_service._select_source_write_backend({"UNILABOS_SOURCE_WRITE_BACKEND": " Portable "}) == PORTABLE


def test_backend_selection_unknown_env_value_falls_back_to_auto(caplog) -> None:
    expected = LEASE if workflow_service._linux_lease_write_supported() else PORTABLE
    assert workflow_service._select_source_write_backend({"UNILABOS_SOURCE_WRITE_BACKEND": "fast"}) == expected
    assert "UNILABOS_SOURCE_WRITE_BACKEND" in caplog.text


@pytest.mark.skipif(not workflow_service._linux_lease_write_supported(), reason="仅 Linux 具备 lease / dir_fd 原语")
def test_backend_selection_prefers_linux_lease_when_primitives_exist() -> None:
    assert workflow_service._select_source_write_backend({}) == LEASE
    assert workflow_service._select_source_write_backend({"UNILABOS_SOURCE_WRITE_BACKEND": "auto"}) == LEASE


def _drop_fcntl(monkeypatch) -> None:
    monkeypatch.setattr(workflow_service, "fcntl", None)


def _drop_attribute(module: Any, name: str) -> Callable[[Any], None]:
    def apply(monkeypatch) -> None:
        monkeypatch.delattr(module, name, raising=False)

    return apply


def _drop_dir_fd(monkeypatch) -> None:
    monkeypatch.setattr(os, "supports_dir_fd", set())


def _drop_fcntl_lease(monkeypatch) -> None:
    if workflow_service.fcntl is not None:
        monkeypatch.delattr(workflow_service.fcntl, "F_SETLEASE", raising=False)


@pytest.mark.parametrize(
    "remove",
    [
        pytest.param(_drop_fcntl, id="windows-no-fcntl"),
        pytest.param(_drop_fcntl_lease, id="macos-no-F_SETLEASE"),
        pytest.param(_drop_attribute(signal, "sigtimedwait"), id="macos-no-sigtimedwait"),
        pytest.param(_drop_attribute(signal, "SIGRTMAX"), id="no-realtime-signal"),
        pytest.param(_drop_attribute(os, "O_DIRECTORY"), id="windows-no-O_DIRECTORY"),
        pytest.param(_drop_dir_fd, id="windows-no-dir_fd"),
    ],
)
def test_backend_selection_falls_back_to_portable_without_linux_primitives(monkeypatch, remove) -> None:
    remove(monkeypatch)
    assert not workflow_service._linux_lease_write_supported()
    assert workflow_service._select_source_write_backend({}) == PORTABLE


# 两种后端共享的外部契约 ---------------------------------------------------------


def test_write_back_create_then_compare_and_replace(source, source_write_backend) -> None:
    service, registration, target = source
    assert service._read_source(registration) is None
    assert service.source_signature(registration) == ("missing",)

    service._atomic_write(registration, b"first = 1\n", expected_hash=None)
    first = service._read_source(registration)
    assert first is not None and first["draft_hash"] == _hash(b"first = 1\n")
    assert service.source_signature(registration)[0] == "file"

    service._atomic_write(registration, b"second = 2\n", expected_hash=first["draft_hash"])
    assert target.read_bytes() == b"second = 2\n"
    assert _leftovers(target.parent) == []
    lock = target.parent / f".{target.name}.lock"
    # 可移植路径的 sidecar 锁文件常驻；Linux lease 路径不使用它。
    assert lock.exists() is (source_write_backend == PORTABLE)


def test_write_back_stale_hash_conflicts_and_keeps_target(source, source_write_backend) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base = 1\n", expected_hash=None)
    stale = _hash(b"base = 1\n")
    target.write_bytes(b"edited elsewhere\n")

    with pytest.raises(WorkflowConflict) as raised:
        service._atomic_write(registration, b"mine\n", expected_hash=stale)
    assert raised.value.code == "draft_hash_conflict"
    assert target.read_bytes() == b"edited elsewhere\n"
    # 目标已存在时 expected_hash=None 也是冲突，不能覆盖。
    with pytest.raises(WorkflowConflict):
        service._atomic_write(registration, b"mine\n", expected_hash=None)
    assert target.read_bytes() == b"edited elsewhere\n"
    assert _leftovers(target.parent) == []


def test_write_back_missing_target_with_expected_hash_conflicts(source, source_write_backend) -> None:
    service, registration, target = source
    with pytest.raises(WorkflowConflict) as raised:
        service._atomic_write(registration, b"x = 1\n", expected_hash=_hash(b"gone\n"))
    assert raised.value.code == "draft_hash_conflict"
    assert not target.exists()
    assert _leftovers(target.parent) == []


def test_write_back_rejects_symlinked_target(source, source_write_backend, tmp_path) -> None:
    service, registration, target = source
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside = 1\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(target, outside)

    with pytest.raises(WorkflowError) as raised:
        service._atomic_write(registration, b"x = 1\n", expected_hash=_hash(b"outside = 1\n"))
    assert raised.value.code == "invalid_input"
    with pytest.raises(WorkflowError):
        service._read_source(registration)
    with pytest.raises(WorkflowError):
        service.source_signature(registration)
    assert outside.read_bytes() == b"outside = 1\n" and target.is_symlink()
    assert _leftovers(target.parent) == []


# 可移植路径 ---------------------------------------------------------------------


def test_portable_rejects_symlinked_workflows_directory(source, portable, tmp_path) -> None:
    service, registration, target = source
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target.parent.rmdir()
    _symlink_or_skip(target.parent, elsewhere, directory=True)

    with pytest.raises(WorkflowError) as raised:
        service._atomic_write(registration, b"x = 1\n", expected_hash=None)
    assert raised.value.code == "invalid_input"
    assert list(elsewhere.iterdir()) == []


def test_portable_rejects_symlinked_lock_file(source, portable, tmp_path) -> None:
    service, registration, target = source
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"")
    _symlink_or_skip(target.parent / f".{target.name}.lock", decoy)

    with pytest.raises(WorkflowError) as raised:
        service._atomic_write(registration, b"x = 1\n", expected_hash=None)
    assert raised.value.code == "invalid_input"
    assert not target.exists() and decoy.read_bytes() == b""


def test_portable_lock_serializes_writers_across_threads(source, portable) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    finished = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            service._atomic_write(registration, b"from thread\n", expected_hash=_hash(b"base\n"))
        except BaseException as exc:  # noqa: BLE001 - 交给主线程断言
            errors.append(exc)
        finally:
            finished.set()

    with WorkflowService._portable_source_lock(target.parent, target.name):
        thread = threading.Thread(target=writer)
        thread.start()
        # 主线程持锁期间，另一线程的读-比较-替换必须等待。
        assert not finished.wait(0.4)
        assert target.read_bytes() == b"base\n"
    thread.join(timeout=10)
    assert finished.is_set() and errors == []
    assert target.read_bytes() == b"from thread\n"


def test_portable_concurrent_writers_with_same_base_have_one_winner(source, portable, monkeypatch) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    real_replace = WorkflowService._portable_replace

    def slow_replace(source_path, target_path):
        # 拉长“最后确认 → replace”的窗口：没有 sidecar 锁时多个写者都会通过比较并各自发布。
        time.sleep(0.05)
        real_replace(source_path, target_path)

    monkeypatch.setattr(WorkflowService, "_portable_replace", staticmethod(slow_replace))
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def writer(index: int) -> None:
        barrier.wait()
        try:
            service._atomic_write(registration, f"writer {index}\n".encode(), expected_hash=_hash(b"base\n"))
            outcomes.append("ok")
        except WorkflowConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(outcomes) == ["conflict", "conflict", "conflict", "ok"]
    assert target.read_bytes().startswith(b"writer ")
    assert _leftovers(target.parent) == []


def test_portable_lock_timeout_is_a_conflict(source, portable, monkeypatch) -> None:
    service, registration, target = source
    monkeypatch.setattr(workflow_service, "_PORTABLE_LOCK_TIMEOUT_SECONDS", 0.1)
    target.parent.mkdir(parents=True, exist_ok=True)
    with WorkflowService._portable_source_lock(target.parent, target.name):
        started = time.monotonic()
        with pytest.raises(WorkflowConflict) as raised:
            service._atomic_write(registration, b"x = 1\n", expected_hash=None)
        assert time.monotonic() - started < 5
    assert raised.value.code == "draft_hash_conflict"
    assert not target.exists() and _leftovers(target.parent) == []


def test_portable_permission_error_on_replace_is_a_conflict(source, portable, monkeypatch) -> None:
    """Windows：目标被其他程序打开时 os.replace 抛 PermissionError，应是冲突而非 internal_error。"""

    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    real_replace = os.replace

    def replace(source_path, target_path, *args, **kwargs):
        if Path(target_path) == target:
            raise PermissionError(13, "The process cannot access the file", str(target_path))
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(WorkflowConflict) as raised:
        service._atomic_write(registration, b"mine\n", expected_hash=_hash(b"base\n"))
    assert raised.value.code == "draft_hash_conflict"
    assert target.read_bytes() == b"base\n"
    assert _leftovers(target.parent) == []


def test_portable_permission_error_reading_target_is_a_conflict(source, portable, monkeypatch) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    real_read = WorkflowService._portable_read_regular

    def read(path):
        if Path(path) == target:
            raise PermissionError(13, "sharing violation", str(path))
        return real_read(path)

    monkeypatch.setattr(WorkflowService, "_portable_read_regular", staticmethod(read))
    with pytest.raises(WorkflowConflict):
        service._atomic_write(registration, b"mine\n", expected_hash=_hash(b"base\n"))
    assert target.read_bytes() == b"base\n" and _leftovers(target.parent) == []


def test_portable_failure_before_publish_cleans_up(source, portable, monkeypatch) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    real_replace = os.replace

    def replace(source_path, target_path, *args, **kwargs):
        if Path(target_path) == target:
            raise OSError(5, "I/O error", str(target_path))
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(WorkflowError) as raised:
        service._atomic_write(registration, b"mine\n", expected_hash=_hash(b"base\n"))
    assert raised.value.code == "internal_error"
    assert target.read_bytes() == b"base\n"
    # 临时文件与未发布的 .cas 备份都已删除。
    assert _leftovers(target.parent) == []


def test_portable_failure_writing_temp_cleans_up(source, portable, monkeypatch) -> None:
    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)

    def fsync(_descriptor):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(WorkflowError) as raised:
        service._atomic_write(registration, b"mine\n", expected_hash=_hash(b"base\n"))
    assert raised.value.code == "internal_error"
    assert target.read_bytes() == b"base\n" and _leftovers(target.parent) == []


def test_portable_external_write_after_publish_keeps_backup(source, portable, monkeypatch) -> None:
    """发布后验证发现外部写入：返回冲突，不覆盖外部内容，保留原稿 .cas 备份。"""

    service, registration, target = source
    service._atomic_write(registration, b"base\n", expected_hash=None)
    real_replace = os.replace

    def replace(source_path, target_path, *args, **kwargs):
        real_replace(source_path, target_path, *args, **kwargs)
        if Path(target_path) == target:
            Path(target_path).write_bytes(b"external editor\n")

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(WorkflowConflict):
        service._atomic_write(registration, b"mine\n", expected_hash=_hash(b"base\n"))
    assert target.read_bytes() == b"external editor\n"
    backups = sorted(target.parent.glob(f".{target.name}.*.cas"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"base\n"
    assert not list(target.parent.glob("*.tmp"))


def test_failed_import_removes_source_and_lock(tmp_path, source_write_backend, monkeypatch) -> None:
    """导入在写入源码之后失败：源码文件与 sidecar 锁文件都被删除，不留半个定义。"""

    catalog = DeviceCatalog()
    catalog.add("pump-1", "pump", PUMP_UUID)
    service = WorkflowService(tmp_path / "runtime.db")
    service.configure_authoring(
        compiler=WorkflowAuthoringCompiler(catalog_provider=lambda: catalog, registry_fingerprint=lambda: "r1"),
        source_root=tmp_path / "workflow_sources",
    )

    def reject(*_args, **_kwargs):
        raise WorkflowError("candidate_invalid")

    monkeypatch.setattr(service, "apply_authoring_by_candidate", reject)
    try:
        with pytest.raises(WorkflowError):
            service.import_python_workflow(file_name="dose_flow.py", python_source=SOURCE)
        assert list((tmp_path / "workflow_sources" / "workflows").iterdir()) == []
        assert service.list_workflows()["total"] == 0
    finally:
        service.close()


def test_portable_no_clobber_rename_publishes_new_target(source, portable, monkeypatch) -> None:
    """Windows 的“目标缺失”发布用不替换已有文件的 rename，这里在 POSIX 上走一遍成功路径。"""

    service, registration, target = source
    monkeypatch.setattr(workflow_service, "_NO_CLOBBER_RENAME", True)
    service._atomic_write(registration, b"fresh\n", expected_hash=None)
    assert target.read_bytes() == b"fresh\n" and _leftovers(target.parent) == []


def test_portable_rejects_target_created_concurrently(source, portable, monkeypatch) -> None:
    service, registration, target = source
    real_publish = WorkflowService._portable_publish_new

    def publish(source_path, target_path):
        Path(target_path).write_bytes(b"someone else\n")
        real_publish(source_path, target_path)

    monkeypatch.setattr(WorkflowService, "_portable_publish_new", staticmethod(publish))
    with pytest.raises(WorkflowConflict):
        service._atomic_write(registration, b"mine\n", expected_hash=None)
    assert target.read_bytes() == b"someone else\n" and _leftovers(target.parent) == []


# 模拟 macOS / Windows：没有 lease、没有 sigtimedwait、（Windows）没有 O_DIRECTORY 与 dir_fd -----


def _emulate_platform(monkeypatch, *, windows: bool) -> None:
    _drop_fcntl_lease(monkeypatch)
    monkeypatch.delattr(signal, "sigtimedwait", raising=False)
    if windows:
        monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
        monkeypatch.setattr(os, "supports_dir_fd", set())
        monkeypatch.setattr(workflow_service, "_O_NOFOLLOW", 0)
        monkeypatch.setattr(workflow_service, "_O_CLOEXEC", 0)
        monkeypatch.setattr(workflow_service, "_NO_CLOBBER_RENAME", True)
        for name in ("open", "stat", "lstat", "mkdir", "link", "unlink", "replace", "rename", "rmdir"):
            original = getattr(os, name)

            def without_dir_fd(*args, __original=original, __name=name, **kwargs):
                if {"dir_fd", "src_dir_fd", "dst_dir_fd"} & {key for key, value in kwargs.items() if value is not None}:
                    raise NotImplementedError(f"{__name}: dir_fd unavailable on this platform")
                return __original(*args, **kwargs)

            monkeypatch.setattr(os, name, without_dir_fd)
    monkeypatch.setattr(workflow_service, "_SOURCE_WRITE_BACKEND", workflow_service._select_source_write_backend({}))


@pytest.mark.parametrize("windows", [False, True], ids=["macos", "windows"])
def test_authoring_flow_on_platform_without_leases(tmp_path, monkeypatch, windows) -> None:
    _emulate_platform(monkeypatch, windows=windows)
    assert workflow_service._SOURCE_WRITE_BACKEND == PORTABLE

    catalog = DeviceCatalog()
    catalog.add("pump-1", "pump", PUMP_UUID)
    service = WorkflowService(tmp_path / "runtime.db")
    service.configure_authoring(
        compiler=WorkflowAuthoringCompiler(catalog_provider=lambda: catalog, registry_fingerprint=lambda: "r1"),
        source_root=tmp_path / "workflow_sources",
    )
    try:
        client = TestClient(create_workflow_app(service))
        headers = {"X-Workflow-Filename": "dose_flow.py", "Content-Type": "text/x-python"}
        imported = client.post("/api/v1/local/workflows/import-python", content=SOURCE.encode(), headers=headers)
        assert imported.status_code == 201 and imported.json()["code"] == 0, imported.text
        # 静态编译失败的导入不写任何文件；目录里只有成功导入的源码及其 sidecar 锁。
        broken = SOURCE.replace('"pump-1/dose"', '"pump-1"').encode()
        assert client.post("/api/v1/local/workflows/import-python", content=broken, headers=headers).json()["code"] == 1000
        workflows_dir = tmp_path / "workflow_sources" / "workflows"
        names = sorted(path.name for path in workflows_dir.iterdir())
        assert len(names) == 2 and names[0].startswith(".dose_flow_") and names[0].endswith(".py.lock")
        assert names[1] == names[0][1:-len(".lock")]

        workflow = client.post("/api/v1/workflows", json={"name": "从零编写"}).json()["data"]
        saved = client.put(f"/api/v1/workflows/{workflow['uuid']}/authoring/draft", json={
            "python_source": SOURCE, "expected_draft_hash": None, "expected_workflow_revision": 1,
        }).json()
        assert saved["code"] == 0 and saved["data"]["state"] == "unapplied_graph", saved
        candidate = saved["data"]["candidate"]
        applied = client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply",
                              json={"candidate_hash": candidate["candidate_hash"]}).json()
        assert applied["code"] == 0 and applied["data"]["authoring"]["state"] == "applied", applied
        edited = SOURCE.replace("amount: float = 2.0", "amount: float = 3.0")
        resaved = client.put(f"/api/v1/workflows/{workflow['uuid']}/authoring/draft", json={
            "python_source": edited, "expected_draft_hash": applied["data"]["authoring"]["draft"]["draft_hash"],
            "expected_workflow_revision": 2,
        }).json()
        assert resaved["code"] == 0 and resaved["data"]["draft"]["python_source"] == edited, resaved
        stale = client.put(f"/api/v1/workflows/{workflow['uuid']}/authoring/draft", json={
            "python_source": SOURCE, "expected_draft_hash": applied["data"]["authoring"]["draft"]["draft_hash"],
            "expected_workflow_revision": 2,
        }).json()
        assert stale["code"] != 0
        assert not [path for path in workflows_dir.iterdir() if path.suffix in {".tmp", ".cas"}]
    finally:
        service.close()
