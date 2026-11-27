"""Windows 工作流草稿（Workflow Draft）原子发布安全合同。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from unilabos.workflow import source_publication, source_workspace
from unilabos.workflow.source_workspace import SourceWorkspaceError


class RecordingWindowsLock:
    """记录 Windows 字节锁生命周期并暴露最后一个目标描述符。"""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        """初始化锁状态；参数无，返回无，不接触真实平台锁。"""

        self.calls: list[tuple[int, int, int]] = []
        self.locked_descriptors: set[int] = set()
        self.last_descriptor: int | None = None

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        """记录锁定或解锁并维护当前锁集合。

        参数：``descriptor`` 是原草稿文件，``mode`` 是锁操作，``length`` 是
        字节区间。返回：无；未知操作使测试立即失败。
        """

        self.calls.append((descriptor, mode, length))
        self.last_descriptor = descriptor
        if mode == self.LK_NBLCK:
            self.locked_descriptors.add(descriptor)
            return
        if mode == self.LK_UNLCK:
            self.locked_descriptors.discard(descriptor)
            return
        raise AssertionError(f"unexpected lock mode: {mode}")


def _draft_hash(content: bytes) -> str:
    """计算工作流草稿（Workflow Draft）的稳定内容身份。

    参数：``content`` 是完整草稿字节。返回：带算法前缀的 SHA-256 哈希；无异常。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_windows_existing_draft_closes_lock_before_native_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有 Windows 草稿必须解锁并关闭原文件后才调用原生替换。

    参数：``tmp_path`` 提供同目录原稿和临时文件；``monkeypatch`` 模拟 Windows
    锁与 ``ReplaceFileW``。返回：无；安全不变量是不得在目标 FD 打开/锁定时调用
    ``os.replace``，且替换瞬间的旧字节必须进入备份供 CAS 再验证。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    replacement = b"value = 'changed'\n"
    target.write_bytes(original)
    windows_lock = RecordingWindowsLock()
    original_replace = os.replace
    native_calls: list[tuple[Path, Path, Path]] = []
    guarded = False

    @contextmanager
    def directory_guard(paths: Sequence[Path]) -> Iterator[None]:
        """记录 Windows 目录链保护窗口。

        参数：``paths`` 是从卷根到草稿父目录的有序路径。返回：上下文无值；
        退出时清除测试内保护状态。
        """

        nonlocal guarded
        assert parent in paths
        guarded = True
        try:
            yield
        finally:
            guarded = False

    def native_replace(target_path: Path, replacement_path: Path, backup: Path) -> None:
        """模拟一次 ``ReplaceFileW`` 并验证调用前的锁和描述符状态。

        参数：三个路径依次是规范草稿、替换稿和旧稿备份。返回：无；模拟器使用
        捕获的宿主替换原语实现同目录发布。
        """

        native_calls.append((target_path, replacement_path, backup))
        assert guarded
        assert windows_lock.locked_descriptors == set()
        assert windows_lock.last_descriptor is not None
        with pytest.raises(OSError):
            os.fstat(windows_lock.last_descriptor)
        original_replace(target_path, backup)
        original_replace(replacement_path, target_path)

    def forbidden_portable_replace(*_args: object, **_kwargs: object) -> None:
        """拒绝 Windows 既有草稿路径退回可移植替换。

        参数：位置与关键字参数只捕获任意误用形状。返回：无；总是抛出
        ``AssertionError``，证明既有草稿必须经过原生 Windows 适配器。
        """

        raise AssertionError("Windows existing draft used os.replace")

    monkeypatch.setattr(source_publication, "_PLATFORM", "win32")
    monkeypatch.setattr(source_publication, "_fcntl", None)
    monkeypatch.setattr(source_publication, "_msvcrt", windows_lock)
    monkeypatch.setattr(
        source_publication,
        "replace_windows_file_with_backup",
        native_replace,
        raising=False,
    )
    monkeypatch.setattr(
        source_publication,
        "hold_windows_directory_chain",
        directory_guard,
        raising=False,
    )
    monkeypatch.setattr(source_publication.os, "replace", forbidden_portable_replace)

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=_draft_hash(original),
    )

    assert target.read_bytes() == replacement
    assert len(native_calls) == 1
    assert [path.name for path in parent.iterdir()] == [target.name]


def test_windows_guard_precedes_temporary_open_and_cleans_both_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 目录 guard 必须在临时稿创建前固定完整发布窗口。

    参数：``tmp_path`` 提供原授权父目录、移走目录和攻击者替换目录；
    ``monkeypatch`` 在 guard 进入时发起重命名攻击。返回：无；保存必须失败关闭，
    原授权目录与攻击者目录都不得出现临时工作流草稿（Workflow Draft）artifact。
    """

    parent = tmp_path / "workflows"
    detached = tmp_path / "detached"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)
    windows_lock = RecordingWindowsLock()
    guard_entries = 0

    @contextmanager
    def attack_before_guard_yield(_paths: Sequence[Path]) -> Iterator[None]:
        """在 guard 建立前调换父目录，再允许被测发布逻辑继续。

        参数：``_paths`` 是被测目录链，本攻击只使用规范父路径。返回：上下文无值；
        攻击只执行一次，避免嵌套 guard 重复重命名。
        """

        nonlocal guard_entries
        guard_entries += 1
        if guard_entries == 1:
            parent.rename(detached)
            parent.mkdir()
        yield

    monkeypatch.setattr(source_publication, "_PLATFORM", "win32")
    monkeypatch.setattr(source_publication, "_fcntl", None)
    monkeypatch.setattr(source_publication, "_msvcrt", windows_lock)
    monkeypatch.setattr(
        source_publication,
        "hold_windows_directory_chain",
        attack_before_guard_yield,
    )

    with pytest.raises(source_publication.SourcePublicationError):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.exists() is False
    assert (detached / target.name).read_bytes() == original
    assert not any(path.name.endswith(".tmp") for path in parent.iterdir())
    assert not any(path.name.endswith(".tmp") for path in detached.iterdir())


@pytest.mark.parametrize(
    ("winerror", "expected_error"),
    [
        (32, source_publication.SourcePublicationConflict),
        (5, source_publication.SourcePublicationError),
    ],
)
def test_windows_raw_os_error_uses_winerror_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
    expected_error: type[RuntimeError],
) -> None:
    """Windows 原生接缝的裸 ``OSError`` 必须按 winerror 稳定分类。

    参数：``tmp_path`` 隔离草稿；``monkeypatch`` 模拟 Windows 发布；
    ``winerror`` 是共享冲突或权限拒绝；``expected_error`` 是期望公共错误。返回：
    无；共享冲突为 CAS 冲突，权限错误为基础设施故障，原稿都保持不变。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)
    windows_lock = RecordingWindowsLock()

    @contextmanager
    def directory_guard(_paths: Sequence[Path]) -> Iterator[None]:
        """接受 Windows 目录链并保持测试发布窗口。

        参数：``_paths`` 是规范目录链。返回：上下文无值；不改变宿主目录。
        """

        yield

    def fail_native_replace(
        _target: Path,
        _replacement: Path,
        _backup: Path,
    ) -> None:
        """从 Windows 原生替换接缝抛出带 winerror 的系统异常。

        参数：三个路径保持被测适配器形状。返回：无；总是抛出注入错误。
        """

        error = OSError(13, "注入的 Windows 源码发布故障")
        error.winerror = winerror  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(source_publication, "_PLATFORM", "win32")
    monkeypatch.setattr(source_publication, "_fcntl", None)
    monkeypatch.setattr(source_publication, "_msvcrt", windows_lock)
    monkeypatch.setattr(
        source_publication,
        "hold_windows_directory_chain",
        directory_guard,
    )
    monkeypatch.setattr(
        source_publication,
        "replace_windows_file_with_backup",
        fail_native_replace,
    )

    with pytest.raises(expected_error):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.read_bytes() == original


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 Windows 文件共享和 ReplaceFileW 合同只在 Windows CI 运行",
)
def test_native_windows_replaces_existing_draft(tmp_path: Path) -> None:
    """真实 Windows 必须完成已有工作流草稿（Workflow Draft）的 CAS 发布。

    参数：``tmp_path`` 是 Windows CI 的本地临时目录。返回：无；发布后只保留
    完整规范草稿，不遗留临时文件或旧稿备份。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    replacement = b"value = 'changed'\n"
    target.write_bytes(original)

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=_draft_hash(original),
    )

    assert target.read_bytes() == replacement
    assert [path.name for path in parent.iterdir()] == [target.name]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 Windows create-if-absent 合同只在 Windows CI 运行",
)
def test_native_windows_creates_missing_draft_without_clobber(tmp_path: Path) -> None:
    """真实 Windows 首次保存必须以仅创建语义发布工作流草稿（Workflow Draft）。

    参数：``tmp_path`` 提供不存在的规范路径。返回：无；新草稿完整落盘且目录中
    不遗留临时文件。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "new.py"
    replacement = b"value = 'new'\n"

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=None,
    )

    assert target.read_bytes() == replacement
    assert [path.name for path in parent.iterdir()] == [target.name]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 Windows 目录共享保护只在 Windows CI 运行",
)
def test_native_windows_blocks_parent_rename_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共保存入口必须在原生替换期间阻止草稿父目录重命名。

    参数：``tmp_path`` 提供真实 NTFS 目录；``monkeypatch`` 在原生替换 seam 中
    插入子进程攻击。返回：无；攻击必须失败且新字节只发布到原规范路径。
    """

    parent = tmp_path / "workflows"
    detached = tmp_path / "detached"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    replacement = b"value = 'changed'\n"
    target.write_bytes(original)
    original_native_replace = source_publication.replace_windows_file_with_backup
    rename_attempts: list[subprocess.CompletedProcess[str]] = []

    def replace_after_rename_attempt(
        target_path: Path,
        replacement_path: Path,
        backup: Path,
    ) -> None:
        """尝试从子进程重命名父目录，再委托真实 ``ReplaceFileW``。

        参数：三个路径保持原生替换顺序。返回：无；重命名若成功则立即使测试失败。
        """

        attempt = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; os.rename(sys.argv[1],sys.argv[2])",
                str(parent),
                str(detached),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        rename_attempts.append(attempt)
        assert attempt.returncode != 0
        original_native_replace(target_path, replacement_path, backup)

    monkeypatch.setattr(
        source_publication,
        "replace_windows_file_with_backup",
        replace_after_rename_attempt,
    )

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=_draft_hash(original),
    )

    assert len(rename_attempts) == 1
    assert target.read_bytes() == replacement
    assert not detached.exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 Windows junction 合同只在 Windows CI 运行",
)
def test_native_windows_rejects_junction_package_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Windows junction 不得成为可编辑包（Editable Package）授权根。

    参数：``tmp_path`` 提供真实目录与 junction，``monkeypatch`` 临时切换到无
    ``dir_fd`` 路径。返回：无；读取工作流源码（Workflow Source）manifest 前
    必须以 ``invalid_package_root`` 失败关闭。
    """

    real_root = tmp_path / "real"
    real_root.mkdir()
    real_root.joinpath("package.yaml").write_text(
        "package:\n  name: demo\nworkflows: []\n",
        encoding="utf-8",
    )
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    monkeypatch.setattr(source_workspace, "_DIRECTORY_FD_PATHS_SUPPORTED", False)

    with pytest.raises(SourceWorkspaceError) as caught:
        source_workspace.read_package_root(junction)

    assert caught.value.code == "invalid_package_root"
