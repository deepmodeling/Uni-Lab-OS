"""工作流数量型库存绑定、预留投影与可恢复消费。

本模块复用工作流库既有 ``workflow_inventory_*`` 表和库存库既有
``reagent``、``current_substance``、``inventory_ledger``、``sync_outbox``。
两个 SQLite 事务域之间不伪装原子事务：先持久化可重放意图，再以稳定事件身份
更新库存，最后收敛工作流分配状态。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from functools import wraps
from threading import RLock
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityInventoryAuthority,
    WorkflowQuantityReservationError,
    WorkflowQuantityShortageError,
    convert_quantity,
)
from unilabos.workflow.store import (
    StoreAdmissionRejected,
    StoreConflict,
    StoreNotFound,
    WorkflowStore,
    utc_now,
)
from unilabos.workflow.task_input import PreparedTaskInput


def _json(value: Any) -> str:
    """编码稳定紧凑 JSON；参数必须是可编码业务值，返回 UTF-8 文本。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any, fallback: Any) -> Any:
    """解码持久 JSON；非法历史值返回调用方提供的安全默认值。"""

    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _uuid(value: Any, *, field: str) -> str:
    """规范非空 UUID；参数 ``field`` 用于稳定错误说明。"""

    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as error:
        raise StoreConflict(f"{field} 不是合法 UUID") from error
    if parsed.int == 0:
        raise StoreConflict(f"{field} 不能是 nil UUID")
    return str(parsed)


def _positive(value: Any, *, field: str) -> float:
    """规范有限正数；零、负数、NaN 与无穷值均关闭式拒绝。"""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise StoreConflict(f"{field} 必须是正数") from error
    if not math.isfinite(number) or number <= 0:
        raise StoreConflict(f"{field} 必须是正数")
    return number


def _non_negative(value: Any, *, field: str) -> float:
    """规范有限非负数；用于设备上报的实际消耗量。"""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise StoreConflict(f"{field} 必须是非负数") from error
    if not math.isfinite(number) or number < 0:
        raise StoreConflict(f"{field} 必须是非负数")
    return number


def _quantity_equal(left: float, right: float) -> bool:
    """按 Backend 相同的相对容差比较两个库存数量。"""

    tolerance = max(1.0, abs(left), abs(right)) * 1e-9
    return abs(left - right) <= tolerance


_Result = TypeVar("_Result")


def _serialized_protocol(method: Callable[..., _Result]) -> Callable[..., _Result]:
    """串行执行同一协调器的跨库协议，禁止更正与旧结果争用同一消费事件。"""

    @wraps(method)
    def serialized(self: "WorkflowQuantityInventory", *args: Any, **kwargs: Any) -> _Result:
        with self._settlement_lock:
            return method(self, *args, **kwargs)

    return serialized


class WorkflowQuantityInventory:
    """隐藏工作流库与库存库之间的数量型库存协议。

    工作流库保存逻辑需求、Task 分配与 Saga；库存库保存实际余量和不可变台账。
    该对象只复用调度器已经持有的 ``InventoryService``，不会创建第二库存权威。
    """

    def __init__(self, workflow_store: WorkflowStore, inventory_service: Any) -> None:
        """绑定两个既有写权威。

        参数：``workflow_store`` 是任务与分配写模型；``inventory_service`` 必须是
        调度器持有且公开 ``store/edge_id/lab_id`` 的库存权威。返回无。异常：组合
        未提供可事务写入的库存库时抛 ``TypeError``，禁止降级成不校验数量。
        """

        inventory_store = getattr(inventory_service, "store", None)
        if not isinstance(inventory_store, InventoryStore):
            raise TypeError("数量型工作流库存必须复用调度器的 InventoryStore")
        self._workflow_store = workflow_store
        self._settlement_lock = RLock()
        self._authority = WorkflowQuantityInventoryAuthority(
            inventory_store,
            edge_id=str(getattr(inventory_service, "edge_id", "edge-default")),
            lab_id=str(getattr(inventory_service, "lab_id", "edge-lab")),
        )

    def prepare_task_allocations(
        self,
        connection: sqlite3.Connection,
        *,
        graph: Mapping[str, Any],
        prepared: PreparedTaskInput,
        task_uuid: str,
        bindings: Sequence[Mapping[str, Any]],
        defer_inventory_reservation: bool = False,
    ) -> list[dict[str, Any]]:
        """在任务创建事务内校验显式绑定并构造分配行。

        参数：``connection`` 是已取得工作流写锁的事务；``graph/prepared`` 来自
        同一图快照；``task_uuid`` 是本次运行身份；``bindings`` 是 HTTP
        ``inventory_bindings``。返回：可与 Task/Jobs 同事务插入的分配列表。
        异常：需求缺绑定、拆分/类型/单位/化学身份不匹配、库存缺失或可用量不足
        时抛 ``StoreConflict``，调用方回滚任务与全部作业。
        """

        try:
            allocations = self._build_task_allocations(
                graph=graph, prepared=prepared, task_uuid=task_uuid, bindings=bindings,
            )
        except StoreConflict as error:
            raise StoreAdmissionRejected(str(error)) from error
        if not defer_inventory_reservation:
            try:
                self._authority.reserve_task(task_uuid, allocations)
            except WorkflowQuantityShortageError as error:
                raise StoreAdmissionRejected(str(error)) from error
            except WorkflowQuantityReservationError as error:
                raise StoreConflict(str(error)) from error
        return allocations

    def task_allocations(self, task_uuid: str) -> list[dict[str, Any]]:
        """返回工作流事务已冻结、尚待库存统一准入的任务数量分配。"""

        task = self._workflow_store.get_task(task_uuid)
        snapshot = task.get("workflow_snapshot")
        requirements = (
            snapshot.get("inventory_requirements", [])
            if isinstance(snapshot, Mapping)
            else []
        )
        source_by_key = {
            str(requirement.get("requirement_key") or ""): str(
                unilab.get("material_source_node_uuid") or ""
            )
            for requirement in requirements
            if isinstance(requirement, Mapping)
            and isinstance(requirement.get("meta_data"), Mapping)
            and isinstance(
                unilab := requirement["meta_data"].get("unilab"),
                Mapping,
            )
        }
        with self._workflow_store.transaction() as connection:
            allocations = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM workflow_inventory_allocation "
                    "WHERE workflow_task_uuid=? ORDER BY workflow_node_job_uuid,uuid",
                    (task_uuid,),
                ).fetchall()
            ]
        for allocation in allocations:
            source_uuid = source_by_key.get(str(allocation["requirement_key"]), "")
            if source_uuid:
                allocation["material_source_node_uuid"] = source_uuid
        return allocations

    def prepare_runtime_source_allocations(
        self, task: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]],
        source_uuid: str, reservation: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """库存事务内按本次已选容器解析数量需求；不提前读取备用分支。"""
        plan = deepcopy(task["execution_plan"])
        selected = reservation["allocations"][source_uuid][0]
        for node in plan["nodes"]:
            if node["uuid"] == source_uuid:
                node["material_requirements"] = [{"instance_uuid": selected}]
        snapshot = deepcopy(task.get("workflow_snapshot") or {})
        prepared = PreparedTaskInput(snapshot, {}, plan, [dict(job) for job in jobs])
        return self._build_task_allocations(graph=snapshot, prepared=prepared,
            task_uuid=str(task["uuid"]), bindings=(), runtime_source_uuid=source_uuid)

    def persist_runtime_allocations(self, allocations: Sequence[Mapping[str, Any]]) -> None:
        """复用既有数量分配表保存库存权威已提交的预留身份。"""
        with self._workflow_store.transaction() as connection:
            for item in allocations:
                fields = ("uuid", "workflow_task_uuid", "workflow_node_job_uuid", "requirement_key",
                          "inventory_type", "inventory_uuid", "material_uuid", "reserved_quantity", "quantity_unit")
                existing = connection.execute("SELECT * FROM workflow_inventory_allocation WHERE uuid=?", (item["uuid"],)).fetchone()
                if existing is not None:
                    if any(existing[key] != item[key] for key in fields):
                        raise StoreConflict("运行时数量分配重放不一致")
                    continue
                connection.execute(
                    "INSERT INTO workflow_inventory_allocation (" + ",".join(fields) + ",status,revision,reserved_at) VALUES (?,?,?,?,?,?,?,?,?,'reserved',1,?)",
                    (*[item[key] for key in fields], utc_now()),
                )

    @_serialized_protocol
    def reserve_deferred_runtime_requirements(
        self, *, task_uuid: str, job_uuid: str
    ) -> list[dict[str, Any]]:
        """为消费作业补做“来源缺料后由补料分支提供容器”的数量预留。

        运行时来源检查缺料时不产生分配（``sufficient=false``），补料分支补液或
        换瓶后，消费动作使用的是实际执行分支提供的容器。本方法在消费作业派发前
        或结算前调用：对该作业声明、但本任务尚无任何分配的运行时来源需求，按
        实际执行分支提供的容器重新检查并在库存权威内预留，然后持久化分配投影。

        参数：任务与作业身份。返回：本次新建的分配（无待补需求时为空）。异常：
        来源未成功执行、补料分支无法唯一确定容器、容器余量不足或单位不可换算时
        抛 ``StoreConflict``，调用方不得把作业当作无数量需求静默结算。同一作业
        重放幂等：需求已有分配后不再重复预留。
        """

        normalized_task = _uuid(task_uuid, field="workflow_task_uuid")
        normalized_job = _uuid(job_uuid, field="workflow_node_job_uuid")
        task = self._workflow_store.get_task(normalized_task)
        plan = task.get("execution_plan")
        snapshot = task.get("workflow_snapshot")
        if not isinstance(plan, Mapping) or not isinstance(snapshot, Mapping):
            return []
        plan_nodes = {
            str(node.get("uuid")): node
            for node in plan.get("nodes", [])
            if isinstance(node, Mapping)
        }
        runtime_sources = {
            node_uuid
            for node_uuid, node in plan_nodes.items()
            if node.get("kind") == "material_source" and node.get("runtime_material_source")
        }
        if not runtime_sources:
            return []
        job = self._workflow_store.get_job(normalized_job)
        if str(job.get("workflow_task_uuid")) != normalized_task:
            raise StoreConflict("库存消费所属 Task 或 Job 不存在")
        node_uuid = str(job.get("workflow_node_uuid") or "")
        requirements = [
            dict(requirement)
            for requirement in snapshot.get("inventory_requirements", [])
            if isinstance(requirement, Mapping)
            and str(requirement.get("consume_node_uuid")) == node_uuid
            and self._requirement_source(requirement) in runtime_sources
        ]
        if not requirements:
            return []
        with self._workflow_store.transaction() as connection:
            allocated_keys = {
                str(row["requirement_key"])
                for row in connection.execute(
                    "SELECT requirement_key FROM workflow_inventory_allocation "
                    "WHERE workflow_task_uuid=?",
                    (normalized_task,),
                ).fetchall()
            }
        pending = [
            requirement for requirement in requirements
            if str(requirement.get("requirement_key")) not in allocated_keys
        ]
        if not pending:
            return []
        jobs = self._workflow_store.list_jobs(normalized_task)
        # 同一节点多次 attempt 时，以当前作业承接本次消费。
        job_rows = [dict(item) for item in jobs if str(item.get("uuid")) != normalized_job]
        job_rows.append(dict(job))
        pending_keys = {str(requirement["requirement_key"]) for requirement in pending}
        allocations: list[dict[str, Any]] = []
        for source_uuid in dict.fromkeys(self._requirement_source(item) for item in pending):
            keys = sorted(
                str(item["requirement_key"]) for item in pending
                if self._requirement_source(item) == source_uuid
            )
            container_uuid = self._deferred_source_container(
                plan_nodes, jobs, source_uuid=source_uuid, requirement_keys=keys
            )
            runtime_plan = deepcopy(dict(plan))
            for node in runtime_plan.get("nodes", []):
                if isinstance(node, dict) and str(node.get("uuid")) == source_uuid:
                    node["material_requirements"] = [{"instance_uuid": container_uuid}]
            prepared = PreparedTaskInput(deepcopy(dict(snapshot)), {}, runtime_plan, job_rows)
            built = self._build_task_allocations(
                graph=snapshot, prepared=prepared, task_uuid=normalized_task,
                bindings=(), runtime_source_uuid=source_uuid,
            )
            allocations.extend(
                allocation for allocation in built
                if allocation["requirement_key"] in pending_keys
                and allocation["workflow_node_job_uuid"] == normalized_job
            )
        try:
            self._authority.reserve_task(normalized_task, allocations, incremental=True)
        except WorkflowQuantityReservationError as error:
            raise StoreConflict(
                f"补料分支提供的容器未能通过数量检查，需人工对账：{error}"
            ) from error
        self.persist_runtime_allocations(allocations)
        return allocations

    @staticmethod
    def _requirement_source(requirement: Mapping[str, Any]) -> str:
        """读取数量需求绑定的物料来源节点身份；未绑定返回空串。"""

        metadata = requirement.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        return str(
            (unilab.get("material_source_node_uuid") if isinstance(unilab, Mapping) else "")
            or ""
        ).strip()

    @staticmethod
    def _job_container_uuid(job: Mapping[str, Any] | None) -> str:
        """返回成功作业结果中对后续动作提供的物料容器身份；无则返回空串。"""

        if not isinstance(job, Mapping) or job.get("status") != "succeeded":
            return ""
        result = job.get("return_info")
        if not isinstance(result, Mapping):
            return ""
        material = result.get("material")
        if isinstance(material, Mapping) and str(material.get("uuid") or "").strip():
            return str(material["uuid"]).strip()
        return str(result.get("material_uuid") or "").strip()

    def _deferred_source_container(
        self,
        plan_nodes: Mapping[str, Mapping[str, Any]],
        jobs: Sequence[Mapping[str, Any]],
        *,
        source_uuid: str,
        requirement_keys: Sequence[str],
    ) -> str:
        """确定运行时来源缺料后由实际执行的补料分支提供给消费动作的容器。

        参数：冻结计划节点、任务全部作业、来源节点与相关需求键。返回：唯一容器
        物料 UUID。异常：来源未成功执行，或引用该来源 ``sufficient`` 的条件节点
        实际选中分支给不出唯一容器时抛 ``StoreConflict``，禁止猜测容器。
        """

        latest: dict[str, Mapping[str, Any]] = {}
        for item in jobs:
            node_uuid = str(item.get("workflow_node_uuid") or "")
            previous = latest.get(node_uuid)
            if previous is None or int(item.get("attempt") or 1) >= int(previous.get("attempt") or 1):
                latest[node_uuid] = item
        label = "、".join(requirement_keys)
        source_job = latest.get(source_uuid)
        if source_job is None or source_job.get("status") != "succeeded":
            raise StoreConflict(
                f"数量需求 {label} 的物料来源尚未成功执行，无法确定消费容器，需人工对账"
            )
        result = source_job.get("return_info")
        selected_container = self._job_container_uuid(source_job)
        if selected_container and not (
            isinstance(result, Mapping) and result.get("sufficient") is False
        ):
            return selected_container
        candidates: list[str] = []
        for node_uuid, node in plan_nodes.items():
            if node.get("kind") != "condition":
                continue
            region = node.get("control_region") or node.get("param")
            if not isinstance(region, Mapping):
                continue
            bindings = region.get("bindings")
            if not isinstance(bindings, Mapping) or not any(
                isinstance(binding, Mapping)
                and binding.get("kind") == "node_result"
                and str(binding.get("node_uuid")) == source_uuid
                for binding in bindings.values()
            ):
                continue
            condition_job = latest.get(node_uuid)
            if condition_job is None or condition_job.get("status") != "succeeded":
                continue
            condition_result = condition_job.get("return_info")
            selected = (
                condition_result.get("selected_branch")
                if isinstance(condition_result, Mapping)
                else None
            )
            branches = region.get("branches")
            if selected is None or not isinstance(branches, Sequence):
                continue
            for branch in branches:
                # selected_branch 的语义是选中分支 label 的字符串（dag_state 以
                # str(label) 持久化），只按 label 精确匹配；避免 label 与兄弟分支
                # 索引字符串碰撞时误匹配多个分支、取错补料容器。
                if (
                    not isinstance(branch, Mapping)
                    or str(branch.get("label")) != str(selected)
                ):
                    continue
                # 补料分支的出口结果即“供后续实验使用的物料”；出口无物料结果时
                # 才退回分支内全部成功作业，且必须唯一。
                found = [
                    uuid for uuid in (
                        self._job_container_uuid(latest.get(str(member)))
                        for member in branch.get("exit_node_uuids") or []
                    ) if uuid
                ]
                if not found:
                    found = [
                        uuid for uuid in (
                            self._job_container_uuid(latest.get(str(member)))
                            for member in branch.get("node_uuids") or []
                        ) if uuid
                    ]
                candidates.extend(found)
        distinct = sorted(set(candidates))
        if len(distinct) != 1:
            raise StoreConflict(
                f"数量需求 {label} 的来源缺料，且无法从实际执行的补料分支唯一确定"
                "消费容器，需人工对账"
            )
        return distinct[0]

    @staticmethod
    def _adopt_retry_allocations(
        connection: sqlite3.Connection, *, task_uuid: str, job_uuid: str
    ) -> list[dict[str, Any]]:
        """在工作流事务内把前次已知失败 attempt 的预留分配交给正式重试作业。

        参数：工作流事务、任务与重试作业身份。返回：已改归属的分配（附
        ``reservation_job_uuid`` 指向库存侧实际持有预留的作业）；无可接管时为空。
        异常：前次 attempt 结果不明、仍在执行或分属多个作业时抛
        ``StoreConflict``，禁止重复结算或吞掉可能已发生的消耗。
        """

        job = connection.execute(
            "SELECT workflow_node_uuid,attempt FROM workflow_node_job "
            "WHERE uuid=? AND workflow_task_uuid=? AND deleted_at IS NULL",
            (job_uuid, task_uuid),
        ).fetchone()
        if job is None:
            return []
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT allocation.*, job.status AS previous_status,
                       job.attempt AS previous_attempt,
                       job.uncertainty_reason AS previous_uncertainty
                FROM workflow_inventory_allocation AS allocation
                JOIN workflow_node_job AS job
                  ON job.uuid = allocation.workflow_node_job_uuid
                WHERE allocation.workflow_task_uuid = ?
                  AND job.workflow_node_uuid = ? AND job.uuid != ?
                  AND allocation.status = 'reserved'
                ORDER BY allocation.inventory_type, allocation.inventory_uuid
                """,
                (task_uuid, job["workflow_node_uuid"], job_uuid),
            ).fetchall()
        ]
        if not rows:
            return []
        previous_jobs = {str(row["workflow_node_job_uuid"]) for row in rows}
        if len(previous_jobs) != 1 or any(
            int(row["previous_attempt"]) >= int(job["attempt"])
            or row["previous_status"] not in {"failed", "timeout", "canceled"}
            or row["previous_uncertainty"]
            for row in rows
        ):
            raise StoreConflict("前次尝试的数量预留状态不明，不能由重试作业结算，需人工对账")
        previous_job = previous_jobs.pop()
        marks = ",".join("?" for _ in rows)
        connection.execute(
            f"""
            UPDATE workflow_inventory_allocation
            SET workflow_node_job_uuid=?, revision=revision+1
            WHERE uuid IN ({marks}) AND workflow_task_uuid=? AND status='reserved'
            """,
            (job_uuid, *[row["uuid"] for row in rows], task_uuid),
        )
        adopted: list[dict[str, Any]] = []
        for row in rows:
            for key in ("previous_status", "previous_attempt", "previous_uncertainty"):
                row.pop(key, None)
            adopted.append({
                **row,
                "workflow_node_job_uuid": job_uuid,
                "revision": int(row["revision"]) + 1,
                "reservation_job_uuid": previous_job,
            })
        return adopted

    def preflight_task_allocations(
        self,
        *,
        graph: Mapping[str, Any],
        prepared: PreparedTaskInput,
        bindings: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """只读检查数量型库存绑定能否完成整任务准入。

        参数：图快照、候选执行计划和运行时绑定与正式创建路径完全相同。返回：
        经过身份、单位、需求总量和当前可用量验证的分配预览。异常：任一条件不满足
        时抛 ``StoreConflict``。本方法不写 WorkflowTask、预留、台账或 Outbox；
        正式提交仍须在写事务内重新检查。
        """

        allocations = self.prepare_preflight_allocations(
            graph=graph, prepared=prepared, bindings=bindings,
        )
        self.check_preflight_allocations(allocations)
        return allocations

    def prepare_preflight_allocations(
        self, *, graph: Mapping[str, Any], prepared: PreparedTaskInput,
        bindings: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """只规范候选数量绑定；余量不足的候选仍需纳入整批合计。"""

        return self._build_task_allocations(
            graph=graph, prepared=prepared,
            task_uuid="00000000-0000-4000-8000-000000000001", bindings=bindings,
        )

    def check_preflight_allocations(
        self, allocations: Sequence[Mapping[str, Any]],
    ) -> None:
        """在同一库存快照内检查整批已规范化分配，重复候选必须重复计量。

        参数：每次候选执行的完整分配，数量已统一到库存实例单位。返回无；不写
        任务、预留、台账或发件箱。库存不足时保留结构化缺口并抛 StoreConflict。
        """

        try:
            self._authority.preflight_task(allocations)
        except WorkflowQuantityShortageError as error:
            conflict = StoreConflict(str(error))
            conflict.shortages = error.shortages
            raise conflict from error
        except WorkflowQuantityReservationError as error:
            raise StoreConflict(str(error)) from error

    def _build_task_allocations(
        self,
        *,
        graph: Mapping[str, Any],
        prepared: PreparedTaskInput,
        task_uuid: str,
        bindings: Sequence[Mapping[str, Any]],
        runtime_source_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """规范绑定并构造任务分配，不产生任何持久写入。"""

        job_by_node = {
            str(job.get("workflow_node_uuid")): str(job.get("uuid"))
            for job in prepared.jobs
        }
        requirements = [
            dict(requirement)
            for requirement in prepared.workflow_snapshot.get(
                "inventory_requirements",
                [],
            )
            if isinstance(requirement, Mapping)
            and str(requirement.get("consume_node_uuid"))
            in prepared.planned_node_uuids
        ]
        runtime_sources = {str(node["uuid"]) for node in prepared.execution_plan.get("nodes", []) if node.get("runtime_material_source")}
        def requirement_source(requirement: Mapping[str, Any]) -> str:
            return str((requirement.get("meta_data") or {}).get("unilab", {}).get("material_source_node_uuid") or "")
        requirements = [requirement for requirement in requirements
                        if (requirement_source(requirement) == runtime_source_uuid if runtime_source_uuid
                            else requirement_source(requirement) not in runtime_sources)]
        if any(
            str(requirement.get("consume_node_uuid")) not in job_by_node
            for requirement in requirements
        ):
            raise StoreConflict("动态执行节点的数量型库存需求尚未支持逐轮分配")
        requirement_by_key = {
            str(requirement.get("requirement_key") or "").strip(): requirement
            for requirement in requirements
        }
        if "" in requirement_by_key or len(requirement_by_key) != len(requirements):
            raise StoreConflict("工作流库存需求键缺失或重复")

        plan_nodes = {
            str(node.get("uuid")): node
            for node in prepared.execution_plan.get("nodes", [])
            if isinstance(node, Mapping)
        }
        explicit_keys = {
            str(binding.get("requirement_key") or "").strip()
            for binding in bindings
            if isinstance(binding, Mapping)
        }
        effective_bindings: list[Any] = [
            dict(binding) if isinstance(binding, Mapping) else binding
            for binding in bindings
        ]
        for requirement in requirements:
            metadata = requirement.get("meta_data")
            unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
            source_uuid = str(
                (unilab.get("material_source_node_uuid") or "")
                if isinstance(unilab, Mapping)
                else ""
            ).strip()
            if not source_uuid:
                continue
            requirement_key = str(requirement["requirement_key"])
            if requirement_key in explicit_keys:
                raise StoreConflict(
                    f"库存需求 {requirement_key} 由物料来源自动绑定，不能手工覆盖"
                )
            source_node = plan_nodes.get(source_uuid)
            if source_node is None or source_node.get("kind") != "material_source":
                raise StoreConflict(
                    f"库存需求 {requirement_key} 引用的物料来源不在本次执行计划中"
                )
            try:
                content = self._authority.content_for_material_source(
                    source_node
                )
            except WorkflowQuantityReservationError as error:
                raise StoreConflict(str(error)) from error
            effective_bindings.append(
                {
                    "requirement_key": requirement_key,
                    "inventory_type": content["inventory_type"],
                    "inventory_uuid": content["uuid"],
                    "reserved_quantity": requirement["required_quantity"],
                    "quantity_unit": requirement["quantity_unit"],
                }
            )

        normalized_bindings: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str, str]] = set()
        seen_node_inventory: set[tuple[str, str, str]] = set()
        for index, raw in enumerate(effective_bindings):
            if not isinstance(raw, Mapping):
                raise StoreConflict(f"inventory_bindings[{index}] 必须是对象")
            requirement_key = str(raw.get("requirement_key") or "").strip()
            requirement = requirement_by_key.get(requirement_key)
            if requirement is None:
                raise StoreConflict(
                    f"inventory_bindings[{index}] 未对应本次执行的逻辑需求"
                )
            inventory_type = str(raw.get("inventory_type") or "").strip().lower()
            requirement_metadata = requirement.get("meta_data")
            requirement_unilab = (
                requirement_metadata.get("unilab")
                if isinstance(requirement_metadata, Mapping)
                else None
            )
            source_bound = bool(
                isinstance(requirement_unilab, Mapping)
                and requirement_unilab.get("material_source_node_uuid")
            )
            expected_types = (
                {"reagent", "current_substance"}
                if source_bound
                else {
                    "reagent"
                    if requirement.get("target_type") == "reagent_info"
                    else "current_substance"
                }
            )
            if inventory_type not in expected_types:
                raise StoreConflict(f"库存绑定 {requirement_key} 的类型与需求不匹配")
            inventory_uuid = _uuid(
                raw.get("inventory_uuid"),
                field=f"inventory_bindings[{index}].inventory_uuid",
            )
            reserved_quantity = _positive(
                raw.get("reserved_quantity"),
                field=f"inventory_bindings[{index}].reserved_quantity",
            )
            quantity_unit = str(raw.get("quantity_unit") or "").strip()
            if (
                quantity_unit.casefold()
                != str(requirement.get("quantity_unit") or "").strip().casefold()
            ):
                raise StoreConflict(
                    f"库存绑定 {requirement_key} 必须使用需求声明的单位"
                )
            identity = (requirement_key, inventory_type, inventory_uuid)
            if identity in seen:
                raise StoreConflict(f"库存绑定 {requirement_key} 包含重复库存实例")
            seen.add(identity)
            node_identity = (
                str(requirement["consume_node_uuid"]),
                inventory_type,
                inventory_uuid,
            )
            if node_identity in seen_node_inventory:
                raise StoreConflict("同一节点不能把一个库存实例绑定给多个逻辑需求")
            seen_node_inventory.add(node_identity)
            normalized_bindings.setdefault(requirement_key, []).append(
                {
                    "requirement_key": requirement_key,
                    "inventory_type": inventory_type,
                    "inventory_uuid": inventory_uuid,
                    "reserved_quantity": reserved_quantity,
                    "quantity_unit": quantity_unit,
                }
            )

        allocations: list[dict[str, Any]] = []
        for requirement in requirements:
            requirement_key = str(requirement["requirement_key"])
            selected = normalized_bindings.get(requirement_key, [])
            if not selected:
                raise StoreConflict(f"库存需求 {requirement_key} 没有运行时绑定")
            if not bool(requirement.get("allow_split")) and len(selected) != 1:
                raise StoreConflict(f"库存需求 {requirement_key} 不允许拆分绑定")
            total = math.fsum(float(binding["reserved_quantity"]) for binding in selected)
            required = float(requirement["required_quantity"])
            if not _quantity_equal(total, required):
                raise StoreConflict(
                    f"库存需求 {requirement_key} 预留 {total:g}，应为 {required:g}"
                )
            for binding in selected:
                try:
                    content = self._authority.content(
                        binding["inventory_type"], binding["inventory_uuid"]
                    )
                except WorkflowQuantityReservationError as error:
                    raise StoreConflict(str(error)) from error
                # 预留与活动预留汇总一律使用库存实例单位：可安全换算的需求单位
                # （如 µL/L 对 mL）先统一到库存单位再判断余量；不可换算保持报错。
                reserved_quantity = float(binding["reserved_quantity"])
                quantity_unit = str(binding["quantity_unit"])
                inventory_unit = str(content["quantity_unit"])
                if inventory_unit.casefold() != quantity_unit.casefold():
                    conversion = convert_quantity(
                        reserved_quantity,
                        quantity_unit,
                        inventory_unit,
                        inventory_type=binding["inventory_type"],
                        physical_state=content.get("physical_state"),
                        concentration_value=content.get("concentration_value"),
                        density_g_per_ml=content.get("density_g_per_ml"),
                    )
                    if conversion is None:
                        raise StoreConflict("库存实例单位与工作流绑定单位不一致")
                    reserved_quantity = _positive(
                        conversion[0], field=f"库存需求 {requirement_key} 换算后的预留量"
                    )
                    quantity_unit = inventory_unit
                if requirement.get("target_type") == "reagent_info" and content.get(
                    "reagent_info_uuid"
                ) != requirement.get("reagent_info_uuid"):
                    raise StoreConflict("试剂实例与工作流要求的试剂身份不一致")
                allocation_uuid = str(
                    uuid5(
                        NAMESPACE_URL,
                        "unilab:workflow-inventory-allocation:"
                        f"{task_uuid}:{requirement_key}:{binding['inventory_type']}:"
                        f"{binding['inventory_uuid']}",
                    )
                )
                allocations.append(
                    {
                        "uuid": allocation_uuid,
                        "workflow_task_uuid": task_uuid,
                        "workflow_node_job_uuid": job_by_node[
                            str(requirement["consume_node_uuid"])
                        ],
                        "requirement_key": requirement_key,
                        "inventory_type": binding["inventory_type"],
                        "inventory_uuid": binding["inventory_uuid"],
                        "material_uuid": content["material_uuid"],
                        "reserved_quantity": reserved_quantity,
                        "quantity_unit": quantity_unit,
                    }
                )
        return allocations

    @_serialized_protocol
    def consume_successful_job(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        consumptions: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """结算一个明确成功作业的数量型库存。

        参数：任务和作业身份限定既有预留；``consumptions`` 是 Edge 可选实际用量，
        未上报的已预留库存按预留量消费。返回：由统一库存台账还原的消费事实。
        异常：未计划库存、重复上报、实际量超过预留或余额不足时抛
        ``StoreConflict``；意图会在扣减前持久化，重启后可幂等恢复。
        作业自身没有分配时：运行时来源缺料后由补料分支提供容器的需求先补做
        预留（无法确定容器则报错）；正式重试作业接管前次已知失败 attempt 的预留。
        """

        normalized_task = _uuid(task_uuid, field="workflow_task_uuid")
        normalized_job = _uuid(job_uuid, field="workflow_node_job_uuid")
        operation_key = f"consume:{normalized_job}"
        rejected_error: StoreConflict | None = None
        # 运行时来源缺料、经补料分支后才确定容器的需求：派发前未补做时，
        # 在结算前按实际执行分支补做预留；无法确定容器时关闭式失败。
        self.reserve_deferred_runtime_requirements(
            task_uuid=normalized_task, job_uuid=normalized_job
        )
        with self._workflow_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_inventory_allocation
                WHERE workflow_task_uuid = ? AND workflow_node_job_uuid = ?
                ORDER BY inventory_type, inventory_uuid
                """,
                (normalized_task, normalized_job),
            ).fetchall()
            allocations = [dict(row) for row in rows]
            if not allocations:
                # 正式重试的新 attempt 接管前次已知失败 attempt 仍持有的预留。
                allocations = self._adopt_retry_allocations(
                    connection, task_uuid=normalized_task, job_uuid=normalized_job
                )
            if allocations and all(row["status"] == "consumed" for row in allocations):
                return self._authority.list_job_consumptions(normalized_job)
            if any(row["status"] != "reserved" for row in allocations):
                raise StoreConflict("工作流库存分配已被释放或部分结算")
            pending = connection.execute(
                "SELECT operation_key,payload,last_error FROM workflow_inventory_saga "
                "WHERE workflow_task_uuid=? AND status='consume_pending'",
                (normalized_task,),
            ).fetchone()
            if pending is not None:
                if str(pending["operation_key"]) != operation_key:
                    raise StoreConflict("任务存在待对账的库存消费，不能结算其他作业")
                # Edge 结果不可变；普通重放始终使用首次持久化或人工对账后的记录。
                records = self._pending_records(
                    pending["payload"], task_uuid=normalized_task, job_uuid=normalized_job
                )
                if _load(pending["payload"], {}).get("needs_correction"):
                    rejected_error = StoreConflict(
                        str(pending["last_error"] or "库存消费上报无效，需人工更正")
                    )
            else:
                audit_context = self._job_audit_context(
                    connection,
                    task_uuid=normalized_task,
                    job_uuid=normalized_job,
                )
                try:
                    records = self._build_consumption_records(
                        allocations, consumptions, audit_context=audit_context,
                    )
                except StoreConflict as error:
                    if not allocations:
                        raise
                    # 无效回执仍可能对应真实耗用，冻结预留与原始证据，不能自动按计划扣量。
                    records = self._build_consumption_records(
                        allocations, [], audit_context=audit_context,
                    )
                    rejected_error = error
                if not allocations and consumptions:
                    raise StoreConflict("作业上报了未在执行计划中预留的库存消耗")
                if not records:
                    return []
                now = utc_now()
                payload: dict[str, Any] = {"records": records}
                if rejected_error is not None:
                    payload.update({
                        "needs_correction": True,
                        "rejected_consumptions": list(consumptions),
                    })
                connection.execute(
                    """
                    INSERT INTO workflow_inventory_saga(
                        workflow_task_uuid,status,operation_key,payload,last_error,
                        attempt,update_time
                    ) VALUES (?, 'consume_pending', ?, ?, ?, 1, ?)
                    ON CONFLICT(workflow_task_uuid) DO UPDATE SET
                        status='consume_pending', operation_key=excluded.operation_key,
                        payload=excluded.payload, last_error=excluded.last_error,
                        attempt=workflow_inventory_saga.attempt + 1,
                        update_time=excluded.update_time
                    """,
                    (normalized_task, operation_key, _json(payload),
                     str(rejected_error) if rejected_error is not None else None, now),
                )
        if rejected_error is not None:
            raise rejected_error
        return self._apply_pending_consumption(
            task_uuid=normalized_task,
            operation_key=operation_key,
            records=records,
        )

    @_serialized_protocol
    def recover_pending(self) -> dict[str, str]:
        """重放未收敛消费/释放并清理任务创建崩溃残留。

        返回本轮需要对账的任务及原因。单任务的确定性业务错误保留预留和消费意图，
        不阻止其他任务恢复；连接、磁盘等基础设施错误仍向上传播。
        """

        def task_exists(task_uuid: str) -> bool:
            """查询工作流任务是否已提交；参数为任务 UUID，返回存在性。"""

            try:
                self._workflow_store.get_task(task_uuid)
            except StoreNotFound:
                return False
            return True

        self._authority.release_orphans(task_exists)
        with self._workflow_store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT workflow_task_uuid,operation_key,payload,last_error
                FROM workflow_inventory_saga
                WHERE status IN ('consume_pending','release_pending')
                ORDER BY update_time, workflow_task_uuid
                """
            ).fetchall()
        failures: dict[str, str] = {}
        for row in rows:
            task_uuid = str(row["workflow_task_uuid"])
            operation_key = str(row["operation_key"])
            payload = _load(row["payload"], {})
            try:
                if operation_key.startswith("release:"):
                    reason = (
                        str(payload.get("reason") or "workflow_terminal")
                        if isinstance(payload, Mapping)
                        else "workflow_terminal"
                    )
                    self.release_task(task_uuid, reason=reason)
                    continue
                if not operation_key.startswith("consume:"):
                    raise StoreConflict("库存消费 Saga 操作身份损坏")
                if isinstance(payload, Mapping) and payload.get("needs_correction"):
                    raise StoreConflict(str(row["last_error"] or "库存消费上报无效，需人工更正"))
                self._apply_pending_consumption(
                    task_uuid=task_uuid,
                    operation_key=operation_key,
                    records=self._pending_records(
                        payload, task_uuid=task_uuid,
                        job_uuid=operation_key.removeprefix("consume:"),
                    ),
                )
            except (WorkflowQuantityReservationError, StoreConflict, sqlite3.IntegrityError) as error:
                failures[task_uuid] = str(error)
                with self._workflow_store.transaction() as connection:
                    connection.execute(
                        "UPDATE workflow_inventory_saga SET last_error=?,update_time=? "
                        "WHERE workflow_task_uuid=? AND operation_key=?",
                        (str(error), utc_now(), task_uuid, operation_key),
                    )
        return failures

    @staticmethod
    def _pending_records(
        payload: Any, *, task_uuid: str, job_uuid: str
    ) -> list[dict[str, Any]]:
        """严格读取消费意图，损坏项不能被过滤成空消费并误释放预留。"""

        loaded = _load(payload, {})
        records = loaded.get("records") if isinstance(loaded, Mapping) else None
        if not isinstance(records, list) or not records or any(
            not isinstance(record, Mapping) for record in records
        ):
            raise StoreConflict("库存消费 Saga 载荷损坏")
        required = {
            "uuid", "event_uuid", "workflow_task_uuid", "workflow_node_job_uuid", "inventory_type",
            "inventory_uuid", "material_uuid", "quantity_unit", "reserved_quantity",
            "planned_quantity", "actual_quantity", "reported_quantity_unit",
            "workflow_node_uuid", "workflow_name", "node_name",
        }
        seen: set[tuple[str, str]] = set()
        for record in records:
            if (
                not required.issubset(record)
                or record["workflow_task_uuid"] != task_uuid
                or record["workflow_node_job_uuid"] != job_uuid
                or not isinstance(record["inventory_type"], str)
                or record["inventory_type"] not in {"reagent", "current_substance"}
            ):
                raise StoreConflict("库存消费 Saga 载荷身份损坏")
            inventory_uuid = _uuid(record["inventory_uuid"], field="inventory_uuid")
            _uuid(record["uuid"], field="allocation_uuid")
            _uuid(record["event_uuid"], field="event_uuid")
            if record.get("reservation_job_uuid") is not None:
                _uuid(record["reservation_job_uuid"], field="reservation_job_uuid")
            _positive(record["reserved_quantity"], field="reserved_quantity")
            _positive(record["planned_quantity"], field="planned_quantity")
            _non_negative(record["actual_quantity"], field="actual_quantity")
            if not str(record["quantity_unit"] or "").strip() or not str(
                record["reported_quantity_unit"] or ""
            ).strip():
                raise StoreConflict("库存消费 Saga 缺少数量单位")
            identity = (record["inventory_type"], inventory_uuid)
            if identity in seen:
                raise StoreConflict("库存消费 Saga 包含重复实例")
            seen.add(identity)
        return [dict(record) for record in records]

    @_serialized_protocol
    def pending_consumption_reconciliations(self) -> list[dict[str, str]]:
        """列出仍持有数量预留的待结算作业，供运行时冻结和人工对账。"""

        with self._workflow_store.transaction() as connection:
            rows = connection.execute(
                "SELECT workflow_task_uuid,operation_key,last_error "
                "FROM workflow_inventory_saga WHERE status='consume_pending' "
                "ORDER BY update_time,workflow_task_uuid"
            ).fetchall()
        return [
            {
                "task_uuid": str(row["workflow_task_uuid"]),
                "job_uuid": str(row["operation_key"]).removeprefix("consume:"),
                "reason": str(row["last_error"] or "工作流库存消费尚未结算"),
            }
            for row in rows
        ]

    @_serialized_protocol
    def reconcile_consumption(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        consumptions: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> list[dict[str, Any]]:
        """按人工原因更正尚未落账的消费；普通 Edge 重放不能更改冻结数量。

        调用方负责验证操作权限和设备停止证据。本方法只更正同 Job 的待消费意图，
        保留未更正实例的原值；同一更正重试幂等，已落账后不能改成其他数量。
        """

        normalized_task = _uuid(task_uuid, field="workflow_task_uuid")
        normalized_job = _uuid(job_uuid, field="workflow_node_job_uuid")
        if not isinstance(reason, str) or not reason.strip():
            raise StoreConflict("库存消费对账必须填写原因")
        reason = reason.strip()
        operation_key = f"consume:{normalized_job}"
        with self._workflow_store.transaction() as connection:
            pending = connection.execute(
                "SELECT status,payload FROM workflow_inventory_saga WHERE workflow_task_uuid=? "
                "AND operation_key=?",
                (normalized_task, operation_key),
            ).fetchone()
            if pending is None:
                raise StoreConflict("作业没有待对账的库存消费")
            payload = _load(pending["payload"], {})
            previous = self._pending_records(
                payload, task_uuid=normalized_task, job_uuid=normalized_job
            )
            corrections = {
                (str(item.get("inventory_type") or "").strip().lower(),
                 _uuid(item.get("inventory_uuid"), field="inventory_uuid"))
                for item in consumptions if isinstance(item, Mapping)
            }
            if payload.get("needs_correction") and corrections != {
                (record["inventory_type"], record["inventory_uuid"]) for record in previous
            }:
                raise StoreConflict("无效消费回执的对账必须明确全部已预留实例的实际用量")
            merged = [
                {
                    "inventory_type": record["inventory_type"],
                    "inventory_uuid": record["inventory_uuid"],
                    "actual_quantity": record["actual_quantity"],
                    "quantity_unit": record["reported_quantity_unit"],
                }
                for record in previous
                if (record["inventory_type"], record["inventory_uuid"]) not in corrections
            ]
            records = self._build_consumption_records(
                previous, [*merged, *consumptions], audit_context={}
            )
            request = {"consumptions": [dict(item) for item in consumptions], "reason": reason}
            for record, old in zip(records, previous):
                if (record["inventory_type"], record["inventory_uuid"]) not in corrections:
                    record["actual_quantity_reported"] = old.get("actual_quantity_reported", True)
            history = payload.get("reconciliations", [])
            if not isinstance(history, list):
                raise StoreConflict("库存消费对账历史损坏")
            replaying = bool(
                history and isinstance(history[-1], Mapping)
                and history[-1].get("reason") == reason
                and all(
                    (old.get("reconciliation") or {}).get("request") == request
                    for old in previous
                )
                and all(
                    all(record[key] == old.get(key, True) for key in (
                        "actual_quantity", "reported_quantity_unit", "actual_quantity_reported"
                    ))
                    for record, old in zip(records, previous)
                )
            )
            committed = self._authority.list_job_consumptions(normalized_job)
            if committed and (not replaying or any(
                ((item.get("meta_data") or {}).get("reconciliation") or {}).get("request") != request
                for item in committed
            )):
                raise StoreConflict("作业消费已落账，请重放结算，不能更改已提交数量")
            if pending["status"] != "consume_pending":
                if committed and replaying:
                    return committed
                raise StoreConflict("作业没有待对账的库存消费")
            if replaying:
                records = previous
            else:
                now = utc_now()
                for record, old in zip(records, previous):
                    record["reconciliation"] = {
                        "request": request,
                        "reason": reason,
                        "at": now,
                        "previous_actual_quantity": old["actual_quantity"],
                        "previous_reported_quantity_unit": old["reported_quantity_unit"],
                    }
                connection.execute(
                    "UPDATE workflow_inventory_saga SET payload=?,last_error=NULL,"
                    "attempt=attempt+1,update_time=? WHERE workflow_task_uuid=? AND operation_key=?",
                    (_json({**payload, "needs_correction": False,
                            "records": records, "reconciliations": [*history, {
                        "reason": reason, "at": now, "previous_records": previous,
                        "corrected_records": records,
                    }]}), now, normalized_task, operation_key),
                )
        return self._apply_pending_consumption(
            task_uuid=normalized_task, operation_key=operation_key, records=records
        )

    def discard_uncommitted_task(self, task_uuid: str) -> None:
        """补偿任务创建事务未提交前已经写入库存权威的数量预留。

        参数：``task_uuid`` 是尚未成为工作流事实的拟创建任务身份。返回无。异常：
        库存事务失败原样传播，调用方必须报告内部错误；相同身份重复补偿幂等，且
        只释放本协议写入的现有库存预留。
        """

        normalized = _uuid(task_uuid, field="workflow_task_uuid")
        self._authority.release_task(
            normalized,
            reason="workflow_task_creation_rolled_back",
        )

    @_serialized_protocol
    def release_task(self, task_uuid: str, *, reason: str) -> None:
        """先释放库存权威预留，再把工作流分配投影收敛为 released。

        参数：``task_uuid`` 是终态任务；``reason`` 是审计原因。返回无。异常：
        库存释放失败时保留 ``release_pending`` 供恢复扫描重放；重复释放幂等，
        绝不先把工作流投影标记为已释放。
        """

        normalized_task = _uuid(task_uuid, field="workflow_task_uuid")
        with self._workflow_store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM workflow_inventory_saga WHERE workflow_task_uuid=? "
                "AND status='consume_pending'", (normalized_task,)
            ).fetchone() is not None:
                raise StoreConflict("库存消费尚未结算，必须完成对账后才能释放预留")
            pending = connection.execute(
                "SELECT COUNT(*) FROM workflow_inventory_allocation "
                "WHERE workflow_task_uuid=? AND status='reserved'",
                (normalized_task,),
            ).fetchone()[0]
            if not pending:
                return
            now = utc_now()
            connection.execute(
                """
                INSERT INTO workflow_inventory_saga(
                    workflow_task_uuid,status,operation_key,payload,last_error,
                    attempt,update_time
                ) VALUES (?, 'release_pending', ?, ?, NULL, 1, ?)
                ON CONFLICT(workflow_task_uuid) DO UPDATE SET
                    status='release_pending', operation_key=excluded.operation_key,
                    payload=excluded.payload,last_error=NULL,
                    attempt=workflow_inventory_saga.attempt+1,
                    update_time=excluded.update_time
                """,
                (
                    normalized_task,
                    f"release:{normalized_task}",
                    _json({"reason": str(reason)}),
                    now,
                ),
            )
        try:
            self._authority.release_task(normalized_task, reason=reason)
        except BaseException as error:
            with self._workflow_store.transaction() as connection:
                connection.execute(
                    "UPDATE workflow_inventory_saga SET last_error=?,update_time=? "
                    "WHERE workflow_task_uuid=? AND status='release_pending'",
                    (str(error), utc_now(), normalized_task),
                )
            raise
        with self._workflow_store.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_inventory_allocation
                SET status='released', released_at=?, revision=revision+1
                WHERE workflow_task_uuid=? AND status='reserved'
                """,
                (utc_now(), normalized_task),
            )
            connection.execute(
                """
                UPDATE workflow_inventory_saga
                SET status='settled',last_error=NULL,update_time=?
                WHERE workflow_task_uuid=? AND operation_key=?
                """,
                (utc_now(), normalized_task, f"release:{normalized_task}"),
            )

    def list_task_consumptions(self, task_uuid: str) -> list[dict[str, Any]]:
        """读取一个任务的库存消费台账投影；结果按发生时间和事件身份排序。"""

        normalized = _uuid(task_uuid, field="workflow_task_uuid")
        return self._authority.list_task_consumptions(normalized)

    def list_task_material_events(
        self, task_uuid: str, job_uuids: set[str], *, since_ms: int = 0
    ) -> list[dict[str, Any]]:
        """读取一个任务的物料变化台账投影（搬运、消耗、标为已使用）。"""

        normalized = _uuid(task_uuid, field="workflow_task_uuid")
        return self._authority.list_task_material_events(
            normalized, set(job_uuids), since_ms=since_ms
        )

    def list_job_consumptions(self, job_uuid: str) -> list[dict[str, Any]]:
        """读取一个作业的库存消费台账投影；不存在时返回空数组。"""

        normalized = _uuid(job_uuid, field="workflow_node_job_uuid")
        return self._authority.list_job_consumptions(normalized)

    def list_reagent_consumptions(self, reagent_uuid: str) -> list[dict[str, Any]]:
        """读取试剂库存实例的消费谱系，删除后的历史身份仍可查询。

        参数：``reagent_uuid`` 是库存实例身份。返回：按发生时间倒序的消费事实；
        无历史时为空。异常：非法 UUID 抛 ``StoreConflict``。
        """

        normalized = _uuid(reagent_uuid, field="reagent_uuid")
        return self._authority.list_reagent_consumptions(normalized)

    def _build_consumption_records(
        self,
        allocations: Sequence[Mapping[str, Any]],
        consumptions: Sequence[Mapping[str, Any]],
        *,
        audit_context: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """把 Edge 实际用量、预留分配与冻结审计上下文合并成稳定记录。"""

        actual_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        for index, raw in enumerate(consumptions):
            if not isinstance(raw, Mapping):
                raise StoreConflict(f"inventory_consumptions[{index}] 必须是对象")
            inventory_type = str(raw.get("inventory_type") or "").strip().lower()
            if inventory_type not in {"reagent", "current_substance"}:
                raise StoreConflict(f"inventory_consumptions[{index}] 类型无效")
            inventory_uuid = _uuid(
                raw.get("inventory_uuid"),
                field=f"inventory_consumptions[{index}].inventory_uuid",
            )
            identity = (inventory_type, inventory_uuid)
            if identity in actual_by_identity:
                raise StoreConflict("同一库存实例不能重复上报消耗")
            quantity_unit = str(raw.get("quantity_unit") or "").strip()
            if not quantity_unit:
                raise StoreConflict(f"inventory_consumptions[{index}] 缺少数量单位")
            actual_by_identity[identity] = {
                "actual_quantity": _non_negative(
                    raw.get("actual_quantity"),
                    field=f"inventory_consumptions[{index}].actual_quantity",
                ),
                "reported_quantity_unit": quantity_unit,
            }
        records: list[dict[str, Any]] = []
        planned_identities: set[tuple[str, str]] = set()
        for allocation in allocations:
            identity = (
                str(allocation["inventory_type"]),
                str(allocation["inventory_uuid"]),
            )
            if identity in planned_identities:
                raise StoreConflict("作业包含重复的库存预留")
            planned_identities.add(identity)
            actual = actual_by_identity.get(identity)
            actual_quantity = (
                float(actual["actual_quantity"])
                if actual is not None
                else float(allocation["reserved_quantity"])
            )
            event_uuid = str(
                uuid5(
                    NAMESPACE_URL,
                    "unilab:workflow-inventory-consumption:"
                    f"{allocation['workflow_node_job_uuid']}:{allocation['uuid']}",
                )
            )
            records.append(
                {
                    **dict(allocation),
                    **dict(audit_context),
                    "event_uuid": event_uuid,
                    "planned_quantity": float(allocation["reserved_quantity"]),
                    "actual_quantity": actual_quantity,
                    "actual_quantity_reported": actual is not None,
                    "reported_quantity_unit": (
                        str(actual["reported_quantity_unit"])
                        if actual is not None
                        else str(allocation["quantity_unit"])
                    ),
                }
            )
        unplanned = actual_by_identity.keys() - planned_identities
        if unplanned:
            raise StoreConflict("作业上报了未在执行计划中预留的库存消耗")
        return records

    @staticmethod
    def _job_audit_context(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        job_uuid: str,
    ) -> dict[str, Any]:
        """从冻结任务和作业读取消费审计名称。

        参数：工作流事务、任务与作业身份。返回：节点身份、工作流名和节点名；
        快照缺少展示名时使用空字符串。异常：Task/Job 关系不存在时关闭式冲突。
        """

        row = connection.execute(
            """
            SELECT task.workflow_snapshot,job.workflow_node_uuid
            FROM workflow_task AS task
            JOIN workflow_node_job AS job
              ON job.workflow_task_uuid=task.uuid AND job.uuid=?
            WHERE task.uuid=? AND task.deleted_at IS NULL
              AND job.deleted_at IS NULL
            """,
            (job_uuid, task_uuid),
        ).fetchone()
        if row is None:
            raise StoreConflict("库存消费所属 Task 或 Job 不存在")
        snapshot = _load(row["workflow_snapshot"], {})
        workflow_name = ""
        node_name = ""
        if isinstance(snapshot, Mapping):
            workflow = snapshot.get("workflow")
            if isinstance(workflow, Mapping):
                workflow_name = str(workflow.get("name") or "")
            nodes = snapshot.get("nodes")
            if isinstance(nodes, list):
                for node in nodes:
                    if isinstance(node, Mapping) and str(node.get("uuid")) == str(
                        row["workflow_node_uuid"]
                    ):
                        node_name = str(node.get("name") or "")
                        break
        return {
            "workflow_node_uuid": str(row["workflow_node_uuid"]),
            "workflow_name": workflow_name,
            "node_name": node_name,
        }

    def _apply_pending_consumption(
        self,
        *,
        task_uuid: str,
        operation_key: str,
        records: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """在库存权威应用消费，再幂等收敛工作流侧分配与 Saga。"""

        try:
            self._authority.apply_consumption(
                task_uuid=task_uuid,
                operation_key=operation_key,
                records=records,
            )
        except BaseException as error:
            with self._workflow_store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE workflow_inventory_saga
                    SET last_error=?,update_time=?
                    WHERE workflow_task_uuid=? AND operation_key=?
                    """,
                    (str(error), utc_now(), task_uuid, operation_key),
                )
            raise

        with self._workflow_store.transaction() as connection:
            allocation_uuids = [str(record["uuid"]) for record in records]
            marks = ",".join("?" for _ in allocation_uuids)
            if allocation_uuids:
                connection.execute(
                    f"""
                    UPDATE workflow_inventory_allocation
                    SET status='consumed',consumed_at=?,revision=revision+1
                    WHERE uuid IN ({marks}) AND workflow_task_uuid=?
                      AND status='reserved'
                    """,
                    (utc_now(), *allocation_uuids, task_uuid),
                )
            remaining = connection.execute(
                """
                SELECT COUNT(*) FROM workflow_inventory_allocation
                WHERE workflow_task_uuid=? AND status='reserved'
                """,
                (task_uuid,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE workflow_inventory_saga
                SET status=?,last_error=NULL,update_time=?
                WHERE workflow_task_uuid=? AND operation_key=?
                """,
                (
                    "reserved" if remaining else "settled",
                    utc_now(),
                    task_uuid,
                    operation_key,
                ),
            )
        job_uuid = str(records[0]["workflow_node_job_uuid"]) if records else ""
        return self._authority.list_job_consumptions(job_uuid) if job_uuid else []


__all__ = ["WorkflowQuantityInventory"]
