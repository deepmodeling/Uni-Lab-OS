from __future__ import annotations

import logging
import os
import time
from typing import Any

import pytest
from opcua import Client

from unilabos.devices.workstation.szlab_mixer.pump import SzlabMixerPumpDevice


LOGGER = logging.getLogger(__name__)

PUMP_VARIABLES = [
    "S06允许加工",
    "S06参数写入完成",
    "S06注射泵选择",
    "S06注射泵1抽液",
    "S06注射泵1排液",
    "S06注射泵2抽液",
    "S06注射泵2排液",
    "S06加工完成",
]


def _ci_log(message: str, *args: Any) -> None:
    if args:
        message = message % args
    print(f"[szlab_mixer_pump_ci] {message}", flush=True)
    LOGGER.info(message)


def _browse_virtual_mixer_nodes(url: str) -> tuple[Client, dict[str, Any]]:
    client = Client(url)
    client.connect()
    objects = client.get_objects_node()
    for child in objects.get_children():
        if child.get_browse_name().Name == "VirtualMixer":
            nodes = {node.get_browse_name().Name: node for node in child.get_children()}
            return client, nodes
    client.disconnect()
    raise RuntimeError("OPC UA 中未找到 VirtualMixer 对象")


def _wait_for_virtual_mixer(url: str, timeout: float = 15.0) -> None:
    started_at = time.time()
    last_error = ""
    while time.time() - started_at < timeout:
        try:
            client, nodes = _browse_virtual_mixer_nodes(url)
            try:
                missing = sorted(set(PUMP_VARIABLES) - set(nodes))
                if missing:
                    raise RuntimeError(f"VirtualMixer 缺少变量: {missing}")
                _ci_log("VirtualMixer ready: url=%s variables=%s", url, sorted(nodes))
                return
            finally:
                client.disconnect()
        except Exception as exc:
            last_error = str(exc)
            _ci_log("等待 VirtualMixer ready: url=%s error=%s", url, last_error)
            time.sleep(0.5)
    raise TimeoutError(f"等待 VirtualMixer 超时: {last_error}")


def test_szlab_mixer_pump_transfer_liquid_against_virtual_opcua() -> None:
    url = os.environ.get("UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL")
    if not url:
        pytest.skip("需要设置 UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL 才运行虚拟 OPC UA 集成测试")

    _ci_log("开始 szlab_mixer pump OPC UA 集成测试: url=%s", url)
    _wait_for_virtual_mixer(url)

    device = SzlabMixerPumpDevice(url=url, timeout=8.0)
    try:
        before = device.get_variables(PUMP_VARIABLES)
        _ci_log("pump action 前 OPC 状态: %s", before)

        result = device.transfer_liquid(pump=1, volume=10, direction="aspirate")

        after = device.get_variables(PUMP_VARIABLES)
        _ci_log("pump action 后 OPC 状态: %s", after)
        _ci_log("pump action 返回: %s", result)

        assert result["success"] is True
        assert after["S06注射泵选择"]["value"] == 1
        assert after["S06注射泵1抽液"]["value"] == 10
    finally:
        device.disconnect()
