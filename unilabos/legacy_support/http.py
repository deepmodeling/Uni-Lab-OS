"""旧 Backend HTTP API 的受控兼容入口。"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

from unilabos.app.web.client import HTTPClient
from unilabos.config.config import BasicConfig
from unilabos.legacy_support import require_legacy_support
from unilabos.utils import logger
from unilabos.utils.tools import (
    fast_dumps as _fast_dumps,
    fast_dumps_pretty as _fast_dumps_pretty,
)


class LegacyHTTPClient(HTTPClient):
    """仅在 --legacy 下开放旧 /lab/* 和同步 API。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        require_legacy_support("legacy HTTP API")
        super().__init__(*args, **kwargs)

    def resource_get(self, id: str, with_children: bool = False) -> dict[str, Any]:
        response = self._session.get(
            f"{self.remote_addr}/lab/material",
            params={"id": id, "with_children": with_children},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def resource_tree_get(
        self, uuid_list: list[str], with_children: bool
    ) -> list[dict[str, Any]]:
        response = self._session.post(
            f"{self.remote_addr}/edge/material/query",
            json={"uuids": uuid_list, "with_children": with_children},
            timeout=30,
        )
        response.raise_for_status()
        return self._extract_material_nodes(response.json())

    def material_bench_discard(self, uuids: list[str]) -> dict[str, Any]:
        response = self._session.post(
            f"{self.remote_addr}/edge/material/bench/discard",
            json={"uuids": uuids},
            timeout=30,
        )
        response.raise_for_status()
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


    def report_inventory_command_result(self, response: object) -> None:
        from unilabos.server.scheduler.inventory.schemas import (
            CloudInventoryCommandResultRequest,
            InventoryCommandResult,
        )

        local = InventoryCommandResult.model_validate(response)
        request = CloudInventoryCommandResultRequest(
            command_id=local.command_id,
            status=local.status,
            result=local.result,
            error=local.error,
        )
        cloud_response = self._session.post(
            f"{self.remote_addr}/edge/inventory/command_result",
            json=request.model_dump(mode="json", exclude_none=True),
            timeout=15,
        )
        cloud_response.raise_for_status()


_legacy_http_client: Optional[LegacyHTTPClient] = None


def get_legacy_http_client() -> LegacyHTTPClient:
    global _legacy_http_client
    require_legacy_support("legacy HTTP API")
    if _legacy_http_client is None:
        _legacy_http_client = LegacyHTTPClient()
    return _legacy_http_client


def reset_legacy_http_client() -> None:
    global _legacy_http_client
    _legacy_http_client = None


def report_inventory_command_result(response: object) -> None:
    get_legacy_http_client().report_inventory_command_result(response)


def __getattr__(name: str) -> Any:
    if name == "http_client":
        return get_legacy_http_client()
    raise AttributeError(name)


__all__ = [
    "LegacyHTTPClient",
    "get_legacy_http_client",
    "report_inventory_command_result",
    "reset_legacy_http_client",
]
