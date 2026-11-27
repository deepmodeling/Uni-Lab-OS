"""驱动运行时（Driver Runtime）的不可变结果与稳定错误模型。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class DriverActivationError(RuntimeError):
    """设备定义无法安全激活为 Python 驱动时的稳定错误。"""

    def __init__(
        self,
        code: str,
        definition_identity: str,
        message: str,
    ) -> None:
        """保存稳定错误分类、请求身份和中文诊断。

        参数：``code`` 是供调用方分类的稳定错误码；``definition_identity`` 是
        物理图请求的设备定义身份；``message`` 是供操作员诊断的中文说明。
        返回：无。
        异常：无；底层异常应由调用点使用 ``raise ... from ...`` 保留。
        """

        super().__init__(message)
        self.code = code
        self.definition_identity = definition_identity


@dataclass(frozen=True, slots=True, init=False)
class PythonDriverActivation:
    """一次已验证 Python 驱动激活的不可变结果。

    可变配置在构造和读取时都深复制。驱动实例因此获得普通可变字典，却不能
    修改注册表条目、调用方输入或同一激活结果供其他实例读取的稳定内容。
    """

    definition_identity: str
    source_identity: str
    content_hash: str | None
    package_catalog_digest: str | None
    driver_class: type[Any]
    driver_is_ros: bool
    _driver_params: dict[str, Any] = field(repr=False)
    _status_types: dict[str, Any] = field(repr=False)
    _action_value_mappings: dict[str, Any] = field(repr=False)
    _hardware_interface: dict[str, Any] = field(repr=False)

    def __init__(
        self,
        *,
        definition_identity: str,
        source_identity: str,
        content_hash: str | None,
        package_catalog_digest: str | None,
        driver_class: type[Any],
        driver_params: dict[str, Any],
        status_types: dict[str, Any],
        action_value_mappings: dict[str, Any],
        hardware_interface: dict[str, Any],
        driver_is_ros: bool,
    ) -> None:
        """冻结身份、包证据、驱动类及全部可变运行配置。

        参数：``definition_identity`` 是解析后的稳定设备定义身份；
        ``source_identity`` 是 ``module:symbol`` 驱动源码身份；``content_hash``
        与 ``package_catalog_digest`` 是包托管定义的可选内容证据；
        ``driver_class`` 是唯一已加载的 Python 类；``driver_params`` 是合并后的
        实例配置；``status_types``、``action_value_mappings`` 和
        ``hardware_interface`` 是 ROS 包装所需合同；``driver_is_ros`` 表示驱动
        是否原生使用 ROS2。
        返回：无；所有可变容器均深复制进不可变结果。
        异常：容器无法深复制时传播原始异常，激活入口会把它包装成稳定错误。
        """

        object.__setattr__(self, "definition_identity", definition_identity)
        object.__setattr__(self, "source_identity", source_identity)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "package_catalog_digest",
            package_catalog_digest,
        )
        object.__setattr__(self, "driver_class", driver_class)
        object.__setattr__(self, "driver_is_ros", driver_is_ros)
        object.__setattr__(self, "_driver_params", copy.deepcopy(driver_params))
        object.__setattr__(self, "_status_types", copy.deepcopy(status_types))
        object.__setattr__(
            self,
            "_action_value_mappings",
            copy.deepcopy(action_value_mappings),
        )
        object.__setattr__(
            self,
            "_hardware_interface",
            copy.deepcopy(hardware_interface),
        )

    @property
    def driver_params(self) -> dict[str, Any]:
        """返回一个设备实例专用的驱动参数副本。

        参数：无。
        返回：与激活结果及其他调用方隔离的普通可变字典。
        异常：内部值无法深复制时传播原始异常。
        """

        return copy.deepcopy(self._driver_params)

    @property
    def status_types(self) -> dict[str, Any]:
        """返回一个 ROS 包装调用专用的状态类型合同副本。

        参数：无。
        返回：与激活结果隔离的状态类型字典。
        异常：内部值无法深复制时传播原始异常。
        """

        return copy.deepcopy(self._status_types)

    @property
    def action_value_mappings(self) -> dict[str, Any]:
        """返回一个 ROS 包装调用专用的动作值映射副本。

        参数：无。
        返回：与激活结果隔离的动作值映射字典。
        异常：内部值无法深复制时传播原始异常。
        """

        return copy.deepcopy(self._action_value_mappings)

    @property
    def hardware_interface(self) -> dict[str, Any]:
        """返回一个 ROS 包装调用专用的硬件接口合同副本。

        参数：无。
        返回：与激活结果隔离的硬件接口字典。
        异常：内部值无法深复制时传播原始异常。
        """

        return copy.deepcopy(self._hardware_interface)
