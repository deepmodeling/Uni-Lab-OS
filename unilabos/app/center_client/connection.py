"""中心连接的持久生命周期：接入码只用来换证书，重连与续期不重建工作流。"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .config import (
    LAUNCHER_HTTP_BASE_URL_ENV,
    CenterClientConfig,
    CenterConfigError,
    _normalize_http_base_url,
    inventory_sync_enabled,
)
from .console_address import LAUNCHER_CONSOLE_URL_ENV, ConsoleAddressSettings, console_origin
from .credential_files import create_private_directory, sync_directory

logger = logging.getLogger(__name__)


def write_private(path: Path, content: str) -> None:
    # 未启用连接时不强制加载可选密码学依赖。
    from .enrollment import write_private as write
    write(path, content)


def generate_key_and_csr(name: str):
    from .enrollment import generate_key_and_csr as generate
    return generate(name)


RENEW_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


class ConnectionError(RuntimeError):
    """只携带可展示的错误码，不回显凭据或远端正文。"""
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("中心地址必须是 HTTPS 地址，不能包含路径、用户名或查询参数")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("中心地址端口无效") from error
    return value.rstrip("/")


def exchange(endpoint: str, path: str, body: dict | None, *, ca: Path | None,
             certificate: tuple[str, str] | None = None) -> dict:
    """TLS 始终验证服务端；拒绝重定向，限制响应体。"""
    import requests
    try:
        with requests.Session() as session:
            with session.request("GET" if body is None else "POST", endpoint + path, json=body,
                                 cert=certificate, verify=str(ca) if ca else True,
                                 timeout=(5, 10), allow_redirects=False, stream=True) as response:
                chunks, size = [], 0
                for chunk in response.iter_content(8192):
                    size += len(chunk)
                    if size > 256 * 1024:
                        raise ConnectionError("INVALID_CENTER_RESPONSE")
                    chunks.append(chunk)
                try:
                    result = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    result = {}
                if response.status_code >= 400:
                    code = result.get("code", "CENTER_REQUEST_FAILED") if isinstance(result, dict) else "CENTER_REQUEST_FAILED"
                    allowed = {"ENROLLMENT_EXPIRED", "ENROLLMENT_CONSUMED", "ENROLLMENT_IDENTITY_MISMATCH", "CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED",
                               "AUTH_INVALID", "MACHINE_ENDPOINT_NOT_CONFIGURED", "CREDENTIAL_ALREADY_ROTATED", "RENEWAL_NOT_DUE"}
                    raise ConnectionError(code if code in allowed else "CENTER_REQUEST_FAILED",
                                          response.status_code >= 500 or response.status_code == 429 or code == "RENEWAL_NOT_DUE")
                if not 200 <= response.status_code < 300 or not isinstance(result, dict):
                    raise ConnectionError("INVALID_CENTER_RESPONSE")
                return result
    except requests.exceptions.SSLError as error:
        raise ConnectionError("CENTER_TLS_UNTRUSTED") from error
    except requests.RequestException as error:
        raise ConnectionError("CENTER_UNREACHABLE", True) from error


def validate_certificate(response: dict, key_pem: str, identity: tuple[str, str] | None, now_ms: int) -> None:
    """在发布凭据之前独立验证公钥、证书用途、身份与实际有效期。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, ObjectIdentifier
    try:
        station, instance = str(UUID(response["station_uuid"])), str(UUID(response["instance_uuid"]))
        if identity is not None and (station, instance) != identity:
            raise ValueError("工站身份不一致")
        cert = x509.load_pem_x509_certificate(response["certificate_pem"].encode())
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        def spki(public_key: Any) -> bytes:
            return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if spki(cert.public_key()) != spki(key.public_key()):
            raise ValueError("证书公钥不一致")
        # 中心 OID 内容是规范 UUID 的 UTF-8 字节，与签发模块一致。
        for suffix, value in (("1", station), ("2", instance)):
            extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.57264.1." + suffix)).value
            if extension.value != value.encode():
                raise ValueError("证书身份扩展不一致")
        if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise ValueError("证书用途无效")
        before = int(cert.not_valid_before_utc.timestamp() * 1000)
        after = int(cert.not_valid_after_utc.timestamp() * 1000)
        if before > now_ms + 60_000 or after <= now_ms or abs(after - response["not_after_ms"]) >= 1000:
            raise ValueError("证书有效期无效")
        https_origin(response["machine_endpoint"])
    except (KeyError, ValueError, TypeError, AttributeError, x509.ExtensionNotFound) as error:
        raise ConnectionError("INVALID_CENTER_CERTIFICATE") from error


class CenterConnection:
    """同一工站一个连接所有者；网络操作串行，状态查询不等待网络。"""
    def __init__(self, directory: Path, runtime_factory: Callable[[CenterClientConfig], Any], *,
                 runtime: Any = None, request: Callable = exchange, clock: Callable = time.time,
                 retain_runtime: Callable[[], None] = lambda: None,
                 prepare_runtime: Callable[[Any], Any] = lambda runtime: runtime):
        self.directory = Path(directory)
        if not self.directory.exists():
            create_private_directory(self.directory)
        self.runtime, self._factory, self._request, self._clock = runtime, runtime_factory, request, clock
        self._retain_runtime = retain_runtime
        self._prepare_runtime = prepare_runtime
        self._runtime_started = runtime is not None and getattr(runtime, "_thread", None) is not None
        self._lock, self._operation = threading.RLock(), threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._storage_error = False
        self._legacy_public_url = getattr(getattr(runtime, "config", None), "public_http_base_url", None)
        self._console_error = False
        self._browser_current = False
        try:
            self._console_settings = ConsoleAddressSettings.from_record(self._read("console-address.json") or {})
        except (OSError, ValueError, TypeError):
            self._console_settings = ConsoleAddressSettings()
            self._console_error = True
        try:
            self._record = self._read("connection.json")
            self._pending = self._read("pending.json")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._record, self._pending = None, None
            self._storage_error = True
        self._status = "connecting" if self._record or runtime else "waiting" if self._pending else "unconfigured"
        self._error: str | None = None
        self._error_message: str | None = None
        self._last_connected_ms: int | None = None
        self._reachability = "unknown"
        if self._storage_error:
            self._status, self._error = "error", "LOCAL_IDENTITY_UNREADABLE"

    def _read(self, name: str) -> dict | None:
        path = self.directory / name
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            raise ValueError("连接记录格式无效")
        if name == "connection.json":
            UUID(record["generation"])
            UUID(record["station_uuid"])
            UUID(record["instance_uuid"])
            if record.get("lab_uuid") is not None:
                try:
                    self._validate_lab(record["lab_uuid"])
                except ConnectionError as error:
                    raise ValueError("保存的实验室身份无效") from error
            https_origin(record["center_url"])
            https_origin(record["machine_endpoint"])
            if type(record["not_after_ms"]) is not int:
                raise ValueError("凭据有效期无效")
        return record

    def _write(self, name: str, record: dict) -> None:
        write_private(self.directory / name, json.dumps(record, ensure_ascii=False))
        sync_directory(self.directory)

    def _http_base_url(self, record: dict) -> str | None:
        value = (os.environ.get(LAUNCHER_HTTP_BASE_URL_ENV) or record.get("http_base_url")
                 or os.environ.get("CENTER_HTTP_BASE_URL"))
        return value or None

    def status(self) -> dict:
        with self._lock:
            record = self._record or self._pending or {}
            if not self._record and self.runtime:
                record = dict(record, station_uuid=self.runtime.config.station_uuid,
                              instance_uuid=self.runtime.config.instance_uuid,
                              center_url=record.get("center_url", ""),
                              not_after_ms=self._legacy_expiry())
            return {"state": self._status, "center_url": record.get("center_url", ""),
                    "station_uuid": record.get("station_uuid"), "instance_uuid": record.get("instance_uuid"),
                    "not_after_ms": record.get("not_after_ms"), "error_code": self._error,
                    "error_message": self._error_message if self._error else None,
                    "lab_uuid": record.get("lab_uuid"),
                    "last_connected_ms": self._last_connected_ms, "http_reachability": self._reachability,
                    "http_base_url": (self.runtime.config.http_base_url if self.runtime else self._http_base_url(record)),
                    "automatic_renewal": bool(self._record), "console_address": self.console_address_status()}

    def _resolved_console_address(self) -> dict:
        if self._console_error:
            raise ConnectionError("LOCAL_CONSOLE_ADDRESS_UNREADABLE")
        return self._console_settings.resolve(
            os.environ.get("CENTER_PUBLIC_HTTP_BASE_URL") or self._legacy_public_url,
            os.environ.get(LAUNCHER_CONSOLE_URL_ENV),
            browser_current=self._browser_current,
        )

    def console_address_status(self) -> dict:
        with self._lock:
            try:
                result = self._resolved_console_address()
            except (ConnectionError, ValueError):
                return {"mode": "auto", "custom_url": None, "automatic_url": None,
                        "automatic_source": "none", "effective_url": None, "source": "none",
                        "sync_state": "error", "error_code": "LOCAL_CONSOLE_ADDRESS_UNREADABLE"}
            acknowledged = getattr(self.runtime, "public_address_synced", lambda _: False)
            return {**result, "sync_state": "synced" if acknowledged(result["effective_url"]) else "pending",
                    "error_code": None}

    def _runtime_console_url(self) -> str | None:
        try:
            return self._resolved_console_address()["effective_url"]
        except (ConnectionError, ValueError):
            # 操作台配置损坏单独报错，不能阻断原工站的后端连接和续证。
            return self.runtime.config.public_http_base_url if self.runtime else None

    def _save_console_address(self, custom_url: str | None, browser_url: str | None = None) -> None:
        if self._console_error:
            raise ConnectionError("LOCAL_CONSOLE_ADDRESS_UNREADABLE")
        settings = ConsoleAddressSettings(console_origin(custom_url),
                                          console_origin(browser_url) or self._console_settings.browser_url)
        browser_current = self._browser_current or bool(console_origin(browser_url))
        settings.resolve(os.environ.get("CENTER_PUBLIC_HTTP_BASE_URL") or self._legacy_public_url,
                         os.environ.get(LAUNCHER_CONSOLE_URL_ENV), browser_current=browser_current)
        self._retain_runtime()
        self._write("console-address.json", asdict(settings))
        # 写盘成功后才发布；更新候选不能改变自定义覆盖。
        self._console_settings = settings
        self._browser_current = browser_current

    def save_console_address(self, custom_url: str | None, browser_url: str | None = None) -> dict:
        with self._lock:
            if self._stop.is_set():
                raise ConnectionError("CONNECTION_CLOSED")
            self._save_console_address(custom_url, browser_url)
        self._wake.set()
        return self.console_address_status()

    def _legacy_expiry(self) -> int | None:
        if self.runtime is None:
            return None
        from cryptography import x509
        try:
            cert = x509.load_pem_x509_certificate(self.runtime.config.client_cert.read_bytes())
            return int(cert.not_valid_after_utc.timestamp() * 1000)
        except (OSError, ValueError):
            return None

    def connect(self, center_url: str, enrollment_code: str, *, server_ca_pem: str = "",
                http_base_url: str | None = None, console_address: dict | None = None) -> dict:
        center_url = https_origin(center_url.strip())
        if re.fullmatch(r"[0-9a-f]{64}", enrollment_code.strip()) is None:
            raise ValueError("接入码必须是中心生成的 64 位小写十六进制字符串")
        if http_base_url:
            http_base_url = _normalize_http_base_url(http_base_url)
        if console_address is not None:
            ConsoleAddressSettings.from_record(console_address)
        if server_ca_pem:
            try:
                ssl.create_default_context(cadata=server_ca_pem)
            except ssl.SSLError as error:
                raise ValueError("可信 CA 必须为有效的 PEM 证书") from error
        with self._operation, self._lock:
            if self._storage_error:
                raise ConnectionError("LOCAL_IDENTITY_UNREADABLE")
            if self._stop.is_set():
                raise ConnectionError("CONNECTION_CLOSED")
            if self._record and center_url != self._record["center_url"]:
                raise ValueError("已绑定工站不能切换中心；请先由管理员核实身份")
            if self._pending and (self._pending["enrollment_code"] != enrollment_code.strip()
                                  or self._pending["center_url"] != center_url):
                if self._status not in {"recovery_required", "error"}:
                    raise ValueError("原接入请求尚未确定结果，请等待重试完成")
            if not self._pending or self._pending["enrollment_code"] != enrollment_code.strip():
                pair = generate_key_and_csr("uni-lab-station")
                pending = {"center_url": center_url, "enrollment_code": enrollment_code.strip(),
                           "key_pem": pair.private_key_pem, "csr_pem": pair.csr_pem}
            else:
                pending = dict(self._pending)
            if self._record:
                server_ca_pem = server_ca_pem or self._record.get("server_ca_pem", "")
                http_base_url = http_base_url or self._record.get("http_base_url")
            elif self.runtime:
                if not server_ca_pem and self.runtime.config.server_ca:
                    server_ca_pem = self.runtime.config.server_ca.read_text()
                http_base_url = http_base_url or self.runtime.config.http_base_url
            identity = self._identity()
            if identity is not None:
                pending["expected_station_uuid"], pending["expected_instance_uuid"] = identity
            pending.update(server_ca_pem=server_ca_pem, http_base_url=http_base_url)
            if console_address is not None:
                self._save_console_address(**console_address)
            self._retain_runtime()
            self._write("pending.json", pending)
            # 只有完整持久化的请求才能被后台线程看到和发送。
            self._pending = pending
            self._status, self._error = "waiting", None
        self._wake.set()
        return self.status()

    def retry(self) -> dict:
        with self._lock:
            if self._storage_error:
                raise ConnectionError("LOCAL_IDENTITY_UNREADABLE")
            self._error = None
            self._status = "waiting" if self._pending else "connecting" if self._record else "unconfigured"
        self._wake.set()
        return self.status()

    def _ca(self, record: dict) -> Path | None:
        pem = record.get("server_ca_pem", "")
        if not pem:
            return None
        # 每次尝试有独立信任文件，避免并发已运行传输看到部分覆盖。
        import hashlib
        path = self.directory / ("trust-" + hashlib.sha256(pem.encode()).hexdigest() + ".pem")
        if not path.exists():
            write_private(path, pem)
        return path

    def _config(self, record: dict) -> CenterClientConfig:
        folder = self.directory / record["generation"]
        return CenterClientConfig(endpoint=record["machine_endpoint"], client_cert=folder / "station.pem",
                                  client_key=folder / "station-key.pem", server_ca=self._ca(record),
                                  station_uuid=record["station_uuid"], instance_uuid=record["instance_uuid"],
                                  lab_uuid=record.get("lab_uuid"),
                                  http_base_url=self._http_base_url(record),
                                  public_http_base_url=self._runtime_console_url())

    def _identity(self) -> tuple[str, str] | None:
        if self._record:
            return self._record["station_uuid"], self._record["instance_uuid"]
        if self.runtime:
            return self.runtime.config.station_uuid, self.runtime.config.instance_uuid
        return None

    def _publish(self, response: dict, material: dict) -> None:
        validate_certificate(response, material["key_pem"], self._identity(), int(self._clock() * 1000))
        previous_lab = (self._record or {}).get("lab_uuid") or (
            self.runtime.config.lab_uuid if self.runtime is not None else None
        )
        lab = response.get("lab_uuid", previous_lab)
        if lab is not None:
            self._validate_lab(lab)
            if previous_lab and previous_lab != lab:
                raise ConnectionError("INVALID_CENTER_RESPONSE")
        generation = str(uuid4())
        folder = self.directory / generation
        create_private_directory(folder)
        write_private(folder / "station-key.pem", material["key_pem"])
        write_private(folder / "station.pem", response["certificate_pem"])
        sync_directory(folder)
        record = {key: response[key] for key in ("station_uuid", "instance_uuid", "credential_uuid", "not_after_ms", "machine_endpoint")}
        record.update(generation=generation, center_url=material["center_url"],
                      server_ca_pem=material.get("server_ca_pem", ""), http_base_url=material.get("http_base_url"))
        if lab is not None:
            record["lab_uuid"] = lab
        config = self._config(record)
        # 原子发布一个清单；崩溃只会选择完整旧代或完整新代证书和私钥。
        self._write("connection.json", record)
        with self._lock:
            self._record = record
        self._ensure_runtime(config, recover_control="enrollment_code" in material)
        for name in ("pending.json", "renewal.json"):
            (self.directory / name).unlink(missing_ok=True)
        sync_directory(self.directory)
        with self._lock:
            self._pending = None

    def _ensure_runtime(self, config: CenterClientConfig, *, recover_control: bool = False) -> None:
        if config.lab_uuid is None and inventory_sync_enabled():
            _, config = self._connection_details(config)
            # 旧中心兼容显式部署配置，不能自动生成或猜测实验室身份。
            if config.lab_uuid is None and os.environ.get("CENTER_LAB_UUID"):
                config = replace(config, lab_uuid=os.environ["CENTER_LAB_UUID"])
        if self.runtime is None:
            runtime = self._factory(config)
            try:
                runtime.start()
            except BaseException:
                runtime.close()
                raise
            self.runtime = runtime
            self._runtime_started = True
        elif recover_control:
            self.runtime.reload_credentials(config, recover_control=True)
        elif self.runtime.config != config:
            self.runtime.reload_credentials(config)

    @staticmethod
    def _validate_lab(lab: str) -> None:
        """中心身份必须使用规范 UUID，响应异常不能退回环境变量。"""
        try:
            if str(UUID(lab)) != lab:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise ConnectionError("INVALID_CENTER_RESPONSE") from error

    def _connection_details(self, config: CenterClientConfig) -> tuple[dict, CenterClientConfig]:
        """通过已有 mTLS 状态接口补齐旧连接身份，并原子保存供重启使用。"""
        response = self._request(config.endpoint, "/api/v1/stations/connection", None, ca=config.server_ca,
                                 certificate=(str(config.client_cert), str(config.client_key)))
        if (response.get("station_uuid"), response.get("instance_uuid")) != (config.station_uuid, config.instance_uuid):
            raise ConnectionError("INVALID_CENTER_RESPONSE")
        lab = response.get("lab_uuid")
        if lab is not None:
            self._validate_lab(lab)
            if config.lab_uuid is not None and config.lab_uuid != lab:
                raise CenterConfigError("CENTER_LAB_UUID 或已保存的实验室身份与中心登记不一致")
            config = replace(config, lab_uuid=lab)
            if self._record is not None and self._record.get("lab_uuid") != lab:
                record = dict(self._record, lab_uuid=lab)
                self._write("connection.json", record)
                with self._lock:
                    self._record = record
        return response, config

    def step(self) -> None:
        """一次可测试的生命周期推进；调用方负责错误状态与退避。"""
        with self._operation:
            if self._stop.is_set():
                return
            # 独立于配对/续证请求推进；中心不可达时也先让现有传输使用已保存的新地址。
            if self.runtime is not None:
                with self._lock:
                    public_url = self._runtime_console_url()
                if self.runtime.config.public_http_base_url != public_url:
                    self._ensure_runtime(replace(self.runtime.config, public_http_base_url=public_url))
            pending = self._pending
            if pending:
                body = {key: pending[key] for key in ("enrollment_code", "csr_pem", "expected_station_uuid", "expected_instance_uuid") if key in pending}
                response = self._request(pending["center_url"], "/api/v1/stations/pair", body, ca=self._ca(pending))
                self._publish(response, pending)
            record = self._record
            if record:
                if record["not_after_ms"] <= int(self._clock() * 1000):
                    raise ConnectionError("CREDENTIAL_EXPIRED")
                config = self._config(record)
                self._ensure_runtime(config)
                config = self.runtime.config
                if record["not_after_ms"] - int(self._clock() * 1000) <= RENEW_WINDOW_MS:
                    renewal = self._read("renewal.json")
                    if renewal is None or renewal.get("credential_uuid") != record["credential_uuid"]:
                        pair = generate_key_and_csr("uni-lab-station-renewal")
                        renewal = dict(record, key_pem=pair.private_key_pem, csr_pem=pair.csr_pem)
                        self._write("renewal.json", renewal)
                    response = self._request(config.endpoint, "/api/v1/stations/credentials/renew",
                                             {"csr_pem": renewal["csr_pem"]}, ca=config.server_ca,
                                             certificate=(str(config.client_cert), str(config.client_key)))
                    self._publish(response, renewal)
                    config = self._config(self._record)
                response, _ = self._connection_details(config)
                with self._lock:
                    self._reachability = response.get("http_reachability", "unknown")
                    self._status, self._error = "connected", None
                    self._last_connected_ms = int(self._clock() * 1000)
            elif self.runtime is not None:
                expiry = self._legacy_expiry()
                if expiry is not None and expiry <= int(self._clock() * 1000):
                    raise ConnectionError("CREDENTIAL_EXPIRED")
                response, config = self._connection_details(self.runtime.config)
                if self.runtime.config != config:
                    self.runtime.reload_credentials(config)
                if not self._runtime_started:
                    self._prepare_runtime(self.runtime)
                    self.runtime.start()
                    self._runtime_started = True
                with self._lock:
                    self._status, self._error = "connected", None
                    self._reachability = response.get("http_reachability", "unknown")
                    self._last_connected_ms = int(self._clock() * 1000)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="center-connection", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            with self._lock:
                blocked = self._status in {"recovery_required", "error"}
            if not blocked:
                try:
                    self.step()
                    delay = 15.0
                except ConnectionError as error:
                    with self._lock:
                        self._error = error.code
                        self._error_message = None
                        self._status = "waiting" if error.retryable else "recovery_required"
                    delay = min(60.0, delay * 2)
                except CenterConfigError as error:
                    logger.error("中心库存连接配置失败：%s", error)
                    with self._lock:
                        self._error, self._status = "CENTER_CONFIGURATION_ERROR", "error"
                        self._error_message = str(error)
                except Exception as error:
                    frames = traceback.extract_tb(error.__traceback__)
                    logger.error("中心连接初始化失败，异常类型=%s，位置=%s", type(error).__name__,
                                 " -> ".join(f"{frame.filename}:{frame.lineno}" for frame in frames))
                    with self._lock:
                        self._error, self._status = "LOCAL_CONNECTION_ERROR", "error"
                        self._error_message = "本地连接初始化失败，请检查工站日志中的异常位置"
            self._wake.wait(delay)
            self._wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise RuntimeError("中心连接管理器尚未退出")
        if self.runtime is not None:
            self.runtime.close()


def connection_runtime(database_path: Path, workflow_service: Any, workflow_store: Any) -> CenterConnection:
    import os

    def configure_inventory(runtime: Any) -> Any:
        if inventory_sync_enabled() and runtime.handoffs is None:
            from unilabos.app.scheduler.integration import get_inventory_service

            from .inventory_setup import configure_inventory as install
            inventory = get_inventory_service()
            if inventory is None:
                raise CenterConfigError("中心库存同步已启用，但本站库存服务尚未就绪")
            install(runtime, inventory.store, workflow_store)
        return runtime

    def factory(config: CenterClientConfig) -> Any:
        from .client import CenterClient
        from .durable import DurableCenterStore
        from .runtime import CenterRuntime
        store = DurableCenterStore(database_path, station_uuid=config.station_uuid, instance_uuid=config.instance_uuid)
        try:
            return configure_inventory(CenterRuntime(config, CenterClient(config), store, workflow_service))
        except BaseException:
            store.close()
            raise

    from unilabos.app.runtime_storage import get_runtime_working_directory, retain_runtime_storage_for_center
    stable_root = get_runtime_working_directory()
    directory = Path(stable_root) / "center-connection" if stable_root else Path(database_path).with_suffix(".center-connection")
    legacy = None
    if not (directory / "connection.json").exists() and os.environ.get("CENTER_ENABLED", "0") == "1":
        from .runtime import configured_runtime
        legacy = configured_runtime(database_path, workflow_service)
    if legacy:
        retain_runtime_storage_for_center()
    return CenterConnection(directory, factory, runtime=legacy, retain_runtime=retain_runtime_storage_for_center,
                            prepare_runtime=configure_inventory)
