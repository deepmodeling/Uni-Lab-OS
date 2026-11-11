"""安静点重启协调器。

重启请求登记后先暂停本地调度派发，等执行端 active job 清空（"执行完最后一个
任务"或"任务都处于等待"）再执行重启，作用域按运行形态自动选择：

``edge``
    调度权威进程（默认拓扑的 ``unilab``，或 ``--role backend``）：通知 Host 进程整进程
    重启（HTTP 调 Host 管理 API），本进程与管理端口常驻，前端连接不断；Host 重连
    runtime.v1 控制面会话后自动恢复派发。默认拓扑里 Host 是本进程看护的子进程，
    退出码 75 即被原参数再拉起。
``process``
    本进程整体重启：停管理 API → 走 main 的正常退出链路（关库、停 backend）→ 以约定
    退出码退出，由父进程用相同参数拉起（Host 子进程的父进程是权威；接远端 Backend 的
    顶层 Host 由薄监督进程拉起）。等待/未派发的任务由调度器 ``start(recover=True)``
    恢复，不会失败。调度权威自己是顶层常驻进程，显式请求 ``process`` 会被拒绝（422）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from unilabos.app.supervisor import RESTART_EXIT_CODE, is_supervised
from unilabos.server.backend.lifecycle import active_device_job_ids

logger = logging.getLogger(__name__)

# 连续两次采样为空才算安静，跨过"上一个 job 刚结束、回调链还没落账"的窗口
_QUIESCENT_CONFIRMATIONS = 2
_POLL_INTERVAL_SECONDS = 0.5


class RestartCoordinator:
    """单例：登记重启请求、检测安静点并执行进程级重启。"""

    def __init__(
        self,
        get_execution_backend: Callable[[], Any],
        get_scheduler: Callable[[], Any],
        *,
        mutation_gate: Any = None,
    ) -> None:
        self._get_execution_backend = get_execution_backend
        self._get_scheduler = get_scheduler
        self._lock = threading.RLock()
        self._pending = False
        self._mode = "quiescent"
        self._scope = "auto"
        self._requested_at: Optional[float] = None
        self._watcher: Optional[threading.Thread] = None
        self._restarting = False
        self._mutation_gate = mutation_gate
        self._edge_restart_requested = False
        self._watch_generation = 0

    @staticmethod
    def _edge_control_service() -> Any:
        from unilabos.server.backend.edge_control import get_edge_control_service

        return get_edge_control_service()

    def _resolve_scope(self) -> str:
        with self._lock:
            scope = self._scope
        if scope != "auto":
            return scope
        if self._edge_control_service() is not None:
            return "edge"
        return "process"

    # ── 状态查询 ─────────────────────────────────────────────

    def active_job_ids(self) -> list[str]:
        return active_device_job_ids(self._get_scheduler(), self._get_execution_backend())

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pending": self._pending,
                "mode": self._mode,
                "scope": self._scope,
                "effective_scope": self._resolve_scope(),
                "requested_at": self._requested_at,
                "restarting": self._restarting,
                "safe_restart": is_safe_restart_enabled() or is_supervised(),
                "active_jobs": self.active_job_ids(),
                "dispatch_paused": self._scheduler_paused(),
                "mutation": self._mutation_gate.status() if self._mutation_gate is not None else None,
            }

    def _scheduler_paused(self) -> bool:
        scheduler = self._get_scheduler()
        return bool(scheduler is not None and scheduler.dispatch_paused)

    # ── 请求 / 取消 ──────────────────────────────────────────

    def request(self, mode: str = "quiescent", scope: str = "auto") -> dict[str, Any]:
        if mode not in ("quiescent", "immediate"):
            raise ValueError(f"unsupported restart mode: {mode!r}")
        if scope not in ("auto", "edge", "process"):
            raise ValueError(f"unsupported restart scope: {scope!r}")
        if scope == "edge" and self._edge_control_service() is None:
            raise ValueError("restart scope 'edge' requires a scheduling authority process")
        if (
            scope == "process"
            and self._edge_control_service() is not None
            and not is_supervised()
        ):
            # 调度权威就是顶层进程，没有谁能把它再拉起来；整进程重启只会变成退出。
            raise ValueError(
                "调度权威进程常驻，不支持整进程重启；重启 Host 请用 scope=edge（默认 auto）"
            )
        with self._lock:
            if self._restarting:
                return self.status()
            new_request = not self._pending
            if new_request:
                self._watch_generation += 1
            self._pending = True
            self._mode = mode
            self._scope = scope
            if self._resolve_scope() == 'process' and self._mutation_gate is not None:
                self._mutation_gate.request_restart(self)
            self._requested_at = time.time()
            scheduler = self._get_scheduler()
            if scheduler is not None:
                scheduler.pause_dispatch(reason="restart")
            if new_request or self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(
                    target=self._watch,
                    args=(self._watch_generation,),
                    name="RestartCoordinator",
                    daemon=True,
                )
                self._watcher.start()
        logger.info(
            "[Restart] 重启请求已登记 (mode=%s, scope=%s)，等待执行端安静",
            mode,
            scope,
        )
        return self.status()

    def cancel(self) -> dict[str, Any]:
        if self._edge_restart_requested:
            from unilabos.server.api.edge_proxy import edge_http
            response = edge_http('DELETE', '/api/v1/restart', timeout=10)
            if response is None or response.status_code >= 300:
                return self.status()
            import json
            state = json.loads(response.body)
            if state.get('pending') or state.get('restarting'):
                return self.status()
            with self._lock:
                self._edge_restart_requested = False
                self._restarting = False
        with self._lock:
            if self._restarting:
                return self.status()
            self._pending = False
            self._watch_generation += 1
            self._requested_at = None
            if self._mutation_gate is not None:
                self._mutation_gate.cancel_restart(self)
            scheduler = self._get_scheduler()
        if scheduler is not None:
            scheduler.resume_dispatch(reason="restart")
        logger.info("[Restart] 重启请求已取消，解除重启的派发暂停")
        return self.status()

    # ── 安静点检测与执行 ─────────────────────────────────────

    def _watch(self, generation: int) -> None:
        confirmations = 0
        while True:
            time.sleep(_POLL_INTERVAL_SECONDS)
            with self._lock:
                if not self._pending or generation != self._watch_generation:
                    return
                mode = self._mode
            if self._resolve_scope() == 'process' and self._mutation_gate is not None:
                # immediate 也不能截断源码切换、依赖安装或持久日志提交。
                if not self._mutation_gate.claim_restart(self):
                    confirmations = 0
                    continue
            if mode == "immediate":
                break
            if self.active_job_ids():
                confirmations = 0
                continue
            confirmations += 1
            if confirmations >= _QUIESCENT_CONFIRMATIONS:
                break
        with self._lock:
            if not self._pending or generation != self._watch_generation:
                return
            self._restarting = True
        self._execute()

    def _execute(self) -> None:
        backend = self._get_execution_backend()
        if backend is not None:
            backend.wait_idle(timeout=5.0)
        if self._resolve_scope() == "edge":
            self._execute_edge_restart()
        else:
            self._execute_process_restart()

    def _execute_edge_restart(self) -> None:
        """经控制面通知 Edge 进程整进程重启；调度权威常驻，重连后恢复派发。"""

        import json

        from unilabos.server.api.edge_proxy import edge_http

        logger.warning("[Restart] 执行端已安静，经控制面通知 Edge 进程整进程重启")
        service = self._edge_control_service()
        previous_process = getattr(service, 'edge_process', '')
        generation = self._watch_generation
        response = edge_http(
            "POST",
            "/api/v1/restart",
            headers={"content-type": "application/json"},
            body=json.dumps({"mode": "immediate", "scope": "process"}).encode("utf-8"),
            timeout=10,
        )
        if response is None or response.status_code >= 300:
            logger.error(
                "[Restart] 无法通知 Edge 重启（%s），派发保持暂停；"
                "确认 Edge 状态后可 DELETE /api/v1/restart 手动恢复",
                "Edge 未在线" if response is None else f"HTTP {response.status_code}",
            )
            with self._lock:
                self._restarting = False
            return
        self._edge_restart_requested = True
        ready = self._wait_edge_reconnected(previous_process=previous_process, generation=generation)
        with self._lock:
            if not self._pending or generation != self._watch_generation:
                return  # 用户已确认取消远端等待，不能再改变新的暂停原因。
        if not ready:
            logger.error(
                "[Restart] Edge 重启后未在预期时间内重连，派发保持暂停；"
                "Edge 恢复后可 DELETE /api/v1/restart 手动恢复"
            )
            with self._lock:
                self._restarting = False
            return
        with self._lock:
            self._pending = False
            self._requested_at = None
            self._restarting = False
            self._edge_restart_requested = False
            scheduler = self._get_scheduler()
        if scheduler is not None:
            scheduler.resume_dispatch(reason="restart")
        logger.warning("[Restart] Edge 已重连 runtime.v1 控制面会话，解除重启的派发暂停")

    def _wait_edge_reconnected(
        self,
        *,
        disconnect_timeout: float = 1200.0,
        reconnect_timeout: float = 300.0,
        previous_process: str = '',
        generation: int | None = None,
    ) -> bool:
        """等待 Edge 断开旧连接并以新进程重连。"""

        service = self._edge_control_service()
        if service is None:
            return False
        deadline = time.monotonic() + disconnect_timeout
        while time.monotonic() < deadline and service.connected:
            if not self._pending or generation is not None and generation != self._watch_generation:
                return False
            if previous_process and getattr(service, 'edge_process', '') not in {'', previous_process}:
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        # 包安装可能持续十几分钟；旧 Host 一直在线不能当作已经重启。
        if service.connected:
            return False
        deadline = time.monotonic() + reconnect_timeout
        while time.monotonic() < deadline:
            if not self._pending or generation is not None and generation != self._watch_generation:
                return False
            if service.connected and (not previous_process or getattr(service, 'edge_process', '') not in {'', previous_process}):
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        return False

    def _execute_process_restart(self) -> None:
        logger.warning("[Restart] 执行端已安静，开始整进程重启")
        _set_restart_requested()
        from unilabos.server.api.app import request_server_shutdown

        if not request_server_shutdown():
            # 重启依赖 uvicorn 停机驱动 main 的正常退出链路；管理 API 未运行
            # 时（嵌入式/测试）无法整进程重启，保留标记交由上层处理。
            logger.error("[Restart] 管理 API 未运行，无法触发整进程重启")
            with self._lock:
                self._restarting = False


# ── 进程级重启标记与再拉起 ──────────────────────────────────────

_restart_requested = threading.Event()
_safe_restart_enabled = True


def _set_restart_requested() -> None:
    _restart_requested.set()


def is_restart_requested() -> bool:
    return _restart_requested.is_set()


def set_safe_restart_enabled(enabled: bool) -> None:
    """``--no-safe-restart`` 时关闭：安静退出后不再由监督进程拉起。"""

    global _safe_restart_enabled
    _safe_restart_enabled = bool(enabled)


def is_safe_restart_enabled() -> bool:
    return _safe_restart_enabled


def exit_if_restart_requested() -> None:
    """正常退出链路走完后调用：有重启标记则按约定码退出，交给监督进程拉起。"""

    if not is_restart_requested():
        return
    if is_supervised() or _safe_restart_enabled:
        logger.warning(
            "[Restart] 本进程退出，监督进程将拉起新实例 (code=%s)",
            RESTART_EXIT_CODE,
        )
        raise SystemExit(RESTART_EXIT_CODE)
    logger.warning("[Restart] 已关闭安全重启，进程退出后不再拉起")
    raise SystemExit(0)


def spawn_replacement_process() -> None:
    """兼容旧名：真正拉起由监督进程完成，这里只按约定退出码退出。"""

    exit_if_restart_requested()


_coordinator: Optional[RestartCoordinator] = None
_coordinator_lock = threading.Lock()


def get_restart_coordinator() -> RestartCoordinator:
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            from unilabos.server.backend.composition import (
                get_execution_backend,
                get_scheduler,
            )

            _coordinator = RestartCoordinator(
                get_execution_backend,
                get_scheduler,
                mutation_gate=_configured_mutation_gate(),
            )
        return _coordinator


def _configured_mutation_gate():
    from pathlib import Path
    from unilabos.config.config import BasicConfig
    from unilabos.server.services.host_mutations import get_host_mutation_gate
    return get_host_mutation_gate(getattr(BasicConfig, 'working_dir', None) or Path.cwd())


__all__ = [
    "RESTART_EXIT_CODE",
    "RestartCoordinator",
    "exit_if_restart_requested",
    "get_restart_coordinator",
    "is_restart_requested",
    "is_safe_restart_enabled",
    "set_safe_restart_enabled",
    "spawn_replacement_process",
]
