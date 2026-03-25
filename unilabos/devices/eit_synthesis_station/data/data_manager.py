"""
数据管理模块 - 负责保存和加载工站运行数据

功能:
    - 保存设备状态、物料信息、任务数据等快照
    - 管理任务生命周期数据
    - 记录操作日志
    - 提供数据查询接口
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid


class DataManager:
    """数据管理器 - 负责所有数据的持久化和查询"""

    def __init__(self, data_dir: Optional[Path] = None):
        """
        初始化数据管理器

        参数:
            data_dir: 数据存储根目录，默认为当前目录下的 data/
        """
        if data_dir is None:
            data_dir = Path(__file__).parent.parent / "data"

        self.data_dir = Path(data_dir)
        self.snapshots_dir = self.data_dir / "snapshots"
        self.tasks_dir = self.data_dir / "tasks"
        self.operations_dir = self.data_dir / "operations"

        self._logger = logging.getLogger(self.__class__.__name__)

        # 确保目录存在
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """确保所有必要的目录存在"""
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.operations_dir.mkdir(parents=True, exist_ok=True)

    def _save_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """
        保存 JSON 数据到文件

        参数:
            file_path: 文件路径
            data: 要保存的数据
        """
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._logger.debug(f"数据已保存到 {file_path}")
        except Exception as e:
            self._logger.error(f"保存数据到 {file_path} 失败: {e}")

    def _load_json(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """
        从文件加载 JSON 数据

        参数:
            file_path: 文件路径

        返回:
            加载的数据，如果文件不存在则返回 None
        """
        try:
            if not file_path.exists():
                return None
            with file_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._logger.error(f"加载数据从 {file_path} 失败: {e}")
            return None

    # ==================== 快照管理 ====================

    def save_device_status(self, data: Dict[str, Any]) -> None:
        """
        保存设备状态快照

        参数:
            data: 设备状态数据，格式:
                {
                    "timestamp": "2026-01-20T14:30:00Z",
                    "devices": [{"device_name": "ARM", "status": "AVAILABLE", ...}]
                }
        """
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        file_path = self.snapshots_dir / "device_status.json"
        self._save_json(file_path, data)

    def save_station_state(self, data: Dict[str, Any]) -> None:
        """
        保存工站状态快照

        参数:
            data: 工站状态数据，格式:
                {
                    "timestamp": "2026-01-20T14:30:00Z",
                    "state": "IDLE",
                    "state_code": 0
                }
        """
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        file_path = self.snapshots_dir / "station_state.json"
        self._save_json(file_path, data)

    def save_glovebox_env(self, data: Dict[str, Any]) -> None:
        """
        保存手套箱环境快照

        参数:
            data: 手套箱环境数据，格式:
                {
                    "timestamp": "2026-01-20T14:30:00Z",
                    "oxygen_ppm": 10.5,
                    "humidity_ppm": 5.2,
                    "pressure_pa": 50.0
                }
        """
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        file_path = self.snapshots_dir / "glovebox_env.json"
        self._save_json(file_path, data)

    def save_resource_info(self, data: Dict[str, Any]) -> None:
        """
        保存物料资源快照

        参数:
            data: 物料资源数据，格式:
                {
                    "timestamp": "2026-01-20T14:30:00Z",
                    "resources": [{"layout_code": "W-1-1", ...}]
                }
        """
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        file_path = self.snapshots_dir / "resource_info.json"
        self._save_json(file_path, data)

    def load_snapshot(self, snapshot_type: str) -> Optional[Dict[str, Any]]:
        """
        加载最新快照

        参数:
            snapshot_type: 快照类型，可选值:
                - "device_status": 设备状态
                - "station_state": 工站状态
                - "glovebox_env": 手套箱环境
                - "resource_info": 物料资源

        返回:
            快照数据，如果不存在则返回 None
        """
        file_path = self.snapshots_dir / f"{snapshot_type}.json"
        return self._load_json(file_path)

    # ==================== 任务数据管理 ====================

    def create_task_record(self, task_id: str, task_info: Dict[str, Any]) -> None:
        """
        创建任务记录

        参数:
            task_id: 任务ID
            task_info: 任务信息，格式:
                {
                    "task_id": "abc123",
                    "experiment_name": "Suzuki偶联反应",
                    "created_at": "2026-01-20T10:00:00Z",
                    "status": "UNSTARTED",
                    ...
                }
        """
        if "created_at" not in task_info:
            task_info["created_at"] = datetime.now().isoformat()

        if "task_id" not in task_info:
            task_info["task_id"] = task_id

        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        file_path = task_dir / "task_info.json"
        self._save_json(file_path, task_info)

    def update_task_status(self, task_id: str, status: str, **kwargs) -> None:
        """
        更新任务状态

        参数:
            task_id: 任务ID
            status: 新状态
            **kwargs: 其他要更新的字段，如 started_at, completed_at 等
        """
        task_dir = self.tasks_dir / task_id
        file_path = task_dir / "task_info.json"

        # 加载现有数据
        task_info = self._load_json(file_path)
        if task_info is None:
            self._logger.warning(f"任务 {task_id} 不存在，创建新记录")
            task_info = {"task_id": task_id}

        # 更新状态
        task_info["status"] = status

        # 更新时间戳
        if status == "RUNNING" and "started_at" not in task_info:
            task_info["started_at"] = datetime.now().isoformat()
        elif status in ["COMPLETED", "FAILED", "STOPPED"] and "completed_at" not in task_info:
            task_info["completed_at"] = datetime.now().isoformat()

        # 更新其他字段
        task_info.update(kwargs)

        # 保存
        self._save_json(file_path, task_info)

    def save_task_payload(self, task_id: str, payload: Dict[str, Any]) -> None:
        """
        保存任务 Payload

        参数:
            task_id: 任务ID
            payload: 任务Payload数据
        """
        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        file_path = task_dir / "task_payload.json"
        self._save_json(file_path, payload)

    def save_resource_check(self, task_id: str, check_result: Dict[str, Any]) -> None:
        """
        保存物料核算结果

        参数:
            task_id: 任务ID
            check_result: 物料核算结果，格式:
                {
                    "timestamp": "2026-01-20T09:55:00Z",
                    "task_id": "abc123",
                    "check_result": "PASS",
                    "required_materials": [...],
                    "missing_materials": [],
                    ...
                }
        """
        if "timestamp" not in check_result:
            check_result["timestamp"] = datetime.now().isoformat()

        if "task_id" not in check_result:
            check_result["task_id"] = task_id

        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        file_path = task_dir / "resource_check.json"
        self._save_json(file_path, check_result)

    def save_unload_info(self, task_id: str, unload_data: Dict[str, Any]) -> None:
        """
        保存下料信息

        参数:
            task_id: 任务ID
            unload_data: 下料数据，格式:
                {
                    "task_id": "abc123",
                    "unload_time": "2026-01-20T12:35:00Z",
                    "unloaded_trays": [...],
                    "empty_trays_unloaded": [...],
                    ...
                }
        """
        if "unload_time" not in unload_data:
            unload_data["unload_time"] = datetime.now().isoformat()

        if "task_id" not in unload_data:
            unload_data["task_id"] = task_id

        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        file_path = task_dir / "unload_info.json"
        self._save_json(file_path, unload_data)

    def save_batch_in_tray_log(self, log_data: Dict[str, Any]) -> None:
        """
        保存上料日志

        参数:
            log_data: 上料日志数据，格式:
                {
                    "start_time": "2026-01-20T12:00:00",
                    "end_time": "2026-01-20T12:05:00",
                    "resources": [
                        {
                            "layout_code": "W-1-1",
                            "count": 2,
                            "resource_type": 220000023,
                            "resource_type_name": "125 mL试剂瓶托盘",
                            "substance_details": [...],
                            "task_id": None
                        }
                    ]
                }
        """
        file_path = self.operations_dir / "batch_in_tray.json"
        self._save_json(file_path, log_data)

    def save_batch_out_tray_log(self, log_data: Dict[str, Any]) -> None:
        """
        保存下料日志

        参数:
            log_data: 下料日志数据，格式:
                {
                    "start_time": "2026-01-20T12:00:00",
                    "end_time": "2026-01-20T12:05:00",
                    "resources": [
                        {
                            "layout_code": "W-1-1",
                            "count": 2,
                            "resource_type": 220000023,
                            "resource_type_name": "125 mL试剂瓶托盘",
                            "substance_details": [...],
                            "task_id": 123,
                            "dst_layout_code": "TB-1-1"
                        }
                    ]
                }
        """
        file_path = self.operations_dir / "batch_out_tray.json"
        self._save_json(file_path, log_data)

    def load_task_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        加载任务信息

        参数:
            task_id: 任务ID

        返回:
            任务信息，如果不存在则返回 None
        """
        file_path = self.tasks_dir / task_id / "task_info.json"
        return self._load_json(file_path)

    def load_task_payload(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        加载任务 Payload

        参数:
            task_id: 任务ID

        返回:
            任务Payload，如果不存在则返回 None
        """
        file_path = self.tasks_dir / task_id / "task_payload.json"
        return self._load_json(file_path)

    def load_resource_check(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        加载物料核算结果

        参数:
            task_id: 任务ID

        返回:
            物料核算结果，如果不存在则返回 None
        """
        file_path = self.tasks_dir / task_id / "resource_check.json"
        return self._load_json(file_path)

    def load_unload_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        加载下料信息

        参数:
            task_id: 任务ID

        返回:
            下料信息，如果不存在则返回 None
        """
        file_path = self.tasks_dir / task_id / "unload_info.json"
        return self._load_json(file_path)

    def query_recent_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        查询最近的任务

        参数:
            limit: 返回的任务数量限制

        返回:
            任务信息列表，按创建时间倒序排列
        """
        tasks = []

        if not self.tasks_dir.exists():
            return tasks

        # 遍历所有任务目录
        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue

            task_info_path = task_dir / "task_info.json"
            if not task_info_path.exists():
                continue

            task_info = self._load_json(task_info_path)
            if task_info:
                tasks.append(task_info)

        # 按创建时间排序
        tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        return tasks[:limit]

    # ==================== 操作日志管理 ====================

    def append_operation_log(self, operation_type: str, data: Dict[str, Any]) -> None:
        """
        追加操作记录

        参数:
            operation_type: 操作类型，可选值:
                - "batch_in": 上料操作
                - "batch_out": 下料操作
            data: 操作数据，格式:
                {
                    "operation_id": "op_001",
                    "timestamp": "2026-01-20T09:00:00Z",
                    "operation_type": "batch_in",
                    "template_file": "batch_in_tray.xlsx",
                    "trays": [...],
                    "status": "success"
                }
        """
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        if "operation_id" not in data:
            data["operation_id"] = f"op_{uuid.uuid4().hex[:8]}"

        if "operation_type" not in data:
            data["operation_type"] = operation_type

        file_path = self.operations_dir / f"{operation_type}_log.json"

        # 加载现有日志
        log_data = self._load_json(file_path)
        if log_data is None:
            log_data = {"operations": []}

        # 追加新记录
        log_data["operations"].append(data)

        # 保存
        self._save_json(file_path, log_data)

    def load_operation_log(self, operation_type: str) -> Optional[Dict[str, Any]]:
        """
        加载操作日志

        参数:
            operation_type: 操作类型 ("batch_in" 或 "batch_out")

        返回:
            操作日志数据，格式:
                {
                    "operations": [...]
                }
        """
        file_path = self.operations_dir / f"{operation_type}_log.json"
        return self._load_json(file_path)

    # ==================== 数据清理 ====================

    def cleanup_old_tasks(self, retention_days: int = 90) -> int:
        """
        清理过期的任务数据

        参数:
            retention_days: 保留天数，默认90天

        返回:
            清理的任务数量
        """
        if not self.tasks_dir.exists():
            return 0

        cutoff_date = datetime.now() - timedelta(days=retention_days)
        cleaned_count = 0

        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue

            task_info_path = task_dir / "task_info.json"
            if not task_info_path.exists():
                continue

            task_info = self._load_json(task_info_path)
            if not task_info:
                continue

            # 检查完成时间
            completed_at = task_info.get("completed_at")
            if not completed_at:
                continue

            try:
                completed_date = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                if completed_date < cutoff_date:
                    # 删除任务目录
                    import shutil
                    shutil.rmtree(task_dir)
                    cleaned_count += 1
                    self._logger.info(f"已清理过期任务: {task_dir.name}")
            except Exception as e:
                self._logger.error(f"清理任务 {task_dir.name} 失败: {e}")

        return cleaned_count
