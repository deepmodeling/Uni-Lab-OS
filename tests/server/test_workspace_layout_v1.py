"""V1 工作区物料布局（AIW-07）移植到 dev 物料权威：源身份、preview 只读、apply 比较并交换。

对应 V1 ``tests/workspace_host/test_material_layout.py``：布局源从设备图文件换成 materials.db 物料树，
修订号从图文件字节摘要换成物料树快照状态哈希的汇总。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialPosition,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.protocol.runtime.process_management import ProcessManagementError
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.workspace_layout import (
    LAYOUT_CHANGE_SCHEMA,
    MaterialLayout,
    layout_revision,
)


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)


def _device(service: MaterialsService, resource_id: str, *, x: float, y: float, size: float) -> str:
    created = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref=resource_id,
            identity=MaterialIdentityWrite(
                resource_id=resource_id,
                # 根物料 name 唯一；展示名相同，且不参与身份
                name=f"{resource_id}-{uuid4().hex[:8]}",
                display_name="同名展示不参与身份",
                resource_type="device",
                class_name="Device",
                template_name="device",
            ),
            position=MaterialPosition(
                position_x=x, position_y=y, position_z=0, size_width=size, size_height=size, size_depth=size,
            ),
        )]),
    )
    return created.data.root_material_uuid


@pytest.fixture
def materials(tmp_path: Path):
    service = MaterialsService(tmp_path / "materials.db")
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(template_uuid="device-template", name="device", resource_type="device", class_name="Device"),
    )
    service.device_a = _device(service, "device-a", x=10, y=20, size=100)
    service.device_b = _device(service, "device-b", x=300, y=200, size=50)
    yield service
    service.close()


@pytest.fixture
def layout(materials: MaterialsService, tmp_path: Path) -> MaterialLayout:
    return MaterialLayout(materials, workspace=tmp_path)


def _positions(materials: MaterialsService, material_uuid: str) -> tuple:
    node = materials.get_tree(material_uuid).nodes[0]
    return (
        node.position.position_x, node.position.position_y, node.position.position_z,
        node.position.position3d_x, node.position.rotation_z,
    )


def test_preview_is_non_mutating_and_apply_writes_exact_proven_candidate(
    layout: MaterialLayout, materials: MaterialsService
) -> None:
    before = _positions(materials, materials.device_a)
    inspected = layout.inspect()
    revision = inspected["revision"]
    assert revision.startswith("sha256:")
    assert {node["resourceId"] for node in inspected["nodes"]} == {"device-a", "device-b"}
    preview = layout.preview(
        {
            "schemaVersion": LAYOUT_CHANGE_SCHEMA,
            # V1 图节点 id 在 dev 落成 resource_id：范围内唯一即可作为源身份
            "nodes": [{"sourceNodeId": "device-a", "positionMm": [120, 75, 5], "rotationDegXYZ": [0, 0, 90]}],
            "view": {"mode": "3d", "cameraPreset": "top", "viewport": {"width": 1200, "height": 800}},
        },
        expected_revision=revision,
    )

    assert _positions(materials, materials.device_a) == before
    assert layout.inspect()["revision"] == revision
    assert preview["sourceRevision"] == revision
    assert preview["structuralDiff"][0]["sourceNodeId"] == materials.device_a
    assert preview["changeSet"]["nodes"][0]["sourceNodeId"] == materials.device_a
    assert len(preview["previewId"]) == 64

    applied = layout.apply(preview, expected_revision=revision, command_uuid="apply-1")
    assert _positions(materials, materials.device_a) == (120.0, 75.0, 5.0, 120.0, 90.0)
    assert applied["revision"] == preview["candidateRevision"]
    assert applied["changedSourceNodeIds"] == [materials.device_a]
    assert [item["rootMaterialUuid"] for item in applied["applied"]] == [materials.device_a]
    assert layout.inspect()["revision"] == preview["candidateRevision"]


def test_apply_rejects_a_concurrent_edit(layout: MaterialLayout, materials: MaterialsService) -> None:
    revision = layout.inspect()["revision"]
    preview = layout.preview(
        {"schemaVersion": LAYOUT_CHANGE_SCHEMA, "nodes": [{"sourceNodeId": materials.device_b, "positionMm": [1, 2, 3]}]},
        expected_revision=revision,
    )
    # 另一个写者（人工 / 设备上报）在 preview 之后改了布局源
    other = MaterialLayout(materials)
    other_preview = other.preview(
        {"nodes": [{"sourceNodeId": materials.device_a, "positionMm": [9, 9, 9]}]}, expected_revision=revision
    )
    other.apply(other_preview, expected_revision=revision, command_uuid="other")

    with pytest.raises(ProcessManagementError) as caught:
        layout.apply(preview, expected_revision=revision, command_uuid="stale")
    assert caught.value.code == "layout_revision_conflict"
    # 带上新的修订号也不行：preview 的源修订已经过期
    current = layout.inspect()["revision"]
    with pytest.raises(ProcessManagementError) as caught:
        layout.apply(preview, expected_revision=current, command_uuid="stale-2")
    assert caught.value.code == "layout_revision_conflict"
    assert caught.value.details["previewId"] == preview["previewId"]
    assert _positions(materials, materials.device_b)[:3] == (300.0, 200.0, 0.0)


def test_preview_requires_the_current_revision(layout: MaterialLayout, materials: MaterialsService) -> None:
    with pytest.raises(ProcessManagementError) as caught:
        layout.preview(
            {"nodes": [{"sourceNodeId": materials.device_a, "positionMm": [0, 0, 0]}]},
            expected_revision="sha256:" + "0" * 64,
        )
    assert caught.value.code == "layout_revision_conflict"
    assert caught.value.details["actual"] == layout.inspect()["revision"]


def test_change_targets_source_identity_and_validates_template_asset(
    layout: MaterialLayout, materials: MaterialsService, tmp_path: Path
) -> None:
    asset = tmp_path / "models" / "device.glb"
    asset.parent.mkdir()
    asset.write_bytes(b"glTF")
    revision = layout.inspect()["revision"]
    preview = layout.preview(
        {
            "schemaVersion": LAYOUT_CHANGE_SCHEMA,
            "nodes": [{
                "sourceNodeId": materials.device_b,
                "assetRef": {
                    "path": "/api/v1/material-models/layout-lab/models/device.glb",
                    "sourcePath": "models/device.glb",
                    "format": "gltf",
                },
            }],
        },
        expected_revision=revision,
    )
    changed = next(node for node in preview["candidate"]["nodes"] if node["sourceNodeId"] == materials.device_b)
    assert changed["assetRef"]["path"] == "/api/v1/material-models/layout-lab/models/device.glb"
    assert "sourcePath" not in changed["assetRef"]
    layout.apply(preview, expected_revision=revision, command_uuid="asset")
    stored = materials.get_tree(materials.device_b).nodes[0].material.config
    assert stored["rendering"]["model"] == {
        "path": "/api/v1/material-models/layout-lab/models/device.glb", "format": "gltf",
    }

    for change, code in (
        # 展示名永不作为变更目标
        ({"sourceNodeId": "同名展示不参与身份", "positionMm": [0, 0, 0]}, "layout_source_not_found"),
        ({"sourceNodeId": materials.device_a, "assetRef": {"path": "/etc/passwd"}}, "layout_asset_invalid"),
        ({"sourceNodeId": materials.device_a,
          "assetRef": {"path": "/api/v1/material-models/a.glb", "sourcePath": "../outside.glb"}}, "layout_asset_not_found"),
        ({"sourceNodeId": materials.device_a, "positionMm": [0, 0]}, "layout_change_invalid"),
        ({"sourceNodeId": materials.device_a}, "layout_change_invalid"),
    ):
        with pytest.raises(ProcessManagementError) as caught:
            layout.preview({"nodes": [change]}, expected_revision=layout.inspect()["revision"])
        assert caught.value.code == code, change


def test_root_scope_limits_sources_and_revision(materials: MaterialsService, tmp_path: Path) -> None:
    scoped = MaterialLayout(materials, root_material_uuid=materials.device_b, workspace=tmp_path)
    scoped_revision = scoped.inspect()["revision"]
    assert scoped_revision == layout_revision([materials.get_tree(materials.device_b)])
    assert [node["resourceId"] for node in scoped.inspect()["nodes"]] == ["device-b"]
    # 范围外的节点（即使 resource_id 存在）不能作为目标
    with pytest.raises(ProcessManagementError) as caught:
        scoped.preview({"nodes": [{"sourceNodeId": "device-a", "positionMm": [1, 1, 1]}]}, expected_revision=scoped_revision)
    assert caught.value.code == "layout_source_not_found"
    preview = scoped.preview(
        {"nodes": [{"sourceNodeId": "device-b", "positionMm": [1, 1, 1]}]}, expected_revision=scoped_revision
    )
    assert preview["rootMaterialUuid"] == materials.device_b
    assert preview["changeSet"]["nodes"][0]["sourceNodeId"] == materials.device_b
    # 范围外的树变化不影响本范围的修订号
    whole = MaterialLayout(materials)
    other = whole.preview(
        {"nodes": [{"sourceNodeId": "device-a", "positionMm": [5, 5, 5]}]}, expected_revision=whole.inspect()["revision"]
    )
    whole.apply(other, expected_revision=other["sourceRevision"], command_uuid="outside")
    assert scoped.inspect()["revision"] == scoped_revision
    scoped.apply(preview, expected_revision=scoped_revision, command_uuid="inside")
    with pytest.raises(ProcessManagementError) as caught:
        MaterialLayout(materials, root_material_uuid="missing-root").inspect()
    assert caught.value.code == "layout_source_not_found"


def test_overlap_diagnostics_are_warnings(layout: MaterialLayout, materials: MaterialsService) -> None:
    revision = layout.inspect()["revision"]
    preview = layout.preview(
        {"nodes": [{"sourceNodeId": materials.device_b, "positionMm": [20, 30, 0]}]}, expected_revision=revision
    )
    assert [item["code"] for item in preview["diagnostics"]] == ["layout_bounds_overlap"]
    assert preview["diagnostics"][0]["severity"] == "warning"
    layout.apply(preview, expected_revision=revision, command_uuid="overlap")
