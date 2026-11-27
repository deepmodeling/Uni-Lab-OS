"""Prepare a Backend Authority from the currently running local projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import requests

from unilabos.app.instance_sync import InstanceSynchronizer

from .model import WorkspaceHostError


@dataclass(frozen=True)
class AuthorityBootstrapReport:
    """Stable counts emitted by one idempotent authority bootstrap."""

    template_count: int
    created_material_count: int
    existing_material_count: int


class BackendAuthorityBootstrapper:
    """Copy the local authoring projection, then initialize graph instances.

    The source is the already-running Local Backend rather than a second registry
    build.  That guarantees the target receives the exact template generation the
    author has inspected in Workbench, including workspace-qualified identities.
    """

    def __init__(
        self,
        source_address: str,
        target_address: str,
        credential: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        """配置 Local Backend 到目标 Backend Authority 的同步边界。

        Args:
            source_address: 当前工作区 Local Backend 的公开 API 地址。
            target_address: 接收模板和物料（Material）的目标 Backend 地址。
            credential: 目标 Backend 的操作凭据。
            session: 可选 HTTP 会话；测试或连接复用时注入。
            timeout: 单次 HTTP 请求超时秒数。

        Returns:
            无返回值；保存规范 API 地址、凭据和请求配置。

        Raises:
            WorkspaceHostError: 操作凭据为空时抛出。
        """

        self.source_api = _api_base(source_address)
        self.target_api = _api_base(target_address)
        self.credential = str(credential or "").strip()
        if not self.credential:
            raise WorkspaceHostError(
                "backend_authority_credentials_missing",
                "Backend Authority 初始化凭据缺失",
            )
        self.session = session or requests.Session()
        self.timeout = timeout

    def bootstrap(self, graph_path: Path) -> AuthorityBootstrapReport:
        """把 Local Backend 模板和设备图幂等收敛到目标 Backend Authority。

        Args:
            graph_path: 当前设备包选中的设备图 JSON 路径。

        Returns:
            本轮同步的模板数、新建物料数和复用物料数。

        Raises:
            WorkspaceHostError: 设备图、模板响应或物料（Material）同步失败时抛出。

        已存在的发行物料仍必须进入实例同步；设备包声明的位置属于部署图事实，
        不能因为物料身份已经发布就跳过后续位置收敛。
        """

        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                f"无法读取当前设备图：{graph_path}",
            ) from error
        if not isinstance(graph, Mapping):
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "当前设备图根必须是 JSON object",
            )

        source_templates = [
            self._source_template(item) for item in self._source_templates()
        ]
        definitions = [_template_definition(item) for item in source_templates]
        if not definitions:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Local Backend 未提供可同步的资源模板",
            )
        source_template_names = {
            str(item.get("uuid") or ""): str(item.get("name") or "")
            for item in source_templates
        }
        _remap_available_site_template_identities(
            definitions,
            source_template_names,
            {
                template_name: template_uuid
                for template_uuid, template_name in source_template_names.items()
            },
            error_code="backend_authority_bootstrap_failed",
        )
        response = self.session.post(
            f"{self.target_api}/resource-templates",
            json={"resources": definitions},
            headers=self._headers(),
            timeout=self.timeout,
        )
        payload = _response_data(response, "资源模板同步")
        identities = payload.get("templates")
        if not isinstance(identities, list) or len(identities) != len(definitions):
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Backend Authority 未返回完整模板身份",
            )
        target_template_uuids = {
            str(item.get("name") or ""): str(item.get("uuid") or "")
            for item in identities
            if isinstance(item, Mapping)
        }
        if set(target_template_uuids) != {
            str(definition["id"]) for definition in definitions
        }:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Backend Authority 未返回完整模板身份",
            )
        if _remap_available_site_template_identities(
            definitions,
            source_template_names,
            target_template_uuids,
            error_code="backend_authority_bootstrap_failed",
        ):
            _response_data(
                self.session.post(
                    f"{self.target_api}/resource-templates",
                    json={"resources": definitions},
                    headers=self._headers(),
                    timeout=self.timeout,
                ),
                "资源模板身份重映射",
            )
        try:
            material_report = InstanceSynchronizer(
                self.target_api,
                self.credential,
                session=self.session,
                timeout=self.timeout,
            ).sync_graph(graph)
        except Exception as error:  # noqa: BLE001 - normalize the transaction seam.
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                f"Backend Authority 资源实例初始化失败：{error}",
            ) from error
        return AuthorityBootstrapReport(
            template_count=len(definitions),
            created_material_count=material_report.created_count,
            existing_material_count=material_report.existing_count,
        )

    def _source_templates(self) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        cursor: str | None = None
        page_number = 1
        pagination_mode: str | None = None
        while True:
            params: dict[str, object]
            if pagination_mode == "cursor":
                params = {"limit": 100}
            else:
                params = {"page": page_number, "page_size": 100}
            if pagination_mode == "cursor" and cursor:
                params["cursor_uuid"] = cursor
            response = self.session.get(
                f"{self.source_api}/resource-templates",
                params=params,
                timeout=self.timeout,
            )
            page = _response_data(response, "Local Backend 模板列表")
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表结构无效",
                )
            items.extend(item for item in raw_items if isinstance(item, Mapping))
            if not page.get("has_more"):
                return items

            has_numbered_fields = "page" in page or "page_size" in page
            next_cursor = next(
                (
                    value
                    for key in ("next_cursor_uuid", "next_cursor", "cursor")
                    if isinstance((value := page.get(key)), str) and value
                ),
                None,
            )
            has_cursor_fields = next_cursor is not None
            if has_numbered_fields and has_cursor_fields:
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表混合了页码与游标合同",
                )
            # Local projections have shipped both a fully annotated numbered
            # response and a compact ``items + has_more`` response.  The
            # request itself is numbered until the server explicitly returns a
            # cursor, so a compact response must keep advancing page numbers.
            response_mode = (
                "cursor"
                if has_cursor_fields
                else "numbered"
            )
            if pagination_mode is not None and response_mode != pagination_mode:
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表分页合同发生漂移",
                )
            pagination_mode = response_mode
            if pagination_mode == "numbered":
                returned_page = page.get("page")
                page_size = page.get("page_size")
                if returned_page is not None and (
                    not isinstance(returned_page, int)
                    or isinstance(returned_page, bool)
                    or returned_page != page_number
                ):
                    raise WorkspaceHostError(
                        "backend_authority_bootstrap_failed",
                        "Local Backend 模板列表页码无效",
                    )
                if page_size is not None and (
                    not isinstance(page_size, int)
                    or isinstance(page_size, bool)
                    or page_size <= 0
                ):
                    raise WorkspaceHostError(
                        "backend_authority_bootstrap_failed",
                        "Local Backend 模板列表页大小无效",
                    )
                page_number += 1
                continue

            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表游标无效",
                )
            cursor = next_cursor

    def _source_template(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        template_uuid = str(item.get("uuid") or "").strip()
        if not template_uuid:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Local Backend 资源模板缺少 UUID",
            )
        response = self.session.get(
            f"{self.source_api}/resource-templates/{template_uuid}",
            timeout=self.timeout,
        )
        return _response_data(response, "Local Backend 模板详情")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential}",
            "Content-Type": "application/json",
        }


def _template_definition(template: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the public Backend DTO back into the registry sync contract."""

    name = str(template.get("name") or "").strip()
    if not name:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            "Local Backend 资源模板缺少业务身份",
        )
    meta_data = template.get("meta_data")
    unilab_metadata = (
        meta_data.get("unilab")
        if isinstance(meta_data, Mapping)
        and isinstance(meta_data.get("unilab"), Mapping)
        else {}
    )
    handles = []
    for raw_handle in template.get("handles") or []:
        if not isinstance(raw_handle, Mapping):
            continue
        handle = {
            key: raw_handle.get(key)
            for key in (
                "name",
                "display_name",
                "type",
                "io_type",
                "source",
                "key",
                "side",
            )
            if raw_handle.get(key) is not None
        }
        handles.append(handle)
    return {
        "id": name,
        "display_name": template.get("display_name") or name,
        "description": template.get("description"),
        "registry_type": template.get("resource_type") or "resource",
        "icon": template.get("icon"),
        "model": template.get("model") or {},
        "class": {
            "module": template.get("module"),
            "type": template.get("language") or "python",
        },
        "category": template.get("tags") or [],
        "init_param_schema": {
            "data": {"properties": template.get("data_schema") or {}},
            "config": {"properties": template.get("config_schema") or {}},
        },
        "config_info": template.get("config_info") or [],
        "available_sites": template.get("available_sites") or [],
        "cover": template.get("cover"),
        "scene": template.get("scene") or [],
        "device_params": template.get("device_params") or {},
        "handles": handles,
        "source_uri": unilab_metadata.get("source_uri"),
    }


def _remap_available_site_template_identities(
    definitions: list[dict[str, Any]],
    source_template_names: Mapping[str, str],
    target_template_uuids: Mapping[str, str],
    *,
    error_code: str,
) -> bool:
    """校验并把库位中的本地资源模板 UUID 映射到目标权威。

    参数：``definitions`` 是即将上传的模板定义，``source_template_names`` 把本地
    UUID 映射到业务名，``target_template_uuids`` 把同一业务名映射到目标 UUID。
    返回：至少一个 UUID 被替换时为真。引用未知模板、目标身份缺失或最终库位合同
    非法时抛出 ``WorkspaceHostError``，禁止静默丢弃默认内容语义。
    """

    changed = False
    target_uuid_values = set(target_template_uuids.values())
    for definition in definitions:
        raw_sites = definition.get("available_sites")
        if not isinstance(raw_sites, list):
            continue
        for site in raw_sites:
            if not isinstance(site, dict):
                raise WorkspaceHostError(
                    error_code, "资源模板 available_sites 成员必须是对象"
                )
            raw_allowed = site.get("allowed_resource_template_uuids", [])
            if not isinstance(raw_allowed, list):
                raise WorkspaceHostError(
                    error_code,
                    "库位 allowed_resource_template_uuids 必须是数组",
                )
            for field, values in (
                ("allowed_resource_template_uuids", raw_allowed),
                (
                    "initial_material_template_uuid",
                    []
                    if site.get("initial_material_template_uuid") is None
                    else [site.get("initial_material_template_uuid")],
                ),
            ):
                remapped: list[str] = []
                for raw_uuid in values:
                    source_uuid = str(raw_uuid or "")
                    if source_uuid in target_uuid_values:
                        target_uuid = source_uuid
                    else:
                        template_name = source_template_names.get(source_uuid)
                        target_uuid = (
                            target_template_uuids.get(template_name, "")
                            if template_name
                            else ""
                        )
                    if not target_uuid:
                        raise WorkspaceHostError(
                            error_code,
                            "库位引用了无法跨权威解析的资源模板 UUID",
                            details={
                                "field": field,
                                "resource_template_uuid": source_uuid,
                            },
                        )
                    remapped.append(target_uuid)
                    changed = changed or target_uuid != source_uuid
                if field == "allowed_resource_template_uuids":
                    site[field] = remapped
                elif values:
                    site[field] = remapped[0]
            initial_uuid = site.get("initial_material_template_uuid")
            if initial_uuid is None:
                continue
            try:
                parsed_initial_uuid = UUID(initial_uuid)
            except (AttributeError, TypeError, ValueError) as error:
                raise WorkspaceHostError(
                    error_code,
                    "库位 initial_material_template_uuid 必须是规范 UUID",
                ) from error
            if (
                parsed_initial_uuid.int == 0
                or initial_uuid != str(parsed_initial_uuid)
            ):
                raise WorkspaceHostError(
                    error_code,
                    "库位 initial_material_template_uuid 必须是规范 UUID",
                )
            if (
                site["allowed_resource_template_uuids"]
                and initial_uuid
                not in site["allowed_resource_template_uuids"]
            ):
                raise WorkspaceHostError(
                    error_code,
                    "库位 initial_material_template_uuid "
                    "必须包含在 allowed_resource_template_uuids 中",
                )
    return changed


def _response_data(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}返回非 JSON 响应",
        ) from error
    if not 200 <= int(response.status_code) < 300 or not isinstance(payload, Mapping):
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}失败：HTTP {response.status_code}",
        )
    if int(payload.get("code") or 0) != 0:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}失败：{payload.get('error')}",
        )
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}返回数据结构无效",
        )
    return dict(data)


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed", "Backend 服务地址缺失"
        )
    return base if base.endswith("/api/v1") else f"{base}/api/v1"
