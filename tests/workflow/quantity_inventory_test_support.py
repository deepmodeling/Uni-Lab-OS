"""数量型库存测试共享构造器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.models import (
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
TASK_UUID = "30000000-0000-4000-8000-000000000001"
JOB_UUID = "40000000-0000-4000-8000-000000000001"


def _inventory(tmp_path: Path) -> tuple[InventoryStore, str, str, str]:
    """创建一个余量为 10 mL 的活动试剂库存。

    参数：``tmp_path`` 提供隔离数据库目录。返回：库存写权威、容器物料、试剂
    身份和试剂实例 UUID。异常：任一公共 HTTP 创建失败时断言失败。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    client = TestClient(app)
    template_response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "local.workflow_reagent_bottle",
                    "display_name": "工作流试剂瓶",
                    "registry_type": "material",
                    "category": ["container"],
                    "metadata": {"capacity": {"max_volume_ul": 100000}},
                    "model": {},
                    "class": {},
                    "handles": [],
                    "config_info": [],
                    "scene": [],
                    "device_params": {},
                }
            ]
        },
    )
    assert template_response.status_code == 200
    template_uuid = template_response.json()["data"]["templates"][0]["uuid"]
    material_response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": "工作流乙醇瓶",
            "barcode": "WF-ETHANOL-001",
        },
    )
    assert material_response.status_code == 201
    material_uuid = material_response.json()["data"]["uuid"]
    info_response = client.post(
        "/api/v1/reagent-infos",
        json={
            "cas": "64-17-5",
            "name": "乙醇",
            "physical_state": "liquid",
        },
    )
    assert info_response.status_code == 201
    info_uuid = info_response.json()["data"]["uuid"]
    reagent_response = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": material_uuid,
            "reagent_info_uuid": info_uuid,
            "quantity": 10,
            "quantity_unit": "mL",
            "meta_data": {},
        },
    )
    assert reagent_response.status_code == 201
    return store, material_uuid, info_uuid, reagent_response.json()["data"]["uuid"]


def _workflow(
    database: Path,
    *,
    reagent_info_uuid: str,
    material_uuid: str,
) -> tuple[WorkflowStore, dict[str, Any], PreparedTaskInput]:
    """创建一个单动作、消耗 2 mL 指定试剂身份的工作流图。

    参数：``database`` 是工作流库路径；``reagent_info_uuid`` 是逻辑需求身份；
    ``material_uuid`` 是执行设备动作绑定的物料实例。
    返回：工作流写权威、应用图和冻结任务输入。异常：图保存失败原样传播。
    """

    store = WorkflowStore(database)
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="数量型库存合同",
        tags=[],
        description=None,
        meta_data={},
    )
    graph = store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            WorkflowNodeWrite(
                uuid=NODE_UUID,
                name="分液",
                type="device_action",
                action_name="dispense",
                action_type="UniLabJsonCommand",
                param={},
                material_uuid=material_uuid,
            )
        ],
        edges=[],
        inventory_requirements=[
            WorkflowInventoryRequirementWrite(
                uuid="50000000-0000-4000-8000-000000000001",
                consume_node_uuid=NODE_UUID,
                requirement_key="ethanol",
                target_type="reagent_info",
                reagent_info_uuid=reagent_info_uuid,
                required_quantity=2,
                quantity_unit="mL",
                allow_split=False,
            )
        ],
    )
    plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [{"uuid": NODE_UUID, "kind": "device_action", "material_uuid": material_uuid}],
        "handles": [],
        "edges": [],
    }
    prepared = PreparedTaskInput(
        workflow_snapshot=graph,
        resolved_input={},
        execution_plan=plan,
        jobs=[
            {
                "uuid": JOB_UUID,
                "workflow_node_uuid": NODE_UUID,
                "topological_index": 0,
                "executor_kind": "device_action",
                "execution_policy": {},
                "execution_timeout_seconds": 60,
                "param": {},
            }
        ],
    )
    return store, graph, prepared
