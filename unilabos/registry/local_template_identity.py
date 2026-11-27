"""同步本地资源模板（ResourceTemplate）身份并发布单代只读解析器。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.template_sync import (
    TemplateSyncError,
    _has_deferred_site_template_identity,
    _resolve_available_site_template_identities,
)
from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
)
from unilabos.registry.runtime_device_catalog import RuntimeDeviceTemplateCatalog
from unilabos.registry.template_projection import (
    RegistryTemplateProjection,
    RegistryTemplateProjectionError,
    compile_resource_template_source_aliases,
)
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)

# ``_ACTION_CONTRACT_PREFLIGHT_UUID`` 仅为无副作用动作合同（Action Contract）
# 编译提供格式有效的占位 UUID，不是持久资源模板（ResourceTemplate）身份。
_ACTION_CONTRACT_PREFLIGHT_UUID = "00000000-0000-4000-8000-000000000001"


class LocalTemplateIdentitySynchronization:
    """保存已验证但尚未发布的本地模板身份代际。

    物料模板同步可以先提交到 SQLite；设备模板目录必须等动作模板与领域工作流
    全部构成一个完整目录后再显式激活，避免启动失败时泄漏半代设备定义。
    """

    __slots__ = ("_device_catalog", "_inventory_store", "_resolver", "_activated")

    def __init__(
        self,
        *,
        inventory_store: InventoryStore,
        device_catalog: RuntimeDeviceTemplateCatalog,
        resolver: Callable[[str], str],
    ) -> None:
        """冻结本次候选代际；构造本身不发布设备模板目录。"""

        self._inventory_store = inventory_store
        self._device_catalog = device_catalog
        self._resolver = resolver
        self._activated = False

    def __call__(self, resource_identity: str) -> str:
        """按本次冻结代际解析资源模板业务身份。"""

        return self._resolver(resource_identity)

    def activate_runtime_device_catalog(self) -> None:
        """迁移旧引用并一次发布已完整验证的设备模板目录。"""

        if self._activated:
            return
        current = self._inventory_store.runtime_device_template_catalog
        if current is not None:
            if current.fingerprint != self._device_catalog.fingerprint:
                raise RuntimeError("库存服务启动后不得切换设备模板目录代际")
            self._activated = True
            return
        _retire_legacy_persisted_device_templates(
            self._inventory_store,
            device_catalog=self._device_catalog,
        )
        self._inventory_store.install_runtime_device_template_catalog(
            self._device_catalog
        )
        self._activated = True


def synchronize_local_template_identities(
    *,
    inventory_store: InventoryStore,
    registry_snapshot: RegistryTemplateSnapshot,
) -> LocalTemplateIdentitySynchronization:
    """同步物料模板身份并返回尚未发布的设备目录候选。

    参数说明：``inventory_store`` 是本地库存资源模板写权威；
    ``registry_snapshot`` 是组合根冻结的不可变注册表快照（Registry Snapshot）。
    返回：可调用的本地模板身份候选；它解析本代注册表（Registry）业务 ID、
    显式资源 ``source_fqid`` 或唯一资源 ``class.module`` 兼容别名。调用方必须
    在设备、动作与领域工作流全部验证通过后显式激活设备目录。
    异常：业务 ID、源码别名、同步回执或 UUID 不完整、不唯一、不可解析时抛出
    ``RegistryTemplateProjectionError``；所有快照预校验均发生在库存写事务之前。
    """

    # ``template_definitions`` 只用于跨类型名称/源码别名完整预校验；设备定义
    # 随后进入内存 Catalog，只有物料资源模板进入 SQLite。
    template_definitions = registry_snapshot.detached_definitions()
    resource_definitions = registry_snapshot.detached_resources()
    device_definitions = registry_snapshot.detached_devices()
    # 一旦产品组合选择领域包目录作为设备模板权威，后续任一启动失败都不得让
    # 公共写接口退回 SQLite 创建设备模板或写入同名物料模板。门禁在库存同步
    # 前携带候选设备名设置，且不可撤销。
    inventory_store.require_runtime_device_template_catalog(
        str(definition.get("id") or "") for definition in device_definitions
    )
    device_catalog = RuntimeDeviceTemplateCatalog(registry_snapshot)
    # ``template_name_by_alias`` 在任何库存写入前证明业务 ID 和源码别名一一归属。
    template_name_by_alias = _prevalidate_template_aliases(template_definitions)
    _prevalidate_action_resource_template_aliases(
        device_definitions,
        template_name_by_alias=template_name_by_alias,
    )
    try:
        # ``synchronization`` 是活动资源模板身份同步事务的规范回执。
        synchronization = (
            BackendResourceService(inventory_store).sync_resource_templates(
                resource_definitions
            )
            if resource_definitions
            else {"templates": []}
        )
    except BackendContractError as error:
        raise RegistryTemplateProjectionError(
            f"本地资源模板同步失败: {error.message}"
        ) from error

    # ``template_uuid_by_name`` 是经回执验证的活动业务 ID 到稳定 UUID 的全集。
    template_uuid_by_name = _validated_receipt_identities(
        synchronization,
        expected_names={str(item["id"]) for item in resource_definitions},
    )
    # 与云端 TemplateSynchronizer 二次上传对齐：首次回执给出稳定 UUID 后，
    # 再把库位 ``meta_data.unilab.initial_material_template`` 短名落成 UUID。
    if resource_definitions and _has_deferred_site_template_identity(
        resource_definitions
    ):
        try:
            _resolve_available_site_template_identities(
                resource_definitions,
                template_uuid_by_name,
            )
            synchronization = BackendResourceService(
                inventory_store
            ).sync_resource_templates(resource_definitions)
        except TemplateSyncError as error:
            raise RegistryTemplateProjectionError(
                f"本地资源模板库位默认内容解析失败: {error}"
            ) from error
        except BackendContractError as error:
            raise RegistryTemplateProjectionError(
                f"本地资源模板同步失败: {error.message}"
            ) from error
        template_uuid_by_name = _validated_receipt_identities(
            synchronization,
            expected_names={str(item["id"]) for item in resource_definitions},
        )
    for device_definition in device_definitions:
        device_name = str(device_definition["id"])
        template_uuid_by_name[device_name] = device_catalog.resolve_uuid(device_name)
    # ``template_uuid_by_alias`` 只在当前快照代际内存活，不建立第二持久身份权威。
    template_uuid_by_alias = {
        alias: template_uuid_by_name[template_name]
        for alias, template_name in template_name_by_alias.items()
    }
    # 先用真实物料模板身份完整编译设备与动作目录；失败时不得安装部分设备代际。
    preflight_projection = RegistryTemplateProjection.in_memory(
        authority_id="local-preflight",
        resource_template_identity_resolver=lambda identity: (
            template_uuid_by_alias.get(identity, "")
        ),
    )
    try:
        preflight_projection.refresh(registry_snapshot)
    finally:
        preflight_projection.close()
    def resolve_resource_template_identity(resource_identity: str) -> str:
        """解析当前代资源模板业务 ID 或源码别名。

        参数说明：``resource_identity`` 来自同一注册表快照（Registry Snapshot）。
        返回：对应活动资源模板（ResourceTemplate）的稳定 UUID；身份不在本代时
        返回空串，不猜测历史 UUID，也不产生新的库存写入。
        """

        return template_uuid_by_alias.get(resource_identity, "")

    return LocalTemplateIdentitySynchronization(
        inventory_store=inventory_store,
        device_catalog=device_catalog,
        resolver=resolve_resource_template_identity,
    )


def _retire_legacy_persisted_device_templates(
    inventory_store: InventoryStore,
    *,
    device_catalog: RuntimeDeviceTemplateCatalog,
) -> None:
    """把旧版持久设备模板引用迁到内存目录身份并软删除活动行。

    参数：``inventory_store`` 是已升级为混合模板引用的库存库；
    ``device_catalog`` 是当前启动代际。返回：无。只处理名称仍存在于当前领域包
    且 ``resource_type=device`` 的旧行；物料和库位引用在同一事务替换为确定性
    UUID。当前包已移除且仍被引用的旧设备关闭式失败；无引用旧设备只做软删除，
    保留 UUID、Handle 和版本历史。
    """

    try:
        with inventory_store.transaction() as connection:
            catalog_items = device_catalog.list()
            device_names = tuple(str(item["name"]) for item in catalog_items)
            if device_names:
                name_markers = ",".join("?" for _ in device_names)
                conflicting_resource = connection.execute(
                    "SELECT name FROM resource_template "
                    f"WHERE name IN ({name_markers}) "
                    "AND resource_type<>'device' AND deleted_at IS NULL LIMIT 1",
                    device_names,
                ).fetchone()
                if conflicting_resource is not None:
                    raise RegistryTemplateProjectionError(
                        "设备模板业务名已被活动物料模板占用: "
                        f"{conflicting_resource['name']}"
                    )
            runtime_name_by_uuid = {
                str(item["uuid"]): str(item["name"]) for item in catalog_items
            }
            if runtime_name_by_uuid:
                uuid_markers = ",".join("?" for _ in runtime_name_by_uuid)
                for collision in connection.execute(
                    "SELECT uuid,name,resource_type FROM resource_template "
                    f"WHERE uuid IN ({uuid_markers}) AND deleted_at IS NULL",
                    tuple(runtime_name_by_uuid),
                ).fetchall():
                    expected_name = runtime_name_by_uuid[str(collision["uuid"])]
                    if (
                        str(collision["resource_type"]) != "device"
                        or str(collision["name"]) != expected_name
                    ):
                        raise RegistryTemplateProjectionError(
                            "设备模板确定性 UUID 已被其他活动模板占用: "
                            f"{collision['uuid']}"
                        )
            legacy_rows = connection.execute(
                "SELECT uuid,name FROM resource_template "
                "WHERE resource_type='device' AND deleted_at IS NULL "
                "ORDER BY name,uuid"
            ).fetchall()
            replacements: dict[str, str] = {}
            legacy_uuids: list[str] = []
            for row in legacy_rows:
                previous_uuid = str(row["uuid"])
                replacement = device_catalog.resolve_uuid(str(row["name"]))
                legacy_uuids.append(previous_uuid)
                if replacement:
                    replacements[previous_uuid] = replacement
            if not legacy_uuids:
                return
            unknown_uuids = set(legacy_uuids) - set(replacements)
            if unknown_uuids:
                markers = ",".join("?" for _ in unknown_uuids)
                referenced = connection.execute(
                    "SELECT resource_template_uuid FROM material "
                    f"WHERE resource_template_uuid IN ({markers}) LIMIT 1",
                    tuple(sorted(unknown_uuids)),
                ).fetchone()
                if referenced is not None:
                    raise RegistryTemplateProjectionError(
                        "旧设备模板已被当前领域包移除但仍有物料实例引用"
                    )
            for previous_uuid, replacement_uuid in replacements.items():
                connection.execute(
                    "UPDATE material SET resource_template_uuid=? "
                    "WHERE resource_template_uuid=?",
                    (replacement_uuid, previous_uuid),
                )
            for site in connection.execute(
                "SELECT uuid,allowed_resource_template_uuids FROM site"
            ).fetchall():
                try:
                    allowed = json.loads(str(site["allowed_resource_template_uuids"]))
                except (TypeError, ValueError):
                    allowed = []
                if not isinstance(allowed, list):
                    continue
                if any(str(item) in unknown_uuids for item in allowed):
                    raise RegistryTemplateProjectionError(
                        "旧设备模板已被当前领域包移除但仍有库位约束引用"
                    )
                migrated = [replacements.get(str(item), str(item)) for item in allowed]
                if migrated != allowed:
                    connection.execute(
                        "UPDATE site SET allowed_resource_template_uuids=? WHERE uuid=?",
                        (
                            json.dumps(
                                sorted(set(migrated)),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            site["uuid"],
                        ),
                    )
            placeholders = ",".join("?" for _ in legacy_uuids)
            now = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            connection.execute(
                "UPDATE resource_handle_template SET deleted_at=?,update_time=? "
                f"WHERE resource_template_uuid IN ({placeholders}) "
                "AND deleted_at IS NULL",
                (now, now, *legacy_uuids),
            )
            connection.execute(
                "UPDATE resource_template SET deleted_at=?,update_time=? "
                f"WHERE uuid IN ({placeholders}) AND deleted_at IS NULL",
                (now, now, *legacy_uuids),
            )
    except sqlite3.Error as error:
        raise RegistryTemplateProjectionError(
            "旧版持久设备模板迁移到内存目录失败"
        ) from error


def _prevalidate_template_aliases(
    template_definitions: list[dict[str, Any]],
) -> dict[str, str]:
    """在库存写入前验证本代模板业务 ID 与可解析资源源码别名。

    参数说明：``template_definitions`` 是同一冻结代际的完整模板定义。返回：每个
    业务 ID、显式资源 ``source_fqid`` 和无歧义资源 ``class.module`` 兼容别名到
    唯一业务 ID 的映射。异常：空业务 ID、跨设备/资源重复业务 ID、缺少源码
    身份、非法 Python 源码身份或显式资源别名冲突时抛出
    ``RegistryTemplateProjectionError``。设备和遗留资源可以共享实现类；共享
    实现类不是稳定业务身份，也不会被猜成任一资源源码别名。
    """

    # ``template_name_by_alias`` 先登记所有业务 ID，它们是本地模板 UUID 的唯一索引。
    template_name_by_alias: dict[str, str] = {}
    # ``resource_definitions`` 只包含可能进入动作物料资源约束的资源模板定义。
    resource_definitions: list[dict[str, Any]] = []
    for definition in template_definitions:
        # ``template_name`` 是当前定义声明的注册表（Registry）业务 ID。
        template_name = definition.get("id")
        if not isinstance(template_name, str) or not template_name:
            raise RegistryTemplateProjectionError("注册表（Registry）模板缺少业务 ID")
        if template_name in template_name_by_alias:
            raise RegistryTemplateProjectionError(
                f"注册表（Registry）模板业务 ID 重复: {template_name}"
            )
        template_name_by_alias[template_name] = template_name
        # ``source_aliases`` 仅做完整 Python 身份格式预校验；设备实现类不注册为
        # 资源源码别名，因此多个设备业务模板复用驱动类不会形成伪冲突。
        _canonical_source_aliases(definition, template_name)
        if definition.get("registry_type") == "resource":
            resource_definitions.append(definition)

    # ``resource_name_by_alias`` 与后续模板投影共用显式优先、实现复用消歧规则。
    resource_name_by_alias = compile_resource_template_source_aliases(
        resource_definitions
    )
    for source_alias, template_name in resource_name_by_alias.items():
        # ``previous_name`` 保护极端情况下源码符号与另一个业务 ID 的字符串冲突。
        previous_name = template_name_by_alias.get(source_alias)
        if previous_name is not None and previous_name != template_name:
            raise RegistryTemplateProjectionError(
                f"资源模板源码身份不得绑定多个注册表（Registry）业务 ID: {source_alias}"
            )
        template_name_by_alias[source_alias] = template_name
    return template_name_by_alias


def _prevalidate_action_resource_template_aliases(
    device_definitions: Sequence[Mapping[str, Any]],
    *,
    template_name_by_alias: Mapping[str, str],
) -> None:
    """在库存写入前关闭式验证全部动作资源模板源码别名。

    参数说明：``device_definitions`` 是冻结注册表快照（Registry Snapshot）的完整
    设备定义；``template_name_by_alias`` 是已证明唯一的业务 ID/源码别名到资源
    模板（ResourceTemplate）业务 ID 映射。返回：无。异常：设备动作集合、动作
    合同（Action Contract）或任一 ``resource_template_symbols`` 别名无法通过现有
    动作模板编译器解析时，抛出 ``RegistryTemplateProjectionError``；调用者不得
    进入后端（Backend）形态资源模板同步事务。
    """

    def resolve_prevalidated_template_alias(source_alias: str) -> str:
        """把已知源码别名映射为仅供预检使用的有效 UUID。

        参数说明：``source_alias`` 是动作合同声明的资源模板源码身份。返回：别名
        存在于当前注册表（Registry）代际时返回固定有效 UUID，否则返回空串；该
        UUID 只驱动无副作用合同校验，绝不作为持久资源模板身份或同步回执。
        """

        return (
            _ACTION_CONTRACT_PREFLIGHT_UUID
            if source_alias in template_name_by_alias
            else ""
        )

    for device_definition in device_definitions:
        # ``device_name`` 只为错误定位标识当前注册表（Registry）设备业务 ID。
        device_name = str(device_definition.get("id") or "")
        # ``class_definition`` 持有设备实现身份及其完整动作（Action）映射。
        class_definition = device_definition.get("class")
        if not isinstance(class_definition, Mapping):
            raise RegistryTemplateProjectionError(f"设备定义缺少类合同: {device_name}")
        # ``action_mappings`` 是本设备冻结代际内的完整动作合同集合。
        action_mappings = class_definition.get("action_value_mappings") or {}
        if not isinstance(action_mappings, Mapping):
            raise RegistryTemplateProjectionError(
                f"设备动作映射必须是对象: {device_name}"
            )
        for action_name, action_definition in action_mappings.items():
            # ``action_name`` 是错误定位使用的动作业务名；自动动作不进入可信投影。
            if not isinstance(action_definition, Mapping):
                raise RegistryTemplateProjectionError(
                    f"设备动作定义必须是对象: {device_name}.{action_name}"
                )
            if action_definition.get("contract_kind") != "typed":
                continue
            # ``action_schema`` 是资源模板源码别名所在的第 2 版动作合同根模式。
            action_schema = action_definition.get("schema")
            if not isinstance(action_schema, Mapping):
                raise RegistryTemplateProjectionError(
                    f"动作合同缺少 Schema: {device_name}.{action_name}"
                )
            try:
                compile_action_template_handles(
                    action_schema,
                    node_business_key=(device_name, str(action_name)),
                    resource_template_identity_resolver=(
                        resolve_prevalidated_template_alias
                    ),
                )
            except ActionTemplateProjectionError as error:
                raise RegistryTemplateProjectionError(
                    f"动作资源模板源码身份预检失败: "
                    f"{device_name}.{action_name}: {error!s}"
                ) from error


def _canonical_source_aliases(
    definition: Mapping[str, Any],
    template_name: str,
) -> set[str]:
    """预校验一个模板声明的 ``source_fqid`` 与 ``class.module``。

    参数说明：``definition`` 是单个冻结模板定义；``template_name`` 是已验证的
    注册表（Registry）业务 ID。返回：仅供格式验证的规范 Python 身份集合，不
    表示这些身份都可作为唯一资源源码别名。异常：两个字段均缺失或任一已声明
    字段不可安全解析时抛出 ``RegistryTemplateProjectionError``，不得进入库存
    写事务。
    """

    # ``class_definition`` 是资源模板实现合同，可能提供类模块源码别名。
    class_definition = definition.get("class")
    # ``class_module`` 是 ``class.module`` 声明的候选 Python 源码身份。
    class_module = (
        class_definition.get("module")
        if isinstance(class_definition, Mapping)
        else None
    )
    # ``raw_aliases`` 保留两个合同字段；任一已声明别名都必须独立通过校验。
    raw_aliases = [definition.get("source_fqid"), class_module]
    # ``declared_aliases`` 只保留作者实际声明、因此必须逐个通过校验的候选别名。
    declared_aliases = [alias for alias in raw_aliases if alias not in (None, "")]
    if not declared_aliases:
        raise RegistryTemplateProjectionError(f"资源模板缺少源码身份: {template_name}")
    # ``canonical_aliases`` 是当前模板经过可信 Python 身份校验的去重别名集合。
    canonical_aliases: set[str] = set()
    for raw_alias in declared_aliases:
        # ``raw_alias`` 是尚未规范化的 ``source_fqid`` 或类模块源码身份。
        try:
            canonical_aliases.add(canonical_python_source_identity(raw_alias))
        except PythonSourceIdentityError:
            raise RegistryTemplateProjectionError(
                f"资源模板源码身份不能安全解析: {template_name}"
            ) from None
    return canonical_aliases


def _validated_receipt_identities(
    synchronization: Any,
    *,
    expected_names: set[str],
) -> dict[str, str]:
    """验证同步回执并返回完整活动模板身份映射。

    参数说明：``synchronization`` 是本地后端（Backend）形态资源服务回执；
    ``expected_names`` 是预校验快照中的完整注册表（Registry）业务 ID 集合。
    返回：业务 ID 到规范 UUID 的映射。异常：回执形状、成员、UUID、唯一性或
    集合完整性不符时抛出 ``RegistryTemplateProjectionError``。
    """

    # ``synchronized_templates`` 是同步事务回执中的资源模板身份成员。
    synchronized_templates = (
        synchronization.get("templates")
        if isinstance(synchronization, Mapping)
        else None
    )
    if not isinstance(synchronized_templates, list):
        raise RegistryTemplateProjectionError("本地资源模板同步回执缺少模板身份列表")
    # ``template_uuid_by_name`` 汇总已验证回执业务 ID 到活动稳定 UUID 的全集。
    template_uuid_by_name: dict[str, str] = {}
    for identity in synchronized_templates:
        # ``identity`` 是同步回执（Receipt）中的单个资源模板身份成员。
        if not isinstance(identity, Mapping):
            raise RegistryTemplateProjectionError("本地资源模板同步回执成员必须是对象")
        # ``template_name`` 是回执成员声明的注册表（Registry）业务 ID。
        template_name = identity.get("name")
        if not isinstance(template_name, str) or not template_name:
            raise RegistryTemplateProjectionError("本地资源模板同步回执缺少业务 ID")
        try:
            # ``template_uuid`` 是资源服务提交的活动资源模板稳定身份。
            template_uuid = validate_uuid(identity.get("uuid"))
        except (TypeError, ValueError):
            raise RegistryTemplateProjectionError(
                f"本地资源模板同步回执 UUID 无效: {template_name}"
            ) from None
        if template_name in template_uuid_by_name:
            raise RegistryTemplateProjectionError("本地资源模板同步回执业务 ID 重复")
        template_uuid_by_name[template_name] = template_uuid
    if set(template_uuid_by_name) != expected_names:
        raise RegistryTemplateProjectionError(
            "本地资源模板同步回执与注册表（Registry）快照不一致"
        )
    return template_uuid_by_name


__all__ = [
    "LocalTemplateIdentitySynchronization",
    "synchronize_local_template_identities",
]
