"""
Web API Controller

提供Web API的控制器函数，处理设备、任务和动作相关的业务逻辑
"""

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

from unilabos.app.execution_adapter import get_execution_adapter
from unilabos.app.model import JobAddReq, JobData
from unilabos.utils import logger


def _job_execution_backend():
    try:
        from unilabos.app.scheduler.integration import get_edge_backend

        return get_edge_backend()
    except ImportError:
        return None


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
            logger.trace(f"[JobResultStore] Stored result for job {job_id[:8]}, status={status}")

    def get_and_remove(self, job_id: str) -> Optional[JobResult]:
        """获取并删除任务结果"""
        with self._results_lock:
            result = self._results.pop(job_id, None)
            if result:
                logger.trace(f"[JobResultStore] Retrieved and removed result for job {job_id[:8]}")
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
        "canceled": 5,  # CANCELED (canonical spelling)
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
    host_node = get_execution_adapter(0)
    if host_node is None:
        return False, "Host node not initialized"

    return True, host_node.resources_config


def devices() -> Tuple[bool, Any]:
    """获取设备配置

    Returns:
        Tuple[bool, Any]: (是否成功, 设备配置或错误信息)
    """
    host_node = get_execution_adapter(0)
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

    # 没有存储的结果，先从微后端读取 canonical job 状态。
    microbackend = _job_execution_backend()
    if microbackend is not None:
        active = microbackend.device_manager.get_job_info(job_id)
        if active is not None:
            return JobData(jobId=job_id, status=2)
    host_node = get_execution_adapter(0)
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
    microbackend = _job_execution_backend()
    if microbackend is None:
        return False, None

    device_action_key = f"/devices/{device_id}/{action_name}"
    for job in microbackend.device_manager.get_active_jobs():
        if job.device_action_key == device_action_key:
            return True, job.job_id

    return False, None


def _get_action_type(device_id: str, action_name: str) -> Optional[str]:
    """从注册表自动获取动作类型

    Args:
        device_id: 设备ID
        action_name: 动作名称

    Returns:
        动作类型字符串，未找到返回None
    """
    host_node = get_execution_adapter(0)
    if host_node is None:
        return None
    mappings = host_node._action_value_mappings.get(device_id, {})
    for key in (action_name, f"auto-{action_name}"):
        mapping = mappings.get(key)
        if not isinstance(mapping, dict):
            continue
        action_type = mapping.get("type")
        if action_type:
            if hasattr(action_type, "__module__") and hasattr(
                action_type,
                "__name__",
            ):
                return f"{action_type.__module__}.{action_type.__name__}"
            return str(action_type)
    return None


def job_add(req: JobAddReq) -> JobData:
    """拒绝 Edge 本地执行；所有动作必须由调度后端通过 ``job_start`` 下发。"""

    job_id = str(req.job_id or "")
    logger.warning(
        "[Controller] Local job execution is disabled; submit the workflow to "
        "the scheduler backend"
    )
    return JobData(jobId=job_id, status=6)


def get_pending_action_error_decisions() -> Tuple[bool, Dict[str, Any]]:
    """只读查看微后端正在等待 Backend release 的设备失败。"""

    microbackend = _job_execution_backend()
    if microbackend is None:
        return False, {"error": "Job execution microbackend not initialized"}
    return True, {"decisions": microbackend.get_pending_action_error_decisions()}


def submit_action_error_decision(
    decision_id: str,
    decision: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    """拒绝 Edge 本地决策；唯一写入口是调度后端的 WebSocket release。"""

    del decision_id, decision
    return False, {
        "error": (
            "action error decisions must be submitted to the scheduler backend; "
            "Host accepts only a scheduler-updated WebSocket release"
        ),
        "error_code": "decision_backend_authority",
    }


def get_online_devices() -> Tuple[bool, Dict[str, Any]]:
    """获取在线设备列表

    Returns:
        Tuple[bool, Dict]: (是否成功, 在线设备信息)
    """
    host_node = get_execution_adapter(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    descriptors = getattr(host_node, "_device_descriptors", {})
    online_devices = {}
    for device_key in host_node._online_devices:
        device_id = device_key.rsplit("/", 1)[-1]
        descriptor = descriptors.get(device_id, {})
        online_devices[device_id] = {
            "device_key": device_key,
            "namespace": host_node.devices_names.get(device_id, ""),
            "machine_name": host_node.device_machine_names.get(
                device_id,
                "未知",
            ),
            "uuid": descriptor.get("resource_uuid", ""),
            "node_name": descriptor.get("registry_name", ""),
        }
    return True, {
        "online_devices": online_devices,
        "total_count": len(online_devices),
        "timestamp": time.time(),
    }


def get_device_actions(device_id: str) -> Tuple[bool, Dict[str, Any]]:
    """获取设备可用的动作列表

    Args:
        device_id: 设备ID

    Returns:
        Tuple[bool, Dict]: (是否成功, 动作列表信息)
    """
    host_node = get_execution_adapter(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    mappings = host_node._action_value_mappings.get(device_id)
    if mappings is None:
        return False, {"error": f"Device not found: {device_id}"}
    actions_list = {}
    for action_name, mapping in mappings.items():
        if action_name.startswith("_execute_driver_command"):
            continue
        is_busy, current_job = check_device_action_busy(device_id, action_name)
        actions_list[action_name] = {
            "type_name": str(mapping.get("type", "")),
            "action_path": f"/devices/{device_id}/{action_name}",
            "schema": mapping.get("schema"),
            "handles": mapping.get("handles", {}),
            "placeholder_keys": mapping.get("placeholder_keys", {}),
            "error_policy": mapping.get("error_policy", {}),
            "node_type": mapping.get("node_type"),
            "feedback_interval": mapping.get("feedback_interval"),
            "supported_backends": mapping.get("supported_backends"),
            "is_busy": is_busy,
            "current_job_id": current_job[:8] if current_job else None,
        }
    return True, {
        "device_id": device_id,
        "actions": actions_list,
        "action_count": len(actions_list),
    }


def get_action_schema(device_id: str, action_name: str) -> Tuple[bool, Dict[str, Any]]:
    """获取动作的Schema详情

    Args:
        device_id: 设备ID
        action_name: 动作名称

    Returns:
        Tuple[bool, Dict]: (是否成功, Schema信息)
    """
    host_node = get_execution_adapter(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    try:
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

        action_mappings = host_node._action_value_mappings.get(device_id, {})
        mapping = action_mappings.get(action_name) or action_mappings.get(
            f"auto-{action_name}"
        )
        if isinstance(mapping, dict):
            result["schema"] = mapping.get("schema")
            result["goal_default"] = mapping.get("goal_default")
            result["action_type"] = str(mapping.get("type", ""))
            result["handles"] = mapping.get("handles", {})
            result["placeholder_keys"] = mapping.get("placeholder_keys", {})
            result["error_policy"] = mapping.get("error_policy", {})
            result["node_type"] = mapping.get("node_type")
            result["feedback_interval"] = mapping.get("feedback_interval")
            result["supported_backends"] = mapping.get("supported_backends")

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
    host_node = get_execution_adapter(0)
    if host_node is None:
        return False, {"error": "Host node not initialized"}

    all_actions = {}
    total_action_count = 0
    for device_id, mappings in host_node._action_value_mappings.items():
        ok, payload = get_device_actions(device_id)
        if not ok or not payload.get("actions"):
            continue
        actions = payload["actions"]
        total_action_count += len(actions)
        all_actions[device_id] = {
            "actions": actions,
            "action_count": len(actions),
            "machine_name": host_node.device_machine_names.get(
                device_id,
                "未知",
            ),
        }
    return True, {
        "devices": all_actions,
        "device_count": len(all_actions),
        "total_action_count": total_action_count,
        "timestamp": time.time(),
    }
