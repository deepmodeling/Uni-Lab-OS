"""
HTTP客户端模块

提供与远程服务器通信的客户端功能，只有host需要用
"""

import json
import os
import time
from threading import Thread
from typing import List, Dict, Any, Optional

import requests
from unilabos.ros.nodes.resource_tracker import ResourceTreeSet
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
        response = requests.post(
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
        with open(os.path.join(BasicConfig.working_dir, "req_resource_tree_add.json"), "w", encoding="utf-8") as f:
            payload = {"nodes": [x for xs in resources.dump() for x in xs], "mount_uuid": mount_uuid}
            f.write(json.dumps(payload, indent=4))
        # 从序列化数据中提取所有节点的UUID（保存旧UUID）
        old_uuids = {n.res_content.uuid: n for n in resources.all_nodes}
        if not self.initialized or first_add:
            self.initialized = True
            info(f"首次添加资源，当前远程地址: {self.remote_addr}")
            response = requests.post(
                f"{self.remote_addr}/edge/material",
                json={"nodes": [x for xs in resources.dump() for x in xs], "mount_uuid": mount_uuid},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=60,
            )
        else:
            response = requests.put(
                f"{self.remote_addr}/edge/material",
                json={"nodes": [x for xs in resources.dump() for x in xs], "mount_uuid": mount_uuid},
                headers={"Authorization": f"Lab {self.auth}"},
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
        response = requests.post(
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
            response = requests.post(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        else:
            response = requests.put(
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
        response = requests.get(
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
            response = requests.post(
                f"{self.remote_addr}/lab/material",
                json={"nodes": resources},
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=100,
            )
        else:
            response = requests.put(
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
            response = requests.post(
                f"{self.remote_addr}/api/account/file_upload/{scene}",
                files=files,
                headers={"Authorization": f"Lab {self.auth}"},
                timeout=30,  # 上传文件可能需要更长的超时时间
            )
        return response

    def resource_registry(self, registry_data: Dict[str, Any] | List[Dict[str, Any]]) -> requests.Response:
        """
        注册资源到服务器

        Args:
            registry_data: 注册表数据，格式为 {resource_id: resource_info} / [{resource_info}]

        Returns:
            Response: API响应对象
        """
        response = requests.post(
            f"{self.remote_addr}/lab/resource",
            json=registry_data,
            headers={"Authorization": f"Lab {self.auth}"},
            timeout=30,
        )
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
        response = requests.get(
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

    def workflow_import(
        self,
        name: str,
        workflow_uuid: str,
        workflow_name: str,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        tags: Optional[List[str]] = None,
        published: bool = False,
    ) -> Dict[str, Any]:
        """
        导入工作流到服务器

        Args:
            name: 工作流名称（顶层）
            workflow_uuid: 工作流UUID
            workflow_name: 工作流名称（data内部）
            nodes: 工作流节点列表
            edges: 工作流边列表
            tags: 工作流标签列表，默认为空列表
            published: 是否发布工作流，默认为False

        Returns:
            Dict: API响应数据，包含 code 和 data (uuid, name)
        """
        # target_lab_uuid 暂时使用默认值，后续由后端根据 ak/sk 获取
        payload = {
            "target_lab_uuid": "28c38bb0-63f6-4352-b0d8-b5b8eb1766d5",
            "name": name,
            "data": {
                "workflow_uuid": workflow_uuid,
                "workflow_name": workflow_name,
                "nodes": nodes,
                "edges": edges,
                "tags": tags if tags is not None else [],
                "published": published,
            },
        }
        # 保存请求到文件
        with open(os.path.join(BasicConfig.working_dir, "req_workflow_upload.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=4, ensure_ascii=False))

        response = requests.post(
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
        else:
            logger.error(f"导入工作流失败: {response.status_code}, {response.text}")
            return {"code": response.status_code, "message": response.text}


# 创建默认客户端实例
http_client = HTTPClient()
