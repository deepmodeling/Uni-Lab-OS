"""HTTP 客户端模块

提供与 uni-lab-backend HTTP API 通信的能力：
- HTTPClient: 基于 httpx 的 HTTP 客户端（ak/sk 鉴权）
- SessionManager: 会话状态管理
- 响应信封解析
- 输出格式化
"""

import sys as _sys

# 跨平台兜底：session.py 使用 fcntl 做文件锁，而 fcntl 是 POSIX 独有模块，Windows 上不存在。
# 为了不侵入 session.py，本处在导入 .session 之前，往 sys.modules 注入一个无操作的 fcntl 垫片。
# session.json 面向本地单用户 CLI，缺锁时并发损坏概率极低，故降级为无锁可接受。
if "fcntl" not in _sys.modules:
    try:
        import fcntl as _fcntl  # POSIX 正常导入，直接复用
    except ImportError:  # Windows 无 fcntl
        import types as _types

        _stub = _types.ModuleType("fcntl")
        _stub.LOCK_EX = 0
        _stub.LOCK_SH = 0
        _stub.LOCK_UN = 0
        _stub.LOCK_NB = 0
        _stub.flock = lambda *args, **kwargs: None
        _stub.lockf = lambda *args, **kwargs: None
        _sys.modules["fcntl"] = _stub

from .envelope import Envelope, EnvelopeError, parse_envelope, unwrap_envelope
from .http import HTTPClient, HTTPClientConfig
from .session import (
    SessionManager,
    SessionState,
    AuthInfo,
    ContextInfo,
    DEFAULT_BASE_URL,
    resolve_addr,
)
from .output import (
    OutputFormat,
    OutputFormatter,
    set_output_format,
    get_formatter,
    print_output,
    print_success,
    print_error,
    print_warning,
)

__all__ = [
    "Envelope",
    "EnvelopeError",
    "parse_envelope",
    "unwrap_envelope",
    "HTTPClient",
    "HTTPClientConfig",
    "SessionManager",
    "SessionState",
    "AuthInfo",
    "ContextInfo",
    "DEFAULT_BASE_URL",
    "resolve_addr",
    "OutputFormat",
    "OutputFormatter",
    "set_output_format",
    "get_formatter",
    "print_output",
    "print_success",
    "print_error",
    "print_warning",
]
