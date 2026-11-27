"""凭据创建所需的窄 Win32 边界；导入本模块不加载 Windows 动态库。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

_FULL_CONTROL = 0x001F01FF
_PROTECTED_DACL = 0x1000
_READ_CONTROL = 0x00020000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _extended_path(path: Path) -> str:
    """使用绝对 Unicode 长路径；不通过 resolve 跟随调用者的链接。"""
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _cleanup(error: BaseException, operation: Callable[[], Any]) -> None:
    """保留原失败，同时明确记录清理失败，不能把权限或 I/O 错误变成成功。"""
    try:
        operation()
    except OSError as cleanup_error:
        error.add_note(f"凭据临时对象清理失败：{cleanup_error}")


@contextmanager
def _owned(value: Any, release: Callable[[Any], None]) -> Iterator[Any]:
    """原生资源只释放一次；清理失败不掩盖最初的错误。"""
    try:
        yield value
    except BaseException as error:
        _cleanup(error, lambda: release(value))
        raise
    else:
        release(value)


class _WindowsAPI:
    """按调用加载标准库 ctypes/msvcrt，并固定所有原生指针与句柄类型。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows 凭据适配器只能在 Windows 上调用")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self.ctypes, self.msvcrt, self.types = ctypes, msvcrt, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer, dword, boolean = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL
        pointer_out, dword_out = ctypes.POINTER(pointer), ctypes.POINTER(dword)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", dword), ("lpSecurityDescriptor", pointer), ("bInheritHandle", boolean)]

        class TokenUser(ctypes.Structure):
            _fields_ = [("sid", pointer), ("attributes", dword)]

        class AclSize(ctypes.Structure):
            _fields_ = [("count", dword), ("used", dword), ("free", dword)]

        class AceHeader(ctypes.Structure):
            _fields_ = [("kind", wintypes.BYTE), ("flags", wintypes.BYTE), ("size", wintypes.WORD)]

        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", dword), ("tag", dword)]

        self.SecurityAttributes, self.TokenUser = SecurityAttributes, TokenUser
        self.AclSize, self.AceHeader, self.AttributeTag = AclSize, AceHeader, AttributeTag
        declarations = {
            "GetCurrentProcess": (self.kernel, [], pointer),
            "CloseHandle": (self.kernel, [pointer], boolean),
            "LocalFree": (self.kernel, [pointer], pointer),
            "CreateFileW": (self.kernel, [wintypes.LPCWSTR, dword, dword,
                                          ctypes.POINTER(SecurityAttributes), dword, dword, pointer], pointer),
            "CreateDirectoryW": (self.kernel, [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)], boolean),
            "GetFileInformationByHandleEx": (self.kernel, [pointer, ctypes.c_int, pointer, dword], boolean),
            "OpenProcessToken": (self.security, [pointer, dword, pointer_out], boolean),
            "GetTokenInformation": (self.security, [pointer, ctypes.c_int, pointer, dword, dword_out], boolean),
            "ConvertSidToStringSidW": (self.security, [pointer, pointer_out], boolean),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
                self.security, [wintypes.LPCWSTR, dword, pointer_out, dword_out], boolean),
            "GetSecurityInfo": (self.security, [pointer, ctypes.c_int, dword, pointer_out, pointer_out,
                                               pointer_out, pointer_out, pointer_out], dword),
            "GetSecurityDescriptorControl": (self.security, [pointer, ctypes.POINTER(wintypes.WORD),
                                                            dword_out], boolean),
            "GetAclInformation": (self.security, [pointer, pointer, dword, ctypes.c_int], boolean),
            "GetAce": (self.security, [pointer, dword, pointer_out], boolean),
        }
        for name, (library, arguments, result) in declarations.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)

    def error(self, code: int | None = None) -> OSError:
        """调用失败后立即保存原 WinError，避免后续清理覆盖线程错误码。"""
        return self.ctypes.WinError(self.ctypes.get_last_error() if code is None else code)

    def close(self, handle: Any) -> None:
        if not self.CloseHandle(handle):
            raise self.error()

    def free(self, pointer: Any) -> None:
        if self.LocalFree(pointer):
            raise self.error()

    def sid_text(self, sid: Any) -> str:
        result = self.ctypes.c_void_p()
        if not self.ConvertSidToStringSidW(sid, self.ctypes.byref(result)):
            raise self.error()
        with _owned(result, self.free):
            return self.ctypes.wstring_at(result)

    def process_sid(self) -> str:
        """直接查询进程令牌，不依赖用户名、交互登录用户或环境变量。"""
        token = self.ctypes.c_void_p()
        if not self.OpenProcessToken(self.GetCurrentProcess(), 0x0008, self.ctypes.byref(token)):
            raise self.error()
        with _owned(token, self.close):
            size = self.types.DWORD()
            succeeded = self.GetTokenInformation(token, 1, None, 0, self.ctypes.byref(size))
            code = self.ctypes.get_last_error()
            if succeeded or code != 122:
                raise self.error(87 if succeeded else code)
            if not self.ctypes.sizeof(self.TokenUser) <= size.value <= 65536:
                raise OSError("Windows 进程令牌长度无效")
            buffer = self.ctypes.create_string_buffer(size.value)
            if not self.GetTokenInformation(token, 1, buffer, size.value, self.ctypes.byref(size)):
                raise self.error()
            user = self.ctypes.cast(buffer, self.ctypes.POINTER(self.TokenUser)).contents
            return self.sid_text(user.sid)

    @contextmanager
    def attributes(self) -> Iterator[tuple[Any, set[str]]]:
        """在对象创建前构造不继承父权限的完整 DACL。"""
        allowed = {self.process_sid(), "S-1-5-18", "S-1-5-32-544"}
        sddl = "D:P" + "".join(f"(A;;FA;;;{sid})" for sid in sorted(allowed))
        descriptor = self.ctypes.c_void_p()
        if not self.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, self.ctypes.byref(descriptor), None,
        ):
            raise self.error()
        with _owned(descriptor, self.free):
            attributes = self.SecurityAttributes(self.ctypes.sizeof(self.SecurityAttributes), descriptor, False)
            yield attributes, allowed

    def check_security(self, handle: Any, allowed: set[str], *, directory: bool = False) -> None:
        """写入前读回句柄 ACL；不支持 ACL 或静默忽略保护的文件系统必须失败。"""
        info = self.AttributeTag()
        if not self.GetFileInformationByHandleEx(handle, 9, self.ctypes.byref(info), self.ctypes.sizeof(info)):
            raise self.error()
        if (bool(info.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
                or info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError("凭据对象类型异常，或路径已变为重解析点")
        descriptor, acl = self.ctypes.c_void_p(), self.ctypes.c_void_p()
        status = self.GetSecurityInfo(handle, 1, 4, None, None, self.ctypes.byref(acl), None,
                                      self.ctypes.byref(descriptor))
        if status:
            raise self.error(status)
        with _owned(descriptor, self.free):
            control, revision = self.types.WORD(), self.types.DWORD()
            if not self.GetSecurityDescriptorControl(descriptor, self.ctypes.byref(control),
                                                     self.ctypes.byref(revision)):
                raise self.error()
            if not acl.value or not control.value & _PROTECTED_DACL:
                raise OSError("文件系统未应用受保护的凭据 DACL")
            size = self.AclSize()
            if not self.GetAclInformation(acl, self.ctypes.byref(size), self.ctypes.sizeof(size), 2):
                raise self.error()
            actual = set()
            for index in range(size.count):
                ace = self.ctypes.c_void_p()
                if not self.GetAce(acl, index, self.ctypes.byref(ace)):
                    raise self.error()
                header = self.AceHeader.from_address(ace.value)
                if header.kind != 0 or header.flags or header.size < 16:
                    raise OSError("凭据 ACL 包含意外的权限或继承规则")
                if self.types.DWORD.from_address(ace.value + 4).value != _FULL_CONTROL:
                    raise OSError("凭据 ACL 未应用预期的完整权限")
                actual.add(self.sid_text(ace.value + 8))
            if actual != allowed or size.count != len(allowed):
                raise OSError("凭据 ACL 未严格限定为当前进程身份、SYSTEM 和 Administrators")


def create_private_temporary(path: Path) -> tuple[int, Path]:
    """同目录独占创建受保护空文件；返回的 CRT fd 由调用者关闭。"""
    api, path = _WindowsAPI(), Path(path)
    handle, staged, descriptor = None, None, None
    try:
        with api.attributes() as (attributes, allowed):
            for _ in range(100):
                candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
                created = api.CreateFileW(_extended_path(candidate), 0xC0000000, 0,
                                          api.ctypes.byref(attributes), 1, 0x80, None)
                if created != api.ctypes.c_void_p(-1).value:
                    handle, staged = created, candidate
                    break
                error = api.error()
                if error.winerror not in {80, 183}:
                    raise error
            else:
                raise FileExistsError("无法为凭据分配唯一临时文件名")
            api.check_security(handle, allowed)
        # open_osfhandle 成功后句柄归 CRT 所有，不能再调用 CloseHandle。
        descriptor = api.msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None
        os.set_inheritable(descriptor, False)
        return descriptor, staged
    except BaseException as error:
        if descriptor is not None:
            _cleanup(error, lambda: os.close(descriptor))
        elif handle is not None:
            _cleanup(error, lambda: api.close(handle))
        if staged is not None:
            _cleanup(error, staged.unlink)
        raise


def create_private_directory(path: Path) -> None:
    """独占新建受保护目录；绝不更改或清理调用前已有的文件、链接或目录。"""
    api, path, created = _WindowsAPI(), Path(path), False
    try:
        with api.attributes() as (attributes, allowed):
            if not api.CreateDirectoryW(_extended_path(path), api.ctypes.byref(attributes)):
                raise api.error()
            created = True
            handle = api.CreateFileW(_extended_path(path), _READ_CONTROL | 0x80, 3, None, 3,
                                     0x02000000 | 0x00200000, None)
            if handle == api.ctypes.c_void_p(-1).value:
                raise api.error()
            with _owned(handle, api.close):
                api.check_security(handle, allowed, directory=True)
    except BaseException as error:
        if created:
            _cleanup(error, path.rmdir)
        raise


__all__ = ["create_private_temporary", "create_private_directory"]
