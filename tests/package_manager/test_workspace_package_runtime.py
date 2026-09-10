"""工作区包运行时（WorkspacePackageRuntime）刷新安全合同。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.package_manager import (
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRegistryRuntime,
    WorkspaceSource,
    compile_registry_snapshot,
)
from unilabos.package_manager.package_catalog import (
    PackageCatalog,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDistributionIdentity,
)
from unilabos.workflow.source_discovery import EditableSourceDiscoveryPlan


class RecordingGenerationPublisher:
    """记录原子工作区代发布并可注入失败的测试 Adapter。"""

    def __init__(self) -> None:
        """建立空发布记录。

        参数：无。
        返回：无；初始代和热发布代均为空。
        异常：无。
        """

        self.initial: list[Any] = []
        self.replacements: list[tuple[Any, Any]] = []
        self.active: Any | None = None
        self.fail_next = False

    def publish_initial(self, candidate: Any) -> None:
        """原子发布初始工作区代。

        参数：``candidate`` 是完成静态编译和门禁的候选代。
        返回：无；成功时记录为当前活跃代。
        异常：注入失败时抛出 ``RuntimeError`` 且不修改已有记录。
        """

        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("注入的初始发布失败")
        self.initial.append(candidate)
        self.active = candidate

    def hot_replace(self, previous: Any, candidate: Any) -> None:
        """原子替换可热发布的工作区代。

        参数：``previous`` 是发布前活跃代；``candidate`` 是完整候选代。
        返回：无；成功时一次替换注册表、模板和工作流源码授权的测试投影。
        异常：注入失败时抛出 ``RuntimeError`` 且保留旧活跃代。
        """

        assert previous is self.active
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("注入的热发布失败")
        self.replacements.append((previous, candidate))
        self.active = candidate


def _input_generation(
    tmp_path: Path,
    identity: str,
    *,
    dependency_revision: str = "dependencies-v1",
) -> WorkspaceInputGeneration:
    """建立只携带稳定输入代身份的测试工作区观察。

    参数：``tmp_path`` 是隔离根；``identity`` 是监视器提交的稳定输入代身份；
    ``dependency_revision`` 是外部依赖锁 Adapter 提供的二进制依赖代。
    返回：可交给工作区包运行时的不可变输入代。
    异常：目录创建失败时传播文件系统异常。
    """

    # ``generation_root`` 代表监视器完成稳定快照后的独立工作区来源根。
    generation_root = tmp_path / identity
    generation_root.mkdir()
    return WorkspaceInputGeneration(
        identity=identity,
        workspace_root=generation_root,
        graph_argument="graph.json",
        dependency_revision=dependency_revision,
    )


def _freeze_json(value: Any) -> Any:
    """为测试候选建立深度不可变 JSON 快照。

    参数：``value`` 是普通 JSON 值。
    返回：对象、数组分别转成只读映射和元组的深度冻结值。
    异常：遇到非 JSON 值时抛出 ``TypeError``。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"测试 JSON 值无效: {type(value).__name__}")


def _definition(
    *,
    kind: str,
    definition_id: str,
    content_hash: str,
    action_schema: Mapping[str, Any] | None = None,
) -> PackageDefinition:
    """建立带规范注册表条目的测试目录定义。

    参数：``kind`` 是设备、资源或工作流种类；``definition_id`` 是包内身份；
    ``content_hash`` 表示作者实现内容；``action_schema`` 是设备动作合同。
    返回：可进入真实包目录（PackageCatalog）的不可变定义。
    异常：种类不受支持时抛出 ``ValueError``。
    """

    fqid = f"community.runtime_lab.{definition_id}"
    if kind == "workflow":
        return PackageDefinition(
            kind="workflow",
            id=definition_id,
            fqid=fqid,
            module=f"runtime_lab.{definition_id}",
            symbol=definition_id,
            declaring_file=f"runtime_lab/{definition_id}.py",
            content_hash=content_hash,
            details={
                "workflow_uuid": "81111111-1111-4111-8111-111111111111",
                "source_uri": f"package://runtime_lab/{definition_id}.py",
            },
        )
    if kind not in {"device", "resource"}:
        raise ValueError("测试目录定义种类无效")
    registry_entry: dict[str, Any] = {
        "class": {
            "module": f"runtime_lab.{definition_id}:{definition_id.title()}",
            "type": "python",
        },
        "registry_type": kind,
    }
    if kind == "device":
        registry_entry["class"]["action_value_mappings"] = {
            "run": {"schema": dict(action_schema or {"type": "object"})}
        }
    return PackageDefinition(
        kind=kind,  # type: ignore[arg-type]
        id=definition_id,
        fqid=fqid,
        module=f"runtime_lab.{definition_id}",
        symbol=definition_id.title(),
        declaring_file=f"runtime_lab/{definition_id}.py",
        content_hash=content_hash,
        details={"registry_entry": registry_entry},
    )


def _candidate(
    root: Path,
    *,
    selected_driver_hash: str = "driver-v1",
    idle_driver_hash: str = "idle-v1",
    action_schema: Mapping[str, Any] | None = None,
    resource_hash: str = "resource-v1",
    workflow_hash: str = "workflow-v1",
    graph_revision: str = "graph-v1",
) -> WorkspaceRegistryRuntime:
    """构造覆盖刷新分类维度的真实注册表候选代。

    参数：``root`` 是测试来源根；``selected_driver_hash`` 与
    ``idle_driver_hash`` 分别代表活跃/非活跃驱动摘要；``action_schema`` 是活跃
    动作合同；``resource_hash`` 是资源结构摘要；``workflow_hash`` 是工作流源码
    摘要；``graph_revision`` 控制物理图。
    返回：使用真实包目录（PackageCatalog）和注册表快照类型的工作区候选代。
    异常：目录或注册表不变量无效时传播真实构造异常。
    """

    root.mkdir(exist_ok=True)
    source = WorkspaceSource(root)
    selected_device = _definition(
        kind="device",
        definition_id="selected_device",
        content_hash=selected_driver_hash,
        action_schema=action_schema,
    )
    idle_device = _definition(
        kind="device",
        definition_id="idle_device",
        content_hash=idle_driver_hash,
    )
    selected_resource = _definition(
        kind="resource",
        definition_id="selected_resource",
        content_hash=resource_hash,
    )
    workflow = _definition(
        kind="workflow",
        definition_id="prepare_workflow",
        content_hash=workflow_hash,
    )
    catalog = PackageCatalog.create(
        distribution=PackageDistributionIdentity(
            name="runtime-lab",
            normalized_name="runtime_lab",
            version="1.0.0",
            dependencies=("driver-binary==1",),
        ),
        import_package="runtime_lab",
        namespace="community.runtime_lab",
        definitions=PackageDefinitionCatalog(
            devices=(selected_device, idle_device),
            resources=(selected_resource,),
            workflows=(workflow,),
        ),
        assets=(),
        content_digest="content-v1",
    )
    snapshot = compile_registry_snapshot((catalog,))
    graph_data = {
        "revision": graph_revision,
        "nodes": [
            {
                "id": "device-a",
                "class": selected_device.fqid,
                "type": "device",
            },
            {
                "id": "resource-a",
                "class": selected_resource.fqid,
                "type": "container",
            },
        ],
    }
    graph_snapshot = _freeze_json(graph_data)
    assert isinstance(graph_snapshot, Mapping)
    return WorkspaceRegistryRuntime(
        source=source,
        graph_path=root / "graph.json",
        graph_snapshot=graph_snapshot,
        catalog=catalog,
        registry_snapshot=snapshot,
        activation_plan=snapshot.select(graph_snapshot),
        workflow_source_plan=EditableSourceDiscoveryPlan(
            registrations=(),
            root_identities=(),
        ),
    )


def _candidate_resolver(
    candidates: dict[str, object],
) -> Callable[[WorkspaceInputGeneration], object]:
    """建立按稳定输入代身份返回测试候选的编译接缝。

    参数：``candidates`` 是输入代身份到完整候选的封闭映射。
    返回：带完整中文合同、只执行映射查询的候选准备函数。
    异常：收到未声明输入代时由返回函数抛出 ``KeyError``，防止测试静默猜测。
    """

    def resolve_candidate(generation: WorkspaceInputGeneration) -> object:
        """按输入代稳定身份读取预构造候选。

        参数：``generation`` 是运行时正在解释的稳定工作区输入代。
        返回：映射中同身份的完整测试候选。
        异常：身份未在测试场景声明时抛出 ``KeyError``。
        """

        return candidates[generation.identity]

    return resolve_candidate


def _execution_state_reader(
    execution_state: str,
) -> Callable[[], tuple[str, ...]]:
    """建立返回固定持久执行状态的测试 Adapter。

    参数：``execution_state`` 是 ``idle`` 或一个阻止重启的执行状态。
    返回：无参数状态读取函数；空闲时返回空集合，否则返回单项元组。
    异常：无。
    """

    def read_execution_states() -> tuple[str, ...]:
        """读取本参数化用例固定的执行状态集合。

        参数：无。
        返回：空闲时为空元组，否则包含唯一状态。
        异常：无。
        """

        return () if execution_state == "idle" else (execution_state,)

    return read_execution_states


def test_runtime_start_status_and_close_hide_publication_order(tmp_path: Path) -> None:
    """启动、状态和关闭接口必须隐藏完整候选发布顺序。

    参数：``tmp_path`` 提供稳定输入代目录。
    返回：无；断言初始代只发布一次，状态公开活跃代且关闭保持幂等。
    异常：重复发布、状态漂移或关闭后仍可启动时测试失败。
    """

    initial_input = _input_generation(tmp_path, "generation-a")
    # ``compiled_candidate`` 是构建器对该稳定输入代产生的完整候选对象。
    compiled_candidate = object()
    publisher = RecordingGenerationPublisher()
    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=_candidate_resolver(
            {initial_input.identity: compiled_candidate}
        ),
        publisher=publisher,
    )

    started = runtime.start()
    runtime.start()

    assert publisher.initial == [compiled_candidate]
    assert started.state == "running"
    assert started.active_input_identity == "generation-a"
    assert runtime.status().pending_restart is False

    runtime.close()
    runtime.close()
    assert runtime.status().state == "closed"


def test_refresh_with_identical_content_is_noop(tmp_path: Path) -> None:
    """文件事件产生新输入代但内容未变化时刷新必须为 no-op。

    参数：``tmp_path`` 提供两个独立稳定来源根。
    返回：无；断言不调用热发布，活跃代身份保持原值且记录最新观察身份。
    异常：实现按监视事件次数而非内容身份重复发布时测试失败。
    """

    initial_input = _input_generation(tmp_path, "input-a")
    repeated_input = _input_generation(tmp_path, "input-b")
    initial_candidate = _candidate(tmp_path / "candidate-a")
    repeated_candidate = _candidate(tmp_path / "candidate-b")
    publisher = RecordingGenerationPublisher()
    candidates = {
        initial_input.identity: initial_candidate,
        repeated_input.identity: repeated_candidate,
    }
    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=_candidate_resolver(candidates),
        publisher=publisher,
    )
    runtime.start()

    result = runtime.refresh(repeated_input)

    assert result.outcome == "noop"
    assert publisher.replacements == []
    assert runtime.status().active_input_identity == "input-a"
    assert runtime.status().observed_input_identity == "input-b"


def test_inactive_definition_and_workflow_changes_publish_hot(tmp_path: Path) -> None:
    """非激活定义和工作流源码（Workflow Source）变化必须完整热发布。

    参数：``tmp_path`` 提供前后两个稳定工作区输入代。
    返回：无；断言原子发布器一次替换完整代且不产生重启请求。
    异常：未选驱动或工作流变化错误触发设备重启、或仅部分发布时测试失败。
    """

    initial_input = _input_generation(tmp_path, "input-a")
    changed_input = _input_generation(tmp_path, "input-b")
    initial_candidate = _candidate(tmp_path / "candidate-a")
    changed_candidate = _candidate(
        tmp_path / "candidate-b",
        idle_driver_hash="idle-v2",
        workflow_hash="workflow-v2",
    )
    publisher = RecordingGenerationPublisher()
    candidates = {
        initial_input.identity: initial_candidate,
        changed_input.identity: changed_candidate,
    }
    restart_requests: list[tuple[str, ...]] = []
    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=_candidate_resolver(candidates),
        publisher=publisher,
        restart_mode=True,
        request_restart=restart_requests.append,
    )
    runtime.start()

    result = runtime.refresh(changed_input)

    assert result.outcome == "hot_published"
    assert publisher.replacements == [(initial_candidate, changed_candidate)]
    assert publisher.active is changed_candidate
    assert runtime.status().active_input_identity == "input-b"
    assert runtime.status().pending_restart is False
    assert restart_requests == []


@pytest.mark.parametrize(
    ("change_kind", "expected_reason"),
    (
        ("graph", "graph_changed"),
        ("driver", "active_driver_implementation_changed"),
        ("action", "active_action_contract_changed"),
        ("resource", "resource_tree_or_site_structure_changed"),
        ("dependency", "binary_dependencies_changed"),
    ),
)
def test_unsafe_active_changes_wait_for_restart_without_auto_exit(
    tmp_path: Path,
    change_kind: str,
    expected_reason: str,
) -> None:
    """越过活跃运行安全边界的变化只能进入待重启状态。

    参数：``tmp_path`` 隔离候选代；``change_kind`` 选择物理图（Graph）、驱动、
    动作合同（Action Contract）、资源/库位（Site）结构或依赖变化；
    ``expected_reason`` 是对应稳定原因。
    返回：无；断言默认未启用 ``restart_mode`` 时只报告，不发布、不请求或退出。
    异常：任一危险变化被热替换或触发隐式重启时测试失败。
    """

    initial_input = _input_generation(tmp_path, "input-a")
    changed_input = _input_generation(
        tmp_path,
        "input-b",
        dependency_revision=(
            "dependencies-v2" if change_kind == "dependency" else "dependencies-v1"
        ),
    )
    initial_candidate = _candidate(tmp_path / "candidate-a")
    candidate_options: dict[str, Any] = {}
    if change_kind == "graph":
        candidate_options["graph_revision"] = "graph-v2"
    elif change_kind == "driver":
        candidate_options["selected_driver_hash"] = "driver-v2"
    elif change_kind == "action":
        candidate_options["action_schema"] = {
            "type": "object",
            "properties": {"speed": {"type": "number"}},
        }
    elif change_kind == "resource":
        candidate_options["resource_hash"] = "resource-v2"
    changed_candidate = _candidate(
        tmp_path / "candidate-b",
        **candidate_options,
    )
    publisher = RecordingGenerationPublisher()
    candidates = {
        initial_input.identity: initial_candidate,
        changed_input.identity: changed_candidate,
    }
    restart_requests: list[tuple[str, ...]] = []
    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=_candidate_resolver(candidates),
        publisher=publisher,
        restart_mode=False,
        request_restart=restart_requests.append,
    )
    runtime.start()

    result = runtime.refresh(changed_input)

    assert result.outcome == "pending_restart"
    assert result.restart_reasons == (expected_reason,)
    assert result.restart_requested is False
    assert runtime.status().pending_restart is True
    assert runtime.status().pending_input_identity == "input-b"
    assert publisher.replacements == []
    assert publisher.active is initial_candidate
    assert restart_requests == []


@pytest.mark.parametrize(
    ("execution_state", "expected_request_count"),
    (
        ("running", 0),
        ("execution_unknown", 0),
        ("idle", 1),
    ),
)
def test_restart_mode_never_restarts_running_or_unknown_execution(
    tmp_path: Path,
    execution_state: str,
    expected_request_count: int,
) -> None:
    """监督重启模式也不得越过在途或结果不确定的物理执行。

    参数：``tmp_path`` 隔离候选代；``execution_state`` 是持久执行投影状态；
    ``expected_request_count`` 是允许提交给监督器的重启请求数。
    返回：无；断言 ``running`` 与 ``execution_unknown`` 永不自动重启，空闲态只
    请求一次且重复刷新保持幂等。
    异常：危险状态触发重启或同一候选重复请求时测试失败。
    """

    initial_input = _input_generation(tmp_path, "input-a")
    changed_input = _input_generation(tmp_path, "input-b")
    candidates = {
        "input-a": _candidate(tmp_path / "candidate-a"),
        "input-b": _candidate(tmp_path / "candidate-b", graph_revision="graph-v2"),
    }
    publisher = RecordingGenerationPublisher()
    restart_requests: list[tuple[str, ...]] = []
    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=_candidate_resolver(candidates),
        publisher=publisher,
        restart_mode=True,
        execution_states=_execution_state_reader(execution_state),
        request_restart=restart_requests.append,
    )
    runtime.start()

    first = runtime.refresh(changed_input)
    second = runtime.refresh(changed_input)

    assert first.outcome == second.outcome == "pending_restart"
    assert len(restart_requests) == expected_request_count
    assert first.restart_requested is (execution_state == "idle")
    assert second.restart_requested is (execution_state == "idle")
