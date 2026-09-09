import asyncio
import csv
import errno
import hashlib
import json
import multiprocessing
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

import scripts.run_workflow_local as run_workflow_local
import scripts.workflow_ui as workflow_ui
import scripts.opc_simulator_profiles as opc_simulator_profiles
from scripts.run_history_store import RunHistoryStore
from scripts.run_workflow_local import (
    WorkflowLogger,
    WorkflowNode,
    _load_class,
    collect_snapshot_variables,
    create_local_devices,
    load_runtime_config,
    run_nodes,
)
from scripts.workflow_ui import (
    RunRecord,
    TaskOrchestrationSnapshotPublisher,
    WorkflowRunManager,
    _load_preset_runtime_config,
    _resolve_ui_path,
    _action_to_dict,
    _runtime_supported_actions,
    _record_to_dict,
    _register_shutdown_handler,
    _run_node_with_live_opc_sampling,
    apply_preset_debug_config,
    build_graph_workflow,
    build_linear_workflow,
    create_app,
    build_local_device_graph,
    build_parser,
    extract_registered_opc_values,
    load_preset,
)
from scripts.task_execution_coordinator import (
    TaskApiConflict,
    _false_result,
    _temporary_s09_state,
    _temporary_s09_trigger_satisfied,
    _temporary_s072_state,
    _temporary_s072_trigger_satisfied,
)


def test_load_ai4c_preset():
    preset = load_preset("ai4c")

    assert preset.id == "ai4c"
    assert preset.title == "szlab 本地调试工具"
    assert preset.target_device_id == "AI4C_robot_arm"
    assert preset.default_config["graph"] == "__generated__"
    assert preset.default_config["url"] == "opc.tcp://jdht1471820.bohrium.tech:50003"
    assert preset.default_config["show_csv"] is False
    assert preset.default_config["csv"] == "ai4c_sim_updated.csv"
    assert "pick_well_plate_from_loading_rack" in preset.actions


def test_task_execution_log_contract_prefers_structured_fields_and_maps_legacy_logs(
):
    structured = workflow_ui._task_execution_log_contract(
        "文本中出现 OPC 和失败也不得覆盖结构化分类",
        level="warning",
        detail={},
        category="result",
        code="action_returned_failure",
        phase="completed",
    )
    assert structured == {
        "category": "result",
        "level": "warning",
        "code": "action_returned_failure",
        "phase": "completed",
    }

    legacy_opc = workflow_ui._task_execution_log_contract(
        "Robot_Home 条件满足",
        level="info",
        detail={"type": "opc_wait", "phase": "finish"},
    )
    assert legacy_opc == {
        "category": "opc",
        "level": "info",
        "code": "opc_wait",
        "phase": "finish",
    }

    failed_opc_wait = workflow_ui._task_execution_log_contract(
        "OPC 变量等待完成 ready == True: success=False, error=offline",
        level="info",
        detail={
            "type": "opc_wait",
            "phase": "finish",
            "success": False,
            "error": "offline",
        },
    )
    assert failed_opc_wait == {
        "category": "opc",
        "level": "error",
        "code": "opc_wait_read_failed",
        "phase": "finish",
    }

    legacy_error = workflow_ui._task_execution_log_contract(
        "节点执行失败: timeout",
        level="info",
        detail={},
    )
    assert legacy_error == {
        "category": "action",
        "level": "error",
        "code": "action_log_error",
        "phase": "executing",
    }

    legacy_opc_error = workflow_ui._task_execution_log_contract(
        "OPC 变量读取失败",
        level="info",
        detail={},
    )
    assert legacy_opc_error == {
        "category": "opc",
        "level": "error",
        "code": "opc",
        "phase": "executing",
    }

    returned_failure = workflow_ui._task_execution_log_contract(
        "动作结果: {'success': False}",
        level="info",
        detail={"result": {"success": False, "message": "设备拒绝执行"}},
    )
    assert returned_failure == {
        "category": "result",
        "level": "error",
        "code": "action_returned_failure",
        "phase": "executing",
    }


def test_ai4c_preset_csv_matches_default_opc_namespace():
    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows_by_english_name = {
            row["EnglishName"]: row for row in csv.DictReader(handle)
        }

    variables = collect_snapshot_variables(
        "pick_well_plate_from_loading_rack",
        {"position": 1},
        runtime_config,
    )

    assert preset.default_config["url"] == "opc.tcp://jdht1471820.bohrium.tech:50003"
    for variable in variables:
        assert rows_by_english_name[variable]["NodeId"].startswith("ns=4;s=UniLab|")


def test_registered_plc_values_are_unwrapped_and_distributed_to_task_service():
    sent_payloads = []

    def sender(url, payload):
        sent_payloads.append((url, payload))
        return {"version": 8} if url.endswith("/opc/registrations") else {"version": 9}

    publisher = TaskOrchestrationSnapshotPublisher(
        base_url="http://scheduler.test/api/v1",
        sender=sender,
    )

    values = extract_registered_opc_values(
        {
            "ready": {"success": True, "value": True},
            "temperature": {"success": True, "value": 23.5},
            "unavailable": {"success": False, "error": "bad node"},
        }
    )
    publisher.publish(
        workflow_path="demo.json",
        plc_device_id="szlab_poly_plc",
        runtime_url="opc.tcp://test:4840",
        registered_variables=["ready", "temperature"],
        variable_aliases={"Ready": "ready"},
        values=values,
    )

    assert values == {"ready": True, "temperature": 23.5}
    assert sent_payloads == [
        (
            "http://scheduler.test/api/v1/opc/registrations",
            {
                "workflow_path": "demo.json",
                "registration": {
                    "plc_device_id": "szlab_poly_plc",
                    "runtime_url": "opc.tcp://test:4840",
                    "variables": ["ready", "temperature"],
                    "aliases": {"Ready": "ready"},
                },
            },
        ),
        (
            "http://scheduler.test/api/v1/opc/snapshots",
            {
                "workflow_path": "demo.json",
                "expected_version": 8,
                "plc_device_id": "szlab_poly_plc",
                "sequence": 1,
                "values": {"ready": True, "temperature": 23.5},
            },
        ),
    ]


@pytest.mark.parametrize(
    ("detail", "expected_code", "expected_message"),
    [
        (
            "expected version 7, found 8",
            "version_conflict",
            "expected version 7, found 8",
        ),
        (
            {
                "code": "resource_leased",
                "message": "dynamic resources are already leased: ['robot']",
            },
            "resource_leased",
            "dynamic resources are already leased: ['robot']",
        ),
    ],
)
def test_task_action_client_decodes_409_conflicts(
    detail,
    expected_code,
    expected_message,
):
    body = json.dumps({"detail": detail}).encode()

    def sender(url, payload):
        assert url == "http://scheduler.test/api/v1/actions:claim"
        assert payload["execution_id"] == "execution-1"
        raise HTTPError(url, 409, "Conflict", {}, BytesIO(body))

    publisher = TaskOrchestrationSnapshotPublisher(
        base_url="http://scheduler.test/api/v1",
        sender=sender,
    )

    with pytest.raises(TaskApiConflict) as caught:
        publisher.claim_action(
            workflow_path="demo.json",
            expected_version=7,
            instance_id="instance-1",
            node_id="node-1",
            execution_id="execution-1",
            resources=["robot"],
        )

    assert caught.value.code == expected_code
    assert str(caught.value) == expected_message


def test_load_ai4c_preset_uses_registry_actions_from_formal_device():
    preset = load_preset("ai4c")

    assert list(preset.actions) == [
        "pick_well_plate_from_loading_rack",
        "place_well_plate_to_pipetting_station",
        "pick_well_plate_from_pipetting_station",
        "place_well_plate_to_magnetic_stirrer",
        "pick_well_plate_from_magnetic_stirrer",
        "place_well_plate_to_hplc_station",
        "pick_well_plate_from_hplc_station",
        "place_well_plate_to_unloading_rack",
    ]
    action = preset.actions["pick_well_plate_from_loading_rack"]
    assert action.label == "步骤2：从上料架抓取孔板"
    assert action.description == "步骤2：从上料架抓取孔板"
    assert action.params == [
        {
            "name": "position",
            "label": "上料架位置",
            "description": "孔板所在上料架位置，范围 1-8",
            "type": "integer",
            "min": 1,
            "max": 8,
            "default": 1,
        }
    ]


def test_load_preset_accepts_json_path(tmp_path):
    preset_path = tmp_path / "example_preset.json"
    preset_path.write_text(
        """
        {
          "id": "example",
          "title": "示例调试工具",
          "target_device_id": "robot",
          "runtime_config": "runtime.json",
          "default_workflow_name": "example_workflow",
          "default_config": {
            "graph": "__generated__",
            "csv": "example.csv"
          },
          "path_roots": ["."],
          "device_graph": {"nodes": [], "links": []},
          "actions": [
            {
              "method": "move_plate",
              "label": "移动孔板",
              "description": "示例动作",
              "params": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    preset = load_preset(str(preset_path))

    assert preset.id == "example"
    assert preset.base_dir == tmp_path
    assert preset.runtime_config == "runtime.json"
    assert "move_plate" in preset.actions


def test_ai4c_preset_uses_formal_device_class():
    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)

    assert (
        runtime_config.device_factory.target_class
        == "unilabos.devices.workstation.AI4C.AI4C_robot_arm.AI4CRobotArmDevice"
    )
    assert "pick_well_plate_from_loading_rack" in preset.actions


def test_photoshotting_preset_uses_s05_camera_config():
    preset = load_preset("debug_s05_photoshotting")
    runtime_config = _load_preset_runtime_config(preset)
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)
    graph = build_local_device_graph(
        opcua_url="opc.tcp://127.0.0.1:48405/",
        csv_path=str(csv_path),
        preset=preset,
    )

    assert preset.id == "debug_s05_photoshotting"
    assert csv_path.exists()
    assert preset.target_device_ids == ["szlab_mixer_photoshotting"]
    assert list(preset.actions) == ["take_photo"]
    assert runtime_config.device_factory.devices == {
        "szlab_mixer_photoshotting": (
            "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting."
            "SzlabMixerPhotoShottingDevice"
        )
    }
    assert collect_snapshot_variables("take_photo", {}, runtime_config) == [
        "S05加工完成",
        "S05拍照结果",
    ]

    camera_node = next(
        node for node in graph["nodes"] if node["id"] == "szlab_mixer_photoshotting"
    )
    assert camera_node["config"]["url"] == "opc.tcp://127.0.0.1:48405/"
    assert camera_node["config"]["csv_path"].endswith(
        "s05_photoshotting/photoshotting_nodes.csv"
    )
    assert (
        camera_node["config"]["save_dir"]
        == "unilabos_data/szlab_poly_studio/s05_photoshotting/photos"
    )
    assert "opcua_node_id_map" not in camera_node["config"]
    assert _action_to_dict(preset.actions["take_photo"], runtime_config)[
        "opc_variables"
    ] == [
        "S05加工完成",
        "S05拍照结果",
    ]


def test_magnetic_stirring_preset_uses_s04_stirrer_config():
    preset = load_preset("debug_s04_magnetic_stirring")
    runtime_config = _load_preset_runtime_config(preset)
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)
    graph = build_local_device_graph(
        opcua_url="opc.tcp://127.0.0.1:48405/",
        csv_path=str(csv_path),
        preset=preset,
    )

    assert preset.id == "debug_s04_magnetic_stirring"
    assert csv_path.exists()
    assert preset.target_device_ids == ["szlab_mixer_stirrer"]
    assert list(preset.actions) == ["run_stirring"]
    assert runtime_config.device_factory.devices == {
        "szlab_mixer_stirrer": (
            "unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring."
            "magnetic_stirring.SzlabMixerMagneticStirrerDevice"
        )
    }
    assert collect_snapshot_variables(
        "run_stirring", {"position": 1}, runtime_config
    ) == [
        "S041磁搅状态",
        "S041允许加工",
        "S041磁搅工艺选择",
        "S041参数写入完成",
        "S041加工完成",
        "磁搅温度反馈_上位机[0]",
        "磁搅速度设置_上位机[0]",
        "磁搅温度设置_上位机[0]",
        "磁搅时间设置_上位机[0]",
        "磁搅安全温度设置_上位机[0]",
    ]

    stirrer_node = next(
        node for node in graph["nodes"] if node["id"] == "szlab_mixer_stirrer"
    )
    assert stirrer_node["config"]["url"] == "opc.tcp://127.0.0.1:48405/"
    assert stirrer_node["config"]["csv_path"].endswith(
        "s04_magnetic_stirring/magnetic_stirring_nodes.csv"
    )
    assert "opcua_node_id_map" not in stirrer_node["config"]
    assert (
        _action_to_dict(preset.actions["run_stirring"], runtime_config)["opc_variables"]
        == []
    )


def test_single_device_runtime_does_not_force_missing_plc_gateway(
    monkeypatch, tmp_path
):
    class FakeStirrerDevice:
        def __init__(self, **config):
            self.config = config
            self.plc_device_id = config.get("plc_device_id", "szlab_poly_plc")

    monkeypatch.setattr(
        run_workflow_local,
        "load_ai4c_graph_config",
        lambda _graph_file: {
            "szlab_mixer_stirrer": {
                "url": "opc.tcp://127.0.0.1:48405/",
                "csv_path": "magnetic_stirring_nodes.csv",
            }
        },
    )
    monkeypatch.setattr(
        run_workflow_local, "_load_class", lambda _class_path: FakeStirrerDevice
    )
    runtime_config = load_runtime_config(
        "tests/szlab_poly_studio/runtime_configs/magnetic_stirring_runtime.json"
    )

    devices = create_local_devices(
        tmp_path / "graph.json", runtime_config=runtime_config
    )

    assert "szlab_mixer_stirrer" in devices
    assert devices["szlab_mixer_stirrer"].config.get("use_plc_gateway") is not True


def test_s07_robot_runtime_binds_solid_addition_to_plc_gateway(monkeypatch, tmp_path):
    class FakePlcDevice:
        def __init__(self, **config):
            self.config = config

    class FakeRobotDevice:
        def __init__(self, **config):
            self.config = config

    class FakeS07Device:
        def __init__(self, **config):
            self.config = config
            self.plc_device_id = config.get("plc_device_id", "szlab_poly_plc")
            self.plc_gateway = None

        def set_plc_gateway(self, plc_gateway):
            self.plc_gateway = plc_gateway

    def fake_load_class(class_path):
        if class_path.endswith("SZLabPolyPLCDevice"):
            return FakePlcDevice
        if class_path.endswith("SzlabMixerRobotDevice"):
            return FakeRobotDevice
        if class_path.endswith("SZLabS07SolidAdditionDevice"):
            return FakeS07Device
        raise AssertionError(class_path)

    monkeypatch.setattr(
        run_workflow_local,
        "load_ai4c_graph_config",
        lambda _graph_file: {
            "szlab_poly_plc": {"url": "opc.tcp://127.0.0.1:48405/", "csv_path": "szlab_plc_0721.csv"},
            "szlab_mixer_robot": {"plc_device_id": "szlab_poly_plc"},
            "szlab_s07_solid_addition": {"plc_device_id": "szlab_poly_plc"},
        },
    )
    monkeypatch.setattr(run_workflow_local, "_load_class", fake_load_class)
    runtime_config = load_runtime_config(
        "tests/szlab_poly_studio/runtime_configs/s07_robot_runtime.json"
    )

    devices = create_local_devices(
        tmp_path / "graph.json", runtime_config=runtime_config
    )

    assert devices["szlab_s07_solid_addition"].plc_gateway is devices["szlab_poly_plc"]
    assert devices["szlab_s07_solid_addition"].config["use_plc_gateway"] is True


def test_s06_debug_runtime_creates_only_plc_and_pump(monkeypatch, tmp_path):
    class FakePlcDevice:
        def __init__(self, **config):
            self.config = config

    class FakePumpDevice:
        def __init__(self, **config):
            self.config = config

    def fake_load_class(class_path):
        if class_path.endswith("SZLabPolyPLCDevice"):
            return FakePlcDevice
        if class_path.endswith("SzlabMixerPumpDevice"):
            return FakePumpDevice
        raise AssertionError(class_path)

    monkeypatch.setattr(
        run_workflow_local,
        "load_ai4c_graph_config",
        lambda _graph_file: {
            "szlab_poly_plc": {
                "url": "opc.tcp://127.0.0.1:48506/",
                "csv_path": "pump_nodes.csv",
            },
            "szlab_mixer_pump": {
                "url": "opc.tcp://127.0.0.1:48506/",
                "pipeline_route_specs": [
                    {
                        "pump": 1,
                        "pipeline": "aspirate",
                        "control_valve": 11,
                        "absolute_position": 21,
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(run_workflow_local, "_load_class", fake_load_class)
    runtime_config = load_runtime_config(
        "tests/szlab_poly_studio/runtime_configs/debug_s06_pump_runtime.json"
    )

    devices = create_local_devices(
        tmp_path / "graph.json",
        csv_path=Path(
            "unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv"
        ),
        runtime_config=runtime_config,
    )

    assert set(devices) == {"szlab_poly_plc", "szlab_mixer_pump"}
    assert (
        devices["szlab_poly_plc"].config["csv_path"].endswith("s06_pump/pump_nodes.csv")
    )
    assert (
        devices["szlab_mixer_pump"]
        .config["csv_path"]
        .endswith("s06_pump/pump_nodes.csv")
    )
    assert devices["szlab_mixer_pump"].config["pipeline_route_specs"] == [
        {
            "pump": 1,
            "pipeline": "aspirate",
            "control_valve": 11,
            "absolute_position": 21,
        }
    ]


def test_szlab_mixer_ui_preset_uses_current_csv_and_s04_s05_actions():
    preset = load_preset("szlab_mixer")
    runtime_config = _load_preset_runtime_config(preset)
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}

    assert graph_nodes["szlab_poly_plc"]["config"]["csv_path"].endswith("szlab_plc_0721.csv")
    assert runtime_config.device_factory.plc_device_id == "szlab_poly_plc"
    assert preset.actions["run_stirring"].device_id == "szlab_mixer_stirrer"
    assert preset.actions["take_photo"].device_id == "szlab_mixer_photoshotting"
    assert preset.actions["submit_pick_from_s04"].device_id == "szlab_mixer_robot"
    assert preset.actions["submit_place_to_s05"].device_id == "szlab_mixer_robot"

    workflow = build_graph_workflow(
        flow_nodes=[
            {
                "id": "stir",
                "data": {
                    "device_id": "szlab_mixer_stirrer",
                    "method": "run_stirring",
                    "params": {
                        "position": 1,
                        "speed": 300,
                        "temperature": 25,
                        "duration": 60,
                    },
                },
            },
            {
                "id": "photo",
                "data": {
                    "device_id": "szlab_mixer_photoshotting",
                    "method": "take_photo",
                    "params": {"sample_id": "sample-1", "require_material": False},
                },
            },
            {
                "id": "place_photo",
                "data": {
                    "device_id": "szlab_mixer_robot",
                    "method": "submit_place_to_s05",
                    "params": {"sample_id": "sample-1"},
                },
            },
        ],
        flow_edges=[
            {"source": "stir", "target": "place_photo"},
            {"source": "place_photo", "target": "photo"},
        ],
        preset=preset,
    )

    assert [node["device_id"] for node in workflow["nodes"]] == [
        "szlab_mixer_stirrer",
        "szlab_mixer_robot",
        "szlab_mixer_photoshotting",
    ]
    assert workflow["edges"] == [
        {"source_node_uuid": "stir", "target_node_uuid": "place_photo"},
        {"source_node_uuid": "place_photo", "target_node_uuid": "photo"},
    ]


def test_s07_robot_preset_includes_robot_and_solid_addition_station():
    preset = load_preset("s07_robot")
    runtime_config = _load_preset_runtime_config(preset)
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    assert preset.target_device_ids == ["szlab_mixer_robot", "szlab_s07_solid_addition"]
    assert preset.default_config["csv"] == "szlab_plc_0721.csv"
    assert csv_path.exists()
    assert set(graph_nodes) == {
        "szlab_poly_plc",
        "szlab_mixer_robot",
        "szlab_s07_solid_addition",
    }
    assert graph_nodes["szlab_s07_solid_addition"]["config"] == {
        "plc_device_id": "szlab_poly_plc",
        "poll_interval": 0.2,
    }
    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_robot": (
            "unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot.SzlabMixerRobotDevice"
        ),
        "szlab_s07_solid_addition": (
            "unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07."
            "SZLabS07SolidAdditionDevice"
        ),
    }
    assert preset.actions["submit_place_to_s071"].device_id == "szlab_mixer_robot"
    assert (
        preset.actions["scan_powder_cartridges"].device_id == "szlab_s07_solid_addition"
    )
    assert (
        preset.actions["rotate_powder_cartridge_to_feed"].device_id
        == "szlab_s07_solid_addition"
    )
    assert preset.actions["dose_powder"].device_id == "szlab_s07_solid_addition"
    assert collect_snapshot_variables("dose_powder", {}, runtime_config) == [
        "S07原点信号",
        "S07允许加工",
        "S07工艺选择",
        "S07参数写入完成",
        "S07工艺完成",
        "S07粗注粉位置号",
        "S07精注粉位置号",
        "S07注粉重量",
    ]


def test_s07_debug_preset_uses_debug_file_name():
    preset = load_preset("debug_s07_solid_addition")

    assert preset.id == "debug_s07_solid_addition"
    assert preset.target_device_ids == ["szlab_s07_solid_addition"]
    assert not Path("tests/szlab_poly_studio/presets/solid_addition_s07.json").exists()


def test_s06_debug_preset_uses_debug_file_name_and_only_pump_device():
    preset = load_preset("debug_s06_pump")
    runtime_config = _load_preset_runtime_config(preset)
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    assert preset.id == "debug_s06_pump"
    assert preset.target_device_ids == ["szlab_mixer_pump"]
    assert preset.default_config["csv"] == "pump_nodes.csv"
    assert csv_path.name == "pump_nodes.csv"
    assert csv_path.exists()
    assert set(graph_nodes) == {"szlab_poly_plc", "szlab_mixer_pump"}
    assert graph_nodes["szlab_mixer_pump"]["config"] == {
        "url": "${opcua_url}",
        "pipeline_route_specs": [
            {
                "pump": 1,
                "pipeline": "aspirate",
                "control_valve": 11,
                "absolute_position": 21,
            },
            {
                "pump": 1,
                "pipeline": "dispense",
                "control_valve": 12,
                "absolute_position": 22,
            },
            {
                "pump": 1,
                "pipeline": "air",
                "control_valve": 13,
                "absolute_position": 23,
            },
            {
                "pump": 2,
                "pipeline": "aspirate",
                "control_valve": 11,
                "absolute_position": 21,
            },
            {
                "pump": 2,
                "pipeline": "dispense",
                "control_valve": 12,
                "absolute_position": 22,
            },
            {
                "pump": 2,
                "pipeline": "air",
                "control_valve": 13,
                "absolute_position": 23,
            },
        ],
    }
    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_pump": "unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump.SzlabMixerPumpDevice",
    }
    assert preset.actions["transfer_liquid"].device_id == "szlab_mixer_pump"
    assert preset.actions["run_solvent_addition"].device_id == "szlab_mixer_pump"
    assert collect_snapshot_variables(
        "run_solvent_addition", {"process": 1}, runtime_config
    ) == [
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


def test_s09_debug_preset_uses_debug_file_name():
    preset = load_preset("debug_s09_pipetting_station")
    runtime_config = _load_preset_runtime_config(preset)
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    assert preset.id == "debug_s09_pipetting_station"
    assert preset.target_device_ids == ["szlab_mixer_pipetting_station"]
    assert (
        preset.runtime_config
        == "../runtime_configs/debug_s09_pipetting_station_runtime.json"
    )
    assert not Path(
        "tests/szlab_poly_studio/presets/s09_pipetting_station.json"
    ).exists()
    assert not Path(
        "tests/szlab_poly_studio/runtime_configs/s09_pipetting_station_runtime.json"
    ).exists()
    assert csv_path.name == "pipetting_station_nodes.csv"
    assert csv_path.exists()
    assert set(graph_nodes) == {"szlab_mixer_pipetting_station"}
    assert runtime_config.device_factory.devices == {
        "szlab_mixer_pipetting_station": (
            "unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station."
            "pipetting_station.SzlabMixerPipettingStationDevice"
        )
    }
    assert "run_process" not in preset.actions
    assert "measure_density" in preset.actions
    assert "go_to_safe_position" not in preset.actions
    assert "add_liquid" in preset.actions
    assert "add_liquid_to_beaker" in preset.actions
    add_liquid_param_names = [
        param["name"] for param in preset.actions["add_liquid"].params
    ]
    assert add_liquid_param_names[:3] == [
        "take_tip_box_index",
        "release_tip_box_index",
        "tip_index",
    ]
    assert add_liquid_param_names[-5:] == [
        "S09液体瓶1剩余液量",
        "S09液体瓶2剩余液量",
        "S09液体瓶3剩余液量",
        "S09液体瓶4剩余液量",
        "S09液体瓶5剩余液量",
    ]
    assert collect_snapshot_variables("add_liquid_to_beaker", {}, runtime_config) == [
        "S09允许加工",
        "S09工艺选择",
        "S09参数写入完成",
        "S09工艺完成",
        "工站状态[8]",
        "S09TIP盒工位编号",
        "S09TIP编号",
        "S09液体瓶编号",
        "S09抽液量",
        "S09放液量",
        "S09液体瓶1剩余液量",
        "S09液体瓶2剩余液量",
        "S09液体瓶3剩余液量",
        "S09液体瓶4剩余液量",
        "S09液体瓶5剩余液量",
    ]


def test_szlab_robot_action_workflow_preset_includes_s03_to_s07_devices():
    preset = load_preset("szlab_robot_action_workflow")
    robot_action_preset = load_preset("robot_action")
    runtime_config = _load_preset_runtime_config(preset)
    graph_nodes = {node["id"]: node for node in preset.device_graph["nodes"]}

    assert preset.id == "szlab_robot_action_workflow"
    assert preset.default_config["task_sample_start_interval_seconds"] == 1
    assert preset.target_device_ids == [
        "szlab_mixer_robot",
        "szlab_s04_magnetic_stirring",
        "szlab_s05_photoshotting",
        "szlab_s06_pump",
        "szlab_s07_solid_addition",
        "szlab_s08_cap_station",
        "szlab_mixer_pipetting_station",
    ]
    assert set(graph_nodes) == {
        "szlab_poly_plc",
        "szlab_mixer_robot",
        "szlab_s04_magnetic_stirring",
        "szlab_s05_photoshotting",
        "szlab_s06_pump",
        "szlab_s07_solid_addition",
        "szlab_s08_cap_station",
        "szlab_mixer_pipetting_station",
    }
    assert graph_nodes["szlab_poly_plc"]["config"]["csv_path"] == "${csv_path}"
    assert graph_nodes["szlab_mixer_pipetting_station"]["config"] == {
        "url": "${opcua_url}",
        "csv_path": "s09_pipetting_station/pipetting_station_nodes.csv",
    }
    assert (
        preset.debug_config["skip_robot_precheck_variables"]
        == robot_action_preset.debug_config["skip_robot_precheck_variables"]
    )
    assert "SKIP_SENSOR_PRECHECK" not in preset.debug_config.get("env", {})
    assert (
        "传感器状态_上位机[3].NO[0]"
        not in preset.debug_config["skip_robot_precheck_variables"]
    )
    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_robot": "unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot.SzlabMixerRobotDevice",
        "szlab_s04_magnetic_stirring": "unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring.SzlabMixerMagneticStirrerDevice",
        "szlab_s05_photoshotting": "unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting.SzlabMixerPhotoShottingDevice",
        "szlab_s06_pump": "unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump.SzlabMixerPumpDevice",
        "szlab_s07_solid_addition": "unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07.SZLabS07SolidAdditionDevice",
        "szlab_s08_cap_station": "unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station.SZLabS08CapStationDevice",
        "szlab_mixer_pipetting_station": "unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station.SzlabMixerPipettingStationDevice",
    }
    assert preset.actions["run_stirring"].device_id == "szlab_s04_magnetic_stirring"
    assert preset.actions["take_photo"].device_id == "szlab_s05_photoshotting"
    assert preset.actions["run_solvent_addition"].device_id == "szlab_s06_pump"
    assert (
        preset.actions["add_liquid_to_beaker"].device_id
        == "szlab_mixer_pipetting_station"
    )
    assert "run_process" not in preset.actions
    assert "measure_density" in preset.actions
    assert "add_liquid" in preset.actions
    assert "run_liquid_workflow" in preset.actions
    assert "get_pipetting_status" in preset.actions
    assert [
        action.method
        for action in preset.actions.values()
        if action.device_id == "szlab_mixer_pipetting_station"
    ] == [
        "check_home_position",
        "read_home_positions",
        "prepare_liquid_station",
        "read_allow_process",
        "bind_sample_to_station",
        "release_station",
        "add_liquid",
        "add_liquid_with_reusable_tip",
        "measure_density",
        "add_liquid_to_beaker",
        "run_liquid_workflow",
        "set_liquid_bottle_remaining_volume",
        "initialize_liquid_bottle_remaining_volumes",
        "read_balance",
        "initialize_reusable_tip_inventory",
        "get_reusable_tip_status",
        "get_pipetting_status",
    ]

    assert [
        param["name"]
        for param in preset.actions["add_liquid_with_reusable_tip"].params
    ] == [
        "liquid_station_index",
        "solvent_batch_id",
        "volume",
        "volume_unit",
        "skip_level_check",
        "reuse_tip",
        "liquid_count",
        "liquid_additions",
        "initialize_tip_inventory",
        "initial_used_tip_count",
    ]
    density_snapshot = collect_snapshot_variables(
        "measure_density",
        {"density_measurement_count": 5},
        runtime_config,
    )
    assert density_snapshot[-10:] == [
        *[f"S09抽液天平读数[{index}]" for index in range(5)],
        *[f"S09放液天平读数[{index}]" for index in range(5)],
    ]
    assert collect_snapshot_variables("dose_powder", {}, runtime_config) == [
        "S07原点信号",
        "S07允许加工",
        "S07工艺选择",
        "S07参数写入完成",
        "S07工艺完成",
        "S07粗注粉位置号",
        "S07精注粉位置号",
        "S07注粉重量",
    ]
    assert collect_snapshot_variables(
        "run_solvent_addition", {"process": 3}, runtime_config
    ) == [
        "S06准备信号",
        "S06允许加工",
        "S06工艺选择",
        "S06_1号溶液添加量",
        "S06_2号溶液添加量",
        "S06参数写入完成",
        "S06加工完成",
    ]
    assert collect_snapshot_variables("process_cap", {}, runtime_config) == [
        "S08原点信号",
        "S08允许加工",
        "S08工艺选择",
        "S08参数写入完成",
        "S08工艺完成",
        "S082瓶盖暂存位",
        "工站状态[7]",
        "传感器状态_上位机[3].NO[14]",
        "传感器状态_上位机[3].NO[15]",
        "传感器状态_上位机[4].NO[0]",
        "传感器状态_上位机[4].NO[1]",
        "传感器状态_上位机[4].NO[2]",
        "传感器状态_上位机[4].NO[3]",
        "传感器状态_上位机[4].NO[4]",
    ]
    assert collect_snapshot_variables("add_liquid_to_beaker", {}, runtime_config) == [
        "S09允许加工",
        "S09工艺选择",
        "S09参数写入完成",
        "S09工艺完成",
        "S09TIP盒工位编号",
        "S09TIP编号",
        "S09液体瓶编号",
        "S09抽液量",
        "S09放液量",
        "S09液体瓶1剩余液量",
        "S09液体瓶2剩余液量",
        "S09液体瓶3剩余液量",
        "S09液体瓶4剩余液量",
        "S09液体瓶5剩余液量",
    ]
    assert collect_snapshot_variables("submit_pour_from_s08", {}, runtime_config) == [
        "S08倒料产品选择",
        "任务号",
    ]
    assert collect_snapshot_variables("submit_place_to_s09", {}, runtime_config) == [
        "S09工艺选择",
        "S09参数写入完成",
        "S09工艺完成",
        "S09原点信号_1",
        "S09原点信号_2",
        "S09原点信号_3",
        "S09原点信号_4",
        "S09取放料产品",
        "S09取放料编号",
        "任务号",
    ]


def test_szlab_action_parameters_have_frontend_help_options_and_units():
    preset = load_preset("szlab_robot_action_workflow")

    undocumented = [
        (action.method, param["name"])
        for action in preset.actions.values()
        for param in action.params
        if not param.get("description")
    ]
    assert undocumented == []

    s03_product = next(
        param for param in preset.actions["submit_pick_from_s03"].params if param["name"] == "product_type"
    )
    assert s03_product["options"] == [
        {"value": 1, "label": "烧杯"},
        {"value": 2, "label": "250 mL 样品瓶"},
        {"value": 3, "label": "500 mL 样品瓶"},
    ]
    s072_product = next(
        param for param in preset.actions["submit_place_to_s072"].params if param["name"] == "product_type"
    )
    assert s072_product["options"] == [
        {"value": 1, "label": "固体粉末"},
        {"value": 2, "label": "烧杯"},
    ]
    assert "S072取放料产品" in s072_product["description"]
    assert "机器人任务号：15" in s072_product["description"]
    s08_product = next(
        param for param in preset.actions["submit_place_to_s08"].params if param["name"] == "product_type"
    )
    assert s08_product["options"][2] == {"value": 3, "label": "100 mL 液体瓶"}
    assert "S08取放料产品" in s08_product["description"]
    target_weight = next(
        param for param in preset.actions["dose_powder"].params if param["name"] == "target_weight"
    )
    assert target_weight["unit"] == "g（待 PLC 确认）"
    aspirate = next(
        param
        for param in preset.actions["add_liquid_to_beaker"].params
        if param["name"] == "aspirate_volume"
    )
    assert "体积单位" in aspirate["description"]
    reusable_volume = next(
        param
        for param in preset.actions["add_liquid_with_reusable_tip"].params
        if param["name"] == "volume"
    )
    assert reusable_volume["label"] == "加液体积"
    assert "S09" in reusable_volume["description"]
    assert "S06" not in reusable_volume["description"]
    density_count = next(
        param
        for param in preset.actions["measure_density"].params
        if param["name"] == "density_measurement_count"
    )
    assert density_count["label"] == "测密度次数"
    assert density_count["min"] == 1
    assert density_count["max"] == 10


def test_single_sample_workflow_uses_internal_s09_balance_read_and_correct_robot_codes():
    workflow_path = Path(
        "unilabos/devices/workstation/szlab_poly_studio/workflows/"
        "szlab_single_sample_atomic_workflow.json"
    )
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    actions = [item["action"] for item in workflow["rules"][0]["actions"]]
    methods = [action["method"] for action in actions]

    assert [action["index"] for action in actions] == list(range(1, len(actions) + 1))
    assert methods.count("read_s07_balance") == 0
    assert methods.count("read_balance") == 0
    assert methods[6:14] == [
        "submit_pick_from_s072",
        "submit_place_to_s071",
        "submit_pick_from_s071_and_rotate_to_feed",
        "submit_place_to_s072",
        "submit_pick_from_s072",
        "submit_place_to_s071",
        "submit_pick_from_s071_and_rotate_to_feed",
        "submit_place_to_s072",
    ]
    by_id = {action["workflow_node_id"]: action for action in actions}
    assert by_id["p02_powder_1_pick_and_rotate"]["params"]["load_position"] == 1
    assert by_id["p02_powder_2_pick_and_rotate"]["params"]["load_position"] == 2
    assert by_id["w01_place_beaker_s072"]["params"]["product_type"] == 2
    assert by_id["w02_pick_beaker_s072"]["params"]["product_type"] == 2
    assert by_id["p03_reagent_place_s08"]["params"]["product_type"] == 3
    assert by_id["p03_reagent_pick_s08"]["params"]["product_type"] == 3


def test_szlab_robot_action_workflow_does_not_auto_apply_debug_sensor_skips(monkeypatch):
    monkeypatch.delenv("SKIP_SENSOR_PRECHECK", raising=False)
    monkeypatch.delenv("SKIP_ROBOT_PRECHECK_VARIABLES", raising=False)

    create_app("szlab_robot_action_workflow")

    assert "SKIP_SENSOR_PRECHECK" not in os.environ
    assert "SKIP_ROBOT_PRECHECK_VARIABLES" not in os.environ


def test_szlab_robot_action_workflow_explicit_debug_keeps_sensor_gates_enabled(
    monkeypatch,
):
    from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import (
        SzlabMixerRobotDevice,
    )

    monkeypatch.setenv("SKIP_SENSOR_PRECHECK", "")
    monkeypatch.setenv("SKIP_ROBOT_PRECHECK_VARIABLES", "")
    apply_preset_debug_config("szlab_robot_action_workflow")

    device = SzlabMixerRobotDevice(auto_connect=False)
    device._read_variable = lambda *_args, **_kwargs: False
    result = device._ensure_sensor_gate(
        "传感器状态_上位机[3].NO[0]", True, "S05 必须有物料"
    )

    assert result is not None
    assert result["actual"] is False


def test_s05_material_gate_cannot_be_skipped_by_debug_environment(monkeypatch):
    from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import (
        SzlabMixerRobotDevice,
    )

    monkeypatch.setenv("SKIP_SENSOR_PRECHECK", "1")
    monkeypatch.setenv(
        "SKIP_ROBOT_PRECHECK_VARIABLES",
        "传感器状态_上位机[3].NO[0]",
    )
    device = SzlabMixerRobotDevice(auto_connect=False)
    device._read_variable = lambda *_args, **_kwargs: True

    result = device._ensure_sensor_gate(
        "传感器状态_上位机[3].NO[0]", False, "S05 放料目标位必须为空"
    )

    assert result is not None
    assert result["actual"] is True


def test_szlab_robot_action_workflow_flow_matches_requested_synthesis_route():
    flow = json.loads(
        Path("szlab_robot_action_workflow_flow.json").read_text(encoding="utf-8")
    )
    actions = [item["action"] for item in flow["rules"][0]["actions"]]

    assert flow["name"] == "szlab_robot_action_workflow"
    assert [action["index"] for action in actions] == list(range(1, 15))
    assert [(action["device_id"], action["method"]) for action in actions] == [
        ("szlab_mixer_robot", "submit_pick_from_s03"),
        ("szlab_mixer_robot", "submit_place_to_s072"),
        ("szlab_s07_solid_addition", "dose_powder"),
        ("szlab_mixer_robot", "submit_pick_from_s072"),
        ("szlab_mixer_robot", "submit_place_to_s06"),
        ("szlab_s06_pump", "run_solvent_addition"),
        ("szlab_mixer_robot", "submit_pick_from_s06"),
        ("szlab_mixer_robot", "submit_place_to_s04"),
        ("szlab_s04_magnetic_stirring", "run_stirring"),
        ("szlab_mixer_robot", "submit_pick_from_s04"),
        ("szlab_mixer_robot", "submit_place_to_s05"),
        ("szlab_s05_photoshotting", "take_photo"),
        ("szlab_mixer_robot", "submit_pick_from_s05"),
        ("szlab_mixer_robot", "submit_place_to_s10"),
    ]
    assert actions[0]["params"] == {"product_type": 1, "position": "1-1"}
    assert actions[2]["params"]["recipe_name"] == "default"
    assert actions[5]["params"] == {
        "process": 3,
        "volume": 1,
        "skip_level_check": True,
    }
    assert actions[8]["params"]["position"] == 1
    assert actions[8]["params"]["mode"] == 3
    assert actions[-1]["params"] == {"position": 1}


def test_szlab_robot_action_workflow_photos_before_density_and_pours_after():
    workflow = json.loads(
        Path("szlab_robot_action_workflow.json").read_text(encoding="utf-8")
    )
    actions = [item["action"] for item in workflow["rules"][0]["actions"]]
    node_ids = [action["workflow_node_id"] for action in actions]
    actions_by_node_id = {
        action["workflow_node_id"]: action
        for action in actions
    }

    assert [action["index"] for action in actions] == list(range(1, 28))
    assert len(node_ids) == len(set(node_ids))
    assert node_ids[11:18] == [
        "w04_run_stirring_s04",
        "w06_pick_beaker_s04",
        "w06_place_beaker_s05",
        "w06_take_photo_s05",
        "w06_pick_beaker_s05_for_density",
        "w05_place_beaker_s09_for_density",
        "w05_measure_density_s09",
    ]
    assert node_ids[21:24] == [
        "w06_pick_beaker_s09_after_density",
        "w07_pour_beaker_s08",
        "w07_place_beaker_s11",
    ]
    assert (
        actions_by_node_id["w06_take_photo_s05"]["params"][
            "trigger_dissolution_detection"
        ]
        is True
    )
    assert not {
        "w06_place_beaker_s05_after_density",
        "w06_take_photo_s05_after_density",
        "w07_pick_beaker_s05_after_density",
    } & set(node_ids)
    assert actions_by_node_id["w03_add_liquid_s09"]["params"]["liquid_additions"] == [
        {
            "liquid_station_index": 1,
            "solvent_batch_id": "solvent-batch-001",
            "volume": 5000,
            "reuse_tip": True,
        }
    ]


def test_ai4c_runtime_device_classes_are_importable():
    if find_spec("rclpy") is None:
        pytest.skip("rclpy 未安装，跳过依赖 ROS2 的 AI4C 设备类导入检查")

    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)

    plc_class = _load_class(runtime_config.device_factory.plc_class)
    target_class = _load_class(runtime_config.device_factory.target_class)

    assert plc_class.__name__ == "AI4CPLCDevice"
    assert target_class.__name__ == "AI4CRobotArmDevice"


def test_build_linear_workflow_creates_nodes_and_ordered_edges():
    workflow = build_linear_workflow(
        [
            {"method": "pick_well_plate_from_loading_rack", "params": {"position": 2}},
            {"method": "place_well_plate_to_pipetting_station", "params": {}},
            {"method": "place_well_plate_to_unloading_rack", "params": {"position": 3}},
        ],
        name="local_test",
    )

    assert workflow["name"] == "local_test"
    assert workflow["nodes"] == [
        {
            "uuid": "step_001_pick_well_plate_from_loading_rack",
            "name": "auto-pick_well_plate_from_loading_rack",
            "device_name": "AI4C_robot_arm",
            "param": {"position": 2},
        },
        {
            "uuid": "step_002_place_well_plate_to_pipetting_station",
            "name": "auto-place_well_plate_to_pipetting_station",
            "device_name": "AI4C_robot_arm",
            "param": {},
        },
        {
            "uuid": "step_003_place_well_plate_to_unloading_rack",
            "name": "auto-place_well_plate_to_unloading_rack",
            "device_name": "AI4C_robot_arm",
            "param": {"position": 3},
        },
    ]
    assert workflow["edges"] == [
        {
            "source_node_uuid": "step_001_pick_well_plate_from_loading_rack",
            "target_node_uuid": "step_002_place_well_plate_to_pipetting_station",
        },
        {
            "source_node_uuid": "step_002_place_well_plate_to_pipetting_station",
            "target_node_uuid": "step_003_place_well_plate_to_unloading_rack",
        },
    ]


@pytest.mark.parametrize("position", [0, 9])
def test_build_linear_workflow_rejects_invalid_rack_position(position):
    with pytest.raises(ValueError, match="position 必须在 1-8 范围内"):
        build_linear_workflow(
            [
                {
                    "method": "pick_well_plate_from_loading_rack",
                    "params": {"position": position},
                }
            ],
        )


def test_build_linear_workflow_rejects_unknown_method():
    with pytest.raises(ValueError, match="不支持的动作"):
        build_linear_workflow([{"method": "unknown_action", "params": {}}])


def test_build_linear_workflow_rejects_empty_steps():
    with pytest.raises(ValueError, match="至少需要一个 workflow 步骤"):
        build_linear_workflow([])


def test_build_graph_workflow_creates_dag_nodes_and_edges():
    workflow = build_graph_workflow(
        flow_nodes=[
            {
                "id": "load",
                "position": {"x": 0, "y": 0},
                "data": {
                    "method": "pick_well_plate_from_loading_rack",
                    "params": {"position": 2},
                    "opc_variables": ["ready", "ready", "done"],
                },
            },
            {
                "id": "pipette",
                "position": {"x": 220, "y": 0},
                "data": {
                    "method": "place_well_plate_to_pipetting_station",
                    "params": {},
                },
            },
            {
                "id": "hplc",
                "position": {"x": 220, "y": 140},
                "data": {"method": "place_well_plate_to_hplc_station", "params": {}},
            },
            {
                "id": "unload",
                "position": {"x": 440, "y": 0},
                "data": {
                    "method": "place_well_plate_to_unloading_rack",
                    "params": {"position": 4},
                },
            },
        ],
        flow_edges=[
            {"id": "load-pipette", "source": "load", "target": "pipette"},
            {"id": "load-hplc", "source": "load", "target": "hplc"},
            {"id": "pipette-unload", "source": "pipette", "target": "unload"},
            {"id": "hplc-unload", "source": "hplc", "target": "unload"},
        ],
        name="canvas_test",
    )

    assert workflow["name"] == "canvas_test"
    assert workflow["nodes"] == [
        {
            "workflow_node_id": "load",
            "device_id": "AI4C_robot_arm",
            "method": "pick_well_plate_from_loading_rack",
            "params": {"position": 2},
            "opc_variables": ["ready", "done"],
        },
        {
            "workflow_node_id": "pipette",
            "device_id": "AI4C_robot_arm",
            "method": "place_well_plate_to_pipetting_station",
            "params": {},
            "opc_variables": [],
        },
        {
            "workflow_node_id": "hplc",
            "device_id": "AI4C_robot_arm",
            "method": "place_well_plate_to_hplc_station",
            "params": {},
            "opc_variables": [],
        },
        {
            "workflow_node_id": "unload",
            "device_id": "AI4C_robot_arm",
            "method": "place_well_plate_to_unloading_rack",
            "params": {"position": 4},
            "opc_variables": [],
        },
    ]
    assert workflow["edges"] == [
        {"source_node_uuid": "load", "target_node_uuid": "pipette"},
        {"source_node_uuid": "load", "target_node_uuid": "hplc"},
        {"source_node_uuid": "pipette", "target_node_uuid": "unload"},
        {"source_node_uuid": "hplc", "target_node_uuid": "unload"},
    ]


@pytest.mark.parametrize(
    "opc_variables",
    [
        ["valid", ""],
        ["valid", 1],
        ["valid", " "],
        "valid",
        [f"variable-{index}" for index in range(501)],
    ],
)
def test_build_graph_workflow_rejects_invalid_node_opc_variables(opc_variables):
    with pytest.raises(ValueError, match="opc_variables"):
        build_graph_workflow(
            flow_nodes=[
                {
                    "id": "load",
                    "data": {
                        "method": "pick_well_plate_from_loading_rack",
                        "params": {"position": 2},
                        "opc_variables": opc_variables,
                    },
                }
            ],
            flow_edges=[],
        )


def test_build_graph_workflow_emits_only_explicit_disabled_node_fields():
    workflow = build_graph_workflow(
        flow_nodes=[
            {
                "id": "load",
                "data": {
                    "method": "pick_well_plate_from_loading_rack",
                    "params": {"position": 2},
                    "execution_disabled": True,
                },
            }
        ],
        flow_edges=[],
    )

    assert workflow["nodes"] == [
        {
            "workflow_node_id": "load",
            "device_id": "AI4C_robot_arm",
            "method": "pick_well_plate_from_loading_rack",
            "params": {"position": 2},
            "opc_variables": [],
            "disabled": True,
        }
    ]


def test_build_graph_workflow_rejects_cycle():
    with pytest.raises(ValueError, match="不能包含环"):
        build_graph_workflow(
            flow_nodes=[
                {
                    "id": "a",
                    "data": {
                        "method": "place_well_plate_to_pipetting_station",
                        "params": {},
                    },
                },
                {
                    "id": "b",
                    "data": {
                        "method": "pick_well_plate_from_pipetting_station",
                        "params": {},
                    },
                },
            ],
            flow_edges=[
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
        )


def test_build_graph_workflow_rejects_edge_with_missing_node():
    with pytest.raises(ValueError, match="连线引用了不存在的节点"):
        build_graph_workflow(
            flow_nodes=[
                {
                    "id": "a",
                    "data": {
                        "method": "place_well_plate_to_pipetting_station",
                        "params": {},
                    },
                }
            ],
            flow_edges=[{"source": "a", "target": "missing"}],
        )


def test_build_graph_workflow_rejects_invalid_node_position_param():
    with pytest.raises(ValueError, match="position 必须在 1-8 范围内"):
        build_graph_workflow(
            flow_nodes=[
                {
                    "id": "load",
                    "data": {
                        "method": "pick_well_plate_from_loading_rack",
                        "params": {"position": 12},
                    },
                }
            ],
            flow_edges=[],
        )


def test_build_local_device_graph_uses_runtime_config_without_csv_by_default():
    graph = build_local_device_graph(
        opcua_url="opc.tcp://example:4840",
        use_subscription=False,
    )

    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["AI4C_plc"]["config"] == {
        "url": "opc.tcp://example:4840",
        "use_subscription": False,
    }
    assert nodes["AI4C_robot_arm"]["config"] == {"plc_device_id": "AI4C_plc"}
    assert graph["links"] == []


def test_build_local_device_graph_keeps_csv_when_explicitly_configured():
    graph = build_local_device_graph(
        opcua_url="opc.tcp://example:4840",
        csv_path="ai4c_sim_updated.csv",
        use_subscription=False,
    )

    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["AI4C_plc"]["config"]["csv_path"] == "ai4c_sim_updated.csv"


def test_s06_robot_generated_graph_keeps_csv_path_without_node_id_map():
    preset = load_preset("s06_robot")
    assert not Path(preset.default_config["csv"]).is_absolute()
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    graph = build_local_device_graph(
        opcua_url=preset.default_config["url"],
        csv_path=str(csv_path),
        use_subscription=False,
        preset=preset,
    )

    nodes = {node["id"]: node for node in graph["nodes"]}
    pump_config = nodes["szlab_mixer_pump"]["config"]
    assert pump_config["csv_path"] == str(csv_path)
    assert "opcua_node_id_map" not in pump_config


def test_szlab_mixer_pump_runtime_snapshot_variables_are_mapped_for_production_opcua():
    runtime_config = load_runtime_config(
        "tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json"
    )
    graph = json.loads(
        Path(
            "tests/szlab_poly_studio/fixtures/szlab_mixer_pump_production_graph.json"
        ).read_text(encoding="utf-8")
    )
    pump_node = next(
        node for node in graph["nodes"] if node["id"] == "szlab_mixer_pump"
    )
    node_id_map = pump_node["config"]["opcua_node_id_map"]

    for method_name in ("transfer_liquid", "run_solvent_addition"):
        variables = collect_snapshot_variables(method_name, {}, runtime_config)
        assert variables
        assert set(variables) <= set(node_id_map)


def test_pump_runtime_only_exposes_pump_actions():
    preset = load_preset("szlab_mixer")
    runtime_config = load_runtime_config(
        "tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json"
    )

    actions = _runtime_supported_actions(preset, runtime_config)

    assert actions == {}
    assert "run_stirring" not in actions


def test_runtime_config_collects_common_action_and_param_variables(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        """
        {
          "device_factory": {
            "plc_device_id": "plc",
            "target_device_id": "robot",
            "route_aliases": ["station"],
            "plc_class": "example.PLC",
            "target_class": "example.Robot",
            "target_config": {"plc_device_id": "plc"},
            "direct_plc_command_method": "_call_plc_command"
          },
          "opc_snapshot": {
            "common_variables": ["Common_A"],
            "action_variables": {
              "move_plate": ["Move_A"]
            },
            "param_variables": {
              "move_plate": [
                {"param": "position", "template": "Rack[{position_minus_1}]"}
              ]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    runtime_config = load_runtime_config(config_path)

    assert runtime_config.device_factory.target_device_id == "robot"
    assert runtime_config.device_factory.route_aliases == {"station"}
    assert collect_snapshot_variables(
        "move_plate", {"position": 3}, runtime_config
    ) == [
        "Common_A",
        "Move_A",
        "Rack[2]",
    ]


def test_run_record_returns_structured_log_events_with_node_id():
    record = RunRecord(run_id="run-1")

    record.append_log("workflow 准备完成")
    record.append_log(
        "节点开始执行",
        node_id="node_1",
        level="info",
        detail={"method": "pick_well_plate_from_loading_rack"},
    )

    payload = _record_to_dict(record)

    assert payload["logs"] == ["workflow 准备完成", "节点开始执行"]
    assert payload["log_events"] == [
        {
            "sequence": 1,
            "message": "workflow 准备完成",
            "level": "info",
            "category": "workflow",
            "scope": "workflow",
            "node_id": None,
            "detail": None,
        },
        {
            "sequence": 2,
            "message": "节点开始执行",
            "level": "info",
            "category": "node",
            "scope": "node",
            "node_id": "node_1",
            "detail": {"method": "pick_well_plate_from_loading_rack"},
        },
    ]


def test_run_record_live_status_updates_in_place_and_can_be_cleared():
    record = RunRecord(run_id="run-1")

    record.update_live_status(
        "node_1",
        "s07_balance",
        {"label": "S07 实时天平", "value": 12.1, "unit": "g", "state": "ok"},
    )
    record.update_live_status(
        "node_1",
        "s07_balance",
        {"label": "S07 实时天平", "value": 12.34, "unit": "g", "state": "ok"},
    )

    payload = _record_to_dict(record)
    assert payload["live_statuses"] == {
        "node_1": {
            "s07_balance": {
                "label": "S07 实时天平",
                "value": 12.34,
                "unit": "g",
                "state": "ok",
            }
        }
    }
    assert record.logs == []
    assert record.log_events == []

    record.clear_live_status("node_1", "s07_balance")
    assert _record_to_dict(record)["live_statuses"] == {}


def test_run_record_live_log_replaces_message_without_appending():
    record = RunRecord(run_id="run-1")

    record.update_live_log("node_1", "s07_balance", "S07 实时天平：12.100 g（每 2 秒刷新）")
    record.update_live_log("node_1", "s07_balance", "S07 实时天平：12.340 g（每 2 秒刷新）")

    assert record.logs == ["S07 实时天平：12.340 g（每 2 秒刷新）"]
    assert len(record.log_events) == 1
    assert record.log_events[0].sequence == 1
    assert record.log_events[0].category == "node"
    assert record.log_events[0].message == "S07 实时天平：12.340 g（每 2 秒刷新）"


def test_register_shutdown_handler_supports_fastapi_on_event_only():
    registered = {}

    class AppWithOnEventOnly:
        def on_event(self, event_name):
            def decorator(handler):
                registered[event_name] = handler
                return handler

            return decorator

    def shutdown():
        registered["called"] = True

    _register_shutdown_handler(AppWithOnEventOnly(), shutdown)
    registered["shutdown"]()

    assert registered["called"] is True


def test_workflow_ui_parser_defaults_match_actual_local_service():
    args = build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8014
    assert args.preset == "ai4c"
    assert args.runtime_config is None
    assert args.no_browser is False
    assert args.debug is False


def test_workflow_ui_main_uses_build_parser_and_preserves_cli_options(
    monkeypatch, tmp_path
):
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text("{}", encoding="utf-8")
    parser = build_parser()
    parse_calls = []
    original_parse_args = parser.parse_args

    def tracked_parse_args(*args, **kwargs):
        parse_calls.append(True)
        return original_parse_args(*args, **kwargs)

    monkeypatch.setattr(parser, "parse_args", tracked_parse_args)
    monkeypatch.setattr(workflow_ui, "build_parser", lambda: parser)
    monkeypatch.setattr(
        "sys.argv",
        [
            "workflow_ui.py",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--preset",
            "szlab_mixer",
            "--runtime-config",
            str(runtime_config),
            "--no-browser",
            "--debug",
        ],
    )
    start_calls = []
    debug_calls = []
    monkeypatch.setattr(workflow_ui, "ignore_opcua_token_time_drift", lambda: None)
    monkeypatch.setattr(workflow_ui, "apply_preset_debug_config", debug_calls.append)
    monkeypatch.setattr(
        workflow_ui, "start_ui", lambda **kwargs: start_calls.append(kwargs)
    )

    assert workflow_ui.main() == 0
    assert parse_calls == [True]
    assert debug_calls == ["szlab_mixer"]
    assert start_calls == [
        {
            "host": "0.0.0.0",
            "port": 9001,
            "open_browser": False,
            "preset_name": "szlab_mixer",
            "runtime_config": load_runtime_config(runtime_config),
            "timing_enabled": False,
        }
    ]


def test_workflow_run_manager_reuses_devices_and_persists_direct_runs(
    tmp_path,
    monkeypatch,
):
    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)
    history_store = RunHistoryStore(tmp_path, session_id="direct-workflows")
    manager = WorkflowRunManager(
        preset,
        runtime_config,
        run_history_store=history_store,
    )
    created_devices = [{"AI4C_plc": object(), "AI4C_robot_arm": object()}]
    create_calls = []
    disconnect_calls = []

    def fake_create_local_devices(**kwargs):
        active_records = [
            record
            for record in manager._records.values()
            if record.status == "preparing"
        ]
        assert active_records[-1].node_statuses == {"manual-node": "preparing"}
        create_calls.append(kwargs)
        return created_devices[0]

    def fake_run_nodes(ordered_nodes, devices, logger=None, runtime_config=None):
        assert devices is created_devices[0]
        return [{"uuid": ordered_nodes[0].uuid, "result": {"success": True}}]

    monkeypatch.setattr(
        "scripts.workflow_ui.create_local_devices", fake_create_local_devices
    )
    monkeypatch.setattr("scripts.workflow_ui.run_nodes", fake_run_nodes)
    monkeypatch.setattr(
        "scripts.workflow_ui._disconnect_devices",
        lambda devices, log=None: disconnect_calls.append(devices),
    )

    payload = {
        "workflow": build_graph_workflow(
            flow_nodes=[{
                "id": "manual-node",
                "data": {
                    "method": "place_well_plate_to_pipetting_station",
                    "params": {},
                },
            }],
            flow_edges=[],
            preset=preset,
        ),
        "graph": "__generated__",
        "url": "opc.tcp://example:4840",
        "no_subscription": True,
    }

    manager._records["run-1"] = RunRecord(run_id="run-1")
    manager._run_payload("run-1", payload)
    manager._records["run-2"] = RunRecord(run_id="run-2")
    manager._run_payload("run-2", payload)

    assert len(create_calls) == 1
    assert create_calls[0]["csv_path"].name == "ai4c_sim_updated.csv"
    assert disconnect_calls == []
    assert manager._records["run-1"].status == "completed"
    assert manager._records["run-2"].status == "completed"
    assert (tmp_path / "history.db").exists()
    actions = history_store.station_ledger("direct-workflows")["stations"][0][
        "actions"
    ]
    assert len(actions) == 2
    assert {action["instance_id"] for action in actions} == {"run-1", "run-2"}
    assert {action["status"] for action in actions} == {"completed"}


def test_workflow_manager_shutdown_waits_for_coordinator_before_devices(
    monkeypatch,
):
    preset = load_preset("ai4c")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    order = []

    class FakeCoordinator:
        def shutdown(self):
            order.append("coordinator")
            return {"success": True, "in_flight": 0}

    manager._task_execution_coordinator = FakeCoordinator()
    monkeypatch.setattr(
        manager,
        "_disconnect_cached_devices",
        lambda: order.append("devices"),
    )

    result = manager.shutdown()

    assert result == {"success": True, "in_flight": 0}
    assert order == ["coordinator", "devices"]


def test_workflow_run_manager_exposes_and_clears_s07_live_balance(monkeypatch):
    preset = load_preset("szlab_robot_action_workflow")
    runtime_config = _load_preset_runtime_config(preset)
    manager = WorkflowRunManager(preset, runtime_config)
    observed_statuses = []

    class FakeS07:
        def __init__(self):
            self.callback = None

        def set_balance_status_callback(self, callback):
            self.callback = callback

    fake_s07 = FakeS07()

    def fake_create_local_devices(**_kwargs):
        return {
            "szlab_poly_plc": object(),
            "szlab_s07_solid_addition": fake_s07,
        }

    def fake_run_node(node, devices, logger=None, runtime_config=None):
        del logger, runtime_config
        assert devices["szlab_s07_solid_addition"] is fake_s07
        assert callable(fake_s07.callback)
        fake_s07.callback(
            {
                "label": "S07 实时天平",
                "value": 12.34,
                "unit": "g",
                "state": "ok",
            }
        )
        observed_statuses.append(_record_to_dict(manager._records["run-live"])["live_statuses"])
        return [{"uuid": node.uuid, "result": {"success": True}}]

    monkeypatch.setattr("scripts.workflow_ui.create_local_devices", fake_create_local_devices)
    monkeypatch.setattr("scripts.workflow_ui._run_node_with_live_opc_sampling", fake_run_node)

    payload = {
        "workflow": build_linear_workflow(
            [
                {
                    "method": "dose_powder",
                    "params": {
                        "coarse_position": 1,
                        "fine_position": 2,
                        "target_weight": 12.5,
                    },
                }
            ],
            preset=preset,
        ),
        "graph": "__generated__",
        "url": "opc.tcp://example:4840",
        "no_subscription": True,
    }
    manager._records["run-live"] = RunRecord(run_id="run-live")

    manager._run_payload("run-live", payload)

    assert observed_statuses == [
        {
            next(iter(manager._records["run-live"].node_statuses)): {
                "s07_balance": {
                    "label": "S07 实时天平",
                    "value": 12.34,
                    "unit": "g",
                    "state": "ok",
                }
            }
        }
    ]
    assert manager._records["run-live"].live_statuses == {}
    assert fake_s07.callback is None
    assert any(
        message == "S07 实时天平：12.340 g（每 2 秒刷新）"
        for message in manager._records["run-live"].logs
    )
    assert not any("粗注粉结束观测值" in message for message in manager._records["run-live"].logs)


def test_run_node_with_live_opc_sampling_logs_changes_during_action(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        """
        {
          "device_factory": {
            "target_device_id": "pump"
          },
          "opc_snapshot": {
            "action_variables": {
              "run_solvent_addition": ["S06加工完成"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    class FakePump:
        def __init__(self):
            self.value = 0

        def get_variables(self, variable_names, use_cache=False):
            return {
                name: {"success": True, "value": self.value} for name in variable_names
            }

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=4;s={variable_name}"

        def run_solvent_addition(self):
            self.value = 1
            time.sleep(0.03)
            self.value = 2
            time.sleep(0.03)
            return {"success": True}

    events = []

    def write_event(message, *, level="info", detail=None):
        events.append({"message": message, "level": level, "detail": detail})

    node = WorkflowNode(
        uuid="node_1",
        name="auto-run_solvent_addition",
        device_name="pump",
        param={},
    )
    pump = FakePump()

    results = _run_node_with_live_opc_sampling(
        node,
        {"pump": pump},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    assert results == [
        {
            "uuid": "node_1",
            "device_name": "pump",
            "method": "run_solvent_addition",
            "param": {},
            "opc_before": {"S06加工完成": {"success": True, "value": 0}},
            "opc_after": {"S06加工完成": {"success": True, "value": 2}},
            "result": {"success": True},
        }
    ]
    live_events = [
        event for event in events if event["message"].startswith("OPC实时变化:")
    ]
    assert live_events
    assert live_events[-1]["detail"]["changes"][0]["after"] == {
        "success": True,
        "value": 2,
    }


def test_task_node_sampling_uses_bound_action_without_resolving_descriptor_again():
    class CountingDescriptor:
        def __init__(self):
            self.binds = 0
            self.calls = 0

        def __get__(self, instance, _owner):
            if instance is None:
                return self
            self.binds += 1

            def bound(**_params):
                self.calls += 1
                return {"success": True}

            return bound

    descriptor = CountingDescriptor()

    class ActionDevice:
        execute_custom = descriptor

    device = ActionDevice()
    action_callable = getattr(device, "execute_custom")
    node = WorkflowNode(
        uuid="custom-node",
        name="ignored-name",
        device_name="custom-device",
        param={"amount": 2},
        method="execute_custom",
        legacy_route_compatible=False,
    )
    runtime_config = run_workflow_local.RuntimeConfig(
        path=Path("runtime.json"),
        device_factory=run_workflow_local.RuntimeDeviceFactoryConfig(),
        opc_snapshot=run_workflow_local.RuntimeOpcSnapshotConfig(),
    )

    result = _run_node_with_live_opc_sampling(
        node,
        {"custom-device": device},
        action_callable=action_callable,
        logger=WorkflowLogger(writer=lambda *_args, **_kwargs: None),
        runtime_config=runtime_config,
    )

    assert result[0]["result"] == {"success": True}
    assert descriptor.binds == 1
    assert descriptor.calls == 1


@pytest.mark.parametrize(
    "display_message",
    [
        "S07 注粉完成：目标 10.000 g，最终 10.012 g，偏差 +0.012 g",
        "S09 密度结果：0.9982 g/mL（抽液、放液各测 1 次，共 2 个结果）",
    ],
)
def test_task_node_sampling_forwards_measurement_display_message(display_message):
    class MeasurementDevice:
        def measure(self):
            return {"success": True, "display_message": display_message}

    device = MeasurementDevice()
    node = WorkflowNode(
        uuid="measurement-node",
        name="measure",
        device_name="measurement-device",
        param={},
        method="measure",
        legacy_route_compatible=False,
    )
    runtime_config = run_workflow_local.RuntimeConfig(
        path=Path("runtime.json"),
        device_factory=run_workflow_local.RuntimeDeviceFactoryConfig(),
        opc_snapshot=run_workflow_local.RuntimeOpcSnapshotConfig(),
    )
    messages = []

    _run_node_with_live_opc_sampling(
        node,
        {"measurement-device": device},
        action_callable=device.measure,
        logger=WorkflowLogger(writer=lambda message, **_kwargs: messages.append(message)),
        runtime_config=runtime_config,
    )

    assert display_message in messages


def test_real_node_runner_wrappers_preserve_recursive_false_results():
    class ActionDevice:
        def bare_false(self):
            return False

        def tuple_false(self):
            return (False, "failed")

        def nested_false(self):
            return {"payload": {"status": {"success": False}}}

        def normal_result(self):
            return {"message": "false", "value": 0}

    device = ActionDevice()
    runtime_config = run_workflow_local.RuntimeConfig(
        path=Path("runtime.json"),
        device_factory=run_workflow_local.RuntimeDeviceFactoryConfig(),
        opc_snapshot=run_workflow_local.RuntimeOpcSnapshotConfig(),
    )

    helper_node = WorkflowNode(
        uuid="helper-false",
        name="bare_false",
        device_name="device",
        param={},
        method="bare_false",
        legacy_route_compatible=False,
    )
    messages = []
    logger = WorkflowLogger(
        writer=lambda message, **_kwargs: messages.append(message)
    )
    with pytest.raises(workflow_ui.ActionReturnedFailure) as bare_failure:
        _run_node_with_live_opc_sampling(
            helper_node,
            {"device": device},
            action_callable=device.bare_false,
            logger=logger,
            runtime_config=runtime_config,
        )
    with pytest.raises(workflow_ui.ActionReturnedFailure) as tuple_failure:
        run_nodes(
            [
                WorkflowNode(
                    uuid="tuple-false",
                    name="auto-tuple_false",
                    device_name="device",
                    param={},
                )
            ],
            {"device": device},
            logger=logger,
            runtime_config=runtime_config,
        )
    with pytest.raises(workflow_ui.ActionReturnedFailure) as nested_failure:
        run_nodes(
            [
                WorkflowNode(
                    uuid="nested-false",
                    name="auto-nested_false",
                    device_name="device",
                    param={},
                )
            ],
            {"device": device},
            logger=logger,
            runtime_config=runtime_config,
        )
    normal_output = run_nodes(
        [
            WorkflowNode(
                uuid="normal",
                name="auto-normal_result",
                device_name="device",
                param={},
            )
        ],
        {"device": device},
        logger=WorkflowLogger(writer=lambda *_args, **_kwargs: None),
        runtime_config=runtime_config,
    )

    assert bare_failure.value.failure is False
    assert tuple_failure.value.failure == (False, "failed")
    assert nested_failure.value.failure == {"success": False}
    result_messages = [
        message for message in messages if message.startswith("动作结果")
    ]
    assert len(result_messages) == 3
    assert result_messages[0] == "动作结果：失败 · False"
    assert _false_result(normal_output) is None


def test_run_node_with_live_opc_sampling_emits_opc_wait_events(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        """
        {
          "device_factory": {
            "plc_device_id": "plc",
            "target_device_id": "pump"
          },
          "opc_snapshot": {
            "action_variables": {
              "run_solvent_addition": ["S06加工完成"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    class FakePLC:
        def get_variables(self, variable_names, use_cache=False):
            return {name: {"success": True, "value": False} for name in variable_names}

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=4;s={variable_name}"

        def drain_opc_wait_events(self):
            return [
                {
                    "message": "等待 OPC 变量 S06加工完成 == True (interval=0.2s)",
                    "detail": {
                        "type": "opc_wait",
                        "phase": "start",
                        "variable": "S06加工完成",
                        "expected": True,
                        "interval": 0.2,
                    },
                    "phase": "start",
                },
                {
                    "message": "OPC 变量等待完成 S06加工完成 == True: success=True, last_value=True",
                    "detail": {
                        "type": "opc_wait",
                        "phase": "finish",
                        "variable": "S06加工完成",
                        "expected": True,
                        "interval": 0.2,
                        "success": True,
                        "last_value": True,
                    },
                    "phase": "finish",
                },
            ]

    class FakePump:
        def run_solvent_addition(self):
            return {"success": True}

    events = []

    def write_event(message, *, level="info", detail=None):
        events.append({"message": message, "level": level, "detail": detail})

    _run_node_with_live_opc_sampling(
        WorkflowNode(
            uuid="node_1",
            name="auto-run_solvent_addition",
            device_name="pump",
            param={},
        ),
        {"plc": FakePLC(), "pump": FakePump()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    wait_events = [
        event
        for event in events
        if event["detail"] and event["detail"].get("type") == "opc_wait"
    ]
    assert [event["message"] for event in wait_events] == [
        "等待 OPC 变量 S06加工完成 == True (interval=0.2s)",
        "OPC 变量等待完成 S06加工完成 == True: success=True, last_value=True",
    ]
    assert wait_events[0]["detail"]["phase"] == "start"
    assert wait_events[1]["detail"]["phase"] == "finish"


def test_run_node_with_live_opc_sampling_marks_failed_wait_as_error():
    class FailedWaitDevice:
        def run(self):
            return {"success": True}

        @staticmethod
        def drain_opc_wait_events():
            return [
                {
                    "phase": "finish",
                    "message": (
                        "OPC 变量等待完成 ready == True: "
                        "success=False, last_value=None, error=offline"
                    ),
                    "detail": {
                        "type": "opc_wait",
                        "phase": "finish",
                        "variable": "ready",
                        "expected": True,
                        "success": False,
                        "last_value": None,
                        "error": "offline",
                    },
                }
            ]

    device = FailedWaitDevice()
    events = []
    runtime_config = run_workflow_local.RuntimeConfig(
        path=Path("runtime.json"),
        device_factory=run_workflow_local.RuntimeDeviceFactoryConfig(),
        opc_snapshot=run_workflow_local.RuntimeOpcSnapshotConfig(),
    )

    _run_node_with_live_opc_sampling(
        WorkflowNode(
            uuid="failed-wait",
            name="run",
            device_name="device",
            method="run",
            param={},
            legacy_route_compatible=False,
        ),
        {"device": device},
        action_callable=device.run,
        logger=WorkflowLogger(
            writer=lambda message, **kwargs: events.append(
                {"message": message, **kwargs}
            )
        ),
        runtime_config=runtime_config,
    )

    failed_wait = next(
        event
        for event in events
        if (event.get("detail") or {}).get("type") == "opc_wait"
    )
    assert failed_wait["level"] == "error"
    assert failed_wait["detail"]["success"] is False


def test_run_node_with_live_opc_sampling_emits_nested_client_wait_events(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        """
        {
          "device_factory": {
            "target_device_id": "stirrer"
          },
          "opc_snapshot": {
            "action_variables": {
              "run_stirring": ["S041加工完成"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self):
            self.writer = None

        def set_opc_wait_event_writer(self, writer):
            self.writer = writer

        def emit_wait_start(self):
            assert self.writer is not None
            self.writer(
                {
                    "message": "等待 OPC 变量 S041加工完成 == True (interval=1.0s)",
                    "detail": {
                        "type": "opc_wait",
                        "phase": "start",
                        "variable": "S041加工完成",
                        "expected": True,
                        "node_id": "ns=4;s=S041加工完成",
                    },
                }
            )

    class FakeStirrer:
        def __init__(self):
            self._client = FakeClient()

        def get_variables(self, variable_names, use_cache=False):
            return {name: {"success": True, "value": False} for name in variable_names}

        def get_opc_variable_metadata(self, variable_name):
            return variable_name, f"ns=4;s={variable_name}"

        def run_stirring(self):
            self._client.emit_wait_start()
            assert any(
                event["detail"] and event["detail"].get("type") == "opc_wait"
                for event in events
            )
            return {"success": True}

    events = []

    def write_event(message, *, level="info", detail=None):
        events.append({"message": message, "level": level, "detail": detail})

    _run_node_with_live_opc_sampling(
        WorkflowNode(
            uuid="node_1", name="auto-run_stirring", device_name="stirrer", param={}
        ),
        {"stirrer": FakeStirrer()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    wait_events = [
        event
        for event in events
        if event["detail"] and event["detail"].get("type") == "opc_wait"
    ]
    assert [event["detail"]["variable"] for event in wait_events] == ["S041加工完成"]


def test_run_node_with_live_opc_sampling_skips_parallel_sampling_for_direct_device(
    tmp_path,
):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        """
        {
          "device_factory": {
            "devices": {
              "camera": "example.Camera"
            }
          },
          "opc_snapshot": {
            "action_variables": {
              "take_photo": ["S05加工完成", "S05拍照结果"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    class FakeCamera:
        def __init__(self):
            self.reading = False

        def get_variables(self, variable_names, use_cache=False):
            if self.reading:
                raise AssertionError("不应并发读取同一个 OPC 客户端")
            return {name: {"success": True, "value": 1} for name in variable_names}

        def take_photo(self):
            self.reading = True
            time.sleep(0.03)
            self.reading = False
            return {"success": True}

    events = []

    def write_event(message, *, level="info", detail=None):
        events.append({"message": message, "level": level, "detail": detail})

    results = _run_node_with_live_opc_sampling(
        WorkflowNode(
            uuid="node_1", name="auto-take_photo", device_name="camera", param={}
        ),
        {"camera": FakeCamera()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    assert results[0]["result"] == {"success": True}
    assert not [
        event for event in events if event["message"].startswith("OPC实时变化:")
    ]


def test_run_nodes_logs_opc_summary_with_detail_instead_of_full_snapshots():
    class FakePLC:
        def __init__(self):
            self.calls = 0
            self._name_mapping = {"Robot_Idle": "机械臂空闲"}
            self._variables_to_find = {"机械臂空闲": {"node_id": "ns=2;s=Robot_Idle"}}

        def get_variables(self, variable_names, use_cache=False):
            self.calls += 1
            value = self.calls == 1
            return {name: {"success": True, "value": value} for name in variable_names}

    class FakeRobotArm:
        def place_well_plate_to_pipetting_station(self):
            return {"success": True, "display_message": "S07 最终天平读数: 12.34"}

    events = []

    def write_event(message, *, level="info", detail=None):
        events.append({"message": message, "level": level, "detail": detail})

    node = WorkflowNode(
        uuid="node_1",
        name="auto-place_well_plate_to_pipetting_station",
        device_name="AI4C_robot_arm",
        param={},
    )

    run_nodes(
        [node],
        {"AI4C_plc": FakePLC(), "AI4C_robot_arm": FakeRobotArm()},
        logger=WorkflowLogger(writer=write_event),
    )

    messages = [event["message"] for event in events]
    assert not any(
        "OPC状态-before" in message or "OPC状态-after" in message
        for message in messages
    )
    assert any("OPC状态采样" in message for message in messages)
    display_event = next(event for event in events if event["message"] == "S07 最终天平读数: 12.34")
    assert display_event["detail"] is None
    diff_event = next(event for event in events if event["message"].startswith("OPC状态变化:"))
    assert diff_event["message"] == "OPC状态变化: 7/7 个变量变化"
    assert diff_event["detail"]["changes"][0] == {
        "name": "Robotic_Arm_Idle",
        "label": "Robotic_Arm_Idle",
        "display_name": "Robotic_Arm_Idle",
        "node_id": None,
        "before": {"success": True, "value": True},
        "after": {"success": True, "value": False},
    }


def test_stack_status_api_returns_live_plc_stack_status(monkeypatch):
    class FakePLC:
        def __init__(self):
            self.calls = []

        def get_stack_status(self, group_names=None):
            self.calls.append(group_names)
            return {
                "success": True,
                "schema": "szlab_poly_studio.stack_status.v1",
                "stacks": {
                    "s10_liquid_reagent": {
                        "id": "s10_liquid_reagent",
                        "display_name": "S10液体试剂瓶仓",
                        "warehouse_name": "S10液体试剂瓶仓占位",
                        "managed_resource": "reagent",
                        "content_type": ["liquid_reagent"],
                        "slots": {"1-1": {"site_key": "1-1", "occupied": True}},
                    }
                },
            }

    fake_plc = FakePLC()

    def fake_get_live_devices(self):
        return {"szlab_poly_plc": fake_plc}

    monkeypatch.setattr(WorkflowRunManager, "get_live_devices", fake_get_live_devices)

    app = create_app("stack_s05_s06")
    stack_status_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/stack-status"
    )
    response = asyncio.run(stack_status_endpoint())
    second_response = asyncio.run(stack_status_endpoint())

    payload = response
    assert payload["success"] is True
    assert payload["stacks"]["s10_liquid_reagent"]["slots"]["1-1"]["occupied"] is True
    assert second_response["success"] is True
    assert fake_plc.calls == [["s10_liquid_reagent", "powder_container"]]


def test_sensor_arrays_api_returns_live_plc_boolean_arrays(monkeypatch):
    class FakePLC:
        def __init__(self):
            self.calls = 0

        def get_sensor_arrays(self):
            self.calls += 1
            return {
                "success": True,
                "schema": "szlab_poly_studio.sensor_arrays.v1",
                "groups": [
                    {
                        "index": 2,
                        "name": "传感器状态_上位机[2].NO",
                        "values": [False] * 10 + [True] + [False] * 5,
                    }
                ],
            }

    fake_plc = FakePLC()

    def fake_get_live_devices(self):
        return {"szlab_poly_plc": fake_plc}

    monkeypatch.setattr(WorkflowRunManager, "get_live_devices", fake_get_live_devices)

    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/sensor-arrays"
    )
    response = asyncio.run(endpoint())
    second_response = asyncio.run(endpoint())

    assert response["success"] is True
    assert response["groups"][0]["values"][10] is True
    assert second_response["success"] is True
    assert fake_plc.calls == 1


def test_sensor_change_subscription_invalidates_stack_and_array_caches(monkeypatch):
    class FakePLC:
        def __init__(self):
            self.subscription_calls = 0
            self.callback = None

        def start_sensor_array_subscription(self, callback):
            self.subscription_calls += 1
            self.callback = callback

    fake_plc = FakePLC()
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    monkeypatch.setattr(
        manager, "get_live_devices", lambda: {"szlab_poly_plc": fake_plc}
    )

    manager._stack_status_cache = (time.monotonic(), {"success": True})
    manager._sensor_arrays_cache = (time.monotonic(), {"success": True})
    manager.ensure_sensor_event_subscription()
    manager.ensure_sensor_event_subscription()
    initial_version = manager.sensor_event_version()

    assert fake_plc.subscription_calls == 1
    assert fake_plc.callback is not None
    fake_plc.callback(3, [False] * 8 + [True] + [False] * 7)

    assert manager._stack_status_cache is None
    assert manager._sensor_arrays_cache is None
    assert (
        manager.wait_for_sensor_change(initial_version, timeout=0.01)
        == initial_version + 1
    )


def test_task_opc_connect_distributes_registry_without_reading_all_plc_variables(
    monkeypatch,
):
    class FakePLC:
        url = "opc.tcp://task-plc:4840"
        client = object()

        def registered_variables(self):
            return ["ready"]

        def get_variables(self, variables, use_cache=False):
            raise AssertionError("连接 Task OPC 时不得读取 PLC 变量")

    fake_plc = FakePLC()
    distributions = []

    def fake_connect(self, *, opcua_url, workflow_path):
        assert opcua_url == "opc.tcp://task-plc:4840"
        assert workflow_path == "task-flow.json"
        return {"szlab_poly_plc": fake_plc}

    def fake_publish(self, devices, *, workflow_path, read_snapshot=False):
        distributions.append((devices, workflow_path, read_snapshot))
        return {"distributed": True, "variable_count": 1}

    monkeypatch.setattr(WorkflowRunManager, "connect_task_opc", fake_connect)
    monkeypatch.setattr(
        WorkflowRunManager, "_publish_registered_plc_snapshot", fake_publish
    )
    app = create_app("stack_s05_s06")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-opc/connect"
    )

    response = asyncio.run(
        endpoint(
            {
                "url": "opc.tcp://task-plc:4840",
                "task_workspace_path": "task-flow.json",
                "task_workspace_version": 4,
            }
        )
    )

    assert response["success"] is True
    assert response["plc"] == {
        "device_id": "szlab_poly_plc",
        "url": "opc.tcp://task-plc:4840",
        "connected": True,
        "registered_variables": ["ready"],
        "variable_aliases": {},
    }
    assert response["task_orchestration"] == {"distributed": True, "variable_count": 1}
    assert distributions == [({"szlab_poly_plc": fake_plc}, "task-flow.json", False)]


def test_task_opc_connect_returns_clear_backend_failure(monkeypatch, tmp_path):
    def fail_connect(self, **_kwargs):
        raise ValueError("PLC 连接失败：认证被拒绝")

    monkeypatch.setattr(WorkflowRunManager, "connect_task_opc", fail_connect)
    app = create_app(
        "stack_s05_s06",
        run_history_store=RunHistoryStore(
            tmp_path, session_id="opc-connect-error"
        ),
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-opc/connect"
    )

    response = asyncio.run(
        endpoint(
            {
                "url": "opc.tcp://task-plc:4840",
                "task_workspace_path": "task-flow.json",
                "task_workspace_version": 4,
            }
        )
    )

    assert response == {
        "success": False,
        "message": "PLC 连接失败：认证被拒绝",
        "plc": {
            "device_id": "szlab_poly_plc",
            "connected": False,
            "registered_variables": [],
        },
    }
    get_logs = _route_endpoint(app, "/api/task-execution/logs", "GET")
    entries = asyncio.run(
        get_logs(task_workspace_path="task-flow.json")
    )["entries"]
    assert len(entries) == 1
    assert entries[0]["category"] == "opc"
    assert entries[0]["level"] == "error"
    assert entries[0]["code"] == "opc_connection_failed"
    assert entries[0]["phase"] == "connecting"


def test_task_opc_poll_delegates_to_manager(monkeypatch):
    def fake_poll(self, *, workflow_path):
        assert workflow_path == "task-flow.json"
        return {"success": True, "active": True, "variable_count": 2}

    monkeypatch.setattr(WorkflowRunManager, "poll_task_opc", fake_poll)
    app = create_app("stack_s05_s06")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-opc/poll"
    )

    assert asyncio.run(endpoint({"task_workspace_path": "task-flow.json"})) == {
        "success": True,
        "active": True,
        "variable_count": 2,
    }


def test_task_execution_tick_delegates_and_returns_cycle_statistics(monkeypatch):
    workflow = {
        "nodes": [
            {
                "uuid": "node_001_pick_from_s03",
                "name": "auto-submit_pick_from_s03",
                "device_name": "szlab_mixer_robot",
                "param": {},
            }
        ],
        "edges": [],
    }

    def fake_cycle(self, *, workflow_path, workflow_payload, harvest_only):
        assert workflow_path == "task-flow.json"
        assert workflow_payload is workflow
        assert harvest_only is False
        return {
            "success": True,
            "active": 1,
            "in_flight": 1,
            "claimed": 1,
            "completed": 0,
            "failed": 0,
        }

    monkeypatch.setattr(
        WorkflowRunManager,
        "run_task_execution_cycle",
        fake_cycle,
        raising=False,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/tick"
    )

    response = asyncio.run(
        endpoint(
            {
                "task_workspace_path": "task-flow.json",
                "workflow": workflow,
            }
        )
    )

    assert response == {
        "success": True,
        "active": 1,
        "in_flight": 1,
        "claimed": 1,
        "completed": 0,
        "failed": 0,
    }


def test_task_workflow_fingerprint_is_canonical_and_content_sensitive():
    first = {
        "nodes": [{
            "workflow_node_id": "task-node",
            "device_id": "device",
            "method": "run",
            "params": {"speed": 10},
        }],
        "edges": [],
    }
    reordered = {
        "edges": [],
        "nodes": [{
            "params": {"speed": 10},
            "method": "run",
            "device_id": "device",
            "workflow_node_id": "task-node",
        }],
    }
    changed = {
        **first,
        "nodes": [{**first["nodes"][0], "params": {"speed": 20}}],
    }

    fingerprint = workflow_ui._task_workflow_fingerprint(first)

    assert fingerprint.startswith("sha256:")
    assert fingerprint == workflow_ui._task_workflow_fingerprint(reordered)
    assert fingerprint != workflow_ui._task_workflow_fingerprint(changed)


def test_workflow_manager_preflight_returns_version_fingerprint_and_errors(
    tmp_path,
):
    class FakeDevice:
        def run(self):
            return None

    class FakeSnapshotPublisher:
        def __init__(self):
            self.response = {
                "version": 7,
                "workspace": {
                    "templates": [{
                        "id": "template-1",
                        "name": "样品处理",
                        "node_ids": ["task-node"],
                    }],
                    "scheduled_template_ids": ["template-1"],
                    "task_instances": [],
                    "pause_reason": None,
                },
            }

        def get_workspace(self, *, workflow_path):
            assert workflow_path == "task-flow.json"
            return self.response

    workflow = {
        "nodes": [{
            "workflow_node_id": "task-node",
            "device_id": "device",
            "method": "run",
            "params": {},
        }],
        "edges": [],
    }
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=RunHistoryStore(tmp_path, session_id="preflight"),
    )
    manager._task_snapshot_publisher = FakeSnapshotPublisher()
    manager._cached_devices = {"device": FakeDevice()}
    try:
        valid = manager.preflight_task_dispatch(
            workflow_path="task-flow.json",
            expected_version=7,
            workflow_payload=workflow,
        )
        missing = manager.preflight_task_dispatch(
            workflow_path="task-flow.json",
            expected_version=7,
            workflow_payload={"nodes": [], "edges": []},
        )
    finally:
        manager.shutdown()

    assert valid == {
        "valid": True,
        "errors": [],
        "warnings": [],
        "workspace_version": 7,
        "workflow_fingerprint": workflow_ui._task_workflow_fingerprint(workflow),
    }
    assert missing["valid"] is False
    assert missing["errors"][0]["code"] == "task_node_missing"
    assert missing["workspace_version"] == 7


def test_workflow_manager_preflight_rejects_stale_workspace_version(
    tmp_path,
):
    class FakeSnapshotPublisher:
        def get_workspace(self, *, workflow_path):
            assert workflow_path == "task-flow.json"
            return {"version": 8, "workspace": {}}

    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=RunHistoryStore(tmp_path, session_id="preflight-conflict"),
    )
    manager._task_snapshot_publisher = FakeSnapshotPublisher()
    try:
        with pytest.raises(TaskApiConflict) as caught:
            manager.preflight_task_dispatch(
                workflow_path="task-flow.json",
                expected_version=7,
                workflow_payload={"nodes": [], "edges": []},
            )
    finally:
        manager.shutdown()

    assert caught.value.code == "version_conflict"
    assert "expected=7, actual=8" in str(caught.value)


def test_workflow_manager_execution_cycle_hard_gates_invalid_preflight(
    tmp_path,
    monkeypatch,
):
    class FakeDevice:
        def run(self):
            return None

    class FakeSnapshotPublisher:
        def get_workspace(self, *, workflow_path):
            assert workflow_path == "task-flow.json"
            return {
                "version": 9,
                "workspace": {
                    "scheduler_paused": False,
                    "pause_reason": None,
                    "templates": [{
                        "id": "template-1",
                        "name": "样品处理",
                        "node_ids": ["task-node"],
                    }],
                    "scheduled_template_ids": ["template-1"],
                    "task_instances": [{
                        "id": "instance-1",
                        "template_id": "template-1",
                        "status": "running",
                    }],
                },
            }

    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=RunHistoryStore(tmp_path, session_id="hard-gate"),
    )
    manager._task_snapshot_publisher = FakeSnapshotPublisher()
    manager._cached_devices = {"device": FakeDevice()}
    cycle_calls = []
    published = []

    def fake_cycle(**kwargs):
        cycle_calls.append(kwargs)
        return {
            "success": True,
            "active": 1,
            "in_flight": 1,
            "claimed": 0,
            "completed": 1,
            "failed": 0,
            "diagnostics": [],
        }

    monkeypatch.setattr(manager._task_execution_coordinator, "cycle", fake_cycle)
    monkeypatch.setattr(
        manager,
        "_publish_task_scheduler_errors",
        lambda **kwargs: published.append(kwargs),
    )
    try:
        result = manager.run_task_execution_cycle(
            workflow_path="task-flow.json",
            workflow_payload={
                "nodes": [{
                    "workflow_node_id": "standalone-node",
                    "device_id": "device",
                    "method": "run",
                    "params": {},
                }],
                "edges": [],
            },
        )
    finally:
        manager.shutdown()

    assert result["success"] is False
    assert result["code"] == "task_dispatch_preflight_failed"
    assert result["claimed"] == 0
    assert result["completed"] == 1
    assert result["message"].startswith("派发预检未通过：")
    assert result["preflight"]["workspace_version"] == 9
    assert result["preflight"]["errors"][0]["code"] == "task_node_missing"
    assert result["diagnostics"][0]["instance_id"] == "instance-1"
    assert result["diagnostics"][0]["immediate"] is True
    assert cycle_calls[0]["harvest_only"] is True
    assert published[0]["diagnostics"] == result["diagnostics"]


def test_workflow_manager_execution_cycle_dispatches_after_server_preflight(
    tmp_path,
    monkeypatch,
):
    class FakeDevice:
        def run(self):
            return None

    class FakeSnapshotPublisher:
        def get_workspace(self, *, workflow_path):
            assert workflow_path == "task-flow.json"
            return {
                "version": 3,
                "workspace": {
                    "scheduler_paused": False,
                    "pause_reason": None,
                    "templates": [{
                        "id": "template-1",
                        "node_ids": ["task-node"],
                    }],
                    "scheduled_template_ids": ["template-1"],
                    "task_instances": [],
                },
            }

    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=RunHistoryStore(tmp_path, session_id="hard-gate-valid"),
    )
    manager._task_snapshot_publisher = FakeSnapshotPublisher()
    manager._cached_devices = {"device": FakeDevice()}
    cycle_calls = []

    def fake_cycle(**kwargs):
        cycle_calls.append(kwargs)
        return {
            "success": True,
            "active": 1,
            "in_flight": 0,
            "claimed": 1,
            "completed": 0,
            "failed": 0,
            "diagnostics": [],
        }

    monkeypatch.setattr(manager._task_execution_coordinator, "cycle", fake_cycle)
    monkeypatch.setattr(
        manager,
        "_publish_task_scheduler_errors",
        lambda **_kwargs: None,
    )
    try:
        result = manager.run_task_execution_cycle(
            workflow_path="task-flow.json",
            workflow_payload={
                "nodes": [{
                    "workflow_node_id": "task-node",
                    "device_id": "device",
                    "method": "run",
                    "params": {},
                }],
                "edges": [],
            },
        )
    finally:
        manager.shutdown()

    assert result["success"] is True
    assert result["claimed"] == 1
    assert cycle_calls[0]["harvest_only"] is False
    assert [node.uuid for node in cycle_calls[0]["workflow_nodes"]] == [
        "task-node"
    ]


def test_task_execution_preflight_endpoint_delegates_and_returns_result(
    monkeypatch,
):
    workflow = {"nodes": [], "edges": []}

    def fake_preflight(
        self, *, workflow_path, expected_version, workflow_payload
    ):
        assert workflow_path == "task-flow.json"
        assert expected_version == 7
        assert workflow_payload is workflow
        return {
            "valid": True,
            "errors": [],
            "warnings": [],
            "workspace_version": 7,
            "workflow_fingerprint": "sha256:valid",
        }

    monkeypatch.setattr(
        WorkflowRunManager,
        "preflight_task_dispatch",
        fake_preflight,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = _route_endpoint(app, "/api/task-execution/preflight", "POST")

    response = asyncio.run(endpoint({
        "task_workspace_path": "task-flow.json",
        "expected_version": 7,
        "workflow": workflow,
    }))

    assert response["valid"] is True
    assert response["workflow_fingerprint"] == "sha256:valid"


def test_task_execution_preflight_endpoint_returns_structured_422(monkeypatch):
    result = {
        "valid": False,
        "errors": [{"code": "task_node_missing", "node_id": "task-node"}],
        "warnings": [],
        "workspace_version": 7,
        "workflow_fingerprint": "sha256:invalid",
    }
    monkeypatch.setattr(
        WorkflowRunManager,
        "preflight_task_dispatch",
        lambda self, **kwargs: result,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = _route_endpoint(app, "/api/task-execution/preflight", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint({
            "task_workspace_path": "task-flow.json",
            "expected_version": 7,
            "workflow": {"nodes": [], "edges": []},
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail == result


def test_task_execution_preflight_endpoint_maps_version_conflict_to_409(
    monkeypatch,
):
    def conflict(self, **kwargs):
        raise TaskApiConflict("version_conflict", "workspace changed")

    monkeypatch.setattr(
        WorkflowRunManager,
        "preflight_task_dispatch",
        conflict,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = _route_endpoint(app, "/api/task-execution/preflight", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint({
            "task_workspace_path": "task-flow.json",
            "expected_version": 7,
            "workflow": {"nodes": [], "edges": []},
        }))

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "version_conflict",
        "message": "workspace changed",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_version": 7, "workflow": {}},
        {"task_workspace_path": "task-flow.json", "workflow": {}},
        {
            "task_workspace_path": "task-flow.json",
            "expected_version": True,
            "workflow": {},
        },
        {
            "task_workspace_path": "task-flow.json",
            "expected_version": 7,
        },
    ],
)
def test_task_execution_preflight_endpoint_rejects_incomplete_request(payload):
    app = create_app("szlab_robot_action_workflow")
    endpoint = _route_endpoint(app, "/api/task-execution/preflight", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint(payload))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "preflight_request_invalid"


def test_task_execution_timings_endpoint_persists_entries(monkeypatch):
    captured = {}

    def fake_append(self, *, workflow_path, entries):
        captured["workflow_path"] = workflow_path
        captured["entries"] = entries
        return len(entries)

    monkeypatch.setattr(
        WorkflowRunManager,
        "append_task_execution_timings",
        fake_append,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/timings"
    )
    entries = [
        {
            "cycle_id": 4,
            "step": "execution_tick",
            "phase": "finish",
            "timestamp_ms": 12_345,
            "duration_ms": 25,
        }
    ]

    response = asyncio.run(
        endpoint({"task_workspace_path": "task-flow.json", "entries": entries})
    )

    assert response == {"success": True, "written": 1}
    assert captured == {"workflow_path": "task-flow.json", "entries": entries}


def test_temporary_s072_triggers_follow_successful_place_and_pick_records():
    workspace = {
        "templates": [
            {
                "id": "inbound",
                "node_ids": [
                    "w01_pick_beaker_s03",
                    "w01_place_beaker_s072",
                ],
            },
            {"id": "dose", "node_ids": ["w01_dose_powder_s07"]},
            {
                "id": "outbound",
                "node_ids": [
                    "w02_pick_beaker_s072",
                    "w02_place_beaker_s06",
                ],
            },
        ],
        "task_instances": [],
    }

    assert _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-inbound",
        node_id="w01_pick_beaker_s03",
    )
    assert not _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-dose",
        node_id="w01_dose_powder_s07",
    )
    assert not _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-outbound",
        node_id="w02_pick_beaker_s072",
    )

    workspace["task_instances"] = [
        {
            "id": "sample-a-inbound",
            "template_id": "inbound",
            "execution_state": {
                "records": [
                    {
                        "node_id": "w01_place_beaker_s072",
                        "status": "succeeded",
                        "finished_at": 20,
                    }
                ]
            },
        }
    ]
    assert _temporary_s072_state(workspace).has_material is True
    assert not _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        node_id="w01_pick_beaker_s03",
    )
    assert _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-dose",
        node_id="w01_dose_powder_s07",
    )
    assert _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-outbound",
        node_id="w02_pick_beaker_s072",
    )

    workspace["task_instances"].append(
        {
            "id": "sample-a-outbound",
            "template_id": "outbound",
            "execution_state": {
                "records": [
                    {
                        "node_id": "w02_pick_beaker_s072",
                        "status": "succeeded",
                        "finished_at": 30,
                    }
                ]
            },
        }
    )
    assert _temporary_s072_state(workspace).has_material is False
    assert _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        node_id="w01_pick_beaker_s03",
    )


def test_temporary_s072_blocks_second_inbound_during_first_transport_group():
    workspace = {
        "templates": [
            {
                "id": "inbound",
                "node_ids": [
                    "w01_pick_beaker_s03",
                    "w01_place_beaker_s072",
                ],
            }
        ],
        "task_instances": [
            {
                "id": "sample-a-inbound",
                "template_id": "inbound",
                "execution_state": {
                    "records": [
                        {
                            "node_id": "w01_pick_beaker_s03",
                            "status": "succeeded",
                            "finished_at": 10,
                        }
                    ]
                },
            }
        ],
    }

    state = _temporary_s072_state(workspace)
    assert state.has_material is False
    assert state.inbound_instance_id == "sample-a-inbound"
    assert _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-a-inbound",
        node_id="w01_place_beaker_s072",
    )
    assert not _temporary_s072_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        node_id="w01_pick_beaker_s03",
    )


def test_temporary_s09_triggers_follow_successful_place_and_pick_records():
    workspace = {
        "templates": [
            {
                "id": "inbound",
                "node_ids": [
                    "w03_pick_beaker_s06",
                    "w03_place_beaker_s09",
                ],
            },
            {"id": "liquid", "node_ids": ["w03_add_liquid_s09"]},
            {
                "id": "outbound",
                "node_ids": [
                    "w04_pick_beaker_s09",
                    "w04_place_beaker_s04",
                ],
            },
        ],
        "task_instances": [],
    }

    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a-inbound",
        node_id="w03_pick_beaker_s06",
    )
    assert not _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a-liquid",
        node_id="w03_add_liquid_s09",
    )
    assert not _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a-outbound",
        node_id="w04_pick_beaker_s09",
    )

    workspace["task_instances"] = [
        {
            "id": "sample-a-inbound",
            "template_id": "inbound",
            "execution_state": {
                "records": [
                    {
                        "node_id": "w03_place_beaker_s09",
                        "status": "succeeded",
                        "finished_at": 20,
                    }
                ]
            },
        }
    ]
    assert _temporary_s09_state(workspace).has_material is True
    assert not _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        node_id="w03_pick_beaker_s06",
    )
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a-liquid",
        node_id="w03_add_liquid_s09",
    )
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a-outbound",
        node_id="w04_pick_beaker_s09",
    )

    workspace["task_instances"].append(
        {
            "id": "sample-a-outbound",
            "template_id": "outbound",
            "execution_state": {
                "records": [
                    {
                        "node_id": "w04_pick_beaker_s09",
                        "status": "succeeded",
                        "finished_at": 30,
                    }
                ]
            },
        }
    )
    assert _temporary_s09_state(workspace).has_material is False
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        node_id="w03_pick_beaker_s06",
    )


def test_task_execution_tick_passes_strict_harvest_only_to_manager(monkeypatch):
    calls = []

    def fake_cycle(self, *, workflow_path, workflow_payload, harvest_only):
        calls.append((workflow_path, workflow_payload, harvest_only))
        return {
            "success": True,
            "active": 0,
            "in_flight": 0,
            "claimed": 0,
            "completed": 1,
            "failed": 0,
        }

    monkeypatch.setattr(
        WorkflowRunManager,
        "run_task_execution_cycle",
        fake_cycle,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/tick"
    )
    response = asyncio.run(
        endpoint(
            {
                "task_workspace_path": "task-flow.json",
                "harvest_only": True,
            }
        )
    )

    assert response["completed"] == 1
    assert calls == [("task-flow.json", None, True)]


@pytest.mark.parametrize("invalid_value", [None, 0, 1, "true", [], {}])
def test_task_execution_tick_rejects_non_boolean_harvest_only(invalid_value):
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/tick"
    )

    response = asyncio.run(
        endpoint(
            {
                "task_workspace_path": "task-flow.json",
                "workflow": {},
                "harvest_only": invalid_value,
            }
        )
    )

    assert response["success"] is False
    assert response["message"] == "harvest_only 必须为 bool"
    assert response["claimed"] == 0


def test_task_execution_tick_runs_sync_cycle_off_event_loop(monkeypatch):
    workflow = {
        "nodes": [
            {
                "uuid": "node_003_dose_powder",
                "name": "auto-dose_powder",
                "device_name": "szlab_s07_solid_addition",
                "param": {},
            }
        ],
        "edges": [],
    }

    def slow_cycle(self, **_kwargs):
        time.sleep(0.08)
        return {
            "success": True,
            "active": 1,
            "in_flight": 0,
            "claimed": 0,
            "completed": 1,
            "failed": 0,
        }

    monkeypatch.setattr(
        WorkflowRunManager,
        "run_task_execution_cycle",
        slow_cycle,
    )
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/tick"
    )

    async def scenario():
        request = asyncio.create_task(
            endpoint(
                {
                    "task_workspace_path": "task-flow.json",
                    "workflow": workflow,
                }
            )
        )
        await asyncio.sleep(0.01)
        event_loop_progressed_while_cycle_running = not request.done()
        response = await request
        return event_loop_progressed_while_cycle_running, response

    progressed, response = asyncio.run(scenario())

    assert progressed is True
    assert response["completed"] == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"workflow": {}}, "缺少当前 workflow 路径"),
        ({"task_workspace_path": "task-flow.json"}, "缺少当前 workflow JSON"),
        (
            {"task_workspace_path": "task-flow.json", "workflow": []},
            "缺少当前 workflow JSON",
        ),
    ],
)
def test_task_execution_tick_rejects_invalid_payload_with_zero_statistics(
    payload, message
):
    app = create_app("szlab_robot_action_workflow")
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/task-execution/tick"
    )

    response = asyncio.run(endpoint(payload))

    assert response == {
        "success": False,
        "message": message,
        "active": 0,
        "in_flight": 0,
        "claimed": 0,
        "completed": 0,
        "failed": 0,
    }


def test_task_snapshot_reads_only_variables_referenced_by_template_conditions():
    class FakePLC:
        url = "opc.tcp://task-plc:4840"

        def registered_variables(self):
            return ["ready", "done", "unrelated"]

        def get_variables(self, variables, use_cache=False):
            assert variables == ["ready", "done"]
            assert use_cache is False
            return {
                "ready": {"success": True, "value": True},
                "done": {"success": True, "value": False},
            }

    class FakePublisher:
        def register(self, **_kwargs):
            return {
                "version": 7,
                "workspace": {
                    "templates": [
                        {
                            "input_triggers": [
                                {
                                    "kind": "opc",
                                    "config": {
                                        "plc_device_id": "szlab_poly_plc",
                                        "variable": "ready",
                                        "value": True,
                                    },
                                }
                            ],
                            "output_triggers": [
                                {
                                    "kind": "opc",
                                    "config": {
                                        "plc_device_id": "szlab_poly_plc",
                                        "variable": "done",
                                        "value": True,
                                    },
                                }
                            ],
                        }
                    ],
                },
            }

        def publish_snapshot(self, **kwargs):
            assert kwargs["expected_version"] == 7
            assert kwargs["values"] == {"ready": True, "done": False}

    manager = WorkflowRunManager(
        load_preset("stack_s05_s06"),
        _load_preset_runtime_config(load_preset("stack_s05_s06")),
    )
    manager._task_snapshot_publisher = FakePublisher()

    result = manager._publish_registered_plc_snapshot(
        {"szlab_poly_plc": FakePLC()},
        workflow_path="task-flow.json",
        read_snapshot=True,
    )

    assert result == {"distributed": True, "variable_count": 2}


def test_active_task_condition_variables_use_input_for_waiting_and_output_for_running():
    workspace = {
        "templates": [
            {
                "id": "prepare",
                "input_triggers": [
                    {
                        "kind": "opc",
                        "config": {
                            "plc_device_id": "szlab_poly_plc",
                            "variable": "ready",
                            "value": True,
                        },
                    }
                ],
                "output_triggers": [
                    {
                        "kind": "opc",
                        "config": {
                            "plc_device_id": "szlab_poly_plc",
                            "variable": "prepared",
                            "value": True,
                        },
                    }
                ],
            },
            {
                "id": "run",
                "input_triggers": [
                    {
                        "kind": "opc",
                        "config": {
                            "plc_device_id": "szlab_poly_plc",
                            "variable": "start",
                            "value": True,
                        },
                    }
                ],
                "output_triggers": [
                    {
                        "kind": "opc",
                        "config": {
                            "plc_device_id": "szlab_poly_plc",
                            "variable": "done",
                            "value": True,
                        },
                    }
                ],
            },
        ],
        "task_instances": [
            {"template_id": "prepare", "status": "waiting"},
            {"template_id": "run", "status": "running"},
            {"template_id": "run", "status": "completed"},
        ],
    }

    assert WorkflowRunManager._active_task_condition_variables(
        workspace,
        "szlab_poly_plc",
        ["ready", "prepared", "start", "done"],
        {},
    ) == ["ready", "done"]


def test_poll_task_opc_reads_active_condition_variables_and_publishes_snapshot():
    class FakePLC:
        url = "opc.tcp://test:4840"
        client = object()

        def __init__(self):
            self.read_requests = []

        @staticmethod
        def registered_variables():
            return ["ready", "done"]

        def get_variables(self, names, use_cache):
            self.read_requests.append((names, use_cache))
            values = {
                "ready": {"success": True, "value": True},
                "done": {"success": True, "value": False},
            }
            return {name: values[name] for name in names}

    class FakePublisher:
        def __init__(self):
            self.published = []

        @staticmethod
        def get_workspace(**_):
            return {
                "version": 7,
                "workspace": {
                    "templates": [
                        {
                            "id": "prepare",
                            "input_triggers": [
                                {
                                    "kind": "opc",
                                    "config": {
                                        "plc_device_id": "szlab_poly_plc",
                                        "variable": "ready",
                                        "value": True,
                                    },
                                }
                            ],
                            "output_triggers": [],
                        },
                        {
                            "id": "run",
                            "input_triggers": [],
                            "output_triggers": [
                                {
                                    "kind": "opc",
                                    "config": {
                                        "plc_device_id": "szlab_poly_plc",
                                        "variable": "done",
                                        "value": True,
                                    },
                                }
                            ],
                        },
                    ],
                    "task_instances": [
                        {"template_id": "prepare", "status": "waiting"},
                        {"template_id": "run", "status": "running"},
                    ],
                },
            }

        def publish_snapshot(self, **payload):
            self.published.append(payload)

    preset = load_preset("stack_s05_s06")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    plc = FakePLC()
    publisher = FakePublisher()
    manager._cached_devices = {"szlab_poly_plc": plc}
    manager._task_snapshot_publisher = publisher

    result = manager.poll_task_opc(workflow_path="task-flow.json")

    assert result == {"success": True, "active": True, "variable_count": 2}
    assert plc.read_requests == [(["ready", "done"], False)]
    assert publisher.published == [
        {
            "workflow_path": "task-flow.json",
            "expected_version": 7,
            "plc_device_id": "szlab_poly_plc",
            "values": {"ready": True, "done": False},
        }
    ]


@pytest.mark.parametrize("status", ["waiting", "running"])
def test_poll_task_opc_keeps_conditionless_nonterminal_task_active(status):
    class FakePLC:
        client = object()

        @staticmethod
        def registered_variables():
            return ["done"]

        @staticmethod
        def get_variables(*_args, **_kwargs):
            raise AssertionError("无输入条件时不得读取输出变量")

    class FakePublisher:
        @staticmethod
        def get_workspace(**_):
            return {
                "version": 3,
                "workspace": {
                    "templates": [{
                        "id": "task",
                        "input_triggers": [],
                        "output_triggers": [],
                    }],
                    "task_instances": [{
                        "template_id": "task",
                        "status": status,
                    }],
                },
            }

        @staticmethod
        def publish_snapshot(**_):
            raise AssertionError("无输入变量时不得发布快照")

    preset = load_preset("stack_s05_s06")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    manager._cached_devices = {"szlab_poly_plc": FakePLC()}
    manager._task_snapshot_publisher = FakePublisher()

    result = manager.poll_task_opc(workflow_path="task-flow.json")

    assert result == {"success": True, "active": True, "variable_count": 0}


def test_poll_task_opc_running_output_failure_is_diagnostic_not_blocking():
    class FakePLC:
        client = object()

        @staticmethod
        def registered_variables():
            return ["done"]

        @staticmethod
        def get_variables(names, use_cache):
            assert names == ["done"]
            assert use_cache is False
            return {"done": {"success": False, "error": "offline"}}

    class FakePublisher:
        @staticmethod
        def get_workspace(**_):
            return {
                "version": 3,
                "workspace": {
                    "templates": [{
                        "id": "task",
                        "input_triggers": [],
                        "output_triggers": [_opc_trigger("done", True)],
                    }],
                    "task_instances": [{
                        "template_id": "task",
                        "status": "running",
                    }],
                },
            }

        @staticmethod
        def publish_snapshot(**_):
            raise AssertionError("输出读取失败时不得发布空快照")

    preset = load_preset("stack_s05_s06")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    manager._cached_devices = {"szlab_poly_plc": FakePLC()}
    manager._task_snapshot_publisher = FakePublisher()

    result = manager.poll_task_opc(workflow_path="task-flow.json")

    assert result == {
        "success": True,
        "active": True,
        "variable_count": 0,
        "message": "当前 Task 输出状态变量均无法读取",
    }


def test_poll_task_opc_waiting_input_failure_still_blocks():
    class FakePLC:
        client = object()

        @staticmethod
        def registered_variables():
            return ["ready"]

        @staticmethod
        def get_variables(names, use_cache):
            assert names == ["ready"]
            assert use_cache is False
            return {"ready": {"success": False, "error": "offline"}}

    class FakePublisher:
        @staticmethod
        def get_workspace(**_):
            return {
                "version": 3,
                "workspace": {
                    "templates": [{
                        "id": "task",
                        "input_triggers": [_opc_trigger("ready", True)],
                        "output_triggers": [],
                    }],
                    "task_instances": [{
                        "template_id": "task",
                        "status": "waiting",
                    }],
                },
            }

        @staticmethod
        def publish_snapshot(**_):
            raise AssertionError("输入读取失败时不得发布空快照")

    preset = load_preset("stack_s05_s06")
    manager = WorkflowRunManager(preset, _load_preset_runtime_config(preset))
    manager._cached_devices = {"szlab_poly_plc": FakePLC()}
    manager._task_snapshot_publisher = FakePublisher()

    result = manager.poll_task_opc(workflow_path="task-flow.json")

    assert result == {
        "success": False,
        "active": True,
        "message": "当前 Task 条件变量均无法读取",
    }


def _opc_trigger(variable, value, *, kind="opc"):
    return {
        "kind": kind,
        "config": {
            "plc_device_id": "szlab_poly_plc",
            "variable": variable,
            "value": value,
        },
    }


def _opc_profile_generation_inputs():
    workflow = {
        "nodes": [
            {
                "uuid": "node-1",
                "name": "auto-first",
                "method": "first",
                "device_name": "device-1",
                "param": {"amount": 1},
            },
            {
                "uuid": "node-2",
                "name": "auto-second",
                "method": "second",
                "device_name": "device-2",
                "param": {"enabled": True},
            },
            {
                "uuid": "node-3",
                "name": "auto-third",
                "method": "third",
                "device_name": "device-3",
                "param": {},
            },
        ]
    }
    templates = [
        {
            "id": "template-a",
            "node_ids": ["node-1", "node-3"],
            "input_triggers": [_opc_trigger("ready", True)],
            "output_triggers": [_opc_trigger("finished", 3)],
        },
        {
            "id": "template-b",
            "node_ids": ["node-2", "node-1"],
            "input_triggers": [
                _opc_trigger("ready", True),
                _opc_trigger("ignored", {"not": "scalar"}, kind="manual"),
            ],
            "output_triggers": [
                _opc_trigger("done", False),
                _opc_trigger("done", False),
            ],
        },
        {
            "id": "not-scheduled",
            "node_ids": ["missing-node"],
            "input_triggers": [_opc_trigger("unused", "unused")],
            "output_triggers": [],
        },
    ]
    return workflow, templates


def test_generate_opc_simulator_draft_filters_orders_and_deduplicates():
    workflow, templates = _opc_profile_generation_inputs()

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-b", "template-a"],
        "Generated profile",
        "opc.tcp://localhost:4840",
    )

    profile = result["profile"]
    assert [node["workflow_node_id"] for node in profile["nodes"]] == [
        "node-2",
        "node-1",
        "node-3",
    ]
    assert profile["nodes"][1]["task_template_ids"] == [
        "template-b",
        "template-a",
    ]
    assert profile["nodes"][0] == {
        "workflow_node_id": "node-2",
        "task_template_ids": ["template-b"],
        "device_id": "device-2",
        "method": "second",
        "params": {"enabled": True},
        "channel": "",
        "trigger": {"all": []},
        "on_trigger": {"writes": []},
        "on_complete": {
            "delay": 0.5,
            "writes": [],
        },
        "reset_when": None,
        "after_reset": None,
    }
    assert profile["nodes"][1]["on_complete"]["writes"] == [
        {"variable": "done", "value": False}
    ]
    assert profile["nodes"][2]["on_complete"]["writes"] == [
        {"variable": "finished", "value": 3}
    ]
    assert profile["variables"] == [
        {
            "name": "ready",
            "direction": "plc_to_pc",
            "data_type": "bool",
            "initial_value": True,
            "source": "task_input",
        },
        {
            "name": "done",
            "direction": "plc_to_pc",
            "data_type": "bool",
            "source": "task_output",
        },
        {
            "name": "finished",
            "direction": "plc_to_pc",
            "data_type": "int",
            "source": "task_output",
        },
    ]
    assert "unused" not in {item["name"] for item in profile["variables"]}
    assert "ignored" not in {item["name"] for item in profile["variables"]}


def test_generate_draft_uses_scheduled_action_variables_after_task_conditions():
    workflow = {
        "nodes": [
            {
                "uuid": "scheduled-node",
                "device_id": "device-a",
                "method": "run",
                "params": {},
                "opc_variables": ["action-only", "shared", "action-only"],
            },
            {
                "uuid": "unscheduled-node",
                "device_id": "device-b",
                "method": "run",
                "params": {},
                "opc_variables": ["must-not-leak"],
            },
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["scheduled-node"],
            "input_triggers": [_opc_trigger("shared", True)],
            "output_triggers": [_opc_trigger("output", 2)],
        },
        {
            "id": "not-scheduled",
            "node_ids": ["unscheduled-node"],
            "input_triggers": [],
            "output_triggers": [],
        },
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Action variables",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=[
            {"name": "action-only", "data_type": "string"},
            {"name": "shared", "data_type": "int"},
        ],
    )

    assert result["profile"]["variables"] == [
        {
            "name": "shared",
            "direction": "plc_to_pc",
            "data_type": "bool",
            "initial_value": True,
            "source": "task_input",
        },
        {
            "name": "output",
            "direction": "plc_to_pc",
            "data_type": "int",
            "source": "task_output",
        },
        {
            "name": "action-only",
            "direction": "plc_to_pc",
            "data_type": "string",
            "source": "action_node",
        },
    ]
    assert "must-not-leak" not in {
        item["name"] for item in result["profile"]["variables"]
    }
    assert "variable_catalog[1].data_type" in result["validation_errors"]
    assert result["profile"]["nodes"][0]["on_complete"]["writes"] == [
        {"variable": "output", "value": 2}
    ]


def test_generate_opc_simulator_draft_includes_action_sensor_variables():
    from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
        product_slot_sensor,
    )

    sensor_variable = product_slot_sensor(1, "1-1", used=False)
    workflow = {
        "nodes": [
            {
                "uuid": "robot-pick",
                "device_id": "szlab_mixer_robot",
                "method": "submit_pick_from_s03",
                "params": {"product_type": 1, "position": "1-1"},
                "opc_variables": ["S03取放料产品", "S03取放料编号"],
            }
        ]
    }
    templates = [
        {
            "id": "robot-task",
            "node_ids": ["robot-pick"],
            "input_triggers": [_opc_trigger("S03取放料产品", 1)],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["robot-task"],
        "Robot sensors",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=[],
    )

    variable_by_name = {
        item["name"]: item for item in result["profile"]["variables"]
    }
    assert sensor_variable in variable_by_name
    assert variable_by_name[sensor_variable]["source"] == "action_sensor"
    assert variable_by_name[sensor_variable]["direction"] == "plc_to_pc"
    assert variable_by_name[sensor_variable]["data_type"] == "bool"


def test_generate_opc_simulator_draft_includes_robot_handshake_variables():
    workflow = {
        "nodes": [
            {
                "uuid": "robot-pick",
                "device_id": "szlab_mixer_robot",
                "method": "submit_pick_from_s03",
                "params": {"product_type": 1, "position": "1-1"},
                "opc_variables": ["S03取放料产品", "S03取放料编号", "任务号"],
            }
        ]
    }
    templates = [
        {
            "id": "robot-task",
            "node_ids": ["robot-pick"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["robot-task"],
        "Robot handshake",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=[],
    )

    variable_by_name = {
        item["name"]: item for item in result["profile"]["variables"]
    }
    assert variable_by_name["Robot_Home"] == {
        "name": "Robot_Home",
        "direction": "plc_to_pc",
        "data_type": "bool",
        "source": "manual",
        "initial_value": True,
    }
    assert variable_by_name["Robot_任务允许写入"]["source"] == "manual"
    assert variable_by_name["Robot_任务写入完成"]["source"] == "manual"
    assert variable_by_name["Robot_任务完成"] == {
        "name": "Robot_任务完成",
        "direction": "plc_to_pc",
        "data_type": "int",
        "source": "manual",
        "initial_value": 0,
    }
    assert variable_by_name["任务号"]["source"] == "action_node"
    assert variable_by_name["S03取放料产品"]["source"] == "action_node"


def test_generate_draft_does_not_validate_or_harvest_unscheduled_nodes():
    workflow = {
        "nodes": [
            {
                "uuid": "scheduled-node",
                "device_id": "device-a",
                "method": "run",
                "params": {},
                "opc_variables": ["scheduled-variable"],
            },
            {
                "uuid": "unscheduled-node",
                "resource_name": "S07",
                "name": "auto-must-not-be-parsed",
                "opc_variables": ["must-not-leak", ""],
            },
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["scheduled-node"],
            "input_triggers": [],
            "output_triggers": [],
        },
        {
            "id": "not-scheduled",
            "node_ids": ["unscheduled-node"],
            "input_triggers": [],
            "output_triggers": [],
        },
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Scheduled only",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=[
            {"name": "scheduled-variable", "data_type": "bool"},
        ],
    )

    assert [item["name"] for item in result["profile"]["variables"]] == [
        "scheduled-variable"
    ]
    assert all(
        not path.startswith("workflow.nodes[1]")
        for path in result["validation_errors"]
    )


def test_generate_draft_action_catalog_fallback_is_exact_and_missing_is_reported():
    workflow = {
        "nodes": [
            {
                "uuid": "fallback",
                "device_id": "device-a",
                "method": "run",
                "params": {},
            },
            {
                "uuid": "explicit-empty",
                "device_id": "device-a",
                "method": "run",
                "params": {},
                "opc_variables": [],
            },
            {
                "uuid": "missing",
                "device_id": "device-b",
                "method": "run",
                "params": {},
            },
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["fallback", "explicit-empty", "missing"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Catalog fallback",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "device-a",
                "method": "run",
                "opc_variables": ["catalog-variable"],
            }
        ],
        variable_catalog=[],
    )

    assert [item["name"] for item in result["profile"]["variables"]] == [
        "catalog-variable"
    ]
    assert result["profile"]["variables"][0]["data_type"] == "unknown"
    assert result["profile"]["variables"][0]["source"] == "action_node"
    assert "variables[0].data_type" in result["validation_errors"]
    assert "workflow.nodes[2].opc_variables" in result["validation_errors"]
    assert "workflow.nodes[1].opc_variables" not in result["validation_errors"]


def test_filter_variable_catalog_keeps_only_referenced_names():
    workflow = {
        "nodes": [
            {
                "uuid": "node-1",
                "device_id": "device-a",
                "method": "run",
                "params": {},
                "opc_variables": ["ready"],
            }
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["node-1"],
            "input_triggers": [
                {
                    "kind": "opc",
                    "config": {
                        "plc_device_id": "szlab_poly_plc",
                        "variable": "done",
                        "value": True,
                    },
                }
            ],
            "output_triggers": [],
        }
    ]
    filtered = opc_simulator_profiles.filter_variable_catalog(
        [
            {"name": "ready", "data_type": "bool"},
            {"name": "unused", "data_type": "bool"},
            {"name": "done", "data_type": "bool"},
        ],
        workflow=workflow,
        templates=templates,
        scheduled_template_ids=["scheduled"],
        action_catalog=[
            {
                "device_id": "device-a",
                "method": "run",
                "opc_variables": ["ready"],
            }
        ],
    )

    assert [item["name"] for item in filtered] == ["ready", "done"]


def test_generate_draft_accepts_large_variable_catalog_when_filtered():
    workflow = {
        "nodes": [
            {
                "uuid": "node-1",
                "device_id": "device-a",
                "method": "run",
                "params": {},
                "opc_variables": ["ready"],
            }
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["node-1"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]
    oversized_catalog = [
        {"name": f"var-{index}", "data_type": "bool"}
        for index in range(opc_simulator_profiles.MAX_VARIABLE_CATALOG_ITEMS + 1)
    ]
    oversized_catalog.append({"name": "ready", "data_type": "bool"})

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Filtered catalog",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "device-a",
                "method": "run",
                "opc_variables": ["ready"],
            }
        ],
        variable_catalog=oversized_catalog,
    )

    assert [item["name"] for item in result["profile"]["variables"]] == ["ready"]
    assert "variable_catalog" not in result["validation_errors"]


def test_generate_draft_rejects_ambiguous_action_catalog_without_name_guessing():
    workflow = {
        "nodes": [
            {
                "uuid": "node",
                "name": "auto-run",
                "device_id": "device-a",
                "method": "run",
                "params": {},
            }
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["node"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Ambiguous catalog",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "device-a",
                "method": "run",
                "opc_variables": ["first"],
            },
            {
                "device_id": "device-a",
                "method": "run",
                "opc_variables": ["second"],
            },
        ],
        variable_catalog=[],
    )

    assert result["profile"]["variables"] == []
    assert "action_catalog[1]" in result["validation_errors"]
    assert "workflow.nodes[0].opc_variables" in result["validation_errors"]


@pytest.mark.parametrize("legacy_field", ["resource_name", "device_name", "deviceId"])
def test_catalog_fallback_rejects_legacy_workflow_device_aliases(legacy_field):
    workflow = {
        "nodes": [
            {
                "uuid": "legacy-node",
                legacy_field: "legacy-device",
                "method": "run",
                "params": {},
            }
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["legacy-node"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Strict workflow catalog key",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "legacy-device",
                "method": "run",
                "opc_variables": ["must-not-match"],
            }
        ],
        variable_catalog=[],
    )

    assert result["profile"]["variables"] == []
    assert "workflow.nodes[0].opc_variables" in result["validation_errors"]
    assert "must-not-match" not in {
        item["name"] for item in result["profile"]["variables"]
    }


@pytest.mark.parametrize("alias", ["deviceId", "device_name", "resource_name"])
def test_action_catalog_rejects_device_id_alias_with_exact_paths(alias):
    workflow = {
        "nodes": [
            {
                "uuid": "node",
                "deviceId": "legacy-device",
                "method": "run",
                "params": {},
            }
        ]
    }
    templates = [
        {
            "id": "scheduled",
            "node_ids": ["node"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["scheduled"],
        "Strict catalog keys",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                alias: "legacy-device",
                "method": "run",
                "opc_variables": ["must-not-use"],
            }
        ],
        variable_catalog=[],
    )

    assert result["profile"]["variables"] == []
    assert f"action_catalog[0].{alias}" in result["validation_errors"]
    assert "action_catalog[0].device_id" in result["validation_errors"]
    assert "workflow.nodes[0].deviceId" not in result["validation_errors"]
    assert "workflow.nodes[0].opc_variables" in result["validation_errors"]


def test_action_catalog_rejects_method_alias_with_exact_paths():
    result = workflow_ui.generate_opc_simulator_draft(
        {"nodes": []},
        [],
        [],
        "Strict catalog method",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "device",
                "name": "run",
                "opc_variables": [],
            }
        ],
        variable_catalog=[],
    )

    assert "action_catalog[0].name" in result["validation_errors"]
    assert "action_catalog[0].method" in result["validation_errors"]


def test_generate_opc_simulator_draft_reports_conflicts_without_guessing():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["input_triggers"].extend(
        [
            _opc_trigger("ready", 1),
            _opc_trigger("same-type", 1),
            _opc_trigger("same-type", 2),
            _opc_trigger("", True),
            _opc_trigger("missing-value", True),
        ]
    )
    del templates[0]["input_triggers"][-1]["config"]["value"]
    templates[0]["output_triggers"].append(_opc_trigger("finished", 4))
    templates[0]["node_ids"].append("unknown-node")

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["unknown-template", "template-a"],
        "Conflicts",
        "opc.tcp://localhost:4840",
    )

    variables = {item["name"]: item for item in result["profile"]["variables"]}
    assert variables["ready"]["direction"] == "plc_to_pc"
    assert variables["ready"]["data_type"] == "bool"
    assert "initial_value" not in variables["ready"]
    assert "initial_value" not in variables["same-type"]
    assert result["profile"]["nodes"][-1]["on_complete"]["writes"] == []
    assert {
        "scheduled_template_ids[0]",
        "templates[0].node_ids[2]",
        "templates[0].input_triggers[1].config.value",
        "templates[0].input_triggers[3].config.value",
        "templates[0].input_triggers[4].config.variable",
        "templates[0].input_triggers[5].config.value",
        "templates[0].output_triggers[1].config.value",
    } <= set(result["validation_errors"])


def test_generate_opc_simulator_draft_parses_rule_action_workflow_nodes():
    workflow = {
        "rules": [
            {
                "actions": [
                    {
                        "action": {
                            "workflow_node_id": "node-1",
                            "device_id": "device-1",
                            "method": "run",
                            "params": {"amount": 2},
                        }
                    }
                ]
            }
        ]
    }
    templates = [
        {
            "id": "template-1",
            "node_ids": ["node-1"],
            "input_triggers": [_opc_trigger("ready", True)],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-1"],
        "Rules workflow",
        "opc.tcp://localhost:4840",
    )

    assert result["profile"]["nodes"][0]["workflow_node_id"] == "node-1"
    assert result["profile"]["nodes"][0]["device_id"] == "device-1"
    assert result["profile"]["nodes"][0]["method"] == "run"
    assert result["profile"]["nodes"][0]["params"] == {"amount": 2}


def test_generate_opc_simulator_draft_deduplicates_and_rejects_cross_template_writes():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["node_ids"] = ["node-1"]
    templates[0]["output_triggers"] = [_opc_trigger("done", False)]

    same = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-b", "template-a"],
        "Same writes",
        "opc.tcp://localhost:4840",
    )
    assert same["profile"]["nodes"][1]["on_complete"]["writes"] == [
        {"variable": "done", "value": False}
    ]

    templates[0]["output_triggers"] = [_opc_trigger("done", True)]
    conflict = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-b", "template-a"],
        "Conflicting writes",
        "opc.tcp://localhost:4840",
    )
    assert conflict["profile"]["nodes"][1]["on_complete"]["writes"] == []
    assert (
        "templates[0].output_triggers[0].config.value"
        in conflict["validation_errors"]
    )


def test_generate_opc_simulator_draft_rejects_duplicate_workflow_node_ids():
    workflow, templates = _opc_profile_generation_inputs()
    workflow["nodes"].append(
        {
            "uuid": "node-1",
            "name": "auto-ambiguous",
            "method": "ambiguous",
            "device_name": "other-device",
            "param": {},
        }
    )

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a"],
        "Duplicate nodes",
        "opc.tcp://localhost:4840",
    )

    assert "workflow.nodes[3].uuid" in result["validation_errors"]
    assert all(
        node["workflow_node_id"] != "node-1" for node in result["profile"]["nodes"]
    )


@pytest.mark.parametrize("invalid_kind", ["missing", "duplicate"])
def test_invalid_template_node_ids_disable_output_last_node_inference(invalid_kind):
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["node_ids"] = (
        ["node-1", "missing-node"]
        if invalid_kind == "missing"
        else ["node-1", "node-1"]
    )

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a"],
        "Invalid template",
        "opc.tcp://localhost:4840",
    )

    assert result["profile"]["nodes"][0]["on_complete"]["writes"] == []
    assert "templates[0].node_ids[1]" in result["validation_errors"]


def test_variable_type_conflict_removes_every_automatic_write_suggestion():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["input_triggers"] = [_opc_trigger("finished", True)]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a"],
        "Variable conflict",
        "opc.tcp://localhost:4840",
    )

    variable = next(
        item for item in result["profile"]["variables"] if item["name"] == "finished"
    )
    assert variable["data_type"] == "unknown"
    assert result["profile"]["nodes"][-1]["on_complete"]["writes"] == []


@pytest.mark.parametrize(
    ("catalog_type", "trigger_value", "compatible", "expected_type"),
    [
        ("float", 1, True, "float"),
        ("int", 1.5, False, "unknown"),
        ("string", True, False, "unknown"),
        ("bool", 1, False, "unknown"),
    ],
)
def test_variable_catalog_cross_validates_trigger_inference_and_writes(
    catalog_type,
    trigger_value,
    compatible,
    expected_type,
):
    workflow = {
        "nodes": [
            {
                "uuid": "node",
                "device_id": "device",
                "method": "run",
                "params": {},
                "opc_variables": ["shared"],
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["node"],
            "input_triggers": [],
            "output_triggers": [_opc_trigger("shared", trigger_value)],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template"],
        "Catalog cross validation",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=[{"name": "shared", "data_type": catalog_type}],
    )

    variable = result["profile"]["variables"][0]
    assert variable["data_type"] == expected_type
    if compatible:
        assert "variable_catalog[0].data_type" not in result["validation_errors"]
        assert result["profile"]["nodes"][0]["on_complete"]["writes"] == [
            {"variable": "shared", "value": trigger_value}
        ]
    else:
        assert "variable_catalog[0].data_type" in result["validation_errors"]
        assert result["profile"]["nodes"][0]["on_complete"]["writes"] == []


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (lambda workflow, templates, profile: templates.extend({} for _ in range(501)), "templates"),
        (
            lambda workflow, templates, profile: workflow["nodes"].extend(
                {"uuid": f"extra-{index}"} for index in range(501)
            ),
            "workflow.nodes",
        ),
        (
            lambda workflow, templates, profile: templates[0].update(
                node_ids=[f"node-{index}" for index in range(501)]
            ),
            "templates[0].node_ids",
        ),
        (
            lambda workflow, templates, profile: templates[0].update(
                input_triggers=[_opc_trigger(f"v-{index}", True) for index in range(1001)]
            ),
            "templates[0].input_triggers",
        ),
        (
            lambda workflow, templates, profile: profile.update(
                variables=[{} for _ in range(501)]
            ),
            "profile.variables",
        ),
        (
            lambda workflow, templates, profile: profile.update(
                nodes=[{} for _ in range(501)]
            ),
            "profile.nodes",
        ),
    ],
)
def test_opc_profile_limits_reject_oversized_collections(
    mutate, expected_path
):
    workflow, templates = _opc_profile_generation_inputs()
    profile = _valid_opc_simulator_profile("draft")
    mutate(workflow, templates, profile)

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(
            workflow=workflow,
            templates=templates,
            profile=profile,
        )

    assert caught.value.validation_errors == [expected_path]


@pytest.mark.parametrize(
    ("params", "expected_path"),
    [
        (
            {"nested": {"value": 1}},
            "workflow.nodes[0].params",
        ),
        (
            list(range(10001)),
            "workflow.nodes[0].params",
        ),
    ],
)
def test_opc_profile_limits_reject_deep_or_scalar_heavy_params(params, expected_path):
    workflow, templates = _opc_profile_generation_inputs()
    if isinstance(params, dict):
        value = 0
        for _ in range(21):
            value = {"nested": value}
        params = value
    workflow["nodes"][0]["param"] = params

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(
            workflow=workflow,
            templates=templates,
        )

    assert caught.value.validation_errors == [expected_path]


def test_opc_profile_limits_accept_maximum_sized_valid_collections():
    workflow = {
        "nodes": [
            {
                "uuid": f"node-{index}",
                "name": "auto-run",
                "device_name": "device",
                "param": {"index": index},
            }
            for index in range(500)
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": [f"node-{index}" for index in range(500)],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    opc_simulator_profiles.validate_payload_limits(
        workflow=workflow,
        templates=templates,
    )


def test_opc_profile_limits_count_rule_action_nodes_and_params():
    workflow = {
        "rules": [
            {
                "actions": [
                    {
                        "action": {
                            "workflow_node_id": f"node-{index}",
                            "params": {},
                        }
                    }
                    for index in range(501)
                ]
            }
        ]
    }

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(workflow=workflow)

    assert caught.value.validation_errors == ["workflow.nodes"]

    workflow["rules"][0]["actions"] = workflow["rules"][0]["actions"][:1]
    value = 0
    for _ in range(21):
        value = {"nested": value}
    workflow["rules"][0]["actions"][0]["action"]["params"] = value

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(workflow=workflow)

    assert caught.value.validation_errors == [
        "workflow.rules[0].actions[0].action.params"
    ]


def test_opc_profile_limits_rule_action_opc_variables():
    workflow = {
        "rules": [
            {
                "actions": [
                    {
                        "action": {
                            "workflow_node_id": "node",
                            "device_id": "device",
                            "method": "run",
                            "opc_variables": [
                                f"variable-{index}" for index in range(501)
                            ],
                        }
                    }
                ]
            }
        ]
    }

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(workflow=workflow)

    assert caught.value.validation_errors == [
        "workflow.rules[0].actions[0].action.opc_variables"
    ]


def test_opc_profile_limits_total_declared_opc_variables_before_deduplication():
    repeated = ["same-variable"] * 500
    action_catalog = [
        {
            "device_id": f"device-{index}",
            "method": "run",
            "opc_variables": repeated,
        }
        for index in range(11)
    ]
    workflow = {
        "nodes": [
            {
                "uuid": f"node-{index}",
                "device_id": f"device-{index}",
                "method": "run",
                "opc_variables": repeated,
            }
            for index in range(11)
        ]
    }

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as action_error:
        opc_simulator_profiles.validate_payload_limits(
            action_catalog=action_catalog
        )
    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as node_error:
        opc_simulator_profiles.validate_payload_limits(workflow=workflow)

    assert action_error.value.validation_errors == [
        "action_catalog.opc_variables"
    ]
    assert node_error.value.validation_errors == ["workflow.opc_variables"]


def test_generate_rejects_1_9mb_template_node_amplification_before_node_mapping(
    monkeypatch,
):
    node_ids = [f"node-{index:04d}-" + ("x" * 170) for index in range(500)]
    templates = [
        {
            "id": f"template-{index}",
            "node_ids": node_ids,
            "input_triggers": [],
            "output_triggers": [],
        }
        for index in range(21)
    ]
    payload_size = len(
        json.dumps(
            {"templates": templates},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    assert 1_800_000 < payload_size < opc_simulator_profiles.MAX_JSON_BYTES
    monkeypatch.setattr(
        opc_simulator_profiles,
        "_workflow_node_map",
        lambda *_args, **_kwargs: pytest.fail(
            "超过关联上限时不得构造 node map 或关联列表"
        ),
    )

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        workflow_ui.generate_opc_simulator_draft(
            {"nodes": []},
            templates,
            [template["id"] for template in templates],
            "Amplification",
            "opc.tcp://localhost:4840",
            action_catalog=[],
            variable_catalog=[],
        )

    assert caught.value.validation_errors == ["template_node_associations"]


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        ("template_id", "templates[0].id"),
        ("node_id", "templates[0].node_ids[0]"),
        ("workflow_node_id", "workflow.nodes[0].uuid"),
        ("device_id", "workflow.nodes[0].device_id"),
        ("method", "workflow.nodes[0].method"),
        ("trigger_variable", "templates[0].input_triggers[0].config.variable"),
        ("action_variable", "workflow.nodes[0].opc_variables[0]"),
        ("catalog_variable", "variable_catalog[0].name"),
    ],
)
def test_generate_rejects_overlong_identifiers_and_variable_names(
    mutation,
    expected_path,
):
    workflow = {
        "nodes": [
            {
                "uuid": "node",
                "device_id": "device",
                "method": "run",
                "params": {},
                "opc_variables": ["action-variable"],
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["node"],
            "input_triggers": [_opc_trigger("trigger-variable", True)],
            "output_triggers": [],
        }
    ]
    variable_catalog = [{"name": "action-variable", "data_type": "bool"}]
    if mutation == "template_id":
        templates[0]["id"] = "x" * 257
    elif mutation == "node_id":
        templates[0]["node_ids"][0] = "x" * 257
    elif mutation == "workflow_node_id":
        workflow["nodes"][0]["uuid"] = "x" * 257
    elif mutation in {"device_id", "method"}:
        workflow["nodes"][0][mutation] = "x" * 257
    elif mutation == "trigger_variable":
        templates[0]["input_triggers"][0]["config"]["variable"] = "x" * 513
    elif mutation == "action_variable":
        workflow["nodes"][0]["opc_variables"][0] = "x" * 513
    else:
        variable_catalog[0]["name"] = "x" * 513

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        [templates[0]["id"]],
        "String limits",
        "opc.tcp://localhost:4840",
        action_catalog=[],
        variable_catalog=variable_catalog,
    )

    assert expected_path in result["validation_errors"]


def test_generate_rejects_canonical_profile_over_2_mib():
    workflow = {
        "nodes": [
            {
                "uuid": "node",
                "device_id": "device",
                "method": "run",
                "params": {"blob": "x" * opc_simulator_profiles.MAX_JSON_BYTES},
                "opc_variables": [],
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["node"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        workflow_ui.generate_opc_simulator_draft(
            workflow,
            templates,
            ["template"],
            "Oversized output",
            "opc.tcp://localhost:4840",
            action_catalog=[],
            variable_catalog=[],
        )

    assert caught.value.validation_errors == ["generated_profile"]


def test_opc_profile_limits_count_combined_template_triggers():
    _, templates = _opc_profile_generation_inputs()
    templates[0]["input_triggers"] = [
        _opc_trigger(f"input-{index}", True) for index in range(600)
    ]
    templates[0]["output_triggers"] = [
        _opc_trigger(f"output-{index}", True) for index in range(401)
    ]

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.validate_payload_limits(templates=templates)

    assert caught.value.validation_errors == ["templates[0].triggers"]


def test_generate_opc_simulator_draft_limits_unique_variables_to_500():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["input_triggers"] = [
        _opc_trigger(f"variable-{index}", True) for index in range(501)
    ]

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        workflow_ui.generate_opc_simulator_draft(
            workflow,
            templates,
            ["template-a"],
            "Too many variables",
            "opc.tcp://localhost:4840",
        )

    assert caught.value.validation_errors == ["profile.variables"]


def test_different_nodes_may_write_different_values_to_same_output_variable():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["node_ids"] = ["node-3"]
    templates[0]["output_triggers"] = [_opc_trigger("shared-output", True)]
    templates[1]["node_ids"] = ["node-2"]
    templates[1]["output_triggers"] = [_opc_trigger("shared-output", False)]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a", "template-b"],
        "Independent lifecycle writes",
        "opc.tcp://localhost:4840",
    )

    nodes = {
        node["workflow_node_id"]: node for node in result["profile"]["nodes"]
    }
    assert nodes["node-3"]["on_complete"]["writes"] == [
        {"variable": "shared-output", "value": True}
    ]
    assert nodes["node-2"]["on_complete"]["writes"] == [
        {"variable": "shared-output", "value": False}
    ]
    variable = next(
        item
        for item in result["profile"]["variables"]
        if item["name"] == "shared-output"
    )
    assert variable["data_type"] == "bool"


def test_scheduled_template_ids_are_first_seen_deduplicated():
    workflow, templates = _opc_profile_generation_inputs()

    once = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a"],
        "Deduplicated schedule",
        "opc.tcp://localhost:4840",
    )
    repeated = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a", "template-a", "template-a"],
        "Deduplicated schedule",
        "opc.tcp://localhost:4840",
    )

    assert repeated == once
    assert all(
        node["task_template_ids"] == ["template-a"]
        for node in repeated["profile"]["nodes"]
    )


@pytest.mark.parametrize(
    ("scheduled_ids", "expected_path"),
    [
        (["template-a"] * 1001, "scheduled_template_ids"),
        ([f"template-{index}" for index in range(501)], "scheduled_template_ids.unique"),
    ],
)
def test_scheduled_template_id_limits(scheduled_ids, expected_path):
    workflow, templates = _opc_profile_generation_inputs()

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        workflow_ui.generate_opc_simulator_draft(
            workflow,
            templates,
            scheduled_ids,
            "Schedule limits",
            "opc.tcp://localhost:4840",
        )

    assert caught.value.validation_errors == [expected_path]


def test_expanded_trigger_observations_are_limited_before_collection():
    workflow, _ = _opc_profile_generation_inputs()
    templates = [
        {
            "id": f"template-{index}",
            "node_ids": ["node-1"],
            "input_triggers": [
                _opc_trigger(f"variable-{index}-{trigger}", True)
                for trigger in range(11)
            ],
            "output_triggers": [],
        }
        for index in range(455)
    ]

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        workflow_ui.generate_opc_simulator_draft(
            workflow,
            templates,
            [template["id"] for template in templates],
            "Observation limits",
            "opc.tcp://localhost:4840",
        )

    assert caught.value.validation_errors == ["trigger_observations"]


def test_duplicate_template_ids_are_ambiguous_and_disable_inference():
    workflow, templates = _opc_profile_generation_inputs()
    templates.append(
        {
            **templates[0],
            "node_ids": ["node-2"],
        }
    )

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template-a"],
        "Ambiguous templates",
        "opc.tcp://localhost:4840",
    )

    assert "templates[3].id" in result["validation_errors"]
    assert result["profile"]["nodes"] == []
    assert result["profile"]["variables"] == []


def test_workflow_node_does_not_parse_method_from_display_name():
    workflow = {
        "nodes": [
            {
                "id": "node-alias",
                "deviceId": "device-alias",
                "name": "auto-run",
                "param": {},
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["node-alias"],
            "input_triggers": [_opc_trigger("ready", True)],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template"],
        "Strict aliases",
        "opc.tcp://localhost:4840",
    )

    assert result["profile"]["nodes"] == []
    assert "workflow.nodes[0].method" in result["validation_errors"]


def test_plain_workflow_node_id_alias_precedes_uuid_and_id():
    workflow = {
        "nodes": [
            {
                "workflow_node_id": "preferred",
                "uuid": "secondary",
                "id": "tertiary",
                "device_id": "device",
                "method": "run",
                "params": {},
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["preferred"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template"],
        "Alias precedence",
        "opc.tcp://localhost:4840",
    )

    assert result["profile"]["nodes"][0]["workflow_node_id"] == "preferred"


def test_plain_workflow_invalid_selected_id_reports_actual_alias_path():
    workflow = {
        "nodes": [
            {
                "workflow_node_id": {"invalid": True},
                "uuid": "fallback-must-not-hide-error",
                "device_id": "device",
                "method": "run",
            }
        ]
    }
    templates = [
        {
            "id": "template",
            "node_ids": ["fallback-must-not-hide-error"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        ["template"],
        "Invalid selected alias",
        "opc.tcp://localhost:4840",
    )

    assert "workflow.nodes[0].workflow_node_id" in result["validation_errors"]
    assert result["profile"]["nodes"] == []


@pytest.mark.parametrize(
    ("node", "expected_path"),
    [
        (
            {"uuid": 1, "device_id": "device", "method": "run"},
            "workflow.nodes[0].uuid",
        ),
        (
            {"id": [], "device_id": "device", "method": "run"},
            "workflow.nodes[0].id",
        ),
        (
            {"uuid": "node", "device_id": {}, "method": "run"},
            "workflow.nodes[0].device_id",
        ),
        (
            {"uuid": "node", "deviceId": False, "method": "run"},
            "workflow.nodes[0].deviceId",
        ),
        (
            {"uuid": "node", "device_id": "device", "method": None},
            "workflow.nodes[0].method",
        ),
        (
            {"uuid": "node", "device_id": "device", "name": ["run"]},
            "workflow.nodes[0].method",
        ),
        (
            {"uuid": " ", "device_id": "device", "method": "run"},
            "workflow.nodes[0].uuid",
        ),
    ],
)
def test_workflow_node_fields_reject_non_string_or_blank_values(
    node, expected_path
):
    templates = [
        {
            "id": "template",
            "node_ids": ["node"],
            "input_triggers": [],
            "output_triggers": [],
        }
    ]

    result = workflow_ui.generate_opc_simulator_draft(
        {"nodes": [node]},
        templates,
        ["template"],
        "Invalid node",
        "opc.tcp://localhost:4840",
    )

    assert expected_path in result["validation_errors"]
    assert result["profile"]["nodes"] == []


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        ("template_id", "templates[0].id"),
        ("node_id", "templates[0].node_ids[0]"),
        (
            "trigger_variable",
            "templates[0].input_triggers[0].config.variable",
        ),
    ],
)
def test_template_and_trigger_identifiers_require_original_nonempty_strings(
    mutation, expected_path
):
    workflow, templates = _opc_profile_generation_inputs()
    if mutation == "template_id":
        templates[0]["id"] = {"not": "text"}
        scheduled = ["template-a"]
    elif mutation == "node_id":
        templates[0]["node_ids"][0] = 1
        scheduled = ["template-a"]
    else:
        templates[0]["input_triggers"][0]["config"]["variable"] = True
        scheduled = ["template-a"]

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        scheduled,
        "Strict identifiers",
        "opc.tcp://localhost:4840",
    )

    assert expected_path in result["validation_errors"]


def test_template_node_ids_and_trigger_variables_are_trimmed_after_validation():
    workflow, templates = _opc_profile_generation_inputs()
    templates[0]["id"] = " template-a "
    templates[0]["node_ids"] = [" node-1 "]
    templates[0]["input_triggers"] = [_opc_trigger(" ready ", True)]
    templates[0]["output_triggers"] = []

    result = workflow_ui.generate_opc_simulator_draft(
        workflow,
        templates,
        [" template-a "],
        "Trimmed identifiers",
        "opc.tcp://localhost:4840",
    )

    assert result["profile"]["nodes"][0]["workflow_node_id"] == "node-1"
    assert result["profile"]["nodes"][0]["task_template_ids"] == ["template-a"]
    assert result["profile"]["variables"][0]["name"] == "ready"


def _valid_opc_simulator_profile(status="runnable"):
    return {
        "schema_version": 2,
        "status": status,
        "name": "valid-profile",
        "opc": {
            "url": "opc.tcp://localhost:4840",
            "poll_interval": 0.2,
            "io_timeout": 2.0,
        },
        "variables": [
            {
                "name": "command",
                "direction": "pc_to_plc",
                "data_type": "bool",
                "source": "task_input",
            },
            {
                "name": "done",
                "direction": "plc_to_pc",
                "data_type": "bool",
                "initial_value": False,
                "source": "task_output",
            },
        ],
        "nodes": [
            {
                "workflow_node_id": "node-1",
                "task_template_ids": ["template-1"],
                "device_id": "device-1",
                "method": "run",
                "params": {},
                "channel": "device-1",
                "trigger": {
                    "all": [
                        {
                            "variable": "command",
                            "operator": "eq",
                            "value": True,
                            "edge": "rising",
                        }
                    ]
                },
                "on_trigger": {"writes": [{"variable": "done", "value": False}]},
                "on_complete": {
                    "delay": 0.5,
                    "writes": [{"variable": "done", "value": True}],
                },
                "reset_when": None,
                "after_reset": None,
            }
        ],
    }


@pytest.mark.parametrize(
    "file_name",
    [
        "",
        ".hidden.json",
        "profile",
        "profile.JSON",
        "profile.json.json",
        "../profile.json",
        "/tmp/profile.json",
        r"nested\profile.json",
        "nested/profile.json",
        "中文.json",
        "profile\x00.json",
    ],
)
def test_opc_simulator_profile_path_rejects_unsafe_file_names(tmp_path, file_name):
    with pytest.raises(ValueError, match="file_name"):
        opc_simulator_profiles.resolve_profile_path(file_name, tmp_path)


def test_opc_simulator_profile_save_rejects_symlink_and_nonregular_target(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "linked.json").symlink_to(outside)
    (tmp_path / "directory.json").mkdir()

    with pytest.raises(ValueError, match="符号链接"):
        opc_simulator_profiles.save_profile(
            "linked.json", _valid_opc_simulator_profile("draft"), tmp_path
        )
    with pytest.raises(ValueError, match="普通文件"):
        opc_simulator_profiles.save_profile(
            "directory.json", _valid_opc_simulator_profile("draft"), tmp_path
        )
    assert outside.read_text(encoding="utf-8") == "outside"


def test_opc_simulator_profile_roundtrip_is_pretty_utf8_and_atomic(tmp_path):
    profile = _valid_opc_simulator_profile()

    saved = opc_simulator_profiles.save_profile("valid-profile.json", profile, tmp_path)
    content = (tmp_path / "valid-profile.json").read_text(encoding="utf-8")

    assert saved["profile"] == profile
    assert saved["validation_errors"] == []
    assert saved["revision"] == hashlib.sha256(
        (tmp_path / "valid-profile.json").read_bytes()
    ).hexdigest()
    assert content.endswith("\n")
    assert "\n  \"schema_version\"" in content
    assert opc_simulator_profiles.read_profile(
        "valid-profile.json", tmp_path
    ) == saved


def _profile_with_request_under_limit_but_pretty_over_limit():
    profile = _valid_opc_simulator_profile("draft")
    profile["nodes"][0]["params"] = {"blob": ""}
    request_base = len(
        json.dumps({"profile": profile}, ensure_ascii=False).encode("utf-8")
    )
    pretty_base = len(opc_simulator_profiles._serialized_profile_bytes(profile))
    minimum_blob = opc_simulator_profiles.MAX_JSON_BYTES - pretty_base + 1
    maximum_blob = opc_simulator_profiles.MAX_JSON_BYTES - request_base - 1
    assert 0 < minimum_blob <= maximum_blob
    profile["nodes"][0]["params"]["blob"] = "x" * (
        (minimum_blob + maximum_blob) // 2
    )
    request_bytes = json.dumps(
        {"profile": profile}, ensure_ascii=False
    ).encode("utf-8")
    pretty_bytes = opc_simulator_profiles._serialized_profile_bytes(profile)
    assert len(request_bytes) < opc_simulator_profiles.MAX_JSON_BYTES
    assert len(pretty_bytes) > opc_simulator_profiles.MAX_JSON_BYTES
    return profile


def test_save_profile_rejects_pretty_oversize_before_any_storage_side_effect(
    tmp_path,
    monkeypatch,
):
    profile = _profile_with_request_under_limit_but_pretty_over_limit()
    monkeypatch.setattr(
        opc_simulator_profiles,
        "_open_profile_process_lock",
        lambda *_args: pytest.fail("超限 profile 不得创建锁或临时文件"),
    )

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.save_profile("oversized.json", profile, tmp_path)

    assert caught.value.validation_errors == ["profile"]
    assert list(tmp_path.iterdir()) == []


def test_save_profile_pretty_oversize_preserves_existing_bytes_and_revision(
    tmp_path,
):
    original = _valid_opc_simulator_profile("draft")
    created = opc_simulator_profiles.save_profile(
        "existing.json",
        original,
        tmp_path,
    )
    path = tmp_path / "existing.json"
    original_bytes = path.read_bytes()
    oversized = _profile_with_request_under_limit_but_pretty_over_limit()

    with pytest.raises(opc_simulator_profiles.ProfileLimitError) as caught:
        opc_simulator_profiles.save_profile(
            "existing.json",
            oversized,
            tmp_path,
            expected_revision=created["revision"],
        )

    assert caught.value.validation_errors == ["profile"]
    assert path.read_bytes() == original_bytes
    assert opc_simulator_profiles.read_profile(
        "existing.json", tmp_path
    )["revision"] == created["revision"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_opc_simulator_profile_atomic_replace_failure_preserves_old_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "atomic.json"
    old_profile = _valid_opc_simulator_profile("draft")
    created = opc_simulator_profiles.save_profile("atomic.json", old_profile, tmp_path)
    old_content = path.read_bytes()
    replacement = _valid_opc_simulator_profile("draft")
    replacement["name"] = "replacement"

    def fail_replace(_source, _target, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(opc_simulator_profiles.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        opc_simulator_profiles.save_profile(
            "atomic.json",
            replacement,
            tmp_path,
            expected_revision=created["revision"],
        )

    assert path.read_bytes() == old_content
    assert list(tmp_path.glob(".atomic.json.*.tmp")) == []


def test_opc_simulator_profile_read_uses_single_nofollow_fd(tmp_path, monkeypatch):
    profile = _valid_opc_simulator_profile("draft")
    saved = opc_simulator_profiles.save_profile("secure.json", profile, tmp_path)

    def forbid_path_read(*_args, **_kwargs):
        raise AssertionError("不得使用 Path.read_text")

    monkeypatch.setattr(Path, "read_text", forbid_path_read)

    assert opc_simulator_profiles.read_profile("secure.json", tmp_path) == saved


def test_reference_opc_profile_in_scripts_config_has_expected_permissions():
    profile_path = (
        workflow_ui.OPC_SIMULATOR_REFERENCE_DIR
        / workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE
    )
    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o644


def test_seed_opc_config_dir_copies_reference_template(tmp_path):
    legacy_dir = tmp_path / "legacy"
    config_dir = tmp_path / "configs"
    legacy_dir.mkdir()
    reference = legacy_dir / workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE
    reference.write_bytes(
        (
            workflow_ui.OPC_SIMULATOR_REFERENCE_DIR
            / workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE
        ).read_bytes()
    )

    migrated = opc_simulator_profiles.migrate_legacy_opc_profiles(
        config_dir,
        legacy_dir,
        reference_profile=workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE,
    )

    assert migrated == [workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE]
    assert (config_dir / workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE).is_file()
    # 已有文件时不覆盖
    assert (
        opc_simulator_profiles.migrate_legacy_opc_profiles(
            config_dir,
            legacy_dir,
            reference_profile=workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE,
        )
        == []
    )


def test_list_opc_simulator_profiles_returns_saved_files(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(workflow_ui, "migrate_legacy_opc_profiles", lambda *_args, **_kwargs: [])
    (tmp_path / "alpha-opc-simulator.json").write_text(
        '{"schema_version":2,"status":"draft","name":"a","opc":{"url":"opc.tcp://127.0.0.1:4840","poll_interval":0.2,"io_timeout":2},"variables":[],"nodes":[]}',
        encoding="utf-8",
    )
    app = create_app("szlab_robot_action_workflow")
    list_profiles = _route_endpoint(app, "/api/opc-simulator/profiles", "GET")

    response = asyncio.run(list_profiles())

    assert response["files"] == ["alpha-opc-simulator.json"]
    assert response["config_dir"]


def test_saved_opc_profile_is_readable_via_get_api(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(workflow_ui, "migrate_legacy_opc_profiles", lambda *_args, **_kwargs: [])
    file_name = "saved-opc-simulator.json"
    payload = {
        "schema_version": 2,
        "status": "draft",
        "name": "Saved",
        "opc": {
            "url": "opc.tcp://127.0.0.1:4840",
            "poll_interval": 0.2,
            "io_timeout": 2.0,
        },
        "variables": [],
        "nodes": [],
    }
    opc_simulator_profiles.save_profile(file_name, payload, tmp_path)
    app = create_app("szlab_robot_action_workflow")
    read = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "GET"
    )

    response = asyncio.run(read(file_name))

    assert response["file_name"] == file_name
    assert response["profile"]["status"] == "draft"
    assert len(response["revision"]) == 64


def test_opc_simulator_reference_template_api_returns_runnable_example():
    app = create_app("szlab_robot_action_workflow")
    read = _route_endpoint(
        app, "/api/opc-simulator/profiles/reference/template", "GET"
    )

    response = asyncio.run(read())

    assert response["file_name"] == workflow_ui.OPC_SIMULATOR_REFERENCE_PROFILE
    assert response["profile"]["status"] == "runnable"
    assert len(response["profile"]["nodes"]) == 6
    assert response["profile"]["nodes"][0]["channel"] == "robot"


def test_opc_simulator_profile_spec_api_returns_markdown():
    app = create_app("szlab_robot_action_workflow")
    read = _route_endpoint(app, "/api/opc-simulator/profile-spec", "GET")

    response = asyncio.run(read())

    assert response["schema_version"] == 2
    assert "opc_simulator_profile_v2.md" in response["path"]
    assert "schema_version" in response["markdown"]
    assert "LLM 生成提示词" in response["markdown"]


@pytest.mark.parametrize("unsafe_kind", ["mode", "owner", "hardlink"])
def test_opc_simulator_profile_read_rejects_unsafe_inode(
    tmp_path, monkeypatch, unsafe_kind
):
    profile = _valid_opc_simulator_profile("draft")
    opc_simulator_profiles.save_profile("unsafe.json", profile, tmp_path)
    path = tmp_path / "unsafe.json"
    if unsafe_kind == "mode":
        path.chmod(0o620)
    elif unsafe_kind == "owner":
        actual_uid = os.getuid()
        monkeypatch.setattr(
            opc_simulator_profiles.os, "getuid", lambda: actual_uid + 1
        )
    else:
        os.link(path, tmp_path / "hardlink.json")

    with pytest.raises(ValueError, match="安全"):
        opc_simulator_profiles.read_profile("unsafe.json", tmp_path)


def test_opc_simulator_profile_save_rejects_existing_hardlink(tmp_path):
    path = tmp_path / "linked.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    os.link(path, tmp_path / "alias.json")

    with pytest.raises(ValueError, match="安全"):
        opc_simulator_profiles.save_profile(
            "linked.json",
            _valid_opc_simulator_profile("draft"),
            tmp_path,
        )


def test_opc_simulator_profile_revision_required_for_overwrite(tmp_path):
    profile = _valid_opc_simulator_profile("draft")
    created = opc_simulator_profiles.save_profile("revision.json", profile, tmp_path)
    assert len(created["revision"]) == 64
    assert opc_simulator_profiles.read_profile("revision.json", tmp_path) == created

    replacement = _valid_opc_simulator_profile("draft")
    replacement["name"] = "replacement"
    with pytest.raises(opc_simulator_profiles.RevisionConflict):
        opc_simulator_profiles.save_profile("revision.json", replacement, tmp_path)
    with pytest.raises(opc_simulator_profiles.RevisionConflict):
        opc_simulator_profiles.save_profile(
            "revision.json",
            replacement,
            tmp_path,
            expected_revision="0" * 64,
        )

    forced = opc_simulator_profiles.save_profile(
        "revision.json",
        replacement,
        tmp_path,
        expected_revision=opc_simulator_profiles.REVISION_FORCE_REPLACE,
    )
    assert forced["revision"] != created["revision"]
    assert forced["profile"]["name"] == "replacement"

    replacement_locked = _valid_opc_simulator_profile("draft")
    replacement_locked["name"] = "replacement-locked"
    updated = opc_simulator_profiles.save_profile(
        "revision.json",
        replacement_locked,
        tmp_path,
        expected_revision=forced["revision"],
    )
    assert updated["revision"] != forced["revision"]
    assert opc_simulator_profiles.read_profile("revision.json", tmp_path) == updated


def test_two_concurrent_overwrites_with_same_revision_allow_only_one(tmp_path):
    profile = _valid_opc_simulator_profile("draft")
    created = opc_simulator_profiles.save_profile("concurrent.json", profile, tmp_path)
    barrier = threading.Barrier(2)

    def overwrite(name):
        replacement = _valid_opc_simulator_profile("draft")
        replacement["name"] = name
        barrier.wait()
        return opc_simulator_profiles.save_profile(
            "concurrent.json",
            replacement,
            tmp_path,
            expected_revision=created["revision"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(overwrite, name) for name in ("first", "second")]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(("ok", future.result()))
        except opc_simulator_profiles.RevisionConflict as exc:
            outcomes.append(("conflict", exc))

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("conflict") == 1
    assert stat.S_IMODE((tmp_path / "concurrent.json").stat().st_mode) == 0o600


def _multiprocess_profile_overwrite(
    config_dir, revision, name, start_event, result_queue
):
    replacement = _valid_opc_simulator_profile("draft")
    replacement["name"] = name
    replacement["nodes"][0]["params"] = {"blob": "x" * 1_500_000}
    start_event.wait()
    try:
        result = opc_simulator_profiles.save_profile(
            "multiprocess.json",
            replacement,
            config_dir,
            expected_revision=revision,
        )
    except opc_simulator_profiles.RevisionConflict:
        result_queue.put("conflict")
    else:
        result_queue.put(("ok", result["revision"]))


def test_profile_lock_artifacts_are_private_and_safe(tmp_path):
    opc_simulator_profiles.save_profile(
        "locked.json",
        _valid_opc_simulator_profile("draft"),
        tmp_path,
    )

    lock_dir = tmp_path / ".opc-profile-locks"
    lock_file = lock_dir / "locked.json.lock"
    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    assert lock_file.stat().st_uid == os.getuid()
    assert lock_file.stat().st_nlink == 1


@pytest.mark.parametrize("target_kind", ["directory", "file"])
def test_profile_lock_rejects_symlink_targets(tmp_path, target_kind):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    lock_dir = tmp_path / ".opc-profile-locks"
    if target_kind == "directory":
        lock_dir.symlink_to(outside, target_is_directory=True)
    else:
        lock_dir.mkdir(mode=0o700)
        outside_file = outside / "lock"
        outside_file.write_text("", encoding="utf-8")
        outside_file.chmod(0o600)
        (lock_dir / "locked.json.lock").symlink_to(outside_file)

    with pytest.raises(ValueError, match="锁"):
        opc_simulator_profiles.save_profile(
            "locked.json",
            _valid_opc_simulator_profile("draft"),
            tmp_path,
        )


def test_profile_save_fsyncs_file_and_containing_directory(tmp_path, monkeypatch):
    original_fsync = opc_simulator_profiles.os.fsync
    synced_kinds = []

    def tracked_fsync(fd):
        mode = os.fstat(fd).st_mode
        synced_kinds.append(
            "directory" if stat.S_ISDIR(mode) else "file"
        )
        return original_fsync(fd)

    monkeypatch.setattr(opc_simulator_profiles.os, "fsync", tracked_fsync)

    opc_simulator_profiles.save_profile(
        "durable.json",
        _valid_opc_simulator_profile("draft"),
        tmp_path,
    )

    assert "file" in synced_kinds
    assert "directory" in synced_kinds


def test_multiprocess_overwrites_with_same_revision_allow_only_one(tmp_path):
    created = opc_simulator_profiles.save_profile(
        "multiprocess.json",
        _valid_opc_simulator_profile("draft"),
        tmp_path,
    )
    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_profile_overwrite,
            args=(
                str(tmp_path),
                created["revision"],
                name,
                start_event,
                result_queue,
            ),
        )
        for name in ("first", "second")
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    outcomes = [result_queue.get(timeout=2) for _ in processes]

    assert sum(isinstance(item, tuple) and item[0] == "ok" for item in outcomes) == 1
    assert outcomes.count("conflict") == 1


def _route_endpoint(app, path, method):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == path
        and method in getattr(route, "methods", set())
    )


class _StreamingJsonRequest:
    def __init__(self, payload=None, *, chunks=None, headers=None):
        if chunks is None:
            chunks = [json.dumps(payload, ensure_ascii=False).encode("utf-8")]
        self._chunks = chunks
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.stream_calls = 0

    async def stream(self):
        self.stream_calls += 1
        if self.stream_calls != 1:
            raise AssertionError("请求 body 被重复读取")
        for chunk in self._chunks:
            yield chunk


class _FakeOpcSimulatorManager:
    def __init__(self):
        self.start_calls = []
        self.stop_calls = []
        self.shutdown_calls = 0
        self.shutdown_gate = None
        self.shutdown_thread = None
        self.start_error = None
        self.stop_error = None
        self.current = {
            "state": "idle",
            "pid": None,
            "file_name": None,
            "revision": None,
            "run_id": None,
            "opc_url": None,
            "started_at": None,
            "ended_at": None,
            "ended_monotonic": None,
            "elapsed_seconds": 0.0,
            "return_code": None,
            "restore_status": "not_started",
            "last_error": None,
            "recent_logs": [],
        }

    def start(self, file_name, expected_revision, *, allow_unsafe_url=False):
        self.start_calls.append((file_name, expected_revision, allow_unsafe_url))
        if self.start_error is not None:
            raise self.start_error
        return {
            **self.current,
            "state": "running",
            "file_name": file_name,
            "run_id": "1" * 32,
        }

    def status(self):
        return dict(self.current)

    def stop(self, *, expected_run_id=None):
        self.stop_calls.append(expected_run_id)
        if self.stop_error is not None:
            raise self.stop_error
        return {**self.current, "state": "stopped"}

    def shutdown(self):
        self.shutdown_calls += 1
        self.shutdown_thread = threading.get_ident()
        if self.shutdown_gate is not None:
            self.shutdown_gate.wait(1)
        return dict(self.current)


def test_opc_simulator_process_api_start_status_stop_and_shutdown_cleanup():
    manager = _FakeOpcSimulatorManager()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    start = _route_endpoint(app, "/api/opc-simulator/start", "POST")
    status = _route_endpoint(app, "/api/opc-simulator/status", "GET")
    stop = _route_endpoint(app, "/api/opc-simulator/stop", "POST")

    started = asyncio.run(
        start(
            _StreamingJsonRequest(
                {"file_name": "demo.json", "expected_revision": "revision-1"}
            )
        )
    )

    assert started["state"] == "running"
    assert started["run_id"] == "1" * 32
    assert manager.start_calls == [("demo.json", "revision-1", False)]
    assert asyncio.run(status())["state"] == "idle"
    assert asyncio.run(
        stop(_StreamingJsonRequest({"expected_run_id": "1" * 32}))
    )["state"] == "stopped"
    assert manager.stop_calls == ["1" * 32]
    asyncio.run(app.router.on_shutdown[-1]())
    assert manager.shutdown_calls == 1


def test_opc_simulator_shutdown_handler_does_not_block_event_loop():
    manager = _FakeOpcSimulatorManager()
    manager.shutdown_gate = threading.Event()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    handler = app.router.on_shutdown[-1]
    caller_thread = threading.get_ident()

    async def scenario():
        task = asyncio.create_task(handler())
        await asyncio.sleep(0.01)
        event_loop_progressed = not task.done()
        manager.shutdown_gate.set()
        await task
        return event_loop_progressed

    assert asyncio.run(scenario()) is True
    assert manager.shutdown_thread != caller_thread


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"file_name": "demo.json"},
        {"expected_revision": "revision"},
        {"file_name": 1, "expected_revision": "revision"},
        {"file_name": "demo.json", "expected_revision": 1},
        {"file_name": "", "expected_revision": "revision"},
        {"file_name": "demo.json", "expected_revision": ""},
    ],
)
def test_opc_simulator_start_api_requires_strict_string_fields(payload):
    manager = _FakeOpcSimulatorManager()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    start = _route_endpoint(app, "/api/opc-simulator/start", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(start(_StreamingJsonRequest(payload)))

    assert caught.value.status_code == 400
    assert manager.start_calls == []


@pytest.mark.parametrize("allow_unsafe_url", [None, 0, 1, "false", [], {}])
def test_opc_simulator_start_api_requires_strict_unsafe_url_boolean(
    allow_unsafe_url,
):
    manager = _FakeOpcSimulatorManager()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    start = _route_endpoint(app, "/api/opc-simulator/start", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            start(
                _StreamingJsonRequest(
                    {
                        "file_name": "demo.json",
                        "expected_revision": "revision",
                        "allow_unsafe_url": allow_unsafe_url,
                    }
                )
            )
        )

    assert caught.value.status_code == 400
    assert manager.start_calls == []


def test_opc_simulator_start_api_forwards_explicit_unsafe_url_confirmation():
    manager = _FakeOpcSimulatorManager()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    start = _route_endpoint(app, "/api/opc-simulator/start", "POST")

    asyncio.run(
        start(
            _StreamingJsonRequest(
                {
                    "file_name": "demo.json",
                    "expected_revision": "revision",
                    "allow_unsafe_url": True,
                }
            )
        )
    )

    assert manager.start_calls == [("demo.json", "revision", True)]


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (
            workflow_ui.InvalidSimulatorRevision("secret /tmp/config.json"),
            400,
            "模拟器 revision 格式无效",
        ),
        (
            workflow_ui.SimulatorConfigNotFound("secret /tmp/config.json"),
            404,
            "模拟器配置不存在",
        ),
        (
            workflow_ui.SimulatorInputError("secret /tmp/config.json"),
            400,
            "模拟器启动参数无效",
        ),
        (
            workflow_ui.SimulatorRevisionConflict("secret /tmp/config.json"),
            409,
            "profile revision 冲突",
        ),
        (
            workflow_ui.InvalidSimulatorConfig("secret /tmp/config.json"),
            422,
            "模拟器配置不可运行",
        ),
        (
            workflow_ui.UnsafeSimulatorUrlConfirmationRequired(
                "secret /tmp/config.json"
            ),
            422,
            "非默认 OPC URL 需明确确认风险",
        ),
        (
            workflow_ui.SimulatorAlreadyRunning("secret /tmp/config.json"),
            409,
            "已有 OPC 模拟器正在运行",
        ),
        (
            workflow_ui.SimulatorSpawnError("secret /tmp/config.json"),
            500,
            "启动模拟器进程失败",
        ),
        (
            workflow_ui.SimulatorStorageFull("secret /tmp/config.json"),
            507,
            "模拟器存储空间不足",
        ),
        (
            workflow_ui.SimulatorStorageError("secret /tmp/config.json"),
            500,
            "模拟器存储操作失败",
        ),
        (
            OSError("secret /tmp/config.json"),
            500,
            "模拟器系统操作失败",
        ),
    ],
)
def test_opc_simulator_start_api_maps_errors_without_internal_details(
    error, status_code, detail
):
    manager = _FakeOpcSimulatorManager()
    manager.start_error = error
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    start = _route_endpoint(app, "/api/opc-simulator/start", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            start(
                _StreamingJsonRequest(
                    {"file_name": "demo.json", "expected_revision": "revision"}
                )
            )
        )

    assert caught.value.status_code == status_code
    assert caught.value.detail == detail
    assert "/tmp" not in str(caught.value.detail)


def test_opc_simulator_stop_timeout_is_504_without_force_kill_details():
    manager = _FakeOpcSimulatorManager()
    manager.stop_error = workflow_ui.StopTimeout(manager.current)
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    stop = _route_endpoint(app, "/api/opc-simulator/stop", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(stop(_StreamingJsonRequest({})))

    assert caught.value.status_code == 504
    assert caught.value.detail == "模拟器停止超时，恢复结果不确定"


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_run_id": 1},
        {"expected_run_id": ""},
        {"expected_run_id": "A" * 32},
        {"expected_run_id": "0" * 31},
        {"unexpected": "field"},
    ],
)
def test_opc_simulator_stop_api_requires_strict_bounded_identity_body(payload):
    manager = _FakeOpcSimulatorManager()
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    stop = _route_endpoint(app, "/api/opc-simulator/stop", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(stop(_StreamingJsonRequest(payload)))

    assert caught.value.status_code == 400
    assert manager.stop_calls == []


def test_opc_simulator_stop_api_maps_run_identity_conflict_without_signal():
    manager = _FakeOpcSimulatorManager()
    manager.stop_error = workflow_ui.SimulatorRunIdentityConflict(
        "secret identity mismatch"
    )
    app = create_app(
        "szlab_robot_action_workflow",
        opc_simulator_manager=manager,
    )
    stop = _route_endpoint(app, "/api/opc-simulator/stop", "POST")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            stop(_StreamingJsonRequest({"expected_run_id": "2" * 32}))
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "模拟器运行身份冲突"
    assert manager.stop_calls == ["2" * 32]


def test_opc_simulator_profile_api_generates_validates_saves_and_reads(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    generate = _route_endpoint(
        app, "/api/opc-simulator/profiles:generate", "POST"
    )
    validate = _route_endpoint(
        app, "/api/opc-simulator/profiles:validate", "POST"
    )
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )
    read = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "GET"
    )
    workflow, templates = _opc_profile_generation_inputs()

    generated = asyncio.run(
        generate(
            _StreamingJsonRequest(
                {
                    "workflow": workflow,
                    "templates": templates,
                    "scheduled_template_ids": ["template-a"],
                    "action_catalog": [],
                    "variable_catalog": [],
                    "name": "Generated profile",
                    "opc_url": "opc.tcp://localhost:4840",
                    "file_name": "generated-profile.json",
                }
            )
        )
    )
    assert generated["file_name"] == "generated-profile.json"
    assert generated["profile"]["status"] == "draft"
    assert all(
        item["direction"] in ("plc_to_pc", "pc_to_plc")
        and item["data_type"] in ("bool", "int", "float", "string")
        for item in generated["profile"]["variables"]
    )
    assert not any(
        error.endswith(".direction") or error.endswith(".data_type")
        for error in generated["validation_errors"]
        if error.startswith("variables[")
    )

    draft = _valid_opc_simulator_profile("draft")
    draft["nodes"][0]["channel"] = ""
    validated = asyncio.run(
        validate(
            _StreamingJsonRequest(
                {"profile": draft, "file_name": "draft-profile.json"}
            )
        )
    )
    assert validated["file_name"] == "draft-profile.json"
    assert validated["profile"]["nodes"][0]["channel"] == ""
    assert validated["validation_errors"] == ["nodes[0].channel"]

    saved = asyncio.run(
        save("draft-profile.json", _StreamingJsonRequest({"profile": draft}))
    )
    assert saved == asyncio.run(read("draft-profile.json"))
    assert saved["validation_errors"] == ["nodes[0].channel"]


def test_opc_simulator_profile_api_defaults_draft_to_loopback_url():
    app = create_app("szlab_robot_action_workflow")
    generate = _route_endpoint(
        app, "/api/opc-simulator/profiles:generate", "POST"
    )
    workflow, templates = _opc_profile_generation_inputs()

    generated = asyncio.run(
        generate(
            _StreamingJsonRequest(
                {
                    "workflow": workflow,
                    "templates": templates,
                    "scheduled_template_ids": ["template-a"],
                    "action_catalog": [],
                    "variable_catalog": [],
                    "name": "Loopback profile",
                    "file_name": "loopback-profile.json",
                }
            )
        )
    )

    assert generated["profile"]["opc"]["url"] == "opc.tcp://127.0.0.1:4840"


def test_opc_simulator_profile_api_rejects_invalid_runnable_and_accepts_valid(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )
    invalid = _valid_opc_simulator_profile()
    invalid["nodes"][0]["channel"] = ""

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            save("invalid.json", _StreamingJsonRequest({"profile": invalid}))
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["validation_errors"] == ["nodes[0].channel"]
    assert not (tmp_path / "invalid.json").exists()

    valid = asyncio.run(
        save(
            "valid.json",
            _StreamingJsonRequest({"profile": _valid_opc_simulator_profile()}),
        )
    )
    assert valid["file_name"] == "valid.json"
    assert valid["validation_errors"] == []
    assert (tmp_path / "valid.json").is_file()


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/opc-simulator/profiles:generate", "POST"),
        ("/api/opc-simulator/profiles:validate", "POST"),
        ("/api/opc-simulator/profiles/{file_name}", "PUT"),
    ],
)
def test_opc_profile_body_endpoints_reject_over_2_mib(path, method):
    app = create_app("szlab_robot_action_workflow")
    endpoint = _route_endpoint(app, path, method)
    request = _StreamingJsonRequest(
        chunks=[b'{"padding":"', b"x" * (2 * 1024 * 1024), b'"}']
    )

    with pytest.raises(HTTPException) as caught:
        if method == "PUT":
            asyncio.run(endpoint("large.json", request))
        else:
            asyncio.run(endpoint(request))

    assert caught.value.status_code == 413
    assert request.stream_calls == 1


def test_opc_profile_validation_rejects_malformed_json_as_400():
    app = create_app("szlab_robot_action_workflow")
    validate = _route_endpoint(
        app, "/api/opc-simulator/profiles:validate", "POST"
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(validate(_StreamingJsonRequest(chunks=[b"{bad json"])))

    assert caught.value.status_code == 400
    assert caught.value.detail == "请求 JSON 无效"


def test_json_nesting_scanner_rejects_100000_levels_under_body_limit():
    raw = b"[" * 100_000 + b"0" + b"]" * 100_000
    assert len(raw) < 2 * 1024 * 1024

    with pytest.raises(HTTPException) as caught:
        workflow_ui._validate_json_nesting(raw)

    assert caught.value.status_code == 413


def test_json_nesting_scanner_ignores_brackets_inside_escaped_strings():
    payload = {
        "text": (
            "{[not nesting]} "
            + '\\"quoted {[ text ]}\\" '
            + "\\\\{[still text]}"
        )
        * 200
    }
    raw = json.dumps(payload).encode("utf-8")

    workflow_ui._validate_json_nesting(raw)


def test_json_nesting_scanner_accepts_100_and_rejects_101_levels():
    accepted = b"[" * 100 + b"0" + b"]" * 100
    rejected = b"[" * 101 + b"0" + b"]" * 101

    workflow_ui._validate_json_nesting(accepted)
    with pytest.raises(HTTPException) as caught:
        workflow_ui._validate_json_nesting(rejected)

    assert caught.value.status_code == 413


def test_opc_profile_json_recursion_error_is_classified_as_400(monkeypatch):
    app = create_app("szlab_robot_action_workflow")
    validate = _route_endpoint(
        app, "/api/opc-simulator/profiles:validate", "POST"
    )

    def fail_with_recursion(_value):
        raise RecursionError("too deep")

    monkeypatch.setattr(workflow_ui.json, "loads", fail_with_recursion)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            validate(
                _StreamingJsonRequest(
                    {"profile": _valid_opc_simulator_profile("draft")}
                )
            )
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == "请求 JSON 嵌套过深"


def test_opc_profile_json_parsing_runs_off_event_loop(monkeypatch):
    app = create_app("szlab_robot_action_workflow")
    validate = _route_endpoint(
        app, "/api/opc-simulator/profiles:validate", "POST"
    )
    original_loads = json.loads
    parser_threads = []

    def tracked_loads(value):
        parser_threads.append(threading.get_ident())
        return original_loads(value)

    monkeypatch.setattr(workflow_ui.json, "loads", tracked_loads)
    main_thread = threading.get_ident()

    response = asyncio.run(
        validate(
            _StreamingJsonRequest(
                {
                    "profile": _valid_opc_simulator_profile("draft"),
                    "file_name": "async.json",
                }
            )
        )
    )

    assert response["file_name"] == "async.json"
    assert parser_threads and parser_threads[0] != main_thread


def test_opc_profile_api_maps_collection_limits_to_422():
    app = create_app("szlab_robot_action_workflow")
    generate = _route_endpoint(
        app, "/api/opc-simulator/profiles:generate", "POST"
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            generate(
                _StreamingJsonRequest(
                    {
                        "workflow": {"nodes": []},
                        "templates": [{} for _ in range(501)],
                        "scheduled_template_ids": [],
                        "action_catalog": [],
                        "variable_catalog": [],
                        "name": "large",
                        "opc_url": "opc.tcp://localhost:4840",
                    }
                )
            )
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["validation_errors"] == ["templates"]


def test_opc_profile_generate_api_rejects_unknown_root_field():
    app = create_app("szlab_robot_action_workflow")
    generate = _route_endpoint(
        app, "/api/opc-simulator/profiles:generate", "POST"
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            generate(
                _StreamingJsonRequest(
                    {
                        "workflow": {"nodes": []},
                        "templates": [],
                        "scheduled_template_ids": [],
                        "action_catalog": [],
                        "variable_catalog": [],
                        "name": "strict",
                        "opc_url": "opc.tcp://localhost:4840",
                        "resources": ["must-not-be-consumed"],
                    }
                )
            )
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == "生成请求含未知字段: resources"


def test_opc_profile_validate_requires_unambiguous_wrapper_shape(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    validate = _route_endpoint(
        app, "/api/opc-simulator/profiles:validate", "POST"
    )
    profile = _valid_opc_simulator_profile("draft")

    for payload in (
        profile,
        {"profile": profile},
        {
            "profile": profile,
            "file_name": "strict.json",
            "ignored": True,
        },
    ):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(validate(_StreamingJsonRequest(payload)))
        assert caught.value.status_code == 400


def test_opc_profile_save_requires_profile_only_wrapper(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )
    profile = _valid_opc_simulator_profile("draft")

    for payload in (
        profile,
        {"profile": profile, "ignored": True},
    ):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(save("strict.json", _StreamingJsonRequest(payload)))
        assert caught.value.status_code == 400


def test_opc_profile_save_api_maps_pretty_oversize_to_422_without_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        workflow_ui,
        "migrate_legacy_opc_profiles",
        lambda *_args, **_kwargs: [],
    )
    app = create_app("szlab_robot_action_workflow")
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )
    profile = _profile_with_request_under_limit_but_pretty_over_limit()

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            save(
                "oversized.json",
                _StreamingJsonRequest({"profile": profile}),
            )
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["validation_errors"] == ["profile"]
    assert list(tmp_path.iterdir()) == []


def test_generate_draft_reports_action_catalog_extra_fields_exactly():
    result = workflow_ui.generate_opc_simulator_draft(
        {"nodes": []},
        [],
        [],
        "Strict catalog",
        "opc.tcp://localhost:4840",
        action_catalog=[
            {
                "device_id": "device",
                "method": "run",
                "opc_variables": [],
                "resources": ["forbidden"],
            }
        ],
        variable_catalog=[],
    )

    assert "action_catalog[0].resources" in result["validation_errors"]


def test_opc_profile_api_requires_matching_if_match_for_overwrite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )
    profile = _valid_opc_simulator_profile("draft")
    created = asyncio.run(
        save("revision.json", _StreamingJsonRequest({"profile": profile}))
    )
    profile["name"] = "replacement"

    with pytest.raises(HTTPException) as missing:
        asyncio.run(
            save("revision.json", _StreamingJsonRequest({"profile": profile}))
        )
    assert missing.value.status_code == 409

    updated = asyncio.run(
        save(
            "revision.json",
            _StreamingJsonRequest(
                {"profile": profile},
                headers={"If-Match": created["revision"]},
            ),
        )
    )
    assert updated["revision"] != created["revision"]


@pytest.mark.parametrize(
    ("error_number", "expected_status"),
    [
        (errno.ENOSPC, 507),
        (errno.EDQUOT, 507),
        (errno.EIO, 500),
    ],
)
def test_opc_profile_api_classifies_storage_errors_without_path_leak(
    tmp_path, monkeypatch, error_number, expected_status
):
    monkeypatch.setattr(workflow_ui, "OPC_SIMULATOR_CONFIG_DIR", tmp_path)
    app = create_app("szlab_robot_action_workflow")
    save = _route_endpoint(
        app, "/api/opc-simulator/profiles/{file_name}", "PUT"
    )

    def fail_save(*_args, **_kwargs):
        raise OSError(error_number, "secret", "/private/secret/profile.json")

    monkeypatch.setattr(workflow_ui, "save_profile", fail_save)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            save(
                "error.json",
                _StreamingJsonRequest(
                    {"profile": _valid_opc_simulator_profile("draft")}
                ),
            )
        )

    assert caught.value.status_code == expected_status
    assert "/private/secret" not in str(caught.value.detail)


def test_s06_debug_stack_status_is_disabled_with_empty_group_list(monkeypatch):
    class FakePLC:
        def __init__(self):
            self.calls = []

        def get_stack_status(self, group_names=None):
            self.calls.append(group_names)
            return {
                "success": True,
                "schema": "szlab_poly_studio.stack_status.v1",
                "stacks": {},
            }

    fake_plc = FakePLC()

    def fake_get_live_devices(self):
        return {"szlab_poly_plc": fake_plc}

    monkeypatch.setattr(WorkflowRunManager, "get_live_devices", fake_get_live_devices)

    app = create_app("debug_s06_pump")
    stack_status_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/stack-status"
    )

    response = asyncio.run(stack_status_endpoint())

    assert response["success"] is True
    assert response["stacks"] == {}
    assert fake_plc.calls == [[]]


def test_stack_s05_s06_preset_uses_trimmed_csv_for_stack_camera_and_pump():
    preset = load_preset("stack_s05_s06")
    runtime_config = _load_preset_runtime_config(preset)
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        variable_names = {row["变量名"] for row in csv.DictReader(handle)}

    required_variables = {
        "S05加工完成",
        "S05拍照结果",
        "S06准备信号",
        "S06允许加工",
        "S06工艺选择",
        "S06_1号溶液添加量",
        "S06_2号溶液添加量",
        "S06参数写入完成",
        "S06加工完成",
        "传感器状态_上位机[4].NO[12]",
        "传感器状态_上位机[5].NO[1]",
        "传感器状态_上位机[3].NO[8]",
        "传感器状态_上位机[3].NO[13]",
    }

    assert preset.id == "stack_s05_s06"
    assert preset.default_config["csv"] == "stack_s05_s06_nodes.csv"
    assert preset.target_device_ids == ["szlab_mixer_photoshotting", "szlab_mixer_pump"]
    plc_node = next(
        node for node in preset.device_graph["nodes"] if node["id"] == "szlab_poly_plc"
    )
    assert plc_node["config"]["opcua_node_id_prefix"] == "ns=4;s=上位机通讯|"
    assert "opcua_node_id_map" not in plc_node["config"]
    pump_node = next(
        node
        for node in preset.device_graph["nodes"]
        if node["id"] == "szlab_mixer_pump"
    )
    assert "opcua_node_id_map" not in pump_node["config"]
    take_photo_snapshot = collect_snapshot_variables("take_photo", {}, runtime_config)
    assert "传感器状态_上位机[3].NO[8]" in take_photo_snapshot
    assert "传感器状态_上位机[3].NO[13]" in take_photo_snapshot
    assert "传感器状态_上位机[4].NO[12]" in take_photo_snapshot
    assert "传感器状态_上位机[5].NO[15]" in take_photo_snapshot
    assert runtime_config.device_factory.plc_device_id == "szlab_poly_plc"
    assert "szlab_poly_plc" in runtime_config.device_factory.devices
    assert "szlab_mixer_photoshotting" in runtime_config.device_factory.devices
    assert "szlab_mixer_pump" in runtime_config.device_factory.devices
    assert required_variables <= variable_names
