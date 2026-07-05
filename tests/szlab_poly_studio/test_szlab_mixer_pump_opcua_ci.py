from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from tests.pseudo_devices.common.opcua_csv_server import OpcUaCsvServer
from tests.pseudo_devices.common.opcua_flow_daemon import OpcUaFlowDaemon
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice

SZLAB_DIR = Path(__file__).resolve().parent
REPO_ROOT = SZLAB_DIR.parents[1]
PUMP_CSV = REPO_ROOT / "unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv"
PUMP_FLOW = REPO_ROOT / "unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_flow.json"
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:48506/"


@pytest.fixture(scope="module")
def opcua_pseudo_stack():
    url = os.environ.get("UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL")
    external = bool(url)
    server = None
    daemon = None

    if not external:
        url = DEFAULT_ENDPOINT
        server = OpcUaCsvServer(endpoint=url, csv_path=PUMP_CSV)
        server.start()
        daemon = OpcUaFlowDaemon(url=url, flow_path=PUMP_FLOW)
        daemon.start()

    logging.info("OPC UA endpoint=%s", url)
    logging.info("CSV=%s", PUMP_CSV)
    logging.info("flow=%s", PUMP_FLOW)
    if daemon:
        logging.info("flow summary:\n%s", daemon.summary)

    yield url, server, daemon

    if daemon:
        daemon.stop()
    if server:
        server.stop()


class TestSzlabMixerPumpOpcUaDevice:
    def test_run_solvent_addition_against_virtual_opcua(self, opcua_pseudo_stack):
        url, server, daemon = opcua_pseudo_stack
        before = {}
        if server:
            before = {name: server.read(name) for name in ("S06允许加工", "S06加工完成", "S06_1号溶液添加量")}

        device = SzlabMixerPumpDevice(
            url=url,
            timeout=8.0,
        )
        try:
            result = device.run_solvent_addition(process=1, volume=10)
            assert result["success"] is True
        finally:
            device.disconnect()

        if server:
            after = {name: server.read(name) for name in ("S06加工完成", "S06工艺选择", "S06_1号溶液添加量")}
            logging.info("OPC before=%s", before)
            logging.info("OPC after=%s", after)
            assert after["S06_1号溶液添加量"] == 0
            assert after["S06工艺选择"] == 0
            assert after["S06加工完成"] is True
