from unilabos.package_manager.driver_runtime import (
    DriverActivationError,
    activate_python_driver,
)
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.action_transport import build_runtime_action_mappings
from unilabos.ros.device_node_wrapper import ros2_device_node
from unilabos.ros.nodes.base_device_node import DeviceInitError, ROS2DeviceNode
from unilabos.utils import logger
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.import_manager import default_manager


def initialize_device_from_dict(
    device_id: str, device_config: ResourceDictInstance
) -> ROS2DeviceNode | None:
    """根据物理图设备配置解析并初始化唯一选中的设备执行器。

    参数：``device_id`` 是运行时设备实例身份；``device_config`` 是携带设备
    定义身份、稳定 UUID 和初始化配置的资源实例。
    返回：成功包装并初始化的 ROS2 设备节点；驱动抛出 ``DeviceInitError`` 时
    返回当前空结果，保留既有启动兼容语义。
    异常：设备定义为空、缺失、歧义、包证据不完整、驱动源码非法或不是字符串
    时抛出 ``DeviceClassInvalid``；设备构造器抛出 ``DeviceInitError`` 时保留既有
    空结果语义。定义解析、源码校验、唯一驱动加载和配置合并只委派给驱动运行时
    （Driver Runtime），本组合根只负责 ROS 包装与实例化。
    """
    initialized_device = None
    # ``device_definition_identity`` 是物理图选择的规范 FQID 或唯一兼容短身份。
    device_definition_identity = device_config.res_content.klass
    # ``runtime_device_uuid`` 是设备实例的稳定身份，不是设备模板身份。
    runtime_device_uuid = device_config.res_content.uuid
    if isinstance(device_definition_identity, str):
        if len(device_definition_identity) == 0:
            raise DeviceClassInvalid(
                f"Device [{device_id}] class cannot be an empty string. {device_config}"
            )
        try:
            # ``activation`` 是驱动运行时完成关闭式验证后的唯一加载结果。
            activation = activate_python_driver(
                lab_registry,
                device_definition_identity,
                device_config.res_content.config,
                loader=default_manager.get_class,
            )
        except DriverActivationError as error:
            raise DeviceClassInvalid(
                f"Device [{device_id}] class {device_definition_identity} invalid: {error}. "
                f"{device_config}"
            ) from error
    elif isinstance(device_definition_identity, dict):
        raise DeviceClassInvalid(
            f"Device [{device_id}] class config should be type 'str' but 'dict' got. {device_config}"
        )
    else:
        logger.warning(
            f"initialize device {device_id} failed, provided device_config: {device_config}"
        )
        return initialized_device
    if isinstance(device_definition_identity, str):
        # ``runtime_action_mappings`` 是仅供本地 ROS 传输使用的动作合同副本；
        # 内部通用命令端点不会反向污染包目录（Package Catalog）的公开投影。
        runtime_action_mappings = build_runtime_action_mappings(
            activation.action_value_mappings
        )
        # 不管是ros2的实例，还是python的，都必须包一次，除了HostNode
        wrapped_driver = ros2_device_node(
            activation.driver_class,
            status_types=activation.status_types,
            device_config=device_config,
            action_value_mappings=runtime_action_mappings,
            hardware_interface=activation.hardware_interface,
        )
        try:
            initialized_device = wrapped_driver(
                device_id=device_id,
                device_uuid=runtime_device_uuid,
                driver_is_ros=activation.driver_is_ros,
                driver_params=activation.driver_params,
            )
        except DeviceInitError:
            return initialized_device
    return initialized_device
