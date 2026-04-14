import ast
import json
from typing import Dict, List, Any, Tuple, Optional

from .common import WorkflowGraph, RegistryAdapter

Json = Dict[str, Any]

# ---------------- Converter ----------------

class DeviceMethodConverter:
    """
    - 字段统一：resource_name（原 device_class）、template_name（原 action_key）
    - params 单层；inputs 使用 'params.' 前缀
    - SimpleGraph.add_workflow_node 负责变量连线与边
    """
    def __init__(self, device_registry: Optional[Dict[str, Any]] = None):
        self.graph = WorkflowGraph()
        self.variable_sources: Dict[str, Dict[str, Any]] = {}        # var -> {node_id, output_name}
        self.instance_to_resource: Dict[str, Optional[str]] = {}     # 实例名 -> resource_name
        self.node_id_counter: int = 0
        self.registry = RegistryAdapter(device_registry or {})

    # ---- helpers ----
    def _new_node_id(self) -> int:
        nid = self.node_id_counter
        self.node_id_counter += 1
        return nid

    def _assign_targets(self, targets) -> List[str]:
        names: List[str] = []
        import ast
        if isinstance(targets, ast.Tuple):
            for elt in targets.elts:
                if isinstance(elt, ast.Name):
                    names.append(elt.id)
        elif isinstance(targets, ast.Name):
            names.append(targets.id)
        return names

    def _extract_device_instantiation(self, node) -> Optional[Tuple[str, str]]:
        import ast
        if not isinstance(node.value, ast.Call):
            return None
        callee = node.value.func
        if isinstance(callee, ast.Name):
            class_name = callee.id
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            class_name = callee.attr
        else:
            return None
        if isinstance(node.targets[0], ast.Name):
            instance = node.targets[0].id
            return instance, class_name
        return None

    def _extract_call(self, call) -> Tuple[str, str, Dict[str, Any], str]:
        import ast
        owner_name, method_name, call_kind = "", "", "func"
        if isinstance(call.func, ast.Attribute):
            method_name = call.func.attr
            if isinstance(call.func.value, ast.Name):
                owner_name = call.func.value.id
                call_kind = "instance" if owner_name in self.instance_to_resource else "class_or_module"
            elif isinstance(call.func.value, ast.Attribute) and isinstance(call.func.value.value, ast.Name):
                owner_name = call.func.value.attr
                call_kind = "class_or_module"
        elif isinstance(call.func, ast.Name):
            method_name = call.func.id
            call_kind = "func"

        def pack(node):
            if isinstance(node, ast.Name):
                return {"type": "variable", "value": node.id}
            if isinstance(node, ast.Constant):
                return {"type": "constant", "value": node.value}
            if isinstance(node, ast.Dict):
                return {"type": "dict", "value": self._parse_dict(node)}
            if isinstance(node, ast.List):
                return {"type": "list", "value": self._parse_list(node)}
            return {"type": "raw", "value": ast.unparse(node) if hasattr(ast, "unparse") else str(node)}

        args: Dict[str, Any] = {}
        pos: List[Any] = []
        for a in call.args:
            pos.append(pack(a))
        for kw in call.keywords:
            args[kw.arg] = pack(kw.value)
        if pos:
            args["_positional"] = pos
        return owner_name, method_name, args, call_kind

    def _parse_dict(self, node) -> Dict[str, Any]:
        import ast
        out: Dict[str, Any] = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                key = str(k.value)
                if isinstance(v, ast.Name):
                    out[key] = f"var:{v.id}"
                elif isinstance(v, ast.Constant):
                    out[key] = v.value
                elif isinstance(v, ast.Dict):
                    out[key] = self._parse_dict(v)
                elif isinstance(v, ast.List):
                    out[key] = self._parse_list(v)
        return out

    def _parse_list(self, node) -> List[Any]:
        import ast
        out: List[Any] = []
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                out.append(f"var:{elt.id}")
            elif isinstance(elt, ast.Constant):
                out.append(elt.value)
            elif isinstance(elt, ast.Dict):
                out.append(self._parse_dict(elt))
            elif isinstance(elt, ast.List):
                out.append(self._parse_list(elt))
        return out

    def _normalize_var_tokens(self, x: Any) -> Any:
        if isinstance(x, str) and x.startswith("var:"):
            return {"__var__": x[4:]}
        if isinstance(x, list):
            return [self._normalize_var_tokens(i) for i in x]
        if isinstance(x, dict):
            return {k: self._normalize_var_tokens(v) for k, v in x.items()}
        return x

    def _make_params_payload(self, resource_name: Optional[str], template_name: str, call_args: Dict[str, Any]) -> Dict[str, Any]:
        input_keys = self.registry.get_action_input_keys(resource_name, template_name) if resource_name else []
        defaults = self.registry.get_action_goal_default(resource_name, template_name) if resource_name else {}
        params: Dict[str, Any] = dict(defaults)

        def unpack(p):
            t, v = p.get("type"), p.get("value")
            if t == "variable":
                return {"__var__": v}
            if t == "dict":
                return self._normalize_var_tokens(v)
            if t == "list":
                return self._normalize_var_tokens(v)
            return v

        for k, p in call_args.items():
            if k == "_positional":
                continue
            params[k] = unpack(p)

        pos = call_args.get("_positional", [])
        if pos:
            if input_keys:
                for i, p in enumerate(pos):
                    if i >= len(input_keys):
                        break
                    name = input_keys[i]
                    if name in params:
                        continue
                    params[name] = unpack(p)
            else:
                for i, p in enumerate(pos):
                    params[f"arg_{i}"] = unpack(p)
        return params

    # ---- handlers ----
    def _on_assign(self, stmt):
        import ast
        inst = self._extract_device_instantiation(stmt)
        if inst:
            instance, code_class = inst
            resource_name = self.registry.resolve_resource_by_classname(code_class)
            self.instance_to_resource[instance] = resource_name
            return

        if isinstance(stmt.value, ast.Call):
            owner, method, call_args, kind = self._extract_call(stmt.value)
            if kind == "instance":
                device_key = owner
                resource_name = self.instance_to_resource.get(owner)
            else:
                device_key = owner
                resource_name = self.registry.resolve_resource_by_classname(owner)

            module = self.registry.get_device_module(resource_name)
            params = self._make_params_payload(resource_name, method, call_args)

            nid = self._new_node_id()
            self.graph.add_workflow_node(
                nid,
                device_key=device_key,
                resource_name=resource_name,      # ✅
                module=module,
                template_name=method,             # ✅
                params=params,
                variable_sources=self.variable_sources,
                add_ready_if_no_vars=True,
                prev_node_id=(nid - 1) if nid > 0 else None,
            )

            out_vars = self._assign_targets(stmt.targets[0])
            for var in out_vars:
                self.variable_sources[var] = {"node_id": nid, "output_name": "result"}

    def _on_expr(self, stmt):
        import ast
        if not isinstance(stmt.value, ast.Call):
            return
        owner, method, call_args, kind = self._extract_call(stmt.value)
        if kind == "instance":
            device_key = owner
            resource_name = self.instance_to_resource.get(owner)
        else:
            device_key = owner
            resource_name = self.registry.resolve_resource_by_classname(owner)

        module = self.registry.get_device_module(resource_name)
        params = self._make_params_payload(resource_name, method, call_args)

        nid = self._new_node_id()
        self.graph.add_workflow_node(
            nid,
            device_key=device_key,
            resource_name=resource_name,      # ✅
            module=module,
            template_name=method,             # ✅
            params=params,
            variable_sources=self.variable_sources,
            add_ready_if_no_vars=True,
            prev_node_id=(nid - 1) if nid > 0 else None,
        )

    def convert(self, python_code: str):
        tree = ast.parse(python_code)
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                self._on_assign(stmt)
            elif isinstance(stmt, ast.Expr):
                self._on_expr(stmt)
        return self
