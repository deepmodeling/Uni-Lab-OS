"""UniLabOS 自带、可由前端引用的受控 Workflow 模板目录。"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping
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

WORKBENCH_RESOURCE_TEMPLATE_UUID = _identity("resource:virtual-workbench")
WORKBENCH_TEMPLATE_UUIDS: Mapping[str, str] = MappingProxyType(
    {
        "prepare": _identity("node:virtual-workbench:prepare-materials:v1"),
        "move": _identity(
            "node:virtual-workbench:move-to-heating-station:v1"
        ),
        "heat": _identity("node:virtual-workbench:start-heating:v1"),
        "output": _identity("node:virtual-workbench:move-to-output:v1"),
    }
)
WORKBENCH_READY_HANDLE_UUIDS: Mapping[str, str] = MappingProxyType(
    {
        f"{phase}_{io_type}": _identity(
            f"handle:virtual-workbench:{phase}:ready:{io_type}"
        )
        for phase in ("prepare", "move", "heat", "output")
        for io_type in ("source", "target")
    }
)
WORKBENCH_DATA_HANDLE_UUIDS: Mapping[str, str] = MappingProxyType(
    {
        **{
            f"prepare_material_{index}_source": _identity(
                "handle:virtual-workbench:prepare-materials:"
                f"channel_{index}:source"
            )
            for index in range(1, 6)
        },
        "move_material_target": _identity(
            "handle:virtual-workbench:move-to-heating-station:"
            "material_input:target"
        ),
        "move_station_source": _identity(
            "handle:virtual-workbench:move-to-heating-station:"
            "heating_station_output:source"
        ),
        "move_material_source": _identity(
            "handle:virtual-workbench:move-to-heating-station:"
            "material_number_output:source"
        ),
        "heat_station_target": _identity(
            "handle:virtual-workbench:start-heating:station_id_input:target"
        ),
        "heat_material_target": _identity(
            "handle:virtual-workbench:start-heating:"
            "material_number_input:target"
        ),
        "heat_station_source": _identity(
            "handle:virtual-workbench:start-heating:"
            "heating_done_station:source"
        ),
        "heat_material_source": _identity(
            "handle:virtual-workbench:start-heating:"
            "heating_done_material:source"
        ),
        "output_station_target": _identity(
            "handle:virtual-workbench:move-to-output:"
            "output_station_input:target"
        ),
        "output_material_target": _identity(
            "handle:virtual-workbench:move-to-output:"
            "output_material_input:target"
        ),
    }
)


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

    def workbench_meta(action_name: str) -> dict[str, Any]:
        return {
            "unilab": {
                "builtin": True,
                "version": 1,
                "registry_device_id": "virtual_workbench",
                "registry_action": action_name,
                "supported_backends": ["hostlink", "ros2"],
            }
        }

    workbench_class = (
        "unilabos.devices.virtual.workbench:VirtualWorkbench"
    )
    nodes.extend(
        [
            WorkflowNodeTemplateWrite.model_validate(
                {
                    "uuid": WORKBENCH_TEMPLATE_UUIDS["prepare"],
                    "resource_template_uuid": WORKBENCH_RESOURCE_TEMPLATE_UUID,
                    "name": "auto-prepare_materials",
                    "display_name": "准备工作台物料",
                    "description": "生成 A1-A5 物料编号并输出独立数据 Handle",
                    "class": workbench_class,
                    "goal": {"count": "int"},
                    "goal_default": {"count": 5},
                    "feedback": {},
                    "result": {
                        "success": "bool",
                        "count": "int",
                        "material_1": "int",
                        "material_2": "int",
                        "material_3": "int",
                        "material_4": "int",
                        "material_5": "int",
                        "message": "str",
                    },
                    "schema": {
                        "type": "object",
                        "properties": {
                            "count": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            }
                        },
                        "required": [],
                    },
                    "type": "action",
                    "node_type": "device_action",
                    "meta_data": workbench_meta("auto-prepare_materials"),
                }
            ),
            WorkflowNodeTemplateWrite.model_validate(
                {
                    "uuid": WORKBENCH_TEMPLATE_UUIDS["move"],
                    "resource_template_uuid": WORKBENCH_RESOURCE_TEMPLATE_UUID,
                    "name": "auto-move_to_heating_station",
                    "display_name": "移入加热工位",
                    "description": "用机械臂把物料移入一个空闲加热工位",
                    "class": workbench_class,
                    "goal": {"material_number": "int"},
                    "goal_default": {},
                    "feedback": {},
                    "result": {
                        "success": "bool",
                        "station_id": "int",
                        "material_id": "str",
                        "material_number": "int",
                        "message": "str",
                    },
                    "schema": {
                        "type": "object",
                        "properties": {
                            "material_number": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            }
                        },
                        "required": ["material_number"],
                    },
                    "type": "action",
                    "node_type": "device_action",
                    "meta_data": workbench_meta(
                        "auto-move_to_heating_station"
                    ),
                }
            ),
            WorkflowNodeTemplateWrite.model_validate(
                {
                    "uuid": WORKBENCH_TEMPLATE_UUIDS["heat"],
                    "resource_template_uuid": WORKBENCH_RESOURCE_TEMPLATE_UUID,
                    "name": "auto-start_heating",
                    "display_name": "启动工作台加热",
                    "description": "启动指定工位的加热程序，允许多个工位并行",
                    "class": workbench_class,
                    "goal": {
                        "station_id": "int",
                        "material_number": "int",
                    },
                    "goal_default": {},
                    "feedback": {},
                    "result": {
                        "success": "bool",
                        "station_id": "int",
                        "material_id": "str",
                        "material_number": "int",
                        "message": "str",
                    },
                    "schema": {
                        "type": "object",
                        "properties": {
                            "station_id": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 3,
                            },
                            "material_number": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            },
                        },
                        "required": ["station_id", "material_number"],
                    },
                    "type": "action",
                    "node_type": "device_action",
                    "meta_data": workbench_meta("auto-start_heating"),
                }
            ),
            WorkflowNodeTemplateWrite.model_validate(
                {
                    "uuid": WORKBENCH_TEMPLATE_UUIDS["output"],
                    "resource_template_uuid": WORKBENCH_RESOURCE_TEMPLATE_UUID,
                    "name": "auto-move_to_output",
                    "display_name": "移至工作台输出位",
                    "description": "用机械臂把完成加热的物料移至 Cn 输出位",
                    "class": workbench_class,
                    "goal": {
                        "station_id": "int",
                        "material_number": "int",
                    },
                    "goal_default": {},
                    "feedback": {},
                    "result": {
                        "success": "bool",
                        "station_id": "int",
                        "material_id": "str",
                        "output_position": "str",
                        "message": "str",
                    },
                    "schema": {
                        "type": "object",
                        "properties": {
                            "station_id": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 3,
                            },
                            "material_number": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            },
                        },
                        "required": ["station_id", "material_number"],
                    },
                    "type": "action",
                    "node_type": "device_action",
                    "meta_data": workbench_meta("auto-move_to_output"),
                }
            ),
        ]
    )

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

    def data_handle(
        *,
        uuid: str,
        template_uuid: str,
        handle_key: str,
        io_type: str,
        display_name: str,
        data_type: str,
        data_key: str,
    ) -> WorkflowHandleTemplateWrite:
        is_target = io_type == "target"
        return WorkflowHandleTemplateWrite(
            uuid=uuid,
            workflow_node_template_uuid=template_uuid,
            handle_key=handle_key,
            io_type=io_type,
            display_name=display_name,
            description="工作台动作的 typed data flow",
            type=data_type,
            required=is_target,
            data_source="handle" if is_target else "executor",
            data_key=data_key,
            meta_data={"unilab": {"builtin": True, "data_flow": True}},
        )

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
    for phase, template_uuid in WORKBENCH_TEMPLATE_UUIDS.items():
        handles.extend(
            ready_handles(
                template_uuid,
                WORKBENCH_READY_HANDLE_UUIDS[f"{phase}_source"],
                WORKBENCH_READY_HANDLE_UUIDS[f"{phase}_target"],
            )
        )

    handles.extend(
        data_handle(
            uuid=WORKBENCH_DATA_HANDLE_UUIDS[
                f"prepare_material_{index}_source"
            ],
            template_uuid=WORKBENCH_TEMPLATE_UUIDS["prepare"],
            handle_key=f"channel_{index}",
            io_type="source",
            display_name=f"实验 {index}",
            data_type="workbench_material",
            data_key=f"material_{index}",
        )
        for index in range(1, 6)
    )
    handles.extend(
        [
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["move_material_target"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["move"],
                handle_key="material_input",
                io_type="target",
                display_name="物料编号",
                data_type="workbench_material",
                data_key="material_number",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["move_station_source"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["move"],
                handle_key="heating_station_output",
                io_type="source",
                display_name="加热台 ID",
                data_type="workbench_station",
                data_key="station_id",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["move_material_source"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["move"],
                handle_key="material_number_output",
                io_type="source",
                display_name="物料编号",
                data_type="workbench_material",
                data_key="material_number",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["heat_station_target"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["heat"],
                handle_key="station_id_input",
                io_type="target",
                display_name="加热台 ID",
                data_type="workbench_station",
                data_key="station_id",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["heat_material_target"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["heat"],
                handle_key="material_number_input",
                io_type="target",
                display_name="物料编号",
                data_type="workbench_material",
                data_key="material_number",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["heat_station_source"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["heat"],
                handle_key="heating_done_station",
                io_type="source",
                display_name="完成加热的工位",
                data_type="workbench_station",
                data_key="station_id",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["heat_material_source"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["heat"],
                handle_key="heating_done_material",
                io_type="source",
                display_name="完成加热的物料",
                data_type="workbench_material",
                data_key="material_number",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["output_station_target"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["output"],
                handle_key="output_station_input",
                io_type="target",
                display_name="加热台 ID",
                data_type="workbench_station",
                data_key="station_id",
            ),
            data_handle(
                uuid=WORKBENCH_DATA_HANDLE_UUIDS["output_material_target"],
                template_uuid=WORKBENCH_TEMPLATE_UUIDS["output"],
                handle_key="output_material_input",
                io_type="target",
                display_name="物料编号",
                data_type="workbench_material",
                data_key="material_number",
            ),
        ]
    )
    return nodes, handles


__all__ = [
    "BUILTIN_CATALOG_AUTHORITY",
    "HEAT_READY_SOURCE_UUID",
    "HEAT_READY_TARGET_UUID",
    "HEAT_SITE_TEMPLATE_UUID",
    "MATERIAL_TRANSFER_TEMPLATE_UUID",
    "TRANSFER_READY_SOURCE_UUID",
    "TRANSFER_READY_TARGET_UUID",
    "WORKBENCH_DATA_HANDLE_UUIDS",
    "WORKBENCH_READY_HANDLE_UUIDS",
    "WORKBENCH_RESOURCE_TEMPLATE_UUID",
    "WORKBENCH_TEMPLATE_UUIDS",
    "builtin_workflow_catalog",
]
