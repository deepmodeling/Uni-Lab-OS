"""
HTTP客户端模块

提供与远程服务器通信的客户端功能，只有host需要用
"""

import gzip
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from unilabos.utils.tools import (
    fast_dumps as _fast_dumps,
    fast_dumps_pretty as _fast_dumps_pretty,
)

import requests
from unilabos.resources.resource_tracker import ResourceTreeSet
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
        material_source: Optional[str] = None,
        material_microbackend_addr: Optional[str] = None,
    ) -> None:
        """
        初始化HTTP客户端

        Args:
            remote_addr: 远程服务器地址，如果不提供则从配置中获取
            auth: 授权信息
        """
        self.initialized = False
        self.remote_addr = remote_addr or HTTPConfig.remote_addr
        self._material_source_override = material_source
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

    def _material_sources(self) -> List[str]:
        configured = (
            self._material_source_override
            if self._material_source_override is not None
            else HTTPConfig.material_source
        )
        source = str(configured or "microbackend").strip().lower()
        aliases = {
            "edge": "microbackend",
            "local": "microbackend",
            "cloud": "backend",
            "remote": "backend",
        }
        source = aliases.get(source, source)
        if source == "auto":
            return ["microbackend", "backend"]
        if source not in {"microbackend", "backend"}:
            raise ValueError(
                "HTTPConfig.material_source must be microbackend, backend, or auto"
            )
        return [source]

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

    def _query_material_source(
        self,
        source: str,
        *,
        uuids: List[str],
        resource_id: Optional[str],
        with_children: bool,
    ) -> List[Dict[str, Any]]:
        timeout = int(HTTPConfig.material_query_timeout)
        if source == "microbackend":
            url = f"{self._material_microbackend_base()}/edge/material/query"
            body: Dict[str, Any] = {
                "uuids": uuids,
                "with_children": with_children,
            }
            if resource_id:
                body["id"] = resource_id
            response = self._session.post(url, json=body, timeout=timeout)
        elif uuids:
            url = f"{self.remote_addr.rstrip('/')}/edge/material/query"
            response = self._session.post(
                url,
                json={"uuids": uuids, "with_children": with_children},
                timeout=timeout,
            )
        else:
            url = f"{self.remote_addr.rstrip('/')}/lab/material"
            response = self._session.get(
                url,
                params={"id": resource_id, "with_children": with_children},
                timeout=timeout,
            )

        self._write_material_debug(
            "res_material_query.json",
            f"source={source}\nurl={url}\n{response.status_code}\n{response.text}",
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
        """Query the configured material component and preserve legacy shape.

        ``microbackend`` uses local HTTP IPC, ``backend`` uses the formal
        service, and ``auto`` tries them in that order.  Empty results in auto
        mode fall through so an Edge cache can be introduced incrementally.
        """

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

        sources = self._material_sources()
        for index, source in enumerate(sources):
            try:
                nodes = self._query_material_source(
                    source,
                    uuids=uuid_list,
                    resource_id=resource_id,
                    with_children=with_children,
                )
                if nodes or index == len(sources) - 1:
                    logger.trace(
                        f"material_query source={source} 查询到 {len(nodes)} 个节点"
                    )
                    return nodes
            except (requests.RequestException, TypeError, ValueError) as exc:
                logger.warning(f"物料查询失败 source={source}: {exc}")
                if index == len(sources) - 1:
                    return []
        return []

    def resource_edge_add(self, resources: List[Dict[str, Any]]) -> requests.Response:
        """
        添加资源

        Args:
            resources: 要添加的资源列表
            database_process_later: 后台处理资源
        Returns:
            Response: API响应对象
        """
        response = self._session.post(
            f"{self.remote_addr}/edge/material/edge",
            json={
                "edges": resources,
            },
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=100,
        )
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"添加物料关系失败: {response.text}")
        if response.status_code != 200 and response.status_code != 201:
            logger.error(f"添加物料关系失败: {response.status_code}, {response.text}")
        return response

    def resource_tree_add(
        self, resources: ResourceTreeSet, mount_uuid: str, first_add: bool
    ) -> Dict[str, str]:
        """
        添加资源

        Args:
            resources: 要添加的资源树集合（ResourceTreeSet）
            mount_uuid: 要挂载的资源的uuid
            first_add: 是否为首次添加资源，可以是host也可以是slave来的
        Returns:
            Dict[str, str]: 旧UUID到新UUID的映射关系 {old_uuid: new_uuid}
        """
        # dump() 只调用一次，复用给文件保存和 HTTP 请求
        nodes_info = [x for xs in resources.dump() for x in xs]
        old_uuids = {n.res_content.uuid: n for n in resources.all_nodes}
        payload = {"nodes": nodes_info, "mount_uuid": mount_uuid}
        body_bytes = _fast_dumps(payload)
        with open(
            os.path.join(BasicConfig.working_dir, "req_resource_tree_add.json"), "wb"
        ) as f:
            f.write(_fast_dumps_pretty(payload))
        http_headers = {"Content-Type": "application/json"}
        if not self.initialized or first_add:
            self.initialized = True
            info(f"首次添加资源，当前远程地址: {self.remote_addr}")
            response = self._session.post(
                f"{self.remote_addr}/edge/material",
                data=body_bytes,
                headers=http_headers,
                timeout=60,
            )
        else:
            response = self._session.put(
                f"{self.remote_addr}/edge/material",
                data=body_bytes,
                headers=http_headers,
                timeout=10,
            )

        with open(
            os.path.join(BasicConfig.working_dir, "res_resource_tree_add.json"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(f"{response.status_code}" + "\n" + response.text)
        # 处理响应，构建UUID映射
        uuid_mapping = {}
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"添加物料失败: {response.text}")
            else:
                data = res["data"]
                for i in data:
                    uuid_mapping[i["uuid"]] = i["cloud_uuid"]
        else:
            logger.error(f"添加物料失败: {response.text}")
            logger.trace(f"添加物料失败: {nodes_info}")
        for u, n in old_uuids.items():
            if u in uuid_mapping:
                n.res_content.uuid = uuid_mapping[u]
                for c in n.children:
                    c.res_content.parent_uuid = n.res_content.uuid
            else:
                logger.warning(f"资源UUID未更新: {u}")
        return uuid_mapping

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
        source = self._material_sources()[0]
        if source == "microbackend":
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

        response = self._session.post(
            f"{self.remote_addr}/edge/material/bench/discard",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=30,
        )
        with open(
            os.path.join(work_dir, "res_material_bench_discard.json"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(f"{response.status_code}" + "\n" + response.text)
        if response.status_code != 200:
            logger.error(f"台面物料废弃失败: {response.status_code}, {response.text}")
            return {"code": response.status_code, "message": response.text}
        res = response.json()
        if "code" in res and res["code"] != 0:
            logger.error(f"台面物料废弃失败: {response.text}")
        return res

    def resource_add(self, resources: List[Dict[str, Any]]) -> requests.Response:
        """
        添加资源

        Args:
            resources: 要添加的资源列表
        Returns:
            Response: API响应对象
        """
        if not self.initialized:
            self.initialized = True
            info(f"首次添加资源，当前远程地址: {self.remote_addr}")
            response = self._session.post(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        else:
            response = self._session.put(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"添加物料失败: {response.text}")
        if response.status_code != 200:
            logger.error(f"添加物料失败: {response.text}")
        return response

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
        # ``/lab/material`` historically returned data directly as a list;
        # retain that envelope for startup and third-party callers.
        return {"code": 0, "data": nodes}

    def resource_del(self, id: str) -> requests.Response:
        """
        删除资源

        Args:
            id: 要删除的资源ID

        Returns:
            Response: API响应对象
        """
        response = self._session.delete(
            f"{self.remote_addr}/lab/resource/batch_delete/",
            params={"id": id},
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=20,
        )
        return response

    def resource_update(self, resources: List[Dict[str, Any]]) -> requests.Response:
        """
        更新资源

        Args:
            resources: 要更新的资源列表

        Returns:
            Response: API响应对象
        """
        if not self.initialized:
            self.initialized = True
            info(f"首次添加资源，当前远程地址: {self.remote_addr}")
            response = self._session.post(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        else:
            response = self._session.put(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"添加物料失败: {response.text}")
        if response.status_code != 200:
            logger.error(f"添加物料失败: {response.text}")
        return response.json()

    def upload_file_to_oss(
        self, file_path: str, scene: str = "models"
    ) -> Tuple[str, str]:
        filename = os.path.basename(file_path)
        # 归档为 tar.gz；Content-Type 必须与签发 token 时一致，否则 OSS V1 验签 403
        content_type = "application/gzip"
        token_resp = self._session.get(
            f"{self.remote_addr}/lab/storage/token",
            params={"scene": scene, "filename": filename, "content_type": content_type},
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=30,
        )
        if token_resp.status_code != 200:
            raise RuntimeError(
                f"获取存储 token 失败：{token_resp.status_code} {token_resp.text}"
            )

        payload = token_resp.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
        put_url = str(data.get("url") or "")
        object_key = str(data.get("path") or "")
        public_url = str(data.get("public_url") or "")
        signed_content_type = str(data.get("content_type") or content_type)
        if not put_url:
            raise RuntimeError(f"存储 token 响应缺少预签名 url：{token_resp.text}")

        with open(file_path, "rb") as file:
            body = file.read()
        logger.info(f"预签名直传 OSS: {file_path} -> {object_key or public_url}")
        # 用裸 requests 直传，避免 session 默认的 Lab Authorization 头干扰 OSS URL 签名校验
        put_resp = requests.put(
            put_url,
            data=body,
            headers={"Content-Type": signed_content_type},
            timeout=120,
        )
        if put_resp.status_code not in (200, 201):
            raise RuntimeError(f"OSS 直传失败：{put_resp.status_code} {put_resp.text}")
        return public_url, object_key

    def resource_registry(
        self,
        registry_data: Dict[str, Any] | List[Dict[str, Any]],
        tag: str = "registry",
    ) -> requests.Response:
        """
        注册资源到服务器，同步保存请求/响应到 unilabos_data

        Args:
            registry_data: 注册表数据，格式为 {resource_id: resource_info} / [{resource_info}]
            tag: 保存文件的标签后缀 (如 "device_registry" / "resource_registry")

        Returns:
            Response: API响应对象
        """
        # 序列化一次，同时用于保存和发送
        json_bytes = _fast_dumps(registry_data)

        # 保存请求数据到 unilabos_data
        req_path = os.path.join(BasicConfig.working_dir, f"req_{tag}_upload.json")
        try:
            os.makedirs(BasicConfig.working_dir, exist_ok=True)
            with open(req_path, "wb") as f:
                f.write(_fast_dumps_pretty(registry_data))
            logger.trace(f"注册表请求数据已保存: {req_path}")
        except Exception as e:
            logger.warning(f"保存注册表请求数据失败: {e}")

        compressed_body = gzip.compress(json_bytes)
        headers = {
            "Authorization": f"Lab {self.auth}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        response = self._session.post(
            f"{self.remote_addr}/lab/resource",
            data=compressed_body,
            headers=headers,
            timeout=30,
        )

        # 保存响应数据到 unilabos_data
        res_path = os.path.join(BasicConfig.working_dir, f"res_{tag}_upload.json")
        try:
            with open(res_path, "w", encoding="utf-8") as f:
                f.write(f"{response.status_code}\n{response.text}")
            logger.trace(f"注册表响应数据已保存: {res_path}")
        except Exception as e:
            logger.warning(f"保存注册表响应数据失败: {e}")

        if response.status_code not in [200, 201]:
            logger.error(f"注册资源失败: {response.status_code}, {response.text}")
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"注册资源失败: {response.text}")
        return response

    def upload_package_resources(
        self,
        resources: List[Dict[str, Any]],
        package_info: Dict[str, Any],
    ) -> requests.Response:
        """
        上传社区设备包的 resources（带顶层 package_info）到 /lab/resource。

        与 resource_registry 同端点/同压缩方式，区别是请求体包一层
        {"package_info": <顶层>, "resources": [...]}，让后端 resolvePackageInfo
        将 package_info（含 class_namespace/download_url/sha256）落到每个设备模板。
        """
        body = {"package_info": package_info, "resources": resources}
        json_bytes = _fast_dumps(body)

        req_path = os.path.join(BasicConfig.working_dir, "req_package_upload.json")
        try:
            os.makedirs(BasicConfig.working_dir, exist_ok=True)
            with open(req_path, "wb") as f:
                f.write(_fast_dumps_pretty(body))
        except Exception as e:
            logger.warning(f"保存包上传请求数据失败: {e}")

        compressed_body = gzip.compress(json_bytes)
        headers = {
            "Authorization": f"Lab {self.auth}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        response = self._session.post(
            f"{self.remote_addr}/lab/resource",
            data=compressed_body,
            headers=headers,
            timeout=60,
        )

        res_path = os.path.join(BasicConfig.working_dir, "res_package_upload.json")
        try:
            with open(res_path, "w", encoding="utf-8") as f:
                f.write(f"{response.status_code}\n{response.text}")
        except Exception as e:
            logger.warning(f"保存包上传响应数据失败: {e}")

        if response.status_code not in [200, 201]:
            logger.error(f"上传社区设备包失败: {response.status_code}, {response.text}")
        return response

    def request_startup_json(self) -> Optional[Dict[str, Any]]:
        """
        请求启动配置

        Args:
            startup_json: 启动配置JSON数据

        Returns:
            Response: API响应对象
        """
        response = self._session.get(
            f"{self.remote_addr}/edge/material/download",
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=(3, 30),
        )
        if response.status_code != 200:
            logger.error(f"请求启动配置失败: {response.status_code}, {response.text}")
        else:
            try:
                with open(
                    os.path.join(BasicConfig.working_dir, "startup_config.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(response.text)
                target_dict = json.loads(response.text)
                if "data" in target_dict:
                    target_dict = target_dict["data"]
                return target_dict
            except json.JSONDecodeError as e:
                logger.error(
                    f"解析启动配置JSON失败: {str(e.args)}\n响应内容: {response.text}"
                )
                logger.error(f"响应内容: {response.text}")
        return None

    def resolve_community_packages(
        self,
        classes: List[str],
        current_packages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        根据 graph 中的 community.* class 解析需要加载的社区设备包。
        """
        payload = {
            "classes": classes,
            "machine_name": BasicConfig.machine_name,
            "current_packages": current_packages or [],
        }
        req_path = os.path.join(
            BasicConfig.working_dir, "req_community_package_resolve.json"
        )
        with open(req_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=4))
        response = self._session.post(
            f"{self.remote_addr}/lab/square/community-packages/resolve",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=(5, 30),
        )
        res_path = os.path.join(
            BasicConfig.working_dir, "res_community_package_resolve.json"
        )
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(f"{response.status_code}" + "\n" + response.text)
        response.raise_for_status()
        return response.json()

    def workflow_import(
        self,
        name: str,
        workflow_uuid: str,
        workflow_name: str,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        tags: Optional[List[str]] = None,
        published: bool = False,
        description: str = "",
    ) -> Dict[str, Any]:
        """
        导入工作流到服务器，如果 published 为 True，则额外发起发布请求

        Args:
            name: 工作流名称（顶层）
            workflow_uuid: 工作流UUID
            workflow_name: 工作流名称（data内部）
            nodes: 工作流节点列表
            edges: 工作流边列表
            tags: 工作流标签列表，默认为空列表
            published: 是否发布工作流，默认为False
            description: 工作流描述，发布时使用

        Returns:
            Dict: API响应数据，包含 code 和 data (uuid, name)
        """
        payload = {
            "name": name,
            "data": {
                "workflow_uuid": workflow_uuid,
                "workflow_name": workflow_name,
                "nodes": nodes,
                "edges": edges,
                "tags": tags if tags is not None else [],
            },
        }
        # 保存请求到文件
        with open(
            os.path.join(BasicConfig.working_dir, "req_workflow_upload.json"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(json.dumps(payload, indent=4, ensure_ascii=False))

        response = self._session.post(
            f"{self.remote_addr}/lab/workflow/owner/import",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=60,
        )
        # 保存响应到文件
        with open(
            os.path.join(BasicConfig.working_dir, "res_workflow_upload.json"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(f"{response.status_code}" + "\n" + response.text)

        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"导入工作流失败: {response.text}")
                return res
            # 导入成功后，如果需要发布则额外发起发布请求
            if published:
                imported_uuid = res.get("data", {}).get("uuid", workflow_uuid)
                publish_res = self.workflow_publish(imported_uuid, description)
                res["publish_result"] = publish_res
            return res
        else:
            logger.error(f"导入工作流失败: {response.status_code}, {response.text}")
            return {"code": response.status_code, "message": response.text}

    def workflow_publish(
        self, workflow_uuid: str, description: str = ""
    ) -> Dict[str, Any]:
        """
        发布工作流

        Args:
            workflow_uuid: 工作流UUID
            description: 工作流描述

        Returns:
            Dict: API响应数据
        """
        payload = {
            "uuid": workflow_uuid,
            "description": description,
            "published": True,
        }
        logger.info(f"正在发布工作流: {workflow_uuid}")
        response = self._session.patch(
            f"{self.remote_addr}/lab/workflow/owner",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=60,
        )
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"发布工作流失败: {response.text}")
            else:
                logger.info(f"工作流发布成功: {workflow_uuid}")
            return res
        else:
            logger.error(f"发布工作流失败: {response.status_code}, {response.text}")
            return {"code": response.status_code, "message": response.text}


# 创建默认客户端实例
http_client = HTTPClient()
