"""
HTTP客户端模块

提供与远程服务器通信的客户端功能，只有host需要用
"""

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import requests
from unilabos.utils.log import info
from unilabos.config.config import HTTPConfig, BasicConfig
from unilabos.utils import logger
from unilabos.utils.tracing import inject_trace_context, span


class TracedSession(requests.Session):
    """为 Edge 主动 HTTP 请求统一创建 Client Span 并注入 W3C 上下文。"""

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        parsed = urlsplit(str(url))
        with span(
            "edge.http.backend.request",
            kind="client",
            attributes={
                "http.request.method": str(method).upper(),
                "server.address": parsed.hostname or "",
                "url.scheme": parsed.scheme,
                "url.path": parsed.path,
            },
        ) as request_span:
            inject_trace_context(headers)
            kwargs["headers"] = headers
            response = super().request(method, url, **kwargs)
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass
            return response


class HTTPClient:
    """HTTP客户端，用于与远程服务器通信"""

    def __init__(
        self,
        remote_addr: Optional[str] = None,
        auth: Optional[str] = None,
        material_microbackend_addr: Optional[str] = None,
    ) -> None:
        """
        初始化HTTP客户端

        Args:
            remote_addr: 远程服务器地址，如果不提供则从配置中获取
            auth: 授权信息
        """
        self.remote_addr = remote_addr or HTTPConfig.remote_addr
        self._material_microbackend_addr_override = material_microbackend_addr
        if auth is not None:
            self.auth = auth
        else:
            auth_secret = BasicConfig.auth_secret()
            self.auth = auth_secret
            info(f"正在使用ak sk作为授权信息：[{auth_secret}]")
        # 复用 TCP/TLS 连接，避免每次请求重新握手
        self._session = TracedSession()
        self._session.headers.update({"Authorization": f"Lab {self.auth}"})
        info(f"HTTPClient 初始化完成: remote_addr={self.remote_addr}")

    @staticmethod
    def _api_base(address: str) -> str:
        """Accept either an origin or an already versioned API base."""

        base = address.rstrip("/")
        if base.endswith("/api/v1"):
            return base
        return f"{base}/api/v1"

    def _material_microbackend_base(self) -> str:
        configured = (
            self._material_microbackend_addr_override
            if self._material_microbackend_addr_override is not None
            else HTTPConfig.material_microbackend_addr
        )
        if not configured:
            configured = f"http://127.0.0.1:{BasicConfig.port}"
        return self._api_base(str(configured))

    @staticmethod
    def _extract_material_nodes(payload: Any) -> List[Dict[str, Any]]:
        """Normalize old envelopes and direct microbackend DTOs to flat nodes."""

        candidate = payload
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and str(code) != "0":
                raise ValueError(f"material service returned business code {code}")
            candidate = payload.get("data", payload)
            if isinstance(candidate, dict) and "nodes" in candidate:
                candidate = candidate["nodes"]

        if candidate is None:
            return []
        if isinstance(candidate, dict) and ("uuid" in candidate or "id" in candidate):
            candidate = [candidate]
        if not isinstance(candidate, list):
            raise ValueError("material response does not contain a node list")
        if not all(isinstance(node, dict) for node in candidate):
            raise ValueError("material response node list contains a non-object value")
        return candidate

    @staticmethod
    def _write_material_debug(filename: str, content: str) -> None:
        """Retain existing request diagnostics without making queries depend on I/O."""

        if not BasicConfig.working_dir:
            return
        try:
            with open(
                os.path.join(BasicConfig.working_dir, filename),
                "w",
                encoding="utf-8",
            ) as file:
                file.write(content)
        except OSError as exc:
            logger.debug(f"写入物料查询诊断文件失败: {exc}")

    def _query_material_microbackend(
        self,
        *,
        uuids: List[str],
        resource_id: Optional[str],
        with_children: bool,
    ) -> List[Dict[str, Any]]:
        timeout = int(HTTPConfig.material_query_timeout)
        url = f"{self._material_microbackend_base()}/edge/material/query"
        body: Dict[str, Any] = {
            "uuids": uuids,
            "with_children": with_children,
        }
        if resource_id:
            body["id"] = resource_id
        response = self._session.post(url, json=body, timeout=timeout)

        self._write_material_debug(
            "res_material_query.json",
            f"source=microbackend\nurl={url}\n{response.status_code}\n{response.text}",
        )
        if response.status_code != 200:
            raise requests.HTTPError(
                f"material query returned HTTP {response.status_code}: {response.text}",
                response=response,
            )
        return self._extract_material_nodes(response.json())

    def material_query(
        self,
        *,
        uuids: Optional[List[str]] = None,
        resource_id: Optional[str] = None,
        with_children: bool = True,
    ) -> List[Dict[str, Any]]:
        """只查询 Edge 微后端物料中心。"""

        if not BasicConfig.is_host_mode:
            logger.warning("Slave 禁止直连物料数据库；请通过 HostLink 向 HostNode 查询")
            return []

        uuid_list = [str(value) for value in (uuids or []) if value]
        if not uuid_list and not resource_id:
            raise ValueError("material_query requires uuids or resource_id")
        request_body = {
            "uuids": uuid_list,
            "id": resource_id,
            "with_children": with_children,
        }
        self._write_material_debug(
            "req_material_query.json",
            json.dumps(request_body, ensure_ascii=False, indent=4),
        )

        try:
            nodes = self._query_material_microbackend(
                uuids=uuid_list,
                resource_id=resource_id,
                with_children=with_children,
            )
            logger.trace(f"material_query 查询到 {len(nodes)} 个节点")
            return nodes
        except (requests.RequestException, TypeError, ValueError) as exc:
            logger.warning(f"微后端物料查询失败: {exc}")
            return []

    def resource_tree_get(
        self, uuid_list: List[str], with_children: bool
    ) -> List[Dict[str, Any]]:
        """
        按 UUID 查询物料树（兼容旧调用名和返回形状）。

        Args:
            uuid_list: List[str]
        Returns:
            扁平 ResourceDict 节点列表
        """
        return self.material_query(
            uuids=uuid_list,
            with_children=with_children,
        )

    def material_bench_discard(self, uuids: List[str]) -> Dict[str, Any]:
        """
        台面物料废弃（Edge 端）

        对应 POST /edge/material/bench/discard，按 uuid 销毁台面物料；实验室归属由认证
        上下文确定，请求体不含 lab_uuid。

        Args:
            uuids: 台面物料 UUID 列表，1~100 个

        Returns:
            Dict: 服务端响应（成功为 {"code": 0}）；错误码 100002 节点不存在 / 100003 当前状态不允许
        """
        if not uuids:
            raise ValueError("台面物料废弃失败：uuids 为空")
        if len(uuids) > 100:
            raise ValueError(
                f"台面物料废弃失败：一次最多 100 个 uuid，收到 {len(uuids)} 个"
            )
        payload = {"uuids": uuids}
        work_dir = BasicConfig.working_dir
        with open(
            os.path.join(work_dir, "req_material_bench_discard.json"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=4))
        responses = [
            self._session.delete(
                f"{self._material_microbackend_base()}/materials/"
                f"{quote(material_uuid, safe='')}",
                timeout=30,
            )
            for material_uuid in uuids
        ]
        for response in responses:
            if response.status_code != 200:
                logger.error(
                    f"台面物料废弃失败: {response.status_code}, {response.text}"
                )
                return {
                    "code": response.status_code,
                    "message": response.text,
                }
            result = response.json()
            if str(result.get("code", 0)) != "0":
                logger.error(f"台面物料废弃失败: {response.text}")
                return result
        return {"code": 0}

    def resource_get(self, id: str, with_children: bool = False) -> Dict[str, Any]:
        """
        获取资源

        Args:
            id: 资源ID
            with_children: 是否包含子资源

        Returns:
            Dict: 返回的资源数据
        """
        nodes = self.material_query(
            resource_id=id,
            with_children=with_children,
        )
        # ROS 查询服务仍使用固定的 data envelope。
        return {"code": 0, "data": nodes}


# 新路径只保留本地/独立微后端物料 HTTP 适配器。
material_http_client = HTTPClient()


def __getattr__(name: str) -> Any:
    """将旧 http_client 名称映射到受 --legacy 控制的兼容层。"""

    if name == "http_client":
        from unilabos.legacy_support.http import get_legacy_http_client

        return get_legacy_http_client()
    raise AttributeError(name)


__all__ = ["HTTPClient", "TracedSession", "material_http_client"]
