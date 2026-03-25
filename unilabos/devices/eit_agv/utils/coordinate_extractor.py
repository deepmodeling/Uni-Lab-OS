# coding:utf-8
"""
功能:
    从program_file目录中的JSON文件提取所有启用的运动函数坐标,
    并利用arm_driver中的正运动学函数将关节坐标转换为位姿坐标
"""

import os
import json
import logging
from typing import Dict, List, Any, Optional

# 配置日志
logger = logging.getLogger(__name__)


class ProgramCoordinateExtractor:
    """
    功能:
        提取程序文件中的运动坐标并进行坐标转换
    """

    # 运动函数标签列表
    MOTION_TAGS = ["MoveJ2", "MoveL", "MoveJ", "Start"]

    def __init__(self, program_dir: str = None, arm_driver=None):
        """
        功能:
            初始化坐标提取器
        参数:
            program_dir: 程序文件目录路径, 默认为当前目录下的program_file
            arm_driver: ArmDriver实例, 用于坐标转换, 为None时仅提取不转换
        """
        if program_dir is None:
            # 默认路径: 项目根目录下的program_file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            program_dir = os.path.join(os.path.dirname(current_dir), "program_file")

        self.program_dir = program_dir
        self.arm_driver = arm_driver
        logger.info(f"坐标提取器初始化, 程序目录: {self.program_dir}")

    def extract_all_files(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        功能:
            提取program_file目录下所有JSON和JSPF文件中的启用运动坐标
        返回:
            Dict, 键为文件名, 值为该文件中提取的运动坐标列表
        """
        results = {}

        if not os.path.exists(self.program_dir):
            logger.error(f"程序目录不存在: {self.program_dir}")
            return results

        # 遍历目录下所有JSON和JSPF文件
        for filename in os.listdir(self.program_dir):
            if filename.endswith(".json") or filename.endswith(".jspf"):
                filepath = os.path.join(self.program_dir, filename)
                try:
                    file_results = self.extract_from_file(filepath)
                    if file_results:
                        results[filename] = file_results
                        logger.info(f"从 {filename} 提取了 {len(file_results)} 个运动坐标")
                except Exception as e:
                    logger.error(f"处理文件 {filename} 时出错: {e}")

        return results

    def extract_from_file(self, filepath: str) -> List[Dict[str, Any]]:
        """
        功能:
            从单个JSON文件中提取启用的运动坐标
        参数:
            filepath: JSON文件路径
        返回:
            List, 包含所有启用运动函数的坐标信息
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        results = []
        filename = os.path.basename(filepath)

        # 遍历所有模块
        for module in data.get("modules", []):
            module_name = module.get("name", "Unknown")
            tasks = module.get("tasks", [])
            self._extract_from_tasks(tasks, results, filename, module_name)

        return results

    def _extract_from_tasks(self, tasks: List[Dict], results: List[Dict],
                           filename: str, module_name: str, parent_disabled: bool = False):
        """
        功能:
            递归提取任务列表中的运动坐标
        参数:
            tasks: 任务列表
            results: 结果列表(会被修改)
            filename: 文件名
            module_name: 模块名
            parent_disabled: 父级是否被禁用
        """
        for task in tasks:
            task_tag = task.get("tag", "")
            task_id = task.get("id", "")
            # 如果父级禁用或当前任务禁用, 则跳过
            is_disabled = parent_disabled or task.get("disabled", False)

            # 检查是否为运动函数
            if task_tag in self.MOTION_TAGS and not is_disabled:
                params = task.get("params", {})
                pose_data = params.get("pose", {})

                # 只处理有具体坐标值的(type为"p"), 跳过变量引用(type为"v")
                if pose_data.get("type") == "p":
                    coord_info = self._extract_coordinate_info(
                        task, pose_data, filename, module_name
                    )
                    if coord_info is not None:
                        results.append(coord_info)
                elif pose_data.get("type") == "v":
                    # 记录变量引用信息
                    var_info = {
                        "file": filename,
                        "module": module_name,
                        "task_id": task_id,
                        "motion_type": task_tag,
                        "coordinate_type": "variable",
                        "variable_name": pose_data.get("val", ""),
                        "point_name": pose_data.get("name", ""),
                        "joint_values": None,
                        "pose_values": None,
                        "comment": params.get("comment", "")
                    }
                    results.append(var_info)

            # 递归处理子任务
            sub_tasks = task.get("tasks", [])
            if sub_tasks:
                self._extract_from_tasks(sub_tasks, results, filename, module_name, is_disabled)

    def _extract_coordinate_info(self, task: Dict, pose_data: Dict,
                                 filename: str, module_name: str) -> Optional[Dict[str, Any]]:
        """
        功能:
            提取单个运动任务的坐标信息
        参数:
            task: 任务字典
            pose_data: 位姿数据字典
            filename: 文件名
            module_name: 模块名
        返回:
            Dict, 包含坐标信息的字典, 提取失败返回None
        """
        task_tag = task.get("tag", "")
        task_id = task.get("id", "")
        params = task.get("params", {})

        # 获取坐标值
        coord_values = pose_data.get("val", [])
        point_name = pose_data.get("name", "")

        if not coord_values or len(coord_values) != 6:
            return None

        # 判断坐标类型: MoveJ2使用关节坐标, MoveL使用位姿坐标
        # 根据params中的type字段判断
        coord_type_param = params.get("type", "")

        # MoveL默认是位姿坐标, MoveJ2根据type参数判断
        if task_tag == "MoveL":
            is_joint = False
        elif task_tag in ["MoveJ2", "MoveJ"]:
            is_joint = (coord_type_param == "joint")
        elif task_tag == "Start":
            is_joint = False  # Start通常是位姿坐标
        else:
            is_joint = False

        result = {
            "file": filename,
            "module": module_name,
            "task_id": task_id,
            "motion_type": task_tag,
            "coordinate_type": "joint" if is_joint else "pose",
            "point_name": point_name,
            "original_values": coord_values,
            "joint_values": None,
            "pose_values": None,
            "comment": params.get("comment", "")
        }

        if is_joint:
            result["joint_values"] = coord_values
            # 如果有arm_driver, 进行正运动学转换
            if self.arm_driver is not None:
                try:
                    pose = self.arm_driver.calculate_forward_kinematics(coord_values)
                    if pose:
                        # 将位姿坐标的前3个值(x,y,z)从米转换为毫米
                        pose_mm = list(pose)
                        pose_mm[0] *= 1000  # x: m -> mm
                        pose_mm[1] *= 1000  # y: m -> mm
                        pose_mm[2] *= 1000  # z: m -> mm
                        result["pose_values"] = pose_mm
                    else:
                        result["pose_values"] = None
                except Exception as e:
                    logger.warning(f"正运动学计算失败 ({point_name}): {e}")
                    result["pose_values"] = None
        else:
            # 将位姿坐标的前3个值(x,y,z)从米转换为毫米
            pose_mm = coord_values.copy()
            pose_mm[0] *= 1000  # x: m -> mm
            pose_mm[1] *= 1000  # y: m -> mm
            pose_mm[2] *= 1000  # z: m -> mm
            result["pose_values"] = pose_mm
            # 如果有arm_driver, 进行逆运动学转换
            if self.arm_driver is not None:
                try:
                    joints = self.arm_driver.calculate_inverse_kinematics(coord_values)
                    result["joint_values"] = list(joints) if joints else None
                except Exception as e:
                    logger.warning(f"逆运动学计算失败 ({point_name}): {e}")
                    result["joint_values"] = None

        return result

    def convert_joints_to_pose(self, joint_values: List[float]) -> Optional[List[float]]:
        """
        功能:
            将关节坐标转换为位姿坐标
        参数:
            joint_values: 关节角度列表[q1,q2,q3,q4,q5,q6], 单位rad
        返回:
            位姿列表[x,y,z,rx,ry,rz], 单位m和rad, 转换失败返回None
        """
        if self.arm_driver is None:
            logger.warning("未设置arm_driver, 无法进行坐标转换")
            return None

        try:
            pose = self.arm_driver.calculate_forward_kinematics(joint_values)
            return list(pose) if pose else None
        except Exception as e:
            logger.error(f"正运动学计算失败: {e}")
            return None

    def generate_report(self, results: Dict[str, List[Dict[str, Any]]]) -> str:
        """
        功能:
            生成坐标提取报告
        参数:
            results: extract_all_files返回的结果字典
        返回:
            str, 格式化的报告文本
        """
        lines = []
        lines.append("=" * 80)
        lines.append("程序文件运动坐标提取报告")
        lines.append("=" * 80)

        total_count = 0

        for filename, coords in results.items():
            lines.append(f"\n文件: {filename}")
            lines.append("-" * 40)

            for coord in coords:
                total_count += 1
                lines.append(f"\n  运动类型: {coord['motion_type']}")
                lines.append(f"  点位名称: {coord.get('point_name', 'N/A')}")
                lines.append(f"  坐标类型: {coord['coordinate_type']}")
                lines.append(f"  备注: {coord.get('comment', '')}")

                if coord['coordinate_type'] == "variable":
                    lines.append(f"  变量名: {coord.get('variable_name', 'N/A')}")
                else:
                    if coord.get('original_values') is not None:
                        original_str = ", ".join([f"{v:.6f}" for v in coord['original_values']])
                        lines.append(f"  原始值: [{original_str}]")

                    if coord.get('joint_values') is not None:
                        joint_str = ", ".join([f"{v:.6f}" for v in coord['joint_values']])
                        lines.append(f"  关节坐标(rad): [{joint_str}]")

                    if coord.get('pose_values') is not None:
                        pose_str = ", ".join([f"{v:.6f}" for v in coord['pose_values']])
                        lines.append(f"  位姿坐标(mm,rad): [{pose_str}]")

        lines.append("\n" + "=" * 80)
        lines.append(f"总计: {len(results)} 个文件, {total_count} 个运动坐标")
        lines.append("=" * 80)

        return "\n".join(lines)

    def export_to_json(self, results: Dict[str, List[Dict[str, Any]]],
                       output_path: str = None) -> str:
        """
        功能:
            将提取结果导出为JSON文件, 坐标数组显示在一行
        参数:
            results: extract_all_files返回的结果字典
            output_path: 输出文件路径, 默认为program_file目录下的coordinates_export.json
        返回:
            str, 输出文件路径
        """
        if output_path is None:
            output_path = os.path.join(self.program_dir, "coordinates_export.json")

        # 先生成标准JSON字符串
        json_str = json.dumps(results, ensure_ascii=False, indent=2)

        # 将坐标数组压缩到一行: 匹配包含6个数字的数组
        import re
        # 匹配形如 [\n    数字,\n    数字,\n    ...] 的模式并压缩为一行
        json_str = re.sub(
            r'\[\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*,\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*,\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*,\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*,\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*,\s*(-?\d+\.?\d*(?:e[+-]?\d+)?)\s*\]',
            r'[\1, \2, \3, \4, \5, \6]',
            json_str
        )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json_str)

        logger.info(f"坐标数据已导出到: {output_path}")
        return output_path


def main():
    """
    功能:
        主函数, 演示坐标提取功能
    """
    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("程序文件运动坐标提取工具")
    print("=" * 60)

    # 创建提取器(不连接机械臂, 仅提取坐标)
    extractor = ProgramCoordinateExtractor()

    # 提取所有文件的坐标
    print("\n正在提取坐标...")
    results = extractor.extract_all_files()

    # 生成报告
    report = extractor.generate_report(results)
    print(report)

    # 导出JSON
    output_path = extractor.export_to_json(results)
    print(f"\n坐标数据已导出到: {output_path}")

    # 如果需要进行坐标转换, 需要连接机械臂
    print("\n" + "=" * 60)
    print("提示: 如需进行关节坐标到位姿坐标的转换, 请连接机械臂后运行")
    print("示例代码:")
    print("  from driver.arm_driver import ArmDriver")
    print("  arm = ArmDriver()")
    print("  arm.connect()")
    print("  extractor = ProgramCoordinateExtractor(arm_driver=arm)")
    print("  results = extractor.extract_all_files()")
    print("=" * 60)


def main_with_arm():
    """
    功能:
        连接机械臂并进行坐标转换的主函数
    """
    import sys
    # 添加项目根目录到路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from driver.arm_driver import ArmDriver

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("程序文件运动坐标提取工具 (带坐标转换)")
    print("=" * 60)

    # 初始化机械臂
    arm = ArmDriver()

    # 连接机械臂
    print("\n正在连接机械臂...")
    if not arm.connect():
        print("连接失败, 将仅提取坐标不进行转换")
        arm = None

    # 创建提取器
    extractor = ProgramCoordinateExtractor(arm_driver=arm)

    # 提取所有文件的坐标
    print("\n正在提取并转换坐标...")
    results = extractor.extract_all_files()

    # 生成报告
    report = extractor.generate_report(results)
    print(report)

    # 导出JSON
    output_path = extractor.export_to_json(results)
    print(f"\n坐标数据已导出到: {output_path}")

    # 断开连接
    if arm is not None:
        arm.disconnect()


if __name__ == "__main__":
    main_with_arm()
