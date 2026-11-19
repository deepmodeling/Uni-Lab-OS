"""业务控制面严格保持 WS notice / HTTP document 分层。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.backend.legacy_adaptor.http import BackendHTTPClient
from unilabos.server.backend.legacy_adaptor.sync import (
    InstanceSynchronizer,
    TemplateSynchronizer,
)
from unilabos.server.backend.legacy_adaptor.websocket import BackendWebSocketClient
from unilabos.protocol.base import canonical_hash
from unilabos.protocol.runtime.control import BackendCommandNotice


class _Response:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self.body = body

    def json(self) -> dict:
        return self.body


class _Session:
    def __init__(self, body: dict) -> None:
        self.headers: dict[str, str] = {}
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.body)


def _notice_data() -> dict:
    return {
        "notice_uuid": "notice-1",
        "command_uuid": "command-1",
        "command_type": "execute_job",
        "session_uuid": "session-1",
        "backend_sequence": 1,
        "edge_uuid": "edge-1",
        "authority_epoch": "authority-1",
        "connection_epoch": "connection-1",
        "content_sha256": "sha",
    }


def test_ws_notice_forbids_execution_or_result_body() -> None:
    with pytest.raises(ValidationError, match="action_args"):
        BackendCommandNotice.model_validate(
            {**_notice_data(), "action_args": {"volume": 5}}
        )


def test_http_client_fetches_full_document_from_uuid_derived_path() -> None:
    payload = {"job_uuid": "job-1"}
    payload_hash = canonical_hash(payload)
    body = {
        "code": 0,
        "data": {
            "protocol_version": "runtime.v1",
            "command": {
                "command_uuid": "command/1",
                "session_uuid": "session-1",
                "backend_sequence": 1,
                "command_type": "execute_job",
                "job_uuid": "job-1",
                "payload_uuid": "payload-1",
                "payload_sha256": payload_hash,
            },
            "payload": payload,
        },
    }
    session = _Session(body)
    client = BackendHTTPClient(
        "https://backend.example/api/v1", session=session
    )

    document = client.fetch_command("command/1")

    assert document.payload == payload
    assert session.calls[0][0].endswith("/edge/commands/command%2F1")


def test_reconnect_failures_are_not_logged_as_errors_each_time(caplog) -> None:
    """连不上权威：首连 / 断线后首次重连 WARNING，随后 DEBUG，长时间不通定期 ERROR；停机中只 DEBUG。

    权威重启、或权威停机时先关 WS 再请本进程退出都是正常现象，逐次 ERROR + 堆栈会淹掉真问题。
    """

    import logging

    comm_logger = logging.getLogger("unilabos.comm")
    previous_propagate = comm_logger.propagate
    comm_logger.propagate = True  # 通信日志独立成文件时不向上传播，caplog 需要它传播
    client = BackendWebSocketClient("ws://127.0.0.1:1/api/v1/ws/schedule")
    client._running = True
    refused = ConnectionRefusedError(1225, "远程计算机拒绝网络连接")
    try:
        with caplog.at_level(logging.DEBUG, logger="unilabos.comm"):
            _drive_reconnect_failures(client, refused)
    finally:
        comm_logger.propagate = previous_propagate

    levels = [record.levelname for record in caplog.records if "Connection error" in record.getMessage()]
    assert levels[:2] == ["WARNING", "WARNING"]
    assert set(levels[2:12]) == {"DEBUG"}
    assert levels[12] == "ERROR" and "12 attempts" in caplog.records[12].getMessage()
    assert levels[13] == "DEBUG" and "shutting down" in caplog.records[13].getMessage()
    # 网络类异常本身就是原因，不附堆栈
    assert not any("Traceback" in record.getMessage() for record in caplog.records)


def _drive_reconnect_failures(client: BackendWebSocketClient, refused: Exception) -> None:
    client._reconnect_count = 0  # 进程刚启动，权威尚未起来
    client._report_connect_failure("Connection error: refused", refused)
    client._reconnect_count = 1  # 断线后的首次重连
    client._report_connect_failure("Connection error: refused", refused)
    for attempt in range(2, 12):
        client._reconnect_count = attempt
        client._report_connect_failure("Connection error: refused", refused)
    client._reconnect_count = 12
    client._report_connect_failure("Connection error: refused", refused)
    client._running = False  # 本进程正在退出
    client._report_connect_failure("Connection error: refused", refused)


def test_backend_adaptor_owns_transport_and_data_sync() -> None:
    """Backend 传输与同步实现归属 ``legacy_adaptor`` 命名空间。"""

    prefix = "unilabos.server.backend.legacy_adaptor"
    assert BackendWebSocketClient.__module__ == f"{prefix}.websocket"
    assert BackendHTTPClient.__module__ == f"{prefix}.http"
    assert InstanceSynchronizer.__module__ == f"{prefix}.sync.instances"
    assert TemplateSynchronizer.__module__ == f"{prefix}.sync.templates"
