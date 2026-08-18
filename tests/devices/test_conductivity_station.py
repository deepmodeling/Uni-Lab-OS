"""电导工站 TCP 客户端与模拟服务端集成测试。"""

from __future__ import annotations

import json
import math
import socketserver
import threading
import time

import pytest

from unilabos.devices.workstation.conductivity_station.conductivity_station import (
    ConductivityStation,
    ConductivityStationProtocolError,
    ConductivityStationTransportError,
)
from unilabos.devices.workstation.conductivity_station.mock_server import (
    MockConductivityServer,
    MockConductivityState,
)


@pytest.fixture()
def station() -> ConductivityStation:
    state = MockConductivityState(step_interval=0.01)
    server = MockConductivityServer(("127.0.0.1", 0), state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1",
        port=server.server_address[1],
        response_timeout=1.0,
    )
    try:
        yield client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_material_start_and_result(station: ConductivityStation) -> None:
    assert station.station_status()["data"]["robot_arm"] == 1
    assert station.material_status()["data"]["bottle"][:3] == [1, 1, 1]

    started = station.start_batch()
    assert started["result"] == 0
    batch_id = started["data"]["batch_id"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = station.batch_status()["data"]
        if not status["running"]:
            break
        time.sleep(0.02)
    else:
        pytest.fail("模拟批次未在预期时间内完成")
    assert status["batch"]["current_test"] == 3
    assert status["batch"]["current_test_step"] == 13

    result = station.batch_result(batch_id)
    tests = result["data"]["batch"]["tests"]
    assert [item["state"] for item in tests] == ["finished"] * 3
    assert tests[0]["bottle_code"].startswith("LOT-")
    assert all(item["test_time"] for item in tests)
    assert tests[0]["R"] > 0
    assert tests[0]["ion_conductivity"] > 0
    assert tests[0]["area"] == pytest.approx(math.pi * 0.5**2)
    thickness_cm = (tests[0]["h2"] - tests[0]["h1"]) / 10.0
    expected_ms_cm = thickness_cm / (tests[0]["R"] * tests[0]["area"]) * 1000
    assert tests[0]["ion_conductivity"] == pytest.approx(expected_ms_cm, rel=1e-5)


def test_manual_validation_and_stop_after_current_sample(
    station: ConductivityStation,
) -> None:
    with pytest.raises(ValueError, match="1 到 13"):
        station.manual_run(14)

    batch_id = station.start_batch()["data"]["batch_id"]
    assert station.manual_run(6)["result"] == 0
    assert station.stop_current_batch()["result"] == 0

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = station.batch_status()["data"]
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["batch"]["state"] == "stopped"
    tests = station.batch_result(batch_id)["data"]["batch"]["tests"]
    assert tests[0]["state"] == "finished"
    assert tests[1]["state"] == "not_started"


def test_unknown_batch_and_clear(station: ConductivityStation) -> None:
    assert station.batch_result("missing")["result"] == 9
    assert station.clear_current_batch()["result"] == 0
    status = station.batch_status()["data"]
    assert status == {"running": False, "batch": None}


def _scripted_station(plans: list[object]) -> tuple[ConductivityStation, socketserver.TCPServer, threading.Thread]:
    """启动一个按连接顺序返回分片、断线或指定响应的协议测试服务。"""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            received = bytearray()
            while b"\r\n" not in received:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                received.extend(chunk)
            request = json.loads(bytes(received).split(b"\r\n", 1)[0])
            with self.server.plan_lock:  # type: ignore[attr-defined]
                plan = self.server.plans.pop(0)  # type: ignore[attr-defined]
            if plan is None:
                return
            if callable(plan):
                plan = plan(request)
            chunks = plan if isinstance(plan, list) else [plan]
            for chunk in chunks:
                self.request.sendall(chunk)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.plans = list(plans)  # type: ignore[attr-defined]
    server.plan_lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ConductivityStation(
        ip="127.0.0.1", port=server.server_address[1], response_timeout=0.5
    )
    return client, server, thread


def _close_scripted_station(
    client: ConductivityStation,
    server: socketserver.TCPServer,
    thread: threading.Thread,
) -> None:
    client.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_tcp_fragmentation_and_read_retry() -> None:
    def fragmented(request: dict) -> list[bytes]:
        payload = json.dumps(
            {"request_id": request["request_id"], "result": 0, "data": {"running": False, "batch": None}}
        ).encode()
        return [payload[:8], payload[8:] + b"\r\n"]

    client, server, thread = _scripted_station([None, fragmented])
    try:
        # 查询动作首次断线后可安全重连，且能拼接分片响应。
        assert client.batch_status()["data"]["running"] is False
        assert server.plans == []  # type: ignore[attr-defined]
    finally:
        _close_scripted_station(client, server, thread)

@pytest.mark.parametrize(
    "response,exception",
    [
        (b'{"request_id":999,"result":0}\r\n', ConductivityStationProtocolError),
        (b'{not-json}\r\n', ConductivityStationProtocolError),
    ],
)
def test_invalid_responses_are_rejected(response: bytes, exception: type[Exception]) -> None:
    client, server, thread = _scripted_station([response])
    try:
        with pytest.raises(exception):
            client.start_batch()
    finally:
        _close_scripted_station(client, server, thread)


def test_side_effect_action_is_not_retried_after_disconnect() -> None:
    client, server, thread = _scripted_station([None, b'{"request_id":1,"result":0}\r\n'])
    try:
        with pytest.raises(ConductivityStationTransportError):
            client.start_batch()
        # 第二个计划仍在，证明有副作用动作未被自动重发。
        assert len(server.plans) == 1  # type: ignore[attr-defined]
    finally:
        _close_scripted_station(client, server, thread)
