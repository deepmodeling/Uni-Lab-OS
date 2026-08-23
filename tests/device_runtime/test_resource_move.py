from __future__ import annotations

import asyncio
from uuid import uuid4

from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.client.materials import LocalMaterialsClient
from unilabos.device_runtime.resource import AuthorityResourceService
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=f"{operation}:{uuid4()}",
        operation=operation,
    )


def _template(
    service: MaterialsService,
    template_uuid: str,
    name: str,
    *,
    with_site: bool = False,
) -> None:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid=template_uuid,
            name=name,
            display_name=name,
            resource_type="container",
            class_name="Container",
            available_sites=(
                [{"index": 0, "label": "A1", "content_type": ["plate"]}]
                if with_site
                else []
            ),
        ),
    )


def _node(
    ref: str,
    template_name: str,
    *,
    parent: str | None = None,
) -> MaterialNodeCreate:
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        identity=MaterialIdentityWrite(
            resource_id=ref,
            name=ref,
            resource_type="container",
            class_name="Container",
            template_name=template_name,
        ),
    )


def test_authority_resource_service_moves_by_site_label(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    service = AuthorityResourceService(LocalMaterialsClient(materials))
    try:
        _template(materials, "deck-template", "deck", with_site=True)
        _template(materials, "tube-template", "tube")
        source = materials.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    _node("source", "deck"),
                    _node("tube", "tube", parent="source"),
                ]
            ),
        )
        target = materials.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(nodes=[_node("target", "deck")]),
        )
        material_uuid = source.data.client_ref_map["tube"]
        source_site_uuid = source.data.nodes[0].sites[0].site_uuid
        target_uuid = target.data.root_material_uuid
        target_site_uuid = target.data.nodes[0].sites[0].site_uuid
        materials.move_material(
            _mutation("move_material"),
            MaterialMove(
                material_uuid=material_uuid,
                destination_site_uuid=source_site_uuid,
            ),
        )

        moved = asyncio.run(
            service.move_resources(
                "source-device",
                "source-device-uuid",
                [material_uuid],
                [target_uuid],
                ["A1"],
            )
        )

        assert moved[0].material.parent_material_uuid == target_uuid
        assert materials.repository.get_site(source_site_uuid).occupied_material_uuid is None
        assert (
            materials.repository.get_site(target_site_uuid).occupied_material_uuid
            == material_uuid
        )
    finally:
        materials.repository.close()
