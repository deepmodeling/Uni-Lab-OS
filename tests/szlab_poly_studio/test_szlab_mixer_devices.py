from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice, load_variable_names_from_csv
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.photoshotting import SzlabMixerPhotoShottingDevice
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.robot import SzlabMixerRobotDevice
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.stirrer import SzlabMixerStirrerDevice
from scripts.run_workflow_local import create_local_devices, load_runtime_config
from scripts.run_workflow_local import WorkflowLogger, WorkflowNode, run_nodes
from scripts.workflow_ui import _load_preset_runtime_config, build_graph_workflow, load_preset


def test_szlab_mixer_devices_are_ast_scannable():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/szlab_mixer")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {
        "szlab_mixer_stirrer",
        "szlab_mixer_photoshotting",
        "szlab_mixer_robot",
        "szlab_mixer_pump",
    }
    assert "run_stirring" in result["devices"]["szlab_mixer_stirrer"]["actions"]
    assert "take_photo" in result["devices"]["szlab_mixer_photoshotting"]["actions"]
    assert "take_dual_view_photos" in result["devices"]["szlab_mixer_photoshotting"]["actions"]
    assert "submit_pick_from_magnetic_stirrer" in result["devices"]["szlab_mixer_robot"]["actions"]
    assert "transfer_liquid" in result["devices"]["szlab_mixer_pump"]["actions"]


def test_szlab_mixer_does_not_keep_dedicated_opcua_client():
    assert not Path("unilabos/devices/workstation/szlab_poly_studio/szlab_mixer/opcua_client.py").exists()


def test_poly_studio_plc_client_exposes_disconnect_for_action_devices():
    assert "disconnect" in SZLabPolyPLCDevice.__dict__


def test_szlab_mixer_registry_actions_can_chain_two_devices():
    preset = load_preset("szlab_mixer")

    assert set(preset.actions) >= {
        "run_stirring",
        "take_photo",
        "take_dual_view_photos",
        "submit_pick_from_magnetic_stirrer",
        "submit_pick_from_photo_station",
        "transfer_liquid",
    }
    assert "set_s1_loading_request" not in preset.actions
    assert "write_variable_action" not in preset.actions
    assert preset.actions["run_stirring"].device_id == "szlab_mixer_stirrer"
    assert preset.actions["take_photo"].device_id == "szlab_mixer_photoshotting"
    assert preset.actions["transfer_liquid"].device_id == "szlab_mixer_pump"

    workflow = build_graph_workflow(
        flow_nodes=[
            {
                "id": "stir",
                "data": {
                    "device_id": "szlab_mixer_stirrer",
                    "method": "run_stirring",
                    "params": {"position": 1, "speed": 300, "temperature": 25, "duration": 60},
                },
            },
            {
                "id": "photo",
                "data": {
                    "device_id": "szlab_mixer_photoshotting",
                    "method": "take_photo",
                    "params": {
                        "sample_id": "sample-1",
                        "photo_path": "",
                        "inspection_result": "",
                        "require_material": False,
                    },
                },
            },
            {
                "id": "pump",
                "data": {
                    "device_id": "szlab_mixer_pump",
                    "method": "transfer_liquid",
                    "params": {"pump": 1, "volume": 1, "direction": "aspirate"},
                },
            },
        ],
        flow_edges=[
            {"source": "stir", "target": "photo"},
            {"source": "photo", "target": "pump"},
        ],
        preset=preset,
    )

    assert workflow["nodes"] == [
        {
            "uuid": "stir",
            "name": "auto-run_stirring",
            "device_name": "szlab_mixer_stirrer",
            "param": {"position": 1, "speed": 300, "temperature": 25, "duration": 60, "require_material": True},
        },
        {
            "uuid": "photo",
            "name": "auto-take_photo",
            "device_name": "szlab_mixer_photoshotting",
            "param": {
                "sample_id": "sample-1",
                "photo_path": "",
                "inspection_result": "",
                "require_material": False,
            },
        },
        {
            "uuid": "pump",
            "name": "auto-transfer_liquid",
            "device_name": "szlab_mixer_pump",
            "param": {"pump": 1, "volume": 1, "direction": "aspirate"},
        },
    ]
    assert workflow["edges"] == [
        {"source_node_uuid": "stir", "target_node_uuid": "photo"},
        {"source_node_uuid": "photo", "target_node_uuid": "pump"},
    ]


def test_szlab_mixer_device_creation_ignores_csv_path(monkeypatch, tmp_path):
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        """
        {
          "nodes": [
            {
              "id": "szlab_mixer_stirrer",
              "config": {"url": "opc.tcp://example:50001", "timeout": 30}
            },
            {
              "id": "szlab_mixer_pump",
              "config": {"url": "opc.tcp://example:50001", "timeout": 30}
            },
            {
              "id": "szlab_mixer_photoshotting",
              "config": {"url": "opc.tcp://example:50001", "timeout": 30}
            },
            {
              "id": "szlab_poly_plc",
              "config": {"url": "opc.tcp://example:50001"}
            },
            {
              "id": "szlab_mixer_robot",
              "config": {"plc_device_id": "szlab_poly_plc"}
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
        runtime_config=load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json"),
    )

    assert set(devices) == {
        "szlab_poly_plc",
        "szlab_mixer_stirrer",
        "szlab_mixer_photoshotting",
        "szlab_mixer_robot",
        "szlab_mixer_pump",
    }
    assert created == {
        0: {"url": "opc.tcp://example:50001"},
        1: {"url": "opc.tcp://example:50001", "timeout": 300.0},
        2: {"url": "opc.tcp://example:50001", "timeout": 300.0},
        3: {"plc_device_id": "szlab_poly_plc"},
        4: {"url": "opc.tcp://example:50001", "timeout": 300.0},
    }


def test_szlab_mixer_device_creation_injects_plc_gateway(monkeypatch, tmp_path):
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        """
        {
          "nodes": [
            {"id": "szlab_poly_plc", "config": {"url": "opc.tcp://example:50001"}},
            {"id": "szlab_mixer_stirrer", "config": {"url": "opc.tcp://example:50001", "use_plc_gateway": true}},
            {"id": "szlab_mixer_robot", "config": {"plc_device_id": "szlab_poly_plc"}}
          ],
          "links": []
        }
        """,
        encoding="utf-8",
    )

    class FakePLC:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeGatewayConsumer:
        def __init__(self, plc_device_id="szlab_poly_plc", **kwargs):
            self.plc_device_id = plc_device_id
            self.gateway = None

        def set_plc_gateway(self, plc_gateway):
            self.gateway = plc_gateway

    def fake_load_class(class_path):
        if class_path.endswith("SZLabPolyPLCDevice"):
            return FakePLC
        return FakeGatewayConsumer

    monkeypatch.setattr("scripts.run_workflow_local._load_class", fake_load_class)

    devices = create_local_devices(
        graph_file=graph_path,
        runtime_config=load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json"),
    )

    assert devices["szlab_mixer_stirrer"].gateway is devices["szlab_poly_plc"]
    assert devices["szlab_mixer_robot"].gateway is devices["szlab_poly_plc"]


def test_szlab_mixer_devices_use_poly_studio_plc_client(monkeypatch):
    created = []

    class FakePolyPLCClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.pump.SZLabPolyPLCDevice",
        FakePolyPLCClient,
    )
    monkeypatch.setattr(
        "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.stirrer.SZLabPolyPLCDevice",
        FakePolyPLCClient,
    )

    pump = SzlabMixerPumpDevice(
        url="opc.tcp://example:50001",
        username="user",
        password="secret",
        timeout=7.0,
        auto_connect=False,
    )
    stirrer = SzlabMixerStirrerDevice(
        url="opc.tcp://example:50001",
        username="user",
        password="secret",
        timeout=8.0,
        auto_connect=False,
    )

    assert isinstance(pump._client, FakePolyPLCClient)
    assert isinstance(stirrer._client, FakePolyPLCClient)
    assert created == [
        {
            "url": "opc.tcp://example:50001",
            "username": "user",
            "password": "secret",
            "timeout": 7.0,
            "auto_connect": False,
        },
        {
            "url": "opc.tcp://example:50001",
            "username": "user",
            "password": "secret",
            "timeout": 8.0,
            "auto_connect": False,
        },
    ]


def test_szlab_mixer_preset_loads_own_runtime_config():
    runtime_config = _load_preset_runtime_config(load_preset("szlab_mixer"))

    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_stirrer": "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.stirrer.SzlabMixerStirrerDevice",
        "szlab_mixer_photoshotting": "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.photoshotting.SzlabMixerPhotoShottingDevice",
        "szlab_mixer_robot": "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.robot.SzlabMixerRobotDevice",
        "szlab_mixer_pump": "unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.pump.SzlabMixerPumpDevice",
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

        def transfer_liquid(self, pump=1, volume=1, direction="aspirate"):
            return {"success": True}

    pump = FakePump()
    events = []
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json")

    run_nodes(
        [
            WorkflowNode(
                uuid="pump",
                name="auto-transfer_liquid",
                device_name="szlab_mixer_pump",
                param={"pump": 1, "volume": 1, "direction": "aspirate"},
            )
        ],
        {"szlab_mixer_pump": pump},
        logger=WorkflowLogger(writer=lambda message, **kwargs: events.append((message, kwargs))),
        runtime_config=runtime_config,
    )

    assert pump.sampled[0] == [
        "S06允许加工",
        "S06参数写入完成",
        "S06注射泵选择",
        "S06注射泵1抽液",
        "S06注射泵1排液",
        "S06注射泵2抽液",
        "S06注射泵2排液",
        "S06加工完成",
    ]
    assert pump.sampled[1] == pump.sampled[0]
    assert any(message.startswith("OPC状态采样") for message, _ in events)


def test_szlab_mixer_run_nodes_samples_new_stirrer_variables():
    class FakeStirrer:
        def __init__(self):
            self.sampled = []

        def get_variables(self, variable_names, use_cache=False):
            self.sampled.append(list(variable_names))
            return {name: {"success": True, "value": False} for name in variable_names}

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=2;s={variable_name}"

        def run_stirring(self, position=1, speed=300, temperature=25, duration=60, require_material=True):
            return {"success": True}

    stirrer = FakeStirrer()
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json")

    run_nodes(
        [
            WorkflowNode(
                uuid="stir",
                name="auto-run_stirring",
                device_name="szlab_mixer_stirrer",
                param={"position": 1, "speed": 300, "temperature": 25, "duration": 60, "require_material": False},
            )
        ],
        {"szlab_mixer_stirrer": stirrer},
        logger=WorkflowLogger(writer=lambda message, **kwargs: None),
        runtime_config=runtime_config,
    )

    assert "S041准备信号" in stirrer.sampled[0]
    assert "S041磁搅状态" in stirrer.sampled[0]
    assert "S041磁搅工艺选择" in stirrer.sampled[0]
    assert "磁搅温度反馈_上位机[0]" in stirrer.sampled[0]
    assert "磁搅搅拌_上位机[0]" not in stirrer.sampled[0]
    assert "磁搅加热_上位机[0]" not in stirrer.sampled[0]


def test_szlab_mixer_run_nodes_samples_photo_variables():
    class FakePhotoShotting:
        def __init__(self):
            self.sampled = []

        def get_variables(self, variable_names, use_cache=False):
            self.sampled.append(list(variable_names))
            return {name: {"success": True, "value": False} for name in variable_names}

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=2;s={variable_name}"

        def take_photo(self, sample_id="", photo_path="", inspection_result="", require_material=False):
            return {"success": True}

    photo = FakePhotoShotting()
    events = []
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json")

    run_nodes(
        [
            WorkflowNode(
                uuid="photo",
                name="auto-take_photo",
                device_name="szlab_mixer_photoshotting",
                param={"sample_id": "sample-1", "photo_path": "", "inspection_result": "", "require_material": False},
            )
        ],
        {"szlab_mixer_photoshotting": photo},
        logger=WorkflowLogger(writer=lambda message, **kwargs: events.append((message, kwargs))),
        runtime_config=runtime_config,
    )

    assert photo.sampled[0] == [
        "S05准备信号",
        "传感器状态_上位机[3].NO[0]",
        "S05拍照结果",
        "S05加工完成",
    ]
    assert photo.sampled[1] == photo.sampled[0]
    assert any(message.startswith("OPC状态采样") for message, _ in events)


def test_szlab_mixer_pump_waits_for_new_completion_cycle_when_done_is_stale():
    class FakeClient:
        def __init__(self):
            self.waits = []
            self.writes = []
            self.pulses = []

        def read(self, name):
            return {"S06允许加工": True, "S06加工完成": True}[name]

        def write(self, name, value):
            self.writes.append((name, value))

        def reset_and_pulse(self, name):
            self.pulses.append(name)

        def wait_equal(self, name, expected, timeout=300.0, interval=0.2):
            self.waits.append((name, expected))
            return True

        def wait_new_cycle_done(self, name, timeout=300.0, interval=0.2):
            if self.read(name):
                self.wait_equal(name, False, timeout=timeout, interval=interval)
            return self.wait_equal(name, True, timeout=timeout, interval=interval)

    device = SzlabMixerPumpDevice.__new__(SzlabMixerPumpDevice)
    device.timeout = 300.0
    device._status = "Idle"
    device._client = FakeClient()

    result = device.transfer_liquid(pump=2, volume=10, direction="dispense")

    assert result["success"] is True
    assert device._client.waits == [("S06加工完成", False), ("S06加工完成", True)]
    assert device._client.pulses == ["S06参数写入完成"]


def test_szlab_mixer_stirrer_waits_for_new_completion_cycle_when_done_is_stale():
    class FakeClient:
        def __init__(self):
            self.waits = []
            self.pulses = []
            self.writes = []

        def read(self, name):
            return {name: True for name in ("S041允许加工", "S041加工完成")}[name]

        def write(self, name, value):
            self.writes.append((name, value))

        def reset_and_pulse(self, name):
            self.pulses.append(name)

        def wait_equal(self, name, expected, timeout=300.0, interval=0.2):
            self.waits.append((name, expected))
            return True

        def wait_new_cycle_done(self, name, timeout=300.0, interval=0.2):
            if self.read(name):
                self.wait_equal(name, False, timeout=timeout, interval=interval)
            return self.wait_equal(name, True, timeout=timeout, interval=interval)

    device = SzlabMixerStirrerDevice.__new__(SzlabMixerStirrerDevice)
    device.timeout = 300.0
    device._status = "Idle"
    device._client = FakeClient()

    result = device.run_stirring(position=1, require_material=False)

    assert result["success"] is True
    assert device._client.waits == [("S041加工完成", False), ("S041加工完成", True)]
    assert device._client.pulses == ["S041参数写入完成"]
    assert ("S041磁搅工艺选择", 3) in device._client.writes
    assert not any(name.startswith("磁搅搅拌_上位机") for name, _ in device._client.writes)
    assert not any(name.startswith("磁搅加热_上位机") for name, _ in device._client.writes)


def test_szlab_mixer_photoshotting_records_ok_result_and_photo_path():
    class FakeClient:
        def __init__(self):
            self.waits = []

        def read(self, name):
            return {
                "S05准备信号": True,
                "传感器状态_上位机[3].NO[0]": True,
                "S05拍照结果": 1,
            }.get(name, False)

        def wait_new_cycle_done(self, name, timeout=300.0, interval=0.2):
            self.waits.append((name, timeout))
            return True

    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)
    device.timeout = 300.0
    device.save_dir = "photos"
    device._status = "Idle"
    device._last_photo_path = ""
    device._last_result = "UNKNOWN"
    device._client = FakeClient()

    result = device.take_photo(sample_id="sample-1", photo_path="/tmp/sample-1.jpg", inspection_result="manual-ok")

    assert result["success"] is True
    assert result["data"]["photo_path"] == "/tmp/sample-1.jpg"
    assert result["data"]["result"] == "OK"
    assert result["data"]["inspection_result"]["result"] == "manual-ok"
    assert device._last_photo_path == "/tmp/sample-1.jpg"
    assert device._last_result == "OK"
    assert device._client.waits == [("S05加工完成", 300.0)]


def test_szlab_mixer_photoshotting_maps_ng_and_unknown_results():
    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)

    assert device._result_label(2) == "NG"
    assert device._result_label(0) == "UNKNOWN"
    assert device._result_label("bad") == "UNKNOWN"


def test_szlab_mixer_photoshotting_rejects_not_ready_station():
    class FakeClient:
        def __init__(self):
            pass

        def read(self, name):
            return False

    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)
    device._client = FakeClient()

    result = device.take_photo(require_material=True)

    assert result == {"success": False, "message": "S05 拍照工位未准备就绪"}


def test_szlab_mixer_photoshotting_rejects_missing_material():
    class FakeClient:
        def __init__(self):
            pass

        def read(self, name):
            return {"S05准备信号": True, "传感器状态_上位机[3].NO[0]": False}[name]

    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)
    device._client = FakeClient()

    result = device.take_photo(require_material=True)

    assert result == {"success": False, "message": "S05 拍照工位未检测到物料"}


def test_szlab_mixer_photoshotting_reports_completion_timeout():
    class FakeClient:
        def __init__(self):
            pass

        def read(self, name):
            return {"S05准备信号": True, "传感器状态_上位机[3].NO[0]": True}[name]

        def wait_new_cycle_done(self, name, timeout=300.0, interval=0.2):
            return False

    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)
    device.timeout = 300.0
    device.save_dir = "photos"
    device._status = "Idle"
    device._client = FakeClient()

    result = device.take_photo(sample_id="sample-1")

    assert result["success"] is False
    assert result["message"] == "S05 拍照完成等待超时"
    assert result["data"]["photo_path"].startswith("photos/s05_photo_sample-1_")
    assert device._status == "Error"


def test_szlab_poly_plc_rejects_non_pc_to_plc_writes():
    class FakeNode:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)
            return None

    device = SZLabPolyPLCDevice.__new__(SZLabPolyPLCDevice)
    node = FakeNode()
    device.use_node = lambda name: node

    assert device.write_variable("S041参数写入完成", True) is True
    assert device.write_variable("PLC_R任务号", 8) is True
    assert device.write_variable("S04取放料编号", 3) is True
    assert node.writes == [True, 8, 3]
    try:
        device.write_variable("S02取料编号", 41)
    except PermissionError as exc:
        assert "非 PC-PLC" in str(exc)
    else:
        raise AssertionError("S02取料编号 不应用作 mixer 机器人取放入口")


def test_szlab_poly_plc_loads_new_tab_separated_communication_csv():
    names = load_variable_names_from_csv(
        "unilabos/devices/workstation/szlab_poly_studio/苏州实验室_0610.csv"
    )

    assert "S041磁搅工艺选择" in names
    assert "S041磁搅状态" in names
    assert "磁搅温度反馈_上位机[0]" in names
    assert "S05拍照结果" in names
    assert "PLC_R任务号" in names
    assert "S04取放料编号" in names


def test_szlab_mixer_runtime_samples_robot_task_variables():
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json")

    assert runtime_config.opc_snapshot.action_variables["submit_pick_from_magnetic_stirrer"] == [
        "S04取放料编号",
        "PLC_R任务号",
    ]
    assert runtime_config.opc_snapshot.action_variables["submit_place_to_magnetic_stirrer"] == [
        "S04取放料编号",
        "PLC_R任务号",
    ]
    assert runtime_config.opc_snapshot.action_variables["submit_pick_from_photo_station"] == [
        "PLC_R任务号",
    ]
    assert runtime_config.opc_snapshot.action_variables["submit_place_to_photo_station"] == [
        "PLC_R任务号",
    ]


def test_szlab_poly_plc_batch_write_validates_before_writing():
    class FakeNode:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)
            return None

    device = SZLabPolyPLCDevice.__new__(SZLabPolyPLCDevice)
    node = FakeNode()
    device.use_node = lambda name: node

    try:
        device.write_variables({"S041参数写入完成": True, "S05加工完成": True})
    except PermissionError:
        pass
    else:
        raise AssertionError("批量写入含只读变量时应整体拒绝")
    assert node.writes == []


def test_szlab_mixer_stirrer_slot_status_and_binding_use_gateway():
    class FakeGateway:
        def __init__(self):
            self.values = {
                "传感器状态_上位机[2].NO[10]": False,
                "传感器状态_上位机[2].NO[11]": True,
                "传感器状态_上位机[2].NO[12]": False,
                "传感器状态_上位机[2].NO[13]": False,
                "传感器状态_上位机[2].NO[14]": False,
                "传感器状态_上位机[2].NO[15]": False,
                "S041准备信号": True,
                "S041磁搅状态": 1,
                "S042准备信号": True,
                "S042磁搅状态": 2,
                "S043准备信号": True,
                "S043磁搅状态": 1,
                "S044准备信号": True,
                "S044磁搅状态": 1,
                "S045准备信号": True,
                "S045磁搅状态": 1,
                "S046准备信号": True,
                "S046磁搅状态": 1,
                "磁搅温度反馈_上位机[0]": 25.0,
                "磁搅温度反馈_上位机[1]": 26.0,
                "磁搅温度反馈_上位机[2]": 25.0,
                "磁搅温度反馈_上位机[3]": 25.0,
                "磁搅温度反馈_上位机[4]": 25.0,
                "磁搅温度反馈_上位机[5]": 25.0,
            }

        def read_variable(self, name, use_cache=True):
            return self.values[name]

    device = SzlabMixerStirrerDevice.__new__(SzlabMixerStirrerDevice)
    device._client = None
    device._plc_gateway = FakeGateway()
    device._sample_by_position = {}

    assert device.request_idle_position(sample_id="sample-1") == {
        "success": True,
        "position": 1,
        "sample_id": "sample-1",
    }
    assert device.bind_sample_to_position(sample_id="sample-2", position=2) == {
        "success": True,
        "position": 2,
        "sample_id": "sample-2",
        "material_confirmed": True,
        "reserved": True,
    }
    status = device.slot_status()
    assert status["slots"]["2"] == {
        "occupied": True,
        "ready": True,
        "status_value": 2,
        "status": "Busy",
        "temperature_feedback": 26.0,
        "sample_id": "sample-2",
        "reserved": True,
    }


def test_szlab_mixer_stirrer_binding_reserves_empty_position_before_robot_place():
    class FakeGateway:
        def __init__(self):
            self.values = {
                "传感器状态_上位机[2].NO[10]": False,
                "传感器状态_上位机[2].NO[11]": False,
                "传感器状态_上位机[2].NO[12]": False,
                "传感器状态_上位机[2].NO[13]": False,
                "传感器状态_上位机[2].NO[14]": False,
                "传感器状态_上位机[2].NO[15]": False,
                "S041准备信号": True,
                "S042准备信号": True,
                "S043准备信号": True,
                "S044准备信号": True,
                "S045准备信号": True,
                "S046准备信号": True,
            }

        def read_variable(self, name, use_cache=True):
            return self.values[name]

    device = SzlabMixerStirrerDevice.__new__(SzlabMixerStirrerDevice)
    device._client = None
    device._plc_gateway = FakeGateway()
    device._sample_by_position = {}

    bind = device.bind_sample_to_position(sample_id="sample-1", position=1)
    idle = device.request_idle_position(sample_id="sample-2")

    assert bind == {
        "success": True,
        "position": 1,
        "sample_id": "sample-1",
        "material_confirmed": False,
        "reserved": True,
    }
    assert idle["position"] == 2


def test_szlab_mixer_robot_actions_write_s04_and_s05_task_numbers():
    class FakeGateway:
        def __init__(self):
            self.writes = []

        def read_variable(self, name, use_cache=True):
            raise AssertionError("机器人任务提交不应读取 PLC")

        def write_variable(self, name, value):
            self.writes.append((name, value))
            return True

    device = SzlabMixerRobotDevice.__new__(SzlabMixerRobotDevice)
    device._plc_gateway = FakeGateway()
    device._last_task = {}

    pick_s04 = device.submit_pick_from_magnetic_stirrer(position=3)
    place_s04 = device.submit_place_to_magnetic_stirrer(position=4, sample_id="sample-2")
    place_s05 = device.submit_place_to_photo_station(sample_id="sample-1")
    pick_s05 = device.submit_pick_from_photo_station(sample_id="sample-1")

    assert pick_s04["success"] is True
    assert pick_s04["task_number"] == 8
    assert pick_s04["written_variables"] == {"S04取放料编号": 3, "PLC_R任务号": 8}
    assert place_s04["success"] is True
    assert place_s04["task_number"] == 7
    assert place_s04["written_variables"] == {"S04取放料编号": 4, "PLC_R任务号": 7}
    assert place_s05["success"] is True
    assert place_s05["task_number"] == 9
    assert place_s05["written_variables"] == {"PLC_R任务号": 9}
    assert pick_s05["success"] is True
    assert pick_s05["task_number"] == 10
    assert pick_s05["written_variables"] == {"PLC_R任务号": 10}
    assert device._plc_gateway.writes == [
        ("S04取放料编号", 3),
        ("PLC_R任务号", 8),
        ("S04取放料编号", 4),
        ("PLC_R任务号", 7),
        ("PLC_R任务号", 9),
        ("PLC_R任务号", 10),
    ]
    assert device.last_submitted_task()["task"]["task_number"] == 10


def test_szlab_mixer_robot_rejects_invalid_position_before_plc_write():
    class FakeGateway:
        def read_variable(self, name, use_cache=True):
            return False

        def write_variable(self, name, value):
            raise AssertionError("不应写入 PLC")

    device = SzlabMixerRobotDevice.__new__(SzlabMixerRobotDevice)
    device._plc_gateway = FakeGateway()
    device._last_task = {}

    result = device.submit_pick_from_magnetic_stirrer(position=7)

    assert result == {"success": False, "message": "磁搅位置必须在 1-6 范围内"}


def test_szlab_mixer_photoshotting_dual_view_records_algorithm_result():
    class FakeGateway:
        def __init__(self):
            self.waits = []

        def read_variable(self, name, use_cache=True):
            return {
                "S05准备信号": True,
                "传感器状态_上位机[3].NO[0]": True,
                "S05拍照结果": 1,
            }[name]

        def wait_new_cycle_done(self, name, timeout=300.0, interval=0.2):
            self.waits.append((name, timeout))
            return True

    device = SzlabMixerPhotoShottingDevice.__new__(SzlabMixerPhotoShottingDevice)
    device.timeout = 300.0
    device.save_dir = "photos"
    device._status = "Idle"
    device._client = None
    device._plc_gateway = FakeGateway()
    device._last_photo_path = ""
    device._last_result = "UNKNOWN"

    result = device.take_dual_view_photos(
        sample_id="sample-1",
        top_photo_path="/tmp/top.jpg",
        side_photo_path="/tmp/side.jpg",
    )

    assert result["success"] is True
    assert result["data"]["top_photo_path"] == "/tmp/top.jpg"
    assert result["data"]["side_photo_path"] == "/tmp/side.jpg"
    assert result["data"]["dissolution"]["dissolved"] == "unknown"
    assert result["data"]["result"] == "OK"
    assert result["data"]["pose_ok"] is True
    assert device._plc_gateway.waits == [("S05加工完成", 300.0)]
