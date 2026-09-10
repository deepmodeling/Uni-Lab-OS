"""验证 OS 运行态 SQLite 的启动、保留与互斥生命周期。"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.runtime_storage import (
    RuntimeStorageInUseError,
    close_runtime_storage_session,
    get_runtime_storage_directory,
    prepare_runtime_storage_session,
)

_RUNTIME_DATABASES = (
    "inventory.db",
    "device_state.db",
    "workflow_history.db",
)
_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


@pytest.fixture(autouse=True)
def _isolated_runtime_storage_session() -> Any:
    """保证每个测试前后都释放本进程持有的运行目录锁。

    参数：无。返回：供 pytest 执行测试主体的生成器。异常：关闭运行态存储
    会话失败时原样传播，防止锁泄漏被静默掩盖。
    """

    close_runtime_storage_session()
    yield
    close_runtime_storage_session()


def _write_database_families(runtime_root: Path) -> dict[str, str]:
    """写入三类运行态数据库及其 SQLite 边车文件。

    参数：``runtime_root`` 是隔离的工作目录。返回：文件名到原始内容的映射，
    用于验证清空或保留结果。异常：目录或文件创建失败时原样传播。
    """

    runtime_root.mkdir(parents=True, exist_ok=True)
    contents: dict[str, str] = {}
    for database_name in _RUNTIME_DATABASES:
        for suffix in _SQLITE_SUFFIXES:
            filename = f"{database_name}{suffix}"
            contents[filename] = f"existing:{filename}"
            (runtime_root / filename).write_text(contents[filename], encoding="utf-8")
    return contents


def _hold_runtime_storage_lock(
    runtime_root: str,
    ready: Any,
    release: Any,
) -> None:
    """在独立进程中持有运行目录锁，供并发启动测试使用。

    参数：``runtime_root`` 是目标目录；``ready`` 通知父进程锁已获得；
    ``release`` 控制何时释放。返回：无。异常：子进程启动失败会通过退出码暴露。
    """

    prepare_runtime_storage_session(
        {"preserve_runtime_databases": True},
        working_dir=runtime_root,
    )
    ready.put("locked")
    release.wait(timeout=10)
    close_runtime_storage_session()


def test_default_startup_uses_a_private_temporary_database_directory(
    tmp_path: Path,
) -> None:
    """证明默认启动使用新临时目录且不触碰稳定工作目录中的任何旧库。

    参数：``tmp_path`` 是隔离工作目录。返回：无；断言边缘控制库
    ``edge_control.db``、三类旧运行库与无关文件均保持不变。异常：生命周期合同
    漂移时测试失败。
    """

    expected = _write_database_families(tmp_path)
    edge_control_files = {
        f"edge_control.db{suffix}": f"stable:{suffix}"
        for suffix in _SQLITE_SUFFIXES
    }
    for filename, content in edge_control_files.items():
        (tmp_path / filename).write_text(content, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated", encoding="utf-8")

    paths = prepare_runtime_storage_session(
        {"preserve_runtime_databases": False},
        working_dir=str(tmp_path),
    )

    runtime_root = Path(paths.inventory_db).parent
    assert runtime_root != tmp_path
    assert runtime_root.name.startswith("unilabos-runtime-")
    assert Path(paths.device_state_db).parent == runtime_root
    assert Path(paths.workflow_history_db).parent == runtime_root
    assert get_runtime_storage_directory() == str(runtime_root)
    assert {
        filename: (tmp_path / filename).read_text(encoding="utf-8")
        for filename in expected
    } == expected
    assert {
        filename: (tmp_path / filename).read_text(encoding="utf-8")
        for filename in edge_control_files
    } == edge_control_files
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "unrelated"
    close_runtime_storage_session()
    assert not runtime_root.exists()


def test_preserve_option_reuses_existing_runtime_databases(tmp_path: Path) -> None:
    """证明显式保留选项会沿用三类数据库及所有 SQLite 边车。

    参数：``tmp_path`` 是隔离工作目录。返回：无；断言每个预存文件内容不变。
    异常：保留选项被忽略时测试失败。
    """

    expected = _write_database_families(tmp_path)

    paths = prepare_runtime_storage_session(
        {"preserve_runtime_databases": True},
        working_dir=str(tmp_path),
    )

    assert Path(paths.inventory_db) == tmp_path / "inventory.db"
    assert get_runtime_storage_directory() == str(tmp_path)
    assert {
        filename: (tmp_path / filename).read_text(encoding="utf-8")
        for filename in expected
    } == expected


def test_repeated_prepare_does_not_reset_an_active_generation(tmp_path: Path) -> None:
    """证明同一进程重复准备不会误删本代启动后产生的运行态。

    参数：``tmp_path`` 是隔离工作目录。返回：无；断言第二次调用具有幂等性。
    异常：活跃代数据库被再次清空时测试失败。
    """

    arguments = {"preserve_runtime_databases": False}
    first_paths = prepare_runtime_storage_session(arguments, working_dir=str(tmp_path))
    inventory_database = Path(first_paths.inventory_db)
    inventory_database.write_text("current-generation", encoding="utf-8")

    second_paths = prepare_runtime_storage_session(arguments, working_dir=str(tmp_path))

    assert second_paths == first_paths
    assert inventory_database.read_text(encoding="utf-8") == "current-generation"


def test_runtime_directory_rejects_a_second_os_process(tmp_path: Path) -> None:
    """证明两个 OS 进程不能同时拥有同一工作目录的运行态数据库。

    参数：``tmp_path`` 是父子进程共享目录。返回：无；断言第二个所有者收到明确
    的运行态占用错误。异常：子进程未就绪或无法结束时测试失败。
    """

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    process = context.Process(
        target=_hold_runtime_storage_lock,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    try:
        assert ready.get(timeout=10) == "locked"
        with pytest.raises(RuntimeStorageInUseError):
            prepare_runtime_storage_session(
                {"preserve_runtime_databases": True},
                working_dir=str(tmp_path),
            )
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert process.exitcode == 0
