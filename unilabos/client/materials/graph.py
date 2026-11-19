"""设备图 ``/api/v1/graphs`` 的 HTTP 客户端。

图属于 materials 域（节点/边/快照都在 materials.db），本客户端只是该域
graphs 端点的窄封装。默认指向本机微后端；``--remote`` 时由 CLI 传入云端
base_url，双方共用同一 envelope 契约。
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.utils.address import normalize_api_address


class HTTPGraphClient:
    """``/api/v1/graphs`` REST API 的窄客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        ak: str = "",
        sk: str = "",
        timeout: float = 30.0,
        http_client: Optional[Any] = None,
    ) -> None:
        self.base_url = normalize_api_address(base_url)
        self._auth_secret = ""
        if ak and sk:
            token = base64.b64encode(f"{ak}:{sk}".encode("utf-8"))
            self._auth_secret = token.decode("ascii")
        self._http = http_client or HTTPClient(
            HTTPClientConfig(base_url=self.base_url, timeout=timeout),
            get_auth_secret=(lambda: self._auth_secret),
        )
        self._owns_http_client = http_client is None

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "HTTPGraphClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_graphs(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        name: str = "",
    ) -> dict[str, Any]:
        return self._http.get(
            "/graphs",
            params={"page": page, "page_size": page_size, "name": name},
        )

    def upsert_graph(
        self,
        *,
        name: str,
        payload: Mapping[str, Any],
        uuid: Optional[str] = None,
        tags: Sequence[Any] = (),
        description: Optional[str] = None,
        meta_data: Optional[Mapping[str, Any]] = None,
        device_site_templates: Optional[Mapping[str, Sequence[Any]]] = None,
        on_existing: str = "replace",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "payload": dict(payload),
            "uuid": uuid,
            "tags": list(tags),
            "description": description,
            "meta_data": dict(meta_data or {}),
            "on_existing": on_existing,
        }
        if device_site_templates is not None:
            body["device_site_templates"] = {
                key: list(value) for key, value in device_site_templates.items()
            }
        return self._http.post("/graphs", json=body)

    def get_graph(self, identity: str) -> dict[str, Any]:
        return self._http.get(f"/graphs/{identity}")

    def download_graph(self, identity: str) -> dict[str, Any]:
        return self._http.get(f"/graphs/{identity}/payload")

    def download_live_graph(self) -> dict[str, Any]:
        """当前真实拓扑（material + material_link 实时序列化，非快照回放）。"""
        return self._http.get("/graphs/live/payload")

    def delete_graph(self, identity: str) -> dict[str, Any]:
        return self._http.delete(f"/graphs/{identity}")

    # 旧格式图上传前按模板补 PLR 类名：查同一调度权威的注册表（Host 上报的目录）

    def registry_status(self) -> dict[str, Any]:
        """``GET /registry/status``：是否收到过 Host 的注册表上报等。"""
        return self._http.get("/registry/status")

    def registry_entry(self, name: str) -> dict[str, Any]:
        """``GET /registry/entries/{name}``：条目状态与生效 payload；不存在时服务端回 404。"""
        return self._http.get(f"/registry/entries/{quote(str(name), safe='')}")


__all__ = ["HTTPGraphClient"]
