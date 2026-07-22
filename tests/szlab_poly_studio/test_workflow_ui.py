import asyncio
import csv
import json
import os
import time
from importlib.util import find_spec
from pathlib import Path

import pytest

import scripts.run_workflow_local as run_workflow_local
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
    load_preset,
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


def test_ai4c_preset_csv_matches_default_opc_namespace():
    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)
    csv_path = _resolve_ui_path(preset.default_config["csv"], preset)

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows_by_english_name = {
            row["EnglishName"]: row
            for row in csv.DictReader(handle)
        }

    variables = collect_snapshot_variables(
        "pick_well_plate_from_loading_rack",
        {"position": 1},
        runtime_config,
    )

    assert preset.default_config["url"] == "opc.tcp://jdht1471820.bohrium.tech:50003"
    for variable in variables:
        assert rows_by_english_name[variable]["NodeId"].startswith("ns=4;s=UniLab|")


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

    camera_node = next(node for node in graph["nodes"] if node["id"] == "szlab_mixer_photoshotting")
    assert camera_node["config"]["url"] == "opc.tcp://127.0.0.1:48405/"
    assert camera_node["config"]["csv_path"].endswith("s05_photoshotting/photoshotting_nodes.csv")
    assert camera_node["config"]["save_dir"] == "unilabos_data/szlab_poly_studio/s05_photoshotting/photos"
    assert "opcua_node_id_map" not in camera_node["config"]
    assert _action_to_dict(preset.actions["take_photo"], runtime_config)["opc_variables"] == [
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
    assert collect_snapshot_variables("run_stirring", {"position": 1}, runtime_config) == [
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

    stirrer_node = next(node for node in graph["nodes"] if node["id"] == "szlab_mixer_stirrer")
    assert stirrer_node["config"]["url"] == "opc.tcp://127.0.0.1:48405/"
    assert stirrer_node["config"]["csv_path"].endswith("s04_magnetic_stirring/magnetic_stirring_nodes.csv")
    assert "opcua_node_id_map" not in stirrer_node["config"]
    assert _action_to_dict(preset.actions["run_stirring"], runtime_config)["opc_variables"] == []


def test_single_device_runtime_does_not_force_missing_plc_gateway(monkeypatch, tmp_path):
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
    monkeypatch.setattr(run_workflow_local, "_load_class", lambda _class_path: FakeStirrerDevice)
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/magnetic_stirring_runtime.json")

    devices = create_local_devices(tmp_path / "graph.json", runtime_config=runtime_config)

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
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/s07_robot_runtime.json")

    devices = create_local_devices(tmp_path / "graph.json", runtime_config=runtime_config)

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
            "szlab_poly_plc": {"url": "opc.tcp://127.0.0.1:48506/", "csv_path": "pump_nodes.csv"},
            "szlab_mixer_pump": {
                "url": "opc.tcp://127.0.0.1:48506/",
                "timeout": 300,
                "pipeline_route_specs": [
                    {"pump": 1, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21}
                ],
            },
        },
    )
    monkeypatch.setattr(run_workflow_local, "_load_class", fake_load_class)
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/debug_s06_pump_runtime.json")

    devices = create_local_devices(
        tmp_path / "graph.json",
        csv_path=Path("unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv"),
        runtime_config=runtime_config,
    )

    assert set(devices) == {"szlab_poly_plc", "szlab_mixer_pump"}
    assert devices["szlab_poly_plc"].config["csv_path"].endswith("s06_pump/pump_nodes.csv")
    assert devices["szlab_mixer_pump"].config["csv_path"].endswith("s06_pump/pump_nodes.csv")
    assert devices["szlab_mixer_pump"].config["pipeline_route_specs"] == [
        {"pump": 1, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21}
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
                    "params": {"position": 1, "speed": 300, "temperature": 25, "duration": 60},
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

    assert [node["device_name"] for node in workflow["nodes"]] == [
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
    assert set(graph_nodes) == {"szlab_poly_plc", "szlab_mixer_robot", "szlab_s07_solid_addition"}
    assert graph_nodes["szlab_s07_solid_addition"]["config"] == {
        "plc_device_id": "szlab_poly_plc",
        "process_timeout": "${timeout}",
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
    assert preset.actions["scan_powder_cartridges"].device_id == "szlab_s07_solid_addition"
    assert preset.actions["rotate_powder_cartridge_to_feed"].device_id == "szlab_s07_solid_addition"
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
        "S07天平读数",
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
        "timeout": "${timeout}",
        "pipeline_route_specs": [
            {"pump": 1, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21},
            {"pump": 1, "pipeline": "dispense", "control_valve": 12, "absolute_position": 22},
            {"pump": 1, "pipeline": "air", "control_valve": 13, "absolute_position": 23},
            {"pump": 2, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21},
            {"pump": 2, "pipeline": "dispense", "control_valve": 12, "absolute_position": 22},
            {"pump": 2, "pipeline": "air", "control_valve": 13, "absolute_position": 23},
        ],
    }
    assert runtime_config.device_factory.devices == {
        "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
        "szlab_mixer_pump": "unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump.SzlabMixerPumpDevice",
    }
    assert preset.actions["transfer_liquid"].device_id == "szlab_mixer_pump"
    assert preset.actions["run_solvent_addition"].device_id == "szlab_mixer_pump"
    assert collect_snapshot_variables("run_solvent_addition", {"process": 1}, runtime_config) == [
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
    assert preset.runtime_config == "../runtime_configs/debug_s09_pipetting_station_runtime.json"
    assert not Path("tests/szlab_poly_studio/presets/s09_pipetting_station.json").exists()
    assert not Path("tests/szlab_poly_studio/runtime_configs/s09_pipetting_station_runtime.json").exists()
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
    assert "go_to_safe_position" not in preset.actions
    assert "add_liquid" in preset.actions
    assert "add_liquid_to_beaker" in preset.actions
    add_liquid_param_names = [param["name"] for param in preset.actions["add_liquid"].params]
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
        "timeout": 300.0,
        "csv_path": "s09_pipetting_station/pipetting_station_nodes.csv",
    }
    assert preset.debug_config["skip_robot_precheck_variables"] == robot_action_preset.debug_config[
        "skip_robot_precheck_variables"
    ]
    assert preset.debug_config["env"]["SKIP_SENSOR_PRECHECK"] == "1"
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
    assert preset.actions["add_liquid_to_beaker"].device_id == "szlab_mixer_pipetting_station"
    assert "run_process" not in preset.actions
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
        "add_liquid_to_beaker",
        "run_liquid_workflow",
        "set_liquid_bottle_remaining_volume",
        "initialize_liquid_bottle_remaining_volumes",
        "read_balance",
        "get_pipetting_status",
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
        "S07天平读数",
    ]
    assert collect_snapshot_variables("run_solvent_addition", {"process": 3}, runtime_config) == [
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
    assert methods[10:20] == [
        "submit_pick_from_s072",
        "submit_place_to_s071",
        "submit_pick_from_s071",
        "rotate_powder_cartridge_to_feed",
        "submit_place_to_s072",
        "submit_pick_from_s072",
        "submit_place_to_s071",
        "submit_pick_from_s071",
        "rotate_powder_cartridge_to_feed",
        "submit_place_to_s072",
    ]
    by_id = {action["workflow_node_id"]: action for action in actions}
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


def test_szlab_robot_action_workflow_explicit_debug_skips_s03_pick_sensor_gate(monkeypatch):
    from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import SzlabMixerRobotDevice

    monkeypatch.delenv("SKIP_SENSOR_PRECHECK", raising=False)
    monkeypatch.delenv("SKIP_ROBOT_PRECHECK_VARIABLES", raising=False)
    apply_preset_debug_config("szlab_robot_action_workflow")

    device = SzlabMixerRobotDevice(auto_connect=False)
    result = device._ensure_sensor_gate("传感器状态_上位机[0].NO[6]", True, "S03 取料源位必须有物料")

    assert result is None


def test_szlab_robot_action_workflow_flow_matches_requested_synthesis_route():
    flow = json.loads(Path("szlab_robot_action_workflow_flow.json").read_text(encoding="utf-8"))
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
    assert actions[5]["params"]["process"] == 3
    assert actions[5]["params"]["skip_level_check"] is True
    assert "skip_robot" not in actions[5]["params"]
    assert actions[8]["params"]["position"] == 1
    assert actions[8]["params"]["mode"] == 3
    assert actions[-1]["params"] == {"position": 1}


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
            [{"method": "pick_well_plate_from_loading_rack", "params": {"position": position}}],
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
                "data": {"method": "pick_well_plate_from_loading_rack", "params": {"position": 2}},
            },
            {
                "id": "pipette",
                "position": {"x": 220, "y": 0},
                "data": {"method": "place_well_plate_to_pipetting_station", "params": {}},
            },
            {
                "id": "hplc",
                "position": {"x": 220, "y": 140},
                "data": {"method": "place_well_plate_to_hplc_station", "params": {}},
            },
            {
                "id": "unload",
                "position": {"x": 440, "y": 0},
                "data": {"method": "place_well_plate_to_unloading_rack", "params": {"position": 4}},
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
    assert [node["uuid"] for node in workflow["nodes"]] == ["load", "pipette", "hplc", "unload"]
    assert workflow["nodes"][0]["name"] == "auto-pick_well_plate_from_loading_rack"
    assert workflow["nodes"][0]["param"] == {"position": 2}
    assert workflow["nodes"][3]["param"] == {"position": 4}
    assert workflow["edges"] == [
        {"source_node_uuid": "load", "target_node_uuid": "pipette"},
        {"source_node_uuid": "load", "target_node_uuid": "hplc"},
        {"source_node_uuid": "pipette", "target_node_uuid": "unload"},
        {"source_node_uuid": "hplc", "target_node_uuid": "unload"},
    ]


def test_build_graph_workflow_rejects_cycle():
    with pytest.raises(ValueError, match="不能包含环"):
        build_graph_workflow(
            flow_nodes=[
                {"id": "a", "data": {"method": "place_well_plate_to_pipetting_station", "params": {}}},
                {"id": "b", "data": {"method": "pick_well_plate_from_pipetting_station", "params": {}}},
            ],
            flow_edges=[
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
        )


def test_build_graph_workflow_rejects_edge_with_missing_node():
    with pytest.raises(ValueError, match="连线引用了不存在的节点"):
        build_graph_workflow(
            flow_nodes=[{"id": "a", "data": {"method": "place_well_plate_to_pipetting_station", "params": {}}}],
            flow_edges=[{"source": "a", "target": "missing"}],
        )


def test_build_graph_workflow_rejects_invalid_node_position_param():
    with pytest.raises(ValueError, match="position 必须在 1-8 范围内"):
        build_graph_workflow(
            flow_nodes=[
                {
                    "id": "load",
                    "data": {"method": "pick_well_plate_from_loading_rack", "params": {"position": 12}},
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
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json")
    graph = json.loads(
        Path("tests/szlab_poly_studio/fixtures/szlab_mixer_pump_production_graph.json").read_text(
            encoding="utf-8"
        )
    )
    pump_node = next(node for node in graph["nodes"] if node["id"] == "szlab_mixer_pump")
    node_id_map = pump_node["config"]["opcua_node_id_map"]

    for method_name in ("transfer_liquid", "run_solvent_addition"):
        variables = collect_snapshot_variables(method_name, {}, runtime_config)
        assert variables
        assert set(variables) <= set(node_id_map)


def test_pump_runtime_only_exposes_pump_actions():
    preset = load_preset("szlab_mixer")
    runtime_config = load_runtime_config("tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json")

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
            "direct_plc_command_method": "_call_plc_command",
            "timeout_config_key": "plc_action_timeout"
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
    assert collect_snapshot_variables("move_plate", {"position": 3}, runtime_config) == [
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


def test_workflow_ui_parser_defaults_to_container_service():
    args = build_parser().parse_args([])

    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.preset == "ai4c"
    assert args.runtime_config is None
    assert args.open_browser is False


def test_workflow_run_manager_reuses_devices_between_runs(monkeypatch):
    preset = load_preset("ai4c")
    runtime_config = _load_preset_runtime_config(preset)
    manager = WorkflowRunManager(preset, runtime_config)
    created_devices = [{"AI4C_plc": object(), "AI4C_robot_arm": object()}]
    create_calls = []
    disconnect_calls = []

    def fake_create_local_devices(**kwargs):
        create_calls.append(kwargs)
        return created_devices[0]

    def fake_run_nodes(ordered_nodes, devices, logger=None, runtime_config=None):
        assert devices is created_devices[0]
        return [{"uuid": ordered_nodes[0].uuid, "result": {"success": True}}]

    monkeypatch.setattr("scripts.workflow_ui.create_local_devices", fake_create_local_devices)
    monkeypatch.setattr("scripts.workflow_ui.run_nodes", fake_run_nodes)
    monkeypatch.setattr(
        "scripts.workflow_ui._disconnect_devices",
        lambda devices, log=None: disconnect_calls.append(devices),
    )

    payload = {
        "workflow": build_linear_workflow(
            [{"method": "place_well_plate_to_pipetting_station", "params": {}}],
            preset=preset,
        ),
        "graph": "__generated__",
        "url": "opc.tcp://example:4840",
        "no_subscription": True,
        "timeout": 60,
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
            return {name: {"success": True, "value": self.value} for name in variable_names}

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
    live_events = [event for event in events if event["message"].startswith("OPC实时变化:")]
    assert live_events
    assert live_events[-1]["detail"]["changes"][0]["after"] == {"success": True, "value": 2}


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
                    "message": "等待 OPC 变量 S06加工完成 == True (timeout=300.0s, interval=0.2s)",
                    "detail": {
                        "type": "opc_wait",
                        "phase": "start",
                        "variable": "S06加工完成",
                        "expected": True,
                        "timeout": 300.0,
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
                        "timeout": 300.0,
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
        WorkflowNode(uuid="node_1", name="auto-run_solvent_addition", device_name="pump", param={}),
        {"plc": FakePLC(), "pump": FakePump()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    wait_events = [event for event in events if event["detail"] and event["detail"].get("type") == "opc_wait"]
    assert [event["message"] for event in wait_events] == [
        "等待 OPC 变量 S06加工完成 == True (timeout=300.0s, interval=0.2s)",
        "OPC 变量等待完成 S06加工完成 == True: success=True, last_value=True",
    ]
    assert wait_events[0]["detail"]["phase"] == "start"
    assert wait_events[1]["detail"]["phase"] == "finish"


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
                    "message": "等待 OPC 变量 S041加工完成 == True (timeout=300.0s, interval=1.0s)",
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
        WorkflowNode(uuid="node_1", name="auto-run_stirring", device_name="stirrer", param={}),
        {"stirrer": FakeStirrer()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    wait_events = [event for event in events if event["detail"] and event["detail"].get("type") == "opc_wait"]
    assert [event["detail"]["variable"] for event in wait_events] == ["S041加工完成"]


def test_run_node_with_live_opc_sampling_skips_parallel_sampling_for_direct_device(tmp_path):
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
        WorkflowNode(uuid="node_1", name="auto-take_photo", device_name="camera", param={}),
        {"camera": FakeCamera()},
        logger=WorkflowLogger(writer=write_event),
        runtime_config=load_runtime_config(config_path),
        sample_interval=0.01,
    )

    assert results[0]["result"] == {"success": True}
    assert not [event for event in events if event["message"].startswith("OPC实时变化:")]


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
            return {"success": True}

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
    assert not any("OPC状态-before" in message or "OPC状态-after" in message for message in messages)
    assert any("OPC状态采样" in message for message in messages)
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
    monkeypatch.setattr(manager, "get_live_devices", lambda: {"szlab_poly_plc": fake_plc})

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
    assert manager.wait_for_sensor_change(initial_version, timeout=0.01) == initial_version + 1


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
    plc_node = next(node for node in preset.device_graph["nodes"] if node["id"] == "szlab_poly_plc")
    assert plc_node["config"]["opcua_node_id_prefix"] == "ns=4;s=上位机通讯|"
    assert "opcua_node_id_map" not in plc_node["config"]
    pump_node = next(node for node in preset.device_graph["nodes"] if node["id"] == "szlab_mixer_pump")
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
