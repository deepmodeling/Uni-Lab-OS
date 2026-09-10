"""三个不同参数 WorkflowRun 的并行排程与物料流转演示。"""

from __future__ import annotations

import json

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def build_run(index: int, water_ml: float, salt_g: float) -> WorkflowSpec:
    run_id = f"run-brine-{index}"
    return WorkflowSpec(
        workflow_id=f"workflow-brine-{index}",
        run_id=run_id,
        task_id="task-parallel-brine",
        priority=100 - index,
        nodes=[
            WorkflowNode(
                id="mix",
                device_id=f"mixer-{index}",
                action_name="mix",
                action_type="goal",
                param={"water_ml": water_ml, "salt_g": salt_g},
                material_requirements=[
                    MaterialRequirement(
                        template_id="water", quantity=water_ml, unit="mL"
                    ),
                    MaterialRequirement(
                        template_id="sodium-chloride", quantity=salt_g, unit="g"
                    ),
                ],
            ),
            WorkflowNode(
                id="store",
                device_id="stack-robot",
                action_name="place_on_stack",
                action_type="goal",
                param={"stack_uuid": "stack-A", "slot_id": f"S0{index}"},
                material_outputs=[
                    {
                        "output_name": "brine",
                        "template_id": "brine-solution",
                        "parent_uuid": "stack-A",
                        "slot_id": f"S0{index}",
                    }
                ],
            ),
        ],
        edges=[
            WorkflowEdge(
                uuid=f"edge-{index}",
                source_node_id="mix",
                target_node_id="store",
            )
        ],
    )


def inflight_job(scheduler: EdgeScheduler, run_id: str, node_id: str) -> str:
    for job_id, job in scheduler.snapshot()["inflight_jobs"].items():
        if job["run_id"] == run_id and job["node_id"] == node_id:
            return job_id
    raise AssertionError(f"missing inflight job: {run_id}/{node_id}")


def main() -> None:
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot(
        template_id="water", lot_id="lot-water", quantity=300, unit="mL"
    )
    inventory.inbound_lot(
        template_id="sodium-chloride", lot_id="lot-salt", quantity=30, unit="g"
    )
    inventory.register_instance(template_id="stack", edge_uuid="stack-A")

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    runs = [
        build_run(1, 20, 2),
        build_run(2, 30, 3),
        build_run(3, 40, 4),
    ]

    submitted = scheduler.submit_workflow_runs(runs, task_id="task-parallel-brine")
    print("1. 首轮并发派发")
    print(
        json.dumps(
            [
                {
                    "run_id": job["run_id"],
                    "node_id": job["node_id"],
                    "device_id": job["device_id"],
                    "params": job["action_args"],
                }
                for job in dispatcher.dispatched[: len(submitted["dispatched"])]
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    print("输入库存已按三个 Run 分别消费")
    print(
        json.dumps(
            {
                "water_remaining_ml": inventory.store.get_lot("lot-water")[
                    "quantity_total"
                ],
                "salt_remaining_g": inventory.store.get_lot("lot-salt")[
                    "quantity_total"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # 三个 mix 完成后，三个 store 都已具备 DAG 条件，但共用 stack-robot，
    # 因此调度器每次只派发一个 store，其余保留在动态可调度队列。
    for index in (1, 2, 3):
        scheduler.on_job_finished(
            inflight_job(scheduler, f"run-brine-{index}", "mix"),
            True,
            {"mixed": True},
            run_id=f"run-brine-{index}",
        )

    print("2. 混合完成后的可调度队列")
    print(json.dumps(scheduler.snapshot()["schedulable_queue"], ensure_ascii=False, indent=2))

    for index, water_ml, salt_g in ((1, 20, 2), (2, 30, 3), (3, 40, 4)):
        job_id = inflight_job(scheduler, f"run-brine-{index}", "store")
        scheduler.on_job_finished(
            job_id,
            True,
            {
                "material_outputs": [
                    {
                        "output_name": "brine",
                        "content": {
                            "quantity": water_ml,
                            "unit": "mL",
                            "components": [
                                {
                                    "template_id": "water",
                                    "quantity": water_ml,
                                    "unit": "mL",
                                },
                                {
                                    "template_id": "sodium-chloride",
                                    "quantity": salt_g,
                                    "unit": "g",
                                },
                            ],
                        },
                    }
                ]
            },
            run_id=f"run-brine-{index}",
        )

    products = []
    for index in (1, 2, 3):
        output_uuid = f"material-output:run-brine-{index}:store:brine"
        instance = inventory.store.get_instance(output_uuid)
        relation = inventory.store.get_relation(output_uuid)
        content = inventory.store.get_content(output_uuid)
        products.append(
            {
                "run_id": f"run-brine-{index}",
                "material_uuid": output_uuid,
                "status": instance["status"],
                "stack_uuid": relation["parent_uuid"],
                "slot_id": relation["slot_id"],
                "content": json.loads(content["state_json"]),
            }
        )

    print("3. 三个 Run 的输出物料登记结果")
    print(json.dumps(products, ensure_ascii=False, indent=2))

    consumed = inventory.consume_material_content(
        products[0]["material_uuid"], 20, quantity_key="quantity", unit="mL"
    )
    print("4. Run 1 盐水耗尽")
    print(
        json.dumps(
            {
                "consume_result": consumed,
                "instance": inventory.store.get_instance(products[0]["material_uuid"]),
                "stack_relation": inventory.store.get_relation(products[0]["material_uuid"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
