# 🆕 创建进度日志动作
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def debug_print(message, prefix="[UNIT_PARSER]"):
    """调试输出"""
    logger.info(f"{prefix} {message}")


def action_log(message: str, emoji: str = "📝", prefix="[HIGH-LEVEL OPERATION]") -> Dict[str, Any]:
    """创建一个动作日志 - 支持中文和emoji"""
    try:
        full_message = f"{prefix} {emoji} {message}"

        return {
            "action_name": "wait",
            "action_kwargs": {
                "time": 0.1,
                "log_message": full_message,
                "progress_message": full_message
            }
        }
    except Exception as e:
        # 如果emoji有问题，使用纯文本
        safe_message = f"{prefix} {message}"

        return {
            "action_name": "wait",
            "action_kwargs": {
                "time": 0.1,
                "log_message": safe_message,
                "progress_message": safe_message
            }
        }
