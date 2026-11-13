"""物料链路契约：创建只发生在微后端，host 只做挂载编排与变更分发。

下行链路（append_resource / 资源树同步）不建 ROS service：
本进程直调设备节点实例方法，跨机（Slave）经 HostLink 下行 RPC。
"""

import inspect

import pytest


def test_microbackend_owns_material_creation() -> None:
    """物料创建由微后端提供，HostNode 只负责执行编排（双 backend 同契约）。"""
    from unilabos.backend.hostlink.host_node import HostNode as HostLinkHostNode
    from unilabos.backend.ros2.presets.host_node import HostNode as ROS2HostNode

    for cls in (ROS2HostNode, HostLinkHostNode):
        assert not hasattr(cls, "create_resource")
        assert not hasattr(cls, "create_resource_detailed")


def test_host_exposes_shared_append_and_notification_dispatch() -> None:
    """host 提供统一下行通道与资源树变更分发。

    下行通道与变更分发都不绑定任何 host 编排类——它们是 hostlink.downlink 的
    模块级函数，HostServices 零参构造时默认落到 material_dispatch，微后端
    直接调用 notify_resource_tree_update。
    """
    from unilabos.backend.hostlink import downlink

    assert inspect.iscoroutinefunction(downlink.material_dispatch)
    dispatch_source = inspect.getsource(downlink.material_dispatch)
    assert "RESOURCE_APPEND" in dispatch_source
    assert "RESOURCE_TREE_SYNC" in dispatch_source

    notify_params = inspect.signature(downlink.notify_resource_tree_update).parameters
    assert list(notify_params) == ["device_id", "action", "resource_uuid_list"]


def test_material_downlink_uses_direct_or_hostlink_dispatch() -> None:
    """物料下行通过本进程直调或 HostLink RPC 分发。"""
    from unilabos.backend.hostlink import host_node as hostlink_host_node
    from unilabos.backend.ros2 import base_device_node
    from unilabos.backend.ros2.presets import host_node as ros2_host_node

    device_source = inspect.getsource(base_device_node)
    assert "/append_resource" not in device_source
    assert "/s2c_resource_tree" not in device_source

    for module in (ros2_host_node, hostlink_host_node):
        host_source = inspect.getsource(module)
        assert "/append_resource" not in host_source
        assert "/s2c_resource_tree" not in host_source


def test_device_exposes_downlink_coroutines() -> None:
    """设备侧以实例方法承接下行：挂载与资源树同步均为协程，供直调/HostLink 桥调用。

    两个方法（连同 transfer_to_new_resource 与资源树锁）定义在 backend 无关的
    DeviceNode 上，ROS 层只继承不重复实现；实现不依赖 rclpy / SerialCommand，
    HostLink backend 的设备节点同样可用。
    """
    from unilabos.backend.runtime.node import DeviceNode
    from unilabos.backend.ros2.base_device_node import BaseROS2DeviceNode

    assert inspect.iscoroutinefunction(DeviceNode.append_resource)
    assert inspect.iscoroutinefunction(DeviceNode.apply_resource_tree_update)
    assert callable(DeviceNode.transfer_to_new_resource)
    for name in ("append_resource", "apply_resource_tree_update", "transfer_to_new_resource"):
        assert name not in vars(BaseROS2DeviceNode), f"{name} 不应在 ROS 层重复实现"
    # DeviceNode 不暴露 transport 专用的 ROS 回调。
    assert not hasattr(BaseROS2DeviceNode, "s2c_resource_tree")

    # DeviceNode 的实现不依赖 ROS，挂载后的快照通过 update_resource 写入权威。
    for method in (DeviceNode.append_resource, DeviceNode.apply_resource_tree_update):
        source = inspect.getsource(method)
        assert "rclpy" not in source
        assert "SerialCommand" not in source
        assert "c2s_update_resource_tree" not in source
        assert "update_resource" in source

    # 等待挂起用 backend 提供的 Future：DeviceNode 默认 asyncio，ROS 层覆写为 rclpy Future
    assert callable(DeviceNode.create_wait_future)
    assert "create_wait_future" in vars(BaseROS2DeviceNode)


def test_device_append_resource_requires_authority_uuid() -> None:
    """设备侧挂载只接受带权威 uuid 的物料引用。

    assign 复用 transfer_to_new_resource，并按 site/spot 选择挂载位置。
    """
    import unilabos.backend.runtime.node as device_node_module
    from unilabos.backend.ros2 import base_device_node

    # BaseROS2DeviceNode 继承自 DeviceNode，取到的是同一份实现
    source = inspect.getsource(base_device_node.BaseROS2DeviceNode.append_resource)
    assert '"resource_uuid"' in source or "'resource_uuid'" in source
    assert "transfer_to_new_resource" in source
    # append 请求不包含本地初始化字段。
    assert "initialize_full" not in inspect.getsource(base_device_node)
    assert "initialize_full" not in inspect.getsource(device_node_module)


def test_resource_tree_mutex_is_backend_neutral() -> None:
    """资源树互斥锁泛化为 DeviceAsyncMutex：唤醒经 node.create_task 调度，
    等待经 node.create_wait_future 挂起，且不依赖 rclpy。"""
    from unilabos.backend.runtime.async_utils import DeviceAsyncMutex
    from unilabos.backend.ros2 import base_device_node

    import unilabos.backend.runtime.async_utils as async_utils_module

    # 不 import rclpy、不调用 rclpy API（docstring 提及不算）
    assert "import rclpy" not in inspect.getsource(async_utils_module)
    mutex_source = inspect.getsource(DeviceAsyncMutex)
    assert "rclpy." not in mutex_source
    assert "create_wait_future" in mutex_source
    assert "create_task" in mutex_source

    assert not hasattr(base_device_node, "RclpyAsyncMutex")


def test_resource_query_goes_through_authority_not_ros() -> None:
    """物料查询在 DeviceNode 上通过 ResourceService 访问权威。"""
    import unilabos.backend.runtime.node as device_node_module
    from unilabos.backend.hostlink import host_node as hostlink_host_node
    from unilabos.backend.hostlink import workstation as hostlink_workstation
    from unilabos.backend.runtime.node import DeviceNode
    from unilabos.backend.ros2 import base_device_node
    from unilabos.backend.ros2.presets import host_node as ros2_host_node
    from unilabos.backend.ros2.presets import workstation as ros2_workstation

    # DeviceNode 暴露 transport 中立的查询协程。
    assert inspect.iscoroutinefunction(DeviceNode.get_resource)
    assert inspect.iscoroutinefunction(DeviceNode.get_resource_by_id)
    assert inspect.iscoroutinefunction(DeviceNode.get_resource_with_dir)
    for name in ("get_resource", "get_resource_by_id", "get_resource_with_dir"):
        assert name not in vars(base_device_node.BaseROS2DeviceNode), f"{name} 不应在 ROS 层重复实现"

    # DeviceNode 的实现不依赖 ROS
    assert "rclpy" not in inspect.getsource(device_node_module.DeviceNode.get_resource_by_id)

    # 查询链路不注册 /resources/get ROS 服务。
    for module in (
        base_device_node,
        ros2_host_node,
        hostlink_host_node,
        ros2_workstation,
        hostlink_workstation,
    ):
        assert "/resources/get" not in inspect.getsource(module), f"{module.__name__} 仍引用 /resources/get"


def test_materials_module_exposes_get_and_search() -> None:
    """materials 提供公共查询入口：get（uuid/dir，未命中抛错）与 search（name，未命中返回 []）。"""
    from unilabos.resources import materials

    assert callable(materials.get)
    assert callable(materials.search)
    assert "get" in materials.__all__ and "search" in materials.__all__

    # 网关协议与三条链路（Local / HTTP / HostLink client）都支持按 name 搜索
    from unilabos.client.materials import (
        HTTPMaterialsClient,
        HostLinkMaterialsClient,
        LocalMaterialsClient,
    )

    for client_cls in (LocalMaterialsClient, HTTPMaterialsClient, HostLinkMaterialsClient):
        assert callable(getattr(client_cls, "search_materials")), client_cls.__name__

    from unilabos.backend.hostlink.protocol import ActionType

    assert ActionType.MATERIAL_SEARCH == "material.search"


def test_material_update_path_uses_materials_authority() -> None:
    """资源树更新通过 materials API 访问同一权威。

    create/ensure/get/update/remove 提供统一入口，Slave 经 HostLink 访问权威，
    启动时使用 materials.ensure 对齐图中的 UUID。
    """
    from unilabos.backend.hostlink import doctor
    from unilabos.backend.hostlink import host_node as hostlink_host_node
    from unilabos.backend.ros2 import main_slave_run
    from unilabos.backend.ros2.presets import host_node as ros2_host_node

    for module in (ros2_host_node, hostlink_host_node, main_slave_run, doctor):
        assert "c2s_update_resource_tree" not in inspect.getsource(module), module.__name__

    for name in (
        "_resource_tree_update_callback",
        "_resource_tree_action_add_callback",
        "_resource_tree_action_get_callback",
        "_resource_tree_action_update_callback",
        "_resource_tree_action_remove_callback",
    ):
        assert not hasattr(ros2_host_node.HostNode, name), f"{name} 不属于 HostNode"
        assert not hasattr(hostlink_host_node.HostNode, name), f"{name} 不属于 HostNode"

    slave_source = inspect.getsource(main_slave_run)
    assert "materials.ensure" in slave_source
    assert "uuid_mapping" not in slave_source


def test_boot_material_alignment_is_shared_between_host_and_slave() -> None:
    """开机物料对齐语义统一：host（main）与两种 slave 入口都走 materials.ensure。"""
    import unilabos.app.main as app_main
    import unilabos.backend.hostlink.main_hostlink_run as hostlink_run
    from unilabos.resources import materials

    for name in ("ensure", "update", "remove", "create"):
        assert callable(getattr(materials, name))
        assert name in materials.__all__

    assert "ensure" in inspect.getsource(app_main)
    assert "materials.ensure" in inspect.getsource(hostlink_run)

    # ensure 的服务端支点：create 请求可携带显式 material_uuid（带条件的创建）
    from unilabos.protocol.materials import MaterialNodeCreate

    assert "material_uuid" in MaterialNodeCreate.model_fields


def test_apply_deduct_resource_lands_material_via_materials_protocol() -> None:
    """出库扣减走 materials 协议：挂载/透传前经 materials.ensure 把扣减产物
    （带 uuid）写入权威，再投影到设备。"""
    from unilabos.backend import host_material_actions

    source = inspect.getsource(host_material_actions.deduct_resource)
    assert "materials.ensure" in source


def test_host_material_actions_shared_by_both_backends() -> None:
    """host 物料 API 固定为四动作，动作定义与业务实现都只有一份：

    - 动作定义位于 backend 无关的 HostServices（@device/@action 扫描源），
      两种 backend 都经通用设备
      管线从外部初始化同一个类（ROS2 initialize_device_from_dict /
      HostLink register_host_services）；
    - 实现唯一：方法体全部调用 host_material_actions（全走 materials.*）；
    - 人工闸门由系统自带的 manual_confirm 承担；
    - 共享实现按 materials.resolve 统一解析 ResourceSlot 入参。
    """
    from unilabos.backend import host_material_actions
    from unilabos.backend.host_services import HOST_SERVICE_ACTIONS, HostServices
    from unilabos.backend.hostlink import host_services as hostlink_assembly
    from unilabos.backend.hostlink.host_node import HostNode as HostLinkHostNode
    from unilabos.backend.ros2.presets.host_node import HostNode
    from unilabos.resources import materials

    assert host_material_actions.HOST_MATERIAL_ACTIONS == (
        "apply_deduct_resource",
        "set_substance",
        "discard_resource",
        "transfer_resource",
    )
    assert set(HOST_SERVICE_ACTIONS) == {
        *host_material_actions.HOST_MATERIAL_ACTIONS,
        "manual_confirm",
        "test_resource",
        "test_latency",
    }

    # 人工确认与物料转移是两个独立动作。
    assert not hasattr(HostNode, "transfer_manual")
    assert not hasattr(HostServices, "transfer_manual")

    # HostNode 只负责执行适配；host_node 服务设备由通用设备管线初始化。
    # test_latency 例外：动作定义在 HostServices（委托适配器），ping-pong
    # 实现在适配器共享基类 HostAdapterBase 上，两种 backend 复用。
    for action_name in HOST_SERVICE_ACTIONS:
        if action_name == "test_latency":
            continue
        for cls in (HostNode, HostLinkHostNode):
            assert not hasattr(cls, action_name), (
                f"{cls.__module__} 不应定义 {action_name}（定义位于 HostServices）"
            )
    from unilabos.backend.runtime.host_adapter import HostAdapterBase

    assert HostNode.test_latency is HostAdapterBase.test_latency
    assert HostLinkHostNode.test_latency is HostAdapterBase.test_latency
    assert "get_execution_adapter" in inspect.getsource(HostServices.test_latency)
    assert hostlink_assembly.HostServices is HostServices
    init_source = inspect.getsource(HostNode.__init__)
    assert "initialize_device(self.device_id, host_node_instance)" in init_source
    assert "HostServices(" not in init_source

    # 方法体指向共享实现
    pairs = {
        "apply_deduct_resource": "deduct_resource",
        "set_substance": "set_substance",
        "discard_resource": "discard_resource",
        "transfer_resource": "transfer_resource",
    }
    for method_name, shared_name in pairs.items():
        source = inspect.getsource(getattr(HostServices, method_name))
        assert f"host_material_actions.{shared_name}" in source, (
            f"HostServices.{method_name} 应调用共享实现 {shared_name}"
        )

    # 共享实现全走 materials.* 门面（出库=创建：registry_class 现场创建 /
    # 带 uuid 产物 ensure adopt，两种来源都在 deduct_resource 内闭环；
    # 来源/目标设备经 owner_device_of 自动推断，转移直接走 materials.transfer）
    assert callable(materials.resolve)
    assert callable(materials.owner_device_of)
    shared_source = inspect.getsource(host_material_actions)
    for expected in (
        "materials.resolve",
        "materials.create",
        "materials.ensure",
        "materials.apply_substances",
        "materials.remove",
        "materials.owner_device_of",
        "materials.transfer",
        "update_resource",
    ):
        assert expected in shared_source, expected
    deduct_params = inspect.signature(
        host_material_actions.deduct_resource
    ).parameters
    assert "registry_class" in deduct_params
    assert "material_name" in deduct_params
    # 设备参数全部可缺省（自动推断）；target_device/device_id 仅作显式覆盖
    transfer_params = inspect.signature(
        host_material_actions.transfer_resource
    ).parameters
    assert transfer_params["target_device"].default == ""
    discard_params = inspect.signature(
        host_material_actions.discard_resource
    ).parameters
    assert discard_params["device_id"].default == ""


def test_host_node_registry_is_single_scan_source() -> None:
    """host_node 注册表唯一定义源：默认扫描 exclude + 单独处理。

    - ``@device(id="host_node")`` 只落在 backend 无关的 HostServices 上；
    - host_services.py 不进默认启动扫描（exclude），registry 单独扫描该文件
      并由 _setup_host_node 登记注册表条目（module 指向 HostServices）；
    - 两个编排类 HostNode（ros2/presets 与 hostlink）都不携带 @device，
      两种 backend 使用同一个注册表 entry。
    """
    import unilabos.backend.hostlink.host_node as hostlink_orchestrator_module
    import unilabos.backend.ros2.presets.host_node as ros2_orchestrator_module
    import unilabos.registry.registry as registry_module
    from unilabos.backend import host_services

    host_services_source = inspect.getsource(host_services)
    # 装饰器允许按格式化工具拆成多行；这里只验证 AST 扫描依赖的语义字段，
    # 不把源码排版形式当成注册表契约。
    assert "@device(" in host_services_source
    assert 'id="host_node"' in host_services_source

    # Registry 被 @singleton 装饰，直接对模块源码断言：
    # host_services.py 进默认扫描 exclude、单独扫描产出 _host_node_ast_entry、
    # 覆写条目的 module 指向 HostServices。
    registry_source = inspect.getsource(registry_module)
    assert 'exclude_files = {"host_services.py"}' in registry_source
    assert "_host_node_ast_entry" in registry_source
    assert "unilabos.backend.host_services:HostServices" in registry_source

    for module in (ros2_orchestrator_module, hostlink_orchestrator_module):
        assert "@device(" not in inspect.getsource(module), module.__name__


def test_ensure_host_node_resource_reuses_graph_identity_or_inserts_default() -> None:
    """host node 按 template_name 判别且全图唯一；实例 id 以 ``--host_node_id`` 为准。

    图中声明时复用其 uuid（实例 id 可重命名，如 host_node_8523）；未声明时
    插入运行时默认树；声明多个直接报错。
    """
    from uuid import uuid4

    from unilabos.backend.ros2.presets.host_node import ensure_host_node_resource
    from unilabos.resources.resource_tracker import ResourceTreeSet

    def _host_payload(node_id: str) -> dict:
        return {
            "id": node_id,
            "uuid": str(uuid4()),
            "name": node_id,
            "type": "device",
            "class": "host_node",
            "template_name": "host_node",
            "config": {},
            "data": {},
            "extra": {},
        }

    renamed_id = "host_node_8523"
    graph_payload = _host_payload("host_node")
    host_uuid = graph_payload["uuid"]
    tree_set = ResourceTreeSet.from_raw_dict_list([graph_payload])
    # 图中按 template_name 声明的 host node，id 与配置实例名不同：以配置为准重命名。
    reused = ensure_host_node_resource(tree_set, renamed_id)
    assert reused.res_content.id == renamed_id
    assert reused.res_content.name == renamed_id
    assert reused.res_content.uuid == host_uuid
    assert reused.res_content.klass == "host_node"
    assert reused.res_content.template_name == "host_node"
    assert reused.res_content.sites_initialized is True
    assert len(tree_set.trees) == 1

    empty = ResourceTreeSet([])
    created = ensure_host_node_resource(empty, renamed_id)
    assert created.res_content.id == renamed_id
    assert created.res_content.klass == "host_node"
    assert created.res_content.template_name == "host_node"
    assert len(empty.trees) == 1

    duplicated = ResourceTreeSet.from_raw_dict_list(
        [_host_payload("host_node"), _host_payload("host_node_backup")]
    )
    with pytest.raises(ValueError, match="只能声明一个 host node"):
        ensure_host_node_resource(duplicated, renamed_id)


def test_run_node_coroutine_is_backend_neutral() -> None:
    """协程桥 run_node_coroutine 定义在 runtime（不依赖 rclpy），
    hostlink.downlink 复用同一实现而非各自维护。"""
    import unilabos.backend.runtime.async_utils as async_utils_module
    from unilabos.backend.runtime.async_utils import run_node_coroutine
    from unilabos.backend.hostlink import downlink

    assert "import rclpy" not in inspect.getsource(async_utils_module)
    assert downlink.run_node_coroutine is run_node_coroutine


def test_pure_hostlink_slave_registers_material_downlink_handlers() -> None:
    """纯 HostLink slave（无 ROS）也承接物料下行：client 注册
    RESOURCE_TREE_SYNC / RESOURCE_APPEND，语义与 ROS slave 的
    register_hostlink_resource_handlers 一致。"""
    from unilabos.backend.hostlink.backend import HostLinkBackend

    slave_source = inspect.getsource(HostLinkBackend._start_slave)
    assert "RESOURCE_TREE_SYNC" in slave_source
    assert "RESOURCE_APPEND" in slave_source

    assert callable(HostLinkBackend._handle_resource_tree_sync)
    assert callable(HostLinkBackend._handle_resource_append)
    from unilabos.backend.runtime.node import DeviceNode

    tree_sync_source = inspect.getsource(HostLinkBackend._handle_resource_tree_sync)
    assert "run_node_coroutine" in tree_sync_source
    # 挂载经 DeviceNode.append_resource_blocking（同一 run_node_coroutine，外加提交点握手）
    append_source = inspect.getsource(HostLinkBackend._handle_resource_append)
    assert "append_resource_blocking" in append_source
    assert "run_node_coroutine" in inspect.getsource(DeviceNode.append_resource_blocking)
    for source in (tree_sync_source, append_source):
        assert "rclpy" not in source


def test_notify_resource_tree_update_is_module_level_and_backend_neutral() -> None:
    """变更分发是模块级函数（不挂在任何 host 编排类上），双 backend 同一份：

    本进程设备直调 apply_resource_tree_update（ros2 查 registered_devices、
    hostlink 查 HostLinkLocalRuntime），跨机经 RESOURCE_TREE_SYNC 下行，
    不可达返回 None。执行编排类与基类不承载 notify_* 方法。"""
    from unilabos.backend.hostlink import downlink
    from unilabos.backend.hostlink.host_node import HostNode as HostLinkHostNode
    from unilabos.backend.ros2.presets.host_node import HostNode as ROS2HostNode
    from unilabos.backend.runtime.host_adapter import HostAdapterBase

    notify_source = inspect.getsource(downlink.notify_resource_tree_update)
    assert "sync_resource_tree_to_device" in notify_source
    assert "has_device" in notify_source

    local_source = inspect.getsource(downlink.get_local_device_node)
    assert "registered_devices" in local_source
    assert "get_runtime" in local_source

    for cls in (HostAdapterBase, ROS2HostNode, HostLinkHostNode):
        assert not hasattr(cls, "notify_resource_tree_update"), cls.__name__
        assert not hasattr(cls, "notify_device_manage"), cls.__name__


def test_material_sync_uses_shared_downlink_rpc() -> None:
    """material_sync 使用统一的 MATERIAL_SYNC 下行 RPC：

    - 设备侧只保留实例协程 material_sync(dict) -> dict；
    - ROS / 纯 HostLink slave 均注册 MATERIAL_SYNC handler；
    - 两种 host 的 dispatcher 本进程直调、跨机使用 HostLink RPC。
    """
    from unilabos.backend.runtime.node import DeviceNode
    from unilabos.backend.hostlink import local_runtime
    from unilabos.backend.hostlink.backend import HostLinkBackend
    from unilabos.backend.hostlink.network import HostNetworkService
    from unilabos.backend.hostlink.protocol import ActionType
    from unilabos.backend.hostlink import downlink
    from unilabos.backend.ros2 import base_device_node

    assert inspect.iscoroutinefunction(DeviceNode.material_sync)
    assert not hasattr(DeviceNode, "setup_material_sync_service")
    assert not hasattr(DeviceNode, "_material_sync_callback")

    assert ActionType.MATERIAL_SYNC == "material.sync"

    # 设备侧不注册 per-device material_sync 服务。
    assert "/material_sync" not in inspect.getsource(base_device_node)
    assert "setup_material_sync_service" not in inspect.getsource(local_runtime)

    # slave 两侧均注册下行 handler
    assert "MATERIAL_SYNC" in inspect.getsource(downlink.register_hostlink_resource_handlers)
    assert "MATERIAL_SYNC" in inspect.getsource(HostLinkBackend._start_slave)

    # 两种 host dispatcher 均不依赖 ROS service 发现。
    ros_dispatch = inspect.getsource(HostNetworkService.dispatch_material_sync)
    assert "wait_for_service" not in ros_dispatch
    assert "create_client" not in ros_dispatch
    assert "material_sync_to_device" in ros_dispatch

    hostlink_dispatch = inspect.getsource(HostLinkBackend.dispatch_material_sync)
    assert "call_service" not in hostlink_dispatch
    assert "MATERIAL_SYNC" in hostlink_dispatch


def test_device_management_uses_shared_downlink_rpc() -> None:
    """设备管理与物料操作使用同构的下行链路：

    - 设备侧只保留实例协程 device_manage(dict) -> dict（含 create/destroy_device
      默认实现），定义在 backend 无关的 DeviceNode 上；
    - ROS / 纯 HostLink slave 均注册 DEVICE_MANAGE handler；
    - host 侧分发是模块级 device_manage_to_device（本进程直调、跨机 HostLink
      RPC）。
    """
    from unilabos.backend.runtime.node import DeviceNode
    from unilabos.backend.hostlink.backend import HostLinkBackend
    from unilabos.backend.hostlink.protocol import ActionType
    from unilabos.backend.hostlink import downlink
    from unilabos.backend.ros2 import base_device_node

    assert inspect.iscoroutinefunction(DeviceNode.device_manage)
    assert callable(DeviceNode.create_device)
    assert callable(DeviceNode.destroy_device)
    assert ActionType.DEVICE_MANAGE == "device.manage"

    # ROS 层不定义 transport 专用回调或重复的默认实现。
    device_source = inspect.getsource(base_device_node)
    assert "s2c_device_manage" not in device_source
    for name in ("device_manage", "create_device", "destroy_device"):
        assert name not in vars(base_device_node.BaseROS2DeviceNode), f"{name} 不应在 ROS 层重复实现"

    # slave 两侧均注册下行 handler
    assert "DEVICE_MANAGE" in inspect.getsource(downlink.register_hostlink_resource_handlers)
    assert "DEVICE_MANAGE" in inspect.getsource(HostLinkBackend._start_slave)

    # host 侧分发不依赖 ROS service 发现。
    manage_source = inspect.getsource(downlink.device_manage_to_device)
    assert "wait_for_service" not in manage_source
    assert "create_client" not in manage_source
    assert "DEVICE_MANAGE" in manage_source


def test_resource_tree_driver_hooks_are_uniformly_async_aware() -> None:
    """资源树驱动回调（add/update/remove）统一经 _invoke_resource_hook 触发，
    并同时支持同步与协程实现。"""
    from unilabos.backend.runtime.node import DeviceNode

    apply_source = inspect.getsource(DeviceNode.apply_resource_tree_update)
    append_source = inspect.getsource(DeviceNode.append_resource)
    for hook in ("resource_tree_add", "resource_tree_remove", "resource_tree_update"):
        assert f'_invoke_resource_hook("{hook}"' in apply_source, hook
    assert '_invoke_resource_hook("resource_tree_add"' in append_source
    for source in (apply_source, append_source):
        assert "resource_tree_add\", None" not in source
        assert "resource_tree_remove\", None" not in source
        assert "resource_tree_update\", None" not in source
