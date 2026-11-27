"""把 Edge Registry 作为一个完整模板图同步到正式后端。"""

from __future__ import annotations

import gzip
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.resources.site_definition import (
    INITIAL_MATERIAL_TEMPLATE_HINT,
    SiteDefinition,
)
from unilabos.utils.tracing import inject_trace_context, span

DEVELOPER_TOKEN_ENV = "UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN"
logger = logging.getLogger(__name__)


class TemplateSyncError(RuntimeError):
    """模板收集或后端事务同步失败。"""


@dataclass(frozen=True)
class TemplateSyncReport:
    """一次完整同步的稳定结果，供初始化 Job 和测试流程消费。"""

    device_count: int
    resource_count: int
    template_uuids: dict[str, str]


class TemplateSyncWorkspaceRuntime(Protocol):
    """模板同步消费的最小工作区候选代 Interface。"""

    material_shapes_by_template: Mapping[str, Mapping[str, Any]]

    def publish(self, registry: Any) -> None:
        """把同代包模板原子发布到活动注册表（Registry）。

        参数：``registry`` 是本次模板同步唯一活动注册表。返回：无。
        异常：候选冲突或发布失败时传播原异常，由同步命令统一映射。
        """


class TemplateSynchronizer:
    """隐藏 Registry 遍历、协议映射、压缩和 HTTP 提交细节。"""

    def __init__(
        self,
        backend_address: str,
        developer_token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        """创建模板同步器并固定一次同步所需的 Backend 连接参数。

        参数说明：``backend_address`` 是 Backend 服务地址；``developer_token``
        是初始化任务使用的开发者凭据；``session`` 是可替换的 HTTP 适配器；
        ``timeout`` 是单次同步请求的秒级超时。无返回值；凭据缺失时抛出
        ``TemplateSyncError``，避免发送无权写入的模板事务。
        """

        token = str(developer_token or "").strip()
        if not token:
            raise TemplateSyncError("developer token is required")
        self.backend_api = _api_base(backend_address)
        self.developer_token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync(
        self,
        registry: Any,
        *,
        deployment_graph: Mapping[str, Any] | None = None,
        material_shapes_by_template: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TemplateSyncReport:
        """通过一个不可变 Registry 快照执行确定性的 Backend 模板同步。

        参数说明：``registry`` 可以是已编译的 ``RegistryTemplateSnapshot``，也可
        以兼容方式传入 Registry；后者只遍历一次并立即冻结；
        ``deployment_graph`` 提供实例固定库位定义，
        ``material_shapes_by_template`` 按模板业务身份提供同代编译后的 2.5D
        外形。返回同步报告；库位、外形结构或目标模板无效时抛出
        ``TemplateSyncError``，且不会发送部分模板事务。
        """

        try:
            # ``registry_snapshot`` 是本次同步唯一的不可变设备注册表定义代际；
            # 后续设备模板和资源模板都从它分离复制，禁止再次遍历活 Registry。
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise TemplateSyncError(str(error)) from error
        # ``devices`` 与 ``resources`` 是发送给 Backend 的相同快照副本，
        # ``definitions`` 保留完整事务成员集合，供身份回执做全量核对。
        devices = registry_snapshot.detached_devices()
        resources = registry_snapshot.detached_resources()
        definitions = [*devices, *resources]
        if not definitions:
            raise TemplateSyncError("Edge Registry does not contain any templates")
        _embed_material_shapes(
            definitions,
            material_shapes_by_template or {},
        )
        if deployment_graph is not None or _has_deferred_site_template_identity(
            definitions
        ):
            # 首次事务先取得 Backend 稳定模板身份；库位准入必须引用 UUID，不能把
            # 部署图里的资源类名伪装成 Material.type。
            template_uuids = self._upload_definitions(
                definitions,
                device_count=len(devices),
                resource_count=len(resources),
            )
            if deployment_graph is not None:
                _project_graph_available_sites(definitions, deployment_graph)
            _resolve_available_site_template_identities(definitions, template_uuids)
        template_uuids = self._upload_definitions(
            definitions,
            device_count=len(devices),
            resource_count=len(resources),
        )

        return TemplateSyncReport(
            device_count=len(devices),
            resource_count=len(resources),
            template_uuids=template_uuids,
        )

    def _upload_definitions(
        self,
        definitions: list[dict[str, Any]],
        *,
        device_count: int,
        resource_count: int,
    ) -> dict[str, str]:
        """原子上传一代模板定义，并严格核对 Backend 返回的稳定身份全集。"""

        payload = {"resources": definitions}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.developer_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        url = f"{self.backend_api}/resource-templates"
        target = urlsplit(url)
        with span(
            "edge.http.template.sync",
            kind="client",
            attributes={
                "http.request.method": "POST",
                "http.route": "/api/v1/resource-templates",
                "server.address": target.hostname or "",
                "template.device.count": device_count,
                "template.resource.count": resource_count,
            },
        ) as request_span:
            inject_trace_context(headers)
            response = self.session.post(
                url,
                data=gzip.compress(encoded),
                headers=headers,
                timeout=self.timeout,
            )
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001,S110 - tracing must remain fail-open
                pass

        result = _decode_sync_response(response)
        template_uuids = {
            str(identity["name"]): str(identity["uuid"])
            for identity in result.get("templates", [])
            if isinstance(identity, Mapping)
            and identity.get("name")
            and identity.get("uuid")
        }
        expected_names = {definition["id"] for definition in definitions}
        if set(template_uuids) != expected_names:
            missing = sorted(expected_names - set(template_uuids))
            raise TemplateSyncError(
                f"backend response is missing template identities: {missing}"
            )
        return template_uuids


def _embed_material_shapes(
    definitions: list[dict[str, Any]],
    material_shapes_by_template: Mapping[str, Mapping[str, Any]],
) -> None:
    """把完整 2.5D 外形嵌入本次上传模板的 ``model.shape``。

    参数说明：``definitions`` 是从不可变注册表（Registry）快照分离出的本次
    上传模板；``material_shapes_by_template`` 以模板业务身份关联包目录
    （PackageCatalog）同代编译结果。返回：无，成功时仅修改分离模板副本；外形
    不是严格 JSON、协议版本错误、图元为空或目标模板不存在时抛出
    ``TemplateSyncError``，禁止把损坏绑定或部分候选发送给 Backend。
    """

    if not isinstance(material_shapes_by_template, Mapping):
        raise TemplateSyncError("material shape bindings must be an object")
    # ``definitions_by_name`` 是本次完整模板事务唯一可写副本索引；外形只能按
    # 稳定模板业务身份精确绑定，禁止按 category 或外形名称猜测所有者。
    definitions_by_name = {
        str(definition.get("id") or ""): definition for definition in definitions
    }
    for template_name, raw_shape in material_shapes_by_template.items():
        if not isinstance(template_name, str) or not template_name.strip():
            raise TemplateSyncError("material shape template identity is invalid")
        definition = definitions_by_name.get(template_name)
        if definition is None:
            raise TemplateSyncError(
                f"material shape target template is missing: {template_name}"
            )
        if not isinstance(raw_shape, Mapping):
            raise TemplateSyncError(
                f"material shape must be an object: {template_name}"
            )
        try:
            # ``shape`` 通过严格 JSON 往返与运行时代容器隔离；设备包原有
            # ``entry`` 由下方注册表绑定合并保留，不由编译结果重复构造。
            shape = json.loads(
                json.dumps(
                    dict(raw_shape),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as error:
            raise TemplateSyncError(
                f"material shape is not strict JSON: {template_name}"
            ) from error
        if shape.get("schema_version") != "unilab.shape/v1":
            raise TemplateSyncError(
                f"material shape schema version is invalid: {template_name}"
            )
        if not isinstance(shape.get("parts"), list) or not shape["parts"]:
            raise TemplateSyncError(
                f"material shape parts are required: {template_name}"
            )
        # ``model`` 保留 Xacro/URDF 等模板模型字段；``shape_binding`` 继续保留
        # 设备包既有的 format/entry 等兼容绑定，完整编译结果只补齐云端可直接
        # 使用的公共外形字段，避免新增 2.5D 同步破坏旧消费者。
        model = definition.get("model")
        if not isinstance(model, dict):
            model = {}
        else:
            model = dict(model)
        raw_shape_binding = model.get("shape")
        shape_binding = (
            dict(raw_shape_binding)
            if isinstance(raw_shape_binding, Mapping)
            else {}
        )
        shape_binding.update(shape)
        model["shape"] = shape_binding
        definition["model"] = model


def sync_registry_from_environment(
    registry: Any,
    backend_address: str,
    *,
    session: requests.Session | None = None,
) -> TemplateSyncReport:
    """使用初始化 Job 注入的开发者身份同步一个注册表（Registry）。

    参数：``registry`` 是待同步的活动注册表；``backend_address`` 是 Backend
    地址；``session`` 是可选 HTTP Adapter。返回模板同步报告；环境缺少凭据、
    模板合同或 HTTP 事务失败时抛出 ``TemplateSyncError``。
    """

    developer_token = os.environ.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def run_template_sync_command(
    arguments: Mapping[str, Any],
    *,
    backend_address: str,
    environment: Mapping[str, str] | None = None,
    registry_builder: Callable[..., Any] | None = None,
    session: requests.Session | None = None,
    workspace_runtime: TemplateSyncWorkspaceRuntime | None = None,
) -> TemplateSyncReport:
    """执行独立模板初始化命令，不进入设备图和驱动启动流程。

    参数说明：``arguments`` 是公共命令行参数；``backend_address`` 是 Backend
    地址；``environment`` 提供开发者凭据；``registry_builder`` 与 ``session`` 是
    可测试 Adapter；``workspace_runtime`` 是可选的同代工作区注册表运行时，负责
    发布包模板并提供 2.5D 外形精确绑定。返回模板同步报告；工作区发布、外形
    合同或 HTTP 事务失败时抛出 ``TemplateSyncError``。
    """

    if registry_builder is None:
        from unilabos.registry.registry import build_registry

        registry_builder = build_registry
    registry = registry_builder(
        registry_paths=arguments.get("registry_path"),
        devices_dirs=arguments.get("devices"),
        # 这里必须保持纯静态收集；旧开关会实例化 PyLabRobot 器材来补配置，
        # 会把初始化 Job 意外耦合到驱动和硬件环境。
        upload_registry=False,
        complete_registry=bool(arguments.get("complete_registry", False)),
        external_only=bool(arguments.get("external_devices_only", False)),
    )
    # ``material_shapes_by_template`` 默认保持遗留 Registry 模板同步行为；启用
    # Workspace 时则必须先把已经编译的完整包代发布到同一个活动 Registry。
    material_shapes_by_template: Mapping[str, Mapping[str, Any]] = {}
    if workspace_runtime is not None:
        publish = getattr(workspace_runtime, "publish", None)
        raw_shape_bindings = getattr(
            workspace_runtime,
            "material_shapes_by_template",
            None,
        )
        if not callable(publish) or not isinstance(raw_shape_bindings, Mapping):
            raise TemplateSyncError("workspace runtime shape generation is invalid")
        try:
            publish(registry)
        except Exception as error:
            raise TemplateSyncError(
                f"workspace registry publication failed: {error}"
            ) from error
        material_shapes_by_template = raw_shape_bindings
    token_source = environment if environment is not None else os.environ
    developer_token = token_source.get(DEVELOPER_TOKEN_ENV, "")
    deployment_graph = _read_deployment_graph(arguments.get("graph"))
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(
        registry,
        deployment_graph=deployment_graph,
        material_shapes_by_template=material_shapes_by_template,
    )


def _read_deployment_graph(graph_path: Any) -> Mapping[str, Any] | None:
    """读取可选部署图，供模板同步提取固定库位（Site）定义。"""

    path = str(graph_path or "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as graph_file:
            graph = json.load(graph_file)
    except (OSError, ValueError) as error:
        raise TemplateSyncError(f"cannot read device graph {path}: {error}") from error
    if not isinstance(graph, Mapping):
        raise TemplateSyncError("device graph root must be an object")
    return graph


def _project_graph_available_sites(
    definitions: list[dict[str, Any]],
    graph: Mapping[str, Any],
) -> None:
    """把部署图的固定库位投影到对应资源模板，拒绝同模板多份冲突定义。"""

    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TemplateSyncError("device graph nodes are required")
    definitions_by_name = {
        str(definition.get("id") or ""): definition for definition in definitions
    }
    projected_by_template: dict[str, list[dict[str, Any]]] = {}
    instance_specific_templates: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            raise TemplateSyncError("device graph node must be an object")
        config = raw_node.get("config")
        raw_sites = config.get("sites") if isinstance(config, Mapping) else None
        if raw_sites is None:
            continue
        if not isinstance(raw_sites, list):
            raise TemplateSyncError("device graph config.sites must be an array")
        class_name = str(raw_node.get("class") or "").strip()
        definition = definitions_by_name.get(class_name) or definitions_by_name.get(
            class_name.rsplit(".", 1)[-1]
        )
        if definition is None:
            raise TemplateSyncError(
                f"resource template {class_name} has not been synchronized"
            )
        template_name = str(definition["id"])
        if template_name in instance_specific_templates:
            continue
        sites = [
            _normalize_available_site(raw_site, index)
            for index, raw_site in enumerate(raw_sites)
        ]
        previous = projected_by_template.get(template_name)
        if previous is not None and previous != sites:
            projected_by_template.pop(template_name)
            instance_specific_templates.add(template_name)
            logger.warning(
                "资源模板 %s 的部署图库位随实例变化，不能投影为 available_sites",
                template_name,
            )
            continue
        projected_by_template[template_name] = sites
    for template_name, sites in projected_by_template.items():
        definitions_by_name[template_name]["available_sites"] = sites


def _normalize_available_site(raw_site: Any, index: int) -> dict[str, Any]:
    """规范化一项部署图库位定义，并剥离 occupied_by 等实例状态。"""

    if not isinstance(raw_site, Mapping):
        raise TemplateSyncError(f"device graph site {index} must be an object")
    label = str(raw_site.get("label") or raw_site.get("name") or "").strip()
    if not label:
        raise TemplateSyncError(f"device graph site {index} requires label")
    position = raw_site.get("position")
    size = raw_site.get("size")
    rotation = raw_site.get("rotation")
    position = position if isinstance(position, Mapping) else {}
    size = size if isinstance(size, Mapping) else {}
    rotation = rotation if isinstance(rotation, Mapping) else {}
    content_types = raw_site.get("content_type")
    if not isinstance(content_types, list) or not all(
        isinstance(content_type, str) and content_type.strip()
        for content_type in content_types
    ):
        raise TemplateSyncError(
            f"device graph site {label} content_type must be a string array"
        )
    known_fields = {
        "label",
        "name",
        "position",
        "size",
        "rotation",
        "content_type",
        "initial_material_template_uuid",
        "visible",
        "occupied_by",
        "parent_link",
        "description",
        "meta_data",
    }
    metadata = dict(raw_site.get("meta_data") or {})
    metadata.update(
        {
            str(key): value
            for key, value in raw_site.items()
            if key not in known_fields
        }
    )
    site = {
        "schema_version": 1,
        "index": index,
        "label": label,
        "visible": bool(raw_site.get("visible", True)),
        "position_x": _site_number(position.get("x"), label, "position.x"),
        "position_y": _site_number(position.get("y"), label, "position.y"),
        "position_z": _site_number(position.get("z"), label, "position.z"),
        "rotation_x": _site_number(rotation.get("x"), label, "rotation.x"),
        "rotation_y": _site_number(rotation.get("y"), label, "rotation.y"),
        "rotation_z": _site_number(rotation.get("z"), label, "rotation.z"),
        "width": _site_number(size.get("width"), label, "size.width"),
        "length": _site_number(
            size.get("length", size.get("height")), label, "size.height"
        ),
        "depth": _site_number(size.get("depth"), label, "size.depth"),
        "content_type": [],
        "_allowed_resource_template_names": [
            content_type.strip() for content_type in content_types
        ],
        "parent_link": str(raw_site.get("parent_link") or "").strip(),
        "meta_data": metadata,
    }
    description = raw_site.get("description")
    if description is not None:
        site["description"] = str(description)
    if "initial_material_template_uuid" in raw_site:
        site["initial_material_template_uuid"] = raw_site.get(
            "initial_material_template_uuid"
        )
    return site


def _resolve_available_site_template_identities(
    definitions: list[dict[str, Any]],
    template_uuids: Mapping[str, str],
) -> None:
    """把部署图库位（Site）资源类白名单解析为 Backend 稳定 UUID。

    参数说明：``definitions`` 是待二次原子上传的完整模板定义；
    ``template_uuids`` 是 Backend 首次上传返回的完整模板名到 UUID 映射。
    返回：无；成功时就地把库位允许的资源模板（ResourceTemplate）
    短名替换为 UUID。异常：库位结构、白名单结构或模板身份无法
    唯一解析时抛出 ``TemplateSyncError``，禁止错误绑定。
    """

    # ``template_identity_aliases`` 只为全局唯一短名建立别名；
    # 完整名和 Backend 返回的 UUID 仍是资源模板身份权威。
    template_identity_aliases = _unique_template_identity_aliases(template_uuids)

    for definition in definitions:
        raw_sites = definition.get("available_sites")
        if not isinstance(raw_sites, list):
            continue
        for site in raw_sites:
            if not isinstance(site, dict):
                raise TemplateSyncError("available_sites member must be an object")
            raw_names = site.pop("_allowed_resource_template_names", [])
            if not isinstance(raw_names, list):
                raise TemplateSyncError(
                    "available site resource template names must be an array"
                )
            existing_allowed_uuids = site.get(
                "allowed_resource_template_uuids", []
            )
            if not isinstance(existing_allowed_uuids, list):
                raise TemplateSyncError(
                    "available site allowed resource template UUIDs must be an array"
                )
            allowed_uuids = list(existing_allowed_uuids)
            for raw_name in raw_names:
                template_name = str(raw_name).strip()
                template_uuid = template_identity_aliases.get(template_name)
                if not template_uuid:
                    raise TemplateSyncError(
                        f"available site references unknown resource template {template_name}"
                    )
                if template_uuid not in allowed_uuids:
                    allowed_uuids.append(template_uuid)
            site["allowed_resource_template_uuids"] = allowed_uuids
            metadata = site.get("meta_data")
            unilab_metadata = (
                metadata.get("unilab")
                if isinstance(metadata, Mapping)
                and isinstance(metadata.get("unilab"), Mapping)
                else {}
            )
            if INITIAL_MATERIAL_TEMPLATE_HINT in unilab_metadata:
                raw_initial_name = unilab_metadata[
                    INITIAL_MATERIAL_TEMPLATE_HINT
                ]
                if not isinstance(raw_initial_name, str) or not raw_initial_name:
                    raise TemplateSyncError(
                        "available site initial material template name is invalid"
                    )
                initial_uuid = template_identity_aliases.get(raw_initial_name)
                if not initial_uuid:
                    raise TemplateSyncError(
                        "available site references unknown initial resource "
                        f"template {raw_initial_name}"
                    )
                existing_initial_uuid = site.get(
                    "initial_material_template_uuid"
                )
                if (
                    existing_initial_uuid is not None
                    and existing_initial_uuid != initial_uuid
                ):
                    raise TemplateSyncError(
                        "available site initial material template name conflicts "
                        "with initial_material_template_uuid"
                    )
                if initial_uuid not in allowed_uuids:
                    content_types = site.get("content_type")
                    content_type_matches_initial = (
                        isinstance(content_types, list)
                        and any(
                            template_identity_aliases.get(str(content_type).strip())
                            == initial_uuid
                            for content_type in content_types
                        )
                    )
                    if content_type_matches_initial and not allowed_uuids:
                        allowed_uuids.append(initial_uuid)
                        site["allowed_resource_template_uuids"] = allowed_uuids
                site["initial_material_template_uuid"] = initial_uuid
            try:
                # 部署图路径会先暂存模板业务名，待首次事务回执后才能完成 UUID
                # 白名单；此处在最终上传前执行与 Registry 完全相同的字段校验。
                SiteDefinition.model_validate(site)
            except ValueError as error:
                raise TemplateSyncError(
                    f"invalid available site definition: {error}"
                ) from error


def _has_deferred_site_template_identity(
    definitions: list[dict[str, Any]],
) -> bool:
    """判断 Registry 模板是否包含必须经 Backend 回执解析的 Site 模板短名。"""

    for definition in definitions:
        sites = definition.get("available_sites")
        if not isinstance(sites, list):
            continue
        for site in sites:
            if not isinstance(site, Mapping):
                continue
            metadata = site.get("meta_data")
            unilab_metadata = (
                metadata.get("unilab")
                if isinstance(metadata, Mapping)
                and isinstance(metadata.get("unilab"), Mapping)
                else {}
            )
            if INITIAL_MATERIAL_TEMPLATE_HINT in unilab_metadata:
                return True
    return False


def _unique_template_identity_aliases(
    template_uuids: Mapping[str, str],
) -> dict[str, str]:
    """为 Backend 完整模板名构建精确且无歧义的短名别名。

    参数说明：``template_uuids`` 是完整资源模板（ResourceTemplate）
    业务名到 Backend UUID 的映射。返回：包含所有精确名、以及仅当
    名字末段全局唯一时才增加的短名别名；歧义短名不进入结果。
    """

    aliases = {
        str(template_name): str(template_uuid)
        for template_name, template_uuid in template_uuids.items()
    }
    # ``short_name_candidates`` 保留同名资源模板的全部 UUID，
    # 使跨包同名时失败关闭，而不是按遍历顺序偶然选中一个。
    short_name_candidates: dict[str, set[str]] = {}
    for template_name, template_uuid in aliases.items():
        short_name = template_name.rsplit(".", 1)[-1]
        short_name_candidates.setdefault(short_name, set()).add(template_uuid)
    for short_name, candidate_uuids in short_name_candidates.items():
        if short_name not in aliases and len(candidate_uuids) == 1:
            aliases[short_name] = next(iter(candidate_uuids))
    return aliases


def _site_number(value: Any, label: str, field: str) -> float:
    """把可选库位几何值规范为有限浮点数。"""

    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise TemplateSyncError(f"device graph site {label} {field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise TemplateSyncError(
            f"device graph site {label} {field} must be numeric"
        ) from error
    if number != number or number in (float("inf"), float("-inf")):
        raise TemplateSyncError(f"device graph site {label} {field} must be finite")
    if field.startswith("size.") and number < 0:
        raise TemplateSyncError(
            f"device graph site {label} {field} must not be negative"
        )
    return number


def _decode_sync_response(response: Any) -> dict[str, Any]:
    """校验 Backend 模板事务响应并返回模板稳定身份映射载荷。

    参数说明：``response`` 是 HTTP 适配器返回的响应对象。返回 ``data`` 中的
    JSON 对象；HTTP、业务错误或模板身份结构不完整时抛出
    ``TemplateSyncError``，禁止调用者接受部分同步结果。
    """

    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TemplateSyncError(
            f"template sync returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise TemplateSyncError(
            f"template sync returned HTTP {response.status_code}: {payload}"
        )
    if not isinstance(payload, Mapping):
        raise TemplateSyncError("template sync returned a non-object response")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise TemplateSyncError(
            f"template sync returned business error {code}: {payload.get('error')}"
        )
    result = payload.get("data", payload)
    if not isinstance(result, Mapping) or not isinstance(result.get("templates"), list):
        raise TemplateSyncError("template sync returned invalid template identities")
    return dict(result)


def _api_base(address: str) -> str:
    """把 Backend 地址规范化为唯一的 ``/api/v1`` 接口前缀。

    参数说明：``address`` 是用户或启动配置提供的 Backend 地址。返回去除末尾
    斜杠后的接口根地址；空地址抛出 ``TemplateSyncError``。
    """

    base = str(address or "").strip().rstrip("/")
    if not base:
        raise TemplateSyncError("backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"
