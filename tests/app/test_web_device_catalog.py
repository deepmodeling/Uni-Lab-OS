"""Edge 设备目录（Device Catalog）公共投影测试。"""

from __future__ import annotations

from types import SimpleNamespace

from unilabos.app.web.device_catalog import project_device_catalog


class _Content:
    """测试用资源节点内容。"""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def model_dump(self, *, by_alias: bool) -> dict[str, object]:
        assert by_alias is True
        return dict(self._values)


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
