"""真实 mTLS 套接字验证永久拒绝和恢复；服务端只模拟中心精确事件回执。"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, event_ack
from tests.app.center_client.test_event_envelope import draft, store_at
from tests.app.center_client.test_runtime import Service
from unilabos.app.center_client.client import CenterClient, ControlSession
from unilabos.app.center_client.config import CenterClientConfig
from unilabos.app.center_client.runtime import CenterRuntime
from unilabos.app.center_client.transport import CenterTransport, CenterTransportError


def test_real_mtls_wss_statistics_include_http_rejection_and_reconnect(tmp_path, tls_receiver):
    from dataclasses import replace

    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    from tests.app.center_client.test_runtime import Center
    from tests.app.center_client.test_websocket import hello
    from unilabos.app.center_client.websocket import NotificationReceiver

    cfg, _http_state = tls_receiver
    store = store_at(tmp_path / "wss-capacity.db")
    client = CenterClient(cfg, Center())
    client.open_session(store.event_epoch_uuid)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cfg.client_cert, cfg.client_key)
    context.load_verify_locations(cfg.server_ca)
    context.verify_mode = ssl.CERT_REQUIRED

    async def scenario():
        attempts, completed = [], []
        receiver = None

        async def process_request(connection, request):
            assert connection.transport.get_extra_info("ssl_object").getpeercert()
            attempts.append(request.path)
            if len(attempts) == 1:
                return Response(503, "Busy", Headers({"Retry-After": "0"}), b"")
            return None

        async def handle(connection):
            greeting = json.loads(await connection.recv())
            assert greeting["type"] == "hello"
            await connection.send(hello(client.session))
            async with asyncio.timeout(3):
                while not receiver.statistics()["connected"]:
                    await asyncio.sleep(0.005)
            completed.append(receiver.statistics())
            if len(completed) == 2:
                receiver._stop.set()
            await connection.close()

        async with serve(handle, "127.0.0.1", 0, ssl=context, process_request=process_request) as server:
            endpoint = f"https://localhost:{server.sockets[0].getsockname()[1]}"
            receiver = NotificationReceiver(replace(cfg, endpoint=endpoint, poll_seconds=0.01, max_backoff_seconds=0.01), client, store, lambda: None)
            await asyncio.wait_for(receiver._run_async(), 10)
        status = receiver.statistics()
        assert len(attempts) == 3 and len(completed) == 2
        assert (status["connect_attempts"], status["connect_successes"], status["connect_failures"]) == (3, 2, 1)
        assert (status["reconnect_attempts"], status["reconnect_successes"], status["reconnect_failures"]) == (1, 1, 0)
        assert status["reconnect_completed_success_ratio"] == 1.0
        assert not status["connected"] and not status["connect_in_flight"]

    try:
        asyncio.run(scenario())
    finally:
        store.close()


@pytest.fixture
def tls_receiver(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "tls.pem", tmp_path / "tls.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key_file.chmod(0o600)
    state = {"status": 413, "received": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.connection.getpeercert(), "测试必须携带已验证客户端证书"
            assert self.path.endswith("/events")
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["received"].append(event)
            status = state["status"]
            response = event_ack(event) if status == 202 else {"code": "CAPACITY_REJECTED"}
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if status == 503:
                self.send_header("Retry-After", "60")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    context.load_verify_locations(cert_file)
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    cfg = CenterClientConfig(endpoint=f"https://localhost:{server.server_port}", client_cert=cert_file,
        client_key=key_file, server_ca=cert_file, machine_secret="a" * 64,
        instance_uuid=INSTANCE, station_uuid=STATION, websocket_enabled=False)
    try:
        yield cfg, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_real_mtls_413_retains_event_until_review_then_replays_identical_body(tmp_path, tls_receiver):
    cfg, server = tls_receiver
    path = tmp_path / "network-events.db"
    store = store_at(path)
    event = store.record_event(INVOCATION, terminal=False, body=draft())

    def build(current):
        client = CenterClient(cfg)
        client.restore_evidence_session(ControlSession(str(uuid4()), str(uuid4()), 1, STATION, INSTANCE))
        return CenterRuntime(cfg, client, current, Service())

    run = build(store)
    run._flush()
    held = store.request_holds()[0]
    run.close()
    store = store_at(path)
    run = build(store)
    try:
        run._flush()
        assert server["received"] == [event]
        assert store.capacity_backlog()["pending_events"] == 1
        server["status"] = 202
        store.request_retry(held["request_digest"], expected_attempts=1, reason="已修正测试中心限额")
        run._flush()
        assert server["received"] == [event, event]
        assert store.capacity_backlog()["pending_events"] == 0
    finally:
        run.close()


def test_real_mtls_retry_after_prevents_an_early_second_network_request(tls_receiver):
    cfg, server = tls_receiver
    server["status"] = 503
    client = CenterTransport(cfg)
    for _ in range(2):
        with pytest.raises(CenterTransportError) as error:
            client.request("POST", "/events", json_body={"original": True})
        assert error.value.retryable
        assert error.value.retry_after_seconds > 59
    assert server["received"] == [{"original": True}]
