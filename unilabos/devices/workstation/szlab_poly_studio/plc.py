import csv
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from opcua import Client

from unilabos.device_comms.opcua_client.node.uniopcua import NodeType
from unilabos.devices.workstation.post_process.post_process import BaseClient, OpcUaNode
from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.utils.log import logger


DEFAULT_CSV_NAME = "苏州实验室_0610.csv"


S3_UNUSED_BEAKER_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[0].NO[6]",
    "1-2": "传感器状态_上位机[0].NO[7]",
    "1-3": "传感器状态_上位机[0].NO[8]",
    "1-4": "传感器状态_上位机[0].NO[9]",
    "1-5": "传感器状态_上位机[0].NO[10]",
    "1-6": "传感器状态_上位机[0].NO[11]",
    "2-1": "传感器状态_上位机[0].NO[12]",
    "2-2": "传感器状态_上位机[0].NO[13]",
    "2-3": "传感器状态_上位机[0].NO[14]",
    "2-4": "传感器状态_上位机[0].NO[15]",
    "2-5": "传感器状态_上位机[1].NO[0]",
    "2-6": "传感器状态_上位机[1].NO[1]",
    "3-1": "传感器状态_上位机[1].NO[2]",
    "3-2": "传感器状态_上位机[1].NO[3]",
    "3-3": "传感器状态_上位机[1].NO[4]",
    "3-4": "传感器状态_上位机[1].NO[5]",
    "3-5": "传感器状态_上位机[1].NO[6]",
    "3-6": "传感器状态_上位机[1].NO[7]",
}

S3_UNUSED_SAMPLE_VIAL_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[1].NO[8]",
    "1-2": "传感器状态_上位机[1].NO[9]",
    "1-3": "传感器状态_上位机[1].NO[10]",
    "1-4": "传感器状态_上位机[1].NO[11]",
    "1-5": "传感器状态_上位机[1].NO[12]",
    "1-6": "传感器状态_上位机[1].NO[13]",
    "2-1": "传感器状态_上位机[1].NO[14]",
    "2-2": "传感器状态_上位机[1].NO[15]",
    "2-3": "传感器状态_上位机[2].NO[0]",
    "2-4": "传感器状态_上位机[2].NO[1]",
    "2-5": "传感器状态_上位机[2].NO[2]",
    "2-6": "传感器状态_上位机[2].NO[3]",
    "3-1": "传感器状态_上位机[2].NO[4]",
    "3-2": "传感器状态_上位机[2].NO[5]",
    "3-3": "传感器状态_上位机[2].NO[6]",
    "3-4": "传感器状态_上位机[2].NO[7]",
    "3-5": "传感器状态_上位机[2].NO[8]",
    "3-6": "传感器状态_上位机[2].NO[9]",
}

S11_USED_BEAKER_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[6].NO[0]",
    "1-2": "传感器状态_上位机[6].NO[1]",
    "1-3": "传感器状态_上位机[6].NO[2]",
    "1-4": "传感器状态_上位机[6].NO[3]",
    "1-5": "传感器状态_上位机[6].NO[4]",
    "1-6": "传感器状态_上位机[6].NO[5]",
    "2-1": "传感器状态_上位机[6].NO[6]",
    "2-2": "传感器状态_上位机[6].NO[7]",
    "2-3": "传感器状态_上位机[6].NO[8]",
    "2-4": "传感器状态_上位机[6].NO[9]",
    "2-5": "传感器状态_上位机[6].NO[10]",
    "2-6": "传感器状态_上位机[6].NO[11]",
    "3-1": "传感器状态_上位机[6].NO[12]",
    "3-2": "传感器状态_上位机[6].NO[13]",
    "3-3": "传感器状态_上位机[6].NO[14]",
    "3-4": "传感器状态_上位机[6].NO[15]",
    "3-5": "传感器状态_上位机[7].NO[0]",
    "3-6": "传感器状态_上位机[7].NO[1]",
}

S11_USED_SAMPLE_VIAL_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[7].NO[2]",
    "1-2": "传感器状态_上位机[7].NO[3]",
    "1-3": "传感器状态_上位机[7].NO[4]",
    "1-4": "传感器状态_上位机[7].NO[5]",
    "1-5": "传感器状态_上位机[7].NO[6]",
    "1-6": "传感器状态_上位机[7].NO[7]",
    "2-1": "传感器状态_上位机[7].NO[8]",
    "2-2": "传感器状态_上位机[7].NO[9]",
    "2-3": "传感器状态_上位机[7].NO[10]",
    "2-4": "传感器状态_上位机[7].NO[11]",
    "2-5": "传感器状态_上位机[7].NO[12]",
    "2-6": "传感器状态_上位机[7].NO[13]",
    "3-1": "传感器状态_上位机[7].NO[14]",
    "3-2": "传感器状态_上位机[7].NO[15]",
    "3-3": "传感器状态_上位机[8].NO[0]",
    "3-4": "传感器状态_上位机[8].NO[1]",
    "3-5": "传感器状态_上位机[8].NO[2]",
    "3-6": "传感器状态_上位机[8].NO[3]",
}

S2_TIP_SENSORS: Dict[str, str] = {
    str(index): f"传感器状态_上位机[0].NO[{index - 1}]"
    for index in range(1, 7)
}

POWDER_CONTAINER_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[3].NO[8]",
    "1-2": "传感器状态_上位机[3].NO[9]",
    "1-3": "传感器状态_上位机[3].NO[10]",
    "2-1": "传感器状态_上位机[3].NO[11]",
    "2-2": "传感器状态_上位机[3].NO[12]",
    "2-3": "传感器状态_上位机[3].NO[13]",
}

S10_LIQUID_REAGENT_SENSORS: Dict[str, str] = {
    "1-1": "传感器状态_上位机[4].NO[12]",
    "1-2": "传感器状态_上位机[4].NO[13]",
    "1-3": "传感器状态_上位机[4].NO[14]",
    "1-4": "传感器状态_上位机[4].NO[15]",
    "1-5": "传感器状态_上位机[5].NO[0]",
    "2-1": "传感器状态_上位机[5].NO[1]",
    "2-2": "传感器状态_上位机[5].NO[2]",
    "2-3": "传感器状态_上位机[5].NO[3]",
    "2-4": "传感器状态_上位机[5].NO[4]",
    "2-5": "传感器状态_上位机[5].NO[5]",
    "3-1": "传感器状态_上位机[5].NO[6]",
    "3-2": "传感器状态_上位机[5].NO[7]",
    "3-3": "传感器状态_上位机[5].NO[8]",
    "3-4": "传感器状态_上位机[5].NO[9]",
    "3-5": "传感器状态_上位机[5].NO[10]",
    "4-1": "传感器状态_上位机[5].NO[11]",
    "4-2": "传感器状态_上位机[5].NO[12]",
    "4-3": "传感器状态_上位机[5].NO[13]",
    "4-4": "传感器状态_上位机[5].NO[14]",
    "4-5": "传感器状态_上位机[5].NO[15]",
}

SENSOR_GROUPS: Dict[str, Dict[str, str]] = {
    "s2_tip": S2_TIP_SENSORS,
    "s3_unused_beaker": S3_UNUSED_BEAKER_SENSORS,
    "s3_unused_sample_vial": S3_UNUSED_SAMPLE_VIAL_SENSORS,
    "s10_liquid_reagent": S10_LIQUID_REAGENT_SENSORS,
    "s11_used_beaker": S11_USED_BEAKER_SENSORS,
    "s11_used_sample_vial": S11_USED_SAMPLE_VIAL_SENSORS,
    "powder_container": POWDER_CONTAINER_SENSORS,
}


def _resolve_csv_path(csv_path: Optional[str]) -> str:
    if csv_path is None:
        csv_path = DEFAULT_CSV_NAME
    if os.path.isabs(csv_path):
        return csv_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), csv_path)


def load_variable_names_from_csv(csv_path: str) -> List[str]:
    """Load PLC variable names from the CSV column named '变量名'."""
    names: List[str] = []
    seen = set()
    last_error: Optional[UnicodeDecodeError] = None
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            with open(csv_path, newline="", encoding=encoding) as csv_file:
                reader = csv.DictReader(csv_file)
                if "变量名" not in (reader.fieldnames or []):
                    raise ValueError("CSV 文件缺少 '变量名' 列")
                for row in reader:
                    name = (row.get("变量名") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    names.append(name)
            return names
        except UnicodeDecodeError as exc:
            names.clear()
            seen.clear()
            last_error = exc
    if last_error:
        raise last_error
    return names


@device(
    id="szlab_poly_plc",
    display_name="苏州实验室 PLC",
    category=["custom"],
    description="苏州实验室聚合物工作站 PLC/OPC UA 通讯设备，负责变量读写和传感器状态发布",
)
class SZLabPolyPLCDevice(BaseClient):
    def __init__(
        self,
        url: str,
        csv_path: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        heartbeat_node: str = "Heart_Beat",
        auto_connect: bool = True,
        opcua_log_level: str = "WARNING",
        *args,
        **kwargs,
    ):
        super().__init__()
        self.csv_path = _resolve_csv_path(csv_path)
        self.heartbeat_node = heartbeat_node
        self.heartbeat_on = False
        self._heartbeat_timer: Optional[threading.Timer] = None

        nodes = [
            OpcUaNode(name=name, node_type=NodeType.VARIABLE, data_type=None)
            for name in load_variable_names_from_csv(self.csv_path)
        ]
        self.register_node_list(nodes)

        logging.getLogger("opcua").setLevel(getattr(logging, opcua_log_level.upper(), logging.WARNING))
        client = Client(url)
        if username and password:
            client.set_user(username)
            client.set_password(password)
        self._set_client(client)
        if auto_connect:
            self._connect()

    @not_action
    def read_variable(self, node_name: str, use_cache: bool = True) -> Any:
        del use_cache  # BaseClient reads directly from the OPC UA node.
        value, error = self.use_node(node_name).read()
        if error:
            raise RuntimeError(f"读取 PLC 变量失败: {node_name}")
        return value

    @not_action
    def write_variable(self, node_name: str, value: Any) -> bool:
        error = self.use_node(node_name).write(value)
        if error:
            raise RuntimeError(f"写入 PLC 变量失败: {node_name}")
        return True

    @not_action
    def get_variables(self, node_names: Optional[List[str]] = None) -> Dict[str, Any]:
        names = node_names or list(self._variables_to_find)
        result: Dict[str, Any] = {}
        for name in names:
            try:
                result[name] = self.read_variable(name)
            except Exception as exc:
                result[name] = {"success": False, "error": str(exc)}
        return result

    @not_action
    def _read_sensor_group(self, sensors: Dict[str, str]) -> Dict[str, Optional[bool]]:
        result: Dict[str, Optional[bool]] = {}
        for site_key, variable_name in sensors.items():
            try:
                result[site_key] = bool(self.read_variable(variable_name))
            except Exception as exc:
                logger.warning(f"读取传感器 {variable_name} 失败: {exc}")
                result[site_key] = None
        return result

    @action(auto_prefix=True, always_free=True, description="启动苏州实验室 PLC 心跳")
    def start_heart_beat(self) -> Dict[str, Any]:
        if self.heartbeat_node not in self._variables_to_find:
            return {
                "success": False,
                "message": f"CSV 中未注册心跳变量 {self.heartbeat_node}",
            }
        if self.heartbeat_on:
            return {"success": True, "message": "心跳已在运行"}
        self.heartbeat_on = True
        self._schedule_heartbeat()
        return {"success": True, "message": "心跳已启动"}

    @action(auto_prefix=True, always_free=True, description="停止苏州实验室 PLC 心跳")
    def stop_heart_beat(self) -> Dict[str, Any]:
        self.heartbeat_on = False
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None
        if self.heartbeat_node in self._variables_to_find:
            try:
                self.write_variable(self.heartbeat_node, False)
            except Exception as exc:
                return {"success": False, "message": str(exc)}
        return {"success": True, "message": "心跳已停止"}

    @not_action
    def _schedule_heartbeat(self) -> None:
        self._heartbeat_timer = threading.Timer(1.0, self._trigger_heart_beat)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    @not_action
    def _trigger_heart_beat(self) -> None:
        if not self.heartbeat_on:
            return
        try:
            current = bool(self.read_variable(self.heartbeat_node))
            self.write_variable(self.heartbeat_node, not current)
        except Exception as exc:
            logger.warning(f"PLC 心跳写入失败: {exc}")
        if self.heartbeat_on:
            self._schedule_heartbeat()

    @action(auto_prefix=True, always_free=True, description="读取指定 PLC 变量")
    def check_variable_status(self, variable_name: str) -> Dict[str, Any]:
        try:
            return {
                "success": True,
                "variable_name": variable_name,
                "value": self.read_variable(variable_name),
            }
        except Exception as exc:
            return {
                "success": False,
                "variable_name": variable_name,
                "error": str(exc),
            }

    @action(auto_prefix=True, always_free=True, description="写入指定 PLC 变量")
    def write_variable_action(self, variable_name: str, value: Any) -> Dict[str, Any]:
        try:
            self.write_variable(variable_name, value)
            return {"success": True, "variable_name": variable_name, "value": value}
        except Exception as exc:
            return {"success": False, "variable_name": variable_name, "error": str(exc)}

    @action(auto_prefix=True, always_free=True, description="读取指定传感器分组")
    def get_sensor_group_status(self, group_name: str) -> Dict[str, Any]:
        sensors = SENSOR_GROUPS.get(group_name)
        if sensors is None:
            return {
                "success": False,
                "group_name": group_name,
                "available_groups": sorted(SENSOR_GROUPS),
            }
        return {
            "success": True,
            "group_name": group_name,
            "status": self._read_sensor_group(sensors),
        }

    @action(auto_prefix=True, always_free=True, description="写入 S01 上料过渡仓取料编号和入料产品")
    def set_s1_loading_request(self, pick_index: int, product_type: int) -> Dict[str, Any]:
        try:
            self.write_variable("S01取料编号", int(pick_index))
            self.write_variable("S01入料产品", int(product_type))
            return {
                "success": True,
                "pick_index": pick_index,
                "product_type": product_type,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @topic_config(period=1.0)
    def s2_tip_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S2_TIP_SENSORS)

    @topic_config(period=1.0)
    def s3_unused_beaker_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S3_UNUSED_BEAKER_SENSORS)

    @topic_config(period=1.0)
    def s3_unused_sample_vial_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S3_UNUSED_SAMPLE_VIAL_SENSORS)

    @topic_config(period=1.0)
    def s10_liquid_reagent_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S10_LIQUID_REAGENT_SENSORS)

    @topic_config(period=1.0)
    def s11_used_beaker_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S11_USED_BEAKER_SENSORS)

    @topic_config(period=1.0)
    def s11_used_sample_vial_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(S11_USED_SAMPLE_VIAL_SENSORS)

    @topic_config(period=1.0)
    def powder_container_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_sensor_group(POWDER_CONTAINER_SENSORS)

    @topic_config(period=5.0)
    def registered_variable_count(self) -> int:
        return len(self._variables_to_find)

    @topic_config(period=5.0)
    def registered_variables(self) -> List[str]:
        return sorted(self._variables_to_find)
