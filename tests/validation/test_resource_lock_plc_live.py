"""验证验收脚本只有在清理和遥测提交均成功后才能发布通过结果。"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from opcua import ua

from scripts.validation import resource_lock_plc_live as validation


class _Node:
    def __init__(self, client: "_Client", name: str) -> None:
        self.client = client
        self.name = name

    def get_value(self) -> Any:
        if self.name.endswith(("准备信号", "允许加工")):
            return True
        return self.client.values.get(self.name, False)

    def get_data_type_as_variant_type(self) -> ua.VariantType:
        return ua.VariantType.Int16 if self.name.endswith("工艺选择") else ua.VariantType.Boolean

    def set_value(self, value: ua.DataValue) -> None:
        raw = value.Value.Value
        self.client.values[self.name] = raw
        channel = self.name[:3]
        done = channel + ("工艺完成" if channel == "S07" else "加工完成")
        if self.name.endswith("参数写入完成"):
            self.client.values[done] = raw


class _Client:
    def __init__(self, endpoint: str, timeout: int) -> None:
        self.values: dict[str, Any] = {}

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def get_node(self, identifier: str) -> _Node:
        return _Node(self, identifier.split("|", 1)[1])


@pytest.mark.parametrize("flush_success", [False, True])
def test_result_is_published_only_after_tracing_flush(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flush_success: bool
) -> None:
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resource_lock_plc_live",
            "--endpoint",
            "test",
            "--otel-endpoint",
            "test",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(validation, "Client", _Client)
    monkeypatch.setattr(validation, "initialize_tracing", lambda settings: True)
    monkeypatch.setattr(validation, "mkdtemp", lambda **kwargs: str(tmp_path))
    flush_statuses: list[str] = []

    def flush(timeout_ms: int) -> bool:
        flush_statuses.append(json.loads(output.read_text())["status"])
        return flush_success

    monkeypatch.setattr(validation, "shutdown_tracing", flush)
    if flush_success:
        validation.main()
    else:
        with pytest.raises(RuntimeError, match="OTel flush"):
            validation.main()
    result = json.loads(output.read_text())
    assert flush_statuses == ["running"]
    assert result["status"] == ("passed" if flush_success else "failed")
    if flush_success:
        assert len(result["events"]) == 12
    else:
        assert "OTel flush" in result["error"]


def test_connection_failure_records_failure_and_flushes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resource_lock_plc_live",
            "--endpoint",
            "test",
            "--otel-endpoint",
            "test",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(validation, "Client", _Client)
    monkeypatch.setattr(validation, "initialize_tracing", lambda settings: True)
    flushed: list[int] = []

    def connect(client: _Client) -> None:
        raise ConnectionError("PLC 连接失败")

    def flush(timeout_ms: int) -> bool:
        flushed.append(timeout_ms)
        return True

    monkeypatch.setattr(_Client, "connect", connect)
    monkeypatch.setattr(validation, "shutdown_tracing", flush)
    with pytest.raises(ConnectionError, match="PLC 连接失败"):
        validation.main()
    assert json.loads(output.read_text()) == {"status": "failed", "error": "PLC 连接失败"}
    assert flushed == [5000]
