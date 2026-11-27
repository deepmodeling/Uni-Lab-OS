from typing import Any, Dict, List, Union

from pylabrobot.resources import Resource


class ResourceSlot(Resource):
    """物料槽位类型——分两层语义，使用时务必区分：

    1. 作为 @action 函数参数类型（**暴露给外部调用的契约**）：表示「**一个完整的物料**」。
       框架已在 send_goal 把原始入参解析为单个 PLR ``Resource`` 实例，函数体内拿到的就是它，
       直接当普通 Resource 用即可，无需关心 list/dict。
    2. 作为原始入参（**仅框架内部解析阶段**）：见 ``ResourceSlotRawInput``，可能是
       list（一棵树的扁平节点组，handle ``@flatten`` 运行期形态）或 dict（资源引用，前端 schema 填写形态）。

    pydantic schema 固定为 object(dict)——前端只按「单物料 dict」展示/填写；list 仅出现在 handle
    连线的运行期，不进入 schema。
    """

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        # pydantic 无法内省 pylabrobot Resource 子类，会导致包含 ResourceSlot 的
        # TypedDict 整体回退为 {"type": "object"}。这里显式声明为对象 schema。
        from pydantic_core import core_schema

        return core_schema.dict_schema()


# 单 ResourceSlot 的「原始入参形态」——**仅框架内部解析阶段**使用，用于标注解析前的 raw 值：
#   - dict：资源引用 {id, uuid}（前端 schema 填写形态，object）→ 按 uuid with_children 拉取；
#   - list：一棵树的扁平节点组（handle @flatten 运行期形态）→ 装配成一个物料（须恰好单根）。
# 解析完成后，@action 函数签名拿到的是「一个完整的 Resource 实例」（见 ResourceSlot 文档第 1 条）。
ResourceSlotRawInput = Union[List[Dict[str, Any]], Dict[str, Any]]


class DeviceSlot(str):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        # DeviceSlot 本质是设备 id 字符串；pydantic 不会把 str 子类当 str 处理，
        # 不声明就会让包含它的 TypedDict 解析失败并回退为 {"type": "object"}。
        from pydantic_core import core_schema

        return core_schema.str_schema()


# ---------------------------------------------------------------------------
# placeholder_keys 常量
# ---------------------------------------------------------------------------
# 这些常量标注「动作参数在前端应以何种选择器填入」。与 ResourceSlot/DeviceSlot 同源：
# ResourceSlot 让框架把传入的 uuid 解析成实例（参数类型层面），而 placeholder_keys
# 常量告诉前端这个字段该用哪种选择器（界面/数据来源层面）。
PLACEHOLDER_RESOURCES = "unilabos_resources"
PLACEHOLDER_DEVICES = "unilabos_devices"
PLACEHOLDER_NODES = "unilabos_nodes"
PLACEHOLDER_CLASS = "unilabos_class"
PLACEHOLDER_MANUAL_CONFIRM = "unilabos_manual_confirm"
# 物料扣减：前端选择资源注册表类型 + 数量，由服务端扣减后回传实例的 uuid。
PLACEHOLDER_DEDUCT_RESOURCE = "unilabos_deduct_resource"
# 试剂扣减：set_substance 设置物料内容物（液体/固体）前选择试剂，由服务端扣减。
PLACEHOLDER_DEDUCT_REAGENT = "unilabos_deduct_reagent"
