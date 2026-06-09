from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.devices.workstation.szlab_mixer.opcua_client import SzlabMixerOpcUaClient
from unilabos.devices.workstation.szlab_mixer.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_mixer.stirrer import SzlabMixerStirrerDevice
from unilabos.szlab.run_workflow_local import create_local_devices, load_runtime_config
from unilabos.szlab.run_workflow_local import WorkflowLogger, WorkflowNode, run_nodes
from unilabos.szlab.workflow_ui import _load_preset_runtime_config, build_graph_workflow, load_preset


def test_szlab_mixer_devices_are_ast_scannable():
    root = Path("unilabos/devices/workstation/szlab_mixer")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_stirrer", "szlab_mixer_pump"}
    assert "run_stirring" in result["devices"]["szlab_mixer_stirrer"]["actions"]
    assert "transfer_liquid" in result["devices"]["szlab_mixer_pump"]["actions"]


def test_szlab_mixer_registry_actions_can_chain_two_devices():
    preset = load_preset("szlab_mixer")

    assert list(preset.actions) == ["run_stirring", "transfer_liquid"]
    assert preset.actions["run_stirring"].device_id == "szlab_mixer_stirrer"
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
                "id": "pump",
                "data": {
                    "device_id": "szlab_mixer_pump",
                    "method": "transfer_liquid",
                    "params": {"pump": 1, "volume": 1, "direction": "aspirate"},
                },
            },
        ],
        flow_edges=[{"source": "stir", "target": "pump"}],
        preset=preset,
    )

    assert workflow["nodes"] == [
        {
            "uuid": "stir",
            "name": "auto-run_stirring",
            "device_name": "szlab_mixer_stirrer",
            "param": {"position": 1, "speed": 300, "temperature": 25, "duration": 60},
        },
        {
            "uuid": "pump",
            "name": "auto-transfer_liquid",
            "device_name": "szlab_mixer_pump",
            "param": {"pump": 1, "volume": 1, "direction": "aspirate"},
        },
    ]
    assert workflow["edges"] == [{"source_node_uuid": "stir", "target_node_uuid": "pump"}]


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

    monkeypatch.setattr("unilabos.szlab.run_workflow_local._load_class", lambda class_path: FakeDevice)

    devices = create_local_devices(
        graph_file=graph_path,
        csv_path=Path("/tmp/invalid.csv"),
        runtime_config=load_runtime_config("unilabos/szlab/runtime_configs/szlab_mixer_runtime.json"),
    )

    assert set(devices) == {"szlab_mixer_stirrer", "szlab_mixer_pump"}
    assert created == {
        0: {"url": "opc.tcp://example:50001", "timeout": 300.0},
        1: {"url": "opc.tcp://example:50001", "timeout": 300.0},
    }


def test_szlab_mixer_preset_loads_own_runtime_config():
    runtime_config = _load_preset_runtime_config(load_preset("szlab_mixer"))

    assert runtime_config.device_factory.devices == {
        "szlab_mixer_stirrer": "unilabos.devices.workstation.szlab_mixer.stirrer.SzlabMixerStirrerDevice",
        "szlab_mixer_pump": "unilabos.devices.workstation.szlab_mixer.pump.SzlabMixerPumpDevice",
    }
    assert runtime_config.device_factory.plc_class == ""


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
    runtime_config = load_runtime_config("unilabos/szlab/runtime_configs/szlab_mixer_runtime.json")

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


def test_szlab_mixer_pump_waits_for_new_completion_cycle_when_done_is_stale():
    class FakeClient(SzlabMixerOpcUaClient):
        def __init__(self):
            self.waits = []
            self.writes = []
            self.pulses = []

        def read(self, name):
            return {"S06允许加工": True, "S06加工完成": True}[name]

        def write(self, name, value):
            self.writes.append((name, value))

        def pulse(self, name):
            self.pulses.append(name)

        def wait_equal(self, name, expected, timeout=300.0, interval=0.2):
            self.waits.append((name, expected))
            return True

    device = SzlabMixerPumpDevice.__new__(SzlabMixerPumpDevice)
    device.timeout = 300.0
    device._status = "Idle"
    device._client = FakeClient()

    result = device.transfer_liquid(pump=2, volume=10, direction="dispense")

    assert result["success"] is True
    assert device._client.waits == [("S06加工完成", False), ("S06加工完成", True)]


def test_szlab_mixer_stirrer_waits_for_new_completion_cycle_when_done_is_stale():
    class FakeClient(SzlabMixerOpcUaClient):
        def __init__(self):
            self.waits = []

        def read(self, name):
            return {name: True for name in ("S041允许加工", "S041加工完成")}[name]

        def write(self, name, value):
            pass

        def pulse(self, name):
            pass

        def wait_equal(self, name, expected, timeout=300.0, interval=0.2):
            self.waits.append((name, expected))
            return True

    device = SzlabMixerStirrerDevice.__new__(SzlabMixerStirrerDevice)
    device.timeout = 300.0
    device._status = "Idle"
    device._client = FakeClient()

    result = device.run_stirring(position=1)

    assert result["success"] is True
    assert device._client.waits == [("S041加工完成", False), ("S041加工完成", True)]
