"""输出格式化

支持两种输出格式：
- HUMAN: 人类可读的格式（表格、彩色文本）
- JSON: 机器可读的 JSON 格式（供 AI agent 使用）
"""

import json
import sys
from enum import Enum
from typing import Any, Dict, List, Optional


class OutputFormat(Enum):
    """输出格式"""
    HUMAN = "human"
    JSON = "json"


class OutputFormatter:
    """输出格式化器"""

    def __init__(self, format: OutputFormat = OutputFormat.HUMAN):
        self.format = format

    def format_output(self, data: Any, message: Optional[str] = None) -> str:
        """格式化输出数据

        Args:
            data: 要输出的数据
            message: 可选的消息

        Returns:
            格式化后的字符串
        """
        if self.format == OutputFormat.JSON:
            return self._format_json(data, message)
        else:
            return self._format_human(data, message)

    def _format_json(self, data: Any, message: Optional[str] = None) -> str:
        """JSON 格式"""
        output = {"data": data}
        if message:
            output["message"] = message
        return json.dumps(output, ensure_ascii=False, indent=2)

    def _format_human(self, data: Any, message: Optional[str] = None) -> str:
        """人类可读格式"""
        lines = []

        if message:
            lines.append(message)
            lines.append("")

        if isinstance(data, dict):
            lines.extend(self._format_dict(data))
        elif isinstance(data, list):
            lines.extend(self._format_list(data))
        else:
            lines.append(str(data))

        return "\n".join(lines)

    def _format_dict(self, data: Dict[str, Any]) -> List[str]:
        """格式化字典"""
        lines = []
        max_key_len = max(len(str(k)) for k in data.keys()) if data else 0

        for key, value in data.items():
            key_str = str(key).ljust(max_key_len)
            if isinstance(value, (dict, list)):
                value_str = json.dumps(value, ensure_ascii=False)
            else:
                value_str = str(value)
            lines.append(f"{key_str} : {value_str}")

        return lines

    def _format_list(self, data: List[Any]) -> List[str]:
        """格式化列表"""
        if not data:
            return ["(空列表)"]

        # 如果是字典列表，尝试表格格式
        if all(isinstance(item, dict) for item in data):
            return self._format_table(data)

        # 否则逐行输出
        lines = []
        for i, item in enumerate(data, 1):
            if isinstance(item, dict):
                lines.append(f"[{i}]")
                lines.extend(f"  {line}" for line in self._format_dict(item))
            else:
                lines.append(f"[{i}] {item}")

        return lines

    def _format_table(self, data: List[Dict[str, Any]]) -> List[str]:
        """格式化表格"""
        if not data:
            return []

        # 获取所有列
        columns = list(data[0].keys())

        # 计算列宽
        col_widths = {}
        for col in columns:
            col_widths[col] = len(col)
            for row in data:
                value = str(row.get(col, ""))
                col_widths[col] = max(col_widths[col], len(value))

        # 生成表格
        lines = []

        # 表头
        header = " | ".join(col.ljust(col_widths[col]) for col in columns)
        lines.append(header)
        lines.append("-" * len(header))

        # 数据行
        for row in data:
            line = " | ".join(
                str(row.get(col, "")).ljust(col_widths[col])
                for col in columns
            )
            lines.append(line)

        return lines


# 全局格式化器
_formatter = OutputFormatter()


def set_output_format(format: OutputFormat):
    """设置全局输出格式"""
    global _formatter
    _formatter = OutputFormatter(format)


def get_formatter() -> OutputFormatter:
    """获取全局格式化器"""
    return _formatter


def print_output(data: Any, message: Optional[str] = None):
    """打印输出数据"""
    output = _formatter.format_output(data, message)
    print(output)


def print_success(message: str):
    """打印成功消息"""
    if _formatter.format == OutputFormat.JSON:
        print(json.dumps({"status": "success", "message": message}, ensure_ascii=False))
    else:
        print(f"✓ {message}")


def print_error(message: str):
    """打印错误消息"""
    if _formatter.format == OutputFormat.JSON:
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"✗ {message}", file=sys.stderr)


def print_warning(message: str):
    """打印警告消息"""
    if _formatter.format == OutputFormat.JSON:
        print(json.dumps({"status": "warning", "message": message}, ensure_ascii=False))
    else:
        print(f"⚠ {message}")
