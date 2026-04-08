"""
资源实例兼容层

提供 ensure_resource_instance() 将 dict / ResourceDictInstance 统一转为
ResourceDictInstance，使编译器可以渐进式迁移到强类型资源。
"""

from typing import Any, Dict, Optional, Union

from unilabos.resources.resource_tracker import ResourceDictInstance


def ensure_resource_instance(
    resource: Union[Dict[str, Any], ResourceDictInstance, None],
) -> Optional[ResourceDictInstance]:
    """将 dict 或 ResourceDictInstance 统一转为 ResourceDictInstance

    编译器入口统一调用此函数，即可同时兼容旧 dict 传参和新 ResourceDictInstance 传参。

    Args:
        resource: 资源数据，可以是 plain dict、ResourceDictInstance 或 None

    Returns:
        ResourceDictInstance 或 None（当输入为 None 时）
    """
    if resource is None:
        return None
    if isinstance(resource, ResourceDictInstance):
        return resource
    if isinstance(resource, dict):
        return ResourceDictInstance.get_resource_instance_from_dict(resource)
    raise TypeError(f"不支持的资源类型: {type(resource)}, 期望 dict 或 ResourceDictInstance")


def resource_to_dict(resource: Union[Dict[str, Any], ResourceDictInstance]) -> Dict[str, Any]:
    """将 ResourceDictInstance 或 dict 统一转为 plain dict

    用于需要 dict 操作的场景（如 children dict 操作）。

    Args:
        resource: ResourceDictInstance 或 dict

    Returns:
        plain dict
    """
    if isinstance(resource, dict):
        return resource
    if isinstance(resource, ResourceDictInstance):
        return resource.get_plr_nested_dict()
    raise TypeError(f"不支持的资源类型: {type(resource)}")


def get_resource_id(resource: Union[str, Dict[str, Any], ResourceDictInstance]) -> str:
    """从资源对象中提取 ID

    Args:
        resource: 字符串 ID、dict 或 ResourceDictInstance

    Returns:
        资源 ID 字符串
    """
    if isinstance(resource, str):
        return resource
    if isinstance(resource, ResourceDictInstance):
        return resource.res_content.id
    if isinstance(resource, dict):
        if "id" in resource:
            return resource["id"]
        # 兼容 {station_id: {...}} 格式
        first_val = next(iter(resource.values()), {})
        if isinstance(first_val, dict):
            return first_val.get("id", "")
        return ""
    raise TypeError(f"不支持的资源类型: {type(resource)}")


def get_resource_data(resource: Union[str, Dict[str, Any], ResourceDictInstance]) -> Dict[str, Any]:
    """从资源对象中提取 data 字段

    Args:
        resource: 字符串、dict 或 ResourceDictInstance

    Returns:
        data 字典
    """
    if isinstance(resource, str):
        return {}
    if isinstance(resource, ResourceDictInstance):
        return dict(resource.res_content.data)
    if isinstance(resource, dict):
        return resource.get("data", {})
    return {}


def get_resource_display_info(resource: Union[str, Dict[str, Any], ResourceDictInstance]) -> str:
    """获取资源的显示信息（用于日志）

    Args:
        resource: 字符串 ID、dict 或 ResourceDictInstance

    Returns:
        显示信息字符串
    """
    if isinstance(resource, str):
        return resource
    if isinstance(resource, ResourceDictInstance):
        res = resource.res_content
        return f"{res.id} ({res.name})" if res.name and res.name != res.id else res.id
    if isinstance(resource, dict):
        res_id = resource.get("id", "unknown")
        res_name = resource.get("name", "")
        if res_name and res_name != res_id:
            return f"{res_id} ({res_name})"
        return res_id
    return str(resource)


def get_resource_liquid_volume(resource: Union[Dict[str, Any], ResourceDictInstance]) -> float:
    """从资源中获取液体体积

    Args:
        resource: dict 或 ResourceDictInstance

    Returns:
        液体总体积 (mL)
    """
    data = get_resource_data(resource)
    liquids = data.get("liquid", [])
    if isinstance(liquids, list):
        return sum(l.get("volume", 0.0) for l in liquids if isinstance(l, dict))
    return 0.0


def update_vessel_volume(vessel, G, new_volume: float, description: str = "") -> None:
    """
    更新容器体积（同时更新vessel字典和图节点）

    Args:
        vessel: 容器字典或 ResourceDictInstance
        G: 网络图 (nx.DiGraph)
        new_volume: 新体积 (mL)
        description: 更新描述（用于日志）
    """
    import logging
    logger = logging.getLogger("unilabos.compile")

    vessel_id = get_resource_id(vessel)

    if description:
        logger.info(f"[RESOURCE] 更新容器体积 - {description}")

    # 更新 vessel 字典中的体积
    if isinstance(vessel, dict):
        if "data" not in vessel:
            vessel["data"] = {}
        lv = vessel["data"].get("liquid_volume")
        if isinstance(lv, list) and len(lv) > 0:
            vessel["data"]["liquid_volume"][0] = new_volume
        else:
            vessel["data"]["liquid_volume"] = new_volume

    # 同时更新图中的容器数据
    if vessel_id and vessel_id in G.nodes():
        if "data" not in G.nodes[vessel_id]:
            G.nodes[vessel_id]["data"] = {}
        node_lv = G.nodes[vessel_id]["data"].get("liquid_volume")
        if isinstance(node_lv, list) and len(node_lv) > 0:
            G.nodes[vessel_id]["data"]["liquid_volume"][0] = new_volume
        else:
            G.nodes[vessel_id]["data"]["liquid_volume"] = new_volume

    logger.info(f"[RESOURCE] 容器 '{vessel_id}' 体积已更新为: {new_volume:.2f}mL")
