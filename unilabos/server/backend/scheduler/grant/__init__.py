"""调度器锁授予策略（可插拔）：谁在这一轮拿到完整资源集合。

- :mod:`.policy`：策略接口与 Python 参考实现（按键严格 FIFO）、宿主复核；
- :mod:`.native`：原生库（native/grant-policy，Rust → C ABI）的加载与选择；
- :mod:`._abi`：由 ``scripts/gen_grant_bindings.py`` 从 cbindgen 头文件生成的 ctypes 声明。
"""

from unilabos.server.backend.scheduler.grant.native import (
    NativeGrantPolicy,
    candidate_library_paths,
    library_filename,
    load_grant_policy,
)
from unilabos.server.backend.scheduler.grant.policy import (
    GrantPolicy,
    KeyIndex,
    PythonFifoPolicy,
    WaitingRequest,
    fifo_grants,
    validate_grants,
)

__all__ = [
    "GrantPolicy",
    "KeyIndex",
    "NativeGrantPolicy",
    "PythonFifoPolicy",
    "WaitingRequest",
    "candidate_library_paths",
    "fifo_grants",
    "library_filename",
    "load_grant_policy",
    "validate_grants",
]
