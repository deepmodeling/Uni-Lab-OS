"""UniLabOS 统一出站客户端。

包含上游 Backend 的 HTTP/会话能力，以及微后端四库的
Local/HTTP/HostLink 等价 client。服务端 API 与数据模型仍归属
``unilabos.server``，调用入口统一归属本命名空间。
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
