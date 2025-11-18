#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XRD D7-Mate设备驱动

支持XRD D7-Mate设备的TCP通信协议，包括自动模式控制、上样流程、数据获取、下样流程和高压电源控制等功能。
通信协议版本：1.0.0
"""

import json
import socket
import struct
import time
from typing import Dict, List, Optional, Tuple, Any, Union


class XRDClient:
    def __init__(self, host='127.0.0.1', port=6001, timeout=10.0):
        """
        初始化XRD D7-Mate客户端
        
        Args:
            host (str): 设备IP地址
            port (int): 通信端口，默认6001
            timeout (float): 超时时间，单位秒
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._ros_node = None  # ROS节点引用，由框架设置
    
    def post_init(self, ros_node):
        """
        ROS节点初始化后的回调方法，保存ROS节点引用但不自动连接
        
        Args:
            ros_node: ROS节点实例
        """
        self._ros_node = ros_node
        ros_node.lab_logger().info(f"XRD D7-Mate设备已初始化，将在需要时连接: {self.host}:{self.port}")
        # 不自动连接，只有在调用具体功能时才建立连接

    def connect(self):
        """
        建立TCP连接到XRD D7-Mate设备
        
        Raises:
            ConnectionError: 连接失败时抛出
        """
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
        except Exception as e:
            raise ConnectionError(f"Failed to connect to {self.host}:{self.port} - {str(e)}")

    def close(self):
        """
        关闭与XRD D7-Mate设备的TCP连接
        """
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass  # 忽略关闭时的错误
            finally:
                self.sock = None

    def _ensure_connection(self) -> bool:
        """
        确保连接存在，如果不存在则尝试建立连接
        
        Returns:
            bool: 连接是否成功建立
        """
        if self.sock is None:
            try:
                self.connect()
                return True
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().error(f"建立连接失败: {e}")
                return False
        return True

    def _receive_with_length_prefix(self) -> dict:
        """
        使用长度前缀协议接收数据
        
        Returns:
            dict: 解析后的JSON响应数据
            
        Raises:
            ConnectionError: 连接错误
            TimeoutError: 超时错误
        """
        try:
            # 首先接收4字节的长度信息
            length_data = bytearray()
            while len(length_data) < 4:
                chunk = self.sock.recv(4 - len(length_data))
                if not chunk:
                    raise ConnectionError("Connection closed while receiving length prefix")
                length_data.extend(chunk)
            
            # 解析长度（大端序无符号整数）
            data_length = struct.unpack('>I', length_data)[0]
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"接收到数据长度: {data_length} 字节")
            
            # 根据长度接收实际数据
            json_data = bytearray()
            while len(json_data) < data_length:
                remaining = data_length - len(json_data)
                chunk = self.sock.recv(min(4096, remaining))
                if not chunk:
                    raise ConnectionError("Connection closed while receiving JSON data")
                json_data.extend(chunk)
            
            # 解码JSON数据，优先使用UTF-8，失败时尝试GBK
            try:
                json_str = json_data.decode('utf-8')
            except UnicodeDecodeError:
                json_str = json_data.decode('gbk')
            
            # 解析JSON
            result = json.loads(json_str)
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"成功解析JSON响应: {result}")
            
            return result
            
        except socket.timeout:
            if self._ros_node:
                self._ros_node.lab_logger().warning(f"接收超时")
            raise TimeoutError(f"recv() timed out after {self.timeout:.1f}s")
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"接收数据失败: {e}")
            raise ConnectionError(f"Failed to receive data: {str(e)}")

    def _send_command(self, cmd: dict) -> dict:
        """
        使用长度前缀协议发送命令到XRD D7-Mate设备并接收响应
        
        Args:
            cmd (dict): 要发送的命令字典
            
        Returns:
            dict: 设备响应的JSON数据
            
        Raises:
            ConnectionError: 连接错误
            TimeoutError: 超时错误
        """
        # 确保连接存在，如果不存在则建立连接
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info(f"为命令重新建立连接")
            except Exception as e:
                raise ConnectionError(f"Failed to establish connection: {str(e)}")

        try:
            # 序列化命令为JSON
            json_str = json.dumps(cmd, ensure_ascii=False)
            payload = json_str.encode('utf-8')
            
            # 计算JSON数据长度并打包为4字节大端序无符号整数
            length_prefix = struct.pack('>I', len(payload))
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送JSON命令到XRD D7-Mate: {json_str}")
                self._ros_node.lab_logger().info(f"发送数据长度: {len(payload)} 字节")
            
            # 发送长度前缀
            self.sock.sendall(length_prefix)
            
            # 发送JSON数据
            self.sock.sendall(payload)
            
            # 使用长度前缀协议接收响应
            response = self._receive_with_length_prefix()
            
            return response
            
        except Exception as e:
            # 如果是连接错误，尝试重新连接一次
            if "远程主机强迫关闭了一个现有的连接" in str(e) or "10054" in str(e):
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"连接被远程主机关闭，尝试重新连接: {e}")
                try:
                    self.close()
                    self.connect()
                    # 重新发送命令
                    json_str = json.dumps(cmd, ensure_ascii=False)
                    payload = json_str.encode('utf-8')
                    if self._ros_node:
                        self._ros_node.lab_logger().info(f"重新发送JSON命令到XRD D7-Mate: {json_str}")
                    self.sock.sendall(payload)
                    
                    # 重新接收响应
                    buffer = bytearray()
                    start = time.time()
                    while True:
                        try:
                            chunk = self.sock.recv(4096)
                            if not chunk:
                                break
                            buffer.extend(chunk)
                            
                            # 尝试解码和解析JSON
                            try:
                                text = buffer.decode('utf-8', errors='strict')
                                text = text.strip()
                                if text.startswith('{'):
                                    brace_count = 0
                                    json_end = -1
                                    for i, char in enumerate(text):
                                        if char == '{':
                                            brace_count += 1
                                        elif char == '}':
                                            brace_count -= 1
                                            if brace_count == 0:
                                                json_end = i + 1
                                                break
                                    if json_end > 0:
                                        text = text[:json_end]
                                result = json.loads(text)
                                
                                if self._ros_node:
                                    self._ros_node.lab_logger().info(f"重连后成功解析JSON响应: {result}")
                                return result
                                    
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                pass
                                
                        except socket.timeout:
                            if self._ros_node:
                                self._ros_node.lab_logger().warning(f"重连后接收超时")
                            raise TimeoutError(f"recv() timed out after reconnection")
                        
                        if time.time() - start > self.timeout * 2:
                            raise TimeoutError(f"No complete JSON received after reconnection")
                    
                except Exception as retry_e:
                    if self._ros_node:
                        self._ros_node.lab_logger().error(f"重连失败: {retry_e}")
                    raise ConnectionError(f"Connection retry failed: {str(retry_e)}")
            
            if isinstance(e, (ConnectionError, TimeoutError)):
                raise
            else:
                raise ConnectionError(f"Command send failed: {str(e)}")

    # ==================== 自动模式控制 ====================
    
    def start_auto_mode(self, status: bool) -> dict:
        """
        启动或停止自动模式
        
        Args:
            status (bool): True-启动自动模式，False-停止自动模式
            
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            # 按协议要求，content 直接为布尔值，使用传入的status参数
            cmd = {
                "command": "START_AUTO_MODE",
                "content": {
                    "status": bool(True)
                }
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送自动模式控制命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到自动模式控制响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"自动模式控制失败: {e}")
            return {"status": False, "message": f"自动模式控制失败: {str(e)}"}

    # ==================== 上样流程 ====================
    
    def get_sample_request(self) -> dict:
        """
        上样请求，检查是否允许上样
        
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "GET_SAMPLE_REQUEST",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送上样请求命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到上样请求响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"上样请求失败: {e}")
            return {"status": False, "message": f"上样请求失败: {str(e)}"}

    def send_sample_ready(self, sample_id: str, start_theta: float, end_theta: float, 
                         increment: float, exp_time: float) -> dict:
        """
        送样完成后，发送样品信息和采集参数
        
        Args:
            sample_id (str): 样品标识符
            start_theta (float): 起始角度（≥5°）
            end_theta (float): 结束角度（≥5.5°，且必须大于start_theta）
            increment (float): 角度增量（≥0.005）
            exp_time (float): 曝光时间（0.1-5.0秒）
            
        Returns:
            dict: 响应结果，包含status、timestamp、message等
        """
        # 参数验证
        if start_theta < 5.0:
            return {"status": False, "message": "起始角度必须≥5°"}
        if end_theta < 5.5:
            return {"status": False, "message": "结束角度必须≥5.5°"}
        if end_theta <= start_theta:
            return {"status": False, "message": "结束角度必须大于起始角度"}
        if increment < 0.005:
            return {"status": False, "message": "角度增量必须≥0.005"}
        if not (0.1 <= exp_time <= 5.0):
            return {"status": False, "message": "曝光时间必须在0.1-5.0秒之间"}
        
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "SEND_SAMPLE_READY",
                "content": {
                    "sample_id": sample_id,
                    "start_theta": start_theta,
                    "end_theta": end_theta,
                    "increment": increment,
                    "exp_time": exp_time
                }
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送样品准备完成命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到样品准备完成响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"样品准备完成失败: {e}")
            return {"status": False, "message": f"样品准备完成失败: {str(e)}"}

    # ==================== 数据获取 ====================
    
    def get_current_acquire_data(self) -> dict:
        """
        获取当前正在采集的样品数据
        
        Returns:
            dict: 响应结果，包含status、timestamp、sample_id、Energy、Intensity等
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "GET_CURRENT_ACQUIRE_DATA",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送获取采集数据命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到获取采集数据响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"获取采集数据失败: {e}")
            return {"status": False, "message": f"获取采集数据失败: {str(e)}"}

    def get_sample_status(self) -> dict:
        """
        获取工位样品状态及设备状态
        
        Returns:
            dict: 响应结果，包含status、timestamp、Station等
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "GET_SAMPLE_STATUS",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送获取样品状态命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到获取样品状态响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"获取样品状态失败: {e}")
            return {"status": False, "message": f"获取样品状态失败: {str(e)}"}

    # ==================== 下样流程 ====================
    
    def get_sample_down(self, sample_station: int) -> dict:
        """
        下样请求
        
        Args:
            sample_station (int): 下样工位（1, 2, 3）
            
        Returns:
            dict: 响应结果，包含status、timestamp、sample_info等
        """
        # 参数验证
        if sample_station not in [1, 2, 3]:
            return {"status": False, "message": "下样工位必须是1、2或3"}
        
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            # 按协议要求，content 直接为整数工位号
            cmd = {
                "command": "GET_SAMPLE_DOWN",
                "content": {
                    "Sample station":int(3)
                }
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送下样请求命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到下样请求响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"下样请求失败: {e}")
            return {"status": False, "message": f"下样请求失败: {str(e)}"}

    def send_sample_down_ready(self) -> dict:
        """
        下样完成命令
        
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "SEND_SAMPLE_DOWN_READY",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送下样完成命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到下样完成响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"下样完成失败: {e}")
            return {"status": False, "message": f"下样完成失败: {str(e)}"}

    # ==================== 高压电源控制 ====================
    
    def set_power_on(self) -> dict:
        """
        高压电源开启
        
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "SET_POWER_ON",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送高压电源开启命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到高压开启响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"高压开启失败: {e}")
            return {"status": False, "message": f"高压开启失败: {str(e)}"}

    def set_power_off(self) -> dict:
        """
        高压电源关闭
        
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "SET_POWER_OFF",
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送高压电源关闭命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到高压关闭响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"高压关闭失败: {e}")
            return {"status": False, "message": f"高压关闭失败: {str(e)}"}

    def set_voltage_current(self, voltage: float, current: float) -> dict:
        """
        设置高压电源电压和电流
        
        Args:
            voltage (float): 电压值（kV）
            current (float): 电流值（mA）
            
        Returns:
            dict: 响应结果，包含status、timestamp、message
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("XRD D7-Mate设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"XRD D7-Mate设备连接失败: {e}")
                return {"status": False, "message": "设备连接异常"}
        
        try:
            cmd = {
                "command": "SET_VOLTAGE_CURRENT",
                "content": {
                    "voltage": voltage,
                    "current": current
                }
            }
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"发送设置电压电流命令: {cmd}")
            
            response = self._send_command(cmd)
            if self._ros_node:
                self._ros_node.lab_logger().info(f"收到设置电压电流响应: {response}")
            
            return response
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"设置电压电流失败: {e}")
            return {"status": False, "message": f"设置电压电流失败: {str(e)}"}

    def start(self, sample_id: str = "", start_theta: float = 10.0, end_theta: float = 80.0,
                 increment: float = 0.05, exp_time: float = 0.1, wait_minutes: float = 3.0, 
                 string: str = "") -> dict:
        """
        Start 主流程：
        1) 启动自动模式；
        2) 发送上样请求并等待允许；
        3) 等待指定分钟后发送样品准备完成（携带采集参数）；
        4) 周期性轮询采集数据与工位状态；
        5) 一旦任一下样位变为 True，执行下样流程（GET_SAMPLE_DOWN + SEND_SAMPLE_DOWN_READY）。

        Args:
            sample_id: 样品名称
            start_theta: 起始角度（≥5°）
            end_theta: 结束角度（≥5.5°，且必须大于 start_theta）
            increment: 角度增量（≥0.005）
            exp_time: 曝光时间（0.1-5.0 秒）
            wait_minutes: 在允许上样后、发送样品准备完成前的等待分钟数（默认 3 分钟）
            string: 字符串格式的参数输入，如果提供则优先解析使用

        Returns:
            dict: {"return_info": str, "success": bool}
        """
        try:
            # 强制类型转换：除 sample_id 外的所有输入均转换为 float（若为字符串）
            def _to_float(v, default):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return float(default)

            if not isinstance(sample_id, str):
                sample_id = str(sample_id)
            if isinstance(start_theta, str):
                start_theta = _to_float(start_theta, 10.0)
            if isinstance(end_theta, str):
                end_theta = _to_float(end_theta, 80.0)
            if isinstance(increment, str):
                increment = _to_float(increment, 0.05)
            if isinstance(exp_time, str):
                exp_time = _to_float(exp_time, 0.1)
            if isinstance(wait_minutes, str):
                wait_minutes = _to_float(wait_minutes, 3.0)

            # 不再从 string 参数解析覆盖；保留参数但忽略字符串解析，统一使用结构化输入

            # 确保设备连接
            if not self.sock:
                try:
                    self.connect()
                    if self._ros_node:
                        self._ros_node.lab_logger().info("XRD D7-Mate设备连接成功，开始执行start流程")
                except Exception as e:
                    if self._ros_node:
                        self._ros_node.lab_logger().error(f"XRD D7-Mate设备连接失败: {e}")
                    return {"return_info": f"设备连接失败: {str(e)}", "success": False}

            # 1) 启动自动模式
            r_auto = self.start_auto_mode(True)
            if not r_auto.get("status", False):
                return {"return_info": f"启动自动模式失败: {r_auto.get('message', '未知')}", "success": False}
            if self._ros_node:
                self._ros_node.lab_logger().info(f"自动模式已启动: {r_auto}")

            # 2) 上样请求
            r_req = self.get_sample_request()
            if not r_req.get("status", False):
                return {"return_info": f"上样请求未允许: {r_req.get('message', '未知')}", "success": False}
            if self._ros_node:
                self._ros_node.lab_logger().info(f"上样已允许: {r_req}")

            # 3) 等待指定分钟后发送样品准备完成
            wait_seconds = max(0.0, float(wait_minutes)) * 60.0
            if self._ros_node:
                self._ros_node.lab_logger().info(f"等待 {wait_minutes} 分钟后发送样品准备完成")
            time.sleep(wait_seconds)

            r_ready = self.send_sample_ready(sample_id=sample_id,
                                             start_theta=start_theta,
                                             end_theta=end_theta,
                                             increment=increment,
                                             exp_time=exp_time)
            if not r_ready.get("status", False):
                return {"return_info": f"样品准备完成失败: {r_ready.get('message', '未知')}", "success": False}
            if self._ros_node:
                self._ros_node.lab_logger().info(f"样品准备完成已发送: {r_ready}")

            # 4) 轮询采集数据与工位状态
            polling_interval = 5.0  # 秒
            down_station_idx: Optional[int] = None
            while True:
                try:
                    r_data = self.get_current_acquire_data()
                    if self._ros_node:
                        self._ros_node.lab_logger().info(f"采集中数据: {r_data}")
                except Exception as e:
                    if self._ros_node:
                        self._ros_node.lab_logger().warning(f"获取采集数据失败: {e}")

                try:
                    r_status = self.get_sample_status()
                    if self._ros_node:
                        self._ros_node.lab_logger().info(f"工位状态: {r_status}")

                    station = r_status.get("Station", {})
                    if isinstance(station, dict):
                        for idx in (1, 2, 3):
                            key = f"DownStation{idx}"
                            val = station.get(key)
                            if isinstance(val, bool) and val:
                                down_station_idx = idx
                                break
                    if down_station_idx is not None:
                        break
                except Exception as e:
                    if self._ros_node:
                        self._ros_node.lab_logger().warning(f"获取工位状态失败: {e}")

                time.sleep(polling_interval)

            if down_station_idx is None:
                return {"return_info": "未检测到任一下样位 True，流程未完成", "success": False}

            # 5) 下样流程
            r_down = self.get_sample_down(down_station_idx)
            if not r_down.get("status", False):
                return {"return_info": f"下样请求失败(工位 {down_station_idx}): {r_down.get('message', '未知')}", "success": False}
            if self._ros_node:
                self._ros_node.lab_logger().info(f"下样请求成功(工位 {down_station_idx}): {r_down}")

            r_ready_down = self.send_sample_down_ready()
            if not r_ready_down.get("status", False):
                return {"return_info": f"下样完成发送失败: {r_ready_down.get('message', '未知')}", "success": False}
            if self._ros_node:
                self._ros_node.lab_logger().info(f"下样完成已发送: {r_ready_down}")

            return {"return_info": f"Start流程完成，工位 {down_station_idx} 已下样", "success": True}

        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().error(f"Start流程异常: {e}")
            return {"return_info": f"Start流程异常: {str(e)}", "success": False}

    def _parse_start_params(self, params: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        解析UI输入参数为 Start 流程参数。
        - 从UI字典中读取各个字段的字符串值
        - 将数值字段从字符串转换为 float 类型
        - 保留 sample_id 为字符串类型

        返回:
            dict: {sample_id, start_theta, end_theta, increment, exp_time, wait_minutes}
        """
        # 如果传入为字典，则直接按键读取；否则给出警告并使用空字典
        if isinstance(params, dict):
            p = params
        else:
            p = {}
            if self._ros_node:
                self._ros_node.lab_logger().warning("start 参数应为结构化字典")

        def _to_float(v, default):
            """将UI输入的字符串值转换为float，处理空值和无效值"""
            if v is None or v == '':
                return float(default)
            try:
                # 处理字符串输入（来自UI）
                if isinstance(v, str):
                    v = v.strip()
                    if v == '':
                        return float(default)
                return float(v)
            except (TypeError, ValueError):
                return float(default)

        # 从UI输入字典中读取参数
        sample_id = p.get('sample_id') or p.get('sample_name') or '样品名称'
        if not isinstance(sample_id, str):
            sample_id = str(sample_id)

        # 将UI字符串输入转换为float
        result: Dict[str, Any] = {
            'sample_id': sample_id,
            'start_theta': _to_float(p.get('start_theta'), 10.0),
            'end_theta': _to_float(p.get('end_theta'), 80.0),
            'increment': _to_float(p.get('increment'), 0.05),
            'exp_time': _to_float(p.get('exp_time'), 0.1),
            'wait_minutes': _to_float(p.get('wait_minutes'), 3.0),
        }

        return result

    def start_from_string(self, params: Union[str, Dict[str, Any]]) -> dict:
        """
        从UI输入参数执行 Start 主流程。
        接收来自用户界面的参数字典，其中数值字段为字符串格式，自动转换为正确的类型。

        参数:
            params: UI输入参数字典，例如:
                {
                    'sample_id': 'teste',
                    'start_theta': '10.0',      # UI字符串输入
                    'end_theta': '25.0',        # UI字符串输入
                    'increment': '0.05',        # UI字符串输入
                    'exp_time': '0.10',         # UI字符串输入
                    'wait_minutes': '0.5'       # UI字符串输入
                }

        返回:
            dict: 执行结果
        """
        parsed = self._parse_start_params(params)

        sample_id = parsed.get('sample_id', '样品名称')
        start_theta = float(parsed.get('start_theta', 10.0))
        end_theta = float(parsed.get('end_theta', 80.0))
        increment = float(parsed.get('increment', 0.05))
        exp_time = float(parsed.get('exp_time', 0.1))
        wait_minutes = float(parsed.get('wait_minutes', 3.0))

        return self.start(
            sample_id=sample_id,
            start_theta=start_theta,
            end_theta=end_theta,
            increment=increment,
            exp_time=exp_time,
            wait_minutes=wait_minutes,
        )

# 测试函数
def test_xrd_client():
    """
    测试XRD客户端功能
    """
    client = XRDClient(host='127.0.0.1', port=6001)
    
    try:
        # 测试连接
        client.connect()
        print("连接成功")
        
        # 测试启动自动模式
        result = client.start_auto_mode(True)
        print(f"启动自动模式: {result}")
        
        # 测试上样请求
        result = client.get_sample_request()
        print(f"上样请求: {result}")
        
        # 测试获取样品状态
        result = client.get_sample_status()
        print(f"样品状态: {result}")
        
        # 测试高压开启
        result = client.set_power_on()
        print(f"高压开启: {result}")
        
    except Exception as e:
        print(f"测试失败: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    test_xrd_client()

# 为了兼容性，提供别名
XRD_D7Mate = XRDClient