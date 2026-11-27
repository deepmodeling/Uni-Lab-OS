"""设备单动作运行（DeviceActionRun）的校验、冻结与持久化深模块。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import uuid4

from unilabos.registry.action_resource_contract import (
    ActionResourceContractError,
    normalize_action_resource_contract,
)
from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.execution_plan import PLAN_VERSION
from unilabos.workflow.execution_resource_policy import (
    ExecutionResourcePolicyError,
    merge_action_resource_policy,
)
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore
from unilabos.workflow.task_input import SiteSelectionResolver

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
        site_selection_resolver: SiteSelectionResolver | None = None,
        definition_store: WorkflowStore | None = None,
    ) -> None:
        """绑定工作流写模型与物料身份解析器。

        参数：``workflow_store`` 是本地工作流任务（WorkflowTask）权威；
        ``definition_store`` 是设备动作模板的内存定义目录，省略时兼容旧的单库调用；
        ``material_resolver`` 按稳定物料 UUID 返回活动物料摘要。返回无；缺少解析器
        时保留关闭式失败（Fail-closed），不会猜测设备身份。
        """

        self._workflow_store = workflow_store
        self._definition_store = definition_store or workflow_store
        self._store = DeviceActionRunStore(workflow_store)
        self._material_resolver = material_resolver
        self._site_selection_resolver = site_selection_resolver

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
            template = self._definition_store.get_node_template(template_uuid)
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

        executor_kind, action_resource_contract = self._action_execution_contract(
            template
        )
        try:
            frozen_policy = merge_action_resource_policy(
                action_resource_contract,
                normalized_policy,
            )
        except ExecutionResourcePolicyError as error:
            raise DeviceActionRunUnavailable(str(error)) from None
        frozen_param, frozen_policy = self._freeze_transfer_target_site(
            executor_kind=executor_kind,
            action_resource_contract=action_resource_contract,
            param=effective_param,
            execution_policy=frozen_policy,
        )

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
            executor_kind=executor_kind,
            action_resource_contract=action_resource_contract,
            param=frozen_param,
            execution_policy=frozen_policy,
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
        except StoreConflict as error:
            raise DeviceActionRunConflict(str(error)) from None

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
    def action_fingerprint(template: Mapping[str, Any]) -> str:
        """以完整动作内容生成版本，稳定模板 UUID 不能代替版本。"""
        definition = {key: value for key, value in template.items() if key not in {"create_time", "update_time"}}
        return "sha256:" + hashlib.sha256(encode_json(definition, sort_keys=True)).hexdigest()

    def list_manual_actions(self, material_uuid: str) -> list[dict[str, Any]]:
        material = self._resolve_material(validate_uuid(material_uuid))
        binding = {"material_uuid": material["uuid"], "edge_local_id": self._edge_local_id(material)}
        return [{"action_id": template["uuid"], "template_uuid": template["uuid"],
                 "name": template["name"], "display_name": template.get("display_name") or template["name"],
                 "action_version": self.action_fingerprint(template),
                 "parameter_schema": template.get("meta_data", {}).get("unilab", {}).get("action_contract_schema", {}).get("properties", {}).get("goal", {}),
                 "defaults": template.get("goal_default") or template.get("goal") or {}, "device_binding": binding}
                for template in self._definition_store.list_device_action_templates(str(material["resource_template_uuid"]))]

    def prepare_manual(self, request: Mapping[str, Any], *, idempotency_key: str, metadata: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """冻结任意已提供动作及输入，不经过占用、Site 选择或资源合同准入。"""
        material_uuid = validate_uuid(str(request["device_id"]))
        template = self._definition_store.get_node_template(validate_uuid(str(request["action_id"])))
        material = self._resolve_material(material_uuid)
        if template.get("resource_template_uuid") != material["resource_template_uuid"] or self._canonical_node_kind(template.get("node_type")) != "device_action":
            raise DeviceActionRunInputError("设备与动作不匹配")
        if request.get("action_version") != self.action_fingerprint(template):
            raise StoreConflict("action_definition_changed")
        param = self._normalize_object(request.get("parameters"), field="parameters")
        self._validate_action_param(template, param)
        executor_kind, contract = self._action_execution_contract(template)
        return self._build_execution(material_uuid=material_uuid, edge_local_id=self._edge_local_id(material),
            template=template, executor_kind="device_action", action_resource_contract={},
            param=param, execution_policy={}, idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(encode_json(dict(request), sort_keys=True)).hexdigest(),
            description="错误处置手动动作", meta_data=metadata)

    @staticmethod
    def _action_execution_contract(
        template: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """读取模板冻结的执行责任与动作资源合同。"""

        metadata = template.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        explicit_kind = str(
            (unilab.get("executor_kind") if isinstance(unilab, Mapping) else "")
            or ""
        ).strip()
        executor_kind = explicit_kind or "device_action"
        if executor_kind not in {"device_action", "material_transfer"}:
            raise DeviceActionRunUnavailable("设备动作模板声明了不支持的执行责任")
        schema = (
            unilab.get("action_contract_schema")
            if isinstance(unilab, Mapping)
            else None
        )
        extension = (
            schema.get("x-unilabos-action-contract")
            if isinstance(schema, Mapping)
            else None
        )
        raw_contract = (
            extension.get("resource_contract")
            if isinstance(extension, Mapping)
            else None
        )
        try:
            contract = normalize_action_resource_contract(raw_contract)
        except ActionResourceContractError as error:
            raise DeviceActionRunUnavailable(error.message) from None
        if executor_kind == "material_transfer" and not isinstance(
            contract.get("transfer"), Mapping
        ):
            raise DeviceActionRunUnavailable("物料转移动作缺少冻结转运资源合同")
        return executor_kind, contract

    def _freeze_transfer_target_site(
        self,
        *,
        executor_kind: str,
        action_resource_contract: Mapping[str, Any],
        param: Mapping[str, Any],
        execution_policy: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """把临时转运动作的人类库位引用冻结为稳定 Site 候选。"""

        frozen_param = dict(param)
        frozen_policy = dict(execution_policy)
        transfer = action_resource_contract.get("transfer")
        if executor_kind != "material_transfer" or not isinstance(
            transfer, Mapping
        ):
            return frozen_param, frozen_policy
        site_parameters = tuple(
            name
            for field in ("target_site_uuid_param", "target_site_name_param")
            if (name := str(transfer.get(field) or "").strip())
        )
        references = [
            str(frozen_param.get(name) or "").strip()
            for name in site_parameters
            if str(frozen_param.get(name) or "").strip()
        ]
        if not references:
            return frozen_param, frozen_policy
        if len(set(references)) != 1:
            raise DeviceActionRunInputError("目标库位 UUID 与名称参数冲突")
        owner_uuid = self._resource_parameter_uuid(
            frozen_param,
            str(transfer.get("target_owner_param") or ""),
            label="目标库位所属资源",
        )
        occupant_uuid = self._resource_parameter_uuid(
            frozen_param,
            str(transfer.get("material_param") or ""),
            label="待搬运物料",
        )
        if self._site_selection_resolver is None:
            raise DeviceActionRunUnavailable("设备单动作缺少库位选择权威")
        request = {
            "version": 1,
            "owner_material_uuid": owner_uuid,
            "occupant_material_uuid": occupant_uuid,
            "group_key": "",
            "exact_site_reference": references[0],
            "strategy": "sort_order",
        }
        try:
            resolution = self._site_selection_resolver(request)
        except Exception as error:
            raise DeviceActionRunInputError("目标库位无法由库存权威唯一解析") from error
        raw_site_uuids = resolution.get("site_uuids")
        if (
            not isinstance(raw_site_uuids, Sequence)
            or isinstance(raw_site_uuids, (str, bytes))
            or not raw_site_uuids
        ):
            raise DeviceActionRunUnavailable("库位选择权威没有返回候选 Site UUID")
        try:
            site_uuids = [validate_uuid(str(value)) for value in raw_site_uuids]
        except (TypeError, ValueError):
            raise DeviceActionRunUnavailable("库位选择权威返回了非法 Site UUID") from None
        if len(set(site_uuids)) != len(site_uuids):
            raise DeviceActionRunUnavailable("库位选择权威返回了重复 Site UUID")
        fingerprint = str(resolution.get("fingerprint") or "").strip()
        if not fingerprint:
            raise DeviceActionRunUnavailable("库位选择权威缺少部署指纹")
        frozen_policy["target_site_group"] = site_uuids
        frozen_policy["target_site_selection"] = {
            "version": 1,
            "owner_material_uuid": owner_uuid,
            "group_key": "",
            "requested_reference": references[0],
            "strategy": "sort_order",
            "site_uuids": site_uuids,
            "fingerprint": fingerprint,
        }
        for parameter in site_parameters:
            frozen_param.pop(parameter, None)
        return frozen_param, frozen_policy

    @staticmethod
    def _resource_parameter_uuid(
        param: Mapping[str, Any],
        parameter: str,
        *,
        label: str,
    ) -> str:
        """从已校验动作参数读取一个 ResourceSlot 稳定身份。"""

        value = param.get(parameter)
        if not parameter or not isinstance(value, Mapping):
            raise DeviceActionRunInputError(f"{label}没有冻结资源身份")
        try:
            return validate_uuid(str(value.get("uuid") or ""))
        except (TypeError, ValueError):
            raise DeviceActionRunInputError(f"{label} UUID 非法") from None

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
        executor_kind: str,
        action_resource_contract: Mapping[str, Any],
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
        always_free = bool(
            unilab_meta_data.get("always_free", False)
            if isinstance(unilab_meta_data, Mapping)
            else False
        )
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
                    "kind": executor_kind,
                    "device_id": edge_local_id,
                    "action_name": template["name"],
                    "action_type": template["type"],
                    "always_free": always_free,
                    "material_uuid": material_uuid,
                    "param_schema": deepcopy(dict(action_contract_schema)),
                    "action_resource_contract": deepcopy(
                        dict(action_resource_contract)
                    ),
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
            "executor_kind": executor_kind,
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
