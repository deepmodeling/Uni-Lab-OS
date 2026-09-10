"""把本地调度器（EdgeScheduler）状态投影到标准任务/作业聚合。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.workflow._execution_plan_graph import final_target_data_key
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import (
    StoreConflict,
    StoreNotFound,
    WorkflowStore,
    utc_now,
)

_ACTIVE_JOB_STATES = frozenset({"pending", "dispatched", "running"})
_TERMINAL_JOB_STATES = frozenset(
    {"succeeded", "failed", "skipped", "canceled", "timeout"}
)
_FINISHED_STATE_MAP = {"success": "succeeded", "failed": "failed"}


def _encode_json_field(value: Any, *, field_name: str) -> str:
    """把一个结果字段编码成稳定 JSON 文本。

    参数：``value`` 是准备持久化的返回或错误信息；``field_name`` 是发生冲突时
    用于定位字段的代码标识。返回：键稳定排序的 JSON 文本。异常：值无法表达为
    JSON 时抛出 ``StoreConflict``，不允许部分状态写入。
    """

    try:
        return encode_json(value, sort_keys=True).decode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StoreConflict(f"{field_name} 不是合法 JSON") from exc


def _decode_json_field(value: str | None, *, fallback: Any) -> Any:
    """把数据库 JSON 文本恢复为领域值。

    参数：``value`` 是数据库字段文本；``fallback`` 是空字段使用的默认值。
    返回：解码后的 JSON 值。异常：损坏的持久化文本异常原样传播，因为它代表
    工作流存储（WorkflowStore）事实已经不可解释。
    """

    if value is None or value == "":
        return fallback
    return decode_json_bytes(value.encode("utf-8"))


class TaskRuntimeProjection:
    """将短期本地状态安全写入标准工作流任务（WorkflowTask）聚合。

    该模块只承担兼容投影，不声明持久调度内核（Durable Scheduler Kernel）能力，
    也不写入遗留 ``workflow_runs`` 或 ``job_runs`` 表。
    """

    def __init__(self, store: WorkflowStore):
        """绑定唯一工作流存储（WorkflowStore）写权威。

        参数：``store`` 是持有任务和作业标准表的工作流存储。返回：无。
        异常：构造阶段不访问数据库，调用方法时传播存储异常。
        """

        # ``_store`` 是本投影唯一允许写入的工作流任务（WorkflowTask）权威。
        self._store = store

    @staticmethod
    def _append_invalidation(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> None:
        """在状态事实同一事务内追加一次前端失效通知。"""

        WorkflowStore._append_event(
            connection,
            event="workflow.runtime.changed",
            data={"workflow_task_uuid": task_uuid},
            now=now,
        )

    @staticmethod
    def _task_row(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> sqlite3.Row:
        """在当前事务读取一个工作流任务（WorkflowTask）数据库行。

        参数：``connection`` 是调用方持有的写事务；``task_uuid`` 是任务稳定身份。
        返回：未软删除的任务行。异常：身份不存在时抛出 ``StoreNotFound``。
        """

        # ``task_row`` 是后续作业状态聚合所属的父任务事实。
        task_row = connection.execute(
            "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
            (task_uuid,),
        ).fetchone()
        if task_row is None:
            raise StoreNotFound(f"工作流任务不存在：{task_uuid}")
        return task_row

    @staticmethod
    def _job_row(
        connection: sqlite3.Connection,
        job_uuid: str,
    ) -> sqlite3.Row:
        """在当前事务读取一个工作流节点作业（WorkflowNodeJob）数据库行。

        参数：``connection`` 是调用方持有的写事务；``job_uuid`` 是作业稳定身份。
        返回：未软删除的作业行。异常：身份不存在时抛出 ``StoreNotFound``。
        """

        # ``job_row`` 是本轮要投影状态或核对重放的目标作业事实。
        job_row = connection.execute(
            "SELECT * FROM workflow_node_job WHERE uuid = ? AND deleted_at IS NULL",
            (job_uuid,),
        ).fetchone()
        if job_row is None:
            raise StoreNotFound(f"工作流节点作业不存在：{job_uuid}")
        return job_row

    @staticmethod
    def _job_rows(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> list[sqlite3.Row]:
        """读取父任务拥有的完整工作流节点作业（WorkflowNodeJob）集合。

        参数：``connection`` 是当前事务；``task_uuid`` 是父任务稳定身份。返回：按
        拓扑序和 UUID 稳定排序的作业行。异常：任务没有作业时抛出
        ``StoreConflict``，防止空集合被错误聚合为成功。
        """

        # ``job_rows`` 是决定父任务业务终态的完整兄弟作业集合。
        job_rows = connection.execute(
            """
            SELECT * FROM workflow_node_job
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL
            ORDER BY topological_index ASC, uuid ASC
            """,
            (task_uuid,),
        ).fetchall()
        if not job_rows:
            raise StoreConflict(f"工作流任务没有可投影作业：{task_uuid}")
        return list(job_rows)

    @classmethod
    def _aggregate(
        cls,
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> dict[str, Any]:
        """在同一事务生成标准任务/作业查询聚合。

        参数：``connection`` 是当前事务；``task_uuid`` 是父任务稳定身份。返回：
        包含公共任务投影和有序作业投影的字典。异常：任务或作业缺失时传播对应
        ``StoreNotFound`` 或 ``StoreConflict``。
        """

        # ``task_row`` 与 ``job_rows`` 来自同一 SQLite 快照，避免撕裂读取。
        task_row = cls._task_row(connection, task_uuid)
        job_rows = cls._job_rows(connection, task_uuid)
        return {
            "task": WorkflowStore._task_row(task_row),
            "jobs": [WorkflowStore._job_row(row) for row in job_rows],
        }

    def project_submission(
        self,
        task_uuid: str,
        scheduler_state: str,
    ) -> dict[str, Any]:
        """投影本地调度器（EdgeScheduler）的首次接收状态。

        参数：``task_uuid`` 是既有工作流任务（WorkflowTask）身份；
        ``scheduler_state`` 接受 ``waiting_for_material``、``running`` 或单步任务
        的 ``paused``。返回：不
        改写标准状态的任务/作业聚合。异常：未知本地状态或不合法的既有聚合抛出
        ``StoreConflict``；身份缺失抛出 ``StoreNotFound``。
        """

        if scheduler_state not in {"waiting_for_material", "running", "paused"}:
            raise StoreConflict(f"不支持的本地提交状态：{scheduler_state}")
        with self._store.transaction() as connection:
            # ``aggregate`` 是首次提交后可公开给 Backend-shaped 接口的标准事实。
            aggregate = self._aggregate(connection, task_uuid)
            task_status = aggregate["task"]["status"]
            job_statuses = {job["status"] for job in aggregate["jobs"]}
            # 物料来源协调作业在准入成功时已经是 ``succeeded``，而普通设备
            # 作业可能因为共享设备忙而保持 ``pending``。这是一种合法的批量
            # 排队状态，不能要求所有作业都仍为 pending，否则第二个并行子任务
            # 会在首个任务占用机器人时被错误取消。
            if task_status == "pending" and job_statuses <= (
                _ACTIVE_JOB_STATES | {"succeeded"}
            ):
                return aggregate
            # 协调器所有的物料来源解析作业可能已经成功；它不属于设备在途状态，
            # 但不应阻止普通动作提交后的任务聚合校验。
            if task_status == "running" and job_statuses <= (
                _ACTIVE_JOB_STATES | {"succeeded"}
            ):
                return aggregate
            raise StoreConflict(
                f"本地提交状态与任务聚合冲突：{task_uuid}/{scheduler_state}"
            )

    def project_material_source_blocked(self, task_uuid: str) -> dict[str, Any]:
        """验证并返回一次受阻的任务物料准入投影。

        参数：``task_uuid`` 是保持待处理的工作流任务（WorkflowTask）身份。返回：
        未发生写入的标准任务/作业聚合。异常：任务不为 ``pending``、没有物料来源
        解析作业（MaterialSourceResolutionJob），或来源作业已离开 ``pending`` 时
        抛出 ``StoreConflict``；身份不存在时传播 ``StoreNotFound``。
        """

        with self._store.transaction() as connection:
            aggregate = self._aggregate(connection, task_uuid)
            if aggregate["task"]["status"] != "pending":
                raise StoreConflict(f"任务不能保持准入受阻：{task_uuid}")
            # ``source_jobs`` 是本次全有或全无准入共同拥有的协调器作业。
            source_jobs = [
                job
                for job in aggregate["jobs"]
                if job.get("executor_kind") == "material_source"
            ]
            if not source_jobs or any(job["status"] != "pending" for job in source_jobs):
                raise StoreConflict(f"物料来源作业不能保持待处理：{task_uuid}")
            return aggregate

    def project_material_source_admission(
        self,
        task_uuid: str,
        bindings: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any]:
        """原子提交成功任务物料准入（TaskMaterialAdmission）的逐来源结果。

        参数：``task_uuid`` 是父工作流任务（WorkflowTask）身份；``bindings`` 按
        物料来源节点 UUID 提供已预留的 ``uuid`` 与
        ``resource_template_uuid``。返回：全部来源作业已直接变为 ``succeeded``
        的标准聚合；若没有普通动作，父任务也直接成功。异常：绑定集合、字段、
        状态或重放载荷冲突时抛出 ``StoreConflict``，整笔事务零部分写入。
        """

        if not isinstance(bindings, Mapping):
            raise StoreConflict("物料来源绑定必须是对象")
        # ``normalized_bindings`` 隔离调用方容器并验证有类型物料占位符身份。
        normalized_bindings: dict[str, dict[str, str]] = {}
        for node_uuid, raw_binding in bindings.items():
            if not isinstance(raw_binding, Mapping):
                raise StoreConflict("物料来源绑定成员必须是对象")
            material_uuid = str(raw_binding.get("uuid") or "").strip()
            template_uuid = str(
                raw_binding.get("resource_template_uuid") or ""
            ).strip()
            if not node_uuid or not material_uuid or not template_uuid:
                raise StoreConflict("物料来源绑定身份不能为空")
            normalized_bindings[str(node_uuid)] = {
                "uuid": material_uuid,
                "resource_template_uuid": template_uuid,
            }

        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            job_rows = self._job_rows(connection, task_uuid)
            # ``source_rows`` 必须与成功准入一次提交的完整绑定集合严格相等。
            source_rows = [
                row for row in job_rows if row["executor_kind"] == "material_source"
            ]
            source_node_uuids = {str(row["workflow_node_uuid"]) for row in source_rows}
            if not source_rows or set(normalized_bindings) != source_node_uuids:
                raise StoreConflict(f"物料来源绑定集合不完整：{task_uuid}")
            if task_row["status"] not in {"pending", "running", "succeeded"}:
                raise StoreConflict(f"任务不能提交物料来源结果：{task_uuid}")

            self._project_material_binding_params(
                connection,
                task_row=task_row,
                job_rows=job_rows,
                bindings=normalized_bindings,
            )
            projected_at = utc_now()
            changed = False
            for row in source_rows:
                node_uuid = str(row["workflow_node_uuid"])
                return_info = {"material": normalized_bindings[node_uuid]}
                return_info_json = _encode_json_field(
                    return_info,
                    field_name="return_info",
                )
                if row["status"] == "succeeded":
                    if _decode_json_field(row["return_info"], fallback={}) != return_info:
                        raise StoreConflict(f"物料来源终态载荷冲突：{row['uuid']}")
                    continue
                if row["status"] != "pending":
                    raise StoreConflict(f"物料来源作业不能成功：{row['uuid']}")
                updated_jobs = connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'succeeded', return_info = ?, error_info = '[]',
                        finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                    """,
                    (return_info_json, projected_at, projected_at, row["uuid"]),
                ).rowcount
                if updated_jobs != 1:
                    raise StoreConflict(f"物料来源作业状态发生并发变化：{row['uuid']}")
                changed = True
                WorkflowStore._append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=str(row["uuid"]),
                    kind="job_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=projected_at,
                )

            # 没有普通动作表示任务业务目标就是完成供料绑定；协调器工作不经历
            # ``running``，也不产生设备执行开始时间。
            ordinary_rows = [
                row for row in job_rows if row["executor_kind"] != "material_source"
            ]
            if not ordinary_rows and task_row["status"] == "pending":
                updated_tasks = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = 'succeeded', finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                    """,
                    (projected_at, projected_at, task_uuid),
                ).rowcount
                if updated_tasks != 1:
                    raise StoreConflict(f"来源任务终态发生并发变化：{task_uuid}")
                changed = True
                WorkflowStore._append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="succeeded",
                    now=projected_at,
                )
            if changed:
                self._append_invalidation(
                    connection,
                    task_uuid=task_uuid,
                    now=projected_at,
                )
            return self._aggregate(connection, task_uuid)

    @staticmethod
    def _project_material_binding_params(
        connection: sqlite3.Connection,
        *,
        task_row: sqlite3.Row,
        job_rows: Sequence[sqlite3.Row],
        bindings: Mapping[str, Mapping[str, str]],
    ) -> None:
        """把自动库存选择结果原子写入既有普通动作作业参数。

        参数：连接、任务行和作业行属于同一工作流存储（WorkflowStore）事务；
        ``bindings`` 是已整组占用的逐来源物料（Material）身份。返回无。异常：
        计划目标、作业状态或既有参数冲突时抛 ``StoreConflict``，来源成功状态与
        参数写入一起回滚。
        """

        plan = _decode_json_field(task_row["execution_plan"], fallback={})
        # 早期兼容任务没有冻结绑定目标；它们仍只投影来源结果，不补写动作参数。
        if plan == {}:
            return
        raw_nodes = plan.get("nodes") if isinstance(plan, Mapping) else None
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
            raise StoreConflict("执行计划节点必须是数组")
        jobs_by_node = {str(row["workflow_node_uuid"]): row for row in job_rows}
        raw_edges = plan.get("edges", [])
        if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
            raise StoreConflict("执行计划边必须是数组")
        inferred_targets: dict[str, list[dict[str, str]]] = {}
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, Mapping):
                raise StoreConflict("执行计划边必须是对象")
            if (
                raw_edge.get("dependency_only") is True
                or raw_edge.get("source_type") != "ResourceSlot"
                or raw_edge.get("target_type") != "ResourceSlot"
            ):
                continue
            source_uuid = str(raw_edge.get("source_node_uuid") or "").strip()
            target_uuid = str(raw_edge.get("target_node_uuid") or "").strip()
            param_key = final_target_data_key(
                str(raw_edge.get("target_data_key") or "")
            )
            if source_uuid and target_uuid and param_key:
                inferred_targets.setdefault(source_uuid, []).append(
                    {"workflow_node_uuid": target_uuid, "param_key": param_key}
                )
        claimed_targets: set[tuple[str, str]] = set()
        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping) or raw_node.get("kind") != "material_source":
                continue
            source_uuid = str(raw_node.get("uuid") or "")
            binding = bindings.get(source_uuid)
            if binding is None:
                raise StoreConflict(f"物料来源缺少运行绑定：{source_uuid}")
            raw_targets = raw_node.get("material_binding_targets", [])
            if not isinstance(raw_targets, Sequence) or isinstance(
                raw_targets, (str, bytes)
            ):
                raise StoreConflict("物料来源绑定目标必须是数组")
            combined_targets = [*raw_targets, *inferred_targets.get(source_uuid, [])]
            for raw_target in combined_targets:
                if not isinstance(raw_target, Mapping):
                    raise StoreConflict("物料来源绑定目标必须是对象")
                target_uuid = str(raw_target.get("workflow_node_uuid") or "").strip()
                param_key = str(raw_target.get("param_key") or "").strip()
                target = (target_uuid, param_key)
                if not target_uuid or not param_key:
                    raise StoreConflict("物料来源绑定目标不能为空")
                if target in claimed_targets:
                    continue
                claimed_targets.add(target)
                target_row = jobs_by_node.get(target_uuid)
                if target_row is None or target_row["executor_kind"] == "material_source":
                    raise StoreConflict(f"物料来源绑定目标不是普通动作：{target_uuid}")
                # 多个物料来源（MaterialSource）可以把不同参数绑定到同一个动作。
                # ``jobs_by_node`` 来自事务开始时的快照；每次写入前重新读取目标，
                # 否则后一个来源会用陈旧参数覆盖前一个来源刚提交的绑定。
                current_target_row = TaskRuntimeProjection._job_row(
                    connection,
                    str(target_row["uuid"]),
                )
                param = _decode_json_field(current_target_row["param"], fallback={})
                if not isinstance(param, Mapping):
                    raise StoreConflict(
                        f"工作流节点作业参数不是对象：{current_target_row['uuid']}"
                    )
                updated_param = dict(param)
                material_reference = {"uuid": str(binding["uuid"])}
                existing = updated_param.get(param_key)
                if existing is not None and existing != material_reference:
                    raise StoreConflict(
                        f"物料来源绑定与作业参数冲突：{current_target_row['uuid']}"
                    )
                if existing == material_reference:
                    continue
                updated_param[param_key] = material_reference
                changed = connection.execute(
                    "UPDATE workflow_node_job SET param = ?, update_time = ? "
                    "WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL",
                    (
                        _encode_json_field(updated_param, field_name="param"),
                        utc_now(),
                        current_target_row["uuid"],
                    ),
                ).rowcount
                if changed != 1:
                    raise StoreConflict(
                        "物料来源绑定目标状态发生并发变化："
                        f"{current_target_row['uuid']}"
                    )

    def project_pre_dispatch(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        resolved_param: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在物理派发前原子推进目标作业及父任务。

        参数：``task_uuid`` 是父工作流任务（WorkflowTask）身份；``job_uuid`` 是
        即将派发的工作流节点作业（WorkflowNodeJob）身份；``resolved_param``
        是已投影全部父节点输出的最终参数。返回：提交后的标准聚合。异常：
        身份不匹配或状态转换冲突时抛出 ``StoreConflict``；身份缺失时抛出
        ``StoreNotFound``。同一派发意图重放时零写入。
        """

        with self._store.transaction() as connection:
            # ``job_row`` 是本次物理派发意图所指向的唯一作业。
            job_row = self._job_row(connection, job_uuid)
            if job_row["workflow_task_uuid"] != task_uuid:
                raise StoreConflict(f"作业不属于指定任务：{job_uuid}/{task_uuid}")
            task_row = self._task_row(connection, task_uuid)
            if job_row["status"] == "dispatched" and task_row["status"] == "running":
                return self._aggregate(connection, task_uuid)
            if job_row["status"] != "pending":
                raise StoreConflict(f"作业不能进入 dispatched：{job_uuid}")
            if task_row["status"] not in {"pending", "running"}:
                raise StoreConflict(f"任务不能开始派发：{task_uuid}")
            param_json = (
                job_row["param"]
                if resolved_param is None
                else _encode_json_field(resolved_param, field_name="resolved_param")
            )

            # ``projected_at`` 是同一事务内任务与作业共享的投影时间。
            projected_at = utc_now()
            updated_jobs = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'dispatched', param = ?, update_time = ?
                WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                """,
                (param_json, projected_at, job_uuid),
            ).rowcount
            if updated_jobs != 1:
                raise StoreConflict(f"作业派发前状态发生并发变化：{job_uuid}")
            if task_row["status"] == "pending":
                updated_tasks = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        update_time = ?
                    WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
                    """,
                    (projected_at, projected_at, task_uuid),
                ).rowcount
                if updated_tasks != 1:
                    raise StoreConflict(f"任务启动状态发生并发变化：{task_uuid}")
                WorkflowStore._append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status="pending",
                    to_status="running",
                    now=projected_at,
                )
            WorkflowStore._append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="pending",
                to_status="dispatched",
                now=projected_at,
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=projected_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_job_finished(
        self,
        *,
        job_uuid: str,
        scheduler_state: str,
        return_info: Mapping[str, Any] | None = None,
        error_info: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """投影工作流节点作业（WorkflowNodeJob）的明确业务结果。

        参数：``job_uuid`` 是作业稳定身份；``scheduler_state`` 只接受本地
        ``success`` 或 ``failed``；``return_info`` 是成功结果对象；``error_info``
        是错误详情序列。返回：提交后的标准任务/作业聚合。异常：未知状态、结果
        类型、终态或载荷冲突抛出 ``StoreConflict``；身份缺失抛出
        ``StoreNotFound``。相同终态和载荷的投递重放（DeliveryReplay）零写入。
        """

        if scheduler_state not in _FINISHED_STATE_MAP:
            raise StoreConflict(f"不支持的本地完成状态：{scheduler_state}")
        if return_info is not None and not isinstance(return_info, Mapping):
            raise StoreConflict("return_info 必须是对象")
        if error_info is not None and (
            not isinstance(error_info, Sequence)
            or isinstance(error_info, (str, bytes, bytearray))
        ):
            raise StoreConflict("error_info 必须是序列")

        # ``target_job_status`` 是 Backend-shaped 合同采用的标准作业终态。
        target_job_status = _FINISHED_STATE_MAP[scheduler_state]
        normalized_return_info = dict(return_info or {})
        normalized_error_info = list(error_info or [])
        return_info_json = _encode_json_field(
            normalized_return_info,
            field_name="return_info",
        )
        error_info_json = _encode_json_field(
            normalized_error_info,
            field_name="error_info",
        )

        with self._store.transaction() as connection:
            job_row = self._job_row(connection, job_uuid)
            task_uuid = job_row["workflow_task_uuid"]
            task_row = self._task_row(connection, task_uuid)
            current_job_status = job_row["status"]
            if current_job_status == target_job_status:
                same_return_info = (
                    _decode_json_field(
                        job_row["return_info"],
                        fallback={},
                    )
                    == normalized_return_info
                )
                same_error_info = (
                    _decode_json_field(
                        job_row["error_info"],
                        fallback=[],
                    )
                    == normalized_error_info
                )
                if same_return_info and same_error_info:
                    return self._aggregate(connection, task_uuid)
                raise StoreConflict(f"作业终态载荷冲突：{job_uuid}")
            if current_job_status in _TERMINAL_JOB_STATES:
                raise StoreConflict(f"作业终态冲突：{job_uuid}")
            if current_job_status not in {"dispatched", "running"}:
                raise StoreConflict(f"作业尚未派发，不能完成：{job_uuid}")
            if task_row["status"] not in {"running", "failed"}:
                raise StoreConflict(f"父任务状态不接受作业结果：{task_uuid}")

            # ``finished_at`` 是明确作业结果落盘的统一完成时间。
            finished_at = utc_now()
            updated_jobs = connection.execute(
                """
                UPDATE workflow_node_job
                SET status = ?, return_info = ?, error_info = ?,
                    finished_at = ?, update_time = ?
                WHERE uuid = ? AND status IN ('dispatched', 'running')
                  AND deleted_at IS NULL
                """,
                (
                    target_job_status,
                    return_info_json,
                    error_info_json,
                    finished_at,
                    finished_at,
                    job_uuid,
                ),
            ).rowcount
            if updated_jobs != 1:
                raise StoreConflict(f"作业完成状态发生并发变化：{job_uuid}")
            WorkflowStore._append_runtime_event(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status=str(current_job_status),
                to_status=target_job_status,
                now=finished_at,
            )

            job_rows = self._job_rows(connection, task_uuid)
            # ``job_statuses`` 是决定父任务业务终态的完整兄弟作业状态集合。
            job_statuses = [row["status"] for row in job_rows]
            target_task_status: str | None = None
            if task_row["status"] != "failed":
                if "failed" in job_statuses:
                    target_task_status = "failed"
                elif all(status == "succeeded" for status in job_statuses):
                    target_task_status = "succeeded"
            if target_task_status is not None:
                updated_tasks = connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = ?, finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status = 'running' AND deleted_at IS NULL
                    """,
                    (target_task_status, finished_at, finished_at, task_uuid),
                ).rowcount
                if updated_tasks != 1:
                    raise StoreConflict(f"任务终态发生并发变化：{task_uuid}")
                WorkflowStore._append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    kind="task_transition",
                    from_status=str(task_row["status"]),
                    to_status=target_task_status,
                    now=finished_at,
                )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=finished_at,
            )
            return self._aggregate(connection, task_uuid)

    def project_cancellation(self, task_uuid: str) -> dict[str, Any]:
        """把一个活动任务及其未完成作业原子投影为已取消。"""

        with self._store.transaction() as connection:
            task_row = self._task_row(connection, task_uuid)
            current_task_status = str(task_row["status"])
            if current_task_status == "canceled":
                return self._aggregate(connection, task_uuid)
            if current_task_status not in {"pending", "running"}:
                raise StoreConflict(f"任务不能取消：{task_uuid}")

            canceled_at = utc_now()
            job_rows = self._job_rows(connection, task_uuid)
            for job_row in job_rows:
                current_job_status = str(job_row["status"])
                if current_job_status not in _ACTIVE_JOB_STATES:
                    continue
                updated_jobs = connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'canceled', finished_at = ?, update_time = ?
                    WHERE uuid = ?
                      AND status IN ('pending', 'dispatched', 'running')
                      AND deleted_at IS NULL
                    """,
                    (canceled_at, canceled_at, job_row["uuid"]),
                ).rowcount
                if updated_jobs != 1:
                    raise StoreConflict(
                        f"作业取消状态发生并发变化：{job_row['uuid']}"
                    )
                WorkflowStore._append_runtime_event(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=str(job_row["uuid"]),
                    kind="job_transition",
                    from_status=current_job_status,
                    to_status="canceled",
                    now=canceled_at,
                )

            updated_tasks = connection.execute(
                """
                UPDATE workflow_task
                SET status = 'canceled', finished_at = ?, update_time = ?
                WHERE uuid = ? AND status IN ('pending', 'running')
                  AND deleted_at IS NULL
                """,
                (canceled_at, canceled_at, task_uuid),
            ).rowcount
            if updated_tasks != 1:
                raise StoreConflict(f"任务取消状态发生并发变化：{task_uuid}")
            WorkflowStore._append_runtime_event(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status=current_task_status,
                to_status="canceled",
                now=canceled_at,
            )
            self._append_invalidation(
                connection,
                task_uuid=task_uuid,
                now=canceled_at,
            )
            return self._aggregate(connection, task_uuid)


__all__ = ["TaskRuntimeProjection"]
