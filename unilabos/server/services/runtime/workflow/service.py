"""本地 Backend-shaped Workflow Authority 的应用服务。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import stat
import struct
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

try:  # Linux authoring CAS 使用 file lease；Windows 仍需支持 Workflow Runtime。
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on Windows CI/runtime
    fcntl = None  # type: ignore[assignment]

from pydantic import ValidationError

from unilabos.server.backend.scheduler.authority import SchedulerAuthorityProfile
from unilabos.protocol.materials import InventoryRequirement
from unilabos.server.services.runtime.workflow.execution_plan_graph import (
    CompositeExecutionPlanNormalizer,
)
from unilabos.protocol.utils.workflow_validation import GraphValidationError, validate_graph
from unilabos.protocol.utils.workflow_hierarchy import (
    HierarchyError,
    execution_parents,
    lift_edge,
)
from unilabos.protocol.utils.json_codec import encode_json, strict_json_equal
from unilabos.protocol.runtime.workflow import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)
from unilabos.server.services.runtime.workflow.errors import (
    StoreAuthoringConflict,
    StoreConflict,
    StoreNotFound,
    StoreRevisionConflict,
)
from unilabos.server.services.runtime.workflow.store import WorkflowStore, utc_now
from unilabos.server.services.runtime.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.server.services.runtime.workflow.composite_expansion import (
    CompositeAuthoring,
)
from unilabos.server.services.runtime.workflow.entry_parameters import (
    EntryParameterError,
    declared_input_contract,
    resolve_declared_output,
    resolve_task_input,
    substitute_entry_references,
    validate_entry_value,
)
from unilabos.server.services.runtime.workflow.publication import (
    PUBLICATION_MODULE,
    PUBLICATION_TEMPLATE_AUTHORITY,
    FrozenSnapshotProvider,
    PublicationError,
    PublicationProjection,
    PublicationResolver,
    authoring_view,
    base_template_uuids,
    freeze_publication_snapshot,
    merged_invocation_entities,
    nested_pins,
    project_publication,
    publication_source_hash,
    publication_summary,
)
from unilabos.server.services.runtime.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)
from unilabos.server.database.sqlite_domain import DomainDatabase

logger = logging.getLogger(__name__)

_ERRORS = {
    "invalid_input": (400, "提交内容格式不正确"),
    "not_found": (404, "请求的资源不存在"),
    "conflict": (409, "资源已发生冲突，请刷新后重试"),
    "workflow_not_found": (404, "工作流不存在或已被删除"),
    "draft_hash_conflict": (
        409,
        "草稿已被其他程序修改，请查看差异后再保存",
    ),
    "workflow_revision_conflict": (
        409,
        "工作流已在其他位置更新，请刷新并重新确认本次修改",
    ),
    "candidate_hash_conflict": (
        409,
        "预览结果已变化，请重新检查 DAG 和源码差异",
    ),
    "template_catalog_conflict": (
        409,
        "设备动作模板已更新，请重新编译并检查工作流",
    ),
    "candidate_not_ready": (409, "当前草稿尚未生成可应用的工作流"),
    "draft_invalid": (422, "草稿存在错误，修复后才能应用"),
    "candidate_invalid": (422, "工作流校验失败，请检查节点、连线和输入输出"),
    "template_catalog_unavailable": (
        503,
        "设备动作模板暂不可用，请稍后重试",
    ),
    "workflow_template_not_found": (404, "工作流模板不存在或已随设备包移除"),
    "workflow_template_unavailable": (
        503,
        "工作流模板暂不可用（注册表权威未就绪）",
    ),
    "template_binding_invalid": (
        422,
        "工作流模板的角色未能绑定到设备，请在 bindings 中指定",
    ),
    "site_binding_invalid": (422, "工作流 Site 绑定未完成"),
    "local_task_authority_forbidden": (
        409,
        "当前调度权威运行模式不允许创建本地可执行工作流任务",
    ),
    "manual_confirmation_not_assignee": (
        409,
        "确认人不在该确认单的指派名单中，请以被指派的用户身份确认",
    ),
    "manual_confirmation_decided": (409, "该人工确认已被处理，请刷新查看结果"),
    "manual_confirmation_key_used": (
        409,
        "决策幂等键已被其他确认单使用，请换一个键重试",
    ),
    "internal_error": (500, "本地工作流服务出现错误，请重试或查看日志"),
}

# store 层 StoreConflict 文案 → 面向前端的稳定错误码（人工确认决策）
_MANUAL_CONFIRMATION_CONFLICTS = {
    "confirmed_by is not an assignee": "manual_confirmation_not_assignee",
    "manual confirmation already has another decision": "manual_confirmation_decided",
    "decision_idempotency_key already used": "manual_confirmation_key_used",
}
_HASH_TOKEN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NO_EXPECTED_HASH = object()
_F_SETOWN_EX = getattr(fcntl, "F_SETOWN_EX", 15) if fcntl is not None else 15
_F_OWNER_TID = 0
_LEASE_BREAK_SIGNAL = getattr(signal, "SIGRTMAX", signal.SIGTERM)
_WORKFLOW_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "revision",
    "description",
}
_NODE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_HANDLE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
    "description",
    "data_source",
    "data_key",
}
_WORKFLOW_REQUIRED_READ_FIELDS = _WORKFLOW_READ_FIELDS - {"description"}
_NODE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_uuid",
    "name",
    "status",
    "type",
    "pose",
    "param",
    "execution_policy",
    "disabled",
    "minimized",
}
_EDGE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "source_node_uuid",
    "target_node_uuid",
    "source_handle_uuid",
    "target_handle_uuid",
}
_NODE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
}
_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
}


class WorkflowError(RuntimeError):
    """面向前端的稳定 Workflow 错误。

    ``detail`` 追加在稳定文案之后（如具体哪个模板角色没绑到设备），错误码不变。
    """

    def __init__(self, code: str, *, detail: str = ""):
        status, message = _ERRORS[code]
        if detail:
            message = f"{message}：{detail}"
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class WorkflowConflict(WorkflowError):
    pass


class AuthoringCompiler(Protocol):
    compiler_version: str
    template_catalog_fingerprint: str

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: Dict[str, Any],
    ) -> CandidateCompilation: ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return encode_json(value, sort_keys=True)


def _source_ranges_fit(
    python_source: str,
    ranges: Iterable[Dict[str, Any]],
) -> bool:
    """检查每个从 1 开始的范围端点是否落在原始源码内。"""

    try:
        line_byte_lengths = [
            len(line.encode("utf-8")) for line in re.split(r"\r\n|\r|\n", python_source)
        ]
    except UnicodeEncodeError:
        return False

    def position_fits(line: int, column: int) -> bool:
        return (
            line <= len(line_byte_lengths) and column <= line_byte_lengths[line - 1] + 1
        )

    return all(
        position_fits(item["start_line"], item["start_column"])
        and position_fits(item["end_line"], item["end_column"])
        for item in ranges
    )


def _mtime_rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class WorkflowService(WorkflowStore):
    """Workflow 域单层服务：持连接（继承 WorkflowStore 的 SQL 事实层）、
    协调 package Draft 文件与编译器状态。

    生产传 RuntimeService 实例共享 runtime.db 连接；测试可传路径或
    ``:memory:``。
    """

    def __init__(
        self,
        database: DomainDatabase,
        *,
        compiler: Optional[AuthoringCompiler] = None,
        compiler_rebuilder: Optional[Callable[[], AuthoringCompiler]] = None,
        authority_profile: SchedulerAuthorityProfile = (
            SchedulerAuthorityProfile.LOCAL_SCHEDULER
        ),
    ):
        super().__init__(database)
        self._preflight_materials = None
        self._preflight_endpoints = None
        self._action_schema_resolver = None
        self.compiler = compiler
        self._compiler_rebuilder = compiler_rebuilder
        self._authority_profile = SchedulerAuthorityProfile.parse(authority_profile)
        self._locks_guard = threading.Lock()
        self._authoring_locks: Dict[str, threading.RLock] = {}
        self._task_submitter: Optional[Callable[[str], None]] = None
        self._task_controller: Optional[Callable[[str], None]] = None
        # Scheduler 绑定此回调消费 durable 人工确认决策；Service 本身不依赖调度器。
        self._manual_confirmation_resolver: Optional[
            Callable[[Dict[str, Any]], None]
        ] = None

    @property
    def authority_profile(self) -> SchedulerAuthorityProfile:
        return self._authority_profile

    def set_task_submitter(
        self,
        submitter: Optional[Callable[[str], None]],
    ) -> None:
        """绑定唯一运行 Adapter；定义/任务事实仍由本服务持有。"""

        self._task_submitter = submitter

    def set_manual_confirmation_resolver(
        self,
        resolver: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        """绑定人工确认决策消费者；决策事实始终先落 runtime.db。"""

        self._manual_confirmation_resolver = resolver

    def set_task_controller(self, controller: Optional[Callable[[str], None]]) -> None:
        """绑定持久化控制命令后的调度唤醒；HTTP 层不直接操作执行器。"""
        self._task_controller = controller

    def command_workflow_task(
        self, task_uuid: str, *, command_type: str,
        expected_revision: int, idempotency_key: str,
    ) -> Dict[str, Any]:
        if not self._authority_profile.can_create_local_workflow_task:
            raise WorkflowError("local_task_authority_forbidden")
        task_uuid = self.get_workflow_task(task_uuid)["uuid"]
        if (command_type not in {"step", "resume", "pause", "cancel"} or not idempotency_key.strip()
                or type(expected_revision) is not int or expected_revision < 0):
            raise WorkflowError("invalid_input")
        try:
            result = self.apply_task_command(task_uuid, command_type=command_type,
                                             expected_revision=expected_revision,
                                             idempotency_key=idempotency_key)
        except StoreConflict as exc:
            raise WorkflowConflict("conflict", detail=str(exc)) from None
        if self._task_controller is not None:
            try:
                self._task_controller(task_uuid)
            except Exception:
                # 命令已落库，执行器暂时不可用时不能误报提交失败；恢复时重新消费。
                logger.exception("任务 %s 控制已持久化，等待调度器恢复", task_uuid)
        return result

    def mark_workflow_node_job_cancel_requested(self, job_uuid: str) -> bool:
        return self.mark_job_cancel_requested(job_uuid)

    def _notify_manual_confirmation_resolver(
        self, confirmation: Dict[str, Any]
    ) -> None:
        resolver = self._manual_confirmation_resolver
        if resolver is None:
            return
        try:
            resolver(confirmation)
        except Exception:  # noqa: BLE001 - durable 决策不可因执行器暂时异常丢失
            logger.exception(
                "人工确认 %s 已持久化，但调度器消费失败",
                confirmation.get("uuid"),
            )

    # Workflow 与 Graph --------------------------------------------------

    def create_workflow(
        self,
        *,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        workflow_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            name = name.strip()
            if not name:
                raise ValueError("workflow name must not be blank")
            identity = validate_uuid(workflow_uuid or str(uuid4()))
            tags = normalize_json_array(tags)
            meta_data = normalize_json_object(meta_data)
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
            return super().create_workflow(
                workflow_uuid=identity,
                name=name,
                tags=tags,
                description=self._optional_text(description),
                meta_data=public_meta_data,
            )
        except (ValueError, ValidationError):
            raise WorkflowError("invalid_input") from None
        except StoreConflict:
            raise WorkflowConflict("conflict") from None

    def get_workflow(self, workflow_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return super().get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflows(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str = "",
    ) -> Dict[str, Any]:
        page, page_size = self._normalize_page(page, page_size)
        return super().list_workflows(page=page, page_size=page_size, name=name)

    def update_workflow(
        self,
        workflow_uuid: str,
        *,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        current = self.get_workflow(workflow_uuid)
        identity = current["uuid"]
        with self._authoring_lock(identity):
            current = self.get_workflow(identity)
            try:
                name = name.strip()
                if not name:
                    raise ValueError("workflow name must not be blank")
                tags = normalize_json_array(tags)
                public_meta_data = dict(normalize_json_object(meta_data))
            except (AttributeError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            public_meta_data.pop("unilab", None)
            if "unilab" in current["meta_data"]:
                public_meta_data["unilab"] = current["meta_data"]["unilab"]
            return super().update_workflow(
                identity,
                name=name,
                tags=tags,
                description=self._optional_text(description),
                meta_data=public_meta_data,
            )

    def delete_workflow(self, workflow_uuid: str) -> None:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            super().delete_workflow(identity)

    def get_graph(self, workflow_uuid: str) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        return self._validated_applied_backend_graph(
            super().get_graph(identity),
        )

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        """返回组合目录使用的同视图已应用工作流快照。"""

        identity = self.get_workflow(workflow_uuid)["uuid"]
        return super().get_published_workflow_snapshot(identity)

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite | Dict[str, Any]],
        edges: List[WorkflowEdgeWrite | Dict[str, Any]],
    ) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            try:
                node_values = [
                    item
                    if isinstance(item, WorkflowNodeWrite)
                    else WorkflowNodeWrite.model_validate(item)
                    for item in nodes
                ]
                edge_values = [
                    item
                    if isinstance(item, WorkflowEdgeWrite)
                    else WorkflowEdgeWrite.model_validate(item)
                    for item in edges
                ]
                return super().save_graph(
                    identity,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                    protect_reserved_metadata=True,
                )
            except ValidationError as exc:
                detail = "; ".join(
                    f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                    for item in exc.errors(include_input=False, include_url=False)[:5]
                )
                raise WorkflowError("invalid_input", detail=detail) from None
            except StoreRevisionConflict:
                raise WorkflowConflict("conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreConflict as exc:
                # 留下 protocol 图校验的具体原因；仅有笼统文案时前端/AI 都无法纠正节点。
                raise WorkflowError("invalid_input", detail=str(exc)) from None

    # 工作流 I/O、发布与组合调用 --------------------------------------------

    def declare_workflow_io(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        input_contract: Optional[Dict[str, Any]],
        output_contract: Optional[Dict[str, Any]],
        output_bindings: Dict[str, Any],
        node_input_bindings: Dict[str, Dict[str, Any]],
        node_params: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """声明工作流公共输入输出，写入服务端保留的 ``meta_data.unilab``。

        ``node_input_bindings`` 是完整替换：未列出的节点清空 input_bindings。绑定即该输入的
        唯一 Provider，节点 param 里同名静态值随之移除；``node_params`` 按节点合并进 param，
        用于解绑必填输入时同时给回静态值。
        """

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            graph = WorkflowStore.get_graph(self, identity)
            if graph["workflow"]["revision"] != revision:
                raise WorkflowConflict("conflict")
            try:
                bindings_by_node = {
                    validate_uuid(node_uuid): normalize_json_object(bindings)
                    for node_uuid, bindings in node_input_bindings.items()
                }
                params_by_node = {
                    validate_uuid(node_uuid): normalize_json_object(values)
                    for node_uuid, values in (node_params or {}).items()
                }
            except (AttributeError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            known = {node["uuid"] for node in graph["nodes"]}
            unknown = sorted((set(bindings_by_node) | set(params_by_node)) - known)
            if unknown:
                raise WorkflowError(
                    "invalid_input",
                    detail=f"I/O 声明引用了图中不存在的节点 {unknown[0]}",
                )
            handles = {item["uuid"]: item for item in graph["handle_templates"]}
            nodes = []
            for node in graph["nodes"]:
                meta_data = dict(node["meta_data"])
                unilab = dict(meta_data.get("unilab") or {})
                param = {
                    **(node.get("param") or {}),
                    **params_by_node.get(node["uuid"], {}),
                }
                bindings = bindings_by_node.get(node["uuid"])
                if bindings:
                    unilab["input_bindings"] = bindings
                    for handle_uuid in bindings:
                        handle = handles.get(handle_uuid)
                        if handle is not None:
                            param.pop(
                                self._final_target_data_key(
                                    self._handle_data_key(handle)
                                ),
                                None,
                            )
                else:
                    unilab.pop("input_bindings", None)
                if unilab:
                    meta_data["unilab"] = unilab
                else:
                    meta_data.pop("unilab", None)
                nodes.append({**node, "meta_data": meta_data, "param": param})
            workflow_meta = dict(graph["workflow"]["meta_data"])
            unilab = dict(workflow_meta.get("unilab") or {})
            unilab["input_contract"] = input_contract
            unilab["output_contract"] = output_contract
            unilab["output_bindings"] = output_bindings
            workflow_meta["unilab"] = unilab
            candidate = {
                **graph,
                "workflow": {**graph["workflow"], "meta_data": workflow_meta},
                "nodes": nodes,
            }
            try:
                workflow_io = validate_workflow_graph_io(candidate)
            except WorkflowIOValidationError as exc:
                raise WorkflowError(
                    "invalid_input",
                    detail=f"{exc.code}（{exc.path}）",
                ) from None
            # 落库规范化后的合同，发布与展开读取的是同一份形状。
            unilab["input_contract"] = workflow_io.input_contract.to_dict()
            unilab["output_contract"] = workflow_io.output_contract.to_dict()
            unilab["output_bindings"] = {
                name: dict(binding)
                for name, binding in workflow_io.output_bindings.items()
            }
            return self._save_reserved_graph(
                identity,
                revision=revision,
                workflow_meta_data=workflow_meta,
                nodes=nodes,
                edges=graph["edges"],
            )

    def publish_workflow(self, workflow_uuid: str, *, revision: int) -> Dict[str, Any]:
        """把当前修订冻结为不可变发布；同一修订重复发布返回既有发布。"""

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            existing = self.find_workflow_publication(identity, revision)
            if existing is not None:
                return self._publication_dto(existing)
            graph = WorkflowStore.get_graph(self, identity)
            if graph["workflow"]["revision"] != revision:
                raise WorkflowConflict("conflict", detail="只能发布工作流的当前修订")
            source_hash = publication_source_hash(graph)
            snapshot = freeze_publication_snapshot(graph, source_hash=source_hash)
            try:
                projection = project_publication(snapshot)
            except PublicationError as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            try:
                row = self.insert_workflow_publication(
                    workflow_uuid=identity,
                    revision=revision,
                    name=graph["workflow"]["name"],
                    source_hash=source_hash,
                    contract_digest=projection.contract_digest,
                    snapshot=snapshot,
                )
            except StoreConflict:
                raise WorkflowConflict("conflict") from None
            logger.info(
                "工作流 %s 已发布修订 %s（%s）", identity, revision, projection.contract_digest
            )
            return publication_summary(row, projection)

    def list_workflow_publications(self, workflow_uuid: str) -> List[Dict[str, Any]]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        return [
            self._publication_dto(row)
            for row in super().list_workflow_publications(identity)
        ]

    def list_published_workflows(self) -> List[Dict[str, Any]]:
        """每个工作流的最新发布及其投影节点模板（前端调色板）。"""

        return [
            self._publication_dto(row)
            for row in self.list_latest_workflow_publications()
        ]

    def insert_composite_invocation(
        self,
        parent_workflow_uuid: str,
        *,
        revision: int,
        child_workflow_uuid: str,
        child_revision: Optional[int] = None,
        invocation_uuid: Optional[str] = None,
        pose: Optional[Dict[str, Any]] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把子工作流的一次发布作为组合调用插入父图，并内联展开为普通分支。"""

        parent = self.get_workflow(parent_workflow_uuid)["uuid"]
        try:
            child = validate_uuid(child_workflow_uuid)
            invocation = validate_uuid(invocation_uuid or str(uuid4()))
            arguments = normalize_json_object(arguments or {})
            pose = normalize_json_object(pose or {})
        except (AttributeError, TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        if child == parent:
            raise WorkflowError(
                "invalid_input",
                detail="composite_recursive_reference（/composite/child_workflow_uuid）",
            )
        # 子工作流已删除时不再允许新插入；已插入的调用不受影响（快照不可变）。
        self.get_workflow(child)
        with self._authoring_lock(parent):
            graph = WorkflowStore.get_graph(self, parent)
            if graph["workflow"]["revision"] != revision:
                raise WorkflowConflict("conflict")
            selected = self.find_workflow_publication(child, child_revision)
            if selected is None:
                raise WorkflowError("not_found", detail="子工作流没有该发布修订")
            try:
                authoring, projection = self._composite_authoring(selected)
            except PublicationError as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            parent_unilab = graph["workflow"]["meta_data"].get("unilab") or {}
            expansion = authoring.compile_invocation(
                parent_workflow_uuid=parent,
                invocation_uuid=invocation,
                module=PUBLICATION_MODULE,
                symbol=projection.source.symbol,
                keyword_arguments=arguments,
                parent_input_contract=(
                    parent_unilab.get("input_contract")
                    or {"version": 1, "parameters": []}
                ),
            )
            if expansion.diagnostics:
                diagnostic = expansion.diagnostics[0]
                raise WorkflowError(
                    "invalid_input",
                    detail=f"{diagnostic['code']}（{diagnostic['path']}）",
                )
            nodes, edges, templates, handles = merged_invocation_entities(
                expansion,
                name=selected["name"],
                pose=pose,
            )
            if {node["uuid"] for node in nodes} & {
                node["uuid"] for node in graph["nodes"]
            } or {edge["uuid"] for edge in edges} & {
                edge["uuid"] for edge in graph["edges"]
            }:
                raise WorkflowConflict("conflict", detail="组合调用身份已存在于父图")
            saved = self._save_reserved_graph(
                parent,
                revision=revision,
                workflow_meta_data=graph["workflow"]["meta_data"],
                nodes=[*graph["nodes"], *nodes],
                edges=[*graph["edges"], *edges],
                node_templates=templates,
                handle_templates=handles,
            )
            return {
                "invocation_uuid": invocation,
                "contract_pin": dict(expansion.contract_pin),
                "graph": saved,
            }

    def _composite_authoring(
        self,
        selected: Dict[str, Any],
    ) -> Tuple[CompositeAuthoring, PublicationProjection]:
        """为一次插入装配封闭目录：选定发布 + 其嵌套调用固定的发布 + 当前普通模板。"""

        rows: Dict[str, Dict[str, Any]] = {selected["workflow_uuid"]: selected}
        pending = nested_pins(authoring_view(selected["snapshot"]))
        while pending:
            workflow_uuid, revision = pending.pop()
            known = rows.get(workflow_uuid)
            if known is not None:
                if known["revision"] != revision:
                    # 一个目录代际里每个工作流只能有一个发布修订
                    raise PublicationError(
                        "composite_catalog_mismatch",
                        "/child/nodes/composite/child_workflow_revision",
                    )
                continue
            row = self.find_workflow_publication(workflow_uuid, revision)
            if row is None:
                raise PublicationError("composite_child_not_found", "/child/workflow_uuid")
            rows[workflow_uuid] = row
            pending.extend(nested_pins(authoring_view(row["snapshot"])))
        projections = {
            workflow_uuid: project_publication(row["snapshot"])
            for workflow_uuid, row in rows.items()
        }
        views = {
            workflow_uuid: authoring_view(row["snapshot"])
            for workflow_uuid, row in rows.items()
        }
        base_templates, base_handles = self.list_catalog_templates(
            base_template_uuids(views.values())
        )
        try:
            catalog = AuthoringCatalogSnapshot.from_entities(
                [*base_templates, *(item.template for item in projections.values())],
                [
                    *base_handles,
                    *(handle for item in projections.values() for handle in item.handles),
                ],
            )
        except AuthoringCatalogError:
            raise PublicationError("composite_catalog_mismatch", "/catalog") from None
        authoring = CompositeAuthoring(
            snapshot_provider=FrozenSnapshotProvider(views),
            catalog=catalog,
            resolver=PublicationResolver(item.source for item in projections.values()),
        )
        return authoring, projections[selected["workflow_uuid"]]

    def _publication_dto(self, row: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return publication_summary(row, project_publication(row["snapshot"]))
        except (KeyError, TypeError, PublicationError):
            logger.exception("工作流发布 %s 的快照无法投影", row.get("uuid"))
            raise WorkflowError("internal_error") from None

    def _save_reserved_graph(self, workflow_uuid: str, **kwargs: Any) -> Dict[str, Any]:
        """保留元数据写图，错误映射与 ``save_graph`` 一致。"""

        try:
            return self.save_graph_with_reserved_metadata(
                workflow_uuid,
                template_authority_id=PUBLICATION_TEMPLATE_AUTHORITY,
                **kwargs,
            )
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                for item in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise WorkflowError("invalid_input", detail=detail) from None
        except StoreRevisionConflict:
            raise WorkflowConflict("conflict") from None
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict as exc:
            raise WorkflowError("invalid_input", detail=str(exc)) from None

    # WorkflowTask 与 WorkflowNodeJob -----------------------------------

    def configure_preflight(
        self, *, materials: Any = None,
        endpoints: Optional[Callable[[], list[dict[str, Any]]]] = None,
        action_schema_resolver: Optional[Callable[[str, str], Optional[dict[str, Any]]]] = None,
    ) -> None:
        """组合根注入只读库存与设备快照，不把 HTTP 依赖带入领域服务。"""
        self._preflight_materials = materials
        self._preflight_endpoints = endpoints
        self._action_schema_resolver = action_schema_resolver

    def preflight_workflow(self, **kwargs: Any) -> dict[str, Any]:
        from .run_preflight import build_run_preflight_report

        return build_run_preflight_report(self, **kwargs)

    def create_workflow_task(
        self,
        *,
        workflow_uuid: str,
        run_mode: str,
        target_node_uuid: Optional[str],
        input_value: Dict[str, Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._authority_profile.can_create_local_workflow_task:
            raise WorkflowError("local_task_authority_forbidden")
        workflow_uuid = self.get_workflow(workflow_uuid)["uuid"]
        run_mode = "normal" if run_mode == "" else run_mode
        if run_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        try:
            input_value = normalize_json_object(input_value)
            meta_data = normalize_json_object(meta_data)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        # 任务输入只承载工作流声明的入口参数（input_contract）；未声明合同的工作流
        # 仍只接受空对象。校验与补默认值在建任务事务内按同一份图完成。
        description = self._optional_text(description)
        # 幂等键可选：同一次提交（按钮重发、网络重试）复用同一键只建一个任务。指纹锁定
        # 运行语义 (workflow, run_mode, target, input)；同键不同语义按 conflict 拒绝。
        fingerprint = ""
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise WorkflowError("invalid_input")
            semantics: Dict[str, Any] = {
                "workflow_uuid": workflow_uuid,
                "run_mode": run_mode,
                "target_node_uuid": target_node_uuid,
            }
            if input_value:
                # 空输入不进指纹：与引入任务输入之前的既有任务保持同一指纹
                semantics["input"] = input_value
            fingerprint = _sha256(_canonical_json(semantics))
            existing = self.find_task_by_idempotency_key("workflow", idempotency_key)
            if existing is not None:
                if existing.get("request_fingerprint", "") != fingerprint:
                    raise WorkflowConflict("conflict")
                return existing
        resolved: Dict[str, Dict[str, Any]] = {}

        def build_input(graph: Dict[str, Any]) -> Dict[str, Any]:
            try:
                contract = declared_input_contract(graph["workflow"])
                resolved["input"] = resolve_task_input(contract, input_value)
            except EntryParameterError as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            return resolved["input"]

        try:
            task = self.create_task_with_jobs(
                workflow_uuid=workflow_uuid,
                task_uuid=str(uuid4()),
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                input_value={},
                input_builder=build_input,
                description=description,
                meta_data=meta_data,
                plan_builder=lambda graph: self._build_execution_plan(
                    graph,
                    run_mode=run_mode,
                    target_node_uuid=target_node_uuid,
                    entry_input=resolved["input"],
                ),
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except StoreConflict:
            if idempotency_key is not None:
                # 唯一索引兜底：并发同键提交，读回先到者
                existing = self.find_task_by_idempotency_key("workflow", idempotency_key)
                if existing is not None:
                    if existing.get("request_fingerprint", "") != fingerprint:
                        raise WorkflowConflict("conflict") from None
                    return existing
            raise WorkflowError("invalid_input") from None
        if self._task_submitter is not None:
            self._task_submitter(task["uuid"])
        return task

    def create_ad_hoc_device_action_task(
        self,
        *,
        device_id: str,
        action_name: str,
        action_type: str = "",
        param: Dict[str, Any],
        execution_policy: Optional[Dict[str, Any]] = None,
        execution_timeout_seconds: int = 0,
        idempotency_key: Optional[str] = None,
        description: Optional[str],
        meta_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """微前端单点设备动作：单 job 任务，复用整图任务的调度/历史/异常链路。

        request_fingerprint 锁定 (device, action, param) 语义；同 idempotency_key
        重复提交时指纹一致返回既有任务（幂等），不一致按 conflict 拒绝。
        """

        if not self._authority_profile.can_create_local_workflow_task:
            raise WorkflowError("local_task_authority_forbidden")
        device_id = str(device_id or "").strip()
        action_name = str(action_name or "").strip()
        if not device_id or not action_name:
            raise WorkflowError("invalid_input")
        try:
            param = normalize_json_object(param)
            meta_data = normalize_json_object(meta_data)
            policy = normalize_json_object(execution_policy or {})
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if execution_timeout_seconds < 0:
            raise WorkflowError("invalid_input")
        description = self._optional_text(description)
        fingerprint = _sha256(
            _canonical_json(
                {
                    "device_id": device_id,
                    "action_name": action_name,
                    "param": param,
                }
            )
        )
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not idempotency_key:
                raise WorkflowError("invalid_input")
        else:
            idempotency_key = str(uuid4())
        existing = self.find_task_by_idempotency_key(
            "ad_hoc_device_action", idempotency_key
        )
        if existing is not None:
            if existing.get("request_fingerprint", "") not in {"", fingerprint}:
                raise WorkflowConflict("conflict")
            return existing
        try:
            task = self.create_ad_hoc_task_with_job(
                task_uuid=str(uuid4()),
                node_uuid=str(uuid4()),
                device_id=device_id,
                action_name=action_name,
                action_type=str(action_type or ""),
                param=param,
                execution_policy=policy,
                execution_timeout_seconds=execution_timeout_seconds,
                description=description,
                meta_data=meta_data,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except StoreConflict:
            # 唯一索引兜底：并发同键提交，读回先到者
            existing = self.find_task_by_idempotency_key(
                "ad_hoc_device_action", idempotency_key
            )
            if existing is None:
                raise WorkflowError("internal_error") from None
            if existing.get("request_fingerprint", "") != fingerprint:
                raise WorkflowConflict("conflict") from None
            return existing
        if self._task_submitter is not None:
            self._task_submitter(task["uuid"])
        return task

    def list_template_action_references(self) -> List[Dict[str, Any]]:
        """活跃 workflow 节点对模板 action 的引用明细行（Registry 冲突检测用）。"""

        return super().list_template_action_references()

    def list_recoverable_workflow_tasks(self) -> List[Dict[str, Any]]:
        """列出本地 Adapter 可接管的非终态规范任务。"""

        if not self._authority_profile.can_recover_local_workflow_task:
            return []
        return self.list_recoverable_tasks()

    def prepare_workflow_task_execution(self, task_uuid: str) -> Dict[str, Any]:
        return self.prepare_task_execution(task_uuid)

    def settle_workflow_task_reconciliation(self, task_uuid: str) -> Dict[str, Any]:
        """重启后待裁决的 attempt 全部收敛后恢复任务控制态（见 store）。"""

        return self.settle_task_reconciliation(task_uuid)

    def mark_workflow_node_job_running(self, job_uuid: str) -> Dict[str, Any]:
        return self.mark_job_running(job_uuid)

    def mark_workflow_node_job_execution_unknown(self, job_uuid: str, reason: str) -> bool:
        return self.mark_job_execution_unknown(job_uuid, reason)

    def mark_workflow_node_job_decision_pending(
        self, job_uuid: str, report: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self.mark_job_decision_pending(job_uuid, report)

    def mark_workflow_node_job_decision_resumed(
        self, job_uuid: str, decision_id: str = ""
    ) -> Dict[str, Any]:
        return self.mark_job_decision_resumed(job_uuid, decision_id)

    def set_workflow_node_run_execution_timeout(
        self, run_uuid: str, seconds: int
    ) -> Dict[str, Any]:
        return self.set_node_run_execution_timeout(run_uuid, seconds)

    def record_workflow_node_job_terminal(
        self,
        job_uuid: str,
        *,
        status: str,
        return_info: Optional[Dict[str, Any]] = None,
        error_info: Optional[List[Dict[str, Any]]] = None,
        error_resolution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """attempt 终态 + 节点运行投影；返回 ``{"job", "run", "next_job"}``（见 store）。"""

        return self.record_job_terminal(
            job_uuid,
            status=status,
            return_info=return_info or {},
            error_info=error_info or [],
            error_resolution=error_resolution,
        )

    def begin_workflow_loop_iteration(
        self,
        loop_run_uuid: str,
        *,
        iteration: int,
        progress: Dict[str, Any],
        body_run_uuids: Sequence[str],
    ) -> Dict[str, Any]:
        """循环节点开始新一轮：循环体节点运行追加 attempt，返回 ``{"run", "next_jobs"}``（见 store）。"""

        return self.begin_loop_iteration(
            loop_run_uuid,
            iteration=iteration,
            progress=progress,
            body_run_uuids=body_run_uuids,
        )

    def close_workflow_node_run(self, run_uuid: str, *, status: str) -> Dict[str, Any]:
        return self.close_node_run(run_uuid, status=status)

    def finish_workflow_task(
        self,
        task_uuid: str,
        *,
        status: str,
        output: Optional[Dict[str, Any]] = None,
        error_info: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if status == "succeeded":
            # 声明了 output_contract 的工作流：任务输出按 output_bindings 求值；
            # 未声明时沿用调用方给的逐节点结果映射。
            try:
                task = self.get_task(task_uuid)
                declared = resolve_declared_output(
                    task.get("workflow_snapshot") or {},
                    self.list_node_runs(task_uuid, include_attempts=False),
                    task.get("input") or {},
                )
            except StoreNotFound:
                declared = None
            if declared is not None:
                output = declared
        return self.finish_task(
            task_uuid,
            status=status,
            output=output or {},
            error_info=error_info or [],
        )

    def edit_workflow_task_input(
        self,
        task_uuid: str,
        *,
        expected_revision: int,
        input_value: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """暂停时修改入口参数（ADR 0002）：只改尚未下发、且未被已下发节点用过的参数。"""

        if not self._authority_profile.can_create_local_workflow_task:
            raise WorkflowError("local_task_authority_forbidden")
        task = self.get_workflow_task(task_uuid)
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkflowError("invalid_input")
        key = str(idempotency_key or uuid4()).strip()
        if not key or len(key) > 200:
            raise WorkflowError("invalid_input")
        try:
            patch = normalize_json_object(input_value)
            contract = declared_input_contract(
                (task.get("workflow_snapshot") or {}).get("workflow") or {}
            )
            descriptors = {
                str(item["name"]): item for item in (contract or {}).get("parameters", [])
            }
            unknown = sorted(set(patch) - set(descriptors))
            if unknown:
                raise EntryParameterError("workflow_input_unknown", f"/input/{unknown[0]}")
            values = {
                name: validate_entry_value(descriptors[name], value)
                for name, value in patch.items()
            }
        except ValueError as exc:
            detail = str(exc) if isinstance(exc, EntryParameterError) else ""
            raise WorkflowError("invalid_input", detail=detail) from None
        try:
            result = self.edit_task_input(
                task["uuid"],
                expected_revision=expected_revision,
                values=values,
                idempotency_key=key,
            )
        except StoreRevisionConflict:
            raise WorkflowConflict("conflict") from None
        except StoreConflict as exc:
            raise WorkflowConflict("conflict", detail=str(exc)) from None
        if self._task_controller is not None:
            try:
                # 唤醒调度器：未下发节点按库内参数重载
                self._task_controller(task["uuid"])
            except Exception:
                logger.exception("任务 %s 入口参数已修改，等待调度器恢复", task["uuid"])
        return result

    def get_workflow_task(self, task_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self.get_task(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: Optional[str] = None,
        status: str = "",
        cleanup_status: str = "",
    ) -> Dict[str, Any]:
        page, page_size = self._normalize_page(page, page_size)
        if workflow_uuid is not None:
            try:
                workflow_uuid = validate_uuid(workflow_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        status = status.strip().lower()
        cleanup_status = cleanup_status.strip().lower()
        if status and status not in {
            "pending",
            "running",
            "canceling",
            "succeeded",
            "failed",
            "canceled",
            "timeout",
        }:
            raise WorkflowError("invalid_input")
        if cleanup_status and cleanup_status not in {
            "none",
            "pending",
            "canceling",
            "settled",
            "requires_attention",
        }:
            raise WorkflowError("invalid_input")
        return self.list_tasks(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            status=status,
            cleanup_status=cleanup_status,
        )

    def list_workflow_node_runs(self, task_uuid: str) -> List[Dict[str, Any]]:
        """节点运行视图：每节点一条，当前 attempt 的结果 + ``attempts`` 历史。"""

        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self.list_node_runs(identity)

    def get_workflow_node_run(self, run_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(run_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self.get_node_run(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_node_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        """attempt（物理执行）平铺视图。"""

        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self.list_jobs(identity)

    def get_workflow_node_job(self, job_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self.get_job(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    @staticmethod
    def _normalize_runtime_window(
        limit: Optional[int], offset: int
    ) -> tuple[Optional[int], int]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WorkflowError("invalid_input")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise WorkflowError("invalid_input")
        return limit, offset

    def list_task_manual_confirmations(
        self, task_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        limit, offset = self._normalize_runtime_window(limit, offset)
        # 读回时顺手收敛已到期单，保证 UI 不会永久看到 pending。
        self.expire_workflow_manual_confirmations()
        return self.list_manual_confirmations(
            identity, limit=limit, offset=offset
        )

    def get_workflow_manual_confirmation(
        self, confirmation_uuid: str
    ) -> Dict[str, Any]:
        try:
            identity = validate_uuid(confirmation_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self.get_manual_confirmation(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def open_workflow_manual_confirmation(
        self,
        job_uuid: str,
        *,
        param: Dict[str, Any],
        description: Optional[str] = None,
        meta_data: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[int] = 3600,
        confirmation_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为 scheduler 暂停的 manual_confirm attempt 建立确认单。"""

        try:
            result = self.open_manual_confirmation(
                job_uuid,
                param=param,
                description=description,
                meta_data=meta_data,
                timeout_seconds=timeout_seconds,
                confirmation_uuid=confirmation_uuid,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except (StoreConflict, TypeError, ValueError):
            raise WorkflowConflict("conflict") from None
        return result

    def decide_workflow_manual_confirmation(
        self,
        confirmation_uuid: str,
        *,
        action: str,
        confirmed_by: Optional[str] = None,
        comment: Optional[str] = None,
        decision_idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录人工确认决策，并通知当前 scheduler（若已装配）。"""

        try:
            identity = validate_uuid(confirmation_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            result = self.decide_manual_confirmation(
                identity,
                action=action,
                confirmed_by=confirmed_by,
                comment=comment,
                decision_idempotency_key=decision_idempotency_key,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict as exc:
            raise WorkflowConflict(
                _MANUAL_CONFIRMATION_CONFLICTS.get(str(exc), "conflict")
            ) from None
        except (TypeError, ValueError):
            raise WorkflowConflict("conflict") from None
        self._notify_manual_confirmation_resolver(result)
        return result

    def expire_workflow_manual_confirmations(
        self, *, now: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        try:
            result = self.expire_due_manual_confirmations(now=now)
        except (StoreConflict, TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        for confirmation in result:
            self._notify_manual_confirmation_resolver(confirmation)
        return result

    def list_task_interventions(
        self, task_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        limit, offset = self._normalize_runtime_window(limit, offset)
        return self.list_interventions(identity, limit=limit, offset=offset)

    def list_node_job_results(
        self, job_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        identity = self.get_workflow_node_job(job_uuid)["uuid"]
        limit, offset = self._normalize_runtime_window(limit, offset)
        return self.list_job_results(identity, limit=limit, offset=offset)

    def list_node_job_feedback_history(
        self, job_uuid: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        identity = self.get_workflow_node_job(job_uuid)["uuid"]
        limit, offset = self._normalize_runtime_window(limit, offset)
        return self.list_job_feedback_history(
            identity, limit=limit, offset=offset
        )

    def _build_execution_plan(
        self,
        graph: Dict[str, Any],
        *,
        run_mode: str,
        target_node_uuid: Optional[str],
        entry_input: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        templates = {
            template["uuid"]: template for template in graph.get("node_templates", [])
        }
        handles = {
            handle["uuid"]: handle for handle in graph.get("handle_templates", [])
        }
        graph_nodes = graph["nodes"]
        node_index = {node["uuid"]: node for node in graph_nodes}
        graph_edges, composite_params = (
            CompositeExecutionPlanNormalizer().flatten_composite_edges(
                nodes=node_index,
                edges=graph["edges"],
                handles=handles,
            )
        )
        if run_mode == "single_node" and target_node_uuid is not None:
            selected_node = next(
                (node for node in graph_nodes if node["uuid"] == target_node_uuid),
                None,
            )
            if selected_node is None or selected_node["disabled"]:
                raise StoreConflict("single_node target is not enabled")
            graph_nodes = [selected_node]
            graph_edges = []

        def stable_key(node_uuid: str) -> Tuple[str, str]:
            node = enabled[node_uuid]
            return str(node.get("create_time") or ""), node_uuid

        enabled: Dict[str, Dict[str, Any]] = {}
        node_kinds: Dict[str, str] = {}
        all_kinds: Dict[str, str] = {}
        invocations: set[str] = set()
        for node in graph_nodes:
            template = templates.get(node.get("workflow_node_template_uuid"))
            raw_kind = (
                template.get("node_type") if template is not None else node["type"]
            )
            if str(raw_kind).strip().lower() == "workflow":
                # Composite 调用节点只用于画布、追溯和边界映射，不拥有 Job。
                invocations.add(node["uuid"])
                continue
            kind = self._executor_kind(raw_kind)
            all_kinds[node["uuid"]] = kind
            if self._node_or_ancestor_disabled(node, node_index) or kind == "group":
                continue
            enabled[node["uuid"]] = node
            node_kinds[node["uuid"]] = kind

        # 循环容器层级：跨边界的依赖提升到同级，拓扑序/环检测按层级进行
        try:
            parents = execution_parents(node_index, all_kinds, enabled=enabled)
        except HierarchyError as exc:
            raise StoreConflict(str(exc)) from exc

        def lifted(source: str, target: str) -> Tuple[str, str]:
            try:
                return lift_edge(parents, source, target)
            except HierarchyError as exc:
                raise StoreConflict(str(exc)) from exc

        indegree = {node_uuid: 0 for node_uuid in enabled}
        outgoing: Dict[str, List[str]] = defaultdict(list)
        ordering_pairs: set[Tuple[str, str]] = set()

        def add_ordering(source: str, target: str) -> None:
            pair = lifted(source, target)
            if pair[0] == pair[1] or pair in ordering_pairs:
                return
            ordering_pairs.add(pair)
            outgoing[pair[0]].append(pair[1])
            indegree[pair[1]] += 1

        planned_edges: List[Dict[str, Any]] = []
        for edge in graph_edges:
            source = edge["source_node_uuid"]
            target = edge["target_node_uuid"]
            if source not in enabled or target not in enabled:
                continue
            source_handle = handles.get(edge["source_handle_uuid"])
            target_handle = handles.get(edge["target_handle_uuid"])
            if source_handle is None or target_handle is None:
                raise StoreConflict("workflow edge references a missing handle")
            add_ordering(source, target)
            planned_edge = {
                "uuid": edge["uuid"],
                "source_node_uuid": source,
                "target_node_uuid": target,
                "source_handle_uuid": edge["source_handle_uuid"],
                "target_handle_uuid": edge["target_handle_uuid"],
                "source_data_key": self._handle_data_key(source_handle),
                "target_data_key": self._handle_data_key(target_handle),
                "source_type": str(source_handle.get("type") or "").strip(),
                "target_type": str(target_handle.get("type") or "").strip(),
            }
            if self._dependency_only(source_handle):
                planned_edge["dependency_only"] = True
            planned_edges.append(planned_edge)

        # execution_policy.depends_on：无 handle 数据流的纯执行序依赖（@workflow 声明式
        # 步骤、编排画布的顺序连线）。不生成计划边，但参与拓扑排序与环检测，
        # 这样 topological_index / 节点运行顺序与调度器实际的 DAG 一致。
        # 组合调用节点没有 Job：depends_on 指向它等价于依赖它展开出的全部可执行后代；
        # 它自己的 depends_on 施加到这些后代上（与 ready 连线经边界映射平面化同义）。
        composite_members: Dict[str, List[str]] = defaultdict(list)
        for node_uuid in enabled:
            seen: set[str] = set()
            parent = node_index[node_uuid].get("parent_uuid")
            while parent is not None and str(parent) in node_index:
                identity = str(parent)
                if identity in seen:
                    break
                seen.add(identity)
                if identity in invocations:
                    composite_members[identity].append(node_uuid)
                parent = node_index[identity].get("parent_uuid")

        def upstream_nodes(upstream: Any) -> List[str]:
            identity = str(upstream)
            return composite_members.get(identity, [identity])

        # 调度器按每个节点运行自己的 depends_on 建 DAG：含组合调用时把平面化后的上游写回。
        effective_depends_on: Dict[str, List[str]] = defaultdict(list)

        def depend(source_uuid: str, target_uuid: str) -> None:
            add_ordering(source_uuid, target_uuid)
            if source_uuid not in effective_depends_on[target_uuid]:
                effective_depends_on[target_uuid].append(source_uuid)

        for target_uuid, node in enabled.items():
            depends_on = (node.get("execution_policy") or {}).get("depends_on") or []
            if not isinstance(depends_on, list):
                continue
            for upstream in depends_on:
                for source_uuid in upstream_nodes(upstream):
                    if source_uuid not in enabled or source_uuid == target_uuid:
                        continue
                    depend(source_uuid, target_uuid)
        for invocation_uuid, members in composite_members.items():
            depends_on = (
                node_index[invocation_uuid].get("execution_policy") or {}
            ).get("depends_on") or []
            if not isinstance(depends_on, list):
                continue
            for upstream in depends_on:
                for source_uuid in upstream_nodes(upstream):
                    if source_uuid not in enabled or source_uuid in members:
                        continue
                    for member in members:
                        depend(source_uuid, member)

        # 分层拓扑排序：同级节点按依赖序，容器紧跟着它的循环体，再回到外层的后继。
        children: Dict[Optional[str], List[str]] = defaultdict(list)
        for node_uuid in enabled:
            children[parents.get(node_uuid)].append(node_uuid)

        def order_level(container: Optional[str]) -> List[str]:
            members = children.get(container, [])
            remaining = {uuid: indegree[uuid] for uuid in members}
            available = sorted(
                (uuid for uuid, degree in remaining.items() if degree == 0),
                key=stable_key,
            )
            result: List[str] = []
            placed = 0
            while available:
                node_uuid = available.pop(0)
                result.append(node_uuid)
                placed += 1
                if node_kinds[node_uuid] == "loop":
                    result.extend(order_level(node_uuid))
                for target in outgoing[node_uuid]:
                    remaining[target] -= 1
                    if remaining[target] == 0:
                        available.append(target)
                        available.sort(key=stable_key)
            if placed != len(members):
                raise StoreConflict("workflow graph contains a cycle")
            return result

        ordered = order_level(None)
        if len(ordered) != len(enabled):
            raise StoreConflict("workflow graph contains a cycle")
        if run_mode == "single_node":
            if target_node_uuid is None:
                if not ordered:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = ordered[0]
            if target_node_uuid not in enabled:
                raise StoreConflict("single_node target is not enabled")
            # 单节点运行一个循环：连同整个循环体
            selected = {target_node_uuid}
            for node_uuid in ordered:
                chain = parents.get(node_uuid)
                while chain is not None and chain not in selected:
                    chain = parents.get(chain)
                if chain is not None:
                    selected.add(node_uuid)
            ordered = [node_uuid for node_uuid in ordered if node_uuid in selected]
            enabled = {node_uuid: enabled[node_uuid] for node_uuid in ordered}
            planned_edges = [
                edge
                for edge in planned_edges
                if edge["source_node_uuid"] in selected and edge["target_node_uuid"] in selected
            ]

        planned_nodes: List[Dict[str, Any]] = []
        jobs: List[Dict[str, Any]] = []
        for index, node_uuid in enumerate(ordered):
            node = enabled[node_uuid]
            kind = node_kinds[node_uuid]
            if kind == "script":
                raise StoreConflict("script executor is not configured")
            policy = node.get("execution_policy") or {}
            if invocations and effective_depends_on.get(node_uuid):
                policy = {**policy, "depends_on": list(effective_depends_on[node_uuid])}
            template_uuid = node.get("workflow_node_template_uuid")
            template = templates.get(template_uuid)
            planned_param = dict(node.get("param") or {})
            planned_param.update(composite_params.get(node_uuid, {}))
            # 入口参数：画布节点的 param 顶层引用与模板节点的 Handle 绑定都替换为任务输入值
            planned_param, entry_bindings = substitute_entry_references(
                planned_param, entry_input or {}
            )
            node_unilab = (node.get("meta_data") or {}).get("unilab")
            input_bindings = (
                node_unilab.get("input_bindings") if isinstance(node_unilab, dict) else None
            )
            for handle_uuid, binding in (
                input_bindings.items() if isinstance(input_bindings, dict) else ()
            ):
                handle = handles.get(handle_uuid)
                parameter = binding.get("parameter") if isinstance(binding, dict) else None
                if handle is None or not isinstance(parameter, str):
                    continue
                data_key = self._final_target_data_key(self._handle_data_key(handle))
                entry_bindings[data_key] = parameter
                if parameter in (entry_input or {}):
                    planned_param[data_key] = (entry_input or {})[parameter]
            target_handles = sorted(
                (
                    handle
                    for handle in handles.values()
                    if template_uuid is not None
                    and handle.get("workflow_node_template_uuid") == template_uuid
                    and handle.get("io_type") == "target"
                ),
                key=lambda item: item["uuid"],
            )
            source_handle_uuids = sorted(
                handle["uuid"]
                for handle in handles.values()
                if template_uuid is not None
                and handle.get("workflow_node_template_uuid") == template_uuid
                and handle.get("io_type") == "source"
            )
            planned_node: Dict[str, Any] = {
                "uuid": node_uuid,
                "topological_index": index,
                "kind": kind,
                "param": planned_param,
                "execution_policy": policy,
                "inputs": [
                    {
                        "handle_uuid": handle["uuid"],
                        "data_key": self._final_target_data_key(
                            self._handle_data_key(handle)
                        ),
                        "type": str(handle.get("type") or "").strip(),
                        "required": bool(handle.get("required")),
                    }
                    for handle in target_handles
                ],
            }
            raw_inventory_requirements = (
                (node.get("meta_data") or {}).get("inventory_requirements") or []
            )
            if not isinstance(raw_inventory_requirements, list):
                raise StoreConflict("inventory_requirements must be a list")
            if raw_inventory_requirements:
                try:
                    planned_node["inventory_requirements"] = [
                        InventoryRequirement.model_validate(item).model_dump(
                            mode="json",
                            exclude_none=False,
                        )
                        for item in raw_inventory_requirements
                    ]
                except ValueError as exc:
                    raise StoreConflict(
                        f"invalid inventory requirement on node {node_uuid}: {exc}"
                    ) from exc
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if parents.get(node_uuid) is not None:
                # 执行父级（最近的循环容器）：调度器据此组装循环体子 DAG
                planned_node["parent_uuid"] = parents[node_uuid]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if source_handle_uuids:
                planned_node["source_handle_uuids"] = source_handle_uuids
            if template is not None and template.get("schema") is not None:
                planned_node["param_schema"] = template["schema"]
            if self._action_schema_resolver is not None and kind == "device_action":
                device_id = str((node.get("meta_data") or {}).get("target_device_id") or node.get("material_uuid") or planned_param.get("device_id") or "")
                action_name = str(node.get("action_name") or planned_param.get("action") or "")
                schema = self._action_schema_resolver(device_id, action_name)
                if schema and "x-unilabos-action-contract" in schema:
                    planned_node["param_schema"] = schema
            if entry_bindings:
                planned_node["entry_bindings"] = entry_bindings
            planned_nodes.append(planned_node)
            jobs.append(
                {
                    "uuid": str(uuid4()),
                    "workflow_node_uuid": node_uuid,
                    "topological_index": index,
                    "executor_kind": kind,
                    "execution_policy": policy,
                    "execution_timeout_seconds": 0,
                    "param": planned_param,
                }
            )
        plan = {
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
        }
        if target_node_uuid is not None:
            plan["target_node_uuid"] = target_node_uuid
        from unilabos.registry.typed_contract import validate_action_values
        from .run_preflight import deferred_parameters

        for node, job in zip(planned_nodes, jobs):
            schema = node.get("param_schema")
            if schema and "x-unilabos-action-contract" in schema:
                try:
                    pending = deferred_parameters(plan, node)
                    normalized = validate_action_values(schema, node["param"], deferred=pending)
                    # 未生成的动态值仍保留引用；预检不得用空值覆盖引用。
                    node["param"] = {**node["param"], **normalized}
                    job["param"] = dict(node["param"])
                except ValueError as error:
                    raise StoreConflict(str(error)) from None
        return plan, jobs

    @staticmethod
    def _node_or_ancestor_disabled(
        node: Mapping[str, Any],
        nodes: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        current: Mapping[str, Any] | None = node
        seen: set[str] = set()
        while current is not None:
            if bool(current.get("disabled", False)):
                return True
            parent_uuid = current.get("parent_uuid")
            if parent_uuid is None:
                return False
            identity = str(parent_uuid)
            if identity in seen:
                raise StoreConflict("workflow node parent hierarchy contains a cycle")
            seen.add(identity)
            current = nodes.get(identity)
        return False

    @staticmethod
    def _handle_data_key(handle: Dict[str, Any]) -> str:
        return str(handle.get("data_key") or handle.get("handle_key") or "").strip()

    @staticmethod
    def _final_target_data_key(data_key: str) -> str:
        return data_key.split("@@@")[-1].strip()

    @staticmethod
    def _dependency_only(handle: Dict[str, Any]) -> bool:
        if str(handle.get("handle_key") or "").strip().lower() == "ready":
            return True
        data_source = str(handle.get("data_source") or "").strip()
        return bool(data_source) and data_source.lower() != "executor"

    @staticmethod
    def _executor_kind(node_type: str) -> str:
        normalized = node_type.strip().lower()
        aliases = {
            "ilab": "device_action",
            "device": "device_action",
            "action": "device_action",
            "resource_action": "device_action",
            "py_script": "script",
        }
        kind = aliases.get(normalized, normalized)
        if kind not in {
            "device_action",
            "compute",
            "condition",
            "script",
            "group",
            "tool_call",
            "manual_confirm",
            "loop",
        }:
            raise StoreConflict(f"unsupported workflow node type {node_type!r}")
        return kind

    # Authoring ----------------------------------------------------------

    def register_editable_source(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            self._get_authoring_workflow(workflow_uuid)
            raw_root = Path(os.path.abspath(package_root))
            if self._path_contains_symlink(raw_root):
                raise WorkflowError("invalid_input")
            try:
                root = raw_root.resolve(strict=True)
            except OSError:
                raise WorkflowError("invalid_input") from None
            if not root.is_dir() or not package_id:
                raise WorkflowError("invalid_input")
            relative = PurePosixPath(relative_path)
            if (
                relative.is_absolute()
                or len(relative.parts) != 2
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.parts[0] != "workflows"
                or relative.suffix != ".py"
                or not relative.stem
            ):
                raise WorkflowError("invalid_input")
            target = root.joinpath(*relative.parts)
            self._assert_contained_regular_target(
                root,
                target,
                allow_missing=True,
            )
            source_uri = f"package://{package_id}/{relative.as_posix()}"
            try:
                return self.register_source(
                    workflow_uuid=workflow_uuid,
                    package_id=package_id,
                    package_root=str(root),
                    relative_path=relative.as_posix(),
                    source_uri=source_uri,
                )
            except StoreConflict:
                raise WorkflowConflict("invalid_input") from None

    def list_registered_sources(self) -> List[Dict[str, Any]]:
        """返回 Draft 监视与启动恢复所需的已注册源码。"""

        return self.list_source_registrations()

    def recover_registered_sources(
        self,
        *,
        force_compile: bool = False,
        preserve_author_source: bool = False,
    ) -> None:
        """按当前目录恢复全部源码；固定点模式不吞目录基础设施错误。"""

        for registration in self.list_registered_sources():
            self.reconcile_registered_source(
                registration["workflow_uuid"],
                force_compile=force_compile,
                preserve_author_source=preserve_author_source,
            )

    def activate_registered_sources_to_fixed_point(self) -> None:
        """按 child-first 依赖逐轮应用工作区候选，直到没有新工作流推进。"""

        registrations = self.list_registered_sources()
        self.recover_registered_sources(
            force_compile=True,
            preserve_author_source=True,
        )
        applied_workflows: set[str] = set()
        while True:
            applied_in_pass = False
            for registration in registrations:
                workflow_uuid = str(registration["workflow_uuid"])
                if workflow_uuid in applied_workflows:
                    continue
                record = self.get_authoring_record(workflow_uuid)
                candidate = record.get("candidate")
                if not isinstance(candidate, dict):
                    continue
                candidate_hash = candidate.get("candidate_hash")
                draft_hash = candidate.get("draft_hash")
                workflow_revision = candidate.get("base_workflow_revision")
                if (
                    not isinstance(candidate_hash, str)
                    or not isinstance(draft_hash, str)
                    or isinstance(workflow_revision, bool)
                    or not isinstance(workflow_revision, int)
                ):
                    raise WorkflowError("candidate_invalid")
                result = self.apply_authoring(
                    workflow_uuid,
                    expected_draft_hash=draft_hash,
                    expected_workflow_revision=workflow_revision,
                    expected_candidate_hash=candidate_hash,
                    preserve_author_source=True,
                )
                warnings = result.get("apply_result", {}).get("warnings")
                if warnings:
                    raise WorkflowError("template_catalog_unavailable")
                applied_workflows.add(workflow_uuid)
                applied_in_pass = True
                if self._compiler_rebuilder is not None:
                    try:
                        self.compiler = self._compiler_rebuilder()
                    except Exception:
                        raise WorkflowError("template_catalog_unavailable") from None
                self.recover_registered_sources(
                    force_compile=True,
                    preserve_author_source=True,
                )
            if not applied_in_pass:
                return

    def close(self) -> None:
        """关闭由该 Service 独占的 Workflow 持久存储。"""

        super().close()

    def get_authoring(self, workflow_uuid: str) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            graph = self.get_graph(workflow_uuid)
            record = self.get_authoring_record(workflow_uuid)
            return self._authoring_aggregate(
                workflow=workflow,
                graph=graph,
                registration=registration,
                source=source,
                record=record,
            )

    def save_draft(
        self,
        workflow_uuid: str,
        *,
        python_source: str,
        expected_draft_hash: Optional[str],
        expected_workflow_revision: int,
    ) -> Dict[str, Any]:
        self._validate_hash(expected_draft_hash, nullable=True)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            current = self._read_source(registration)
            current_hash = current["draft_hash"] if current is not None else None
            if current_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            try:
                encoded = python_source.encode("utf-8")
            except UnicodeEncodeError:
                raise WorkflowError("invalid_input") from None
            try:
                self._atomic_write(
                    registration,
                    encoded,
                    expected_hash=current_hash,
                )
            except OSError:
                raise WorkflowError("internal_error") from None
            source = self._read_source(registration)
            assert source is not None
            if source["draft_hash"] != _sha256(encoded):
                raise WorkflowConflict("draft_hash_conflict")
            applied_graph = self.get_graph(workflow_uuid)
            compilation = self._compile(
                workflow=workflow,
                graph=applied_graph,
                registration=registration,
                python_source=source["python_source"],
            )
            candidate = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
                applied_graph=applied_graph,
                draft_python_source=source["python_source"],
            )
            event_data = {
                "workflow_uuid": workflow_uuid,
                "cause": "draft_saved",
                "workflow_revision": workflow["revision"],
                "draft_hash": source["draft_hash"],
                "candidate_hash": (
                    candidate["candidate_hash"] if candidate is not None else None
                ),
            }
            self.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=source["draft_hash"],
                draft_update_time=source["update_time"],
                diagnostics=compilation.diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data=event_data,
            )
            return self.get_authoring(workflow_uuid)

    def reconcile_registered_source(
        self,
        workflow_uuid: str,
        *,
        force_compile: bool = False,
        preserve_author_source: bool = False,
    ) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            record = self.get_authoring_record(workflow_uuid)
            applied_source = record.get("applied_source")
            writeback_marker_valid = (
                record.get("writeback_source") is not None
                and record.get("writeback_expected_hash") is not None
                and record.get("writeback_generation") is not None
            )
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and source is not None
                and applied_source is not None
                and source["draft_hash"] == applied_source["source_hash"]
            ):
                self.settle_writeback(
                    workflow_uuid=workflow_uuid,
                    expected_writeback_source=record["writeback_source"],
                    expected_writeback_hash=record["writeback_expected_hash"],
                    expected_writeback_generation=record["writeback_generation"],
                    observed_draft_hash=source["draft_hash"],
                    draft_update_time=source["update_time"],
                    event_data={
                        "workflow_uuid": workflow_uuid,
                        "cause": "recovered",
                        "workflow_revision": workflow["revision"],
                        "draft_hash": source["draft_hash"],
                        "candidate_hash": None,
                    },
                )
                return self.get_authoring(workflow_uuid)
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and (
                    source is None
                    or source["draft_hash"] == record["writeback_expected_hash"]
                )
            ):
                recovery_source = record.get("writeback_source")
                if recovery_source is not None:
                    try:
                        recovery_bytes = recovery_source.encode("utf-8")
                        recovery_hash = _sha256(recovery_bytes)
                        self._atomic_write(
                            registration,
                            recovery_bytes,
                            expected_hash=(
                                source["draft_hash"] if source is not None else None
                            ),
                        )
                        source = self._read_source(registration)
                        if source is not None and source["draft_hash"] == recovery_hash:
                            self.settle_writeback(
                                workflow_uuid=workflow_uuid,
                                expected_writeback_source=record["writeback_source"],
                                expected_writeback_hash=record[
                                    "writeback_expected_hash"
                                ],
                                expected_writeback_generation=record[
                                    "writeback_generation"
                                ],
                                observed_draft_hash=source["draft_hash"],
                                draft_update_time=source["update_time"],
                                event_data={
                                    "workflow_uuid": workflow_uuid,
                                    "cause": "recovered",
                                    "workflow_revision": workflow["revision"],
                                    "draft_hash": source["draft_hash"],
                                    "candidate_hash": None,
                                },
                            )
                            return self.get_authoring(workflow_uuid)
                    except (OSError, UnicodeError, WorkflowError):
                        return self.get_authoring(workflow_uuid)
            actual_hash = source["draft_hash"] if source is not None else None
            invalid_writeback_marker = (
                record["writeback_status"] == "pending" and not writeback_marker_valid
            )
            if (
                actual_hash == record["observed_draft_hash"]
                and not invalid_writeback_marker
                and not (actual_hash is None and record.get("candidate") is not None)
                and not force_compile
            ):
                return self.get_authoring(workflow_uuid)

            candidate: Optional[Dict[str, Any]] = None
            diagnostics: List[Dict[str, Any]] = []
            if source is not None:
                applied_graph = self.get_graph(workflow_uuid)
                compilation = self._compile(
                    workflow=workflow,
                    graph=applied_graph,
                    registration=registration,
                    python_source=source["python_source"],
                )
                if preserve_author_source:
                    compilation = self._preserve_author_source_compilation(
                        compilation=compilation,
                        workflow=workflow,
                        graph=applied_graph,
                        python_source=source["python_source"],
                    )
                diagnostics = compilation.diagnostics
                candidate = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                    applied_graph=applied_graph,
                    draft_python_source=source["python_source"],
                )
            cause = (
                "recovered"
                if source is not None
                and record["observed_draft_hash"] is None
                and record["update_time"] is not None
                else "external_draft_changed"
            )
            self.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=actual_hash,
                draft_update_time=(
                    source["update_time"] if source is not None else None
                ),
                diagnostics=diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data={
                    "workflow_uuid": workflow_uuid,
                    "cause": cause,
                    "workflow_revision": workflow["revision"],
                    "draft_hash": actual_hash,
                    "candidate_hash": (
                        candidate["candidate_hash"] if candidate is not None else None
                    ),
                },
            )
            return self.get_authoring(workflow_uuid)

    def apply_authoring(
        self,
        workflow_uuid: str,
        *,
        expected_draft_hash: str,
        expected_workflow_revision: int,
        expected_candidate_hash: str,
        preserve_author_source: bool = False,
    ) -> Dict[str, Any]:
        self._validate_hash(expected_draft_hash, nullable=False)
        self._validate_hash(expected_candidate_hash, nullable=False)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            actual_hash = source["draft_hash"] if source is not None else None

            # D-079 固定了这里的冲突顺序。
            if actual_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            record = self.get_authoring_record(workflow_uuid)
            candidate = record.get("candidate")
            current_catalog = self._catalog_fingerprint()
            if (
                candidate is not None
                and candidate["template_catalog_fingerprint"] != current_catalog
            ):
                raise WorkflowConflict("template_catalog_conflict")
            if candidate is None:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in record["diagnostics"]
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowConflict("candidate_not_ready")
            if candidate["candidate_hash"] != expected_candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")
            if source is None:
                raise WorkflowConflict("draft_hash_conflict")

            applied_graph = self.get_graph(workflow_uuid)
            compilation = self._compile(
                workflow=workflow,
                graph=applied_graph,
                registration=registration,
                python_source=source["python_source"],
            )
            if preserve_author_source:
                compilation = self._preserve_author_source_compilation(
                    compilation=compilation,
                    workflow=workflow,
                    graph=applied_graph,
                    python_source=source["python_source"],
                )
            if not self._normalize_candidate_diagnostics(
                compilation,
                python_source=source["python_source"],
            ):
                raise WorkflowError("candidate_invalid")
            if not compilation.valid:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in compilation.diagnostics
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowError("candidate_invalid")
            revalidated = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
                applied_graph=applied_graph,
                draft_python_source=source["python_source"],
            )
            if revalidated is None:
                raise WorkflowError("candidate_invalid")
            if (
                revalidated["template_catalog_fingerprint"]
                != candidate["template_catalog_fingerprint"]
            ):
                raise WorkflowConflict("template_catalog_conflict")
            if revalidated["candidate_hash"] != candidate["candidate_hash"]:
                raise WorkflowConflict("candidate_hash_conflict")

            def validate_authorities() -> None:
                latest_source = self._read_source(registration)
                if (
                    latest_source is None
                    or latest_source["draft_hash"] != expected_draft_hash
                ):
                    raise WorkflowConflict("draft_hash_conflict")
                if (
                    self._catalog_fingerprint()
                    != candidate["template_catalog_fingerprint"]
                ):
                    raise WorkflowConflict("template_catalog_conflict")

            validate_authorities()

            normalized_source = candidate["normalized_python_source"]
            normalized_bytes = normalized_source.encode("utf-8")
            normalized_hash = _sha256(normalized_bytes)
            applied_source = {
                "python_source": normalized_source,
                "source_hash": normalized_hash,
                "source_map": candidate["source_map"],
                "compiler_version": candidate["compiler_version"],
                "template_catalog_fingerprint": candidate[
                    "template_catalog_fingerprint"
                ],
            }
            previous_revision = workflow["revision"]
            try:
                (
                    resulting_revision,
                    writeback_generation,
                ) = self.apply_authoring_candidate(
                    workflow_uuid=workflow_uuid,
                    expected_revision=previous_revision,
                    expected_draft_hash=expected_draft_hash,
                    expected_candidate_hash=expected_candidate_hash,
                    expected_catalog_fingerprint=candidate[
                        "template_catalog_fingerprint"
                    ],
                    candidate=candidate,
                    applied_source=applied_source,
                    event_data={
                        "workflow_uuid": workflow_uuid,
                        "cause": "applied",
                        "draft_hash": normalized_hash,
                        "candidate_hash": None,
                    },
                )
            except StoreAuthoringConflict as error:
                raise WorkflowConflict(error.code) from None
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except (StoreConflict, ValidationError):
                raise WorkflowError("candidate_invalid") from None

            warnings: List[Dict[str, str]] = []
            response_source = source

            def warn_writeback() -> None:
                if warnings:
                    return
                warnings.append(
                    {
                        "code": "draft_writeback_pending",
                        "message": (
                            "工作流已应用，但本地源码同步失败；"
                            "OS 已保留可恢复的源码记录。"
                        ),
                    }
                )

            def mark_pending_best_effort() -> None:
                for _attempt in range(2):
                    try:
                        marker_owned = self.mark_writeback_pending(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                        )
                        if not marker_owned:
                            # marker 已由另一轮 Apply/Draft 接管，本轮到此结束。
                            return
                        return
                    except Exception:  # noqa: BLE001 - 提交后只能尽力恢复
                        continue

            try:
                latest = self._read_source(registration)
                if latest is None or latest["draft_hash"] != actual_hash:
                    raise WorkflowError("draft_hash_conflict")
                self._atomic_write(
                    registration,
                    normalized_bytes,
                    expected_hash=actual_hash,
                )
                written = self._read_source(registration)
                assert written is not None
                response_source = written
                if written["draft_hash"] != normalized_hash:
                    raise WorkflowConflict("draft_hash_conflict")
            except Exception:  # noqa: BLE001 - 主事务已提交
                # 主事务已经提交。之后任何文件系统、数据库或聚合错误
                # 都只能降级为可恢复警告，不能把成功伪装成失败。
                warn_writeback()
                mark_pending_best_effort()
            else:
                settled = False
                for _attempt in range(2):
                    try:
                        marker_owned = self.settle_writeback(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                            observed_draft_hash=written["draft_hash"],
                            draft_update_time=written["update_time"],
                        )
                        if not marker_owned:
                            # 新 generation 已接管；陈旧 settle 无需恢复。
                            settled = True
                            break
                        settled = True
                        break
                    except Exception:  # noqa: BLE001 - 主事务已提交
                        warn_writeback()
                if not settled:
                    mark_pending_best_effort()

            fallback_meta_data = dict(workflow["meta_data"])
            candidate_workflow = candidate["graph"].get("workflow") or {}
            candidate_meta_data = candidate_workflow.get("meta_data") or {}
            if (
                isinstance(candidate_meta_data, dict)
                and "unilab" in candidate_meta_data
            ):
                fallback_meta_data["unilab"] = candidate_meta_data["unilab"]
            fallback_workflow = {
                **workflow,
                "revision": resulting_revision,
                "meta_data": fallback_meta_data,
                "update_time": utc_now(),
            }
            fallback_applied_source = {
                **applied_source,
                "workflow_revision": resulting_revision,
                "update_time": utc_now(),
            }
            fallback_record = {
                "observed_draft_hash": (
                    response_source["draft_hash"]
                    if response_source is not None
                    else None
                ),
                "diagnostics": [],
                "candidate": None,
                "applied_source": fallback_applied_source,
            }
            try:
                authoring = self.get_authoring(workflow_uuid)
            except Exception:  # noqa: BLE001 - 主事务已提交
                try:
                    authoring = self.get_authoring(workflow_uuid)
                except Exception:  # noqa: BLE001 - 使用已知事实降级
                    try:
                        fallback_graph = self.get_graph(workflow_uuid)
                        fallback_workflow = fallback_graph["workflow"]
                        fallback_record = self.get_authoring_record(
                            workflow_uuid
                        )
                    except Exception:  # noqa: BLE001 - 使用提交时事实降级
                        fallback_graph = self._post_commit_candidate_graph(
                            candidate["graph"],
                            workflow=fallback_workflow,
                        )
                    authoring = self._authoring_aggregate(
                        workflow=fallback_workflow,
                        graph=fallback_graph,
                        registration=registration,
                        source=response_source,
                        record=fallback_record,
                    )

            return {
                "apply_result": {
                    "kind": candidate["changeset"]["kind"],
                    "previous_workflow_revision": previous_revision,
                    "workflow_revision": resulting_revision,
                    "applied_candidate_hash": candidate["candidate_hash"],
                    "applied_source_hash": normalized_hash,
                    "warnings": warnings,
                },
                "authoring": authoring,
            }

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> Dict[str, Any]:
        if after_id < 0 or not 1 <= limit <= 1000:
            raise WorkflowError("invalid_input")
        return {
            "items": super().list_events(after_id=after_id, limit=limit),
            "after_id": after_id,
        }

    # Authoring 内部实现 -------------------------------------------------

    def _get_authoring_workflow(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return super().get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _registration(self, workflow_uuid: str) -> Dict[str, Any]:
        try:
            return self.get_source_registration(workflow_uuid)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _read_source(
        self,
        registration: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                return None
            parent_fd, filename = source_parent
            try:
                descriptor = os.open(
                    filename,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise WorkflowError("invalid_input") from None
            try:
                stat_result = os.fstat(descriptor)
                if not stat.S_ISREG(stat_result.st_mode):
                    raise WorkflowError("invalid_input")
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    try:
                        raw = stream.read()
                    except OSError:
                        raise WorkflowError("invalid_input") from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        try:
            source = raw.decode("utf-8")
        except UnicodeError:
            raise WorkflowError("invalid_input") from None
        return {
            "python_source": source,
            "draft_hash": _sha256(raw),
            "update_time": _mtime_rfc3339(stat_result.st_mtime),
        }

    def source_reconciliation_pending(self, workflow_uuid: str) -> bool:
        """告知源码监视器一次成功调用后是否仍需重试。"""

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            record = self.get_authoring_record(workflow_uuid)
            return record["writeback_status"] == "pending"

    def source_signature(
        self,
        registration: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        """返回无需读取文件内容的稳定性签名，供 Draft 监视器去抖。"""

        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                return ("missing",)
            parent_fd, filename = source_parent
            try:
                stat_result = os.stat(
                    filename,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return ("missing",)
            except OSError:
                raise WorkflowError("invalid_input") from None
            if not stat.S_ISREG(stat_result.st_mode):
                raise WorkflowError("invalid_input")
        return (
            "file",
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    def _atomic_write(
        self,
        registration: Dict[str, Any],
        content: bytes,
        *,
        expected_hash: Any = _NO_EXPECTED_HASH,
    ) -> None:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        # 先以目录 FD 安全地创建（如有需要）固定的 workflows 目录。
        # 该上下文关闭后再次打开，避免依赖校验时拿到的字符串路径。
        with self._source_parent_fd(registration, create=True):
            pass
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                raise WorkflowError("invalid_input")
            parent_fd, filename = source_parent
            temporary_name = f".{filename}.{uuid4().hex}.tmp"
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary_name,
                    (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                    ),
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected_hash is _NO_EXPECTED_HASH:
                    os.replace(
                        temporary_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                else:
                    self._compare_and_replace(
                        parent_fd=parent_fd,
                        target_name=filename,
                        temporary_name=temporary_name,
                        expected_hash=expected_hash,
                    )
                os.fsync(parent_fd)
            except WorkflowError:
                raise
            except OSError:
                raise WorkflowError("internal_error") from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_fd)

    @staticmethod
    def _compare_and_replace(
        *,
        parent_fd: int,
        target_name: str,
        temporary_name: str,
        expected_hash: Optional[str],
    ) -> None:
        """在可安全中断的 lease 下执行 fsync 后的原子 CAS replace。"""

        target_descriptor = -1
        temporary_descriptor = -1
        backup_name = f".{target_name}.{uuid4().hex}.cas"
        backup_created = False
        replacement_attempted = False
        lease_held = False
        previous_signal_mask: Optional[set[signal.Signals]] = None
        try:
            try:
                target_descriptor = os.open(
                    target_name,
                    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                if expected_hash is not None:
                    raise WorkflowConflict("draft_hash_conflict") from None
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise WorkflowConflict("draft_hash_conflict") from None
                os.unlink(temporary_name, dir_fd=parent_fd)
                return

            if expected_hash is None:
                raise WorkflowConflict("draft_hash_conflict")
            try:
                previous_signal_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    {_LEASE_BREAK_SIGNAL},
                )
                fcntl.fcntl(
                    target_descriptor,
                    _F_SETOWN_EX,
                    struct.pack(
                        "ii",
                        _F_OWNER_TID,
                        threading.get_native_id(),
                    ),
                )
                fcntl.fcntl(
                    target_descriptor,
                    fcntl.F_SETSIG,
                    _LEASE_BREAK_SIGNAL,
                )
                fcntl.fcntl(
                    target_descriptor,
                    fcntl.F_SETLEASE,
                    fcntl.F_WRLCK,
                )
                lease_held = True
            except (AttributeError, OSError, ValueError):
                # 无法证明没有预打开的读写句柄时必须失败关闭。
                raise WorkflowConflict("draft_hash_conflict") from None

            original = WorkflowService._read_regular_fd(target_descriptor)
            if _sha256(original) != expected_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if not WorkflowService._target_matches_fd(
                parent_fd,
                target_name,
                target_descriptor,
            ):
                raise WorkflowConflict("draft_hash_conflict")

            temporary_descriptor = os.open(
                temporary_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            replacement_hash = WorkflowService._hash_regular_fd(temporary_descriptor)

            os.link(
                target_name,
                backup_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            backup_created = True
            os.fsync(parent_fd)
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")

            replacement_attempted = True
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")
            if (
                not WorkflowService._target_matches_fd(
                    parent_fd,
                    target_name,
                    temporary_descriptor,
                )
                or WorkflowService._hash_regular_fd(temporary_descriptor)
                != replacement_hash
            ):
                raise WorkflowConflict("draft_hash_conflict")

            fcntl.fcntl(
                target_descriptor,
                fcntl.F_SETLEASE,
                fcntl.F_UNLCK,
            )
            lease_held = False
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")
            if (
                not WorkflowService._target_matches_fd(
                    parent_fd,
                    target_name,
                    temporary_descriptor,
                )
                or WorkflowService._hash_regular_fd(temporary_descriptor)
                != replacement_hash
            ):
                raise WorkflowConflict("draft_hash_conflict")

            with suppress(OSError):
                os.unlink(backup_name, dir_fd=parent_fd)
                backup_created = False
                os.fsync(parent_fd)
        except Exception:
            # os.replace() 一旦被调用，异常路径便无法证明 canonical
            # 仍是本进程发布的 inode；外部 authority 可能已经原地写入
            # 或再次原子替换。此时绝不能用历史 `.cas` 覆盖或删除它。
            # 保留 fsync 过的原稿 artifact，只允许显式人工/Git 恢复。
            if backup_created and not replacement_attempted:
                with suppress(OSError):
                    os.unlink(backup_name, dir_fd=parent_fd)
                    backup_created = False
                    os.fsync(parent_fd)
            raise
        finally:
            if lease_held and target_descriptor >= 0:
                with suppress(OSError):
                    fcntl.fcntl(
                        target_descriptor,
                        fcntl.F_SETLEASE,
                        fcntl.F_UNLCK,
                    )
            if previous_signal_mask is not None:
                with suppress(OSError, ValueError):
                    WorkflowService._drain_lease_break_signal()
                signal.pthread_sigmask(
                    signal.SIG_SETMASK,
                    previous_signal_mask,
                )
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            if target_descriptor >= 0:
                os.close(target_descriptor)

    @staticmethod
    def _drain_lease_break_signal() -> bool:
        """同步消费发给当前线程的 lease 通知。"""

        observed = False
        while True:
            try:
                notification = signal.sigtimedwait(
                    {_LEASE_BREAK_SIGNAL},
                    0,
                )
            except InterruptedError:
                continue
            if notification is None:
                return observed
            observed = True

    @staticmethod
    def _target_matches_fd(
        parent_fd: int,
        target_name: str,
        descriptor: int,
    ) -> bool:
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        descriptor_stat = os.fstat(descriptor)
        return (
            stat.S_ISREG(target_stat.st_mode)
            and target_stat.st_dev == descriptor_stat.st_dev
            and target_stat.st_ino == descriptor_stat.st_ino
        )

    @staticmethod
    def _read_regular_fd(descriptor: int) -> bytes:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise WorkflowError("invalid_input")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_regular_fd(descriptor: int, content: bytes) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short write while replacing Workflow Draft")
            offset += written
        os.ftruncate(descriptor, len(content))
        os.fsync(descriptor)

    @staticmethod
    def _restore_regular_fd(descriptor: int, content: bytes) -> None:
        WorkflowService._write_regular_fd(descriptor, content)

    @staticmethod
    def _hash_regular_fd(descriptor: int) -> str:
        return _sha256(WorkflowService._read_regular_fd(descriptor))

    @classmethod
    @contextmanager
    def _source_parent_fd(
        cls,
        registration: Dict[str, Any],
        *,
        create: bool,
    ) -> Iterator[Optional[Tuple[int, str]]]:
        relative = PurePosixPath(registration["relative_path"])
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "workflows"
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.suffix != ".py"
            or not relative.stem
        ):
            raise WorkflowError("invalid_input")

        root_fd = cls._open_directory_chain(Path(registration["package_root"]))
        parent_fd = -1
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                parent_fd = os.open(
                    relative.parts[0],
                    flags,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                with suppress(FileExistsError):
                    os.mkdir(relative.parts[0], 0o755, dir_fd=root_fd)
                parent_fd = os.open(
                    relative.parts[0],
                    flags,
                    dir_fd=root_fd,
                )
            yield parent_fd, relative.parts[1]
        except WorkflowError:
            raise
        except OSError:
            raise WorkflowError("invalid_input") from None
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)

    @staticmethod
    def _open_directory_chain(path: Path) -> int:
        absolute = Path(os.path.abspath(path))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            current_fd = os.open(absolute.anchor, flags)
            for part in absolute.parts[1:]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                finally:
                    os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except OSError:
            raise WorkflowError("invalid_input") from None

    @staticmethod
    def _source_path(
        registration: Dict[str, Any],
    ) -> Tuple[Path, Path]:
        stored_root = Path(registration["package_root"])
        if WorkflowService._path_contains_symlink(stored_root):
            raise WorkflowError("invalid_input")
        try:
            root = stored_root.resolve(strict=True)
        except OSError:
            raise WorkflowError("invalid_input") from None
        relative = PurePosixPath(registration["relative_path"])
        return root, root.joinpath(*relative.parts)

    @staticmethod
    def _path_contains_symlink(path: Path) -> bool:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def _assert_contained_regular_target(
        root: Path,
        target: Path,
        *,
        allow_missing: bool,
    ) -> None:
        if target.is_symlink():
            raise WorkflowError("invalid_input")
        try:
            relative = target.relative_to(root)
            current = root
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise WorkflowError("invalid_input")
            resolved = target.resolve(strict=False)
            resolved.relative_to(root)
        except WorkflowError:
            raise
        except (OSError, ValueError):
            raise WorkflowError("invalid_input") from None
        if target.exists() and not target.is_file():
            raise WorkflowError("invalid_input")
        if not allow_missing and not target.exists():
            raise WorkflowError("invalid_input")

    def _compile(
        self,
        *,
        workflow: Dict[str, Any],
        graph: Dict[str, Any],
        registration: Dict[str, Any],
        python_source: str,
    ) -> CandidateCompilation:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            result = self.compiler.compile(
                workflow_uuid=workflow["uuid"],
                workflow_revision=workflow["revision"],
                python_source=python_source,
                source_uri=registration["source_uri"],
                applied_graph=graph,
            )
            return CandidateCompilation.model_validate(result)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("internal_error") from None

    def _preserve_author_source_compilation(
        self,
        *,
        compilation: CandidateCompilation,
        workflow: Dict[str, Any],
        graph: Dict[str, Any],
        python_source: str,
    ) -> CandidateCompilation:
        """冷启动自动应用时保留作者源码，不制造未确认的规范化编辑。"""

        if (
            not compilation.valid
            or compilation.normalized_python_source == python_source
        ):
            return compilation
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        preserve = getattr(self.compiler, "preserve_author_source", None)
        if not callable(preserve):
            raise WorkflowError("candidate_invalid")
        try:
            result = preserve(
                compilation=compilation,
                workflow_uuid=workflow["uuid"],
                workflow_revision=workflow["revision"],
                python_source=python_source,
                applied_graph=graph,
            )
            preserved = CandidateCompilation.model_validate(result)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("candidate_invalid") from None
        if preserved.normalized_python_source != python_source:
            raise WorkflowError("candidate_invalid")
        return preserved

    def _catalog_fingerprint(self) -> str:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            value = self.compiler.template_catalog_fingerprint
        except Exception:
            raise WorkflowError("template_catalog_unavailable") from None
        if not isinstance(value, str) or _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("template_catalog_unavailable")
        return value

    def _issue_candidate(
        self,
        *,
        workflow_revision: int,
        draft_hash: str,
        compilation: CandidateCompilation,
        applied_graph: Dict[str, Any],
        draft_python_source: str,
    ) -> Optional[Dict[str, Any]]:
        applied_graph = self._validated_applied_backend_graph(applied_graph)
        if not self._normalize_candidate_diagnostics(
            compilation,
            python_source=draft_python_source,
        ):
            return None
        if not compilation.valid:
            return None
        assert compilation.graph is not None
        try:
            graph = self._backend_candidate_graph(
                compilation.graph,
                applied_graph=applied_graph,
            )
            if not isinstance(compilation.source_map, list):
                raise ValueError
            source_map = [
                CandidateSourceMapEntry.model_validate(item).model_dump()
                for item in compilation.source_map
            ]
            if not _source_ranges_fit(
                compilation.normalized_python_source,
                source_map,
            ):
                raise ValueError
            changeset = CandidateChangeset.model_validate(
                compilation.changeset,
            ).model_dump()
            self._validate_candidate_bundle_semantics(
                graph=graph,
                applied_graph=applied_graph,
                source_map=source_map,
                changeset=changeset,
            )
            compiler_version = compilation.compiler_version
            if not compiler_version.strip():
                raise ValueError
            template_catalog_fingerprint = compilation.template_catalog_fingerprint
            if _HASH_TOKEN.fullmatch(template_catalog_fingerprint) is None:
                raise ValueError
        except (
            GraphValidationError,
            KeyError,
            TypeError,
            ValidationError,
            ValueError,
            WorkflowError,
        ) as error:
            if isinstance(error, WorkflowError) and error.code != "candidate_invalid":
                raise
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        bundle = {
            "base_workflow_revision": workflow_revision,
            "draft_hash": draft_hash,
            "graph": graph,
            "normalized_python_source": compilation.normalized_python_source,
            "source_map": source_map,
            "changeset": changeset,
            "compiler_version": compiler_version,
            "template_catalog_fingerprint": template_catalog_fingerprint,
        }
        try:
            canonical_bundle = _canonical_json(bundle)
        except (TypeError, UnicodeError, ValueError):
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        return {
            "candidate_hash": _sha256(canonical_bundle),
            **bundle,
            "update_time": utc_now(),
        }

    @staticmethod
    def _set_candidate_invalid_diagnostic(
        compilation: CandidateCompilation,
    ) -> None:
        compilation.diagnostics = [
            {
                "severity": "error",
                "code": "candidate_invalid",
                "message": _ERRORS["candidate_invalid"][1],
            }
        ]

    @classmethod
    def _normalize_candidate_diagnostics(
        cls,
        compilation: CandidateCompilation,
        *,
        python_source: str,
    ) -> bool:
        try:
            if not isinstance(compilation.diagnostics, list):
                raise ValueError
            compilation.diagnostics = [
                CandidateDiagnostic.model_validate(item).model_dump(
                    exclude_none=True,
                )
                for item in compilation.diagnostics
            ]
            source_ranges = [
                item["source_range"]
                for item in compilation.diagnostics
                if item.get("source_range") is not None
            ]
            if not _source_ranges_fit(python_source, source_ranges):
                raise ValueError
        except (TypeError, ValidationError, ValueError):
            cls._set_candidate_invalid_diagnostic(compilation)
            return False
        return True

    @staticmethod
    def _semantic_node(node: Dict[str, Any]) -> Dict[str, Any]:
        return WorkflowNodeWrite.model_validate(
            {
                field: node[field]
                for field in WorkflowNodeWrite.model_fields
                if field in node
            }
        ).model_dump()

    @staticmethod
    def _semantic_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
        return WorkflowEdgeWrite.model_validate(
            {
                field: edge[field]
                for field in WorkflowEdgeWrite.model_fields
                if field in edge
            }
        ).model_dump()

    @classmethod
    def _validate_candidate_bundle_semantics(
        cls,
        *,
        graph: Dict[str, Any],
        applied_graph: Dict[str, Any],
        source_map: List[Dict[str, Any]],
        changeset: Dict[str, Any],
    ) -> None:
        """证明编译器 bundle 精确描述了完整工作流图。"""

        workflow = graph["workflow"]
        applied_workflow = applied_graph["workflow"]
        for field in _WORKFLOW_READ_FIELDS - {"meta_data"}:
            if not strict_json_equal(
                workflow.get(field),
                applied_workflow.get(field),
            ):
                raise ValueError("Candidate changed an unsupported Workflow field")

        workflow_meta = dict(workflow["meta_data"])
        applied_meta = dict(applied_workflow["meta_data"])
        reserved_changed = not strict_json_equal(
            workflow_meta.pop("unilab", None),
            applied_meta.pop("unilab", None),
        )
        if not strict_json_equal(workflow_meta, applied_meta):
            raise ValueError("Candidate changed non-authoring Workflow metadata")

        for field in ("node_templates", "handle_templates"):
            candidate_entities = sorted(graph[field], key=lambda item: item["uuid"])
            applied_entities = sorted(
                applied_graph[field],
                key=lambda item: item["uuid"],
            )
            if not strict_json_equal(candidate_entities, applied_entities):
                raise ValueError("Candidate catalog projection is not authoritative")

        nodes = [cls._semantic_node(item) for item in graph["nodes"]]
        edges = [cls._semantic_edge(item) for item in graph["edges"]]
        candidate_nodes = {item["uuid"]: item for item in nodes}
        candidate_edges = {item["uuid"]: item for item in edges}
        if len(candidate_nodes) != len(nodes) or len(candidate_edges) != len(edges):
            raise ValueError("Candidate graph contains duplicate UUIDs")

        templates = {item["uuid"]: item for item in graph["node_templates"]}
        handles = {item["uuid"]: item for item in graph["handle_templates"]}
        validate_graph(
            nodes=[
                WorkflowNodeWrite.model_validate(item)
                for item in candidate_nodes.values()
            ],
            edges=[
                WorkflowEdgeWrite.model_validate(item)
                for item in candidate_edges.values()
            ],
            templates=templates,
            handles=handles,
            effective_params={
                uuid: item["param"] or {} for uuid, item in candidate_nodes.items()
            },
            workflow_meta_data=workflow["meta_data"],
            node_meta_data={
                uuid: item["meta_data"] for uuid, item in candidate_nodes.items()
            },
        )

        if any(
            entry["workflow_node_uuid"] not in candidate_nodes for entry in source_map
        ):
            raise ValueError("Source map references a Node outside the Candidate")

        applied_nodes = {
            item["uuid"]: cls._semantic_node(item) for item in applied_graph["nodes"]
        }
        applied_edges = {
            item["uuid"]: cls._semantic_edge(item) for item in applied_graph["edges"]
        }
        expected = {
            "created_node_uuids": set(candidate_nodes) - set(applied_nodes),
            "updated_node_uuids": {
                uuid
                for uuid in set(candidate_nodes) & set(applied_nodes)
                if not strict_json_equal(
                    candidate_nodes[uuid],
                    applied_nodes[uuid],
                )
            },
            "deleted_node_uuids": set(applied_nodes) - set(candidate_nodes),
            "created_edge_uuids": set(candidate_edges) - set(applied_edges),
            "updated_edge_uuids": {
                uuid
                for uuid in set(candidate_edges) & set(applied_edges)
                if not strict_json_equal(
                    candidate_edges[uuid],
                    applied_edges[uuid],
                )
            },
            "deleted_edge_uuids": set(applied_edges) - set(candidate_edges),
        }
        node_fields = (
            "created_node_uuids",
            "updated_node_uuids",
            "deleted_node_uuids",
        )
        edge_fields = (
            "created_edge_uuids",
            "updated_edge_uuids",
            "deleted_edge_uuids",
        )
        for fields in (node_fields, edge_fields):
            values = [changeset[field] for field in fields]
            if any(len(value) != len(set(value)) for value in values):
                raise ValueError("Changeset contains duplicate UUIDs")
            if any(
                set(values[left]) & set(values[right])
                for left in range(len(values))
                for right in range(left + 1, len(values))
            ):
                raise ValueError("Changeset lifecycle UUID sets overlap")
        if any(set(changeset[field]) != expected[field] for field in expected):
            raise ValueError("Changeset does not describe the Candidate graph")
        if changeset["reserved_metadata_changed"] is not reserved_changed:
            raise ValueError("Changeset reserved metadata flag is inaccurate")

        graph_changed = reserved_changed or any(expected.values())
        expected_kind = "graph" if graph_changed else "source_only"
        if changeset["kind"] != expected_kind:
            raise ValueError("Changeset kind does not match graph semantics")

    @staticmethod
    def _backend_graph_projection(
        graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """按 Backend JSON omitempty 语义投影 Candidate。"""

        def omit_none(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            return {key: item for key, item in value.items() if item is not None}

        return {
            "workflow": omit_none(graph.get("workflow") or {}),
            "nodes": [omit_none(item) for item in (graph.get("nodes") or [])],
            "edges": [omit_none(item) for item in (graph.get("edges") or [])],
            "node_templates": [
                omit_none(item) for item in (graph.get("node_templates") or [])
            ],
            "handle_templates": [
                omit_none(item) for item in (graph.get("handle_templates") or [])
            ],
        }

    @classmethod
    def _backend_candidate_graph(
        cls,
        graph: Dict[str, Any],
        *,
        applied_graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """把编译器写实体补全为冻结的 Backend 读取形状。"""

        applied = cls._validated_applied_backend_graph(applied_graph)
        cls._require_candidate_graph_containers(graph)
        projected = cls._backend_graph_projection(graph)
        applied_workflow = applied["workflow"]
        workflow_uuid = applied_workflow["uuid"]
        timestamp = applied_workflow["update_time"]
        applied_nodes = {item["uuid"]: item for item in applied["nodes"]}
        applied_edges = {item["uuid"]: item for item in applied["edges"]}
        applied_node_templates = {
            item["uuid"]: item for item in applied["node_templates"]
        }
        applied_handle_templates = {
            item["uuid"]: item for item in applied["handle_templates"]
        }

        nodes = []
        for item in projected["nodes"]:
            value = WorkflowNodeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            persisted = applied_nodes.get(value["uuid"], {})
            nodes.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    "workflow_uuid": workflow_uuid,
                    **value,
                }
            )
        cls._require_backend_read_fields(
            nodes,
            _NODE_REQUIRED_READ_FIELDS,
        )

        edges = []
        for item in projected["edges"]:
            value = WorkflowEdgeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            persisted = applied_edges.get(value["uuid"], {})
            edges.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        cls._require_backend_read_fields(
            edges,
            _EDGE_REQUIRED_READ_FIELDS,
        )

        projected["workflow"] = {
            key: value
            for key, value in {
                **applied_workflow,
                **projected["workflow"],
                "uuid": workflow_uuid,
                "create_time": applied_workflow["create_time"],
                "update_time": timestamp,
            }.items()
            if key in _WORKFLOW_READ_FIELDS
        }
        projected["nodes"] = nodes
        projected["edges"] = edges
        projected["node_templates"] = cls._hydrate_backend_catalog_entities(
            projected["node_templates"],
            persisted=applied_node_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "resource_template_uuid"},
            allowed_fields=_NODE_TEMPLATE_READ_FIELDS,
            required_fields=_NODE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        projected["handle_templates"] = cls._hydrate_backend_catalog_entities(
            projected["handle_templates"],
            persisted=applied_handle_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "workflow_node_template_uuid"},
            allowed_fields=_HANDLE_TEMPLATE_READ_FIELDS,
            required_fields=_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        cls._require_backend_read_fields(
            [projected["workflow"]],
            _WORKFLOW_REQUIRED_READ_FIELDS,
        )
        try:
            cls._require_backend_entity_types(projected)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("candidate_invalid") from None
        if projected["workflow"]["revision"] != applied_workflow["revision"]:
            raise WorkflowError("candidate_invalid")
        return projected

    @classmethod
    def _validated_applied_backend_graph(
        cls,
        graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """检查 Candidate 前先校验 Authority 持有的工作流图。"""

        try:
            applied = cls._backend_graph_projection(graph)
            cls._require_backend_read_fields(
                [applied["workflow"]],
                _WORKFLOW_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["nodes"],
                _NODE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["edges"],
                _EDGE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["node_templates"],
                _NODE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["handle_templates"],
                _HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_entity_types(applied)
            return applied
        except WorkflowError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("internal_error") from None

    @staticmethod
    def _require_backend_entity_types(graph: Dict[str, Any]) -> None:
        """在完整工作流图上强制执行冻结的 Backend JSON 类型。"""

        def exact(entity: Dict[str, Any], fields: set[str], expected: type) -> None:
            if any(type(entity[field]) is not expected for field in fields):
                raise ValueError

        def optional(
            entity: Dict[str, Any],
            fields: set[str],
            expected: type,
        ) -> None:
            if any(
                field in entity and type(entity[field]) is not expected
                for field in fields
            ):
                raise ValueError

        def uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            exact(entity, fields, str)
            for field in fields:
                validate_uuid(entity[field])

        def optional_uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            for field in fields:
                if field in entity:
                    uuids(entity, {field})

        workflow = graph["workflow"]
        uuids(workflow, {"uuid"})
        exact(workflow, {"create_time", "update_time", "name"}, str)
        exact(workflow, {"meta_data"}, dict)
        exact(workflow, {"tags"}, list)
        normalize_json_object(workflow["meta_data"])
        normalize_json_array(workflow["tags"])
        optional(workflow, {"description"}, str)
        revision = workflow["revision"]
        if type(revision) is not int or not 1 <= revision <= (1 << 63) - 1:
            raise ValueError

        for node in graph["nodes"]:
            uuids(node, {"uuid", "workflow_uuid"})
            optional_uuids(
                node,
                {
                    "workflow_node_template_uuid",
                    "parent_uuid",
                    "material_uuid",
                },
            )
            exact(
                node,
                {"create_time", "update_time", "name", "status", "type"},
                str,
            )
            exact(
                node,
                {"meta_data", "pose", "param", "execution_policy"},
                dict,
            )
            for field in ("meta_data", "pose", "param", "execution_policy"):
                normalize_json_object(node[field])
            exact(node, {"disabled", "minimized"}, bool)
            optional(
                node,
                {
                    "description",
                    "icon",
                    "footer",
                    "action_name",
                    "action_type",
                    "script",
                },
                str,
            )

        for edge in graph["edges"]:
            uuids(
                edge,
                {
                    "uuid",
                    "source_node_uuid",
                    "target_node_uuid",
                    "source_handle_uuid",
                    "target_handle_uuid",
                },
            )
            exact(edge, {"create_time", "update_time"}, str)
            exact(edge, {"meta_data"}, dict)
            normalize_json_object(edge["meta_data"])
            optional(edge, {"description"}, str)

        for template in graph["node_templates"]:
            uuids(template, {"uuid", "resource_template_uuid"})
            exact(
                template,
                {
                    "create_time",
                    "update_time",
                    "name",
                    "display_name",
                    "type",
                    "node_type",
                },
                str,
            )
            exact(
                template,
                {
                    "meta_data",
                    "goal",
                    "goal_default",
                    "feedback",
                    "result",
                },
                dict,
            )
            for field in (
                "meta_data",
                "goal",
                "goal_default",
                "feedback",
                "result",
            ):
                normalize_json_object(template[field])
            optional(
                template,
                {
                    "description",
                    "class",
                    "schema",
                    "icon",
                    "header",
                    "footer",
                },
                str,
            )

        for handle in graph["handle_templates"]:
            uuids(handle, {"uuid", "workflow_node_template_uuid"})
            exact(
                handle,
                {
                    "create_time",
                    "update_time",
                    "handle_key",
                    "io_type",
                    "display_name",
                    "type",
                },
                str,
            )
            exact(handle, {"meta_data"}, dict)
            normalize_json_object(handle["meta_data"])
            exact(handle, {"required"}, bool)
            optional(
                handle,
                {"description", "data_source", "data_key"},
                str,
            )

    @staticmethod
    def _require_candidate_graph_containers(graph: Dict[str, Any]) -> None:
        workflow = graph.get("workflow")
        if workflow is not None and not isinstance(workflow, dict):
            raise WorkflowError("candidate_invalid")
        for field in (
            "nodes",
            "edges",
            "node_templates",
            "handle_templates",
        ):
            entities = graph.get(field)
            if entities is None:
                continue
            if not isinstance(entities, list) or any(
                not isinstance(item, dict) for item in entities
            ):
                raise WorkflowError("candidate_invalid")

    @staticmethod
    def _hydrate_backend_catalog_entities(
        entities: List[Dict[str, Any]],
        *,
        persisted: Dict[str, Dict[str, Any]],
        timestamp: str,
        uuid_fields: set[str],
        allowed_fields: set[str],
        required_fields: set[str],
    ) -> List[Dict[str, Any]]:
        hydrated = []
        for item in entities:
            value = {
                key: child
                for key, child in item.items()
                if key in allowed_fields and child is not None
            }
            for field in uuid_fields:
                try:
                    value[field] = validate_uuid(value[field])
                except (KeyError, ValueError):
                    raise WorkflowError("candidate_invalid") from None
            previous = persisted.get(value["uuid"], {})
            hydrated.append(
                {
                    "uuid": value["uuid"],
                    "create_time": previous.get("create_time", timestamp),
                    "update_time": previous.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        WorkflowService._require_backend_read_fields(
            hydrated,
            required_fields,
        )
        return hydrated

    @staticmethod
    def _require_backend_read_fields(
        entities: List[Dict[str, Any]],
        required_fields: set[str],
        *,
        error_code: str = "candidate_invalid",
    ) -> None:
        if any(
            not isinstance(item, dict) or not required_fields.issubset(item)
            for item in entities
        ):
            raise WorkflowError(error_code)

    @classmethod
    def _post_commit_candidate_graph(
        cls,
        graph: Dict[str, Any],
        *,
        workflow: Dict[str, Any],
    ) -> Dict[str, Any]:
        projected = cls._backend_graph_projection(graph)
        projected["workflow"] = {
            **projected["workflow"],
            **workflow,
        }
        return projected

    def _authoring_aggregate(
        self,
        *,
        workflow: Dict[str, Any],
        graph: Dict[str, Any],
        registration: Dict[str, Any],
        source: Optional[Dict[str, Any]],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        draft: Optional[Dict[str, Any]] = None
        diagnostics: List[Dict[str, Any]] = []
        if source is not None:
            if record["observed_draft_hash"] == source["draft_hash"]:
                diagnostics = record["diagnostics"]
            draft = {
                "source_uri": registration["source_uri"],
                **source,
                "diagnostics": diagnostics,
            }

        stored_candidate = record.get("candidate")
        candidate: Optional[Dict[str, Any]] = None
        candidate_stale = False
        if stored_candidate is not None and source is not None:
            catalog_matches = False
            try:
                catalog_matches = (
                    stored_candidate["template_catalog_fingerprint"]
                    == self._catalog_fingerprint()
                )
            except WorkflowError:
                catalog_matches = False
            candidate_current = (
                record["observed_draft_hash"] == source["draft_hash"]
                and stored_candidate["draft_hash"] == source["draft_hash"]
                and stored_candidate["base_workflow_revision"] == workflow["revision"]
                and catalog_matches
            )
            if candidate_current:
                candidate = stored_candidate
            else:
                candidate_stale = True

        applied_source = record.get("applied_source")
        if source is None:
            state = "draft_missing"
        elif candidate_stale:
            state = "candidate_stale"
        elif any(
            str(item.get("severity", "")).lower() == "error" for item in diagnostics
        ):
            state = "draft_invalid"
        elif candidate is not None:
            state = (
                "unapplied_source_only"
                if candidate["changeset"]["kind"] == "source_only"
                else "unapplied_graph"
            )
        elif (
            applied_source is not None
            and applied_source["workflow_revision"] == workflow["revision"]
            and applied_source["source_hash"] == source["draft_hash"]
        ):
            state = "applied"
        else:
            state = "applied_source_stale"

        return {
            "workflow_uuid": workflow["uuid"],
            "workflow_revision": workflow["revision"],
            "state": state,
            "applied_graph": graph,
            "draft": draft,
            "candidate": candidate,
            "applied_source": applied_source,
        }

    def _authoring_lock(self, workflow_uuid: str) -> threading.RLock:
        with self._locks_guard:
            return self._authoring_locks.setdefault(
                workflow_uuid,
                threading.RLock(),
            )

    @staticmethod
    def _normalize_page(page: int, page_size: int) -> Tuple[int, int]:
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        if page_size > 100:
            page_size = 100
        return page, page_size

    @staticmethod
    def _optional_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _validate_hash(value: Optional[str], *, nullable: bool) -> None:
        if value is None:
            if nullable:
                return
            raise WorkflowError("invalid_input")
        if _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("invalid_input")


__all__ = [
    "AuthoringCompiler",
    "WorkflowConflict",
    "WorkflowError",
    "WorkflowService",
]
