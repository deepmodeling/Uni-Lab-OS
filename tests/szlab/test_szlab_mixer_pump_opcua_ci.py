from __future__ import annotations

import logging
import os
import threading
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


def _start_completion_daemon(url: str, hold_seconds: float = 1.0) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def run() -> None:
        client: Client | None = None
        try:
            client, nodes = _browse_virtual_mixer_nodes(url)
            trigger = nodes["S06参数写入完成"]
            complete = nodes["S06加工完成"]
            _ci_log("pump 完成守护进程已连接: url=%s", url)
            while not stop_event.is_set():
                if bool(trigger.get_value()):
                    pump = nodes["S06注射泵选择"].get_value()
                    aspirate = nodes["S06注射泵1抽液"].get_value()
                    dispense = nodes["S06注射泵1排液"].get_value()
                    _ci_log(
                        "检测到 S06参数写入完成: pump=%s pump1_aspirate=%s pump1_dispense=%s",
                        pump,
                        aspirate,
                        dispense,
                    )
                    time.sleep(0.25)
                    complete.set_value(True)
                    _ci_log("写入 S06加工完成=True，保持 %.1fs", hold_seconds)
                    time.sleep(hold_seconds)
                    return
                time.sleep(0.02)
        except Exception:
            LOGGER.exception("pump 完成守护进程异常")
        finally:
            if client is not None:
                client.disconnect()

    thread = threading.Thread(target=run, name="szlab-mixer-pump-ci-daemon", daemon=True)
    thread.start()
    return stop_event, thread


def test_szlab_mixer_pump_transfer_liquid_against_virtual_opcua() -> None:
    url = os.environ.get("UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL")
    if not url:
        pytest.skip("需要设置 UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL 才运行虚拟 OPC UA 集成测试")

    _ci_log("开始 szlab_mixer pump OPC UA 集成测试: url=%s", url)
    _wait_for_virtual_mixer(url)

    stop_daemon, daemon_thread = _start_completion_daemon(url)
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
        stop_daemon.set()
        daemon_thread.join(timeout=2)
        device.disconnect()
