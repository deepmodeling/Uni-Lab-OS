"""
HTTP客户端模块

提供与远程服务器通信的客户端功能，只有host需要用
"""
import gzip
import json
import os
from typing import List, Dict, Any, Optional

from unilabos.utils.tools import fast_dumps as _fast_dumps, fast_dumps_pretty as _fast_dumps_pretty

import requests
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.log import info
from unilabos.config.config import HTTPConfig, BasicConfig
from unilabos.utils import logger


class HTTPClient:
    """HTTP客户端，用于与远程服务器通信"""

    def __init__(self, remote_addr: Optional[str] = None, auth: Optional[str] = None) -> None:
        """
        初始化HTTP客户端

        Args:
            remote_addr: 远程服务器地址，如果不提供则从配置中获取
            auth: 授权信息
        """
        self.initialized = False
        self.remote_addr = remote_addr or HTTPConfig.remote_addr
        if auth is not None:
            self.auth = auth
        else:
            auth_secret = BasicConfig.auth_secret()
            self.auth = auth_secret
            info(f"正在使用ak sk作为授权信息：[{auth_secret}]")
        # 复用 TCP/TLS 连接，避免每次请求重新握手
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Lab {self.auth}"})
        info(f"HTTPClient 初始化完成: remote_addr={self.remote_addr}")

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

    def resource_tree_add(self, resources: ResourceTreeSet, mount_uuid: str, first_add: bool) -> Dict[str, str]:
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
        with open(os.path.join(BasicConfig.working_dir, "req_resource_tree_add.json"), "wb") as f:
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

        with open(os.path.join(BasicConfig.working_dir, "res_resource_tree_add.json"), "w", encoding="utf-8") as f:
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

    def resource_tree_get(self, uuid_list: List[str], with_children: bool) -> List[Dict[str, Any]]:
        """
        添加资源

        Args:
            uuid_list: List[str]
        Returns:
            Dict[str, str]: 旧UUID到新UUID的映射关系 {old_uuid: new_uuid}
        """
        with open(os.path.join(BasicConfig.working_dir, "req_resource_tree_get.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"uuids": uuid_list, "with_children": with_children}, indent=4))
        response = self._session.post(
            f"{self.remote_addr}/edge/material/query",
            json={"uuids": uuid_list, "with_children": with_children},
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=100,
        )
        with open(os.path.join(BasicConfig.working_dir, "res_resource_tree_get.json"), "w", encoding="utf-8") as f:
            f.write(f"{response.status_code}" + "\n" + response.text)
        if response.status_code == 200:
            res = response.json()
            if "code" in res and res["code"] != 0:
                logger.error(f"查询物料失败: {response.text}")
            else:
                data = res["data"]["nodes"]
                logger.trace(f"resource_tree_get查询到物料: {data}")
                return data
        else:
            logger.error(f"查询物料失败: {response.text}")
        return []

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
        with open(os.path.join(BasicConfig.working_dir, "req_resource_get.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": id, "with_children": with_children}, indent=4))
        response = self._session.get(
            f"{self.remote_addr}/lab/material",
            params={"id": id, "with_children": with_children},
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=20,
        )
        with open(os.path.join(BasicConfig.working_dir, "res_resource_get.json"), "w", encoding="utf-8") as f:
            f.write(f"{response.status_code}" + "\n" + response.text)
        return response.json()

    def resource_del(self, id: str) -> requests.Response:
        """
        删除资源

        Args:
            id: 要删除的资源ID

        Returns:
            Response: API响应对象
        """
        response = requests.delete(
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

    def upload_file(self, file_path: str, scene: str = "models") -> requests.Response:
        """
        上传文件到服务器

        使用multipart/form-data格式上传文件，类似curl -F "files=@filepath"

        Args:
            file_path: 要上传的文件路径
            scene: 上传场景，可选值为"user"或"models"，默认为"models"

        Returns:
            Response: API响应对象
        """
        with open(file_path, "rb") as file:
            files = {"files": file}
            logger.info(f"上传文件: {file_path} 到 {scene}")
            response = self._session.post(
                f"{self.remote_addr}/api/account/file_upload/{scene}",
                files=files,
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=30,  # 上传文件可能需要更长的超时时间
            )
        return response

    def resource_registry(
        self, registry_data: Dict[str, Any] | List[Dict[str, Any]], tag: str = "registry",
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
                with open(os.path.join(BasicConfig.working_dir, "startup_config.json"), "w", encoding="utf-8") as f:
                    f.write(response.text)
                target_dict = json.loads(response.text)
                if "data" in target_dict:
                    target_dict = target_dict["data"]
                return target_dict
            except json.JSONDecodeError as e:
                logger.error(f"解析启动配置JSON失败: {str(e.args)}\n响应内容: {response.text}")
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
        req_path = os.path.join(BasicConfig.working_dir, "req_community_package_resolve.json")
        with open(req_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=4))
        response = self._session.post(
            f"{self.remote_addr}/lab/square/community-packages/resolve",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=(5, 30),
        )
        res_path = os.path.join(BasicConfig.working_dir, "res_community_package_resolve.json")
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
        with open(os.path.join(BasicConfig.working_dir, "req_workflow_upload.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=4, ensure_ascii=False))

        response = self._session.post(
            f"{self.remote_addr}/lab/workflow/owner/import",
            json=payload,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=60,
        )
        # 保存响应到文件
        with open(os.path.join(BasicConfig.working_dir, "res_workflow_upload.json"), "w", encoding="utf-8") as f:
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

    def workflow_publish(self, workflow_uuid: str, description: str = "") -> Dict[str, Any]:
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
        response = requests.patch(
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
