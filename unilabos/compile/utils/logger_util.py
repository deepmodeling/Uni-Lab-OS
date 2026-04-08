"""编译器共享日志工具"""

import inspect
import logging
from typing import Dict, Any

# 模块名到前缀的映射
_MODULE_PREFIXES = {
    "add_protocol": "[ADD]",
    "adjustph_protocol": "[ADJUSTPH]",
    "clean_vessel_protocol": "[CLEAN_VESSEL]",
    "dissolve_protocol": "[DISSOLVE]",
    "dry_protocol": "[DRY]",
    "evacuateandrefill_protocol": "[EVACUATE]",
    "evaporate_protocol": "[EVAPORATE]",
    "filter_protocol": "[FILTER]",
    "heatchill_protocol": "[HEATCHILL]",
    "hydrogenate_protocol": "[HYDROGENATE]",
    "pump_protocol": "[PUMP]",
    "recrystallize_protocol": "[RECRYSTALLIZE]",
    "reset_handling_protocol": "[RESET]",
    "run_column_protocol": "[RUN_COLUMN]",
    "separate_protocol": "[SEPARATE]",
    "stir_protocol": "[STIR]",
    "wash_solid_protocol": "[WASH_SOLID]",
    "vessel_parser": "[VESSEL_PARSER]",
    "unit_parser": "[UNIT_PARSER]",
    "resource_helper": "[RESOURCE_HELPER]",
}


def debug_print(message, prefix=None):
    """调试输出 — 自动根据调用模块设置前缀"""
    if prefix is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame else None
        module_name = ""
        if caller:
            module_name = caller.f_globals.get("__name__", "")
            # 取最后一段作为模块短名
            module_name = module_name.rsplit(".", 1)[-1]
        prefix = _MODULE_PREFIXES.get(module_name, f"[{module_name.upper()}]")
    logger = logging.getLogger("unilabos.compile")
    logger.info(f"{prefix} {message}")


def action_log(message: str, emoji: str = "📝", prefix="[HIGH-LEVEL OPERATION]") -> Dict[str, Any]:
    """创建一个动作日志"""
    full_message = f"{prefix} {emoji} {message}"
    return {
        "action_name": "wait",
        "action_kwargs": {
            "time": 0.1,
            "log_message": full_message,
            "progress_message": full_message
        }
    }
