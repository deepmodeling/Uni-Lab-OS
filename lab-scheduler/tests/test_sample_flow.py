"""Tests for sample_flow analysis."""

from __future__ import annotations

from scheduler.api.schemas import Step, StepEntry, Task
from scheduler.core.sample_flow import TransferRequest, analyze_sample_flow


def _make_tasks_and_schedule(
    tasks_spec: list[dict],
    schedule_spec: list[dict],
) -> tuple[list[Task], list[StepEntry]]:
    tasks = []
    for t in tasks_spec:
        tasks.append(Task(
            task_id=t["task_id"],
            priority=t.get("priority", 1.0),
            steps=[Step(**s) for s in t["steps"]],
            dependencies=t.get("dependencies", []),
        ))
    schedule = [StepEntry(**s) for s in schedule_spec]
    return tasks, schedule


class TestAnalyzeSampleFlow:
    def test_cross_device_transfer_detected(self):
        """样品从设备 A_0 到设备 B_0 → 产生 TransferRequest."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                    {"step_id": "s2", "machine_type": "B", "duration": 20,
                     "input_samples": ["sample-1"]},
                ],
                "dependencies": [("s1", "s2")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "B_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)

        assert len(results) == 1
        req = results[0]
        assert req.sample_id == "sample-1"
        assert req.from_device == "A_0"
        assert req.to_device == "B_0"
        assert req.ready_time == 10
        assert req.deadline == 10

    def test_same_device_no_transfer(self):
        """同一设备上的 producer/consumer → 不需要转运."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                    {"step_id": "s2", "machine_type": "A", "duration": 20,
                     "input_samples": ["sample-1"]},
                ],
                "dependencies": [("s1", "s2")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "A_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)
        assert len(results) == 0

    def test_no_consumer_no_transfer(self):
        """样品无消费者 → 不产生转运请求."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                ],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)
        assert len(results) == 0

    def test_multiple_samples_multiple_transfers(self):
        """多个样品跨设备 → 产生多个 TransferRequest."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1", "sample-2"]},
                    {"step_id": "s2", "machine_type": "B", "duration": 20,
                     "input_samples": ["sample-1"]},
                    {"step_id": "s3", "machine_type": "C", "duration": 15,
                     "input_samples": ["sample-2"]},
                ],
                "dependencies": [("s1", "s2"), ("s1", "s3")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "B_0"},
                {"step_id": "s3", "task_id": "t1", "start": 10, "end": 25, "resource": "C_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)

        assert len(results) == 2
        sample_ids = {r.sample_id for r in results}
        assert sample_ids == {"sample-1", "sample-2"}

    def test_fan_out_sample(self):
        """一个样品被两个不同设备的 step 消费 → 两个 TransferRequest."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                    {"step_id": "s2", "machine_type": "B", "duration": 20,
                     "input_samples": ["sample-1"]},
                    {"step_id": "s3", "machine_type": "C", "duration": 15,
                     "input_samples": ["sample-1"]},
                ],
                "dependencies": [("s1", "s2"), ("s1", "s3")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "B_0"},
                {"step_id": "s3", "task_id": "t1", "start": 10, "end": 25, "resource": "C_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)

        assert len(results) == 2
        destinations = {r.to_device for r in results}
        assert destinations == {"B_0", "C_0"}

    def test_results_sorted_by_ready_time(self):
        """结果按 ready_time 升序排列."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                    {"step_id": "s2", "machine_type": "A", "duration": 20,
                     "output_samples": ["sample-2"]},
                    {"step_id": "s3", "machine_type": "B", "duration": 5,
                     "input_samples": ["sample-1"]},
                    {"step_id": "s4", "machine_type": "B", "duration": 5,
                     "input_samples": ["sample-2"]},
                ],
                "dependencies": [("s1", "s3"), ("s2", "s4")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "A_0"},
                {"step_id": "s3", "task_id": "t1", "start": 10, "end": 15, "resource": "B_0"},
                {"step_id": "s4", "task_id": "t1", "start": 30, "end": 35, "resource": "B_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)

        assert len(results) == 2
        assert results[0].ready_time <= results[1].ready_time

    def test_priority_inherited(self):
        """TransferRequest 继承 task 优先级."""
        tasks, schedule = _make_tasks_and_schedule(
            tasks_spec=[{
                "task_id": "t1",
                "priority": 5.0,
                "steps": [
                    {"step_id": "s1", "machine_type": "A", "duration": 10,
                     "output_samples": ["sample-1"]},
                    {"step_id": "s2", "machine_type": "B", "duration": 20,
                     "input_samples": ["sample-1"]},
                ],
                "dependencies": [("s1", "s2")],
            }],
            schedule_spec=[
                {"step_id": "s1", "task_id": "t1", "start": 0, "end": 10, "resource": "A_0"},
                {"step_id": "s2", "task_id": "t1", "start": 10, "end": 30, "resource": "B_0"},
            ],
        )
        results = analyze_sample_flow(tasks, schedule)

        assert len(results) == 1
        assert results[0].priority == 5.0
