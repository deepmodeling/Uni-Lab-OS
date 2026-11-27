"""把完成构建的设备注册表（Registry）发布为工作流创作模板投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
    goal_parameter_schema,
)
from unilabos.registry.in_memory_template_projection_store import (
    InMemoryRegistryTemplateProjectionStore,
)
from unilabos.registry.template_delta import (
    TemplateProjectionDelta,
    TemplateProjectionDeltaError,
    build_template_projection_delta,
)
from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_handle_uuid,
    action_template_uuid,
    device_template_uuid,
)
from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionGeneration,
    RegistryTemplateProjectionStore,
    TemplateProjectionIdentityConflict,
)


class RegistryTemplateProjectionError(ValueError):
    """设备注册表不能安全投影为规范模板。"""


def _condition_parameter_schema() -> dict[str, Any]:
    """返回条件节点参数的展示合同。

    返回值：一个新的 JSON Schema 风格字典，说明调度器读取的变量、绑定和有序
    分支字段。该合同只用于目录和前端编辑提示，真正的结构校验仍由工作流编译器
    与执行计划构建器负责，避免两套校验规则产生分歧。
    """

    return {
        "type": "object",
        "description": "条件节点按顺序检查分支；第一个为真的分支会被执行。",
        "properties": {
            "variables": {
                "type": "object",
                "description": "条件表达式使用的初始变量；也可由 bindings 提供。",
            },
            "bindings": {
                "type": "object",
                "description": "变量来源，可引用 workflow_input 或 node_result。",
                "additionalProperties": {"type": "object"},
            },
            "branches": {
                "type": "array",
                "description": "按执行顺序排列的分支；最后一个分支可用 condition=null 表示兜底。",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "condition": {"type": ["object", "null"]},
                        "node_uuids": {"type": "array", "items": {"type": "string"}},
                        "entry_node_uuids": {"type": "array", "items": {"type": "string"}},
                        "exit_node_uuids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["label", "node_uuids", "entry_node_uuids", "exit_node_uuids"],
                },
            },
            "predecessor_node_uuids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "进入条件节点前必须完成的节点。",
            },
        },
        "required": ["branches"],
    }


def _repeat_until_parameter_schema() -> dict[str, Any]:
    """返回循环节点参数的展示合同。

    返回值：一个新的 JSON Schema 风格字典，说明循环上限、carry、退出条件和
    区域边界字段。该合同用于前端生成参数编辑提示，不替代执行计划的严格校验。
    """

    return {
        "type": "object",
        "description": "循环节点逐轮执行区域内节点，直到 until 严格为真或达到上限。",
        "properties": {
            "loop_variable": {"type": "string", "description": "循环状态名称。"},
            "max_iterations": {"type": "integer", "minimum": 1, "description": "最大执行轮数。"},
            "initial_carry": {"type": "object", "description": "第一轮 carry 的初始值或来源。"},
            "next_carry": {"type": "object", "description": "下一轮 carry 的值或来源。"},
            "until": {"type": "object", "description": "每轮完成后计算的退出条件表达式。"},
            "bindings": {"type": "object", "description": "退出条件变量来源，可引用 workflow_input 或 node_result。"},
            "variables": {"type": "object", "description": "退出条件使用的初始工作流输入值。"},
            "node_uuids": {"type": "array", "items": {"type": "string"}, "description": "循环体内的节点。"},
            "entry_node_uuids": {"type": "array", "items": {"type": "string"}},
            "exit_node_uuids": {"type": "array", "items": {"type": "string"}},
            "predecessor_node_uuids": {"type": "array", "items": {"type": "string"}},
            "successor_node_uuids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "max_iterations",
            "initial_carry",
            "next_carry",
            "until",
            "bindings",
            "node_uuids",
            "entry_node_uuids",
            "exit_node_uuids",
        ],
    }


def _definition_business_id(definition: Mapping[str, Any]) -> str:
    """读取注册表定义的稳定业务身份排序键。

    参数：``definition`` 是同一完整注册表快照中的设备或资源定义。
    返回：``id`` 字段的字符串表示。
    异常：无；字段合法性由后续编译函数关闭式校验。
    """

    return str(definition.get("id", ""))


def compile_resource_template_source_aliases(
    resource_definitions: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """编译可安全用于创作的资源模板源码别名。

    参数说明：``resource_definitions`` 是同一注册表快照（Registry Snapshot）的
    完整资源模板（ResourceTemplate）定义。返回：规范 Python 源码身份到资源
    模板业务 ID 的映射；显式 ``source_fqid`` 优先。仅当 ``class.module`` 在
    全代际恰好有一个资源模板（ResourceTemplate）所有者，且该所有者没有显式
    ``source_fqid`` 时，才保留遗留兼容别名。异常：业务 ID、显式源码身份或
    实现类身份缺失、非法，或两个业务模板显式声明同一 ``source_fqid`` 时抛出
    ``RegistryTemplateProjectionError``。多个业务模板合法复用同一实现类时，
    该实现类因无法唯一解析而不进入返回映射，不得猜测其中任一模板。
    """

    # ``explicit_owner_by_alias`` 保存作者明确声明的一一源码身份及其业务模板。
    explicit_owner_by_alias: dict[str, str] = {}
    # ``class_owners_by_alias`` 汇总全代际所有实现类所有者；显式源码身份不能把
    # 自身从计数中隐藏，否则“显式 + 遗留”共享类会被误判为唯一兼容别名。
    class_owners_by_alias: dict[str, set[str]] = {}
    # ``templates_with_explicit_source`` 标记已脱离遗留兼容策略的业务模板。
    templates_with_explicit_source: set[str] = set()
    for definition in resource_definitions:
        # ``template_name`` 是库存同步和 UUID 生命周期使用的资源模板业务 ID。
        template_name = definition.get("id")
        if not isinstance(template_name, str) or not template_name:
            raise RegistryTemplateProjectionError("资源模板缺少业务唯一名称")
        # ``class_definition`` 提供遗留 YAML 模板可复用的 Python 实现类身份。
        class_definition = definition.get("class")
        # ``raw_class_module`` 是尚未规范化的实现类身份；它可以被多个模板复用。
        raw_class_module = (
            class_definition.get("module")
            if isinstance(class_definition, Mapping)
            else None
        )
        # ``raw_source_fqid`` 是作者明确声明、必须一一对应的资源源码身份。
        raw_source_fqid = definition.get("source_fqid")
        if raw_source_fqid in (None, "") and raw_class_module in (None, ""):
            raise RegistryTemplateProjectionError(
                f"资源模板缺少源码身份: {template_name}"
            )
        try:
            # ``class_module`` 只验证实际声明的实现类；显式源码身份不掩盖非法驱动路径。
            class_module = (
                canonical_python_source_identity(raw_class_module)
                if raw_class_module not in (None, "")
                else None
            )
            # ``source_fqid`` 保持作者声明优先，不由可复用实现类覆盖。
            source_fqid = (
                canonical_python_source_identity(raw_source_fqid)
                if raw_source_fqid not in (None, "")
                else None
            )
        except PythonSourceIdentityError as error:
            raise RegistryTemplateProjectionError(
                f"资源模板源码身份不能安全解析: {template_name}"
            ) from error
        if class_module is not None:
            class_owners_by_alias.setdefault(class_module, set()).add(template_name)
        if source_fqid is not None:
            # ``previous_name`` 检测两个显式声明争用同一稳定资源源码身份。
            previous_name = explicit_owner_by_alias.get(source_fqid)
            if previous_name is not None and previous_name != template_name:
                raise RegistryTemplateProjectionError(
                    "资源模板源码身份不得绑定多个注册表（Registry）业务 ID: "
                    f"{source_fqid}"
                )
            explicit_owner_by_alias[source_fqid] = template_name
            templates_with_explicit_source.add(template_name)
            continue
        if class_module is None:
            raise RegistryTemplateProjectionError(
                f"资源模板缺少源码身份: {template_name}"
            )

    # ``template_name_by_alias`` 先保留所有显式声明，使遗留回退不能覆盖作者身份。
    template_name_by_alias = dict(explicit_owner_by_alias)
    for source_alias, template_names in class_owners_by_alias.items():
        # ``template_name`` 只有在全代际唯一时才是候选兼容来源所有者。
        if len(template_names) != 1:
            continue
        template_name = next(iter(template_names))
        # 显式声明模板不再同时发布实现类别名；已有显式别名也不能被遗留入口覆盖。
        if (
            template_name not in templates_with_explicit_source
            and source_alias not in template_name_by_alias
        ):
            template_name_by_alias[source_alias] = template_name
    return template_name_by_alias


class RegistryTemplateProjection:
    """发布并提供单代不可变设备动作模板快照的深模块。"""

    def __init__(
        self,
        workflow_store: WorkflowStore | None,
        *,
        authority_id: str,
        resource_template_identity_resolver: Callable[[str], str],
        generation_extension: Callable[
            [Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
            tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
        ]
        | None = None,
        projection_store: Any | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        """装配设备注册表（Registry）模板投影及资源模板身份解析器。

        参数说明：``workflow_store`` 持有现有工作流模板表和唯一 SQLite 事务；
        ``authority_id`` 标识本地投影来源；
        ``resource_template_identity_resolver`` 把设备注册身份映射为稳定资源模板
        （ResourceTemplate）UUID；``generation_extension`` 可在持久提交前追加
        同代框架拥有模板，不能执行第二次替换。返回：无；构造时从同一投影代际
        恢复内存快照。
        异常：已持久化目录或资源身份不一致时抛出
        ``RegistryTemplateProjectionError``，不得发布部分目录。
        """

        if projection_store is not None and workflow_store is not None:
            raise TypeError("模板投影不能同时绑定 SQLite 与内存存储")
        if projection_store is None:
            if workflow_store is None:
                raise TypeError("模板投影缺少存储适配器")
            projection_store = RegistryTemplateProjectionStore(workflow_store)
        self._store = projection_store
        self._close_callback = close_callback
        self._authority_id = authority_id
        self._resource_template_identity_resolver = resource_template_identity_resolver
        if generation_extension is not None and not callable(generation_extension):
            raise TypeError("generation_extension 必须是可调用对象")
        self._generation_extension = generation_extension
        try:
            generation = self._store.load_generation(authority_id=authority_id)
            self._snapshot = AuthoringCatalogSnapshot.from_entities(
                generation.node_templates,
                generation.handle_templates,
                resource_template_symbols=generation.resource_template_symbols,
            )
            self._last_delta = build_template_projection_delta(
                authority_id=authority_id,
                current_generation=generation.generation,
                previous_members=(),
                candidate_members=(),
            )
        except (AuthoringCatalogError, TemplateProjectionIdentityConflict) as error:
            raise RegistryTemplateProjectionError(str(error)) from error

    @classmethod
    def in_memory(
        cls,
        *,
        authority_id: str,
        resource_template_identity_resolver: Callable[[str], str],
        generation_extension: Callable[
            [Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
            tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
        ]
        | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> "RegistryTemplateProjection":
        """建立不读写 SQLite 模板表的进程内投影。

        参数：权威、物料模板身份解析器及可选发布工作流扩展与普通构造函数一致；
        ``close_callback`` 仅释放扩展函数拥有的工作流事实存储。返回：空代际投影，
        首次 ``refresh`` 完整校验成功后才发布目录。
        """

        return cls(
            None,
            authority_id=authority_id,
            resource_template_identity_resolver=resource_template_identity_resolver,
            generation_extension=generation_extension,
            projection_store=InMemoryRegistryTemplateProjectionStore(),
            close_callback=close_callback,
        )

    def refresh(self, registry: Any) -> AuthoringCatalogSnapshot:
        """从一次完整设备注册表快照原子发布新模板代际。

        参数说明：``registry`` 是已冻结快照或同时提供设备与资源定义读取接口的
        设备注册表（Registry）。返回：新提交的不可变工作流创作目录快照；节点、
        连接点（Handle）和资源模板（ResourceTemplate）源码身份在同一事务替换。
        异常：快照、动作合同、资源身份或持久模板不一致时抛出
        ``RegistryTemplateProjectionError``；事务失败时旧内存快照和旧持久投影
        保持不变。
        """

        try:
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
        device_definitions = registry_snapshot.detached_devices()
        resource_definitions = registry_snapshot.detached_resources()
        nodes, handles = self._compile(device_definitions)
        if self._generation_extension is not None:
            try:
                extension_nodes, extension_handles = self._generation_extension(
                    tuple(nodes),
                    tuple(handles),
                )
                nodes.extend(dict(item) for item in extension_nodes)
                handles.extend(dict(item) for item in extension_handles)
            except RegistryTemplateProjectionError:
                raise
            except (TypeError, ValueError) as error:
                raise RegistryTemplateProjectionError(str(error)) from error
        resource_template_symbols = self._compile_resource_template_identities(
            resource_definitions
        )
        # ``next_snapshot`` 由事务内完整目录校验器赋值；仅在提交成功后发布到内存。
        next_snapshot: AuthoringCatalogSnapshot | None = None

        def validate_generation(
            generation: RegistryTemplateProjectionGeneration,
        ) -> None:
            """在 SQLite 提交前验证完整工作流创作目录代际。

            参数说明：``generation`` 是同一事务已写入但尚未提交的节点、连接点
            （Handle）和资源模板（ResourceTemplate）身份全集。返回：无；成功时
            把不可变工作流创作目录（Authoring Catalog）快照保存到闭包变量；目录
            身份、连接点或资源映射冲突时抛出 ``AuthoringCatalogError``，由投影
            存储深模块回滚整个代际。
            """

            nonlocal next_snapshot
            next_snapshot = AuthoringCatalogSnapshot.from_entities(
                generation.node_templates,
                generation.handle_templates,
                resource_template_symbols=generation.resource_template_symbols,
            )

        try:
            _committed_generation, delta = self._store.replace_generation_with_delta(
                authority_id=self._authority_id,
                node_templates=nodes,
                handle_templates=handles,
                resource_template_symbols=resource_template_symbols,
                validate_generation=validate_generation,
            )
        except (
            TemplateProjectionDeltaError,
            TemplateProjectionIdentityConflict,
        ) as error:
            raise RegistryTemplateProjectionError(f"模板身份冲突: {error!s}") from error
        except AuthoringCatalogError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
        if next_snapshot is None:
            raise RegistryTemplateProjectionError("模板投影未执行完整目录校验")
        # 内存快照和最近差量只在 SQLite 事务成功提交后共同替换。
        self._snapshot = next_snapshot
        self._last_delta = delta
        return next_snapshot

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回最近一次已提交的不可变模板快照。

        返回值不触发设备注册表扫描、网络读取或数据库重新导入。
        """

        return self._snapshot

    def last_delta(self) -> TemplateProjectionDelta:
        """返回最近一次成功刷新产生的模板投影差量。

        参数：无。
        返回：提交后保存的不可变模板投影差量（TemplateProjectionDelta）；构造后
        尚未刷新时返回当前持久代际上的空差量。
        异常：无；失败刷新不会替换最近成功差量。
        """

        return self._last_delta

    def close(self) -> None:
        """关闭投影及其显式拥有的外部资源。"""

        self._store.close()
        if self._close_callback is not None:
            callback = self._close_callback
            self._close_callback = None
            callback()

    def _compile(
        self,
        device_definitions: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把完整设备定义规范化为节点和句柄模板候选。

        参数说明：``device_definitions`` 是设备注册表已完成构建的只读快照；返回
        两个完整候选集合，只接收第 2 版强类型动作合同（Action Contract）。
        异常：输入不是数组或任一设备/动作合同无效时抛出
        ``RegistryTemplateProjectionError``，不返回部分候选。
        """

        if not isinstance(device_definitions, Sequence) or isinstance(
            device_definitions, (str, bytes)
        ):
            raise RegistryTemplateProjectionError("设备注册表快照必须是数组")
        nodes: list[dict[str, Any]] = []
        handles: list[dict[str, Any]] = []
        for device in sorted(
            device_definitions,
            key=_definition_business_id,
        ):
            node_candidates, handle_candidates = self._compile_device(device)
            nodes.extend(node_candidates)
            handles.extend(handle_candidates)
        return nodes, handles

    def _compile_resource_template_identities(
        self,
        resource_definitions: Sequence[Mapping[str, Any]],
    ) -> dict[str, str]:
        """冻结无歧义源码资源符号到既有资源模板 UUID 的一一映射。

        参数说明：``resource_definitions`` 是同代设备注册表（Registry）资源模板
        （ResourceTemplate）全集。
        返回：以显式 ``source_fqid`` 或唯一遗留实现类源码符号为键、本地模板
        UUID 为值的新字典；业务唯一名缺失、身份解析失败、显式身份冲突或 UUID
        被多个符号复用时关闭式失败。共享 ``class.module`` 的遗留模板仍按业务 ID
        同步库存身份，但不猜测源码符号映射。
        异常：资源定义或本地资源模板（ResourceTemplate）身份无效、歧义或冲突时
        抛出 ``RegistryTemplateProjectionError``。
        """

        # ``template_uuid_by_symbol`` 供工作流源码（Workflow Source）编译使用；
        # ``symbol_by_template_uuid`` 防止两个源码符号静默绑定同一模板身份。
        template_uuid_by_symbol: dict[str, str] = {}
        symbol_by_template_uuid: dict[str, str] = {}
        # ``resource_name_by_symbol`` 是已执行显式优先和实现复用消歧的源码映射。
        resource_name_by_symbol = compile_resource_template_source_aliases(
            resource_definitions
        )

        def source_alias_sort_key(item: tuple[str, str]) -> str:
            """读取资源模板源码别名的稳定排序键。

            参数：``item`` 是源码符号与资源业务名二元组。返回：源码符号。
            异常：无；输入由已验证别名映射产生。
            """

            return item[0]

        for source_symbol, resource_name in sorted(
            resource_name_by_symbol.items(),
            key=source_alias_sort_key,
        ):
            try:
                template_uuid = validate_uuid(
                    self._resource_template_identity_resolver(resource_name)
                )
            except (KeyError, TypeError, ValueError):
                raise RegistryTemplateProjectionError(
                    f"资源模板身份解析失败: {resource_name}"
                ) from None
            if source_symbol in template_uuid_by_symbol:
                raise RegistryTemplateProjectionError("资源模板源码身份重复")
            previous_symbol = symbol_by_template_uuid.get(template_uuid)
            if previous_symbol is not None and previous_symbol != source_symbol:
                raise RegistryTemplateProjectionError(
                    "资源模板 UUID 不得绑定多个源码身份"
                )
            template_uuid_by_symbol[source_symbol] = template_uuid
            symbol_by_template_uuid[template_uuid] = source_symbol
        return template_uuid_by_symbol

    def _compile_device(
        self,
        device: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """规范化一个设备定义中的全部强类型动作。

        参数说明：``device`` 是单个设备注册表（Registry）设备条目；返回该设备
        的节点和连接点（Handle）候选，旧式或自动生成动作不进入可信工作流创作
        投影。异常：条目、类合同、动作映射、资源模板（ResourceTemplate）身份
        或第 2 版动作合同（Action Contract）非法时抛出
        ``RegistryTemplateProjectionError``，不得返回部分设备模板。
        """

        if not isinstance(device, Mapping):
            raise RegistryTemplateProjectionError("设备注册表条目必须是对象")
        # 设备模板是内存目录成员；稳定业务名直接生成 UUID，不查询库存 SQLite。
        resource_identity = device.get("id")
        if not isinstance(resource_identity, str) or not resource_identity:
            raise RegistryTemplateProjectionError("设备定义缺少稳定资源身份")
        try:
            resource_template_uuid = device_template_uuid(resource_identity)
        except TemplateIdentityError as error:
            raise RegistryTemplateProjectionError(str(error)) from error

        class_definition = device.get("class")
        if not isinstance(class_definition, Mapping):
            raise RegistryTemplateProjectionError("设备定义缺少类合同")
        class_identity = class_definition.get("module")
        if not isinstance(class_identity, str) or not class_identity:
            raise RegistryTemplateProjectionError("设备定义缺少类身份")
        action_mappings = class_definition.get("action_value_mappings") or {}
        if not isinstance(action_mappings, Mapping):
            raise RegistryTemplateProjectionError("设备动作映射必须是对象")
        resource_name = str(device.get("id") or resource_identity)
        resource_display_name = str(
            device.get("display_name") or device.get("displayname") or resource_name
        )

        nodes: list[dict[str, Any]] = []
        handles: list[dict[str, Any]] = []
        for action_name in sorted(action_mappings):
            action = action_mappings[action_name]
            if not isinstance(action, Mapping):
                raise RegistryTemplateProjectionError("设备动作定义必须是对象")
            contract_kind = action.get("contract_kind")
            if contract_kind == "invalid_typed":
                diagnostic = action.get("contract_diagnostic")
                diagnostic_message = (
                    diagnostic.get("message")
                    if isinstance(diagnostic, Mapping)
                    else None
                )
                raise RegistryTemplateProjectionError(
                    f"强类型动作合同 {action_name} 无效"
                    + (f": {diagnostic_message}" if diagnostic_message else "")
                )
            if contract_kind != "typed":
                continue
            try:
                node, action_handles = self._compile_action(
                    action_name=str(action_name),
                    action=action,
                    class_identity=class_identity,
                    resource_template_uuid=resource_template_uuid,
                    resource_name=resource_name,
                    resource_display_name=resource_display_name,
                    resource_template_identity_resolver=(
                        self._resource_template_identity_resolver
                    ),
                )
            except (ActionTemplateProjectionError, TemplateIdentityError) as error:
                raise RegistryTemplateProjectionError(str(error)) from error
            nodes.append(node)
            handles.extend(action_handles)
        if resource_name == "host_node":
            # 框架本地服务只生成类型合同；不注册为设备或添加设备实例。
            import ast
            import inspect
            from pathlib import Path

            from unilabos.registry.action_contract_schema import parse_action_contract
            from unilabos.workflow import native_commands
            source_ast = ast.parse(
                Path(inspect.getfile(native_commands)).read_text(encoding="utf-8")
            )
            for declaration in source_ast.body:
                if not isinstance(declaration, ast.ClassDef):
                    continue
                identity = f"{native_commands.__name__}:{declaration.name}"
                if identity not in native_commands.COMMAND_CLASSES:
                    continue
                for method in declaration.body:
                    if not isinstance(method, ast.FunctionDef) or method.name.startswith("_"):
                        continue
                    schema = parse_action_contract(source_ast, method, module_name=native_commands.__name__).to_action_schema(action_name=method.name)
                    local_node, local_handles = self._compile_action(
                        action_name=method.name,
                        action={"schema": schema, "node_type": "workflow_command", "type": "workflow_command", "executor_kind": "workflow_command"},
                        class_identity=identity, resource_template_uuid=resource_template_uuid,
                        resource_name=resource_name, resource_display_name=resource_display_name,
                        resource_template_identity_resolver=self._resource_template_identity_resolver,
                    )
                    if declaration.name == "HumanCommands" and method.name == "form":
                        local_node["display_name"] = "人工判定表单"
                    nodes.append(local_node)
                    handles.extend(local_handles)
            try:
                framework_node, framework_handle = self._compile_material_source(
                    resource_template_uuid=resource_template_uuid,
                    resource_name=resource_name,
                    resource_display_name=resource_display_name,
                )
            except TemplateIdentityError as error:
                raise RegistryTemplateProjectionError(str(error)) from error
            nodes.append(framework_node)
            handles.append(framework_handle)
            handles.append(
                {
                    **framework_handle,
                    "uuid": action_handle_uuid(
                        str(framework_node["uuid"]),
                        io_type="target",
                        handle_key="mount",
                    ),
                    "handle_key": "mount",
                    "io_type": "target",
                    "display_name": "Mount",
                    "description": "运行时物料来源的上游挂载容器。",
                    "required": False,
                    "data_source": "goal",
                    "data_key": "mount",
                }
            )
            handles.append({
                **framework_handle,
                "uuid": action_handle_uuid(str(framework_node["uuid"]), io_type="source", handle_key="sufficient"),
                "handle_key": "sufficient", "display_name": "物料是否足够",
                "description": "本次检查能否取得符合要求的可用物料。",
                "type": "boolean", "data_key": "sufficient",
                "meta_data": {"unilab": {"value_schema": {"type": "boolean"}}},
            })
            for io_type in ("source", "target"):
                handles.append({
                    **framework_handle,
                    "uuid": action_handle_uuid(str(framework_node["uuid"]), io_type=io_type, handle_key="ready"),
                    "handle_key": "ready", "display_name": "执行顺序",
                    "description": "保留物料检查与前后实验步骤的执行依赖。",
                    "io_type": io_type, "type": "any", "data_key": "ready",
                    "data_source": "dependency", "meta_data": {"unilab": {"value_schema": {}}},
                })
            material_io_node, material_io_handles = self._compile_material_io(
                resource_template_uuid=resource_template_uuid,
                resource_name=resource_name,
                resource_display_name=resource_display_name,
            )
            nodes.append(material_io_node)
            handles.extend(material_io_handles)
            try:
                nodes.extend(
                    [
                        self._compile_group(
                            resource_template_uuid=resource_template_uuid,
                            resource_name=resource_name,
                            resource_display_name=resource_display_name,
                        ),
                        self._compile_condition(
                            resource_template_uuid=resource_template_uuid,
                            resource_name=resource_name,
                            resource_display_name=resource_display_name,
                        ),
                        self._compile_repeat_until(
                            resource_template_uuid=resource_template_uuid,
                            resource_name=resource_name,
                            resource_display_name=resource_display_name,
                        ),
                    ]
                )
            except TemplateIdentityError as error:
                raise RegistryTemplateProjectionError(str(error)) from error
        return nodes, handles

    @staticmethod
    def _compile_repeat_until(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> dict[str, Any]:
        """编译宿主节点唯一拥有的 RepeatUntil 控制区域模板。"""

        return {
            "uuid": action_template_uuid(resource_name, "repeat_until"),
            "resource_template_uuid": resource_template_uuid,
            "name": "repeat_until",
            "display_name": "重复直到",
            "description": "由调度器逐轮执行并在严格布尔条件满足时退出。",
            "class": "unilabos.workflow.authoring:repeat_until",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "repeat_until",
            "node_type": "repeat_until",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "executor_kind": "repeat_until",
                    "parameter_schema": _repeat_until_parameter_schema(),
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }

    @staticmethod
    def _compile_condition(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> dict[str, Any]:
        """编译宿主节点唯一拥有的结构化条件控制区域模板。"""

        return {
            "uuid": action_template_uuid(resource_name, "condition"),
            "resource_template_uuid": resource_template_uuid,
            "name": "condition",
            "display_name": "条件",
            "description": "由调度器本地求值并选择唯一分支的结构化控制区域。",
            "class": "unilabos.workflow.authoring:condition",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "condition",
            "node_type": "condition",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "executor_kind": "condition",
                    "parameter_schema": _condition_parameter_schema(),
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }

    @staticmethod
    def _compile_group(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> dict[str, Any]:
        """编译宿主节点（Host Node）唯一拥有的展示分组框架模板。

        参数说明：``resource_template_uuid`` 是宿主节点资源模板
        （ResourceTemplate）的稳定身份；``resource_name`` 与
        ``resource_display_name`` 进入可审计框架所有者摘要。返回：一个没有执行
        连接点（Handle）的 ``group`` 节点模板；持久投影按资源模板 UUID 和名称
        复用稳定模板身份。异常：无，入参已由调用者完成身份校验。
        """

        return {
            "uuid": action_template_uuid(resource_name, "group"),
            "resource_template_uuid": resource_template_uuid,
            "name": "group",
            "display_name": "分组",
            "description": "组织工作流节点的展示层级，不参与执行依赖。",
            "class": "unilabos.workflow.authoring:group",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "group",
            "node_type": "group",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }

    @staticmethod
    def _compile_material_source(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """编译宿主节点（Host Node）唯一拥有的物料来源框架模板。

        参数说明：``resource_template_uuid`` 是本地宿主节点（Host Node）资源模板
        （ResourceTemplate）的稳定身份；``resource_name`` 与
        ``resource_display_name`` 用于 HTTP 资源摘要。返回：一个物料来源
        （MaterialSource）框架节点和一个 source 物料占位符（ResourceSlot）；
        节点 Schema 另行发布物料来源专用库位选择器（SiteSelector）字段，稳定
        UUID 由持久投影按业务唯一键复用或首次分配。
        """

        node_business_key = (resource_template_uuid, "material_source")
        # ``site_selector`` 与普通动作使用同一扩展形状；物料来源（MaterialSource）
        # 的 occupant 是资源模板字段，而不是尚未准入产生的具体物料占位符。
        site_selector = {
            "version": 1,
            "owner": "mount",
            "occupant": "resource_template_uuid",
            "show_occupied": True,
            "allow_occupied": False,
        }
        # ``mount_schema`` 是拥有候选库位（Site）的部署物料稳定引用。
        mount_schema = {
            # 动态挂载由上游 Material 输出边在运行时提供；此时持久参数必须
            # 允许 None，公共图校验不能在执行计划解析该边之前把候选拒绝。
            "type": ["object", "null"],
            "properties": {
                "uuid": {"type": "string", "format": "uuid"},
            },
            "required": ["uuid"],
            "additionalProperties": False,
        }
        # ``site_schema`` 表达可选精确候选库位；省略仍表示全部兼容直接库位。
        site_schema = {
            "type": ["string", "null"],
            "format": "uuid",
            "x-unilabos-editor-control": "site_selector",
            "x-unilabos-site-selector": dict(site_selector),
        }
        node = {
            "uuid": action_template_uuid(resource_name, "material_source"),
            "resource_template_uuid": resource_template_uuid,
            "name": "material_source",
            "display_name": "Material Source",
            "description": "声明工作流进入边界的物料来源。",
            "class": "unilabos.workflow.authoring:material_source",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["existing"],
                    },
                    "resource_template_uuid": {
                        "type": "string",
                        "format": "uuid",
                    },
                    "supports_replenishment": {"type": "boolean", "default": False, "title": "支持补料"},
                    "mount": mount_schema,
                    "material_uuid": {
                        "type": ["string", "null"],
                        "format": "uuid",
                    },
                    "site": site_schema,
                    "slot_range": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "format": "uuid"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "flow_role": {
                        "type": "string",
                        "enum": [
                            "primary_sample",
                            "aliquot_sample",
                            "reagent",
                            "consumable",
                        ],
                    },
                    "custody_policy": {
                        "type": "string",
                        "enum": ["task_exclusive", "shared_source"],
                    },
                    "search_descendants": {
                        "type": "boolean",
                        "default": False,
                        "description": "按各层库位顺序递归搜索挂载资源的实际占位物料。",
                    },
                },
                "required": [
                    "mode",
                    "resource_template_uuid",
                    "mount",
                    "material_uuid",
                    "site",
                    "slot_range",
                    "flow_role",
                    "custody_policy",
                ],
                "additionalProperties": False,
            },
            "type": "material_source",
            "node_type": "material_source",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }
        handle = {
            "node_business_key": node_business_key,
            "handle_key": "material",
            "io_type": "source",
            "display_name": "Material",
            "description": "向下游节点传递具体物料实例的物料占位符。",
            "type": "ResourceSlot",
            "required": False,
            "data_source": "executor",
            "data_key": "material",
            "meta_data": {
                "unilab": {
                    "value_schema": {"$slot": "ResourceSlot"},
                }
            },
        }
        handle["uuid"] = action_handle_uuid(
            str(node["uuid"]),
            io_type="source",
            handle_key="material",
        )
        return node, handle

    @staticmethod
    def _compile_material_io(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """编译宿主拥有、内置人工确认的物料装卸框架模板。"""

        node = {
            "uuid": action_template_uuid(resource_name, "material_io"),
            "resource_template_uuid": resource_template_uuid,
            "name": "material_io",
            "display_name": "Material IO",
            "description": "经人工确认在指定挂载库位装载、卸载或补液。",
            "class": "unilabos.workflow.authoring:material_io",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["load", "unload", "refill"]},
                    "resource_template_uuid": {
                        "type": ["string", "null"],
                        "format": "uuid",
                    },
                    "mount": {
                        "type": "object",
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"}
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    },
                    "site": {"type": ["string", "null"]},
                    "site_uuid": {"type": ["string", "null"], "format": "uuid"},
                    "material": {
                        "type": ["object", "null"],
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"}
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    },
                    "disposition": {
                        "type": ["string", "null"],
                        "enum": ["detach", "soft_delete", None],
                    },
                    "scope": {
                        "type": ["string", "null"],
                        "enum": [
                            "material_only",
                            "material_with_contents",
                            None,
                        ],
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                    },
                    "name": {"type": ["string", "null"]},
                    "barcode": {"type": ["string", "null"]},
                    "reagent": {
                        "type": ["object", "null"],
                        "properties": {
                            "reagent_info_uuid": {
                                "type": "string",
                                "format": "uuid",
                            },
                            "cas": {"type": "string"},
                            "name": {"type": "string"},
                            "name_en": {"type": "string"},
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "molecular_formula": {"type": "string"},
                            "smiles": {"type": "string"},
                            "inchi_key": {"type": "string"},
                            "molecular_weight": {"type": "number"},
                            "density_g_per_ml": {"type": "number"},
                            "physical_state": {"type": "string"},
                            "quantity": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                            },
                            "quantity_unit": {"type": "string"},
                            "concentration_value": {"type": "number"},
                            "concentration_unit": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["quantity", "quantity_unit"],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "mode",
                    "resource_template_uuid",
                    "mount",
                    "site",
                    "material",
                    "disposition",
                    "scope",
                    "timeout_seconds",
                ],
                "additionalProperties": False,
            },
            "type": "material_io",
            "node_type": "material_io",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "executor_kind": "material_io",
                    "builtin_manual_confirmation": True,
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }
        handle_specs = (
            ("target", "material", "Material", "ResourceSlot", "goal"),
            ("target", "site", "库位", "str", "goal"),
            ("source", "material", "Material", "ResourceSlot", "executor"),
            ("target", "ready", "执行顺序", "any", "dependency"),
            ("source", "ready", "执行顺序", "any", "dependency"),
        )
        handles: list[dict[str, Any]] = []
        for io_type, key, display_name, value_type, data_source in handle_specs:
            handles.append(
                {
                    "uuid": action_handle_uuid(
                        str(node["uuid"]), io_type=io_type, handle_key=key
                    ),
                    "node_business_key": (
                        resource_template_uuid,
                        "material_io",
                    ),
                    "workflow_node_template_uuid": str(node["uuid"]),
                    "handle_key": key,
                    "io_type": io_type,
                    "display_name": display_name,
                    "description": "物料装卸合同连接点。",
                    "type": value_type,
                    # ``material`` 仅在 unload 必填；静态 AST/计划合同执行条件校验，
                    # 通用 Handle 层不能把 load 的同名输入误判为必填。
                    "required": False,
                    "data_source": data_source,
                    "data_key": key,
                    "meta_data": {
                        "unilab": {
                            "value_schema": (
                                {"$slot": "ResourceSlot"}
                                if value_type == "ResourceSlot"
                                else {"type": "string"} if value_type == "str" else {}
                            )
                        }
                    },
                }
            )
        return node, handles

    @staticmethod
    def _compile_action(
        *,
        action_name: str,
        action: Mapping[str, Any],
        class_identity: str,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
        resource_template_identity_resolver: Callable[[str], str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """把一个第 2 版强类型动作合同编译为模板候选。

        参数说明：``action_name`` 与 ``resource_template_uuid`` 构成持久业务身份；
        ``action`` 提供 Schema 和展示字段；``class_identity`` 供 F02 工作流创作编译
        按 Python 设备类解析动作；``resource_name`` 和 ``resource_display_name``
        固化 HTTP 摘要；``resource_template_identity_resolver`` 把动作中的源码资源
        约束解析为本地模板 UUID。返回一个节点模板及其完整控制/数据连接点。
        """

        schema = action.get("schema")
        if not isinstance(schema, Mapping):
            raise RegistryTemplateProjectionError("强类型动作缺少 JSON Schema")
        contract = schema.get("x-unilabos-action-contract")
        if not isinstance(contract, Mapping) or contract.get("version") != 2:
            raise RegistryTemplateProjectionError("只接受第 2 版动作合同")
        node_business_key = (resource_template_uuid, action_name)
        unilab_metadata: dict[str, Any] = {
            "contract_kind": "typed",
            "action_contract_schema": dict(schema),
            "resource_template": {
                "uuid": resource_template_uuid,
                "name": resource_name,
                "display_name": resource_display_name,
            },
        }
        executor_kind = str(action.get("executor_kind") or "").strip()
        if executor_kind:
            unilab_metadata["executor_kind"] = executor_kind
        # ``always_free`` 是设备排队语义，必须随模板冻结，避免任务运行时重新
        # 查询可变注册表后得到与创作时不同的并发边界。
        if action.get("always_free"):
            unilab_metadata["always_free"] = True
        deterministic_uuid = action_template_uuid(resource_name, action_name)
        node = {
            "uuid": deterministic_uuid,
            "resource_template_uuid": resource_template_uuid,
            "name": action_name,
            "display_name": (
                action.get("display_name") or action.get("displayname") or action_name
            ),
            "description": action.get("description") or schema.get("description"),
            "class": class_identity,
            "goal": dict(action.get("goal") or {}),
            "goal_default": dict(action.get("goal_default") or {}),
            "feedback": dict(action.get("feedback") or {}),
            "result": dict(action.get("result") or {}),
            "schema": goal_parameter_schema(schema),
            "type": action.get("type") or "UniLabJsonCommand",
            "node_type": action.get("node_type") or "ILab",
            "meta_data": {"unilab": unilab_metadata},
        }
        if action.get("uuid") is not None:
            try:
                explicit_uuid = validate_uuid(action["uuid"])
            except (TypeError, ValueError):
                raise RegistryTemplateProjectionError(
                    "节点模板显式 UUID 非法"
                ) from None
            if explicit_uuid != deterministic_uuid:
                raise RegistryTemplateProjectionError(
                    "节点模板显式 UUID 与设备名、动作名身份不一致"
                )

        handles = compile_action_template_handles(
            schema,
            node_business_key=node_business_key,
            resource_template_identity_resolver=(resource_template_identity_resolver),
        )
        for handle in handles:
            handle["uuid"] = action_handle_uuid(
                str(node["uuid"]),
                io_type=str(handle["io_type"]),
                handle_key=str(handle["handle_key"]),
            )
        return node, handles


__all__ = [
    "RegistryTemplateProjection",
    "RegistryTemplateProjectionError",
]
