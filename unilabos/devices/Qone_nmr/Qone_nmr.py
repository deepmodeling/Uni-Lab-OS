#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oxford NMR Device Driver for Uni-Lab OS

支持Oxford NMR设备的CSV字符串到TXT文件转换功能。
通过ROS2动作接口接收CSV字符串，批量生成TXT文件到指定目录。
"""

import csv
import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Any

class UniversalDriver:
    """Fallback UniversalDriver for standalone testing"""
    def __init__(self):
        self.success = False

class Qone_nmr(UniversalDriver):
    """Oxford NMR Device Driver
    
    支持CSV字符串到TXT文件的批量转换功能。
    """
    
    def __init__(self, **kwargs):
        """Initialize the Oxford NMR driver
        
        Args:
            **kwargs: Device-specific configuration parameters
                - config: Configuration dictionary containing output_dir
        """
        super().__init__()
        
        # Device configuration
        self.config = kwargs
        config_dict = kwargs.get('config', {})
        
        # 设置输出目录，优先使用配置中的output_dir，否则使用默认值
        self.output_directory = "D:\\setup\\txt"  # 默认输出目录
        if config_dict and 'output_dir' in config_dict:
            self.output_directory = config_dict['output_dir']
            
        # 设置监督目录，优先使用配置中的monitor_dir，否则使用默认值
        self.monitor_directory = "D:/Data/MyPC/Automation"  # 默认监督目录
        if config_dict and 'monitor_dir' in config_dict:
            self.monitor_directory = config_dict['monitor_dir']
            
        # 设置文件大小稳定性检查参数
        self.stability_checks = 3  # 默认稳定性检查次数
        if config_dict and 'stability_checks' in config_dict:
            self.stability_checks = config_dict['stability_checks']
            
        # 设置检查间隔时间
        self.check_interval = 60  # 默认检查间隔（秒）
        if config_dict and 'check_interval' in config_dict:
            self.check_interval = config_dict['check_interval']
            
        # 确保输出目录存在
        os.makedirs(self.output_directory, exist_ok=True)
        
        # ROS节点引用，由框架设置
        self._ros_node = None
            
        # ROS2 action result properties
        self.success = False
        self.return_info = ""
        
        # Setup logging
        self.logger = logging.getLogger(f"Qone_nmr-{kwargs.get('id', 'unknown')}")
        self.logger.info(f"Oxford NMR driver initialized with output directory: {self.output_directory}")
        self.logger.info(f"Monitor directory set to: {self.monitor_directory}")
        self.logger.info(f"Stability checks: {self.stability_checks}, Check interval: {self.check_interval}s")
    
    def post_init(self, ros_node):
        """ROS节点初始化后的回调方法
        
        Args:
            ros_node: ROS节点实例
        """
        self._ros_node = ros_node
        ros_node.lab_logger().info(f"Oxford NMR设备初始化完成，输出目录: {self.output_directory}")
    
    def get_status(self) -> str:
        """获取设备状态
        
        Returns:
            str: 设备状态 (Idle|Offline|Error|Busy|Unknown)
        """
        return "Idle"  # NMR设备始终处于空闲状态，等待处理请求
    
    def strings_to_txt(self, string_list, output_dir=None, txt_encoding="utf-8"):
        """
        将字符串列表写入多个 txt 文件
        string_list: ["A 1 B 1 C 1 D 1 E 1 F 1 G 1 H 1 END", ...]
        
        Args:
            string_list: 字符串列表
            output_dir: 输出目录（如果未指定，使用self.output_directory）
            txt_encoding: 文件编码
            
        Returns:
            int: 生成的文件数量
        """
        # 使用指定的输出目录或默认目录
        target_dir = output_dir if output_dir else self.output_directory
        
        # 确保输出目录存在
        os.makedirs(target_dir, exist_ok=True)
        
        self.logger.info(f"开始生成文件到目录: {target_dir}")

        for i, s in enumerate(string_list, start=1):
            try:
                # 去掉开头结尾的引号（如果有）
                s = s.strip('"').strip("'")

                # 拆分字符串
                parts = s.split()

                # 按两两一组重新排版为多行
                txt_lines = []
                for j in range(0, len(parts) - 1, 2):
                    txt_lines.append("{} {}".format(parts[j], parts[j+1]))
                txt_lines.append("END")

                txt_content = "\n".join(txt_lines)

                # 生成文件名（row_1.txt, row_2.txt, ...）
                file_name = "row_{}.txt".format(i)
                out_path = os.path.join(target_dir, file_name)

                with open(out_path, "w", encoding=txt_encoding) as f:
                    f.write(txt_content)
                    
                self.logger.info(f"成功生成文件: {file_name}")

            except Exception as e:
                self.logger.error(f"处理第{i}个字符串时出错: {str(e)}")
                raise

        return len(string_list)  # 返回生成文件数量
    
    def monitor_folder_for_new_content(self, monitor_dir=None, check_interval=60, expected_count=1, stability_checks=3):
        """监督指定文件夹中.nmr文件的大小变化，当文件大小稳定时认为文件完成
        
        Args:
            monitor_dir (str): 要监督的目录路径，如果未指定则使用self.monitor_directory
            check_interval (int): 检查间隔时间（秒），默认60秒
            expected_count (int): 期望生成的.nmr文件数量，默认1个
            stability_checks (int): 文件大小稳定性检查次数，默认3次
            
        Returns:
            bool: 如果检测到期望数量的.nmr文件且大小稳定返回True，否则返回False
        """
        target_dir = monitor_dir if monitor_dir else self.monitor_directory
        
        # 确保监督目录存在
        if not os.path.exists(target_dir):
            self.logger.warning(f"监督目录不存在: {target_dir}")
            return False
            
        self.logger.info(f"开始监督目录: {target_dir}，检查间隔: {check_interval}秒，期望.nmr文件数量: {expected_count}，稳定性检查: {stability_checks}次")
        
        # 记录初始的.nmr文件及其大小
        initial_nmr_files = {}
        
        try:
            for root, dirs, files in os.walk(target_dir):
                for file in files:
                    if file.lower().endswith('.nmr'):
                        file_path = os.path.join(root, file)
                        try:
                            file_size = os.path.getsize(file_path)
                            initial_nmr_files[file_path] = file_size
                        except OSError:
                            pass  # 忽略无法访问的文件
        except Exception as e:
            self.logger.error(f"读取初始目录状态失败: {str(e)}")
            return False
            
        self.logger.info(f"初始状态: {len(initial_nmr_files)} 个.nmr文件")
        
        # 跟踪新文件的大小变化历史
        new_files_size_history = {}
        completed_files = set()
        
        # 开始监督循环
        while True:
            time.sleep(check_interval)
            
            current_nmr_files = {}
            
            try:
                for root, dirs, files in os.walk(target_dir):
                    for file in files:
                        if file.lower().endswith('.nmr'):
                            file_path = os.path.join(root, file)
                            try:
                                file_size = os.path.getsize(file_path)
                                current_nmr_files[file_path] = file_size
                            except OSError:
                                pass
                            
                # 找出新生成的.nmr文件
                new_nmr_files = set(current_nmr_files.keys()) - set(initial_nmr_files.keys())
                
                if len(new_nmr_files) < expected_count:
                    self.logger.info(f"检测到 {len(new_nmr_files)} 个新.nmr文件，还需要 {expected_count - len(new_nmr_files)} 个...")
                    continue
                
                # 检查新文件的大小稳定性
                for file_path in new_nmr_files:
                    if file_path in completed_files:
                        continue
                        
                    current_size = current_nmr_files.get(file_path, 0)
                    
                    # 初始化文件大小历史记录
                    if file_path not in new_files_size_history:
                        new_files_size_history[file_path] = []
                    
                    # 记录当前大小
                    new_files_size_history[file_path].append(current_size)
                    
                    # 保持历史记录长度不超过稳定性检查次数
                    if len(new_files_size_history[file_path]) > stability_checks:
                        new_files_size_history[file_path] = new_files_size_history[file_path][-stability_checks:]
                    
                    # 检查大小是否稳定
                    size_history = new_files_size_history[file_path]
                    if len(size_history) >= stability_checks:
                        # 检查最近几次的大小是否相同且不为0
                        if len(set(size_history[-stability_checks:])) == 1 and size_history[-1] > 0:
                            self.logger.info(f"文件大小已稳定: {file_path} (大小: {current_size} 字节)")
                            completed_files.add(file_path)
                        else:
                            self.logger.debug(f"文件大小仍在变化: {file_path} (当前: {current_size} 字节, 历史: {size_history[-3:]})")
                    else:
                        self.logger.debug(f"文件大小监测中: {file_path} (当前: {current_size} 字节, 检查次数: {len(size_history)}/{stability_checks})")
                
                # 检查是否所有期望的文件都已完成
                if len(completed_files) >= expected_count:
                    self.logger.info(f"所有期望的.nmr文件都已完成生成! 完成文件数: {len(completed_files)}/{expected_count}")
                    for completed_file in list(completed_files)[:expected_count]:
                        final_size = current_nmr_files.get(completed_file, 0)
                        self.logger.info(f"完成的.nmr文件: {completed_file} (最终大小: {final_size} 字节)")
                    self.logger.info("停止文件夹监测，所有文件已完成")
                    return True
                else:
                    self.logger.info(f"已完成 {len(completed_files)} 个文件，还需要 {expected_count - len(completed_files)} 个文件完成...")
                
            except Exception as e:
                self.logger.error(f"监督过程中出错: {str(e)}")
                return False
    
    def start(self, string: str = None) -> dict:
        """使用字符串列表启动TXT文件生成（支持ROS2动作调用）
        
        Args:
            string (str): 包含多个字符串的输入数据，支持两种格式：
                        1. 逗号分隔：如 "A 1 B 2 C 3, X 10 Y 20 Z 30"
                        2. 换行分隔：如 "A 1 B 2 C 3\nX 10 Y 20 Z 30"
            
        Returns:
            dict: ROS2动作结果格式 {"return_info": str, "success": bool, "files_generated": int}
        """
        try:
            if string is None or string.strip() == "":
                error_msg = "未提供字符串参数或参数为空"
                self.logger.error(error_msg)
                return {"return_info": error_msg, "success": False, "files_generated": 0}
            
            self.logger.info(f"开始处理字符串数据，长度: {len(string)} 字符")
            
            # 支持两种分隔方式：逗号分隔或换行分隔
            string_list = []
            
            # 首先尝试逗号分隔
            if ',' in string:
                string_list = [item.strip() for item in string.split(',') if item.strip()]
            else:
                # 如果没有逗号，则按换行分隔
                string_list = [line.strip() for line in string.strip().split('\n') if line.strip()]
            
            if not string_list:
                error_msg = "输入字符串解析后为空"
                self.logger.error(error_msg)
                return {"return_info": error_msg, "success": False, "files_generated": 0}
            
            # 确保输出目录存在
            os.makedirs(self.output_directory, exist_ok=True)
            
            # 使用strings_to_txt函数生成TXT文件
            file_count = self.strings_to_txt(
                string_list=string_list,
                output_dir=self.output_directory,
                txt_encoding='utf-8'
            )
            
            success_msg = f"Oxford NMR处理完成: 已生成 {file_count} 个 txt 文件，保存在: {self.output_directory}"
            self.logger.info(success_msg)
            
            # 在string转txt完成后，启动文件夹监督功能
            self.logger.info(f"开始启动文件夹监督功能，期望生成 {file_count} 个.nmr文件...")
            monitor_result = self.monitor_folder_for_new_content(
                expected_count=file_count,
                check_interval=self.check_interval,
                stability_checks=self.stability_checks
            )
            
            if monitor_result:
                success_msg += f" | 监督完成: 成功检测到 {file_count} 个.nmr文件已完成生成，start函数执行完毕"
            else:
                success_msg += f" | 监督结束: 监督过程中断或失败，start函数执行完毕"
            
            return {"return_info": success_msg, "success": True, "files_generated": file_count}
            
        except Exception as e:
            error_msg = f"字符串处理失败: {str(e)}"
            self.logger.error(error_msg)
            return {"return_info": error_msg, "success": False, "files_generated": 0}
    


def test_qone_nmr():
    """测试Qone_nmr设备的字符串处理功能"""
    try:
        # 配置日志输出
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        logger = logging.getLogger("Qone_nmr_test")
        
        logger.info("开始测试Qone_nmr设备...")
        
        # 创建设备实例，使用正确的配置格式
        device = Qone_nmr(config={'output_dir': "D:\\setup\\txt"})
        logger.info(f"设备初始化完成，输出目录: {device.output_directory}")
        
        # 测试数据：多个字符串，逗号分隔
        test_strings = "A 1 B 1 C 1 D 1 E 1 F 1 G 1 H 1 END, A 2 B 2 C 2 D 2 E 2 F 2 G 2 H 2 END"
        logger.info(f"测试输入: {test_strings}")
        
        # 确保输出目录存在
        if not os.path.exists(device.output_directory):
            os.makedirs(device.output_directory, exist_ok=True)
            logger.info(f"创建输出目录: {device.output_directory}")
        
        # 调用start方法
        result = device.start(string=test_strings)
        logger.info(f"处理结果: {result}")
        
        # 显示生成的文件内容
        if result.get('success', False):
            output_dir = device.output_directory
            if os.path.exists(output_dir):
                txt_files = [f for f in os.listdir(output_dir) if f.endswith('.txt')]
                logger.info(f"生成的文件数量: {len(txt_files)}")
                for i, filename in enumerate(txt_files[:2]):  # 只显示前2个文件
                    filepath = os.path.join(output_dir, filename)
                    logger.info(f"文件 {i+1}: {filename}")
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                        logger.info(f"内容:\n{content}")
        
        logger.info("测试完成!")
        return result
    except Exception as e:
        logger.error(f"测试过程中出现错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"return_info": f"测试失败: {str(e)}", "success": False, "files_generated": 0}


if __name__ == "__main__":
    test_qone_nmr()