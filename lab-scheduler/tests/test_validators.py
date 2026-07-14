from scheduler.core.validators import validate_time_constraints
from scheduler.models.dag import StepNode, TaskDAG, TimeConstraint
from scheduler.models.schedule_result import ScheduledStep


def _build_dag():
    nodes = {
        "s1": StepNode(step_id="s1", task_id="W", machine_type="X", duration=5),
        "s2": StepNode(step_id="s2", task_id="W", machine_type="X", duration=3),
    }
    return TaskDAG(
        task_id="W", priority=1.0, weight=1.0, nodes=nodes,
        time_constraints=[TimeConstraint(from_step="s1", to_step="s2", min_gap=2)],
    )


def test_no_violation():
    dag = _build_dag()
    results = [
        ScheduledStep(step_id="s1", task_id="W", start=0, end=5, device_instance="X_0"),
        ScheduledStep(step_id="s2", task_id="W", start=7, end=10, device_instance="X_0"),
    ]
    assert validate_time_constraints(results, [dag]) == []


def test_min_gap_violated():
    dag = _build_dag()
    results = [
        ScheduledStep(step_id="s1", task_id="W", start=0, end=5, device_instance="X_0"),
        ScheduledStep(step_id="s2", task_id="W", start=6, end=9, device_instance="X_0"),  # gap=1 < 2
    ]
    violations = validate_time_constraints(results, [dag])
    assert len(violations) == 1
    assert "s2" in violations[0] and "min_gap" in violations[0]
