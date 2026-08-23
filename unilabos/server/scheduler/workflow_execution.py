"""Canonical WorkflowTask -> TaskDagRunner -> JobExecutionBackend adapter.

WorkflowStore owns task/job facts.  TaskDagRunner owns only dependency walking,
and JobExecutionBackend owns device locking plus HostNode side effects.  The
adapter persists every terminal job before releasing its DAG successors.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional
from uuid import uuid5, UUID

from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
)
from unilabos.server.scheduler.dispatch import build_job_start_payload
from unilabos.server.scheduler.param_resolver import (
    ParamResolveError,
    json_get_exists,
    json_set,
)
from unilabos.server.scheduler.dag.dag_executor import DagWalk
from unilabos.server.scheduler.dag.dag_model import DagEdge, DagNode, NodeState, TaskDag
from unilabos.server.scheduler.dag.task_dag_runner import TaskDagRunner
from unilabos.server.workflow.service import WorkflowService

logger = logging.getLogger(__name__)

_JOB_TERMINAL = {"succeeded", "failed", "skipped", "canceled", "timeout"}


class WorkflowExecutionError(RuntimeError):
    """A persisted execution plan cannot be mapped to the local executor."""


class WorkflowTaskExecutor:
    """Run canonical tasks on one background asyncio loop."""

    def __init__(
        self,
        service: WorkflowService,
        backend: Any,
        *,
        materials_gateway: Any = None,
    ) -> None:
        self.service = service
        self.backend = backend
        self.materials_gateway = materials_gateway
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._guard = threading.RLock()
        self._runners: Dict[str, TaskDagRunner] = {}
        self._scheduled: set[str] = set()
        self._job_to_task: Dict[str, str] = {}
        self._job_specs: Dict[str, Dict[str, Any]] = {}
        self.backend.add_job_finished_listener(self._on_backend_finished)

    def start(self, *, recover: bool = True) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="WorkflowTaskExecutor",
                daemon=True,
            )
            self._thread.start()
        if not self._started.wait(timeout=5):
            raise RuntimeError("workflow task executor event loop did not start")
        if recover:
            for task in self.service.list_recoverable_workflow_tasks():
                self.submit(str(task["uuid"]))

    def submit(self, task_uuid: str) -> None:
        """Queue a persisted task; duplicate submissions share one active runner."""

        self.start(recover=False)
        assert self._loop is not None
        with self._guard:
            if task_uuid in self._runners or task_uuid in self._scheduled:
                return
            self._scheduled.add(task_uuid)
        future = asyncio.run_coroutine_threadsafe(self.run_task(task_uuid), self._loop)

        def report(done: Any) -> None:
            with self._guard:
                self._scheduled.discard(task_uuid)
            try:
                done.result()
            except Exception:  # noqa: BLE001 - task state is persisted by run_task
                logger.exception("workflow task %s execution failed", task_uuid)

        future.add_done_callback(report)

    async def run_task(self, task_uuid: str) -> Dict[str, NodeState]:
        prepared = self.service.prepare_workflow_task_execution(task_uuid)
        if prepared["state"] != "ready":
            return {}
        task = prepared["task"]
        jobs = prepared["jobs"]
        try:
            dag, specs = self._build_dag(task, jobs)
            self._reserve_task_inventory(task, specs)
        except Exception as exc:
            self._fail_unstarted_task(task_uuid, jobs, exc)
            self._release_unconsumed_task_inventory(task_uuid)
            raise

        completed = [
            str(job["uuid"])
            for job in jobs
            if job["status"] in {"succeeded", "skipped"}
        ]
        walk = DagWalk(dag, completed=completed)
        runner = TaskDagRunner(
            dag,
            lambda node: self._start_node(task, node),
            on_node_terminal=self._on_node_terminal,
            on_cancel_remaining=lambda: self.backend.cancel_task(task_uuid),
            loop=asyncio.get_running_loop(),
            walk=walk,
        )
        with self._guard:
            if task_uuid in self._runners:
                return {}
            self._runners[task_uuid] = runner
            self._job_specs.update(specs)
            for job_id in specs:
                self._job_to_task[job_id] = task_uuid
        try:
            result = await runner.run()
            for job_id, state in result.items():
                self._persist_terminal_if_needed(job_id, state)
            task_status = (
                "succeeded"
                if result and all(state == NodeState.SUCCESS for state in result.values())
                else (
                    "failed"
                    if any(state == NodeState.FAILED for state in result.values())
                    else "canceled"
                )
            )
            output = {
                spec["workflow_node_uuid"]: self.service.get_workflow_node_job(
                    job_id
                ).get("return_info", {})
                for job_id, spec in specs.items()
                if self.service.get_workflow_node_job(job_id)["status"]
                in {"succeeded", "skipped"}
            }
            self.service.finish_workflow_task(
                task_uuid,
                status=task_status,
                output=output,
                error_info=(
                    []
                    if task_status == "succeeded"
                    else [{"code": "node_execution_failed"}]
                ),
            )
            return result
        finally:
            self._release_unconsumed_task_inventory(task_uuid)
            with self._guard:
                self._runners.pop(task_uuid, None)
                for job_id in specs:
                    self._job_to_task.pop(job_id, None)
                    self._job_specs.pop(job_id, None)

    def stop(self) -> None:
        with self._guard:
            runners = list(self._runners.values())
            loop = self._loop
        for runner in runners:
            runner.cancel()
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        remove = getattr(self.backend, "remove_job_finished_listener", None)
        if callable(remove):
            remove(self._on_backend_finished)
        self._thread = None
        self._loop = None

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._started.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def _build_dag(
        self,
        task: Dict[str, Any],
        jobs: list[Dict[str, Any]],
    ) -> tuple[TaskDag, Dict[str, Dict[str, Any]]]:
        plan = task.get("execution_plan") or {}
        snapshot = task.get("workflow_snapshot") or {}
        snapshot_nodes = {
            str(node["uuid"]): node for node in snapshot.get("nodes", [])
        }
        planned_nodes = {
            str(node["uuid"]): node for node in plan.get("nodes", [])
        }
        jobs_by_node = {str(job["workflow_node_uuid"]): job for job in jobs}
        dag_nodes: Dict[str, DagNode] = {}
        specs: Dict[str, Dict[str, Any]] = {}
        for workflow_node_uuid, job in jobs_by_node.items():
            planned = planned_nodes.get(workflow_node_uuid, {})
            source = snapshot_nodes.get(workflow_node_uuid, {})
            if job["executor_kind"] != "device_action":
                raise WorkflowExecutionError(
                    f"executor_kind {job['executor_kind']!r} is not wired locally"
                )
            param = dict(job.get("param") or planned.get("param") or {})
            source_meta = dict(source.get("meta_data") or {})
            device_id = str(
                source_meta.get("target_device_id")
                or job.get("material_uuid")
                or planned.get("material_uuid")
                or source.get("material_uuid")
                or param.get("device_id")
                or ""
            )
            action = str(source.get("action_name") or param.get("action") or "")
            if not device_id or not action:
                raise WorkflowExecutionError(
                    f"workflow node {workflow_node_uuid} lacks material_uuid/device action"
                )
            job_id = str(job["uuid"])
            dag_nodes[job_id] = DagNode(
                node_id=job_id,
                device_id=device_id,
                action=action,
                action_type=str(source.get("action_type") or ""),
                action_args=param,
                always_free=bool((job.get("execution_policy") or {}).get("always_free")),
            )
            specs[job_id] = {
                "workflow_node_uuid": workflow_node_uuid,
                "base_param": param,
                "edges": list(plan.get("edges") or []),
                "jobs_by_node": {
                    node_uuid: str(node_job["uuid"])
                    for node_uuid, node_job in jobs_by_node.items()
                },
                "inventory_requirements": list(
                    planned.get("inventory_requirements") or []
                ),
            }

        dag_edges = []
        for edge in plan.get("edges") or []:
            source_job = jobs_by_node.get(str(edge["source_node_uuid"]))
            target_job = jobs_by_node.get(str(edge["target_node_uuid"]))
            if source_job is None or target_job is None:
                continue
            dag_edges.append(
                DagEdge(
                    source_node_uuid=str(source_job["uuid"]),
                    target_node_uuid=str(target_job["uuid"]),
                )
            )
        return (
            TaskDag(
                task_id=str(task["uuid"]),
                notebook_id="",
                server_info={},
                nodes=dag_nodes,
                edges=dag_edges,
            ),
            specs,
        )

    def _start_node(self, task: Dict[str, Any], node: DagNode) -> None:
        args = self._resolve_action_args(node.node_id)
        self.service.mark_workflow_node_job_running(node.node_id)
        payload = build_job_start_payload(
            job_id=node.node_id,
            task_id=str(task["uuid"]),
            workflow_id=str(task.get("workflow_uuid") or ""),
            node_id=self._job_specs[node.node_id]["workflow_node_uuid"],
            device_id=node.device_id,
            action_name=node.action,
            action_type=node.action_type,
            action_args=args,
            inventory_requirements=self._job_specs[node.node_id][
                "inventory_requirements"
            ],
            inventory_reservation_uuid=self._job_specs[node.node_id].get(
                "inventory_reservation_uuid"
            ),
        )
        payload["always_free"] = node.always_free
        self.backend.dispatch(payload)

    @staticmethod
    def _inventory_command_uuid(task_uuid: str) -> str:
        try:
            namespace = UUID(task_uuid)
        except ValueError:
            namespace = UUID("4f632a8d-f5cc-41e5-9471-f37c79dad537")
        return str(uuid5(namespace, f"inventory:{task_uuid}"))

    def _reserve_task_inventory(
        self,
        task: Dict[str, Any],
        specs: Dict[str, Dict[str, Any]],
    ) -> None:
        requests = [
            InventoryReservationCreate(
                task_uuid=str(task["uuid"]),
                node_uuid=str(spec["workflow_node_uuid"]),
                job_uuid=job_uuid,
                scheduler_revision=0,
                requirements=spec["inventory_requirements"],
            )
            for job_uuid, spec in specs.items()
            if spec["inventory_requirements"]
        ]
        if not requests:
            return
        if self.materials_gateway is None:
            raise WorkflowExecutionError(
                "workflow declares inventory requirements but materials authority "
                "is unavailable"
            )
        task_uuid = str(task["uuid"])
        value = InventoryTaskReservationCreate(
            task_uuid=task_uuid,
            scheduler_revision=0,
            reservations=requests,
        )
        mutation = InventoryMutation(
            command_uuid=self._inventory_command_uuid(task_uuid),
            effect_key="inventory.task.reserve",
            operation="reserve_task_inventory",
            actor_type="scheduler",
        )
        result = self.materials_gateway.reserve_task_inventory(mutation, value)
        for reservation in result.data.reservations:
            spec = specs.get(reservation.job_uuid)
            if spec is not None:
                spec["inventory_reservation_uuid"] = (
                    reservation.reservation_uuid
                )

    def _release_unconsumed_task_inventory(self, task_uuid: str) -> None:
        if self.materials_gateway is None:
            return
        try:
            reservations = self.materials_gateway.list_inventory_reservations(
                task_uuid=task_uuid,
                status="active",
            )
        except Exception:  # noqa: BLE001 - task result must remain persisted
            logger.exception(
                "failed to list active inventory reservations for task %s",
                task_uuid,
            )
            return
        command_uuid = self._inventory_command_uuid(task_uuid)
        for reservation in reservations:
            try:
                value = InventoryReservationTransition(
                    reservation_uuid=reservation.reservation_uuid,
                    reason="workflow_terminal",
                )
                mutation = InventoryMutation(
                    command_uuid=command_uuid,
                    effect_key=(
                        f"inventory.release:{reservation.reservation_uuid}"
                    ),
                    operation="release_inventory_reservation",
                    actor_type="scheduler",
                    job_uuid=reservation.job_uuid,
                )
                self.materials_gateway.release_inventory_reservation(
                    mutation,
                    value,
                )
            except Exception:  # noqa: BLE001 - ledger can be reconciled and retried
                logger.exception(
                    "failed to release inventory reservation %s",
                    reservation.reservation_uuid,
                )

    def _resolve_action_args(self, job_id: str) -> Dict[str, Any]:
        spec = self._job_specs[job_id]
        target_node = spec["workflow_node_uuid"]
        result: Any = dict(spec["base_param"])
        for edge in spec["edges"]:
            if str(edge.get("target_node_uuid")) != target_node:
                continue
            if edge.get("dependency_only"):
                continue
            source_key = str(edge.get("source_data_key") or "")
            target_key = str(edge.get("target_data_key") or "")
            if not source_key or not target_key:
                continue
            source_job_id = spec["jobs_by_node"].get(
                str(edge.get("source_node_uuid"))
            )
            if not source_job_id:
                raise ParamResolveError("source workflow job is missing")
            source_job = self.service.get_workflow_node_job(source_job_id)
            value: Any = (source_job.get("return_info") or {}).get("return_value")
            exists, value = json_get_exists(value, source_key)
            if not exists:
                raise ParamResolveError(
                    f"value not exist: source data_key {source_key!r}"
                )
            keys = target_key.split("@@@")
            for nested in keys[:-1]:
                exists, value = json_get_exists(value, nested)
                if not exists:
                    raise ParamResolveError(
                        f"value not exist: nested target key {nested!r}"
                    )
            result = json_set(result, keys[-1], value)
        return dict(result)

    def _on_backend_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str = "normal",
    ) -> None:
        with self._guard:
            task_uuid = self._job_to_task.get(job_id)
            runner = self._runners.get(task_uuid or "")
        if task_uuid is None or runner is None:
            return
        job_status = "skipped" if success and suc_type == "skip" else (
            "succeeded" if success else "failed"
        )
        self.service.record_workflow_node_job_terminal(
            job_id,
            status=job_status,
            return_info={
                "suc": success,
                "suc_type": suc_type,
                "return_value": ret_value,
            },
            error_info=[] if success else [{"code": "action_failed"}],
        )
        runner.notify_terminal(
            job_id,
            NodeState.SUCCESS if success else NodeState.FAILED,
        )

    def _on_node_terminal(self, job_id: str, state: NodeState) -> None:
        self._persist_terminal_if_needed(job_id, state)

    def _persist_terminal_if_needed(self, job_id: str, state: NodeState) -> None:
        job = self.service.get_workflow_node_job(job_id)
        if job["status"] in _JOB_TERMINAL:
            return
        status = {
            NodeState.SUCCESS: "succeeded",
            NodeState.FAILED: "failed",
            NodeState.CANCELLED: "canceled",
        }.get(state)
        if status is None:
            return
        self.service.record_workflow_node_job_terminal(
            job_id,
            status=status,
            return_info={},
            error_info=([] if status == "succeeded" else [{"code": status}]),
        )

    def _fail_unstarted_task(
        self,
        task_uuid: str,
        jobs: list[Dict[str, Any]],
        error: Exception,
    ) -> None:
        for job in jobs:
            if job["status"] not in _JOB_TERMINAL:
                self.service.record_workflow_node_job_terminal(
                    str(job["uuid"]),
                    status="canceled",
                    error_info=[{"code": "plan_not_executable"}],
                )
        self.service.finish_workflow_task(
            task_uuid,
            status="failed",
            error_info=[
                {"code": "plan_not_executable", "message": str(error)}
            ],
        )


__all__ = ["WorkflowExecutionError", "WorkflowTaskExecutor"]
