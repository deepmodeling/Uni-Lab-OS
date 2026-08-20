"""仅供 ``--test_mode`` 使用的可审计虚拟物料环境编排。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialDataWrite,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialPosition,
    MaterialSubstance,
    MaterialTreeCreate,
    SiteCreate,
)
from unilabos.server.protocol.virtual_environment import (
    VirtualEnvironmentCatalogRead,
    VirtualEnvironmentId,
    VirtualEnvironmentPresetRead,
    VirtualEnvironmentResetResult,
    VirtualEnvironmentState,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialValidationError,
    MaterialsService,
)


@dataclass(frozen=True)
class _MaterialSpec:
    key: str
    name: str
    template_name: str
    resource_type: str
    class_name: str
    barcode: str
    state: str
    substances: tuple[tuple[str, float, str, str], ...] = ()


@dataclass(frozen=True)
class _Preset:
    preset_id: VirtualEnvironmentId
    name: str
    description: str
    root_name: str
    materials: tuple[_MaterialSpec, ...]


PRESETS: dict[VirtualEnvironmentId, _Preset] = {
    "organic": _Preset(
        preset_id="organic",
        name="有机合成虚拟实验室",
        description="反应台、常用溶剂、还原剂、反应瓶与产物瓶。",
        root_name="有机合成虚拟反应台",
        materials=(
            _MaterialSpec(
                "thf",
                "无水 THF",
                "openlab_reagent_bottle",
                "container",
                "ReagentBottle",
                "OPENLAB-ORG-THF",
                "ready",
                (("Tetrahydrofuran", 500, "mL", "liquid"),),
            ),
            _MaterialSpec(
                "benzaldehyde",
                "苯甲醛",
                "openlab_reagent_bottle",
                "container",
                "ReagentBottle",
                "OPENLAB-ORG-BENZ",
                "ready",
                (("Benzaldehyde", 100, "mL", "liquid"),),
            ),
            _MaterialSpec(
                "nabh4",
                "硼氢化钠",
                "openlab_reagent_vial",
                "container",
                "ReagentVial",
                "OPENLAB-ORG-NABH4",
                "ready",
                (("Sodium borohydride", 25, "g", "solid"),),
            ),
            _MaterialSpec(
                "reactor",
                "反应瓶 R1",
                "openlab_reaction_flask",
                "container",
                "ReactionFlask",
                "OPENLAB-ORG-R1",
                "clean",
            ),
            _MaterialSpec(
                "product",
                "产物收集瓶",
                "openlab_product_vial",
                "container",
                "ProductVial",
                "OPENLAB-ORG-P1",
                "empty",
            ),
        ),
    ),
    "biology": _Preset(
        preset_id="biology",
        name="生物 PCR 虚拟实验室",
        description="DNA 样本、引物、PCR Mix、无核酸酶水与 PCR 板。",
        root_name="生物实验虚拟样本台",
        materials=(
            _MaterialSpec(
                "dna",
                "DNA 样本 A01",
                "openlab_sample_tube",
                "sample",
                "SampleTube",
                "OPENLAB-BIO-DNA-A01",
                "quality_checked",
                (("DNA sample", 50, "uL", "liquid"),),
            ),
            _MaterialSpec(
                "forward",
                "Forward Primer",
                "openlab_bio_reagent_tube",
                "container",
                "ReagentTube",
                "OPENLAB-BIO-FWD",
                "ready",
                (("Forward primer", 100, "uL", "liquid"),),
            ),
            _MaterialSpec(
                "reverse",
                "Reverse Primer",
                "openlab_bio_reagent_tube",
                "container",
                "ReagentTube",
                "OPENLAB-BIO-REV",
                "ready",
                (("Reverse primer", 100, "uL", "liquid"),),
            ),
            _MaterialSpec(
                "mix",
                "2× PCR Master Mix",
                "openlab_bio_reagent_tube",
                "container",
                "ReagentTube",
                "OPENLAB-BIO-MIX",
                "cold_storage",
                (("PCR Master Mix", 1000, "uL", "liquid"),),
            ),
            _MaterialSpec(
                "water",
                "Nuclease-free Water",
                "openlab_bio_reagent_tube",
                "container",
                "ReagentTube",
                "OPENLAB-BIO-WATER",
                "ready",
                (("Nuclease-free water", 1500, "uL", "liquid"),),
            ),
            _MaterialSpec(
                "plate",
                "96 孔 PCR 板",
                "openlab_pcr_plate",
                "container",
                "PCRPlate",
                "OPENLAB-BIO-PLATE-01",
                "sterile",
            ),
        ),
    ),
    "materials": _Preset(
        preset_id="materials",
        name="材料制备虚拟实验室",
        description="正极粉体、导电剂、粘结剂、溶剂与烧结样品。",
        root_name="材料研发虚拟样品台",
        materials=(
            _MaterialSpec(
                "lfp",
                "LiFePO4 正极粉",
                "openlab_powder_jar",
                "container",
                "PowderJar",
                "OPENLAB-MAT-LFP",
                "dry",
                (("LiFePO4", 100, "g", "solid"),),
            ),
            _MaterialSpec(
                "carbon",
                "导电炭黑",
                "openlab_powder_jar",
                "container",
                "PowderJar",
                "OPENLAB-MAT-CARBON",
                "dry",
                (("Carbon black", 25, "g", "solid"),),
            ),
            _MaterialSpec(
                "pvdf",
                "PVDF 粘结剂",
                "openlab_powder_jar",
                "container",
                "PowderJar",
                "OPENLAB-MAT-PVDF",
                "dry",
                (("PVDF", 50, "g", "solid"),),
            ),
            _MaterialSpec(
                "nmp",
                "NMP 溶剂",
                "openlab_reagent_bottle",
                "container",
                "ReagentBottle",
                "OPENLAB-MAT-NMP",
                "ready",
                (("N-Methyl-2-pyrrolidone", 500, "mL", "liquid"),),
            ),
            _MaterialSpec(
                "specimen",
                "烧结样品 S01",
                "openlab_specimen_box",
                "sample",
                "SpecimenBox",
                "OPENLAB-MAT-S01",
                "characterization_ready",
                (("Calcined specimen", 12.5, "g", "solid"),),
            ),
            _MaterialSpec(
                "reference",
                "NaCl 晶体标样",
                "openlab_specimen_box",
                "sample",
                "SpecimenBox",
                "OPENLAB-MAT-NACL",
                "reference",
                (("Sodium chloride", 8, "g", "solid"),),
            ),
        ),
    ),
}


class VirtualEnvironmentService:
    """用现有 materials.v1 命令重建物料树，不直接操作 SQLite 表。"""

    def __init__(self, materials: MaterialsService):
        self.materials = materials
        self._reset_lock = threading.RLock()

    @staticmethod
    def _preset_read(preset: _Preset) -> VirtualEnvironmentPresetRead:
        return VirtualEnvironmentPresetRead(
            preset_id=preset.preset_id,
            name=preset.name,
            description=preset.description,
            material_count=len(preset.materials) + 1,
            featured_materials=[item.name for item in preset.materials[:4]],
        )

    def current_state(self) -> VirtualEnvironmentState:
        materials = self.materials.list_materials()
        tagged_root = None
        for aggregate in materials:
            marker = aggregate.material.meta_data.get("openlab_virtual_environment")
            if aggregate.material.parent_material_uuid is None and isinstance(
                marker, dict
            ):
                tagged_root = aggregate
                break
        if tagged_root is None:
            return VirtualEnvironmentState(active_material_count=len(materials))
        marker = tagged_root.material.meta_data["openlab_virtual_environment"]
        preset_id = marker.get("preset_id")
        if preset_id not in PRESETS:
            return VirtualEnvironmentState(active_material_count=len(materials))
        return VirtualEnvironmentState(
            preset_id=preset_id,
            setup_uuid=str(marker.get("setup_uuid") or "") or None,
            root_material_uuid=tagged_root.material.material_uuid,
            active_material_count=len(materials),
            setup_at_ms=int(marker.get("setup_at_ms") or 0) or None,
        )

    def catalog(self, *, reset_allowed: bool) -> VirtualEnvironmentCatalogRead:
        return VirtualEnvironmentCatalogRead(
            reset_allowed=reset_allowed,
            current=self.current_state(),
            presets=[self._preset_read(PRESETS[key]) for key in PRESETS],
        )

    @staticmethod
    def _mutation(
        request_uuid: str,
        *,
        effect_key: str,
        operation: str,
        payload: Any,
        timestamp: int,
    ) -> InventoryMutation:
        body = payload.model_dump(mode="json", exclude_none=False)
        return InventoryMutation(
            command_uuid=request_uuid,
            effect_key=effect_key,
            operation=operation,
            actor_type="openlab-virtual-setup",
            actor_uuid="openlab-web",
            observed_at_ms=timestamp,
            payload=body,
        )

    @staticmethod
    def _tree(preset: _Preset, setup_uuid: str, timestamp: int) -> MaterialTreeCreate:
        suffix = setup_uuid.replace("-", "")[:12]
        root_template = f"openlab_virtual_bench_{preset.preset_id}"
        root_sites = [
            SiteCreate(
                template_name=root_template,
                site_index=index,
                label=f"{index + 1:02d} · {item.name}",
                occupied_client_ref=item.key,
                allowed_resource_categories=[item.resource_type],
                pose={
                    "x": 0.6 + (index % 3) * 1.75,
                    "y": 0.7 + (index // 3) * 1.7,
                    "z": 0.2,
                },
            )
            for index, item in enumerate(preset.materials)
        ]
        nodes = [
            MaterialNodeCreate(
                client_ref="bench",
                identity=MaterialIdentityWrite(
                    resource_id=f"openlab-demo:{preset.preset_id}:{suffix}:bench",
                    name=preset.root_name,
                    description=preset.description,
                    resource_type="container",
                    class_name="VirtualWorkbench",
                    template_name=root_template,
                    barcode=f"OPENLAB-{preset.preset_id.upper()}-BENCH",
                    barcode_symbology="CODE128",
                    meta_data={
                        "openlab_virtual_environment": {
                            "preset_id": preset.preset_id,
                            "setup_uuid": setup_uuid,
                            "setup_at_ms": timestamp,
                        }
                    },
                ),
                position=MaterialPosition(
                    size_depth=4.2,
                    size_width=6.2,
                    size_height=0.25,
                    layout="x-y",
                    cross_section_type="rounded_rectangle",
                ),
                data=MaterialDataWrite(
                    data={"preset_id": preset.preset_id, "virtual": True},
                    sites_initialized=True,
                    state_status="ready",
                    observed_at_ms=timestamp,
                ),
                sites=root_sites,
            )
        ]
        for index, item in enumerate(preset.materials):
            substances = [
                MaterialSubstance(
                    name=name,
                    quantity=quantity,
                    quantity_unit=unit,
                    physical_state=physical_state,
                    meta_data={"virtual": True},
                )
                for name, quantity, unit, physical_state in item.substances
            ]
            nodes.append(
                MaterialNodeCreate(
                    client_ref=item.key,
                    parent_client_ref="bench",
                    identity=MaterialIdentityWrite(
                        resource_id=f"openlab-demo:{preset.preset_id}:{suffix}:{item.key}",
                        name=item.name,
                        description="OpenLab virtual environment preset material",
                        resource_type=item.resource_type,
                        class_name=item.class_name,
                        template_name=item.template_name,
                        barcode=item.barcode,
                        barcode_symbology="CODE128",
                        meta_data={"virtual": True, "preset_id": preset.preset_id},
                    ),
                    position=MaterialPosition(
                        size_depth=0.8,
                        size_width=0.8,
                        size_height=1.2,
                        position3d_x=0.6 + (index % 3) * 1.75,
                        position3d_y=0.7 + (index // 3) * 1.7,
                        position3d_z=0.25,
                        cross_section_type="circle"
                        if "bottle" in item.template_name
                        or "tube" in item.template_name
                        else "rounded_rectangle",
                    ),
                    data=MaterialDataWrite(
                        data={"virtual": True, "preset_id": preset.preset_id},
                        substances=substances,
                        state_status=item.state,
                        observed_at_ms=timestamp,
                    ),
                )
            )
        return MaterialTreeCreate(nodes=nodes)

    def reset(
        self,
        preset_id: VirtualEnvironmentId,
        *,
        request_uuid: str,
    ) -> VirtualEnvironmentResetResult:
        preset = PRESETS.get(preset_id)
        if preset is None:
            raise MaterialValidationError(f"unknown virtual environment: {preset_id}")
        with self._reset_lock:
            current = self.current_state()
            if current.setup_uuid == request_uuid and current.preset_id == preset_id:
                return VirtualEnvironmentResetResult(
                    replayed=True,
                    deleted_root_count=0,
                    state=current,
                )

            timestamp = int(time.time() * 1000)
            roots = self.materials.list_materials(roots_only=True)
            deleted = 0
            for root in roots:
                value = MaterialDelete(
                    material_uuid=root.material.material_uuid,
                    recursive=True,
                )
                self.materials.delete_material(
                    self._mutation(
                        request_uuid,
                        effect_key=f"virtual-reset:delete:{root.material.material_uuid}",
                        operation="delete_material",
                        payload=value,
                        timestamp=timestamp,
                    ),
                    value,
                )
                deleted += 1

            tree = self._tree(preset, request_uuid, timestamp)
            result = self.materials.create_tree(
                self._mutation(
                    request_uuid,
                    effect_key=f"virtual-reset:create:{preset_id}",
                    operation="create_material_tree",
                    payload=tree,
                    timestamp=timestamp,
                ),
                tree,
            )
            state = self.current_state()
            if state.root_material_uuid != result.data.root_material_uuid:
                raise MaterialConflictError(
                    "virtual environment state was replaced concurrently"
                )
            return VirtualEnvironmentResetResult(
                deleted_root_count=deleted,
                state=state,
            )


__all__ = ["PRESETS", "VirtualEnvironmentService"]
