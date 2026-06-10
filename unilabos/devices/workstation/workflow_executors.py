"""
工作流执行器模块
Workflow Executors Module

基于单一硬件接口的工作流执行器实现
支持Modbus、HTTP、PyLabRobot和代理模式
"""
import time
import json
import asyncio
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from abc import ABC, abstractmethod

if TYPE_CHECKING:
    from unilabos.devices.work_station.workstation_base import WorkstationBase

from unilabos.utils.log import logger


class WorkflowExecutor(ABC):
    """工作流执行器基类 - 基于单一硬件接口"""

    def __init__(self, workstation: 'WorkstationBase'):
        self.workstation = workstation
        self.hardware_interface = workstation.hardware_interface
        self.material_management = workstation.material_management

    @abstractmethod
    def execute_workflow(self, workflow_name: str, parameters: Dict[str, Any]) -> bool:
        """执行工作流"""
        pass

    @abstractmethod
    def stop_workflow(self, emergency: bool = False) -> bool:
        """停止工作流"""
        pass

    def call_device(self, method: str, *args, **kwargs) -> Any:
        """调用设备方法的统一接口"""
        return self.workstation.call_device_method(method, *args, **kwargs)

    def get_device_status(self) -> Dict[str, Any]:
        """获取设备状态"""
        return self.workstation.get_device_status()


class ModbusWorkflowExecutor(WorkflowExecutor):
    """Modbus工作流执行器 - 适配 coin_cell_assembly_system"""

    def __init__(self, workstation: 'WorkstationBase'):
        super().__init__(workstation)

        # 验证Modbus接口
        if not (hasattr(self.hardware_interface, 'write_register') and
                hasattr(self.hardware_interface, 'read_register')):
            raise RuntimeError("工作站硬件接口不是有效的Modbus客户端")

    def execute_workflow(self, workflow_name: str, parameters: Dict[str, Any]) -> bool:
        """执行Modbus工作流"""
        if workflow_name == "battery_manufacturing":
            return self._execute_battery_manufacturing(parameters)
        elif workflow_name == "material_loading":
            return self._execute_material_loading(parameters)
        elif workflow_name == "quality_check":
            return self._execute_quality_check(parameters)
        else:
            logger.warning(f"不支持的Modbus工作流: {workflow_name}")
            return False

    def _execute_battery_manufacturing(self, parameters: Dict[str, Any]) -> bool:
        """执行电池制造工作流"""
        try:
            # 1. 物料准备检查
            available_slot = self._find_available_press_slot()
            if not available_slot:
                raise RuntimeError("没有可用的压制槽")

            logger.info(f"找到可用压制槽: {available_slot}")

            # 2. 设置工艺参数（直接调用Modbus接口）
            if "electrolyte_num" in parameters:
                self.hardware_interface.write_register('REG_MSG_ELECTROLYTE_NUM', parameters["electrolyte_num"])
                logger.info(f"设置电解液编号: {parameters['electrolyte_num']}")

            if "electrolyte_volume" in parameters:
                self.hardware_interface.write_register('REG_MSG_ELECTROLYTE_VOLUME',
                                                       parameters["electrolyte_volume"],
                                                       data_type="FLOAT32")
                logger.info(f"设置电解液体积: {parameters['electrolyte_volume']}")

            if "assembly_pressure" in parameters:
                self.hardware_interface.write_register('REG_MSG_ASSEMBLY_PRESSURE',
                                                       parameters["assembly_pressure"],
                                                       data_type="FLOAT32")
                logger.info(f"设置装配压力: {parameters['assembly_pressure']}")

            # 3. 启动制造流程
            self.hardware_interface.write_register('COIL_SYS_START_CMD', True)
            logger.info("启动电池制造流程")

            # 4. 确认启动成功
            time.sleep(0.5)
            status = self.hardware_interface.read_register('COIL_SYS_START_STATUS', count=1)
            success = status[0] if status else False

            if success:
                logger.info(f"电池制造工作流启动成功，参数: {parameters}")
            else:
                logger.error("电池制造工作流启动失败")

            return success

        except Exception as e:
            logger.error(f"执行电池制造工作流失败: {e}")
            return False

    def _execute_material_loading(self, parameters: Dict[str, Any]) -> bool:
        """执行物料装载工作流"""
        try:
            material_type = parameters.get('material_type', 'cathode')
            position = parameters.get('position', 'A1')

            logger.info(f"开始物料装载: {material_type} -> {position}")

            # 设置物料类型和位置
            self.hardware_interface.write_register('REG_MATERIAL_TYPE', material_type)
            self.hardware_interface.write_register('REG_MATERIAL_POSITION', position)

            # 启动装载
            self.hardware_interface.write_register('COIL_LOAD_START', True)

            # 等待装载完成
            timeout = parameters.get('timeout', 30)
            start_time = time.time()

            while time.time() - start_time < timeout:
                status = self.hardware_interface.read_register('COIL_LOAD_COMPLETE', count=1)
                if status and status[0]:
                    logger.info(f"物料装载完成: {material_type} -> {position}")
                    return True
                time.sleep(0.5)

            logger.error(f"物料装载超时: {material_type} -> {position}")
            return False

        except Exception as e:
            logger.error(f"执行物料装载失败: {e}")
            return False

    def _execute_quality_check(self, parameters: Dict[str, Any]) -> bool:
        """执行质量检测工作流"""
        try:
            check_type = parameters.get('check_type', 'dimensional')

            logger.info(f"开始质量检测: {check_type}")

            # 启动质量检测
            self.hardware_interface.write_register('REG_QC_TYPE', check_type)
            self.hardware_interface.write_register('COIL_QC_START', True)

            # 等待检测完成
            timeout = parameters.get('timeout', 60)
            start_time = time.time()

            while time.time() - start_time < timeout:
                status = self.hardware_interface.read_register('COIL_QC_COMPLETE', count=1)
                if status and status[0]:
                    # 读取检测结果
                    result = self.hardware_interface.read_register('REG_QC_RESULT', count=1)
                    passed = result[0] if result else False

                    if passed:
                        logger.info(f"质量检测通过: {check_type}")
                        return True
                    else:
                        logger.warning(f"质量检测失败: {check_type}")
                        return False

                time.sleep(1.0)

            logger.error(f"质量检测超时: {check_type}")
            return False

        except Exception as e:
            logger.error(f"执行质量检测失败: {e}")
            return False

    def _find_available_press_slot(self) -> Optional[str]:
        """查找可用压制槽"""
        try:
            press_slots = self.material_management.find_by_category("battery_press_slot")
            for slot in press_slots:
                if hasattr(slot, 'has_battery') and not slot.has_battery():
                    return slot.name
            return None
        except:
            # 如果物料管理系统不可用，返回默认槽位
            return "A1"

    def stop_workflow(self, emergency: bool = False) -> bool:
        """停止工作流"""
        try:
            if emergency:
                self.hardware_interface.write_register('COIL_SYS_RESET_CMD', True)
                logger.warning("执行紧急停止")
            else:
                self.hardware_interface.write_register('COIL_SYS_STOP_CMD', True)
                logger.info("执行正常停止")

            time.sleep(0.5)
            status = self.hardware_interface.read_register('COIL_SYS_STOP_STATUS', count=1)
            return status[0] if status else False

        except Exception as e:
            logger.error(f"停止Modbus工作流失败: {e}")
            return False


class HttpWorkflowExecutor(WorkflowExecutor):
    """HTTP工作流执行器 - 适配 reaction_station_bioyong"""

    def __init__(self, workstation: 'WorkstationBase'):
        super().__init__(workstation)

        # 验证HTTP接口
        if not (hasattr(self.hardware_interface, 'post') or
                hasattr(self.hardware_interface, 'get')):
            raise RuntimeError("工作站硬件接口不是有效的HTTP客户端")

    def execute_workflow(self, workflow_name: str, parameters: Dict[str, Any]) -> bool:
        """执行HTTP工作流"""
        try:
            if workflow_name == "reaction_synthesis":
                return self._execute_reaction_synthesis(parameters)
            elif workflow_name == "liquid_feeding":
                return self._execute_liquid_feeding(parameters)
            elif workflow_name == "temperature_control":
                return self._execute_temperature_control(parameters)
            else:
                logger.warning(f"不支持的HTTP工作流: {workflow_name}")
                return False

        except Exception as e:
            logger.error(f"执行HTTP工作流失败: {e}")
            return False

    def _execute_reaction_synthesis(self, parameters: Dict[str, Any]) -> bool:
        """执行反应合成工作流"""
        try:
            # 1. 设置工作流序列
            sequence = self._build_reaction_sequence(parameters)
            self._call_rpc_method('set_workflow_sequence', json.dumps(sequence))

            # 2. 设置反应参数
            if parameters.get('temperature'):
                self._call_rpc_method('set_temperature', parameters['temperature'])

            if parameters.get('pressure'):
                self._call_rpc_method('set_pressure', parameters['pressure'])

            if parameters.get('stirring_speed'):
                self._call_rpc_method('set_stirring_speed', parameters['stirring_speed'])

            # 3. 执行工作流
            result = self._call_rpc_method('execute_current_sequence', {
                "task_name": "reaction_synthesis"
            })

            success = result.get('success', False)
            if success:
                logger.info("反应合成工作流执行成功")
            else:
                logger.error(f"反应合成工作流执行失败: {result.get('error', '未知错误')}")

            return success

        except Exception as e:
            logger.error(f"执行反应合成工作流失败: {e}")
            return False

    def _execute_liquid_feeding(self, parameters: Dict[str, Any]) -> bool:
        """执行液体投料工作流"""
        try:
            reagents = parameters.get('reagents', [])
            volumes = parameters.get('volumes', [])

            if len(reagents) != len(volumes):
                raise ValueError("试剂列表和体积列表长度不匹配")

            # 执行投料序列
            for reagent, volume in zip(reagents, volumes):
                result = self._call_rpc_method('feed_liquid', {
                    'reagent': reagent,
                    'volume': volume
                })

                if not result.get('success', False):
                    logger.error(f"投料失败: {reagent} {volume}mL")
                    return False

                logger.info(f"投料成功: {reagent} {volume}mL")

            return True

        except Exception as e:
            logger.error(f"执行液体投料失败: {e}")
            return False

    def _execute_temperature_control(self, parameters: Dict[str, Any]) -> bool:
        """执行温度控制工作流"""
        try:
            target_temp = parameters.get('temperature', 25)
            hold_time = parameters.get('hold_time', 300)  # 秒

            # 设置目标温度
            result = self._call_rpc_method('set_temperature', target_temp)
            if not result.get('success', False):
                logger.error(f"设置温度失败: {target_temp}°C")
                return False

            # 等待温度稳定
            logger.info(f"等待温度稳定到 {target_temp}°C")

            # 保持温度指定时间
            if hold_time > 0:
                logger.info(f"保持温度 {hold_time} 秒")
                time.sleep(hold_time)

            return True

        except Exception as e:
            logger.error(f"执行温度控制失败: {e}")
            return False

    def _build_reaction_sequence(self, parameters: Dict[str, Any]) -> List[str]:
        """构建反应合成工作流序列"""
        sequence = []

        # 添加预处理步骤
        if parameters.get('purge_with_inert'):
            sequence.append("purge_inert_gas")

        # 添加温度设置
        if parameters.get('temperature'):
            sequence.append(f"set_temperature_{parameters['temperature']}")

        # 添加压力设置
        if parameters.get('pressure'):
            sequence.append(f"set_pressure_{parameters['pressure']}")

        # 添加搅拌设置
        if parameters.get('stirring_speed'):
            sequence.append(f"set_stirring_{parameters['stirring_speed']}")

        # 添加反应步骤
        sequence.extend([
            "start_reaction",
            "monitor_progress",
            "complete_reaction"
        ])

        # 添加后处理步骤
        if parameters.get('cooling_required'):
            sequence.append("cool_down")

        return sequence

    def _call_rpc_method(self, method: str, params: Any = None) -> Dict[str, Any]:
        """调用RPC方法"""
        try:
            if hasattr(self.hardware_interface, method):
                # 直接方法调用
                if isinstance(params, dict):
                    params = json.dumps(params)
                elif params is None:
                    params = ""
                return getattr(self.hardware_interface, method)(params)
            else:
                # HTTP请求调用
                if hasattr(self.hardware_interface, 'post'):
                    response = self.hardware_interface.post(f"/api/{method}", json=params)
                    return response.json()
                else:
                    raise AttributeError(f"HTTP接口不支持方法: {method}")
        except Exception as e:
            logger.error(f"调用RPC方法失败 {method}: {e}")
            return {'success': False, 'error': str(e)}

    def stop_workflow(self, emergency: bool = False) -> bool:
        """停止工作流"""
        try:
            if emergency:
                result = self._call_rpc_method('scheduler_reset')
            else:
                result = self._call_rpc_method('scheduler_stop')

            return result.get('success', False)

        except Exception as e:
            logger.error(f"停止HTTP工作流失败: {e}")
            return False


class PyLabRobotWorkflowExecutor(WorkflowExecutor):
    """PyLabRobot工作流执行器 - 适配 prcxi.py"""

    def __init__(self, workstation: 'WorkstationBase'):
        super().__init__(workstation)

        # 验证PyLabRobot接口
        if not (hasattr(self.hardware_interface, 'transfer_liquid') or
                hasattr(self.hardware_interface, 'pickup_tips')):
            raise RuntimeError("工作站硬件接口不是有效的PyLabRobot设备")

    def execute_workflow(self, workflow_name: str, parameters: Dict[str, Any]) -> bool:
        """执行PyLabRobot工作流"""
        try:
            if workflow_name == "liquid_transfer":
                return self._execute_liquid_transfer(parameters)
            elif workflow_name == "tip_pickup_drop":
                return self._execute_tip_operations(parameters)
            elif workflow_name == "plate_handling":
                return self._execute_plate_handling(parameters)
            else:
                logger.warning(f"不支持的PyLabRobot工作流: {workflow_name}")
                return False

        except Exception as e:
            logger.error(f"执行PyLabRobot工作流失败: {e}")
            return False

    def _execute_liquid_transfer(self, parameters: Dict[str, Any]) -> bool:
        """执行液体转移工作流"""
        try:
            # 1. 解析物料引用
            sources = self._resolve_containers(parameters.get('sources', []))
            targets = self._resolve_containers(parameters.get('targets', []))
            tip_racks = self._resolve_tip_racks(parameters.get('tip_racks', []))

            if not sources or not targets:
                raise ValueError("液体转移需要指定源容器和目标容器")

            if not tip_racks:
                logger.warning("未指定枪头架，将尝试自动查找")
                tip_racks = self._find_available_tip_racks()

            # 2. 执行液体转移
            volumes = parameters.get('volumes', [])
            if not volumes:
                volumes = [100.0] * len(sources)  # 默认体积

            # 如果是同步接口
            if hasattr(self.hardware_interface, 'transfer_liquid'):
                result = self.hardware_interface.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=tip_racks,
                    asp_vols=volumes,
                    dis_vols=volumes,
                    **parameters.get('options', {})
                )
            else:
                # 异步接口需要特殊处理
                asyncio.run(self._async_liquid_transfer(sources, targets, tip_racks, volumes, parameters))
                result = True

            if result:
                logger.info(f"液体转移工作流完成: {len(sources)}个源 -> {len(targets)}个目标")

            return bool(result)

        except Exception as e:
            logger.error(f"执行液体转移失败: {e}")
            return False

    async def _async_liquid_transfer(self, sources, targets, tip_racks, volumes, parameters):
        """异步液体转移"""
        await self.hardware_interface.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=tip_racks,
            asp_vols=volumes,
            dis_vols=volumes,
            **parameters.get('options', {})
        )

    def _execute_tip_operations(self, parameters: Dict[str, Any]) -> bool:
        """执行枪头操作工作流"""
        try:
            operation = parameters.get('operation', 'pickup')
            tip_racks = self._resolve_tip_racks(parameters.get('tip_racks', []))

            if not tip_racks:
                raise ValueError("枪头操作需要指定枪头架")

            if operation == 'pickup':
                result = self.hardware_interface.pickup_tips(tip_racks[0])
                logger.info("枪头拾取完成")
            elif operation == 'drop':
                result = self.hardware_interface.drop_tips()
                logger.info("枪头丢弃完成")
            else:
                raise ValueError(f"不支持的枪头操作: {operation}")

            return bool(result)

        except Exception as e:
            logger.error(f"执行枪头操作失败: {e}")
            return False

    def _execute_plate_handling(self, parameters: Dict[str, Any]) -> bool:
        """执行板类处理工作流"""
        try:
            operation = parameters.get('operation', 'move')
            source_position = parameters.get('source_position')
            target_position = parameters.get('target_position')

            if operation == 'move' and source_position and target_position:
                # 移动板类
                result = self.hardware_interface.move_plate(source_position, target_position)
                logger.info(f"板类移动完成: {source_position} -> {target_position}")
            else:
                logger.warning(f"不支持的板类操作或参数不完整: {operation}")
                return False

            return bool(result)

        except Exception as e:
            logger.error(f"执行板类处理失败: {e}")
            return False

    def _resolve_containers(self, container_names: List[str]):
        """解析容器名称为实际容器对象"""
        containers = []
        for name in container_names:
            try:
                container = self.material_management.find_material_by_id(name)
                if container:
                    containers.append(container)
                else:
                    logger.warning(f"未找到容器: {name}")
            except:
                logger.warning(f"解析容器失败: {name}")
        return containers

    def _resolve_tip_racks(self, tip_rack_names: List[str]):
        """解析枪头架名称为实际对象"""
        tip_racks = []
        for name in tip_rack_names:
            try:
                tip_rack = self.material_management.find_by_category("tip_rack")
                matching_racks = [rack for rack in tip_rack if rack.name == name]
                if matching_racks:
                    tip_racks.extend(matching_racks)
                else:
                    logger.warning(f"未找到枪头架: {name}")
            except:
                logger.warning(f"解析枪头架失败: {name}")
        return tip_racks

    def _find_available_tip_racks(self):
        """查找可用的枪头架"""
        try:
            tip_racks = self.material_management.find_by_category("tip_rack")
            available_racks = [rack for rack in tip_racks if hasattr(rack, 'has_tips') and rack.has_tips()]
            return available_racks[:1]  # 返回第一个可用的枪头架
        except:
            return []

    def stop_workflow(self, emergency: bool = False) -> bool:
        """停止工作流"""
        try:
            if emergency:
                if hasattr(self.hardware_interface, 'emergency_stop'):
                    return self.hardware_interface.emergency_stop()
                else:
                    logger.warning("设备不支持紧急停止")
                    return False
            else:
                if hasattr(self.hardware_interface, 'graceful_stop'):
                    return self.hardware_interface.graceful_stop()
                elif hasattr(self.hardware_interface, 'stop'):
                    return self.hardware_interface.stop()
                else:
                    logger.warning("设备不支持优雅停止")
                    return False

        except Exception as e:
            logger.error(f"停止PyLabRobot工作流失败: {e}")
            return False


class ProxyWorkflowExecutor(WorkflowExecutor):
    """代理工作流执行器 - 处理代理模式的工作流"""

    def __init__(self, workstation: 'WorkstationBase'):
        super().__init__(workstation)

        # 验证代理接口
        if not isinstance(self.hardware_interface, str) or not self.hardware_interface.startswith("proxy:"):
            raise RuntimeError("工作站硬件接口不是有效的代理字符串")

        self.device_id = self.hardware_interface[6:]  # 移除 "proxy:" 前缀

    def execute_workflow(self, workflow_name: str, parameters: Dict[str, Any]) -> bool:
        """执行代理工作流"""
        try:
            # 通过协议节点调用目标设备的工作流
            if self.workstation._workstation_node:
                return self.workstation._workstation_node.call_device_method(
                    self.device_id, 'execute_workflow', workflow_name, parameters
                )
            else:
                logger.error("代理模式需要workstation_node")
                return False

        except Exception as e:
            logger.error(f"执行代理工作流失败: {e}")
            return False

    def stop_workflow(self, emergency: bool = False) -> bool:
        """停止代理工作流"""
        try:
            if self.workstation._workstation_node:
                return self.workstation._workstation_node.call_device_method(
                    self.device_id, 'stop_workflow', emergency
                )
            else:
                logger.error("代理模式需要workstation_node")
                return False

        except Exception as e:
            logger.error(f"停止代理工作流失败: {e}")
            return False


# 辅助函数
def get_executor_for_interface(hardware_interface) -> str:
    """根据硬件接口类型获取执行器类型名称"""
    if isinstance(hardware_interface, str) and hardware_interface.startswith("proxy:"):
        return "ProxyWorkflowExecutor"
    elif hasattr(hardware_interface, 'write_register') and hasattr(hardware_interface, 'read_register'):
        return "ModbusWorkflowExecutor"
    elif hasattr(hardware_interface, 'post') or hasattr(hardware_interface, 'get'):
        return "HttpWorkflowExecutor"
    elif hasattr(hardware_interface, 'transfer_liquid') or hasattr(hardware_interface, 'pickup_tips'):
        return "PyLabRobotWorkflowExecutor"
    else:
        return "UnknownExecutor"
