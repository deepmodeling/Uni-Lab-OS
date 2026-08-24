"""加热演示的物料初态 provision；不属于设备动作。"""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid5

from unilabos.devices.virtual.heating_platform import AMBIENT_TEMPERATURE_C
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.heating_demo import (
    HeatingScenarioEnvironment,
    HeatingScenarioId,
)
from unilabos.server.protocol.materials import MaterialDataWrite, MaterialMove
from unilabos.server.services.materials import (
    MaterialNotFoundError,
    MaterialsService,
)


class HeatingDemoProvisionService:
    """通过 materials.v1 重建三种场景的确定初态。"""

    def __init__(self, materials: MaterialsService) -> None:
        self.materials = materials

    @staticmethod
    def _command(request_uuid: str, effect: str) -> str:
        return str(uuid5(UUID(request_uuid), f"heating-demo:{effect}"))

    @classmethod
    def _mutation(
        cls,
        request_uuid: str,
        *,
        effect: str,
        operation: str,
    ) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=cls._command(request_uuid, effect),
            effect_key=f"heating-demo:{effect}",
            operation=operation,
            actor_type="demo_provisioner",
            actor_uuid="openlab-heating-demo",
        )

    def _platform(self, device_id: str):
        try:
            return self.materials.get_material_by_resource_id(device_id)
        except MaterialNotFoundError as exc:
            raise MaterialNotFoundError(
                f"heating demo device {device_id!r} is not provisioned"
            ) from exc

    def _samples(self, device_id: str) -> list:
        return [
            self.materials.get_material_by_resource_id(
                f"{device_id}-sample-{index}"
            )
            for index in range(1, 4)
        ]

    def reset(
        self,
        scenario_id: HeatingScenarioId,
        *,
        request_uuid: str,
        source_device_id: str,
        target_device_id: str,
    ) -> HeatingScenarioEnvironment:
        """两阶段恢复位置，再刷新设备 resource projection。"""

        source = self._platform(source_device_id)
        source_sites = sorted(source.sites, key=lambda item: int(item.site_index))
        if len(source_sites) != 3:
            raise RuntimeError("source heating platform requires exactly three sites")
        source_samples = self._samples(source_device_id)

        target = None
        target_sites: list = []
        target_samples: list = []
        if scenario_id == "cross_device_transfer":
            if source_device_id == target_device_id:
                raise ValueError("cross-device scenario requires two device ids")
            target = self._platform(target_device_id)
            target_sites = sorted(
                target.sites,
                key=lambda item: int(item.site_index),
            )
            if len(target_sites) != 3:
                raise RuntimeError("target heating platform requires exactly three sites")
            target_samples = self._samples(target_device_id)

        desired: dict[str, str | None] = {}
        if scenario_id == "parallel_three_site":
            for sample, site in zip(source_samples, source_sites):
                desired[sample.material.material_uuid] = site.site_uuid
        else:
            desired[source_samples[0].material.material_uuid] = source_sites[0].site_uuid
            for sample in source_samples[1:]:
                desired[sample.material.material_uuid] = None

        if target is not None:
            desired[target_samples[0].material.material_uuid] = target_sites[0].site_uuid
            desired[target_samples[1].material.material_uuid] = target_sites[1].site_uuid
            desired[target_samples[2].material.material_uuid] = None

        platforms = [source] + ([target] if target is not None else [])
        occupied = {
            site.occupied_material_uuid: site
            for platform in platforms
            for site in platform.sites
            if site.occupied_material_uuid
        }

        # 先卸载全部错误位置，再装载，避免交换位置时出现短暂双占位。
        for material_uuid, destination_uuid in desired.items():
            current = occupied.get(material_uuid)
            if current is not None and current.site_uuid != destination_uuid:
                self.materials.move_material(
                    self._mutation(
                        request_uuid,
                        effect=f"unmount:{material_uuid}",
                        operation="move_material",
                    ),
                    MaterialMove(material_uuid=material_uuid),
                )

        current_sites = {
            site.site_uuid: site
            for platform_uuid in [source.material.material_uuid]
            + ([target.material.material_uuid] if target is not None else [])
            for site in self.materials.get_material(platform_uuid).sites
        }
        for material_uuid, destination_uuid in desired.items():
            if destination_uuid is None:
                continue
            destination = current_sites[destination_uuid]
            if destination.occupied_material_uuid != material_uuid:
                self.materials.move_material(
                    self._mutation(
                        request_uuid,
                        effect=f"mount:{material_uuid}:{destination_uuid}",
                        operation="move_material",
                    ),
                    MaterialMove(
                        material_uuid=material_uuid,
                        destination_site_uuid=destination_uuid,
                    ),
                )

        material_versions: dict[str, int] = {}
        sample_index = {
            sample.material.material_uuid: index
            for samples in (source_samples, target_samples)
            for index, sample in enumerate(samples, start=1)
        }
        for material_uuid, site_uuid in desired.items():
            current = self.materials.get_material(material_uuid)
            site_id = sample_index[material_uuid] if site_uuid is not None else None
            data = deepcopy(current.data.data)
            data.update(
                {
                    "temperature_c": AMBIENT_TEMPERATURE_C,
                    "target_temperature_c": AMBIENT_TEMPERATURE_C,
                    "serialized_state": {
                        "site_id": site_id,
                        "temperature_c": AMBIENT_TEMPERATURE_C,
                        "target_temperature_c": AMBIENT_TEMPERATURE_C,
                        "progress": 0.0,
                        "state": "idle",
                    },
                }
            )
            if current.data.data == data and current.data.state_status == "ready":
                material_versions[material_uuid] = current.material.version
                continue
            result = self.materials.put_data(
                self._mutation(
                    request_uuid,
                    effect=f"temperature:{material_uuid}",
                    operation="put_data",
                ),
                material_uuid,
                MaterialDataWrite(
                    data=data,
                    substances=current.data.substances,
                    sites_initialized=current.data.sites_initialized,
                    unknown_counter=current.data.unknown_counter,
                    state_status="ready",
                    source_event_uuid=current.data.source_event_uuid,
                ),
            )
            material_versions[material_uuid] = result.data.material.version

        for device_id, platform in (
            (source_device_id, source),
            *(([(target_device_id, target)] if target is not None else [])),
        ):
            assert platform is not None
            self.materials.reconcile_device_projection(
                command_uuid=self._command(
                    request_uuid,
                    f"projection:{device_id}",
                ),
                device_id=device_id,
                root_material_uuid=platform.material.material_uuid,
            )

        transfer_material_uuid = (
            source_samples[0].material.material_uuid
            if scenario_id == "cross_device_transfer"
            else None
        )
        return HeatingScenarioEnvironment(
            scenario_id=scenario_id,
            source_device_id=source_device_id,
            target_device_id=target_device_id,
            source_platform_uuid=source.material.material_uuid,
            target_platform_uuid=(
                target.material.material_uuid if target is not None else None
            ),
            transfer_material_uuid=transfer_material_uuid,
            transfer_target_site_uuid=(
                target_sites[2].site_uuid if target is not None else None
            ),
            assignments=desired,
            material_versions=material_versions,
        )


__all__ = ["HeatingDemoProvisionService"]
