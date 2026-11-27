"""Center 客户端的重试语义与身份规则。

这些测试盯的是**验收断言本身**：断网、重启、重复投递之后，同一个调用不能被启动两次。
中心侧靠"调用身份 + 事件身份"去重，因此工站侧的客户端必须保证：

- 重试重发的是**同一个请求体**，特别是 ``event_uuid`` 与 ``local_task_uuid`` 不能重算；
- 会话失效可以重新握手并重发，但请求内容一字不改；
- 会话被接管时必须**停下**，而不是抢回来——抢回来会让两台工站同时执行同一批调用；
- 中心报"事件冲突"时必须浮出水面，而不是被当成可重试的失败反复重发。

用一个假的传输层来做，这样每条规则都能被单独触发，而不必真的断网。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.center_client.client import (
    PROTOCOL_VERSION,
    CenterClient,
    CenterClientError,
    SessionFencedError,
)
from unilabos.app.center_client.config import CenterClientConfig, CenterConfigError
from unilabos.app.center_client.digest import DigestError, digest, is_digest
from unilabos.app.center_client.transport import CenterTransport, CenterTransportError

STATION = "11111111-1111-4111-8111-111111111111"
INSTANCE = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
EPOCH = "44444444-4444-4444-8444-444444444444"
INVOCATION = "55555555-5555-4555-8555-555555555555"
DIGEST = "sha256:" + "ab" * 32


EVENT = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
LOCAL_TASK = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def boundary_event(*, event_uuid=EVENT, local_task_uuid=LOCAL_TASK, event_type="succeeded",
                   payload_digest=DIGEST, output=None, occurred_at_ms=1, event_epoch_uuid=EPOCH,
                   event_sequence=1):
    from unilabos.app.center_client.events import make_event, utc_timestamp_from_ms
    occurred_at = utc_timestamp_from_ms(occurred_at_ms)
    payload = {"task_payload_digest": payload_digest}
    if event_type in ("succeeded", "failed", "canceled"):
        payload.update(status=event_type, output={} if output is None else output)
        event_type = "outcome"
    if event_type == "accepted":
        payload.update(accepted_at=occurred_at, local_task_uuid=local_task_uuid)
    return make_event(event_uuid=event_uuid, event_epoch_uuid=event_epoch_uuid,
                      event_sequence=event_sequence, station_uuid=STATION, instance_uuid=INSTANCE,
                      station_invocation_uuid=INVOCATION, local_task_uuid=local_task_uuid,
                      event_type=event_type, payload=payload, occurred_at=occurred_at)


def event_ack(event=None, *, application="applied", prefix=None):
    event = boundary_event() if event is None else event
    return {**{key: event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")},
            "application": application,
            "last_contiguous_event_sequence": event["event_sequence"] if prefix is None else prefix}


def config(tmp_path: Path, **overrides: Any) -> CenterClientConfig:
    for name in ("client.pem", "client.key", "ca.pem"):
        (tmp_path / name).write_text("placeholder", encoding="utf-8")
    values: dict[str, Any] = {
        "endpoint": "https://center.invalid",
        "client_cert": tmp_path / "client.pem",
        "client_key": tmp_path / "client.key",
        "server_ca": tmp_path / "ca.pem",
        "machine_secret": "a" * 64,
        "instance_uuid": INSTANCE,
        "station_uuid": STATION,
    }
    values.update(overrides)
    return CenterClientConfig(**values)


@pytest.mark.parametrize("incoming,outgoing", [("03", "01"), ("02", "00"), ("01", "01"), ("00", "00")])
def test_center_traceparent_preserves_identity_and_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incoming: str, outgoing: str,
) -> None:
    """新 SDK 的随机位不得让旧中心丢失 trace；不改变会话与业务请求。"""
    from unilabos.utils import tracing

    prefix = "00-0a9d5fc2c74da135bf731e3cfcaa85ab-7e8a20be6989a386-"
    transport = CenterTransport(config(tmp_path))
    captured: dict[str, Any] = {}
    body = {"event_uuid": EVENT}
    session = {"X-Station-Session": SESSION}

    def inject(headers: dict[str, str]) -> None:
        headers.update(traceparent=prefix + incoming, tracestate="vendor=value")

    def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(tracing, "inject_trace_context", inject)
    monkeypatch.setattr(transport, "_request", request)
    assert transport.request("POST", "/events", json_body=body, session_headers=session) == {"ok": True}
    assert captured["session_headers"] == {
        **session, "traceparent": prefix + outgoing, "tracestate": "vendor=value",
    }
    assert captured["json_body"] is body
    assert session == {"X-Station-Session": SESSION}


class FakeTransport:
    """记录每次请求，并按脚本决定成功或失败。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping_or_none = None,
        session_headers=None,
        expected_status=None,
    ) -> dict[str, Any]:
        if path.endswith("/events"):
            assert expected_status == 202
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": json.loads(json.dumps(json_body)) if json_body else None,
                "headers": dict(session_headers or {}),
            }
        )
        if not self.responses:
            raise AssertionError(f"未预期的请求：{method} {path}")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


Mapping_or_none = Any


def session_response(epoch: int = 1) -> dict[str, Any]:
    return {
        "session_uuid": SESSION,
        "session_epoch": epoch,
        "station_uuid": STATION,
        "instance_uuid": INSTANCE,
    }


def package_response() -> dict[str, Any]:
    revision = "88888888-8888-4888-8888-888888888888"
    payload = {
        "protocol_version": "1.0", "adapter_version": "1.0",
        "station_invocation_uuid": INVOCATION,
        "global_task_uuid": "66666666-6666-4666-8666-666666666666",
        "global_workflow_revision_uuid": revision,
        "contract_uuid": "77777777-7777-4777-8777-777777777777",
        "workflow_uuid": "99999999-9999-4999-8999-999999999999",
        "station_uuid": STATION,
        "invocation_key": revision + ":aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "publication_version": 1,
        "revision_fingerprint": DIGEST, "contract_digest": DIGEST, "projection_digest": DIGEST,
        "input": {"sample": "value"},
        "handoff_uuids": [], "material_refs": [], "container_refs": [], "artifact_refs": [],
    }
    return {"payload": payload, "payload_digest": digest(payload)}


# --------------------------------------------------------------- 会话

def test_session_handshake_uses_the_certificate_identity(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(epoch=7)])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    session = client.open_session(EPOCH)

    assert session.session_uuid == SESSION
    assert session.session_epoch == 7
    call = transport.calls[0]
    assert call["path"] == "/api/v1/stations/session"
    assert call["body"] == {
        "process_uuid": "proc-1",
        "event_epoch_uuid": EPOCH,
        "protocol_version": PROTOCOL_VERSION,
    }
    # 请求体里没有任何自报身份字段：身份只能来自证书。
    assert STATION not in json.dumps(call["body"])
    assert INSTANCE not in json.dumps(call["body"])


def test_config_normalizes_station_http_base_url(tmp_path: Path) -> None:
    client_config = config(tmp_path, http_base_url="http://station.example:8002/")

    assert client_config.http_base_url == "http://station.example:8002"


@pytest.mark.parametrize(
    "http_base_url",
    [
        "",
        "ftp://station.example:8002",
        "https://station.example:8443",
        "http://user:password@station.example:8002",
        "http://station.example:8002/api",
        "http://station.example:8002?query=1",
    ],
)
def test_config_rejects_invalid_station_http_base_url(
    tmp_path: Path, http_base_url: str
) -> None:
    with pytest.raises(CenterConfigError):
        config(tmp_path, http_base_url=http_base_url)


def test_session_rejects_a_certificate_that_belongs_to_another_station(
    tmp_path: Path,
) -> None:
    other = dict(session_response())
    other["station_uuid"] = "99999999-9999-4999-8999-999999999999"
    client = CenterClient(config(tmp_path), FakeTransport([other]))

    with pytest.raises(CenterClientError, match="证书身份与本地配置不一致"):
        client.open_session(EPOCH)


def test_resumed_session_carries_the_previous_fence(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(epoch=1), session_response(epoch=2)])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")
    first = client.open_session(EPOCH)
    client.open_session(EPOCH)

    body = transport.calls[1]["body"]
    # 带上次会话的 fence：中心据此区分"续期"与"被接管"。
    assert body["previous_session_uuid"] == first.session_uuid
    assert body["previous_session_epoch"] == first.session_epoch


# ----------------------------------------------------- 重试必须不改内容

def test_lost_session_resends_the_identical_claim_body(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            session_response(),
            CenterTransportError("stale", code="SESSION_EXPIRED", retryable=False),
            session_response(epoch=2),
            {"invocation_uuid": INVOCATION, "owner_instance_uuid": INSTANCE, "replayed": False},
        ]
    )
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    replayed = client.claim(INVOCATION, DIGEST, EPOCH)

    assert replayed is False
    claims = [call for call in transport.calls if call["path"].endswith("/claim")]
    assert len(claims) == 2, "会话失效后必须重发同一次 claim"
    # 请求体必须逐字相同：改内容会让中心的去重失效。
    assert claims[0]["body"] == claims[1]["body"] == {"payload_digest": DIGEST}
    # 会话头则必须换成新的，否则重发还是会被判失效。
    assert claims[0]["headers"]["X-Station-Session"] == SESSION
    assert claims[1]["headers"]["X-Session-Epoch"] == "2"


def test_lost_session_resends_the_identical_report_body(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            session_response(),
            CenterTransportError("stale", code="SESSION_EXPIRED", retryable=False),
            session_response(epoch=3),
            event_ack(boundary_event(output={"result": 1}, occurred_at_ms=1_700_000_000_000)),
        ]
    )
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    application = client.report(boundary_event(event_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", local_task_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff", event_type="succeeded", payload_digest=DIGEST, output={"result": 1}, occurred_at_ms=1_700_000_000_000, event_epoch_uuid=EPOCH))

    assert application == "applied"
    reports = [call for call in transport.calls if call["path"].endswith("/events")]
    assert len(reports) == 2
    assert reports[0]["body"] == reports[1]["body"]
    # 事件身份与本地任务身份必须原样重发：重算就等于换了一件事。
    assert reports[1]["body"]["event_uuid"] == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    assert reports[1]["body"]["local_task_uuid"] == "ffffffff-ffff-4fff-8fff-ffffffffffff"


def test_fenced_session_stops_instead_of_taking_the_session_back(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            session_response(),
            CenterTransportError("fenced", code="SESSION_FENCED", retryable=False),
        ]
    )
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    with pytest.raises(SessionFencedError):
        client.claim(INVOCATION, DIGEST, EPOCH)

    # 不得重新握手抢回会话：那会让两台工站同时执行同一批调用。
    assert len(transport.calls) == 2
    assert client.session is None


def test_duplicate_application_is_success_not_failure(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(), event_ack(application="duplicate")])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    application = client.report(boundary_event(event_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", local_task_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff", event_type="succeeded", payload_digest=DIGEST, occurred_at_ms=1, event_epoch_uuid=EPOCH))

    # 中心已经收下这件事，只是这次没有新记录。当成失败会让工站无限重发。
    assert application == "duplicate"


def test_conflicting_event_is_surfaced_rather_than_retried(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(), event_ack(application="conflict")])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    assert client.report(boundary_event()) == "conflict"
    # 只发了一次：冲突重发不会变好，必须交给人工。
    assert len([c for c in transport.calls if c["path"].endswith("/events")]) == 1


def test_report_requires_a_persisted_event_identity(tmp_path: Path) -> None:
    client = CenterClient(config(tmp_path), FakeTransport([]), process_uuid="proc-1")
    envelope = boundary_event()
    envelope["event_uuid"] = ""
    with pytest.raises(CenterClientError, match="规范 UUID"):
        client.report(envelope)


def test_non_session_errors_are_not_retried(tmp_path: Path) -> None:
    transport = FakeTransport(
        [session_response(), CenterTransportError("bad", code="FORBIDDEN", retryable=False)]
    )
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    with pytest.raises(CenterTransportError) as raised:
        client.claim(INVOCATION, DIGEST, EPOCH)
    assert raised.value.code == "FORBIDDEN"
    assert len(transport.calls) == 2, "只有会话类错误才允许重新握手重发"


# ------------------------------------------------------------- 任务包

def test_task_package_reads_the_center_frozen_digest(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(), package_response()])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")

    package = client.task_package(INVOCATION, EPOCH)

    assert package.station_invocation_uuid == INVOCATION
    assert package.input == {"sample": "value"}
    # 接收时必须重算规范化摘要，与中心冻结事实一致才能继续。
    assert package.payload_digest == package_response()["payload_digest"]
    assert transport.calls[1]["method"] == "GET"


def test_task_package_without_a_payload_is_refused(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(), {"payload_digest": DIGEST}])
    client = CenterClient(config(tmp_path), transport, process_uuid="proc-1")
    with pytest.raises(CenterClientError, match="缺少 payload"):
        client.task_package(INVOCATION, EPOCH)


# --------------------------------------------------------------- 传输

class _StubResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.content = json.dumps(body).encode()
        self._body = body

    def json(self):
        return self._body


class _StubTransport(CenterTransport):
    """只替换连接本身，保留真实的错误映射逻辑。"""

    def __init__(self, config: CenterClientConfig, outcome: Any) -> None:
        super().__init__(config)
        self._outcome = outcome

    def _session(self):  # type: ignore[override]
        outcome = self._outcome

        class Session:
            def request(self, *args: Any, **kwargs: Any):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return Session()


@pytest.mark.parametrize("status", [200, 201, 202])
def test_report_requires_http_202_even_when_ack_is_exact(tmp_path: Path, status: int) -> None:
    from unilabos.app.center_client.client import ControlSession

    envelope = boundary_event()
    cfg = config(tmp_path)
    client = CenterClient(cfg, _StubTransport(cfg, _StubResponse(status, event_ack(envelope))))
    client.restore_evidence_session(ControlSession(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", SESSION, 1, STATION, INSTANCE,
    ))
    if status == 202:
        assert client.report(envelope) == "applied"
    else:
        with pytest.raises(CenterTransportError, match="状态码应为 202") as raised:
            client.report(envelope)
        assert raised.value.status_code == status


def test_transport_marks_only_transient_failures_retryable(tmp_path: Path) -> None:
    import requests

    cfg = config(tmp_path)

    # 网络层失败可以重发：中心靠"调用身份 + 事件身份"去重，重发不会多执行一次。
    with pytest.raises(CenterTransportError) as raised:
        _StubTransport(cfg, requests.exceptions.ConnectionError("down")).request("GET", "/x")
    assert raised.value.retryable is True

    # 证书被拒不可重发：同一张证书重发还是会被拒。
    with pytest.raises(CenterTransportError) as raised:
        _StubTransport(cfg, requests.exceptions.SSLError("bad cert")).request("GET", "/x")
    assert raised.value.retryable is False

    # 5xx 与 429 是瞬时的；4xx 是请求本身的问题，重发不会变好。
    for status, expected in ((503, True), (429, True), (403, False), (404, False)):
        stub = _StubTransport(cfg, _StubResponse(status, {"code": "X", "message": "m"}))
        with pytest.raises(CenterTransportError) as raised:
            stub.request("GET", "/x")
        assert raised.value.retryable is expected, status
        assert raised.value.code == "X"
        assert raised.value.status_code == status


def test_transport_surfaces_tls_rejection_as_not_retryable(tmp_path: Path) -> None:
    import requests

    # 证书不被中心接受时，重发同一张证书只会再被拒一次；必须让人看见而不是静默重试。
    stub = _StubTransport(config(tmp_path), requests.exceptions.SSLError("unknown ca"))
    with pytest.raises(CenterTransportError, match="TLS 握手失败") as raised:
        stub.request("GET", "/x")
    assert raised.value.retryable is False


# --------------------------------------------------------------- 摘要

def test_digest_is_stable_across_key_order_and_number_spelling() -> None:
    assert digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1})
    # RFC 8785 按 ECMAScript 数字格式化：1 与 1.0 是同一个值。
    # 若两者算出不同摘要，中心会把语义相同的数据判成"任务包已变更"。
    assert digest({"v": 1}) == digest({"v": 1.0})
    assert digest({"v": 1}) != digest({"v": 2})
    assert is_digest(digest({}))
    assert not is_digest("sha256:zz")
    assert not is_digest("md5:" + "ab" * 32)


def test_digest_refuses_input_it_cannot_canonicalize() -> None:
    with pytest.raises(DigestError):
        digest({"v": float("nan")})


# --------------------------------------------------------------- 配置

def test_config_refuses_plaintext_and_missing_material(tmp_path: Path) -> None:
    with pytest.raises(CenterConfigError, match="https"):
        config(tmp_path, endpoint="http://center.invalid")
    with pytest.raises(CenterConfigError, match="客户端证书不存在"):
        config(tmp_path, client_cert=tmp_path / "missing.pem")
    assert config(tmp_path, machine_secret="").machine_secret == ""


def test_config_never_prints_the_machine_secret(tmp_path: Path) -> None:
    secret = "c" * 64
    rendered = repr(config(tmp_path, machine_secret=secret))
    assert secret not in rendered


@pytest.mark.parametrize("mutation", ["tamper", "unknown", "wrong_station", "wrong_invocation", "uuid", "version",
                                     "uuid_case", "invocation_node_case"])
def test_package_is_verified_before_it_can_be_claimed(tmp_path: Path, mutation: str) -> None:
    response = package_response()
    payload = response["payload"]
    if mutation == "tamper":
        payload["input"]["sample"] = "different"
    else:
        if mutation == "unknown":
            payload["surprise"] = True
        elif mutation == "wrong_station":
            payload["station_uuid"] = INSTANCE
        elif mutation == "wrong_invocation":
            payload["station_invocation_uuid"] = INSTANCE
        elif mutation == "uuid":
            payload["contract_uuid"] = "bad"
        elif mutation == "uuid_case":
            payload["contract_uuid"] = "abcdefab-abcd-4abc-8abc-abcdefabcdef".upper()
        elif mutation == "invocation_node_case":
            payload["invocation_key"] = payload["global_workflow_revision_uuid"] + ":ABCDEFAB-ABCD-4ABC-8ABC-ABCDEFABCDEF"
        else:
            payload["publication_version"] = True
        response["payload_digest"] = digest(payload)
    transport = FakeTransport([session_response(), response])
    client = CenterClient(config(tmp_path), transport)
    with pytest.raises(CenterClientError):
        client.task_package(INVOCATION, EPOCH)
    assert all(not call["path"].endswith("/claim") for call in transport.calls)


def test_lost_handshake_response_replays_the_exact_request(tmp_path: Path) -> None:
    transport = FakeTransport([
        session_response(), CenterTransportError("响应丢失", retryable=True), session_response(2),
    ])
    client = CenterClient(config(tmp_path), transport)
    client.open_session(EPOCH)
    with pytest.raises(CenterTransportError):
        client.open_session(EPOCH)
    client.open_session(EPOCH)
    assert transport.calls[1]["body"] == transport.calls[2]["body"]
    assert client.session.session_epoch == 2


def test_stale_control_stays_fenced_but_original_owner_can_report(tmp_path: Path) -> None:
    transport = FakeTransport([
        session_response(), CenterTransportError("接管", code="STALE_SESSION"),
        event_ack(),
    ])
    client = CenterClient(config(tmp_path), transport)
    with pytest.raises(SessionFencedError):
        client.claim(INVOCATION, DIGEST, EPOCH)
    with pytest.raises(SessionFencedError):
        client.open_session(EPOCH)
    assert client.report(boundary_event(event_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", local_task_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff", event_type="succeeded", payload_digest=DIGEST, occurred_at_ms=1, event_epoch_uuid=EPOCH)) == "applied"
    assert len(transport.calls) == 3
    assert transport.calls[-1]["headers"]["X-Station-Session"] == SESSION


def test_restart_replays_evidence_with_historical_session_without_stealing_control(tmp_path: Path) -> None:
    from unilabos.app.center_client.client import ControlSession
    transport = FakeTransport([event_ack(application="duplicate")])
    client = CenterClient(config(tmp_path), transport)
    old_process = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    client.restore_evidence_session(ControlSession(old_process, SESSION, 1, STATION, INSTANCE))
    assert client.report(boundary_event(event_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", local_task_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff", event_type="succeeded", payload_digest=DIGEST, occurred_at_ms=1, event_epoch_uuid=EPOCH)) == "duplicate"
    assert client.process_uuid != old_process
    assert client.session is None
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["X-Station-Process"] == old_process


def test_mismatched_event_ack_does_not_confirm_the_event(tmp_path: Path) -> None:
    transport = FakeTransport([session_response(), {**event_ack(), "event_uuid": "other"}])
    client = CenterClient(config(tmp_path), transport)
    with pytest.raises(CenterClientError, match="确认身份不匹配"):
        client.report(boundary_event(event_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", local_task_uuid="ffffffff-ffff-4fff-8fff-ffffffffffff", event_type="succeeded", payload_digest=DIGEST, occurred_at_ms=1, event_epoch_uuid=EPOCH))


@pytest.mark.parametrize("body", [b'{"x":1,"x":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'[]', b'{} {}'])
def test_transport_rejects_ambiguous_json(tmp_path: Path, body: bytes) -> None:
    response = _StubResponse(200, {})
    response.content = body
    with pytest.raises(CenterTransportError):
        _StubTransport(config(tmp_path), response).request("GET", "/x")


def test_transport_uses_only_mtls_and_disables_redirects(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    transport = CenterTransport(cfg)
    calls = []

    class Session:
        def request(self, *args, **kwargs):
            calls.append(kwargs)
            return _StubResponse(307, {})

    transport._local.session = Session()
    transport._local.config = cfg
    with pytest.raises(CenterTransportError, match="重定向"):
        transport.request("GET", "/x", session_headers={"Authorization": "Bearer wrong"})
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["allow_redirects"] is False


def test_handshake_rejects_other_instance_and_invalid_epoch(tmp_path: Path) -> None:
    for updates in ({"instance_uuid": STATION}, {"session_epoch": True}, {"session_epoch": 0}):
        transport = FakeTransport([dict(session_response(), **updates)])
        with pytest.raises(CenterClientError):
            CenterClient(config(tmp_path), transport).open_session(EPOCH)
