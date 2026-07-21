import asyncio
import logging
import re
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode

class VirtualSolidDispenser:
    """
    虚拟固体粉末加样器 - 用于处理 Add Protocol 中的固体试剂添加 ⚗️
    
    特点：
    - 高兼容性：缺少参数不报错 ✅
    - 智能识别：自动查找固体试剂瓶 🔍
    - 简单反馈：成功/失败 + 消息 📊
    """
    
    _ros_node: "BaseROS2DeviceNode"
    
    def __init__(self, device_id: str = None, config: Dict[str, Any] = None, **kwargs):
        self.device_id = device_id or "virtual_solid_dispenser"
        self.config = config or {}
        
        # 设备参数
        self.max_capacity = float(self.config.get('max_capacity', 100.0))  # 最大加样量 (g)
        self.precision = float(self.config.get('precision', 0.001))  # 精度 (g)
        
        # 状态变量
        self._status = "Idle"
        self._current_reagent = ""
        self._dispensed_amount = 0.0
        self._total_operations = 0
        
        self.logger = logging.getLogger(f"VirtualSolidDispenser.{self.device_id}")
        
        print(f"⚗️ === 虚拟固体分配器 {self.device_id} 创建成功! === ✨")
        print(f"📊 设备规格: 最大容量 {self.max_capacity}g | 精度 {self.precision}g 🎯")
    
    def post_init(self, ros_node: "BaseROS2DeviceNode"):
        self._ros_node = ros_node
    
    async def initialize(self) -> bool:
        """初始化固体加样器 🚀"""
        self.logger.info(f"🔧 初始化固体分配器 {self.device_id} ✨")
        self._status = "Ready"
        self._current_reagent = ""
        self._dispensed_amount = 0.0
        
        self.logger.info(f"✅ 固体分配器 {self.device_id} 初始化完成 ⚗️")
        return True
    
    async def cleanup(self) -> bool:
        """清理固体加样器 🧹"""
        self.logger.info(f"🧹 清理固体分配器 {self.device_id} 🔚")
        self._status = "Idle"
        
        self.logger.info(f"✅ 固体分配器 {self.device_id} 清理完成 💤")
        return True
    
    def parse_mass_string(self, mass_str: str) -> float:
        """
        解析质量字符串为数值 (g) ⚖️
        
        支持格式: "2.9 g", "19.3g", "4.5 mg", "1.2 kg" 等
        """
        if not mass_str or not isinstance(mass_str, str):
            return 0.0
        
        # 移除空格并转小写
        mass_clean = mass_str.strip().lower()
        
        # 正则匹配数字和单位
        pattern = r'(\d+(?:\.\d+)?)\s*([a-z]*)'
        match = re.search(pattern, mass_clean)
        
        if not match:
            self.logger.debug(f"🔍 无法解析质量字符串: {mass_str}")
            return 0.0
        
        try:
            value = float(match.group(1))
            unit = match.group(2) or 'g'  # 默认单位 g
            
            # 单位转换为 g
            unit_multipliers = {
                'g': 1.0,
                'gram': 1.0,
                'grams': 1.0,
                'mg': 0.001,
                'milligram': 0.001,
                'milligrams': 0.001,
                'kg': 1000.0,
                'kilogram': 1000.0,
                'kilograms': 1000.0,
                'μg': 0.000001,
                'ug': 0.000001,
                'microgram': 0.000001,
                'micrograms': 0.000001,
            }
            
            multiplier = unit_multipliers.get(unit, 1.0)
            result = value * multiplier
            
            self.logger.debug(f"⚖️ 质量解析: {mass_str} → {result:.6f}g (原值: {value} {unit})")
            return result
        
        except (ValueError, TypeError):
            self.logger.warning(f"⚠️ 无法解析质量字符串: {mass_str}")
            return 0.0
    
    def parse_mol_string(self, mol_str: str) -> float:
        """
        解析摩尔数字符串为数值 (mol) 🧮
        
        支持格式: "0.12 mol", "16.2 mmol", "25.2mmol" 等
        """
        if not mol_str or not isinstance(mol_str, str):
            return 0.0
        
        # 移除空格并转小写
        mol_clean = mol_str.strip().lower()
        
        # 正则匹配数字和单位
        pattern = r'(\d+(?:\.\d+)?)\s*(m?mol)'
        match = re.search(pattern, mol_clean)
        
        if not match:
            self.logger.debug(f"🔍 无法解析摩尔数字符串: {mol_str}")
            return 0.0
        
        try:
            value = float(match.group(1))
            unit = match.group(2)
            
            # 单位转换为 mol
            if unit == 'mmol':
                result = value * 0.001
            else:  # mol
                result = value
            
            self.logger.debug(f"🧮 摩尔数解析: {mol_str} → {result:.6f}mol (原值: {value} {unit})")
            return result
        
        except (ValueError, TypeError):
            self.logger.warning(f"⚠️ 无法解析摩尔数字符串: {mol_str}")
            return 0.0
    
    def find_solid_reagent_bottle(self, reagent_name: str) -> str:
        """
        查找固体试剂瓶 🔍
        
        这是一个简化版本，实际使用时应该连接到系统的设备图
        """
        if not reagent_name:
            self.logger.debug(f"🔍 未指定试剂名称，使用默认瓶")
            return "unknown_solid_bottle"
        
        # 可能的固体试剂瓶命名模式
        possible_names = [
            f"solid_bottle_{reagent_name}",
            f"reagent_solid_{reagent_name}",
            f"powder_{reagent_name}",
            f"{reagent_name}_solid",
            f"{reagent_name}_powder",
            f"solid_{reagent_name}",
        ]
        
        # 这里简化处理，实际应该查询设备图
        selected_bottle = possible_names[0]
        self.logger.debug(f"🔍 为试剂 {reagent_name} 选择试剂瓶: {selected_bottle}")
        return selected_bottle
    
    async def add_solid(
        self,
        vessel: str,
        reagent: str,
        mass: str = "",
        mol: str = "",
        purpose: str = "",
        **kwargs  # 兼容额外参数
    ) -> Dict[str, Any]:
        """
        添加固体试剂的主要方法 ⚗️
        
        Args:
            vessel: 目标容器
            reagent: 试剂名称
            mass: 质量字符串 (如 "2.9 g")
            mol: 摩尔数字符串 (如 "0.12 mol")
            purpose: 添加目的
            **kwargs: 其他兼容参数
        
        Returns:
            Dict: 操作结果
        """
        try:
            self.logger.info(f"⚗️ === 开始固体加样操作 === ✨")
            self.logger.info(f"  🥽 目标容器: {vessel}")
            self.logger.info(f"  🧪 试剂: {reagent}")
            self.logger.info(f"  ⚖️ 质量: {mass}")
            self.logger.info(f"  🧮 摩尔数: {mol}")
            self.logger.info(f"  📝 目的: {purpose}")
            
            # 参数验证 - 宽松处理
            if not vessel:
                vessel = "main_reactor"  # 默认容器
                self.logger.warning(f"⚠️ 未指定容器，使用默认容器: {vessel} 🏠")
            
            if not reagent:
                error_msg = "❌ 错误: 必须指定试剂名称"
                self.logger.error(error_msg)
                return {
                    "success": False,
                    "message": error_msg,
                    "return_info": "missing_reagent"
                }
            
            # 解析质量和摩尔数
            mass_value = self.parse_mass_string(mass)
            mol_value = self.parse_mol_string(mol)
            
            self.logger.info(f"📊 解析结果 - 质量: {mass_value:.6f}g | 摩尔数: {mol_value:.6f}mol")
            
            # 确定实际加样量
            if mass_value > 0:
                actual_amount = mass_value
                amount_unit = "g"
                amount_emoji = "⚖️"
                self.logger.info(f"⚖️ 按质量加样: {actual_amount:.6f} {amount_unit}")
            elif mol_value > 0:
                # 简化处理：假设分子量为100 g/mol
                assumed_mw = 100.0
                actual_amount = mol_value * assumed_mw
                amount_unit = "g (from mol)"
                amount_emoji = "🧮"
                self.logger.info(f"🧮 按摩尔数加样: {mol_value:.6f} mol → {actual_amount:.6f} g (假设分子量 {assumed_mw})")
            else:
                # 没有指定量，使用默认值
                actual_amount = 1.0
                amount_unit = "g (default)"
                amount_emoji = "🎯"
                self.logger.warning(f"⚠️ 未指定质量或摩尔数，使用默认值: {actual_amount} {amount_unit} 🎯")
            
            # 检查容量限制
            if actual_amount > self.max_capacity:
                error_msg = f"❌ 错误: 请求量 {actual_amount:.3f}g 超过最大容量 {self.max_capacity}g"
                self.logger.error(error_msg)
                return {
                    "success": False,
                    "message": error_msg,
                    "return_info": "exceeds_capacity"
                }
            
            # 查找试剂瓶
            reagent_bottle = self.find_solid_reagent_bottle(reagent)
            self.logger.info(f"🔍 使用试剂瓶: {reagent_bottle}")
            
            # 模拟加样过程
            self._status = "Dispensing"
            self._current_reagent = reagent
            
            # 计算操作时间 (基于质量)
            operation_time = max(0.5, actual_amount * 0.1)  # 每克0.1秒，最少0.5秒
            
            self.logger.info(f"🚀 开始加样，预计时间: {operation_time:.1f}秒 ⏱️")
            
            # 显示进度的模拟
            steps = max(3, int(operation_time))
            step_time = operation_time / steps
            
            for i in range(steps):
                progress = (i + 1) / steps * 100
                await self._ros_node.sleep(step_time)
                if i % 2 == 0:  # 每隔一步显示进度
                    self.logger.debug(f"📊 加样进度: {progress:.0f}% | {amount_emoji} 正在分配 {reagent}...")
            
            # 更新状态
            self._dispensed_amount = actual_amount
            self._total_operations += 1
            self._status = "Ready"
            
            # 成功结果
            success_message = f"✅ 成功添加 {reagent} {actual_amount:.6f} {amount_unit} 到 {vessel}"
            
            self.logger.info(f"🎉 === 固体加样完成 === ✨")
            self.logger.info(f"📊 操作结果:")
            self.logger.info(f"  ✅ {success_message}")
            self.logger.info(f"  🧪 试剂瓶: {reagent_bottle}")
            self.logger.info(f"  ⏱️ 用时: {operation_time:.1f}秒")
            self.logger.info(f"  🎯 总操作次数: {self._total_operations} 🏁")
            
            return {
                "success": True,
                "message": success_message,
                "return_info": f"dispensed_{actual_amount:.6f}g",
                "dispensed_amount": actual_amount,
                "reagent": reagent,
                "vessel": {"id": vessel},
            }
            
        except Exception as e:
            error_message = f"❌ 固体加样失败: {str(e)} 💥"
            self.logger.error(error_message)
            self._status = "Error"
            
            return {
                "success": False,
                "message": error_message,
                "return_info": "operation_failed"
            }
    
    # 状态属性
    @property
    def status(self) -> str:
        return self._status
    
    @property
    def current_reagent(self) -> str:
        return self._current_reagent
    
    @property
    def dispensed_amount(self) -> float:
        return self._dispensed_amount
    
    @property
    def total_operations(self) -> int:
        return self._total_operations
    
    def __str__(self):
        status_emoji = "✅" if self._status == "Ready" else "🔄" if self._status == "Dispensing" else "❌" if self._status == "Error" else "🏠"
        return f"⚗️ VirtualSolidDispenser({status_emoji} {self.device_id}: {self._status}, 最后加样 {self._dispensed_amount:.3f}g)"


# 测试函数
async def test_solid_dispenser():
    """测试固体加样器 🧪"""
    print("⚗️ === 固体加样器测试开始 === 🧪")
    
    dispenser = VirtualSolidDispenser("test_dispenser")
    await dispenser.initialize()
    
    # 测试1: 按质量加样
    print(f"\n🧪 测试1: 按质量加样...")
    result1 = await dispenser.add_solid(
        vessel="main_reactor",
        reagent="magnesium",
        mass="2.9 g"
    )
    print(f"📊 测试1结果: {result1}")
    
    # 测试2: 按摩尔数加样
    print(f"\n🧮 测试2: 按摩尔数加样...")
    result2 = await dispenser.add_solid(
        vessel="main_reactor",
        reagent="sodium_nitrite",
        mol="0.28 mol"
    )
    print(f"📊 测试2结果: {result2}")
    
    # 测试3: 缺少参数
    print(f"\n⚠️ 测试3: 缺少参数测试...")
    result3 = await dispenser.add_solid(
        reagent="test_compound"
    )
    print(f"📊 测试3结果: {result3}")
    
    # 测试4: 超容量测试
    print(f"\n❌ 测试4: 超容量测试...")
    result4 = await dispenser.add_solid(
        vessel="main_reactor",
        reagent="heavy_compound",
        mass="150 g"  # 超过100g限制
    )
    print(f"📊 测试4结果: {result4}")
    print(f"✅ === 测试完成 === 🎉")


if __name__ == "__main__":
    asyncio.run(test_solid_dispenser())