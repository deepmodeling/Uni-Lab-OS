"""物料 ``class`` 的约定：初始化它用的注册表名；``config.type`` 是 PLR 类名（反序列化靠它）。

- 按注册表资源初始化的物料：class 是注册表资源名（与模板名相同），实现模块按名字在注册表里查；
- 没有注册表条目的孩子（孔位等）：class 是 PLR 类名；
- 设备：注册表设备名不变。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.registry.registry import lab_registry
from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import plr_resources_to_create
from unilabos.resources.material_class import material_class, resolve_material_class
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.core import MaterialValidationError
from tests.plr_fixtures import PLATE_MODULE, plate, registry_entry

PLATE_REGISTRY_NAME = "class_contract_plate"


@pytest.fixture
def plate_entry(monkeypatch):
    monkeypatch.setitem(lab_registry.resource_type_registry, PLATE_REGISTRY_NAME, registry_entry(PLATE_MODULE))
    return PLATE_REGISTRY_NAME


def _mutation(operation: str) -> InventoryMutation:
    command = str(uuid4())
    return InventoryMutation(command_uuid=command, effect_key=f"{operation}:{command}", operation=operation, actor_type="test")


def test_registry_material_class_is_its_registry_name_and_children_keep_the_plr_class(plate_entry) -> None:
    draft = materials.instantiate_registry_resource(plate_entry, "plate_a")

    tree = ResourceTreeSet.from_plr_resources([draft], known_random_uuid=True).trees[0]

    root = tree.root_node.res_content
    assert (root.klass, root.template_name, root.config["type"]) == (plate_entry, plate_entry, "Plate")
    well = tree.root_node.children[0].res_content
    assert (well.klass, well.config["type"]) == ("Well", "Well")


def test_plr_resource_without_registry_entry_falls_back_to_its_plr_class() -> None:
    tree = ResourceTreeSet.from_plr_resources([plate("loose")], known_random_uuid=True).trees[0]

    assert tree.root_node.res_content.klass == tree.root_node.res_content.config["type"] == "Plate"


def test_device_class_stays_the_registry_device_name() -> None:
    assert material_class(resource_type="device", klass="virtual_stirrer", template_name="x") == "virtual_stirrer"


def test_graph_nodes_written_with_the_plr_class_are_normalized_to_the_registry_name(plate_entry) -> None:
    """旧写法：class 写成 PLR 类名（= config.type），模板是注册表资源；入库时按模板换成注册表名。"""

    assert material_class(klass="Plate", template_name=plate_entry, plr_type="Plate") == plate_entry


@pytest.mark.parametrize(
    ("template_class", "identity_class", "plr_type", "stored"),
    [
        ("container", "container", "RegularContainer", "container"),
        # 升级前登记的注册表模板 class 还是 PLR 类名：物料带来注册表名照收
        ("RegularContainer", "container", "RegularContainer", "container"),
        # 查不到注册表的一方把 class 退回成了 PLR 类名：落模板的注册表名
        ("container", "RegularContainer", "RegularContainer", "container"),
        ("container", "Plate", "RegularContainer", None),
        (None, "anything", "Plate", "anything"),
    ],
)
def test_resolve_material_class(template_class, identity_class, plr_type, stored) -> None:
    assert (
        resolve_material_class(
            template_name="container", template_class=template_class, identity_class=identity_class, plr_type=plr_type
        )
        == stored
    )


def test_authority_stores_the_registry_name_for_a_registry_template(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    try:
        service.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(name="container", class_name="container", module_name="m.n:get_container"),
        )

        def create(class_name: str, plr_type: str) -> str:
            created = service.create_tree(
                _mutation("create_material_tree"),
                MaterialTreeCreate(
                    nodes=[
                        MaterialNodeCreate(
                            client_ref="c",
                            identity=MaterialIdentityWrite(
                                resource_id=f"c-{uuid4().hex[:6]}",
                                class_name=class_name,
                                template_name="container",
                                config={"type": plr_type},
                            ),
                        )
                    ]
                ),
            ).data
            return created.client_ref_map["c"]

        assert service.get_material(create("container", "RegularContainer")).material.class_name == "container"
        assert service.get_material(create("RegularContainer", "RegularContainer")).material.class_name == "container"
        with pytest.raises(MaterialValidationError, match="does not match template"):
            create("Plate", "RegularContainer")
    finally:
        service.close()


def test_registry_sync_writes_the_registry_name_as_the_template_class() -> None:
    from unilabos.resources.adapters.registry_materials import registry_definition_to_template

    template = registry_definition_to_template({"id": "container", **registry_entry("m.n:get_container")})

    assert (template.class_name, template.module_name) == ("container", "m.n:get_container")


def test_registry_entry_without_an_implementation_does_not_constrain_the_class() -> None:
    """条目没有 class.module 就没法按它初始化：模板不约束物料的 class。"""

    from unilabos.resources.adapters.registry_materials import registry_definition_to_template

    template = registry_definition_to_template({"id": "rack", "registry_type": "resource"})

    assert template.class_name is None


def test_create_request_carries_the_registry_name(plate_entry) -> None:
    request = plr_resources_to_create([materials.instantiate_registry_resource(plate_entry, "plate_b")])

    root = next(node for node in request.nodes if node.parent_client_ref is None)
    assert (root.identity.class_name, root.identity.template_name) == (plate_entry, plate_entry)
