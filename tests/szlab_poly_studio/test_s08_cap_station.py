"""S08 开盖工位单元测试（公开 process_cap 与 not_action 内部实现）。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.szlab_poly_studio.pseudo_clients.s08_decap import PseudoSzlabS08OpcUaClient
from tests.szlab_poly_studio.s08_test_helpers import (
    NODE_PARAMS_WRITTEN,
    NODE_PROCESS_COMPLETE,
    NODE_PROCESS_SELECT,
    NODE_STATION_STATUS,
    S08ProcessType,
    SAMPLE_A,
    SAMPLE_B,
    SZLabS08CapStationDevice,
    _cap_cache_element_name,
    make_s08_device,
    s08_module,
)
from unilabos.registry.ast_registry_scanner import scan_directory
from scripts.workflow_ui import load_preset
from scripts.run_workflow_local import load_runtime_config


def test_s08_cap_station_is_ast_scannable():
    root = Path("unilabos/devices/workstation/szlab_poly_studio")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    device = result["devices"]["szlab_s08_cap_station"]
    assert device["class_name"] == "SZLabS08CapStationDevice"
    assert set(device["actions"]) == {"process_cap"}


def test_s08_registry_actions_only_expose_process_cap():
    preset = load_preset("debug_s08_cap_station")
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/debug_s08_cap_station_runtime.json")
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}

    assert preset.id == "debug_s08_cap_station"
    assert preset.runtime_config == "../runtime_configs/debug_s08_cap_station_runtime.json"
    assert not Path("tests/szlab_poly_studio/presets/s08_cap_station.json").exists()
    assert not Path("tests/szlab_poly_studio/runtime_configs/s08_cap_station_runtime.json").exists()
    assert list(preset.actions) == ["process_cap"]
    action = preset.actions["process_cap"]
    assert action.device_id == "szlab_s08_cap_station"
    assert [param["name"] for param in action.params] == [
        "工艺选择",
        "样品ID",
        "瓶盖暂存位",
    ]
    assert action.description == "S08 开/关盖"
    assert runtime_config.device_factory.plc_device_id == "szlab_poly_plc"
    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_s08_cap_station": (
            "unilabos.devices.workstation.szlab_poly_studio.s08_decap."
            "decap_s08_cap_station.SZLabS08CapStationDevice"
        ),
    }
    assert set(graph_nodes) == {"szlab_poly_plc", "szlab_s08_cap_station"}
    assert graph_nodes["szlab_s08_cap_station"]["config"]["plc_device_id"] == "szlab_poly_plc"
    assert graph_nodes["szlab_s08_cap_station"]["config"]["use_plc_gateway"] is True


def test_s08_process_cap_uses_plc_gateway_for_waits_and_resets():
    class FakeS08PlcGateway:
        def __init__(self):
            self.values = {
                "S08原点信号": True,
                "S08允许加工": True,
                "S08工艺选择": 0,
                "S08参数写入完成": False,
                "S08工艺完成": 0,
                "S082瓶盖暂存位": 0,
                "工站状态[7]": 2,
                "传感器状态_上位机[3].NO[14]": True,
                "传感器状态_上位机[3].NO[15]": True,
                "传感器状态_上位机[4].NO[0]": False,
                "传感器状态_上位机[4].NO[1]": False,
                "传感器状态_上位机[4].NO[2]": False,
                "传感器状态_上位机[4].NO[3]": False,
                "传感器状态_上位机[4].NO[4]": False,
            }
            for slot in range(1, 6):
                for index in range(s08_module.CAP_CACHE_LENGTH):
                    self.values[_cap_cache_element_name(slot, index)] = 0
            self.reads = []
            self.writes = []
            self.waits = []

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            return self.values[name]

        def write_variable(self, name, value):
            self.values[name] = value
            self.writes.append((name, value))

        def wait_variable_equal(self, name, expected, interval=0.2):
            self.waits.append((name, expected, interval))
            if name == NODE_PROCESS_COMPLETE:
                self.values[name] = expected
                slot = int(self.values.get("S082瓶盖暂存位", 0))
                if expected and slot:
                    sensor = s08_module.CAP_STORAGE_SLOT_SENSORS[slot]
                    self.values[sensor] = expected in s08_module.OPEN_PROCESS_IDS
            return True

        def wait_sensor_conditions(self, conditions, interval=0.2, context=None):
            del interval, context
            values = {name: self.read_variable(name, use_cache=False) for name in conditions}
            return all(values[name] == expected for name, expected in conditions.items()), values

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=4;s=上位机通讯|{variable_name}"

    gateway = FakeS08PlcGateway()
    device = SZLabS08CapStationDevice(
        use_plc_gateway=True,
        poll_interval=0.05,
    )
    device.set_plc_gateway(gateway)

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is True
    assert (NODE_PROCESS_SELECT, int(S08ProcessType.OPEN_LIQUID_VIAL_100ML)) in gateway.writes
    assert (NODE_PARAMS_WRITTEN, True) in gateway.writes
    assert (NODE_PROCESS_COMPLETE, 0) not in gateway.writes
    assert gateway.waits == [
        ("S08原点信号", True, 0.05),
        ("S08允许加工", True, 0.05),
        (NODE_PROCESS_COMPLETE, int(S08ProcessType.OPEN_LIQUID_VIAL_100ML), 0.05),
        (NODE_PROCESS_COMPLETE, 0, 0.05),
    ]


def test_device_init_resets_unilab_written_params_on_connect():
    pseudo = PseudoSzlabS08OpcUaClient(
        initial_values={
            NODE_PROCESS_SELECT: 6,
            NODE_PARAMS_WRITTEN: True,
            "S082瓶盖暂存位": 2,
        }
    )
    _device, client = make_s08_device(pseudo)

    assert client.values[NODE_PROCESS_SELECT] == 0
    assert client.values[NODE_PARAMS_WRITTEN] is False
    assert client.values["S082瓶盖暂存位"] == 0
    assert (NODE_PROCESS_COMPLETE, 0) not in client.writes


def test_build_opcua_node_id_map_for_uplink_comm_includes_handshake_and_cache_nodes():
    node_id_map = s08_module.build_opcua_node_id_map_for_uplink_comm("ns=4;s=上位机通讯")

    assert node_id_map[NODE_PROCESS_SELECT] == "ns=4;s=上位机通讯|S08工艺选择"
    assert node_id_map["传感器状态_上位机[3].NO[14]"] == "ns=4;s=上位机通讯|传感器状态_上位机[3].NO[14]"
    assert node_id_map[_cap_cache_element_name(1, 0)] == "ns=4;s=上位机通讯|S082_1数据缓存[0]"
    assert node_id_map[_cap_cache_element_name(5, 29)] == "ns=4;s=上位机通讯|S082_5数据缓存[29]"


def test_wait_process_complete_waits_until_process_complete_equals_expected():
    pseudo = PseudoSzlabS08OpcUaClient()
    device = SZLabS08CapStationDevice(
        url="opc.tcp://127.0.0.1:50102/",
        poll_interval=0.05,
        opcua_client=pseudo,
    )
    process_id = int(S08ProcessType.OPEN_LIQUID_VIAL_100ML)

    pseudo.values[NODE_PROCESS_SELECT] = process_id
    pseudo.values[NODE_PARAMS_WRITTEN] = True
    assert device._wait_process_complete(process_id) is True


def test_is_virtual_test_opcua_url():
    assert s08_module._is_virtual_test_opcua_url("opc.tcp://127.0.0.1:50102/") is True
    assert s08_module._is_virtual_test_opcua_url("opc.tcp://192.168.1.10:4840/") is False


def test_real_opcua_url_auto_uses_uplink_prefix():
    with patch.object(s08_module, "SZLabPolyPLCDevice") as mock_cls:
        mock_cls.return_value = MagicMock()
        s08_module.SZLabS08CapStationDevice(
            url="opc.tcp://192.168.1.10:4840/",
            opcua_uplink_comm_prefix=None,
        )
        node_id_map = mock_cls.call_args.kwargs["node_id_map"]
        assert mock_cls.call_args.kwargs["csv_path"] is False
        assert node_id_map[NODE_PROCESS_SELECT] == "ns=4;s=上位机通讯|S08工艺选择"


def test_process_cap_open_liquid_vial_writes_sample_id_to_slot_cache():
    device, client = make_s08_device()
    client.seed_slot_sample_id(1, SAMPLE_B)
    client.seed_slot_sample_id(2, SAMPLE_B)

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )

    assert result["success"] is True
    assert result["process_type"] == int(S08ProcessType.OPEN_LIQUID_VIAL_100ML)
    assert result["cap_storage_slot"] == 3
    assert result["sample_id"][: len(SAMPLE_A)] == SAMPLE_A
    assert (NODE_PROCESS_SELECT, int(S08ProcessType.OPEN_LIQUID_VIAL_100ML)) in client.writes
    assert ("S082瓶盖暂存位", 3) in client.writes
    assert (_cap_cache_element_name(3, 0), 101) in client.writes
    assert (NODE_PARAMS_WRITTEN, True) in client.writes
    assert client.writes[-1] == ("S082瓶盖暂存位", 0)


def test_process_cap_open_sample_500ml_uses_process_one():
    device, client = make_s08_device()
    client.seed_slot_sample_id(1, SAMPLE_A)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_SAMPLE_VIAL_500ML),
        样品ID=SAMPLE_B,
        瓶盖暂存位=0,
    )

    assert result["success"] is True
    assert result["process_type"] == int(S08ProcessType.OPEN_SAMPLE_VIAL_500ML)
    assert result["cap_storage_slot"] == 2
    assert (NODE_PROCESS_SELECT, 1) in client.writes


def test_process_cap_sample_250ml_dispatches_open_and_close():
    device, client = make_s08_device()
    client.seed_slot_sample_id(1, SAMPLE_B)
    open_result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_SAMPLE_VIAL_250ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )
    client.set_cap_storage_slot_present(open_result["cap_storage_slot"], True)
    close_result = device.process_cap(
        工艺选择=int(S08ProcessType.CLOSE_SAMPLE_VIAL_250ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )

    assert open_result["success"] is True
    assert open_result["process_type"] == int(S08ProcessType.OPEN_SAMPLE_VIAL_250ML)
    assert open_result["cap_storage_slot"] == 2
    assert close_result["success"] is True
    assert close_result["process_type"] == int(S08ProcessType.CLOSE_SAMPLE_VIAL_250ML)
    assert (NODE_PROCESS_SELECT, int(S08ProcessType.OPEN_SAMPLE_VIAL_250ML)) in client.writes
    assert (NODE_PROCESS_SELECT, int(S08ProcessType.CLOSE_SAMPLE_VIAL_250ML)) in client.writes
    assert (_cap_cache_element_name(2, 0), 0) in client.writes


def test_process_cap_rejects_unknown_process_choice():
    device, _client = make_s08_device()

    result = device.process_cap(工艺选择=99, 样品ID=SAMPLE_A)

    assert result["success"] is False
    assert "工艺选择" in result["message"]


def test_process_cap_open_auto_allocates_first_empty_cache_slot():
    device, client = make_s08_device()
    client.seed_slot_sample_id(1, SAMPLE_B)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )

    assert result["success"] is True
    assert result["cap_storage_slot"] == 2
    assert ("S082瓶盖暂存位", 2) in client.writes
    assert (_cap_cache_element_name(2, 0), SAMPLE_A[0]) in client.writes


def test_process_cap_open_default_auto_allocates_first_free_slot():
    device, client = make_s08_device()
    client.seed_slot_sample_id(1, SAMPLE_B)
    client.seed_slot_sample_id(2, SAMPLE_B)

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is True
    assert result["cap_storage_slot"] == 3
    assert ("S082瓶盖暂存位", 3) in client.writes


def test_process_cap_open_uses_explicit_cap_storage_slot():
    device, client = make_s08_device()

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=4,
    )

    assert result["success"] is True
    assert result["cap_storage_slot"] == 4
    assert ("S082瓶盖暂存位", 4) in client.writes
    assert (_cap_cache_element_name(4, 0), SAMPLE_A[0]) in client.writes
    assert (_cap_cache_element_name(1, 0), SAMPLE_A[0]) not in client.writes


def test_process_cap_open_requires_sample_id():
    device, _client = make_s08_device()
    result = device.process_cap(工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML), 样品ID=[])

    assert result["success"] is False
    assert "样品ID" in result["message"]


def test_process_cap_close_requires_sample_id():
    device, _client = make_s08_device()
    result = device.process_cap(工艺选择=int(S08ProcessType.CLOSE_LIQUID_VIAL_100ML), 样品ID=[0, 0, 0])
    assert result["success"] is False
    assert "样品ID" in result["message"]


def test_process_cap_close_finds_slot_by_sample_id_and_clears_cache():
    device, client = make_s08_device()
    client.seed_slot_sample_id(4, SAMPLE_A)
    client.set_cap_storage_slot_present(4, True)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.CLOSE_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )

    assert result["success"] is True
    assert result["cap_storage_slot"] == 4
    assert (NODE_PROCESS_SELECT, int(S08ProcessType.CLOSE_LIQUID_VIAL_100ML)) in client.writes
    assert ("S082瓶盖暂存位", 4) in client.writes
    assert (_cap_cache_element_name(4, 0), 0) in client.writes


def test_process_cap_close_fails_when_sample_not_found():
    device, _client = make_s08_device(validate_cap_constraints=True)
    result = device.process_cap(工艺选择=int(S08ProcessType.CLOSE_LIQUID_VIAL_100ML), 样品ID=SAMPLE_A)

    assert result["success"] is False
    assert "尚未开盖" in result["message"]


def test_process_cap_open_fails_when_sample_already_opened_on_storage_slot():
    device, client = make_s08_device(validate_cap_constraints=True)
    client.seed_slot_sample_id(1, SAMPLE_A)
    client.set_cap_storage_slot_present(1, True)

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is False
    assert "已开盖" in result["message"]
    assert "不能对同一瓶重复开盖" in result["message"]
    assert "暂存位1" in result["message"]


def test_process_cap_waits_for_cap_station_bottle_through_sensor_waiter(monkeypatch):
    device, client = make_s08_device(validate_cap_constraints=True)
    sensor_waits = []

    def wait_for_sensor_conditions(conditions, *, phase):
        sensor_waits.append((conditions, phase))
        return {
            "success": True,
            "phase": phase,
            "conditions": conditions,
            "values": conditions,
            "mismatches": {},
        }

    monkeypatch.setattr(device, "_wait_cap_sensor_conditions", wait_for_sensor_conditions)
    monkeypatch.setattr(
        device,
        "_validate_cap_station_has_bottle",
        lambda _process_type: (_ for _ in ()).throw(
            AssertionError("process_cap 不应在入口单次读取开盖工位传感器")
        ),
    )
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_SAMPLE_VIAL_500ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is True
    assert sensor_waits == [
        (
            {
                "传感器状态_上位机[3].NO[14]": True,
                "传感器状态_上位机[4].NO[0]": False,
            },
            "pre",
        ),
        (
            {
                "传感器状态_上位机[3].NO[14]": True,
                "传感器状态_上位机[4].NO[0]": True,
            },
            "post",
        ),
    ]


def test_process_cap_open_requires_empty_cap_storage_sensor_without_optional_validation():
    device, client = make_s08_device(validate_cap_constraints=False)
    client.set_cap_storage_slot_present(1, True)

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=1,
    )

    assert result["success"] is False
    assert result["sensor_precheck"]["mismatches"]["传感器状态_上位机[4].NO[0]"] == {
        "expected": False,
        "actual": True,
    }
    assert (NODE_PARAMS_WRITTEN, True) not in client.writes


def test_process_cap_reports_verification_failed_when_cap_sensor_does_not_change():
    class NoSensorTransitionClient(PseudoSzlabS08OpcUaClient):
        def read(self, name):
            if name == NODE_PROCESS_COMPLETE:
                if self.values.get(NODE_PARAMS_WRITTEN):
                    return int(self.values.get(NODE_PROCESS_SELECT, 0))
                return int(self.values.get(NODE_PROCESS_COMPLETE, 0))
            return super().read(name)

    device, client = make_s08_device(NoSensorTransitionClient())

    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=1,
    )

    assert result["success"] is False
    assert result["status"] == "verification_failed"
    assert result["sensor_postcheck"]["mismatches"]["传感器状态_上位机[4].NO[0]"] == {
        "expected": True,
        "actual": False,
    }


def test_process_cap_waits_until_non_alarm_station_status_is_ready():
    class BecomingReadyClient(PseudoSzlabS08OpcUaClient):
        def __init__(self):
            super().__init__()
            self.station_status_reads = 0

        def read(self, name):
            if name == NODE_STATION_STATUS:
                self.station_status_reads += 1
                return 1 if self.station_status_reads == 1 else 2
            return super().read(name)

    device, client = make_s08_device(BecomingReadyClient(), require_station_status=True)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is True
    assert client.station_status_reads >= 2


def test_process_cap_rejects_alarm_station_status_without_waiting():
    class AlarmClient(PseudoSzlabS08OpcUaClient):
        def __init__(self):
            super().__init__()
            self.station_status_reads = 0

        def read(self, name):
            if name == NODE_STATION_STATUS:
                self.station_status_reads += 1
                return 0
            return super().read(name)

    device, client = make_s08_device(AlarmClient(), require_station_status=True)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is False
    assert "工站未就绪" in result["message"]
    assert NODE_STATION_STATUS in result["message"]
    assert "OPC UA" in result["message"]
    assert client.station_status_reads == 1


def test_process_cap_skips_station_status_check_when_disabled():
    device, client = make_s08_device(require_station_status=False)
    client.set_station_status(0)
    client.values["传感器状态_上位机[3].NO[15]"] = True
    result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
        瓶盖暂存位=0,
    )

    assert result["success"] is True


def test_process_cap_keeps_physical_sensor_checks_when_business_constraints_disabled():
    device, client = make_s08_device(validate_cap_constraints=False)
    client.set_cap_station_present(2, False)
    client.seed_slot_sample_id(1, SAMPLE_A)
    client.set_cap_storage_slot_present(1, True)

    open_result = device.process_cap(
        工艺选择=int(S08ProcessType.OPEN_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_B,
    )
    assert open_result["success"] is False
    assert "等待瓶体与瓶盖暂存位状态失败" in open_result["message"]
    assert open_result["sensor_precheck"]["mismatches"]["传感器状态_上位机[3].NO[15]"]["actual"] is False
    assert (NODE_PARAMS_WRITTEN, True) not in client.writes


def test_process_cap_close_fails_when_cap_storage_slot_empty():
    device, client = make_s08_device(validate_cap_constraints=True)
    client.seed_slot_sample_id(3, SAMPLE_A)
    result = device.process_cap(
        工艺选择=int(S08ProcessType.CLOSE_LIQUID_VIAL_100ML),
        样品ID=SAMPLE_A,
    )

    assert result["success"] is False
    assert "尚未开盖" in result["message"] or "无瓶盖" in result["message"]
