from typing import List, Any, Dict
import xml.etree.ElementTree as ET


def convert_to_type(val: str) -> Any:
    """将字符串值转换为适当的数据类型"""
    if val == "True":
        return True
    if val == "False":
        return False
    if val == "?":
        return None
    if val.endswith(" g"):
        return float(val.split(" ")[0])
    if val.endswith("mg"):
        return float(val.split("mg")[0])
    elif val.endswith("mmol"):
        return float(val.split("mmol")[0]) / 1000
    elif val.endswith("mol"):
        return float(val.split("mol")[0])
    elif val.endswith("ml"):
        return float(val.split("ml")[0])
    elif val.endswith("RPM"):
        return float(val.split("RPM")[0])
    elif val.endswith(" °C"):
        return float(val.split(" ")[0])
    elif val.endswith(" %"):
        return float(val.split(" ")[0])
    return val


def flatten_xdl_procedure(procedure_elem: ET.Element) -> List[ET.Element]:
    """展平嵌套的XDL程序结构"""
    flattened_operations = []
    TEMP_UNSUPPORTED_PROTOCOL = ["Purge", "Wait", "Stir", "ResetHandling"]

    def extract_operations(element: ET.Element):
        if element.tag not in ["Prep", "Reaction", "Workup", "Purification", "Procedure"]:
            if element.tag not in TEMP_UNSUPPORTED_PROTOCOL:
                flattened_operations.append(element)

        for child in element:
            extract_operations(child)

    for child in procedure_elem:
        extract_operations(child)

    return flattened_operations


def parse_xdl_content(xdl_content: str) -> tuple:
    """解析XDL内容"""
    try:
        xdl_content_cleaned = "".join(c for c in xdl_content if c.isprintable())
        root = ET.fromstring(xdl_content_cleaned)

        synthesis_elem = root.find("Synthesis")
        if synthesis_elem is None:
            return None, None, None

        # 解析硬件组件
        hardware_elem = synthesis_elem.find("Hardware")
        hardware = []
        if hardware_elem is not None:
            hardware = [{"id": c.get("id"), "type": c.get("type")} for c in hardware_elem.findall("Component")]

        # 解析试剂
        reagents_elem = synthesis_elem.find("Reagents")
        reagents = []
        if reagents_elem is not None:
            reagents = [{"name": r.get("name"), "role": r.get("role", "")} for r in reagents_elem.findall("Reagent")]

        # 解析程序
        procedure_elem = synthesis_elem.find("Procedure")
        if procedure_elem is None:
            return None, None, None

        flattened_operations = flatten_xdl_procedure(procedure_elem)
        return hardware, reagents, flattened_operations

    except ET.ParseError as e:
        raise ValueError(f"Invalid XDL format: {e}")


def convert_xdl_to_dict(xdl_content: str) -> Dict[str, Any]:
    """
    将XDL XML格式转换为标准的字典格式

    Args:
        xdl_content: XDL XML内容

    Returns:
        转换结果，包含步骤和器材信息
    """
    try:
        hardware, reagents, flattened_operations = parse_xdl_content(xdl_content)
        if hardware is None:
            return {"error": "Failed to parse XDL content", "success": False}

        # 将XDL元素转换为字典格式
        steps_data = []
        for elem in flattened_operations:
            # 转换参数类型
            parameters = {}
            for key, val in elem.attrib.items():
                converted_val = convert_to_type(val)
                if converted_val is not None:
                    parameters[key] = converted_val

            step_dict = {
                "operation": elem.tag,
                "parameters": parameters,
                "description": elem.get("purpose", f"Operation: {elem.tag}"),
            }
            steps_data.append(step_dict)

        # 合并硬件和试剂为统一的labware_info格式
        labware_data = []
        labware_data.extend({"id": hw["id"], "type": "hardware", **hw} for hw in hardware)
        labware_data.extend({"name": reagent["name"], "type": "reagent", **reagent} for reagent in reagents)

        return {
            "success": True,
            "steps": steps_data,
            "labware": labware_data,
            "message": f"Successfully converted XDL to dict format. Found {len(steps_data)} steps and {len(labware_data)} labware items.",
        }

    except Exception as e:
        error_msg = f"XDL conversion failed: {str(e)}"
        return {"error": error_msg, "success": False}
