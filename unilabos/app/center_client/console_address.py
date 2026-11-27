"""操作台地址选择：显式覆盖与自动候选分开保存，不参与工站身份。"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import CenterConfigError, _normalize_public_http_base_url

LAUNCHER_CONSOLE_URL_ENV = "UNILABOS_LAUNCHER_CONSOLE_HTTP_BASE_URL"


def console_origin(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("操作台地址必须为字符串")
    value = value.strip()
    if not value:
        return None
    try:
        normalized = _normalize_public_http_base_url(value)
    except CenterConfigError as error:
        raise ValueError("操作台地址必须为不带路径、凭据、查询参数或片段的 HTTP/HTTPS 地址") from error
    if urlsplit(normalized).hostname in {"0.0.0.0", "::"}:
        raise ValueError("请填写可访问的 IP 或域名，不能使用监听所有网卡的地址")
    return normalized


@dataclass(frozen=True)
class ConsoleAddressSettings:
    custom_url: str | None = None
    browser_url: str | None = None

    @classmethod
    def from_record(cls, record: dict) -> ConsoleAddressSettings:
        return cls(console_origin(record.get("custom_url")), console_origin(record.get("browser_url")))

    def resolve(self, deployment_url: str | None, launcher_url: str | None, *, browser_current: bool = True) -> dict:
        automatic, source = None, "none"
        # 重启后的浏览器候选只能兜底，不能盖住 Launcher 本次真正绑定的新端口。
        candidates = [(deployment_url, "deployment")]
        if browser_current:
            candidates.append((self.browser_url, "browser"))
        candidates.extend(((launcher_url, "launcher"), (self.browser_url, "browser")))
        for candidate, name in candidates:
            if candidate:
                try:
                    automatic, source = console_origin(candidate), name
                except ValueError:
                    if not self.custom_url:
                        raise
                    # 自定义地址不依赖自动来源的有效性。
                break
        return {"mode": "custom" if self.custom_url else "auto", "custom_url": self.custom_url,
                "automatic_url": automatic, "automatic_source": source,
                "effective_url": self.custom_url or automatic,
                "source": "custom" if self.custom_url else source}


def local_browser_origin(host: str, port: int) -> str:
    """生成当前监听端口的本机访问地址，支持 IPv4/IPv6 通配监听。"""
    address = ipaddress.ip_address(host)
    if address.is_unspecified:
        address = ipaddress.ip_address("::1" if address.version == 6 else "127.0.0.1")
    return _origin(address, port)


def detect_console_origin(host: str, port: int, center_url: str | None = None) -> str | None:
    """只使用已绑定端口；多网卡无法确定时留空，等待浏览器候选或手动配置。"""
    address = ipaddress.ip_address(host)
    if not 1 <= port <= 65535:
        raise ValueError("操作台端口必须为实际监听端口")
    if address.is_unspecified:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        # UDP connect 只查询出站路由，不向中心发送数据。
        if center_url:
            parsed = urlsplit(center_url)
            try:
                routes = socket.getaddrinfo(parsed.hostname, parsed.port or 443, family, socket.SOCK_DGRAM)
                for _, _, _, _, target in routes:
                    with socket.socket(family, socket.SOCK_DGRAM) as route:
                        route.connect(target)
                        candidate = ipaddress.ip_address(route.getsockname()[0])
                        if _usable_network_address(candidate):
                            return _origin(candidate, port)
            except (OSError, ValueError):
                pass
        try:
            candidates = {ipaddress.ip_address(item[4][0]) for item in
                          socket.getaddrinfo(socket.gethostname(), None, family, socket.SOCK_DGRAM)}
        except (OSError, ValueError):
            return None
        candidates = {item for item in candidates if _usable_network_address(item)}
        if len(candidates) != 1:
            return None
        address = candidates.pop()
    return _origin(address, port)


def _usable_network_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast)


def _origin(address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int) -> str:
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{port}"
