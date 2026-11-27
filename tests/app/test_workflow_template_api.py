"""后端形状工作流模板查询接口的合同测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_template_api import create_workflow_template_app
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

NODE_TEMPLATE_A = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_B = "20000000-0000-4000-8000-000000000002"
CONDITION_TEMPLATE = "20000000-0000-4000-8000-000000000003"
REPEAT_TEMPLATE = "20000000-0000-4000-8000-000000000004"
HANDLE_TEMPLATE_A = "30000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"


class SnapshotProvider:
    """为 HTTP 适配器提供一个不会在读取时变化的模板快照。"""

    def __init__(self, snapshot: AuthoringCatalogSnapshot) -> None:
        """保存不可变快照。

        参数说明：``snapshot`` 模拟设备注册表模板投影最近一次完整提交结果。
        """

        self._snapshot = snapshot

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回同一不可变模板快照，不访问设备注册表或网络。"""

        return self._snapshot


def _node(
    *,
    template_uuid: str,
    action_name: str,
    display_name: str,
    create_time: str,
    description: str | None = None,
) -> dict[str, Any]:
    """构造一个后端形状节点模板实体。

    参数说明：UUID、动作名、展示名和创建时间用于验证摘要映射、筛选与游标顺序；
    返回值包含 HTTP 列表需要的资源模板摘要元数据。
    """

    return {
        "uuid": template_uuid,
        "create_time": create_time,
        "update_time": create_time,
        "description": description,
        "meta_data": {
            "unilab": {
                "resource_template": {
                    "uuid": RESOURCE_TEMPLATE_UUID,
                    "name": "pump",
                    "display_name": "注射泵",
                }
            }
        },
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        "name": action_name,
        "display_name": display_name,
        "class": "lab.devices:Pump",
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": {"type": "object"},
        "type": "UniLabJsonCommand",
        "icon": None,
        "header": None,
        "footer": None,
        "node_type": "device_action",
    }


def _control_node(*, template_uuid: str, node_type: str, display_name: str) -> dict[str, Any]:
    """构造一个结构化控制节点模板，供公开目录接口测试使用。

    参数说明：``template_uuid`` 是稳定模板身份，``node_type`` 是调度器控制类型，
    ``display_name`` 是前端展示名称；返回值只包含控制节点所需的目录元数据，
    不伪造普通设备动作句柄。
    """

    return {
        "uuid": template_uuid,
        "create_time": "2026-08-04T00:00:02Z",
        "update_time": "2026-08-04T00:00:02Z",
        "description": "由调度器处理的结构化工作流控制节点。",
        "meta_data": {
            "unilab": {
                "resource_template": {
                    "uuid": RESOURCE_TEMPLATE_UUID,
                    "name": "host_node",
                    "display_name": "工作流控制",
                },
                "executor_kind": node_type,
                "parameter_schema": {
                    "type": "object",
                    "description": "控制节点参数由工作流图保存，并由调度器在运行时校验。",
                },
            }
        },
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        "name": node_type,
        "display_name": display_name,
        "class": f"unilabos.workflow.authoring:{node_type}",
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": node_type,
        "node_type": node_type,
    }


def _client() -> TestClient:
    """创建含两个节点模板和一个句柄模板的聚焦 HTTP 客户端。"""

    snapshot = AuthoringCatalogSnapshot.from_entities(
        [
            _node(
                template_uuid=NODE_TEMPLATE_A,
                action_name="transfer",
                display_name="输送",
                create_time="2026-08-04T00:00:00Z",
            ),
            _node(
                template_uuid=NODE_TEMPLATE_B,
                action_name="mix",
                display_name="混合",
                create_time="2026-08-04T00:00:01Z",
            ),
        ],
        [
            {
                "uuid": HANDLE_TEMPLATE_A,
                "create_time": "2026-08-04T00:00:00Z",
                "update_time": "2026-08-04T00:00:00Z",
                "description": None,
                "meta_data": {},
                "workflow_node_template_uuid": NODE_TEMPLATE_A,
                "handle_key": "plate",
                "io_type": "target",
                "display_name": "反应板",
                "type": "ResourceSlot",
                "required": True,
                "data_source": None,
                "data_key": "plate",
            }
        ],
    )
    return TestClient(create_workflow_template_app(SnapshotProvider(snapshot)))


def test_workflow_template_list_and_detail_match_backend_shape() -> None:
    """列表使用页码摘要，详情在同一响应内嵌句柄模板。"""

    client = _client()
    first_page = client.get(
        "/api/v1/workflow-node-templates",
        params={"page": 1, "page_size": 1},
    )

    assert first_page.status_code == 200
    # ``first_data`` 与 Go Backend 共用同一 has-more 页码外壳。
    first_data = first_page.json()["data"]
    assert {
        "code": first_page.json()["code"],
        "data": {
            "items": [
                {
                    "uuid": NODE_TEMPLATE_B,
                    "name": "mix",
                    "display_name": "混合",
                    "type": "UniLabJsonCommand",
                    "node_type": "device_action",
                    "resource_template": {
                        "uuid": RESOURCE_TEMPLATE_UUID,
                        "name": "pump",
                        "display_name": "注射泵",
                    },
                }
            ],
            "has_more": True,
            "page": 1,
            "page_size": 1,
        },
    } == {"code": 0, "data": first_data}

    second_page = client.get(
        "/api/v1/workflow-node-templates",
        params={"page": 1, "page_size": 1, "keyword": "输"},
    )
    assert [item["uuid"] for item in second_page.json()["data"]["items"]] == [
        NODE_TEMPLATE_A
    ]

    detail = client.get(f"/api/v1/workflow-node-templates/{NODE_TEMPLATE_A}")
    assert detail.status_code == 200
    assert detail.json()["code"] == 0
    assert set(detail.json()["data"]) == {"template", "handles"}
    assert detail.json()["data"]["template"]["uuid"] == NODE_TEMPLATE_A
    assert detail.json()["data"]["handles"] == [
        {
            "uuid": HANDLE_TEMPLATE_A,
            "create_time": "2026-08-04T00:00:00Z",
            "update_time": "2026-08-04T00:00:00Z",
            "meta_data": {},
            "workflow_node_template_uuid": NODE_TEMPLATE_A,
            "handle_key": "plate",
            "io_type": "target",
            "display_name": "反应板",
            "type": "ResourceSlot",
            "required": True,
            "data_key": "plate",
        }
    ]


def test_workflow_template_query_uses_backend_business_errors() -> None:
    """非法查询身份和未知模板必须返回稳定的业务错误码和可读说明。"""

    client = _client()
    invalid_path = client.get("/api/v1/workflow-node-templates/not-a-uuid")
    invalid_resource_template = client.get(
        "/api/v1/workflow-node-templates",
        params={"resource_template_uuid": "not-a-uuid"},
    )
    missing_template = client.get(
        "/api/v1/workflow-node-templates/ffffffff-ffff-4fff-8fff-ffffffffffff"
    )

    assert invalid_path.status_code == 200
    assert invalid_path.json()["code"] == 1000
    assert "模板查询参数不符合要求" in invalid_path.json()["error"]["msg"]
    assert invalid_resource_template.status_code == 200
    assert invalid_resource_template.json()["code"] == 1000
    assert "模板查询参数不符合要求" in invalid_resource_template.json()["error"]["msg"]
    assert missing_template.status_code == 200
    assert missing_template.json()["code"] == 3002
    assert missing_template.json()["error"]["msg"] == (
        "工作流节点模板不存在或已被删除"
        "（模板 UUID：ffffffff-ffff-4fff-8fff-ffffffffffff）"
    )


def test_workflow_template_list_exposes_scheduler_control_nodes() -> None:
    """默认模板目录应同时暴露条件和循环节点，供实验操作前端选择和说明参数。"""

    snapshot = AuthoringCatalogSnapshot.from_entities(
        [
            _node(
                template_uuid=NODE_TEMPLATE_A,
                action_name="transfer",
                display_name="输送",
                create_time="2026-08-04T00:00:00Z",
            ),
            _control_node(
                template_uuid=CONDITION_TEMPLATE,
                node_type="condition",
                display_name="条件",
            ),
            _control_node(
                template_uuid=REPEAT_TEMPLATE,
                node_type="repeat_until",
                display_name="重复直到",
            ),
            _control_node(
                template_uuid="11111111-1111-4111-8111-111111111111",
                node_type="material_io",
                display_name="Material IO",
            ),
        ],
        [],
    )
    client = TestClient(create_workflow_template_app(SnapshotProvider(snapshot)))

    response = client.get(
        "/api/v1/workflow-node-templates",
        params={"page": 1, "page_size": 20},
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["node_type"] for item in items} == {
        "device_action",
        "condition",
        "repeat_until",
        "material_io",
    }

    condition = client.get(f"/api/v1/workflow-node-templates/{CONDITION_TEMPLATE}")
    assert condition.status_code == 200
    condition_data = condition.json()["data"]
    assert condition_data["handles"] == []
    assert condition_data["template"]["meta_data"]["unilab"]["executor_kind"] == "condition"
    assert condition_data["template"]["meta_data"]["unilab"]["parameter_schema"]["type"] == "object"

    repeat = client.get(
        "/api/v1/workflow-node-templates",
        params={"node_type": "repeat_until", "page": 1, "page_size": 20},
    )
    assert [item["uuid"] for item in repeat.json()["data"]["items"]] == [REPEAT_TEMPLATE]


def test_node_template_summary_carries_action_description() -> None:
    """列表摘要带上设备包写的业务说明；没有说明时省略该字段。"""

    snapshot = AuthoringCatalogSnapshot.from_entities(
        [
            _node(
                template_uuid=NODE_TEMPLATE_A,
                action_name="clamp",
                display_name="clamp",
                create_time="2026-09-21T10:00:00Z",
                description="收集-夹持气缸夹紧",
            ),
            _node(
                template_uuid=NODE_TEMPLATE_B,
                action_name="extend",
                display_name="extend",
                create_time="2026-09-21T10:00:01Z",
            ),
        ],
        [],
    )
    client = TestClient(create_workflow_template_app(SnapshotProvider(snapshot)))

    items = client.get("/api/v1/workflow-node-templates").json()["data"]["items"]
    by_name = {item["name"]: item for item in items}
    assert by_name["clamp"]["description"] == "收集-夹持气缸夹紧"
    assert "description" not in by_name["extend"]
