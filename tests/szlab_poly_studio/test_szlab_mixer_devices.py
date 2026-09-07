import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
    S06PipelineRoute,
    default_s06_pipeline_routes,
    s06_pump_valve_var,
)
from unilabos.devices.workstation.szlab_poly_studio.plc import (
    SZLabPolyPLCDevice,
    load_variable_aliases_from_csv,
    load_variable_definitions_from_csv,
    wait_sensor_conditions,
    wait_variable_true,
)
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    load_sensor_bit_metadata_from_csv,
)
from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring import (
    SzlabMixerMagneticStirrerDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting import SzlabMixerPhotoShottingDevice
from unilabos.devices.workstation.szlab_poly_studio.sensor import S07Sensors
from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07 import SZLabS07SolidAdditionDevice
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import (
    GRIPPER_ORIGIN_VARIABLE,
    GRIPPER_POSITION_VARIABLES,
    GRIPPER_STATUS_VARIABLE,
    SzlabMixerRobotDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S04 import S04_SENSOR_BY_POSITION
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import ROBOT_ACTION_SPECS
from scripts.run_workflow_local import clear_pc_to_plc_variables, create_local_devices, load_runtime_config
from scripts.run_workflow_local import WorkflowLogger, WorkflowNode, run_nodes
from scripts.workflow_ui import _load_preset_runtime_config, build_graph_workflow, load_preset


def test_szlab_mixer_devices_are_ast_scannable():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s06_pump")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_pump"}
    assert "run_solvent_addition" in result["devices"]["szlab_mixer_pump"]["actions"]
    assert "transfer_liquid" in result["devices"]["szlab_mixer_pump"]["actions"]


def test_szlab_wait_variable_true_reuses_read_variable_and_interval(monkeypatch):
    class FakeReader:
        def __init__(self):
            self.values = [False, False, True]
            self.reads = []

        def read_variable(self, name, use_cache=False):
            self.reads.append((name, use_cache))
            return self.values.pop(0)

    sleeps = []
    reader = FakeReader()
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    assert wait_variable_true(reader, "S05加工完成", interval=1.0) is True
    assert reader.reads == [
        ("S05加工完成", False),
        ("S05加工完成", False),
        ("S05加工完成", False),
    ]
    assert sleeps == [1.0, 1.0]


def test_s071_auto_place_position_selects_first_empty_slot():
    class FakePlc:
        def read_variable(self, name, use_cache=False):
            del use_cache
            occupied = {
                S07Sensors.POWDER_CONTAINER_BY_POSITION["1-1"]: True,
                S07Sensors.POWDER_CONTAINER_BY_POSITION["1-2"]: False,
            }
            return occupied.get(name, True)

    robot = SzlabMixerRobotDevice()
    robot.set_plc_gateway(FakePlc())

    assert robot._resolve_s071_place_position("auto") == "1-2"


def test_s071_auto_place_position_waits_until_a_slot_is_empty(monkeypatch):
    class FakePlc:
        def __init__(self):
            self.round = 0

        def read_variable(self, name, use_cache=False):
            del use_cache
            if name == list(S07Sensors.POWDER_CONTAINER_BY_POSITION.values())[-1]:
                self.round += 1
            return self.round < 2

    robot = SzlabMixerRobotDevice()
    robot.set_plc_gateway(FakePlc())
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S07.time.sleep",
        lambda _seconds: None,
    )

    assert robot._resolve_s071_place_position("auto") == list(S07Sensors.POWDER_CONTAINER_BY_POSITION)[-1]


def test_s071_pick_and_rotate_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(2)
    calls = []
    robot = SzlabMixerRobotDevice()
    robot.set_plc_gateway(object())
    sensor_waits = []
    monkeypatch.setattr(
        robot,
        "_wait_sensor_conditions",
        lambda conditions, *, phase: sensor_waits.append((conditions, phase)) or {"success": True},
    )
    monkeypatch.setattr(robot, "_run_robot_handshake_precheck", lambda station: {"target_station": station})

    def pick(position):
        calls.append(("pick", position))
        barrier.wait(timeout=1.0)
        return {"success": True, "task": "pick"}

    def wait_ready(_self, _name, _expected, _description):
        return True

    def rotate(_self, position):
        calls.append(("rotate", position))
        barrier.wait(timeout=1.0)
        return {"success": True, "process_type": 2}

    monkeypatch.setattr(robot, "_run_s071_pick", pick)
    monkeypatch.setattr(SZLabS07SolidAdditionDevice, "_wait_plc_bool", wait_ready)
    monkeypatch.setattr(SZLabS07SolidAdditionDevice, "rotate_powder_cartridge_to_feed", rotate)

    result = robot.submit_pick_from_s071_and_rotate_to_feed(
        position="1-1",
        load_position=2,
    )

    assert result["success"] is True
    assert result["status"] == "completed"
    assert sorted(call[0] for call in calls) == ["pick", "rotate"]
    assert sensor_waits == [({S07Sensors.POWDER_CONTAINER_BY_POSITION["1-1"]: True}, "pre")]
    assert result["robot_pick"]["success"] is True
    assert result["s07_rotate"]["success"] is True


def test_s071_pick_and_rotate_partial_failure_is_not_retried(monkeypatch):
    calls = {"pick": 0, "rotate": 0}
    robot = SzlabMixerRobotDevice()
    robot.set_plc_gateway(object())
    monkeypatch.setattr(
        robot,
        "_wait_sensor_conditions",
        lambda _conditions, *, phase: {"success": True, "phase": phase},
    )
    monkeypatch.setattr(robot, "_run_robot_handshake_precheck", lambda station: {"target_station": station})

    def pick(_position):
        calls["pick"] += 1
        return {"success": True}

    def wait_ready(_self, _name, _expected, _description):
        return True

    def rotate(_self, _position):
        calls["rotate"] += 1
        return {"success": False, "message": "旋转失败"}

    monkeypatch.setattr(robot, "_run_s071_pick", pick)
    monkeypatch.setattr(SZLabS07SolidAdditionDevice, "_wait_plc_bool", wait_ready)
    monkeypatch.setattr(SZLabS07SolidAdditionDevice, "rotate_powder_cartridge_to_feed", rotate)

    result = robot.submit_pick_from_s071_and_rotate_to_feed()

    assert result["success"] is False
    assert result["status"] == "partial_failure"
    assert "禁止自动重试" in result["message"]
    assert calls == {"pick": 1, "rotate": 1}


def test_szlab_wait_sensor_conditions_reads_all_values_without_cache(monkeypatch):
    class FakeReader:
        def __init__(self):
            self.cycles = [
                {"sensor_a": True, "sensor_b": False},
                {"sensor_a": True, "sensor_b": True},
            ]
            self.reads = []

        def read_variable(self, name, use_cache=False):
            self.reads.append((name, use_cache))
            value = self.cycles[0][name]
            if name == "sensor_b":
                self.cycles.pop(0)
            return value

    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc.time.sleep",
        lambda _seconds: None,
    )
    reader = FakeReader()

    success, values = wait_sensor_conditions(
        reader,
        {"sensor_a": True, "sensor_b": True},
        interval=0.2,
    )

    assert success is True
    assert values == {"sensor_a": True, "sensor_b": True}
    assert reader.reads == [
        ("sensor_a", False),
        ("sensor_b", False),
        ("sensor_a", False),
        ("sensor_b", False),
    ]


def test_szlab_wait_sensor_conditions_propagates_read_errors():
    class FakeReader:
        def read_variable(self, name, use_cache=False):
            raise RuntimeError(f"读取失败: {name}")

    with pytest.raises(RuntimeError, match="读取失败"):
        wait_sensor_conditions(FakeReader(), {"sensor_a": True})


def test_szlab_plc_wait_variable_equal_records_start_and_finish_events(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)
    device.values = [False, True]
    device.reads = []

    def read_variable(name, use_cache=False):
        device.reads.append((name, use_cache))
        return device.values.pop(0)

    monkeypatch.setattr(device, "read_variable", read_variable)
    monkeypatch.setattr(
        device,
        "get_opc_variable_metadata",
        lambda name: (name, f"ns=4;s={name}"),
    )
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc.time.sleep",
        lambda _seconds: None,
    )

    assert device.wait_variable_equal("S06加工完成", True, interval=0.2) is True

    events = device.drain_opc_wait_events()
    assert [event["phase"] for event in events] == ["start", "finish"]
    assert events[0]["message"] == "等待 OPC 变量 S06加工完成 == True (interval=0.2s)"
    assert events[0]["detail"] == {
        "type": "opc_wait",
        "phase": "start",
        "variable": "S06加工完成",
        "expected": True,
        "interval": 0.2,
        "display_name": "S06加工完成",
        "node_id": "ns=4;s=S06加工完成",
        "label": "S06加工完成 (ns=4;s=S06加工完成)",
    }
    assert events[1]["message"].startswith("OPC 变量等待完成 S06加工完成 == True: success=True")
    assert events[1]["detail"]["phase"] == "finish"
    assert events[1]["detail"]["success"] is True
    assert events[1]["detail"]["last_value"] is True
    assert events[1]["detail"]["node_id"] == "ns=4;s=S06加工完成"
    assert device.drain_opc_wait_events() == []


def test_szlab_plc_empty_stack_status_group_list_reads_no_sensors():
    device = object.__new__(SZLabPolyPLCDevice)
    device.stack_sensor_groups = {"s10_liquid_reagent": {"1-1": "传感器状态_上位机[4].NO[12]"}}

    def fail_read_sensor_group(_sensors):
        raise AssertionError("空 stack_status_groups 不应读取任何传感器")

    device._read_sensor_group = fail_read_sensor_group

    assert device._read_stack_sensor_groups(group_names=[]) == {}


def test_szlab_plc_sensor_group_reads_real_boolean_array_once(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)
    device._sensor_read_warning_names = set()
    reads = []

    def read_sensor_array(group_index):
        reads.append(group_index)
        return [index in {10, 12} for index in range(16)]

    monkeypatch.setattr(device, "_read_sensor_array", read_sensor_array)

    status = device._read_sensor_group(
        {
            "1": "传感器状态_上位机[2].NO[10]",
            "2": "传感器状态_上位机[2].NO[11]",
            "3": "传感器状态_上位机[2].NO[12]",
        }
    )

    assert status == {"1": True, "2": False, "3": True}
    assert reads == [2]


def test_szlab_plc_get_sensor_arrays_includes_csv_metadata(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)
    device._sensor_bit_metadata = {
        "传感器状态_上位机[2].NO[10]": {
            "label": "搅拌1-1",
            "address": "R10002.10",
            "node_id": "ns=4;s=bit",
        }
    }
    device._direct_node_id_map = {
        "传感器状态_上位机[2].NO": "ns=4;s=array",
    }
    monkeypatch.setattr(
        device,
        "_read_sensor_array",
        lambda group_index: [group_index == 2 and bit_index == 10 for bit_index in range(16)],
    )

    payload = device.get_sensor_arrays()

    assert payload["success"] is True
    assert len(payload["groups"]) == 10
    group = payload["groups"][2]
    assert group["node_id"] == "ns=4;s=array"
    assert group["values"][10] is True
    assert group["bits"][10] == {
        "index": 10,
        "name": "传感器状态_上位机[2].NO[10]",
        "value": True,
        "label": "搅拌1-1",
        "address": "R10002.10",
        "node_id": "ns=4;s=bit",
    }


def test_clear_pc_to_plc_variables_treats_failed_write_as_success_when_already_clear():
    class FakePlcGateway:
        def __init__(self):
            self.writes = []
            self.reads = []

        def write_variable(self, name, value):
            self.writes.append((name, value))
            if name == "Robot_任务写入完成":
                raise RuntimeError("写入 PLC 变量失败: Robot_任务写入完成: BadWriteNotSupported")
            return True

        def read_variable(self, name, use_cache=False):
            self.reads.append((name, use_cache))
            if name == "Robot_任务写入完成":
                return False
            raise KeyError(name)

    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json")
    gateway = FakePlcGateway()

    result = clear_pc_to_plc_variables({runtime_config.device_factory.plc_device_id: gateway}, runtime_config)

    assert result["errors"] == {}
    assert result["already_clear_variables"] == {"Robot_任务写入完成": False}
    assert ("Robot_任务写入完成", False) in gateway.writes
    assert gateway.reads == [("Robot_任务写入完成", False)]


def test_szlab_plc_write_reports_direct_node_id_unknown_without_browse_retry(monkeypatch):
    class FakeNode:
        def __init__(self, node_id: str):
            self.node_id = node_id

    device = object.__new__(SZLabPolyPLCDevice)
    stale_node = FakeNode("stale")
    writes = []
    device.client = object()
    device._node_registry = {"Robot_任务写入完成": stale_node}
    device._variables_to_find = {"Robot_任务写入完成": {}}
    device._found_node_objects = {"Robot_任务写入完成": stale_node}
    device._name_mapping = {}
    device._direct_node_id_map = {"Robot_任务写入完成": "ns=4;s=上位机通讯|Robot_任务写入完成"}
    device._opc_io_lock = threading.RLock()
    device.use_node = lambda node_name: device._node_registry[node_name]

    def fake_write(node, value):
        writes.append((node, value))
        raise RuntimeError(
            '"The node id refers to a node that does not exist in the server address space."(BadNodeIdUnknown)'
        )

    monkeypatch.setattr(device, "_write_value_only", fake_write)

    with pytest.raises(RuntimeError, match="直连 NodeId 无效"):
        device.write_variable("Robot_任务写入完成", False)
    assert writes == [(stale_node, False)]
    assert device._node_registry == {"Robot_任务写入完成": stale_node}
    assert device._found_node_objects == {"Robot_任务写入完成": stale_node}


def test_szlab_photoshotting_device_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s05_photoshotting")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_photoshotting"}
    actions = result["devices"]["szlab_mixer_photoshotting"]["actions"]
    assert list(actions) == ["take_photo"]


def test_szlab_magnetic_stirrer_device_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s04_magnetic_stirring")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_stirrer"}
    actions = result["devices"]["szlab_mixer_stirrer"]["actions"]
    assert list(actions) == ["run_stirring"]


def test_szlab_robot_device_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s12_robot")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_robot"}
    actions = result["devices"]["szlab_mixer_robot"]["actions"]
    assert list(actions) == [
        "submit_pick_from_s01",
        "submit_place_to_s02",
        "submit_pick_from_s02",
        "submit_place_to_s03",
        "submit_pick_from_s03",
        "submit_place_to_s04",
        "submit_pick_from_s04",
        "submit_place_to_s05",
        "submit_pick_from_s05",
        "submit_place_to_s06",
        "submit_pick_from_s06",
        "submit_place_to_s071",
        "submit_pick_from_s071",
        "submit_pick_from_s071_and_rotate_to_feed",
        "submit_place_to_s072",
        "submit_pick_from_s072",
        "submit_place_to_s08",
        "submit_pick_from_s08",
        "submit_pour_from_s08",
        "submit_place_to_s09",
        "submit_pick_from_s09",
        "submit_place_to_s10",
        "submit_pick_from_s10",
        "submit_place_to_s11",
        "submit_pick_from_s11",
        "last_submitted_task",
    ]
    place_s09_handles = actions["submit_place_to_s09"]["action_args"]["handles"]
    assert [handle["label"] for handle in place_s09_handles] == ["S09取放料产品", "S09取放料编号"]
    assert place_s09_handles[0]["description"] == "S09取放料产品：1=TIP盒，2=液体试剂瓶，3=烧杯，4=测密度烧杯"
    assert place_s09_handles[1]["description"] == "S09取放料编号：TIP盒 1-2，液体试剂瓶 1-5，烧杯 1"


def test_szlab_magnetic_stirrer_run_stirring_writes_s041_parameters():
    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.writes = []
            self.events = []
            self.done_values = [False, False, True]

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            self.events.append(("read", name))
            if name == "S041磁搅状态":
                return 1
            if name == "S041允许加工":
                return True
            if name == "S041加工完成":
                return self.done_values.pop(0)
            if name == "传感器状态_上位机[2].NO[10]":
                return True
            raise KeyError(name)

        def write_variable(self, name, value):
            self.writes.append((name, value))
            self.events.append(("write", name, value))
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(
        position=1,
        mode=3,
        speed=300,
        temperature=60,
        duration=30,
        safe_temperature=80,
    )

    assert result["success"] is True
    assert result["data"]["station"] == "S041"
    assert gateway.reads == [
        "传感器状态_上位机[2].NO[10]",
        "S041磁搅状态",
        "S041允许加工",
        "S041加工完成",
        "S041加工完成",
        "S041加工完成",
        "传感器状态_上位机[2].NO[10]",
    ]
    assert gateway.writes == [
        ("S041磁搅工艺选择", 3),
        ("磁搅速度设置_上位机[0]", 300),
        ("磁搅温度设置_上位机[0]", 60),
        ("磁搅时间设置_上位机[0]", 30000),
        ("磁搅安全温度设置_上位机[0]", 80),
        ("S041参数写入完成", True),
        # PLC 报加工完成后，PC 再 reset 本轮参数。
        ("S041磁搅工艺选择", 0),
        ("磁搅速度设置_上位机[0]", 0),
        ("磁搅温度设置_上位机[0]", 0),
        ("磁搅时间设置_上位机[0]", 0),
        ("磁搅安全温度设置_上位机[0]", 0),
        ("S041参数写入完成", False),
    ]
    done_index = gateway.events.index(("read", "S041加工完成"))
    reset_index = gateway.events.index(("write", "S041磁搅工艺选择", 0))
    assert done_index < reset_index
    assert all(name != "S041加工完成" for name, _value in gateway.writes)


def test_szlab_magnetic_stirrer_waits_for_idle_status_before_writing(monkeypatch):
    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.writes = []
            self.status_values = [0, 1]
            self.done_values = [False, True]

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            if name == "S041磁搅状态":
                return self.status_values.pop(0)
            if name == "S041允许加工":
                return True
            if name == "S041加工完成":
                return self.done_values.pop(0)
            if name == "传感器状态_上位机[2].NO[10]":
                return True
            raise KeyError(name)

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc.time.sleep",
        lambda _seconds: None,
    )
    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is True
    assert gateway.reads[:4] == [
        "传感器状态_上位机[2].NO[10]",
        "S041磁搅状态",
        "S041磁搅状态",
        "S041允许加工",
    ]
    assert ("S041磁搅工艺选择", 3) in gateway.writes


def test_szlab_magnetic_stirrer_idle_status_wait_failure_before_writing():
    class FakePlcGateway:
        def __init__(self):
            self.waits = []
            self.writes = []

        def wait_equal(self, name, expected, interval=1.0):
            self.waits.append((name, expected, interval))
            return False

        def wait_variable_true(self, name, interval=1.0):
            self.waits.append((name, interval))
            return name == "传感器状态_上位机[2].NO[10]"

        def read_variable(self, name, use_cache=False):
            raise AssertionError("磁搅状态应通过 wait_equal 等待")

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is False
    assert result["message"] == "S041 磁搅状态等待空闲失败（期望 1）"
    assert gateway.waits == [
        ("传感器状态_上位机[2].NO[10]", 1.0),
        ("S041磁搅状态", 1, 1.0),
    ]
    assert gateway.writes == []


def test_szlab_magnetic_stirrer_rejects_missing_material_before_writing():
    class FakePlcGateway:
        def __init__(self):
            self.writes = []

        def wait_variable_true(self, name, interval=1.0):
            assert name == "传感器状态_上位机[2].NO[10]"
            return False

        def write_variable(self, name, value):
            self.writes.append((name, value))

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is False
    assert result["message"] == "S041 等待搅拌位置有料失败"
    assert gateway.writes == []


def test_szlab_magnetic_stirrer_reports_missing_material_after_completion():
    class FakePlcGateway:
        def __init__(self):
            self.material_waits = 0
            self.writes = []

        def wait_variable_true(self, name, interval=1.0):
            if name == "传感器状态_上位机[2].NO[10]":
                self.material_waits += 1
                return self.material_waits == 1
            return True

        def wait_equal(self, name, expected, interval=1.0):
            return True

        def write_variable(self, name, value):
            self.writes.append((name, value))

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is False
    assert result["status"] == "verification_failed"
    assert "物料在位验证失败" in result["message"]
    assert ("S041参数写入完成", False) in gateway.writes


def test_szlab_magnetic_stirrer_waits_for_new_done_cycle_when_done_is_stale_true():
    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.writes = []
            self.done_values = [True, False, True]

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            if name == "S041磁搅状态":
                return 1
            if name == "S041允许加工":
                return True
            if name == "S041加工完成":
                return self.done_values.pop(0)
            if name == "传感器状态_上位机[2].NO[10]":
                return True
            raise KeyError(name)

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is True
    assert [name for name in gateway.reads if name == "S041加工完成"] == [
        "S041加工完成",
        "S041加工完成",
        "S041加工完成",
    ]


def test_szlab_magnetic_stirrer_treats_done_wait_timeout_as_success():
    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.writes = []

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            values = {
                "传感器状态_上位机[2].NO[10]": True,
                "S041磁搅状态": 1,
                "S041允许加工": True,
                "S041加工完成": False,
            }
            return values[name]

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

        def wait_new_cycle_done(self, name, interval=1.0):
            return False

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is True
    assert result["message"] == "S041 磁搅已达到规定时间，按正常完成处理"
    assert result["data"]["done_signal_received"] is False
    assert ("S041磁搅工艺选择", 0) in gateway.writes
    assert ("磁搅速度设置_上位机[0]", 0) in gateway.writes
    assert ("磁搅温度设置_上位机[0]", 0) in gateway.writes
    assert ("磁搅时间设置_上位机[0]", 0) in gateway.writes
    assert ("磁搅安全温度设置_上位机[0]", 0) in gateway.writes
    assert ("S041参数写入完成", False) in gateway.writes


def test_szlab_magnetic_stirrer_bounds_done_wait_by_duration_plus_grace():
    class FakePlcGateway:
        def __init__(self):
            self.done_waits = []
            self.writes = []

        def wait_variable_true(self, name, interval=1.0):
            return True

        def wait_equal(self, name, expected, interval=1.0):
            return True

        def wait_new_cycle_done(self, name, interval=1.0, timeout=None):
            self.done_waits.append((name, interval, timeout))
            return False

        def write_variable(self, name, value):
            self.writes.append((name, value))

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, duration=600)

    assert result["success"] is True
    assert result["data"]["done_signal_received"] is False
    assert gateway.done_waits[0][1:] == (1.0, 630.0)
    assert any(value is False for _name, value in gateway.writes)


def test_szlab_magnetic_stirrer_uses_plc_wait_helper_when_available():
    class FakePlcGateway:
        def __init__(self):
            self.waits = []
            self.writes = []

        def wait_variable_true(self, name, interval=1.0):
            self.waits.append((name, interval))
            return True

        def wait_equal(self, name, expected, interval=1.0):
            self.waits.append((name, expected, interval))
            return True

        def read_variable(self, name, use_cache=False):
            raise AssertionError("应优先使用 wait helper")

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is True
    assert gateway.waits == [
        ("传感器状态_上位机[2].NO[10]", 1.0),
        ("S041磁搅状态", 1, 1.0),
        ("S041允许加工", 1.0),
        ("S041加工完成", False, 1.0),
        ("S041加工完成", 1.0),
        ("传感器状态_上位机[2].NO[10]", 1.0),
    ]


def test_szlab_magnetic_stirrer_does_not_mark_params_written_after_write_failure():
    class FakePlcGateway:
        def __init__(self):
            self.writes = []

        def read_variable(self, name, use_cache=False):
            if name == "S041磁搅状态":
                return 1
            return True

        def write_variable(self, name, value):
            self.writes.append((name, value))
            if name == "S041磁搅工艺选择":
                raise RuntimeError("写入 PLC 变量失败: S041磁搅工艺选择")
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, mode=3)

    assert result["success"] is False
    assert result["message"] == "写入 PLC 变量失败: S041磁搅工艺选择"
    assert ("S041参数写入完成", True) not in gateway.writes


def test_szlab_magnetic_stirrer_reset_restores_pc_to_plc_defaults():
    class FakePlcGateway:
        def __init__(self):
            self.writes = []

        def read_variable(self, name, use_cache=False):
            raise AssertionError("reset 不需要等待允许加工")

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    gateway = FakePlcGateway()
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_stirring(position=1, reset=True)

    assert result["success"] is True
    assert result["message"] == "S041 磁搅 PC->PLC 参数已恢复初始值"
    assert gateway.writes == [
        ("S041磁搅工艺选择", 0),
        ("磁搅速度设置_上位机[0]", 0),
        ("磁搅温度设置_上位机[0]", 0),
        ("磁搅时间设置_上位机[0]", 0),
        ("磁搅安全温度设置_上位机[0]", 0),
        ("S041参数写入完成", False),
    ]


def test_szlab_magnetic_stirrer_rejects_invalid_mode():
    device = SzlabMixerMagneticStirrerDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )

    result = device.run_stirring(position=1, mode=4)

    assert result == {"success": False, "message": "磁搅工艺选择必须是 1(搅拌)、2(加热)、3(搅拌+加热)"}


def test_szlab_photoshotting_debug_assets_use_current_s05_variables():
    device_dir = Path("unilabos/devices/workstation/szlab_poly_studio/s05_photoshotting")
    latest_csv = Path("unilabos/devices/workstation/szlab_poly_studio/szlab_plc_0721.csv")
    nodes_csv = device_dir / "photoshotting_nodes.csv"
    flow_path = device_dir / "photoshotting_flow.json"
    config_path = device_dir / "photoshotting_debug.json"
    expected_names = {
        "S05加工完成",
        "S05拍照结果",
    }

    latest_text = latest_csv.read_text(encoding="utf-8-sig")
    for name in expected_names:
        assert name in latest_text

    node_names = {
        line.split(",", 2)[1]
        for line in nodes_csv.read_text(encoding="utf-8").splitlines()[1:]
        if line.strip()
    }
    assert node_names == expected_names

    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    assert flow["rules"] == []

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["virtual_opcua"]["csv"].endswith("s05_photoshotting/photoshotting_nodes.csv")
    assert config["device"]["csv_path"] == "s05_photoshotting/photoshotting_nodes.csv"
    assert config["device"]["opcua_node_id_map"] == {
        "S05加工完成": "ns=4;s=上位机通讯|S05加工完成",
        "S05拍照结果": "ns=4;s=上位机通讯|S05拍照结果",
    }
    assert config["action"]["name"] == "take_photo"


def test_szlab_photoshotting_take_photo_polls_done_every_second_until_complete(monkeypatch):
    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.done_values = [False, False, True]

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            if name == "传感器状态_上位机[3].NO[0]":
                return True
            if name == "S05加工完成":
                return self.done_values.pop(0)
            values = {"S05拍照结果": 1}
            return values[name]

    sleeps = []
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    gateway = FakePlcGateway()
    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.take_photo(sample_id="sample-1", require_material=True)

    assert result["success"] is True
    assert result["data"]["result"] == "OK"
    assert result["data"]["photo_url"] == ""
    assert gateway.reads == [
        "传感器状态_上位机[3].NO[0]",
        "S05加工完成",
        "S05加工完成",
        "S05加工完成",
        "传感器状态_上位机[3].NO[0]",
        "S05拍照结果",
    ]
    assert sleeps == [1.0, 1.0]


def test_szlab_photoshotting_uses_plc_wait_helper_when_available():
    class FakePlcGateway:
        def __init__(self):
            self.waits = []
            self.reads = []

        def wait_variable_true(self, name, interval=1.0):
            self.waits.append((name, interval))
            return True

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            values = {"S05拍照结果": 1}
            return values[name]

    gateway = FakePlcGateway()
    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is True
    assert gateway.waits == [
        ("传感器状态_上位机[3].NO[0]", 1.0),
        ("S05加工完成", 1.0),
        ("传感器状态_上位机[3].NO[0]", 1.0),
    ]
    assert gateway.reads == ["S05拍照结果"]


def test_szlab_photoshotting_waits_for_nonzero_photo_result_after_done(monkeypatch):
    class FakePlcGateway:
        def __init__(self):
            self.waits = []
            self.reads = []
            self.result_values = [0, 0, 1]

        def wait_variable_true(self, name, interval=1.0):
            self.waits.append((name, interval))
            return True

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            if name == "S05拍照结果":
                return self.result_values.pop(0)
            raise KeyError(name)

    sleeps = []
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    gateway = FakePlcGateway()
    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is True
    assert result["data"]["result_code"] == 1
    assert result["data"]["result"] == "OK"
    assert gateway.reads == ["S05拍照结果", "S05拍照结果", "S05拍照结果"]
    assert sleeps == [1.0, 1.0]


def test_szlab_photoshotting_rejects_missing_material_before_photo():
    class FakePlcGateway:
        def wait_variable_true(self, name, interval=1.0):
            assert name == "传感器状态_上位机[3].NO[0]"
            return False

        def read_variable(self, name, use_cache=False):
            raise AssertionError("无物料时不应读取拍照结果")

    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(FakePlcGateway())

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is False
    assert result["message"] == "S05 等待拍照位置有料失败"


def test_szlab_photoshotting_reports_material_missing_after_photo():
    class FakePlcGateway:
        def __init__(self):
            self.material_waits = 0

        def wait_variable_true(self, name, interval=1.0):
            if name == "传感器状态_上位机[3].NO[0]":
                self.material_waits += 1
                return self.material_waits == 1
            return True

        def read_variable(self, name, use_cache=False):
            raise AssertionError("后置物料验证失败时不应读取拍照结果")

    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(FakePlcGateway())

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is False
    assert result["status"] == "verification_failed"
    assert "物料在位验证失败" in result["message"]


def test_szlab_photoshotting_take_photo_fails_when_result_is_ng():
    class FakePlcGateway:
        def __init__(self):
            self.reads = []

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            values = {
                "传感器状态_上位机[3].NO[0]": True,
                "S05加工完成": True,
                "S05拍照结果": 2,
            }
            return values[name]

    gateway = FakePlcGateway()
    device = SzlabMixerPhotoShottingDevice(
        url="opc.tcp://127.0.0.1:0/",
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is False
    assert result["message"] == "S05 拍照检测 NG"
    assert result["data"]["result"] == "NG"
    assert gateway.reads == [
        "传感器状态_上位机[3].NO[0]",
        "S05加工完成",
        "传感器状态_上位机[3].NO[0]",
        "S05拍照结果",
    ]


def test_szlab_photoshotting_schedules_dissolution_without_blocking(monkeypatch):
    class FakePlcGateway:
        def wait_variable_true(self, name, interval=1.0):
            return True

        def read_variable(self, name, use_cache=False):
            return 1

    started_threads = []

    class DeferredThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started_threads.append(self)

    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting.threading.Thread",
        DeferredThread,
    )
    device = SzlabMixerPhotoShottingDevice(
        use_plc_gateway=True,
        dissolution_service_url="http://inference:8003/",
    )
    device.set_plc_gateway(FakePlcGateway())

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is True
    assert result["data"]["dissolution_detection_triggered"] is True
    assert "dissolution_result" not in result["data"]
    assert len(started_threads) == 1
    assert started_threads[0].kwargs["daemon"] is True
    assert started_threads[0].kwargs["args"] == ("sample-1",)
    assert json.loads(device.last_dissolution_result) == {
        "status": "scheduled",
        "sample_id": "sample-1",
        "solubility": "unknown",
        "delay_seconds": 2.0,
    }


def test_szlab_photoshotting_can_skip_dissolution_detection(monkeypatch):
    class FakePlcGateway:
        def wait_variable_true(self, name, interval=1.0):
            return True

        def read_variable(self, name, use_cache=False):
            return 1

    device = SzlabMixerPhotoShottingDevice(
        use_plc_gateway=True,
        dissolution_service_url="http://inference:8003/",
    )
    device.set_plc_gateway(FakePlcGateway())
    monkeypatch.setattr(
        device,
        "_start_dissolution_detection",
        lambda sample_id: pytest.fail("关闭溶解检测时不应启动异步任务"),
    )

    result = device.take_photo(
        sample_id="sample-1",
        trigger_dissolution_detection=False,
    )

    assert result["success"] is True
    assert result["data"]["dissolution_detection_triggered"] is False


def test_szlab_photoshotting_waits_before_dissolution_detection(monkeypatch):
    device = SzlabMixerPhotoShottingDevice(
        use_plc_gateway=True,
        dissolution_service_url="http://inference:8003/",
        dissolution_trigger_delay=1.5,
    )
    calls = []
    monkeypatch.setattr(
        device,
        "_wait_for_dissolution_trigger",
        lambda: calls.append(("wait", 1.5)),
    )
    monkeypatch.setattr(
        device,
        "_run_dissolution_detection",
        lambda sample_id: calls.append(("detect", sample_id)),
    )

    device._run_delayed_dissolution_detection("sample-1")

    assert calls == [("wait", 1.5), ("detect", "sample-1")]


def test_szlab_photoshotting_parses_dissolution_result(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"result": 1}'

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting.request.urlopen",
        fake_urlopen,
    )
    device = SzlabMixerPhotoShottingDevice(
        use_plc_gateway=True,
        dissolution_service_url="http://inference:8003/",
        dissolution_timeout=12.0,
    )
    result = device._run_dissolution_detection("sample-1")

    assert captured == {
        "url": "http://inference:8003/trigger_detect",
        "method": "POST",
        "timeout": 12.0,
    }
    assert json.loads(device.last_dissolution_result) == {
        "status": "completed",
        "sample_id": "sample-1",
        "result": 1,
        "solubility": True,
        "raw_result": {"result": 1},
    }
    assert result == json.loads(device.last_dissolution_result)


def test_szlab_poly_plc_uses_node_id_map_without_browsing(monkeypatch, tmp_path):
    pytest.importorskip("pylabrobot")
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

    csv_path = tmp_path / "s05.csv"
    csv_path.write_text(
        "序号,变量名,数据类型\n"
        "1,S05加工完成,BOOL\n"
        "2,S05拍照结果,INT\n",
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.connected = False

        def connect(self):
            self.connected = True

    def fail_find_nodes(self):
        raise AssertionError("已提供 NodeId map 时不应浏览 OPC UA 地址空间")

    monkeypatch.setattr("unilabos.devices.workstation.szlab_poly_studio.plc.Client", FakeClient)
    monkeypatch.setattr(SZLabPolyPLCDevice, "_find_nodes", fail_find_nodes)

    device = SZLabPolyPLCDevice(
        url="opc.tcp://127.0.0.1:4840/",
        csv_path=str(csv_path),
        opcua_node_id_map={
            "S05加工完成": "ns=4;s=上位机通讯|S05加工完成",
            "S05拍照结果": "ns=4;s=上位机通讯|S05拍照结果",
        },
    )

    assert device.client.connected is True
    assert device.use_node("S05加工完成").node_id == "ns=4;s=上位机通讯|S05加工完成"
    assert device.use_node("S05拍照结果").node_id == "ns=4;s=上位机通讯|S05拍照结果"


def test_szlab_poly_plc_uses_csv_node_ids_without_browsing(monkeypatch, tmp_path):
    pytest.importorskip("pylabrobot")
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

    csv_path = tmp_path / "sensors.csv"
    csv_path.write_text(
        "序号,变量名,数据类型,node_id\n"
        "1,传感器状态_上位机,ST_BOOL16[10],\n"
        "2,传感器状态_上位机[0].NO[0],BOOL,ns=4;s=上位机通讯|传感器状态_上位机[0].NO[0]\n"
        "3,S09允许加工,BOOL,ns=4;s=上位机通讯|S09允许加工\n",
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.connected = False

        def connect(self):
            self.connected = True

    def fail_find_nodes(self):
        raise AssertionError("已提供 NodeId prefix 时不应浏览 OPC UA 地址空间")

    monkeypatch.setattr("unilabos.devices.workstation.szlab_poly_studio.plc.Client", FakeClient)
    monkeypatch.setattr(SZLabPolyPLCDevice, "_find_nodes", fail_find_nodes)

    device = SZLabPolyPLCDevice(
        url="opc.tcp://127.0.0.1:4840/",
        csv_path=str(csv_path),
    )

    assert device.client.connected is True
    assert "传感器状态_上位机" not in device._variables_to_find
    assert (
        device.use_node("传感器状态_上位机[0].NO[0]").node_id
        == "ns=4;s=上位机通讯|传感器状态_上位机[0].NO[0]"
    )
    assert device.use_node("S09允许加工").node_id == "ns=4;s=上位机通讯|S09允许加工"


def test_szlab_csv_loaders_support_real_utf16_with_bom(tmp_path):
    csv_path = tmp_path / "plc.csv"
    sensor_name = "传感器状态_上位机[0].NO[0]"
    csv_path.write_text(
        "序号,变量名,EnglishName,数据类型,注释,软元件地址,node_id\n"
        f"1,{sensor_name},material_present,BOOL,物料在位,R10000.0,ns=4;s=sensor\n",
        encoding="utf-16",
    )

    names, node_ids = load_variable_definitions_from_csv(str(csv_path))
    aliases = load_variable_aliases_from_csv(str(csv_path))
    metadata = load_sensor_bit_metadata_from_csv(str(csv_path))

    assert names == [sensor_name]
    assert node_ids == {sensor_name: "ns=4;s=sensor"}
    assert aliases == {"material_present": sensor_name}
    assert metadata[sensor_name] == {
        "label": "物料在位",
        "address": "R10000.0",
        "node_id": "ns=4;s=sensor",
    }


def test_szlab_plc_0628_addnodeid_excludes_non_value_sensor_parents():
    from unilabos.devices.workstation.szlab_poly_studio.plc import load_variable_names_from_csv

    csv_path = Path("unilabos/devices/workstation/szlab_poly_studio/szlab_plc_0628_addnodeid.csv")
    text = csv_path.read_text(encoding="utf-8")
    names = load_variable_names_from_csv(str(csv_path))

    assert "node_id" in text.splitlines()[0]
    assert "传感器状态_上位机" not in names
    assert "传感器状态_上位机[0]" not in names
    assert "传感器状态_上位机[0].NO" in names
    assert "传感器状态_上位机[0].NO[0]" in names
    assert "S09允许加工" in names


def test_szlab_poly_plc_can_enable_opcua_token_time_drift_patch(monkeypatch, tmp_path):
    pytest.importorskip("pylabrobot")
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

    csv_path = tmp_path / "s09.csv"
    csv_path.write_text("序号,变量名,数据类型\n1,S09允许加工,BOOL\n", encoding="utf-8")
    patch_calls = []

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.connected = False

        def connect(self):
            self.connected = True

    monkeypatch.setattr("unilabos.devices.workstation.szlab_poly_studio.plc.Client", FakeClient)
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.plc._patch_opcua_token_time_drift_check",
        lambda: patch_calls.append("patched"),
    )

    SZLabPolyPLCDevice(
        url="opc.tcp://127.0.0.1:4840/",
        csv_path=str(csv_path),
        opcua_node_id_map={"S09允许加工": "ns=4;s=上位机通讯|S09允许加工"},
        ignore_opcua_token_time_drift=True,
    )

    assert patch_calls == ["patched"]


def test_szlab_poly_plc_missing_sensor_group_is_not_silent():
    device = object.__new__(SZLabPolyPLCDevice)
    device.stack_sensor_groups = {}

    with pytest.raises(KeyError, match="stack_sensor_layout.json"):
        device._read_named_sensor_group("s2_tip")


def test_szlab_poly_plc_metadata_only_suppresses_missing_node(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)

    def missing_node(_name):
        raise KeyError("missing")

    monkeypatch.setattr(device, "use_node", missing_node)
    assert device.get_opc_variable_metadata("missing") == ("missing", None)

    def broken_node(_name):
        raise RuntimeError("opcua disconnected")

    monkeypatch.setattr(device, "use_node", broken_node)
    with pytest.raises(RuntimeError, match="opcua disconnected"):
        device.get_opc_variable_metadata("broken")


def test_szlab_mixer_keeps_pipeline_route_helpers():
    route = S06PipelineRoute(control_valve=11, absolute_position=21)
    routes = default_s06_pipeline_routes()

    assert route.control_valve == 11
    assert route.absolute_position == 21
    assert s06_pump_valve_var(1) == "S06注射泵1控制阀"
    assert (1, "aspirate") in routes


class FakeRobotPlcGateway:
    def __init__(
        self,
        *,
        sensor_values=None,
        home_value=True,
        write_allowed_values=None,
        completion_values=None,
    ):
        self.sensor_values = dict(sensor_values or {})
        self.home_value = home_value
        self.write_allowed_values = list(write_allowed_values or [True])
        self.completion_values = list(completion_values or [])
        self.written_values = {}
        self.reads = []
        self.writes = []
        self.events = []
        self.wait_equal_calls = []
        self.sensor_wait_calls = []

    def read_variable(self, name, use_cache=False):
        self.reads.append((name, use_cache))
        self.events.append(("read", name, use_cache))
        if name == "Robot_Home":
            return self.home_value
        if name == "Robot_任务允许写入":
            if self.write_allowed_values:
                return self.write_allowed_values.pop(0)
            return False
        if name == "Robot_任务完成":
            if self.completion_values:
                return self.completion_values.pop(0)
            return self.written_values.get("任务号", 0)
        if name in self.written_values:
            return self.written_values[name]
        return self.sensor_values[name]

    def write_variable(self, name, value):
        self.writes.append((name, value))
        self.events.append(("write", name, value))
        self.written_values[name] = value
        return True

    def wait_variable_equal(self, name, expected, interval=1.0):
        self.wait_equal_calls.append((name, expected, interval))
        self.events.append(("wait", name, expected))
        return self.read_variable(name, use_cache=False) == expected

    def wait_sensor_conditions(self, conditions, interval=0.2, context=None):
        del context
        self.sensor_wait_calls.append((dict(conditions), interval))
        task_completed = any(
            event[0] == "wait" and event[1] == "Robot_任务完成"
            for event in self.events
        )
        if task_completed:
            self.sensor_values.update(conditions)
        values = {
            name: self.read_variable(name, use_cache=False)
            for name in conditions
        }
        return all(values[name] == expected for name, expected in conditions.items()), values


class FakeGripperRobotPlcGateway(FakeRobotPlcGateway):
    def __init__(self, *, gripper_before, gripper_after, **kwargs):
        super().__init__(**kwargs)
        self.gripper_before = dict(gripper_before)
        self.gripper_after = dict(gripper_after)

    def read_variable(self, name, use_cache=False):
        if name in {GRIPPER_ORIGIN_VARIABLE, GRIPPER_STATUS_VARIABLE, *GRIPPER_POSITION_VARIABLES.values()}:
            self.reads.append((name, use_cache))
            task_completed = any(
                event[0] == "wait" and event[1] == "Robot_任务完成"
                for event in self.events
            )
            values = self.gripper_after if task_completed else self.gripper_before
            return values.get(name, False)
        return super().read_variable(name, use_cache=use_cache)


@pytest.mark.parametrize(
    ("task", "station", "data", "expected"),
    [
        ("pick", "S01", {"product_type": 1}, "tip_box"),
        ("pick", "S01", {"product_type": 6}, "solid_powder"),
        ("pick", "S02", {}, "tip_box"),
        ("pick", "S03", {"product_type": 1}, "beaker"),
        ("pick", "S03", {"product_type": 2}, "sample_vial_250ml"),
        ("pick", "S03", {"product_type": 3}, "sample_vial_500ml"),
        ("pick", "S04", {}, "beaker"),
        ("pick", "S05", {}, "beaker"),
        ("pick", "S06", {}, "beaker"),
        ("pick", "S071", {}, "solid_powder"),
        ("pick", "S072", {"product_type": 1}, "solid_powder"),
        ("pick", "S072", {"product_type": 2}, "beaker"),
        ("pick", "S08", {"product_type": 1}, "sample_vial_250ml"),
        ("pick", "S08", {"product_type": 2}, "sample_vial_500ml"),
        ("pick", "S08", {"product_type": 3}, "liquid_reagent_100ml"),
        ("pour", "S08", {"product_type": 1}, "beaker"),
        ("pick", "S09", {"product_type": 1}, "tip_box"),
        ("pick", "S09", {"product_type": 2}, "liquid_reagent_100ml"),
        ("pick", "S09", {"product_type": 4}, "beaker"),
        ("pick", "S10", {}, "liquid_reagent_100ml"),
        ("pick", "S11", {"product_type": 3}, "sample_vial_500ml"),
    ],
)
def test_szlab_robot_gripper_position_mapping(task, station, data, expected):
    robot = SzlabMixerRobotDevice(enable_gripper_check=True)

    assert robot._gripper_position_variable(task, station, data) == GRIPPER_POSITION_VARIABLES[expected]


@pytest.mark.parametrize(
    ("method", "station_sensor", "station_value", "before_position", "after_position"),
    [
        ("submit_pick_from_s04", "传感器状态_上位机[2].NO[10]", True, GRIPPER_ORIGIN_VARIABLE, "beaker"),
        ("submit_place_to_s04", "传感器状态_上位机[2].NO[10]", False, "beaker", GRIPPER_ORIGIN_VARIABLE),
    ],
)
def test_szlab_robot_gripper_check_wraps_pick_and_place(
    method,
    station_sensor,
    station_value,
    before_position,
    after_position,
):
    before_variable = GRIPPER_POSITION_VARIABLES.get(before_position, before_position)
    after_variable = GRIPPER_POSITION_VARIABLES.get(after_position, after_position)
    gateway = FakeGripperRobotPlcGateway(
        sensor_values={
            station_sensor: station_value,
            "S041准备信号": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        gripper_before={GRIPPER_STATUS_VARIABLE: 1, before_variable: True},
        gripper_after={GRIPPER_STATUS_VARIABLE: 1, after_variable: True},
    )
    robot = SzlabMixerRobotDevice(enable_gripper_check=True)
    robot.set_plc_gateway(gateway)

    result = getattr(robot, method)(position=1)

    assert result["success"] is True
    assert result["gripper_precheck"]["values"][before_variable] is True
    assert result["gripper_postcheck"]["values"][after_variable] is True
    assert result["gripper_postcheck"]["values"][GRIPPER_STATUS_VARIABLE] == 1


def test_szlab_robot_gripper_drop_rejects_action_before_writing():
    gateway = FakeGripperRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        gripper_before={GRIPPER_STATUS_VARIABLE: 3, GRIPPER_ORIGIN_VARIABLE: True},
        gripper_after={},
    )
    robot = SzlabMixerRobotDevice(enable_gripper_check=True)
    robot.set_plc_gateway(gateway)

    result = robot.submit_pick_from_s04(position=1)

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert "掉落" in result["message"]
    assert gateway.writes == []


def test_szlab_robot_s04_sensor_mapping_matches_plc_csv_positions():
    assert S04_SENSOR_BY_POSITION == {
        1: "传感器状态_上位机[2].NO[10]",
        2: "传感器状态_上位机[2].NO[11]",
        3: "传感器状态_上位机[2].NO[12]",
        4: "传感器状态_上位机[2].NO[13]",
    }


def test_szlab_robot_s04_rejects_unavailable_position_five():
    device = SzlabMixerRobotDevice()

    result = device.submit_place_to_s04(position=5)

    assert result["success"] is False
    assert result["message"] == "磁搅位置必须在 1-4 范围内"


def test_szlab_robot_task_specs_cover_xlsx_task_numbers_once():
    assert sorted(spec.task_number for spec in ROBOT_ACTION_SPECS.values()) == [1, *range(3, 26)]
    assert len({spec.task_number for spec in ROBOT_ACTION_SPECS.values()}) == 24


def test_szlab_robot_s04_pick_requires_material_and_resets_pc_to_plc_variables():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        completion_values=[8],
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["reset"]["success"] is True
    assert gateway.wait_equal_calls == [
        ("Robot_Home", True, 1.0),
        ("Robot_任务允许写入", True, 1.0),
        ("Robot_任务完成", 8, 1.0),
    ]
    assert gateway.reads[:1] == [
        ("传感器状态_上位机[2].NO[10]", False),
    ]
    assert gateway.reads[-1:] == [
        ("传感器状态_上位机[2].NO[10]", False),
    ]
    assert gateway.writes == [
        ("S04取放料编号", 1),
        ("任务号", 8),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S04取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_waits_emit_plc_opc_wait_events():
    plc = object.__new__(SZLabPolyPLCDevice)
    plc.set_opc_wait_event_writer(None)
    values = {
        "传感器状态_上位机[0].NO[6]": True,
        "Robot_Home": True,
        "Robot_任务允许写入": True,
        "Robot_任务完成": 6,
    }

    def read_variable(name, use_cache=False):
        del use_cache
        return values[name]

    def write_variable(name, value):
        values[name] = value
        if name == "Robot_任务写入完成" and value is True:
            values["传感器状态_上位机[0].NO[6]"] = False

    plc.read_variable = read_variable
    plc.write_variable = write_variable
    plc.get_opc_variable_metadata = lambda name: (name, f"ns=4;s=上位机通讯|{name}")

    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(plc)

    result = device.submit_pick_from_s03(product_type=1, position="1-1")

    assert result["success"] is True
    events = plc.drain_opc_wait_events()
    variable_events = [event for event in events if "variable" in event["detail"]]
    assert [(event["phase"], event["detail"]["variable"]) for event in variable_events] == [
        ("start", "Robot_Home"),
        ("finish", "Robot_Home"),
        ("start", "Robot_任务允许写入"),
        ("finish", "Robot_任务允许写入"),
        ("start", "Robot_任务完成"),
        ("finish", "Robot_任务完成"),
    ]
    assert [event["detail"]["expected"] for event in variable_events] == [True, True, True, True, 6, 6]
    sensor_events = [event for event in events if event["detail"].get("wait_kind") == "sensor_conditions"]
    assert [event["detail"]["context"] for event in sensor_events] == [
        "机器人前置传感器检查",
        "机器人前置传感器检查",
        "机器人后置传感器检查",
        "机器人后置传感器检查",
    ]


def test_sensor_condition_wait_logs_start_change_and_finish():
    plc = object.__new__(SZLabPolyPLCDevice)
    plc.set_opc_wait_event_writer(None)
    plc._sensor_bit_metadata = {
        "传感器状态_上位机[3].NO[1]": {"label": "加溶剂检测"},
        "传感器状态_上位机[4].NO[12]": {"label": "液体试剂瓶1-1"},
    }
    plc.get_opc_variable_metadata = lambda name: (name, f"ns=4;s=上位机通讯|{name}")
    solvent_reads = iter([False, True])

    def read_variable(name, use_cache=False):
        del use_cache
        if name == "传感器状态_上位机[3].NO[1]":
            return next(solvent_reads)
        return True

    plc.read_variable = read_variable
    conditions = {
        "传感器状态_上位机[3].NO[1]": True,
        "传感器状态_上位机[4].NO[12]": True,
    }

    success, values = wait_sensor_conditions(
        plc,
        conditions,
        interval=0.0,
        context="S06 加液前置传感器检查",
    )

    assert success is True
    assert values == {name: True for name in conditions}
    events = plc.drain_opc_wait_events()
    assert [event["phase"] for event in events] == ["start", "change", "finish"]
    assert "仍等待 加溶剂检测" in events[0]["message"]
    assert "False → True" in events[1]["message"]
    assert "2/2 已满足" in events[2]["message"]
    assert all(event["detail"]["wait_kind"] == "sensor_conditions" for event in events)


def test_sensor_condition_wait_reads_all_conditions_concurrently():
    condition_count = 3
    reads_started = threading.Barrier(condition_count)
    reads_finished: list[str] = []
    reads_lock = threading.Lock()

    class ConcurrentReader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            reads_started.wait(timeout=1.0)
            with reads_lock:
                reads_finished.append(name)
            return True

    conditions = {f"sensor_{index}": True for index in range(condition_count)}

    success, values = wait_sensor_conditions(
        ConcurrentReader(),
        conditions,
        interval=0.0,
        context="并发传感器检查",
    )

    assert success is True
    assert values == {name: True for name in conditions}
    assert set(reads_finished) == set(conditions)


def test_sensor_condition_wait_has_no_timeout_metadata():
    plc = object.__new__(SZLabPolyPLCDevice)
    plc.set_opc_wait_event_writer(None)
    plc._sensor_bit_metadata = {
        "传感器状态_上位机[5].NO[1]": {"label": "液体试剂瓶2-1"},
    }
    plc.get_opc_variable_metadata = lambda name: (name, f"ns=4;s=上位机通讯|{name}")
    values = iter([False, True])
    plc.read_variable = lambda name, use_cache=False: next(values)

    success, _ = wait_sensor_conditions(
        plc,
        {"传感器状态_上位机[5].NO[1]": True},
        interval=0.0,
        context="S06 加液前置传感器检查",
    )

    assert success is True
    events = plc.drain_opc_wait_events()
    assert [event["phase"] for event in events] == ["start", "change", "finish"]
    assert "液体试剂瓶2-1" in events[0]["message"]
    assert all("timeout" not in event["detail"] for event in events)


def test_opc_wait_event_writer_is_thread_local():
    """并行 Action 绑定 wait logger 时，事件应落在各自线程的 writer。"""
    plc = object.__new__(SZLabPolyPLCDevice)
    received_a: list[str] = []
    received_b: list[str] = []
    ready = threading.Barrier(2)

    def worker(bucket: list[str], marker: str) -> None:
        plc.set_opc_wait_event_writer(
            lambda event: bucket.append(str(event.get("message") or marker))
        )
        ready.wait()
        plc._emit_or_store_opc_wait_event({"message": marker, "detail": {"type": "opc_wait"}})
        plc.set_opc_wait_event_writer(None)

    threads = [
        threading.Thread(target=worker, args=(received_a, "action-a")),
        threading.Thread(target=worker, args=(received_b, "action-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert received_a == ["action-a"]
    assert received_b == ["action-b"]


def test_szlab_robot_s04_pick_rejects_empty_position_without_writing_task():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": False,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is False
    assert result["message"] == "S04 pick 前置传感器状态等待失败"
    assert result["sensor_precheck"]["mismatches"]["传感器状态_上位机[2].NO[10]"]["actual"] is False
    assert gateway.writes == []


@pytest.mark.parametrize(
    ("s05_occupied", "s05_ready", "blocked_variable"),
    [
        (True, True, "传感器状态_上位机[3].NO[0]"),
        (False, False, "S05准备信号"),
    ],
)
def test_szlab_robot_s04_pick_waits_for_s05_destination_before_writing_task(
    s05_occupied,
    s05_ready,
    blocked_variable,
):
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": s05_occupied,
            "S05准备信号": s05_ready,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is False
    assert result["message"] == "S04 pick 前置传感器状态等待失败"
    assert result["sensor_precheck"]["mismatches"][blocked_variable]["actual"] is not None
    assert gateway.writes == []


def test_szlab_robot_does_not_use_unrelated_transfer_sensors():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
            "传感器状态_上位机[3].NO[6]": True,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is True
    assert not any(name == "传感器状态_上位机[3].NO[6]" for name, _ in gateway.reads)
    assert not any(name == "传感器状态_上位机[3].NO[1]" for name, _ in gateway.reads)
    assert ("任务号", 8) in gateway.writes


def test_szlab_robot_reports_verification_failed_without_resubmitting_task():
    class NoTransitionGateway(FakeRobotPlcGateway):
        def wait_sensor_conditions(self, conditions, interval=0.2, context=None):
            del context
            self.sensor_wait_calls.append((dict(conditions), interval))
            values = {
                name: self.read_variable(name, use_cache=False)
                for name in conditions
            }
            return all(values[name] == expected for name, expected in conditions.items()), values

    gateway = NoTransitionGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is False
    assert result["status"] == "verification_failed"
    assert "禁止自动重试" in result["message"]
    assert gateway.writes.count(("任务号", 8)) == 1


def test_szlab_robot_waits_until_task_params_read_back_nonzero():
    class ZeroReadbackGateway(FakeRobotPlcGateway):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.zero_reads_remaining = 2

        def read_variable(self, name, use_cache=False):
            if name == "S04取放料编号" and self.zero_reads_remaining:
                self.zero_reads_remaining -= 1
                self.reads.append((name, use_cache))
                return 0
            return super().read_variable(name, use_cache=use_cache)

    gateway = ZeroReadbackGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is True
    assert ("Robot_任务写入完成", True) in gateway.writes
    assert sum(name == "S04取放料编号" for name, _ in gateway.reads) >= 3
    assert gateway.writes == [
        ("S04取放料编号", 1),
        ("任务号", 8),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S04取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_s04_place_requires_empty_position_without_writing_task():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "S041准备信号": True,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s04(position=1)

    assert result["success"] is False
    assert result["message"] == "S04 place 前置传感器状态等待失败"
    assert result["sensor_precheck"]["mismatches"]["传感器状态_上位机[2].NO[10]"]["actual"] is True
    assert gateway.writes == []


def test_szlab_robot_s04_place_requires_position_ready_without_writing_task():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": False,
            "S041准备信号": False,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s04(position=1)

    assert result["success"] is False
    assert result["message"] == "S04 place 前置传感器状态等待失败"
    assert result["sensor_precheck"]["mismatches"]["S041准备信号"]["actual"] is False
    assert gateway.writes == []


def test_szlab_robot_s04_place_writes_position_before_task_number():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[11]": False,
            "S042准备信号": True,
        },
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s04(position=2, sample_id="sample-1")

    assert result["success"] is True
    assert result["sample_id"] == "sample-1"
    assert gateway.writes == [
        ("S04取放料编号", 2),
        ("任务号", 7),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S04取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_s05_only_writes_task_number_and_resets_it():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s05(sample_id="sample-1")

    assert result["success"] is True
    assert result["station"] == "S05"
    assert gateway.writes == [
        ("任务号", 9),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("任务号", 0),
    ]


def test_szlab_robot_s05_place_requires_ready_without_writing_task():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": False,
        },
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s05(sample_id="sample-1")

    assert result["success"] is False
    assert result["message"] == "S05 place 前置传感器状态等待失败"
    assert result["sensor_precheck"]["mismatches"]["S05准备信号"]["actual"] is False
    assert gateway.writes == []


def test_szlab_robot_s01_does_not_read_retired_gripper_sensor():
    gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s01(product_type=1, position=1)

    assert result["success"] is True
    assert result["sensor_check_skipped_reason"] == "S01 暂无物料传感器"
    assert not any(name == "传感器状态_上位机[3].NO[6]" for name, _ in gateway.reads)
    assert gateway.writes == [
        ("S01出入料产品", 1),
        ("S01取放料编号", 1),
        ("任务号", 1),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S01出入料产品", 0),
        ("S01取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_s03_pick_writes_product_position_and_task_number():
    gateway = FakeRobotPlcGateway(
        sensor_values={"传感器状态_上位机[0].NO[6]": True},
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s03(product_type=1, position="1-1")

    assert result["success"] is True
    assert result["source_sensor_variable"] == "传感器状态_上位机[0].NO[6]"
    assert gateway.reads[:1] == [
        ("传感器状态_上位机[0].NO[6]", False),
    ]
    assert gateway.reads[-1:] == [
        ("传感器状态_上位机[0].NO[6]", False),
    ]
    assert gateway.writes == [
        ("S03取放料产品", 1),
        ("S03取放料编号", 1),
        ("任务号", 6),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S03取放料产品", 0),
        ("S03取放料编号", 0),
        ("任务号", 0),
    ]
    assert result["reset"]["readback"] == {
        "Robot_任务写入完成": False,
        "S03取放料产品": 0,
        "S03取放料编号": 0,
        "任务号": 0,
    }


def test_szlab_robot_s03_sample_vial_pick_requires_matching_beaker_slot_empty():
    sample_vial_sensor = "传感器状态_上位机[1].NO[8]"
    beaker_sensor = "传感器状态_上位机[0].NO[6]"
    gateway = FakeRobotPlcGateway(
        sensor_values={sample_vial_sensor: True, beaker_sensor: True},
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    blocked = device.submit_pick_from_s03(product_type=2, position="1-1")

    assert blocked["success"] is False
    assert blocked["sensor_precheck"]["mismatches"] == {
        beaker_sensor: {"expected": False, "actual": True},
    }
    assert not gateway.writes

    gateway.sensor_values[beaker_sensor] = False
    allowed = device.submit_pick_from_s03(product_type=2, position="1-1")

    assert allowed["success"] is True
    assert gateway.sensor_wait_calls[1][0] == {
        sample_vial_sensor: True,
        beaker_sensor: False,
    }


def test_szlab_robot_s03_reset_retries_until_pc_to_plc_variables_are_clear():
    class DelayedResetGateway(FakeRobotPlcGateway):
        def __init__(self):
            super().__init__(sensor_values={"传感器状态_上位机[0].NO[6]": True})
            self.stale_reset_reads = {"S03取放料产品": 1, "S03取放料编号": 1, "任务号": 1}

        def read_variable(self, name, use_cache=False):
            if name in self.stale_reset_reads and self.written_values.get(name) == 0:
                value = self.stale_reset_reads.pop(name)
                self.reads.append((name, use_cache))
                self.events.append(("read", name, use_cache))
                return value
            return super().read_variable(name, use_cache=use_cache)

    gateway = DelayedResetGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s03(product_type=1, position="1-1")

    assert result["success"] is True
    assert result["reset"]["success"] is True
    assert result["reset"]["readback"] == {
        "Robot_任务写入完成": False,
        "S03取放料产品": 0,
        "S03取放料编号": 0,
        "任务号": 0,
    }
    assert gateway.writes.count(("S03取放料产品", 0)) == 2
    assert gateway.writes.count(("S03取放料编号", 0)) == 2
    assert gateway.writes.count(("任务号", 0)) == 2


def test_szlab_robot_s072_place_skips_all_sensor_checks():
    gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s072(product_type=1, position=2)

    assert result["success"] is True
    assert result["sensor_check_skipped_reason"] == "S072 取放料暂不检查传感器"
    assert gateway.sensor_wait_calls == []
    assert not any(name.startswith("传感器状态_") for name, _ in gateway.reads)
    assert ("任务号", 15) in gateway.writes


def test_szlab_robot_s072_pick_skips_all_sensor_checks():
    gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s072(product_type=1, position=1)

    assert result["success"] is True
    assert result["sensor_check_skipped_reason"] == "S072 取放料暂不检查传感器"
    assert gateway.sensor_wait_calls == []
    assert not any(name.startswith("传感器状态_") for name, _ in gateway.reads)
    assert ("任务号", 16) in gateway.writes


def test_szlab_robot_s08_pick_uses_position_sensor_mapping():
    gateway = FakeRobotPlcGateway(
        sensor_values={"传感器状态_上位机[3].NO[15]": True},
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s08(product_type=1, position=2)

    assert result["success"] is True
    assert result["source_sensor_variable"] == "传感器状态_上位机[3].NO[15]"
    assert gateway.writes == [
        ("S08取放料产品", 1),
        ("S08取放料编号", 2),
        ("任务号", 18),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S08取放料产品", 0),
        ("S08取放料编号", 0),
        ("任务号", 0),
    ]


@pytest.mark.parametrize(
    ("position", "sensor"),
    [
        (1, "传感器状态_上位机[3].NO[14]"),
        (2, "传感器状态_上位机[3].NO[15]"),
    ],
)
def test_szlab_robot_s08_place_uses_cap_station_sensor(position, sensor):
    gateway = FakeRobotPlcGateway(sensor_values={sensor: False})
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s08(product_type=1, position=position)

    assert result["success"] is True
    assert result["target_sensor_variable"] == sensor
    assert (sensor, False) in gateway.reads
    assert not any(name.startswith("传感器状态_上位机[4].NO[") for name, _ in gateway.reads)


def test_szlab_robot_s08_place_rejects_cap_storage_slot_as_position():
    gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s08(product_type=1, position=3)

    assert result["success"] is False
    assert "S08 放瓶位置必须在 1-2 范围内" in result["message"]
    assert gateway.writes == []


def test_szlab_robot_s08_pour_writes_product_selection_and_task_number():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[3].NO[1]": False,
            "传感器状态_上位机[3].NO[14]": True,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pour_from_s08(product_type=2)

    assert result["success"] is True
    assert result["sensor_precheck"]["values"] == {
        "传感器状态_上位机[3].NO[14]": True,
    }
    assert result["sensor_postcheck"]["success"] is True
    assert result["sensor_postcheck"]["values"] == {
        "传感器状态_上位机[3].NO[14]": True,
    }
    assert not any(name == "传感器状态_上位机[3].NO[1]" for name, _ in gateway.reads)
    assert gateway.writes == [
        ("S08倒料产品选择", 2),
        ("任务号", 25),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S08倒料产品选择", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_s08_pour_rejects_unknown_product_type():
    gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pour_from_s08(product_type=3)

    assert result["success"] is False
    assert "S08倒料产品选择必须是 1" in result["message"]
    assert gateway.writes == []


def test_szlab_robot_s09_tip_place_uses_sensor_checks():
    gateway = FakeRobotPlcGateway(
        sensor_values={"传感器状态_上位机[4].NO[6]": False},
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s09(product_type=1, position=2)

    assert result["success"] is True
    assert result["s09_safe_position"] == 1
    assert result["target_sensor_variable"] == "传感器状态_上位机[4].NO[6]"
    assert result["sensor_precheck"]["success"] is True
    assert result["sensor_postcheck"]["success"] is True
    assert gateway.sensor_wait_calls
    assert gateway.writes == [
        ("S09取放料产品", 1),
        ("S09取放料编号", 2),
        ("任务号", 19),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S09取放料产品", 0),
        ("S09取放料编号", 0),
        ("任务号", 0),
    ]
    assert ("write", "S09工艺选择", 1) not in gateway.events
    assert not any(event[1] == "S09原点信号_1" for event in gateway.events)
    complete_wait_index = gateway.events.index(("wait", "Robot_任务完成", 19))
    write_done_true_index = gateway.events.index(("write", "Robot_任务写入完成", True))
    assert write_done_true_index < complete_wait_index


def test_szlab_robot_s09_liquid_bottle_place_uses_sensor_checks():
    gateway = FakeRobotPlcGateway(
        sensor_values={"传感器状态_上位机[4].NO[11]": False},
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s09(product_type=2, position=5)

    assert result["success"] is True
    assert result["s09_safe_position"] == 3
    assert result["target_sensor_variable"] == "传感器状态_上位机[4].NO[11]"
    assert result["sensor_precheck"]["success"] is True
    assert result["sensor_postcheck"]["success"] is True
    assert gateway.sensor_wait_calls
    assert ("S09工艺选择", 3) not in gateway.writes
    assert not any(event[1] == "S09原点信号_3" for event in gateway.events)
    assert gateway.writes == [
        ("S09取放料产品", 2),
        ("S09取放料编号", 5),
        ("任务号", 19),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S09取放料产品", 0),
        ("S09取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_robot_s09_pick_directly_submits_beaker_robot_task():
    gateway = FakeRobotPlcGateway(sensor_values={"传感器状态_上位机[4].NO[7]": True})
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s09(product_type=3, position=1)

    assert result["success"] is True
    assert result["s09_safe_position"] == 4
    assert result["sensor_check_skipped_reason"] == "S09 烧杯位暂无独立物料传感器"
    assert not gateway.sensor_wait_calls
    assert not any(name == "传感器状态_上位机[3].NO[1]" for name, _ in gateway.reads)
    assert ("S09工艺选择", 4) not in gateway.writes
    assert not any(event[1] == "S09原点信号_4" for event in gateway.events)
    assert ("任务号", 20) in gateway.writes


def test_szlab_robot_s09_beaker_place_directly_submits_robot_task():
    gateway = FakeRobotPlcGateway(sensor_values={"传感器状态_上位机[4].NO[7]": False})
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_place_to_s09(product_type=3, position=1)

    assert result["success"] is True
    assert result["s09_safe_position"] == 4
    assert result["sensor_check_skipped_reason"] == "S09 烧杯位暂无独立物料传感器"
    assert not gateway.sensor_wait_calls
    assert not any(name == "传感器状态_上位机[3].NO[1]" for name, _ in gateway.reads)
    assert ("S09工艺选择", 4) not in gateway.writes
    assert ("S09原点信号_4", True, 1.0) not in gateway.wait_equal_calls
    assert ("任务号", 19) in gateway.writes


def test_szlab_robot_s09_density_beaker_reuses_pick_place_tasks_with_product_type_4():
    place_gateway = FakeRobotPlcGateway()
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(place_gateway)

    place_result = device.submit_place_to_s09(product_type=4, position=1)

    assert place_result["success"] is True
    assert place_result["s09_safe_position"] == 4
    assert place_result["sensor_check_skipped_reason"] == "S09 烧杯位暂无独立物料传感器"
    assert ("S09取放料产品", 4) in place_gateway.writes
    assert ("S09取放料编号", 1) in place_gateway.writes
    assert ("任务号", 19) in place_gateway.writes

    pick_gateway = FakeRobotPlcGateway()
    device.set_plc_gateway(pick_gateway)

    pick_result = device.submit_pick_from_s09(product_type=4, position=1)

    assert pick_result["success"] is True
    assert pick_result["s09_safe_position"] == 4
    assert ("S09取放料产品", 4) in pick_gateway.writes
    assert ("S09取放料编号", 1) in pick_gateway.writes
    assert ("任务号", 20) in pick_gateway.writes


def test_szlab_robot_waits_for_home_signal_before_pc_to_plc_write():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        home_value=False,
    )
    original_wait = gateway.wait_variable_equal

    def wait_until_home(name, expected, interval=1.0):
        if name == "Robot_Home":
            gateway.wait_equal_calls.append((name, expected, interval))
            return True
        return original_wait(name, expected, interval)

    gateway.wait_variable_equal = wait_until_home
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is True
    assert ("Robot_Home", True, device.poll_interval) in gateway.wait_equal_calls
    assert ("任务号", 8) in gateway.writes


def test_szlab_robot_can_skip_only_home_signal(monkeypatch):
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        home_value=False,
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)
    monkeypatch.setenv("SKIP_ROBOT_PRECHECK_VARIABLES", "Robot_Home")

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is True
    assert ("Robot_Home", False) not in gateway.reads
    assert ("Robot_任务允许写入", False) in gateway.reads
    assert ("Robot_任务完成", False) in gateway.reads
    assert gateway.writes[:4] == [
        ("S04取放料编号", 1),
        ("任务号", 8),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
    ]


def test_szlab_robot_can_skip_configured_sensor_precheck(monkeypatch):
    sensor = "传感器状态_上位机[3].NO[14]"
    gateway = FakeRobotPlcGateway(
        sensor_values={
            sensor: False,
        }
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)
    monkeypatch.setenv("SKIP_ROBOT_PRECHECK_VARIABLES", sensor)

    result = device.submit_pick_from_s08(product_type=1, position=1)

    assert result["success"] is True
    assert (sensor, False) not in gateway.reads
    assert gateway.writes[:5] == [
        ("S08取放料产品", 1),
        ("S08取放料编号", 1),
        ("任务号", 18),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
    ]


def test_szlab_robot_resets_written_pc_to_plc_variables_after_completion_wait_failure():
    gateway = FakeRobotPlcGateway(
        sensor_values={
            "传感器状态_上位机[2].NO[10]": True,
            "传感器状态_上位机[3].NO[0]": False,
            "S05准备信号": True,
        },
        completion_values=[0],
    )
    device = SzlabMixerRobotDevice()
    device.set_plc_gateway(gateway)

    result = device.submit_pick_from_s04(position=1)

    assert result["success"] is False
    assert "Robot_任务完成 == 8 失败" in result["message"]
    assert result["reset"]["success"] is True
    assert gateway.writes == [
        ("S04取放料编号", 1),
        ("任务号", 8),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S04取放料编号", 0),
        ("任务号", 0),
    ]


def test_szlab_mixer_registry_actions_expose_s04_s05_robot_actions():
    preset = load_preset("szlab_mixer")

    assert preset.actions["run_stirring"].device_id == "szlab_mixer_stirrer"
    assert preset.actions["take_photo"].device_id == "szlab_mixer_photoshotting"
    assert preset.actions["submit_pick_from_s04"].device_id == "szlab_mixer_robot"

    workflow = build_graph_workflow(
        flow_nodes=[
            {
                "id": "stir",
                "data": {
                    "device_id": "szlab_mixer_stirrer",
                    "method": "run_stirring",
                    "params": {"position": 1, "mode": 3, "speed": 300, "temperature": 60, "duration": 30},
                },
            },
        ],
        flow_edges=[],
        preset=preset,
    )

    assert workflow["nodes"] == [
        {
            "workflow_node_id": "stir",
            "device_id": "szlab_mixer_stirrer",
            "method": "run_stirring",
            "params": {
                "position": 1,
                "mode": 3,
                "speed": 300,
                "temperature": 60,
                "duration": 30,
                "safe_temperature": 80,
                "reset": False,
            },
            "opc_variables": [],
        },
    ]
    assert workflow["edges"] == []


def test_szlab_mixer_device_creation_passes_csv_path_to_gateway_devices(monkeypatch, tmp_path):
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        """
        {
          "nodes": [
            {
              "id": "szlab_mixer_pump",
              "config": {"url": "opc.tcp://example:50001"}
            }
          ],
          "links": []
        }
        """,
        encoding="utf-8",
    )
    created = {}

    class FakeDevice:
        def __init__(self, **kwargs):
            created[len(created)] = kwargs

    monkeypatch.setattr("scripts.run_workflow_local._load_class", lambda class_path: FakeDevice)

    devices = create_local_devices(
        graph_file=graph_path,
        csv_path=Path("/tmp/invalid.csv"),
        runtime_config=load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json"),
    )

    assert set(devices) == {"szlab_mixer_pump"}
    assert created == {
        0: {
            "url": "opc.tcp://example:50001",
            "csv_path": str(Path("/tmp/invalid.csv").resolve()),
        }
    }
    assert "use_plc_gateway" not in created[0]


def test_production_graph_passes_only_pipeline_specs_for_s06_pump(monkeypatch):
    created = {}

    class FakePump:
        def __init__(self, **kwargs):
            created["pump"] = kwargs

    def fake_load(class_path: str):
        if class_path.endswith(".pump.SzlabMixerPumpDevice"):
            return FakePump
        raise AssertionError(class_path)

    monkeypatch.setattr("scripts.run_workflow_local._load_class", fake_load)

    create_local_devices(
        graph_file=Path("tests/szlab_poly_studio/fixtures/szlab_mixer_pump_production_graph.json"),
        runtime_config=load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json"),
    )

    assert "robot_addition_position" not in created["pump"]
    assert "robot_stirrer_position" not in created["pump"]
    assert len(created["pump"]["pipeline_route_specs"]) == 6


def test_szlab_mixer_preset_loads_own_runtime_config():
    runtime_config = _load_preset_runtime_config(load_preset("szlab_mixer"))

    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_stirrer": "unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring.SzlabMixerMagneticStirrerDevice",
        "szlab_mixer_photoshotting": "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting.SzlabMixerPhotoShottingDevice",
        "szlab_mixer_robot": "unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot.SzlabMixerRobotDevice",
    }
    assert runtime_config.device_factory.plc_device_id == "szlab_poly_plc"


def test_szlab_mixer_run_nodes_samples_current_device_variables():
    class FakePump:
        def __init__(self):
            self.sampled = []

        def get_variables(self, variable_names, use_cache=False):
            self.sampled.append(list(variable_names))
            return {name: {"success": True, "value": False} for name in variable_names}

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=2;s={variable_name}"

        def run_solvent_addition(self, process=1, volume_pump_1=1, volume_pump_2=1):
            return {"success": True}

    pump = FakePump()
    events = []
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json")

    run_nodes(
        [
            WorkflowNode(
                uuid="pump",
                name="auto-run_solvent_addition",
                device_name="szlab_mixer_pump",
                param={"process": 1, "volume_pump_1": 1, "volume_pump_2": 1},
            )
        ],
        {"szlab_mixer_pump": pump},
        logger=WorkflowLogger(writer=lambda message, **kwargs: events.append((message, kwargs))),
        runtime_config=runtime_config,
    )

    assert pump.sampled[0] == [
        "S06准备信号",
        "S06允许加工",
        "S06工艺选择",
        "S06_1号溶液添加量",
        "S06_2号溶液添加量",
        "S06参数写入完成",
        "S06加工完成",
        "传感器状态_上位机[3].NO[1]",
        "传感器状态_上位机[4].NO[12]",
        "传感器状态_上位机[5].NO[1]",
    ]
    assert pump.sampled[1] == pump.sampled[0]
    assert any(message.startswith("OPC状态采样") for message, _ in events)
