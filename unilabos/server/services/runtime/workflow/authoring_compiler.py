"""工作流作者编译器：dev ``@workflow`` 源码 ⇄ Backend 形状工作流图（实现 ``AuthoringCompiler``）。

与设备包工作流模板共用**同一个**静态编译器 ``registry/workflow_ast.py``（AST 子集，不 import、
不执行作者源码）和同一条模板实例化路径 ``registry/workflows.py``；不另起第三套语法。

- ``compile``：源码 → 候选图 + 变更集 + 源码映射（每步语句的行列范围）。节点身份默认由
  工作流 UUID + 步骤序号确定性派生；语句上一行的 ``# unilab:node_uuid <uuid>`` 锚点（V1 同格式）
  显式指定身份。源码表达不了的字段（画布位置、模板引用、人工确认配置、其它元数据）沿用
  已应用图里同身份节点的值，连线只保留两端仍存在的。
- ``generate_python``：图 → 规范源码（每个节点带身份锚点），再回编译确认图不变；表达不了的结构
  （条件分支、脚本、人工确认、组合调用、具名结果）返回诊断而不是近似源码。
- ``validate``：源码与图的语义固定点校验（回编译后图不变）。

V1 的 ``authoring_engine`` 语法（``device()`` 句柄、物料源等）不移植：dev 的作者语法就是设备包
``@workflow`` 语法，两者语义差异见 ``docs/developer_guide/api_v1_route_map.md``。
"""

from __future__ import annotations

import hashlib
import json
import keyword
from collections.abc import Callable, Mapping
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from unilabos.protocol.runtime.workflow import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    validate_uuid,
)
from unilabos.protocol.utils.json_codec import encode_json, strict_json_equal

COMPILER_VERSION = "unilabos-authoring/dev-ast-1"
_IO_KEYS = ("input_contract", "output_contract", "output_bindings")
#: 源码不表达、按身份沿用已应用图的节点字段
_PRESERVED_NODE_FIELDS = ("pose", "icon", "footer", "minimized", "manual_confirmation")
#: 源码表达的节点元数据键；其余元数据沿用已应用图
_SOURCE_META_KEYS = ("target_device_id", "inventory_mode", "inventory_requirements")
_JSON_TO_PYTHON = {"integer": "int", "number": "float", "string": "str", "boolean": "bool", "object": "dict", "array": "list"}


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(encode_json(value, sort_keys=True)).hexdigest()


def _error(code: str, message: str, *, line: Optional[int] = None, column: Optional[int] = None,
           python_source: str = "") -> Dict[str, Any]:
    diagnostic: Dict[str, Any] = {"severity": "error", "code": code, "message": message}
    if line is not None and python_source:
        lines = python_source.splitlines() or [""]
        if 1 <= line <= len(lines):
            width = len(lines[line - 1].encode("utf-8"))
            start = min(max(column or 1, 1), width + 1)
            diagnostic["source_range"] = {
                "start_line": line, "start_column": start, "end_line": line, "end_column": width + 1,
            }
    return diagnostic


def _semantic_node(node: Mapping[str, Any]) -> Dict[str, Any]:
    return WorkflowNodeWrite.model_validate(
        {field: node[field] for field in WorkflowNodeWrite.model_fields if field in node}
    ).model_dump()


def _semantic_edge(edge: Mapping[str, Any]) -> Dict[str, Any]:
    return WorkflowEdgeWrite.model_validate(
        {field: edge[field] for field in WorkflowEdgeWrite.model_fields if field in edge}
    ).model_dump()


def candidate_changeset(graph: Mapping[str, Any], applied: Mapping[str, Any]) -> Dict[str, Any]:
    """与 ``WorkflowService._validate_candidate_bundle_semantics`` 同口径的变更集。"""

    nodes = {item["uuid"]: _semantic_node(item) for item in graph["nodes"]}
    edges = {item["uuid"]: _semantic_edge(item) for item in graph["edges"]}
    old_nodes = {item["uuid"]: _semantic_node(item) for item in applied["nodes"]}
    old_edges = {item["uuid"]: _semantic_edge(item) for item in applied["edges"]}

    def diff(new: Dict[str, Any], old: Dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        return (
            sorted(set(new) - set(old)),
            sorted(key for key in set(new) & set(old) if not strict_json_equal(new[key], old[key])),
            sorted(set(old) - set(new)),
        )

    created_nodes, updated_nodes, deleted_nodes = diff(nodes, old_nodes)
    created_edges, updated_edges, deleted_edges = diff(edges, old_edges)
    reserved_changed = not strict_json_equal(
        (graph["workflow"].get("meta_data") or {}).get("unilab"),
        (applied["workflow"].get("meta_data") or {}).get("unilab"),
    )
    changed = reserved_changed or any(
        (created_nodes, updated_nodes, deleted_nodes, created_edges, updated_edges, deleted_edges)
    )
    return {
        "kind": "graph" if changed else "source_only",
        "created_node_uuids": created_nodes,
        "updated_node_uuids": updated_nodes,
        "deleted_node_uuids": deleted_nodes,
        "created_edge_uuids": created_edges,
        "updated_edge_uuids": updated_edges,
        "deleted_edge_uuids": deleted_edges,
        "reserved_metadata_changed": reserved_changed,
    }


def _default_catalog() -> Any:
    from unilabos.registry.workflows import DeviceCatalog
    from unilabos.server.backend.composition import get_materials_service

    return DeviceCatalog.from_materials_service(get_materials_service())


def _default_registry_fingerprint() -> str:
    from unilabos.server.services.runtime.registry import get_registry_service

    registry = get_registry_service()
    return registry.active_fingerprint() if registry is not None else ""


class WorkflowAuthoringCompiler:
    """``AuthoringCompiler`` 的 dev 实现（同时提供 V1 ``/authoring/*`` 三个纯转换）。"""

    compiler_version = COMPILER_VERSION

    def __init__(
        self,
        *,
        catalog_provider: Callable[[], Any] = _default_catalog,
        registry_fingerprint: Callable[[], str] = _default_registry_fingerprint,
    ) -> None:
        self._catalog_provider = catalog_provider
        self._registry_fingerprint = registry_fingerprint

    @property
    def template_catalog_fingerprint(self) -> str:
        """设备目录（设备 ID → 设备类 / 物料）与生效注册表的指纹；变化后旧候选不能应用。"""

        catalog = self._catalog_provider()
        return _sha256({
            "compiler": COMPILER_VERSION,
            "registry": self._registry_fingerprint(),
            "devices": getattr(catalog, "by_device_id", {}),
        })

    # ── 源码 → 图 ──────────────────────────────────────────────────────

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: Mapping[str, Any],
    ) -> Dict[str, Any]:
        fingerprint = self.template_catalog_fingerprint
        failure = {
            "graph": None, "normalized_python_source": None, "source_map": [], "changeset": None,
            "compiler_version": COMPILER_VERSION, "template_catalog_fingerprint": fingerprint,
        }
        try:
            graph, source_map = self._compile_graph(
                workflow_uuid=validate_uuid(workflow_uuid), python_source=python_source, applied_graph=applied_graph,
            )
        except _CompileError as exc:
            return {**failure, "diagnostics": [exc.diagnostic]}
        return {
            "diagnostics": [],
            "graph": graph,
            "normalized_python_source": python_source,
            "source_map": source_map,
            "changeset": candidate_changeset(graph, applied_graph),
            "compiler_version": COMPILER_VERSION,
            "template_catalog_fingerprint": fingerprint,
        }

    def preserve_author_source(self, *, compilation: Any, python_source: str, **_: Any) -> Any:
        """规范化即恒等：作者源码原样保留。"""

        return compilation

    def _compile_graph(
        self, *, workflow_uuid: str, python_source: str, applied_graph: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        from unilabos.registry.workflow_ast import WorkflowSourceError, compile_workflow_source
        from unilabos.registry.workflows import (
            WorkflowTemplateBindingError,
            build_workflow_template_payload,
            materialize_workflow_template,
        )

        try:
            program = compile_workflow_source(python_source)
        except WorkflowSourceError as exc:
            raise _CompileError(_error("authoring_syntax", str(exc), line=exc.line, column=exc.column,
                                       python_source=python_source)) from None
        definition = SimpleNamespace(
            collect=lambda: program.ctx,
            source_path=f"authoring:{program.function_name}",
            uuid=workflow_uuid,
            display_name=program.display_name or program.function_name,
            description=program.description,
            tags=program.tags,
            guide=None,
        )
        try:
            template = build_workflow_template_payload(definition)  # type: ignore[arg-type]
            payload = materialize_workflow_template(
                template, self._catalog_provider(), {}, instance_uuid=workflow_uuid, node_uuids=program.anchors,
            )
        except (WorkflowTemplateBindingError, ValueError) as exc:
            raise _CompileError(_error("graph_invalid", str(exc))) from None

        applied_nodes = {str(item["uuid"]): item for item in applied_graph.get("nodes") or []}
        catalog = self._catalog_provider()
        for node in payload["nodes"]:
            if node.get("type") == "device_action" and not node.get("action_type"):
                node["action_type"] = _registry_action_type(catalog, node) or "UniLabJsonCommand"
        nodes = [self._merge_applied(node, applied_nodes.get(str(node["uuid"]))) for node in payload["nodes"]]
        node_ids = {node["uuid"] for node in nodes}
        handle_ids = {str(item["uuid"]) for item in applied_graph.get("handle_templates") or []}
        edges = [
            deepcopy(dict(edge)) for edge in applied_graph.get("edges") or []
            if edge.get("source_node_uuid") in node_ids and edge.get("target_node_uuid") in node_ids
            and str(edge.get("source_handle_uuid")) in handle_ids and str(edge.get("target_handle_uuid")) in handle_ids
        ]
        workflow = deepcopy(dict(applied_graph["workflow"]))
        meta_data = dict(workflow.get("meta_data") or {})
        unilab = dict(meta_data.get("unilab") or {})
        for key in _IO_KEYS:
            unilab.pop(key, None)
        if payload.get("io"):
            unilab.update(deepcopy(payload["io"]))
        unilab.pop("resource_scopes", None)
        if payload.get("resource_scopes"):
            unilab["resource_scopes"] = self._normalized_scopes(payload["resource_scopes"], workflow)
        if unilab:
            meta_data["unilab"] = unilab
        else:
            meta_data.pop("unilab", None)
        workflow["meta_data"] = meta_data
        graph = {
            "workflow": workflow,
            "nodes": nodes,
            "edges": edges,
            "node_templates": deepcopy(list(applied_graph.get("node_templates") or [])),
            "handle_templates": deepcopy(list(applied_graph.get("handle_templates") or [])),
        }
        if "input_contract" in unilab:
            graph = self._normalized_io(graph)
        key_to_uuid = {str(template_node["key"]): node["uuid"] for template_node, node in zip(template["nodes"], payload["nodes"])}
        source_map = [
            {"workflow_node_uuid": key_to_uuid[key], "start_line": start_line, "start_column": start_column,
             "end_line": end_line, "end_column": end_column}
            for key, (start_line, start_column, end_line, end_column) in sorted(program.step_ranges.items())
            if key in key_to_uuid
        ]
        return graph, source_map

    @staticmethod
    def _merge_applied(node: Dict[str, Any], applied: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """源码表达不了的字段按身份沿用已应用图；同一动作的模板节点保留模板引用。"""

        result = dict(node)
        result.setdefault("pose", {})
        if applied is None:
            return result
        if not result.get("action_type") and applied.get("action_name") == result.get("action_name"):
            result["action_type"] = applied.get("action_type")
        for field in _PRESERVED_NODE_FIELDS:
            if field in applied:
                result[field] = deepcopy(applied[field])
        if (
            applied.get("workflow_node_template_uuid")
            and applied.get("action_name") == result.get("action_name")
            and result.get("type") == "device_action"
        ):
            result["workflow_node_template_uuid"] = applied["workflow_node_template_uuid"]
            result["type"] = applied["type"]
            result["action_type"] = applied.get("action_type") or result.get("action_type")
            if applied.get("material_uuid"):
                result["material_uuid"] = applied["material_uuid"]
        applied_meta = applied.get("meta_data") or {}
        meta_data = {key: deepcopy(value) for key, value in applied_meta.items() if key not in _SOURCE_META_KEYS}
        meta_data.update(result.get("meta_data") or {})
        if meta_data.get("inventory_mode") == "task" and "inventory_mode" not in applied_meta:
            # task 是默认库存时机，已应用节点没写时不当作变更
            meta_data.pop("inventory_mode")
        result["meta_data"] = meta_data
        if not result.get("description") and applied.get("description"):
            result["description"] = applied["description"]
        return result

    @staticmethod
    def _normalized_scopes(scopes: List[Dict[str, Any]], workflow: Mapping[str, Any]) -> List[Dict[str, Any]]:
        from unilabos.protocol.runtime.resource_intervals import WorkflowResourceScopesRequest

        try:
            checked = WorkflowResourceScopesRequest(revision=int(workflow.get("revision") or 1), scopes=scopes)
        except ValueError as exc:
            raise _CompileError(_error("graph_invalid", f"资源作用域无效：{exc}")) from None
        return [scope.model_dump(exclude_none=True) for scope in checked.scopes]

    @staticmethod
    def _normalized_io(graph: Dict[str, Any]) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.workflow_io import (
            WorkflowIOValidationError,
            validate_workflow_graph_io,
        )

        try:
            checked = validate_workflow_graph_io(graph)
        except WorkflowIOValidationError as exc:
            raise _CompileError(_error("graph_invalid", f"{exc.code}（{exc.path}）")) from None
        unilab = graph["workflow"]["meta_data"]["unilab"]
        unilab.update({"input_contract": checked.input_contract.to_dict(),
                       "output_contract": checked.output_contract.to_dict(),
                       "output_bindings": checked.output_bindings})
        return graph

    # ── 图 → 源码 ──────────────────────────────────────────────────────

    def generate_python(
        self, *, workflow_uuid: str, workflow_revision: int, graph: Mapping[str, Any], source_uri: str,
    ) -> Dict[str, Any]:
        fingerprint = self.template_catalog_fingerprint
        failure = {
            "graph": None, "normalized_python_source": None, "source_map": [], "changeset": None,
            "compiler_version": COMPILER_VERSION, "template_catalog_fingerprint": fingerprint,
        }
        try:
            python_source = _PythonRenderer(graph, self._catalog_provider()).render()
        except _CompileError as exc:
            return {**failure, "diagnostics": [exc.diagnostic]}
        return self.validate(
            workflow_uuid=workflow_uuid, workflow_revision=workflow_revision, graph=graph,
            python_source=python_source, source_uri=source_uri,
        )

    def validate(
        self, *, workflow_uuid: str, workflow_revision: int, graph: Mapping[str, Any], python_source: str,
        source_uri: str,
    ) -> Dict[str, Any]:
        result = self.compile(
            workflow_uuid=workflow_uuid, workflow_revision=workflow_revision, python_source=python_source,
            source_uri=source_uri, applied_graph=graph,
        )
        changeset = result.get("changeset")
        if result["graph"] is not None and changeset is not None and changeset["kind"] != "source_only":
            changed = changeset["created_node_uuids"] + changeset["updated_node_uuids"] + changeset["deleted_node_uuids"]
            return {
                **result, "graph": None, "normalized_python_source": None, "source_map": [], "changeset": None,
                "diagnostics": [_error(
                    "graph_not_representable",
                    "源码与工作流图语义不一致" + (f"：节点 {', '.join(changed[:5])}" if changed else "（工作流输入输出或连线）"),
                )],
            }
        return result


def _registry_action_type(catalog: Any, node: Mapping[str, Any]) -> str:
    """动作类型取生效注册表（权威进程没有本地 lab_registry）。"""

    device_id = str((node.get("meta_data") or {}).get("target_device_id") or "")
    device_class = str((getattr(catalog, "by_device_id", {}).get(device_id) or {}).get("class") or "")
    if not device_class:
        return ""
    try:
        from unilabos.server.services.runtime.registry import get_registry_service

        registry = get_registry_service()
        definition = registry.action_definition(device_class, str(node.get("action_name") or "")) if registry else None
    except Exception:  # noqa: BLE001 - 注册表不可用时按默认动作类型
        return ""
    return str((definition or {}).get("type") or "")


class _CompileError(Exception):
    def __init__(self, diagnostic: Dict[str, Any]) -> None:
        super().__init__(diagnostic["message"])
        self.diagnostic = diagnostic


def _python_literal(value: Any, references: Callable[[Mapping[str, Any]], Optional[str]]) -> str:
    """JSON 值 → Python 字面量源码；工作流输入 / 节点结果引用渲染为名称表达式。"""

    if isinstance(value, Mapping):
        reference = references(value)
        if reference is not None:
            return reference
        items = ", ".join(f"{json.dumps(str(key), ensure_ascii=False)}: {_python_literal(item, references)}"
                          for key, item in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item, references) for item in value) + "]"
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, float) and value != value:
        raise _CompileError(_error("graph_not_representable", "参数不能是 NaN"))
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


class _PythonRenderer:
    """图 → ``@workflow`` 源码；只覆盖源码语法能无损表达的结构。"""

    def __init__(self, graph: Mapping[str, Any], catalog: Any) -> None:
        self.graph = graph
        self.catalog = catalog
        self.nodes = {str(node["uuid"]): node for node in graph.get("nodes") or []}
        self.variables: Dict[str, str] = {}
        self.lines: List[str] = []

    def fail(self, message: str) -> None:
        raise _CompileError(_error("graph_not_representable", message))

    def device_of(self, node: Mapping[str, Any]) -> str:
        meta = node.get("meta_data") or {}
        if meta.get("target_device_id"):
            return str(meta["target_device_id"])
        material = str(node.get("material_uuid") or "")
        for device_id, info in getattr(self.catalog, "by_device_id", {}).items():
            if info.get("uuid") == material:
                return device_id
        self.fail(f"节点 {node.get('name')} 缺少执行设备 ID")
        return ""

    def reference(self, value: Mapping[str, Any]) -> Optional[str]:
        kind = value.get("kind")
        if kind == "workflow_input" and set(value) == {"kind", "parameter"}:
            name = str(value["parameter"])
            if not name.isidentifier() or keyword.iskeyword(name):
                self.fail(f"工作流输入名 {name!r} 不是合法 Python 标识符")
            return name
        if kind == "node_output" and set(value) <= {"kind", "workflow_node_uuid", "data_key"}:
            variable = self.variables.get(str(value.get("workflow_node_uuid")))
            if variable is None:
                self.fail("节点结果引用的来源节点不在当前作用域（需先声明来源节点）")
            path = str(value.get("data_key") or "")
            if "\\" in path:
                self.fail("结果路径含转义字符，暂不支持生成源码")
            expression = variable
            for segment in [item for item in path.split(".") if item]:
                if segment.isidentifier() and not keyword.iskeyword(segment):
                    expression += f".{segment}"
                else:
                    expression += f"[{json.dumps(segment, ensure_ascii=False)}]"
            return expression
        return None

    def ordered(self, parent: Optional[str]) -> List[Dict[str, Any]]:
        children = [node for node in self.nodes.values() if (node.get("parent_uuid") or None) == parent]
        ids = {str(node["uuid"]) for node in children}
        remaining = sorted(children, key=lambda node: (str(node.get("create_time") or ""), str(node["uuid"])))
        ordered: List[Dict[str, Any]] = []
        done: set[str] = set()
        while remaining:
            ready = [node for node in remaining if all(
                dependency in done or dependency not in ids
                for dependency in (node.get("execution_policy") or {}).get("depends_on", []))]
            if not ready:
                self.fail("节点依赖存在环")
            ordered.append(ready[0])
            done.add(str(ready[0]["uuid"]))
            remaining.remove(ready[0])
        return ordered

    def emit(self, indent: int, text: str) -> None:
        self.lines.append("    " * indent + text)

    def anchor(self, indent: int, node: Mapping[str, Any]) -> None:
        self.emit(indent, f"# unilab:node_uuid {node['uuid']}")

    def render_block(self, parent: Optional[str], indent: int) -> None:
        for node in self.ordered(parent):
            node_type = str(node.get("type") or "")
            policy = node.get("execution_policy") or {}
            if policy.get("condition_guards") or set(policy) - {"depends_on"}:
                self.fail(f"节点 {node.get('name')} 的执行策略无法用源码表达")
            if node.get("disabled"):
                self.fail(f"节点 {node.get('name')} 已禁用，源码无法表达")
            if node_type in {"device_action", "ILab", "ilab", "device", "action", "resource_action"}:
                self.render_action(node, indent)
            elif node_type == "loop":
                self.render_loop(node, indent)
            elif node_type == "group":
                self.render_group(node, indent)
            else:
                self.fail(f"节点类型 {node_type!r} 暂不支持生成源码")

    def render_action(self, node: Mapping[str, Any], indent: int) -> None:
        if (node.get("meta_data") or {}).get("unilab", {}).get("composite"):
            self.fail("组合调用暂不支持生成源码")
        target = f"{self.device_of(node)}/{node.get('action_name') or ''}"
        variable = f"step_{len(self.variables) + 1}"
        arguments = [json.dumps(target, ensure_ascii=False), _python_literal(node.get("param") or {}, self.reference)]
        name = str(node.get("name") or "")
        if name and name != target.replace("/", "."):
            arguments.append(f"name={json.dumps(name, ensure_ascii=False)}")
        meta = node.get("meta_data") or {}
        if meta.get("inventory_requirements"):
            arguments.append(f"inventory={_python_literal(meta['inventory_requirements'], self.reference)}")
        if meta.get("inventory_mode") not in (None, "task"):
            arguments.append(f"inventory_mode={json.dumps(meta['inventory_mode'])}")
        self.anchor(indent, node)
        self.emit(indent, f"{variable} = ctx.run({', '.join(arguments)})")
        self.variables[str(node["uuid"])] = variable

    def render_loop(self, node: Mapping[str, Any], indent: int) -> None:
        spec = dict(node.get("param") or {})
        name = json.dumps(str(node.get("name") or ""), ensure_ascii=False)
        interval = spec.get("interval_seconds") or 0.0
        if spec.get("mode") == "for":
            header = f"with ctx.loop_for({int(spec.get('count') or 1)}, name={name}, interval_seconds={float(interval)!r}):"
        else:
            condition = dict(spec.get("condition") or {})
            if condition.get("source") == "device_state":
                expression = (f"ctx.device_state({json.dumps(condition.get('device_id'))}, {json.dumps(condition.get('field'))}, "
                              f"{json.dumps(condition.get('op'))}, {_python_literal(condition.get('value'), lambda _v: None)})")
            elif condition.get("source") == "node_output" and str(condition.get("node_uuid")) in self.variables:
                expression = (f"ctx.step_output({self.variables[str(condition['node_uuid'])]}, {json.dumps(condition.get('data_key') or '')}, "
                              f"{json.dumps(condition.get('op'))}, {_python_literal(condition.get('value'), lambda _v: None)})")
            else:
                self.fail("循环条件暂不支持生成源码")
            header = (f"with ctx.loop_while({expression}, max_iterations={int(spec.get('max_iterations') or 1000)}, "
                      f"interval_seconds={float(interval)!r}, name={name}):")
        self.anchor(indent, node)
        self.emit(indent, header)
        before = len(self.lines)
        self.render_block(str(node["uuid"]), indent + 1)
        if len(self.lines) == before:
            self.emit(indent + 1, "pass")

    def render_group(self, node: Mapping[str, Any], indent: int) -> None:
        mode = ((node.get("meta_data") or {}).get("unilab") or {}).get("group_mode", "sequence")
        self.anchor(indent, node)
        self.emit(indent, f"with ctx.{'parallel' if mode == 'parallel' else 'group'}({json.dumps(str(node.get('name') or '分组'), ensure_ascii=False)}):")
        before = len(self.lines)
        self.render_block(str(node["uuid"]), indent + 1)
        if len(self.lines) == before:
            self.fail("空分组无法用源码表达")

    def parameters(self) -> str:
        unilab = ((self.graph.get("workflow") or {}).get("meta_data") or {}).get("unilab") or {}
        if [item for item in (unilab.get("output_contract") or {}).get("outputs", []) if not item.get("implicit")]:
            self.fail("具名工作流结果暂不支持生成源码")
        rendered = []
        for parameter in (unilab.get("input_contract") or {}).get("parameters", []):
            name = str(parameter.get("name") or "")
            schema = parameter.get("schema") or {}
            annotation = _JSON_TO_PYTHON.get(str(schema.get("type") or ""))
            if annotation is None or set(schema) - {"type"}:
                self.fail(f"工作流输入 {name!r} 的类型暂不支持生成源码")
            if not name.isidentifier() or keyword.iskeyword(name):
                self.fail(f"工作流输入名 {name!r} 不是合法 Python 标识符")
            default = f" = {_python_literal(parameter['default'], lambda _v: None)}" if "default" in parameter else ""
            rendered.append(f"{name}: {annotation}{default}")
        return ", *, " + ", ".join(rendered) if rendered else ""

    def render(self) -> str:
        workflow = self.graph.get("workflow") or {}
        if (workflow.get("meta_data") or {}).get("unilab", {}).get("resource_scopes"):
            self.fail("资源作用域暂不支持生成源码")
        if self.graph.get("edges"):
            self.fail("带 Handle 连线的图暂不支持生成源码；顺序请使用 execution_policy.depends_on")
        signature = self.parameters()
        self.render_block(None, 1)
        if not self.lines:
            self.fail("空工作流无法生成源码")
        display_name = json.dumps(str(workflow.get("name") or "工作流"), ensure_ascii=False)
        header = [
            "from unilabos.registry.workflows import workflow",
            "",
            "",
            f"@workflow({display_name})",
            f"def flow(ctx{signature}):",
        ]
        return "\n".join([*header, *self.lines]) + "\n"


__all__ = ["COMPILER_VERSION", "WorkflowAuthoringCompiler", "candidate_changeset"]
