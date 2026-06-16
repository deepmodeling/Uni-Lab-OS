# agv_driver.py
# -*- coding: utf-8 -*-
"""
AGV导航状态查询驱动模块
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..config.agv_config import (
    FRAME_HEAD_SIZE,
    REQ_CMD_ROBOT_STATUS_TASK,
    RSP_CMD_ROBOT_STATUS_TASK,
    REQ_CMD_ROBOT_TASK_GOTARGET,
    RSP_CMD_ROBOT_TASK_GOTARGET,
    REQ_CMD_ROBOT_STATUS_LOC,
    RSP_CMD_ROBOT_STATUS_LOC,
    REQ_CMD_ROBOT_STATUS_BATTERY,
    RSP_CMD_ROBOT_STATUS_BATTERY,
    TASK_STATUS_MAP,
    TASK_TYPE_MAP,
    AGV_HOST,
    AGV_PORT,
    AGV_PORT_NAVIGATION,
    AGV_TIMEOUT,
    AGV_MAX_SPEED,
    AGV_MAX_WSPEED,
    AGV_MAX_ACC,
    AGV_MAX_WACC,
)

logger = logging.getLogger(__name__)


def _hexdump(b: bytes, max_len: int = 64) -> str:
    """
    功能:
        将字节数据转换为十六进制字符串用于调试
    参数:
        b: 待转换的字节数据
        max_len: 最大显示长度, 默认64字节
    返回:
        str, 十六进制字符串表示
    """
    bb = b[:max_len]
    s = " ".join(f"{x:02X}" for x in bb)
    return s + (" ..." if len(b) > max_len else "")

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    功能:
        从socket精确读取指定字节数
    参数:
        sock: socket对象
        n: 需要读取的字节数
    返回:
        bytes, 读取到的数据
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf.extend(chunk)
    return bytes(buf)

def _build_frame(cmd_id: int, payload: Optional[bytes] = None, req_id: int = 1) -> bytes:
    """
    功能:
        构造AGV协议请求帧, 完全参考厂家协议格式
    参数:
        cmd_id: 命令ID (消息类型)
        payload: 可选的负载数据
        req_id: 请求序列号, 默认为1
    返回:
        bytes, 完整的请求帧数据
    协议格式:
        字节0-1: 帧头标识 0x5A 0x01
        字节2-3: 请求ID (大端序)
        字节4-7: 负载长度 (大端序)
        字节8-9: 消息类型/命令ID (大端序)
        字节10-15: 保留字段 (6字节, 填充0x00)
    """
    payload = payload or b""
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("负载数据过大")

    header = bytearray(FRAME_HEAD_SIZE)
    header[0] = 0x5A  # 帧头标识1
    header[1] = 0x01  # 帧头标识2
    header[2:4] = req_id.to_bytes(2, byteorder="big", signed=False)  # 请求ID
    header[4:8] = len(payload).to_bytes(4, byteorder="big", signed=False)  # 负载长度
    header[8:10] = cmd_id.to_bytes(2, byteorder="big", signed=False)  # 消息类型
    header[10:16] = b"\x00" * 6  # 保留字段

    return bytes(header) + payload

def _parse_frame_header(hdr: bytes) -> Tuple[int, int, int]:
    """
    功能:
        解析AGV协议响应帧头部, 参考厂家协议格式
    参数:
        hdr: 帧头部字节数据
    返回:
        Tuple[int, int, int], 包含(负载长度, 响应命令ID, 响应序列号)
    协议格式:
        字节0-1: 帧头标识 0x5A 0x01
        字节2-3: 响应序列号 (大端序)
        字节4-7: 负载长度 (大端序)
        字节8-9: 响应消息类型/命令ID (大端序)
        字节10-15: 保留字段 (6字节)
    """
    if len(hdr) != FRAME_HEAD_SIZE:
        raise ValueError(f"帧头长度错误: {len(hdr)} != {FRAME_HEAD_SIZE}")
    if hdr[0] != 0x5A or hdr[1] != 0x01:
        raise ValueError(f"帧头标识错误: {hdr[0]:02X} {hdr[1]:02X}")

    rsp_seq_num = int.from_bytes(hdr[2:4], byteorder="big", signed=False)  # 响应序列号
    payload_len = int.from_bytes(hdr[4:8], byteorder="big", signed=False)  # 负载长度
    rsp_cmd_id = int.from_bytes(hdr[8:10], byteorder="big", signed=False)  # 响应命令ID

    return payload_len, rsp_cmd_id, rsp_seq_num

def _decode_json_payload(payload: bytes) -> Dict[str, Any]:
    """
    功能:
        解析JSON格式的负载数据, 参考厂家代码实现
    参数:
        payload: JSON字节数据
    返回:
        Dict[str, Any], 解析后的字典对象
    """
    if not payload:
        return {}
    # 去除尾部的空字节填充
    payload = payload.rstrip(b"\x00")
    if not payload:
        return {}
    # 使用ASCII解码(厂家代码使用ascii编码)
    text = payload.decode("ascii", errors="replace")
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("JSON负载不是对象类型")
    return obj

def _try_parse_json_string(value: Any) -> Any:
    """
    功能:
        尝试将字符串形式的JSON解析为字典对象
    参数:
        value: 待解析的值
    返回:
        Any, 解析后的对象或原值
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return value
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return value

@dataclass
class AGVDriverConfig:
    """AGV驱动配置类"""
    host: str
    port: int
    port_navigation: int  # 路径导航专用端口
    timeout_s: float = 3.0
    debug_hex: bool = False

class AGVDriver:
    """AGV驱动类"""

    def __init__(self, cfg: AGVDriverConfig):
        """
        功能:
            初始化AGV驱动
        参数:
            cfg: AGV驱动配置对象
        """
        self.cfg = cfg
        self._sock: Optional[socket.socket] = None
        self._sock_nav: Optional[socket.socket] = None  # 导航专用socket
        self._req_id: int = 1  # 请求序列号, 每次请求递增

    def connect(self) -> None:
        """
        功能:
            连接到AGV控制器
        """
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout_s)
        s.connect((self.cfg.host, self.cfg.port))
        self._sock = s
        logger.debug("已连接到查询端口 %s:%s", self.cfg.host, self.cfg.port)

    def connect_navigation(self) -> None:
        """
        功能:
            连接到AGV导航控制器
        """
        if self._sock_nav is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout_s)
        s.connect((self.cfg.host, self.cfg.port_navigation))
        self._sock_nav = s
        logger.debug("已连接到导航端口 %s:%s", self.cfg.host, self.cfg.port_navigation)

    def close(self) -> None:
        """
        功能:
            关闭与AGV控制器的连接
        """
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._sock_nav:
            try:
                self._sock_nav.close()
            finally:
                self._sock_nav = None

    def reconnect(self) -> None:
        """
        功能:
            重新连接到AGV查询端口, 先关闭已有连接再创建新连接
        """
        # 关闭旧的查询端口socket
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        # 创建新连接
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout_s)
        s.connect((self.cfg.host, self.cfg.port))
        self._sock = s
        logger.info("已重新连接到查询端口 %s:%s", self.cfg.host, self.cfg.port)

    def reconnect_navigation(self) -> None:
        """
        功能:
            重新连接到AGV导航端口, 先关闭已有连接再创建新连接
        """
        if self._sock_nav is not None:
            try:
                self._sock_nav.close()
            except Exception:
                pass
            self._sock_nav = None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout_s)
        s.connect((self.cfg.host, self.cfg.port_navigation))
        self._sock_nav = s
        logger.info("已重新连接到导航端口 %s:%s", self.cfg.host, self.cfg.port_navigation)

    def _send_and_recv_json(
        self,
        cmd_id: int,
        payload_obj: Optional[Dict[str, Any]] = None,
        expect_cmd_id: Optional[int] = None,
        use_navigation_port: bool = False,
    ) -> Dict[str, Any]:
        """
        功能:
            发送JSON请求并接收JSON响应, 参考厂家协议实现
        参数:
            cmd_id: 命令ID (消息类型)
            payload_obj: 可选的请求负载对象
            expect_cmd_id: 期望的响应命令ID
            use_navigation_port: 是否使用导航专用端口
        返回:
            Dict[str, Any], 响应数据字典
        """
        # 根据命令类型选择socket
        if use_navigation_port:
            if self._sock_nav is None:
                raise RuntimeError("未连接到导航端口, 请先调用connect_navigation()")
            sock = self._sock_nav
        else:
            if self._sock is None:
                raise RuntimeError("未连接, 请先调用connect()")
            sock = self._sock

        # 构造JSON负载
        payload_bytes = b""
        if payload_obj is not None:
            payload_bytes = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")

        # 使用当前请求序列号构造请求帧
        current_req_id = self._req_id
        frame = _build_frame(cmd_id, payload_bytes, req_id=current_req_id)
        self._req_id += 1  # 请求序列号递增

        if self.cfg.debug_hex:
            logger.info("发送请求 [序列号=%d 命令=0x%04X 负载长度=%d 端口=%s]",
                       current_req_id, cmd_id, len(payload_bytes),
                       "导航" if use_navigation_port else "查询")
            logger.info("发送帧: %s", _hexdump(frame, 256))
            if payload_obj:
                logger.info("发送负载: %s", json.dumps(payload_obj, ensure_ascii=False))

        # 发送请求
        sock.sendall(frame)

        # 接收响应头部
        rsp_hdr = _recv_exact(sock, FRAME_HEAD_SIZE)
        rsp_len, rsp_cmd, rsp_seq = _parse_frame_header(rsp_hdr)

        if self.cfg.debug_hex:
            logger.info("接收响应 [序列号=%d 命令=0x%04X 负载长度=%d]",
                       rsp_seq, rsp_cmd, rsp_len)
            logger.info("接收头部: %s", _hexdump(rsp_hdr, 64))

        # 验证响应命令ID
        if expect_cmd_id is not None and rsp_cmd != expect_cmd_id:
            raise ValueError(f"响应命令ID不匹配: 0x{rsp_cmd:04X} (期望 0x{expect_cmd_id:04X})")

        # 接收响应负载
        rsp_payload = _recv_exact(sock, rsp_len) if rsp_len > 0 else b""

        if self.cfg.debug_hex and rsp_payload:
            logger.info("接收负载(前512字节): %s", _hexdump(rsp_payload, 512))

        # 解析JSON负载
        data = _decode_json_payload(rsp_payload)

        if self.cfg.debug_hex and data:
            logger.info("解析响应: %s", json.dumps(data, ensure_ascii=False, indent=2))

        # 特殊处理move_status_info字段(可能是嵌套JSON字符串)
        if "move_status_info" in data:
            data["move_status_info"] = _try_parse_json_string(data["move_status_info"])

        return data

    def query_agv_nav_status(self, simple: bool = False) -> Dict[str, Any]:
        """
        功能:
            查询AGV当前导航状态
        参数:
            simple: True表示只返回任务状态, False返回完整信息
        返回:
            Dict[str, Any], 包含导航状态的响应字典
        """
        payload = {"simple": True} if simple else None
        return self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_STATUS_TASK,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_STATUS_TASK,
        )

    def query_robot_location(self) -> Dict[str, Any]:
        """
        功能:
            查询机器人当前位置信息
        返回:
            Dict[str, Any], 包含位置信息的响应字典, 包括:
                - x: X坐标
                - y: Y坐标
                - angle: 角度(弧度)
                - confidence: 定位置信度
                - current_station: 当前站点名称
                - last_station: 上一个站点名称
        """
        return self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_STATUS_LOC,
            payload_obj=None,
            expect_cmd_id=RSP_CMD_ROBOT_STATUS_LOC,
        )

    def query_battery_status(self, simple: bool = True) -> Dict[str, Any]:
        """
        功能:
            查询机器人电池状态
        参数:
            simple: True表示只返回电池电量, False返回完整信息, 默认True
        返回:
            Dict[str, Any], 包含电池状态的响应字典, 包括:
                - battery_level: 电池电量, 范围[0, 1]
                - battery_temp: 电池温度, 单位℃ (仅完整模式)
                - charging: 是否正在充电 (仅完整模式)
                - voltage: 电压, 单位V (仅完整模式)
                - current: 电流, 单位A (仅完整模式)
                - max_charge_voltage: 允许充电的最大电压, 单位V (仅完整模式)
                - max_charge_current: 允许充电的最大电流, 单位A (仅完整模式)
                - manual_charge: 是否连接手动充电器 (仅完整模式)
                - auto_charge: 是否连接自动充电桩 (仅完整模式)
                - battery_cycle: 电池循环次数 (仅完整模式)
                - ret_code: API错误码
                - err_msg: 错误信息
        """
        payload = {"simple": True} if simple else {"simple": False}
        return self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_STATUS_BATTERY,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_STATUS_BATTERY,
        )

    def send_navigate_command(
        self,
        target_id: str,
        source_id: str = "SELF_POSITION",
        task_id: Optional[str] = None,
        angle: Optional[float] = None,
        method: Optional[str] = None,
        max_speed: Optional[float] = None,
        max_wspeed: Optional[float] = None,
        max_acc: Optional[float] = None,
        max_wacc: Optional[float] = None,
        duration: Optional[int] = None,
        orientation: Optional[float] = None,
        spin: Optional[bool] = None,
        delay: Optional[int] = None,
        start_rot_dir: Optional[int] = None,
        end_rot_dir: Optional[int] = None,
        reach_dist: Optional[float] = None,
        reach_angle: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        功能:
            发送路径导航命令, 控制AGV前往指定目标站点, 发送完命令立即返回, 不等待导航完成
        参数:
            target_id: 目标站点名称, 必填
            source_id: 起始站点名称, 固定为"SELF_POSITION"表示当前位置
            task_id: 任务编号, 可选
            angle: 目标站点角度值, 单位rad, 可选
            method: 运动方式, "forward"(正走)或"backward"(倒走), 可选
            max_speed: 最大速度, 单位m/s, 可选
            max_wspeed: 最大角速度, 单位rad/s, 可选
            max_acc: 最大加速度, 单位m/s^2, 可选
            max_wacc: 最大角加速度, 单位rad/s^2, 可选
            duration: 导航结束后等待时间, 单位ms, 可选
            orientation: 全向车保持的角度, 可选
            spin: 是否随动, 可选
            delay: 延迟结束导航状态的时间, 单位ms, 可选
            start_rot_dir: 起步原地旋转方向(-1顺时针, 0近方向, 1逆时针), 可选
            end_rot_dir: 到点原地旋转方向(-1顺时针, 0近方向, 1逆时针), 可选
            reach_dist: 到点位置精度, 单位m, 可选
            reach_angle: 到点角度精度, 单位rad, 可选
        返回:
            Dict[str, Any], 包含导航命令发送响应的字典
        """
        # 确保导航端口已连接
        self.connect_navigation()

        payload = {
            "id": target_id,
            "source_id": source_id,
        }

        # 只添加用户提供的可选参数
        if task_id is not None:
            payload["task_id"] = task_id
        if angle is not None:
            payload["angle"] = angle
        if method is not None:
            payload["method"] = method
        # 速度和加速度参数, 使用配置文件中的默认值
        payload["max_speed"] = max_speed if max_speed is not None else AGV_MAX_SPEED
        payload["max_wspeed"] = max_wspeed if max_wspeed is not None else AGV_MAX_WSPEED
        payload["max_acc"] = max_acc if max_acc is not None else AGV_MAX_ACC
        payload["max_wacc"] = max_wacc if max_wacc is not None else AGV_MAX_WACC
        if duration is not None:
            payload["duration"] = duration
        if orientation is not None:
            payload["orientation"] = orientation
        if spin is not None:
            payload["spin"] = spin
        if delay is not None:
            payload["delay"] = delay
        if start_rot_dir is not None:
            payload["start_rot_dir"] = start_rot_dir
        if end_rot_dir is not None:
            payload["end_rot_dir"] = end_rot_dir
        if reach_dist is not None:
            payload["reach_dist"] = reach_dist
        if reach_angle is not None:
            payload["reach_angle"] = reach_angle

        # 发送导航指令并返回响应
        response = self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_TASK_GOTARGET,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_TASK_GOTARGET,
            use_navigation_port=True,
        )

        logger.info("导航指令已发送, 目标站点: %s", target_id)
        return response

    def navigate_to_target(
        self,
        target_id: str,
        source_id: str = "SELF_POSITION",
        task_id: Optional[str] = None,
        angle: Optional[float] = None,
        method: Optional[str] = None,
        max_speed: Optional[float] = None,
        max_wspeed: Optional[float] = None,
        max_acc: Optional[float] = None,
        max_wacc: Optional[float] = None,
        duration: Optional[int] = None,
        orientation: Optional[float] = None,
        spin: Optional[bool] = None,
        delay: Optional[int] = None,
        start_rot_dir: Optional[int] = None,
        end_rot_dir: Optional[int] = None,
        reach_dist: Optional[float] = None,
        reach_angle: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        功能:
            路径导航, 控制AGV前往指定目标站点, 并等待导航完成
        参数:
            target_id: 目标站点名称, 必填
            source_id: 起始站点名称, 固定为"SELF_POSITION"表示当前位置
            task_id: 任务编号, 可选
            angle: 目标站点角度值, 单位rad, 可选
            method: 运动方式, "forward"(正走)或"backward"(倒走), 可选
            max_speed: 最大速度, 单位m/s, 可选
            max_wspeed: 最大角速度, 单位rad/s, 可选
            max_acc: 最大加速度, 单位m/s^2, 可选
            max_wacc: 最大角加速度, 单位rad/s^2, 可选
            duration: 导航结束后等待时间, 单位ms, 可选
            orientation: 全向车保持的角度, 可选
            spin: 是否随动, 可选
            delay: 延迟结束导航状态的时间, 单位ms, 可选
            start_rot_dir: 起步原地旋转方向(-1顺时针, 0近方向, 1逆时针), 可选
            end_rot_dir: 到点原地旋转方向(-1顺时针, 0近方向, 1逆时针), 可选
            reach_dist: 到点位置精度, 单位m, 可选
            reach_angle: 到点角度精度, 单位rad, 可选
        返回:
            Dict[str, Any], 包含最终导航状态的字典
        """
        # 确保导航端口已连接
        self.connect_navigation()
        # 确保查询端口已连接, 用于状态查询
        self.connect()

        payload = {
            "id": target_id,
            "source_id": source_id,
        }

        # 只添加用户提供的可选参数
        if task_id is not None:
            payload["task_id"] = task_id
        if angle is not None:
            payload["angle"] = angle
        if method is not None:
            payload["method"] = method
        # 速度和加速度参数, 使用配置文件中的默认值
        payload["max_speed"] = max_speed if max_speed is not None else AGV_MAX_SPEED
        payload["max_wspeed"] = max_wspeed if max_wspeed is not None else AGV_MAX_WSPEED
        payload["max_acc"] = max_acc if max_acc is not None else AGV_MAX_ACC
        payload["max_wacc"] = max_wacc if max_wacc is not None else AGV_MAX_WACC
        if duration is not None:
            payload["duration"] = duration
        if orientation is not None:
            payload["orientation"] = orientation
        if spin is not None:
            payload["spin"] = spin
        if delay is not None:
            payload["delay"] = delay
        if start_rot_dir is not None:
            payload["start_rot_dir"] = start_rot_dir
        if end_rot_dir is not None:
            payload["end_rot_dir"] = end_rot_dir
        if reach_dist is not None:
            payload["reach_dist"] = reach_dist
        if reach_angle is not None:
            payload["reach_angle"] = reach_angle

        # 发送导航指令
        self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_TASK_GOTARGET,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_TASK_GOTARGET,
            use_navigation_port=True,
        )

        logger.info("导航指令已发送, 目标站点: %s", target_id)

        # 循环查询导航状态, 直到完成
        max_reconnect_attempts = 5  # 最大连续重连次数
        consecutive_errors = 0      # 连续错误计数器

        while True:
            time.sleep(0.5)  # 每0.5秒查询一次, 避免频繁查询

            try:
                status_data = self.query_agv_nav_status(simple=False)
                # 查询成功, 重置连续错误计数
                consecutive_errors = 0

                task_status = status_data.get("task_status")

                if task_status is None:
                    logger.warning("未获取到任务状态, 继续查询")
                    continue

                status_name = TASK_STATUS_MAP.get(task_status, "UNKNOWN")
                logger.debug("当前导航状态: %s (%d)", status_name, task_status)

                if task_status == 4:  # COMPLETED
                    logger.info("已运动到目标点位: %s", target_id)
                    return status_data
                elif task_status == 5:  # FAILED
                    logger.error("导航失败, 目标站点: %s", target_id)
                    return status_data
                elif task_status == 6:  # CANCELED
                    logger.warning("导航已取消, 目标站点: %s", target_id)
                    return status_data

            except (ConnectionError, ConnectionResetError, OSError) as e:
                consecutive_errors += 1
                logger.warning(
                    "查询导航状态时连接异常(第%d次): %s",
                    consecutive_errors, e
                )

                if consecutive_errors >= max_reconnect_attempts:
                    logger.error(
                        "连续%d次连接异常, 放弃导航状态查询, 目标站点: %s",
                        consecutive_errors, target_id
                    )
                    raise ConnectionError(
                        "导航状态查询连续%d次连接失败, 放弃查询" % consecutive_errors
                    ) from e

                # 尝试重新连接查询端口
                try:
                    logger.info("正在尝试重新连接查询端口...")
                    self.reconnect()
                except Exception as reconnect_err:
                    logger.error("重新连接失败: %s", reconnect_err)

            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "查询导航状态时发生非连接错误(第%d次): %s",
                    consecutive_errors, e
                )

                if consecutive_errors >= max_reconnect_attempts:
                    logger.error(
                        "连续%d次错误, 放弃导航状态查询, 目标站点: %s",
                        consecutive_errors, target_id
                    )
                    raise

    def play_sound(
        self,
        target_id: str = "SELF_POSITION",
        source_id: str = "SELF_POSITION",
        task_id: Optional[str] = None,
        sound_name: Optional[str] = None,
        loop: bool = False,
        stop: bool = False,
    ) -> Dict[str, Any]:
        """
        功能:
            控制AGV音频播放
        参数:
            target_id: 目标站点名称, 默认"SELF_POSITION"表示原地执行
            source_id: 起始站点名称, 默认"SELF_POSITION"
            task_id: 任务编号, 可选
            sound_name: 音频名称, 可选
            loop: True表示循环播放, False表示只播放一次, 默认False
            stop: True表示停止播放, False表示不处理, 默认False
        返回:
            Dict[str, Any], 包含音频控制响应的字典
        """
        payload = {
            "id": target_id,
            "source_id": source_id,
            "operation": "sound",
            "sounds_args": {},
        }

        if task_id is not None:
            payload["task_id"] = task_id

        # 构建sounds_args参数
        if sound_name is not None:
            payload["sounds_args"]["name"] = sound_name
        payload["sounds_args"]["loop"] = 1 if loop else 0
        payload["sounds_args"]["stop"] = 1 if stop else 0

        return self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_TASK_GOTARGET,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_TASK_GOTARGET,
            use_navigation_port=True,
        )

    def rotate_in_place(
        self,
        move_angle: float,
        speed_w: float = 1.57,
        loc_mode: int = 1,
    ) -> Dict[str, Any]:
        """
        功能:
            控制AGV原地旋转
        参数:
            move_angle: 旋转角度, 单位rad, 必填
            speed_w: 角速度, 单位rad/s, 默认1.57(约90度/秒)
            loc_mode: 定位模式, 1表示激光定位, 0表示里程定位, 默认1
        返回:
            Dict[str, Any], 包含旋转响应的字典
        """
        payload = {
            "move_angle": move_angle,
            "speed_w": speed_w,
            "skill_name": "GoByOdometer",
            "loc_mode": loc_mode,
        }

        return self._send_and_recv_json(
            cmd_id=REQ_CMD_ROBOT_TASK_GOTARGET,
            payload_obj=payload,
            expect_cmd_id=RSP_CMD_ROBOT_TASK_GOTARGET,
            use_navigation_port=True,
        )

    @staticmethod
    def pretty_status(data: Dict[str, Any]) -> str:
        """
        功能:
            格式化状态数据为可读字符串
        参数:
            data: 状态数据字典
        返回:
            str, 格式化后的状态信息
        """
        ts = data.get("task_status")
        tt = data.get("task_type")

        lines = []
        if ts is not None:
            lines.append(f"任务状态: {ts} ({TASK_STATUS_MAP.get(ts, 'UNKNOWN')})")
        if tt is not None:
            lines.append(f"任务类型: {tt} ({TASK_TYPE_MAP.get(tt, 'UNKNOWN')})")
        if "target_id" in data:
            lines.append(f"目标ID: {data.get('target_id')}")
        if "target_point" in data:
            lines.append(f"目标点: {data.get('target_point')}")
        if "finished_path" in data:
            lines.append(f"已完成路径: {data.get('finished_path')}")
        if "unfinished_path" in data:
            lines.append(f"未完成路径: {data.get('unfinished_path')}")
        if "move_status_info" in data:
            lines.append(f"移动状态信息: {data.get('move_status_info')}")
        if "containers" in data:
            lines.append(f"容器信息: {data.get('containers')}")
        if "ret_code" in data:
            lines.append(f"返回码: {data.get('ret_code')}")
        if "err_msg" in data:
            lines.append(f"错误信息: {data.get('err_msg')}")

        return "\n".join(lines) if lines else json.dumps(data, ensure_ascii=False, indent=2)


def main() -> None:
    """
    功能:
        交互式测试主函数
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    driver = AGVDriver(AGVDriverConfig(
        host=AGV_HOST,
        port=AGV_PORT,
        port_navigation=AGV_PORT_NAVIGATION,
        timeout_s=AGV_TIMEOUT,
        debug_hex=False,
    ))

    print("=" * 60)
    print("AGV导航状态查询交互式测试")
    print("=" * 60)
    print(f"当前配置: {AGV_HOST}:{AGV_PORT}")
    print("=" * 60)

    try:
        driver.connect()
        print("连接成功\n")

        while True:
            print("\n请选择操作:")
            print("1. 查询完整导航状态")
            print("2. 查询简单导航状态")
            print("3. 路径导航到目标站点")
            print("4. 查询机器人位置")
            print("5. 播放音乐")
            print("6. 停止音乐")
            print("7. 原地旋转")
            print("8. 开启十六进制调试模式")
            print("9. 关闭十六进制调试模式")
            print("10. 查询电池状态(简单)")
            print("11. 查询电池状态(完整)")
            print("0. 退出")

            choice = input("\n请输入选项 (0-11): ").strip()

            if choice == "0":
                print("退出测试")
                break
            elif choice == "1":
                print("\n正在查询完整导航状态...")
                try:
                    data = driver.query_agv_nav_status(simple=False)
                    print("\n=== 查询结果 ===")
                    print(AGVDriver.pretty_status(data))
                except Exception as e:
                    logger.error("查询失败: %s", e)
            elif choice == "2":
                print("\n正在查询简单导航状态...")
                try:
                    data = driver.query_agv_nav_status(simple=True)
                    print("\n=== 查询结果 ===")
                    print(AGVDriver.pretty_status(data))
                except Exception as e:
                    logger.error("查询失败: %s", e)
            elif choice == "3":
                target = input("请输入目标站点名称: ").strip()
                if not target:
                    print("目标站点名称不能为空")
                    continue
                print(f"\n正在导航到目标站点 {target}...")
                try:
                    data = driver.navigate_to_target(target_id=target)
                    print("\n=== 导航响应 ===")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                except Exception as e:
                    logger.error("导航失败: %s", e)
            elif choice == "4":
                print("\n正在查询机器人位置...")
                try:
                    data = driver.query_robot_location()
                    print("\n=== 位置信息 ===")
                    print(f"X坐标: {data.get('x')}")
                    print(f"Y坐标: {data.get('y')}")
                    print(f"角度: {data.get('angle')} rad")
                    print(f"置信度: {data.get('confidence')}")
                    print(f"当前站点: {data.get('current_station')}")
                    print(f"上一站点: {data.get('last_station')}")
                except Exception as e:
                    logger.error("查询位置失败: %s", e)
            elif choice == "5":
                sound_name = input("请输入音频名称 (默认: navigation): ").strip() or "navigation"
                loop_input = input("是否循环播放? (y/n, 默认: n): ").strip().lower()
                loop = loop_input == "y"
                print(f"\n正在播放音乐 {sound_name} (循环: {loop})...")
                try:
                    data = driver.play_sound(sound_name=sound_name, loop=loop)
                    print("\n=== 音乐控制响应 ===")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                except Exception as e:
                    logger.error("播放音乐失败: %s", e)
            elif choice == "6":
                print("\n正在停止音乐...")
                try:
                    data = driver.play_sound(stop=True)
                    print("\n=== 音乐控制响应 ===")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                except Exception as e:
                    logger.error("停止音乐失败: %s", e)
            elif choice == "7":
                angle_input = input("请输入旋转角度(度, 默认: 90): ").strip()
                try:
                    angle_deg = float(angle_input) if angle_input else 90.0
                    angle_rad = angle_deg * 3.14159265359 / 180.0  # 转换为弧度
                    print(f"\n正在原地旋转 {angle_deg} 度 ({angle_rad:.4f} 弧度)...")
                    data = driver.rotate_in_place(move_angle=angle_rad)
                    print("\n=== 旋转响应 ===")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                except ValueError:
                    print("无效的角度值")
                except Exception as e:
                    logger.error("旋转失败: %s", e)
            elif choice == "8":
                driver.cfg.debug_hex = True
                print("已开启十六进制调试模式")
            elif choice == "9":
                driver.cfg.debug_hex = False
                print("已关闭十六进制调试模式")
            elif choice == "10":
                print("\n正在查询电池状态(简单)...")
                try:
                    data = driver.query_battery_status(simple=True)
                    print("\n=== 电池状态 ===")
                    battery_level = data.get("battery_level")
                    if battery_level is not None:
                        print(f"电池电量: {battery_level * 100:.1f}%")
                    else:
                        print("未获取到电池电量")
                    if "ret_code" in data:
                        print(f"返回码: {data.get('ret_code')}")
                    if "err_msg" in data:
                        print(f"错误信息: {data.get('err_msg')}")
                except Exception as e:
                    logger.error("查询电池状态失败: %s", e)
            elif choice == "11":
                print("\n正在查询电池状态(完整)...")
                try:
                    data = driver.query_battery_status(simple=False)
                    print("\n=== 电池状态 ===")
                    battery_level = data.get("battery_level")
                    if battery_level is not None:
                        print(f"电池电量: {battery_level * 100:.1f}%")
                    if "battery_temp" in data:
                        print(f"电池温度: {data.get('battery_temp')}℃")
                    if "charging" in data:
                        print(f"正在充电: {'是' if data.get('charging') else '否'}")
                    if "voltage" in data:
                        print(f"电压: {data.get('voltage')}V")
                    if "current" in data:
                        print(f"电流: {data.get('current')}A")
                    if "max_charge_voltage" in data:
                        max_v = data.get("max_charge_voltage")
                        print(f"最大充电电压: {max_v}V" if max_v != -1 else "最大充电电压: 不支持")
                    if "max_charge_current" in data:
                        max_c = data.get("max_charge_current")
                        print(f"最大充电电流: {max_c}A" if max_c != -1 else "最大充电电流: 不支持")
                    if "manual_charge" in data:
                        print(f"手动充电器连接: {'是' if data.get('manual_charge') else '否'}")
                    if "auto_charge" in data:
                        print(f"自动充电桩连接: {'是' if data.get('auto_charge') else '否'}")
                    if "battery_cycle" in data:
                        print(f"电池循环次数: {data.get('battery_cycle')}")
                    if "ret_code" in data:
                        print(f"返回码: {data.get('ret_code')}")
                    if "err_msg" in data:
                        print(f"错误信息: {data.get('err_msg')}")
                except Exception as e:
                    logger.error("查询电池状态失败: %s", e)
            else:
                print("无效选项, 请重新输入")

    except KeyboardInterrupt:
        print("\n\n用户中断")
    except Exception as e:
        logger.error("连接失败: %s", e)
    finally:
        driver.close()
        print("连接已关闭")


if __name__ == "__main__":
    main()
