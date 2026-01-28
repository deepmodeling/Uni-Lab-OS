"""
Web API Controller

提供Web API的控制器函数，处理设备、任务和动作相关的业务逻辑
"""

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

from unilabos.app.model import JobAddReq, JobData
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.utils import logger


@dataclass
class JobResult:
    """任务结果数据"""

    job_id: str
    status: int  # 4:SUCCEEDED, 5:CANCELED, 6:ABORTED
    result: Dict[str, Any] = field(default_factory=dict)
    feedback: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class JobResultStore:
    """任务结果存储（单例）"""

    _instance: Optional["JobResultStore"] = None
    _lock = threading.Lock()

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self._results: Dict[str, JobResult] = {}
            self._results_lock = threading.RLock()
            self._initialized = True

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def store_result(
        self, job_id: str, status: int, result: Optional[Dict[str, Any]], feedback: Optional[Dict[str, Any]] = None
    ):
        """存储任务结果"""
        with self._results_lock:
            self._results[job_id] = JobResult(
                job_id=job_id,
                status=status,
                result=result or {},
                feedback=feedback or {},
                timestamp=time.time(),
            )
            logger.debug(f"[JobResultStore] Stored result for job {job_id[:8]}, status={status}")

    def get_and_remove(self, job_id: str) -> Optional[JobResult]:
        """获取并删除任务结果"""
        with self._results_lock:
            result = self._results.pop(job_id, None)
            if result:
                logger.debug(f"[JobResultStore] Retrieved and removed result for job {job_id[:8]}")
            return result

    def get_result(self, job_id: str) -> Optional[JobResult]:
        """仅获取任务结果（不删除）"""
        with self._results_lock:
            return self._results.get(job_id)

    def cleanup_old_results(self, max_age_seconds: float = 3600):
        """清理过期的结果"""
        current_time = time.time()
        with self._results_lock:
            expired_jobs = [
                job_id for job_id, result in self._results.items() if current_time - result.timestamp > max_age_seconds
            ]
            for job_id in expired_jobs:
                del self._results[job_id]
                logger.debug(f"[JobResultStore] Cleaned up expired result for job {job_id[:8]}")


# 全局结果存储实例
job_result_store = JobResultStore()


def store_job_result(
    job_id: str, status: str, result: Optional[Dict[str, Any]], feedback: Optional[Dict[str, Any]] = None
):
    """存储任务结果（供外部调用）

    Args:
        job_id: 任务ID
        status: 状态字符串 ("success", "failed", "cancelled")
        result: 结果数据
        feedback: 反馈数据
    """
    # 转换状态字符串为整数
    status_map = {
        "success": 4,  # SUCCEEDED
        "failed": 6,  # ABORTED
        "cancelled": 5,  # CANCELED
        "running": 2,  # EXECUTING
    }
    status_int = status_map.get(status, 0)

    # 只存储最终状态
    if status_int in (4, 5, 6):
        job_result_store.store_result(job_id, status_int, result, feedback)


def get_resources() -> Tuple[bool, Any]:
    """获取资源配置

    Returns:
        Tuple[bool, Any]: (是否成功, 资源配置或错误信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, "Host node not initialized"

    return True, host_node.resources_config


def devices() -> Tuple[bool, Any]:
    """获取设备配置

    Returns:
        Tuple[bool, Any]: (是否成功, 设备配置或错误信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, "Host node not initialized"

    return True, host_node.devices_config


def job_info(job_id: str, remove_after_read: bool = True) -> JobData:
    """获取任务信息

    Args:
        job_id: 任务ID
        remove_after_read: 是否在读取后删除结果（默认True）

    Returns:
        JobData: 任务数据
    """
    # 首先检查结果存储中是否有已完成的结果
    if remove_after_read:
        stored_result = job_result_store.get_and_remove(job_id)
    else:
        stored_result = job_result_store.get_result(job_id)

    if stored_result:
        # 有存储的结果，直接返回
        return JobData(
            jobId=job_id,
            status=stored_result.status,
            result=stored_result.result,
        )

    # 没有存储的结果，从 HostNode 获取当前状态
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return JobData(jobId=job_id, status=0)

    get_goal_status = host_node.get_goal_status(job_id)
    return JobData(jobId=job_id, status=get_goal_status)


def check_device_action_busy(device_id: str, action_name: str) -> Tuple[bool, Optional[str]]:
    """检查设备动作是否正在执行（被占用）

    Args:
        device_id: 设备ID
        action_name: 动作名称

    Returns:
        Tuple[bool, Optional[str]]: (是否繁忙, 当前执行的job_id或None)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, None

    device_action_key = f"/devices/{device_id}/{action_name}"

    # 检查 _device_action_status 中是否有正在执行的任务
    if device_action_key in host_node._device_action_status:
        status = host_node._device_action_status[device_action_key]
        if status.job_ids:
            # 返回第一个正在执行的job_id
            current_job_id = next(iter(status.job_ids.keys()), None)
            return True, current_job_id

    return False, None


def _get_action_type(device_id: str, action_name: str) -> Optional[str]:
    """从注册表自动获取动作类型

    Args:
        device_id: 设备ID
        action_name: 动作名称

    Returns:
        动作类型字符串，未找到返回None
    """
    try:
        from unilabos.ros.nodes.base_device_node import registered_devices

        # 方法1: 从运行时注册设备获取
        if device_id in registered_devices:
            device_info = registered_devices[device_id]
            base_node = device_info.get("base_node_instance")
            if base_node and hasattr(base_node, "_action_value_mappings"):
                action_mappings = base_node._action_value_mappings
                # 尝试直接匹配或 auto- 前缀匹配
                for key in [action_name, f"auto-{action_name}"]:
                    if key in action_mappings:
                        action_type = action_mappings[key].get("type")
                        if action_type:
                            # 转换为字符串格式
                            if hasattr(action_type, "__module__") and hasattr(action_type, "__name__"):
                                return f"{action_type.__module__}.{action_type.__name__}"
                            return str(action_type)

        # 方法2: 从lab_registry获取
        from unilabos.registry.registry import lab_registry

        host_node = HostNode.get_instance(0)
        if host_node and lab_registry:
            devices_config = host_node.devices_config
            device_class = None

            for tree in devices_config.trees:
                node = tree.root_node
                if node.res_content.id == device_id:
                    device_class = node.res_content.klass
                    break

            if device_class and device_class in lab_registry.device_type_registry:
                device_type_info = lab_registry.device_type_registry[device_class]
                class_info = device_type_info.get("class", {})
                action_mappings = class_info.get("action_value_mappings", {})

                for key in [action_name, f"auto-{action_name}"]:
                    if key in action_mappings:
                        action_type = action_mappings[key].get("type")
                        if action_type:
                            if hasattr(action_type, "__module__") and hasattr(action_type, "__name__"):
                                return f"{action_type.__module__}.{action_type.__name__}"
                            return str(action_type)

    except Exception as e:
        logger.warning(f"[Controller] Failed to get action type for {device_id}/{action_name}: {str(e)}")

    return None


def job_add(req: JobAddReq) -> JobData:
    """添加任务（检查设备是否繁忙，繁忙则返回失败）

    Args:
        req: 任务添加请求

    Returns:
        JobData: 任务数据（包含状态）
    """
    # 服务端自动生成 job_id 和 task_id
    job_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    # 服务端自动生成 server_info
    server_info = {"send_timestamp": time.time()}

    host_node = HostNode.get_instance(0)
    if host_node is None:
        logger.error(f"[Controller] Host node not initialized for job: {job_id[:8]}")
        return JobData(jobId=job_id, status=6)  # 6 = ABORTED

    # 解析动作信息
    action_name = req.data.get("action", req.action) if req.data else req.action
    action_args = req.data.get("action_kwargs") or req.data.get("action_args") if req.data else req.action_args

    if action_args is None:
        action_args = req.action_args or {}
    elif isinstance(action_args, dict) and "command" in action_args:
        action_args = action_args["command"]

    # 自动获取 action_type
    action_type = _get_action_type(req.device_id, action_name)
    if action_type is None:
        logger.error(f"[Controller] Action type not found for {req.device_id}/{action_name}")
        return JobData(jobId=job_id, status=6)  # ABORTED

    # 检查设备动作是否繁忙
    is_busy, current_job_id = check_device_action_busy(req.device_id, action_name)

    if is_busy:
        logger.warning(
            f"[Controller] Device action busy: {req.device_id}/{action_name}, "
            f"current job: {current_job_id[:8] if current_job_id else 'unknown'}"
        )
        # 返回失败状态，status=6 表示 ABORTED
        return JobData(jobId=job_id, status=6)

    # 设备空闲，提交任务执行
    try:
        from unilabos.app.ws_client import QueueItem

        device_action_key = f"/devices/{req.device_id}/{action_name}"
        queue_item = QueueItem(
            task_type="job_call_back_status",
            device_id=req.device_id,
            action_name=action_name,
            task_id=task_id,
            job_id=job_id,
            device_action_key=device_action_key,
        )

        host_node.send_goal(
            queue_item,
            action_type=action_type,
            action_kwargs=action_args,
            server_info=server_info,
        )

        logger.info(f"[Controller] Job submitted: {job_id[:8]} -> {req.device_id}/{action_name}")
        # 返回已接受状态，status=1 表示 ACCEPTED
        return JobData(jobId=job_id, status=1)

    except ValueError as e:
        # ActionClient not found 等错误
        logger.error(f"[Controller] Action not available: {str(e)}")
        return JobData(jobId=job_id, status=6)  # ABORTED

    except Exception as e:
        logger.error(f"[Controller] Error submitting job: {str(e)}")
        traceback.print_exc()
        return JobData(jobId=job_id, status=6)  # ABORTED


def get_online_devices() -> Tuple[bool, Dict[str, Any]]:
    """获取在线设备列表

    Returns:
        Tuple[bool, Dict]: (是否成功, 在线设备信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    try:
        from unilabos.ros.nodes.base_device_node import registered_devices

        online_devices = {}
        for device_key in host_node._online_devices:
            # device_key 格式: "namespace/device_id"
            parts = device_key.split("/")
            if len(parts) >= 2:
                device_id = parts[-1]
            else:
                device_id = device_key

            # 获取设备详细信息
            device_info = registered_devices.get(device_id, {})
            machine_name = host_node.device_machine_names.get(device_id, "未知")

            online_devices[device_id] = {
                "device_key": device_key,
                "namespace": host_node.devices_names.get(device_id, ""),
                "machine_name": machine_name,
                "uuid": device_info.get("uuid", "") if device_info else "",
                "node_name": device_info.get("node_name", "") if device_info else "",
            }

        return True, {
            "online_devices": online_devices,
            "total_count": len(online_devices),
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[Controller] Error getting online devices: {str(e)}")
        traceback.print_exc()
        return False, {"error": str(e)}


def get_device_actions(device_id: str) -> Tuple[bool, Dict[str, Any]]:
    """获取设备可用的动作列表

    Args:
        device_id: 设备ID

    Returns:
        Tuple[bool, Dict]: (是否成功, 动作列表信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    try:
        from unilabos.ros.nodes.base_device_node import registered_devices
        from unilabos.app.web.utils.action_utils import get_action_info

        # 检查设备是否已注册
        if device_id not in registered_devices:
            return False, {"error": f"Device not found: {device_id}"}

        device_info = registered_devices[device_id]
        actions = device_info.get("actions", {})

        actions_list = {}
        for action_name, action_server in actions.items():
            try:
                action_info = get_action_info(action_server, action_name)
                # 检查动作是否繁忙
                is_busy, current_job = check_device_action_busy(device_id, action_name)
                actions_list[action_name] = {
                    **action_info,
                    "is_busy": is_busy,
                    "current_job_id": current_job[:8] if current_job else None,
                }
            except Exception as e:
                logger.warning(f"[Controller] Error getting action info for {action_name}: {str(e)}")
                actions_list[action_name] = {
                    "type_name": "unknown",
                    "action_path": f"/devices/{device_id}/{action_name}",
                    "is_busy": False,
                    "error": str(e),
                }

        return True, {
            "device_id": device_id,
            "actions": actions_list,
            "action_count": len(actions_list),
        }

    except Exception as e:
        logger.error(f"[Controller] Error getting device actions: {str(e)}")
        traceback.print_exc()
        return False, {"error": str(e)}


def get_action_schema(device_id: str, action_name: str) -> Tuple[bool, Dict[str, Any]]:
    """获取动作的Schema详情

    Args:
        device_id: 设备ID
        action_name: 动作名称

    Returns:
        Tuple[bool, Dict]: (是否成功, Schema信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    try:
        from unilabos.registry.registry import lab_registry
        from unilabos.ros.nodes.base_device_node import registered_devices

        result = {
            "device_id": device_id,
            "action_name": action_name,
            "schema": None,
            "goal_default": None,
            "action_type": None,
            "is_busy": False,
        }

        # 检查动作是否繁忙
        is_busy, current_job = check_device_action_busy(device_id, action_name)
        result["is_busy"] = is_busy
        result["current_job_id"] = current_job[:8] if current_job else None

        # 方法1: 从 registered_devices 获取运行时信息
        if device_id in registered_devices:
            device_info = registered_devices[device_id]
            base_node = device_info.get("base_node_instance")

            if base_node and hasattr(base_node, "_action_value_mappings"):
                action_mappings = base_node._action_value_mappings
                if action_name in action_mappings:
                    mapping = action_mappings[action_name]
                    result["schema"] = mapping.get("schema")
                    result["goal_default"] = mapping.get("goal_default")
                    result["action_type"] = str(mapping.get("type", ""))

        # 方法2: 从 lab_registry 获取注册表信息（如果运行时没有）
        if result["schema"] is None and lab_registry:
            # 尝试查找设备类型
            devices_config = host_node.devices_config
            device_class = None

            # 从配置中获取设备类型
            for tree in devices_config.trees:
                node = tree.root_node
                if node.res_content.id == device_id:
                    device_class = node.res_content.klass
                    break

            if device_class and device_class in lab_registry.device_type_registry:
                device_type_info = lab_registry.device_type_registry[device_class]
                class_info = device_type_info.get("class", {})
                action_mappings = class_info.get("action_value_mappings", {})

                # 尝试直接匹配或 auto- 前缀匹配
                for key in [action_name, f"auto-{action_name}"]:
                    if key in action_mappings:
                        mapping = action_mappings[key]
                        result["schema"] = mapping.get("schema")
                        result["goal_default"] = mapping.get("goal_default")
                        result["action_type"] = str(mapping.get("type", ""))
                        result["handles"] = mapping.get("handles", {})
                        result["placeholder_keys"] = mapping.get("placeholder_keys", {})
                        break

        if result["schema"] is None:
            return False, {"error": f"Action schema not found: {device_id}/{action_name}"}

        return True, result

    except Exception as e:
        logger.error(f"[Controller] Error getting action schema: {str(e)}")
        traceback.print_exc()
        return False, {"error": str(e)}


def get_all_available_actions() -> Tuple[bool, Dict[str, Any]]:
    """获取所有设备的可用动作

    Returns:
        Tuple[bool, Dict]: (是否成功, 所有设备的动作信息)
    """
    host_node = HostNode.get_instance(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    try:
        from unilabos.ros.nodes.base_device_node import registered_devices
        from unilabos.app.web.utils.action_utils import get_action_info

        all_actions = {}
        total_action_count = 0

        for device_id, device_info in registered_devices.items():
            actions = device_info.get("actions", {})
            device_actions = {}

            for action_name, action_server in actions.items():
                try:
                    action_info = get_action_info(action_server, action_name)
                    is_busy, current_job = check_device_action_busy(device_id, action_name)
                    device_actions[action_name] = {
                        "type_name": action_info.get("type_name", ""),
                        "action_path": action_info.get("action_path", ""),
                        "is_busy": is_busy,
                        "current_job_id": current_job[:8] if current_job else None,
                    }
                    total_action_count += 1
                except Exception as e:
                    logger.warning(f"[Controller] Error processing action {device_id}/{action_name}: {str(e)}")

            if device_actions:
                all_actions[device_id] = {
                    "actions": device_actions,
                    "action_count": len(device_actions),
                    "machine_name": host_node.device_machine_names.get(device_id, "未知"),
                }

        return True, {
            "devices": all_actions,
            "device_count": len(all_actions),
            "total_action_count": total_action_count,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[Controller] Error getting all available actions: {str(e)}")
        traceback.print_exc()
        return False, {"error": str(e)}
