"""接入生命周期测试：真实 CSR/证书，模拟网络故障，不重建本地工作流。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.center_client.connection import CenterConnection, ConnectionError
from unilabos.app.center_client.connection_api import create_router


class Runtime:
    def __init__(self, config):
        self.config = config
        self.session = object()
        self.starts = 0
        self.reloads = 0
        self.closed = False

    def start(self):
        self.starts += 1

    def reload_credentials(self, config, *, recover_control=False):
        self.config = config
        self.reloads += 1

    def close(self):
        self.closed = True


class Center:
    def __init__(self):
        self.station, self.instance = str(uuid4()), str(uuid4())
        self.lab = str(uuid4())
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.requests = []
        self.responses = {}
        self.offline = False
        self.lose_response = False
        self.expires_days = 30

    def __call__(self, endpoint, path, body, **kwargs):
        if self.offline:
            raise ConnectionError("CENTER_UNREACHABLE", True)
        if body is None:
            return {"station_uuid": self.station, "instance_uuid": self.instance,
                    "lab_uuid": self.lab, "http_reachability": "unknown"}
        self.requests.append((path, body.copy()))
        csr = body["csr_pem"]
        if csr not in self.responses:
            request = x509.load_pem_x509_csr(csr.encode())
            builder = (x509.CertificateBuilder().subject_name(request.subject)
                       .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Center")]))
                       .public_key(request.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(self.now).not_valid_after(self.now + timedelta(days=self.expires_days))
                       .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False))
            for suffix, value in (("1", self.station), ("2", self.instance)):
                builder = builder.add_extension(x509.UnrecognizedExtension(
                    ObjectIdentifier("1.3.6.1.4.1.57264.1." + suffix), value.encode()), False)
            cert = builder.sign(self.key, hashes.SHA256())
            self.responses[csr] = {"station_uuid": self.station, "instance_uuid": self.instance,
                                   "lab_uuid": self.lab,
                                   "credential_uuid": str(uuid4()), "certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                                   "ca_pem": "unused", "machine_endpoint": "https://center.test:8443",
                                   "not_before_ms": int(self.now.timestamp() * 1000),
                                   "not_after_ms": int((self.now + timedelta(days=self.expires_days)).timestamp() * 1000)}
        if self.lose_response:
            self.lose_response = False
            raise ConnectionError("CENTER_UNREACHABLE", True)
        return self.responses[csr]


def test_station_first_retries_same_key_across_restart_and_discards_code(tmp_path):
    center = Center()
    first = CenterConnection(tmp_path / "connection", Runtime, request=center)
    first.connect("https://center.test", "a" * 64)
    center.offline = True
    with pytest.raises(ConnectionError):
        first.step()
    assert first.runtime is None
    center.offline = False
    center.lose_response = True
    with pytest.raises(ConnectionError):
        first.step()
    second = CenterConnection(tmp_path / "connection", Runtime, request=center)
    second.step()
    assert center.requests[0] == center.requests[1]
    assert second.status()["state"] == "connected"
    assert second.runtime.starts == 1
    assert not (second.directory / "pending.json").exists()
    assert "enrollment_code" not in str(second.status())
    assert "PRIVATE KEY" not in str(second.status())
    third = CenterConnection(second.directory, Runtime, request=center)
    third.step()
    assert len(center.requests) == 2
    assert third.status()["station_uuid"] == center.station
    assert third.runtime.config.lab_uuid == center.lab
    if __import__('os').name != "nt":
        assert (third.runtime.config.client_key.stat().st_mode & 0o777) == 0o600


def test_renewal_preserves_runtime_session_and_replays_original_csr(tmp_path):
    center = Center()
    owner = CenterConnection(tmp_path / "connection", Runtime, request=center, clock=lambda: center.now.timestamp())
    owner.connect("https://center.test", "a" * 64)
    owner.step()
    runtime, session, old_key = owner.runtime, owner.runtime.session, owner.runtime.config.client_key
    center.now += timedelta(days=24)
    center.lose_response = True
    with pytest.raises(ConnectionError):
        owner.step()
    owner.step()
    assert center.requests[-1] == center.requests[-2]
    assert owner.runtime is runtime and runtime.session is session
    assert runtime.starts == 1 and runtime.reloads == 1
    assert runtime.config.client_key != old_key
    assert not (owner.directory / "renewal.json").exists()


def test_recovery_rejects_another_station_and_expired_identity_does_not_register(tmp_path):
    center = Center()
    owner = CenterConnection(tmp_path / "connection", Runtime, request=center, clock=lambda: center.now.timestamp())
    owner.connect("https://center.test", "a" * 64)
    owner.step()
    runtime = owner.runtime
    center.now += timedelta(days=31)
    with pytest.raises(ConnectionError, match="CREDENTIAL_EXPIRED"):
        owner.step()
    assert len(center.requests) == 1
    owner.connect("https://center.test", "b" * 64)
    center.station = str(uuid4())
    with pytest.raises(ConnectionError, match="INVALID_CENTER_CERTIFICATE"):
        owner.step()
    assert owner.runtime is runtime and runtime.reloads == 0


def test_local_api_requires_explicit_local_request_and_returns_only_public_status(tmp_path):
    owner = CenterConnection(tmp_path / "connection", Runtime, request=Center())
    app = FastAPI()
    app.include_router(create_router(lambda: owner))
    client = TestClient(app)
    payload = {"center_url": "https://center.test", "enrollment_code": "a" * 64}
    assert client.post("/api/v1/center-connection", json=payload).status_code == 403
    assert client.post("/api/v1/center-connection", json=payload,
                       headers={"x-unilab-local-request": "1", "sec-fetch-site": "cross-site"}).status_code == 403
    response = client.post("/api/v1/center-connection", json=payload, headers={"x-unilab-local-request": "1"})
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "waiting"
    assert "a" * 64 not in response.text
    assert client.get("/api/v1/center-connection").json()["data"]["http_reachability"] == "unknown"


def test_disk_failure_never_publishes_unpersisted_registration(tmp_path, monkeypatch):
    center = Center()
    owner = CenterConnection(tmp_path / "connection", Runtime, request=center)
    def full_disk(*args):
        raise OSError("disk full")
    monkeypatch.setattr(owner, "_write", full_disk)
    with pytest.raises(OSError):
        owner.connect("https://center.test", "a" * 64)
    owner.step()
    assert not center.requests
    assert not (owner.directory / "pending.json").exists()


def test_default_runtime_storage_keeps_identity_and_same_database_after_enabling_center(tmp_path):
    from unilabos.app.center_client.connection import connection_runtime
    from unilabos.app.runtime_storage import close_runtime_storage_session, prepare_runtime_storage_session
    close_runtime_storage_session()
    try:
        paths = prepare_runtime_storage_session({}, working_dir=str(tmp_path))
        database = Path(paths.workflow_history_db)
        database.write_bytes(b"same database with existing task facts")
        first = connection_runtime(database, None, None)
        first.connect("https://center.test", "a" * 64)
        original = (first.directory / "pending.json").read_bytes()
        first.close()
        close_runtime_storage_session()
        again = prepare_runtime_storage_session({}, working_dir=str(tmp_path))
        assert again.workflow_history_db == str(database)
        assert database.read_bytes() == b"same database with existing task facts"
        second = connection_runtime(Path(again.workflow_history_db), None, None)
        assert second.directory == tmp_path / "center-connection"
        assert (second.directory / "pending.json").read_bytes() == original
        second.close()
    finally:
        close_runtime_storage_session()


def test_exchange_preserves_center_error_and_retries_renewal_window(monkeypatch):
    import json

    import requests

    from unilabos.app.center_client.connection import exchange
    class Response:
        status_code = 409
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            yield json.dumps({"code": "RENEWAL_NOT_DUE", "message": "later"}).encode()
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, *args, **kwargs): return Response()
    monkeypatch.setattr(requests, "Session", Session)
    with pytest.raises(ConnectionError) as raised:
        exchange("https://center.test", "/renew", {"csr_pem": "same request"}, ca=None)
    assert raised.value.code == "RENEWAL_NOT_DUE"
    assert raised.value.retryable


def test_legacy_station_can_migrate_using_management_url_without_changing_identity(tmp_path):
    center = Center()
    source = CenterConnection(tmp_path / "original", Runtime, request=center)
    source.connect("https://center.test", "a" * 64)
    source.step()
    legacy = CenterConnection(tmp_path / "legacy", Runtime, runtime=source.runtime, request=center)
    legacy.step()
    assert legacy.status()["state"] == "connected"
    assert legacy.status()["center_url"] == ""
    assert legacy.status()["station_uuid"] == center.station
    assert not legacy.status()["automatic_renewal"]
    legacy.connect("https://management.center.test", "b" * 64)
    legacy.step()
    assert legacy.status()["center_url"] == "https://management.center.test"
    assert legacy.status()["station_uuid"] == center.station
    assert legacy.status()["automatic_renewal"]
    assert center.requests[-1][1]["expected_station_uuid"] == center.station
    assert center.requests[-1][1]["expected_instance_uuid"] == center.instance


def test_pairing_and_restart_keep_deployment_station_addresses(tmp_path, monkeypatch):
    """动态接入保留部署的内网查询地址和浏览器入口，显式内网地址仍优先。"""
    monkeypatch.setenv("CENTER_HTTP_BASE_URL", "http://station-internal:18003")
    monkeypatch.setenv("CENTER_PUBLIC_HTTP_BASE_URL", "https://station.example:30502")
    center = Center()
    owner = CenterConnection(tmp_path / "connection", Runtime, request=center)
    owner.connect("https://center.test", "a" * 64)
    owner.step()
    assert owner.runtime.config.http_base_url == "http://station-internal:18003"
    assert owner.runtime.config.public_http_base_url == "https://station.example:30502"
    restored = CenterConnection(owner.directory, Runtime, request=center)
    restored.step()
    assert restored.runtime.config == owner.runtime.config
    explicit_center = Center()
    explicit = CenterConnection(tmp_path / "explicit", Runtime, request=explicit_center)
    explicit.connect("https://center.test", "b" * 64, http_base_url="http://explicit:18003")
    explicit.step()
    assert explicit.runtime.config.http_base_url == "http://explicit:18003"
    assert explicit.runtime.config.public_http_base_url == "https://station.example:30502"

    # 已接入的工站用启动器的新地址恢复连接，无需再次配对。
    monkeypatch.setenv("UNILABOS_LAUNCHER_CENTER_HTTP_BASE_URL", "http://launcher-station:8002/")
    restored = CenterConnection(explicit.directory, Runtime, request=explicit_center)
    restored.step()
    assert restored.status()["state"] == "connected"
    assert restored.runtime.config.http_base_url == "http://launcher-station:8002"
    assert restored.status()["http_base_url"] == restored.runtime.config.http_base_url
