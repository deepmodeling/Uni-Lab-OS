"""凭据落盘必须从创建时限制权限，并在写入失败时保留已有内容。"""

from __future__ import annotations

import builtins
import errno
import io
import os
from pathlib import Path

import pytest

from tests.app.center_client.private_file_assertions import (
    assert_private,
    make_test_file_public,
    permission_snapshot,
    symlink_or_skip,
)
from unilabos.app.center_client import enrollment


@pytest.mark.parametrize("filename", ["private.pem", "工站私钥.pem"])
def test_private_file_is_restricted_before_contents_are_written(tmp_path: Path, monkeypatch, filename) -> None:
    original = os.fdopen
    observed: list[bool] = []

    def inspect_descriptor(descriptor, *args, **kwargs):
        assert_private(descriptor)
        observed.append(True)
        assert os.fstat(descriptor).st_size == 0
        return original(descriptor, *args, **kwargs)

    monkeypatch.setattr(enrollment.os, "fdopen", inspect_descriptor)
    destination = tmp_path / filename
    enrollment.write_private(destination, "private material")
    assert observed == [True]
    assert destination.read_text() == "private material"
    assert_private(destination)
    assert list(tmp_path.iterdir()) == [destination]


def test_private_replacement_does_not_follow_destination_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "unrelated.pem"
    victim.write_text("original")
    destination = tmp_path / "private.pem"
    symlink_or_skip(destination, victim)
    enrollment.write_private(destination, "new private material")
    assert victim.read_text() == "original"
    assert not destination.is_symlink()
    assert destination.read_text() == "new private material"
    assert_private(destination)


@pytest.mark.parametrize("failure_point", ["write", "fsync", "replace"])
def test_private_write_failure_keeps_previous_file(tmp_path: Path, monkeypatch, failure_point) -> None:
    destination = tmp_path / "private.pem"
    destination.write_text("existing key")

    failure = OSError(errno.ENOSPC, "模拟磁盘写入失败")
    if failure_point == "write":
        original = os.fdopen

        class FailedWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, contents):
                self.stream.write(contents[:3])
                raise failure

        monkeypatch.setattr(enrollment.os, "fdopen", lambda *args, **kwargs: FailedWriter(original(*args, **kwargs)))
    else:
        def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(enrollment.os, failure_point, fail)
    with pytest.raises(OSError, match="模拟磁盘写入失败") as caught:
        enrollment.write_private(destination, "replacement key")
    assert caught.value is failure
    assert destination.read_text() == "existing key"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("existing", [False, True])
def test_certificate_publish_failure_restores_previous_credentials(
    tmp_path: Path, monkeypatch, existing: bool
) -> None:
    private = tmp_path / enrollment.PRIVATE_KEY_FILE
    certificate = tmp_path / enrollment.CERTIFICATE_FILE
    parent_permissions = permission_snapshot(tmp_path)
    if existing:
        enrollment.persist_credentials(tmp_path, "old certificate", "old key")
    original = os.replace

    def replace(source, destination):
        if Path(destination) == certificate:
            raise OSError("模拟证书发布失败")
        return original(source, destination)

    monkeypatch.setattr(enrollment.os, "replace", replace)
    with pytest.raises(OSError, match="模拟证书发布失败"):
        enrollment.persist_credentials(tmp_path, "new certificate", "new key")
    if existing:
        assert private.read_text() == "old key"
        assert certificate.read_text() == "old certificate"
        assert_private(private)
        assert set(tmp_path.iterdir()) == {private, certificate}
    else:
        assert list(tmp_path.iterdir()) == []
    assert permission_snapshot(tmp_path) == parent_permissions


def test_certificate_staging_failure_preserves_both_existing_files(tmp_path: Path, monkeypatch) -> None:
    enrollment.persist_credentials(tmp_path, "old certificate", "old key")
    original = enrollment._stage_private

    def stage(path: Path, contents: str) -> Path:
        if path.name == enrollment.CERTIFICATE_FILE:
            raise OSError("模拟磁盘空间不足")
        return original(path, contents)

    monkeypatch.setattr(enrollment, "_stage_private", stage)
    with pytest.raises(OSError, match="模拟磁盘空间不足"):
        enrollment.persist_credentials(tmp_path, "new certificate", "new key")
    assert (tmp_path / enrollment.PRIVATE_KEY_FILE).read_text() == "old key"
    assert (tmp_path / enrollment.CERTIFICATE_FILE).read_text() == "old certificate"
    assert len(list(tmp_path.iterdir())) == 2


def test_private_replacement_restricts_wide_old_file_without_changing_parent(tmp_path: Path) -> None:
    directory = tmp_path / "凭据目录"
    directory.mkdir()
    destination = directory / "旧私钥.pem"
    destination.write_text("old public material", encoding="utf-8")
    make_test_file_public(destination)
    old_permissions = permission_snapshot(destination)
    parent_permissions = permission_snapshot(directory)
    enrollment.write_private(destination, "新的私密材料")
    assert destination.read_text(encoding="utf-8") == "新的私密材料"
    assert_private(destination)
    assert permission_snapshot(destination) != old_permissions
    assert permission_snapshot(directory) == parent_permissions
    assert list(directory.iterdir()) == [destination]


def test_fdopen_failure_closes_temporary_descriptor_and_preserves_old_file(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "private.pem"
    destination.write_text("original", encoding="utf-8")
    captured = []

    def fail(descriptor, *args, **kwargs):
        captured.append(descriptor)
        assert_private(descriptor)
        raise OSError(errno.EIO, "模拟文件包装失败")

    monkeypatch.setattr(enrollment.os, "fdopen", fail)
    with pytest.raises(OSError, match="模拟文件包装失败"):
        enrollment.write_private(destination, "new private material")
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [destination]
    assert len(captured) == 1
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_real_fdopen_constructor_failure_preserves_error_and_closes_descriptor_once(tmp_path: Path, monkeypatch) -> None:
    """真实 TextIO 构造失败后仍由调用者唯一关闭 fd，不能用 EBADF 掩盖原异常。"""
    destination = tmp_path / "private.pem"
    destination.write_text("original", encoding="utf-8")
    original_fdopen, original_close = os.fdopen, os.close
    captured, closed, original_errors = [], [], []

    def invalid_encoding(descriptor, *args, **kwargs):
        captured.append(descriptor)
        assert_private(descriptor)
        # 保留产品传来的 closefd；只在真实 io.open/TextIOWrapper 中触发编码查找失败。
        kwargs["encoding"] = "credential-test-nonexistent-codec"
        try:
            return original_fdopen(descriptor, *args, **kwargs)
        except LookupError as error:
            original_errors.append(error)
            assert os.fstat(descriptor).st_size == 0
            raise

    def close(descriptor):
        if descriptor in captured:
            closed.append(descriptor)
        return original_close(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(enrollment.os, "fdopen", invalid_encoding)
        patch.setattr(enrollment.os, "close", close)
        with pytest.raises(LookupError, match="credential-test-nonexistent-codec") as caught:
            enrollment.write_private(destination, "replacement key")
    assert original_errors == [caught.value]
    assert len(captured) == 1 and closed == captured
    with pytest.raises(OSError) as closed_error:
        os.fstat(captured[0])
    assert closed_error.value.errno == errno.EBADF
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [destination]


def test_private_writer_never_opens_destination_and_publishes_only_complete_temporary(tmp_path: Path, monkeypatch) -> None:
    """无 symlink 权限时也验证发布边界；真实链接语义由独立集成用例覆盖。"""
    destination = tmp_path / "private.pem"
    destination.write_text("original", encoding="utf-8")
    original_replace, original_io_open = os.replace, io.open
    replaced = []

    def guard_open(original):
        def guarded(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path) == destination:
                pytest.fail("私密写入不可直接打开目标路径，避免跟随目标链接")
            return original(path, *args, **kwargs)
        return guarded

    def replace(source, target):
        assert Path(target) == destination
        assert Path(source) != destination and Path(source).parent == destination.parent
        assert_private(Path(source))
        with original_io_open(source, encoding="utf-8") as stream:
            assert stream.read() == "complete private contents"
        replaced.append(True)
        original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard_open(builtins.open))
        patch.setattr(io, "open", guard_open(io.open))
        patch.setattr(enrollment.os, "replace", replace)
        enrollment.write_private(destination, "complete private contents")
    assert replaced == [True]
    assert destination.read_text(encoding="utf-8") == "complete private contents"
    assert list(tmp_path.iterdir()) == [destination]
