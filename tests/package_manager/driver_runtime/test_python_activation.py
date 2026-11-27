"""Python 驱动激活（Python Driver Activation）的关闭式行为测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.package_manager.driver_runtime import (
    DriverActivationError,
    activate_python_driver,
)


class _Driver:
    """不建立设备连接的已选驱动类。"""


class _Registry:
    """只实现驱动运行时所需解析接口的测试注册表 Adapter。"""

    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        """保存测试定义，并记录解析请求。

        参数：``entries`` 是规范身份和唯一短名可见的设备定义集合。
        返回：无。
        异常：无。
        """

        self.entries = entries
        self.requests: list[tuple[str, str]] = []

    def resolve_definition(self, kind: str, identity: str) -> dict[str, Any]:
        """按设备身份返回隔离定义，模拟实时注册表统一解析器。

        参数：``kind`` 是定义种类；``identity`` 是规范全限定身份或唯一短名。
        返回：与内部容器隔离的设备定义。
        异常：身份缺失时抛出 ``KeyError``，歧义短名时抛出 ``ValueError``。
        """

        self.requests.append((kind, identity))
        if identity == "ambiguous":
            raise ValueError("设备定义短名歧义")
        try:
            return deepcopy(self.entries[identity])
        except KeyError:
            raise KeyError(identity) from None


def _package_entry() -> dict[str, Any]:
    """构造带完整包证据的设备定义。

    参数：无。
    返回：包含规范定义身份、源码身份和两个摘要的独立注册表条目。
    异常：无。
    """

    return {
        "id": "community.demo.heater",
        "package_definition_fqid": "community.demo.heater",
        "source_fqid": "demo.heater:Heater",
        "content_hash": "sha256:" + "1" * 64,
        "package_catalog_digest": "sha256:" + "2" * 64,
        "init_param_enforce": {"transport": {"baudrate": 115200}},
        "class": {
            "module": "demo.heater:Heater",
            "type": "python",
            "status_types": {"temperature": "float"},
            "action_value_mappings": {"heat": {"goal": {"value": "value"}}},
            "hardware_interface": {
                "name": "hardware_interface",
                "write": "send_command",
                "read": "read_data",
                "extra_info": [],
            },
        },
    }


@pytest.mark.parametrize(
    "definition_identity",
    ("community.demo.heater", "heater"),
    ids=("canonical-fqid", "unique-short-name"),
)
def test_fqid_and_unique_short_name_activate_the_same_selected_driver(
    definition_identity: str,
) -> None:
    """规范全限定身份与唯一短名经同一注册表解析并只加载选中驱动。

    参数：``definition_identity`` 是物理图采用的规范或兼容设备身份。
    返回：无；断言稳定身份、来源证据、驱动合同和覆盖后的配置完整返回。
    异常：身份解析或激活语义漂移时断言失败。
    """

    entry = _package_entry()
    registry = _Registry(
        {
            "community.demo.heater": entry,
            "heater": entry,
        }
    )
    loaded_sources: list[str] = []

    def load_selected(source_identity: str) -> type[_Driver]:
        """记录唯一被加载的源码身份并返回测试驱动类。

        参数：``source_identity`` 是 ``module:symbol`` 驱动源码身份。
        返回：测试驱动类。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    # ``runtime_config`` 是设备实例配置；注册表强制值必须最后覆盖它。
    runtime_config = {
        "transport": {"port": "COM1", "baudrate": 9600},
        "operator": "alice",
    }
    activation = activate_python_driver(
        registry,
        definition_identity,
        runtime_config,
        loader=load_selected,
    )

    assert registry.requests == [("device", definition_identity)]
    assert loaded_sources == ["demo.heater:Heater"]
    assert activation.definition_identity == "community.demo.heater"
    assert activation.source_identity == "demo.heater:Heater"
    assert activation.content_hash == "sha256:" + "1" * 64
    assert activation.package_catalog_digest == "sha256:" + "2" * 64
    assert activation.driver_class is _Driver
    assert activation.driver_params == {
        "transport": {"port": "COM1", "baudrate": 115200},
        "operator": "alice",
    }
    assert activation.status_types == {"temperature": "float"}
    assert activation.action_value_mappings == {"heat": {"goal": {"value": "value"}}}
    assert activation.hardware_interface["write"] == "send_command"
    assert activation.driver_is_ros is False
    assert runtime_config["transport"]["baudrate"] == 9600


@pytest.mark.parametrize(
    ("identity", "expected_code"),
    (
        ("missing", "definition_resolution_error"),
        ("ambiguous", "definition_resolution_error"),
    ),
    ids=("missing", "ambiguous"),
)
def test_definition_resolution_fails_before_loading_any_driver(
    identity: str,
    expected_code: str,
) -> None:
    """缺失或歧义定义必须在作者驱动模块加载前关闭式失败。

    参数：``identity`` 是非法选择；``expected_code`` 是稳定错误分类。
    返回：无；断言加载器零调用且原解析异常保留为 cause。
    异常：若任意选择或提前导入作者模块则断言失败。
    """

    registry = _Registry({})
    loaded_sources: list[str] = []

    def reject_load(source_identity: str) -> type[_Driver]:
        """记录不应发生的作者模块加载。

        参数：``source_identity`` 是意外请求的源码身份。
        返回：测试驱动类，仅为满足接口。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(registry, identity, {}, loader=reject_load)

    assert caught.value.code == expected_code
    assert caught.value.__cause__ is not None
    assert loaded_sources == []


@pytest.mark.parametrize(
    "missing_evidence",
    (
        "package_definition_fqid",
        "content_hash",
        "package_catalog_digest",
        "source_fqid",
    ),
)
def test_package_managed_entry_requires_complete_evidence_before_loading(
    missing_evidence: str,
) -> None:
    """任一包证据缺失都必须在驱动加载前关闭式失败。

    参数：``missing_evidence`` 是本例删除的包证据字段。
    返回：无；断言得到稳定证据错误且加载器零调用。
    异常：若残缺条目被误判为内置定义或继续激活，则断言失败。
    """

    entry = _package_entry()
    entry.pop(missing_evidence)
    registry = _Registry({"community.demo.heater": entry})
    loaded_sources: list[str] = []

    def reject_load(source_identity: str) -> type[_Driver]:
        """记录不应发生的残缺包驱动加载。

        参数：``source_identity`` 是意外请求的驱动源码身份。
        返回：测试驱动类，仅为满足接口。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            registry,
            "community.demo.heater",
            {},
            loader=reject_load,
        )

    assert caught.value.code == "package_evidence_incomplete"
    assert loaded_sources == []


def test_only_the_selected_package_driver_is_loaded() -> None:
    """完整注册表中未被物理图选择的包驱动保持零导入。

    参数：无。
    返回：无；断言加载器只收到选中定义的源码身份。
    异常：若驱动运行时枚举或预加载其他定义，则断言失败。
    """

    selected = _package_entry()
    idle = _package_entry()
    idle["id"] = "community.demo.idle"
    idle["package_definition_fqid"] = "community.demo.idle"
    idle["source_fqid"] = "demo.idle:Idle"
    idle["class"]["module"] = "demo.idle:Idle"
    registry = _Registry(
        {
            "community.demo.heater": selected,
            "community.demo.idle": idle,
        }
    )
    loaded_sources: list[str] = []

    def load_selected(source_identity: str) -> type[_Driver]:
        """记录被物理图选中的唯一驱动源码。

        参数：``source_identity`` 是选中驱动源码身份。
        返回：测试驱动类。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    activate_python_driver(
        registry,
        "community.demo.heater",
        {},
        loader=load_selected,
    )

    assert loaded_sources == ["demo.heater:Heater"]


def test_loader_result_must_be_a_class() -> None:
    """加载器返回函数或实例时必须产生稳定非类错误。

    参数：无。
    返回：无；断言错误分类不依赖底层对象 repr。
    异常：若非类对象进入 ROS 组合根则断言失败。
    """

    registry = _Registry({"community.demo.heater": _package_entry()})

    def load_function(_source_identity: str) -> object:
        """返回非法普通对象以验证类门禁。

        参数：``_source_identity`` 是已验证的驱动源码身份。
        返回：不具备类身份的普通对象。
        异常：无。
        """

        return object()

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            registry,
            "community.demo.heater",
            {},
            loader=load_function,
        )

    assert caught.value.code == "driver_not_class"


def test_loader_exception_is_wrapped_and_preserves_cause() -> None:
    """作者模块导入异常被稳定包装，并保留原异常供诊断。

    参数：无。
    返回：无；断言错误 code 与 cause 均稳定可见。
    异常：若导入异常泄漏成不稳定类型或 cause 丢失则断言失败。
    """

    registry = _Registry({"community.demo.heater": _package_entry()})
    import_failure = ImportError("driver dependency missing")

    def fail_load(_source_identity: str) -> type[_Driver]:
        """模拟选中作者驱动的依赖导入失败。

        参数：``_source_identity`` 是已验证的驱动源码身份。
        返回：不会返回。
        异常：始终抛出本例固定 ``ImportError``。
        """

        raise import_failure

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            registry,
            "community.demo.heater",
            {},
            loader=fail_load,
        )

    assert caught.value.code == "driver_load_error"
    assert caught.value.__cause__ is import_failure


def test_enforced_config_is_merged_once_without_mutating_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表强制配置恰好合并一次，并覆盖运行时同名值。

    参数：``monkeypatch`` 记录驱动运行时调用既有合并器的次数。
    返回：无；断言合并器一次调用、输入隔离且注册表值优先。
    异常：重复合并、反向优先或修改来源配置时断言失败。
    """

    from unilabos.package_manager.driver_runtime import python_activation

    entry = _package_entry()
    registry = _Registry({"community.demo.heater": entry})
    calls: list[tuple[Any, Any]] = []
    original_merge = python_activation.merge_init_param_enforce

    def count_merge(config: Any, enforce: Any) -> dict[str, Any]:
        """记录一次配置合并并委派产品实现。

        参数：``config`` 是实例配置；``enforce`` 是注册表强制配置。
        返回：产品合并器生成的隔离字典。
        异常：透传产品合并异常。
        """

        calls.append((config, enforce))
        return original_merge(config, enforce)

    monkeypatch.setattr(python_activation, "merge_init_param_enforce", count_merge)
    runtime_config = {"transport": {"baudrate": 9600}}

    def load_driver(_source_identity: str) -> type[_Driver]:
        """返回已选测试驱动而不产生作者代码副作用。

        参数：``_source_identity`` 是已验证的驱动源码身份。
        返回：测试驱动类。
        异常：无。
        """

        return _Driver

    activation = activate_python_driver(
        registry,
        "community.demo.heater",
        runtime_config,
        loader=load_driver,
    )

    assert len(calls) == 1
    assert activation.driver_params == {"transport": {"baudrate": 115200}}
    assert runtime_config == {"transport": {"baudrate": 9600}}
    assert entry["init_param_enforce"] == {"transport": {"baudrate": 115200}}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("source_mismatch", "package_source_mismatch"),
        ("invalid_module", "invalid_registry_entry"),
        ("invalid_class_mapping", "invalid_registry_entry"),
    ),
)
def test_invalid_registry_contract_fails_before_loading(
    mutation: str,
    expected_code: str,
) -> None:
    """源码身份不一致或类映射非法时在作者代码加载前失败。

    参数：``mutation`` 选择破坏方式；``expected_code`` 是稳定错误分类。
    返回：无；断言驱动加载器零调用。
    异常：若非法合同进入加载阶段则断言失败。
    """

    entry = _package_entry()
    if mutation == "source_mismatch":
        entry["source_fqid"] = "demo.other:Other"
    elif mutation == "invalid_module":
        entry["class"]["module"] = "relative_or_missing_symbol"
        entry["source_fqid"] = "relative_or_missing_symbol"
    else:
        entry["class"] = "demo.heater:Heater"
    registry = _Registry({"community.demo.heater": entry})
    loaded_sources: list[str] = []

    def reject_load(source_identity: str) -> type[_Driver]:
        """记录非法合同触发的不应发生加载。

        参数：``source_identity`` 是意外加载身份。
        返回：测试驱动类，仅为满足接口。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            registry,
            "community.demo.heater",
            {},
            loader=reject_load,
        )

    assert caught.value.code == expected_code
    assert loaded_sources == []


@pytest.mark.parametrize(
    ("contract_field", "invalid_value"),
    (
        ("status_types", None),
        ("action_value_mappings", "not-a-mapping"),
        ("hardware_interface", ["not", "a", "mapping"]),
    ),
    ids=("status-types-none", "action-mappings-string", "hardware-interface-list"),
)
def test_explicit_invalid_driver_contract_mapping_fails_before_load_and_merge(
    monkeypatch: pytest.MonkeyPatch,
    contract_field: str,
    invalid_value: Any,
) -> None:
    """显式非法驱动合同不能被缺省值掩盖，并在加载与配置合并前失败。

    参数：``monkeypatch`` 监测配置合并；``contract_field`` 是被破坏的类合同键；
    ``invalid_value`` 是显式 ``None``、字符串或列表非法值。
    返回：无；断言稳定注册表错误、加载器零调用且合并器零调用。
    异常：若显式非法值被静默替换为默认合同或发生作者代码加载则断言失败。
    """

    from unilabos.package_manager.driver_runtime import python_activation

    entry = _package_entry()
    entry["class"][contract_field] = invalid_value
    registry = _Registry({"community.demo.heater": entry})
    loaded_sources: list[str] = []
    merge_calls: list[tuple[Any, Any]] = []

    def reject_load(source_identity: str) -> type[_Driver]:
        """记录非法合同后不应发生的作者驱动加载。

        参数：``source_identity`` 是意外请求的驱动源码身份。
        返回：测试驱动类，仅为满足接口。
        异常：无。
        """

        loaded_sources.append(source_identity)
        return _Driver

    def reject_merge(config: Any, enforce: Any) -> dict[str, Any]:
        """记录非法合同后不应发生的配置合并。

        参数：``config`` 是实例配置；``enforce`` 是注册表强制配置。
        返回：空配置，仅为满足合并接口。
        异常：无。
        """

        merge_calls.append((config, enforce))
        return {}

    monkeypatch.setattr(
        python_activation,
        "merge_init_param_enforce",
        reject_merge,
    )

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            registry,
            "community.demo.heater",
            {},
            loader=reject_load,
        )

    assert caught.value.code == "invalid_registry_entry"
    assert loaded_sources == []
    assert merge_calls == []


def test_builtin_registry_entry_needs_no_package_evidence() -> None:
    """稳定内置注册表条目无需伪造包摘要即可激活。

    参数：无。
    返回：无；断言内置身份和驱动参数仍经统一接口返回。
    异常：若新包证据规则破坏内置设备启动则断言失败。
    """

    registry = _Registry(
        {
            "builtin-heater": {
                "id": "builtin-heater",
                "init_param_enforce": {"limit": 80},
                "class": {
                    "module": "unilabos.devices.heater:Heater",
                    "type": "ros2",
                },
            }
        }
    )

    def load_builtin(_source_identity: str) -> type[_Driver]:
        """返回内置测试驱动类。

        参数：``_source_identity`` 是内置驱动源码身份。
        返回：测试驱动类。
        异常：无。
        """

        return _Driver

    activation = activate_python_driver(
        registry,
        "builtin-heater",
        {"limit": 100},
        loader=load_builtin,
    )

    assert activation.definition_identity == "builtin-heater"
    assert activation.content_hash is None
    assert activation.package_catalog_digest is None
    assert activation.driver_params == {"limit": 80}
    assert activation.driver_is_ros is True
