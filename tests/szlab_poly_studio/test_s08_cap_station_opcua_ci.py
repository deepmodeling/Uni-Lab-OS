from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from unilabos.devices.workstation.szlab_poly_studio.s08_decap import decap_s08_cap_station as _s08_module
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    SZLabS08CapStationDevice,
)

_cap_cache_element_name = _s08_module._cap_cache_element_name

REPO_ROOT = Path(__file__).resolve().parents[2]
S08_CSV_PATH = (
    REPO_ROOT
    / "unilabos"
    / "devices"
    / "workstation"
    / "szlab_poly_studio"
    / "s08_decap"
    / "decap_s08_nodes.csv"
)
S08_FLOW_PATH = REPO_ROOT / "tests" / "pseudo_devices" / "szlab_s08_cap_station" / "open_liquid_cap_flow.json"
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:50102/"
SAMPLE_ID = [101, 102, 103]


@pytest.fixture(scope="module")
def opcua_pseudo_stack():
    from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer
    from tests.pseudo_devices.common.opcua_flow_daemon import FlowDaemon

    logging.getLogger("opcua").setLevel(logging.WARNING)
    url = os.environ.get("UNILABOS_TEST_SZLAB_S08_OPCUA_URL")
    external = bool(url)
    server = None
    stop_event = None
    daemon_thread = None

    if not external:
        url = DEFAULT_ENDPOINT
        server = CsvOpcUaServer(
            endpoint=url,
            csv_path=S08_CSV_PATH,
            object_name="VirtualS08",
            namespace_uri="http://unilabos.com/opcua/test/pseudo-device",
            server_name="UniLabOS Test OPC UA Server",
            name_column="变量名",
            data_type_column="数据类型",
            initial_value_column="初始值",
            node_id_column="",
            initial_values={
                "S08原点信号": True,
                "S08允许加工": True,
                "传感器状态_上位机[3].NO[14]": True,
                "传感器状态_上位机[3].NO[15]": True,
                "工站状态[7]": 2,
            },
        )
        server.start()
        stop_event = threading.Event()
        daemon = FlowDaemon(
            url=url,
            object_name="VirtualS08",
            flow_path=S08_FLOW_PATH,
            poll_interval=0.02,
            stop_requested=stop_event.is_set,
        )
        daemon_thread = threading.Thread(target=daemon.run, daemon=True)
        daemon_thread.start()
        time.sleep(0.5)

    logging.info("OPC UA endpoint=%s", url)
    logging.info("CSV=%s", S08_CSV_PATH)
    logging.info("flow=%s", S08_FLOW_PATH)

    yield url, server, stop_event, daemon_thread

    if stop_event is not None:
        stop_event.set()
    if daemon_thread is not None:
        daemon_thread.join(timeout=2.0)
    if server is not None:
        server.stop()


class TestSzlabS08CapStationOpcUaDevice:
    def test_open_and_close_liquid_vial_100ml_cap_against_virtual_opcua(self, opcua_pseudo_stack):
        url, _server, _stop_event, _daemon_thread = opcua_pseudo_stack
        device = SZLabS08CapStationDevice(url=url, timeout=30.0)
        try:
            open_result = device.process_cap(
                工艺选择=5,
                样品ID=SAMPLE_ID,
                超时时间=30.0,
            )
            assert open_result["success"] is True
            assert open_result["cap_storage_slot"] == 1

            close_result = device.process_cap(
                工艺选择=6,
                样品ID=SAMPLE_ID,
                超时时间=30.0,
            )
            assert close_result["success"] is True
            assert close_result["cap_storage_slot"] == 1

            status = close_result["status"]
            assert status["process_select"] == 0
            assert status["params_written"] is False
            assert int(device._client.read(_cap_cache_element_name(1, 0)) or 0) == 0
        finally:
            device._client.disconnect()
