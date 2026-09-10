"""按机械臂/工位拆分的多 Workflow 物料流水线演示。"""

from __future__ import annotations

import json

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


ARM = "robot-arm"
WATER_STATION = "water-station"
SALT_STATION = "salt-station"
MIXER = "magnetic-stirrer"
STACK = "stack-robot"


def build_run(index: int, water_ml: float, salt_g: float) -> WorkflowSpec:
    run_id = f"run-pipeline-{index}"
    beaker_uuid = f"beaker-{index}"
    slot_id = f"S0{index}"
    # 取放和转运是独立的机械臂 Action；液体工位只在烧杯已经放下后运行。
    node_data = [
        ("pick", ARM, "pick_beaker", {"beaker_uuid": beaker_uuid}),
        ("place_water", ARM, "place_at_water_station", {"beaker_uuid": beaker_uuid}),
        ("add_water", WATER_STATION, "dispense_water", {"beaker_uuid": beaker_uuid, "volume_ml": water_ml}),
        ("take_water", ARM, "take_from_water_station", {"beaker_uuid": beaker_uuid}),
        ("place_salt", ARM, "place_at_salt_station", {"beaker_uuid": beaker_uuid}),
        ("add_salt", SALT_STATION, "dispense_salt", {"beaker_uuid": beaker_uuid, "mass_g": salt_g}),
        ("take_salt", ARM, "take_from_salt_station", {"beaker_uuid": beaker_uuid}),
        ("place_mixer", ARM, "place_at_mixer", {"beaker_uuid": beaker_uuid}),
        ("stir", MIXER, "stir", {"beaker_uuid": beaker_uuid, "duration_s": 60}),
        ("take_mixer", ARM, "take_from_mixer", {"beaker_uuid": beaker_uuid}),
        ("place_stack", ARM, "place_at_stack", {"beaker_uuid": beaker_uuid}),
        ("store", STACK, "place_on_stack", {"beaker_uuid": beaker_uuid, "slot_id": slot_id}),
    ]
    custody = {
        "warehouse": {"location_kind": "warehouse"},
        "held": {"location_kind": "held", "holder_kind": "device", "holder_uuid": ARM},
        "water": {"location_kind": "station", "location_uuid": WATER_STATION, "holder_kind": "station", "holder_uuid": WATER_STATION},
        "salt": {"location_kind": "station", "location_uuid": SALT_STATION, "holder_kind": "station", "holder_uuid": SALT_STATION},
        "mixer": {"location_kind": "station", "location_uuid": MIXER, "holder_kind": "station", "holder_uuid": MIXER},
        "stack": {"location_kind": "stack", "location_uuid": "stack-A"},
    }
    transitions = {
        "pick": ("warehouse", "held"),
        "place_water": ("held", "water"),
        "add_water": ("water", "water"),
        "take_water": ("water", "held"),
        "place_salt": ("held", "salt"),
        "add_salt": ("salt", "salt"),
        "take_salt": ("salt", "held"),
        "place_mixer": ("held", "mixer"),
        "stir": ("mixer", "mixer"),
        "take_mixer": ("mixer", "held"),
        "place_stack": ("held", "stack"),
    }
    nodes = []
    for node_id, device_id, action_name, params in node_data:
        requirements = []
        if node_id == "pick":
            requirements = [MaterialRequirement(instance_uuid=beaker_uuid)]
        elif node_id == "add_water":
            requirements = [MaterialRequirement(template_id="water", quantity=water_ml, unit="mL")]
        elif node_id == "add_salt":
            requirements = [MaterialRequirement(template_id="sodium-chloride", quantity=salt_g, unit="g")]
        outputs = []
        if node_id == "store":
            outputs = [{
                "output_name": "brine",
                # 输出沿用同一个烧杯实例，内容物发生变化，而不是凭空创建第二个烧杯。
                "edge_uuid": beaker_uuid,
                "template_id": "beaker",
                "parent_uuid": "stack-A",
                "slot_id": slot_id,
            }]
        preconditions = []
        effects = []
        if node_id in transitions:
            before, after = transitions[node_id]
            preconditions = [{"material_uuid": beaker_uuid, **custody[before]}]
            effects = [{
                "material_uuid": beaker_uuid,
                **custody[after],
                "expected_location_kind": custody[before]["location_kind"],
                "expected_location_uuid": custody[before].get("location_uuid", ""),
                "expected_holder_kind": custody[before].get("holder_kind", ""),
                "expected_holder_uuid": custody[before].get("holder_uuid", ""),
            }]
        elif node_id == "store":
            preconditions = [{"material_uuid": beaker_uuid, **custody["stack"]}]
        nodes.append(
            WorkflowNode(
                id=node_id,
                device_id=device_id,
                action_name=action_name,
                action_type="goal",
                param=params,
                material_requirements=requirements,
                material_outputs=outputs,
                material_preconditions=preconditions,
                material_effects=effects,
            )
        )
    edges = [
        WorkflowEdge(
            uuid=f"{run_id}-{left}->{right}",
            source_node_id=left,
            target_node_id=right,
        )
        for (left, *_), (right, *_) in zip(node_data, node_data[1:])
    ]
    return WorkflowSpec(
        workflow_id=f"workflow-pipeline-{index}",
        run_id=run_id,
        task_id="task-parallel-pipeline",
        priority=100 - index,
        nodes=nodes,
        edges=edges,
    )


def find_job(scheduler: EdgeScheduler, *, device_id: str | None = None, node_id: str | None = None) -> str:
    for job_id, job in scheduler.snapshot()["inflight_jobs"].items():
        if device_id is not None and job["device_id"] != device_id:
            continue
        if node_id is not None and job["node_id"] != node_id:
            continue
        return job_id
    raise AssertionError(f"inflight job not found: device={device_id}, node={node_id}")


def finish(scheduler: EdgeScheduler, job_id: str, ret_value: dict | None = None) -> None:
    job = scheduler.snapshot()["inflight_jobs"][job_id]
    scheduler.on_job_finished(
        job_id, True, ret_value or {"ok": True}, run_id=job["run_id"]
    )


def print_dispatches(label: str, dispatcher: RecordingDispatcher, start: int) -> int:
    new_items = dispatcher.dispatched[start:]
    if new_items:
        print(label)
        print(json.dumps([
            {
                "run_id": item["run_id"],
                "action": item["action"],
                "device": item["device_id"],
                "params": item["action_args"],
            }
            for item in new_items
        ], ensure_ascii=False, indent=2))
    return len(dispatcher.dispatched)


def main() -> None:
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot(template_id="water", lot_id="water-lot", quantity=300, unit="mL")
    inventory.inbound_lot(template_id="sodium-chloride", lot_id="salt-lot", quantity=30, unit="g")
    inventory.register_instance(template_id="stack", edge_uuid="stack-A")
    for index in (1, 2, 3):
        inventory.register_instance(template_id="beaker", edge_uuid=f"beaker-{index}")

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    runs = [build_run(1, 20, 2), build_run(2, 30, 3), build_run(3, 40, 4)]
    scheduler.submit_workflow_runs(runs, task_id="task-parallel-pipeline")
    cursor = print_dispatches("首轮：机械臂取第一个烧杯；随后放到加水位", dispatcher, 0)
    # 逐个完成“当前最短/已结束”的动作，模拟设备回调。每次回调都会触发
    # Scheduler 重排；非机械臂工位运行时，机械臂动作会立即补进来。
    round_index = 0
    while scheduler.snapshot()["inflight_jobs"]:
        jobs = scheduler.snapshot()["inflight_jobs"]
        # 优先完成非机械臂工位动作，制造“工位工作、机械臂服务其他 Run”的重叠。
        chosen = next(
            (job_id for job_id, job in jobs.items() if job["device_id"] != ARM),
            next(iter(jobs)),
        )
        selected_job = jobs[chosen]
        output = None
        if selected_job["action_name"] == "place_on_stack":
            index = int(selected_job["run_id"].rsplit("-", 1)[-1])
            water_ml, salt_g = {1: (20, 2), 2: (30, 3), 3: (40, 4)}[index]
            output = {
                "material_outputs": [{
                    "output_name": "brine",
                    "content": {
                        "quantity": water_ml,
                        "unit": "mL",
                        "components": [
                            {"template_id": "water", "quantity": water_ml, "unit": "mL"},
                            {"template_id": "sodium-chloride", "quantity": salt_g, "unit": "g"},
                        ],
                    },
                }]
            }
        finish(scheduler, chosen, output)
        round_index += 1
        cursor = print_dispatches(
            f"第 {round_index} 轮回调后（工位与机械臂分别占用）",
            dispatcher,
            cursor,
        )

    products = []
    for index in (1, 2, 3):
        beaker_uuid = f"beaker-{index}"
        instance = inventory.store.get_instance(beaker_uuid)
        relation = inventory.store.get_relation(beaker_uuid)
        content = inventory.store.get_content(beaker_uuid)
        products.append({
            "run_id": f"run-pipeline-{index}",
            "beaker_uuid": beaker_uuid,
            "status": instance["status"],
            "stack": relation,
            "content": json.loads(content["state_json"]),
        })
    print("最终物料流转结果")
    print(json.dumps(products, ensure_ascii=False, indent=2))
    print("输入剩余", {
        "water_ml": inventory.store.get_lot("water-lot")["quantity_total"],
        "salt_g": inventory.store.get_lot("salt-lot")["quantity_total"],
    })


if __name__ == "__main__":
    main()
