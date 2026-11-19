from pydantic import BaseModel, Field
import pint


class Point3D(BaseModel):
    x: float = Field(..., title="X coordinate")
    y: float = Field(..., title="Y coordinate")
    z: float = Field(..., title="Z coordinate")

# Start Protocols

class PumpTransferProtocol(BaseModel):
    # === 核心参数（保持必需） ===
    from_vessel: dict
    to_vessel: dict
    
    # === 所有其他参数都改为可选，添加默认值 ===
    volume: float = 0.0  # 🔧 改为-1，表示转移全部体积
    amount: str = ""
    time: float = 0.0
    viscous: bool = False
    rinsing_solvent: str = ""
    rinsing_volume: float = 0.0
    rinsing_repeats: int = 0
    solid: bool = False
    flowrate: float = 2.5
    transfer_flowrate: float = 0.5
    
    # === 新版XDL兼容参数（可选） ===
    rate_spec: str = ""
    event: str = ""
    through: str = ""
    
    def model_post_init(self, __context):
        """后处理：智能参数处理和兼容性调整"""
            
        # 如果指定了 amount 但volume是默认值，尝试解析 amount
        if self.amount and self.volume == 0.0:
            parsed_volume = self._parse_amount_to_volume(self.amount)
            if parsed_volume > 0:
                self.volume = parsed_volume
        
        # 如果指定了 time 但没有明确设置流速，根据时间计算流速
        if self.time > 0 and self.volume > 0:
            if self.flowrate == 2.5 and self.transfer_flowrate == 0.5:
                calculated_flowrate = self.volume / self.time
                self.flowrate = min(calculated_flowrate, 10.0)
                self.transfer_flowrate = min(calculated_flowrate, 5.0)
        
        # 🔧 核心修复：如果flowrate为0（ROS2传入），使用默认值
        if self.flowrate <= 0:
            self.flowrate = 2.5
        if self.transfer_flowrate <= 0:
            self.transfer_flowrate = 0.5
            
        # 根据 rate_spec 调整流速
        if self.rate_spec == "dropwise":
            self.flowrate = min(self.flowrate, 0.1)
            self.transfer_flowrate = min(self.transfer_flowrate, 0.1)
        elif self.rate_spec == "slowly":
            self.flowrate = min(self.flowrate, 0.5)
            self.transfer_flowrate = min(self.transfer_flowrate, 0.3)
        elif self.rate_spec == "quickly":
            self.flowrate = max(self.flowrate, 5.0)
            self.transfer_flowrate = max(self.transfer_flowrate, 2.0)
    
    def _parse_amount_to_volume(self, amount: str) -> float:
        """解析 amount 字符串为体积"""
        if not amount:
            return 0.0
        
        amount = amount.lower().strip()
        
        # 处理特殊关键词
        if amount == "all":
            return 0.0  # 🔧 "all"也表示转移全部
        
        # 提取数字
        import re
        numbers = re.findall(r'[\d.]+', amount)
        if numbers:
            volume = float(numbers[0])
            
            # 单位转换
            if 'ml' in amount or 'milliliter' in amount:
                return volume
            elif 'l' in amount and 'ml' not in amount:
                return volume * 1000
            elif 'μl' in amount or 'microliter' in amount:
                return volume / 1000
            else:
                return volume
        
        return 0.0


class CleanProtocol(BaseModel):
    vessel: dict
    solvent: str
    volume: float
    temp: float
    repeats: int = 1


class SeparateProtocol(BaseModel):
    purpose: str
    product_phase: str
    from_vessel: dict
    separation_vessel: dict
    to_vessel: dict
    waste_phase_to_vessel: dict
    solvent: str
    solvent_volume: float
    through: str
    repeats: int
    stir_time: float
    stir_speed: float
    settling_time: float


class EvaporateProtocol(BaseModel):
    # === 核心参数（必需） ===
    vessel: dict = Field(..., description="蒸发容器名称")
    
    # === 所有其他参数都改为可选，添加默认值 ===
    pressure: float = Field(0.1, description="真空度 (bar)，默认0.1 bar")
    temp: float = Field(60.0, description="加热温度 (°C)，默认60°C")
    time: float = Field(180.0, description="蒸发时间 (秒)，默认1800s (30分钟)")
    stir_speed: float = Field(100.0, description="旋转速度 (RPM)，默认100 RPM")
    
    # === 新版XDL兼容参数（可选） ===
    solvent: str = Field("", description="溶剂名称（用于识别蒸发的溶剂类型）")
    
    def model_post_init(self, __context):
        """后处理：智能参数处理和兼容性调整"""
        
        # 参数范围验证和修正
        if self.pressure <= 0 or self.pressure > 1.0:
            logger.warning(f"真空度 {self.pressure} bar 超出范围，修正为 0.1 bar")
            self.pressure = 0.1
        
        if self.temp < 10.0 or self.temp > 200.0:
            logger.warning(f"温度 {self.temp}°C 超出范围，修正为 60°C")
            self.temp = 60.0
        
        if self.time <= 0:
            logger.warning(f"时间 {self.time}s 无效，修正为 1800s")
            self.time = 1800.0
        
        if self.stir_speed < 10.0 or self.stir_speed > 300.0:
            logger.warning(f"旋转速度 {self.stir_speed} RPM 超出范围，修正为 100 RPM")
            self.stir_speed = 100.0
        
        # 根据溶剂类型调整参数
        if self.solvent:
            self._adjust_parameters_by_solvent()
    
    def _adjust_parameters_by_solvent(self):
        """根据溶剂类型调整蒸发参数"""
        solvent_lower = self.solvent.lower()
        
        # 水系溶剂：较高温度，较低真空度
        if any(s in solvent_lower for s in ['water', 'aqueous', 'h2o']):
            if self.temp == 60.0:  # 如果是默认值，则调整
                self.temp = 80.0
            if self.pressure == 0.1:
                self.pressure = 0.2
        
        # 有机溶剂：根据沸点调整
        elif any(s in solvent_lower for s in ['ethanol', 'methanol', 'acetone']):
            if self.temp == 60.0:
                self.temp = 50.0
            if self.pressure == 0.1:
                self.pressure = 0.05
        
        # 高沸点溶剂：更高温度
        elif any(s in solvent_lower for s in ['dmso', 'dmi', 'toluene']):
            if self.temp == 60.0:
                self.temp = 100.0
            if self.pressure == 0.1:
                self.pressure = 0.01


class EvacuateAndRefillProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="目标容器名称")
    gas: str = Field(..., description="气体名称")
    
    # 🔧 删除 repeats 参数，直接在代码中硬编码为 3 次
    
    def model_post_init(self, __context):
        """后处理：参数验证和兼容性调整"""
        
        # 验证气体名称
        if not self.gas.strip():
            logger.warning("气体名称为空，使用默认值 'nitrogen'")
            self.gas = "nitrogen"
        
        # 标准化气体名称
        gas_aliases = {
            'n2': 'nitrogen',
            'ar': 'argon', 
            'air': 'air',
            'o2': 'oxygen',
            'co2': 'carbon_dioxide',
            'h2': 'hydrogen'
        }
        
        gas_lower = self.gas.lower().strip()
        if gas_lower in gas_aliases:
            self.gas = gas_aliases[gas_lower]


class AGVTransferProtocol(BaseModel):
    from_repo: dict
    to_repo: dict
    from_repo_position: str
    to_repo_position: str

#=============新添加的新的协议================
class AddProtocol(BaseModel):
    vessel: dict
    reagent: str
    volume: float
    mass: float
    amount: str
    time: float
    stir: bool
    stir_speed: float
    viscous: bool
    purpose: str

class CentrifugeProtocol(BaseModel):
    vessel: dict
    speed: float
    time: float
    temp: float

class FilterProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="过滤容器名称")
    
    # === 可选参数 ===
    filtrate_vessel: dict = Field("", description="滤液容器名称（可选，自动查找）")
    
    def model_post_init(self, __context):
        """后处理：参数验证"""
        # 验证容器名称
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")

class HeatChillProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="加热容器名称")
    
    # === 可选参数 - 温度相关 ===
    temp: float = Field(25.0, description="目标温度 (°C)")
    temp_spec: str = Field("", description="温度规格（如 'room temperature', 'reflux'）")
    
    # === 可选参数 - 时间相关 ===
    time: float = Field(300.0, description="加热时间 (秒)")
    time_spec: str = Field("", description="时间规格（如 'overnight', '2 h'）")
    
    # === 可选参数 - 其他XDL参数 ===
    pressure: str = Field("", description="压力规格（如 '1 mbar'），不做特殊处理")
    reflux_solvent: str = Field("", description="回流溶剂名称，不做特殊处理")
    
    # === 可选参数 - 搅拌相关 ===
    stir: bool = Field(False, description="是否搅拌")
    stir_speed: float = Field(300.0, description="搅拌速度 (RPM)")
    purpose: str = Field("", description="操作目的")
    
    def model_post_init(self, __context):
        """后处理：参数验证和解析"""
        
        # 验证必需参数
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")
        
        # 温度解析：优先使用 temp_spec，然后是 temp
        if self.temp_spec:
            self.temp = self._parse_temp_spec(self.temp_spec)
        
        # 时间解析：优先使用 time_spec，然后是 time
        if self.time_spec:
            self.time = self._parse_time_spec(self.time_spec)
        
        # 参数范围验证
        if self.temp < -50.0 or self.temp > 300.0:
            logger.warning(f"温度 {self.temp}°C 超出范围，修正为 25°C")
            self.temp = 25.0
        
        if self.time < 0:
            logger.warning(f"时间 {self.time}s 无效，修正为 300s")
            self.time = 300.0
        
        if self.stir_speed < 0 or self.stir_speed > 1500.0:
            logger.warning(f"搅拌速度 {self.stir_speed} RPM 超出范围，修正为 300 RPM")
            self.stir_speed = 300.0
    
    def _parse_temp_spec(self, temp_spec: str) -> float:
        """解析温度规格为具体温度"""
        
        temp_spec = temp_spec.strip().lower()
        
        # 特殊温度规格
        special_temps = {
            "room temperature": 25.0,      # 室温
            "reflux": 78.0,                 # 默认回流温度（乙醇沸点）
            "ice bath": 0.0,                # 冰浴
            "boiling": 100.0,               # 沸腾
            "hot": 60.0,                    # 热
            "warm": 40.0,                   # 温热
            "cold": 10.0,                   # 冷
        }
        
        if temp_spec in special_temps:
            return special_temps[temp_spec]
        
        # 解析带单位的温度（如 "256 °C"）
        import re
        temp_pattern = r'(\d+(?:\.\d+)?)\s*°?[cf]?'
        match = re.search(temp_pattern, temp_spec)
        
        if match:
            return float(match.group(1))
        
        return 25.0  # 默认室温
    
    def _parse_time_spec(self, time_spec: str) -> float:
        """解析时间规格为秒数"""
        
        time_spec = time_spec.strip().lower()
        
        # 特殊时间规格
        special_times = {
            "overnight": 43200.0,           # 12小时
            "several hours": 10800.0,       # 3小时
            "few hours": 7200.0,            # 2小时
            "long time": 3600.0,            # 1小时
            "short time": 300.0,            # 5分钟
        }
        
        if time_spec in special_times:
            return special_times[time_spec]
        
        # 解析带单位的时间（如 "2 h"）
        import re
        time_pattern = r'(\d+(?:\.\d+)?)\s*([a-zA-Z]+)'
        match = re.search(time_pattern, time_spec)
        
        if match:
            value = float(match.group(1))
            unit = match.group(2).lower()
            
            unit_multipliers = {
                's': 1.0,
                'sec': 1.0,
                'second': 1.0,
                'seconds': 1.0,
                'min': 60.0,
                'minute': 60.0,
                'minutes': 60.0,
                'h': 3600.0,
                'hr': 3600.0,
                'hour': 3600.0,
                'hours': 3600.0,
            }
            
            multiplier = unit_multipliers.get(unit, 3600.0)  # 默认按小时计算
            return value * multiplier
        
        return 300.0  # 默认5分钟


class HeatChillStartProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="加热容器名称")

    # === 可选参数 - 温度相关 ===
    temp: float = Field(25.0, description="目标温度 (°C)")
    temp_spec: str = Field("", description="温度规格（如 'room temperature', 'reflux'）")

    # === 可选参数 - 其他XDL参数 ===
    pressure: str = Field("", description="压力规格（如 '1 mbar'），不做特殊处理")
    reflux_solvent: str = Field("", description="回流溶剂名称，不做特殊处理")

    # === 可选参数 - 搅拌相关 ===
    stir: bool = Field(False, description="是否搅拌")
    stir_speed: float = Field(300.0, description="搅拌速度 (RPM)")
    purpose: str = Field("", description="操作目的")


class HeatChillStopProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="加热容器名称")


class StirProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="搅拌容器名称")
    
    # === 可选参数 ===
    time: str = Field("5 min", description="搅拌时间（如 '0.5 h', '30 min'）")
    event: str = Field("", description="事件标识（如 'A', 'B'）")
    time_spec: str = Field("", description="时间规格（如 'several minutes', 'overnight'）")
    
    def model_post_init(self, __context):
        """后处理：参数验证和时间解析"""
        
        # 验证必需参数
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")
        
        # 优先使用 time_spec，然后是 time
        if self.time_spec:
            self.time = self.time_spec
        
        # 时间解析和验证
        if self.time:
            try:
                # 解析时间字符串为秒数
                parsed_time = self._parse_time_string(self.time)
                if parsed_time <= 0:
                    logger.warning(f"时间 '{self.time}' 解析结果无效，使用默认值 300s")
                    self.time = "5 min"
            except Exception as e:
                logger.warning(f"时间 '{self.time}' 解析失败: {e}，使用默认值 300s")
                self.time = "5 min"
    
    def _parse_time_string(self, time_str: str) -> float:
        """解析时间字符串为秒数"""
        import re
        
        time_str = time_str.strip().lower()
        
        # 特殊时间规格
        special_times = {
            "several minutes": 300.0,    # 5分钟
            "few minutes": 180.0,        # 3分钟
            "overnight": 43200.0,        # 12小时
            "room temperature": 300.0,   # 默认5分钟
        }
        
        if time_str in special_times:
            return special_times[time_str]
        
        # 正则表达式匹配数字和单位
        pattern = r'(\d+\.?\d*)\s*([a-zA-Z]+)'
        match = re.match(pattern, time_str)
        
        if not match:
            return 300.0  # 默认5分钟
        
        value = float(match.group(1))
        unit = match.group(2).lower()
        
        # 时间单位转换
        unit_multipliers = {
            's': 1.0,
            'sec': 1.0,
            'second': 1.0,
            'seconds': 1.0,
            'min': 60.0,
            'minute': 60.0,
            'minutes': 60.0,
            'h': 3600.0,
            'hr': 3600.0,
            'hour': 3600.0,
            'hours': 3600.0,
            'd': 86400.0,
            'day': 86400.0,
            'days': 86400.0,
        }
        
        multiplier = unit_multipliers.get(unit, 60.0)  # 默认按分钟计算
        return value * multiplier
    
class StartStirProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="搅拌容器名称")
    
    # === 可选参数，添加默认值 ===
    stir_speed: float = Field(200.0, description="搅拌速度 (RPM)，默认200 RPM")
    purpose: str = Field("", description="搅拌目的（可选）")
    
    def model_post_init(self, __context):
        """后处理：参数验证和修正"""
        
        # 验证必需参数
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")
        
        # 修正参数范围
        if self.stir_speed < 10.0:
            logger.warning(f"搅拌速度 {self.stir_speed} RPM 过低，修正为 100 RPM")
            self.stir_speed = 100.0
        elif self.stir_speed > 1500.0:
            logger.warning(f"搅拌速度 {self.stir_speed} RPM 过高，修正为 1000 RPM")
            self.stir_speed = 1000.0

class StopStirProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="搅拌容器名称")
    
    def model_post_init(self, __context):
        """后处理：参数验证"""
        
        # 验证必需参数
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")

class TransferProtocol(BaseModel):
    from_vessel: dict
    to_vessel: dict
    volume: float
    amount: str = ""
    time: float = 0
    viscous: bool = False
    rinsing_solvent: str = ""
    rinsing_volume: float = 0.0
    rinsing_repeats: int = 0
    solid: bool = False

class CleanVesselProtocol(BaseModel):
    vessel: dict
    solvent: str
    volume: float
    temp: float
    repeats: int = 1

class DissolveProtocol(BaseModel):
    vessel: dict
    solvent: str
    volume: float
    amount: str = ""
    temp: float = 25.0
    time: float = 0.0
    stir_speed: float = 0.0

class FilterThroughProtocol(BaseModel):
    from_vessel: dict
    to_vessel: dict
    filter_through: str
    eluting_solvent: str = ""
    eluting_volume: float = 0.0
    eluting_repeats: int = 0
    residence_time: float = 0.0

class RunColumnProtocol(BaseModel):
    from_vessel: dict
    to_vessel: dict
    column: str

class WashSolidProtocol(BaseModel):
    # === 必需参数 ===
    vessel: dict = Field(..., description="装有固体的容器名称")
    solvent: str = Field(..., description="清洗溶剂名称")
    volume: float = Field(..., description="清洗溶剂体积 (mL)")
    
    # === 可选参数，添加默认值 ===
    filtrate_vessel: dict = Field("", description="滤液收集容器（可选，自动查找）")
    temp: float = Field(25.0, description="清洗温度 (°C)，默认25°C")
    stir: bool = Field(False, description="是否搅拌，默认False")
    stir_speed: float = Field(0.0, description="搅拌速度 (RPM)，默认0")
    time: float = Field(0.0, description="清洗时间 (秒)，默认0")
    repeats: int = Field(1, description="重复次数，默认1")
    
    def model_post_init(self, __context):
        """后处理：参数验证和修正"""
        
        # 验证必需参数
        if not self.vessel.strip():
            raise ValueError("vessel 参数不能为空")
        
        if not self.solvent.strip():
            raise ValueError("solvent 参数不能为空")
        
        if self.volume <= 0:
            raise ValueError("volume 必须大于0")
        
        # 修正参数范围
        if self.temp < 0 or self.temp > 200:
            logger.warning(f"温度 {self.temp}°C 超出范围，修正为 25°C")
            self.temp = 25.0
        
        if self.stir_speed < 0 or self.stir_speed > 500:
            logger.warning(f"搅拌速度 {self.stir_speed} RPM 超出范围，修正为 0")
            self.stir_speed = 0.0
        
        if self.time < 0:
            logger.warning(f"时间 {self.time}s 无效，修正为 0")
            self.time = 0.0
        
        if self.repeats < 1:
            logger.warning(f"重复次数 {self.repeats} 无效，修正为 1")
            self.repeats = 1
        elif self.repeats > 10:
            logger.warning(f"重复次数 {self.repeats} 过多，修正为 10")
            self.repeats = 10
            
class AdjustPHProtocol(BaseModel):
    vessel: dict = Field(..., description="目标容器")
    ph_value: float = Field(..., description="目标pH值")  # 改为 ph_value
    reagent: str = Field(..., description="酸碱试剂名称")
    # 移除其他可选参数，使用默认值

class ResetHandlingProtocol(BaseModel):
    solvent: str = Field(..., description="溶剂名称")

class DryProtocol(BaseModel):
    compound: str = Field(..., description="化合物名称")
    vessel: dict = Field(..., description="目标容器")

class RecrystallizeProtocol(BaseModel):
    ratio: str = Field(..., description="溶剂比例（如 '1:1', '3:7'）")
    solvent1: str = Field(..., description="第一种溶剂名称")
    solvent2: str = Field(..., description="第二种溶剂名称")
    vessel: dict = Field(..., description="目标容器")
    volume: float = Field(..., description="总体积 (mL)")

class HydrogenateProtocol(BaseModel):
    temp: str = Field(..., description="反应温度（如 '45 °C'）")
    time: str = Field(..., description="反应时间（如 '2 h'）")
    vessel: dict = Field(..., description="反应容器")

__all__ = [
    "Point3D", "PumpTransferProtocol", "CleanProtocol", "SeparateProtocol", 
    "EvaporateProtocol", "EvacuateAndRefillProtocol", "AGVTransferProtocol", 
    "CentrifugeProtocol", "AddProtocol", "FilterProtocol", 
    "HeatChillProtocol",
    "HeatChillStartProtocol", "HeatChillStopProtocol",
    "StirProtocol", "StartStirProtocol", "StopStirProtocol", 
    "TransferProtocol", "CleanVesselProtocol", "DissolveProtocol", 
    "FilterThroughProtocol", "RunColumnProtocol", "WashSolidProtocol",
    "AdjustPHProtocol", "ResetHandlingProtocol", "DryProtocol", 
    "RecrystallizeProtocol", "HydrogenateProtocol"
]
# End Protocols
