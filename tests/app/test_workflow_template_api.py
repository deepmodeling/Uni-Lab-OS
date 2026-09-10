"""后端形状工作流模板查询接口的合同测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_template_api import create_workflow_template_app
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

NODE_TEMPLATE_A = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_B = "20000000-0000-4000-8000-000000000002"
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
) -> dict[str, Any]:
    """构造一个后端形状节点模板实体。

    参数说明：UUID、动作名、展示名和创建时间用于验证摘要映射、筛选与游标顺序；
    返回值包含 HTTP 列表需要的资源模板摘要元数据。
    """

    return {
        "uuid": template_uuid,
        "create_time": create_time,
        "update_time": create_time,
        "description": None,
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
    """列表使用游标摘要，详情在同一响应内嵌句柄模板。"""

    client = _client()
    first_page = client.get(
        "/api/v1/workflow-node-templates",
        params={"limit": 1},
    )

    assert first_page.status_code == 200
    # ``first_data`` 是同一不可变目录代际的游标页及权威元数据。
    first_data = first_page.json()["data"]
    assert first_data["authority"] == {"authority_id": "local", "kind": "local"}
    assert first_data["catalog_fingerprint"].startswith("sha256:")
    catalog_fingerprint = first_data["catalog_fingerprint"]
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
            "next_cursor_uuid": NODE_TEMPLATE_B,
        },
    } == {
        "code": 0,
        "data": {
            key: value
            for key, value in first_data.items()
            if key not in {"authority", "catalog_fingerprint"}
        },
    }

    second_page = client.get(
        "/api/v1/workflow-node-templates",
        params={"limit": 1, "cursor_uuid": NODE_TEMPLATE_B, "keyword": "输"},
    )
    assert [item["uuid"] for item in second_page.json()["data"]["items"]] == [
        NODE_TEMPLATE_A
    ]

    detail = client.get(f"/api/v1/workflow-node-templates/{NODE_TEMPLATE_A}")
    assert detail.status_code == 200
    assert detail.json()["code"] == 0
    assert detail.json()["data"]["authority"] == first_data["authority"]
    assert detail.json()["data"]["catalog_fingerprint"] == catalog_fingerprint
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
    """非法查询身份和未知模板必须使用 Backend HTTP 200 业务错误外壳。"""

    client = _client()
    invalid_path = client.get("/api/v1/workflow-node-templates/not-a-uuid")
    nil_cursor = client.get(
        "/api/v1/workflow-node-templates",
        params={"cursor_uuid": "00000000-0000-0000-0000-000000000000"},
    )
    missing_template = client.get(
        "/api/v1/workflow-node-templates/ffffffff-ffff-4fff-8fff-ffffffffffff"
    )

    assert invalid_path.status_code == 200
    assert invalid_path.json()["code"] == 1000
    assert nil_cursor.status_code == 200
    assert nil_cursor.json()["code"] == 1000
    assert missing_template.status_code == 200
    assert missing_template.json()["code"] == 5001
    assert missing_template.json()["error"]["msg"]
