"""设备注册表模板投影（Registry Template Projection）的公共行为测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.template_projection import (
    RegistryTemplateProjection,
    RegistryTemplateProjectionError,
)
from unilabos.workflow.authoring_kernel import AuthoringCatalogError
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.store import WorkflowStore

RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
ALLOWED_MATERIAL_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
EXPLICIT_NODE_UUID_A = "20000000-0000-4000-8000-000000000001"
EXPLICIT_NODE_UUID_B = "20000000-0000-4000-8000-000000000002"
DEVICE_MATERIAL_UUID = "30000000-0000-4000-8000-000000000001"


class FakeInventoryStore(InventoryStore):
    """提供真实同步事务和固定设备物料身份的内存库存存储。"""

    def __init__(self) -> None:
        """建立含固定设备模板与设备物料的后端（Backend）形态库存事实。

        参数：无。返回：无；资源模板（ResourceTemplate）和物料（Material）使用
        测试固定 UUID，后续组合根必须通过真实同步事务复用该业务身份。
        """

        super().__init__()
        # ``seed_transaction`` 使用基类事务，确保故障测试子类只影响后续同步阶段。
        with InventoryStore.transaction(self) as seed_transaction:
            seed_transaction.execute(
                """
                INSERT INTO resource_template(
                    uuid, create_time, update_time, name, display_name,
                    resource_type, module
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    RESOURCE_TEMPLATE_UUID,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    "pump",
                    "注射泵",
                    "device",
                    "lab.devices:Pump",
                ),
            )
            seed_transaction.execute(
                """
                INSERT INTO resource_template_inventory(
                    resource_template_uuid, aggregate_version
                ) VALUES (?,1)
                """,
                (RESOURCE_TEMPLATE_UUID,),
            )
            seed_transaction.execute(
                """
                INSERT INTO material(
                    uuid, create_time, update_time, meta_data,
                    resource_template_uuid, class, barcode, name
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    DEVICE_MATERIAL_UUID,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    '{"edge_local_id":"pump-01"}',
                    RESOURCE_TEMPLATE_UUID,
                    "device",
                    "",
                    "pump-01",
                ),
            )
            seed_transaction.execute(
                """
                INSERT INTO material_inventory(
                    material_uuid, aggregate_version
                )
                VALUES (?,1)
                """,
                (DEVICE_MATERIAL_UUID,),
            )


class FakeRegistry:
    """提供一个已完成构建的只读设备注册表（Registry）快照。"""

    def __init__(
        self,
        *,
        display_name: str = "输送",
        include_action: bool = True,
        explicit_uuid: str | None = None,
        material_contract: bool = False,
        explicit_material_output: bool = False,
        site_and_array_contract: bool = False,
        material_symbols: bool = False,
        invalid_typed_contract: bool = False,
    ) -> None:
        """保存动作显示名称。

        参数说明：``display_name`` 是动作（Action）的可变展示字段，不参与稳定
        业务身份；``include_action`` 控制完整快照是否仍发布该动作，用于验证遗漏
        成员的软删除（Soft Delete）与重新引入生命周期；``explicit_uuid`` 模拟
        上游已经提供的节点模板稳定身份；``material_contract`` 切换为包含默认锁定
        和显式 free（自由传递）物料字段的第 2 版动作合同；
        ``explicit_material_output`` 为反应板增加同名显式输出；
        ``site_and_array_contract`` 增加库位（Site）编辑器字段和多物料数组；
        ``material_symbols`` 声明物料允许的资源模板源码身份；
        ``invalid_typed_contract`` 模拟显式 ``@action`` 编译诊断失败。
        """

        self.display_name = display_name
        self.include_action = include_action
        self.explicit_uuid = explicit_uuid
        self.material_contract = material_contract
        self.explicit_material_output = explicit_material_output
        self.site_and_array_contract = site_and_array_contract
        self.material_symbols = material_symbols
        self.invalid_typed_contract = invalid_typed_contract

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回包含一个规范动作合同（Action Contract）的设备定义。"""

        # ``action_mappings`` 是本轮设备注册表完整动作集合；空字典表示成功发布空集。
        action_mappings = (
            {
                "transfer": {
                    "contract_kind": "typed",
                    "displayname": self.display_name,
                    "description": "把物料输送到目标库位",
                    "type": "UniLabJsonCommand",
                    "goal": {"volume": "volume"},
                    "goal_default": {"volume": 1.0},
                    "feedback": {},
                    "result": {"accepted": "accepted"},
                    "schema": {
                        "type": "object",
                        "properties": {
                            "goal": {
                                "type": "object",
                                "properties": {
                                    "volume": {
                                        "type": "number",
                                        "title": "体积",
                                    }
                                },
                                "required": ["volume"],
                                "additionalProperties": False,
                            },
                            "feedback": {},
                            "result": {
                                "type": "object",
                                "properties": {
                                    "accepted": {
                                        "type": "boolean",
                                        "title": "是否接受",
                                    }
                                },
                                "required": ["accepted"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["goal"],
                        "x-unilabos-action-contract": {
                            "version": 2,
                            "input_order": ["volume"],
                            "output_order": ["accepted"],
                            "resource_template_symbols": {
                                "goal": {},
                                "result": {},
                            },
                        },
                    },
                }
            }
            if self.include_action
            else {}
        )
        if self.explicit_uuid is not None:
            action_mappings["transfer"]["uuid"] = self.explicit_uuid
        if self.material_contract:
            action = action_mappings["transfer"]
            action["goal"] = {"plate": "plate", "mount_resource": "mount_resource"}
            action["goal_default"] = {}
            action_schema = action["schema"]
            action_schema["properties"]["goal"] = {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "title": "反应板",
                        "x-unilabos-material-lock": True,
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                    },
                    "mount_resource": {
                        "type": ["object", "null"],
                        "title": "可选承载物料",
                        "x-unilabos-material-lock": False,
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                    },
                },
                "required": ["plate"],
                "additionalProperties": False,
            }
            action_schema["x-unilabos-action-contract"]["input_order"] = [
                "plate",
                "mount_resource",
            ]
            if self.explicit_material_output:
                action["result"] = {"plate": "plate", "accepted": "accepted"}
                action_schema["properties"]["result"]["properties"] = {
                    "plate": {
                        "type": "object",
                        "title": "处理后的反应板",
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                        "additionalProperties": False,
                    },
                    "accepted": {
                        "type": "boolean",
                        "title": "是否接受",
                    },
                }
                action_schema["properties"]["result"]["required"] = [
                    "plate",
                    "accepted",
                ]
                action_schema["x-unilabos-action-contract"]["output_order"] = [
                    "plate",
                    "accepted",
                ]
            if self.material_symbols:
                action_schema["x-unilabos-action-contract"][
                    "resource_template_symbols"
                ]["goal"] = {
                    "plate": [
                        "lab.resources:plate_96",
                        "lab.resources:plate_96",
                    ]
                }
        if self.site_and_array_contract:
            action = action_mappings["transfer"]
            action["goal"] = {"tips": "tips", "destination": "destination"}
            action["goal_default"] = {"destination": None}
            action_schema = action["schema"]
            action_schema["properties"]["goal"] = {
                "type": "object",
                "properties": {
                    "tips": {
                        "type": "array",
                        "title": "吸头集合",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "x-unilabos-material-lock": True,
                            "properties": {
                                "uuid": {"type": "string", "format": "uuid"}
                            },
                            "required": ["uuid"],
                            "additionalProperties": False,
                        },
                    },
                    "destination": {
                        "type": ["string", "null"],
                        "default": None,
                        "x-unilabos-editor-control": "site_selector",
                        "x-unilabos-site-selector": {
                            "version": 1,
                            "owner": "tips",
                            "occupant": None,
                            "show_occupied": True,
                            "allow_occupied": False,
                        },
                    },
                },
                "required": ["tips"],
                "additionalProperties": False,
            }
            action_schema["x-unilabos-action-contract"]["input_order"] = [
                "tips",
                "destination",
            ]
        if self.invalid_typed_contract:
            action = action_mappings["transfer"]
            action["contract_kind"] = "invalid_typed"
            action["contract_diagnostic"] = {
                "code": "invalid_action_contract",
                "message": "动作缺少参数注解",
            }
            action["schema"].pop("x-unilabos-action-contract", None)
        return [
            {
                "id": "pump",
                "displayname": "注射泵",
                "registry_type": "device",
                "class": {
                    "module": "lab.devices:Pump",
                    "type": "python",
                    "action_value_mappings": action_mappings,
                },
                "handles": [],
                "category": ["pump"],
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空器材模板集合；本测试只覆盖设备动作模板身份。"""

        return []


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """为同一 SQLite 文件装配设备注册表模板投影。

    参数说明：``database_path`` 是本地工作流/调度存储路径；返回的投影使用固定
    资源模板 UUID，使测试只观察节点模板和句柄模板的身份生命周期。
    """

    # ``workflow_store`` 是本地工作流权威（Workflow Authority）的持久化适配器。
    workflow_store = WorkflowStore(database_path)
    return RegistryTemplateProjection(
        workflow_store,
        authority_id="local",
        resource_template_identity_resolver=lambda resource_name: {
            "pump": RESOURCE_TEMPLATE_UUID,
            "lab.resources:plate_96": ALLOWED_MATERIAL_TEMPLATE_UUID,
        }.get(resource_name, ""),
    )


def test_projection_reuses_active_business_identity_across_refresh_and_restart(
    tmp_path: Path,
) -> None:
    """没有显式 UUID 的同一活动业务键跨刷新和重启必须复用稳定身份。

    参数说明：``tmp_path`` 提供隔离的本地工作流数据库目录；测试同时证明展示字段
    变化会改变目录指纹，但不会替换节点模板或句柄模板 UUID。
    """

    # ``database_path`` 是跨投影实例重启后仍应保留身份映射的 SQLite 文件。
    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)

    first_snapshot = projection.refresh(FakeRegistry(display_name="输送"))
    first_action = first_snapshot.require_action("lab.devices:Pump", "transfer")
    # ``first_template_uuid`` 是业务键首次发布时分配的节点模板稳定身份。
    first_template_uuid = str(first_action.template["uuid"])
    # ``first_handle_uuids`` 是动作输入、输出和结构连接点的稳定身份集合。
    first_handle_uuids = tuple(str(handle["uuid"]) for handle in first_action.handles)

    second_snapshot = projection.refresh(FakeRegistry(display_name="转移"))
    second_action = second_snapshot.require_action("lab.devices:Pump", "transfer")

    assert second_action.template["uuid"] == first_template_uuid
    assert tuple(str(handle["uuid"]) for handle in second_action.handles) == (
        first_handle_uuids
    )
    assert second_action.template["display_name"] == "转移"
    assert second_snapshot.fingerprint != first_snapshot.fingerprint

    projection.close()
    restarted_projection = _projection(database_path)
    restarted_action = restarted_projection.snapshot().require_action(
        "lab.devices:Pump",
        "transfer",
    )

    assert restarted_action.template["uuid"] == first_template_uuid
    assert tuple(str(handle["uuid"]) for handle in restarted_action.handles) == (
        first_handle_uuids
    )
    restarted_projection.close()


def test_projection_rejects_invalid_explicit_uuid_before_persisting(
    tmp_path: Path,
) -> None:
    """非法显式 UUID 必须整批拒绝，且不得污染持久模板投影。

    参数说明：``tmp_path`` 隔离数据库；测试在失败后重启投影，证明旧的成功空快照
    与 SQLite 事实仍一致。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)

    with pytest.raises(RegistryTemplateProjectionError, match="UUID"):
        projection.refresh(FakeRegistry(explicit_uuid="not-a-uuid"))

    assert projection.snapshot().actions == ()
    projection.close()
    restarted_projection = _projection(database_path)
    assert restarted_projection.snapshot().actions == ()
    restarted_projection.close()


def test_projection_allocates_new_generation_after_omission_and_reintroduction(
    tmp_path: Path,
) -> None:
    """无显式 UUID 的模板被完整刷新遗漏后，重新引入必须分配新一代身份。

    参数说明：``tmp_path`` 隔离工作流数据库；测试同时确认空投影已经提交，而不是
    继续从内存快照返回已软删除的动作。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    first_action = projection.refresh(FakeRegistry()).require_action(
        "lab.devices:Pump",
        "transfer",
    )
    first_template_uuid = str(first_action.template["uuid"])
    first_handle_uuids = tuple(str(handle["uuid"]) for handle in first_action.handles)

    empty_snapshot = projection.refresh(FakeRegistry(include_action=False))
    try:
        empty_snapshot.require_action("lab.devices:Pump", "transfer")
    except AuthoringCatalogError:
        pass
    else:
        raise AssertionError("完整空投影不应继续暴露已遗漏动作")

    next_action = projection.refresh(FakeRegistry()).require_action(
        "lab.devices:Pump",
        "transfer",
    )
    assert next_action.template["uuid"] != first_template_uuid
    assert tuple(str(handle["uuid"]) for handle in next_action.handles) != (
        first_handle_uuids
    )
    projection.close()


def test_projection_keeps_contract_handle_order_and_material_placeholder_metadata(
    tmp_path: Path,
) -> None:
    """强类型动作保留物料占位符语义、必填性和显式数据连接点顺序。

    参数说明：``tmp_path`` 隔离数据库；测试覆盖默认锁定物料、显式 free 物料、
    必填性；结构控制连接点由独立测试固定，不参与动作参数顺序。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    action = projection.refresh(FakeRegistry(material_contract=True)).require_action(
        "lab.devices:Pump",
        "transfer",
    )

    # ``data_handles`` 排除后端（Backend）规范的 ready 控制连接点，只验证动作
    # 数据合同。
    data_handles = [
        handle for handle in action.handles if handle["handle_key"] != "ready"
    ]
    # ``handles_by_key`` 以方向和字段名表达稳定业务身份，不依赖数据库 UUID 顺序。
    handles_by_key = {
        (handle["io_type"], handle["handle_key"]): handle for handle in data_handles
    }
    assert set(handles_by_key) == {
        ("target", "plate"),
        ("target", "mount_resource"),
        ("source", "accepted"),
        ("source", "plate"),
        ("source", "mount_resource"),
    }
    plate_handle = handles_by_key[("target", "plate")]
    free_handle = handles_by_key[("target", "mount_resource")]
    result_handle = handles_by_key[("source", "accepted")]
    # ``stored_contract`` 保留完整第 2 版动作合同（Action Contract），供前端从
    # 后端（Backend）形状详情恢复物料占位符（ResourceSlot）与编辑器元数据；
    # 节点 ``schema`` 仍只承载 goal。
    stored_contract = action.template["meta_data"]["unilab"]["action_contract_schema"]
    # ``projected_goal_schema`` 是从后端（Backend）文本合同解码出的动作 goal
    # 参数子模式；完整动作合同（Action Contract）只保存在元数据投影中。
    projected_goal_schema = json.loads(action.template["schema"])
    assert stored_contract["x-unilabos-action-contract"]["version"] == 2
    assert "goal" not in projected_goal_schema.get("properties", {})
    assert plate_handle["type"] == "ResourceSlot"
    assert plate_handle["required"] is True
    assert (
        plate_handle["meta_data"]["unilab"]["value_schema"]["x-unilabos-material-lock"]
        is True
    )
    assert free_handle["type"] == "ResourceSlot"
    assert free_handle["required"] is False
    assert (
        free_handle["meta_data"]["unilab"]["value_schema"]["x-unilabos-material-lock"]
        is False
    )
    assert result_handle["type"] == "boolean"
    assert result_handle["data_source"] == "executor"
    # ``passthrough_handles`` 是服务端管理的同名隐式物料输出。
    passthrough_handles = [
        handles_by_key[("source", key)] for key in ("plate", "mount_resource")
    ]
    assert all(
        handle["meta_data"]["unilab"]["implicit_passthrough"] is True
        for handle in passthrough_handles
    )
    assert all(
        "x-unilabos-material-lock" not in handle["meta_data"]["unilab"]["value_schema"]
        for handle in passthrough_handles
    )
    projection.close()


def test_projection_does_not_duplicate_explicit_material_output(
    tmp_path: Path,
) -> None:
    """同名显式物料输出必须压制隐式物料输出，保持唯一 source 业务身份。

    参数说明：``tmp_path`` 隔离数据库；显式 ``source:plate`` 保留结果字段模式，
    并标记为非隐式传递。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    action = projection.refresh(
        FakeRegistry(material_contract=True, explicit_material_output=True)
    ).require_action("lab.devices:Pump", "transfer")

    # ``plate_outputs`` 收集所有同名输出，用于证明没有重复业务身份。
    plate_outputs = [
        handle
        for handle in action.handles
        if handle["io_type"] == "source" and handle["handle_key"] == "plate"
    ]
    assert len(plate_outputs) == 1
    assert plate_outputs[0]["type"] == "ResourceSlot"
    assert plate_outputs[0]["meta_data"]["unilab"]["implicit_passthrough"] is False
    projection.close()


def test_projection_projects_site_selector_and_material_array_metadata(
    tmp_path: Path,
) -> None:
    """库位编辑器提示与多物料数组必须保持各自独立的连接点语义。

    参数说明：``tmp_path`` 隔离数据库；数组保留 ``type=array`` 与完整成员模式，
    同时使用 material_port；库位（Site）字段保留 ``site_selector``。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    action = projection.refresh(
        FakeRegistry(site_and_array_contract=True)
    ).require_action("lab.devices:Pump", "transfer")

    # ``targets`` 是动作输入连接点映射，不包含 ready 控制连接点。
    targets = {
        handle["handle_key"]: handle
        for handle in action.handles
        if handle["io_type"] == "target" and handle["handle_key"] != "ready"
    }
    assert targets["tips"]["type"] == "array"
    assert targets["tips"]["meta_data"]["unilab"]["editor_control"] == ("material_port")
    assert targets["destination"]["type"] == "string"
    assert (
        targets["destination"]["meta_data"]["unilab"]["editor_control"]
        == "site_selector"
    )
    projection.close()


def test_projection_resolves_and_deduplicates_allowed_material_templates(
    tmp_path: Path,
) -> None:
    """资源模板源码身份必须解析成本地 UUID，并按声明顺序去重。

    参数说明：``tmp_path`` 隔离数据库；允许集属于物料占位符兼容性约束，不是
    库位（Site）字段，也不能把源码符号直接暴露给前端。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    action = projection.refresh(
        FakeRegistry(material_contract=True, material_symbols=True)
    ).require_action("lab.devices:Pump", "transfer")

    plate_handle = next(
        handle
        for handle in action.handles
        if handle["io_type"] == "target" and handle["handle_key"] == "plate"
    )
    assert plate_handle["meta_data"]["unilab"]["allowed_resource_template_uuids"] == (
        ALLOWED_MATERIAL_TEMPLATE_UUID,
    )
    projection.close()


def test_projection_follows_backend_ilab_and_ready_control_handles(
    tmp_path: Path,
) -> None:
    """OS 动作模板必须跟随后端（Backend）的 ILab 节点类型和 ready 控制连接点。

    参数说明：``tmp_path`` 隔离数据库；测试要求每个动作恰好生成一个输入侧
    ``target:ready`` 和一个输出侧 ``source:ready``。两者只表达控制依赖，不得
    携带动作参数的数据键或物料占位符（ResourceSlot）语义。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    action = projection.refresh(FakeRegistry()).require_action(
        "lab.devices:Pump",
        "transfer",
    )

    # ``ready_handles`` 是后端（Backend）规范中的结构控制连接点，不是动作数据连接点。
    ready_handles = [
        handle for handle in action.handles if handle["handle_key"] == "ready"
    ]
    assert action.template["node_type"] == "ILab"
    assert {
        (handle["io_type"], handle["handle_key"]): {
            "type": handle["type"],
            "required": handle["required"],
            "data_key": handle["data_key"],
        }
        for handle in ready_handles
    } == {
        ("target", "ready"): {
            "type": "default",
            "required": False,
            "data_key": None,
        },
        ("source", "ready"): {
            "type": "default",
            "required": False,
            "data_key": None,
        },
    }
    projection.close()


def test_projection_fingerprint_is_stable_across_identical_refresh_and_restart(
    tmp_path: Path,
) -> None:
    """相同活动身份和合同跨重复刷新与重启必须产生确定目录指纹。

    参数说明：``tmp_path`` 隔离数据库；测试证明 SQLite 操作时间和注册表（Registry）遍历
    时机不属于目录指纹（CatalogFingerprint）的业务语义。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    first_snapshot = projection.refresh(FakeRegistry())
    second_snapshot = projection.refresh(FakeRegistry())

    assert second_snapshot.fingerprint == first_snapshot.fingerprint
    assert second_snapshot.actions[0].template["create_time"]
    assert second_snapshot.actions[0].template["update_time"]
    assert second_snapshot.actions[0].template["meta_data"]["unilab"][
        "resource_template"
    ] == {
        "uuid": RESOURCE_TEMPLATE_UUID,
        "name": "pump",
        "display_name": "注射泵",
    }

    projection.close()
    restarted_projection = _projection(database_path)
    assert restarted_projection.snapshot().fingerprint == first_snapshot.fingerprint
    restarted_projection.close()


def test_local_runtime_shares_projection_with_authoring_compiler(
    tmp_path: Path,
) -> None:
    """本地组合根必须让模板查询投影和 F02 创作编译器共享同一目录代际。

    参数说明：``tmp_path`` 是本地工作流数据库目录；测试使用库存活动行解析资源
    模板身份，并在结束时释放进程级组合根。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是组合根共享且必须由测试显式关闭的真实库存存储。
    inventory_store = FakeInventoryStore()
    try:
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=FakeRegistry(),
        )

        assert workflow_service.compiler is not None
        assert workflow_service.compiler.template_catalog_fingerprint == (
            projection.snapshot().fingerprint
        )
        assert (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template["resource_template_uuid"]
            == RESOURCE_TEMPLATE_UUID
        )
        # ``device_action_run`` 证明组合根注入了同一库存权威的设备物料解析器，
        # 而非仅在直接构造服务的合同测试中可用。
        action_template = (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template
        )
        device_action_run = workflow_service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=action_template["uuid"],
            param={"volume": 2.0},
            execution_policy={},
            idempotency_key="local-composition-device-action-run",
            description=None,
            meta_data={},
        )
        assert device_action_run["created"] is True
        assert device_action_run["job"]["material_uuid"] == DEVICE_MATERIAL_UUID
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_invalid_typed_action_preserves_previous_complete_projection(
    tmp_path: Path,
) -> None:
    """强类型动作合同诊断失败必须拒绝刷新并保留上一完整投影。

    参数说明：``tmp_path`` 隔离数据库；测试在失败后同时检查内存快照和重启后的
    SQLite 投影，禁止把无效动作误判为完整快照中的合法遗漏。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    previous_snapshot = projection.refresh(FakeRegistry())

    with pytest.raises(RegistryTemplateProjectionError, match="动作合同"):
        projection.refresh(FakeRegistry(invalid_typed_contract=True))

    assert projection.snapshot().fingerprint == previous_snapshot.fingerprint
    projection.close()
    restarted_projection = _projection(database_path)
    assert restarted_projection.snapshot().fingerprint == previous_snapshot.fingerprint
    restarted_projection.close()


def test_explicit_uuid_conflict_rolls_back_without_leaking_store_error(
    tmp_path: Path,
) -> None:
    """活动业务键换绑另一个显式 UUID 必须回滚并返回投影领域错误。

    参数说明：``tmp_path`` 隔离数据库；测试证明显式身份优先不等于允许改写已有
    活动身份映射，且调用方不需要理解 SQLite 存储异常。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    previous_snapshot = projection.refresh(
        FakeRegistry(explicit_uuid=EXPLICIT_NODE_UUID_A)
    )

    with pytest.raises(RegistryTemplateProjectionError, match="身份"):
        projection.refresh(FakeRegistry(explicit_uuid=EXPLICIT_NODE_UUID_B))

    current_action = projection.snapshot().require_action(
        "lab.devices:Pump",
        "transfer",
    )
    assert current_action.template["uuid"] == EXPLICIT_NODE_UUID_A
    assert projection.snapshot().fingerprint == previous_snapshot.fingerprint
    projection.close()
