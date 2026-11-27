"""Optional ROS Resource fields must not trigger an empty backend lookup."""

from unilabos.ros.nodes.base_device_node import (
    _is_blank_resource_placeholder,
    _resource_lookup_identity,
)


def test_blank_resource_placeholder_has_no_lookup_identity() -> None:
    placeholder = {
        "id": "",
        "data": {},
        "name": "",
        "sample_id": "",
    }

    assert _resource_lookup_identity(placeholder) is None
    assert _is_blank_resource_placeholder(placeholder)
    assert not _is_blank_resource_placeholder({"id": "local-id", "data": {}})


def test_resource_lookup_prefers_backend_uuid_and_falls_back_to_id() -> None:
    assert _resource_lookup_identity(
        {"id": "local-id", "data": {"unilabos_uuid": "backend-uuid"}}
    ) == ("uuid", "backend-uuid")
    assert _resource_lookup_identity({"id": "local-id", "data": {}}) == (
        "id",
        "local-id",
    )
