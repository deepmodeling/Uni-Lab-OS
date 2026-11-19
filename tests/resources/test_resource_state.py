from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pylabrobot.resources import Resource

from unilabos.resources.resource_tracker import (
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_META_DATA,
    ResourceDict,
    ResourceTreeSet,
)


def _resource_payload(**overrides):
    payload = {
        "id": "material",
        "uuid": str(uuid4()),
        "name": "material",
        "type": "container",
        "class": "",
        "config": {"type": "RegularContainer"},
        "data": {},
        "extra": {},
    }
    payload.update(overrides)
    return payload


def test_custom_state_uses_native_hooks_without_reserved_attribute_name(monkeypatch):
    class CustomStateResource(Resource):
        def __init__(
            self,
            name: str,
            size_x: float = 10,
            size_y: float = 10,
            size_z: float = 10,
            **kwargs,
        ):
            super().__init__(name, size_x, size_y, size_z, **kwargs)
            self.package_payload = {"material_id": "private-id"}

        def serialize_state(self):
            return {
                **super().serialize_state(),
                "device_package_state": dict(self.package_payload),
            }

        def load_state(self, state):
            super().load_state(state)
            self.package_payload = dict(state["device_package_state"])

    resource = CustomStateResource("material")
    states = resource.serialize_all_state()
    assert states[resource.name]["device_package_state"] == {
        "material_id": "private-id"
    }
    assert not hasattr(resource, "_unilabos_state")

    restored = CustomStateResource("material")
    restored.package_payload = {}
    restored.load_all_state(states)
    assert restored.package_payload == {"material_id": "private-id"}
    assert restored.serialize_all_state() == states

    resource.unilabos_uuid = str(uuid4())
    resource.unilabos_extra = {
        EXTRA_RESOURCE_CLASS: "CustomStateResource",
    }
    tree = ResourceTreeSet.from_plr_resources([resource])
    root = tree.root_nodes[0].res_content
    assert root.data["device_package_state"] == {"material_id": "private-id"}
    assert "_unilabos_state" not in root.data

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    roundtripped = tree.to_plr_resources(skip_devices=False)[0]
    assert isinstance(roundtripped, CustomStateResource)
    assert roundtripped.package_payload == {"material_id": "private-id"}
    assert not hasattr(roundtripped, "_unilabos_state")


def test_template_name_extra_is_promoted_to_root():
    resource = ResourceDict.model_validate(
        _resource_payload(
            **{
                "class": "IndependentMaterialClass",
                "extra": {EXTRA_RESOURCE_CLASS: "TubeTemplate"},
            }
        )
    )

    assert resource.template_name == "TubeTemplate"
    assert resource.klass == "IndependentMaterialClass"
    assert EXTRA_RESOURCE_CLASS not in resource.extra


def test_root_class_is_not_used_as_template_name():
    resource = ResourceDict.model_validate(
        _resource_payload(**{"class": "ShadowTemplate"})
    )

    assert resource.template_name == "RegularContainer"


def test_template_name_rejects_conflicting_extra():
    with pytest.raises(ValidationError, match="template_name.*extra.*冲突"):
        ResourceDict.model_validate(
            _resource_payload(
                template_name="CanonicalTemplate",
                extra={EXTRA_RESOURCE_CLASS: "SidecarTemplate"},
            )
        )


def test_resource_meta_data_is_kept_only_at_root():
    resource = ResourceDict.model_validate(
        _resource_payload(
            meta_data={"vendor": {"lot": "A-1"}, "priority": 2},
            config={"type": "RegularContainer", "driver": "virtual"},
            data={"runtime": "kept"},
        )
    )

    assert resource.meta_data == {"vendor": {"lot": "A-1"}, "priority": 2}
    assert "meta_data" not in resource.config
    assert "meta_data" not in resource.data
    assert EXTRA_RESOURCE_META_DATA not in resource.extra


def test_resource_meta_data_sidecar_is_promoted_and_removed():
    resource = ResourceDict.model_validate(
        _resource_payload(
            extra={
                EXTRA_RESOURCE_META_DATA: {"vendor": {"lot": "sidecar"}},
                "transport_trace": "kept",
            }
        )
    )

    assert resource.meta_data == {"vendor": {"lot": "sidecar"}}
    assert resource.extra == {"transport_trace": "kept"}


def test_resource_meta_data_nested_duplicates_are_checked_and_removed():
    meta_data = {"vendor": {"lot": "nested"}}
    resource = ResourceDict.model_validate(
        _resource_payload(
            meta_data=meta_data,
            config={"type": "RegularContainer", "meta_data": meta_data},
            data={"runtime": "kept", "meta_data": meta_data},
            extra={EXTRA_RESOURCE_META_DATA: meta_data},
        )
    )

    assert resource.meta_data == meta_data
    assert resource.config == {"type": "RegularContainer"}
    assert resource.data == {"runtime": "kept"}
    assert resource.extra == {}


def test_resource_meta_data_rejects_conflicting_sidecar():
    with pytest.raises(ValidationError, match="meta_data.*extra.*冲突"):
        ResourceDict.model_validate(
            _resource_payload(
                meta_data={"vendor": "canonical"},
                extra={EXTRA_RESOURCE_META_DATA: {"vendor": "stale"}},
            )
        )


def test_resource_meta_data_rejects_explicit_empty_root_against_nested_value():
    with pytest.raises(ValidationError, match="根字段 meta_data.*config.meta_data 冲突"):
        ResourceDict.model_validate(
            _resource_payload(
                meta_data={},
                config={
                    "type": "RegularContainer",
                    "meta_data": {"vendor": "nested"},
                },
            )
        )


def test_ros_transport_uuid_is_promoted_once_and_removed_from_data():
    transport_uuid = str(uuid4())
    resource = ResourceDict.model_validate(
        _resource_payload(
            uuid="",
            data={"unilabos_uuid": transport_uuid, "runtime": "kept"},
        )
    )

    assert resource.uuid == transport_uuid
    assert resource.data == {"runtime": "kept"}
    assert ResourceDict.model_validate(resource.model_dump(by_alias=True)).uuid == transport_uuid


def test_ros_transport_uuid_rejects_conflicting_root_uuid():
    with pytest.raises(ValidationError, match="uuid.*data.unilabos_uuid.*冲突"):
        ResourceDict.model_validate(
            _resource_payload(
                uuid=str(uuid4()),
                data={"unilabos_uuid": str(uuid4())},
            )
        )


def test_resource_without_backend_or_import_uuid_is_rejected():
    with pytest.raises(ValidationError, match="缺少 UUID"):
        ResourceDict.model_validate(_resource_payload(uuid=""))
