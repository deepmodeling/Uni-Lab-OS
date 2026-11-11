"""工作流入口参数：任务输入校验、节点参数引用替换与声明输出求值。

画布节点不引用模板，入口参数以 param 顶层的
``{"kind": "workflow_input", "parameter": 名}`` 引用；模板节点沿用
``meta_data.unilab.input_bindings``（Handle → 参数名）。执行计划把两者都替换为任务输入的
具体值，并在计划节点上记下 ``entry_bindings``（param 键 → 入口参数名），供暂停时修改判定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from unilabos.server.backend.scheduler.parameters import json_get_exists
from unilabos.server.services.runtime.workflow.value_schema import (
    WorkflowValueSchemaError,
    validate_value,
)
from unilabos.server.services.runtime.workflow.workflow_io import (
    WorkflowIOValidationError,
    normalize_input_contract,
)

_MAX_OUTPUT_DEPTH = 32


class EntryParameterError(ValueError):
    """任务输入不满足工作流输入合同。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}（{path}）")


def declared_input_contract(workflow: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """工作流声明的输入合同；未声明（或没有参数）返回 None。"""

    unilab = _unilab(workflow)
    if unilab.get("input_contract") is None:
        return None
    try:
        contract = normalize_input_contract(unilab.get("input_contract"))
    except WorkflowIOValidationError as error:
        raise EntryParameterError(error.code, error.path) from error
    return contract if contract["parameters"] else None


def resolve_task_input(
    contract: Optional[Mapping[str, Any]],
    submitted: Mapping[str, Any],
) -> dict[str, Any]:
    """按合同校验提交的任务输入并补默认值，返回有效输入。"""

    descriptors = {
        str(item["name"]): item for item in (contract or {}).get("parameters", [])
    }
    unknown = sorted(set(submitted) - set(descriptors))
    if unknown:
        raise EntryParameterError("workflow_input_unknown", f"/input/{unknown[0]}")
    effective: dict[str, Any] = {}
    for name, descriptor in descriptors.items():
        if name in submitted:
            effective[name] = validate_entry_value(descriptor, submitted[name])
        elif "default" in descriptor:
            effective[name] = _plain(descriptor["default"])
        elif descriptor.get("required") is True:
            raise EntryParameterError("workflow_input_required", f"/input/{name}")
    return effective


def validate_entry_value(descriptor: Mapping[str, Any], value: Any) -> Any:
    name = str(descriptor["name"])
    try:
        return validate_value(descriptor["schema"], value, path=f"/input/{name}")
    except WorkflowValueSchemaError as error:
        raise EntryParameterError(error.code, error.path) from error


def entry_reference(value: Any) -> Optional[str]:
    """``{"kind": "workflow_input", "parameter": 名}`` 返回参数名，否则 None。"""

    if (
        isinstance(value, Mapping)
        and value.get("kind") == "workflow_input"
        and isinstance(value.get("parameter"), str)
    ):
        return str(value["parameter"])
    return None


def substitute_entry_references(
    param: Mapping[str, Any],
    entry_input: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """把 param 顶层的入口参数引用换成任务输入值；未提供的可选参数对应键移除。"""

    from unilabos.protocol.runtime.input_binding import map_input_bindings, literal_input

    result = {}
    bindings: dict[str, str] = {}
    def resolve(name: str) -> Any:
        if name not in entry_input:
            raise EntryParameterError("workflow_input_required", f"/input/{name}")
        return literal_input(entry_input[name])
    for key, value in param.items():
        name = entry_reference(value)
        if name is not None:
            bindings[str(key)] = name
            if name not in entry_input:
                continue
        result[key] = map_input_bindings(value, resolve)
    return result, bindings


def resolve_declared_output(
    snapshot: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    task_input: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """按 output_bindings 从节点运行结果求任务输出；未声明输出返回 None。"""

    unilab = _unilab(snapshot.get("workflow") or {})
    contract = unilab.get("output_contract")
    outputs = contract.get("outputs") if isinstance(contract, Mapping) else None
    if not outputs:
        return None
    bindings = unilab.get("output_bindings")
    bindings = bindings if isinstance(bindings, Mapping) else {}
    resolver = _OutputResolver(snapshot, runs, task_input)
    result: dict[str, Any] = {}
    for descriptor in outputs:
        name = str(descriptor.get("name") or "")
        found, value = resolver.resolve(bindings.get(name), depth=0)
        if not found:
            raise EntryParameterError("workflow_output_missing", f"/output/{name}")
        try:
            result[name] = validate_value(descriptor["schema"], value, path=f"/output/{name}")
        except WorkflowValueSchemaError as exc:
            raise EntryParameterError("workflow_output_invalid", exc.path) from exc
    return result


class _OutputResolver:
    def __init__(
        self,
        snapshot: Mapping[str, Any],
        runs: Sequence[Mapping[str, Any]],
        task_input: Mapping[str, Any],
    ) -> None:
        self._nodes = {str(item["uuid"]): item for item in snapshot.get("nodes") or []}
        self._handles = {
            str(item["uuid"]): item for item in snapshot.get("handle_templates") or []
        }
        self._runs = {str(item["workflow_node_uuid"]): item for item in runs}
        self._input = task_input

    def resolve(self, binding: Any, *, depth: int) -> tuple[bool, Any]:
        if not isinstance(binding, Mapping) or depth > _MAX_OUTPUT_DEPTH:
            return False, None
        if binding.get("kind") == "workflow_input":
            name = str(binding.get("parameter") or "")
            return (name in self._input), self._input.get(name)
        if binding.get("kind") != "node_output":
            return False, None
        node_uuid = str(binding.get("workflow_node_uuid") or "")
        if "data_key" in binding:
            return self._run_value(node_uuid, str(binding.get("data_key") or ""))
        handle_uuid = str(binding.get("source_handle_uuid") or "")
        node = self._nodes.get(node_uuid)
        composite = _unilab(node).get("composite") if node is not None else None
        if isinstance(composite, Mapping):
            # 组合调用节点没有运行：沿冻结的输出映射追到展开后的叶子
            mapping = (composite.get("source_mappings") or {}).get(handle_uuid)
            if isinstance(mapping, Mapping) and mapping.get("kind") == "workflow_input":
                argument = (node.get("param") or {}).get(str(mapping.get("parameter") or ""))
                name = entry_reference(argument)
                if name is not None:
                    return (name in self._input), self._input.get(name)
                return argument is not None, argument
            return self.resolve(mapping, depth=depth + 1)
        handle = self._handles.get(handle_uuid)
        if handle is None:
            return False, None
        path = str(handle.get("data_key") or handle.get("handle_key") or "").strip()
        return self._run_value(node_uuid, path)

    def _run_value(self, node_uuid: str, path: str) -> tuple[bool, Any]:
        run = self._runs.get(node_uuid)
        if run is None or run.get("status") != "succeeded":
            return False, None
        value = (run.get("return_info") or {}).get("return_value")
        if not path:
            return True, value
        return json_get_exists(value, path)


def _unilab(entity: Any) -> Mapping[str, Any]:
    meta_data = entity.get("meta_data") if isinstance(entity, Mapping) else None
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab if isinstance(unilab, Mapping) else {}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "EntryParameterError",
    "declared_input_contract",
    "entry_reference",
    "resolve_declared_output",
    "resolve_task_input",
    "substitute_entry_references",
    "validate_entry_value",
]
