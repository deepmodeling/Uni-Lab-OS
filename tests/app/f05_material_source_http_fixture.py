"""F05.4-A 物料来源（MaterialSource）公开 HTTP 测试支持。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.registry.action_template_projection import goal_parameter_schema
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionStore,
)

# 以下 UUID 分别固定节点、模板、连接点和边的测试领域身份。
SOURCE_NODE_UUID = "21000000-0000-4000-8000-000000000404"
ACTION_NODE_UUID = "21000000-0000-4000-8000-000000000405"
SOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000404"
ACTION_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000405"
SOURCE_HANDLE_UUID = "41000000-0000-4000-8000-000000000404"
TARGET_HANDLE_UUID = "41000000-0000-4000-8000-000000000405"
EDGE_UUID = "51000000-0000-4000-8000-000000000404"


class SchedulerGetter:
    """为调度器（Scheduler）HTTP Router 提供具名本地调度器获取器。"""

    def __init__(self, scheduler: EdgeScheduler) -> None:
        """绑定唯一调度器（Scheduler）。

        参数：``scheduler`` 是组合运行时创建的本地调度器。返回无。异常：无；
        获取器不创建第二个调度器实例。
        """

        # ``_scheduler`` 是 Router 与调度桥共享的同一运行时实例。
        self._scheduler = scheduler

    def __call__(self) -> EdgeScheduler:
        """返回已绑定的调度器（Scheduler）。

        参数：无。返回：组合运行时唯一调度器。异常：无。
        """

        return self._scheduler


def action_contract_schema() -> dict[str, Any]:
    """构造注册表持有的完整动作合同（Action Contract）。

    参数：无。返回：要求 ``plate`` 是具体物料引用的动作 Schema envelope。
    异常：无。
    """

    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"},
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    }
                },
                "required": ["plate"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }


def node_templates(
    material_resource_template_uuid: str,
    device_resource_template_uuid: str,
) -> list[dict[str, Any]]:
    """构造物料来源与设备动作的正式节点模板投影。

    参数：两个资源模板 UUID 分别描述来源物料和执行设备类型。返回：显式 UUID
    的物料来源（MaterialSource）和 ``ILab`` 动作模板。异常：无。
    """

    # ``contract_schema`` 是注册表持有的完整动作合同；节点模板仅投影 Goal 子树。
    contract_schema = action_contract_schema()
    return [
        {
            "uuid": SOURCE_TEMPLATE_UUID,
            "resource_template_uuid": material_resource_template_uuid,
            "name": "material_source",
            "display_name": "物料来源",
            "class": "unilabos.workflow.authoring:material_source",
            "description": "声明工作流进入边界的物料来源。",
            "meta_data": {"framework": "material_source"},
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "material_source",
            "node_type": "material_source",
        },
        {
            "uuid": ACTION_TEMPLATE_UUID,
            "resource_template_uuid": device_resource_template_uuid,
            "name": "process_plate",
            "display_name": "处理孔板",
            "class": "tests.f05:FixtureReactor",
            "description": "处理固定孔板。",
            "meta_data": {
                "unilab": {
                    "contract_kind": "typed",
                    "action_contract_schema": contract_schema,
                }
            },
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            # 工作流节点模板只投影 Goal 参数 Schema；完整动作 envelope 仍属于
            # 注册表动作合同（Action Contract），两者不得混写。
            "schema": goal_parameter_schema(contract_schema),
            "type": "UniLabJsonCommand",
            "node_type": "ILab",
        },
    ]


def handle_templates(
    material_resource_template_uuid: str,
    device_resource_template_uuid: str,
) -> list[dict[str, Any]]:
    """构造线性物料占位符（ResourceSlot）连接点投影。

    参数：两个资源模板 UUID 分别参与来源和动作父模板业务键。返回：物料来源
    输出与动作必填输入两个连接点；投影存储解析稳定父模板 UUID。异常：无。
    """

    return [
        {
            "uuid": SOURCE_HANDLE_UUID,
            "node_business_key": (
                material_resource_template_uuid,
                "material_source",
            ),
            "handle_key": "material",
            "io_type": "source",
            "display_name": "物料",
            "type": "ResourceSlot",
            "required": False,
            "data_source": "executor",
            "data_key": "material",
            "meta_data": {"unilab": {"value_schema": {"$slot": "ResourceSlot"}}},
        },
        {
            # ``node_business_key`` 是动作输入连接点（Handle）所属节点模板的
            # 持久业务身份，不是待消费物料的身份。
            "uuid": TARGET_HANDLE_UUID,
            "node_business_key": (
                device_resource_template_uuid,
                "process_plate",
            ),
            "handle_key": "plate",
            "io_type": "target",
            "display_name": "孔板",
            "type": "ResourceSlot",
            "required": True,
            "data_source": "executor",
            "data_key": "plate",
            "meta_data": {"unilab": {"value_schema": {"$slot": "ResourceSlot"}}},
        },
    ]


def graph_payload(
    *,
    material_resource_template_uuid: str,
    material_uuid: str,
    device_material_uuid: str,
    mode: str,
    automatic: bool = False,
    site_uuid: str | None = None,
    slot_uuids: list[str] | None = None,
    custody_policy: str = "task_exclusive",
) -> dict[str, list[dict[str, Any]]]:
    """构造公开图接口使用的真实物料来源工作流图。

    参数：物料资源模板 UUID 限定来源类型；``material_uuid`` 是固定物料或挂载
    物料身份；``device_material_uuid`` 是动作执行设备物料身份；``mode`` 是
    ``existing``/``create_new``。返回：节点与边的完整替换 payload。异常：未知
    模式抛出 ``ValueError``，禁止夹具静默产生歧义选择器。
    """

    if mode not in {"existing", "create_new"}:
        raise ValueError("测试物料来源模式必须是 existing 或 create_new")
    # ``selector_material_uuid`` 仅在 existing 模式固定具体待消费物料身份。
    selector_material_uuid = (
        material_uuid if mode == "existing" and not automatic else None
    )
    return {
        "nodes": [
            {
                "uuid": SOURCE_NODE_UUID,
                "workflow_node_template_uuid": SOURCE_TEMPLATE_UUID,
                "name": "固定孔板来源",
                "type": "material_source",
                "pose": {},
                "param": {
                    "mode": mode,
                    "resource_template_uuid": material_resource_template_uuid,
                    "material_uuid": selector_material_uuid,
                    "mount": {"uuid": material_uuid},
                    "site": site_uuid,
                    "slot_range": slot_uuids,
                    "flow_role": "primary_sample",
                    "custody_policy": custody_policy,
                },
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {},
            },
            {
                "uuid": ACTION_NODE_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "material_uuid": device_material_uuid,
                "name": "处理孔板",
                "type": "ILab",
                "pose": {},
                "param": {},
                "action_name": "process_plate",
                "action_type": "UniLabJsonCommand",
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        # ``executor_binding`` 是执行器分配（ExecutorAssignment）的
                        # 创作期固定输入，调度时不从物料身份反推。
                        "executor_binding": {
                            "mode": "fixed",
                            "device_id": "reactor-a",
                        }
                    }
                },
            },
        ],
        "edges": [
            {
                "uuid": EDGE_UUID,
                "source_node_uuid": SOURCE_NODE_UUID,
                "source_handle_uuid": SOURCE_HANDLE_UUID,
                "target_node_uuid": ACTION_NODE_UUID,
                "target_handle_uuid": TARGET_HANDLE_UUID,
                "meta_data": {},
            }
        ],
    }


def install_applied_graph(
    client: TestClient,
    workflow_store: WorkflowStore,
    *,
    material_resource_template_uuid: str,
    device_resource_template_uuid: str,
    material_uuid: str,
    device_material_uuid: str,
    mode: str,
    automatic: bool = False,
    site_uuid: str | None = None,
    slot_uuids: list[str] | None = None,
    custody_policy: str = "task_exclusive",
) -> str:
    """投影模板并经公开 HTTP 保存真实已应用工作流图。

    参数：客户端调用公开工作流（Workflow）接口；``workflow_store`` 是相同运行时的唯一工作流
    写模型；两个资源模板与两个物料身份分别生成来源和执行设备绑定。返回：公开
    接口生成并被真实图采用的工作流（Workflow）UUID。异常：模板投影、服务端保留元数据播种
    或 HTTP 图校验失败直接传播/断言失败，不替换任何生产私有方法。
    """

    # ``projection_store`` 把模板投影到工作流任务（WorkflowTask）创建将读取的
    # 同一工作流存储（WorkflowStore）。
    projection_store = RegistryTemplateProjectionStore(workflow_store)
    projection_store.replace(
        authority_id="f05-material-source-http",
        node_templates=node_templates(
            material_resource_template_uuid,
            device_resource_template_uuid,
        ),
        handle_templates=handle_templates(
            material_resource_template_uuid,
            device_resource_template_uuid,
        ),
    )
    created = client.post(
        "/api/v1/workflows",
        json={"name": "F05 HTTP 物料来源", "tags": [], "meta_data": {}},
    )
    assert created.status_code == 201
    assert created.json()["code"] == 0
    assert created.json()["data"]["uuid"]
    # ``created_workflow_uuid`` 是公开创建、图保存和工作流任务（WorkflowTask）
    # 创建共享的唯一工作流（Workflow）身份。
    created_workflow_uuid = str(created.json()["data"]["uuid"])

    payload = graph_payload(
        material_resource_template_uuid=material_resource_template_uuid,
        material_uuid=material_uuid,
        device_material_uuid=device_material_uuid,
        mode=mode,
        automatic=automatic,
        site_uuid=site_uuid,
        slot_uuids=slot_uuids,
        custody_policy=custody_policy,
    )
    # ``node_values``/``edge_values`` 先建立服务端拥有的执行器绑定；后续公开 PUT
    # 必须保留而不能接受浏览器篡改该保留元数据。
    node_values = [WorkflowNodeWrite.model_validate(item) for item in payload["nodes"]]
    edge_values = [WorkflowEdgeWrite.model_validate(item) for item in payload["edges"]]
    workflow_store.save_graph(
        created_workflow_uuid,
        revision=1,
        nodes=node_values,
        edges=edge_values,
    )
    applied = client.put(
        f"/api/v1/workflows/{created_workflow_uuid}/graph",
        json={"revision": 2, **payload},
    )
    assert applied.status_code == 200
    assert applied.json()["code"] == 0
    return created_workflow_uuid
