from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from scheduler.api.schemas import Priority, PRIORITY_WEIGHTS, Task, Step


def _mk_step():
    return Step(step_id="s1", machine_type="X", duration=1)


def test_priority_enum_values():
    assert Priority.urgent == "urgent"
    assert Priority.high == "high"
    assert Priority.normal == "normal"
    assert Priority.low == "low"


def test_priority_weights():
    assert PRIORITY_WEIGHTS[Priority.urgent] == 300.0
    assert PRIORITY_WEIGHTS[Priority.high] == 200.0
    assert PRIORITY_WEIGHTS[Priority.normal] == 100.0
    assert PRIORITY_WEIGHTS[Priority.low] == 50.0


def test_task_default_priority_is_float_1():
    t = Task(task_id="W", steps=[_mk_step()])
    assert t.priority == 1.0
    assert t.weight == 1.0


def test_task_priority_string_resolves_to_enum_and_weight():
    t = Task(task_id="W", priority="high", steps=[_mk_step()])
    assert t.priority == Priority.high
    assert t.weight == 200.0


def test_task_priority_enum_resolves_to_weight():
    t = Task(task_id="W", priority=Priority.urgent, steps=[_mk_step()])
    assert t.weight == 300.0


def test_task_priority_float_passthrough():
    t = Task(task_id="W", priority=2.5, steps=[_mk_step()])
    assert t.priority == 2.5
    assert t.weight == 2.5


def test_task_priority_int_passthrough():
    t = Task(task_id="W", priority=3, steps=[_mk_step()])
    assert t.weight == 3.0


def test_task_priority_invalid_string_raises():
    with pytest.raises(ValidationError):
        Task(task_id="W", priority="not-a-level", steps=[_mk_step()])


def test_task_submitted_at_optional():
    t1 = Task(task_id="W", steps=[_mk_step()])
    assert t1.submitted_at is None
    now = datetime.now(timezone.utc)
    t2 = Task(task_id="W", submitted_at=now, steps=[_mk_step()])
    assert t2.submitted_at == now


def test_task_weight_serialized():
    t = Task(task_id="W", priority="urgent", steps=[_mk_step()])
    dumped = t.model_dump()
    assert dumped["weight"] == 300.0
