"""Example: directly using scheduler as a Python library.

This mirrors how the lab equipment design tool would consume the optimizer
without going through HTTP.
"""

from datetime import datetime, timezone

from scheduler import (
    Machine, Priority, Resources, ScheduleRequest, Step, Task,
    SchedulerService, list_algorithms,
)


def main():
    print("Available algorithms:", list_algorithms())

    req = ScheduleRequest(
        lab_id="design-lab-001",
        tasks=[
            Task(
                task_id="ELISA_run_1",
                priority=Priority.high,
                submitted_at=datetime.now(timezone.utc),
                steps=[
                    Step(step_id="prepare", machine_type="Denso_Arm", duration=5),
                    Step(step_id="incubate", machine_type="Incubation_Carrier", duration=30),
                    Step(step_id="read", machine_type="Reader", duration=10),
                ],
                dependencies=[("prepare", "incubate"), ("incubate", "read")],
            ),
            Task(
                task_id="background_qc",
                priority=Priority.low,
                steps=[Step(step_id="qc1", machine_type="Reader", duration=5)],
            ),
        ],
        resources=Resources(machines=[
            Machine(type="Denso_Arm", count=2),
            Machine(type="Incubation_Carrier", count=2, batch_capacity=4),
            Machine(type="Reader", count=1),
        ]),
        algorithm="GA",
    )

    service = SchedulerService()
    resp = service.schedule(req)

    print(f"\nObjective: {resp.objective.total_makespan} min makespan")
    print(f"Weighted cost: {resp.objective.priority_weighted_cost:.1f}")
    print(f"\nScheduled {len(resp.schedule)} entries:")
    for s in resp.schedule[:5]:
        if hasattr(s, "step_id"):
            print(f"  {s.task_id}.{s.step_id}: [{s.start}-{s.end}] on {s.resource}")
        else:
            print(f"  transfer {s.transfer_id}: {s.path} [{s.start}-{s.end}]")
    if len(resp.schedule) > 5:
        print(f"  ... and {len(resp.schedule) - 5} more")


if __name__ == "__main__":
    main()
