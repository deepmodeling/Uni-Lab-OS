"""验证离散载架公开的一等库位（Site）身份合同。"""

from __future__ import annotations

import pytest
from pylabrobot.resources import Resource

from unilabos.resources.itemized_carrier import BottleCarrier

SITE_UUID = "61000000-0000-4000-8000-000000000001"
OTHER_SITE_UUID = "61000000-0000-4000-8000-000000000002"


def _site_descriptor(
    *,
    label: str,
    site_uuid: str,
) -> dict[str, object]:
    """构造带稳定身份的最小 PLR 库位描述。

    参数：``label`` 是设备局部库位名称，``site_uuid`` 是库存权威分配的稳定
    库位 UUID。返回：可由 ``BottleCarrier`` 直接反序列化的库位描述；本辅助
    函数不产生物理占用。
    """

    return {
        "label": label,
        "uuid": site_uuid,
        "visible": True,
        "occupied_by": None,
        "position": {"x": 0, "y": 0, "z": 0},
        "size": {"width": 10, "height": 10, "depth": 10},
        "content_type": ["container"],
    }


def _carrier(*site_descriptors: dict[str, object]) -> BottleCarrier:
    """用给定库位描述建立测试载架。

    参数：``site_descriptors`` 是待验证的一等库位目录。返回：没有初始占用物料
    的真实 PLR ``BottleCarrier``；描述非法时构造异常原样传播。
    """

    return BottleCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites=list(site_descriptors),
    )


def test_itemized_carrier_resolves_child_and_stable_site_uuid() -> None:
    """载架必须同时支持子物料与稳定 UUID 的局部库位反查。

    参数：无。返回：无；断言两条公开查询都收敛到同一设备局部名称。异常：
    任一身份无法由载架自身证明时测试失败。
    """

    carrier = _carrier(_site_descriptor(label="L1B1", site_uuid=SITE_UUID))
    # ``material`` 是机械臂取料动作最终收到的真实 PLR 物料对象。
    material = Resource(name="material", size_x=1, size_y=1, size_z=1)
    carrier["L1B1"] = material

    assert carrier.site_name_for_child(material) == "L1B1"
    assert carrier.site_name_for_uuid(SITE_UUID.upper()) == "L1B1"


def test_itemized_carrier_serializes_site_uuid_as_first_class_site_field() -> None:
    """稳定库位 UUID 必须随 ``sites[]`` 序列化而不是进入扩展字段。

    参数：无。返回：无；断言 UUID 位于对应库位描述，且载架不创建
    ``unilabos_site_name_by_uuid`` 扩展状态。
    """

    carrier = _carrier(_site_descriptor(label="L1B1", site_uuid=SITE_UUID))

    serialized = carrier.serialize()

    assert serialized["sites"][0]["uuid"] == SITE_UUID
    assert not hasattr(carrier, "unilabos_site_name_by_uuid")


@pytest.mark.parametrize(
    ("site_descriptors", "error_pattern"),
    [
        (
            [_site_descriptor(label="L1B1", site_uuid="not-a-uuid")],
            "库位 UUID",
        ),
        (
            [
                _site_descriptor(label="L1B1", site_uuid=SITE_UUID),
                _site_descriptor(label="L1B2", site_uuid=SITE_UUID),
            ],
            "库位 UUID 重复",
        ),
        (
            [
                _site_descriptor(label="L1B1", site_uuid=SITE_UUID),
                _site_descriptor(label="L1B1", site_uuid=OTHER_SITE_UUID),
            ],
            "库位名称重复",
        ),
    ],
)
def test_itemized_carrier_rejects_invalid_site_identity_directory(
    site_descriptors: list[dict[str, object]],
    error_pattern: str,
) -> None:
    """畸形或重复的一等库位身份必须在载架构造时失败关闭。

    参数：``site_descriptors`` 是不可信库位目录，``error_pattern`` 是预期中文
    拒绝原因。返回：无；断言错误目录不能进入动作运行时。
    """

    with pytest.raises(ValueError, match=error_pattern):
        _carrier(*site_descriptors)
