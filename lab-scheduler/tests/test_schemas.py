"""Pydantic schema serialization tests."""

from __future__ import annotations

from scheduler.api.schemas import (
    CompletedStep,
    ExecutionOrderEntry,
    InFlightStep,
    Machine,
    ObjectiveResult,
    RescheduleRequest,
    Resources,
    Robot,
    RobotState,
    ScheduleRequest,
    ScheduleResponse,
    Step,
    StepEntry,
    Task,
    TransferEntry,
)


class TestRequestModels:
    def test_step_roundtrip(self):
        s = Step(step_id="s1", machine_type="A", duration=10)
        data = s.model_dump()
        s2 = Step.model_validate(data)
        assert s2.step_id == "s1"
        assert s2.type == "experiment"
        assert s2.duration == 10

    def test_task_with_dependencies(self):
        t = Task(
            task_id="t1",
            priority=2.0,
            steps=[
                Step(step_id="s1", machine_type="A", duration=10),
                Step(step_id="s2", machine_type="B", duration=20),
            ],
            dependencies=[("s1", "s2")],
        )
        data = t.model_dump()
        t2 = Task.model_validate(data)
        assert len(t2.steps) == 2
        assert t2.dependencies == [("s1", "s2")]

    def test_schedule_request_defaults(self):
        req = ScheduleRequest(
            lab_id="lab1",
            tasks=[],
            resources=Resources(machines=[Machine(type="A", count=1)]),
        )
        assert req.algorithm == "WeightedCriticalPath"
        assert req.current_time == 0

    def test_reschedule_request(self):
        req = RescheduleRequest(
            lab_id="lab1",
            schedule_id="sched-123",
            tasks=[],
            resources=Resources(machines=[]),
            completed_steps=[
                CompletedStep(
                    step_id="s1", task_id="t1", status="success", actual_end=100
                )
            ],
            in_flight_steps=[
                InFlightStep(
                    step_id="s2",
                    task_id="t1",
                    device="A_0",
                    started_at=80,
                    estimated_end=120,
                )
            ],
            robot_states=[
                RobotState(robot_id="r1", location="A_0")
            ],
        )
        data = req.model_dump()
        req2 = RescheduleRequest.model_validate(data)
        assert req2.schedule_id == "sched-123"
        assert len(req2.completed_steps) == 1
        assert len(req2.in_flight_steps) == 1

    def test_robot_model(self):
        r = Robot(robot_id="r1", location="hub", capacity=5)
        assert r.available_from == 0


class TestResponseModels:
    def test_step_entry(self):
        e = StepEntry(
            step_id="s1", task_id="t1", start=0, end=10, resource="A_0"
        )
        assert e.model_dump()["resource"] == "A_0"

    def test_transfer_entry(self):
        t = TransferEntry(
            transfer_id="tr1",
            samples=["sample1"],
            start=10,
            end=15,
            robot_id="r1",
            path=["A_0", "B_0"],
        )
        data = t.model_dump()
        assert data["samples"] == ["sample1"]

    def test_schedule_response(self):
        resp = ScheduleResponse(
            schedule_id="sched-1",
            algorithm="Greedy",
            schedule=[
                StepEntry(
                    step_id="s1", task_id="t1", start=0, end=10, resource="A_0"
                )
            ],
            objective=ObjectiveResult(
                task_completions={"t1": 10},
                priority_weighted_cost=10.0,
                total_makespan=10,
            ),
            execution_order=[
                ExecutionOrderEntry(
                    priority=0,
                    step_id="s1",
                    task_id="t1",
                    device="A_0",
                    earliest_start=0,
                )
            ],
        )
        data = resp.model_dump()
        assert data["algorithm"] == "Greedy"
        assert len(data["schedule"]) == 1
        assert data["objective"]["total_makespan"] == 10

    def test_objective_result(self):
        obj = ObjectiveResult(
            task_completions={"t1": 100, "t2": 200},
            priority_weighted_cost=500.0,
            total_makespan=200,
        )
        assert obj.priority_weighted_cost == 500.0


# ── batch_capacity 测试 ──────────────────────────────────────


def test_machine_batch_capacity_default_is_1():
    m = Machine(type="X", count=1)
    assert m.batch_capacity == 1


def test_machine_batch_capacity_explicit():
    m = Machine(type="LightCycler_480", count=3, batch_capacity=2)
    assert m.batch_capacity == 2


def test_device_instance_batch_capacity_default():
    from scheduler.models.resources import DeviceInstance

    inst = DeviceInstance(instance_id="A_0", device_type="A")
    assert inst.batch_capacity == 1


def test_device_pool_propagates_batch_capacity():
    from scheduler.models.resources import DevicePool

    pool = DevicePool()
    pool.add_device_type("PCR", count=3, batch_capacity=2)
    for inst in pool.instances.values():
        assert inst.batch_capacity == 2


# ── Service layer integration tests ──────────────────────────


def test_service_propagates_batch_capacity_to_pool():
    from scheduler.service.scheduler_service import SchedulerService

    svc = SchedulerService()
    req = ScheduleRequest(
        lab_id="L",
        tasks=[Task(
            task_id="W",
            priority="high",
            steps=[Step(step_id="s1", machine_type="PCR", duration=1)],
        )],
        resources=Resources(machines=[Machine(type="PCR", count=2, batch_capacity=2)]),
    )
    # Build device pool via service internals
    pool = svc._build_device_pool(req)  # noqa: SLF001
    assert all(inst.batch_capacity == 2 for inst in pool.instances.values())


def test_service_uses_weight_not_priority():
    from scheduler.service.scheduler_service import SchedulerService

    svc = SchedulerService()
    req = ScheduleRequest(
        lab_id="L",
        tasks=[Task(
            task_id="W",
            priority="urgent",  # weight 300
            steps=[Step(step_id="s1", machine_type="PCR", duration=10)],
        )],
        resources=Resources(machines=[Machine(type="PCR", count=1)]),
    )
    resp = svc.schedule(req)
    assert resp.objective.priority_weighted_cost == 300.0 * 10
