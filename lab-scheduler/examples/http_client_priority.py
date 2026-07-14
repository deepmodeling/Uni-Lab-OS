"""Example: posting a ScheduleRequest with Priority strings via HTTP."""

import json
import httpx


def main():
    payload = {
        "lab_id": "design-lab-001",
        "algorithm": "Batch_WeightedCriticalPath",
        "tasks": [
            {
                "task_id": "urgent_workflow",
                "priority": "urgent",
                "submitted_at": "2026-05-18T08:00:00Z",
                "steps": [
                    {"step_id": "s1", "machine_type": "PCR", "duration": 30}
                ],
            }
        ],
        "resources": {
            "machines": [{"type": "PCR", "count": 2, "batch_capacity": 2}]
        },
    }

    r = httpx.post("http://localhost:8090/api/v1/schedule", json=payload, timeout=30)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
