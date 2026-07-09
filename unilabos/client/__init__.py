"""HTTP 客户端模块

提供与 uni-lab-backend HTTP API 通信的能力：
- HTTPClient: 基于 httpx 的 HTTP 客户端（ak/sk 鉴权）
- SessionManager: 会话状态管理
- 响应信封解析
- 输出格式化
"""

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
