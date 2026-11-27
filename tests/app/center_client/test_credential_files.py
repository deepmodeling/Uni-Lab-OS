"""私密文件平台边界：真实权限、独占创建、故障传播及目录同步能力。"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.center_client import private_file_assertions as permissions
from tests.app.center_client.test_credential_rotation import prepare
from unilabos.app.center_client import credential_files, credential_rotation


def test_private_temporary_is_exclusive_restricted_empty_and_beside_target(tmp_path):
    target = tmp_path / "站点私钥.pem"
    target.write_text("existing", encoding="utf-8")
    parent_permissions = permissions.permission_snapshot(tmp_path)
    first, first_path = credential_files.create_private_temporary(target)
    second, second_path = credential_files.create_private_temporary(target)
    try:
        assert first_path != second_path
        for descriptor, path in ((first, first_path), (second, second_path)):
            assert path.parent == target.parent
            assert os.fstat(descriptor).st_size == 0
            assert not os.get_inheritable(descriptor)
            permissions.assert_private(descriptor)
    finally:
        os.close(first)
        os.close(second)
        first_path.unlink()
        second_path.unlink()
    assert target.read_text(encoding="utf-8") == "existing"
    assert permissions.permission_snapshot(tmp_path) == parent_permissions
    assert list(tmp_path.iterdir()) == [target]


def test_private_directory_is_restricted_at_creation_and_never_alters_parent(tmp_path):
    before = permissions.permission_snapshot(tmp_path)
    directory = tmp_path / "新凭据"
    credential_files.create_private_directory(directory)
    permissions.assert_private(directory, directory=True)
    assert permissions.permission_snapshot(tmp_path) == before
    with pytest.raises(FileExistsError):
        credential_files.create_private_directory(directory)
    assert list(directory.iterdir()) == []


def test_existing_regular_file_cannot_become_rotation_directory(tmp_path):
    target = tmp_path / "existing"
    target.write_bytes(b"unrelated")
    with pytest.raises(FileExistsError):
        credential_files.create_private_directory(target)
    assert target.read_bytes() == b"unrelated"


@pytest.mark.parametrize("failure_at", [None, "open", "fsync"])
def test_posix_directory_sync_propagates_failures_and_closes_open_descriptor(tmp_path, monkeypatch, failure_at):
    calls = []
    failure = OSError(errno.EIO, "模拟目录持久化失败")

    def open_directory(path, flags):
        calls.append(("open", path, flags))
        if failure_at == "open":
            raise failure
        return 42

    def sync(descriptor):
        calls.append(("fsync", descriptor))
        if failure_at == "fsync":
            raise failure

    monkeypatch.setattr(credential_files, "_WINDOWS", False)
    monkeypatch.setattr(credential_files, "os", SimpleNamespace(
        O_RDONLY=os.O_RDONLY, open=open_directory, fsync=sync,
        close=lambda descriptor: calls.append(("close", descriptor)),
    ))
    if failure_at:
        with pytest.raises(OSError) as caught:
            credential_files.sync_directory(tmp_path)
        assert caught.value is failure
    else:
        credential_files.sync_directory(tmp_path)
    assert calls[0] == ("open", tmp_path, os.O_RDONLY)
    assert calls[1:] == ([] if failure_at == "open" else [("fsync", 42), ("close", 42)])


def test_windows_does_not_attempt_unsupported_posix_directory_sync(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Windows 不应使用 CRT 目录描述符伪装 POSIX fsync")

    monkeypatch.setattr(credential_files, "_WINDOWS", True)
    monkeypatch.setattr(credential_files, "os", SimpleNamespace(open=forbidden, fsync=forbidden, close=forbidden))
    credential_files.sync_directory(tmp_path)


@pytest.mark.parametrize("failing_directory", ["new", "parent"])
def test_rotation_directory_sync_failure_is_reported_and_cleans_only_new_directory(tmp_path, monkeypatch, failing_directory):
    target = tmp_path / "new"
    marker = tmp_path / "unrelated.txt"
    marker.write_text("keep", encoding="utf-8")
    called = []
    failure = OSError(errno.EIO, "模拟目录同步故障")

    def sync(path):
        called.append(path)
        assert sorted(child.name for child in target.iterdir()) == [
            "machine-secret.txt", "rotation-request.json", "station-key.pem",
        ]
        if path == (target if failing_directory == "new" else tmp_path):
            raise failure

    monkeypatch.setattr(credential_rotation, "sync_directory", sync)
    with pytest.raises(OSError) as caught:
        prepare(target)
    assert caught.value is failure
    assert called == ([target] if failing_directory == "new" else [target, tmp_path])
    assert not target.exists()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.iterdir()) == [marker]


@pytest.mark.parametrize("winerror", [5, 32, 1314])
@pytest.mark.parametrize("windows", [False, True])
def test_symlink_capability_skip_is_limited_to_windows_privilege_error(tmp_path, monkeypatch, winerror, windows):
    failure = OSError("模拟创建链接错误")
    failure.winerror = winerror

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(permissions, "_WINDOWS", windows)
    monkeypatch.setattr(Path, "symlink_to", fail)
    if windows and winerror == 1314:
        with pytest.raises(pytest.skip.Exception, match="1314"):
            permissions.symlink_or_skip(tmp_path / "link", tmp_path / "target")
    else:
        with pytest.raises(OSError) as caught:
            permissions.symlink_or_skip(tmp_path / "link", tmp_path / "target")
        assert caught.value is failure
