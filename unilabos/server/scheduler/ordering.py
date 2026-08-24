"""通用任务排序接口。

入参 = ready tasks + 资源锁状态 + 优先级；出参 = 有序 task 列表。

两个实现：

- ``StableLocalOrderer``：本地稳定排序 stub（权重降序 → 提交时间升序 → node id），
  不依赖外部服务，是默认兜底。
- ``HttpSchedulerOrderer``：HTTP 调 uni-lab-scheduler（lab-scheduler 仓）的
  ``POST /api/v1/schedule``，请求/响应形状对齐其 api/schemas.py
  （ScheduleRequest / ScheduleResponse.execution_order）。失败时自动回退本地排序。

接口形状对齐 leaplab 设计文档 06_scheduler_public_api.md：Python 端是纯批量
优化器，Edge 侧扮演 Arbiter——每次触发点重新提交当前 ready 集合。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Protocol, Set

from unilabos.server.scheduler.models import ReadyTask
from unilabos.utils.tracing import inject_trace_context, span

logger = logging.getLogger(__name__)


class OrderingContext:
    """一次重排的资源上下文。"""

    def __init__(self, busy_device_action_keys: Set[str]):
        # 当前被占用的 device_action_key（已下发未完结 job 持有的锁）
        self.busy_device_action_keys = busy_device_action_keys


class TaskOrderer(Protocol):
    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        """返回下发顺序（可含全部 ready；service 层负责跳过锁忙的节点）。"""
        ...


class StableLocalOrderer:
    """稳定排序 stub：权重降序 → 提交时间升序 → workflow_id/node id 字典序。"""

    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        return sorted(
            ready,
            key=lambda t: (
                -t.priority_weight,
                t.submitted_at,
                t.workflow_id,
                t.node.id,
            ),
        )


class HttpSchedulerOrderer:
    """HTTP 调 uni-lab-scheduler 的排序实现。

    把每个 ready 节点映射为单 step 的 Task，机器类型用 device_action_key
    表达设备互斥；busy 锁通过 in_flight 无法表达（schedule 接口），
    因此这里只做全局排序，锁过滤仍由 service 层执行。

    step duration 通过 ``DurationEstimator`` 计算（声明式 gjson 取参 /
    历史 EMA 两种模式），未注入 estimator 时退回固定 default_duration_min ——
    时长直接影响 WeightedCriticalPath 等 makespan 算法的排序质量。
    """

    def __init__(
        self,
        base_url: str,
        lab_id: str = "edge-lab",
        algorithm: str = "WeightedCriticalPath",
        default_duration_min: float = 1.0,
        timeout_s: float = 5.0,
        fallback: TaskOrderer | None = None,
        estimator: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.lab_id = lab_id
        self.algorithm = algorithm
        self.default_duration_min = default_duration_min
        self.timeout_s = timeout_s
        self.fallback = fallback or StableLocalOrderer()
        # DurationEstimator（与 EdgeScheduler 共享实例，历史样本一处积累）
        self.estimator = estimator

    def _duration_min(self, task: "ReadyTask") -> float:
        """节点预估时长（分钟）。排序时参数尚未 resolve，用原始 param 声明。"""
        if self.estimator is None:
            return self.default_duration_min
        try:
            seconds, _source = self.estimator.estimate(
                task.node.device_action_key, task.node.param
            )
            return max(seconds / 60.0, 0.01)
        except Exception:  # noqa: BLE001 - 预估失败退回默认值，不影响排序
            return self.default_duration_min

    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        if len(ready) <= 1:
            return list(ready)
        try:
            with span(
                "scheduler.order",
                kind="client",
                attributes={
                    "scheduler.algorithm": self.algorithm,
                    "scheduler.ready.count": len(ready),
                    "lab.id": self.lab_id,
                },
            ):
                return self._order_remote(ready)
        except Exception as exc:  # noqa: BLE001 - 远端排序失败必须兜底
            logger.warning("[EdgeScheduler] remote ordering failed, fallback to local: %s", exc)
            return self.fallback.order(ready, ctx)

    def _order_remote(self, ready: List[ReadyTask]) -> List[ReadyTask]:
        machines: Dict[str, int] = {}
        tasks = []
        key_by_step: Dict[str, ReadyTask] = {}
        for t in ready:
            machine_type = t.node.device_action_key or "unknown"
            machines[machine_type] = machines.get(machine_type, 0) + 1
            step_id = f"{t.workflow_id}:{t.node.id}"
            key_by_step[step_id] = t
            tasks.append(
                {
                    "task_id": t.workflow_id,
                    "priority": t.priority_weight,
                    "steps": [
                        {
                            "step_id": step_id,
                            "machine_type": machine_type,
                            "duration": self._duration_min(t),
                        }
                    ],
                    "dependencies": [],
                }
            )

        payload = {
            "lab_id": self.lab_id,
            "algorithm": self.algorithm,
            "tasks": tasks,
            "resources": {
                "machines": [
                    {"type": machine_type, "count": 1} for machine_type in machines
                ]
            },
        }

        headers: Dict[str, Any] = {"Content-Type": "application/json"}
        inject_trace_context(headers)
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/schedule",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        execution_order = body.get("execution_order") or []
        ordered: List[ReadyTask] = []
        seen: Set[str] = set()
        for entry in sorted(execution_order, key=lambda e: e.get("priority", 0)):
            step_id = entry.get("step_id", "")
            task = key_by_step.get(step_id)
            if task is not None and step_id not in seen:
                ordered.append(task)
                seen.add(step_id)
        # 远端漏掉的节点补到队尾（保持不丢任务）
        for t in ready:
            step_id = f"{t.workflow_id}:{t.node.id}"
            if step_id not in seen:
                ordered.append(t)
                seen.add(step_id)
        return ordered


__all__ = [
    "HttpSchedulerOrderer",
    "OrderingContext",
    "StableLocalOrderer",
    "TaskOrderer",
]
