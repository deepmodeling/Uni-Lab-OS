"""UniLabOS 自带、可由前端引用的受控 Workflow 模板目录。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5

from unilabos.server.workflow.models import (
    WorkflowHandleTemplateWrite,
    WorkflowNodeTemplateWrite,
)


BUILTIN_CATALOG_AUTHORITY = "unilabos.builtin.v1"
_CATALOG_NAMESPACE = UUID("877dcf49-b59f-4e71-8921-b8f18c4b1f1d")


def _identity(name: str) -> str:
    return str(uuid5(_CATALOG_NAMESPACE, name))


HEAT_SITE_RESOURCE_TEMPLATE_UUID = _identity("resource:virtual-heating-platform")
HEAT_SITE_TEMPLATE_UUID = _identity("node:virtual-heating-platform:heat-site:v1")
MATERIAL_TRANSFER_RESOURCE_TEMPLATE_UUID = _identity("resource:materials-v1-authority")
MATERIAL_TRANSFER_TEMPLATE_UUID = _identity("node:materials-v1:transfer:v1")

HEAT_READY_SOURCE_UUID = _identity("handle:heat-site:ready:source")
HEAT_READY_TARGET_UUID = _identity("handle:heat-site:ready:target")
TRANSFER_READY_SOURCE_UUID = _identity("handle:materials-transfer:ready:source")
TRANSFER_READY_TARGET_UUID = _identity("handle:materials-transfer:ready:target")


def builtin_workflow_catalog() -> tuple[
    list[WorkflowNodeTemplateWrite],
    list[WorkflowHandleTemplateWrite],
]:
    """返回稳定 UUID 的原子动作与 Authority operation 模板。"""

    nodes = [
        WorkflowNodeTemplateWrite.model_validate(
            {
                "uuid": HEAT_SITE_TEMPLATE_UUID,
                "resource_template_uuid": HEAT_SITE_RESOURCE_TEMPLATE_UUID,
                "name": "heat_site",
                "display_name": "加热工位",
                "description": "三工位虚拟加热台的原子动作",
                "class": "unilabos.devices.virtual.heating_platform:VirtualHeatingPlatform",
                "goal": {
                    "site_id": "int",
                    "target_temperature_c": "float",
                    "duration_seconds": "float",
                },
                "goal_default": {
                    "site_id": 1,
                    "target_temperature_c": 80.0,
                    "duration_seconds": 0.8,
                },
                "feedback": {
                    "site_id": "int",
                    "temperature_c": "float",
                    "progress": "float",
                },
                "result": {
                    "success": "bool",
                    "material_uuid": "str",
                    "temperature_c": "float",
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        "site_id": {"type": "integer", "minimum": 1, "maximum": 3},
                        "target_temperature_c": {
                            "type": "number",
                            "minimum": -20,
                            "maximum": 250,
                        },
                        "duration_seconds": {
                            "type": "number",
                            "minimum": 0.05,
                            "maximum": 3600,
                        },
                    },
                    "required": [
                        "site_id",
                        "target_temperature_c",
                        "duration_seconds",
                    ],
                },
                "type": "action",
                "node_type": "device_action",
                "meta_data": {"unilab": {"builtin": True, "version": 1}},
            }
        ),
        WorkflowNodeTemplateWrite.model_validate(
            {
                "uuid": MATERIAL_TRANSFER_TEMPLATE_UUID,
                "resource_template_uuid": MATERIAL_TRANSFER_RESOURCE_TEMPLATE_UUID,
                "name": "materials.transfer",
                "display_name": "权威物料转移",
                "description": "materials.v1 commit-before-unload/load 原子操作",
                "class": "unilabos.server.services.materials:MaterialsService",
                "goal": {
                    "source_device_id": "str",
                    "target_device_id": "str",
                    "items": "list",
                },
                "goal_default": {"items": []},
                "feedback": {},
                "result": {
                    "material_uuids": "list",
                    "destination_site_uuids": "list",
                    "materials": "list",
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        "source_device_id": {"type": "string", "minLength": 1},
                        "target_device_id": {"type": "string", "minLength": 1},
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "material_uuid": {"type": "string", "minLength": 1},
                                    "target_material_uuid": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "target_site": {
                                        "oneOf": [
                                            {"type": "integer"},
                                            {"type": "string", "minLength": 1},
                                        ]
                                    },
                                },
                                "required": [
                                    "material_uuid",
                                    "target_material_uuid",
                                ],
                            },
                        },
                    },
                    "required": ["source_device_id", "target_device_id", "items"],
                },
                "type": "operation",
                "node_type": "tool_call",
                "meta_data": {
                    "unilab": {
                        "builtin": True,
                        "authority": "materials.v1",
                        "operation": "transfer_material",
                        "version": 1,
                    }
                },
            }
        ),
    ]

    def ready_handles(
        template_uuid: str,
        source_uuid: str,
        target_uuid: str,
    ) -> list[WorkflowHandleTemplateWrite]:
        common: dict[str, Any] = {
            "workflow_node_template_uuid": template_uuid,
            "handle_key": "ready",
            "display_name": "Ready",
            "description": "仅表达执行依赖，不传输业务正文",
            "type": "default",
            "required": False,
            "data_source": None,
            "data_key": None,
            "meta_data": {"unilab": {"dependency_only": True}},
        }
        return [
            WorkflowHandleTemplateWrite(uuid=source_uuid, io_type="source", **common),
            WorkflowHandleTemplateWrite(uuid=target_uuid, io_type="target", **common),
        ]

    handles = [
        *ready_handles(
            HEAT_SITE_TEMPLATE_UUID,
            HEAT_READY_SOURCE_UUID,
            HEAT_READY_TARGET_UUID,
        ),
        *ready_handles(
            MATERIAL_TRANSFER_TEMPLATE_UUID,
            TRANSFER_READY_SOURCE_UUID,
            TRANSFER_READY_TARGET_UUID,
        ),
    ]
    return nodes, handles


__all__ = [
    "BUILTIN_CATALOG_AUTHORITY",
    "HEAT_READY_SOURCE_UUID",
    "HEAT_READY_TARGET_UUID",
    "HEAT_SITE_TEMPLATE_UUID",
    "MATERIAL_TRANSFER_TEMPLATE_UUID",
    "TRANSFER_READY_SOURCE_UUID",
    "TRANSFER_READY_TARGET_UUID",
    "builtin_workflow_catalog",
]
