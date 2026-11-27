"""Center 客户端配置。

私钥由工站生成；中心返回证书，机器密钥在注册响应里出现一次后必须写入本地密钥存储。

这里刻意不做"缺什么就用默认值补上"：缺少客户端证书或客户端 CA 时直接报错。一个
能连上中心但没有可验证身份的客户端，比连不上更危险——它会让中心侧的错误配置看起
来一切正常。
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

#: HTTP 恢复轮询间隔（秒）。WSS 通知只用于提前唤醒，不替代持久补拉。
DEFAULT_POLL_SECONDS = 2.0
#: 请求超时（秒）。必须小于中心侧的会话/租约期限，否则本地超时会掩盖真实的租约过期。
DEFAULT_TIMEOUT_SECONDS = 10.0
#: 指数退避上限（秒）。断网期间不能无限拉长，否则恢复后要等很久才重连。
DEFAULT_MAX_BACKOFF_SECONDS = 30.0


LAUNCHER_HTTP_BASE_URL_ENV = "UNILABOS_LAUNCHER_CENTER_HTTP_BASE_URL"


class CenterConfigError(RuntimeError):
    """配置缺失或不合法。"""


def inventory_sync_enabled(environ: dict[str, str] | None = None) -> bool:
    """接入中心后默认同步库存；显式关闭供独立部署使用。"""
    source = os.environ if environ is None else environ
    value = source.get("CENTER_INVENTORY_ENABLED", "1")
    if value not in {"0", "1"}:
        raise CenterConfigError("CENTER_INVENTORY_ENABLED 必须为 0 或 1")
    return value == "1"


def _normalize_http_base_url(value: str) -> str:
    """规范工站对中心可访问的 HTTP origin。"""

    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or not value or parsed.scheme != "http" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or "?" in value or "#" in value
                or any(c.isspace() for c in value) or not value.isascii()
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError("地址不是 HTTP origin")
    except (ValueError, TypeError, AttributeError) as error:
        raise CenterConfigError("CENTER_HTTP_BASE_URL 必须为无路径、凭据或查询参数的 HTTP 地址") from error
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_public_http_base_url(value: str) -> str:
    """校验浏览器访问地址；公网入口允许 HTTP 或 HTTPS，不替代内网地址。"""
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or not value
                or parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or any(c in value for c in ("?", "#", "\\"))
                or any(c.isspace() for c in value) or not value.isascii()
                or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError("地址不是 HTTP/HTTPS origin")
    except (ValueError, TypeError, AttributeError) as error:
        raise CenterConfigError(
            "CENTER_PUBLIC_HTTP_BASE_URL 必须为无路径、凭据、查询参数或片段的 HTTP/HTTPS 地址"
        ) from error
    return f"{parsed.scheme}://{parsed.netloc}"


def _read_file(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CenterConfigError(f"{what} 无法读取：{path}（{error}）") from error


@dataclass(frozen=True)
class CapacityLimits:
    outbox_high_water_events: int = 10000
    outbox_high_water_bytes: int = 64 * 1024 * 1024
    disk_free_reserve_bytes: int = 256 * 1024 * 1024
    result_reserve_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if (not all(type(value) is int and 0 < value <= 2**53 - 1 for value in (
                self.outbox_high_water_events, self.outbox_high_water_bytes,
                self.disk_free_reserve_bytes, self.result_reserve_bytes))
                or self.outbox_high_water_events < 2
                or self.result_reserve_bytes > self.outbox_high_water_bytes):
            raise CenterConfigError("积压高水位和结果预留量必须为有效正整数，且至少能容纳一次领取")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> CapacityLimits:
        source = os.environ if environ is None else environ
        defaults = cls()
        return cls(**{name: int(source.get(f"CENTER_{name.upper()}", str(getattr(defaults, name))))
                      for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class CenterClientConfig:
    """连接中心所需的全部材料。"""

    #: 中心 mTLS 端点，例如 ``https://127.0.0.1:8443``。必须是 https：
    #: 明文端点不会提供客户端证书，身份就无从谈起。
    endpoint: str
    #: 工站客户端证书与私钥（PEM）。
    client_cert: Path
    client_key: Path
    #: 信任的中心 CA（PEM）。用于验证服务端证书。
    server_ca: Path | None
    #: 中心侧注册时分配的实例身份。用于组装请求头，并在本地记录里标注归属。
    instance_uuid: str
    #: 工站身份。仅用于本地校验与日志；中心侧的身份来自证书，不采信这个值。
    station_uuid: str
    # 仅兼容旧配置；机器接口现在只使用 mTLS。
    machine_secret: str = ""
    poll_seconds: float = DEFAULT_POLL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    websocket_enabled: bool = True
    capacity: CapacityLimits = CapacityLimits()
    #: 工站自身 HTTP 服务的可访问 origin；通过 WSS hello 上报给中心。
    http_base_url: str | None = None
    #: 浏览器使用的公网 origin；只上报给中心展示，不参与工站控制连接。
    public_http_base_url: str | None = None
    local_recovery_max_attempts: int = 3
    lab_uuid: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise CenterConfigError(
                "Center 端点必须是 https：明文端点不提供客户端证书，无法建立工站身份"
            )
        for name, path in (
            ("客户端证书", self.client_cert),
            ("客户端私钥", self.client_key),
            ("中心 CA", self.server_ca),
        ):
            if path is not None and not path.is_file():
                raise CenterConfigError(f"{name}不存在：{path}")
        try:
            uuid.UUID(self.instance_uuid)
            uuid.UUID(self.station_uuid)
        except (ValueError, TypeError, AttributeError) as error:
            raise CenterConfigError("工站和实例身份必须为有效 UUID") from error
        if self.lab_uuid is not None:
            try:
                if str(uuid.UUID(self.lab_uuid)) != self.lab_uuid:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as error:
                raise CenterConfigError("CENTER_LAB_UUID 必须为规范 UUID") from error
        if self.http_base_url is not None:
            object.__setattr__(
                self, "http_base_url", _normalize_http_base_url(self.http_base_url)
            )
        if self.public_http_base_url is not None:
            object.__setattr__(
                self, "public_http_base_url", _normalize_public_http_base_url(self.public_http_base_url)
            )
        if not all(math.isfinite(value) and value > 0 for value in (
                self.poll_seconds, self.timeout_seconds, self.max_backoff_seconds)):
            raise CenterConfigError("轮询间隔与超时必须为正数")
        if self.max_backoff_seconds < self.poll_seconds:
            raise CenterConfigError("退避上限不得小于轮询间隔")
        if type(self.local_recovery_max_attempts) is not int or not 1 <= self.local_recovery_max_attempts <= 100:
            raise CenterConfigError("单调用本地恢复总尝试次数必须为 1 至 100 的整数")
        if not isinstance(self.capacity, CapacityLimits):
            raise CenterConfigError("容量配置类型无效")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "CenterClientConfig":
        """从环境变量与证书文件装配配置。

        变量名与部署脚本一致，便于在容器里注入：
        ``CENTER_ENDPOINT`` / ``CENTER_CLIENT_CERT`` / ``CENTER_CLIENT_KEY`` /
        ``CENTER_SERVER_CA`` / ``CENTER_MACHINE_SECRET`` / ``CENTER_INSTANCE_UUID`` /
        ``CENTER_STATION_UUID`` / ``CENTER_HTTP_BASE_URL`` / ``CENTER_PUBLIC_HTTP_BASE_URL``。
        """
        source = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return str(source.get(name, default)).strip()

        endpoint = get("CENTER_ENDPOINT")
        if not endpoint:
            raise CenterConfigError("缺少必需的环境变量 CENTER_ENDPOINT")

        def path(name: str) -> Path:
            value = get(name)
            if not value:
                raise CenterConfigError(f"缺少必需的环境变量 {name}")
            return Path(value)

        return cls(
            endpoint=endpoint.rstrip("/"),
            websocket_enabled=get("CENTER_WSS_ENABLED", "1") == "1",
            client_cert=path("CENTER_CLIENT_CERT"),
            client_key=path("CENTER_CLIENT_KEY"),
            server_ca=path("CENTER_SERVER_CA") if get("CENTER_SERVER_CA") else None,
            machine_secret=get("CENTER_MACHINE_SECRET"),
            instance_uuid=get("CENTER_INSTANCE_UUID"),
            station_uuid=get("CENTER_STATION_UUID"),
            lab_uuid=get("CENTER_LAB_UUID") or None,
            http_base_url=get(LAUNCHER_HTTP_BASE_URL_ENV) or get("CENTER_HTTP_BASE_URL") or None,
            public_http_base_url=get("CENTER_PUBLIC_HTTP_BASE_URL") or None,
            poll_seconds=float(get("CENTER_POLL_SECONDS") or DEFAULT_POLL_SECONDS),
            timeout_seconds=float(get("CENTER_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS),
            max_backoff_seconds=float(
                get("CENTER_MAX_BACKOFF_SECONDS") or DEFAULT_MAX_BACKOFF_SECONDS
            ),
            local_recovery_max_attempts=int(get("CENTER_LOCAL_RECOVERY_MAX_ATTEMPTS") or 3),
            capacity=CapacityLimits.from_env(source),
        )

    def certificates(self) -> tuple[str, str]:
        """返回 (cert, key) 的 PEM 文本，直接交给请求库。"""
        return (
            _read_file(self.client_cert, "客户端证书"),
            _read_file(self.client_key, "客户端私钥"),
        )

    def ca_bundle(self) -> str:
        return _read_file(self.server_ca, "中心 CA") if self.server_ca else ""

    def __repr__(self) -> str:  # pragma: no cover - 仅为避免日志泄漏
        # 机器密钥与私钥路径不得出现在日志里。
        return (
            f"CenterClientConfig(endpoint={self.endpoint!r}, "
            f"station_uuid={self.station_uuid!r}, instance_uuid={self.instance_uuid!r})"
        )

    __str__ = __repr__
