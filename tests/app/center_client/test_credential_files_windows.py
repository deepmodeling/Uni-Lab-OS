"""真实 Win32 创建路径的权限故障与句柄所有权回归；仅使用 pytest 临时目录。"""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager

import pytest

from tests.app.center_client.private_file_assertions import assert_private, permission_snapshot
from unilabos.app.center_client import _credential_files_windows as windows

pytestmark = pytest.mark.skipif(os.name != "nt", reason="验证真实 Windows 安全描述符和原生句柄")


@pytest.fixture
def native_api(monkeypatch):
    api = windows._WindowsAPI()
    closed = []
    native_close = api.close
    query = api.kernel.GetHandleInformation
    query.argtypes = [api.ctypes.c_void_p, api.ctypes.POINTER(api.types.DWORD)]
    query.restype = api.types.BOOL

    def close(handle):
        native_close(handle)
        flags = api.types.DWORD()
        # 必须在下一次句柄分配前检查；事后数字可能已被 Windows 重用。
        assert not query(handle, api.ctypes.byref(flags))
        assert api.ctypes.get_last_error() == 6
        closed.append(getattr(handle, "value", handle))

    monkeypatch.setattr(api, "close", close)
    monkeypatch.setattr(windows, "_WindowsAPI", lambda: api)
    return api, closed


@pytest.mark.parametrize("kind", ["unsupported", "denied"])
def test_acl_query_failure_closes_empty_file_and_never_returns_fd(tmp_path, native_api, monkeypatch, kind):
    api, closed = native_api
    # 模拟底层卷不提供 ACL，或真实权限查询被拒绝；创建/关闭仍运行真实 API。
    status = 50 if kind == "unsupported" else 5
    monkeypatch.setattr(api, "GetSecurityInfo", lambda *args: status)
    with pytest.raises(OSError) as failure:
        windows.create_private_temporary(tmp_path / "private.pem")
    assert failure.value.winerror == status
    assert len(closed) == 2  # 进程令牌和临时文件。
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("protected", [True, False])
def test_native_readback_rejects_actual_broad_or_unprotected_acl(
    tmp_path, native_api, monkeypatch, protected,
):
    api, closed = native_api
    attributes = api.attributes

    @contextmanager
    def wrong_attributes():
        with attributes() as (original, allowed):
            # 只为此空测试文件注入宽 DACL；适配器必须在调用者写入任何数据前拒绝。
            sddl = "D:" + ("P" if protected else "") + "(A;;FA;;;WD)"
            descriptor = api.ctypes.c_void_p()
            assert api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, api.ctypes.byref(descriptor), None,
            )
            try:
                changed = api.SecurityAttributes(original.nLength, descriptor, False)
                yield changed, allowed
            finally:
                api.free(descriptor)

    monkeypatch.setattr(api, "attributes", wrong_attributes)
    with pytest.raises(OSError, match="DACL|ACL"):
        windows.create_private_temporary(tmp_path / "private.pem")
    assert len(closed) == 2
    assert list(tmp_path.iterdir()) == []


def test_security_descriptor_conversion_failure_creates_nothing(tmp_path, native_api, monkeypatch):
    api, closed = native_api

    def denied(*args):
        api.ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(api, "ConvertStringSecurityDescriptorToSecurityDescriptorW", denied)
    with pytest.raises(OSError) as failure:
        windows.create_private_temporary(tmp_path / "private.pem")
    assert failure.value.winerror == 5 and len(closed) == 1
    assert list(tmp_path.iterdir()) == []


def test_crt_conversion_failure_closes_native_handle_once(tmp_path, native_api, monkeypatch):
    api, closed = native_api

    def fail(handle, flags):
        raise OSError("模拟 CRT 接管句柄失败")

    monkeypatch.setattr(api.msvcrt, "open_osfhandle", fail)
    with pytest.raises(OSError, match="CRT"):
        windows.create_private_temporary(tmp_path / "private.pem")
    assert len(closed) == 2 and list(tmp_path.iterdir()) == []


def test_failure_after_crt_transfer_uses_crt_close_only(tmp_path, native_api, monkeypatch):
    api, closed = native_api
    transferred, crt_closed = [], []
    original_bridge, original_close = api.msvcrt.open_osfhandle, os.close

    def bridge(handle, flags):
        descriptor = original_bridge(handle, flags)
        transferred.append(descriptor)
        return descriptor

    def close(descriptor):
        original_close(descriptor)
        with pytest.raises(OSError) as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF
        crt_closed.append(descriptor)

    def fail(descriptor, inheritable):
        raise OSError("模拟禁止句柄继承失败")

    monkeypatch.setattr(api.msvcrt, "open_osfhandle", bridge)
    monkeypatch.setattr(windows.os, "close", close)
    monkeypatch.setattr(windows.os, "set_inheritable", fail)
    with pytest.raises(OSError, match="继承"):
        windows.create_private_temporary(tmp_path / "private.pem")
    assert len(transferred) == 1 and transferred == crt_closed
    assert len(closed) == 1  # 原生文件句柄已归 CRT，只有令牌走 CloseHandle。
    assert list(tmp_path.iterdir()) == []


def test_directory_acl_failure_removes_only_the_new_empty_directory(tmp_path, native_api, monkeypatch):
    api, closed = native_api
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("unrelated test data", encoding="utf-8")
    monkeypatch.setattr(api, "GetSecurityInfo", lambda *args: 50)
    with pytest.raises(OSError) as failure:
        windows.create_private_directory(tmp_path / "new-private-directory")
    assert failure.value.winerror == 50 and len(closed) == 2
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_text(encoding="utf-8") == "unrelated test data"


@pytest.mark.parametrize("directory", [True, False])
def test_existing_paths_are_never_modified_or_cleaned(tmp_path, native_api, directory):
    _, closed = native_api
    existing = tmp_path / "existing"
    if directory:
        existing.mkdir()
        content = existing / "keep.txt"
    else:
        content = existing
    content.write_text("original test data", encoding="utf-8")
    before = permission_snapshot(existing)
    with pytest.raises(FileExistsError):
        windows.create_private_directory(existing)
    assert content.read_text(encoding="utf-8") == "original test data"
    assert permission_snapshot(existing) == before
    assert len(closed) == 1


def test_success_transfers_private_noninheritable_fd_to_caller(tmp_path, native_api):
    _, closed = native_api
    target = tmp_path / "中文凭据.pem"
    descriptor, temporary = windows.create_private_temporary(target)
    try:
        assert temporary.parent == target.parent and not target.exists()
        assert os.fstat(descriptor).st_size == 0 and not os.get_inheritable(descriptor)
        assert_private(descriptor)
    finally:
        os.close(descriptor)
    assert len(closed) == 1
    os.replace(temporary, target)
    assert_private(target)
    assert target.read_bytes() == b""
