"""OS 本地 DAG 执行器 — 走图核心。

分两层，职责严格解耦：

1. ``DagWalk``：**纯同步状态机**，只管依赖偏序（I1/I2/I5/I6）。无 I/O、无锁、
   无时钟、无 asyncio。调度是数学 —— 这一层是 Hypothesis 不变量测试的靶子。
2. ``DagExecutor``：**异步驱动**，把纯状态机接到「注入的节点调度器」
   ``submit(node) -> awaitable(NodeState)`` 上。不同设备的 ready 节点并发起跑；
   同设备互斥（I3）不在此层做 —— 由注入的调度器（生产=DeviceActionManager
   每设备锁）天然保证。时钟亦在调度器一侧注入，执行器保持与墙钟无关、无 time.sleep。

见 docs/features/F002-os-local-dag-executor/interface-design.md §二。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Optional

from unilabos.scheduler.dag_model import (
    NodeState,
    TaskDag,
    TERMINAL_STATES,
)

logger = logging.getLogger(__name__)

# 注入的节点调度器：给一个节点，返回一个 awaitable，解析为该节点终态。
SubmitFn = Callable[["object"], Awaitable[NodeState]]
# 节点终态回调（用于游标持久化 / 日志），(node_id, 终态)。
OnTerminalFn = Callable[[str, NodeState], None]

# 非终态集合：可被 fail-fast 取消的状态
_ACTIVE_STATES = frozenset(
    {NodeState.PENDING, NodeState.READY, NodeState.RUNNING}
)


class DagWalk:
    """纯同步走图状态机：依赖偏序的唯一真相。

    - ready()：indeg==0 且仍 PENDING 的节点集（供驱动层提交）。
    - mark_running(id)：PENDING/READY -> RUNNING（每节点恰好一次，I1）。
    - on_success(id)：-> SUCCESS，并对每条 out-edge 递减后继 indeg（I2）。
    - on_failed(id)：-> FAILED 且 fail-fast：其余非终态 -> CANCELLED。
    - resume：以 completed 初始化，已完成节点视作 SUCCESS 并预满足后继依赖（I4）。
    """

    def __init__(self, dag: TaskDag, *, completed: Iterable[str] = ()) -> None:
        self.dag = dag
        self.indeg: dict[str, int] = dag.build_indegree()
        self.adj: dict[str, list[str]] = dag.adjacency()
        self.states: dict[str, NodeState] = {
            nid: NodeState.PENDING for nid in dag.nodes
        }
        self.failed = False
        # resume：把已完成节点直接置 SUCCESS 并释放其对后继的约束
        for nid in completed:
            if nid in self.states and self.states[nid] != NodeState.SUCCESS:
                self._apply_success(nid)

    def ready(self) -> list[str]:
        """当前可提交的节点：仍 PENDING 且入度归零。顺序稳定（按插入序）。"""
        if self.failed:
            return []
        return [
            nid
            for nid in self.dag.nodes
            if self.states[nid] == NodeState.PENDING and self.indeg[nid] == 0
        ]

    def mark_running(self, node_id: str) -> None:
        st = self.states[node_id]
        if st != NodeState.PENDING:
            raise RuntimeError(f"节点 {node_id} 状态为 {st}，不可重复提交（违反 I1）")
        self.states[node_id] = NodeState.RUNNING

    def on_success(self, node_id: str) -> None:
        if self.states[node_id] != NodeState.RUNNING:
            raise RuntimeError(f"节点 {node_id} 未在运行，不能置成功")
        self._apply_success(node_id)

    def _apply_success(self, node_id: str) -> None:
        self.states[node_id] = NodeState.SUCCESS
        for succ in self.adj[node_id]:
            self.indeg[succ] -= 1

    def on_failed(self, node_id: str) -> None:
        self.states[node_id] = NodeState.FAILED
        self.failed = True
        # fail-fast：对齐 backend gctx 兄弟组取消语义，其余非终态一律 CANCELLED
        for nid, st in self.states.items():
            if nid != node_id and st in _ACTIVE_STATES:
                self.states[nid] = NodeState.CANCELLED

    def cancel_remaining(self) -> None:
        """外部取消（cancel_task）：把所有仍非终态的节点一律标记为 CANCELLED。

        与 on_failed 不同：不置 failed（非失败终止），仅停止后续并把未决节点收敛到
        终态，避免 run() 返回含 PENDING/RUNNING 的非终态快照。
        """
        for nid, st in self.states.items():
            if st in _ACTIVE_STATES:
                self.states[nid] = NodeState.CANCELLED

    def is_done(self) -> bool:
        return all(st in TERMINAL_STATES for st in self.states.values())

    def running_nodes(self) -> list[str]:
        return [nid for nid, st in self.states.items() if st == NodeState.RUNNING]

    def snapshot(self) -> dict[str, NodeState]:
        return dict(self.states)


class DagExecutor:
    """异步驱动：把 ready 集提交给注入的调度器，并发走完整张图。

    submit：async callable(DagNode) -> NodeState（SUCCESS/FAILED）。生产实现 =
        复用 ws_client _handle_job_start 的 send_goal 路径（经 DeviceActionManager）；
        测试实现 = fake 调度器（可控时钟 + 可编程结果）。
    on_node_terminal：每个节点到终态时回调，用于 T03 游标持久化 / 日志。
    walk：可注入既有 DagWalk（用于 resume），缺省新建。
    """

    def __init__(
        self,
        dag: TaskDag,
        submit: SubmitFn,
        *,
        on_node_terminal: Optional[OnTerminalFn] = None,
        walk: Optional[DagWalk] = None,
    ) -> None:
        self.dag = dag
        self._submit = submit
        self._on_terminal = on_node_terminal
        self.walk = walk if walk is not None else DagWalk(dag)
        self._cancelled = False

    def cancel(self) -> None:
        """外部取消（对应 cancel_task）：停止调度后继，已在跑节点由上层取消。"""
        self._cancelled = True

    async def run(self) -> dict[str, NodeState]:
        """走完整张图，返回每节点终态快照。无环则有限步终止（I6）。"""
        inflight: dict[str, asyncio.Task] = {}
        try:
            while not self.walk.is_done() and not self._cancelled:
                # 1) 提交本轮全部 ready 节点（不同设备并发；同设备由调度器串行）
                for nid in self.walk.ready():
                    self.walk.mark_running(nid)
                    inflight[nid] = asyncio.ensure_future(self._run_node(nid))

                if not inflight:
                    # 无 ready 且无在跑：无环时意味着已完成；否则解析期已拒环，不会到这
                    break

                # 2) 等任一节点完成
                done, _ = await asyncio.wait(
                    inflight.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    nid, status = task.result()
                    inflight.pop(nid, None)
                    if status == NodeState.SUCCESS:
                        self.walk.on_success(nid)
                    elif status == NodeState.CANCELLED:
                        # 外部取消回流：不触发 fail-fast，直接落 CANCELLED 终态
                        self.walk.states[nid] = NodeState.CANCELLED
                    else:
                        self.walk.on_failed(nid)
                    self._notify_terminal(nid, status)
                    # fail-fast：某节点**失败**即取消其余在跑并停止调度（取消不算失败）
                    if status == NodeState.FAILED:
                        await self._cancel_inflight(inflight)
                        return self.walk.snapshot()
        finally:
            if inflight:
                await self._cancel_inflight(inflight)
        # 外部取消：把剩余未决节点收敛为 CANCELLED，返回全终态快照
        if self._cancelled:
            self.walk.cancel_remaining()
        return self.walk.snapshot()

    async def _run_node(self, node_id: str) -> tuple[str, NodeState]:
        status = await self._submit(self.dag.nodes[node_id])
        return node_id, status

    def _notify_terminal(self, node_id: str, status: NodeState) -> None:
        """触发终态回调；回调失败（如断网时上行 publish 抛错）绝不打断走图（AC-3）。"""
        if self._on_terminal is None:
            return
        try:
            self._on_terminal(node_id, status)
        except Exception:  # noqa: BLE001 —— 上行/持久化失败不应停摆本地走图
            logger.exception("DagExecutor on_node_terminal 回调失败，忽略并继续走图")

    @staticmethod
    async def _cancel_inflight(inflight: dict[str, asyncio.Task]) -> None:
        for task in inflight.values():
            task.cancel()
        # 等待取消完成，吞掉 CancelledError
        await asyncio.gather(*inflight.values(), return_exceptions=True)
        inflight.clear()
