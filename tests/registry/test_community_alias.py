"""社区设备使用稳定完整命名空间，不再建立本地 alias。"""

from unilabos.package_manager.package_distribution.registry_discovery import (
    resolve_class_namespace,
)


def test_default_community_namespace_is_derived_from_normalized_project_name():
    """未指定命名空间时从规范化发行身份生成社区定义身份。

    参数：无。
    返回：无；断言默认身份稳定。
    异常：归一化或前缀规则漂移时测试失败。
    """

    assert (
        resolve_class_namespace("Vendor Liquid-Handler", None)
        == "community.vendor_liquid_handler"
    )


def test_explicit_namespace_gets_community_prefix():
    """显式短命名空间必须获得唯一社区前缀。

    参数：无。
    返回：无；断言短身份不会逃逸社区定义范围。
    异常：前缀规则漂移时测试失败。
    """

    assert resolve_class_namespace("ignored", "vendor.lh") == "community.vendor.lh"


def test_explicit_full_namespace_is_preserved():
    """已经完整限定的社区命名空间保持原身份。

    参数：无。
    返回：无；断言解析器不会重复增加前缀。
    异常：完整身份被改写时测试失败。
    """

    assert (
        resolve_class_namespace("ignored", "community.vendor.lh")
        == "community.vendor.lh"
    )
