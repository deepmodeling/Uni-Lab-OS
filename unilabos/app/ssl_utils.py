"""WebSocket TLS 工具。"""

from __future__ import annotations

import ssl

import certifi


def create_wss_ssl_context() -> ssl.SSLContext:
    """使用 certifi CA 构建 WSS 上下文，避免 Windows 证书库异常。"""
    return ssl.create_default_context(cafile=certifi.where())
