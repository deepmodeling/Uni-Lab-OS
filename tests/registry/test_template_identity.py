"""设备内存目录确定性身份的单元测试。"""

from __future__ import annotations

import pytest

from unilabos.registry.template_identity import (
    TemplateIdentityError,
    action_handle_uuid,
    action_template_key,
    action_template_uuid,
    device_template_uuid,
)


def test_same_business_names_generate_the_same_uuids() -> None:
    """相同业务名在独立调用中应生成完全相同的三级身份。"""

    first_device = device_template_uuid("liquid_pump")
    second_device = device_template_uuid("liquid_pump")
    first_action = action_template_uuid("liquid_pump", "inject")
    second_action = action_template_uuid("liquid_pump", "inject")
    first_handle = action_handle_uuid(
        first_action,
        io_type="target",
        handle_key="liquid",
    )
    second_handle = action_handle_uuid(
        second_action,
        io_type="target",
        handle_key="liquid",
    )

    assert first_device == second_device
    assert first_action == second_action
    assert first_handle == second_handle
    assert action_template_key("liquid_pump", "inject") == "liquid_pump.inject"


def test_identity_dimensions_are_isolated() -> None:
    """设备、动作、方向或 Handle 业务键变化必须生成新身份。"""

    action_uuid = action_template_uuid("liquid_pump", "inject")

    assert device_template_uuid("liquid_pump") != action_uuid
    assert action_uuid != action_template_uuid("liquid_pump", "aspirate")
    assert action_uuid != action_template_uuid("powder_feeder", "inject")
    assert action_handle_uuid(
        action_uuid,
        io_type="source",
        handle_key="liquid",
    ) != action_handle_uuid(
        action_uuid,
        io_type="target",
        handle_key="liquid",
    )


@pytest.mark.parametrize(
    "invalid_name",
    ["", " leading", "trailing ", "bad\nname", "e\u0301"],
)
def test_invalid_business_name_fails_closed(invalid_name: str) -> None:
    """非规范业务名不得被隐式修正后参与持久身份计算。"""

    with pytest.raises(TemplateIdentityError):
        device_template_uuid(invalid_name)
