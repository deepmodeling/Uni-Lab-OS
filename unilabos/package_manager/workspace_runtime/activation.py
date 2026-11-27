"""工作区注册表运行时（Workspace Registry Runtime）的准备与激活接缝。"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    directory_identity,
)

from ..package_catalog import PackageCatalog, PackageDefinition
from ..package_catalog.registry_snapshot import (
    RegistryActivationPlan,
    RegistrySnapshot,
    RegistrySnapshotError,
    compile_registry_snapshot,
)
from ..package_catalog.sources import WorkspaceSource
from ..package_distribution import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    load_locked_package_sources,
)
from .discovery import (
    WorkspaceStartupPlan,
    compile_package_source,
    compile_workspace_startup,
    project_catalog_startup_plan,
)
from .package_source import (
    PackageCatalogSource,
    compile_material_shape_generation,
    selected_package_import_roots,
)


def publish_registry_snapshot(snapshot: RegistrySnapshot, registry: Any) -> None:
    """把完整注册表快照（Registry Snapshot）原子发布到实时注册表。

    参数：``snapshot`` 是已经完整校验的不可变候选代；``registry`` 提供设备与
    资源注册表映射，产品实现可提供 ``publish_package_snapshot`` 原子接缝。
    返回：无；成功后保留内置定义并一次替换完整软件包定义。
    异常：快照或注册表形状无效、身份冲突或发布失败时传播异常；通用 Adapter
    会尽力恢复发布前的两个映射，避免留下跨代部分事实。
    """

    if not isinstance(snapshot, RegistrySnapshot):
        raise TypeError("snapshot 必须是 RegistrySnapshot")
    # ``publish_snapshot`` 是产品注册表提供的锁内原子发布 Interface。
    publish_snapshot = getattr(registry, "publish_package_snapshot", None)
    if callable(publish_snapshot):
        publish_snapshot(snapshot)
        return
    try:
        # 两个原始映射共同组成发布失败时必须恢复的实时注册表基线。
        original_devices = copy.deepcopy(dict(registry.device_type_registry))
        original_resources = copy.deepcopy(dict(registry.resource_type_registry))
    except (AttributeError, TypeError) as error:
        raise RegistrySnapshotError("注册表必须提供设备和资源定义映射") from error
    candidate_devices, candidate_resources = snapshot.registry_candidates(
        original_devices,
        original_resources,
    )
    try:
        registry.device_type_registry = candidate_devices
        registry.resource_type_registry = candidate_resources
    except Exception:
        _restore_registry_mapping(
            registry,
            attribute="device_type_registry",
            original=original_devices,
        )
        _restore_registry_mapping(
            registry,
            attribute="resource_type_registry",
            original=original_resources,
        )
        raise


def _restore_registry_mapping(
    registry: Any,
    *,
    attribute: str,
    original: Mapping[str, Any],
) -> None:
    """尽力恢复一次通用注册表发布前的单个映射。

    参数：``registry`` 是实时注册表；``attribute`` 是设备或资源映射属性；
    ``original`` 是发布前深拷贝事实。
    返回：无；恢复失败不遮蔽最初的发布异常。
    异常：无；所有 Adapter 失败均被吞掉以保留根因。
    """

    try:
        # ``current`` 用于识别前一步已经完成的恢复，避免重复覆盖其他恢复动作。
        current = getattr(registry, attribute)
        if dict(current) == dict(original):
            return
        setattr(registry, attribute, copy.deepcopy(dict(original)))
    except Exception:  # noqa: BLE001 - 回滚不得遮蔽最初的发布异常。
        return


@dataclass(frozen=True, slots=True)
class WorkspaceRegistryRuntime:
    """持有一次工作区静态编译代及其受控运行激活责任。"""

    source: WorkspaceSource
    graph_path: Path
    graph_snapshot: Mapping[str, Any] = field(repr=False)
    catalog: PackageCatalog
    registry_snapshot: RegistrySnapshot
    activation_plan: RegistryActivationPlan
    workflow_source_plan: EditableSourceDiscoveryPlan
    startup_plan: WorkspaceStartupPlan | None = None
    package_catalog_sources: tuple[PackageCatalogSource, ...] = ()
    material_shapes: tuple[dict[str, object], ...] = ()
    # ``material_shapes_by_template`` 保留云端模板同步所需的同代精确归属。
    material_shapes_by_template: Mapping[str, dict[str, object]] = field(
        default_factory=dict
    )
    dependency_revision: str = ""
    _published: bool = field(default=False, init=False, repr=False, compare=False)
    _import_path_active: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """补齐只含主包的遗留候选并冻结模板外形归属。

        参数：无；读取 ``source``、``catalog``、``package_catalog_sources`` 和
        ``material_shapes_by_template``。返回：无；正式完整候选保持原配对，旧
        Adapter 省略配对时只补入已有主包来源和目录（PackageCatalog），并将模板
        外形根映射冻结。异常：主包来源、目录或外形绑定类型无效时抛出
        ``TypeError``；不会根据 ``sys.path`` 猜测缺失来源。
        """

        if not self.package_catalog_sources:
            object.__setattr__(
                self,
                "package_catalog_sources",
                (PackageCatalogSource(source=self.source, catalog=self.catalog),),
            )
        if not isinstance(self.material_shapes_by_template, Mapping):
            raise TypeError("模板物料外形绑定必须是对象映射")
        # ``shape_bindings`` 与构造者容器隔离并按模板 FQID 固定顺序；运行代公开
        # 后调用方不能增删模板归属，原始构造者后续修改也不会产生跨代漂移。
        shape_bindings: dict[str, dict[str, object]] = {}
        for template_fqid, shape in self.material_shapes_by_template.items():
            if (
                not isinstance(template_fqid, str)
                or not template_fqid.strip()
                or not isinstance(shape, Mapping)
            ):
                raise TypeError("模板物料外形绑定结构无效")
            shape_bindings[template_fqid] = copy.deepcopy(dict(shape))
        object.__setattr__(
            self,
            "material_shapes_by_template",
            MappingProxyType(dict(sorted(shape_bindings.items()))),
        )

    def publish(self, registry: Any) -> None:
        """先完整发布注册表快照，再开放作者模块导入资格。

        参数：``registry`` 是本次进程唯一的产品注册表（Registry）实例。
        返回：无；成功后本运行时记录已经完成完整快照发布。
        异常：注册表候选冲突或原子发布失败时传播原异常，且导入资格保持关闭。
        """

        publish_registry_snapshot(self.registry_snapshot, registry)
        object.__setattr__(self, "_published", True)

    def graph_copy(self) -> dict[str, Any]:
        """为一个会修改输入的启动消费者分离物理图（Graph）副本。

        参数：无；使用准备阶段一次读取并深度冻结的 ``graph_snapshot``。
        返回：与固定快照等价、但容器完全独立的普通 JSON 字典。
        异常：无；快照已在准备阶段验证为完整 JSON 对象。调用者修改返回值不会
        改变后续消费者或本次注册表激活计划。
        """

        # ``consumer_graph`` 是单个预览器或资源图解析器独占的可变输入副本。
        consumer_graph = _thaw_graph_json(self.graph_snapshot)
        assert isinstance(consumer_graph, dict)
        return consumer_graph

    def activate_import_path(self) -> None:
        """在注册表成功发布后激活工作区作者模块导入根。

        参数：无；使用准备阶段固定的工作区来源。
        返回：无；授权工作区根位于 ``sys.path`` 首位，重复调用保持幂等。
        异常：尚未成功发布注册表快照时抛出 ``RuntimeError``，不会修改导入环境。
        """

        if not self._published:
            raise RuntimeError("注册表快照发布（publish）成功前不得激活作者导入路径")
        # ``workspace_import_roots`` 保留主可编辑根，并仅加入物理图实际选中的
        # 外部包根；完整目录中未选驱动不会因此获得导入资格。
        workspace_import_roots = tuple(
            str(root)
            for root in selected_package_import_roots(
                self.package_catalog_sources,
                self.activation_plan,
                editable_source=self.source,
            )
        )
        if self._import_path_active and tuple(
            sys.path[: len(workspace_import_roots)]
        ) == (workspace_import_roots):
            return
        for workspace_import_root in workspace_import_roots:
            while workspace_import_root in sys.path:
                sys.path.remove(workspace_import_root)
        for workspace_import_root in reversed(workspace_import_roots):
            sys.path.insert(0, workspace_import_root)
        object.__setattr__(self, "_import_path_active", True)


def prepare_workspace_registry_runtime(
    arguments: dict[str, Any],
    *,
    compile_catalog: Callable[[WorkspaceSource], PackageCatalog] = (
        compile_package_source
    ),
    startup_plan: WorkspaceStartupPlan | None = None,
) -> WorkspaceRegistryRuntime | None:
    """从公共启动参数准备一个无作者导入副作用的工作区运行时。

    参数：``arguments`` 是公共命令行（CLI）解析后的可变参数；
    ``compile_catalog`` 是可测试替换、每次准备只调用一次的包目录
    （PackageCatalog）编译接缝；``startup_plan`` 是调用者已固定的同一工作区
    启动计划，省略时由本函数统一编译。
    返回：未配置 ``--workspace`` 时返回 ``None``；否则返回同时持有完整目录、完整
    注册表快照、有限激活计划和工作流源码计划的运行时。
    异常：参数冲突、软件包编译、物理图 JSON、定义身份或源码身份无效时传播
    ``TypeError``/``ValueError``；失败不会修改 ``sys.path`` 或发布注册表。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    workspace_argument = arguments.get("workspace")
    if workspace_argument is None:
        return None
    if not isinstance(workspace_argument, str) or not workspace_argument.strip():
        raise ValueError("--workspace 必须是非空目录路径")
    if arguments.get("devices"):
        raise ValueError("--workspace 不可与 legacy --devices 同时使用")
    if arguments.get("workflow_editable_package_root"):
        raise ValueError("--workspace 不可与 --workflow_editable_package_root 同时使用")
    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")

    # ``workspace_source`` 是本次准备唯一显式授权的文件来源。
    workspace_source = WorkspaceSource(workspace_argument)
    # 默认产品编译器先固定一次启动清单，并把同一模型和原始字节传入完整目录编译；
    # 可测试替换的预编译目录仍走旧兼容投影，禁止重新读取 ``package.yaml``。
    if compile_catalog is compile_package_source:
        resolved_startup_plan = startup_plan or compile_workspace_startup(
            workspace_source
        )
        catalog = compile_package_source(
            workspace_source,
            startup_plan=resolved_startup_plan,
        )
    else:
        catalog = compile_catalog(workspace_source)
        resolved_startup_plan = startup_plan or project_catalog_startup_plan(
            workspace_source,
            catalog,
        )
    if not isinstance(catalog, PackageCatalog):
        raise TypeError("compile_catalog 必须返回 PackageCatalog")
    if (
        not isinstance(resolved_startup_plan, WorkspaceStartupPlan)
        or resolved_startup_plan.source.root != workspace_source.root
    ):
        raise TypeError("startup_plan 必须属于当前显式工作区来源")
    resolved_startup_plan.apply_product_defaults(arguments)
    # ``dependency_packages`` 只来自工作区显式依赖声明和锁，同时保留后续有限
    # 运行激活所需的来源根；主包已有目录不会在聚合验证中被再次编译。
    dependency_packages = _locked_dependency_packages(
        workspace_source,
        compile_catalog=compile_catalog,
    )
    package_catalog_sources = (
        PackageCatalogSource(source=workspace_source, catalog=catalog),
        *dependency_packages,
    )
    registry_snapshot = compile_registry_snapshot(
        tuple(item.catalog for item in package_catalog_sources)
    )
    graph_argument = arguments.get("graph") or "graph.json"
    graph_path, graph_data = _read_fixed_graph(
        workspace_source,
        graph_argument=graph_argument,
    )
    # ``graph_snapshot`` 是本次启动代唯一物理图（Graph）观察；后续不得重读文件。
    graph_snapshot = _freeze_graph_json(graph_data)
    assert isinstance(graph_snapshot, Mapping)
    activation_plan = registry_snapshot.select(graph_snapshot)
    workflow_source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=catalog,
    )
    # ``material_shape_generation`` 与设备、资源、显式工作流和资产消费同一完整
    # 候选代，同时保留公共查询清单和资源模板（ResourceTemplate）精确归属。
    material_shape_generation = compile_material_shape_generation(
        package_catalog_sources
    )
    # ``arguments`` 只接收不会开启第二套扫描权威的稳定路径事实。
    arguments["_workspace_root"] = str(workspace_source.root)
    arguments["graph"] = str(graph_path)
    return WorkspaceRegistryRuntime(
        source=workspace_source,
        startup_plan=resolved_startup_plan,
        graph_path=graph_path,
        graph_snapshot=graph_snapshot,
        catalog=catalog,
        registry_snapshot=registry_snapshot,
        activation_plan=activation_plan,
        workflow_source_plan=workflow_source_plan,
        package_catalog_sources=package_catalog_sources,
        material_shapes=material_shape_generation.public_shapes,
        material_shapes_by_template=material_shape_generation.shapes_by_template,
        dependency_revision=_dependency_files_revision(workspace_source),
    )


def _locked_dependency_packages(
    source: WorkspaceSource,
    *,
    compile_catalog: Callable[[WorkspaceSource], PackageCatalog],
) -> tuple[PackageCatalogSource, ...]:
    """读取当前工作区显式锁定的外部包来源与目录（PackageCatalog）。

    参数：``source`` 是主工作区的唯一文件读取边界；``compile_catalog`` 是本候选
    代主包与外部包必须共享的目录编译器。
    返回：未声明依赖时返回空元组；否则返回经过锁摘要复核、且保留显式来源根的
    完整外部目录配对。
    异常：声明和锁缺一、外部来源或摘要无效、聚合身份冲突时传播关闭式异常；
    不回退到 ``sys.path`` 或环境软件包扫描。
    """

    # ``dependency_files_present`` 区分无依赖的既有工作区与损坏的半对文件。
    dependency_files_present = tuple(
        source.has_file(logical_path)
        for logical_path in (
            DEPENDENCY_DECLARATION_FILE,
            DEPENDENCY_LOCK_FILE,
        )
    )
    if not any(dependency_files_present):
        return ()
    # ``resolved_dependencies`` 是包分发（Package Distribution）核验后的低层配对；
    # 工作区运行时在此将其提升为候选代所需的来源对象。
    resolved_dependencies = load_locked_package_sources(
        source.root,
        compile_catalog=compile_catalog,
    )
    return tuple(
        PackageCatalogSource(source=item.source, catalog=item.catalog)
        for item in resolved_dependencies
    )


def _dependency_files_revision(source: WorkspaceSource) -> str:
    """计算显式软件包依赖声明与锁原始字节的稳定摘要。

    参数：``source`` 是主工作区的安全文件来源。
    返回：固定文件顺序、包含存在性和原始字节的 ``sha256:`` 摘要；没有依赖
    文件时也返回确定摘要。
    异常：路径不安全或文件读取失败时传播 ``ValueError``，禁止发布不完整输入代。
    """

    # ``digest`` 同时覆盖文件名、存在性、长度和内容，避免两文件拼接歧义。
    digest = hashlib.sha256()
    for logical_path in (
        DEPENDENCY_DECLARATION_FILE,
        DEPENDENCY_LOCK_FILE,
    ):
        encoded_path = logical_path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        if not source.has_file(logical_path):
            digest.update(b"absent")
            continue
        content = source.read_bytes(logical_path)
        digest.update(b"present")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _read_fixed_graph(
    source: WorkspaceSource,
    *,
    graph_argument: Any,
) -> tuple[Path, Mapping[str, Any]]:
    """从授权工作区读取并一次解析固定的物理图（Graph）JSON。

    参数：``source`` 是工作区文件边界；``graph_argument`` 是绝对工作区内路径或
    POSIX 相对路径。
    返回：经过边界校验的规范文件路径和本次准备唯一使用的 JSON 对象。
    异常：路径为空、越界、文件不安全、编码或 JSON 结构非法时抛出 ``ValueError``。
    """

    if not isinstance(graph_argument, str) or not graph_argument.strip():
        raise ValueError("物理图参数必须是非空路径")
    # ``selected_graph`` 保留调用者输入形式，随后规范化为工作区逻辑路径。
    selected_graph = Path(graph_argument).expanduser()
    if selected_graph.is_absolute():
        try:
            logical_graph = selected_graph.relative_to(source.root).as_posix()
        except ValueError as error:
            raise ValueError("绝对物理图路径必须位于工作区内") from error
    else:
        logical_graph = PurePosixPath(graph_argument).as_posix()
    graph_bytes = source.read_bytes(logical_graph)
    try:
        # ``graph_data`` 是本次静态编译代固定观察到的完整 JSON 对象。
        graph_data = json.loads(graph_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("物理图必须是 UTF-8 JSON") from error
    if not isinstance(graph_data, Mapping):
        raise TypeError("物理图 JSON 顶层必须是对象")
    return source.root / logical_graph, graph_data


def _freeze_graph_json(value: Any) -> Any:
    """递归冻结一次物理图（Graph）JSON 观察。

    参数：``value`` 是标准 JSON 解析器产生的对象、数组或标量。
    返回：对象转为只读映射、数组转为元组、标量保持不变的深度不可变值。
    异常：若调用者绕过 JSON 解析传入不受支持的对象，则抛出 ``TypeError``，
    防止运行时保存含可变实现对象的伪快照。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_graph_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_graph_json(item) for item in value)
    raise TypeError(f"物理图 JSON 含不支持的值: {type(value).__name__}")


def _thaw_graph_json(value: Any) -> Any:
    """把固定物理图（Graph）快照深度分离为普通 JSON 容器。

    参数：``value`` 是 ``_freeze_graph_json`` 产生的不可变值。
    返回：所有映射与数组均为全新容器的等价 JSON 值。
    异常：若内部快照被错误构造为未知类型，则抛出 ``TypeError``。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _thaw_graph_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_graph_json(item) for item in value]
    raise TypeError(f"物理图快照含不支持的值: {type(value).__name__}")


def workflow_source_plan_from_catalog(
    *,
    source: WorkspaceSource,
    catalog: PackageCatalog,
) -> EditableSourceDiscoveryPlan:
    """只用冻结目录和目录身份生成工作流源码（Workflow Source）计划。

    参数：``source`` 是已授权工作区；``catalog`` 是同一轮唯一完整编译结果。
    返回：不重读 ``package.yaml``、不应用工作流图的可编辑源码发现计划。
    异常：目录内工作流缺少 UUID、来源 URI、规范包内路径或包目录身份不稳定时
    抛出 ``ValueError``，不返回部分注册计划。
    """

    # ``package_root`` 是工作流服务后续安全读写源码的实际 Python 包根。
    package_root = source.root / catalog.import_package
    try:
        root_identity = directory_identity(package_root)
    except StableFileAccessError as error:
        raise ValueError("工作流源码包目录身份无效") from error
    workflow_uuids_by_import = {
        (definition.module, definition.symbol): str(
            definition.details["workflow_uuid"]
        )
        for definition in catalog.definitions.workflows
    }
    registrations = tuple(
        _workflow_source_registration(
            definition=definition,
            catalog=catalog,
            package_root=package_root,
            workflow_uuids_by_import=workflow_uuids_by_import,
        )
        for definition in catalog.definitions.workflows
    )
    return EditableSourceDiscoveryPlan(
        registrations=registrations,
        root_identities=((package_root, root_identity),),
    )


def _workflow_source_registration(
    *,
    definition: PackageDefinition,
    catalog: PackageCatalog,
    package_root: Path,
    workflow_uuids_by_import: Mapping[tuple[str, str], str],
) -> EditableSourceRegistration:
    """把一个冻结工作流目录定义转换为源码登记身份。

    参数：``definition`` 是工作流静态定义；``catalog`` 提供唯一导入包身份；
    ``package_root`` 是已通过目录身份检查的实际 Python 包根。
    返回：可交给现有工作流源码服务原子登记的不可变注册项。
    异常：定义种类、UUID、来源 URI或包内相对路径不符合目录合同时抛出
    ``ValueError``。
    """

    if definition.kind != "workflow":
        raise ValueError("源码登记只接受工作流目录定义")
    workflow_uuid = definition.details.get("workflow_uuid")
    source_uri = definition.details.get("source_uri")
    if not isinstance(workflow_uuid, str) or not workflow_uuid:
        raise ValueError("工作流目录定义缺少 workflow_uuid")
    if not isinstance(source_uri, str) or not source_uri:
        raise ValueError("工作流目录定义缺少 source_uri")
    declaring_path = PurePosixPath(definition.declaring_file)
    expected_prefix = (catalog.import_package,)
    if (
        declaring_path.is_absolute()
        or len(declaring_path.parts) < 2
        or declaring_path.parts[:1] != expected_prefix
    ):
        raise ValueError("工作流目录声明文件不在规范导入包内")
    # ``relative_path`` 是相对实际 Python 包根的稳定源码位置。
    relative_path = PurePosixPath(*declaring_path.parts[1:]).as_posix()
    action_references = definition.details.get("action_references")
    dependency_workflow_uuids = tuple(
        sorted(
            {
                dependency_uuid
                for reference in (
                    action_references
                    if isinstance(action_references, tuple)
                    else ()
                )
                if isinstance(reference, Mapping)
                and reference.get("kind") == "workflow"
                and isinstance(reference.get("module"), str)
                and isinstance(reference.get("symbol"), str)
                and (
                    dependency_uuid := workflow_uuids_by_import.get(
                        (str(reference["module"]), str(reference["symbol"]))
                    )
                )
                is not None
            }
        )
    )
    return EditableSourceRegistration(
        workflow_uuid=workflow_uuid,
        package_id=catalog.import_package,
        package_root=package_root,
        relative_path=relative_path,
        source_uri=source_uri,
        module=definition.module,
        symbol=definition.symbol,
        definition_content_hash=definition.content_hash,
        dependency_workflow_uuids=dependency_workflow_uuids,
    )


__all__ = [
    "WorkspaceRegistryRuntime",
    "prepare_workspace_registry_runtime",
    "publish_registry_snapshot",
    "workflow_source_plan_from_catalog",
]
