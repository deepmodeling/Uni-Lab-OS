"""
类实例创建工具

此模块提供了通过字典配置创建类实例的功能，支持嵌套实例的递归创建。
类似于Hydra和Weights & Biases的配置系统。
"""

import importlib
import traceback
from typing import Any, Dict, TypeVar
from unilabos.utils import import_manager, logger

T = TypeVar("T")

# 定义实例创建规范的关键字
INSTANCE_TYPE_KEY = "_cls"  # 类的完整路径
INSTANCE_PARAMS_KEY = "_params"  # 构造函数参数
INSTANCE_ARGS_KEY = "_args"  # 位置参数列表（可选）


def is_instance_config(config: Any) -> bool:
    """
    检查配置是否符合实例创建规范

    Args:
        config: 要检查的配置对象

    Returns:
        是否符合实例创建规范
    """
    if not isinstance(config, dict):
        return False

    return INSTANCE_TYPE_KEY in config and INSTANCE_PARAMS_KEY in config


def import_class(class_path: str) -> type:
    """
    根据类路径导入类

    Args:
        class_path: 类的完整路径，如"pylabrobot.liquid_handling:LiquidHandler"

    Returns:
        导入的类

    Raises:
        ImportError: 如果导入失败
        AttributeError: 如果找不到指定的类
    """
    try:
        return import_manager.get_class(class_path)
    except ValueError as e:
        raise ImportError(f"无法导入类 {class_path}: {str(e)}")
    except (ImportError, AttributeError) as e:
        raise ImportError(f"无法导入类 {class_path}: {str(e)}")


def create_instance_from_config(config: Dict[str, Any]) -> Any:
    """
    从配置字典创建实例，递归处理嵌套的实例配置

    Args:
        config: 配置字典，必须包含_type和_params键

    Returns:
        创建的实例

    Raises:
        ValueError: 如果配置不符合规范
        ImportError: 如果类导入失败
    """
    if not is_instance_config(config):
        raise ValueError(f"配置不符合实例创建规范: {config}")

    class_path = config[INSTANCE_TYPE_KEY]
    params = config[INSTANCE_PARAMS_KEY]
    args = config.get(INSTANCE_ARGS_KEY, [])

    # 递归处理嵌套的实例配置
    processed_args = []
    for arg in args:
        if is_instance_config(arg):
            processed_args.append(create_instance_from_config(arg))
        else:
            processed_args.append(arg)

    processed_params = {}
    for key, value in params.items():
        if is_instance_config(value):
            processed_params[key] = create_instance_from_config(value)
        elif isinstance(value, list):
            # 处理列表中的实例配置
            processed_list = []
            for item in value:
                if is_instance_config(item):
                    processed_list.append(create_instance_from_config(item))
                else:
                    processed_list.append(item)
            processed_params[key] = processed_list
        elif isinstance(value, dict) and not is_instance_config(value):
            # 处理普通字典中可能包含的实例配置
            processed_dict = {}
            for k, v in value.items():
                if is_instance_config(v):
                    processed_dict[k] = create_instance_from_config(v)
                else:
                    processed_dict[k] = v
            processed_params[key] = processed_dict
        else:
            processed_params[key] = value

    # 导入类并创建实例
    cls = import_class(class_path)
    return cls(*processed_args, **processed_params)


