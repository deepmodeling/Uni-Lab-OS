"""物料装卸（MaterialIO）的静态创作、图投影与源码生成合同。"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unilabos.app.scheduler.inventory.material_io import (
    MaterialIOError,
    canonicalize_load_reagent,
    canonicalize_refill_reagent,
)
from unilabos.registry.template_identity import action_handle_uuid
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.material_selector import validate_canonical_uuid
from unilabos.workflow.resource_reference import (
    ResourceReferenceResolutionError,
    ResourceReferenceResolver,
    resolve_resource_reference,
)
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    validate_python_source_identity,
)

_AUTHORING_MODULE = "unilabos.workflow.authoring"
_MATERIAL_IO = f"{_AUTHORING_MODULE}:material_io"
_RESOURCE_REF = f"{_AUTHORING_MODULE}:resource_ref"
_FIELDS = frozenset(
    {
        "mode",
        "resource_template",
        "mount",
        "site",
        "material",
        "disposition",
        "scope",
        "timeout_seconds",
    }
)
_OPTIONAL_FIELDS = frozenset({"reagent", "name", "barcode", "site_uuid"})
_PARAM_FIELDS = frozenset(
    {
        "mode",
        "resource_template_uuid",
        "mount",
        "site",
        "material",
        "disposition",
        "scope",
        "timeout_seconds",
    }
)


class MaterialIOAuthoringError(ValueError):
    """物料装卸声明不能安全编译或生成。"""

    def __init__(self, code: str, message: str, node: ast.AST | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


@dataclass(frozen=True, slots=True)
class MaterialIOBinding:
    """物料输入的静态来源。"""

    kind: str
    value: Any
    result_name: str | None = None


@dataclass(frozen=True, slots=True)
class MaterialIODeclaration:
    """一个 material_io 节点的完整静态声明。"""

    node_uuid: str
    result_name: str
    title: str | None
    description: str | None
    mode: str
    resource_template_symbol: str | None
    mount_resource_id: str
    site: str | None
    site_uuid: str | None
    site_uuid_declared: bool
    site_parameter: str | None
    material_binding: MaterialIOBinding
    disposition: str | None
    scope: str | None
    timeout_seconds: int
    reagent: dict[str, Any] | None
    load_name: str | None
    load_barcode: str | None
    source_node: ast.Assign

    @property
    def arguments(self) -> tuple[tuple[str, MaterialIOBinding], ...]:
        """返回需要投影为数据边的物料参数。"""

        if self.material_binding.kind in {"node_output", "workflow_input"}:
            return (("material", self.material_binding),)
        return ()


@dataclass(frozen=True, slots=True)
class RenderedMaterialIO:
    """确定性 material_io 调用所需的冻结事实。"""

    resource_import: tuple[str, str] | None
    mode: str
    resource_symbol: str | None
    mount_resource_id: str
    site_expression: str
    site_uuid_expression: str | None
    material_expression: str | None
    disposition: str | None
    scope: str | None
    timeout_seconds: int
    reagent: dict[str, Any] | None
    load_name: str | None
    load_barcode: str | None
    requires_material_edge: bool

    def call(self, material_expression: str | None = None) -> str:
        """渲染最终调用；上游物料必须由候选边提供。"""

        expression = material_expression or self.material_expression
        if self.requires_material_edge and not expression:
            raise MaterialIOAuthoringError(
                "invalid_material_io", "unload/refill 的上游物料边缺失"
            )
        arguments = [
            f"mode={self.mode!r}",
            f"resource_template={self.resource_symbol or 'None'}",
            f"mount=resource_ref({json.dumps(self.mount_resource_id, ensure_ascii=False)})",
            f"site={self.site_expression}",
            *(
                [f"site_uuid={self.site_uuid_expression}"]
                if self.site_uuid_expression is not None
                else []
            ),
            f"material={expression or 'None'}",
            f"disposition={self.disposition!r}",
            f"scope={self.scope!r}",
            f"timeout_seconds={self.timeout_seconds!r}",
        ]
        if self.reagent is not None:
            arguments.append(f"reagent={self.reagent!r}")
        if self.load_name is not None:
            arguments.append(f"name={self.load_name!r}")
        if self.load_barcode is not None:
            arguments.append(f"barcode={self.load_barcode!r}")
        return f"material_io({', '.join(arguments)})"


def parse_material_io_declaration(
    statement: ast.stmt,
    *,
    imports: Mapping[str, str],
    anchors: Mapping[int, str],
    node_metadata: Mapping[int, tuple[str, str]],
    input_names: set[str],
    known_results: set[str],
    material_results: set[str],
) -> MaterialIODeclaration | None:
    """静态解析一个 material_io 赋值声明。"""

    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    call = statement.value
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or imports.get(call.func.id) != _MATERIAL_IO
    ):
        return None
    target = statement.targets[0]
    if not isinstance(target, ast.Name):
        _fail("material_io 必须赋值给一个新名称", statement)
    if call.args:
        _fail("material_io 只接受命名参数", call)
    keywords: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in keywords:
            _fail("material_io 参数重复或包含 ** 展开", call)
        keywords[keyword.arg] = keyword.value
    declared = set(keywords)
    if not _FIELDS <= declared or declared - _FIELDS - _OPTIONAL_FIELDS:
        _fail(
            "material_io 必须完整声明八个合同字段；reagent 在 load 可选、refill 必填、unload 禁止；name 与 barcode 仅 load 可选",
            call,
        )

    mode = _literal_string(keywords["mode"], "mode")
    if mode not in {"load", "unload", "refill"}:
        _fail("material_io mode 必须是 load、unload 或 refill", keywords["mode"])

    resource_expression = keywords["resource_template"]
    resource_symbol: str | None = None
    if not _is_none(resource_expression):
        if not isinstance(resource_expression, ast.Name):
            _fail("resource_template 必须是显式导入的静态资源符号或 None", resource_expression)
        resource_symbol = imports.get(resource_expression.id)
        if not isinstance(resource_symbol, str) or ":" not in resource_symbol:
            _fail("resource_template 必须是显式导入的静态资源符号或 None", resource_expression)

    mount_resource_id = _resource_ref_id(keywords["mount"], imports=imports, label="mount")
    site_expression = keywords["site"]
    site_parameter: str | None = None
    site: str | None = None
    site_uuid: str | None = None
    site_uuid_declared = "site_uuid" in keywords
    if site_uuid_declared and not _is_none(keywords["site_uuid"]):
        site_uuid = _uuid_literal(keywords["site_uuid"], "site_uuid")
    # site 在 load、unload、refill 都是库位名称；身份用可选的 site_uuid。
    if _is_none(site_expression):
        if mode == "load":
            _fail("load 的 site 必须是库位名称", site_expression)
        site = None
    elif isinstance(site_expression, ast.Name) and site_expression.id in input_names:
        site_parameter = site_expression.id
    else:
        site = _literal_string(site_expression, "site")
        if not site.strip():
            _fail("site 库位名称不能为空", site_expression)

    material_binding = _material_binding(
        keywords["material"],
        imports=imports,
        input_names=input_names,
        known_results=known_results,
        material_results=material_results,
    )
    disposition = _optional_enum(
        keywords["disposition"], "disposition", {"detach", "soft_delete"}
    )
    scope = _optional_enum(
        keywords["scope"],
        "scope",
        {"material_only", "material_with_contents"},
    )
    timeout_seconds = _timeout(keywords["timeout_seconds"])
    reagent: dict[str, Any] | None = None
    if "reagent" in keywords:
        if mode == "unload":
            _fail("unload 不能声明 reagent", keywords["reagent"])
        reagent_expression = keywords["reagent"]
        if not _is_none(reagent_expression):
            try:
                reagent_value = ast.literal_eval(reagent_expression)
            except (TypeError, ValueError):
                _fail("reagent 必须是对象字面量或 None", reagent_expression)
            try:
                reagent = (
                    canonicalize_refill_reagent(reagent_value)
                    if mode == "refill"
                    else canonicalize_load_reagent(reagent_value)
                )
            except MaterialIOError as error:
                _fail(error.message, reagent_expression)

    if mode == "load":
        if resource_symbol is None:
            _fail("load 必须声明 resource_template", resource_expression)
        if site is None and site_parameter is None:
            _fail("load 必须声明 site", keywords["site"])
        if material_binding.kind != "none" or disposition is not None or scope is not None:
            _fail("load 的 material、disposition、scope 必须为 None", call)
    elif mode == "unload":
        if resource_symbol is not None:
            _fail("unload 的 resource_template 必须为 None", resource_expression)
        if material_binding.kind == "none":
            _fail("unload 必须声明 material", keywords["material"])
        if disposition is None or scope is None:
            _fail("unload 必须声明 disposition 和 scope", call)
    else:
        if resource_symbol is not None:
            _fail("refill 的 resource_template 必须为 None", resource_expression)
        if material_binding.kind == "none":
            _fail("refill 必须声明 material", keywords["material"])
        if disposition is not None or scope is not None:
            _fail("refill 的 disposition、scope 必须为 None", call)
        if reagent is None:
            _fail("refill 必须声明 reagent", call)

    load_name: str | None = None
    if "name" in keywords:
        if mode != "load":
            _fail("只有 load 可以声明 name", keywords["name"])
        load_name = _literal_string(keywords["name"], "name")
    load_barcode: str | None = None
    if "barcode" in keywords:
        if mode != "load":
            _fail("只有 load 可以声明 barcode", keywords["barcode"])
        load_barcode = _literal_string(keywords["barcode"], "barcode")
        if load_barcode != load_barcode.strip():
            _fail("barcode 不能包含首尾空白", keywords["barcode"])

    node_uuid = anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail("每个 material_io 前必须有相邻节点 UUID 锚点", statement)
    metadata = node_metadata.get(statement.lineno - 1)
    return MaterialIODeclaration(
        node_uuid=node_uuid,
        result_name=target.id,
        title=metadata[0] if metadata else None,
        description=metadata[1] if metadata else None,
        mode=mode,
        resource_template_symbol=resource_symbol,
        mount_resource_id=mount_resource_id,
        site=site,
        site_uuid=site_uuid,
        site_uuid_declared=site_uuid_declared,
        site_parameter=site_parameter,
        material_binding=material_binding,
        disposition=disposition,
        scope=scope,
        timeout_seconds=timeout_seconds,
        reagent=reagent,
        load_name=load_name,
        load_barcode=load_barcode,
        source_node=statement,
    )


def build_material_io_node(
    declaration: MaterialIODeclaration,
    *,
    catalog: AuthoringCatalogSnapshot,
    resource_reference_resolver: ResourceReferenceResolver | None = None,
) -> tuple[dict[str, Any], AuthoringCatalogAction]:
    """把 material_io 声明投影为冻结候选节点。"""

    try:
        framework = catalog.require_material_io()
        resource_template_uuid = (
            catalog.require_resource_template_uuid(declaration.resource_template_symbol)
            if declaration.resource_template_symbol is not None
            else None
        )
    except AuthoringCatalogError as error:
        raise MaterialIOAuthoringError(
            "template_catalog_mismatch", "material_io 引用的框架或资源模板不在当前目录"
        ) from error
    try:
        mount = resolve_resource_reference(
            declaration.mount_resource_id, resource_reference_resolver
        )
        material = None
        if declaration.material_binding.kind == "resource_ref":
            material = resolve_resource_reference(
                str(declaration.material_binding.value),
                resource_reference_resolver,
            )
    except ResourceReferenceResolutionError as error:
        raise MaterialIOAuthoringError(
            "resource_reference_resolution_error", str(error), declaration.source_node
        ) from error

    param = {
        "mode": declaration.mode,
        "resource_template_uuid": resource_template_uuid,
        "mount": {"uuid": mount["uuid"]},
        "site": declaration.site,
        "material": ({"uuid": material["uuid"]} if material is not None else None),
        "disposition": declaration.disposition,
        "scope": declaration.scope,
        "timeout_seconds": declaration.timeout_seconds,
    }
    if declaration.site_uuid_declared:
        param["site_uuid"] = declaration.site_uuid
    if declaration.reagent is not None:
        param["reagent"] = dict(declaration.reagent)
    if declaration.load_name is not None:
        param["name"] = declaration.load_name
    if declaration.load_barcode is not None:
        param["barcode"] = declaration.load_barcode
    template = framework.template
    unilab: dict[str, Any] = {
        "input_bindings": {},
        "authoring_result_name": declaration.result_name,
        "resource_refs": {"mount": {"resource_id": declaration.mount_resource_id}},
    }
    def bind_input(key: str, parameter: str) -> None:
        handles = [h for h in framework.handles if h.get("io_type") == "target" and h.get("handle_key") == key]
        if not handles and key == "site":
            # 旧目录仅有 material/ready 连接点，保留既有独立装卸入口。
            unilab["material_io_site_binding"] = {"parameter": parameter}
            return
        if len(handles) != 1:
            raise MaterialIOAuthoringError("template_catalog_mismatch", "物料装卸输入连接点缺失或重复")
        handle = str(handles[0]["uuid"])
        unilab.setdefault("material_io_input_handles", {})[key] = handle
        unilab["input_bindings"][handle] = {"parameter": parameter}

    if declaration.site_parameter is not None:
        bind_input("site", declaration.site_parameter)
    if declaration.material_binding.kind == "workflow_input":
        bind_input("material", str(declaration.material_binding.value))
    if declaration.material_binding.kind == "resource_ref":
        unilab["resource_refs"]["material"] = {
            "resource_id": str(declaration.material_binding.value)
        }
    elif declaration.material_binding.kind == "node_output":
        unilab["material_io_material_binding"] = {
            "result_name": declaration.material_binding.result_name
        }
    return {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": None,
        "name": declaration.title or template.get("display_name") or "Material IO",
        "type": "material_io",
        "icon": template.get("icon"),
        "pose": {},
        "param": param,
        "footer": template.get("footer"),
        "action_name": "material_io",
        "action_type": None,
        "execution_policy": {},
        "manual_confirmation": {"timeout_seconds": declaration.timeout_seconds},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": declaration.description
        if declaration.description is not None
        else template.get("description"),
        "meta_data": {"unilab": unilab},
    }, framework


def render_material_io_call(
    node: Mapping[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> RenderedMaterialIO:
    """校验候选节点并准备确定性 material_io 源码。"""

    param = validate_material_io_node(node)
    mode = param.get("mode")
    if mode not in {"load", "unload", "refill"}:
        raise MaterialIOAuthoringError("invalid_material_io", "material_io mode 无效")
    mount = param.get("mount")
    if not isinstance(mount, Mapping) or not isinstance(mount.get("uuid"), str):
        raise MaterialIOAuthoringError("invalid_material_io", "material_io mount 无效")
    _uuid_value(str(mount["uuid"]), "mount")
    site = param.get("site")
    if "site_uuid" in param and param.get("site_uuid") is not None:
        _uuid_value(param.get("site_uuid"), "site_uuid")
    if site is not None and (not isinstance(site, str) or not site.strip()):
        raise MaterialIOAuthoringError("invalid_material_io", "site 库位名称无效")
    timeout_seconds = param.get("timeout_seconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 86400
    ):
        raise MaterialIOAuthoringError("invalid_material_io", "timeout_seconds 必须在 1..86400")

    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    refs = unilab.get("resource_refs") if isinstance(unilab, Mapping) else None
    mount_ref = refs.get("mount") if isinstance(refs, Mapping) else None
    mount_resource_id = (
        mount_ref.get("resource_id") if isinstance(mount_ref, Mapping) else None
    )
    if not isinstance(mount_resource_id, str) or not mount_resource_id.strip():
        raise MaterialIOAuthoringError("invalid_material_io", "material_io mount 源码身份缺失")

    site_expression = repr(site)
    site_binding = _input_binding(node, "site")
    if site_binding is not None:
        parameter = (
            site_binding.get("parameter") if isinstance(site_binding, Mapping) else None
        )
        if not isinstance(parameter, str) or not parameter.isidentifier():
            raise MaterialIOAuthoringError("invalid_material_io", "material_io site 输入绑定无效")
        site_expression = parameter

    resource_import: tuple[str, str] | None = None
    resource_symbol: str | None = None
    material_expression: str | None = None
    requires_material_edge = False
    if mode == "load":
        if (
            param.get("material") is not None
            or param.get("disposition") is not None
            or param.get("scope") is not None
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "load 冻结合同无效")
        try:
            source_identity = catalog.require_resource_template_symbol(
                str(param.get("resource_template_uuid") or "")
            )
            module, resource_symbol = validate_python_source_identity(source_identity)
        except (AuthoringCatalogError, PythonSourceIdentityError) as error:
            raise MaterialIOAuthoringError(
                "template_catalog_mismatch", "material_io 资源模板身份不能反解"
            ) from error
        resource_import = (module, resource_symbol)
    elif mode == "unload":
        if (
            param.get("resource_template_uuid") is not None
            or param.get("disposition") not in {"detach", "soft_delete"}
            or param.get("scope") not in {
                "material_only",
                "material_with_contents",
            }
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "unload 冻结合同无效")
        material_ref = refs.get("material") if isinstance(refs, Mapping) else None
        if material_ref is not None:
            resource_id = (
                material_ref.get("resource_id")
                if isinstance(material_ref, Mapping)
                else None
            )
            material = param.get("material")
            if (
                not isinstance(resource_id, str)
                or not resource_id.strip()
                or not isinstance(material, Mapping)
                or not isinstance(material.get("uuid"), str)
            ):
                raise MaterialIOAuthoringError("invalid_material_io", "unload material 引用无效")
            material_expression = (
                f"resource_ref({json.dumps(resource_id, ensure_ascii=False)})"
            )
        else:
            requires_material_edge = True
    else:
        if (
            param.get("resource_template_uuid") is not None
            or param.get("disposition") is not None
            or param.get("scope") is not None
            or not isinstance(param.get("reagent"), dict)
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "refill 冻结合同无效")
        material_ref = refs.get("material") if isinstance(refs, Mapping) else None
        if material_ref is not None:
            resource_id = (
                material_ref.get("resource_id")
                if isinstance(material_ref, Mapping)
                else None
            )
            material = param.get("material")
            if (
                not isinstance(resource_id, str)
                or not resource_id.strip()
                or not isinstance(material, Mapping)
                or not isinstance(material.get("uuid"), str)
            ):
                raise MaterialIOAuthoringError("invalid_material_io", "refill material 引用无效")
            material_expression = (
                f"resource_ref({json.dumps(resource_id, ensure_ascii=False)})"
            )
        else:
            requires_material_edge = True
    input_material = _input_binding(node, "material")
    if input_material is not None and mode in {"unload", "refill"}:
        material_expression = str(input_material["parameter"])
        requires_material_edge = False
    return RenderedMaterialIO(
        resource_import=resource_import,
        mode=str(mode),
        resource_symbol=resource_symbol,
        mount_resource_id=mount_resource_id,
        site_expression=site_expression,
        site_uuid_expression=(
            repr(param.get("site_uuid")) if "site_uuid" in param else None
        ),
        material_expression=material_expression,
        disposition=param.get("disposition"),
        scope=param.get("scope"),
        timeout_seconds=timeout_seconds,
        reagent=param.get("reagent") if isinstance(param.get("reagent"), dict) else None,
        load_name=(
            str(param.get("name")).strip()
            if isinstance(param.get("name"), str) and str(param.get("name")).strip()
            else None
        ),
        load_barcode=(
            str(param.get("barcode"))
            if isinstance(param.get("barcode"), str) and str(param.get("barcode")).strip()
            else None
        ),
        requires_material_edge=requires_material_edge,
    )


def validate_material_io_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """校验已冻结 material_io 节点，不读取目录、库存或数据库。"""

    param = node.get("param")
    if not isinstance(param, Mapping) or not _PARAM_FIELDS <= set(param):
        raise MaterialIOAuthoringError(
            "invalid_material_io", "material_io 冻结参数字段无效"
        )
    extra = set(param) - _PARAM_FIELDS
    if extra - _OPTIONAL_FIELDS:
        raise MaterialIOAuthoringError(
            "invalid_material_io", "material_io 冻结参数字段无效"
        )
    mode = param.get("mode")
    if mode not in {"load", "unload", "refill"}:
        raise MaterialIOAuthoringError("invalid_material_io", "material_io mode 无效")
    if mode != "load" and ("barcode" in param or "name" in param):
        raise MaterialIOAuthoringError(
            "invalid_material_io", "只有 load 可以冻结 name 或 barcode"
        )
    if "reagent" in param:
        if mode == "unload":
            raise MaterialIOAuthoringError(
                "invalid_material_io", "unload 不能冻结 reagent"
            )
        try:
            reagent = (
                canonicalize_refill_reagent(param.get("reagent"))
                if mode == "refill"
                else canonicalize_load_reagent(param.get("reagent"))
            )
        except MaterialIOError as error:
            raise MaterialIOAuthoringError("invalid_material_io", error.message) from error
        if reagent is None:
            raise MaterialIOAuthoringError(
                "invalid_material_io",
                "refill 必须冻结 reagent" if mode == "refill" else "load reagent 必须省略或为对象",
            )
        param = dict(param)
        param["reagent"] = reagent
    elif mode == "refill":
        raise MaterialIOAuthoringError("invalid_material_io", "refill 必须冻结 reagent")
    mount = param.get("mount")
    if not isinstance(mount, Mapping) or set(mount) != {"uuid"}:
        raise MaterialIOAuthoringError("invalid_material_io", "material_io mount 无效")
    _uuid_value(mount.get("uuid"), "mount")
    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    site = param.get("site")
    if "site_uuid" in param and param.get("site_uuid") is not None:
        _uuid_value(param.get("site_uuid"), "site_uuid")
    site_binding = _input_binding(node, "site")
    if site is not None:
        if not isinstance(site, str) or not site.strip():
            raise MaterialIOAuthoringError("invalid_material_io", "site 库位名称无效")
        if site_binding is not None:
            raise MaterialIOAuthoringError(
                "invalid_material_io", "material_io site 不能同时固定并绑定输入"
            )
    elif site is None:
        parameter = (
            site_binding.get("parameter") if isinstance(site_binding, Mapping) else None
        )
        if isinstance(parameter, str) and parameter.isidentifier():
            pass
        elif mode == "refill" and site_binding is None:
            pass
        else:
            raise MaterialIOAuthoringError(
                "invalid_material_io", "material_io 必须冻结 site 名称或输入绑定"
            )
    timeout = param.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise MaterialIOAuthoringError(
            "invalid_material_io", "timeout_seconds 必须在 1..86400"
        )
    material = param.get("material")
    material_binding = (
        unilab.get("material_io_material_binding")
        if isinstance(unilab, Mapping)
        else None
    )
    workflow_material = _input_binding(node, "material")
    if mode == "load":
        if (
            not isinstance(param.get("resource_template_uuid"), str)
            or material is not None
            or material_binding is not None
            or param.get("disposition") is not None
            or param.get("scope") is not None
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "load 冻结合同无效")
        _uuid_value(param["resource_template_uuid"], "resource_template_uuid")
        if "barcode" in param and param.get("barcode") is not None:
            barcode = param.get("barcode")
            if (
                not isinstance(barcode, str)
                or not barcode
                or barcode != barcode.strip()
            ):
                raise MaterialIOAuthoringError(
                    "invalid_material_io", "load barcode 必须是无首尾空白的非空字符串"
                )
    elif mode == "unload":
        if (
            param.get("resource_template_uuid") is not None
            or param.get("disposition") not in {"detach", "soft_delete"}
            or param.get("scope")
            not in {"material_only", "material_with_contents"}
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "unload 冻结合同无效")
        if material is not None:
            if not isinstance(material, Mapping) or set(material) != {"uuid"}:
                raise MaterialIOAuthoringError(
                    "invalid_material_io", "unload material 引用无效"
                )
            _uuid_value(material.get("uuid"), "material")
            if material_binding is not None:
                raise MaterialIOAuthoringError(
                    "invalid_material_io", "unload material 来源重复"
                )
        elif workflow_material is None and (
            not isinstance(material_binding, Mapping)
            or not isinstance(material_binding.get("result_name"), str)
            or not material_binding["result_name"]
        ):
            raise MaterialIOAuthoringError(
                "invalid_material_io", "unload 必须冻结 material 引用或上游绑定"
            )
    else:
        if (
            param.get("resource_template_uuid") is not None
            or param.get("disposition") is not None
            or param.get("scope") is not None
        ):
            raise MaterialIOAuthoringError("invalid_material_io", "refill 冻结合同无效")
        if material is not None:
            if not isinstance(material, Mapping) or set(material) != {"uuid"}:
                raise MaterialIOAuthoringError(
                    "invalid_material_io", "refill material 引用无效"
                )
            _uuid_value(material.get("uuid"), "material")
            if material_binding is not None:
                raise MaterialIOAuthoringError(
                    "invalid_material_io", "refill material 来源重复"
                )
        elif workflow_material is None and (
            not isinstance(material_binding, Mapping)
            or not isinstance(material_binding.get("result_name"), str)
            or not material_binding["result_name"]
        ):
            raise MaterialIOAuthoringError(
                "invalid_material_io", "refill 必须冻结 material 引用或上游绑定"
            )
    return dict(param)


def _material_binding(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
    input_names: set[str],
    known_results: set[str],
    material_results: set[str],
) -> MaterialIOBinding:
    if _is_none(expression):
        return MaterialIOBinding("none", None)
    if isinstance(expression, ast.Name) and expression.id in input_names:
        return MaterialIOBinding("workflow_input", expression.id)
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and imports.get(expression.func.id) == _RESOURCE_REF
    ):
        return MaterialIOBinding(
            "resource_ref",
            _resource_ref_id(expression, imports=imports, label="material"),
        )
    if isinstance(expression, ast.Name) and expression.id in known_results:
        if expression.id not in material_results:
            _fail("material 必须引用上游物料结果", expression)
        return MaterialIOBinding("node_output", "material", expression.id)
    _fail("material 必须是上游物料结果、resource_ref(...) 或 None", expression)


def _input_binding(node: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """读取普通输入连接点；兼容旧的库位选择器元数据。"""
    unilab = (node.get("meta_data") or {}).get("unilab") or {}
    handle = unilab.get("material_io_input_handles", {}).get(key)
    if handle is None and node.get("workflow_node_template_uuid"):
        handle = action_handle_uuid(str(node["workflow_node_template_uuid"]), io_type="target", handle_key=key)
    binding = unilab.get("input_bindings", {}).get(handle)
    if binding is None and key == "site":
        binding = unilab.get("material_io_site_binding")
    if binding is not None and (not isinstance(binding, Mapping) or set(binding) != {"parameter"} or not isinstance(binding["parameter"], str) or not binding["parameter"].isidentifier()):
        raise MaterialIOAuthoringError("invalid_material_io", "物料装卸输入绑定无效")
    return binding


def _resource_ref_id(
    expression: ast.expr, *, imports: Mapping[str, str], label: str
) -> str:
    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or imports.get(expression.func.id) != _RESOURCE_REF
        or len(expression.args) != 1
        or expression.keywords
    ):
        _fail(f"{label} 必须调用 resource_ref(资源 ID)", expression)
    try:
        value = ast.literal_eval(expression.args[0])
    except (TypeError, ValueError):
        _fail(f"{label} 资源 ID 必须是字符串字面量", expression)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(f"{label} 资源 ID 必须是无首尾空白的非空字符串", expression)
    return value


def _literal_string(expression: ast.expr, label: str) -> str:
    try:
        value = ast.literal_eval(expression)
    except (TypeError, ValueError):
        _fail(f"{label} 必须是字符串字面量", expression)
    if not isinstance(value, str) or not value:
        _fail(f"{label} 必须是非空字符串", expression)
    return value


def _optional_enum(expression: ast.expr, label: str, allowed: set[str]) -> str | None:
    if _is_none(expression):
        return None
    value = _literal_string(expression, label)
    if value not in allowed:
        _fail(f"{label} 不在允许值集合中", expression)
    return value


def _timeout(expression: ast.expr) -> int:
    try:
        value = ast.literal_eval(expression)
    except (TypeError, ValueError):
        _fail("timeout_seconds 必须是整数字面量", expression)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
        _fail("timeout_seconds 必须在 1..86400", expression)
    return value


def _uuid_literal(expression: ast.expr, label: str) -> str:
    try:
        return _uuid_value(ast.literal_eval(expression), label)
    except (TypeError, ValueError):
        _fail(f"{label} 必须是规范 UUID 字面量或工作流输入", expression)


def _uuid_value(value: Any, label: str) -> str:
    try:
        return validate_canonical_uuid(value)
    except (TypeError, ValueError):
        raise MaterialIOAuthoringError(
            "invalid_material_io", f"{label} 必须是规范 UUID"
        ) from None


def _is_none(expression: ast.expr) -> bool:
    return isinstance(expression, ast.Constant) and expression.value is None


def _fail(message: str, node: ast.AST | None = None) -> None:
    raise MaterialIOAuthoringError("invalid_material_io", message, node)


__all__ = [
    "MaterialIOAuthoringError",
    "MaterialIOBinding",
    "MaterialIODeclaration",
    "RenderedMaterialIO",
    "build_material_io_node",
    "parse_material_io_declaration",
    "render_material_io_call",
    "validate_material_io_node",
]
