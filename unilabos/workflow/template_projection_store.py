"""设备注册表模板投影（Registry Template Projection）的 SQLite 适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from unilabos.registry.template_delta import (
    TemplateProjectionDelta,
    TemplateProjectionDeltaError,
    TemplateProjectionManifestPersistence,
    TemplateProjectionMember,
    build_template_projection_delta,
    semantic_template_hash,
    stable_projection_key,
)
from unilabos.workflow.store import StoreConflict, WorkflowStore, utc_now
from unilabos.workflow.template_projection_generation import (
    RegistryTemplateGenerationPersistence,
    RegistryTemplateProjectionGeneration,
    TemplateProjectionGenerationDataError,
)


class TemplateProjectionIdentityConflict(StoreConflict):
    """模板 UUID、活动业务唯一键或父子身份发生冲突。"""


def _removed_member_sort_key(
    member: TemplateProjectionMember,
) -> tuple[bool, str]:
    """读取待软删除模板成员的稳定事务排序键。

    参数：``member`` 是旧清单中已经退出新完整候选代的成员。
    返回：连接点模板优先标志与稳定成员身份组成的二元组。
    异常：无。
    """

    return member.projection_kind != "handle_template", member.identity


class RegistryTemplateProjectionStore:
    """把完整设备注册表模板代际原子写入现有工作流模板表。"""

    def __init__(self, workflow_store: WorkflowStore) -> None:
        """绑定本地工作流存储。

        参数说明：``workflow_store`` 持有唯一 SQLite 连接和写事务；本适配器不再
        创建第二个数据库，也不自行推导数据库路径。返回：无；构造时只确保 OS
        私有投影代际表存在。异常：SQLite 初始化失败原样传播并关闭式失败。
        """

        self._workflow_store = workflow_store
        self._generation_persistence = RegistryTemplateGenerationPersistence(
            workflow_store
        )
        self._manifest_persistence = TemplateProjectionManifestPersistence(
            workflow_store
        )

    def replace(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """原子替换一个权威（Authority）的完整活动模板投影。

        参数说明：``authority_id`` 是投影来源；``node_templates`` 和
        ``handle_templates`` 是一次完整刷新。返回：已解析稳定 UUID 且不含
        时间戳的节点与连接点（Handle）语义实体；本兼容接口会发布空资源模板
        （ResourceTemplate）源码身份映射。异常：参数非法抛出 ``ValueError``，
        模板身份冲突抛出 ``TemplateProjectionIdentityConflict``，事务整体回滚。
        """

        committed = self.replace_generation(
            authority_id=authority_id,
            node_templates=node_templates,
            handle_templates=handle_templates,
            resource_template_symbols={},
        )
        return committed.node_templates, committed.handle_templates

    def replace_generation(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
        resource_template_symbols: Mapping[str, str],
        validate_generation: (
            Callable[[RegistryTemplateProjectionGeneration], None] | None
        ) = None,
    ) -> RegistryTemplateProjectionGeneration:
        """兼容返回单一完整模板投影代际的替换接口。

        参数：``authority_id``、模板全集、资源模板（ResourceTemplate）源码映射
        与 ``validate_generation`` 共同描述一次完整候选发布。
        返回：差量事务提交后的完整投影代际。
        异常：候选、身份、manifest 或完整目录校验失败时传播异常并整体回滚。
        """

        committed_generation, _delta = self.replace_generation_with_delta(
            authority_id=authority_id,
            node_templates=node_templates,
            handle_templates=handle_templates,
            resource_template_symbols=resource_template_symbols,
            validate_generation=validate_generation,
        )
        return committed_generation

    def replace_generation_with_delta(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
        resource_template_symbols: Mapping[str, str],
        validate_generation: (
            Callable[[RegistryTemplateProjectionGeneration], None] | None
        ) = None,
    ) -> tuple[RegistryTemplateProjectionGeneration, TemplateProjectionDelta]:
        """差量提交一个权威（Authority）的完整模板与资源身份代际。

        参数说明：``authority_id`` 是投影来源；``node_templates`` 与
        ``handle_templates`` 是本轮完整节点和连接点（Handle）集合；
        ``resource_template_symbols`` 是资源模板（ResourceTemplate）源码身份到
        UUID 的完整映射；``validate_generation`` 是在写入完成、提交发生前调用的
        只读完整代际校验器。返回：通过同一 SQLite 事务提交的完整投影代际和
        模板投影差量（TemplateProjectionDelta）。异常：空权威、非法资源身份映射抛出 ``ValueError``；模板身份冲突
        抛出 ``TemplateProjectionIdentityConflict``；校验器异常原样传播。任一
        异常都会回滚节点、连接点、资源身份与代际号，旧代际保持不变。
        """

        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ValueError("模板投影权威不能为空")
        # ``node_candidates`` 与 ``handle_candidates`` 是脱离调用方容器的候选代际。
        node_candidates = [dict(candidate) for candidate in node_templates]
        handle_candidates = [dict(candidate) for candidate in handle_templates]
        symbol_candidates = self._generation_persistence.normalize_symbols(
            resource_template_symbols
        )
        with self._workflow_store.transaction() as connection:
            try:
                current_generation, current_symbols = (
                    self._generation_persistence.load_in_transaction(
                        connection,
                        authority_id=authority_id,
                    )
                )
                previous_members = self._manifest_persistence.load_in_transaction(
                    connection,
                    authority_id=authority_id,
                )
            except (
                TemplateProjectionDeltaError,
                TemplateProjectionGenerationDataError,
            ) as error:
                raise TemplateProjectionIdentityConflict(str(error)) from error
            # ``node_identities`` 把本轮活动业务唯一键映射到持久 UUID。
            node_identities: dict[tuple[str, str], str] = {}
            # 两类候选索引只在差量确认有变化后写入对应模板。
            node_candidates_by_identity: dict[
                tuple[str, str], tuple[Mapping[str, Any], tuple[str, str]]
            ] = {}
            handle_candidates_by_identity: dict[
                tuple[str, str],
                tuple[Mapping[str, Any], str, tuple[str, str, str]],
            ] = {}
            # ``node_source_parts`` 是节点及其连接点共享的作者定义身份。
            node_source_parts: dict[tuple[str, str], tuple[str, str]] = {}
            candidate_members: list[TemplateProjectionMember] = []
            for candidate in node_candidates:
                business_key = self._node_business_key(candidate)
                if business_key in node_identities:
                    raise TemplateProjectionIdentityConflict(
                        "完整模板投影包含重复节点活动业务唯一键"
                    )
                template_uuid = self._resolve_node_uuid(
                    connection,
                    authority_id=authority_id,
                    candidate=candidate,
                    business_key=business_key,
                )
                node_identities[business_key] = template_uuid
                class_identity = candidate.get("class")
                if not isinstance(class_identity, str) or not class_identity:
                    raise TemplateProjectionIdentityConflict("节点模板缺少来源类身份")
                source_parts = (class_identity, business_key[1])
                node_source_parts[business_key] = source_parts
                member = TemplateProjectionMember(
                    authority_id=authority_id,
                    projection_kind="node_template",
                    source_definition_key=stable_projection_key(source_parts),
                    business_key=stable_projection_key(business_key),
                    target_uuid=template_uuid,
                    semantic_hash=semantic_template_hash(
                        "node_template",
                        self._node_values(candidate),
                    ),
                    generation=0,
                )
                candidate_members.append(member)
                node_candidates_by_identity[member.identity] = (
                    candidate,
                    business_key,
                )

            seen_handle_keys: set[tuple[str, str, str]] = set()
            for candidate in handle_candidates:
                parent_key = self._handle_parent_key(candidate)
                try:
                    parent_uuid = node_identities[parent_key]
                except KeyError:
                    raise TemplateProjectionIdentityConflict(
                        "句柄模板引用了本轮投影之外的节点模板"
                    ) from None
                business_key = self._handle_business_key(candidate, parent_uuid)
                if business_key in seen_handle_keys:
                    raise TemplateProjectionIdentityConflict(
                        "完整模板投影包含重复句柄活动业务唯一键"
                    )
                seen_handle_keys.add(business_key)
                handle_uuid = self._resolve_handle_uuid(
                    connection,
                    candidate=candidate,
                    business_key=business_key,
                )
                source_parts = (
                    *node_source_parts[parent_key],
                    str(candidate.get("io_type")),
                    str(candidate.get("handle_key")),
                )
                member = TemplateProjectionMember(
                    authority_id=authority_id,
                    projection_kind="handle_template",
                    source_definition_key=stable_projection_key(source_parts),
                    business_key=stable_projection_key(business_key),
                    target_uuid=handle_uuid,
                    semantic_hash=semantic_template_hash(
                        "handle_template",
                        self._handle_values(candidate, parent_uuid=parent_uuid),
                    ),
                    generation=0,
                )
                candidate_members.append(member)
                handle_candidates_by_identity[member.identity] = (
                    candidate,
                    parent_uuid,
                    business_key,
                )

            for source_symbol, template_uuid in symbol_candidates.items():
                candidate_members.append(
                    TemplateProjectionMember(
                        authority_id=authority_id,
                        projection_kind="resource_template_symbol",
                        source_definition_key=source_symbol,
                        business_key=stable_projection_key((source_symbol,)),
                        target_uuid=template_uuid,
                        semantic_hash=semantic_template_hash(
                            "resource_template_symbol",
                            template_uuid,
                        ),
                        generation=0,
                    )
                )
            try:
                delta = build_template_projection_delta(
                    authority_id=authority_id,
                    current_generation=current_generation,
                    previous_members=previous_members,
                    candidate_members=candidate_members,
                )
            except TemplateProjectionDeltaError as error:
                raise TemplateProjectionIdentityConflict(str(error)) from error

            if delta.changed:
                now = utc_now()
                for member in (*delta.added, *delta.modified):
                    if member.projection_kind == "node_template":
                        candidate, business_key = node_candidates_by_identity[
                            member.identity
                        ]
                        # ``persisted_candidate`` 冻结只读规划阶段分配的目标 UUID，
                        # 防止首次新增在写入阶段再次生成另一身份。
                        persisted_candidate = dict(candidate)
                        persisted_candidate["uuid"] = member.target_uuid
                        written_uuid = self._upsert_node(
                            connection,
                            authority_id=authority_id,
                            candidate=persisted_candidate,
                            business_key=business_key,
                            now=now,
                        )
                    elif member.projection_kind == "handle_template":
                        candidate, parent_uuid, business_key = (
                            handle_candidates_by_identity[member.identity]
                        )
                        persisted_candidate = dict(candidate)
                        persisted_candidate["uuid"] = member.target_uuid
                        written_uuid = self._upsert_handle(
                            connection,
                            authority_id=authority_id,
                            candidate=persisted_candidate,
                            parent_uuid=parent_uuid,
                            business_key=business_key,
                            now=now,
                        )
                    else:
                        continue
                    if written_uuid != member.target_uuid:
                        raise TemplateProjectionIdentityConflict(
                            "模板目标 UUID 在事务内发生漂移"
                        )
                self._soft_delete_removed(
                    connection,
                    authority_id=authority_id,
                    removed_members=delta.removed,
                    now=now,
                )
                generation = self._generation_persistence.upsert_in_transaction(
                    connection,
                    authority_id=authority_id,
                    resource_template_symbols=symbol_candidates,
                    now=now,
                )
                if generation != delta.generation:
                    raise TemplateProjectionIdentityConflict(
                        "模板投影代际推进与差量不一致"
                    )
                self._manifest_persistence.apply_in_transaction(
                    connection,
                    delta=delta,
                )
            else:
                generation = current_generation
                if current_symbols != symbol_candidates:
                    raise TemplateProjectionIdentityConflict(
                        "模板投影 manifest 与资源身份代际不一致"
                    )
            nodes, handles = self._load(connection, authority_id=authority_id)
            committed_generation = RegistryTemplateProjectionGeneration(
                authority_id=authority_id,
                generation=generation,
                node_templates=nodes,
                handle_templates=handles,
                resource_template_symbols=dict(symbol_candidates),
            )
            if validate_generation is not None:
                validate_generation(committed_generation)
            result = committed_generation, delta
        return result

    def load(
        self,
        *,
        authority_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取一个权威当前已提交的完整活动投影。

        参数说明：``authority_id`` 用于隔离来源。返回：当前节点与连接点
        （Handle）活动投影，不访问设备注册表（Registry）或网络。异常：空权威
        抛出 ``ValueError``；持久资源身份损坏抛出
        ``TemplateProjectionIdentityConflict``，不返回部分数据。
        """

        generation = self.load_generation(authority_id=authority_id)
        return generation.node_templates, generation.handle_templates

    def load_generation(
        self,
        *,
        authority_id: str,
    ) -> RegistryTemplateProjectionGeneration:
        """读取一个权威当前提交的完整设备注册表模板投影代际。

        参数说明：``authority_id`` 隔离不同投影来源。返回：在同一数据库视图中
        读取的节点、连接点（Handle）、资源模板（ResourceTemplate）源码身份映射
        和单调代际号；尚未发布时返回代际 ``0`` 与三个空集合。异常：持久映射被
        破坏时抛出 ``TemplateProjectionIdentityConflict``，不返回部分快照。
        """

        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ValueError("模板投影权威不能为空")
        with self._workflow_store.transaction() as connection:
            nodes, handles = self._load(connection, authority_id=authority_id)
            try:
                generation, symbols = self._generation_persistence.load_in_transaction(
                    connection,
                    authority_id=authority_id,
                )
            except TemplateProjectionGenerationDataError as error:
                raise TemplateProjectionIdentityConflict(str(error)) from error
            return RegistryTemplateProjectionGeneration(
                authority_id=authority_id,
                generation=generation,
                node_templates=nodes,
                handle_templates=handles,
                resource_template_symbols=symbols,
            )

    def close(self) -> None:
        """关闭本适配器拥有的工作流存储连接。"""

        self._workflow_store.close()

    @staticmethod
    def _node_business_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
        """取得节点模板的大小写敏感活动业务唯一键。

        参数说明：``candidate`` 是节点模板候选；返回值严格采用后端（Backend）
        当前规范 ``(resource_template_uuid, name)``，不做 trim 或大小写折叠。
        """

        resource_template_uuid = candidate.get("resource_template_uuid")
        name = candidate.get("name")
        if not isinstance(resource_template_uuid, str) or not resource_template_uuid:
            raise TemplateProjectionIdentityConflict("节点模板缺少资源模板 UUID")
        if not isinstance(name, str) or not name:
            raise TemplateProjectionIdentityConflict("节点模板缺少动作业务名")
        return resource_template_uuid, name

    @staticmethod
    def _handle_parent_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
        """取得句柄模板候选引用的节点活动业务唯一键。

        参数说明：``candidate`` 是尚未解析父 UUID 的句柄候选；返回值由节点资源
        模板 UUID 和动作业务名组成。
        """

        parent_key = candidate.get("node_business_key")
        if (
            not isinstance(parent_key, (list, tuple))
            or len(parent_key) != 2
            or not all(isinstance(part, str) and part for part in parent_key)
        ):
            raise TemplateProjectionIdentityConflict("句柄模板缺少节点业务身份")
        return str(parent_key[0]), str(parent_key[1])

    @staticmethod
    def _handle_business_key(
        candidate: Mapping[str, Any],
        parent_uuid: str,
    ) -> tuple[str, str, str]:
        """取得句柄模板的大小写敏感活动业务唯一键。

        参数说明：``candidate`` 提供连接点名和方向，``parent_uuid`` 是本轮解析的
        节点模板 UUID；返回后端（Backend）规范三元组。
        """

        handle_key = candidate.get("handle_key")
        io_type = candidate.get("io_type")
        if not isinstance(handle_key, str) or not handle_key:
            raise TemplateProjectionIdentityConflict("句柄模板缺少连接点业务名")
        if io_type not in {"source", "target"}:
            raise TemplateProjectionIdentityConflict(
                "句柄模板方向必须是 source 或 target"
            )
        return parent_uuid, handle_key, str(io_type)

    @staticmethod
    def _resolve_node_uuid(
        connection: Any,
        *,
        authority_id: str,
        candidate: Mapping[str, Any],
        business_key: tuple[str, str],
    ) -> str:
        """只读解析节点模板候选应使用的稳定 UUID。

        参数：``connection`` 是当前模板事务；``authority_id`` 是投影权威；
        ``candidate`` 是完整节点候选；``business_key`` 是活动业务唯一键。
        返回：显式 UUID、既有活动业务键 UUID，或新分配 UUID。
        异常：显式 UUID 非法、改绑业务身份或争用活动业务键时抛出
        ``TemplateProjectionIdentityConflict``，不写数据库。
        """

        explicit_uuid = candidate.get("uuid")
        existing_by_key = connection.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE resource_template_uuid = ? AND name = ? AND deleted_at IS NULL
            """,
            business_key,
        ).fetchone()
        existing_by_uuid = None
        if explicit_uuid is not None:
            if not isinstance(explicit_uuid, str) or not explicit_uuid:
                raise TemplateProjectionIdentityConflict("节点模板显式 UUID 非法")
            existing_by_uuid = connection.execute(
                "SELECT * FROM workflow_node_template WHERE uuid = ?",
                (explicit_uuid,),
            ).fetchone()
            if (
                existing_by_uuid is not None
                and (
                    existing_by_uuid["resource_template_uuid"],
                    existing_by_uuid["name"],
                )
                != business_key
            ):
                raise TemplateProjectionIdentityConflict(
                    "节点模板 UUID 不得改绑业务身份"
                )
            if existing_by_key is not None and existing_by_key["uuid"] != explicit_uuid:
                raise TemplateProjectionIdentityConflict(
                    "节点活动业务唯一键已有其他 UUID"
                )
            return explicit_uuid
        if existing_by_key is not None:
            return str(existing_by_key["uuid"])
        # ``authority_id`` 参与调用合同以明确身份作用域；当前共享表业务唯一键为全局。
        _ = authority_id
        return str(uuid4())

    @staticmethod
    def _resolve_handle_uuid(
        connection: Any,
        *,
        candidate: Mapping[str, Any],
        business_key: tuple[str, str, str],
    ) -> str:
        """只读解析连接点（Handle）模板候选应使用的稳定 UUID。

        参数：``connection`` 是当前模板事务；``candidate`` 是完整连接点候选；
        ``business_key`` 由父节点 UUID、连接点名和方向组成。
        返回：显式 UUID、既有活动业务键 UUID，或新分配 UUID。
        异常：显式 UUID 非法、改绑父节点/业务身份或争用活动业务键时抛出
        ``TemplateProjectionIdentityConflict``，不写数据库。
        """

        explicit_uuid = candidate.get("uuid")
        existing_by_key = connection.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE workflow_node_template_uuid = ? AND handle_key = ?
              AND io_type = ? AND deleted_at IS NULL
            """,
            business_key,
        ).fetchone()
        existing_by_uuid = None
        if explicit_uuid is not None:
            if not isinstance(explicit_uuid, str) or not explicit_uuid:
                raise TemplateProjectionIdentityConflict("句柄模板显式 UUID 非法")
            existing_by_uuid = connection.execute(
                "SELECT * FROM workflow_handle_template WHERE uuid = ?",
                (explicit_uuid,),
            ).fetchone()
            if (
                existing_by_uuid is not None
                and (
                    existing_by_uuid["workflow_node_template_uuid"],
                    existing_by_uuid["handle_key"],
                    existing_by_uuid["io_type"],
                )
                != business_key
            ):
                raise TemplateProjectionIdentityConflict(
                    "句柄模板 UUID 不得改绑业务身份"
                )
            if existing_by_key is not None and existing_by_key["uuid"] != explicit_uuid:
                raise TemplateProjectionIdentityConflict(
                    "句柄活动业务唯一键已有其他 UUID"
                )
            return explicit_uuid
        if existing_by_key is not None:
            return str(existing_by_key["uuid"])
        return str(uuid4())

    def _upsert_node(
        self,
        connection: Any,
        *,
        authority_id: str,
        candidate: Mapping[str, Any],
        business_key: tuple[str, str],
        now: str,
    ) -> str:
        """按显式 UUID 或活动业务唯一键写入一个节点模板。

        参数：``connection`` 是当前唯一写事务；``authority_id`` 是模板来源权威；
        ``candidate`` 是已经规范化的节点模板候选；``business_key`` 是来源内稳定
        业务唯一键；``now`` 是本事务统一更新时间。
        返回：本轮解析或创建的稳定节点模板 UUID。
        异常：显式 UUID 与活动业务身份冲突、候选字段无效或 SQL 写入失败时传播
        原始异常；同一事务不得留下部分节点模板投影（Template Projection）。
        """

        template_uuid = self._resolve_node_uuid(
            connection,
            authority_id=authority_id,
            candidate=candidate,
            business_key=business_key,
        )
        values = self._node_values(candidate)
        existing = connection.execute(
            "SELECT uuid FROM workflow_node_template WHERE uuid = ?",
            (template_uuid,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (template_uuid, now, now, *values[:2], authority_id, *values[2:]),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_node_template
                SET update_time = ?, deleted_at = NULL, description = ?, meta_data = ?,
                    authority_id = ?, resource_template_uuid = ?, name = ?,
                    display_name = ?, class = ?, goal = ?, goal_default = ?,
                    feedback = ?, result = ?, schema = ?, type = ?, icon = ?,
                    header = ?, footer = ?, node_type = ?
                WHERE uuid = ?
                """,
                (now, *values[:2], authority_id, *values[2:], template_uuid),
            )
        return template_uuid

    def _upsert_handle(
        self,
        connection: Any,
        *,
        authority_id: str,
        candidate: Mapping[str, Any],
        parent_uuid: str,
        business_key: tuple[str, str, str],
        now: str,
    ) -> str:
        """按显式 UUID 或活动业务唯一键写入一个句柄模板。

        参数：``connection`` 是当前模板代际的唯一 SQLite 写事务；
        ``authority_id`` 是拥有该模板的注册表权威；``candidate`` 是已规范化连接点
        （Handle）候选；``parent_uuid`` 是已解析父节点模板身份；``business_key``
        是权威内活动业务唯一键；``now`` 是本事务统一时间。
        返回：复用显式/既有身份或新建得到的连接点模板稳定 UUID。
        异常：候选身份、父子关系或活动业务键冲突时抛出
        ``TemplateProjectionIdentityConflict``；SQL 错误原样传播。
        不变量：身份解析、行写入和清单发布属于调用者同一事务，不允许部分提交。
        """

        handle_uuid = self._resolve_handle_uuid(
            connection,
            candidate=candidate,
            business_key=business_key,
        )
        values = self._handle_values(candidate, parent_uuid=parent_uuid)
        existing = connection.execute(
            "SELECT uuid FROM workflow_handle_template WHERE uuid = ?",
            (handle_uuid,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, workflow_node_template_uuid,
                    handle_key, io_type, display_name, type, required,
                    data_source, data_key
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (handle_uuid, now, now, *values[:2], authority_id, *values[2:]),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_handle_template
                SET update_time = ?, deleted_at = NULL, description = ?, meta_data = ?,
                    authority_id = ?, workflow_node_template_uuid = ?, handle_key = ?,
                    io_type = ?, display_name = ?, type = ?, required = ?,
                    data_source = ?, data_key = ?
                WHERE uuid = ?
                """,
                (now, *values[:2], authority_id, *values[2:], handle_uuid),
            )
        return handle_uuid

    @staticmethod
    def _node_values(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        """把节点候选转换为数据库列顺序。

        参数说明：``candidate`` 是已通过身份检查的节点模板；返回值不含 UUID、
        时间和权威字段，JSON 领域字段采用稳定编码。
        """

        return (
            candidate.get("description"),
            _json(candidate.get("meta_data") or {}),
            candidate["resource_template_uuid"],
            candidate["name"],
            candidate.get("display_name") or candidate["name"],
            candidate.get("class"),
            _json(candidate.get("goal") or {}),
            _json(candidate.get("goal_default") or {}),
            _json(candidate.get("feedback") or {}),
            _json(candidate.get("result") or {}),
            _schema_text(candidate.get("schema")),
            candidate.get("type") or "UniLabJsonCommand",
            candidate.get("icon"),
            candidate.get("header"),
            candidate.get("footer"),
            candidate.get("node_type") or "device_action",
        )

    @staticmethod
    def _handle_values(
        candidate: Mapping[str, Any],
        *,
        parent_uuid: str,
    ) -> tuple[Any, ...]:
        """把句柄候选转换为数据库列顺序。

        参数说明：``candidate`` 是句柄模板，``parent_uuid`` 是已解析父节点身份；
        返回值不含 UUID、时间和权威字段。
        """

        return (
            candidate.get("description"),
            _json(candidate.get("meta_data") or {}),
            parent_uuid,
            candidate["handle_key"],
            candidate["io_type"],
            candidate.get("display_name") or candidate["handle_key"],
            candidate.get("type") or "any",
            int(bool(candidate.get("required"))),
            candidate.get("data_source"),
            candidate.get("data_key"),
        )

    @staticmethod
    def _soft_delete_removed(
        connection: Any,
        *,
        authority_id: str,
        removed_members: Sequence[TemplateProjectionMember],
        now: str,
    ) -> None:
        """只软删除差量确认移除的节点和连接点模板成员。

        参数：``connection`` 是模板写事务；``authority_id`` 限定删除权威；
        ``removed_members`` 是旧 manifest 中退出完整候选的成员；``now`` 是统一
        事务时间。返回：无；资源模板源码映射没有共享模板行，直接跳过。
        异常：成员跨权威或种类非法时抛出 ``TemplateProjectionIdentityConflict``。
        """

        table_by_kind = {
            "handle_template": "workflow_handle_template",
            "node_template": "workflow_node_template",
        }
        for member in sorted(
            removed_members,
            key=_removed_member_sort_key,
        ):
            if member.authority_id != authority_id:
                raise TemplateProjectionIdentityConflict("模板移除成员跨越权威")
            table = table_by_kind.get(member.projection_kind)
            if table is None:
                if member.projection_kind == "resource_template_symbol":
                    continue
                raise TemplateProjectionIdentityConflict("模板移除成员种类非法")
            connection.execute(
                f"""
                UPDATE {table}
                SET deleted_at = ?, update_time = ?
                WHERE authority_id = ? AND uuid = ? AND deleted_at IS NULL
                """,
                (now, now, authority_id, member.target_uuid),
            )

    @staticmethod
    def _load(
        connection: Any,
        *,
        authority_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """在一个数据库视图内装载节点与句柄语义实体。

        参数说明：``connection`` 是同一事务连接，``authority_id`` 限定投影来源；
        返回值排除时间戳，避免目录指纹随刷新时间漂移。
        """

        node_rows = connection.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE authority_id = ? AND deleted_at IS NULL
            ORDER BY uuid
            """,
            (authority_id,),
        ).fetchall()
        handle_rows = connection.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE authority_id = ? AND deleted_at IS NULL
            ORDER BY uuid
            """,
            (authority_id,),
        ).fetchall()
        nodes = [
            {
                "uuid": row["uuid"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "description": row["description"],
                "meta_data": _load_json(row["meta_data"], {}),
                "resource_template_uuid": row["resource_template_uuid"],
                "name": row["name"],
                "display_name": row["display_name"],
                "class": row["class"],
                "goal": _load_json(row["goal"], {}),
                "goal_default": _load_json(row["goal_default"], {}),
                "feedback": _load_json(row["feedback"], {}),
                "result": _load_json(row["result"], {}),
                "schema": _load_schema(row["schema"]),
                "type": row["type"],
                "icon": row["icon"],
                "header": row["header"],
                "footer": row["footer"],
                "node_type": row["node_type"],
            }
            for row in node_rows
        ]
        handles = [
            {
                "uuid": row["uuid"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "description": row["description"],
                "meta_data": _load_json(row["meta_data"], {}),
                "workflow_node_template_uuid": row["workflow_node_template_uuid"],
                "handle_key": row["handle_key"],
                "io_type": row["io_type"],
                "display_name": row["display_name"],
                "type": row["type"],
                "required": bool(row["required"]),
                "data_source": row["data_source"],
                "data_key": row["data_key"],
            }
            for row in handle_rows
        ]
        return nodes, handles


def _json(value: Any) -> str:
    """把领域 JSON 值编码为稳定文本。

    参数说明：``value`` 是节点或句柄模板字段；返回排序且禁止 NaN 的 JSON。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json(value: str | None, fallback: Any) -> Any:
    """读取数据库 JSON 文本。

    参数说明：``value`` 是可空文本，``fallback`` 是空值默认；返回解码后的值。
    """

    if value is None or value == "":
        return fallback
    return json.loads(value)


def _schema_text(value: Any) -> str | None:
    """把动作 JSON Schema 转换为数据库文本。

    参数说明：``value`` 可为空、文本或 JSON 对象；返回可持久化文本。
    """

    if value is None or isinstance(value, str):
        return value
    return _json(value)


def _load_schema(value: str | None) -> str | None:
    """按后端（Backend）线合同读取工作流节点模板（WorkflowNodeTemplate）参数 Schema。

    参数说明：``value`` 是 SQLite 可空 Schema 文本列。返回：非空值保持原始 JSON
    字符串，空值返回 ``None``，使注册表模板投影（Registry Template
    Projection）、工作流服务（WorkflowService）和 HTTP 始终共享后端（Backend）
    工作流节点模板（WorkflowNodeTemplate）的 ``*string`` 语义。异常：无；JSON
    Schema 的语法与值语义由图校验（Graph Validation）在候选版本（Candidate）
    签发时关闭式校验。
    """

    return None if value is None or value == "" else value


__all__ = [
    "RegistryTemplateProjectionGeneration",
    "RegistryTemplateProjectionStore",
    "TemplateProjectionIdentityConflict",
]
