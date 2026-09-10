"""F05.1 物料来源（MaterialSource）框架模板投影合同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unilabos.registry.template_projection import (
    RegistryTemplateProjection,
    RegistryTemplateProjectionError,
)
from unilabos.workflow.store import WorkflowStore

HOST_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
PLATE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
PUMP_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000003"
PLATE_SOURCE_IDENTITY = "lab.resources:plate_96"


class _Registry:
    """提供宿主节点（Host Node）与一个资源模板的设备注册表冻结快照。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回框架物料来源（MaterialSource）的唯一宿主节点（Host Node）所有者。

        参数：无。返回：只含 ``host_node`` 的完整设备定义集。
        """

        return [
            {
                "id": "host_node",
                "display_name": "Host Node",
                "registry_type": "device",
                "class": {
                    "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                    "type": "python",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": [],
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回可供创作编译器解析的物料资源模板定义。

        参数：无。返回：含稳定源码身份的资源定义集。
        """

        return [
            {
                "id": "plate_96",
                "source_fqid": PLATE_SOURCE_IDENTITY,
                "display_name": "96 孔板",
                "registry_type": "resource",
                "class": {"module": PLATE_SOURCE_IDENTITY, "type": "python"},
                "handles": [],
                "category": [],
            }
        ]


class _UnsafeSourceRegistry(_Registry):
    """把单个资源模板源码身份替换为不可信反例。"""

    def __init__(self, source_identity: str) -> None:
        """保存待投影的不可信 ``source_fqid``。

        参数说明：``source_identity`` 是单个模块/符号反例。返回：无；只构造
        测试注册表，不访问文件或网络。
        """

        self._source_identity = source_identity

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回带不可信源码身份的资源模板定义。

        参数：无。返回：从合法注册表分离复制且只替换 ``source_fqid`` 与类模块
        身份的资源定义集。
        """

        resources = super().obtain_registry_resource_info()
        resources[0]["source_fqid"] = self._source_identity
        resources[0]["class"]["module"] = self._source_identity
        return resources


class _ResourceOnlyRegistry(_Registry):
    """只发布资源模板（ResourceTemplate）、不发布节点模板的设备注册表。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回空设备定义代际。

        参数：无。返回：空列表，明确表示当前代没有宿主节点（Host Node）或动作。
        """

        return []


class _ActionRegistry(_Registry):
    """发布一个无参数动作及同一资源模板的设备注册表。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回可被模板投影编译的单动作设备定义。

        参数：无。返回：含一个第 2 版动作合同（ActionContract）的泵定义，
        用于证明删除最后动作时目录级资源身份仍独立存活。
        """

        return [
            {
                "id": "pump",
                "display_name": "测试泵",
                "registry_type": "device",
                "class": {
                    "module": "lab.devices:Pump",
                    "type": "python",
                    "action_value_mappings": {
                        "prime": {
                            "contract_kind": "typed",
                            "display_name": "预充",
                            "description": "预充测试泵。",
                            "goal": {},
                            "goal_default": {},
                            "feedback": {},
                            "result": {},
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "goal": {
                                        "type": "object",
                                        "properties": {},
                                        "required": [],
                                        "additionalProperties": False,
                                    },
                                    "feedback": {},
                                    "result": {},
                                },
                                "required": ["goal"],
                                "x-unilabos-action-contract": {
                                    "version": 2,
                                    "input_order": [],
                                    "output_order": [],
                                    "resource_template_symbols": {
                                        "goal": {},
                                        "result": {},
                                    },
                                },
                            },
                        }
                    },
                },
                "handles": [],
                "category": [],
            }
        ]


def _identity(source_identity: str) -> str:
    """把设备注册表（Registry）唯一名称解析为本地稳定资源模板 UUID。

    参数说明：``source_identity`` 是宿主节点（Host Node）或物料资源的业务唯一
    名称。返回：已存储的资源模板（ResourceTemplate）UUID。异常：未知身份抛出
    ``KeyError``，投影层必须关闭式失败。
    """

    # ``identities`` 是测试代表的已有模板数据库身份映射。
    identities = {
        "host_node": HOST_TEMPLATE_UUID,
        "plate_96": PLATE_TEMPLATE_UUID,
        "pump": PUMP_TEMPLATE_UUID,
    }
    return identities[source_identity]


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """打开使用固定身份解析器的模板投影。

    参数说明：``database_path`` 是工作流模板 SQLite 路径。
    返回：可刷新和读取的注册表模板投影。
    """

    return RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="local",
        resource_template_identity_resolver=_identity,
    )


def test_registry_projects_one_stable_material_source_framework_template(
    tmp_path: Path,
) -> None:
    """宿主节点（Host Node）应发布单一物料来源模板及 source 物料占位符。

    参数说明：``tmp_path`` 提供跨刷新和重启的隔离 SQLite 路径。
    返回：无；断言节点、连接点（Handle）和 UUID 生命周期。
    """

    # ``database_path`` 保留本地节点模板业务唯一键到 UUID 的身份映射。
    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    first = projection.refresh(_Registry()).require_material_source()
    # ``framework_uuid`` 是首次发布时分配或复用的框架节点模板身份。
    framework_uuid = str(first.template["uuid"])

    assert {
        "resource_template_uuid": first.template["resource_template_uuid"],
        "name": first.template["name"],
        "class": first.template["class"],
        "type": first.template["type"],
        "node_type": first.template["node_type"],
    } == {
        "resource_template_uuid": HOST_TEMPLATE_UUID,
        "name": "material_source",
        "class": "unilabos.workflow.authoring:material_source",
        "type": "material_source",
        "node_type": "material_source",
    }
    assert len(first.handles) == 1
    assert {
        "handle_key": first.handles[0]["handle_key"],
        "io_type": first.handles[0]["io_type"],
        "type": first.handles[0]["type"],
        "required": first.handles[0]["required"],
    } == {
        "handle_key": "material",
        "io_type": "source",
        "type": "ResourceSlot",
        "required": False,
    }
    assert (
        projection.refresh(_Registry()).require_material_source().template["uuid"]
        == framework_uuid
    )
    projection.close()

    restarted = _projection(database_path)
    assert restarted.snapshot().require_material_source().template["uuid"] == (
        framework_uuid
    )
    restarted.close()


def test_material_source_framework_projects_site_selector_contract(
    tmp_path: Path,
) -> None:
    """物料来源（MaterialSource）必须发布与普通动作相同的库位选择合同。

    参数说明：``tmp_path`` 隔离模板投影 SQLite。返回：无；断言 ``site`` 以
    ``mount`` 为 owner，并用 ``resource_template_uuid`` 提供待选物料模板，前端
    不需把框架节点伪装成普通动作或按字段名猜测。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    try:
        source = projection.refresh(_Registry()).require_material_source()
        # ``template_schema`` 是物料来源参数界面消费的规范框架模式。
        template_schema = json.loads(source.template["schema"])
    finally:
        projection.close()

    expected_selector = {
        "version": 1,
        "owner": "mount",
        "occupant": "resource_template_uuid",
        "show_occupied": True,
        "allow_occupied": False,
    }
    assert (
        template_schema["properties"]["site"]["x-unilabos-site-selector"]
        == expected_selector
    )


def test_registry_projects_one_stable_group_framework_template(tmp_path: Path) -> None:
    """宿主节点（Host Node）应发布唯一、无连接点的展示分组模板。

    参数说明：``tmp_path`` 提供跨刷新和重启的隔离 SQLite 路径。返回：无。
    断言：展示分组（Group）使用稳定业务身份，类型为 ``group``，没有执行连接点
    （Handle），且刷新与重启后复用同一 UUID。
    """

    # ``database_path`` 保存展示分组业务唯一键到模板 UUID 的生命周期映射。
    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    first = projection.refresh(_Registry()).require_action(
        "unilabos.workflow.authoring:group",
        "group",
    )
    # ``group_uuid`` 是展示分组框架模板首次发布或从旧代际复用的身份。
    group_uuid = str(first.template["uuid"])

    assert {
        "resource_template_uuid": first.template["resource_template_uuid"],
        "name": first.template["name"],
        "class": first.template["class"],
        "type": first.template["type"],
        "node_type": first.template["node_type"],
    } == {
        "resource_template_uuid": HOST_TEMPLATE_UUID,
        "name": "group",
        "class": "unilabos.workflow.authoring:group",
        "type": "group",
        "node_type": "group",
    }
    assert first.handles == ()
    assert (
        projection.refresh(_Registry())
        .require_action("unilabos.workflow.authoring:group", "group")
        .template["uuid"]
        == group_uuid
    )
    projection.close()

    restarted = _projection(database_path)
    assert (
        restarted.snapshot()
        .require_action("unilabos.workflow.authoring:group", "group")
        .template["uuid"]
        == group_uuid
    )
    restarted.close()


def test_catalog_snapshot_freezes_bidirectional_resource_template_identity(
    tmp_path: Path,
) -> None:
    """创作快照应以冻结设备注册表（Registry）投影解析资源模板符号。

    参数说明：``tmp_path`` 提供隔离存储。返回：无；断言源码身份
    与资源模板 UUID 双向一一对应。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    snapshot = projection.refresh(_Registry())

    assert snapshot.require_resource_template_uuid(PLATE_SOURCE_IDENTITY) == (
        PLATE_TEMPLATE_UUID
    )
    assert snapshot.require_resource_template_symbol(PLATE_TEMPLATE_UUID) == (
        PLATE_SOURCE_IDENTITY
    )
    projection.close()


def test_catalog_restart_restores_resource_identity_and_fingerprint(
    tmp_path: Path,
) -> None:
    """重启后应从 SQLite 恢复资源模板身份和相同目录指纹。

    参数说明：``tmp_path`` 提供跨进程生命周期的隔离数据库路径。返回：无；
    断言资源模板（ResourceTemplate）源码身份双向映射与目录指纹
    （CatalogFingerprint）在只读重启后保持不变。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    before_restart = projection.refresh(_Registry())
    expected_fingerprint = before_restart.fingerprint
    projection.close()

    restarted = _projection(database_path)
    recovered = restarted.snapshot()
    assert recovered.require_resource_template_uuid(PLATE_SOURCE_IDENTITY) == (
        PLATE_TEMPLATE_UUID
    )
    assert recovered.require_resource_template_symbol(PLATE_TEMPLATE_UUID) == (
        PLATE_SOURCE_IDENTITY
    )
    assert recovered.fingerprint == expected_fingerprint
    restarted.close()


@pytest.mark.parametrize(
    "unsafe_source_identity",
    (
        "lab.resources\nfrom os import system:plate_96",
        "lab.bad-module:plate_96",
        "lab.class:plate_96",
        "lab..resources:plate_96",
    ),
    ids=("control-character", "invalid-segment", "keyword-segment", "empty-segment"),
)
def test_registry_rejects_unsafe_source_identity_without_replacing_projection(
    tmp_path: Path,
    unsafe_source_identity: str,
) -> None:
    """不可信资源源码身份必须在 SQLite 写入前失败关闭。

    参数说明：``tmp_path`` 提供隔离数据库，``unsafe_source_identity`` 是单一
    Python 模块反例。返回：无；断言当前内存和重启后的可信目录投影均未变化。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    trusted = projection.refresh(_Registry())

    with pytest.raises(RegistryTemplateProjectionError):
        projection.refresh(_UnsafeSourceRegistry(unsafe_source_identity))

    assert projection.snapshot().fingerprint == trusted.fingerprint
    assert (
        projection.snapshot().require_resource_template_uuid(PLATE_SOURCE_IDENTITY)
        == PLATE_TEMPLATE_UUID
    )
    projection.close()

    restarted = _projection(database_path)
    assert restarted.snapshot().fingerprint == trusted.fingerprint
    assert (
        restarted.snapshot().require_resource_template_symbol(PLATE_TEMPLATE_UUID)
        == PLATE_SOURCE_IDENTITY
    )
    restarted.close()


def test_resource_only_generation_survives_restart_without_node_templates(
    tmp_path: Path,
) -> None:
    """纯资源模板代际必须独立持久化双向身份和目录指纹。

    参数说明：``tmp_path`` 提供隔离 SQLite。返回：无；断言没有节点模板时，
    资源模板（ResourceTemplate）身份仍可双向查询且重启后的目录指纹
    （CatalogFingerprint）完全相同。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    published = projection.refresh(_ResourceOnlyRegistry())
    assert published.actions == ()
    assert published.require_resource_template_uuid(PLATE_SOURCE_IDENTITY) == (
        PLATE_TEMPLATE_UUID
    )
    expected_fingerprint = published.fingerprint
    projection.close()

    restarted = _projection(database_path)
    recovered = restarted.snapshot()
    assert recovered.actions == ()
    assert recovered.require_resource_template_symbol(PLATE_TEMPLATE_UUID) == (
        PLATE_SOURCE_IDENTITY
    )
    assert recovered.fingerprint == expected_fingerprint
    restarted.close()


def test_resource_identity_survives_last_action_removal_without_metadata_carrier(
    tmp_path: Path,
) -> None:
    """删除最后动作后目录级资源身份不得依赖节点元数据继续存在。

    参数说明：``tmp_path`` 提供隔离 SQLite。返回：无；先断言动作节点没有身份
    映射寄生字段，再发布纯资源代际并验证重启后的身份和指纹保持不变。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    with_action = projection.refresh(_ActionRegistry())
    action = with_action.require_action("lab.devices:Pump", "prime")
    unilab_meta = action.template.get("meta_data", {}).get("unilab", {})
    assert "resource_template_identity_projection" not in unilab_meta

    without_action = projection.refresh(_ResourceOnlyRegistry())
    assert without_action.actions == ()
    assert without_action.require_resource_template_uuid(PLATE_SOURCE_IDENTITY) == (
        PLATE_TEMPLATE_UUID
    )
    expected_fingerprint = without_action.fingerprint
    projection.close()

    restarted = _projection(database_path)
    recovered = restarted.snapshot()
    assert recovered.actions == ()
    assert recovered.require_resource_template_symbol(PLATE_TEMPLATE_UUID) == (
        PLATE_SOURCE_IDENTITY
    )
    assert recovered.fingerprint == expected_fingerprint
    restarted.close()
