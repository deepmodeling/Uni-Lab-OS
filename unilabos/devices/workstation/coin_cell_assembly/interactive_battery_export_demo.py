#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扣式电池组装系统 - 交互式CSV导出演示脚本（增强版）

此脚本专为交互式使用优化，提供清洁的命令行界面，
禁用了所有调试信息输出，确保用户可以顺畅地输入命令。

主要功能:
1. 手动导出设备数据到CSV文件（包含6个关键数据字段）
2. 查看CSV文件内容和导出状态
3. 兼容原有的电池组装完成状态自动导出功能
4. 实时查看设备数据和电池数量

数据字段:
- timestamp: 时间戳
- assembly_time: 单颗电池组装时间（秒）
- open_circuit_voltage: 开路电压值（V）
- pole_weight: 正极片称重数据（g）
- battery_qr_code: 电池二维码序列号
- electrolyte_qr_code: 电解液二维码序列号

使用方法:
1. 确保设备已连接并可正常通信
2. 运行此脚本: python interactive_battery_export_demo.py
3. 使用交互式命令控制导出功能
"""

import time
import os
import sys
import logging
import csv
from datetime import datetime
from pathlib import Path

# 完全禁用所有调试和信息级别的日志输出
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger('pymodbus').setLevel(logging.CRITICAL)
logging.getLogger('unilabos').setLevel(logging.CRITICAL)
logging.getLogger('pymodbus.logging').setLevel(logging.CRITICAL)
logging.getLogger('pymodbus.logging.tcp').setLevel(logging.CRITICAL)
logging.getLogger('pymodbus.logging.base').setLevel(logging.CRITICAL)
logging.getLogger('pymodbus.logging.decoders').setLevel(logging.CRITICAL)

# 添加当前目录到Python路径，以便正确导入模块
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir.parent.parent.parent))  # 添加unilabos根目录
sys.path.insert(0, str(current_dir))  # 添加当前目录

# 导入扣式电池组装系统
try:
    from unilabos.devices.coin_cell_assembly.coin_cell_assembly_system import Coin_Cell_Assembly
except ImportError:
    # 如果上述导入失败，尝试直接导入
    try:
        from coin_cell_assembly_system import Coin_Cell_Assembly
    except ImportError as e:
        print(f"导入错误: {e}")
        print("请确保在正确的目录下运行此脚本，或者将unilabos添加到Python路径中")
        sys.exit(1)

def clear_screen():
    """清屏函数"""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header():
    """打印程序头部信息"""
    print("="*60)
    print("    扣式电池组装系统 - 交互式CSV导出控制台")
    print("="*60)
    print()

def print_commands():
    """打印可用命令"""
    print("可用命令:")
    print("  start   - 启动电池组装完成状态导出")
    print("  stop    - 停止导出")
    print("  status  - 查看导出状态")
    print("  data    - 查看当前设备数据")
    print("  count   - 查看当前电池数量")
    print("  export  - 手动导出当前数据到CSV")
    print("  setpath - 设置自定义CSV文件路径")
    print("  view    - 查看CSV文件内容")
    print("  force   - 强制继续CSV导出(即使设备停止)")
    print("  detail  - 显示详细设备状态")
    print("  clear   - 清屏")
    print("  help    - 显示帮助信息")
    print("  quit    - 退出程序")
    print("-"*60)

def print_status_info(device, csv_file_path):
    """打印状态信息"""
    try:
        status = device.get_csv_export_status()
        is_running = status.get('running', False)
        export_file = status.get('file_path', None)
        thread_alive = status.get('thread_alive', False)
        device_status = status.get('device_status', 'N/A')
        battery_count = status.get('battery_count', 'N/A')
        
        print(f"导出状态: {'运行中' if is_running else '已停止'}")
        print(f"导出文件: {export_file if export_file else 'N/A'}")
        print(f"线程状态: {'活跃' if thread_alive else '非活跃'}")
        print(f"设备状态: {device_status}")
        print(f"电池计数: {battery_count}")
        
        # 检查手动导出的CSV文件
        if os.path.exists(csv_file_path):
            file_size = os.path.getsize(csv_file_path)
            print(f"手动导出文件: {csv_file_path} ({file_size} 字节)")
        else:
            print(f"手动导出文件: {csv_file_path} (不存在)")
            
        # 显示设备运行状态
        try:
            print("\n=== 设备运行状态 ===")
            print(f"系统启动状态: {device.sys_start_status}")
            print(f"系统停止状态: {device.sys_stop_status}")
            print(f"自动模式状态: {device.sys_auto_status}")
            print(f"手动模式状态: {device.sys_hand_status}")
        except Exception as e:
            print(f"获取设备运行状态失败: {e}")
            
    except Exception as e:
        print(f"获取状态失败: {e}")

def collect_device_data(device):
    """收集设备的六个关键数据"""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 读取各项数据，添加错误处理和重试机制
        try:
            assembly_time = device.data_assembly_time  # 单颗电池组装时间(秒)
            # 确保返回的是数值类型
            if isinstance(assembly_time, (list, tuple)) and len(assembly_time) > 0:
                assembly_time = float(assembly_time[0])
            else:
                assembly_time = float(assembly_time)
        except Exception as e:
            print(f"读取组装时间失败: {e}")
            assembly_time = 0.0
            
        try:
            open_circuit_voltage = device.data_open_circuit_voltage  # 开路电压值(V)
            # 确保返回的是数值类型
            if isinstance(open_circuit_voltage, (list, tuple)) and len(open_circuit_voltage) > 0:
                open_circuit_voltage = float(open_circuit_voltage[0])
            else:
                open_circuit_voltage = float(open_circuit_voltage)
        except Exception as e:
            print(f"读取开路电压失败: {e}")
            open_circuit_voltage = 0.0
            
        try:
            pole_weight = device.data_pole_weight  # 正极片称重数据(g)
            # 确保返回的是数值类型
            if isinstance(pole_weight, (list, tuple)) and len(pole_weight) > 0:
                pole_weight = float(pole_weight[0])
            else:
                pole_weight = float(pole_weight)
        except Exception as e:
            print(f"读取正极片重量失败: {e}")
            pole_weight = 0.0
            
        try:
            assembly_pressure = device.data_assembly_pressure  # 电池压制力(N)
            # 确保返回的是数值类型
            if isinstance(assembly_pressure, (list, tuple)) and len(assembly_pressure) > 0:
                assembly_pressure = int(assembly_pressure[0])
            else:
                assembly_pressure = int(assembly_pressure)
        except Exception as e:
            print(f"读取压制力失败: {e}")
            assembly_pressure = 0
            
        try:
            battery_qr_code = device.data_coin_cell_code  # 电池二维码序列号
            # 处理字符串类型数据
            if isinstance(battery_qr_code, str):
                battery_qr_code = battery_qr_code.strip()
            else:
                battery_qr_code = str(battery_qr_code)
        except Exception as e:
            print(f"读取电池二维码失败: {e}")
            battery_qr_code = "N/A"
            
        try:
            electrolyte_qr_code = device.data_electrolyte_code  # 电解液二维码序列号
            # 处理字符串类型数据
            if isinstance(electrolyte_qr_code, str):
                electrolyte_qr_code = electrolyte_qr_code.strip()
            else:
                electrolyte_qr_code = str(electrolyte_qr_code)
        except Exception as e:
            print(f"读取电解液二维码失败: {e}")
            electrolyte_qr_code = "N/A"
        
        # 获取电池数量
        try:
            battery_count = device.data_assembly_coin_cell_num
            # 确保返回的是数值类型
            if isinstance(battery_count, (list, tuple)) and len(battery_count) > 0:
                battery_count = int(battery_count[0])
            else:
                battery_count = int(battery_count)
        except Exception as e:
            print(f"读取电池数量失败: {e}")
            battery_count = 0
        
        return {
            'Timestamp': timestamp,
            'Battery_Count': battery_count,
            'Assembly_Time': assembly_time,
            'Open_Circuit_Voltage': open_circuit_voltage,
            'Pole_Weight': pole_weight,
            'Assembly_Pressure': assembly_pressure,
            'Battery_Code': battery_qr_code,
            'Electrolyte_Code': electrolyte_qr_code
        }
    except Exception as e:
        print(f"收集数据时出错: {e}")
        return None

def export_to_csv(data, csv_file_path):
    """将数据导出到CSV文件"""
    try:
        # 检查文件是否存在，如果不存在则创建并写入表头
        file_exists = os.path.exists(csv_file_path)
        
        # 确保目录存在
        csv_dir = os.path.dirname(csv_file_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        
        # 确保数值字段为正确的数值类型，避免前导单引号问题
        processed_data = data.copy()
        
        # 处理数值字段，确保它们是数值类型而不是字符串，增强错误处理
        numeric_fields = ['Battery_Count', 'Assembly_Time', 'Open_Circuit_Voltage', 'Pole_Weight', 'Assembly_Pressure']
        for field in numeric_fields:
            if field in processed_data:
                try:
                    value = processed_data[field]
                    # 处理可能的列表或元组类型
                    if isinstance(value, (list, tuple)) and len(value) > 0:
                        value = value[0]
                    
                    if field == 'Battery_Count' or field == 'Assembly_Pressure':
                        processed_data[field] = int(float(value))  # 先转float再转int，处理字符串数字
                    else:
                        processed_data[field] = float(value)
                except (ValueError, TypeError, IndexError) as e:
                    print(f"字段 {field} 类型转换失败: {e}, 使用默认值")
                    processed_data[field] = 0 if field == 'Battery_Count' else 0.0
        
        # 处理字符串字段
        for field in ['Battery_Code', 'Electrolyte_Code']:
            if field in processed_data:
                try:
                    value = processed_data[field]
                    if isinstance(value, (list, tuple)) and len(value) > 0:
                        value = value[0]
                    processed_data[field] = str(value).strip()
                except Exception as e:
                    print(f"字段 {field} 处理失败: {e}, 使用默认值")
                    processed_data[field] = "N/A"
        
        with open(csv_file_path, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Timestamp', 'Battery_Count', 'Assembly_Time', 'Open_Circuit_Voltage', 
                         'Pole_Weight', 'Assembly_Pressure', 'Battery_QR_Code', 'Electrolyte_QR_Code']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
            
            # 如果文件不存在，写入表头
            if not file_exists:
                writer.writeheader()
                print(f"创建新的CSV文件: {csv_file_path}")
            
            # 写入数据
            writer.writerow(processed_data)
            print(f"数据已导出到: {csv_file_path}")
        
        return True
    except Exception as e:
        print(f"导出CSV时出错: {e}")
        return False

def view_csv_content(csv_file_path, lines=10):
    """查看CSV文件内容"""
    try:
        if not os.path.exists(csv_file_path):
            print("CSV文件不存在")
            return
        
        with open(csv_file_path, 'r', encoding='utf-8') as csvfile:
            content = csvfile.readlines()
            
        if not content:
            print("CSV文件为空")
            return
        
        print(f"CSV文件内容 (显示最后{min(lines, len(content))}行):")
        print("-" * 80)
        
        # 显示表头
        if len(content) > 0:
            print(content[0].strip())
            print("-" * 80)
        
        # 显示最后几行数据
        start_line = max(1, len(content) - lines + 1)
        for i in range(start_line, len(content)):
            print(content[i].strip())
        
        print("-" * 80)
        print(f"总共 {len(content)-1} 条数据记录")
        
    except Exception as e:
        print(f"读取CSV文件时出错: {e}")

def interactive_demo():
    """
    交互式演示模式（优化版）
    """
    clear_screen()
    print_header()
    
    print("正在初始化设备连接...")
    print("设备地址: 192.168.1.20:502")
    print("正在尝试连接...")
    
    try:
        device = Coin_Cell_Assembly(address="192.168.1.20", port="502")
        print("✓ 设备连接成功")
        
        # 测试设备数据读取
        print("正在测试设备数据读取...")
        try:
            test_count = device.data_assembly_coin_cell_num
            print(f"✓ 当前电池数量: {test_count}")
        except Exception as e:
            print(f"⚠ 数据读取测试失败: {e}")
            print("设备连接正常，但数据读取可能存在问题")
            
    except Exception as e:
        print(f"✗ 设备连接失败: {e}")
        print("请检查以下项目:")
        print("1. 设备是否已开机并正常运行")
        print("2. 网络连接是否正常")
        print("3. 设备IP地址是否为192.168.1.20")
        print("4. Modbus服务是否在端口502上运行")
        input("按回车键退出...")
        return
    
    csv_file_path = "battery_data_export.csv"
    print(f"CSV文件路径: {os.path.abspath(csv_file_path)}")
    print()
    print("功能说明:")
    print("- 支持手动导出当前设备数据到CSV文件")
    print("- 包含六个关键数据: 组装时间、开路电压、正极片重量、电池码、电解液码")
    print("- 电池码和电解液码可能显示为N/A（当二维码读取失败时）")
    print("- 支持查看CSV文件内容和导出状态")
    print("- 兼容原有的电池组装完成状态自动导出功能")
    print()
    
    print_commands()
    
    while True:
        try:
            command = input("\n请输入命令 > ").strip().lower()
            
            if command == "start":
                print("启动电池组装完成状态导出...")
                try:
                    success, message = device.start_battery_completion_export(csv_file_path)
                    if success:
                        print(f"✓ {message}")
                        print("系统正在监控电池组装完成状态...")
                    else:
                        print(f"✗ {message}")
                except Exception as e:
                    print(f"启动导出时出错: {e}")
            
            elif command == "stop":
                 print("停止导出...")
                 try:
                     success, message = device.stop_csv_export()
                     if success:
                         print(f"✓ {message}")
                     else:
                         print(f"✗ {message}")
                 except Exception as e:
                     print(f"停止导出时出错: {e}")
            
            elif command == "force":
                print("强制继续CSV导出...")
                try:
                    success, message = device.force_continue_csv_export()
                    if success:
                        print(f"✓ {message}")
                        print("注意: CSV导出将继续监控数据变化，即使设备处于停止状态")
                    else:
                        print(f"✗ {message}")
                except AttributeError:
                    print("✗ 当前版本不支持强制继续功能")
                except Exception as e:
                    print(f"✗ 强制继续失败: {e}")
                    
            elif command == "detail":
                print("=== 详细设备状态 ===")
                print_status_info(device, csv_file_path)
            
            elif command == "status":
                print_status_info(device, csv_file_path)
            
            elif command == "data":
                print("读取当前设备数据...")
                try:
                    data = collect_device_data(device)
                    if data:
                        print("\n=== 当前设备数据 ===")
                        print(f"时间戳: {data['Timestamp']}")
                        print(f"电池数量: {data['Battery_Count']}")
                        print(f"单颗电池组装时间: {data['Assembly_Time']:.2f} 秒")
                        print(f"开路电压值: {data['Open_Circuit_Voltage']:.4f} V")
                        print(f"正极片称重数据: {data['Pole_Weight']:.4f} g")
                        print(f"电池压制力: {data['Assembly_Pressure']} N")
                        print(f"电池二维码序列号: {data['Battery_Code']}")
                        print(f"电解液二维码序列号: {data['Electrolyte_Code']}")
                        print("===================")
                    else:
                        print("无法获取设备数据")
                except Exception as e:
                    print(f"读取数据时出错: {e}")
            
            elif command == "count":
                print("读取当前电池数量...")
                try:
                    count = device.data_assembly_coin_cell_num
                    print(f"当前已完成电池数量: {count}")
                except Exception as e:
                    print(f"读取电池数量时出错: {e}")
            
            elif command == "export":
                print("正在收集设备数据并导出到CSV...")
                data = collect_device_data(device)
                if data:
                    print(f"收集到数据: 电池数量={data.get('Battery_Count', 'N/A')}, 组装时间={data.get('Assembly_Time', 'N/A')}s")
                    if export_to_csv(data, csv_file_path):
                        print("✓ 数据已成功导出到CSV文件")
                        print(f"导出数据: 时间={data['Timestamp']}, 电池数量={data['Battery_Count']}, 组装时间={data['Assembly_Time']}秒, "
                              f"电压={data['Open_Circuit_Voltage']}V, 重量={data['Pole_Weight']}g, 压制力={data['Assembly_Pressure']}N")
                        print(f"电池码={data['Battery_Code']}, 电解液码={data['Electrolyte_Code']}")
                    else:
                        print("✗ 导出失败")
                else:
                    print("✗ 数据收集失败，无法导出！请检查设备连接状态。")
                    # 尝试重新连接设备
                    try:
                        if hasattr(device, 'connect'):
                            device.connect()
                            print("尝试重新连接设备...")
                    except Exception as e:
                        print(f"重新连接失败: {e}")
            
            elif command == "setpath":
                print("设置自定义CSV文件路径")
                print(f"当前CSV文件路径: {csv_file_path}")
                new_path = input("请输入新的CSV文件路径（包含文件名，如: D:/data/my_battery_data.csv）: ").strip()
                if new_path:
                    try:
                        # 确保目录存在
                        new_dir = os.path.dirname(new_path)
                        if new_dir and not os.path.exists(new_dir):
                            os.makedirs(new_dir, exist_ok=True)
                            print(f"✓ 已创建目录: {new_dir}")
                        
                        csv_file_path = new_path
                        print(f"✓ CSV文件路径已更新为: {os.path.abspath(csv_file_path)}")
                        
                        # 检查文件是否存在
                        if os.path.exists(csv_file_path):
                            file_size = os.path.getsize(csv_file_path)
                            print(f"文件已存在，大小: {file_size} 字节")
                        else:
                            print("文件不存在，将在首次导出时创建")
                    except Exception as e:
                        print(f"✗ 设置路径失败: {e}")
                else:
                    print("路径不能为空")
            
            elif command == "view":
                print("查看CSV文件内容...")
                view_csv_content(csv_file_path)
            
            elif command == "clear":
                clear_screen()
                print_header()
                print_commands()
            
            elif command == "help":
                print_commands()
            
            elif command == "quit" or command == "exit":
                print("正在退出...")
                # 停止导出
                try:
                    device.stop_csv_export()
                    print("✓ 导出已停止")
                except:
                    pass
                print("程序已退出")
                break
            
            elif command == "":
                # 空命令，不做任何操作
                continue
            
            else:
                print(f"未知命令: {command}")
                print("输入 'help' 查看可用命令")
        
        except KeyboardInterrupt:
            print("\n\n检测到 Ctrl+C，正在退出...")
            try:
                device.stop_csv_export()
                print("✓ 导出已停止")
            except:
                pass
            print("程序已退出")
            break
        except Exception as e:
            print(f"执行命令时出错: {e}")

if __name__ == '__main__':
    interactive_demo()