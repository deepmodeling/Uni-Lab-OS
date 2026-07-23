"""FIFO 资源调度策略测试。"""

from task_orchestration.models import TaskInstance, Template
from task_orchestration.policy import FifoResourcePolicy


def _template(template_id: str, resources: list[str]) -> Template:
    return Template(
        id=template_id,
        name=template_id,
        workflow_path="demo.json",
        node_ids=[],
        resources=resources,
    )


def _instance(
    instance_id: str,
    template_id: str,
    sample_id: str,
    order: int,
    *,
    status: str = "pending",
) -> TaskInstance:
    return TaskInstance(
        id=instance_id,
        template_id=template_id,
        status=status,
        sample_id=sample_id,
        order=order,
    )


def test_waits_for_earlier_sample_task_before_starting_later_order():
    policy = FifoResourcePolicy()
    templates = [_template("prepare", ["robot"]), _template("measure", ["station"])]
    instances = [
        _instance("sample-a-2", "measure", "sample-a", 2),
        _instance("sample-a-1", "prepare", "sample-a", 1),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources={"robot", "station"},
        condition_satisfied_instance_ids={"sample-a-2"},
    )

    assert result.startable_instance_ids == []
    reason = result.waiting_reasons["sample-a-2"]
    assert reason.code == "sample_order_pending"
    assert reason.context == {"sample_id": "sample-a", "predecessor_order": 1}
    assert reason.message == "等待样品 sample-a 的 order 1 完成"


def test_static_template_resources_do_not_block_multiple_samples_from_starting():
    policy = FifoResourcePolicy()
    templates = [_template("first", ["robot"]), _template("second", ["robot"])]
    instances = [
        _instance("task-1", "first", "sample-a", 1),
        _instance("task-2", "second", "sample-b", 1),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources={"robot"},
        condition_satisfied_instance_ids={"task-1", "task-2"},
    )

    assert result.startable_instance_ids == ["task-1", "task-2"]
    assert result.waiting_reasons == {}


def test_policy_receives_empty_available_resources_without_using_template_resources():
    policy = FifoResourcePolicy()
    templates = [_template("shared", ["legacy-robot"])]
    instances = [
        _instance("task-a", "shared", "sample-a", 0),
        _instance("task-b", "shared", "sample-b", 0),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"task-a", "task-b"},
    )

    assert result.startable_instance_ids == ["task-a", "task-b"]


def test_starts_independent_tasks_in_parallel_when_resources_do_not_conflict():
    policy = FifoResourcePolicy()
    templates = [_template("robot-work", ["robot"]), _template("station-work", ["station"])]
    instances = [
        _instance("task-1", "robot-work", "sample-a", 1),
        _instance("task-2", "station-work", "sample-b", 1),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources={"robot", "station"},
        condition_satisfied_instance_ids={"task-1", "task-2"},
    )

    assert result.startable_instance_ids == ["task-1", "task-2"]
    assert result.waiting_reasons == {}


def test_ignores_static_resource_availability_but_reports_missing_template():
    policy = FifoResourcePolicy()
    instances = [
        _instance("needs-station", "known-template", "sample-a", 1),
        _instance("orphan", "missing-template", "sample-b", 1),
    ]

    result = policy.select(
        [_template("known-template", ["station"])],
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"needs-station", "orphan"},
    )

    assert result.startable_instance_ids == ["needs-station"]
    assert result.waiting_reasons["orphan"].code == "template_missing"
    assert result.waiting_reasons["orphan"].context == {
        "template_id": "missing-template"
    }


def test_uses_order_sample_and_instance_id_as_stable_fifo_order():
    policy = FifoResourcePolicy()
    templates = [_template("shared", ["robot"])]
    instances = [
        _instance("z-task", "shared", "sample-a", 1),
        _instance("b-task", "shared", "sample-b", 1),
        _instance("a-task", "shared", "sample-b", 1),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources={"robot"},
        condition_satisfied_instance_ids={instance.id for instance in instances},
    )

    assert result.startable_instance_ids == ["z-task", "a-task", "b-task"]
    assert result.waiting_reasons == {}


def test_preserves_sample_predecessor_rule_when_instances_share_an_order():
    policy = FifoResourcePolicy()
    instances = [
        _instance("z-task", "shared", "sample-a", 1),
        _instance("a-task", "shared", "sample-a", 1),
    ]

    result = policy.select(
        [_template("shared", ["robot"])],
        instances,
        available_resources={"robot"},
        condition_satisfied_instance_ids={"z-task", "a-task"},
    )

    assert result.startable_instance_ids == ["a-task", "z-task"]
    assert result.waiting_reasons == {}
