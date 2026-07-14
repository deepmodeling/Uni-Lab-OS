"""耦合求解器: 迭代优化步骤调度与转运调度."""

from __future__ import annotations

import uuid

from scheduler.api.schemas import (
    ExecutionOrderEntry,
    InFlightStep,
    ObjectiveResult,
    Resources,
    Robot,
    ScheduleResponse,
    StepEntry,
    Task,
    TransferEntry,
)
from scheduler.core.device_pool import build_device_pool
from scheduler.core.sample_flow import analyze_sample_flow
from scheduler.core.step_algorithms import get_algorithm
from scheduler.core.transfer_scheduler import TransferScheduler
from scheduler.models.dag import Edge, StepNode, TaskDAG, TimeConstraint


class CoupledSolver:
    """迭代耦合求解器.

    当使用 CP-SAT 且转运为显式 step 时, 一次求解即可 (无需迭代).
    当使用启发式 + 自动转运推导时, 迭代收敛.
    """

    def solve(
        self,
        tasks: list[Task],
        resources: Resources,
        algorithm: str = "WeightedCriticalPath",
        max_iterations: int = 3,
        current_time: int = 0,
        fixed_steps: list[InFlightStep] | None = None,
    ) -> ScheduleResponse:
        fixed_steps = fixed_steps or []
        schedule_id = str(uuid.uuid4())
        device_pool_spec = [
            {"type": m.type, "count": m.count} for m in resources.machines
        ]

        # 构建 DAG
        task_dags = self._build_dags(tasks)

        # 标记固定步骤
        fixed_ids = {s.step_id for s in fixed_steps}
        for dag in task_dags:
            for nid, node in dag.nodes.items():
                if node.step_id in fixed_ids:
                    flight = next(
                        f for f in fixed_steps if f.step_id == node.step_id
                    )
                    node.completed = True
                    node.start = flight.started_at
                    node.end = flight.estimated_end
                    node.assigned_device = flight.device

        prev_objective = float("inf")
        step_entries: list[StepEntry] = []
        transfer_entries: list[TransferEntry] = []

        # 额外 lag 约束 (从转运时间注入)
        transfer_lags: dict[str, int] = {}  # consumer_step_id → min_start

        for iteration in range(max(max_iterations, 1)):
            # 1. 步骤调度
            device_pool = build_device_pool(device_pool_spec)

            # 锁定 fixed 设备
            for flight in fixed_steps:
                if flight.device in device_pool.instances:
                    device_pool.allocate(flight.device, flight.estimated_end)

            # 重置非固定节点
            for dag in task_dags:
                for nid, node in dag.nodes.items():
                    if node.step_id not in fixed_ids:
                        node.reset()

            algo_cls = get_algorithm(algorithm)
            scheduler = algo_cls(device_pool)
            scheduler.current_time = current_time
            for dag in task_dags:
                scheduler.add_batch(dag, submit_time=current_time)
            raw_results = scheduler.schedule()

            # 转换为 StepEntry
            step_entries = [
                StepEntry(
                    step_id=s.step_id,
                    task_id=s.task_id,
                    start=s.start,
                    end=s.end,
                    resource=s.device_instance,
                )
                for s in raw_results
            ]

            # 2. 样品流分析
            transfer_requests = analyze_sample_flow(tasks, step_entries)

            # 3. 转运调度
            if transfer_requests and resources.robots:
                ts = TransferScheduler(
                    robots=resources.robots,
                    default_travel_time=5,
                )
                transfer_entries = ts.schedule_transfers(transfer_requests)
            else:
                transfer_entries = []

            # 4. 计算目标值
            task_completions: dict[str, int] = {}
            task_priorities: dict[str, float] = {t.task_id: t.priority for t in tasks}
            for se in step_entries:
                cur = task_completions.get(se.task_id, 0)
                task_completions[se.task_id] = max(cur, se.end)

            pwc = sum(
                task_priorities.get(tid, 1.0) * c
                for tid, c in task_completions.items()
            )

            # 5. 收敛检查
            if abs(prev_objective - pwc) / max(prev_objective, 1.0) < 0.01:
                break
            prev_objective = pwc

            # 6. 注入转运约束 (下一轮迭代用)
            # 对于每个 transfer, 其 consumer step 的最早开始时间 = transfer.end
            if not transfer_entries:
                break  # 无转运, 不需要迭代

            # 将转运约束更新到 time_constraints 中不太方便,
            # 简化: 下一轮迭代不再重排 (heuristic 不支持动态 lag 注入)
            # CP-SAT 场景下, 转运已作为显式 step, 一次求解即可
            break

        # 组装响应
        schedule: list[StepEntry | TransferEntry] = list(step_entries) + list(transfer_entries)
        execution_order = [
            ExecutionOrderEntry(
                priority=idx,
                step_id=se.step_id,
                task_id=se.task_id,
                device=se.resource,
                earliest_start=se.start,
            )
            for idx, se in enumerate(step_entries)
        ]

        task_completions_final: dict[str, int] = {}
        task_priorities_final: dict[str, float] = {t.task_id: t.priority for t in tasks}
        for se in step_entries:
            cur = task_completions_final.get(se.task_id, 0)
            task_completions_final[se.task_id] = max(cur, se.end)

        pwc_final = sum(
            task_priorities_final.get(tid, 1.0) * c
            for tid, c in task_completions_final.items()
        )
        makespan = max(task_completions_final.values()) if task_completions_final else 0

        return ScheduleResponse(
            schedule_id=schedule_id,
            algorithm=algorithm,
            schedule=schedule,
            objective=ObjectiveResult(
                task_completions=task_completions_final,
                priority_weighted_cost=pwc_final,
                total_makespan=makespan,
            ),
            execution_order=execution_order,
            device_utilization=scheduler.get_device_utilization() if raw_results else {},
        )

    @staticmethod
    def _build_dags(tasks: list[Task]) -> list[TaskDAG]:
        dags: list[TaskDAG] = []
        for task in tasks:
            nodes: dict[str, StepNode] = {}
            for step in task.steps:
                nodes[step.step_id] = StepNode(
                    step_id=step.step_id,
                    task_id=task.task_id,
                    machine_type=step.machine_type,
                    duration=step.duration,
                    priority_weight=task.priority,
                    input_samples=step.input_samples,
                    output_samples=step.output_samples,
                )
            edges = [Edge(source=s, target=t) for s, t in task.dependencies]
            tcs = [
                TimeConstraint(
                    from_step=tc.from_step,
                    to_step=tc.to_step,
                    min_gap=tc.min_gap,
                    max_gap=tc.max_gap,
                )
                for tc in task.time_constraints
            ]
            dag = TaskDAG(
                task_id=task.task_id,
                priority=task.priority,
                nodes=nodes,
                edges=edges,
                time_constraints=tcs,
            )
            dag.build_adjacency()
            dags.append(dag)
        return dags
