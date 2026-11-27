"""Publish one immutable WorkspaceRelease through existing Backend v1 APIs."""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests

from .authority_sync import (
    _api_base,
    _remap_available_site_template_identities,
    _response_data,
    _template_definition,
)
from .model import WorkspaceHostError, atomic_write_json, utc_timestamp


@dataclass(frozen=True)
class WorkspaceRelease:
    """Detached Local authoring facts addressed by one content hash."""

    release_id: str
    source_workspace: str
    templates: tuple[Mapping[str, Any], ...]
    material_graph: Mapping[str, Any]
    workflows: tuple[Mapping[str, Any], ...]
    workflow_node_templates: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class DeploymentPlan:
    """Validated, non-mutating description of a dedicated Backend deployment."""

    release: WorkspaceRelease
    target_address: str
    template_count: int
    material_count: int
    workflow_count: int


@dataclass(frozen=True)
class DeploymentResult:
    """Local-to-Backend identities produced by Apply."""

    release_id: str
    target_address: str
    template_identities: Mapping[str, str]
    material_identities: Mapping[str, str]
    workflow_identities: Mapping[str, str]
    material_template_names: Mapping[str, str] | None = None


@dataclass(frozen=True)
class PreparedDeploymentTemplates:
    """Source templates plus deterministic instance adapters for Backend Sites."""

    templates: tuple[Mapping[str, Any], ...]
    material_template_names: Mapping[str, str]


@dataclass(frozen=True)
class VerificationReport:
    """Readback comparison result; activation is forbidden when false."""

    verified: bool
    template_count: int
    material_count: int
    workflow_count: int
    diagnostics: tuple[str, ...]


class ReleaseBuilder(Protocol):
    def build(self) -> WorkspaceRelease: ...


class DeploymentTarget(Protocol):
    def plan(self, release: WorkspaceRelease) -> DeploymentPlan: ...
    def apply(self, plan: DeploymentPlan) -> DeploymentResult: ...


class ReleaseVerifier(Protocol):
    def verify(
        self, release: WorkspaceRelease, result: DeploymentResult
    ) -> VerificationReport: ...


class WorkspaceReleasePublisher:
    """Deep orchestration seam: Build -> Plan -> Apply -> Verify -> record."""

    def __init__(
        self,
        builder: ReleaseBuilder,
        target: DeploymentTarget,
        verifier: ReleaseVerifier,
        *,
        deployment_directory: Path,
    ) -> None:
        self.builder = builder
        self.target = target
        self.verifier = verifier
        self.deployment_directory = deployment_directory

    def build(self) -> WorkspaceRelease:
        """Freeze the source before any destructive target operation begins."""

        return self.builder.build()

    def publish(self, release: WorkspaceRelease | None = None) -> dict[str, Any]:
        """Apply and verify a release, reusing a safely prebuilt source if supplied."""

        release = release or self.build()
        plan = self.target.plan(release)
        result = self.target.apply(plan)
        verification = self.verifier.verify(release, result)
        if not verification.verified:
            raise WorkspaceHostError(
                "release_verification_failed",
                "WorkspaceRelease 回读校验失败",
                details={"diagnostics": list(verification.diagnostics)},
            )
        receipt = {
            "schemaVersion": "unilab-workspace-deployment/v1",
            "releaseId": release.release_id,
            "sourceWorkspace": release.source_workspace,
            "targetAddress": result.target_address,
            "verified": True,
            "verifiedAt": utc_timestamp(),
            "counts": {
                "templates": verification.template_count,
                "materials": verification.material_count,
                "workflows": verification.workflow_count,
            },
            "identities": {
                "templates": dict(result.template_identities),
                "materials": dict(result.material_identities),
                "workflows": dict(result.workflow_identities),
                "materialTemplates": dict(result.material_template_names or {}),
            },
        }
        filename = release.release_id.replace(":", "-", 1) + ".json"
        atomic_write_json(self.deployment_directory / filename, receipt)
        return receipt


class LocalBackendReleaseBuilder:
    """Freeze the exact Local Backend generation currently visible in Workbench."""

    def __init__(
        self,
        source_address: str,
        source_workspace: Path,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.source_api = _api_base(source_address)
        self.source_workspace = str(source_workspace.resolve())
        self.session = session or requests.Session()
        self.timeout = timeout

    def build(self) -> WorkspaceRelease:
        templates = list(
            self._get(f"/resource-templates/{item['uuid']}")
            for item in self._paged("/resource-templates")
        )
        _embed_release_material_shapes(
            templates,
            _compile_workspace_material_shapes(self.source_workspace),
        )
        frozen_templates = tuple(templates)
        material_graph = self._get("/materials/graph")
        workflows = tuple(
            {
                "catalog": item,
                "graph": self._get(f"/workflows/{item['uuid']}/graph"),
            }
            for item in self._paged("/workflows")
        )
        workflow_node_templates = tuple(
            self._get(f"/workflow-node-templates/{item['uuid']}")
            for item in self._paged("/workflow-node-templates")
        )
        detached = {
            "sourceWorkspace": self.source_workspace,
            "templates": frozen_templates,
            "materialGraph": material_graph,
            "workflows": workflows,
            "workflowNodeTemplates": workflow_node_templates,
        }
        digest = hashlib.sha256(
            json.dumps(
                detached,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return WorkspaceRelease(
            release_id=f"sha256:{digest}",
            source_workspace=self.source_workspace,
            templates=tuple(deepcopy(list(frozen_templates))),
            material_graph=deepcopy(material_graph),
            workflows=tuple(deepcopy(list(workflows))),
            workflow_node_templates=tuple(
                deepcopy(list(workflow_node_templates))
            ),
        )

    def _paged(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._get(path, params={"page": page, "page_size": 100})
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise WorkspaceHostError(
                    "release_source_invalid", f"Local Backend {path} 列表结构无效"
                )
            items.extend(dict(item) for item in raw_items if isinstance(item, Mapping))
            if not payload.get("has_more"):
                return items
            page += 1

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.get(
            f"{self.source_api}{path}", timeout=self.timeout, **kwargs
        )
        return _release_response(response, f"读取 Local Backend {path}")


def _compile_workspace_material_shapes(
    source_workspace: str,
) -> Mapping[str, Mapping[str, Any]]:
    """Compile exact template-to-Shape bindings from the editable package."""

    from unilabos.package_manager import WorkspaceSource, compile_package_source
    from unilabos.package_manager.workspace_runtime.package_source import (
        PackageCatalogSource,
        compile_material_shape_generation,
    )

    source = WorkspaceSource(Path(source_workspace))
    catalog = compile_package_source(source)
    generation = compile_material_shape_generation(
        (PackageCatalogSource(source=source, catalog=catalog),)
    )
    return generation.shapes_by_template


def _embed_release_material_shapes(
    templates: list[dict[str, Any]],
    shapes_by_template: Mapping[str, Mapping[str, Any]],
) -> None:
    """Embed compiled Shapes into detached Local Backend template DTOs."""

    templates_by_name = {
        str(template.get("name") or ""): template for template in templates
    }
    missing = sorted(set(shapes_by_template) - set(templates_by_name))
    if missing:
        raise WorkspaceHostError(
            "release_source_invalid",
            "Workspace Shape 找不到对应资源模板",
            details={"templates": missing},
        )
    for template_name, raw_shape in shapes_by_template.items():
        shape = deepcopy(dict(raw_shape))
        if (
            shape.get("schema_version") != "unilab.shape/v1"
            or not isinstance(shape.get("parts"), list)
            or not shape["parts"]
        ):
            raise WorkspaceHostError(
                "release_source_invalid",
                f"Workspace Shape 无效：{template_name}",
            )
        template = templates_by_name[template_name]
        model = deepcopy(_mapping_or_empty(template.get("model")))
        binding = deepcopy(_mapping_or_empty(model.get("shape")))
        binding.update(shape)
        model["shape"] = binding
        template["model"] = model

class ExistingBackendDeploymentTarget:
    """First-stage Adapter for a dedicated/clean existing Go Backend."""

    def __init__(
        self,
        target_address: str,
        credential: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        before_workflows: Callable[[], None] | None = None,
    ) -> None:
        self.target_api = _api_base(target_address)
        self.target_address = self.target_api.removesuffix("/api/v1")
        self.credential = str(credential or "").strip()
        if not self.credential:
            raise WorkspaceHostError(
                "release_credentials_missing", "Backend 发布凭据缺失"
            )
        self.session = session or requests.Session()
        self.timeout = timeout
        self.before_workflows = before_workflows

    def plan(self, release: WorkspaceRelease) -> DeploymentPlan:
        material_nodes = _material_nodes(release.material_graph)
        if self._paged("/materials", with_children="true"):
            raise WorkspaceHostError(
                "release_target_not_clean",
                "首阶段发布只允许专用空 Backend；目标已存在 Material",
            )
        if self._paged("/workflows"):
            raise WorkspaceHostError(
                "release_target_not_clean",
                "首阶段发布只允许专用空 Backend；目标已存在 Workflow",
            )
        if release.workflows and self.before_workflows is None:
            raise WorkspaceHostError(
                "release_activation_required",
                "包含设备动作的 WorkspaceRelease 必须允许受管 Edge 注册后再导入工作流",
            )
        _prepare_deployment_templates(release)
        return DeploymentPlan(
            release=release,
            target_address=self.target_api,
            template_count=len(release.templates),
            material_count=len(material_nodes),
            workflow_count=len(release.workflows),
        )

    def inspect(self) -> dict[str, Any]:
        """Return destructive-reset preflight counts for the target."""

        templates = self._paged("/resource-templates")
        materials = self._paged("/materials", with_children="true")
        workflows = self._paged("/workflows")
        return {
            "targetAddress": self.target_address,
            "empty": not templates and not materials and not workflows,
            "counts": {
                "templates": len(templates),
                "materials": len(materials),
                "workflows": len(workflows),
            },
        }

    def clear(self) -> dict[str, Any]:
        """Delete target authoring data in dependency-safe order."""

        before = self.inspect()
        for workflow in self._paged("/workflows"):
            self._request("DELETE", f"/workflows/{_required_identity(workflow, 'workflow')}")

        materials = self._paged("/materials", with_children="true")
        roots = [item for item in materials if not item.get("parent_uuid")]
        for material in roots:
            self._request(
                "DELETE",
                f"/materials/{_required_identity(material, 'material')}",
                json={
                    "idempotency_key": (
                        "workspace-release-reset/"
                        f"{_required_identity(material, 'material')}"
                    )
                },
            )
        for template in self._paged("/resource-templates"):
            self._request(
                "DELETE",
                f"/resource-templates/{_required_identity(template, 'resource template')}",
            )
        after = self.inspect()
        if not after["empty"]:
            raise WorkspaceHostError(
                "release_target_reset_failed",
                "Backend 清空后仍存在模板、物料或工作流",
                details={"target": after},
            )
        return {"before": before, "after": after}

    def apply(self, plan: DeploymentPlan) -> DeploymentResult:
        release = plan.release
        prepared = _prepare_deployment_templates(release)
        definitions = [
            _deployment_template_definition(
                item,
                release.workflow_node_templates,
            )
            for item in prepared.templates
        ]
        source_template_names = {
            str(item.get("uuid") or ""): str(item.get("name") or "")
            for item in release.templates
        }
        # 首次创建目标模板前先证明所有 Site 模板引用都属于当前发布代；使用本地
        # 身份自映射只做校验，不改变待发布事实。
        _remap_available_site_template_identities(
            definitions,
            source_template_names,
            {
                template_name: template_uuid
                for template_uuid, template_name in source_template_names.items()
            },
            error_code="release_source_invalid",
        )
        self._request(
            "POST", "/resource-templates", json={"resources": definitions}
        )
        target_templates = {
            str(item.get("name") or ""): str(item.get("uuid") or "")
            for item in self._paged("/resource-templates")
        }
        missing_templates = sorted(
            name for name in source_template_names.values() if name not in target_templates
        )
        if missing_templates:
            raise WorkspaceHostError(
                "release_apply_failed",
                "Backend 未返回全部资源模板身份",
                details={"templates": missing_templates},
            )
        if _remap_available_site_template_identities(
            definitions,
            source_template_names,
            target_templates,
            error_code="release_apply_failed",
        ):
            # 目标 UUID 只有在首次模板 upsert 后才可获得；第二次原子 upsert
            # 把 allowed/default 两类 Site 模板引用一起收敛到目标权威。
            self._request(
                "POST", "/resource-templates", json={"resources": definitions}
            )
        material_identities = self._apply_materials(
            release,
            source_template_names,
            target_templates,
            prepared.material_template_names,
        )
        if release.workflows and self.before_workflows is not None:
            self.before_workflows()
        workflow_identities = self._apply_workflows(
            release,
            material_identities,
            source_template_names,
            target_templates,
            prepared.material_template_names,
        )
        return DeploymentResult(
            release_id=release.release_id,
            target_address=self.target_api,
            template_identities=target_templates,
            material_identities=material_identities,
            workflow_identities=workflow_identities,
            material_template_names=prepared.material_template_names,
        )

    def _apply_materials(
        self,
        release: WorkspaceRelease,
        source_template_names: Mapping[str, str],
        target_templates: Mapping[str, str],
        material_template_names: Mapping[str, str],
    ) -> dict[str, str]:
        nodes = _material_nodes(release.material_graph)
        node_by_uuid = {
            str(_mapping(node.get("material"), "material").get("uuid") or ""): node
            for node in nodes
        }
        local_site_owner: dict[str, tuple[str, str]] = {}
        for local_uuid, node in node_by_uuid.items():
            for site in _mapping_list(node.get("sites")):
                site_uuid = str(site.get("uuid") or "")
                if site_uuid:
                    local_site_owner[site_uuid] = (
                        local_uuid,
                        str(site.get("name") or ""),
                    )
        pending = dict(node_by_uuid)
        identities: dict[str, str] = {}
        targets_by_barcode: dict[str, Mapping[str, Any]] = {}
        while pending:
            progressed = False
            for local_uuid, node in list(pending.items()):
                material = _mapping(node.get("material"), "material")
                barcode = str(material.get("barcode") or "").strip()
                existing = targets_by_barcode.get(barcode)
                if existing is not None:
                    identities[local_uuid] = _required_identity(existing, "material")
                    del pending[local_uuid]
                    progressed = True
                    continue
                parent_uuid = str(material.get("parent_uuid") or "")
                if parent_uuid and parent_uuid not in identities:
                    continue
                local_template_uuid = str(material.get("resource_template_uuid") or "")
                template_name = material_template_names.get(
                    local_uuid,
                    source_template_names.get(local_template_uuid, ""),
                )
                target_template_uuid = target_templates.get(str(template_name or ""))
                if not target_template_uuid:
                    raise WorkspaceHostError(
                        "release_apply_failed",
                        f"Material {local_uuid} 的资源模板未映射",
                    )
                metadata = deepcopy(_mapping_or_empty(material.get("meta_data")))
                metadata["unilab_release"] = {
                    "release_id": release.release_id,
                    "source_material_uuid": local_uuid,
                    "source_workspace": release.source_workspace,
                }
                body: dict[str, Any] = {
                    "resource_template_uuid": target_template_uuid,
                    "parent_uuid": identities.get(parent_uuid),
                    "barcode": barcode,
                    "name": material.get("name"),
                    "description": material.get("description"),
                    "meta_data": metadata,
                    "config": deepcopy(_mapping_or_empty(material.get("config"))),
                    "data": deepcopy(_mapping_or_empty(material.get("data"))),
                    "relative_position": deepcopy(node.get("relative_position")),
                    "idempotency_key": f"unilab-release/{release.release_id}/{local_uuid}",
                    "expected_revision": 0,
                }
                current_site_uuid = str(node.get("current_site_uuid") or "")
                if current_site_uuid:
                    owner = local_site_owner.get(current_site_uuid)
                    if owner is None or owner[0] not in identities:
                        continue
                    target_site_uuid = self._target_site_uuid(
                        identities[owner[0]], owner[1]
                    )
                    body["site_placement"] = {
                        "action": "place",
                        "site_uuid": target_site_uuid,
                    }
                created = self._request("POST", "/materials", json=body)
                identities[local_uuid] = _required_identity(created, "material")
                for target_material in [created, *_mapping_list(created.get("children"))]:
                    target_barcode = str(target_material.get("barcode") or "")
                    if target_barcode:
                        targets_by_barcode[target_barcode] = target_material
                del pending[local_uuid]
                progressed = True
            if not progressed:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    "Material 父关系或库位关系无法按依赖顺序迁移",
                    details={"materials": sorted(pending)},
                )
        return identities

    def _target_site_uuid(self, material_uuid: str, site_name: str) -> str:
        detail = self._request("GET", f"/materials/{material_uuid}")
        for site in _mapping_list(detail.get("sites")):
            if str(site.get("name") or "").casefold() == site_name.casefold():
                return _required_identity(site, "site")
        raise WorkspaceHostError(
            "release_apply_failed",
            f"Backend 模板未重建库位 {site_name}",
        )

    def _apply_workflows(
        self,
        release: WorkspaceRelease,
        material_identities: Mapping[str, str],
        source_template_names: Mapping[str, str],
        target_templates: Mapping[str, str],
        material_template_names: Mapping[str, str],
    ) -> dict[str, str]:
        identities: dict[str, str] = {}
        workflow_template_identities: dict[str, str] = {}
        workflow_publications: dict[str, Mapping[str, Any]] = {}
        resource_template_identities = {
            source_uuid: target_templates[name]
            for source_uuid, name in source_template_names.items()
            if name in target_templates
        }
        for item in _workflow_dependency_order(release.workflows):
            graph = _backend_workflow_projection(
                _mapping(item.get("graph"), "workflow graph"),
                known_material_uuids=set(material_identities),
            )
            graph = _bind_backend_material_sources(
                graph,
                material_graph=release.material_graph,
                material_identities=material_identities,
                material_template_names=material_template_names,
                source_template_names=source_template_names,
                target_templates=target_templates,
                target_site_resolver=lambda owner_uuid, site_name: (
                    self._target_site_uuid(
                        material_identities[owner_uuid], site_name
                    )
                ),
            )
            import_graph = self._prepare_composite_import_graph(
                graph,
                release=release,
                publications=workflow_publications,
                material_identities=material_identities,
                resource_template_identities=resource_template_identities,
            )
            workflow = _mapping(graph.get("workflow"), "workflow")
            source_uuid = str(workflow.get("uuid") or "")
            payload = _workflow_import_payload(
                import_graph,
                release=release,
                workflow_identities=identities,
                workflow_template_identities=workflow_template_identities,
                material_identities=material_identities,
                source_template_names=source_template_names,
                resource_template_identities=resource_template_identities,
            )
            imported = self._request("POST", "/workflows/import", json=payload)
            target_workflow = _mapping(imported.get("workflow"), "imported workflow")
            target_uuid = _required_identity(target_workflow, "workflow")
            identities[source_uuid] = target_uuid
            imported_graph = self._request("GET", f"/workflows/{target_uuid}/graph")
            remapped_graph = _remap_imported_workflow_graph(
                graph,
                imported_graph,
                release=release,
                workflow_identities=identities,
                material_identities=material_identities,
                resource_template_identities=resource_template_identities,
            )
            remapped_workflow = _mapping(
                remapped_graph.get("workflow"), "remapped workflow"
            )
            updated_workflow = self._request(
                "PUT",
                f"/workflows/{target_uuid}",
                json={
                    "name": remapped_workflow.get("name"),
                    "tags": deepcopy(remapped_workflow.get("tags") or []),
                    "description": remapped_workflow.get("description"),
                    "meta_data": deepcopy(
                        _mapping_or_empty(remapped_workflow.get("meta_data"))
                    ),
                },
            )
            imported_graph = self._request(
                "GET", f"/workflows/{target_uuid}/graph"
            )
            publication_graph = _repair_public_node_metadata(
                imported_graph,
                remapped_graph,
            )
            for node_uuid, patch in _workflow_node_patches(
                imported_graph,
                publication_graph,
                fields=("meta_data",),
            ):
                self._request(
                    "PATCH",
                    f"/workflow-nodes/{node_uuid}",
                    json=patch,
                )
            imported_graph = self._request(
                "GET", f"/workflows/{target_uuid}/graph"
            )
            revision = int(
                _mapping(imported_graph.get("workflow"), "imported workflow").get(
                    "revision"
                )
                or updated_workflow.get("revision")
                or 0
            )
            publication = self._request(
                "POST",
                f"/workflows/{target_uuid}/publications",
                json={"revision": revision},
            )
            workflow_template_identities[source_uuid] = _required_field(
                publication,
                "node_template_uuid",
                "published workflow node template",
            )
            workflow_publications[source_uuid] = deepcopy(dict(publication))
            current_graph = self._request(
                "GET", f"/workflows/{target_uuid}/graph"
            )
            authoring_graph = _restore_public_authoring_params(
                current_graph,
                remapped_graph,
            )
            for node_uuid, patch in _workflow_node_patches(
                current_graph,
                authoring_graph,
                fields=("param",),
            ):
                self._request(
                    "PATCH",
                    f"/workflow-nodes/{node_uuid}",
                    json=patch,
                )
        return identities

    def _prepare_composite_import_graph(
        self,
        graph: Mapping[str, Any],
        *,
        release: WorkspaceRelease,
        publications: Mapping[str, Mapping[str, Any]],
        material_identities: Mapping[str, str],
        resource_template_identities: Mapping[str, str],
    ) -> dict[str, Any]:
        """在正式导入前通过 Backend 公共接口展开组合工作流。

        参数：``graph`` 是来源工作流图；``release`` 提供本次工作区发布物料图；
        ``publications`` 是依赖工作流的已发布合同；两个身份映射把来源物料和资源
        模板转换为目标 Backend 身份。返回移除临时展开身份后的独立导入图。异常：
        依赖未发布、执行设备无法绑定或 Backend 展开失败时抛
        ``WorkspaceHostError``；临时工作流始终在 ``finally`` 中删除。
        """

        roots = [
            node
            for node in _mapping_list(graph.get("nodes"))
            if str(node.get("type") or "").strip().casefold() == "workflow"
            and not node.get("parent_uuid")
        ]
        if not roots:
            return deepcopy(dict(graph))

        source_nodes = {
            str(node.get("uuid") or ""): node
            for node in _mapping_list(graph.get("nodes"))
        }
        source_templates = {
            str(template.get("uuid") or ""): template
            for template in _mapping_list(graph.get("node_templates"))
        }
        target_materials_by_template: dict[str, list[str]] = {}
        for material_node in _material_nodes(release.material_graph):
            material = _mapping_or_empty(material_node.get("material"))
            source_material_uuid = str(material.get("uuid") or "")
            target_material_uuid = material_identities.get(source_material_uuid)
            source_template_uuid = str(
                material.get("resource_template_uuid")
                or _mapping_or_empty(
                    material_node.get("resource_template")
                ).get("uuid")
                or ""
            )
            target_template_uuid = resource_template_identities.get(
                source_template_uuid
            )
            if target_material_uuid and target_template_uuid:
                target_materials_by_template.setdefault(
                    target_template_uuid, []
                ).append(target_material_uuid)
        for values in target_materials_by_template.values():
            values.sort()

        temporary = self._request(
            "POST",
            "/workflows",
            json={
                "name": f"__unilab_release_prepare__{release.release_id[-12:]}",
                "tags": ["unilab-release-temporary"],
                "meta_data": {},
            },
        )
        temporary_uuid = _required_identity(temporary, "temporary workflow")
        temporary_graph: Mapping[str, Any] = {
            "workflow": temporary,
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        }
        try:
            temporary_root_identities: dict[str, str] = {}
            for root in sorted(roots, key=lambda item: str(item.get("uuid") or "")):
                source_template = source_templates.get(
                    str(root.get("workflow_node_template_uuid") or "")
                )
                dependency_uuid = str(
                    _mapping_or_empty(
                        _mapping_or_empty(
                            _mapping_or_empty(root.get("meta_data")).get(
                                "unilab"
                            )
                        ).get("composite")
                    ).get("child_workflow_uuid")
                    or (
                        str(source_template.get("name") or "").removeprefix(
                            "workflow:"
                        )
                        if source_template
                        else ""
                    )
                )
                published = publications.get(dependency_uuid)
                if published is None:
                    raise WorkspaceHostError(
                        "release_apply_failed",
                        f"组合工作流依赖尚未发布：{dependency_uuid}",
                    )
                subtree_materials: dict[str, list[str]] = {}
                root_uuid = str(root.get("uuid") or "")
                for node in source_nodes.values():
                    if str(node.get("parent_uuid") or "") != root_uuid:
                        continue
                    source_material_uuid = str(node.get("material_uuid") or "")
                    target_material_uuid = material_identities.get(
                        source_material_uuid
                    )
                    source_node_template = source_templates.get(
                        str(node.get("workflow_node_template_uuid") or "")
                    )
                    source_resource_template = str(
                        source_node_template.get("resource_template_uuid") or ""
                    ) if source_node_template else ""
                    target_resource_template = resource_template_identities.get(
                        source_resource_template
                    )
                    if target_material_uuid and target_resource_template:
                        subtree_materials.setdefault(
                            target_resource_template, []
                        ).append(target_material_uuid)
                device_bindings: dict[str, str] = {}
                for requirement in _mapping_list(
                    published.get("executor_requirements")
                ):
                    requirement_key = str(requirement.get("key") or "")
                    target_template_uuid = str(
                        requirement.get("resource_template_uuid") or ""
                    )
                    candidates = sorted(
                        set(subtree_materials.get(target_template_uuid) or [])
                    ) or target_materials_by_template.get(target_template_uuid, [])
                    if not requirement_key or not candidates:
                        raise WorkspaceHostError(
                            "release_apply_failed",
                            f"组合工作流执行器 {requirement_key} 无法绑定目标设备",
                        )
                    device_bindings[requirement_key] = candidates[0]
                param = deepcopy(dict(_mapping_or_empty(root.get("param"))))
                for contract_input in _mapping_list(
                    _mapping_or_empty(published.get("input_contract")).get(
                        "parameters"
                    )
                ):
                    name = str(contract_input.get("name") or "")
                    if not name or name in param or not contract_input.get("required"):
                        continue
                    declared_type = str(contract_input.get("type") or "")
                    schema = (
                        {"$slot": "ResourceSlot"}
                        if declared_type == "ResourceSlot"
                        else {"type": declared_type.casefold()}
                    )
                    param[name] = _publication_scaffold_value(
                        schema,
                        material_identities=material_identities,
                        parameter_name=name,
                    )
                # Composite invocation UUIDs are globally unique in Backend.
                # The source UUID may already belong to an imported workflow,
                # so the temporary expansion must use an isolated identity.
                temporary_root_uuid = str(
                    uuid.uuid5(
                        uuid.UUID(temporary_uuid),
                        f"unilab-release-prepare:{root_uuid}",
                    )
                )
                temporary_root_identities[temporary_root_uuid] = root_uuid
                temporary_graph = self._request(
                    "POST",
                    f"/workflows/{temporary_uuid}/composite-invocations",
                    json={
                        "revision": int(
                            _mapping(
                                temporary_graph.get("workflow"),
                                "temporary workflow",
                            ).get("revision")
                            or 0
                        ),
                        "contract_uuid": _required_identity(
                            published, "published workflow contract"
                        ),
                        "invocation_uuid": temporary_root_uuid,
                        "device_bindings": device_bindings,
                        "pose": deepcopy(_mapping_or_empty(root.get("pose"))),
                        "param": param,
                    },
                )
            return _merge_backend_composite_expansions(
                graph,
                temporary_graph,
                roots=roots,
                backend_root_identities=temporary_root_identities,
            )
        finally:
            self._request("DELETE", f"/workflows/{temporary_uuid}")

    def _paged(self, path: str, **extra: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request(
                "GET", path, params={"page": page, "page_size": 100, **extra}
            )
            page_items = _mapping_list(payload.get("items"))
            items.extend(dict(item) for item in page_items)
            if path == "/materials":
                if len(items) >= int(payload.get("total") or 0) or not page_items:
                    return items
            elif not payload.get("has_more"):
                return items
            page += 1

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.credential}"
        headers.setdefault("Content-Type", "application/json")
        response = getattr(self.session, method.lower())(
            f"{self.target_api}{path}",
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        return _release_response(response, f"Backend {method} {path}")


class ExistingBackendReleaseVerifier:
    """Read back all three domain projections through public Backend APIs."""

    def __init__(self, target: ExistingBackendDeploymentTarget) -> None:
        self.target = target

    def verify(
        self, release: WorkspaceRelease, result: DeploymentResult
    ) -> VerificationReport:
        diagnostics: list[str] = []
        target_templates = {
            str(item.get("name") or ""): str(item.get("uuid") or "")
            for item in self.target._paged("/resource-templates")
        }
        for template in release.templates:
            name = str(template.get("name") or "")
            if not target_templates.get(name):
                diagnostics.append(f"resource template missing: {name}")

        target_graph = self.target._request("GET", "/materials/graph")
        target_by_barcode = {
            str(_mapping(node.get("material"), "material").get("barcode") or ""): node
            for node in _material_nodes(target_graph)
        }
        material_count = 0
        for source_node in _material_nodes(release.material_graph):
            source_material = _mapping(source_node.get("material"), "material")
            barcode = str(source_material.get("barcode") or "")
            target_node = target_by_barcode.get(barcode)
            if target_node is None:
                diagnostics.append(f"material missing: {barcode}")
                continue
            material_count += 1
            target_material = _mapping(target_node.get("material"), "material")
            if str(target_material.get("name") or "") != str(source_material.get("name") or ""):
                diagnostics.append(f"material name mismatch: {barcode}")
            source_template = _mapping(source_node.get("resource_template"), "resource_template")
            target_template = _mapping(target_node.get("resource_template"), "resource_template")
            expected_template_name = (result.material_template_names or {}).get(
                str(source_material.get("uuid") or ""),
                str(source_template.get("name") or ""),
            )
            if expected_template_name != target_template.get("name"):
                diagnostics.append(f"material template mismatch: {barcode}")
            for display_field in ("display_name",):
                if source_template.get(display_field) != target_template.get(display_field):
                    diagnostics.append(
                        f"material template {display_field} mismatch: {barcode}"
                    )

        workflow_count = 0
        for item in release.workflows:
            source_graph = _backend_workflow_projection(
                _mapping(item.get("graph"), "workflow graph"),
                known_material_uuids=set(result.material_identities),
            )
            source_workflow = _mapping(source_graph.get("workflow"), "workflow")
            source_uuid = str(source_workflow.get("uuid") or "")
            target_uuid = result.workflow_identities.get(source_uuid)
            if not target_uuid:
                diagnostics.append(f"workflow identity missing: {source_uuid}")
                continue
            target_workflow_graph = self.target._request(
                "GET", f"/workflows/{target_uuid}/graph"
            )
            if _normalized_workflow(source_graph, source=True) != _normalized_workflow(
                target_workflow_graph, source=False
            ):
                diagnostics.append(f"workflow graph mismatch: {source_uuid}")
                continue
            workflow_count += 1
        return VerificationReport(
            verified=not diagnostics,
            template_count=len(release.templates) - sum(
                item.startswith("resource template missing:") for item in diagnostics
            ),
            material_count=material_count,
            workflow_count=workflow_count,
            diagnostics=tuple(diagnostics),
        )


def create_existing_backend_publisher(
    *,
    source_address: str,
    source_workspace: Path,
    target_address: str,
    credential: str,
    deployment_directory: Path,
    session: requests.Session | None = None,
    timeout: float = 30.0,
    before_workflows: Callable[[], None] | None = None,
) -> WorkspaceReleasePublisher:
    """Create the first-stage existing-Backend Adapter composition."""

    shared_session = session or requests.Session()
    builder = LocalBackendReleaseBuilder(
        source_address,
        source_workspace,
        session=shared_session,
        timeout=timeout,
    )
    target = ExistingBackendDeploymentTarget(
        target_address,
        credential,
        session=shared_session,
        timeout=timeout,
        before_workflows=before_workflows,
    )
    return WorkspaceReleasePublisher(
        builder,
        target,
        ExistingBackendReleaseVerifier(target),
        deployment_directory=deployment_directory,
    )


def _deployment_template_definition(
    template: Mapping[str, Any],
    workflow_node_templates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Restore Backend action mappings from the shared Local catalog contract."""

    definition = _template_definition(template)
    if template.get("_unilab_release_derived"):
        definition["class"]["action_value_mappings"] = {}
        return definition
    template_uuid = str(template.get("uuid") or "")
    actions: dict[str, dict[str, Any]] = {}
    for detail in workflow_node_templates:
        action = _mapping(detail.get("template"), "workflow node template")
        if str(action.get("resource_template_uuid") or "") != template_uuid:
            continue
        action_name = str(action.get("name") or "").strip()
        if not action_name:
            raise WorkspaceHostError(
                "release_source_invalid", "WorkflowNodeTemplate 缺少动作业务身份"
            )
        input_handles: list[dict[str, Any]] = []
        output_handles: list[dict[str, Any]] = []
        for raw_handle in _mapping_list(detail.get("handles")):
            handle_key = str(raw_handle.get("handle_key") or "").strip()
            if handle_key == "ready":
                continue
            handle = {
                "label": raw_handle.get("display_name") or handle_key,
                "data_key": raw_handle.get("data_key"),
                "data_type": raw_handle.get("type") or "default",
                "data_source": raw_handle.get("data_source"),
                "handler_key": handle_key,
            }
            if raw_handle.get("io_type") == "target":
                input_handles.append(handle)
            elif raw_handle.get("io_type") == "source":
                output_handles.append(handle)
        schema = action.get("schema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except ValueError as error:
                raise WorkspaceHostError(
                    "release_source_invalid",
                    f"WorkflowNodeTemplate {action_name} schema 不是有效 JSON",
                ) from error
        action_definition = {
            "feedback": deepcopy(_mapping_or_empty(action.get("feedback"))),
            "goal": deepcopy(_mapping_or_empty(action.get("goal"))),
            "goal_default": deepcopy(
                _mapping_or_empty(action.get("goal_default"))
            ),
            "result": deepcopy(_mapping_or_empty(action.get("result"))),
            "schema": schema,
            "type": action.get("type"),
            "node_type": action.get("node_type"),
            "display_name": action.get("display_name") or action_name,
            "handles": {"input": input_handles, "output": output_handles},
        }
        executor_kind = str(
            action.get("executor_kind")
            or _mapping_or_empty(
                _mapping_or_empty(action.get("meta_data")).get("unilab")
            ).get("executor_kind")
            or ""
        ).strip()
        if executor_kind:
            action_definition["executor_kind"] = executor_kind
        if executor_kind == "material_transfer":
            # Local authoring may still expose HostNode.transfer_resource as a
            # regular device action.  Backend owns this mutation atomically,
            # so its deployment catalog must advertise the trusted ILab seam
            # instead of routing the node back through Edge device dispatch.
            action_definition["node_type"] = "ILab"
        actions[action_name] = action_definition
    definition["class"]["action_value_mappings"] = actions
    return definition


def _prepare_deployment_templates(
    release: WorkspaceRelease,
) -> PreparedDeploymentTemplates:
    """Promote verified instance-owned Sites into release-only templates.

    Backend creates Sites from the resource template ``available_sites``
    contract. Local resource graphs may own the same declaration on one
    material instance so labels can vary per physical carrier. The Adapter
    clones only those templates and records the chosen derived name per source
    material; source facts stay immutable and readback can still prove the
    visible graph.
    """

    nodes = _material_nodes(release.material_graph)
    templates = {
        str(item.get("uuid") or ""): item for item in release.templates
    }
    deployment_templates: list[Mapping[str, Any]] = [
        deepcopy(dict(item)) for item in release.templates
    ]
    material_template_names: dict[str, str] = {}
    material_types_by_template: dict[str, set[str]] = {}
    leaf_material_types_by_template: dict[str, set[str]] = {}
    for node in nodes:
        material = _mapping(node.get("material"), "material")
        template_uuid = str(material.get("resource_template_uuid") or "")
        material_type = str(material.get("type") or "").strip()
        if template_uuid and material_type:
            material_types_by_template.setdefault(template_uuid, set()).add(
                material_type
            )
            if material.get("parent_uuid") and not _mapping_list(node.get("sites")):
                leaf_material_types_by_template.setdefault(
                    template_uuid, set()
                ).add(material_type)
    for template in deployment_templates:
        template_uuid = str(template.get("uuid") or "")
        leaf_types = leaf_material_types_by_template.get(template_uuid, set())
        if len(leaf_types) > 1:
            raise WorkspaceHostError(
                "release_material_template_type_ambiguous",
                "同一资源模板的叶子物料具有多个运行类型，无法无损发布",
                details={
                    "resource_template_uuid": template_uuid,
                    "material_types": sorted(leaf_types, key=str.casefold),
                },
            )
        if not leaf_types:
            continue
        material_type = next(iter(leaf_types))
        config_info = [
            deepcopy(dict(component))
            for component in _mapping_list(template.get("config_info"))
        ]
        if config_info:
            config_info[0]["type"] = material_type
        else:
            template_name = str(template.get("name") or "").strip()
            config_info = [
                {
                    "id": "root",
                    "name": template.get("display_name") or template_name,
                    "class": template_name,
                    "type": material_type,
                    "config": {},
                    "data": {},
                }
            ]
        template["config_info"] = config_info
    barcodes: set[str] = set()
    for node in nodes:
        material = _mapping(node.get("material"), "material")
        material_uuid = str(material.get("uuid") or "")
        barcode = str(material.get("barcode") or "").strip()
        if not material_uuid or not barcode or barcode in barcodes:
            raise WorkspaceHostError(
                "release_material_identity_invalid",
                "每个发布 Material 必须有 UUID 和唯一非空 barcode",
            )
        barcodes.add(barcode)
        template = templates.get(str(material.get("resource_template_uuid") or ""))
        if template is None:
            raise WorkspaceHostError(
                "release_material_identity_invalid", "Material 引用未知资源模板"
            )
        template_name = str(template.get("name") or "").strip()
        if not template_name:
            raise WorkspaceHostError(
                "release_material_identity_invalid", "资源模板缺少业务身份"
            )
        declared_sites = _declared_template_sites(template)
        actual_sites = {
            str(site.get("name") or "") for site in _mapping_list(node.get("sites"))
        }
        missing_sites = sorted(actual_sites - declared_sites)
        root_requires_non_resource_template = (
            not material.get("parent_uuid")
            and str(template.get("resource_type") or "").casefold() == "resource"
        )
        root_requires_material_type_override = (
            not material.get("parent_uuid")
            and str(material.get("type") or "").casefold()
            != str(template.get("resource_type") or "").casefold()
        )
        if (
            not actual_sites
            and not root_requires_non_resource_template
            and not root_requires_material_type_override
        ):
            material_template_names[material_uuid] = template_name
            continue
        config = deepcopy(_mapping_or_empty(material.get("config")))
        configured_sites = {
            str(site.get("name") or site.get("label") or "").strip(): site
            for site in _mapping_list(config.get("sites"))
        }
        if actual_sites and (
            set(configured_sites) != actual_sites
            or not _sites_match_instance_config(
                _mapping_list(node.get("sites")), configured_sites
            )
        ):
            raise WorkspaceHostError(
                "release_site_not_reconstructable",
                "Backend 没有 Site 写接口；库位必须可由模板或实例配置无损重建",
                details={"sites": missing_sites, "material": barcode},
            )
        if actual_sites:
            config["sites"] = _translated_backend_sites(
                _mapping_list(config.get("sites")),
                _mapping_list(node.get("sites")),
                material_types_by_template,
            )
        suffix = hashlib.sha256(material_uuid.encode("utf-8")).hexdigest()[:16]
        derived_name = (
            f"{template_name[:190]}.__unilab_release__.{suffix}"
        )
        derived = deepcopy(dict(template))
        derived["name"] = derived_name
        derived["_unilab_release_derived"] = True
        if root_requires_non_resource_template:
            derived["resource_type"] = material.get("type") or "device"
        derived["config_info"] = [
            {
                "id": "root",
                "name": material.get("name") or "root",
                "class": material.get("class") or template_name,
                "type": material.get("type") or template.get("resource_type"),
                "config": config,
                "data": deepcopy(_mapping_or_empty(material.get("data"))),
            }
        ]
        derived["available_sites"] = _translated_backend_available_sites(
            _mapping_list(config.get("sites")),
            _mapping_list(node.get("sites")),
            material_types_by_template,
        )
        deployment_templates.append(derived)
        material_template_names[material_uuid] = derived_name
    return PreparedDeploymentTemplates(
        templates=tuple(deployment_templates),
        material_template_names=material_template_names,
    )


def _sites_match_instance_config(
    actual_sites: Sequence[Mapping[str, Any]],
    configured_sites: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Prove Site business identity and geometry before template promotion."""

    for site in actual_sites:
        name = str(site.get("name") or "")
        configured = configured_sites.get(name)
        if configured is None:
            return False
        position = _mapping_or_empty(configured.get("position"))
        size = _mapping_or_empty(configured.get("size"))
        pairs = (
            (site.get("position_x"), position.get("x")),
            (site.get("position_y"), position.get("y")),
            (site.get("position_z"), position.get("z")),
            (site.get("depth"), size.get("depth")),
            (site.get("length"), size.get("height")),
            (site.get("width"), size.get("width")),
        )
        try:
            if any(abs(float(actual) - float(expected)) > 1e-9 for actual, expected in pairs):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _translated_backend_sites(
    configured_sites: Sequence[Mapping[str, Any]],
    actual_sites: Sequence[Mapping[str, Any]],
    material_types_by_template: Mapping[str, set[str]],
) -> list[dict[str, Any]]:
    """Translate Local template UUID admission into Backend Material.type rules."""

    actual_by_name = {
        str(site.get("name") or ""): site for site in actual_sites
    }
    translated: list[dict[str, Any]] = []
    for configured_site in configured_sites:
        site = deepcopy(dict(configured_site))
        name = str(site.get("name") or site.get("label") or "")
        actual = actual_by_name.get(name)
        allowed_types: set[str] = set()
        if actual is not None:
            for template_uuid in actual.get("allowed_resource_template_uuids") or []:
                allowed_types.update(
                    material_types_by_template.get(str(template_uuid), set())
                )
        if allowed_types:
            site["content_type"] = sorted(allowed_types, key=str.casefold)
        translated.append(site)
    return translated


def _translated_backend_available_sites(
    configured_sites: Sequence[Mapping[str, Any]],
    actual_sites: Sequence[Mapping[str, Any]],
    material_types_by_template: Mapping[str, set[str]],
) -> list[dict[str, Any]]:
    """Build the Backend v1 Site source of truth without dangling Local UUIDs."""

    configured_by_name = {
        str(site.get("name") or site.get("label") or ""): site
        for site in configured_sites
    }
    translated: list[dict[str, Any]] = []
    for fallback_index, actual in enumerate(actual_sites):
        name = str(actual.get("name") or "")
        configured = configured_by_name.get(name, {})
        allowed_types: set[str] = set()
        for template_uuid in actual.get("allowed_resource_template_uuids") or []:
            allowed_types.update(
                material_types_by_template.get(str(template_uuid), set())
            )
        if not allowed_types:
            allowed_types.update(
                str(value).strip()
                for value in configured.get("content_type") or []
                if str(value).strip()
            )
        translated_site = {
            "schema_version": 1,
            "index": configured.get(
                "index", actual.get("sort_order", fallback_index)
            ),
            "label": name,
            "visible": bool(configured.get("visible", True)),
            "position_x": actual.get("position_x", 0),
            "position_y": actual.get("position_y", 0),
            "position_z": actual.get("position_z", 0),
            "rotation_x": actual.get("rotation_x", 0),
            "rotation_y": actual.get("rotation_y", 0),
            "rotation_z": actual.get("rotation_z", 0),
            "parent_link": actual.get("parent_link") or "",
            "width": actual.get("width", 0),
            "length": actual.get("length", 0),
            "depth": actual.get("depth", 0),
            "content_type": sorted(allowed_types, key=str.casefold),
            # Backend UUIDs do not exist until template upsert completes。
            # Dynamic Material.type 准入保留第一阶段 Adapter 语义。
            "allowed_resource_template_uuids": [],
            "description": actual.get("description"),
            "meta_data": deepcopy(
                _mapping_or_empty(actual.get("meta_data"))
            ),
        }
        if "initial_material_template_uuid" in configured:
            translated_site["initial_material_template_uuid"] = configured.get(
                "initial_material_template_uuid"
            )
        translated.append(translated_site)
    return translated


def _declared_template_sites(template: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for component in template.get("config_info") or []:
        if not isinstance(component, Mapping):
            continue
        candidates = list(component.get("sites") or [])
        config = component.get("config")
        if isinstance(config, Mapping):
            candidates.extend(config.get("sites") or [])
        for site in candidates:
            if isinstance(site, Mapping):
                name = str(site.get("name") or site.get("label") or "").strip()
                if name:
                    names.add(name)
    return names


def _workflow_dependency_order(
    workflows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    by_uuid = {
        str(_mapping(_mapping(item.get("graph"), "workflow graph").get("workflow"), "workflow").get("uuid") or ""): item
        for item in workflows
    }
    pending = dict(by_uuid)
    result: list[Mapping[str, Any]] = []
    emitted: set[str] = set()
    while pending:
        progressed = False
        for workflow_uuid, item in list(pending.items()):
            graph = _mapping(item.get("graph"), "workflow graph")
            dependencies = {
                name.removeprefix("workflow:")
                for template in _mapping_list(graph.get("node_templates"))
                if (name := str(template.get("name") or "")).startswith("workflow:")
            }
            if any(dependency in pending and dependency not in emitted for dependency in dependencies):
                continue
            result.append(item)
            emitted.add(workflow_uuid)
            del pending[workflow_uuid]
            progressed = True
        if not progressed:
            raise WorkspaceHostError(
                "release_workflow_dependency_cycle",
                "组合工作流依赖形成循环",
                details={"workflows": sorted(pending)},
            )
    return result


def _merge_backend_composite_expansions(
    source_graph: Mapping[str, Any],
    backend_graph: Mapping[str, Any],
    *,
    roots: Sequence[Mapping[str, Any]],
    backend_root_identities: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Replace Local composite snapshots with Backend-native expansions."""

    source_nodes = {
        str(node.get("uuid") or ""): node
        for node in _mapping_list(source_graph.get("nodes"))
    }
    backend_nodes = {
        str(node.get("uuid") or ""): node
        for node in _mapping_list(backend_graph.get("nodes"))
    }
    root_uuids = {str(root.get("uuid") or "") for root in roots}

    def child_key(node: Mapping[str, Any]) -> tuple[str, str, int]:
        unilab = _mapping_or_empty(
            _mapping_or_empty(node.get("meta_data")).get("unilab")
        )
        order = unilab.get("authoring_source_order")
        return (
            str(node.get("name") or ""),
            str(node.get("type") or "").casefold(),
            int(order) if isinstance(order, int) else -1,
        )

    backend_to_source: dict[str, str] = dict(
        backend_root_identities
        or {root_uuid: root_uuid for root_uuid in root_uuids}
    )
    if set(backend_to_source.values()) != root_uuids:
        raise WorkspaceHostError(
            "release_apply_failed", "Backend 组合根节点身份无法对应 Local"
        )
    source_private: set[str] = set()
    for backend_root_uuid, root_uuid in sorted(backend_to_source.items()):
        source_children = sorted(
            (
                node
                for node in source_nodes.values()
                if str(node.get("parent_uuid") or "") == root_uuid
            ),
            key=child_key,
        )
        backend_children = sorted(
            (
                node
                for node in backend_nodes.values()
                if str(node.get("parent_uuid") or "") == backend_root_uuid
            ),
            key=child_key,
        )
        if len(source_children) != len(backend_children) or [
            child_key(node) for node in source_children
        ] != [child_key(node) for node in backend_children]:
            raise WorkspaceHostError(
                "release_apply_failed",
                "Backend 组合工作流展开与 Local 子节点不一致",
                details={"invocationUuid": root_uuid},
            )
        for source_child, backend_child in zip(
            source_children, backend_children, strict=True
        ):
            source_uuid = str(source_child.get("uuid") or "")
            backend_uuid = str(backend_child.get("uuid") or "")
            source_private.add(source_uuid)
            backend_to_source[backend_uuid] = source_uuid

    merged_nodes: list[dict[str, Any]] = [
        deepcopy(dict(node))
        for node_uuid, node in source_nodes.items()
        if node_uuid not in root_uuids and node_uuid not in source_private
    ]
    for backend_uuid, source_uuid in backend_to_source.items():
        backend_node = backend_nodes.get(backend_uuid)
        source_node = source_nodes.get(source_uuid)
        if backend_node is None or source_node is None:
            raise WorkspaceHostError(
                "release_apply_failed", "Backend 组合节点身份无法对应 Local"
            )
        node = _replace_identities(
            deepcopy(dict(backend_node)), backend_to_source
        )
        node["uuid"] = source_uuid
        node["workflow_node_template_uuid"] = source_node.get(
            "workflow_node_template_uuid"
        )
        node["parent_uuid"] = source_node.get("parent_uuid")
        for field in ("name", "type", "pose", "execution_policy", "disabled", "minimized"):
            if field in source_node:
                node[field] = deepcopy(source_node.get(field))
        metadata = deepcopy(dict(_mapping_or_empty(node.get("meta_data"))))
        metadata["unilab_release"] = {"source_node_uuid": source_uuid}
        node["meta_data"] = metadata
        merged_nodes.append(node)

    merged_edges = [
        deepcopy(dict(edge))
        for edge in _mapping_list(source_graph.get("edges"))
        if str(edge.get("source_node_uuid") or "") not in source_private
        and str(edge.get("target_node_uuid") or "") not in source_private
    ]
    for edge in _mapping_list(backend_graph.get("edges")):
        source_uuid = str(edge.get("source_node_uuid") or "")
        target_uuid = str(edge.get("target_node_uuid") or "")
        if source_uuid not in backend_to_source or target_uuid not in backend_to_source:
            continue
        merged_edges.append(
            _replace_identities(deepcopy(dict(edge)), backend_to_source)
        )

    merged = deepcopy(dict(source_graph))
    merged["nodes"] = merged_nodes
    merged["edges"] = merged_edges
    templates = {
        str(item.get("uuid") or ""): deepcopy(dict(item))
        for item in [
            *_mapping_list(source_graph.get("node_templates")),
            *_mapping_list(backend_graph.get("node_templates")),
        ]
    }
    handles = {
        str(item.get("uuid") or ""): deepcopy(dict(item))
        for item in [
            *_mapping_list(source_graph.get("handle_templates")),
            *_mapping_list(backend_graph.get("handle_templates")),
        ]
    }
    merged["node_templates"] = list(templates.values())
    merged["handle_templates"] = list(handles.values())
    return merged


def _backend_workflow_projection(
    graph: Mapping[str, Any],
    *,
    known_material_uuids: set[str] | None = None,
) -> dict[str, Any]:
    """Remove Local-only visual groups from the graph published to Backend.

    Group nodes are an authoring/layout concern, not an executable Backend
    node kind. Children are promoted to the nearest non-group ancestor. A
    semantic edge or inventory requirement attached to a group fails closed
    because silently dropping it could change execution semantics.
    """

    projected = deepcopy(dict(graph))
    source_nodes = _mapping_list(graph.get("nodes"))
    nodes_by_uuid = {
        str(node.get("uuid") or ""): node
        for node in source_nodes
        if str(node.get("uuid") or "")
    }
    group_uuids = {
        node_uuid
        for node_uuid, node in nodes_by_uuid.items()
        if str(node.get("type") or "").strip().casefold() == "group"
    }
    def promoted_parent(node: Mapping[str, Any]) -> str | None:
        parent_uuid = str(node.get("parent_uuid") or "")
        visited: set[str] = set()
        while parent_uuid in group_uuids:
            if parent_uuid in visited:
                raise WorkspaceHostError(
                    "release_source_invalid",
                    "Workflow 分组父关系形成循环",
                    details={"groupNodeUuids": sorted(visited | {parent_uuid})},
                )
            visited.add(parent_uuid)
            parent = nodes_by_uuid[parent_uuid]
            parent_uuid = str(parent.get("parent_uuid") or "")
        return parent_uuid or None

    projected_nodes: list[dict[str, Any]] = []
    for source_node in source_nodes:
        node_uuid = str(source_node.get("uuid") or "")
        if node_uuid in group_uuids:
            continue
        node = deepcopy(dict(source_node))
        if str(node.get("parent_uuid") or "") in group_uuids:
            node["parent_uuid"] = promoted_parent(source_node)
        if (
            known_material_uuids is not None
            and str(node.get("type") or "").strip().casefold()
            == "material_source"
        ):
            param = dict(_mapping_or_empty(node.get("param")))
            material_uuid = str(param.get("material_uuid") or "")
            if material_uuid and material_uuid not in known_material_uuids:
                # Local authoring allocates a deterministic visual identity
                # when ``material_uuid=None``. It is not inventory and must
                # remain an unbound existing-material selector in Backend.
                param["material_uuid"] = None
                node["param"] = param
        projected_nodes.append(node)

    incident_edges = [
        str(edge.get("uuid") or "")
        for edge in _mapping_list(graph.get("edges"))
        if (
            str(edge.get("source_node_uuid") or "") in group_uuids
            or str(edge.get("target_node_uuid") or "") in group_uuids
        )
    ]
    incident_requirements = [
        str(requirement.get("uuid") or "")
        for requirement in _mapping_list(graph.get("inventory_requirements"))
        if str(requirement.get("consume_node_uuid") or "") in group_uuids
    ]
    if incident_edges or incident_requirements:
        raise WorkspaceHostError(
            "release_source_invalid",
            "Workflow 分组节点承载了执行语义，无法安全发布到 Backend",
            details={
                "edgeUuids": sorted(incident_edges),
                "inventoryRequirementUuids": sorted(incident_requirements),
            },
        )

    projected["nodes"] = projected_nodes
    _promote_backend_material_passthrough_output(projected)
    return projected


def _bind_backend_material_sources(
    graph: Mapping[str, Any],
    *,
    material_graph: Mapping[str, Any],
    material_identities: Mapping[str, str],
    material_template_names: Mapping[str, str],
    source_template_names: Mapping[str, str],
    target_templates: Mapping[str, str],
    target_site_resolver: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """把本地库存选择器转换为一份 Backend 发布快照。

    ``graph`` 是待发布工作流图，``material_graph`` 提供本地物料与库位事实；
    ``material_identities``、``material_template_names``、
    ``source_template_names`` 和 ``target_templates`` 分别完成发布身份与模板映射，
    ``target_site_resolver`` 可把本地库位（Site）转换为目标库位身份。返回图是深拷贝，
    不修改调用方输入；映射缺失或图结构不合法时抛出 ``ReleasePublishError``。

    Backend 只按精确模板分配既有物料。拥有库位（Site）的本地物料需要发布期派生模板，
    因此这里用一个兼容候选确定目标模板。共享来源或显式选择冻结具体物料身份；未显式
    指定实例的任务独占来源保持未绑定，由 Backend 在每条工作流任务（WorkflowTask）的
    任务物料准入（TaskMaterialAdmission）中选择不同的空闲物料。当前没有兼容库存的
    选择器也保持未绑定，使合法工作流在库存变化后仍可重新准入。
    """

    bound = deepcopy(dict(graph))
    material_nodes = _material_nodes(material_graph)
    materials: dict[str, Mapping[str, Any]] = {}
    site_owner: dict[str, tuple[str, int, str]] = {}
    for material_node in material_nodes:
        material = _mapping(material_node.get("material"), "material")
        material_uuid = str(material.get("uuid") or "")
        if material_uuid:
            materials[material_uuid] = material_node
        for fallback_order, site in enumerate(_mapping_list(material_node.get("sites"))):
            site_uuid = str(site.get("uuid") or "")
            if not site_uuid:
                continue
            raw_order = site.get("sort_order", fallback_order)
            try:
                order = int(raw_order)
            except (TypeError, ValueError):
                order = fallback_order
            site_owner[site_uuid] = (
                material_uuid,
                order,
                str(site.get("name") or ""),
            )

    def source_order(node: Mapping[str, Any]) -> tuple[int, str]:
        unilab = _mapping_or_empty(
            _mapping_or_empty(node.get("meta_data")).get("unilab")
        )
        raw_order = unilab.get("authoring_source_order")
        return (
            raw_order if isinstance(raw_order, int) else 2**31 - 1,
            str(node.get("uuid") or ""),
        )

    def material_order(material_uuid: str) -> tuple[int, str, str]:
        material_node = materials[material_uuid]
        current_site_uuid = str(material_node.get("current_site_uuid") or "")
        owner = site_owner.get(current_site_uuid)
        if owner is not None:
            return owner[1], owner[2].casefold(), material_uuid
        return 2**31 - 1, "", material_uuid

    source_nodes = sorted(
        (
            node
            for node in _mapping_list(bound.get("nodes"))
            if str(node.get("type") or "").strip().casefold()
            == "material_source"
        ),
        key=source_order,
    )
    selected_materials: set[str] = set()
    for node in source_nodes:
        param = deepcopy(dict(_mapping_or_empty(node.get("param"))))
        mode = str(param.get("mode") or "existing").strip().casefold()
        if mode != "existing":
            continue
        required_template_uuid = str(param.get("resource_template_uuid") or "")
        mount_uuid = str(
            _mapping_or_empty(param.get("mount")).get("uuid") or ""
        )
        explicit_material_uuid = str(param.get("material_uuid") or "")
        explicit_site = str(param.get("site") or "").strip()
        dynamic_task_exclusive = (
            not explicit_material_uuid
            and str(param.get("custody_policy") or "").strip().casefold()
            == "task_exclusive"
        )

        if explicit_material_uuid:
            candidates = [explicit_material_uuid]
        else:
            candidates = []
            for material_uuid, material_node in materials.items():
                material = _mapping(material_node.get("material"), "material")
                if (
                    str(material.get("resource_template_uuid") or "")
                    != required_template_uuid
                ):
                    continue
                current_site_uuid = str(
                    material_node.get("current_site_uuid") or ""
                )
                owner_uuid = str(
                    (site_owner.get(current_site_uuid) or ("", 0, ""))[0]
                )
                parent_uuid = str(material.get("parent_uuid") or "")
                if mount_uuid and mount_uuid not in {owner_uuid, parent_uuid}:
                    continue
                if explicit_site:
                    owner = site_owner.get(current_site_uuid)
                    if owner is None:
                        continue
                    if explicit_site not in {
                        current_site_uuid,
                        owner[2],
                    }:
                        continue
                candidates.append(material_uuid)
            candidates.sort(key=material_order)

        selected = next(
            (
                material_uuid
                for material_uuid in candidates
                if material_uuid in materials
                and material_uuid in material_identities
                and material_uuid not in selected_materials
            ),
            None,
        )
        if selected is None:
            # A workflow is valid authoring content even when its selector has
            # no matching inventory in the release snapshot.  Preserve that
            # unbound selector instead of either rejecting the whole release
            # or silently selecting material from another mount.  Backend run
            # admission will resolve it after compatible inventory is placed.
            target_template_name = str(
                source_template_names.get(required_template_uuid) or ""
            )
            target_template_uuid = str(
                target_templates.get(target_template_name) or ""
            )
            if not target_template_uuid:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    "MaterialSource 的发布模板身份未映射",
                    details={
                        "workflow_node_uuid": str(node.get("uuid") or ""),
                        "resource_template_uuid": required_template_uuid,
                        "template_name": target_template_name,
                    },
                )
            param["material_uuid"] = None
            param["resource_template_uuid"] = target_template_uuid
            if explicit_site:
                owner = site_owner.get(explicit_site)
                if owner is None:
                    owner = next(
                        (
                            candidate
                            for candidate in site_owner.values()
                            if candidate[0] == mount_uuid
                            and candidate[2].casefold()
                            == explicit_site.casefold()
                        ),
                        None,
                    )
                if owner is None:
                    raise WorkspaceHostError(
                        "release_material_source_unresolved",
                        "MaterialSource 指定的库位不存在",
                        details={
                            "workflow_node_uuid": str(node.get("uuid") or ""),
                            "site": explicit_site,
                        },
                    )
                if target_site_resolver is not None:
                    param["site"] = target_site_resolver(owner[0], owner[2])
            node["param"] = param
            continue

        target_template_name = str(material_template_names.get(selected) or "")
        target_template_uuid = str(
            target_templates.get(target_template_name) or ""
        )
        if not target_template_uuid:
            raise WorkspaceHostError(
                "release_apply_failed",
                "MaterialSource 的发布模板身份未映射",
                details={
                    "workflow_node_uuid": str(node.get("uuid") or ""),
                    "material_uuid": selected,
                    "template_name": target_template_name,
                },
            )
        param["material_uuid"] = (
            None
            if dynamic_task_exclusive
            else material_identities[selected]
        )
        param["resource_template_uuid"] = target_template_uuid
        if explicit_site:
            owner = site_owner.get(explicit_site)
            if owner is None:
                owner = next(
                    (
                        candidate
                        for candidate in site_owner.values()
                        if candidate[0] == mount_uuid
                        and candidate[2].casefold() == explicit_site.casefold()
                    ),
                    None,
                )
            if owner is None:
                raise WorkspaceHostError(
                    "release_material_source_unresolved",
                    "MaterialSource 指定的库位不存在",
                    details={
                        "workflow_node_uuid": str(node.get("uuid") or ""),
                        "site": explicit_site,
                    },
                )
            if target_site_resolver is not None:
                param["site"] = target_site_resolver(owner[0], owner[2])
        node["param"] = param
        if not dynamic_task_exclusive:
            selected_materials.add(selected)
    return bound


def _promote_backend_material_passthrough_output(graph: dict[str, Any]) -> None:
    """Expose Local's implicit material passthrough as a Backend node output.

    Backend publications intentionally omit implicit outputs. Composite parent
    graphs, however, use ``resource`` as both data flow and ordering between
    transfer invocations. Binding it to the last leaf resource output keeps
    that ordering while producing a real Backend contract handle.
    """

    workflow = _mapping_or_empty(graph.get("workflow"))
    workflow_meta = _mapping_or_empty(workflow.get("meta_data"))
    unilab = _mapping_or_empty(workflow_meta.get("unilab"))
    output_contract = _mapping_or_empty(unilab.get("output_contract"))
    outputs = output_contract.get("outputs")
    if not isinstance(outputs, list):
        return
    resource_output = next(
        (
            item
            for item in outputs
            if isinstance(item, Mapping)
            and str(item.get("name") or "") == "resource"
            and bool(item.get("implicit"))
        ),
        None,
    )
    if resource_output is None:
        return
    output_bindings = _mapping_or_empty(unilab.get("output_bindings"))
    binding = _mapping_or_empty(output_bindings.get("resource"))
    if str(binding.get("kind") or "") != "workflow_input":
        return

    nodes = _mapping_list(graph.get("nodes"))
    node_by_template: dict[str, list[Mapping[str, Any]]] = {}
    for node in nodes:
        template_uuid = str(node.get("workflow_node_template_uuid") or "")
        if template_uuid:
            node_by_template.setdefault(template_uuid, []).append(node)
    candidates: list[tuple[int, str, str]] = []
    for handle in _mapping_list(graph.get("handle_templates")):
        if (
            str(handle.get("io_type") or "").casefold() != "source"
            or str(handle.get("handle_key") or handle.get("data_key") or "")
            != "resource"
        ):
            continue
        template_uuid = str(handle.get("workflow_node_template_uuid") or "")
        for node in node_by_template.get(template_uuid, []):
            node_unilab = _mapping_or_empty(
                _mapping_or_empty(node.get("meta_data")).get("unilab")
            )
            order = node_unilab.get("authoring_source_order")
            candidates.append(
                (
                    int(order) if isinstance(order, int) else -1,
                    str(node.get("uuid") or ""),
                    str(handle.get("uuid") or ""),
                )
            )
    if not candidates:
        return
    _, node_uuid, handle_uuid = max(candidates)
    promoted_outputs = [deepcopy(dict(item)) for item in _mapping_list(outputs)]
    for item in promoted_outputs:
        if str(item.get("name") or "") == "resource":
            item["implicit"] = False
    promoted_bindings = deepcopy(dict(output_bindings))
    promoted_bindings["resource"] = {
        "kind": "node_output",
        "workflow_node_uuid": node_uuid,
        "source_handle_uuid": handle_uuid,
    }
    promoted_contract = deepcopy(dict(output_contract))
    promoted_contract["outputs"] = promoted_outputs
    promoted_unilab = deepcopy(dict(unilab))
    promoted_unilab["output_contract"] = promoted_contract
    promoted_unilab["output_bindings"] = promoted_bindings
    promoted_meta = deepcopy(dict(workflow_meta))
    promoted_meta["unilab"] = promoted_unilab
    projected_workflow = deepcopy(dict(workflow))
    projected_workflow["meta_data"] = promoted_meta
    graph["workflow"] = projected_workflow


def _publication_scaffold_value(
    schema: Mapping[str, Any],
    *,
    material_identities: Mapping[str, str],
    parameter_name: str,
) -> Any:
    """Build a schema-shaped value used only while freezing a publication.

    Existing Backend import/publication validates leaf-node parameters before
    its published workflow boundary can supply them. Local authoring instead
    keeps those parameters absent and binds them from the workflow input
    contract. The temporary value is therefore removed from the current
    Backend authoring graph immediately after the contract snapshot is frozen.
    """

    if "const" in schema:
        return deepcopy(schema.get("const"))
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return deepcopy(enum[0])
    for union_key in ("oneOf", "anyOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and variants:
            first = variants[0]
            if isinstance(first, Mapping):
                return _publication_scaffold_value(
                    first,
                    material_identities=material_identities,
                    parameter_name=parameter_name,
                )
    if str(schema.get("$slot") or "") == "ResourceSlot":
        target_materials = sorted(
            str(identity)
            for identity in material_identities.values()
            if str(identity)
        )
        if target_materials:
            return {"uuid": target_materials[0]}
        raise WorkspaceHostError(
            "release_apply_failed",
            f"Workflow 输入 {parameter_name} 需要临时 Material，但发布目标为空",
        )
    value_type = str(schema.get("type") or "").strip().casefold()
    if value_type == "string":
        return "unilab-release-input"
    if value_type == "boolean":
        return False
    if value_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            return int(minimum) if value_type == "integer" else minimum
        return 0
    if value_type == "array":
        minimum_items = int(schema.get("minItems") or 0)
        item_schema = _mapping_or_empty(schema.get("items"))
        return [
            _publication_scaffold_value(
                item_schema,
                material_identities=material_identities,
                parameter_name=parameter_name,
            )
            for _ in range(minimum_items)
        ]
    if value_type == "object":
        properties = _mapping_or_empty(schema.get("properties"))
        result: dict[str, Any] = {}
        for key in schema.get("required") or []:
            key_text = str(key)
            result[key_text] = _publication_scaffold_value(
                _mapping_or_empty(properties.get(key_text)),
                material_identities=material_identities,
                parameter_name=f"{parameter_name}.{key_text}",
            )
        return result
    raise WorkspaceHostError(
        "release_apply_failed",
        f"Workflow 输入 {parameter_name} 无法构造发布期临时值",
        details={"schema": deepcopy(dict(schema))},
    )


def _workflow_import_payload(
    graph: Mapping[str, Any],
    *,
    release: WorkspaceRelease,
    workflow_identities: Mapping[str, str],
    workflow_template_identities: Mapping[str, str] | None = None,
    material_identities: Mapping[str, str],
    source_template_names: Mapping[str, str],
    resource_template_identities: Mapping[str, str],
) -> dict[str, Any]:
    """把本地工作流图转换成 Backend 导入载荷。

    参数说明：``graph`` 是已移除本地视觉分组的工作流图；``release`` 提供发布
    身份；各 ``*_identities`` 映射把本地工作流、物料和资源模板身份替换为目标
    Backend 身份；``source_template_names`` 用于按稳定资源名解析动作模板。
    返回值是可直接提交 ``POST /api/v1/workflows/import`` 的纯 JSON 对象。
    当组合依赖、物料身份、Handle 或必填输入无法确定性映射时抛出
    ``WorkspaceHostError``，不得发布部分或含糊的执行图。
    """
    workflow_template_identities = workflow_template_identities or {}
    workflow = _mapping(graph.get("workflow"), "workflow")
    node_templates = {
        str(item.get("uuid") or ""): item
        for item in _mapping_list(graph.get("node_templates"))
    }
    handles = {
        str(item.get("uuid") or ""): item
        for item in _mapping_list(graph.get("handle_templates"))
    }
    handles_by_template: dict[str, list[Mapping[str, Any]]] = {}
    for handle in handles.values():
        handles_by_template.setdefault(
            str(handle.get("workflow_node_template_uuid") or ""), []
        ).append(handle)
    source_uuid = str(workflow.get("uuid") or "")
    preimport_replacements = _identity_replacements(
        workflow_identities,
        material_identities,
        resource_template_identities,
    )
    workflow_meta = _replace_identities(
        deepcopy(_mapping_or_empty(workflow.get("meta_data"))),
        preimport_replacements,
    )
    workflow_meta["unilab_release"] = {
        "release_id": release.release_id,
        "source_workflow_uuid": source_uuid,
        "source_workspace": release.source_workspace,
    }
    workflow_inputs = {
        str(parameter.get("name") or ""): deepcopy(dict(parameter))
        for parameter in _mapping_list(
            _mapping_or_empty(
                _mapping_or_empty(
                    _mapping_or_empty(workflow.get("meta_data")).get("unilab")
                ).get("input_contract")
            ).get("parameters")
        )
        if str(parameter.get("name") or "")
    }
    nodes: list[dict[str, Any]] = []
    for source_node in _mapping_list(graph.get("nodes")):
        source_template_uuid = str(
            source_node.get("workflow_node_template_uuid") or ""
        )
        source_template = node_templates.get(source_template_uuid)
        executor_kind = str(
            _mapping_or_empty(source_template).get("executor_kind")
            or _mapping_or_empty(
                _mapping_or_empty(
                    _mapping_or_empty(source_template).get("meta_data")
                ).get("unilab")
            ).get("executor_kind")
            or ""
        ).strip()
        node = _replace_identities(
            deepcopy(dict(source_node)), preimport_replacements
        )
        # 本地读模型仍把 HostNode.transfer_resource 表示为 ``device_action``，
        # 但 Backend 的原子物料转运合同要求对应节点使用受信任的 ILab seam。
        if (
            str(source_node.get("type") or "").strip().casefold()
            == "device_action"
            and executor_kind == "material_transfer"
        ):
            node["type"] = "ILab"
        node.pop("create_time", None)
        node.pop("update_time", None)
        node.pop("workflow_uuid", None)
        template_uuid = str(node.pop("workflow_node_template_uuid", "") or "")
        node.pop("material_uuid", None)
        param = deepcopy(_mapping_or_empty(node.get("param")))
        input_bindings = _mapping_or_empty(
            _mapping_or_empty(
                _mapping_or_empty(source_node.get("meta_data")).get("unilab")
            ).get("input_bindings")
        )
        for source_handle_uuid, raw_binding in input_bindings.items():
            binding = _mapping_or_empty(raw_binding)
            parameter_name = str(binding.get("parameter") or "")
            parameter = workflow_inputs.get(parameter_name)
            if parameter is None:
                continue
            handle = handles.get(str(source_handle_uuid))
            if handle is None:
                raise WorkspaceHostError(
                    "release_source_invalid",
                    "Workflow 输入绑定引用了未知 Handle",
                    details={
                        "nodeUuid": str(source_node.get("uuid") or ""),
                        "handleUuid": str(source_handle_uuid),
                    },
                )
            data_key = str(
                handle.get("data_key") or handle.get("handle_key") or ""
            )
            if data_key and data_key not in param:
                if "default" in parameter:
                    scaffold = deepcopy(parameter.get("default"))
                elif bool(parameter.get("required")):
                    scaffold = _publication_scaffold_value(
                        _mapping_or_empty(parameter.get("schema")),
                        material_identities=material_identities,
                        parameter_name=parameter_name,
                    )
                else:
                    continue
                param[data_key] = _replace_identities(
                    scaffold,
                    preimport_replacements,
                )
        if str(source_node.get("type") or "").strip().casefold() != "workflow":
            for handle in handles_by_template.get(source_template_uuid, []):
                if (
                    str(handle.get("io_type") or "").strip().casefold()
                    != "target"
                    or not bool(handle.get("required"))
                ):
                    continue
                data_key = str(
                    handle.get("data_key") or handle.get("handle_key") or ""
                )
                if not data_key or data_key in param:
                    continue
                handle_unilab = _mapping_or_empty(
                    _mapping_or_empty(handle.get("meta_data")).get("unilab")
                )
                if str(handle.get("type") or "") == "ResourceSlot":
                    value_schema: Mapping[str, Any] = {"$slot": "ResourceSlot"}
                else:
                    value_schema = _mapping_or_empty(
                        handle_unilab.get("value_schema")
                    )
                param[data_key] = _replace_identities(
                    _publication_scaffold_value(
                        value_schema,
                        material_identities=material_identities,
                        parameter_name=data_key,
                    ),
                    preimport_replacements,
                )
        if str(source_node.get("type") or "").strip().casefold() == "workflow":
            source_unilab = _mapping_or_empty(
                _mapping_or_empty(source_node.get("meta_data")).get("unilab")
            )
            composite = _mapping_or_empty(source_unilab.get("composite"))
            compatibility = _mapping_or_empty(
                composite.get("contract_compatibility")
            )
            composite_parameters = _mapping_list(
                compatibility.get("parameters")
            )
            for composite_input in composite_parameters:
                parameter_name = str(composite_input.get("name") or "")
                if not parameter_name or parameter_name in param:
                    continue
                if "default" in composite_input:
                    scaffold = deepcopy(composite_input.get("default"))
                elif bool(composite_input.get("required")):
                    scaffold = _publication_scaffold_value(
                        _mapping_or_empty(composite_input.get("schema")),
                        material_identities=material_identities,
                        parameter_name=parameter_name,
                    )
                else:
                    continue
                param[parameter_name] = _replace_identities(
                    scaffold,
                    preimport_replacements,
                )
        node["param"] = param
        template = node_templates.get(template_uuid)
        if template is not None:
            template_name = str(template.get("name") or "")
            node_kind = str(source_node.get("type") or "").strip().casefold()
            if node_kind in {
                "compute",
                "condition",
                "script",
                "py_script",
                "group",
                "tool_call",
            }:
                # These are native standalone Backend nodes. Supplying the
                # Local host_node catalog name would incorrectly resolve them
                # as device actions.
                template_name = ""
            elif template_name.startswith("workflow:"):
                local_dependency = template_name.removeprefix("workflow:")
                target_dependency = workflow_identities.get(local_dependency)
                target_template_uuid = workflow_template_identities.get(
                    local_dependency
                )
                if not target_dependency or not target_template_uuid:
                    raise WorkspaceHostError(
                        "release_apply_failed",
                        f"组合工作流依赖尚未发布：{local_dependency}",
                    )
                node["workflow_node_template_uuid"] = target_template_uuid
                template_name = ""
            if template_name:
                node["template_name"] = template_name
                resource_template_uuid = str(
                    template.get("resource_template_uuid") or ""
                )
                node["resource_name"] = source_template_names.get(
                    resource_template_uuid, ""
                )
        local_material_uuid = str(source_node.get("material_uuid") or "")
        if local_material_uuid:
            target_material_uuid = material_identities.get(local_material_uuid)
            if (
                target_material_uuid is None
                and local_material_uuid in set(material_identities.values())
            ):
                target_material_uuid = local_material_uuid
            if not target_material_uuid:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    f"Workflow 节点引用未迁移 Material：{local_material_uuid}",
                )
            node["material_uuid"] = target_material_uuid
        meta_data = _replace_identities(
            deepcopy(_mapping_or_empty(source_node.get("meta_data"))),
            preimport_replacements,
        )
        if str(source_node.get("type") or "").strip().casefold() == "workflow":
            template = node_templates.get(template_uuid)
            template_unilab = _mapping_or_empty(
                _mapping_or_empty(
                    _mapping_or_empty(template).get("meta_data")
                ).get("unilab")
            )
            workflow_source = _mapping_or_empty(
                template_unilab.get("workflow_source")
            )
            if workflow_source:
                node_unilab = dict(_mapping_or_empty(meta_data.get("unilab")))
                node_unilab["workflow_source"] = deepcopy(dict(workflow_source))
                meta_data["unilab"] = node_unilab
        meta_data["unilab_release"] = {
            "release_id": release.release_id,
            "source_node_uuid": str(source_node.get("uuid") or ""),
        }
        node["meta_data"] = meta_data
        nodes.append(node)
    edges: list[dict[str, Any]] = []
    for source_edge in _mapping_list(graph.get("edges")):
        edge = {
            "source_node_uuid": source_edge.get("source_node_uuid"),
            "target_node_uuid": source_edge.get("target_node_uuid"),
            "source_handle_key": _handle_key(handles, source_edge.get("source_handle_uuid")),
            "target_handle_key": _handle_key(handles, source_edge.get("target_handle_uuid")),
            "description": source_edge.get("description"),
            "meta_data": _replace_identities(
                deepcopy(_mapping_or_empty(source_edge.get("meta_data"))),
                preimport_replacements,
            ),
        }
        edges.append(edge)
    return {
        "workflow_name": workflow.get("name"),
        "tags": deepcopy(workflow.get("tags") or []),
        "description": workflow.get("description"),
        "meta_data": workflow_meta,
        "nodes": nodes,
        "edges": edges,
        "inventory_requirements": _replace_identities(
            deepcopy(graph.get("inventory_requirements") or []),
            preimport_replacements,
        ),
    }


def _remap_imported_workflow_graph(
    source_graph: Mapping[str, Any],
    imported_graph: Mapping[str, Any],
    *,
    release: WorkspaceRelease,
    workflow_identities: Mapping[str, str],
    material_identities: Mapping[str, str],
    resource_template_identities: Mapping[str, str],
    preserve_imported_param_defaults: bool = False,
) -> dict[str, Any]:
    """Restore graph semantics after Backend regenerated node/template identities."""

    source_workflow = _mapping(source_graph.get("workflow"), "source workflow")
    source_nodes = {
        str(item.get("uuid") or ""): item
        for item in _mapping_list(source_graph.get("nodes"))
    }
    target_nodes = _mapping_list(imported_graph.get("nodes"))
    node_identities: dict[str, str] = {}
    node_template_identities: dict[str, str] = {}
    target_by_source: dict[str, Mapping[str, Any]] = {}
    for target_node in target_nodes:
        release_meta = _mapping_or_empty(
            _mapping_or_empty(target_node.get("meta_data")).get("unilab_release")
        )
        source_node_uuid = str(release_meta.get("source_node_uuid") or "")
        target_node_uuid = str(target_node.get("uuid") or "")
        if not source_node_uuid or source_node_uuid not in source_nodes:
            raise WorkspaceHostError(
                "release_apply_failed",
                "Backend 导入节点缺少 Local 来源身份",
                details={"targetNodeUuid": target_node_uuid},
            )
        if source_node_uuid in target_by_source:
            raise WorkspaceHostError(
                "release_apply_failed",
                "Backend 导入节点的 Local 来源身份重复",
                details={"sourceNodeUuid": source_node_uuid},
            )
        target_by_source[source_node_uuid] = target_node
        node_identities[source_node_uuid] = target_node_uuid
        source_template_uuid = str(
            source_nodes[source_node_uuid].get("workflow_node_template_uuid") or ""
        )
        target_template_uuid = str(
            target_node.get("workflow_node_template_uuid") or ""
        )
        if source_template_uuid and target_template_uuid:
            existing = node_template_identities.get(source_template_uuid)
            if existing and existing != target_template_uuid:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    "Backend 导入节点模板身份映射不唯一",
                    details={"sourceTemplateUuid": source_template_uuid},
                )
            node_template_identities[source_template_uuid] = target_template_uuid
    missing_nodes = sorted(set(source_nodes) - set(target_by_source))
    if missing_nodes:
        raise WorkspaceHostError(
            "release_apply_failed",
            "Backend 导入结果缺少 Local 节点",
            details={"sourceNodeUuids": missing_nodes},
        )

    target_handles: dict[tuple[str, str, str], str] = {}
    for handle in _mapping_list(imported_graph.get("handle_templates")):
        identity = (
            str(handle.get("workflow_node_template_uuid") or ""),
            str(handle.get("handle_key") or ""),
            str(handle.get("io_type") or ""),
        )
        handle_uuid = str(handle.get("uuid") or "")
        existing = target_handles.get(identity)
        if existing and existing != handle_uuid:
            raise WorkspaceHostError(
                "release_apply_failed",
                "Backend Handle 身份不唯一",
                details={"identity": list(identity)},
            )
        target_handles[identity] = handle_uuid
    handle_identities: dict[str, str] = {}
    for handle in _mapping_list(source_graph.get("handle_templates")):
        source_handle_uuid = str(handle.get("uuid") or "")
        source_template_uuid = str(
            handle.get("workflow_node_template_uuid") or ""
        )
        target_template_uuid = node_template_identities.get(source_template_uuid)
        if not target_template_uuid:
            continue
        identity = (
            target_template_uuid,
            str(handle.get("handle_key") or ""),
            str(handle.get("io_type") or ""),
        )
        target_handle_uuid = target_handles.get(identity)
        if target_handle_uuid:
            handle_identities[source_handle_uuid] = target_handle_uuid

    replacements = _identity_replacements(
        workflow_identities,
        material_identities,
        resource_template_identities,
        node_identities,
        handle_identities,
    )
    semantic_values: list[object] = [
        _mapping_or_empty(source_workflow.get("meta_data")),
    ]
    for source_node in source_nodes.values():
        if str(source_node.get("type") or "").strip().casefold() != "workflow":
            semantic_values.append(
                _mapping_or_empty(source_node.get("meta_data"))
            )
        semantic_values.extend((source_node.get("param"), source_node.get("pose")))
    semantic_values.extend(
        _mapping_or_empty(edge.get("meta_data"))
        for edge in _mapping_list(source_graph.get("edges"))
    )
    referenced_handles = set().union(
        *(
            _referenced_identities(value, set(
                str(handle.get("uuid") or "")
                for handle in _mapping_list(source_graph.get("handle_templates"))
            ))
            for value in semantic_values
        )
    )
    missing_handles = sorted(referenced_handles - set(handle_identities))
    if missing_handles:
        raise WorkspaceHostError(
            "release_apply_failed",
            "Backend 导入结果无法映射工作流 Handle 引用",
            details={"sourceHandleUuids": missing_handles},
        )

    remapped = deepcopy(dict(imported_graph))
    target_workflow = deepcopy(
        dict(_mapping(remapped.get("workflow"), "imported workflow"))
    )
    source_workflow_meta = deepcopy(
        dict(_mapping_or_empty(source_workflow.get("meta_data")))
    )
    source_workflow_meta.pop("unilab_release", None)
    target_workflow["meta_data"] = _replace_identities(
        source_workflow_meta, replacements
    )
    target_workflow["meta_data"]["unilab_release"] = {
        "release_id": release.release_id,
        "source_workflow_uuid": str(source_workflow.get("uuid") or ""),
        "source_workspace": release.source_workspace,
    }
    remapped["workflow"] = target_workflow

    remapped_nodes: list[dict[str, Any]] = []
    for target_node in target_nodes:
        release_meta = _mapping_or_empty(
            _mapping_or_empty(target_node.get("meta_data")).get("unilab_release")
        )
        source_node_uuid = str(release_meta.get("source_node_uuid") or "")
        source_node = source_nodes[source_node_uuid]
        remapped_node = _replace_identities(deepcopy(dict(target_node)), replacements)
        for field in ("param", "pose", "execution_policy"):
            if field in source_node:
                remapped_node[field] = _replace_identities(
                    deepcopy(source_node.get(field)), replacements
                )
        if preserve_imported_param_defaults:
            source_param = dict(_mapping_or_empty(remapped_node.get("param")))
            for key, value in _mapping_or_empty(target_node.get("param")).items():
                source_param.setdefault(str(key), deepcopy(value))
            remapped_node["param"] = source_param
        if str(source_node.get("type") or "").strip().casefold() == "workflow":
            # The Backend import endpoint regenerates node UUIDs but preserves
            # composite metadata verbatim.  Rewrite the invocation's private
            # boundary mappings to the imported child identities before the
            # graph is published; otherwise execution planning sees Local
            # child UUIDs crossing the Backend invocation boundary.
            remapped_node["meta_data"] = _replace_identities(
                deepcopy(dict(_mapping_or_empty(target_node.get("meta_data")))),
                replacements,
            )
        else:
            source_meta = deepcopy(
                dict(_mapping_or_empty(source_node.get("meta_data")))
            )
            source_meta.pop("unilab_release", None)
            remapped_node["meta_data"] = _replace_identities(
                source_meta, replacements
            )
        remapped_node["meta_data"]["unilab_release"] = {
            "release_id": release.release_id,
            "source_node_uuid": source_node_uuid,
        }
        remapped_nodes.append(remapped_node)
    remapped["nodes"] = remapped_nodes
    remapped["edges"] = _replace_identities(
        deepcopy(_mapping_list(imported_graph.get("edges"))), replacements
    )
    if "inventory_requirements" in imported_graph:
        remapped["inventory_requirements"] = _replace_identities(
            deepcopy(imported_graph.get("inventory_requirements") or []), replacements
        )
    return remapped


def _identity_replacements(*mappings: Mapping[str, str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for mapping in mappings:
        for source, target in mapping.items():
            source_identity = str(source or "")
            target_identity = str(target or "")
            if not source_identity or not target_identity:
                continue
            existing = replacements.get(source_identity)
            if existing and existing != target_identity:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    "WorkspaceRelease 身份映射冲突",
                    details={"sourceIdentity": source_identity},
                )
            replacements[source_identity] = target_identity
    return replacements


def _replace_identities(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            target_key = replacements.get(str(key), str(key))
            if target_key in result:
                raise WorkspaceHostError(
                    "release_apply_failed",
                    "WorkspaceRelease 元数据身份替换后键冲突",
                    details={"key": target_key},
                )
            result[target_key] = _replace_identities(item, replacements)
        return result
    if isinstance(value, list):
        return [_replace_identities(item, replacements) for item in value]
    if isinstance(value, tuple):
        return [_replace_identities(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def _referenced_identities(value: Any, candidates: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in candidates:
                found.add(str(key))
            found.update(_referenced_identities(item, candidates))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_referenced_identities(item, candidates))
    elif isinstance(value, str) and value in candidates:
        found.add(value)
    return found


def _restore_public_authoring_params(
    current_graph: Mapping[str, Any],
    authoring_graph: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove fake publication scaffolds while retaining declared defaults.

    Publishing a composite freezes its invocation contract metadata and all
    private descendants.  The public invocation parameter map remains
    author-editable, so restore only parameters on top-level nodes while using
    the post-publication Backend graph as the authority for every immutable
    field. Backend tasks currently execute the materialized graph directly,
    so workflow input defaults bound to node handles must remain in that graph;
    only schema-shaped placeholders for inputs without defaults are removed.
    """

    workflow_unilab = _mapping_or_empty(
        _mapping_or_empty(
            _mapping_or_empty(authoring_graph.get("workflow")).get("meta_data")
        ).get("unilab")
    )
    declared_defaults = {
        str(parameter.get("name") or ""): deepcopy(parameter.get("default"))
        for parameter in _mapping_list(
            _mapping_or_empty(workflow_unilab.get("input_contract")).get(
                "parameters"
            )
        )
        if str(parameter.get("name") or "") and "default" in parameter
    }
    current_handles = {
        str(handle.get("uuid") or ""): str(
            handle.get("data_key") or handle.get("handle_key") or ""
        )
        for handle in _mapping_list(current_graph.get("handle_templates"))
    }

    authoring_by_source: dict[str, Mapping[str, Any]] = {}
    for node in _mapping_list(authoring_graph.get("nodes")):
        release_meta = _mapping_or_empty(
            _mapping_or_empty(node.get("meta_data")).get("unilab_release")
        )
        source_uuid = str(release_meta.get("source_node_uuid") or "")
        if source_uuid:
            authoring_by_source[source_uuid] = node

    restored = deepcopy(dict(current_graph))
    nodes: list[dict[str, Any]] = []
    for current_node in _mapping_list(current_graph.get("nodes")):
        node = deepcopy(dict(current_node))
        if node.get("parent_uuid") is None:
            release_meta = _mapping_or_empty(
                _mapping_or_empty(node.get("meta_data")).get("unilab_release")
            )
            source_uuid = str(release_meta.get("source_node_uuid") or "")
            authoring_node = authoring_by_source.get(source_uuid)
            if authoring_node is not None:
                params = deepcopy(dict(_mapping_or_empty(authoring_node.get("param"))))
                current_params = _mapping_or_empty(current_node.get("param"))
                input_bindings = _mapping_or_empty(
                    _mapping_or_empty(
                        _mapping_or_empty(current_node.get("meta_data")).get(
                            "unilab"
                        )
                    ).get("input_bindings")
                )
                for handle_uuid, raw_binding in input_bindings.items():
                    parameter_name = str(
                        _mapping_or_empty(raw_binding).get("parameter") or ""
                    )
                    if parameter_name not in declared_defaults:
                        continue
                    data_key = current_handles.get(str(handle_uuid), "")
                    if not data_key or data_key in params:
                        continue
                    params[data_key] = deepcopy(
                        current_params.get(data_key, declared_defaults[parameter_name])
                    )
                node["param"] = params
        nodes.append(node)
    restored["nodes"] = nodes
    return restored


def _repair_public_node_metadata(
    current_graph: Mapping[str, Any],
    remapped_graph: Mapping[str, Any],
) -> dict[str, Any]:
    """Remap Local handle identities only on editable public atomic nodes.

    Legacy import resolves a node template and creates target handles after it
    has persisted node metadata, so metadata keys such as ``input_bindings``
    and composite private-boundary mappings still contain Local identities.
    Private descendants remain immutable; replace metadata only on top-level
    public nodes, including the composite invocation boundary itself.
    """

    remapped_by_source: dict[str, Mapping[str, Any]] = {}
    for node in _mapping_list(remapped_graph.get("nodes")):
        release_meta = _mapping_or_empty(
            _mapping_or_empty(node.get("meta_data")).get("unilab_release")
        )
        source_uuid = str(release_meta.get("source_node_uuid") or "")
        if source_uuid:
            remapped_by_source[source_uuid] = node

    repaired = deepcopy(dict(current_graph))
    nodes: list[dict[str, Any]] = []
    for current_node in _mapping_list(current_graph.get("nodes")):
        node = deepcopy(dict(current_node))
        if node.get("parent_uuid") is None:
            release_meta = _mapping_or_empty(
                _mapping_or_empty(node.get("meta_data")).get("unilab_release")
            )
            source_uuid = str(release_meta.get("source_node_uuid") or "")
            remapped_node = remapped_by_source.get(source_uuid)
            if remapped_node is not None:
                node["meta_data"] = deepcopy(
                    _mapping_or_empty(remapped_node.get("meta_data"))
                )
        nodes.append(node)
    repaired["nodes"] = nodes
    return repaired


def _workflow_node_patches(
    current_graph: Mapping[str, Any],
    desired_graph: Mapping[str, Any],
    *,
    fields: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    """Return minimal node PATCH requests for explicitly selected fields."""

    desired_nodes = {
        str(node.get("uuid") or ""): node
        for node in _mapping_list(desired_graph.get("nodes"))
    }
    patches: list[tuple[str, dict[str, Any]]] = []
    for current_node in _mapping_list(current_graph.get("nodes")):
        node_uuid = str(current_node.get("uuid") or "")
        desired_node = desired_nodes.get(node_uuid)
        if not node_uuid or desired_node is None:
            continue
        patch: dict[str, Any] = {}
        for field in fields:
            current_value = current_node.get(field)
            desired_value = desired_node.get(field)
            if current_value != desired_value:
                patch[field] = deepcopy(desired_value)
        if patch:
            patches.append((node_uuid, patch))
    return patches


def _normalized_workflow(graph: Mapping[str, Any], *, source: bool) -> dict[str, Any]:
    workflow = _mapping(graph.get("workflow"), "workflow")
    handles = {
        str(item.get("uuid") or ""): item
        for item in _mapping_list(graph.get("handle_templates"))
    }
    id_map: dict[str, str] = {}
    for node in _mapping_list(graph.get("nodes")):
        node_uuid = str(node.get("uuid") or "")
        if source:
            id_map[node_uuid] = node_uuid
        else:
            release_meta = _mapping_or_empty(
                _mapping_or_empty(node.get("meta_data")).get("unilab_release")
            )
            id_map[node_uuid] = str(release_meta.get("source_node_uuid") or "")
    nodes = sorted(
        (
            id_map.get(str(node.get("uuid") or ""), ""),
            str(node.get("name") or ""),
            _normalized_node_kind(node.get("type")),
            bool(node.get("disabled")),
            id_map.get(str(node.get("parent_uuid") or ""), ""),
        )
        for node in _mapping_list(graph.get("nodes"))
    )
    edges = sorted(
        (
            id_map.get(str(edge.get("source_node_uuid") or ""), ""),
            id_map.get(str(edge.get("target_node_uuid") or ""), ""),
            _handle_key(handles, edge.get("source_handle_uuid")),
            _handle_key(handles, edge.get("target_handle_uuid")),
        )
        for edge in _mapping_list(graph.get("edges"))
    )
    return {"name": str(workflow.get("name") or ""), "nodes": nodes, "edges": edges}


def _normalized_node_kind(value: object) -> str:
    kind = str(value or "").strip()
    if kind.casefold() == "ilab":
        return "device_action"
    return kind.casefold()


def _handle_key(handles: Mapping[str, Mapping[str, Any]], value: object) -> str:
    handle = handles.get(str(value or ""))
    return str(handle.get("handle_key") or "") if handle else ""


def _material_nodes(graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = graph.get("nodes")
    if not isinstance(raw, list):
        raise WorkspaceHostError("release_source_invalid", "MaterialGraph.nodes 必须是数组")
    return [item for item in raw if isinstance(item, Mapping)]


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("release_source_invalid", f"{field} 必须是对象")
    return value


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _required_identity(value: Mapping[str, Any], kind: str) -> str:
    identity = str(value.get("uuid") or "")
    if not identity:
        raise WorkspaceHostError("release_apply_failed", f"Backend {kind} 缺少 UUID")
    return identity


def _required_field(value: Mapping[str, Any], field: str, kind: str) -> str:
    result = str(value.get(field) or "")
    if not result:
        raise WorkspaceHostError(
            "release_apply_failed", f"Backend {kind} 缺少 {field}"
        )
    return result


def _release_response(response: Any, operation: str) -> dict[str, Any]:
    try:
        return _response_data(response, operation)
    except WorkspaceHostError as error:
        raise WorkspaceHostError(
            "release_transport_failed",
            str(error),
            details=error.details,
        ) from error


__all__ = [
    "DeploymentPlan",
    "DeploymentResult",
    "ExistingBackendDeploymentTarget",
    "ExistingBackendReleaseVerifier",
    "LocalBackendReleaseBuilder",
    "VerificationReport",
    "WorkspaceRelease",
    "WorkspaceReleasePublisher",
    "create_existing_backend_publisher",
]
