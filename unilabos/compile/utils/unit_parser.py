"""
统一的单位解析工具模块
支持时间、体积、质量等各种单位的解析
"""

import re
from typing import Union

from .logger_util import debug_print


def parse_volume_input(volume_input: Union[str, float, int], default_unit: str = "mL") -> float:
    """
    解析带单位的体积输入
    
    Args:
        volume_input: 体积输入（如 "100 mL", "2.5 L", "500", "?", 100.0）
        default_unit: 默认单位（默认为毫升）
    
    Returns:
        float: 体积（毫升）
    """
    if not volume_input:
        return 0.0
    
    # 处理数值输入
    if isinstance(volume_input, (int, float)):
        result = float(volume_input)
        debug_print(f"数值体积输入: {volume_input} → {result}mL（默认单位）")
        return result
    
    # 处理字符串输入
    volume_str = str(volume_input).lower().strip()
    debug_print(f"解析体积字符串: '{volume_str}'")
    
    # 处理特殊值
    if volume_str in ['?', 'unknown', 'tbd', 'to be determined']:
        default_volume = 50.0  # 50mL默认值
        debug_print(f"检测到未知体积，使用默认值: {default_volume}mL")
        return default_volume
    
    # 如果是纯数字，使用默认单位
    try:
        value = float(volume_str)
        if default_unit.lower() in ["ml", "milliliter"]:
            result = value
        elif default_unit.lower() in ["l", "liter"]:
            result = value * 1000.0
        elif default_unit.lower() in ["μl", "ul", "microliter"]:
            result = value / 1000.0
        else:
            result = value  # 默认mL
        debug_print(f"纯数字输入: {volume_str} → {result}mL（单位: {default_unit}）")
        return result
    except ValueError:
        pass
    
    # 移除空格并提取数字和单位
    volume_clean = re.sub(r'\s+', '', volume_str)
    
    # 匹配数字和单位的正则表达式
    match = re.match(r'([0-9]*\.?[0-9]+)\s*(ml|l|μl|ul|microliter|milliliter|liter)?', volume_clean)
    
    if not match:
        debug_print(f"⚠️ 无法解析体积: '{volume_str}'，使用默认值: 50mL")
        return 50.0
    
    value = float(match.group(1))
    unit = match.group(2) or default_unit.lower()
    
    # 转换为毫升
    if unit in ['l', 'liter']:
        volume = value * 1000.0  # L -> mL
    elif unit in ['μl', 'ul', 'microliter']:
        volume = value / 1000.0  # μL -> mL
    else:  # ml, milliliter 或默认
        volume = value  # 已经是mL
    
    debug_print(f"体积解析: '{volume_str}' → {value} {unit} → {volume}mL")
    return volume


def parse_mass_input(mass_input: Union[str, float]) -> float:
    """
    解析质量输入，支持带单位的字符串

    Args:
        mass_input: 质量输入（如 "19.3 g", "4.5 g", 2.5）

    Returns:
        float: 质量（克）
    """
    if isinstance(mass_input, (int, float)):
        debug_print(f"⚖️ 质量输入为数值: {mass_input}g")
        return float(mass_input)

    if not mass_input or not str(mass_input).strip():
        debug_print(f"⚠️ 质量输入为空，返回0.0g")
        return 0.0

    mass_str = str(mass_input).lower().strip()
    debug_print(f"🔍 解析质量输入: '{mass_str}'")

    # 移除空格并提取数字和单位
    mass_clean = re.sub(r'\s+', '', mass_str)

    # 匹配数字和单位的正则表达式
    match = re.match(r'([0-9]*\.?[0-9]+)\s*(g|mg|kg|gram|milligram|kilogram)?', mass_clean)

    if not match:
        debug_print(f"❌ 无法解析质量: '{mass_str}'，返回0.0g")
        return 0.0

    value = float(match.group(1))
    unit = match.group(2) or 'g'  # 默认单位为克

    # 转换为克
    if unit in ['mg', 'milligram']:
        mass = value / 1000.0  # mg -> g
        debug_print(f"🔄 质量转换: {value}mg → {mass}g")
    elif unit in ['kg', 'kilogram']:
        mass = value * 1000.0  # kg -> g
        debug_print(f"🔄 质量转换: {value}kg → {mass}g")
    else:  # g, gram 或默认
        mass = value  # 已经是g
        debug_print(f"✅ 质量已为g: {mass}g")

    return mass


def parse_time_input(time_input: Union[str, float]) -> float:
    """
    解析时间输入，支持带单位的字符串

    Args:
        time_input: 时间输入（如 "1 h", "20 min", "30 s", 60.0）

    Returns:
        float: 时间（秒）
    """
    if isinstance(time_input, (int, float)):
        debug_print(f"⏱️ 时间输入为数值: {time_input}秒")
        return float(time_input)

    if not time_input or not str(time_input).strip():
        debug_print(f"⚠️ 时间输入为空，返回0秒")
        return 0.0

    time_str = str(time_input).lower().strip()
    debug_print(f"🔍 解析时间输入: '{time_str}'")

    # 处理未知时间
    if time_str in ['?', 'unknown', 'tbd']:
        default_time = 60.0  # 默认1分钟
        debug_print(f"❓ 检测到未知时间，使用默认值: {default_time}s (1分钟) ⏰")
        return default_time

    # 移除空格并提取数字和单位
    time_clean = re.sub(r'\s+', '', time_str)

    # 匹配数字和单位的正则表达式
    match = re.match(r'([0-9]*\.?[0-9]+)\s*(s|sec|second|min|minute|h|hr|hour|d|day)?', time_clean)

    if not match:
        debug_print(f"❌ 无法解析时间: '{time_str}'，返回0s")
        return 0.0

    value = float(match.group(1))
    unit = match.group(2) or 's'  # 默认单位为秒

    # 转换为秒
    if unit in ['m', 'min', 'minute', 'mins', 'minutes']:
        time_sec = value * 60.0  # min -> s
        debug_print(f"🔄 时间转换: {value}分钟 → {time_sec}秒")
    elif unit in ['h', 'hr', 'hour', 'hrs', 'hours']:
        time_sec = value * 3600.0  # h -> s
        debug_print(f"🔄 时间转换: {value}小时 → {time_sec}秒")
    elif unit in ['d', 'day', 'days']:
        time_sec = value * 86400.0  # d -> s
        debug_print(f"🔄 时间转换: {value}天 → {time_sec}秒")
    else:  # s, sec, second 或默认
        time_sec = value  # 已经是s
        debug_print(f"✅ 时间已为秒: {time_sec}秒")

    return time_sec


def parse_temperature_input(temp_input: Union[str, float], default_temp: float = 25.0) -> float:
    """
    解析温度输入，支持字符串和数值

    Args:
        temp_input: 温度输入（如 "256 °C", "reflux", 45.0）
        default_temp: 默认温度

    Returns:
        float: 温度（°C）
    """
    if not temp_input:
        return default_temp

    if isinstance(temp_input, (int, float)):
        return float(temp_input)

    temp_str = str(temp_input).lower().strip()

    # 特殊温度关键词
    special_temps = {
        "room temperature": 25.0, "reflux": 78.0, "ice bath": 0.0,
        "boiling": 100.0, "hot": 60.0, "warm": 40.0, "cold": 10.0,
    }
    if temp_str in special_temps:
        return special_temps[temp_str]

    # 正则解析（如 "256 °C", "45°C", "45"）
    match = re.search(r'(\d+(?:\.\d+)?)\s*°?[cf]?', temp_str)
    if match:
        return float(match.group(1))

    debug_print(f"无法解析温度: '{temp_str}'，使用默认值: {default_temp}°C")
    return default_temp

# 测试函数
def test_unit_parser():
    """测试单位解析功能"""
    print("=== 单位解析器测试 ===")
    
    # 测试时间解析
    time_tests = [
        "30 min", "1 h", "300", "5.5 h", "?", 60.0, "2 hours", "30 s"
    ]
    
    print("\n时间解析测试:")
    for time_input in time_tests:
        result = parse_time_input(time_input)
        print(f"  {time_input} → {result}s ({result/60:.1f}min)")
    
    # 测试体积解析
    volume_tests = [
        "100 mL", "2.5 L", "500", "?", 100.0, "500 μL", "1 liter"
    ]
    
    print("\n体积解析测试:")
    for volume_input in volume_tests:
        result = parse_volume_input(volume_input)
        print(f"  {volume_input} → {result}mL")
    
    print("\n✅ 测试完成")

if __name__ == "__main__":
    test_unit_parser()