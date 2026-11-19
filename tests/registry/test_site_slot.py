"""SiteSlot 槽位类型：registry 检测、placeholder 注入与 schema 生成契约。

SiteSlot 与 DeviceSlot 同型（str 子类，运行时不做实例装配）：
- 值语义：权威 ResourceSite 的 uuid；
- registry：@action 参数标注为 SiteSlot 时生成字符串 schema，
  并经 detect_placeholder_keys 自动注入 PLACEHOLDER_SITES，
  前端按 Site 选择器渲染。
"""

from unilabos.registry.placeholder_type import (
    PLACEHOLDER_DEVICES,
    PLACEHOLDER_RESOURCES,
    PLACEHOLDER_SITES,
    SiteSlot,
)
from unilabos.registry.utils.tools import (
    SLOT_TYPES,
    detect_placeholder_keys,
    detect_slot_type,
)


def test_site_slot_is_plain_str_subclass() -> None:
    assert issubclass(SiteSlot, str)
    value = SiteSlot("site-uuid-1")
    assert value == "site-uuid-1"
    assert "SiteSlot" in SLOT_TYPES


def test_detect_slot_type_recognizes_site_slot_variants() -> None:
    # AST 裸名 / runtime 完整路径
    assert detect_slot_type("SiteSlot") == ("SiteSlot", False)
    assert detect_slot_type(
        "unilabos.registry.placeholder_type:SiteSlot"
    ) == ("SiteSlot", False)
    # AST 复杂格式
    assert detect_slot_type("Optional[SiteSlot]") == ("SiteSlot", False)
    assert detect_slot_type("List[SiteSlot]") == ("SiteSlot", True)
    # runtime tuple 格式
    assert detect_slot_type(
        ("list", "unilabos.registry.placeholder_type:SiteSlot")
    ) == ("SiteSlot", True)


def test_detect_slot_type_existing_slots_not_affected() -> None:
    assert detect_slot_type("ResourceSlot") == ("ResourceSlot", False)
    assert detect_slot_type("DeviceSlot") == ("DeviceSlot", False)
    assert detect_slot_type("List[ResourceSlot]") == ("ResourceSlot", True)
    assert detect_slot_type("str") == (None, False)
    assert detect_slot_type("Dict[str, Any]") == (None, False)


def test_detect_placeholder_keys_maps_site_slot() -> None:
    params = [
        {"name": "resource", "type": "ResourceSlot"},
        {"name": "target_device", "type": "DeviceSlot"},
        {"name": "site", "type": "SiteSlot"},
        {"name": "volume", "type": "float"},
    ]
    assert detect_placeholder_keys(params) == {
        "resource": PLACEHOLDER_RESOURCES,
        "target_device": PLACEHOLDER_DEVICES,
        "site": PLACEHOLDER_SITES,
    }


def test_host_transfer_actions_declare_site_slot() -> None:
    """transfer_resource 的 site 参数应是 SiteSlot 注解（唯一定义在 HostServices）。"""

    import inspect

    from unilabos.backend.host_services import HostServices

    method = HostServices.transfer_resource
    parameters = inspect.signature(method).parameters
    assert parameters["site"].annotation is SiteSlot
