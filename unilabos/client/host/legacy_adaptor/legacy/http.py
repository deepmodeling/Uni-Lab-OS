"""旧云端 Backend 的 HTTP 数据面客户端。

与 dev 分支 ``unilabos/app/web/client.py`` 的线协议一致：

- ``POST /lab/resource``                 注册表上报（gzip JSON，``{"resources": [...]}``）；
- ``POST|PUT /edge/material``            物料树全量 / 增量上报（``{"nodes", "mount_uuid"}``）；
- ``POST /edge/material/edge``           物料拓扑边上报（``{"edges": [...]}``）；
- ``POST /edge/material/query``          按 uuid 拉取物料树；
- ``POST /edge/material/bench/discard``  台面物料废弃；
- ``GET  /edge/material/download``       下载实验室完整设备/物料图；
- ``GET  /edge/lab/info``                实验室信息（鉴权自检）。

所有响应都是 ``{"code": 0, "data": ...}`` 信封；``code != 0`` 或非 2xx 抛
:class:`LegacyBackendHTTPError`。
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.utils.log import get_comm_logger
from unilabos.utils.serialization import normalize_json

logger = get_comm_logger()


class LegacyBackendHTTPError(RuntimeError):
    """旧 Backend HTTP 数据面返回失败或无效响应。"""

    def __init__(self, message: str, *, status_code: int = 0, code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise ValueError("legacy backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def _decode(response: requests.Response, *, operation: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LegacyBackendHTTPError(
            f"{operation} returned non-JSON HTTP {response.status_code}: "
            f"{response.text[:200]!r}",
            status_code=response.status_code,
        ) from exc
    if not 200 <= response.status_code < 300:
        raise LegacyBackendHTTPError(
            f"{operation} returned HTTP {response.status_code}: {payload}",
            status_code=response.status_code,
        )
    if not isinstance(payload, Mapping):
        raise LegacyBackendHTTPError(
            f"{operation} returned a non-object response",
            status_code=response.status_code,
        )
    code = int(payload.get("code") or 0)
    if code != 0:
        error = payload.get("error")
        if isinstance(error, Mapping):
            error = error.get("msg") or error
        raise LegacyBackendHTTPError(
            f"{operation} returned business error {code}: {error}",
            status_code=response.status_code,
            code=code,
        )
    return payload.get("data")


class LegacyBackendHTTPClient:
    """旧 Backend 的通用 HTTP 客户端，复用一个带鉴权头的 ``requests.Session``。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = _api_base(base_url or HTTPConfig.remote_addr)
        self.timeout = timeout
        self.session = session or requests.Session()
        if "Authorization" not in self.session.headers:
            self.session.headers.update(
                {"Authorization": f"Lab {BasicConfig.auth_secret()}"}
            )

    # ── 实验室 ─────────────────────────────────────────────────

    def lab_info(self) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/edge/lab/info", timeout=self.timeout
        )
        data = _decode(response, operation="GET /edge/lab/info")
        return dict(data) if isinstance(data, Mapping) else {}

    # ── 注册表 ─────────────────────────────────────────────────

    def upload_registry(
        self,
        resources: Sequence[Mapping[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """``POST /lab/resource``：上报一批注册表条目（设备或物料模板）。

        返回 ``data``（旧后端在内容未变化时返回 ``{"skipped": true}``）。
        """

        body = json.dumps(
            {"resources": [normalize_json(dict(item)) for item in resources]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = self.session.post(
            f"{self.base_url}/lab/resource",
            data=gzip.compress(body),
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            timeout=timeout or max(self.timeout, 60.0),
        )
        data = _decode(response, operation="POST /lab/resource")
        return dict(data) if isinstance(data, Mapping) else {}

    # ── 物料树 ─────────────────────────────────────────────────

    def upload_material_tree(
        self,
        nodes: Sequence[Mapping[str, Any]],
        *,
        mount_uuid: str = "",
        first_add: bool,
    ) -> Dict[str, str]:
        """``POST``（首次全量）或 ``PUT``（增量）``/edge/material``。

        返回 ``{edge_uuid: cloud_uuid}``；权威 uuid 已由微后端发号，旧后端
        应原样回显，映射仅用于日志核对。
        """

        payload = {"nodes": [dict(node) for node in nodes], "mount_uuid": mount_uuid}
        method = self.session.post if first_add else self.session.put
        response = method(
            f"{self.base_url}/edge/material",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=max(self.timeout, 60.0) if first_add else self.timeout,
        )
        data = _decode(
            response,
            operation=f"{'POST' if first_add else 'PUT'} /edge/material",
        )
        mapping: Dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, Mapping) and item.get("uuid"):
                    mapping[str(item["uuid"])] = str(
                        item.get("cloud_uuid") or item["uuid"]
                    )
        return mapping

    def upload_material_edges(
        self, edges: Sequence[Mapping[str, Any]]
    ) -> None:
        """``POST /edge/material/edge``：上报物料拓扑边（source_uuid/target_uuid + handle）。"""

        if not edges:
            return
        response = self.session.post(
            f"{self.base_url}/edge/material/edge",
            json={"edges": [dict(edge) for edge in edges]},
            timeout=max(self.timeout, 60.0),
        )
        _decode(response, operation="POST /edge/material/edge")

    def query_material_tree(
        self, uuids: Sequence[str], *, with_children: bool = True
    ) -> List[Dict[str, Any]]:
        """``POST /edge/material/query``：按 uuid 拉取旧后端的物料节点（扁平列表）。"""

        if not uuids:
            return []
        response = self.session.post(
            f"{self.base_url}/edge/material/query",
            json={"uuids": list(uuids), "with_children": bool(with_children)},
            timeout=max(self.timeout, 60.0),
        )
        data = _decode(response, operation="POST /edge/material/query")
        nodes = data.get("nodes") if isinstance(data, Mapping) else data
        return [dict(node) for node in nodes or [] if isinstance(node, Mapping)]

    def discard_bench_materials(self, uuids: Sequence[str]) -> None:
        """``POST /edge/material/bench/discard``：按 uuid 废弃台面物料（每批 ≤100）。"""

        pending = [str(value) for value in uuids if str(value).strip()]
        while pending:
            batch, pending = pending[:100], pending[100:]
            response = self.session.post(
                f"{self.base_url}/edge/material/bench/discard",
                json={"uuids": batch},
                timeout=self.timeout,
            )
            _decode(response, operation="POST /edge/material/bench/discard")

    def download_lab_graph(self) -> Dict[str, Any]:
        """``GET /edge/material/download``：实验室当前设备 + 物料 node-link 图。"""

        response = self.session.get(
            f"{self.base_url}/edge/material/download",
            timeout=(5, max(self.timeout, 60.0)),
        )
        data = _decode(response, operation="GET /edge/material/download")
        if not isinstance(data, Mapping):
            raise LegacyBackendHTTPError("lab graph download returned invalid data")
        return {
            "nodes": list(data.get("nodes") or []),
            "links": list(data.get("edges") or data.get("links") or []),
        }


__all__ = ["LegacyBackendHTTPClient", "LegacyBackendHTTPError"]
