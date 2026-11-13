"""host_node 服务设备的 transport 中立动作定义。

``host_node`` 在系统中是一个「服务设备」：物料编排四动作
（apply_deduct_resource / set_substance / discard_resource / transfer_resource）、
系统自带 manual_confirm，以及诊断动作 test_resource / test_latency。

本类提供 @device/@action 扫描所需的 schema、placeholder、handles 和签名，
业务实现委托给 :mod:`unilabos.backend.host_material_actions`。两种 backend
通过各自的设备管线初始化本类：

- ROS2：``initialize_device_from_dict`` 按 registry 的 host_node 条目
  包装成普通 ROS2 设备节点，下行通道默认落
  ``hostlink.downlink.material_dispatch``；
- HostLink：``register_host_services`` 把本类作为 driver 装进
  ``HostLinkDeviceNode``，下行通道走 backend.local / server 路由。

test_latency 的 ping-pong 实现在执行适配器共享基类
（:class:`unilabos.backend.runtime.host_adapter.HostAdapterBase`），
两种 backend 均可执行，这里仅委托当前适配器。

注意：本模块不使用 ``from __future__ import annotations``——动作签名的
ResourceSlot/SiteSlot 等注解必须保持运行时类型对象，HostLink runtime 按注解
识别并解析 ResourceSlot 入参。
"""

import asyncio
from typing import Any, Dict, List

from typing_extensions import TypedDict

from unilabos.backend import host_material_actions
from unilabos.backend.hostlink.protocol import ActionType
from unilabos.backend.runtime.node import BackendCapabilityError
from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    DataSource,
    NodeType,
    action,
    device,
)
from unilabos.registry.placeholder_type import (
    PLACEHOLDER_DEDUCT_REAGENT,
    PLACEHOLDER_DEDUCT_RESOURCE,
    PLACEHOLDER_DEVICES,
    PLACEHOLDER_MANUAL_CONFIRM,
    PLACEHOLDER_NODES,
    DeviceSlot,
    ResourceSlot,
    SiteSlot,
)
from unilabos.resources.objects.pose import ResourceDictPositionObject
from unilabos.resources.objects.resource import ResourceDictType
from unilabos.resources.objects.sample import LabSample, SampleUUIDsType
from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet

#: 由 host 服务设备承载、两种 backend 语义一致的动作。
HOST_SERVICE_ACTIONS = (
    *host_material_actions.HOST_MATERIAL_ACTIONS,
    "manual_confirm",
    "test_resource",
    "test_latency",
)


class TestResourceReturn(TypedDict):
    resources: List[List[ResourceDictType]]
    devices: List[DeviceSlot]
    unilabos_samples: List[LabSample]


class DeductResourceReturn(TypedDict):
    """apply_deduct_resource 返回值：字段本身保持 dump() 完整分组形态（数据传输用完整的）。

    物料创建只发生在微后端；本返回值承载「扣减/挂载」结果。单根树经 handle data_key 的
    @flatten 拍平一层后（见 created_resource_tree.@flatten），用户/下游拿到扁平节点 list。
    消费侧（ResourceSlot / materials.from_str）对两种形态都能自动拆包。

    substance_resource_tree：已加入 substance（内容物）的物料树，统一说法——
    出库物料若已带内容物（set_substance / 权威 data.substances），在此单独承载。
    """

    created_resource_tree: List[List[ResourceDictType]]
    substance_resource_tree: List[List[ResourceDictType]]
    unilabos_samples: List[LabSample]
    mount_resource: List[List[ResourceDictType]]


class TransferResourceReturn(TypedDict):
    """transfer_resource 返回值：透传被转移物料、目标孔位与槽位，便于下游引用。

    resource / mount_resource 均为「单个物料」的扁平节点形态（list[list[ResourceDict]]，单根，
    经 @flatten 后即一棵树的扁平节点 list），与 apply_deduct 输出一致、可直接连到下游单物料输入。
    """

    resource: List[List[ResourceDictType]]
    mount_resource: List[List[ResourceDictType]]
    site: str
    result: Any


class TestLatencyReturn(TypedDict):
    """test_latency 的返回值：顶层是主链路（HTTP，退回 WS）的统计，links 给每条链路明细。"""

    avg_rtt_ms: float
    avg_time_diff_ms: float
    max_time_error_ms: float
    task_delay_ms: float
    raw_delay_ms: float
    test_count: int
    #: success | all_timeout | backend_unreachable
    status: str
    #: Edge 连接的 Backend 地址（HTTP 与控制 WS 同地址）
    backend_url: str
    #: configured（--address 指定）| default（本机 Backend 端口 HTTPConfig.backend_port）
    address_source: str
    #: 会话描述的每条链路：{"http": {transport, target, status, avg_rtt_ms, …},
    #: "control": {transport: websocket, status: success | all_timeout | not_connected, …}}
    links: Dict[str, Any]


@device(
    id="host_node",
    category=[],
    description="Host Node",
    display_name="主机服务节点",
    icon="icon_device.webp",
)
class HostServices:
    """host_node 服务设备驱动（backend 无关）。

    构造与下行通道（按优先级）：

    - ``dispatch``：显式注入的下行协程（测试/嵌入方使用）；
    - ``backend``：HostLink Host 进程的 backend——本进程设备直调实例
      协程、跨机（Slave）设备经 HostLink RPC；
    - 都未注入（ROS2 通用管线零参构造）：默认走
      ``hostlink.downlink.material_dispatch``（本进程直调 / 跨机 HostLink
      下行，语义与 HostLink 形态一致）。
    """

    def __init__(self, backend: Any = None, dispatch: Any = None, **_kwargs: Any) -> None:
        self._backend = backend
        self._dispatch_override = dispatch
        self._node: Any = None

    def post_init(self, node: Any) -> None:
        self._node = node

    # ------------------------------------------------------------------
    # 下行分发
    # ------------------------------------------------------------------

    def _dispatch_blocking(
        self, device_id: str, action_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """HostLink backend 路由：本进程设备直调协程；跨机设备走 HostLink RPC。"""
        from unilabos.backend.runtime.async_utils import run_node_coroutine

        backend = self._backend
        if backend is None:
            raise RuntimeError("HostServices 未注入 dispatch，也未绑定 HostLink backend")
        node = backend.local.get_device(device_id)
        if node is not None:
            data = {k: v for k, v in payload.items() if k != "device_id"}
            if action_type == ActionType.RESOURCE_APPEND:
                return node.append_resource_blocking(data)
            if action_type == ActionType.RESOURCE_TREE_SYNC:
                return run_node_coroutine(
                    node, node.apply_resource_tree_update(data["operations"])
                )
            raise ValueError(f"未支持的本地下行类型: {action_type}")
        server = backend.server
        if server is None or not server.has_device(device_id):
            raise ValueError(
                f"设备 {device_id!r} 不在本进程、也不在 HostLink 在线表，无法下行 {action_type}"
            )
        return server.request_device(
            str(device_id),
            action_type,
            {"device_id": str(device_id), **payload},
            timeout=30.0,
        )

    async def _dispatch(
        self, device_id: str, action_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self._dispatch_override is not None:
            return await self._dispatch_override(device_id, action_type, payload)
        if self._backend is not None:
            # 阻塞 RPC / 跨节点协程桥放到线程池，避免阻塞本设备事件循环
            return await asyncio.to_thread(
                self._dispatch_blocking, device_id, action_type, payload
            )
        # 默认通道：本进程直调 / 跨机 HostLink 下行（双 backend 通用）
        from unilabos.backend.hostlink.downlink import material_dispatch

        return await material_dispatch(device_id, action_type, payload)

    # ------------------------------------------------------------------
    # 动作定义（schema / placeholder / handles 的唯一来源）
    # ------------------------------------------------------------------

    @action(
        display_name="人工确认",
        description="通用人工确认：等待指定用户在前端确认后继续",
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": PLACEHOLDER_MANUAL_CONFIRM},
        goal_default={"timeout_seconds": 3600, "assignee_user_ids": []},
    )
    def manual_confirm(self, timeout_seconds: int, assignee_user_ids: list[str], **kwargs) -> dict:
        """通用人工确认动作的注册表入口。

        ``timeout_seconds`` 为等待上限，``assignee_user_ids`` 限定可确认用户；
        直接调用时透传附加上下文。
        """
        return kwargs

    @action(
        display_name="物料出库并挂载",
        description="申请扣减物料并挂载（接收服务端已扣减的单个根物料，挂载到目标设备的目标物料上）",
        always_free=True,
        # 挂载目标允许只给名字（{"name": ...}），执行时才回权威解析，派发前没有可锁的身份；
        # 挂载本身由目标设备 RESOURCE_APPEND 串行。出库物料 resource 照常默认加锁。
        materials_free=["mount_resource"],
        placeholder_keys={
            "resource": PLACEHOLDER_DEDUCT_RESOURCE,
            "device_id": PLACEHOLDER_DEVICES,
            "mount_resource": PLACEHOLDER_NODES,
        },
        handles=[
            ActionInputHandle(
                key="device_id",
                data_type="device_id",
                label="目标设备",
                data_key="device_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="挂载目标",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="labware",
                data_type="resource",
                label="物料创建结果",
                data_key="created_resource_tree.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="mount_resource",
                data_type="resource",
                label="挂载目标",
                data_key="mount_resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    async def apply_deduct_resource(
        self,
        resource: ResourceSlot = None,
        registry_class: str = "",
        material_name: str = "",
        device_id: DeviceSlot = "",
        mount_resource: ResourceSlot = None,
        bind_locations: ResourceDictPositionObject = None,
        slot_on_deck: str = "",
    ) -> DeductResourceReturn:
        """
        出库物料：物料进入系统的统一入口——创建/落权威，并可选挂载到目标设备的目标物料上。

        出库产物两种来源（二选一）：
        - resource：已带 uuid 的扣减产物（云端仓储扣减 / 前端 instantiate 出库端点的产物引用），
          经 materials.ensure 落权威（缺失时以原 uuid adopt 创建，已存在则采用权威记录）。
        - registry_class（+ material_name）：按 registry 资源类名现场创建全新物料
          （materials.create 权威发号，与 instantiate 端点同款语义）。

        与 transfer_resource 同构：resource / mount_resource 均为**单个物料**
        （单 ResourceSlot）。框架在 send_goal 把以下两种入参形态解析为单个 PLR 实例：
        - list：一棵树的扁平节点组（上游 handle 的 @flatten）→ 装配成一个物料（这一组必须只有一个根）。
        - dict：资源引用 → 按 uuid with_children 拉取一个物料。

        两种用法：
        - 仅登记/透传（不传 mount_resource）：把出库物料经 labware 输出，
          方便后续 set_substance 设置内容物、再由 transfer_resource 派发。
        - 出库并挂载（给 mount_resource）：下行 RESOURCE_APPEND 请求设备
          append_resource——设备按 uuid 从微后端权威拉取实例化 → 实际 assign → "update" 快照回报
          （相当于从仓库放到仓储设备上；设备侧只做投影挂载）。目标设备缺省自动推断
          （本进程 tracker 零通信优先，materials.owner_device_of 兜底）；device_id 仅作显式覆盖。

        输出 handle：labware = 出库/挂载得到的物料树；mount_resource = 实际挂载到的目标物料树
        （未挂载时为空），便于下游节点继续引用挂载位置。

        Args:
            resource[扣减物料]: 已扣减的单个根物料（可选；与 registry_class 二选一，dict/list 两形态均解析为一个物料）。
            registry_class[资源类]: registry 资源类 id（可选；与 resource 二选一，按类名现场创建全新物料）。
            material_name[实例名]: 现场创建时的实例名（配合 registry_class）。
            device_id[目标设备]: 挂载到的边缘设备 id（可选；缺省由 mount_resource 自动推断，仅作显式覆盖）。
            mount_resource[挂载目标]: 实际挂载到的单个目标物料/父节点（可选；不传则仅登记/透传，可由图 handle 连入，dict/list 两形态）。
            bind_locations[挂载位置]: 挂载目标坐标系下的挂载坐标（挂载时使用）。
            slot_on_deck[Deck槽位]: 挂载目标槽位，label 或 0-based 数字索引（如 "A1"/"0"，可选）。
        """
        return await host_material_actions.deduct_resource(
            self._node,
            self._dispatch,
            resource,
            registry_class=registry_class,
            material_name=material_name,
            device_id=device_id,
            mount_resource=mount_resource,
            bind_locations=bind_locations,
            slot_on_deck=slot_on_deck,
        )

    @action(
        display_name="设置物料内容物",
        description="设置物料内容物（液体/固体，默认单位 微升/微克）；接收单个物料，设置后输出",
        always_free=True,
        materials_need_lock=["resource"],
        placeholder_keys={"resource": PLACEHOLDER_DEDUCT_REAGENT},
        handles=[
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="目标物料",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="resource",
                data_type="resource",
                label="目标物料",
                data_key="resource",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    async def set_substance(
        self,
        resource: ResourceSlot,
        substance_names: List[str],
        amounts: List[float],
        slots: List[str] = [],
        is_solid: List[bool] = [],
    ) -> dict:
        """
        设置单个物料的内容物（液体或固体）。

        接收的物料必须是单个，且为以下之一：
        - container：直接设置在自身的 tracker 上；
        - well（带标号的容器）：同样设置在自身；
        - carrier / plate 带 container：按 slots 设置在对应子容器的 tracker 上（支持 tracker 输入）。

        设置目标只有两种：物料自身，或物料下面 children 的孔位。由 slots 区分（空=自身）。
        单位固定默认：固体=微克(ug)、液体=微升(ul)，由 is_solid 区分（unilab 定制 PLR 的
        set_liquids 仅支持 ul/ug）。底层走 set_liquids 三元组 (名称, 量, 单位)。

        Args:
            resource[目标物料]: 单个物料（container / well / 带子容器的 carrier|plate）。
            substance_names[物质名称]: 每个目标的物质名（液体名或固体名）。
            amounts[用量]: 每个目标的用量（液体=体积/微升，固体=质量/微克）。
            slots[子孔位]: 子孔位 id/索引；为空=设在物料自身，非空=设在对应子容器。
            is_solid[是否固体]: 每个目标是否固体（可选，缺省按液体处理；决定单位 ug/ul）。
        """
        return await host_material_actions.set_substance(
            self._node, resource, substance_names, amounts, slots=slots, is_solid=is_solid
        )

    @action(
        display_name="废弃物料",
        description="废弃台面物料（指定设备 + uuid：云端销毁并通知该设备本地移除）",
        always_free=True,
        materials_need_lock=["resource"],
        placeholder_keys={
            "resource": PLACEHOLDER_NODES,
            "device_id": PLACEHOLDER_DEVICES,
        },
        handles=[
            ActionInputHandle(
                key="device_id",
                data_type="device_id",
                label="所属设备",
                data_key="device_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="废弃物料",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    async def discard_resource(self, resource: ResourceSlot, device_id: DeviceSlot = "") -> dict:
        """
        废弃单个台面物料。

        与 apply_deduct_resource 对称（扣减→挂载到设备 / 废弃→从设备移除并销毁）：接收单个
        已存在物料（前端用节点选择器选择，或图 handle 传入，框架在 send_goal 已解析为 PLR
        实例），先由 MaterialsService 权威执行销毁，成功后再通知所属边缘设备本地移除该
        物料。物料被销毁后无图输出 handle。

        所属设备缺省自动推断（本进程 tracker 零通信优先，materials.owner_device_of 兜底）；
        显式传 device_id 可覆盖。

        Args:
            resource[废弃物料]: 要废弃的单个台面物料（须带 unilabos_uuid）。
            device_id[所属设备]: 物料所在的边缘设备 id（可选；缺省自动推断）。
        """
        return await host_material_actions.discard_resource(
            self._node, self._dispatch, resource, device_id
        )

    @action(
        display_name="转移物料",
        description="转移物料（系统派发）：把已物理就位的物料在系统中改挂到目标设备的目标孔位（人工/机械臂工作流的统一末步）",
        always_free=True,
        materials_need_lock=["resource", "mount_resource"],
        placeholder_keys={
            "target_device": PLACEHOLDER_DEVICES,
            "mount_resource": PLACEHOLDER_NODES,
        },
        handles=[
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="待转移物料",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="target_device",
                data_type="device_id",
                label="目标设备",
                data_key="target_device",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="site",
                data_type="site",
                label="目标槽位",
                data_key="site",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="resource",
                data_type="resource",
                label="已转移物料",
                data_key="resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="site",
                data_type="site",
                label="目标槽位",
                data_key="site",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    async def transfer_resource(
        self,
        resource: ResourceSlot,
        mount_resource: ResourceSlot = None,
        site: SiteSlot = "",
        target_device: DeviceSlot = "",
    ) -> TransferResourceReturn:
        """
        转移物料到目标物料的孔位（系统记账，不含物理搬运）。物理搬运由前序节点保证：
        - 人工：apply_deduct_resource → manual_confirm（人工搬运到位）→ transfer_resource
        - 机械臂：apply_deduct_resource → 机械臂 pick → 机械臂 place → transfer_resource

        只需给物料与目标物料，两端设备自动推断（本进程 tracker 零通信优先，
        materials.owner_device_of 兜底）——来源设备 = 物料当前所在根树的归属
        （unload 通知发给真实持有者），目标设备 = 目标物料所在根树的归属；
        target_device 仅作显式覆盖。

        与 apply_deduct_resource 同构：resource / mount_resource 均为**单个物料**（单 ResourceSlot）。
        单物料有两种入参形态，框架在 send_goal 自动解析为一个 PLR 实例：
        - list：一棵树的扁平节点组（上游 handle 的 @flatten）→ 装配成一个物料（这一组必须只有一个根）。
        - dict：资源引用 → 按 uuid with_children 拉取一个物料。

        Args:
            resource[待转移物料]: 待转移的单个物料（须带 unilabos_uuid，可由图 handle 连入，list/dict 两形态）。
            mount_resource[目标孔位]: 目标物料——单个挂载孔位/父物料（list/dict 两形态）。
            site[目标槽位]: 目标父级上的 Site（SiteSlot：前端选择器提交权威 ResourceSite 的 uuid，
                兼容 label/index 便捷形态）；不传则由父级默认排布。
            target_device[目标设备]: 可选覆盖；缺省由 mount_resource 自动推断。
        """
        return await host_material_actions.transfer_resource(
            self._node, resource, mount_resource, site, target_device
        )

    # ------------------------------------------------------------------
    # 诊断动作
    # ------------------------------------------------------------------

    @action(
        display_name="测试物料传递",
        description="诊断动作：回显传入的物料 / 设备 / 样品，用于验证 ResourceSlot、DeviceSlot 与 handle 链路",
        always_free=True,
        # 只回显、不改变物料：不占用物料锁
        materials_free=["resource", "resources"],
        handles=[
            ActionInputHandle(
                key="input_resources",
                data_type="resource",
                label="输入物料",
                data_key="resources",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    def test_resource(
        self,
        sample_uuids: SampleUUIDsType,
        resource: ResourceSlot = None,
        resources: List[ResourceSlot] = None,
        device: DeviceSlot = None,
        devices: List[DeviceSlot] = None,
    ) -> TestResourceReturn:
        """
        回显传入的物料、设备与样品，用于验证 ResourceSlot / DeviceSlot 解析与 handle 链路。

        Args:
            sample_uuids[样品]: 样品 uuid 到物料内容的映射，原样转成 LabSample 返回。
            resource[物料引用]: 单个物料（可选；不传时用一个占位容器代替）。
            resources[物料引用（多选）]: 多个物料，可由上游 handle 连入。
            device[设备引用]: 单个设备 id（可选）。
            devices[设备引用（多选）]: 多个设备 id（可选）。
        """
        if resources is None:
            resources = []
        if devices is None:
            devices = []
        if resource is None:
            resource = RegularContainer("test_resource传入None")
        return {
            "resources": ResourceTreeSet.from_plr_resources([resource, *resources]).dump(),
            "devices": [device, *devices],
            "unilabos_samples": [
                LabSample(
                    sample_uuid=sample_uuid,
                    oss_path="",
                    extra={"material_uuid": content} if isinstance(content, str) else content.serialize(),
                )
                for sample_uuid, content in sample_uuids.items()
            ],
        }

    @action(
        display_name="测试通讯延迟",
        description=(
            "诊断动作：对本执行端连接的 Backend（--address，缺省本机 Backend 端口）做 5 次 ping-pong——"
            "同地址上的 HTTP 数据面与 runtime.v1 控制 WebSocket 各一条链路，"
            "统计往返时延、时钟偏差与任务下发延迟"
        ),
        always_free=True,
    )
    def test_latency(self) -> TestLatencyReturn:
        """委托当前执行适配器的共享实现（HostAdapterBase.test_latency）。

        测的是 Edge ↔ Backend 的链路（按部署形态自动选目标），不是本机执行适配器本身。
        """
        from unilabos.backend.hostlink.adapter_registry import get_execution_adapter

        adapter = get_execution_adapter(timeout=5.0)
        impl = getattr(adapter, "test_latency", None) if adapter is not None else None
        if not callable(impl):
            raise BackendCapabilityError(
                "test_latency 需要执行适配器（ROS2 HostNode / HostLink adapter）在位"
            )
        return impl()


__all__ = [
    "DeductResourceReturn",
    "HOST_SERVICE_ACTIONS",
    "HostServices",
    "TestLatencyReturn",
    "TestResourceReturn",
    "TransferResourceReturn",
]
