"""原生锁授予策略的 ctypes 声明。

由 scripts/gen_grant_bindings.py 从 native/grant-policy/include/unilab_grant_policy.h 生成，不要手改；
头文件本身由 cargo build 经 cbindgen 从 Rust 源码生成。

头文件 sha256: 59c494b739870b6b1f7801205a787a163a76caeda7f949a958ee3f0c5cf98a38
"""

from __future__ import annotations

import ctypes

UNI_GRANT_ABI_VERSION = 131072
UNI_GRANT_OK = 0
UNI_GRANT_ERR_ARGUMENT = -1


class UniGrantWaiting(ctypes.Structure):
    _fields_ = [
        ("keys", ctypes.POINTER(ctypes.c_uint32)),
        ("key_count", ctypes.c_size_t),
        ("handoff_target", ctypes.c_uint8),
    ]


# 函数名 → (返回类型, 参数类型)
FUNCTIONS = {
    "uni_grant_abi_version": (ctypes.c_uint32, []),
    "uni_grant_decide": (ctypes.c_int32, [
        ctypes.POINTER(UniGrantWaiting),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]),
}


def bind(library: ctypes.CDLL) -> ctypes.CDLL:
    """给已加载的库设置全部函数原型。"""

    for name, (restype, argtypes) in FUNCTIONS.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes
    return library
