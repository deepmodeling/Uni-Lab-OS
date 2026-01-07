"""
纽扣电池组装工作站
Coin Cell Assembly Workstation

继承工作站基类，实现纽扣电池特定功能
"""
from typing import Dict, Any, List, Optional, Union

from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
from unilabos.device_comms.workstation_base import WorkstationBase, WorkflowInfo
from unilabos.device_comms.workstation_communication import (
    WorkstationCommunicationBase, CommunicationConfig, CommunicationProtocol, CoinCellCommunication
)
from unilabos.device_comms.workstation_material_management import (
    MaterialManagementBase, CoinCellMaterialManagement
)
from unilabos.utils.log import logger


class CoinCellAssemblyWorkstation(WorkstationBase):
    """纽扣电池组装工作站
    
    基于工作站基类，实现纽扣电池制造的特定功能：
    1. 纽扣电池特定的通信协议
    2. 纽扣电池物料管理（料板、极片、电池等）
    3. 电池制造工作流
    4. 质量检查工作流
    """

    def __init__(
        self,
        device_id: str,
        children: Dict[str, Dict[str, Any]],
        protocol_type: Union[str, List[str]] = "BatteryManufacturingProtocol",
        resource_tracker: Optional[DeviceNodeResourceTracker] = None,
        modbus_config: Optional[Dict[str, Any]] = None,
        deck_config: Optional[Dict[str, Any]] = None,
        csv_path: str = "./coin_cell_assembly.csv",
        *args,
        **kwargs,
    ):
        # 设置通信配置
        modbus_config = modbus_config or {"host": "127.0.0.1", "port": 5021}
        self.communication_config = CommunicationConfig(
            protocol=CommunicationProtocol.MODBUS_TCP,
            host=modbus_config["host"],
            port=modbus_config["port"],
            timeout=modbus_config.get("timeout", 5.0),
            retry_count=modbus_config.get("retry_count", 3)
        )
        
        # 设置台面配置
        self.deck_config = deck_config or {
            "size_x": 1620.0,
            "size_y": 1270.0,
            "size_z": 500.0
        }
        
        # CSV地址映射文件路径
        self.csv_path = csv_path
        
        # 创建资源跟踪器（如果没有提供）
        if resource_tracker is None:
            from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
            resource_tracker = DeviceNodeResourceTracker()
        
        # 初始化基类
        super().__init__(
            device_id=device_id,
            children=children,
            protocol_type=protocol_type,
            resource_tracker=resource_tracker,
            communication_config=self.communication_config,
            deck_config=self.deck_config,
            *args,
            **kwargs
        )
        
        logger.info(f"纽扣电池组装工作站 {device_id} 初始化完成")

    def _create_communication_module(self) -> WorkstationCommunicationBase:
        """创建纽扣电池通信模块"""
        return CoinCellCommunication(
            communication_config=self.communication_config,
            csv_path=self.csv_path
        )

    def _create_material_management_module(self) -> MaterialManagementBase:
        """创建纽扣电池物料管理模块"""
        return CoinCellMaterialManagement(
            device_id=self.device_id,
            deck_config=self.deck_config,
            resource_tracker=self.resource_tracker,
            children_config=self.children
        )

    def _register_supported_workflows(self):
        """注册纽扣电池工作流"""
        # 电池制造工作流
        self.supported_workflows["battery_manufacturing"] = WorkflowInfo(
            name="battery_manufacturing",
            description="纽扣电池制造工作流",
            estimated_duration=300.0,  # 5分钟
            required_materials=["cathode_sheet", "anode_sheet", "separator", "electrolyte"],
            output_product="coin_cell_battery",
            parameters_schema={
                "type": "object",
                "properties": {
                    "electrolyte_num": {
                        "type": "integer",
                        "description": "电解液瓶数",
                        "minimum": 1,
                        "maximum": 32
                    },
                    "electrolyte_volume": {
                        "type": "number",
                        "description": "电解液体积 (μL)",
                        "minimum": 0.1,
                        "maximum": 100.0
                    },
                    "assembly_pressure": {
                        "type": "number",
                        "description": "组装压力 (N)",
                        "minimum": 100.0,
                        "maximum": 5000.0
                    },
                    "cathode_material": {
                        "type": "string",
                        "description": "正极材料类型",
                        "enum": ["LiFePO4", "LiCoO2", "NCM", "LMO"]
                    },
                    "anode_material": {
                        "type": "string", 
                        "description": "负极材料类型",
                        "enum": ["Graphite", "LTO", "Silicon"]
                    }
                },
                "required": ["electrolyte_num", "electrolyte_volume", "assembly_pressure"]
            }
        )
        
        # 质量检查工作流
        self.supported_workflows["quality_inspection"] = WorkflowInfo(
            name="quality_inspection",
            description="产品质量检查工作流",
            estimated_duration=60.0,  # 1分钟
            required_materials=["finished_battery"],
            output_product="quality_report",
            parameters_schema={
                "type": "object",
                "properties": {
                    "test_voltage": {
                        "type": "boolean",
                        "description": "是否测试电压",
                        "default": True
                    },
                    "test_capacity": {
                        "type": "boolean",
                        "description": "是否测试容量",
                        "default": False
                    },
                    "voltage_threshold": {
                        "type": "number",
                        "description": "电压阈值 (V)",
                        "minimum": 2.0,
                        "maximum": 4.5,
                        "default": 3.0
                    }
                }
            }
        )
        
        # 设备初始化工作流
        self.supported_workflows["device_initialization"] = WorkflowInfo(
            name="device_initialization", 
            description="设备初始化工作流",
            estimated_duration=30.0,  # 30秒
            required_materials=[],
            output_product="ready_status",
            parameters_schema={
                "type": "object",
                "properties": {
                    "auto_mode": {
                        "type": "boolean",
                        "description": "是否启用自动模式",
                        "default": True
                    }
                }
            }
        )

    # ============ 纽扣电池特定方法 ============
    
    def get_electrode_sheet_inventory(self) -> Dict[str, int]:
        """获取极片库存统计"""
        try:
            sheets = self.material_management.find_electrode_sheets()
            inventory = {}
            
            for sheet in sheets:
                material_type = getattr(sheet, 'material_type', 'unknown')
                inventory[material_type] = inventory.get(material_type, 0) + 1
            
            return inventory
            
        except Exception as e:
            logger.error(f"获取极片库存失败: {e}")
            return {}

    def get_battery_production_statistics(self) -> Dict[str, Any]:
        """获取电池生产统计"""
        try:
            production_data = self.communication.get_production_data()
            
            # 添加物料统计
            electrode_inventory = self.get_electrode_sheet_inventory()
            battery_count = len(self.material_management.find_batteries())
            
            return {
                **production_data,
                "electrode_inventory": electrode_inventory,
                "finished_battery_count": battery_count,
                "material_plates": len(self.material_management.find_material_plates()),
                "press_slots": len(self.material_management.find_press_slots())
            }
            
        except Exception as e:
            logger.error(f"获取生产统计失败: {e}")
            return {"error": str(e)}

    def create_new_battery(self, battery_spec: Dict[str, Any]) -> Optional[str]:
        """创建新电池资源"""
        try:
            from unilabos.device_comms.button_battery_station import Battery
            import uuid
            
            battery_id = f"battery_{uuid.uuid4().hex[:8]}"
            
            battery = Battery(
                name=battery_id,
                diameter=battery_spec.get("diameter", 20.0),
                height=battery_spec.get("height", 3.2),
                max_volume=battery_spec.get("max_volume", 100.0),
                barcode=battery_spec.get("barcode", "")
            )
            
            # 添加到物料管理系统
            self.material_management.plr_resources[battery_id] = battery
            self.material_management.resource_tracker.add_resource(battery)
            
            logger.info(f"创建新电池资源: {battery_id}")
            return battery_id
            
        except Exception as e:
            logger.error(f"创建电池资源失败: {e}")
            return None

    def find_available_press_slot(self) -> Optional[str]:
        """查找可用的压制槽"""
        try:
            press_slots = self.material_management.find_press_slots()
            
            for slot in press_slots:
                if hasattr(slot, 'has_battery') and not slot.has_battery():
                    return slot.name
            
            return None
            
        except Exception as e:
            logger.error(f"查找可用压制槽失败: {e}")
            return None

    def get_glove_box_environment(self) -> Dict[str, Any]:
        """获取手套箱环境数据"""
        try:
            device_status = self.communication.get_device_status()
            environment = device_status.get("environment", {})
            
            return {
                "pressure": environment.get("glove_box_pressure", 0.0),
                "o2_content": environment.get("o2_content", 0.0),
                "water_content": environment.get("water_content", 0.0),
                "is_safe": (
                    environment.get("o2_content", 0.0) < 10.0 and  # 氧气含量 < 10ppm
                    environment.get("water_content", 0.0) < 1.0      # 水分含量 < 1ppm
                )
            }
            
        except Exception as e:
            logger.error(f"获取手套箱环境失败: {e}")
            return {"error": str(e)}

    def start_data_export(self, file_path: str) -> bool:
        """开始生产数据导出"""
        try:
            return self.communication.start_data_export(file_path, export_interval=5.0)
        except Exception as e:
            logger.error(f"启动数据导出失败: {e}")
            return False

    def stop_data_export(self) -> bool:
        """停止生产数据导出"""
        try:
            return self.communication.stop_data_export()
        except Exception as e:
            logger.error(f"停止数据导出失败: {e}")
            return False

    # ============ 重写基类方法以支持纽扣电池特定功能 ============
    
    def start_workflow(self, workflow_type: str, parameters: Dict[str, Any] = None) -> bool:
        """启动工作流（重写以支持纽扣电池特定预处理）"""
        try:
            # 进行纽扣电池特定的预检查
            if workflow_type == "battery_manufacturing":
                # 检查手套箱环境
                env = self.get_glove_box_environment()
                if not env.get("is_safe", False):
                    logger.error("手套箱环境不安全，无法启动电池制造工作流")
                    return False
                
                # 检查是否有可用的压制槽
                available_slot = self.find_available_press_slot()
                if not available_slot:
                    logger.error("没有可用的压制槽，无法启动电池制造工作流")
                    return False
                
                # 检查极片库存
                electrode_inventory = self.get_electrode_sheet_inventory()
                if not electrode_inventory.get("cathode", 0) > 0 or not electrode_inventory.get("anode", 0) > 0:
                    logger.error("极片库存不足，无法启动电池制造工作流")
                    return False
            
            # 调用基类方法
            return super().start_workflow(workflow_type, parameters)
            
        except Exception as e:
            logger.error(f"启动纽扣电池工作流失败: {e}")
            return False

    # ============ 纽扣电池特定状态属性 ============
    
    @property
    def electrode_sheet_count(self) -> int:
        """极片总数"""
        try:
            return len(self.material_management.find_electrode_sheets())
        except:
            return 0

    @property
    def battery_count(self) -> int:
        """电池总数"""
        try:
            return len(self.material_management.find_batteries())
        except:
            return 0

    @property
    def available_press_slots(self) -> int:
        """可用压制槽数"""
        try:
            press_slots = self.material_management.find_press_slots()
            available = 0
            for slot in press_slots:
                if hasattr(slot, 'has_battery') and not slot.has_battery():
                    available += 1
            return available
        except:
            return 0

    @property
    def environment_status(self) -> Dict[str, Any]:
        """环境状态"""
        return self.get_glove_box_environment()


# ============ 工厂函数 ============

def create_coin_cell_workstation(
    device_id: str,
    config_file: str,
    modbus_host: str = "127.0.0.1",
    modbus_port: int = 5021,
    csv_path: str = "./coin_cell_assembly.csv"
) -> CoinCellAssemblyWorkstation:
    """工厂函数：创建纽扣电池组装工作站
    
    Args:
        device_id: 设备ID
        config_file: 配置文件路径（JSON格式）
        modbus_host: Modbus主机地址
        modbus_port: Modbus端口
        csv_path: 地址映射CSV文件路径
    
    Returns:
        CoinCellAssemblyWorkstation: 工作站实例
    """
    import json
    
    try:
        # 加载配置文件
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # 提取配置
        children = config.get("children", {})
        deck_config = config.get("deck_config", {})
        
        # 创建工作站
        workstation = CoinCellAssemblyWorkstation(
            device_id=device_id,
            children=children,
            modbus_config={
                "host": modbus_host,
                "port": modbus_port
            },
            deck_config=deck_config,
            csv_path=csv_path
        )
        
        logger.info(f"纽扣电池工作站创建成功: {device_id}")
        return workstation
        
    except Exception as e:
        logger.error(f"创建纽扣电池工作站失败: {e}")
        raise


if __name__ == "__main__":
    # 示例用法
    workstation = create_coin_cell_workstation(
        device_id="coin_cell_station_01",
        config_file="./button_battery_workstation.json",
        modbus_host="127.0.0.1",
        modbus_port=5021
    )
    
    # 启动电池制造工作流
    success = workstation.start_workflow(
        "battery_manufacturing",
        {
            "electrolyte_num": 16,
            "electrolyte_volume": 50.0,
            "assembly_pressure": 2000.0,
            "cathode_material": "LiFePO4",
            "anode_material": "Graphite"
        }
    )
    
    if success:
        print("电池制造工作流启动成功")
    else:
        print("电池制造工作流启动失败")
