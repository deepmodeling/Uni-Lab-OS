"""FIFO 资源调度策略测试。"""

import json
from pathlib import Path

from task_orchestration.models import TaskDependency, TaskInstance, Template
from task_orchestration.policy import FifoResourcePolicy


def _template(
    template_id: str,
    resources: list[str],
    *,
    node_ids: list[str] | None = None,
    dependencies: list[TaskDependency] | None = None,
) -> Template:
    return Template(
        id=template_id,
        name=template_id,
        workflow_path="demo.json",
        node_ids=node_ids or [],
        resources=resources,
        dependencies=dependencies,
    )


def _instance(
    instance_id: str,
    template_id: str,
    sample_id: str,
    order: int,
    *,
    status: str = "pending",
    execution_state: dict | None = None,
) -> TaskInstance:
    timestamps = {}
    if status == "running":
        timestamps["started_at"] = 1
    elif status == "completed":
        timestamps.update({"started_at": 1, "finished_at": 2})
    return TaskInstance(
        id=instance_id,
        template_id=template_id,
        status=status,
        sample_id=sample_id,
        order=order,
        execution_state=execution_state or {},
        **timestamps,
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


def test_explicit_root_bypasses_legacy_lower_order_rule():
    policy = FifoResourcePolicy()
    instances = [
        _instance("earlier", "legacy", "sample-a", 1),
        _instance("root", "explicit-root", "sample-a", 2),
    ]

    result = policy.select(
        [
            _template("legacy", []),
            _template("explicit-root", [], dependencies=[]),
        ],
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"root"},
    )

    assert result.startable_instance_ids == ["root"]
    assert result.waiting_reasons == {}


def test_starts_same_sample_siblings_after_shared_dependency_completes():
    policy = FifoResourcePolicy()
    transfer_dependency = [TaskDependency(template_id="transfer")]
    templates = [
        _template("transfer", [], dependencies=[]),
        _template("density", [], dependencies=transfer_dependency),
        _template("vial", [], dependencies=transfer_dependency),
    ]
    instances = [
        _instance("transfer", "transfer", "sample-a", 10, status="completed"),
        _instance("density", "density", "sample-a", 11),
        _instance("vial", "vial", "sample-a", 12),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"density", "vial"},
    )

    assert result.startable_instance_ids == ["density", "vial"]
    assert result.waiting_reasons == {}


def test_waits_for_whole_task_dependency_to_complete():
    policy = FifoResourcePolicy()
    templates = [
        _template("transfer", [], dependencies=[]),
        _template(
            "density",
            [],
            dependencies=[TaskDependency(template_id="transfer")],
        ),
    ]
    instances = [
        _instance("transfer", "transfer", "sample-a", 10, status="running"),
        _instance("density", "density", "sample-a", 11),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"density"},
    )

    assert result.startable_instance_ids == []
    reason = result.waiting_reasons["density"]
    assert reason.code == "task_dependency_pending"
    assert reason.context == {
        "dependency_instance_id": "transfer",
        "dependency_template_id": "transfer",
        "dependency_status": "running",
    }


def test_node_dependency_starts_before_predecessor_task_finishes():
    policy = FifoResourcePolicy()
    templates = [
        _template(
            "pour",
            [],
            node_ids=["pick", "pour", "place"],
            dependencies=[],
        ),
        _template(
            "close",
            [],
            dependencies=[
                TaskDependency(template_id="pour", node_id="pour")
            ],
        ),
    ]
    instances = [
        _instance(
            "pour",
            "pour",
            "sample-a",
            15,
            status="running",
            execution_state={
                "cursor": 2,
                "records": [
                    {
                        "node_id": "pick",
                        "attempt": 1,
                        "execution_id": "pick-1",
                        "status": "succeeded",
                        "started_at": 1,
                        "finished_at": 2,
                    },
                    {
                        "node_id": "pour",
                        "attempt": 1,
                        "execution_id": "pour-1",
                        "status": "succeeded",
                        "started_at": 3,
                        "finished_at": 4,
                    },
                ],
            },
        ),
        _instance("close", "close", "sample-a", 16),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"close"},
    )

    assert result.startable_instance_ids == ["close"]
    assert result.waiting_reasons == {}


def test_node_dependency_waits_until_target_action_finishes():
    policy = FifoResourcePolicy()
    templates = [
        _template(
            "pour",
            [],
            node_ids=["pick", "pour", "place"],
            dependencies=[],
        ),
        _template(
            "close",
            [],
            dependencies=[
                TaskDependency(template_id="pour", node_id="pour")
            ],
        ),
    ]
    instances = [
        _instance(
            "pour",
            "pour",
            "sample-a",
            15,
            status="running",
            execution_state={
                "cursor": 1,
                "records": [
                    {
                        "node_id": "pick",
                        "attempt": 1,
                        "execution_id": "pick-1",
                        "status": "succeeded",
                        "started_at": 1,
                        "finished_at": 2,
                    }
                ],
            },
        ),
        _instance("close", "close", "sample-a", 16),
    ]

    result = policy.select(
        templates,
        instances,
        available_resources=set(),
        condition_satisfied_instance_ids={"close"},
    )

    assert result.startable_instance_ids == []
    reason = result.waiting_reasons["close"]
    assert reason.code == "task_dependency_node_pending"
    assert reason.context == {
        "dependency_instance_id": "pour",
        "dependency_template_id": "pour",
        "dependency_node_id": "pour",
    }


def test_szlab_seventeen_task_flow_reaches_both_parallel_branches():
    project_root = Path(__file__).resolve().parents[2]
    sidecar = json.loads(
        (
            project_root
            / "task-orchestration"
            / "szlab_robot_action_workflow.json.task-workspace.json"
        ).read_text(encoding="utf-8")
    )
    templates = [
        Template.model_validate(item)
        for item in sidecar["workspace"]["templates"]
    ]
    instances = [
        _instance(
            f"sample-a-task-{index:02d}",
            template.id,
            "sample-a",
            index,
        )
        for index, template in enumerate(templates, start=1)
    ]
    policy = FifoResourcePolicy()

    def ready_instance_ids() -> list[str]:
        result = policy.select(
            templates,
            instances,
            available_resources=set(),
            condition_satisfied_instance_ids={
                instance.id
                for instance in instances
                if instance.status == "pending"
            },
        )
        return result.startable_instance_ids

    def replace_instance(
        index: int,
        status: str,
        *,
        execution_state: dict | None = None,
    ) -> None:
        current = instances[index]
        instances[index] = _instance(
            current.id,
            current.template_id,
            current.sample_id,
            current.order,
            status=status,
            execution_state=execution_state,
        )

    for index in range(11):
        assert ready_instance_ids() == [instances[index].id]
        replace_instance(index, "completed")

    assert ready_instance_ids() == [instances[11].id, instances[12].id]

    replace_instance(11, "running")
    replace_instance(12, "completed")
    assert ready_instance_ids() == [instances[13].id]
    replace_instance(13, "completed")
    assert ready_instance_ids() == []

    replace_instance(11, "completed")
    assert ready_instance_ids() == [instances[14].id]

    replace_instance(
        14,
        "running",
        execution_state={
            "cursor": 1,
            "records": [
                {
                    "node_id": "w06_pick_beaker_s09_after_density",
                    "attempt": 1,
                    "execution_id": "pick-beaker-1",
                    "status": "succeeded",
                    "started_at": 1,
                    "finished_at": 2,
                }
            ],
        },
    )
    assert ready_instance_ids() == []

    replace_instance(
        14,
        "running",
        execution_state={
            "cursor": 2,
            "records": [
                {
                    "node_id": "w06_pick_beaker_s09_after_density",
                    "attempt": 1,
                    "execution_id": "pick-beaker-1",
                    "status": "succeeded",
                    "started_at": 1,
                    "finished_at": 2,
                },
                {
                    "node_id": "w07_pour_beaker_s08",
                    "attempt": 1,
                    "execution_id": "pour-beaker-1",
                    "status": "succeeded",
                    "started_at": 3,
                    "finished_at": 4,
                },
            ],
        },
    )
    assert ready_instance_ids() == [instances[15].id]
    assert templates[14].node_ids[instances[14].execution_state.cursor] == (
        "w07_place_beaker_s11"
    )

    replace_instance(15, "running")
    assert ready_instance_ids() == []
    replace_instance(14, "completed")
    assert ready_instance_ids() == []
    replace_instance(15, "completed")
    assert ready_instance_ids() == [instances[16].id]


def test_reports_missing_dependency_template_node_and_instance():
    policy = FifoResourcePolicy()
    current = _instance("current", "current", "sample-a", 2)

    missing_template = policy.select(
        [
            _template(
                "current",
                [],
                dependencies=[TaskDependency(template_id="missing")],
            )
        ],
        [current],
        available_resources=set(),
        condition_satisfied_instance_ids={"current"},
    )
    assert (
        missing_template.waiting_reasons["current"].code
        == "task_dependency_template_missing"
    )

    missing_node = policy.select(
        [
            _template("previous", [], node_ids=["known"], dependencies=[]),
            _template(
                "current",
                [],
                dependencies=[
                    TaskDependency(template_id="previous", node_id="missing")
                ],
            ),
        ],
        [
            _instance(
                "previous", "previous", "sample-a", 1, status="completed"
            ),
            current,
        ],
        available_resources=set(),
        condition_satisfied_instance_ids={"current"},
    )
    assert (
        missing_node.waiting_reasons["current"].code
        == "task_dependency_node_missing"
    )

    missing_instance = policy.select(
        [
            _template("previous", [], dependencies=[]),
            _template(
                "current",
                [],
                dependencies=[TaskDependency(template_id="previous")],
            ),
        ],
        [current],
        available_resources=set(),
        condition_satisfied_instance_ids={"current"},
    )
    assert (
        missing_instance.waiting_reasons["current"].code
        == "task_dependency_instance_missing"
    )
