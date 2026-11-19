"""把 Backend Scheduler DAG 接到回调式动作执行层。

执行栈是回调驱动：Scheduler 获得完整资源集合后调用 JobExecutionBackend，
完成经另一线程的 publish_job_status 终态回调回流。而 DagExecutor 需要的是
``submit(node) -> awaitable(NodeState)``。TaskDagRunner 做这层适配：

- submit(node)：在事件循环上建一个 future 登记进 pending，调用注入的
  ``on_start_node``（资源准入 + 执行副作用），await 该 future。
- notify_terminal(job_id, status)：由 publish_job_status 在终态时**跨线程**回调，
  经 loop.call_soon_threadsafe 解析对应 future 为 NodeState（node_id 即 job_id）。
- cancel()：停止 DagExecutor 调度后继，并把未决 future 一律解析为 CANCELLED，
  避免被取消的设备任务永不回终态而使 run() 悬挂。

DagExecutor 只管依赖偏序，资源互斥由唯一 SchedulerResourceManager 处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Optional

from unilabos.server.backend.scheduler.dag.executor import DagExecutor, DagWalk, OnTerminalFn
from unilabos.server.backend.scheduler.dag.models import DagNode, NodeState, TaskDag

logger = logging.getLogger(__name__)

# 入队 + 视情况 send_goal 的副作用（ws 侧提供，复用 _handle_job_start 路径）。
StartNodeFn = Callable[[DagNode], None]
# 走图终止（失败/取消）后清理仍在设备侧运行的本 task 任务（ws 侧提供，
# 复用 DeviceActionManager.cancel_jobs_by_task_id）。
CancelRemainingFn = Callable[[], None]
# 跑一轮循环体：入参是本轮可视作已完成的节点（恢复用），返回循环体各节点终态。
RunBodyFn = Callable[[Iterable[str]], Awaitable[dict[str, NodeState]]]
# 循环节点的驱动（调度器提供）：按 LoopSpec 决定跑几轮、每轮前重臂循环体 attempt、
# 求值 while 条件，最终给出循环节点自己的终态。
RunLoopFn = Callable[[DagNode, RunBodyFn], Awaitable[NodeState]]


def _status_to_state(status: str) -> NodeState:
    """把 ws 的 job_status 字符串映射为 NodeState（成功/失败二态）。"""
    return NodeState.SUCCESS if status == "success" else NodeState.FAILED


class TaskDagRunner:
    """单张 task_dag 的驱动器：桥接 DagExecutor 与回调式 per-node 执行栈。"""

    def __init__(
        self,
        dag: TaskDag,
        on_start_node: StartNodeFn,
        *,
        on_node_terminal: Optional[OnTerminalFn] = None,
        on_cancel_remaining: Optional[CancelRemainingFn] = None,
        on_run_loop: Optional[RunLoopFn] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        walk: Optional[DagWalk] = None,
    ) -> None:
        self.dag = dag
        self._on_start_node = on_start_node
        self._on_node_terminal = on_node_terminal
        self._on_cancel_remaining = on_cancel_remaining
        self._on_run_loop = on_run_loop
        self._loop = loop
        self._pending: dict[str, asyncio.Future] = {}  # node_id(=job_id) -> future
        self._cancelled = False
        # 用户取消 / fail-fast：不再起跑新节点，但已下发的节点等真实终态（见 stop_dispatching）
        self._stopping = False
        self._stop_requested = False
        self._executor = DagExecutor(
            dag,
            self._submit,
            on_node_terminal=on_node_terminal,
            walk=walk,
            on_stop=self._request_stop,
        )
        # 正在跑的循环体执行器（嵌套层级各一个），取消时一并停掉
        self._nested: set[DagExecutor] = set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def run(self) -> dict[str, NodeState]:
        """走完整张 DAG，返回每节点终态。失败/取消后清理设备侧残余任务。"""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        try:
            result = await self._executor.run()
        finally:
            # 未决 future 兜底解析，避免异常路径下的悬挂
            self._resolve_all_pending(NodeState.CANCELLED)
        # 任一节点非 SUCCESS -> fail-fast，清理仍在设备侧运行/排队的本 task 任务
        if not self._stop_requested and self._on_cancel_remaining is not None and any(
            st != NodeState.SUCCESS for st in result.values()
        ):
            try:
                self._on_cancel_remaining()
            except Exception:  # noqa: BLE001 —— 清理失败不应改变已定终态
                logger.exception("TaskDagRunner on_cancel_remaining 清理失败，忽略")
        return result

    async def _submit(self, node: DagNode) -> NodeState:
        """DagExecutor 注入点：登记 future -> 触发入队/起跑 -> 等终态。

        循环容器不下发设备：交给调度器的循环驱动，循环体每轮作为嵌套 DAG 用同一个
        ``_submit`` 跑（循环体里的设备节点、嵌套循环都走这里）。
        """
        if node.is_loop:
            return await self._run_loop_node(node)
        loop = self._loop or asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # 先登记再触发副作用：即便终态瞬间回流（跨线程 call_soon_threadsafe），
        # 也只会在本协程让出后执行 _resolve，pending 已就位，无竞态。
        self._pending[node.node_id] = fut
        if self._cancelled or self._stopping:
            self._pending.pop(node.node_id, None)
            return NodeState.CANCELLED
        try:
            self._on_start_node(node)
        except Exception:  # noqa: BLE001 —— 单节点起跑失败即置该节点 FAILED
            logger.exception("TaskDagRunner on_start_node 失败，节点 %s 置 FAILED", node.node_id)
            self._pending.pop(node.node_id, None)
            return NodeState.FAILED
        return await fut

    async def _run_loop_node(self, node: DagNode) -> NodeState:
        if self._cancelled:
            return NodeState.CANCELLED
        if self._on_run_loop is None or node.body is None:
            logger.error("TaskDagRunner 没有循环驱动，循环节点 %s 置 FAILED", node.node_id)
            return NodeState.FAILED
        body = node.body

        async def run_body(completed: Iterable[str]) -> dict[str, NodeState]:
            if self._cancelled or self._stopping:
                return {node_id: NodeState.CANCELLED for node_id in body.nodes}
            executor = DagExecutor(
                body,
                self._submit,
                on_node_terminal=self._on_node_terminal,
                walk=DagWalk(body, completed=completed),
            )
            self._nested.add(executor)
            try:
                return await executor.run()
            finally:
                self._nested.discard(executor)

        try:
            return await self._on_run_loop(node, run_body)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 循环驱动异常即该循环失败，不拖垮整张图的收敛
            logger.exception("循环节点 %s 执行异常，置 FAILED", node.node_id)
            return NodeState.FAILED

    def notify_terminal(self, job_id: str, status: str | NodeState) -> None:
        """由 publish_job_status 终态时**跨线程**回调，解析对应节点 future。"""
        state = status if isinstance(status, NodeState) else _status_to_state(status)
        loop = self._loop
        if loop is None:
            # run() 尚未起跑：直接同线程解析
            self._resolve(job_id, state)
            return
        loop.call_soon_threadsafe(self._resolve, job_id, state)

    def _request_stop(self) -> None:
        """失败 / 取消后请调度器停掉本任务：未下发的节点收为 CANCELLED，已下发的发取消请求。"""

        self._stopping = True
        if self._stop_requested or self._on_cancel_remaining is None:
            return
        self._stop_requested = True
        try:
            self._on_cancel_remaining()
        except Exception:  # noqa: BLE001 —— 停止失败不应改变已定终态
            logger.exception("TaskDagRunner 停止任务失败，继续等待在跑节点")

    def stop_dispatching(self) -> None:
        """用户取消：不再起跑任何节点，在跑节点排空——已下发的动作等设备真实返回。

        与 :meth:`cancel`（进程停止）不同，这里不把在跑节点直接解析为 CANCELLED。
        """

        self._stopping = True
        self._executor.cancel()
        for nested in list(self._nested):
            nested.cancel()
        self._request_stop()

    def cancel(self) -> None:
        """外部取消（cancel_task）：停止调度后继，未决节点解析为 CANCELLED。

        设备侧仍在运行的任务由 ws 层 cancel_jobs_by_task_id 取消（此处不重复）。
        """
        self._cancelled = True
        self._executor.cancel()
        for nested in list(self._nested):
            nested.cancel()
        loop = self._loop
        if loop is None:
            self._resolve_all_pending(NodeState.CANCELLED)
        else:
            loop.call_soon_threadsafe(self._resolve_all_pending, NodeState.CANCELLED)

    def _resolve(self, job_id: str, state: NodeState) -> None:
        fut = self._pending.pop(job_id, None)
        if fut is None or fut.done():
            return
        fut.set_result(state)

    def _resolve_all_pending(self, state: NodeState) -> None:
        for job_id in list(self._pending):
            self._resolve(job_id, state)
