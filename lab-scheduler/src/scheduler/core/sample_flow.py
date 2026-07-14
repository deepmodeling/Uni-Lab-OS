"""样品流分析: 从调度结果推导跨设备转运需求."""

from __future__ import annotations

from dataclasses import dataclass

from scheduler.api.schemas import StepEntry, Task


@dataclass
class TransferRequest:
    """一个样品的跨设备转运需求."""

    sample_id: str
    from_device: str  # 产出设备
    to_device: str  # 消费设备
    ready_time: int  # 样品就绪时间 (producer_step.end)
    deadline: int  # 最晚送达 (consumer_step.start)
    task_id: str
    priority: float  # 继承任务优先级


def analyze_sample_flow(
    tasks: list[Task],
    step_schedule: list[StepEntry],
) -> list[TransferRequest]:
    """分析步骤间的样品转运需求.

    1. 建立 sample → producer_step (output_samples) 映射
    2. 建立 sample → consumer_step (input_samples) 映射
    3. 查找调度结果中的设备分配和时间
    4. 若 producer 和 consumer 在不同设备 → 生成 TransferRequest
    """
    # 建立 step_id → (task_id, Step) 映射
    step_info: dict[str, tuple[str, object]] = {}
    task_priority: dict[str, float] = {}
    for task in tasks:
        task_priority[task.task_id] = task.priority
        for step in task.steps:
            step_info[step.step_id] = (task.task_id, step)

    # 建立 step_id → StepEntry 映射 (调度结果)
    schedule_map: dict[str, StepEntry] = {}
    for entry in step_schedule:
        schedule_map[entry.step_id] = entry

    # 建立 sample → producer / consumer 映射
    sample_producer: dict[str, str] = {}  # sample_id → step_id
    sample_consumers: dict[str, list[str]] = {}  # sample_id → [step_id, ...]

    for task in tasks:
        for step in task.steps:
            for sample in step.output_samples:
                sample_producer[sample] = step.step_id
            for sample in step.input_samples:
                sample_consumers.setdefault(sample, []).append(step.step_id)

    # 生成 TransferRequest
    results: list[TransferRequest] = []
    for sample_id, producer_sid in sample_producer.items():
        consumers = sample_consumers.get(sample_id, [])
        if not consumers:
            continue

        producer_entry = schedule_map.get(producer_sid)
        if producer_entry is None:
            continue

        tid, _ = step_info.get(producer_sid, ("", None))
        priority = task_priority.get(tid, 1.0)

        for consumer_sid in consumers:
            consumer_entry = schedule_map.get(consumer_sid)
            if consumer_entry is None:
                continue

            # 跳过同一设备上的操作 (无需转运)
            if producer_entry.resource == consumer_entry.resource:
                continue

            results.append(
                TransferRequest(
                    sample_id=sample_id,
                    from_device=producer_entry.resource,
                    to_device=consumer_entry.resource,
                    ready_time=producer_entry.end,
                    deadline=consumer_entry.start,
                    task_id=tid,
                    priority=priority,
                )
            )

    results.sort(key=lambda r: r.ready_time)
    return results
