"""对象序列化工具：JSON 类型编码、YAML dumper 与动作结果序列化。"""

import json
from collections import OrderedDict
from typing import Optional

import yaml

from unilabos.registry.action_policy import SUCCESS_TYPE_NORMAL, SuccessType


def json_default(obj):
    """将 type 对象序列化为类名，其余 fallback 到 str()。"""
    if isinstance(obj, type):
        return str(obj)[8:-2]
    return str(obj)


class TypeEncoder(json.JSONEncoder):
    """自定义JSON编码器处理特殊类型"""

    def default(self, obj):
        try:
            return json_default(obj)
        except Exception:
            return super().default(obj)


try:
    import orjson

    def normalize_json(info: dict) -> dict:
        """经 JSON 序列化/反序列化一轮来清理非标准类型。

        非字符串键（PyLabRobot 1.0 孔的 ``height_volume_data`` 是 ``{float: float}``）按 JSON 规则
        转成字符串，与下面标准库 json 的回退实现一致；PLR 反序列化时自己把键还原成 float。
        """
        return orjson.loads(orjson.dumps(info, default=json_default, option=orjson.OPT_NON_STR_KEYS))

except ImportError:

    def normalize_json(info: dict) -> dict:  # type: ignore[misc]
        return json.loads(json.dumps(info, ensure_ascii=False, cls=TypeEncoder))


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def represent_ordereddict(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())


NoAliasDumper.add_representer(OrderedDict, represent_ordereddict)


def _material_reference(obj) -> Optional[dict]:
    """动作结果里的 PLR 物料实例按物料引用 ``{"uuid"}`` 返回（与 ResourceSlot 的结果形状一致）。

    驱动直接返回 PLR 实例时不必自己转换；没有权威 uuid 的 PLR 对象不在此处理。
    """

    uuid = getattr(obj, "unilabos_uuid", None)
    if not uuid:
        return None
    try:
        from pylabrobot.resources import Resource
    except ImportError:  # pragma: no cover - 核心总带 PLR
        return None
    return {"uuid": str(uuid)} if isinstance(obj, Resource) else None


class ResultInfoEncoder(json.JSONEncoder):
    """专门用于处理任务执行结果信息的JSON编码器"""

    def default(self, obj):
        if isinstance(obj, type):
            return json_default(obj)
        reference = _material_reference(obj)
        if reference is not None:
            return reference

        try:
            if hasattr(obj, "__dict__"):
                return obj.__dict__
            elif hasattr(obj, "_asdict"):  # namedtuple
                return obj._asdict()
            elif hasattr(obj, "to_dict"):
                return obj.to_dict()
            elif hasattr(obj, "dict"):
                return obj.dict()
            else:
                return str(obj)
        except Exception:
            return str(obj)


def serialize_result_info(
    error: str,
    suc: bool,
    return_value=None,
    suc_type: Optional[SuccessType] = None,
    error_info: Optional[dict] = None,
) -> dict:
    """
    序列化任务执行结果信息

    Args:
        error: 错误信息字符串
        suc: 是否成功的布尔值
        return_value: 返回值，可以是任何类型

    Returns:
        可直接用于 HTTP/WebSocket，或在 ROS 字符串字段边界编码的结果字典
    """
    result_info = {"error": error, "suc": suc, "return_value": return_value}
    if suc:
        result_info["suc_type"] = suc_type or SUCCESS_TYPE_NORMAL
    else:
        if suc_type is not None:
            result_info["suc_type"] = suc_type
        if error_info:
            result_info["error_info"] = error_info

    return json.loads(json.dumps(result_info, ensure_ascii=False, cls=ResultInfoEncoder))


__all__ = [
    "NoAliasDumper",
    "ResultInfoEncoder",
    "TypeEncoder",
    "json_default",
    "normalize_json",
    "serialize_result_info",
]
