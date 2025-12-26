import collections.abc
import json
from collections import OrderedDict
from typing import get_origin, get_args

import yaml


def get_type_class(type_hint):
    origin = get_origin(type_hint)
    if origin is not None and issubclass(origin, collections.abc.Sequence):
        final_type = [get_args(type_hint)[0]]  # 默认sequence中类型都一样
    else:
        final_type = type_hint
    return final_type


class TypeEncoder(json.JSONEncoder):
    """自定义JSON编码器处理特殊类型"""

    def default(self, obj):
        # 优先处理类型对象
        if isinstance(obj, type):
            return str(obj)[8:-2]
        return super().default(obj)


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


# 为NoAliasDumper添加OrderedDict的representation方法
def represent_ordereddict(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())


# 注册OrderedDict的representer
NoAliasDumper.add_representer(OrderedDict, represent_ordereddict)


class ResultInfoEncoder(json.JSONEncoder):
    """专门用于处理任务执行结果信息的JSON编码器"""

    def default(self, obj):
        # 优先处理类型对象
        if isinstance(obj, type):
            return str(obj)[8:-2]

        # 对于无法序列化的对象，统一转换为字符串
        try:
            # 尝试调用 __dict__ 或者其他序列化方法
            if hasattr(obj, "__dict__"):
                return obj.__dict__
            elif hasattr(obj, "_asdict"):  # namedtuple
                return obj._asdict()
            elif hasattr(obj, "to_dict"):
                return obj.to_dict()
            elif hasattr(obj, "dict"):
                return obj.dict()
            else:
                # 如果都不行，转换为字符串
                return str(obj)
        except Exception:
            # 如果转换失败，直接返回字符串表示
            return str(obj)


def get_result_info_str(error: str, suc: bool, return_value=None) -> str:
    """
    序列化任务执行结果信息

    Args:
        error: 错误信息字符串
        suc: 是否成功的布尔值
        return_value: 返回值，可以是任何类型

    Returns:
        JSON字符串格式的结果信息
    """
    samples = None
    if isinstance(return_value, dict):
        if "samples" in return_value:
            samples = return_value.pop("samples")
    result_info = {"error": error, "suc": suc, "return_value": return_value, "samples": samples}

    return json.dumps(result_info, ensure_ascii=False, cls=ResultInfoEncoder)



def serialize_result_info(error: str, suc: bool, return_value=None) -> dict:
    """
    序列化任务执行结果信息

    Args:
        error: 错误信息字符串
        suc: 是否成功的布尔值
        return_value: 返回值，可以是任何类型

    Returns:
        JSON字符串格式的结果信息
    """
    result_info = {"error": error, "suc": suc, "return_value": return_value}

    return json.loads(json.dumps(result_info, ensure_ascii=False, cls=ResultInfoEncoder))
