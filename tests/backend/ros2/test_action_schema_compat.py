from unilabos.backend.ros2.msgs.message_converter import (
    NavigateThroughPoses,
    ros_action_result_mapping,
    ros_action_to_json_schema,
)


def test_navigate_through_poses_result_contract_is_distro_independent() -> None:
    """Humble/Jazzy 的原始 Result 不同，但 Uni-Lab-OS 对外合同必须一致。"""
    native_fields = set(
        NavigateThroughPoses.Result.get_fields_and_field_types()
    )
    assert native_fields in ({"result"}, {"error_code", "error_msg"})

    assert ros_action_result_mapping(NavigateThroughPoses) == {}
    result_schema = ros_action_to_json_schema(NavigateThroughPoses)["properties"][
        "result"
    ]
    assert result_schema == {
        "title": "NavigateThroughPoses_Result",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
