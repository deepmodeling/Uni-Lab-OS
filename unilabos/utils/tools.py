import json

from unilabos.utils.type_check import TypeEncoder, json_default

try:
    import orjson

    def fast_dumps(obj, **kwargs) -> bytes:
        """JSON 序列化为 bytes，优先使用 orjson。"""
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS, default=json_default)

    def fast_dumps_pretty(obj, **kwargs) -> bytes:
        """JSON 序列化为 bytes（带缩进），优先使用 orjson。"""
        return orjson.dumps(
            obj,
            option=orjson.OPT_NON_STR_KEYS | orjson.OPT_INDENT_2,
            default=json_default,
        )

    def fast_loads(data) -> dict:
        """JSON 反序列化，优先使用 orjson。接受 str / bytes。"""
        return orjson.loads(data)

    def fast_dumps_str(obj, **kwargs) -> str:
        """JSON 序列化为 str，优先使用 orjson。用于需要 str 而非 bytes 的场景（如 ROS msg）。"""
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS, default=json_default).decode("utf-8")

    def normalize_json(info: dict) -> dict:
        """经 JSON 序列化/反序列化一轮来清理非标准类型。"""
        return orjson.loads(orjson.dumps(info, default=json_default))

except ImportError:

    def fast_dumps(obj, **kwargs) -> bytes:  # type: ignore[misc]
        return json.dumps(obj, ensure_ascii=False, cls=TypeEncoder).encode("utf-8")

    def fast_dumps_pretty(obj, **kwargs) -> bytes:  # type: ignore[misc]
        return json.dumps(obj, indent=2, ensure_ascii=False, cls=TypeEncoder).encode("utf-8")

    def fast_loads(data) -> dict:  # type: ignore[misc]
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)

    def fast_dumps_str(obj, **kwargs) -> str:  # type: ignore[misc]
        return json.dumps(obj, ensure_ascii=False, cls=TypeEncoder)

    def normalize_json(info: dict) -> dict:  # type: ignore[misc]
        return json.loads(json.dumps(info, ensure_ascii=False, cls=TypeEncoder))


# 辅助函数：将UUID数组转换为字符串
def uuid_to_str(uuid_array) -> str:
    """将UUID字节数组转换为十六进制字符串"""
    return "".join(format(byte, "02x") for byte in uuid_array)
