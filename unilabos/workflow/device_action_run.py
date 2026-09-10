"""设备单动作运行（DeviceActionRun）的校验、冻结与持久化深模块。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.execution_plan import PLAN_VERSION
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore

MaterialResolver = Callable[[str], Mapping[str, Any] | None]
_MAX_EXECUTION_TIMEOUT_SECONDS = (1 << 63) // 1_000_000_000 - 1


class DeviceActionRunInputError(ValueError):
    """设备单动作运行（DeviceActionRun）的请求或引用不合法。"""


class DeviceActionRunUnavailable(RuntimeError):
    """本地缺少可靠模板或物料解析权威，因而关闭式失败。"""


class DeviceActionRunConflict(RuntimeError):
    """设备单动作运行（DeviceActionRun）的幂等身份发生冲突。"""


class DeviceActionRunService:
    """以一个公共操作隐藏直接设备动作的全部创建不变量。"""

    def __init__(
        self,
        workflow_store: WorkflowStore,
        *,
        material_resolver: MaterialResolver | None,
    ) -> None:
        """绑定工作流写模型与物料身份解析器。

        参数：``workflow_store`` 是本地工作流任务（WorkflowTask）权威；
        ``material_resolver`` 按稳定物料 UUID 返回活动物料摘要。返回无；缺少解析器
        时保留关闭式失败（Fail-closed），不会猜测设备身份。
        """

        self._workflow_store = workflow_store
        self._store = DeviceActionRunStore(workflow_store)
        self._material_resolver = material_resolver

    def create(
        self,
        *,
        material_uuid: str,
        workflow_node_template_uuid: str,
        param: Mapping[str, Any] | None,
        execution_policy: Mapping[str, Any] | None,
        idempotency_key: str,
        description: str | None,
        meta_data: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """校验并原子创建或复用设备单动作运行（DeviceActionRun）。

        参数：``material_uuid`` 是实际设备物料（Material）身份；模板 UUID 决定
        动作合同；``param`` 是最终动作参数；``execution_policy`` 是执行期限策略；
        ``idempotency_key`` 标识同一逻辑创建命令；描述和元数据只作展示与追踪。
        返回 Backend 形状的 ``task/job/created``；引用无效、模板不匹配、参数不满足
        Schema 时抛出输入错误，同幂等键改义时抛出冲突。
        """

        try:
            device_material_uuid = validate_uuid(material_uuid)
            template_uuid = validate_uuid(workflow_node_template_uuid)
        except (TypeError, ValueError):
            raise DeviceActionRunInputError("设备物料或动作模板 UUID 非法") from None
        normalized_key = str(idempotency_key).strip()
        if not normalized_key or len(normalized_key) > 255:
            raise DeviceActionRunInputError("幂等键不能为空且不得超过 255 个字符")
        normalized_param = self._normalize_object(param, field="param")
        normalized_policy = self._normalize_policy(execution_policy)
        normalized_meta_data = self._normalize_object(meta_data, field="meta_data")
        normalized_description = self._normalize_description(description)

        # ``device_material`` 是执行器（Executor）的实际物料身份，不是前端设备别名。
        device_material = self._resolve_material(device_material_uuid)
        # ``edge_local_id`` 是本次执行计划（ExecutionPlan）冻结的具体执行器身份；
        # 创建事务后不允许调度器再从可变物料摘要补齐该事实。
        edge_local_id = self._edge_local_id(device_material)
        try:
            template = self._workflow_store.get_node_template(template_uuid)
        except StoreNotFound:
            raise DeviceActionRunInputError("设备动作模板不存在") from None
        if self._canonical_node_kind(template.get("node_type")) != "device_action":
            raise DeviceActionRunInputError("节点模板不是设备动作")
        if str(device_material.get("resource_template_uuid") or "") != str(
            template["resource_template_uuid"]
        ):
            raise DeviceActionRunInputError("设备物料与动作模板资源类型不匹配")

        effective_param = normalized_param
        if param is None:
            effective_param = dict(
                template.get("goal_default") or template.get("goal") or {}
            )
        # ``locked_material_uuids`` 是动作合同声明会被物理操作的业务物料集合；
        # 这里只校验身份存在，后续持久调度器统一建立作业执行占用（JobExecutionClaim）。
        locked_material_uuids = self._validate_action_param(template, effective_param)
        for locked_material_uuid in locked_material_uuids:
            self._resolve_material(locked_material_uuid)

        request_fingerprint = self._request_fingerprint(
            material_uuid=device_material_uuid,
            template_uuid=template_uuid,
            param=effective_param,
            execution_policy=normalized_policy,
            description=normalized_description,
            meta_data=normalized_meta_data,
        )
        task, job = self._build_execution(
            material_uuid=device_material_uuid,
            edge_local_id=edge_local_id,
            template=template,
            param=effective_param,
            execution_policy=normalized_policy,
            idempotency_key=normalized_key,
            request_fingerprint=request_fingerprint,
            description=normalized_description,
            meta_data=normalized_meta_data,
        )
        try:
            return self._store.create_or_reuse(
                task=task,
                job=job,
                idempotency_key=normalized_key,
                request_fingerprint=request_fingerprint,
            )
        except StoreConflict:
            raise DeviceActionRunConflict("设备单动作幂等身份冲突") from None

    def _resolve_material(self, material_uuid: str) -> Mapping[str, Any]:
        """关闭式解析一个活动物料（Material）身份。

        参数：``material_uuid`` 是实际物料 UUID。返回含 UUID 与资源模板 UUID 的
        权威摘要；解析器未装配、物料不存在或返回身份不一致时抛出输入/可用性错误。
        """

        if self._material_resolver is None:
            raise DeviceActionRunUnavailable("本地物料身份解析器尚未装配")
        material = self._material_resolver(material_uuid)
        if material is None:
            raise DeviceActionRunInputError("引用的物料不存在或不可用")
        try:
            resolved_uuid = validate_uuid(str(material.get("uuid") or ""))
            validate_uuid(str(material.get("resource_template_uuid") or ""))
        except (TypeError, ValueError):
            raise DeviceActionRunUnavailable("本地物料摘要缺少稳定身份") from None
        if resolved_uuid != material_uuid:
            raise DeviceActionRunUnavailable("本地物料解析器返回了不一致身份")
        return material

    @staticmethod
    def _edge_local_id(material: Mapping[str, Any]) -> str:
        """从设备物料摘要解析 Edge 本地执行器身份。

        参数：``material`` 是创建阶段读取的活动设备物料（Material）摘要。
        返回：去除首尾空白的 ``edge_local_id``。异常：直接字段及 ``meta_data``
        映射/JSON 均未提供有效身份时抛 ``DeviceActionRunUnavailable``，确保失败
        发生在任务持久化之前。
        """

        direct_identity = str(material.get("edge_local_id") or "").strip()
        if direct_identity:
            return direct_identity
        # ``material_meta_data`` 是库存权威保存的设备路由元数据；字符串只接受
        # JSON 对象，不从名称、条码或物料 UUID 推断执行器。
        material_meta_data = material.get("meta_data")
        if isinstance(material_meta_data, str):
            try:
                material_meta_data = json.loads(material_meta_data)
            except (TypeError, ValueError):
                material_meta_data = None
        if isinstance(material_meta_data, Mapping):
            nested_identity = str(material_meta_data.get("edge_local_id") or "").strip()
            if nested_identity:
                return nested_identity
        raise DeviceActionRunUnavailable("设备物料缺少明确 edge_local_id")

    @staticmethod
    def _validate_action_param(
        template: Mapping[str, Any],
        param: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """按冻结动作合同校验最终参数并提取物料锁身份。

        参数：``template`` 是已发布动作模板；``param`` 是合并完成的最终参数。
        返回去重稳定排序的物料 UUID；模板未携带第 2 版完整合同或参数非法时关闭失败。
        """

        meta_data = template.get("meta_data")
        unilab_meta = (
            meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        )
        action_schema = (
            unilab_meta.get("action_contract_schema")
            if isinstance(unilab_meta, Mapping)
            else None
        )
        if not isinstance(action_schema, Mapping):
            raise DeviceActionRunUnavailable("动作模板缺少第 2 版冻结 Schema")
        try:
            return compile_material_lock_schema(action_schema).material_lock_uuids(
                param
            )
        except MaterialLockSchemaError as error:
            raise DeviceActionRunInputError(error.message) from None

    @staticmethod
    def _normalize_object(
        value: Mapping[str, Any] | None,
        *,
        field: str,
    ) -> dict[str, Any]:
        """复制一个 JSON 对象并拒绝非对象值。

        参数：``value`` 是待规范化字段，``field`` 是诊断名称。返回独立字典；
        ``None`` 按 Backend 零值规则变成空对象。
        """

        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise DeviceActionRunInputError(f"{field} 必须是对象")
        return dict(value)

    @staticmethod
    def _normalize_policy(value: Mapping[str, Any] | None) -> dict[str, Any]:
        """规范化工作流节点执行策略（WorkflowNodeExecutionPolicy）。

        参数：``value`` 是请求策略对象或 ``None``。返回只含 Backend 当前字段的
        策略；未知字段、布尔值、负数或超过 int64 纳秒范围的秒数均拒绝。
        """

        policy = DeviceActionRunService._normalize_object(
            value,
            field="execution_policy",
        )
        if set(policy) - {"execution_timeout_seconds"}:
            raise DeviceActionRunInputError("execution_policy 包含未知字段")
        timeout = policy.get("execution_timeout_seconds")
        if timeout is None:
            return {}
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout < 0
            or timeout > _MAX_EXECUTION_TIMEOUT_SECONDS
        ):
            raise DeviceActionRunInputError("execution_timeout_seconds 非法")
        return {"execution_timeout_seconds": timeout}

    @staticmethod
    def _normalize_description(value: str | None) -> str | None:
        """规范化可选设备单动作说明。

        参数：``value`` 是请求描述。返回去除首尾空白后的文本；空文本映射为
        ``None``，与 Backend 的可空字段语义一致。
        """

        if value is None:
            return None
        if not isinstance(value, str):
            raise DeviceActionRunInputError("description 必须是字符串")
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _canonical_node_kind(value: Any) -> str:
        """把 Backend 规范节点类型归一为执行器类型。

        参数：``value`` 是持久模板 ``node_type``。返回 ``device_action`` 或空串；
        OS 跟随 Backend，将 wire 值 ``ILab`` 解释为设备动作节点。
        """

        return "device_action" if str(value).strip().lower() == "ilab" else ""

    @staticmethod
    def _request_fingerprint(
        *,
        material_uuid: str,
        template_uuid: str,
        param: Mapping[str, Any],
        execution_policy: Mapping[str, Any],
        description: str | None,
        meta_data: Mapping[str, Any],
    ) -> str:
        """计算设备单动作创建命令的稳定请求指纹。

        参数覆盖会改变执行语义或审计内容的全部 Backend DTO 字段。返回小写
        SHA-256 十六进制摘要，供幂等键冲突检测；指纹不是新的业务身份。
        """

        # ``fingerprint_payload`` 使用显式字段名固定跨重放语义，不包含随机 UUID。
        fingerprint_payload = {
            "material_uuid": material_uuid,
            "workflow_node_template_uuid": template_uuid,
            "param": dict(param),
            "execution_policy": dict(execution_policy),
            "description": description,
            "meta_data": dict(meta_data),
        }
        return hashlib.sha256(
            encode_json(fingerprint_payload, sort_keys=True)
        ).hexdigest()

    @staticmethod
    def _build_execution(
        *,
        material_uuid: str,
        edge_local_id: str,
        template: Mapping[str, Any],
        param: Mapping[str, Any],
        execution_policy: Mapping[str, Any],
        idempotency_key: str,
        request_fingerprint: str,
        description: str | None,
        meta_data: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """冻结一次直接设备动作的 Task、计划与唯一 Job。

        参数：``material_uuid`` 是实际设备物料（Material）的稳定身份；
        ``edge_local_id`` 是已解析并冻结的具体执行器身份；``template`` 是与设备
        物料匹配的已发布动作模板；``param`` 是已校验的最终动作参数；
        ``execution_policy`` 是规范化执行期限策略；``idempotency_key`` 标识同一
        逻辑创建命令；``request_fingerprint`` 固定该命令的执行与审计语义；
        ``description`` 是可空展示说明；``meta_data`` 是规范化追踪元数据。
        返回：可在单事务中写入的工作流任务（WorkflowTask）和工作流节点作业
        （WorkflowNodeJob）；随机 UUID 只用于新聚合，幂等复用由持久层返回既有
        聚合。异常：``template`` 缺少第 2 版冻结动作合同时抛出
        ``DeviceActionRunUnavailable``，其他映射读取或复制错误原样传播。
        """

        # ``node_uuid`` 是计划工作流节点（Planned Workflow Node）的稳定身份，
        # 仅属于本次冻结执行，不会创建可编辑工作流节点定义。
        node_uuid = str(uuid4())
        # ``task_uuid`` 是本次设备单动作调试（D1A）完整运行的稳定工作流任务
        # （WorkflowTask）身份，持久重放不会另行授权新身份。
        task_uuid = str(uuid4())
        # ``job_uuid`` 是该计划节点首次工作流节点作业尝试
        # （WorkflowNodeJobAttempt）的稳定身份，不与任务或节点身份混用。
        job_uuid = str(uuid4())
        node_name = str(template.get("display_name") or template.get("name") or "")
        template_meta_data = template.get("meta_data")
        unilab_meta_data = (
            template_meta_data.get("unilab")
            if isinstance(template_meta_data, Mapping)
            else None
        )
        action_contract_schema = (
            unilab_meta_data.get("action_contract_schema")
            if isinstance(unilab_meta_data, Mapping)
            else None
        )
        if not isinstance(action_contract_schema, Mapping):
            raise DeviceActionRunUnavailable("动作模板缺少第 2 版冻结 Schema")
        node_snapshot = {
            "uuid": node_uuid,
            "workflow_node_template_uuid": template["uuid"],
            "material_uuid": material_uuid,
            "name": node_name,
            "type": template["node_type"],
            "param": dict(param),
            "action_name": template["name"],
            "action_type": template["type"],
            "execution_policy": dict(execution_policy),
        }
        workflow_snapshot = {
            "execution_kind": "ad_hoc_device_action",
            "material_uuid": material_uuid,
            "nodes": [node_snapshot],
            "node_templates": [dict(template)],
        }
        execution_plan = {
            "version": PLAN_VERSION,
            "run_mode": "single_node",
            "target_node_uuid": node_uuid,
            "nodes": [
                {
                    "uuid": node_uuid,
                    "topological_index": 0,
                    "kind": "device_action",
                    "device_id": edge_local_id,
                    "action_name": template["name"],
                    "action_type": template["type"],
                    "material_uuid": material_uuid,
                    "param_schema": deepcopy(dict(action_contract_schema)),
                    "param": dict(param),
                    "execution_policy": dict(execution_policy),
                    "inputs": [],
                    "source_handle_uuids": [],
                    "material_requirements": [],
                }
            ],
            "edges": [],
            "handles": [],
        }
        task = {
            "uuid": task_uuid,
            "description": description,
            "meta_data": dict(meta_data),
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "workflow_snapshot": workflow_snapshot,
            "execution_plan": execution_plan,
            "target_node_uuid": node_uuid,
        }
        job = {
            "uuid": job_uuid,
            "workflow_node_uuid": node_uuid,
            "material_uuid": material_uuid,
            "execution_policy": dict(execution_policy),
            "param": dict(param),
        }
        return task, job


__all__ = [
    "DeviceActionRunConflict",
    "DeviceActionRunInputError",
    "DeviceActionRunService",
    "DeviceActionRunUnavailable",
    "MaterialResolver",
]
