"""ResourceSlot 参数识别：HostLink 与 ROS 2 共用，字符串注解与真实类型同样识别。"""

from __future__ import annotations

from typing import List, Optional

import pytest

from unilabos.backend.runtime.resource_slot import resource_slot_shape, resource_slot_shapes
from unilabos.registry.decorators import action
from unilabos.registry.placeholder_type import ResourceSlot


@pytest.mark.parametrize(
    ("annotation", "shape"),
    [
        (ResourceSlot, "single"),
        (Optional[ResourceSlot], "single"),
        (ResourceSlot | None, "single"),
        (list[ResourceSlot], "list"),
        (List[ResourceSlot], "list"),
        ("ResourceSlot", "single"),
        ("Optional[ResourceSlot]", "single"),
        ("list[ResourceSlot]", "list"),
        ("typing.List[ResourceSlot]", "list"),
        (str, None),
        ("str", None),
        (list[str], None),
        (dict, None),
    ],
)
def test_resource_slot_shape(annotation, shape):
    assert resource_slot_shape(annotation) == shape


class _Driver:
    # 本模块启用了 from __future__ import annotations：这里的注解全是字符串，
    # 与驱动包里常见写法一致（ROS 2 曾因此把 ResourceSlot 原样以 dict 传入驱动）。
    @action(contract="typed", always_free=False)
    def pick(self, sample: ResourceSlot, owners: list[ResourceSlot], site: str, note: Optional[str] = None) -> dict:
        return {}


def test_resource_slot_shapes_with_string_annotations_through_action_decorator():
    assert resource_slot_shapes(_Driver().pick) == {"sample": "single", "owners": "list"}


def test_resource_slot_shapes_tolerates_uninspectable_callables():
    assert resource_slot_shapes(print) == {}
