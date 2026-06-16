import pytest

from scripts.run_workflow_local import (
    WorkflowLogger,
    WorkflowNode,
    _load_class,
    collect_snapshot_variables,
    load_runtime_config,
    run_nodes,
)
from scripts.workflow_ui import (
    RunRecord,
    WorkflowRunManager,
    _load_preset_runtime_config,
    _record_to_dict,
    _register_shutdown_handler,
    build_graph_workflow,
    build_linear_workflow,
    build_local_device_graph,
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


def test_ai4c_runtime_device_classes_are_importable():
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
            "scope": "workflow",
            "node_id": None,
            "detail": None,
        },
        {
            "sequence": 2,
            "message": "节点开始执行",
            "level": "info",
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
        "value_goal": {"success": True, "value": False},
        "after": {"success": True, "value": False},
    }
