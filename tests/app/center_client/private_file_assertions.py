"""独立读取真实文件权限；Windows 验证 DACL，不用 POSIX mode 推断保密性。"""
from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

_WINDOWS = os.name == "nt"


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    pointer_out = ctypes.POINTER(pointer)

    def bind(library, name, args, result):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
        return function

    return SimpleNamespace(
        c=ctypes, w=wintypes,
        free=bind(kernel, "LocalFree", [pointer], pointer),
        process=bind(kernel, "GetCurrentProcess", [], wintypes.HANDLE),
        close=bind(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        open_token=bind(security, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, pointer_out], wintypes.BOOL),
        token_info=bind(security, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        sid_text=bind(security, "ConvertSidToStringSidW", [pointer, pointer_out], wintypes.BOOL),
        named_info=bind(security, "GetNamedSecurityInfoW", [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
            pointer_out, pointer_out, pointer_out, pointer_out, pointer_out], wintypes.DWORD),
        handle_info=bind(security, "GetSecurityInfo", [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
            pointer_out, pointer_out, pointer_out, pointer_out, pointer_out], wintypes.DWORD),
        control=bind(security, "GetSecurityDescriptorControl", [pointer, ctypes.POINTER(wintypes.WORD),
                                                               ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        get_ace=bind(security, "GetAce", [pointer, wintypes.DWORD, pointer_out], wintypes.BOOL),
        sddl=bind(security, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [pointer, wintypes.DWORD,
            wintypes.DWORD, pointer_out, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
        parse_sddl=bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [wintypes.LPCWSTR,
            wintypes.DWORD, pointer_out, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
        get_dacl=bind(security, "GetSecurityDescriptorDacl", [pointer, ctypes.POINTER(wintypes.BOOL),
            pointer_out, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        set_named_info=bind(security, "SetNamedSecurityInfoW", [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
            pointer, pointer, pointer, pointer], wintypes.DWORD),
    )


def _require_success(success) -> None:
    if not success:
        api = _windows_api()
        raise api.c.WinError(api.c.get_last_error())


def _sid_string(sid) -> str:
    api = _windows_api()
    text = api.c.c_void_p()
    _require_success(api.sid_text(sid, api.c.byref(text)))
    try:
        return api.c.wstring_at(text)
    finally:
        api.free(text)


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    api = _windows_api()
    token, size = api.c.c_void_p(), api.w.DWORD()
    _require_success(api.open_token(api.process(), 0x0008, api.c.byref(token)))
    try:
        api.token_info(token, 1, None, 0, api.c.byref(size))
        assert size.value > 0
        buffer = api.c.create_string_buffer(size.value)
        _require_success(api.token_info(token, 1, buffer, size, api.c.byref(size)))
        return _sid_string(api.c.c_void_p.from_buffer(buffer).value)
    finally:
        api.close(token)


def windows_security(path_or_descriptor: Path | int) -> dict:
    """通过独立 Win32 查询取回完整 DACL，支持写入之前的已打开句柄。"""
    api = _windows_api()
    descriptor, dacl = api.c.c_void_p(), api.c.c_void_p()
    arguments = (1, 0x00000004, None, None, api.c.byref(dacl), None, api.c.byref(descriptor))
    if isinstance(path_or_descriptor, int):
        import msvcrt
        error = api.handle_info(msvcrt.get_osfhandle(path_or_descriptor), *arguments)
    else:
        error = api.named_info(str(path_or_descriptor), *arguments)
    if error:
        raise api.c.WinError(error)
    try:
        control, revision = api.w.WORD(), api.w.DWORD()
        _require_success(api.control(descriptor, api.c.byref(control), api.c.byref(revision)))
        assert dacl.value, "不允许 NULL DACL"
        ace_count = api.c.c_ushort.from_address(dacl.value + 4).value
        entries = []
        for index in range(ace_count):
            ace = api.c.c_void_p()
            _require_success(api.get_ace(dacl, index, api.c.byref(ace)))
            kind = api.c.c_ubyte.from_address(ace.value).value
            flags = api.c.c_ubyte.from_address(ace.value + 1).value
            mask = api.w.DWORD.from_address(ace.value + 4).value
            # 本合同仅允许普通 ACCESS_ALLOWED_ACE；复杂 ACE 不能被误读为通过。
            entries.append((kind, flags, mask, _sid_string(ace.value + 8) if kind == 0 else None))
        text = api.c.c_void_p()
        _require_success(api.sddl(descriptor, 1, 0x00000004, api.c.byref(text), None))
        try:
            sddl = api.c.wstring_at(text)
        finally:
            api.free(text)
        return {"protected": bool(control.value & 0x1000), "entries": entries, "sddl": sddl}
    finally:
        api.free(descriptor)


def assert_private(path_or_descriptor: Path | int, *, directory: bool = False) -> None:
    if not _WINDOWS:
        metadata = os.fstat(path_or_descriptor) if isinstance(path_or_descriptor, int) else path_or_descriptor.stat()
        assert stat.S_IMODE(metadata.st_mode) == (0o700 if directory else 0o600)
        return
    security = windows_security(path_or_descriptor)
    assert security["protected"], "私密文件不能继承父目录的宽权限"
    allowed = {
        current_user_sid(), "S-1-5-18", "S-1-5-32-544",
    }
    assert {entry[3] for entry in security["entries"]} == allowed
    assert len(security["entries"]) == len(allowed)
    assert all(kind == 0 and flags == 0 and mask == 0x1F01FF
               for kind, flags, mask, _ in security["entries"])


def permission_snapshot(path: Path):
    return windows_security(path)["sddl"] if _WINDOWS else stat.S_IMODE(path.stat().st_mode)


def make_test_file_public(path: Path) -> None:
    """仅用于临时测试文件，构造一个需要被私密替换的宽权限旧文件。"""
    if not _WINDOWS:
        path.chmod(0o666)
        return
    api = _windows_api()
    descriptor, dacl = api.c.c_void_p(), api.c.c_void_p()
    present, defaulted = api.w.BOOL(), api.w.BOOL()
    _require_success(api.parse_sddl("D:P(A;;FA;;;WD)", 1, api.c.byref(descriptor), None))
    try:
        _require_success(api.get_dacl(descriptor, api.c.byref(present), api.c.byref(dacl), api.c.byref(defaulted)))
        assert present.value and dacl.value
        error = api.set_named_info(str(path), 1, 0x80000004, None, None, dacl, None)
        if error:
            raise api.c.WinError(error)
    finally:
        api.free(descriptor)


def symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if _WINDOWS and getattr(error, "winerror", None) == 1314:
            pytest.skip("当前 Windows 账号没有创建符号链接的权限（WinError 1314）")
        raise
