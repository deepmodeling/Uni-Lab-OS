#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智达GCMS设备驱动

支持智达GCMS设备的TCP通信协议，包括状态查询、方法获取、样品分析等功能。
通信协议版本：1.0.1
"""

import base64
import json
import socket
import time
import os
from pathlib import Path
from typing import Dict


class ZhidaClient:
    def __init__(self, host='10.40.6.101', port=5792, timeout=10.0):
        # 如果部署在智达GCMS上位机本地，可使用localhost: host='127.0.0.1'
        """
        初始化智达GCMS客户端
        
        Args:
            host (str): 设备IP地址，本地部署时可使用'127.0.0.1'
            port (int): 通信端口，默认5792
            timeout (float): 超时时间，单位秒
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._ros_node = None  # ROS节点引用，由框架设置
    
    def post_init(self, ros_node):
        """
        ROS节点初始化后的回调方法，用于建立设备连接
        
        Args:
            ros_node: ROS节点实例
        """
        self._ros_node = ros_node
        try:
            self.connect()
            ros_node.lab_logger().info(f"智达GCMS设备连接成功: {self.host}:{self.port}")
        except Exception as e:
            ros_node.lab_logger().error(f"智达GCMS设备连接失败: {e}")
            # 不抛出异常，允许节点继续运行，后续可以重试连接

    def connect(self):
        """
        建立TCP连接到智达GCMS设备
        
        Raises:
            ConnectionError: 连接失败时抛出
        """
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            # 确保后续 recv/send 都会在 timeout 秒后抛 socket.timeout
            self.sock.settimeout(self.timeout)
        except Exception as e:
            raise ConnectionError(f"Failed to connect to {self.host}:{self.port} - {str(e)}")

    def close(self):
        """
        关闭与智达GCMS设备的TCP连接
        """
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass  # 忽略关闭时的错误
            finally:
                self.sock = None

    def _send_command(self, cmd: dict) -> dict:
        """
        发送命令到智达GCMS设备并接收响应
        
        Args:
            cmd (dict): 要发送的命令字典
            
        Returns:
            dict: 设备响应的JSON数据
            
        Raises:
            ConnectionError: 连接错误
            TimeoutError: 超时错误
        """
        if not self.sock:
            raise ConnectionError("Not connected to device")

        try:
            # 发送JSON命令（UTF-8编码）
            payload = json.dumps(cmd, ensure_ascii=False).encode('utf-8')
            self.sock.sendall(payload)

            # 循环接收数据直到能成功解析完整JSON
            buffer = bytearray()
            start = time.time()
            while True:
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        # 对端关闭连接，尝试解析已接收的数据
                        if buffer:
                            try:
                                text = buffer.decode('utf-8', errors='strict')
                                return json.loads(text)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                pass
                        break
                    buffer.extend(chunk)
                    
                    # 尝试解码和解析JSON
                    text = buffer.decode('utf-8', errors='strict')
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        # JSON不完整，继续接收
                        pass
                        
                except socket.timeout:
                    # 超时时，尝试解析已接收的数据
                    if buffer:
                        try:
                            text = buffer.decode('utf-8', errors='strict')
                            return json.loads(text)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    raise TimeoutError(f"recv() timed out after {self.timeout:.1f}s")
                
                # 防止死循环，总时长超过2倍超时时间就报错
                if time.time() - start > self.timeout * 2:
                    # 最后尝试解析已接收的数据
                    if buffer:
                        try:
                            text = buffer.decode('utf-8', errors='strict')
                            return json.loads(text)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    raise TimeoutError(f"No complete JSON received after {time.time() - start:.1f}s")

            # 连接关闭，如果有数据则尝试解析
            if buffer:
                try:
                    text = buffer.decode('utf-8', errors='strict')
                    return json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            
            raise ConnectionError("Connection closed before JSON could be parsed")
            
        except Exception as e:
            if isinstance(e, (ConnectionError, TimeoutError)):
                raise
            else:
                raise ConnectionError(f"Command send failed: {str(e)}")

    @staticmethod
    def _parse_status_detail(raw_status: str) -> Dict[str, str]:
        """
        功能:
        将智达协议返回的原始状态拆分为主状态和子状态, 统一复合状态判定.
        参数:
        raw_status: 协议返回的 result 字段, 例如 "Idle" 或 "Idle#SeqRun:Error".
        返回:
        Dict[str, str], 包含 raw_status/base_status/sub_status 三个键.
        """
        normalized_raw_status = raw_status.strip() if isinstance(raw_status, str) else ""
        if normalized_raw_status == "":
            return {"raw_status": "", "base_status": "Unknown", "sub_status": ""}

        base_status, separator, sub_status = normalized_raw_status.partition("#")
        base_status = base_status.strip() or "Unknown"
        sub_status = sub_status.strip() if separator else ""
        return {
            "raw_status": normalized_raw_status,
            "base_status": base_status,
            "sub_status": sub_status,
        }

    def get_status_detail(self) -> Dict[str, str]:
        """
        功能:
        查询设备状态并返回原始状态、归一化主状态和子状态, 便于流程判定与排障.
        参数:
        无.
        返回:
        Dict[str, str], 形如 {"raw_status": str, "base_status": str, "sub_status": str}.
        """
        if not self.sock:
            # 尝试重新连接, 避免长连接断开后状态查询直接失败.
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("智达GCMS设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"智达GCMS设备连接失败: {e}")
                return self._parse_status_detail("Offline")

        try:
            response = self._send_command({"command": "getstatus"})
            return self._parse_status_detail(response.get("result", ""))
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().warning(f"获取设备状态失败: {e}")
            return self._parse_status_detail("Error")

    def get_status(self) -> str:
        """
        功能:
        获取归一化后的设备主状态, 兼容旧调用方的字符串状态接口.
        参数:
        无.
        返回:
        str, 设备主状态. 复合状态如 "Idle#SeqRun:Error" 会返回 "Idle".
        """
        return self.get_status_detail()["base_status"]
    
    def get_methods(self) -> dict:
        """
        获取当前Project的方法列表
        
        Returns:
            dict: 包含方法列表的响应
        """
        if not self.sock:
            try:
                self.connect()
                if self._ros_node:
                    self._ros_node.lab_logger().info("智达GCMS设备重新连接成功")
            except Exception as e:
                if self._ros_node:
                    self._ros_node.lab_logger().warning(f"智达GCMS设备连接失败: {e}")
                return {"error": "Device not connected"}
        
        try:
            return self._send_command({"command": "getmethods"})
        except Exception as e:
            if self._ros_node:
                self._ros_node.lab_logger().warning(f"获取方法列表失败: {e}")
            return {"error": str(e)}

    def get_version(self) -> dict:
        """
        获取接口版本和InLabPAL固件版本
        
        Returns:
            dict: 响应格式 {"result": "OK|Error", "message": "Interface:x.x.x;FW:x.x.x.xxx"}
        """
        return self._send_command({"command": "version"})
    
    def put_tray(self) -> dict:
        """
        放盘操作，准备InLabPAL进样器
        
        注意：此功能仅在特殊场景下使用，例如：
        - 机械臂比较短，需要让开一个位置
        - 盘支架是可移动的，需要进样器配合做动作
        
        对于宜宾深势这套配置，空间足够，不需要这个额外的控制组件。
        
        Returns:
            dict: 响应格式 {"result": "OK|Error", "message": "ready_info|error_info"}
        """
        return self._send_command({"command": "puttray"})

    def start_with_csv_file(self, string: str = None, csv_file_path: str = None) -> dict:
        """
        使用CSV文件启动分析（支持ROS2动作调用）
        
        Args:
            string (str): CSV文件路径（ROS2参数名）
            csv_file_path (str): CSV文件路径（兼容旧接口）
            
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
            
        Raises:
            FileNotFoundError: CSV文件不存在
            Exception: 文件读取或通信错误
        """
        try:
            # 支持两种参数传递方式：ROS2的string参数和直接的csv_file_path参数
            file_path = string if string is not None else csv_file_path
            if file_path is None:
                error_msg = "未提供CSV文件路径参数"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False}
            
            # 使用Path对象进行更健壮的文件处理
            csv_path = Path(file_path)
            if not csv_path.exists():
                error_msg = f"CSV文件不存在: {file_path}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False}
            
            # 读取CSV文件内容（UTF-8编码，替换未知字符）
            csv_content = csv_path.read_text(encoding="utf-8", errors="replace")
            
            # 转换为Base64编码
            b64_content = base64.b64encode(csv_content.encode('utf-8')).decode('ascii')
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"正在发送CSV文件到智达GCMS: {file_path}")
                self._ros_node.lab_logger().info(f"Base64编码长度: {len(b64_content)} 字符")
            
            # 发送start命令
            response = self._send_command({
                "command": "start",
                "message": b64_content
            })
            
            # 转换为ROS2动作结果格式
            if response.get("result") == "OK":
                success_msg = f"智达GCMS分析启动成功: {response.get('message', 'Unknown')}"
                if self._ros_node:
                    self._ros_node.lab_logger().info(success_msg)
                return {"return_info": success_msg, "success": True}
            else:
                error_msg = f"智达GCMS分析启动失败: {response.get('message', 'Unknown error')}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False}
            
        except Exception as e:
            error_msg = f"CSV文件处理失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def start(self, string: str = None, text: str = None) -> dict:
        """
        使用Base64编码数据启动分析（支持ROS2动作调用）
        
        Args:
            string (str): Base64编码的CSV数据（ROS2参数名）
            text (str): Base64编码的CSV数据（兼容旧接口）
            
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool}
        """
        try:
            # 支持两种参数传递方式：ROS2的string参数和原有的text参数
            b64_content = string if string is not None else text
            if b64_content is None:
                error_msg = "未提供Base64编码数据参数"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False}
            
            if self._ros_node:
                self._ros_node.lab_logger().info(f"正在发送Base64数据到智达GCMS")
                self._ros_node.lab_logger().info(f"Base64编码长度: {len(b64_content)} 字符")
            
            # 发送start命令
            response = self._send_command({
                "command": "start",
                "message": b64_content
            })
            
            # 转换为ROS2动作结果格式
            if response.get("result") == "OK":
                success_msg = f"智达GCMS分析启动成功: {response.get('message', 'Unknown')}"
                if self._ros_node:
                    self._ros_node.lab_logger().info(success_msg)
                return {"return_info": success_msg, "success": True}
            else:
                error_msg = f"智达GCMS分析启动失败: {response.get('message', 'Unknown error')}"
                if self._ros_node:
                    self._ros_node.lab_logger().error(error_msg)
                return {"return_info": error_msg, "success": False}
            
        except Exception as e:
            error_msg = f"Base64数据处理失败: {str(e)}"
            if self._ros_node:
                self._ros_node.lab_logger().error(error_msg)
            return {"return_info": error_msg, "success": False}

    def abort(self) -> dict:
        """
        停止当前运行的分析
        
        Returns:
            dict: 响应格式 {"result": "OK|Error", "message": "error_info"}
        """
        return self._send_command({"command": "abort"})


def test_zhida_client():
    """
    测试智达GCMS客户端功能
    """
    client = ZhidaClient()
    
    try:
        # 连接设备
        print("Connecting to Zhida GCMS...")
        client.connect()
        print("Connected successfully!")
        
        # 获取设备状态
        print(f"Device status: {client.get_status()}")
        
        # 获取版本信息
        version_info = client.get_version()
        print(f"Version info: {version_info}")
        
        # 获取方法列表
        methods = client.get_methods()
        print(f"Available methods: {methods}")
        
        # # 测试CSV文件发送（如果文件存在）
        # csv_file = Path(__file__).parent / "zhida_gcms-test_1.csv"
        # if csv_file.exists():
        #     print(f"Testing CSV file: {csv_file}")
        #     result = client.start_with_csv_file(str(csv_file))
        #     print(f"Start result: {result}")
        
    except Exception as e:
        print(f"Error: {str(e)}")
    
    finally:
        # 关闭连接
        client.close()
        print("Connection closed.")


if __name__ == "__main__":
    test_zhida_client()
