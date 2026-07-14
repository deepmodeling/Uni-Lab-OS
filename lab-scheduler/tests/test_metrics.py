"""Tests for device load metrics (排队论 ρ)."""

from __future__ import annotations

import math

from scheduler.core.metrics import compute_device_load


class TestComputeDeviceLoad:
    def test_bottleneck_load_ratio_is_one(self):
        # A: 200/1=200 work/machine (瓶颈)；B: 200/2=100；C: 50/1=50
        summary = compute_device_load(
            busy_by_type={"A": 200.0, "B": 200.0, "C": 50.0},
            count_by_type={"A": 1, "B": 2, "C": 1},
            makespan=250.0,
        )
        assert summary.bottleneck_type == "A"
        assert summary.by_type["A"].load_ratio == 1.0
        assert summary.by_type["B"].load_ratio == 0.5  # 100/200
        assert summary.by_type["C"].load_ratio == 0.25  # 50/200

    def test_utilization_is_empirical_rho(self):
        # util = work / (makespan · count)
        summary = compute_device_load(
            busy_by_type={"A": 200.0, "B": 200.0},
            count_by_type={"A": 1, "B": 2},
            makespan=250.0,
        )
        assert summary.by_type["A"].utilization == 0.8  # 200/(250·1)
        assert summary.by_type["B"].utilization == 0.4  # 200/(250·2)

    def test_load_ratio_equals_normalized_utilization(self):
        # ρ̂_i = util_i / max(util)  —— 两口径在 makespan 共享下成比例
        summary = compute_device_load(
            busy_by_type={"A": 300.0, "B": 150.0},
            count_by_type={"A": 1, "B": 1},
            makespan=400.0,
        )
        max_util = max(d.utilization for d in summary.by_type.values())
        for d in summary.by_type.values():
            assert math.isclose(
                d.load_ratio, d.utilization / max_util, rel_tol=1e-6
            )

    def test_schedule_efficiency_equals_bottleneck_utilization(self):
        # eff = LB/makespan = max(utilization) = 瓶颈真实 ρ
        summary = compute_device_load(
            busy_by_type={"A": 200.0, "B": 50.0},
            count_by_type={"A": 1, "B": 1},
            makespan=250.0,
        )
        assert summary.makespan_lower_bound == 200.0
        assert summary.schedule_efficiency == 0.8
        assert summary.schedule_efficiency == max(
            d.utilization for d in summary.by_type.values()
        )

    def test_work_per_machine(self):
        summary = compute_device_load(
            busy_by_type={"A": 240.0},
            count_by_type={"A": 4},
            makespan=100.0,
        )
        assert summary.by_type["A"].count == 4
        assert summary.by_type["A"].busy_minutes == 240.0
        assert summary.by_type["A"].work_per_machine == 60.0

    def test_idle_type_included_with_zero_load(self):
        # count 决定输出类型; busy 缺失视为 0
        summary = compute_device_load(
            busy_by_type={"A": 100.0},
            count_by_type={"A": 1, "Idle": 2},
            makespan=200.0,
        )
        assert "Idle" in summary.by_type
        assert summary.by_type["Idle"].busy_minutes == 0.0
        assert summary.by_type["Idle"].load_ratio == 0.0
        assert summary.by_type["Idle"].utilization == 0.0

    def test_zero_makespan_returns_empty(self):
        summary = compute_device_load(
            busy_by_type={"A": 0.0},
            count_by_type={"A": 1},
            makespan=0.0,
        )
        assert summary.by_type == {}
        assert summary.bottleneck_type is None
        assert summary.schedule_efficiency == 0.0

    def test_no_devices_returns_empty(self):
        summary = compute_device_load({}, {}, 100.0)
        assert summary.by_type == {}
        assert summary.bottleneck_type is None


class TestSchedulerGetDeviceLoad:
    def _make_request(self):
        from scheduler.api.schemas import (
            Machine,
            Resources,
            ScheduleRequest,
            Step,
            Task,
        )

        # 两类设备: A 是瓶颈 (1 台串行 3 步), B 单步
        tasks = [
            Task(
                task_id="t0",
                steps=[
                    Step(step_id="t0_a", machine_type="A", duration=10),
                    Step(step_id="t0_b", machine_type="B", duration=5),
                ],
                dependencies=[("t0_a", "t0_b")],
            ),
            Task(
                task_id="t1",
                steps=[Step(step_id="t1_a", machine_type="A", duration=10)],
            ),
        ]
        return ScheduleRequest(
            lab_id="lab",
            tasks=tasks,
            resources=Resources(
                machines=[Machine(type="A", count=1), Machine(type="B", count=1)]
            ),
        )

    def test_response_carries_load_fields(self):
        from scheduler.service.scheduler_service import SchedulerService

        resp = SchedulerService().schedule(self._make_request())
        assert "A" in resp.device_load
        assert "B" in resp.device_load
        # A: 2 步 ×10 = 20 机时 / 1 台 = 20 work/machine (瓶颈)
        assert resp.device_load["A"].busy_minutes == 20.0
        assert resp.device_load["A"].load_ratio == 1.0
        assert resp.bottleneck_type == "A"
        # eff = 瓶颈真实 ρ = max(util) ∈ (0, 1]
        assert 0.0 < resp.schedule_efficiency <= 1.0
        assert resp.makespan_lower_bound == 20.0
        # 向后兼容字段仍在
        assert resp.device_utilization
