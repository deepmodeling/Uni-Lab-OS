"""Host 物料编排四动作的共享实现（backend 无关）。

「host_node」是一个面向前端画布与工作流的固定物料 API：出库
（apply_deduct_resource）、设置物质（set_substance）、丢弃
（discard_resource）、移动（transfer_resource）。人工确认
（manual_confirm）是系统自带的通用动作，不属于物料 API。

两种 backend 都只是本模块的薄壳：ROS2 HostNode 的 ``@action`` 提供
schema / 画布 handle 与 ROS action 入口，HostLink 内置 host 服务设备提供
HostLink 入口；业务全部收敛在这里、且全部走 ``materials.*`` 门面。

调用方注入两样东西：

- ``node``：host 自身的 DeviceNode——权威读写入口（update_resource）、仅登记
  出库物料的 tracker 落点，也是设备归属零通信推断的首选起点；
- ``dispatch``：async 下行通道 ``(device_id, action_type, payload) -> dict``，
  两端语义一致——本进程设备直调实例协程，跨机（Slave）经 HostLink RPC。

设备归属推断本地优先：先扫本进程各设备节点的 tracker（零通信），未命中才退
回 ``materials.owner_device_of``——后者沿权威 parent 链逐级查询，微后端在远端
（HTTP / HostLink）时每跳都是一次 RPC，能省则省。

设备身份在 API 参数中统一用 device id（全局唯一，不允许重复），不再另传
device uuid；mutation 归因 actor_uuid 由服务层落到 device id。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Sequence

from unilabos.backend.hostlink.protocol import ActionType
from unilabos.backend.runtime.async_utils import run_blocking as _run_blocking
from unilabos.protocol.materials import ACTOR_WORKFLOW
from unilabos.resources import materials
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.log import logger

#: 下行通道：(device_id, action_type, payload) -> 结果 dict。
Dispatch = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

#: host 物料 API 的固定动作集（画布 / 工作流以此为准）。
HOST_MATERIAL_ACTIONS = (
    "apply_deduct_resource",
    "set_substance",
    "discard_resource",
    "transfer_resource",
)


def _normalize_point(value: Any) -> Dict[str, float]:
    """把挂载坐标归一成 dict：接受 dict / Point 消息 / None。"""

    if value is None:
        return {"x": 0.0, "y": 0.0, "z": 0.0}
    if isinstance(value, dict):
        return {
            "x": float(value.get("x", 0.0)),
            "y": float(value.get("y", 0.0)),
            "z": float(value.get("z", 0.0)),
        }
    return {
        "x": float(getattr(value, "x", 0.0)),
        "y": float(getattr(value, "y", 0.0)),
        "z": float(getattr(value, "z", 0.0)),
    }


def _edge_id(device_id: Any) -> str:
    return str(device_id or "").split("/")[-1]


def _owner_device_local(node: Any, value: Any) -> str:
    """零通信推断物料归属：按 uuid 扫本进程各设备节点的 tracker。

    tracker 由挂载与转移的 unload/load 投影实时维护，uuid 命中即「物料此刻
    就在该设备台面上」；host 自身（node）优先。未命中返回空串，调用方再退回
    ``materials.owner_device_of``（权威爬根，涉及通信）。
    """

    res_uuid = str(getattr(value, "unilabos_uuid", "") or "")
    if not res_uuid:
        return ""
    from unilabos.backend.hostlink.downlink import iter_local_device_nodes

    candidates: list[tuple[str, Any]] = []
    if node is not None:
        candidates.append((str(getattr(node, "device_id", "") or ""), node))
    candidates.extend(iter_local_device_nodes())
    for device_id, device_node in candidates:
        if not device_id:
            continue
        tracker = getattr(device_node, "resource_tracker", None)
        uuid_map = getattr(tracker, "uuid_to_resources", None)
        if uuid_map and res_uuid in uuid_map:
            return _edge_id(device_id)
    return ""


async def deduct_resource(
    node: Any,
    dispatch: Dispatch,
    resource: Any = None,
    registry_class: str = "",
    material_name: str = "",
    device_id: Any = "",
    mount_resource: Any = None,
    bind_locations: Any = None,
    slot_on_deck: str = "",
) -> Dict[str, Any]:
    """出库物料：物料进入系统的统一入口——创建/落权威，可选下行挂载。

    出库产物两种来源（二选一）：

    - ``resource``：已带 uuid 的扣减产物（云端仓储扣减 / 前端
      ``POST /api/v1/inventory/commands``（``instantiate_material``）出库命令的产物引用）——经
      ``materials.ensure`` 落权威：权威缺失时以原 uuid 显式创建（adopt），
      已存在则直接采用权威记录；
    - ``registry_class``（+ ``material_name``）：按 registry 资源类名现场
      创建全新物料——``materials.create`` 权威发号，与 instantiate 端点
      同款语义（动作路径不需要前端先调端点）。

    之后：

    - 仅登记/透传（缺 mount_resource）：物料树经 ``created_resource_tree``
      输出，供 set_substance / transfer_resource 接续。现场创建（registry_class）
      时带 ``node=`` 落 host 名下——权威登记归属、进 host tracker，后续
      transfer 的来源推断零通信命中；
    - 出库并挂载（给 mount_resource）：下行 ``RESOURCE_APPEND``——目标设备按
      uuid 从权威拉取实例化 → 物理 assign → 快照回权威。归属随挂载登记到
      目标设备，host 侧不留簿记。目标设备缺省自动推断（本进程 tracker 零通信
      优先，owner_device_of 权威爬根兜底）；``device_id`` 仅作显式覆盖。
    """

    # 先解析挂载目标：决定出库产物落点（仅登记在 host 名下 / 直接挂到设备）
    mount = (
        mount_resource
        if isinstance(mount_resource, str)
        else await _run_blocking(materials.resolve, mount_resource)
    )
    mounting = not (mount is None or (isinstance(mount, str) and not mount))

    if registry_class:
        if resource is not None and not (
            isinstance(resource, str) and not resource.strip()
        ):
            raise ValueError("resource 与 registry_class 只能二选一")
        # 仅登记路径带 node：归属登记为 host 并进 host tracker；挂载路径不带
        # ——物料直接落目标设备，host 不留下永不 unload 的 tracker 条目。
        plr = await _run_blocking(
            materials.create,
            str(registry_class),
            name=str(material_name),
            node=None if mounting else node,
            actor_type=ACTOR_WORKFLOW,
            actor_uuid=_edge_id(getattr(node, "device_id", "")) or None,
        )
        # create 已在权威落库（发号），无需再 ensure
        deduct_tree_set = ResourceTreeSet.from_plr_resources([plr])
    else:
        plr = await _run_blocking(materials.resolve, resource)
        if plr is None:
            raise ValueError(
                "申请扣减失败：未接收到已扣减物料（带 uuid 的 resource 或"
                " registry_class 现场创建，二选一）"
            )
        if getattr(plr, "unilabos_uuid", None) is None:
            raise ValueError(
                f"物料 {getattr(plr, 'name', plr)} 缺少 unilabos_uuid，无法处理"
            )
        # 出库扣减走 materials 协议：权威缺失时以原 uuid 显式创建（adopt），
        # 已存在则直接采用权威记录；之后设备侧按 uuid 拉取必然命中。
        deduct_tree_set = ResourceTreeSet.from_plr_resources([plr])
        await _run_blocking(
            materials.ensure,
            deduct_tree_set,
            actor_type=ACTOR_WORKFLOW,
            actor_uuid=_edge_id(getattr(node, "device_id", "")) or None,
        )
    dumped = deduct_tree_set.dump()
    if not dumped:
        raise ValueError(f"物料 {getattr(plr, 'name', plr)} 序列化为空")

    if not mounting:
        logger.info(
            f"[HostMaterials] 仅登记/透传物料 name={getattr(plr, 'name', '')}"
            "（未指定 mount_resource，不挂载）"
        )
        return {
            "created_resource_tree": dumped,
            "substance_resource_tree": [],
            "mount_resource": [],
        }
    target_id = _edge_id(device_id)
    if not target_id:
        # 目标设备自动推断：挂载目标物料属于哪台设备，就下发给哪台
        if isinstance(mount, str):
            raise ValueError(
                "mount_resource 为名字字符串时无法推断目标设备，请显式传 device_id"
            )
        target_id = _owner_device_local(node, mount) or await _run_blocking(
            materials.owner_device_of, mount
        )
    mount_name = getattr(mount, "name", None) or _edge_id(mount)
    logger.info(
        f"[HostMaterials] 挂载物料 name={getattr(plr, 'name', '')} "
        f"-> device={target_id} mount_resource={mount_name}"
    )
    payload: Dict[str, Any] = {
        "resource_uuid": [getattr(plr, "unilabos_uuid")],
        "bind_parent_id": str(mount_name),
        "bind_location": _normalize_point(bind_locations),
    }
    if slot_on_deck:
        payload["other_calling_param"] = {"slot": str(slot_on_deck)}
    result = await dispatch(target_id, ActionType.RESOURCE_APPEND, payload)
    result["mount_resource"] = (
        ResourceTreeSet.from_plr_resources([mount]).dump()
        if not isinstance(mount, str)
        else []
    )
    return result


async def set_substance(
    node: Any,
    resource: Any,
    substance_names: Sequence[str],
    amounts: Sequence[float],
    slots: Sequence[str] = (),
    is_solid: Sequence[bool] = (),
) -> Dict[str, Any]:
    """设置物料物质：写内容物（液体 ul / 固体 ug）并把整棵树同步回权威。"""

    plr = await _run_blocking(materials.resolve, resource)
    if plr is None:
        raise ValueError("设置内容物失败：未接收到物料")
    materials.apply_substances(
        plr,
        list(substance_names),
        list(amounts),
        slots=list(slots),
        is_solid=list(is_solid),
    )
    await node.update_resource(plr)
    dumped = ResourceTreeSet.from_plr_resources([plr]).dump()
    return {"resource": dumped[0] if dumped else []}


async def discard_resource(
    node: Any,
    dispatch: Dispatch,
    resource: Any,
    device_id: Any = "",
) -> Dict[str, Any]:
    """丢弃物料：权威销毁，成功后通知所属设备本地移除。

    所属设备缺省自动推断（本进程 tracker 零通信优先，owner_device_of 权威
    爬根兜底）；显式传 device_id 可覆盖。推断必须发生在权威销毁之前。
    通知失败不回滚——权威已销毁，边缘侧下次同步对齐。
    """

    plr = await _run_blocking(materials.resolve, resource)
    if plr is None:
        raise ValueError("废弃失败：未接收到物料")
    res_uuid = getattr(plr, "unilabos_uuid", None)
    if res_uuid is None:
        raise ValueError(
            f"物料 {getattr(plr, 'name', plr)} 缺少 unilabos_uuid，无法废弃"
        )
    edge_id = _edge_id(device_id) or _owner_device_local(node, plr)
    if not edge_id:
        edge_id = await _run_blocking(materials.owner_device_of, plr)
    logger.info(
        f"[HostMaterials] 废弃物料 name={getattr(plr, 'name', '')} "
        f"uuid={res_uuid} device={edge_id}"
    )
    deleted = await _run_blocking(
        materials.remove,
        str(res_uuid),
        source_device_id=edge_id,
    )
    if res_uuid not in deleted:
        raise ValueError(f"微后端未确认物料废弃：{res_uuid}")
    try:
        await dispatch(
            edge_id,
            ActionType.RESOURCE_TREE_SYNC,
            {"operations": [{"action": "remove", "data": [res_uuid]}]},
        )
    except Exception as exc:  # noqa: BLE001 - 权威已销毁，通知失败仅告警
        logger.warning(
            f"[HostMaterials] 权威已销毁 uuid={res_uuid}，但通知设备 "
            f"{edge_id} 本地移除失败：{exc}"
        )
    return {"code": 0, "uuids": [res_uuid], "device_id": edge_id}


async def transfer_resource(
    node: Any,
    resource: Any,
    mount_resource: Any,
    site: str = "",
    target_device: Any = "",
) -> Dict[str, Any]:
    """移动物料：把已物理就位的物料在系统中改挂到目标物料的孔位。

    只需给物料与目标物料，两端设备都自动推断（本进程 tracker 零通信优先，
    materials.owner_device_of 权威爬根兜底）：

    - 来源设备 = 被转移物料当前所在根树的归属——unload 通知发给真实持有者，
      而不是 host 自己（仅登记在 host 名下的出库物料，来源即 host）；
    - 目标设备 = 挂载目标物料所在根树的归属；``target_device`` 仅作显式覆盖。

    换位与跨设备同一语义（materials.transfer）：微后端先提交权威位置，再按
    来源 unload → 目标 load 通知设备投影，物理搬运由前序节点保证
    （manual_confirm 人工闸门 / 机械臂 pick+place）。

    site：目标父级上的 Site 选择器——前端提交权威 ResourceSite 的 uuid，
    兼容 label / 数字索引；空串表示由父级默认排布。
    """

    plr = await _run_blocking(materials.resolve, resource)
    mount = await _run_blocking(materials.resolve, mount_resource)
    if plr is None:
        raise ValueError("转移失败：未接收到待转移物料")
    if mount is None:
        raise ValueError("转移失败：未指定挂载目标孔位")
    source_id = _owner_device_local(node, plr) or await _run_blocking(
        materials.owner_device_of, plr
    )
    target_id = _edge_id(target_device) or _owner_device_local(node, mount)
    if not target_id:
        target_id = await _run_blocking(materials.owner_device_of, mount)
    result = await materials.transfer(
        [plr],
        target_id,
        [mount],
        [site if site else None],
        source_device_id=source_id,
    )
    return {
        "resource": ResourceTreeSet.from_plr_resources([plr]).dump(),
        "mount_resource": ResourceTreeSet.from_plr_resources([mount]).dump(),
        "site": site,
        "result": result,
    }


__all__ = [
    "Dispatch",
    "HOST_MATERIAL_ACTIONS",
    "deduct_resource",
    "discard_resource",
    "set_substance",
    "transfer_resource",
]
