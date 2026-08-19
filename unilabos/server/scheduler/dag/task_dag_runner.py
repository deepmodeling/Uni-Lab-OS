"""把整张 task_dag 接到现有 per-node job_start 执行栈的桥接层。

生产的执行栈是**回调/队列驱动**：ws_client._handle_job_start 构造 JobInfo →
DeviceActionManager.enqueue_job（每设备锁/幂等/串行 I3）→ HostNode.send_goal；
完成经另一线程的 publish_job_status 终态回调回流。而 DagExecutor 需要的是
``submit(node) -> awaitable(NodeState)``。TaskDagRunner 做这层适配：

- submit(node)：在事件循环上建一个 future 登记进 pending，调用注入的
  ``on_start_node``（真正的入队 + 视情况 send_goal 副作用），await 该 future。
- notify_terminal(job_id, status)：由 publish_job_status 在终态时**跨线程**回调，
  经 loop.call_soon_threadsafe 解析对应 future 为 NodeState（node_id 即 job_id）。
- cancel()：停止 DagExecutor 调度后继，并把未决 future 一律解析为 CANCELLED，
  避免被取消的设备任务永不回终态而使 run() 悬挂。

DagExecutor 只管依赖偏序（I1/I2），同设备互斥由注入栈的 DeviceActionManager
天然保证（I3）。本层不复制互斥逻辑。见
docs/features/F002-os-local-dag-executor/interface-design.md §三。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from unilabos.server.scheduler.dag.dag_executor import DagExecutor, DagWalk, OnTerminalFn
from unilabos.server.scheduler.dag.dag_model import DagNode, NodeState, TaskDag

logger = logging.getLogger(__name__)

# 入队 + 视情况 send_goal 的副作用（ws 侧提供，复用 _handle_job_start 路径）。
StartNodeFn = Callable[[DagNode], None]
# 走图终止（失败/取消）后清理仍在设备侧运行的本 task 任务（ws 侧提供，
# 复用 DeviceActionManager.cancel_jobs_by_task_id）。
CancelRemainingFn = Callable[[], None]


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
        loop: Optional[asyncio.AbstractEventLoop] = None,
        walk: Optional[DagWalk] = None,
    ) -> None:
        self.dag = dag
        self._on_start_node = on_start_node
        self._on_cancel_remaining = on_cancel_remaining
        self._loop = loop
        self._pending: dict[str, asyncio.Future] = {}  # node_id(=job_id) -> future
        self._cancelled = False
        self._executor = DagExecutor(
            dag, self._submit, on_node_terminal=on_node_terminal, walk=walk
        )

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
        if self._on_cancel_remaining is not None and any(
            st != NodeState.SUCCESS for st in result.values()
        ):
            try:
                self._on_cancel_remaining()
            except Exception:  # noqa: BLE001 —— 清理失败不应改变已定终态
                logger.exception("TaskDagRunner on_cancel_remaining 清理失败，忽略")
        return result

    async def _submit(self, node: DagNode) -> NodeState:
        """DagExecutor 注入点：登记 future -> 触发入队/起跑 -> 等终态。"""
        loop = self._loop or asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # 先登记再触发副作用：即便终态瞬间回流（跨线程 call_soon_threadsafe），
        # 也只会在本协程让出后执行 _resolve，pending 已就位，无竞态。
        self._pending[node.node_id] = fut
        if self._cancelled:
            self._pending.pop(node.node_id, None)
            return NodeState.CANCELLED
        try:
            self._on_start_node(node)
        except Exception:  # noqa: BLE001 —— 单节点起跑失败即置该节点 FAILED
            logger.exception("TaskDagRunner on_start_node 失败，节点 %s 置 FAILED", node.node_id)
            self._pending.pop(node.node_id, None)
            return NodeState.FAILED
        return await fut

    def notify_terminal(self, job_id: str, status: str | NodeState) -> None:
        """由 publish_job_status 终态时**跨线程**回调，解析对应节点 future。"""
        state = status if isinstance(status, NodeState) else _status_to_state(status)
        loop = self._loop
        if loop is None:
            # run() 尚未起跑：直接同线程解析
            self._resolve(job_id, state)
            return
        loop.call_soon_threadsafe(self._resolve, job_id, state)

    def cancel(self) -> None:
        """外部取消（cancel_task）：停止调度后继，未决节点解析为 CANCELLED。

        设备侧仍在运行的任务由 ws 层 cancel_jobs_by_task_id 取消（此处不重复）。
        """
        self._cancelled = True
        self._executor.cancel()
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
