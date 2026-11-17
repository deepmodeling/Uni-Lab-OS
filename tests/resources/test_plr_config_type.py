"""PLR 反序列化必须使用 config.type，包括所有子物料。"""

from uuid import uuid4

import pytest
from pylabrobot.resources import Resource

from unilabos.resources.resource_tracker import ResourceTreeSet


def payload(config):
    return {
        "id": "sample", "name": "sample", "uuid": str(uuid4()),
        "type": "resource", "class": "Resource", "template_name": "Resource",
        "config": config, "children": [], "parent": None,
    }


def restore(data):
    def flatten(node):
        return [node, *(item for child in node["children"] for item in flatten(child))]

    return ResourceTreeSet.from_raw_dict_list(flatten(data)).to_plr_resources()[0]


@pytest.mark.parametrize("config", [{}, {"type": None}, {"type": ""}, {"type": "   "}, {"type": 1}, {"type": "MissingPLRClass"}])
def test_invalid_config_type_never_falls_back(config):
    data = payload(config)
    with pytest.raises(ValueError, match="config.type") as error:
        restore(data)
    assert data["uuid"] in str(error.value)
    assert "template_name" in str(error.value)


def test_config_type_overrides_business_fields():
    data = payload({"type": "Resource"})
    data.update(type="plate", template_name="not_a_python_class", **{"class": "not_a_class"})
    assert type(restore(data)) is Resource


def test_child_also_requires_config_type():
    data = payload({"type": "Resource"})
    child = payload({})
    child.update(id="child", name="child", parent=data["id"], parent_uuid=data["uuid"])
    data["children"] = [child]
    with pytest.raises(ValueError, match="config.type") as error:
        restore(data)
    assert child["uuid"] in str(error.value)
