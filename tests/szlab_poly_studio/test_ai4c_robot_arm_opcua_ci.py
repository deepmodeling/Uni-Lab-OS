from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from scripts.run_workflow_local import (
    WorkflowLogger,
    WorkflowNode,
    build_execution_order,
    create_local_devices,
    load_runtime_config,
    run_nodes,
)
from scripts.workflow_ui import build_local_device_graph, load_preset


LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
AI4C_FLOW_PATH = REPO_ROOT / "tests" / "psuedo_devices" / "ai4c_robot_arm" / "pick_place_flow.json"
AI4C_CSV_PATH = REPO_ROOT / "tests" / "szlab_poly_studio" / "fixtures" / "ai4c_robot_arm_ci_nodes.csv"
AI4C_RUNTIME_CONFIG = REPO_ROOT / "tests" / "szlab_poly_studio" / "runtime_configs" / "ai4c_runtime.json"

AI4C_VARIABLES = [
    "Robotic_Arm_Idle",
    "Robotic_Arm_Action_Complete",
    "Robotic_Arm_Target_Position_Code",
    "Robotic_Arm_Target_Pick_Place_Code",
    "Robotic_Arm_Action_Code",
    "Robotic_Arm_Action_Trigger",
    "Well_Plate_Loading_Rack_InPut[0]",
    "Pipetting_Station_Occupied",
]

AI4C_INITIAL_VALUES = {
    "Robotic_Arm_Idle": True,
    "Robotic_Arm_Action_Complete": False,
    "Robotic_Arm_Action_Trigger": False,
    "Well_Plate_Loading_Rack_InPut[0]": True,
    "Pipetting_Station_Occupied": False,
}


def _ci_log(message: str, *args: Any) -> None:
    if args:
        message = message % args
    print(f"[ai4c_robot_arm_ci] {message}", flush=True)
    LOGGER.info(message)


def _load_action_flow(flow_path: Path) -> dict[str, Any]:
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    rules = flow.get("rules") or []
    if len(rules) != 1:
        raise ValueError(f"AI4C action flow 应只包含一个 rule，当前为 {len(rules)} 个")
    actions = rules[0].get("actions") or []
    if not actions:
        raise ValueError("AI4C action flow 缺少 actions")
    return flow


def _workflow_from_action_flow(flow: dict[str, Any]) -> dict[str, Any]:
    rule = flow["rules"][0]
    action_items = sorted(
        [item["action"] for item in rule["actions"]],
        key=lambda item: int(item.get("index", 0)),
    )
    nodes = [
        {
            "uuid": str(item.get("workflow_node_id") or f"ai4c-step-{index:03d}"),
            "name": f"auto-{item['method']}",
            "device_name": str(item.get("device_id") or "AI4C_robot_arm"),
            "param": dict(item.get("params") or {}),
        }
        for index, item in enumerate(action_items, start=1)
    ]
    edges = [
        {
            "source_node_uuid": nodes[index]["uuid"],
            "target_node_uuid": nodes[index + 1]["uuid"],
        }
        for index in range(len(nodes) - 1)
    ]
    return {"name": flow.get("name", "ai4c_robot_arm_pick_place"), "nodes": nodes, "edges": edges}


def _write_temp_json(data: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        return Path(handle.name)


def _disconnect_devices(devices: dict[str, Any]) -> None:
    for device in devices.values():
        if hasattr(device, "disconnect"):
            device.disconnect()


def _assert_ai4c_opcua_nodes_ready(plc: Any, url: str) -> None:
    probe = plc.get_variables(["Robotic_Arm_Idle"], use_cache=False)
    status = probe.get("Robotic_Arm_Idle")
    if isinstance(status, dict) and status.get("success") is False:
        raise RuntimeError(
            "AI4C OPC UA 服务缺少关键节点 Robotic_Arm_Idle: "
            f"url={url}, csv={AI4C_CSV_PATH}, error={status.get('error')}"
        )


class TestAI4CRobotArmOpcUaDevice:
    """CI 中针对 AI4C robot arm device 的真实 OPC UA action 流程测试。"""

    @staticmethod
    def opcua_url() -> str:
        url = os.environ.get("UNILABOS_TEST_AI4C_OPCUA_URL")
        if not url:
            pytest.skip("需要设置 UNILABOS_TEST_AI4C_OPCUA_URL 才运行 AI4C OPC UA 集成测试")
        return url

    def test_pick_and_place_workflow_against_ai4c_opcua(self) -> None:
        url = self.opcua_url()
        flow = _load_action_flow(AI4C_FLOW_PATH)
        workflow = _workflow_from_action_flow(flow)
        runtime_config = load_runtime_config(AI4C_RUNTIME_CONFIG)
        preset = load_preset("ai4c")
        graph = build_local_device_graph(
            opcua_url=url,
            csv_path=str(AI4C_CSV_PATH),
            use_subscription=False,
            preset=preset,
        )

        graph_path = _write_temp_json(graph)
        devices: dict[str, Any] = {}
        try:
            _ci_log("开始 AI4C robot arm OPC UA 集成测试: url=%s", url)
            _ci_log("Flow: %s", flow["name"])
            for action in flow["rules"][0]["actions"]:
                action_data = action["action"]
                _ci_log(
                    "Action %s: %s.%s(%s)",
                    action_data.get("index"),
                    action_data.get("device_id"),
                    action_data.get("method"),
                    action_data.get("params") or {},
                )

            devices = create_local_devices(
                graph_file=graph_path,
                opcua_url=url,
                csv_path=AI4C_CSV_PATH,
                use_subscription=False,
                plc_action_timeout=30.0,
                runtime_config=runtime_config,
            )
            plc = devices["AI4C_plc"]
            _assert_ai4c_opcua_nodes_ready(plc, url)
            for node_name, value in AI4C_INITIAL_VALUES.items():
                plc.write_variable(node_name, value)
            before = plc.get_variables(AI4C_VARIABLES, use_cache=False)
            _ci_log("AI4C action 前 OPC 状态: %s", before)

            nodes = [
                WorkflowNode(
                    uuid=str(item["uuid"]),
                    name=str(item["name"]),
                    device_name=str(item["device_name"]),
                    param=dict(item.get("param") or {}),
                )
                for item in workflow["nodes"]
            ]
            ordered_nodes = build_execution_order(nodes, workflow["edges"])
            results = run_nodes(ordered_nodes, devices, logger=WorkflowLogger(
                writer=_ci_log), runtime_config=runtime_config)

            after = plc.get_variables(AI4C_VARIABLES, use_cache=False)
            _ci_log("AI4C action 后 OPC 状态: %s", after)
            _ci_log("AI4C action 返回: %s", results)

            assert len(results) == 2
            assert all(item["result"]["success"] is True for item in results)
            assert after["Robotic_Arm_Target_Position_Code"] == 3
            assert after["Robotic_Arm_Target_Pick_Place_Code"] == 1
            assert after["Robotic_Arm_Action_Code"] == 2
            assert after["Robotic_Arm_Action_Trigger"] is False
            assert after["Robotic_Arm_Action_Complete"] is False
        finally:
            _disconnect_devices(devices)
            graph_path.unlink(missing_ok=True)
