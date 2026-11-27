"""Edge 设备目录（Device Catalog）公共投影测试。"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from unilabos.app.device_action_capabilities import (
    project_device_action_capabilities,
)
from unilabos.app.web.device_catalog import (
    DEVICE_OVERVIEW_MATERIALS_SQL,
    project_backend_device_overviews,
    project_device_catalog,
)
from unilabos.registry.template_identity import action_template_uuid


class _Content:
    """测试用资源节点内容。"""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def model_dump(self, *, by_alias: bool) -> dict[str, object]:
        assert by_alias is True
        return dict(self._values)


class _AsyncCommand:
    """验证运行时 ROS 类型对象按类名投影。"""


def test_project_device_action_capabilities_filters_transport_endpoints() -> None:
    assert project_device_action_capabilities(
        {
            "_execute_driver_command": {"type": "StrSingleInput"},
            "_execute_driver_command_async": {"type": "StrSingleInput"},
            "pick": {"type": "UniLabJsonCommand"},
            "transfer": {"type": _AsyncCommand},
            "missing-type": {},
        }
    ) == [
        {"name": "pick", "type": "UniLabJsonCommand"},
        {"name": "transfer", "type": "_AsyncCommand"},
    ]


def test_project_device_catalog_joins_resource_online_and_registry_facts() -> None:
    """设备实例、ROS 在线事实和注册表动作合同汇合为前端唯一目录。"""

    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": "10000000-0000-4000-8000-000000000001",
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.lab.pump",
                    }
                )
            ),
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "rack-1",
                        "uuid": "10000000-0000-4000-8000-000000000002",
                        "name": "物料架",
                        "type": "warehouse",
                        "class": "community.lab.rack",
                    }
                )
            ),
        ]
    )
    registry_devices = [
        {
            "id": "community.lab.pump",
            "displayname": "泵类型",
            "class": {
                "status_types": {"pressure": "float"},
                "action_value_mappings": {
                    "dose": {
                        "display_name": "加液",
                        "type": "Dose",
                        "schema": {
                            "properties": {
                                "goal": {
                                    "type": "object",
                                    "properties": {"volume": {"type": "number"}},
                                },
                                "result": {
                                    "type": "object",
                                    "properties": {"success": {"type": "boolean"}},
                                },
                            }
                        },
                    },
                    "_execute_driver_command": {"type": "Internal"},
                },
            },
        }
    ]
    online_devices = {
        "pump-1": {
            "device_key": "/devices/pump-1/pump-1",
            "namespace": "/devices/pump-1",
            "machine_name": "本地",
        }
    }

    result = project_device_catalog(
        resources=resources,
        registry_devices=registry_devices,
        online_devices=online_devices,
        generated_at=123.0,
    )

    assert result == {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": 123.0,
        "items": [
            {
                "id": "pump-1",
                "materialUuid": "10000000-0000-4000-8000-000000000001",
                "deviceTypeId": "community.lab.pump",
                "deviceKey": "/devices/pump-1/pump-1",
                "namespace": "/devices/pump-1",
                "name": "一号泵",
                "online": True,
                "stateSchema": {"pressure": {"type": "number"}},
                "actions": [
                    {
                        "id": "dose",
                        "actionRef": "pump-1.dose",
                        "name": "加液",
                        "typeName": "Dose",
                        "riskLevel": "normal",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"volume": {"type": "number"}},
                        },
                        "outputSchema": {
                            "type": "object",
                            "properties": {"success": {"type": "boolean"}},
                        },
                        "busy": False,
                        "currentJobId": None,
                    }
                ],
            }
        ],
    }


def test_project_backend_device_overviews_matches_go_backend_shape() -> None:
    """Local 设备列表返回可调度读模型，并带上动作模板身份。"""

    edge_uuid = "20000000-0000-4000-8000-000000000001"
    material_uuid = "10000000-0000-4000-8000-000000000001"
    result = project_backend_device_overviews(
        registration={
            "edge_uuid": edge_uuid,
            "connected": True,
            "created_at": 0.0,
            "updated_at": 1.0,
            "devices": [
                {
                    "local_id": "pump-1",
                    "material_uuid": material_uuid,
                    "name": "一号泵",
                    "actions": [
                        {"name": "dose", "type": "Dose"},
                        {"name": "", "type": "ignored"},
                    ],
                },
                {
                    "local_id": "missing-material",
                    "material_uuid": "30000000-0000-4000-8000-000000000001",
                    "actions": [],
                },
            ],
        },
        materials=[
            {
                "uuid": material_uuid,
                "create_time": "2026-08-14T00:00:00.000Z",
                "update_time": "2026-08-14T00:01:00.000Z",
                "description": None,
                "meta_data": '{"source":"resource-tree-set"}',
                "resource_template_uuid": "40000000-0000-4000-8000-000000000001",
                "parent_uuid": None,
                "class": "community.lab.pump",
                "type": "device",
                "barcode": "pump-1",
                "name": "一号泵",
                "config": '{"port":"loopback"}',
                "data": "{}",
                "revision": 3,
            }
        ],
    )

    assert len(result) == 1
    overview = result[0]
    assert set(overview) == {
        "binding",
        "material",
        "online",
        "edge_status",
        "dispatchable",
        "actions",
    }
    assert overview["binding"] == {
        "uuid": "870b3ca6-41f3-546f-90c2-f2d20c1b78fe",
        "create_time": "1970-01-01T00:00:00.000000Z",
        "update_time": "1970-01-01T00:00:01.000000Z",
        "meta_data": {},
        "edge_uuid": edge_uuid,
        "material_uuid": material_uuid,
        "local_id": "pump-1",
        "name": "一号泵",
    }
    assert overview["material"] == {
        "uuid": material_uuid,
        "create_time": "2026-08-14T00:00:00.000Z",
        "update_time": "2026-08-14T00:01:00.000Z",
        "meta_data": {"source": "resource-tree-set"},
        "resource_template_uuid": "40000000-0000-4000-8000-000000000001",
        "class": "community.lab.pump",
        "type": "device",
        "barcode": "pump-1",
        "name": "一号泵",
        "config": {"port": "loopback"},
        "data": {},
        "revision": 3,
    }
    assert overview["online"] is True
    assert overview["edge_status"] == "online"
    assert overview["dispatchable"] is True
    assert overview["actions"] == [
        {
            "name": "dose",
            "type": "Dose",
            "workflow_node_template_uuid": action_template_uuid(
                "community.lab.pump",
                "dose",
            ),
        }
    ]


def test_project_backend_device_overviews_blocks_offline_or_held_devices() -> None:
    """会话断开或设备自报阻断时，设备页不能把设备当成可调度。"""

    material_uuid = "10000000-0000-4000-8000-000000000001"
    material = {
        "uuid": material_uuid,
        "create_time": "2026-08-14T00:00:00.000Z",
        "update_time": "2026-08-14T00:01:00.000Z",
        "meta_data": "{}",
        "resource_template_uuid": "40000000-0000-4000-8000-000000000001",
        "class": "community.lab.pump",
        "type": "device",
        "barcode": "pump-1",
        "name": "一号泵",
        "config": "{}",
        "data": "{}",
        "revision": 1,
    }
    registration = {
        "edge_uuid": "20000000-0000-4000-8000-000000000001",
        "connected": False,
        "created_at": 0.0,
        "updated_at": 1.0,
        "devices": [
            {
                "local_id": "pump-1",
                "material_uuid": material_uuid,
                "name": "一号泵",
                "online": True,
                "actions": [{"name": "dose", "type": "Dose"}],
            }
        ],
    }

    offline = project_backend_device_overviews(
        registration=registration,
        materials=[material],
    )[0]
    assert offline["online"] is False
    assert offline["dispatchable"] is False

    registration["connected"] = True
    registration["devices"][0]["dispatch_block_reason"] = "存在未核实命令"
    blocked = project_backend_device_overviews(
        registration=registration,
        materials=[material],
    )[0]
    assert blocked["online"] is True
    assert blocked["dispatchable"] is False
    assert blocked["binding"]["dispatch_block_reason"] == "存在未核实命令"


def test_device_overview_materials_keep_devices_without_sqlite_templates() -> None:
    """内存设备模板没有 SQLite 行时，设备物料仍进入设备列表。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE material (
            uuid TEXT PRIMARY KEY,
            deleted_at TEXT,
            type TEXT,
            resource_template_uuid TEXT,
            class TEXT,
            name TEXT,
            aggregate_version INTEGER
        );
        CREATE TABLE resource_template (
            uuid TEXT PRIMARY KEY,
            resource_type TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO resource_template(uuid, resource_type) VALUES (?, ?)",
        ("bottle-template", "resource"),
    )
    connection.execute(
        """
        INSERT INTO material(
            uuid, deleted_at, type, resource_template_uuid, class, name
        ) VALUES (?, NULL, ?, ?, ?, ?)
        """,
        (
            "bottle",
            "container",
            "bottle-template",
            "community.lab.bottle",
            "试剂瓶",
        ),
    )
    connection.execute(
        """
        INSERT INTO material(
            uuid, deleted_at, type, resource_template_uuid, class, name
        ) VALUES (?, NULL, ?, ?, ?, ?)
        """,
        (
            "stirrer",
            "device",
            "missing-device-template",
            "community.lab.stirrer",
            "磁搅",
        ),
    )
    rows = [dict(row) for row in connection.execute(DEVICE_OVERVIEW_MATERIALS_SQL)]

    assert [row["uuid"] for row in rows] == ["bottle", "stirrer"]
    assert rows[1]["type"] == "device"
    assert rows[1]["class"] == "community.lab.stirrer"


def test_project_backend_device_overviews_does_not_guess_without_registration() -> None:
    assert project_backend_device_overviews(registration=None, materials=[]) == []


def test_project_device_action_capabilities_reports_description_and_display_name() -> None:
    """设备包 @action(description=...) 的业务说明随能力上报；与名称相同的显示名省略。"""
    assert project_device_action_capabilities(
        {
            "clamp": {
                "type": "UniLabJsonCommand",
                "displayname": "clamp",
                "description": "收集-夹持气缸夹紧",
            },
            "extend": {
                "type": "UniLabJsonCommand",
                "displayname": "收集伸出",
                "description": "  ",
            },
            "retract": {"type": "UniLabJsonCommand"},
        }
    ) == [
        {"name": "clamp", "type": "UniLabJsonCommand", "description": "收集-夹持气缸夹紧"},
        {"name": "extend", "type": "UniLabJsonCommand", "display_name": "收集伸出"},
        {"name": "retract", "type": "UniLabJsonCommand"},
    ]
