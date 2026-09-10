"""从实时注册表安全激活唯一选中的 Python 设备驱动。"""

from __future__ import annotations

import copy
import inspect
import keyword
import re
from collections.abc import Callable, Mapping
from typing import Any

from unilabos.registry.init_enforce import merge_init_param_enforce

from .model import DriverActivationError, PythonDriverActivation

_PACKAGE_EVIDENCE_FIELDS = (
    "package_definition_fqid",
    "content_hash",
    "package_catalog_digest",
    "source_fqid",
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DEFAULT_HARDWARE_INTERFACE = {
    "name": "hardware_interface",
    "write": "send_command",
    "read": "read_data",
    "extra_info": [],
}


def activate_python_driver(
    registry: Any,
    definition_identity: str,
    runtime_config: Any,
    *,
    loader: Callable[[str], Any],
) -> PythonDriverActivation:
    """解析、验证并加载唯一选中的 Python 驱动。

    参数：``registry`` 必须实现 ``resolve_definition(kind, identity)``；
    ``definition_identity`` 是物理图中的规范全限定身份或唯一短名；
    ``runtime_config`` 是设备实例配置；``loader`` 只按验证后的
    ``module:symbol`` 源码身份加载一个对象。
    返回：包含规范定义身份、包证据、已加载类及隔离配置的不可变激活结果。
    异常：身份解析、注册表结构、包证据、源码一致性或驱动加载失败时抛出
    ``DriverActivationError``；加载器异常作为 cause 保留。解析与验证完成前绝不
    调用 ``loader``，未选中的定义也不会被枚举或导入。
    """

    if not isinstance(definition_identity, str) or not definition_identity.strip():
        raise DriverActivationError(
            "definition_resolution_error",
            str(definition_identity),
            "设备定义身份必须是非空字符串",
        )
    try:
        # ``registry_entry`` 是实时注册表权威代际解析出的唯一设备定义副本。
        registry_entry = registry.resolve_definition("device", definition_identity)
    except Exception as error:
        raise DriverActivationError(
            "definition_resolution_error",
            definition_identity,
            f"设备定义无法唯一解析: {definition_identity}",
        ) from error
    if not isinstance(registry_entry, Mapping):
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            "设备注册表条目必须是对象",
        )

    # ``class_mapping`` 是静态编译产生的驱动加载与 ROS 包装合同。
    class_mapping = registry_entry.get("class")
    if not isinstance(class_mapping, Mapping):
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            "设备注册表条目缺少 class 对象",
        )
    # ``source_identity`` 是经过语法门禁后唯一允许交给加载器的作者源码身份。
    source_identity = class_mapping.get("module")
    if not _valid_source_identity(source_identity):
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            "设备驱动必须使用合法的绝对 module:symbol 源码身份",
        )
    # 三项 ``driver_contract`` 在加载作者代码前完成关闭式结构校验；只有缺键可默认。
    status_types = _contract_mapping(
        class_mapping,
        "status_types",
        definition_identity=definition_identity,
    )
    action_value_mappings = _contract_mapping(
        class_mapping,
        "action_value_mappings",
        definition_identity=definition_identity,
    )
    hardware_interface = _contract_mapping(
        class_mapping,
        "hardware_interface",
        definition_identity=definition_identity,
        default=_DEFAULT_HARDWARE_INTERFACE,
    )

    package_evidence = _validate_package_evidence(
        registry_entry,
        definition_identity=definition_identity,
        source_identity=source_identity,
    )
    # ``resolved_definition_identity`` 是包定义 FQID、显式条目 ID 或精确注册表键。
    resolved_definition_identity = _resolved_definition_identity(
        registry_entry,
        definition_identity=definition_identity,
        package_definition_fqid=package_evidence[0],
    )
    try:
        # ``driver_class`` 是本次唯一允许加载的物理图选中驱动实现。
        driver_class = loader(source_identity)
    except Exception as error:
        raise DriverActivationError(
            "driver_load_error",
            resolved_definition_identity,
            f"设备驱动加载失败: {source_identity}",
        ) from error
    if not inspect.isclass(driver_class):
        raise DriverActivationError(
            "driver_not_class",
            resolved_definition_identity,
            f"设备驱动源码身份没有解析为类: {source_identity}",
        )

    try:
        # ``driver_params`` 是实例配置与注册表强制值恰好一次合并的隔离结果。
        driver_params = merge_init_param_enforce(
            runtime_config,
            registry_entry.get("init_param_enforce"),
        )
        return PythonDriverActivation(
            definition_identity=resolved_definition_identity,
            source_identity=source_identity,
            content_hash=package_evidence[1],
            package_catalog_digest=package_evidence[2],
            driver_class=driver_class,
            driver_params=driver_params,
            status_types=status_types,
            action_value_mappings=action_value_mappings,
            hardware_interface=hardware_interface,
            driver_is_ros=class_mapping.get("type") == "ros2",
        )
    except DriverActivationError:
        raise
    except Exception as error:
        raise DriverActivationError(
            "activation_config_error",
            resolved_definition_identity,
            "设备驱动运行配置无法安全冻结",
        ) from error


def _validate_package_evidence(
    registry_entry: Mapping[str, Any],
    *,
    definition_identity: str,
    source_identity: str,
) -> tuple[str | None, str | None, str | None]:
    """关闭式验证包托管条目的定义、源码与摘要证据。

    参数：``registry_entry`` 是解析后的设备条目；``definition_identity`` 是请求
    身份；``source_identity`` 是已通过语法门禁的驱动源码身份。
    返回：规范包定义 FQID、内容摘要和包目录摘要；内置条目返回三个空值。
    异常：只要任一包证据字段出现，其他字段缺失、摘要非法、条目身份不一致或
    ``source_fqid`` 与 ``class.module`` 不一致时抛出 ``DriverActivationError``。
    """

    if not any(field in registry_entry for field in _PACKAGE_EVIDENCE_FIELDS):
        return None, None, None
    # ``evidence`` 是包托管定义必须原子齐备的四项静态来源证明。
    evidence = {field: registry_entry.get(field) for field in _PACKAGE_EVIDENCE_FIELDS}
    if any(not isinstance(value, str) or not value for value in evidence.values()):
        raise DriverActivationError(
            "package_evidence_incomplete",
            definition_identity,
            "包托管设备定义缺少完整身份或摘要证据",
        )
    package_definition_fqid = evidence["package_definition_fqid"]
    content_hash = evidence["content_hash"]
    package_catalog_digest = evidence["package_catalog_digest"]
    source_fqid = evidence["source_fqid"]
    if not _DIGEST.fullmatch(content_hash) or not _DIGEST.fullmatch(
        package_catalog_digest
    ):
        raise DriverActivationError(
            "package_evidence_incomplete",
            definition_identity,
            "包托管设备定义的内容摘要格式非法",
        )
    if source_fqid != source_identity:
        raise DriverActivationError(
            "package_source_mismatch",
            definition_identity,
            "包托管设备定义的 source_fqid 与 class.module 不一致",
        )
    entry_identity = registry_entry.get("id")
    if not isinstance(entry_identity, str) or entry_identity != package_definition_fqid:
        raise DriverActivationError(
            "package_evidence_incomplete",
            definition_identity,
            "包托管设备定义的条目 ID 与 package_definition_fqid 不一致",
        )
    return package_definition_fqid, content_hash, package_catalog_digest


def _resolved_definition_identity(
    registry_entry: Mapping[str, Any],
    *,
    definition_identity: str,
    package_definition_fqid: str | None,
) -> str:
    """选择激活结果携带的稳定设备定义身份。

    参数：``registry_entry`` 是解析条目；``definition_identity`` 是精确请求键；
    ``package_definition_fqid`` 是可选包定义身份。
    返回：优先包 FQID，其次条目 ID，最后精确注册表请求键。
    异常：显式条目 ID 存在但为空或非字符串时抛出 ``DriverActivationError``。
    """

    if package_definition_fqid is not None:
        return package_definition_fqid
    entry_identity = registry_entry.get("id", definition_identity)
    if not isinstance(entry_identity, str) or not entry_identity:
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            "内置设备注册表条目缺少稳定身份",
        )
    return entry_identity


def _valid_source_identity(value: Any) -> bool:
    """判断值是否为绝对 Python ``module:symbol`` 身份。

    参数：``value`` 是注册表 ``class.module`` 值。
    返回：模块路径各段和符号均为非关键字 Python 标识符时返回 ``True``。
    异常：无。
    """

    if not isinstance(value, str) or value.count(":") != 1:
        return False
    module, symbol = value.split(":", 1)
    return (
        bool(module)
        and _valid_identifier(symbol)
        and all(_valid_identifier(part) for part in module.split("."))
    )


def _valid_identifier(value: str) -> bool:
    """判断一个源码身份片段是否为非关键字 Python 标识符。

    参数：``value`` 是模块路径片段或类符号。
    返回：合法时为 ``True``，否则为 ``False``。
    异常：无。
    """

    return value.isidentifier() and not keyword.iskeyword(value)


def _contract_mapping(
    class_mapping: Mapping[str, Any],
    key: str,
    *,
    definition_identity: str,
    default: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """关闭式读取并隔离一个设备类映射合同。

    参数：``class_mapping`` 是设备类合同；``key`` 是待读取字段；
    ``definition_identity`` 是错误关联的设备定义身份；``default`` 只在键完全
    缺失时使用，不能替换显式 ``None`` 或其他非法类型。
    返回：原映射或缺键默认映射的深复制普通字典。
    异常：显式值不是映射或内容无法隔离复制时抛出 ``DriverActivationError``。
    """

    # ``contract_value`` 保留“键缺失”与“显式空值”的区别，防止非法合同降级。
    contract_value = class_mapping.get(key, default or {})
    if not isinstance(contract_value, Mapping):
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            f"设备注册表 class.{key} 必须是对象",
        )
    try:
        return copy.deepcopy(dict(contract_value))
    except Exception as error:
        raise DriverActivationError(
            "invalid_registry_entry",
            definition_identity,
            f"设备注册表 class.{key} 无法隔离复制",
        ) from error
