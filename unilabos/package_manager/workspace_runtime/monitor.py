"""不解释文件语义的稳定工作区输入代监视器。"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Protocol

try:
    from watchfiles import watch
except ImportError:
    # 旧运行镜像仍可使用剪枝后的内容轮询，不丢失热重载能力。
    watch = None

from .generation import (
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRefreshResult,
    WorkspaceRuntimeStatus,
)

_LOGGER = logging.getLogger(__name__)

_IGNORED_DIRECTORIES = frozenset(
    (
        ".git",
        ".hg",
        ".svn",
        ".unilabos",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "unilabos_data",
        "venv",
    )
)
_IGNORED_FILE_NAMES = frozenset((".unilabos.packages.mutation.lock",))
_IGNORED_PATH_SEQUENCES = (
    (".claude", "skills"),
    (".codex", "skills"),
    (".cursor", "skills"),
)
_IGNORED_SUFFIXES = frozenset((".pyc", ".pyo", ".swp", ".tmp"))
_IGNORED_SQLITE_SUFFIXES = ("-journal", "-shm", "-wal")


def _path_posix(path: Path) -> str:
    """读取相对路径的 POSIX 排序键。

    参数：``path`` 是已规范化的工作区相对路径。
    返回：POSIX 文本。
    异常：无。
    """

    return path.as_posix()


class StableWorkspaceFileMonitor:
    """把相邻文件事件收敛为完整稳定工作区输入代。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        graph_argument: str,
        ignored_paths: Iterable[str | Path] = (),
        interval_seconds: float = 0.25,
        settle_seconds: float = 0.15,
        reconcile_seconds: float = 30.0,
    ) -> None:
        """建立尚未启动的内容观察器。

        参数：``workspace_root`` 是唯一观察根；``graph_argument`` 原样传给完整代
        编译器；``ignored_paths`` 是位于工作区内、由产品显式选择的运行状态目录；
        ``interval_seconds`` 是事件等待与降级轮询间隔；``settle_seconds`` 是相同摘要持续多久才
        可提交；``reconcile_seconds`` 是恢复遗漏事件的全量内容复核间隔。
        返回：无；构造不读文件、不启动线程。
        异常：目录、参数或时间范围非法时抛出 ``ValueError``。
        """

        selected_root = Path(workspace_root).absolute()
        if not selected_root.is_dir() or selected_root.is_symlink():
            raise ValueError("工作区监视根必须是无符号链接的既有目录")
        if not isinstance(graph_argument, str) or not graph_argument.strip():
            raise ValueError("工作区监视器的物理图参数不能为空")
        if interval_seconds <= 0 or settle_seconds < 0 or reconcile_seconds <= 0:
            raise ValueError("工作区监视时间参数必须为正间隔和非负稳定期")
        self._workspace_root = selected_root
        self._graph_argument = graph_argument.strip()
        self._ignored_relative_paths = _normalize_ignored_paths(
            selected_root,
            ignored_paths,
        )
        self._interval_seconds = float(interval_seconds)
        self._settle_seconds = float(settle_seconds)
        self._reconcile_seconds = float(reconcile_seconds)
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_captured_identity: str | None = None

    def capture(self) -> WorkspaceInputGeneration:
        """同步捕获当前完整工作区文件输入代。

        参数：无。
        返回：以全部非临时普通文件内容摘要为身份的不可变输入代；不解析 Python、
        YAML、JSON、动作合同（Action Contract）或工作流（Workflow）语义。
        异常：文件遍历中出现符号链接、越界、读取竞争或 I/O 故障时传播异常。
        """

        # ``generation_identity`` 只表达字节级文件世代，不承担包内容解释责任。
        generation_identity = _workspace_content_identity(
            self._workspace_root,
            ignored_relative_paths=self._ignored_relative_paths,
        )
        self._last_captured_identity = generation_identity
        return WorkspaceInputGeneration(
            identity=generation_identity,
            workspace_root=self._workspace_root,
            graph_argument=self._graph_argument,
        )

    def start(
        self,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
    ) -> None:
        """幂等启动稳定文件世代后台观察。

        参数：``submit`` 接收完整稳定输入代并返回刷新结算结果。
        返回：无；已经运行时不创建第二线程。
        异常：回调不可调用、初始捕获或线程启动失败时传播异常。
        """

        if not callable(submit):
            raise TypeError("稳定工作区输入代提交回调必须可调用")
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # ``processed_identity`` 是启动前已由产品编译的文件代；若调用者没有
            # 先 capture，则在开启线程前同步建立唯一基线。
            processed_identity = self._last_captured_identity or self.capture().identity
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(stop_event, submit, processed_identity),
                name="workspace-file-generation-monitor",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = worker
            try:
                worker.start()
            except BaseException:
                self._thread = None
                raise

    def close(self) -> None:
        """幂等停止监视线程并等待有界退出。

        参数：无。
        返回：无；成功后不再提交新输入代。
        异常：线程五秒内无法退出时抛出 ``RuntimeError``。
        """

        with self._lifecycle_lock:
            worker = self._thread
            if worker is None:
                return
            self._stop_event.set()
        if worker is not threading.current_thread():
            worker.join(timeout=5)
        with self._lifecycle_lock:
            if worker.is_alive():
                raise RuntimeError("稳定工作区文件监视器未能停止")
            if self._thread is worker:
                self._thread = None

    def _run(
        self,
        stop_event: threading.Event,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
        processed_identity: str,
    ) -> None:
        """观察文件摘要并只提交经过稳定期的完整输入代。

        参数：``stop_event`` 属于本线程生命周期；``submit`` 是协调器命令；
        ``processed_identity`` 是启动前已编译并发布的输入身份。
        返回：无；文件竞争和刷新失败保留同一代继续重试，停止事件结束循环。
        异常：未分类回调错误留在后台线程边界，不会把不稳定代标记为已处理。
        """

        pending_identity: str | None = None
        pending_since = 0.0
        retry_capture = False
        for changed in self._observe_changes(stop_event):
            if not changed and pending_identity is None and not retry_capture:
                continue
            try:
                observed = self.capture()
            except (OSError, RuntimeError, ValueError):
                pending_identity = None
                retry_capture = True
                continue
            retry_capture = False
            if observed.identity == processed_identity:
                pending_identity = None
                continue
            now = time.monotonic()
            if observed.identity != pending_identity:
                pending_identity = observed.identity
                pending_since = now
                continue
            if now - pending_since < self._settle_seconds:
                continue
            try:
                # ``refresh_result`` 决定该稳定代是否已经由运行时可靠结算。
                refresh_result = submit(observed)
            except (OSError, RuntimeError, TypeError, ValueError):
                pending_since = now
                continue
            if getattr(refresh_result, "outcome", None) == "failed":
                pending_since = now
                continue
            processed_identity = observed.identity
            pending_identity = None

    def _observe_changes(self, stop_event: threading.Event) -> Iterator[bool]:
        """原生事件不可用时内容轮询，并定期恢复原生订阅。"""
        if watch is None:
            _LOGGER.warning("watchfiles 不可用，工作区使用剪枝后的内容轮询")
            while not stop_event.wait(self._interval_seconds):
                yield True
            return
        while not stop_event.is_set():
            try:
                yield from self._native_changes(stop_event)
                return
            except (OSError, RuntimeError, ValueError):
                _LOGGER.warning(
                    "工作区原生文件观察暂不可用，内容轮询期间将定期重试订阅",
                    exc_info=True,
                )
            retry_at = time.monotonic() + self._reconcile_seconds
            while not stop_event.wait(self._interval_seconds):
                yield True
                if time.monotonic() >= retry_at:
                    break

    def _native_changes(self, stop_event: threading.Event) -> Iterator[bool]:
        """只对剪枝后的目录注册非递归观察，避免运行日志和虚拟环境制造事件。

        每次订阅建立后重新核对内容，覆盖启动及目录重订阅的事件空窗；周期性
        全量复核同时修正遗漏的目录创建事件。事件本身从不充当文件代身份。
        """
        while not stop_event.is_set():
            _, directories = _workspace_paths(
                self._workspace_root, self._ignored_relative_paths
            )
            observed_directories = set(directories)
            deadline = time.monotonic() + self._reconcile_seconds
            first = True
            tick_ms = max(1, round(self._interval_seconds * 1000))
            with closing(watch(
                *directories, recursive=False, watch_filter=None,
                stop_event=stop_event, debounce=tick_ms,
                step=min(50, tick_ms), rust_timeout=tick_ms,
                yield_on_timeout=True, debug=False,
            )) as events:
                for changes in events:
                    if stop_event.is_set():
                        return
                    relevant = False
                    topology_changed = False
                    for kind, raw_path in changes:
                        path = Path(raw_path)
                        try:
                            relative = path.relative_to(self._workspace_root)
                        except ValueError:
                            continue
                        if _ignored_relative(relative, self._ignored_relative_paths):
                            continue
                        was_directory = path in observed_directories
                        is_directory = not path.is_symlink() and path.is_dir()
                        if not (was_directory or is_directory) and _ignore_file(path):
                            continue
                        relevant = True
                        if kind in (1, 3) and (was_directory or is_directory):
                            topology_changed = True
                    reconcile = time.monotonic() >= deadline
                    if first or reconcile:
                        _, current = _workspace_paths(
                            self._workspace_root, self._ignored_relative_paths
                        )
                        topology_changed |= set(current) != observed_directories
                        deadline = time.monotonic() + self._reconcile_seconds
                    yield first or relevant or reconcile
                    first = False
                    if topology_changed:
                        break
                else:
                    if not stop_event.is_set():
                        raise RuntimeError("工作区原生文件事件流意外结束")
                    return


def _ignored_relative(relative: Path, ignored_paths: tuple[Path, ...]) -> bool:
    """统一快照与事件观察的忽略规则；在进入子目录前使用。"""
    return (
        any(part in _IGNORED_DIRECTORIES for part in relative.parts)
        or any(
            relative.parts[index:index + len(sequence)] == sequence
            for sequence in _IGNORED_PATH_SEQUENCES
            for index in range(len(relative.parts) - len(sequence) + 1)
        )
        or any(relative == path or relative.is_relative_to(path) for path in ignored_paths)
    )


def _workspace_paths(root: Path, ignored_paths: tuple[Path, ...]) -> tuple[list[Path], list[Path]]:
    """枚举相关普通文件与目录，忽略子树不进入、符号链接关闭式拒绝。"""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("工作区观察根必须保持为既有普通目录")
    files: list[Path] = []
    directories: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        directories.append(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                candidate = Path(entry.path)
                relative = candidate.relative_to(root)
                if _ignored_relative(relative, ignored_paths):
                    continue
                if entry.is_symlink():
                    raise ValueError(f"工作区监视范围不得包含符号链接: {relative.as_posix()}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif entry.is_file(follow_symlinks=False) and not _ignore_file(candidate):
                    files.append(candidate)
    return files, directories


def _workspace_content_identity(
    workspace_root: Path,
    *,
    ignored_relative_paths: tuple[Path, ...] = (),
) -> str:
    """按规范相对路径和原始字节计算工作区文件代身份。

    参数：``workspace_root`` 是已经过构造校验的观察根；
    ``ignored_relative_paths`` 是产品显式声明的工作区内运行状态子树。
    返回：包含全部相关普通文件的 ``sha256:`` 摘要。
    异常：发现符号链接、路径逃逸或读取竞争时抛出 ``ValueError``/``OSError``，
    不产生看似完整的部分摘要。
    """

    digest = hashlib.sha256()
    # ``observed_files`` 只按路径枚举，不解释扩展名或包内领域语义。
    observed_files, _ = _workspace_paths(workspace_root, ignored_relative_paths)
    for candidate in sorted(
        observed_files,
        key=_path_posix,
    ):
        relative_bytes = (
            candidate.relative_to(workspace_root).as_posix().encode("utf-8")
        )
        content = candidate.read_bytes()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _normalize_ignored_paths(
    workspace_root: Path,
    ignored_paths: Iterable[str | Path],
) -> tuple[Path, ...]:
    """规范化产品显式运行目录的工作区内相对身份。

    参数：``workspace_root`` 是监视根；``ignored_paths`` 是绝对或相对运行目录。
    返回：只包含工作区内部、按路径稳定排序且去重的相对路径元组。
    异常：参数不可迭代、包含空路径或路径解析失败时抛出
    ``TypeError``/``ValueError``；工作区外路径不在观察范围内，因此安全忽略。
    """

    try:
        # ``candidate_paths`` 冻结调用者输入，避免遍历期间被外部修改。
        candidate_paths = tuple(ignored_paths)
    except TypeError as error:
        raise TypeError("ignored_paths 必须是路径集合") from error
    normalized: set[Path] = set()
    for candidate in candidate_paths:
        selected_path = Path(candidate).expanduser()
        if not selected_path.is_absolute():
            selected_path = workspace_root / selected_path
        selected_path = selected_path.absolute()
        try:
            relative_path = selected_path.relative_to(workspace_root)
        except ValueError:
            continue
        if not relative_path.parts:
            raise ValueError("不得忽略整个工作区监视根")
        normalized.add(relative_path)
    return tuple(sorted(normalized, key=_path_posix))


def _ignore_file(candidate: Path) -> bool:
    """判断一个普通文件是否属于编辑器或解释器临时产物。

    参数：``candidate`` 是工作区根下的既有普通文件。
    返回：文件名或后缀属于固定基础设施忽略集合时为 ``True``。
    异常：无；本函数不读取文件内容。
    """

    return (
        candidate.name in _IGNORED_FILE_NAMES
        or candidate.suffix in _IGNORED_SUFFIXES
        or candidate.name.endswith(_IGNORED_SQLITE_SUFFIXES)
    )


class StableWorkspaceGenerationMonitor(Protocol):
    """只观察稳定文件世代、不解释包内容的监视器 Adapter Interface。"""

    def start(
        self,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
    ) -> None:
        """启动稳定输入代观察。

        参数：``submit`` 是完整输入代稳定后调用的唯一命令接缝。
        返回：无。
        异常：监视线程无法启动时传播异常；不得提交零散文件事件。
        """

        ...

    def close(self) -> None:
        """停止监视并等待提交线程退出。

        参数：无。
        返回：无。
        异常：无法确认停止时抛出异常，不能伪装为已经关闭。
        """

        ...


class WorkspaceRefreshCoordinator:
    """串行连接稳定输入监视器与工作区包运行时深模块。"""

    def __init__(
        self,
        runtime: WorkspacePackageRuntime,
        monitor: StableWorkspaceGenerationMonitor,
    ) -> None:
        """建立尚未启动的工作区刷新协调器。

        参数：``runtime`` 负责解释、门禁和发布完整输入代；``monitor`` 只负责
        稳定观察和提交输入代。
        返回：无；构造不启动线程、不编译文件。
        异常：依赖缺少所需公开方法时抛出 ``TypeError``。
        """

        if not isinstance(runtime, WorkspacePackageRuntime):
            raise TypeError("runtime 必须是 WorkspacePackageRuntime")
        if not callable(getattr(monitor, "start", None)) or not callable(
            getattr(monitor, "close", None)
        ):
            raise TypeError("monitor 必须实现稳定输入代监视接口")
        self._runtime = runtime
        self._monitor = monitor
        self._lock = threading.RLock()
        self._started = False
        self._closing = False
        self._closed = False

    def start(self) -> WorkspaceRuntimeStatus:
        """先启动包运行时，再幂等开启稳定输入代监视。

        参数：无。
        返回：工作区包运行时当前状态。
        异常：关闭后启动抛出 ``RuntimeError``；初始发布或监视器启动失败传播原
        异常，且不会创建第二个监视器生命周期。
        """

        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("工作区刷新协调器已经关闭")
            if self._started:
                return self._runtime.status()
            status = self._runtime.start()
            self._monitor.start(self.submit)
            self._started = True
            return status

    def submit(
        self,
        generation: WorkspaceInputGeneration,
    ) -> WorkspaceRefreshResult:
        """串行转交一个监视器已经稳定观察的完整输入代。

        参数：``generation`` 是不可变稳定工作区输入代。
        返回：工作区包运行时的刷新结果。
        异常：协调器未启动、已关闭或输入类型无效时抛出
        ``RuntimeError``/``TypeError``；本方法不自行读取或解释文件。
        """

        if not isinstance(generation, WorkspaceInputGeneration):
            raise TypeError("generation 必须是 WorkspaceInputGeneration")
        with self._lock:
            if self._closed or self._closing or not self._started:
                raise RuntimeError("工作区刷新协调器未运行")
            return self._runtime.refresh(generation)

    def status(self) -> WorkspaceRuntimeStatus:
        """读取工作区包运行时的稳定状态。

        参数：无。
        返回：运行时不可变状态投影。
        异常：无；协调器不另建第二份状态权威。
        """

        return self._runtime.status()

    def close(self) -> None:
        """幂等停止监视器并关闭刷新命令接缝。

        参数：无。
        返回：无；监视器先停止，运行时随后拒绝刷新。
        异常：监视器关闭故障在运行时完成关闭后传播；协调器保留可重试关闭所有权，
        第二次调用仍会再次要求监视器确认停止。
        """

        with self._lock:
            if self._closed:
                return
            if self._closing:
                raise RuntimeError("工作区刷新协调器正在关闭")
            self._closing = True
            # ``monitor_was_started`` 固定本轮是否必须等待后台观察器停止。
            monitor_was_started = self._started
        try:
            if monitor_was_started:
                self._monitor.close()
        except BaseException:
            self._runtime.close()
            with self._lock:
                self._closing = False
            raise
        self._runtime.close()
        with self._lock:
            self._started = False
            self._closing = False
            self._closed = True


__all__ = [
    "StableWorkspaceFileMonitor",
    "StableWorkspaceGenerationMonitor",
    "WorkspaceRefreshCoordinator",
]
