"""工作区包运行时（WorkspacePackageRuntime）的生命周期与刷新安全内核。"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

import rfc8785

from .activation import WorkspaceRegistryRuntime

RuntimeState = Literal["created", "running", "closed"]
RefreshOutcome = Literal["noop", "hot_published", "pending_restart", "failed"]
_RESTART_BLOCKING_EXECUTION_STATES = frozenset(
    ("dispatched", "running", "cancel_requested", "execution_unknown")
)


def _allow_hot_publication(
    _previous: Any,
    _candidate: Any,
) -> tuple[str, ...]:
    """默认允许通过通用安全分类的候选执行完整代热发布。

    参数：``_previous`` 与 ``_candidate`` 是旧、新完整工作区候选代。
    返回：固定空原因集合，表示调用产品已经提供原子完整代发布能力。
    异常：无。
    """

    return ()


def _empty_execution_states() -> tuple[str, ...]:
    """返回没有在途执行的默认持久状态集合。

    参数：无。
    返回：空元组，表示通用运行时调用者没有提供执行状态 Adapter。
    异常：无。
    """

    return ()


def _ignore_restart_request(_reasons: tuple[str, ...]) -> None:
    """忽略未启用产品监督器时的重启请求。

    参数：``_reasons`` 是工作区差异产生的稳定重启原因。
    返回：无。
    异常：无。
    """


@dataclass(frozen=True, slots=True)
class WorkspaceInputGeneration:
    """监视器完成稳定观察后提交的一代工作区输入。"""

    identity: str
    workspace_root: Path
    graph_argument: str
    dependency_revision: str = ""

    def __post_init__(self) -> None:
        """规范输入代身份与来源路径而不解释目录文件。

        参数：无；使用构造时的身份、工作区根、物理图参数和依赖修订。
        返回：无；路径转为绝对路径但不读取或解析文件。
        异常：身份或物理图参数为空时抛出 ``ValueError``；依赖修订类型无效时
        抛出 ``TypeError``。
        """

        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("工作区输入代身份不能为空")
        if not isinstance(self.graph_argument, str) or not self.graph_argument.strip():
            raise ValueError("工作区输入代的物理图参数不能为空")
        if not isinstance(self.dependency_revision, str):
            raise TypeError("工作区输入代的依赖修订必须是字符串")
        object.__setattr__(self, "identity", self.identity.strip())
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).absolute())


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeStatus:
    """工作区包运行时的只读稳定状态投影。"""

    state: RuntimeState
    active_input_identity: str | None
    observed_input_identity: str | None
    active_fingerprint: str | None
    pending_restart: bool
    pending_input_identity: str | None
    restart_reasons: tuple[str, ...]
    restart_requested: bool
    last_outcome: RefreshOutcome | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshResult:
    """一次刷新命令的确定性结果。"""

    outcome: RefreshOutcome
    active_input_identity: str
    observed_input_identity: str
    restart_reasons: tuple[str, ...] = ()
    restart_requested: bool = False
    error: str | None = None


class WorkspaceGenerationPublisher(Protocol):
    """原子发布完整工作区代的外部 Adapter 接口。"""

    def publish_initial(self, candidate: Any) -> None:
        """原子发布初始候选代。

        参数：``candidate`` 是完成全部预校验的候选代。
        返回：无。
        异常：失败必须保留发布前注册表、模板、授权、设备和库存事实。
        """

        ...

    def hot_replace(self, previous: Any, candidate: Any) -> None:
        """原子热替换完整候选代。

        参数：``previous`` 是旧活跃代；``candidate`` 是完整新候选代。
        返回：无。
        异常：失败必须回滚为 ``previous`` 的全部投影和授权。
        """

        ...


class WorkspacePackageRuntime:
    """用四个操作封装工作区编译、差异门禁、发布和重启安全。"""

    def __init__(
        self,
        initial_input: WorkspaceInputGeneration,
        *,
        prepare_generation: Callable[[WorkspaceInputGeneration], Any],
        publisher: WorkspaceGenerationPublisher,
        restart_mode: bool = False,
        execution_states: Callable[[], Iterable[str]] = _empty_execution_states,
        request_restart: Callable[[tuple[str, ...]], None] = _ignore_restart_request,
        hot_publish_guard: Callable[[Any, Any], tuple[str, ...]] = (
            _allow_hot_publication
        ),
    ) -> None:
        """建立尚未发布的工作区包运行时。

        参数：``initial_input`` 是首个稳定输入代；``prepare_generation`` 负责解释
        输入并产生完整候选；``publisher`` 原子发布跨注册表、模板与授权的完整代；
        ``restart_mode`` 决定是否可请求监督器重启；``execution_states`` 返回当前
        持久执行状态；``request_restart`` 向外部监督器提交重启原因；
        ``hot_publish_guard`` 允许产品在完整代原子发布尚不可用时关闭热发布。
        返回：无；构造不编译、不发布、不重启。
        异常：依赖不可调用或初始输入类型错误时抛出 ``TypeError``。
        """

        if not isinstance(initial_input, WorkspaceInputGeneration):
            raise TypeError("initial_input 必须是 WorkspaceInputGeneration")
        for dependency, name in (
            (prepare_generation, "prepare_generation"),
            (execution_states, "execution_states"),
            (request_restart, "request_restart"),
            (hot_publish_guard, "hot_publish_guard"),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if not callable(getattr(publisher, "publish_initial", None)) or not callable(
            getattr(publisher, "hot_replace", None)
        ):
            raise TypeError("publisher 必须实现完整代原子发布接口")
        self._initial_input = initial_input
        self._prepare_generation = prepare_generation
        self._publisher = publisher
        self._restart_mode = bool(restart_mode)
        self._execution_states = execution_states
        self._request_restart = request_restart
        self._hot_publish_guard = hot_publish_guard
        self._lock = threading.RLock()
        self._active_candidate: Any | None = None
        self._active_input: WorkspaceInputGeneration | None = None
        self._pending_candidate: Any | None = None
        self._status = WorkspaceRuntimeStatus(
            state="created",
            active_input_identity=None,
            observed_input_identity=None,
            active_fingerprint=None,
            pending_restart=False,
            pending_input_identity=None,
            restart_reasons=(),
            restart_requested=False,
            last_outcome=None,
            last_error=None,
        )

    def start(self) -> WorkspaceRuntimeStatus:
        """幂等编译并原子发布初始工作区代。

        参数：无。
        返回：成功启动后的只读状态投影。
        异常：关闭后启动抛出 ``RuntimeError``；编译或发布失败传播原异常，且不会
        宣布活跃代或留下部分注册表、模板、授权、设备与库存事实。
        """

        with self._lock:
            if self._status.state == "closed":
                raise RuntimeError("工作区包运行时已经关闭")
            if self._status.state == "running":
                return self._status
            candidate = self._prepare_generation(self._initial_input)
            self._publisher.publish_initial(candidate)
            self._active_candidate = candidate
            self._active_input = self._initial_input
            self._status = replace(
                self._status,
                state="running",
                active_input_identity=self._initial_input.identity,
                observed_input_identity=self._initial_input.identity,
                active_fingerprint=candidate_fingerprint(
                    candidate,
                    self._initial_input,
                ),
                last_error=None,
            )
            return self._status

    def refresh(
        self,
        generation: WorkspaceInputGeneration,
    ) -> WorkspaceRefreshResult:
        """编译并评估一个稳定工作区输入代。

        参数：``generation`` 是监视器已经稳定提交的完整输入代。
        返回：``noop``、``hot_published``、``pending_restart`` 或 ``failed`` 结果。
        异常：未启动、已关闭或输入类型错误时抛出 ``RuntimeError``/``TypeError``；
        候选编译与发布故障转换为 ``failed`` 并保留旧活跃代。
        """

        if not isinstance(generation, WorkspaceInputGeneration):
            raise TypeError("generation 必须是 WorkspaceInputGeneration")
        with self._lock:
            if self._status.state != "running":
                raise RuntimeError("工作区包运行时必须处于 running 才能刷新")
            assert self._active_candidate is not None
            assert self._active_input is not None
            try:
                candidate = self._prepare_generation(generation)
            except Exception as error:  # noqa: BLE001
                return self._record_refresh_failure(generation, error)
            prepared_fingerprint = candidate_fingerprint(candidate, generation)
            if prepared_fingerprint == self._status.active_fingerprint:
                self._status = replace(
                    self._status,
                    observed_input_identity=generation.identity,
                    last_outcome="noop",
                    last_error=None,
                )
                return WorkspaceRefreshResult(
                    outcome="noop",
                    active_input_identity=self._active_input.identity,
                    observed_input_identity=generation.identity,
                )
            unsafe_reasons = restart_reasons(
                previous=self._active_candidate,
                candidate=candidate,
                previous_input=self._active_input,
                candidate_input=generation,
            )
            if not unsafe_reasons:
                # ``product_reasons`` 只补充产品组合根能力边界，不重新解释文件。
                product_reasons = self._hot_publish_guard(
                    self._active_candidate,
                    candidate,
                )
                if not isinstance(product_reasons, tuple) or any(
                    not isinstance(reason, str) or not reason
                    for reason in product_reasons
                ):
                    return self._record_refresh_failure(
                        generation,
                        TypeError("hot_publish_guard 必须返回非空字符串原因元组"),
                    )
                unsafe_reasons = tuple(sorted(set(product_reasons)))
            if unsafe_reasons:
                return self._record_pending_restart(
                    generation=generation,
                    candidate=candidate,
                    reasons=unsafe_reasons,
                )
            try:
                self._publisher.hot_replace(self._active_candidate, candidate)
            except Exception as error:  # noqa: BLE001
                return self._record_refresh_failure(generation, error)
            self._active_candidate = candidate
            self._active_input = generation
            self._pending_candidate = None
            self._status = replace(
                self._status,
                active_input_identity=generation.identity,
                observed_input_identity=generation.identity,
                active_fingerprint=prepared_fingerprint,
                pending_restart=False,
                pending_input_identity=None,
                restart_reasons=(),
                restart_requested=False,
                last_outcome="hot_published",
                last_error=None,
            )
            return WorkspaceRefreshResult(
                outcome="hot_published",
                active_input_identity=generation.identity,
                observed_input_identity=generation.identity,
            )

    def _record_pending_restart(
        self,
        *,
        generation: WorkspaceInputGeneration,
        candidate: Any,
        reasons: tuple[str, ...],
    ) -> WorkspaceRefreshResult:
        """保留候选但不热替换需要进程重启的工作区代。

        参数：``generation`` 是新稳定输入代；``candidate`` 是已完整验证的候选；
        ``reasons`` 是关闭集合内的重启原因。
        返回：``pending_restart`` 结果；是否已请求监督器重启由安全门禁决定。
        异常：监督器请求回调失败时转为 ``failed``，旧活跃代仍保持不变。
        """

        # ``restart_requested`` 保留同一待处理输入代是否已经向监督器提交过请求，
        # 重复文件事件不得导致重复进程控制命令。
        restart_requested = (
            self._status.pending_input_identity == generation.identity
            and self._status.restart_requested
        )
        if self._restart_mode:
            try:
                # ``current_execution_states`` 来自持久执行投影；不确定或在途执行
                # 均关闭自动重启门禁，不能用进程状态猜测物理设备已经停止。
                current_execution_states = tuple(self._execution_states())
                if any(
                    not isinstance(state, str) for state in current_execution_states
                ):
                    raise TypeError("执行状态 Adapter 必须返回字符串集合")
                if not restart_requested and not (
                    set(current_execution_states) & _RESTART_BLOCKING_EXECUTION_STATES
                ):
                    self._request_restart(reasons)
                    restart_requested = True
            except Exception as error:  # noqa: BLE001
                return self._record_refresh_failure(generation, error)
        self._pending_candidate = candidate
        assert self._active_input is not None
        self._status = replace(
            self._status,
            observed_input_identity=generation.identity,
            pending_restart=True,
            pending_input_identity=generation.identity,
            restart_reasons=reasons,
            restart_requested=restart_requested,
            last_outcome="pending_restart",
            last_error=None,
        )
        return WorkspaceRefreshResult(
            outcome="pending_restart",
            active_input_identity=self._active_input.identity,
            observed_input_identity=generation.identity,
            restart_reasons=reasons,
            restart_requested=restart_requested,
        )

    def _record_refresh_failure(
        self,
        generation: WorkspaceInputGeneration,
        error: Exception,
    ) -> WorkspaceRefreshResult:
        """记录候选编译或发布失败且保留旧活跃代。

        参数：``generation`` 是失败输入代；``error`` 是编译或原子发布异常。
        返回：稳定 ``failed`` 结果。
        异常：无；本方法不重新发布、不修改设备或库存，并保留旧候选身份。
        """

        assert self._active_input is not None
        diagnostic = str(error) or type(error).__name__
        self._status = replace(
            self._status,
            observed_input_identity=generation.identity,
            last_outcome="failed",
            last_error=diagnostic,
        )
        return WorkspaceRefreshResult(
            outcome="failed",
            active_input_identity=self._active_input.identity,
            observed_input_identity=generation.identity,
            error=diagnostic,
        )

    def status(self) -> WorkspaceRuntimeStatus:
        """读取当前工作区包运行时状态。

        参数：无。
        返回：不可变状态对象；读取不触发编译、发布或重启。
        异常：无。
        """

        with self._lock:
            return self._status

    def close(self) -> None:
        """幂等关闭刷新接缝但不擅自停止设备或修改库存。

        参数：无。
        返回：无；关闭后不再接受启动或刷新命令。
        异常：无；外部监视器由工作区刷新协调器负责先行停止。
        """

        with self._lock:
            if self._status.state == "closed":
                return
            self._pending_candidate = None
            self._status = replace(self._status, state="closed")


class WorkspaceGenerationIdentity(Protocol):
    """差异分类所需的最小工作区输入代 Interface。"""

    identity: str
    dependency_revision: str


def candidate_fingerprint(
    candidate: Any,
    generation: WorkspaceGenerationIdentity,
) -> str:
    """读取候选代稳定指纹并纳入显式依赖文件修订。

    参数：``candidate`` 是完整准备结果；``generation`` 提供输入代身份，以及
    监视器可选提交的依赖修订。
    返回：覆盖注册表快照（Registry Snapshot）、物理图（Graph）和显式依赖文件
    原始字节修订的稳定摘要；通用测试候选退回输入代身份。
    异常：无；正式候选缺少内建依赖修订时兼容使用输入代修订。
    """

    if isinstance(candidate, WorkspaceRegistryRuntime):
        # ``generation_payload`` 覆盖候选真正编译观察到的完整稳定输入。
        generation_payload = {
            "dependency_revision": _dependency_revision(candidate, generation),
            "graph": candidate.graph_copy(),
            "registry_snapshot": candidate.registry_snapshot.fingerprint,
        }
        return "sha256:" + hashlib.sha256(rfc8785.dumps(generation_payload)).hexdigest()
    return f"{generation.identity}:{generation.dependency_revision}"


def restart_reasons(
    *,
    previous: Any,
    candidate: Any,
    previous_input: WorkspaceGenerationIdentity,
    candidate_input: WorkspaceGenerationIdentity,
) -> tuple[str, ...]:
    """判断候选变化是否越过可热发布安全边界。

    参数：``previous`` 与 ``candidate`` 是完整旧/新候选；``previous_input`` 与
    ``candidate_input`` 是其输入代身份兼容后备。
    返回：稳定排序且去重的关闭重启原因集合；空集合表示可原子热发布。
    异常：无；未知候选类型保守返回驱动实现变化，禁止猜测热发布。
    """

    if not isinstance(previous, WorkspaceRegistryRuntime) or not isinstance(
        candidate,
        WorkspaceRegistryRuntime,
    ):
        return ("active_driver_implementation_changed",)
    # ``reasons`` 是必须以监督重启处理的候选差异闭集。
    reasons: set[str] = set()
    if previous.graph_copy() != candidate.graph_copy():
        reasons.add("graph_changed")
    if _dependency_revision(previous, previous_input) != _dependency_revision(
        candidate,
        candidate_input,
    ):
        reasons.add("binary_dependencies_changed")

    # 两代设备索引用于只比较旧物理图（Graph）已激活的设备定义。
    previous_devices = {
        definition.fqid: definition for definition in previous.registry_snapshot.devices
    }
    candidate_devices = {
        definition.fqid: definition
        for definition in candidate.registry_snapshot.devices
    }
    # ``active_device_fqids`` 来自旧活跃物理图，未选定义变化不触碰设备。
    active_device_fqids = {item.fqid for item in previous.activation_plan.devices}
    for fqid in active_device_fqids:
        # 新旧定义身份与动作合同（Action Contract）共同决定是否可热发布。
        old_definition = previous_devices.get(fqid)
        new_definition = candidate_devices.get(fqid)
        if old_definition is None or new_definition is None:
            reasons.add("active_driver_implementation_changed")
            continue
        if _action_contract(old_definition) != _action_contract(new_definition):
            reasons.add("active_action_contract_changed")
        elif _implementation_identity(old_definition) != _implementation_identity(
            new_definition
        ):
            reasons.add("active_driver_implementation_changed")

    # 两代资源索引用于检测已物化资源树及库位（Site）的结构变化。
    previous_resources = {
        definition.fqid: definition
        for definition in previous.registry_snapshot.resources
    }
    candidate_resources = {
        definition.fqid: definition
        for definition in candidate.registry_snapshot.resources
    }
    # ``active_resource_fqids`` 是已参与资源树及库位（Site）物化的资源定义。
    active_resource_fqids = {item.fqid for item in previous.activation_plan.resources}
    for fqid in active_resource_fqids:
        # ``old_definition`` 与 ``new_definition`` 代表同一激活资源跨代事实。
        old_definition = previous_resources.get(fqid)
        new_definition = candidate_resources.get(fqid)
        if (
            old_definition is None
            or new_definition is None
            or _implementation_identity(old_definition)
            != _implementation_identity(new_definition)
            or old_definition.details != new_definition.details
        ):
            reasons.add("resource_tree_or_site_structure_changed")
    return tuple(sorted(reasons))


def _dependency_revision(
    candidate: WorkspaceRegistryRuntime,
    generation: WorkspaceGenerationIdentity,
) -> str:
    """选择候选实际观察到的依赖文件修订。

    参数：``candidate`` 是正式工作区运行代；``generation`` 是监视器输入代。
    返回：优先使用候选准备阶段读取的依赖声明和锁摘要；旧候选为空时使用监视器
    修订，保持现有 Adapter 与测试兼容。
    异常：无。
    """

    return candidate.dependency_revision or generation.dependency_revision


def _implementation_identity(definition: Any) -> tuple[str, str, str]:
    """读取一个静态定义的作者实现身份。

    参数：``definition`` 是包目录（PackageCatalog）定义。
    返回：模块、符号和声明文件内容摘要三元组。
    异常：无；正式候选已完成目录字段验证。
    """

    return (definition.module, definition.symbol, definition.content_hash)


def _action_contract(definition: Any) -> Any:
    """读取设备定义的规范动作合同（Action Contract）投影。

    参数：``definition`` 是包目录（PackageCatalog）中的设备定义。
    返回：规范 ``action_value_mappings`` 冻结值；缺失时返回空元组。
    异常：无；目录已保证详情只含不可变 JSON 值。
    """

    # ``registry_entry`` 是设备定义的静态注册表投影，不是实时实例。
    registry_entry = definition.details.get("registry_entry")
    if not isinstance(registry_entry, dict) and not hasattr(registry_entry, "get"):
        return ()
    # ``class_entry`` 保存动作合同（Action Contract）映射的类级投影。
    class_entry = registry_entry.get("class")
    if not isinstance(class_entry, dict) and not hasattr(class_entry, "get"):
        return ()
    return class_entry.get("action_value_mappings", ())


__all__ = [
    "WorkspaceGenerationIdentity",
    "WorkspaceGenerationPublisher",
    "WorkspaceInputGeneration",
    "WorkspacePackageRuntime",
    "WorkspaceRefreshResult",
    "WorkspaceRuntimeStatus",
    "candidate_fingerprint",
    "restart_reasons",
]
