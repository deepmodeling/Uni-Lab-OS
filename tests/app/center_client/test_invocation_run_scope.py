"""同一公开流程跨运行隔离；同一调用反复提交不追加Task或首批Job。"""
from copy import deepcopy
from uuid import uuid4

from tests.app.center_client.test_atomic_workflow_events import RecordingScheduler, _workflow
from tests.app.center_client.test_center_client import INSTANCE, STATION
from unilabos.app.center_client.client import TaskPackage
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import submission
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def test_same_invocation_key_in_two_global_tasks_has_distinct_permanent_execution(tmp_path):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, first, scheduler):
        payload = deepcopy(first.raw)
        payload.update(global_task_uuid=str(uuid4()), station_invocation_uuid=str(uuid4()))
        second = TaskPackage.from_response({"payload": payload, "payload_digest": digest(payload)})
        durable.remember_package(second)
        durable.confirm_claim(second.station_invocation_uuid)
        assert first.raw["invocation_key"] == second.raw["invocation_key"]
        assert first.global_task_uuid != second.global_task_uuid
        packages = [first, second]
        tasks = [service.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
                 for package in packages]
        identities = [task["uuid"] for task in tasks]
        assert len(set(identities)) == 2
        jobs = [{job["uuid"] for job in store.list_jobs(identity)} for identity in identities]
        assert all(jobs) and jobs[0].isdisjoint(jobs[1])
        assert all(sum(job["executor_kind"] == "device_action" for job in store.list_jobs(identity)) == 1 for identity in identities)
        assert scheduler.submissions == identities
    store = WorkflowStore(tmp_path / "workflow_history.db")
    scheduler = RecordingScheduler(store)
    service = WorkflowService(store, task_scheduler_bridge=scheduler)
    durable = DurableCenterStore(tmp_path / "workflow_history.db", station_uuid=STATION, instance_uuid=INSTANCE)
    try:
        for _ in range(100):
            for index, package in enumerate(packages):
                task = service.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
                assert task["uuid"] == identities[index]
                assert {job["uuid"] for job in store.list_jobs(task["uuid"])} == jobs[index]
        assert store.db.execute("SELECT count(*) FROM workflow_task").fetchone()[0] == 2
        assert scheduler.submissions == []
        events = [event for _, event in durable.events()]
        assert {event["station_invocation_uuid"] for event in events} == {package.station_invocation_uuid for package in packages}
        assert [event["event_sequence"] for event in events] == [1, 2]
    finally:
        durable.close()
        service.close()
