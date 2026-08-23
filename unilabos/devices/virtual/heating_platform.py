"""Three-site virtual heating platform used by the OpenLab demo.

The driver deliberately exercises the same boundaries as a physical driver:

* actions are dispatched through HostLink;
* scalar properties are published through the normal device-state bridge;
* the heating platform is the measurement authority and publishes telemetry;
* material identity, occupancy and the latest passive temperature projection
  are written through ``materials.v1`` rather than a private demo store.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
from threading import RLock
from typing import Any
from uuid import uuid4

from unilabos.device_runtime.action import ActionContext
from unilabos.hostlink.protocol import LinkError
from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionSize,
)
from unilabos.resources.objects.site import SiteDefinition
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialDataWrite,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPosition,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteCreate,
)
from unilabos.utils import logger


AMBIENT_TEMPERATURE_C = 25.0
SAMPLE_DISPLAY_COLORS = ("#2F80ED", "#9B51E0", "#27AE60")


VIRTUAL_HEATING_PLATFORM_SITES = [
    SiteDefinition(
        index=1,
        label="site_1",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=130.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=130.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["heating_sample"],
        parent_link="site_1",
        description="虚拟加热平台工位 1",
        meta_data={"site_id": 1, "role": "heating"},
    ),
    SiteDefinition(
        index=2,
        label="site_2",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=260.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=260.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["heating_sample"],
        parent_link="site_2",
        description="虚拟加热平台工位 2",
        meta_data={"site_id": 2, "role": "heating"},
    ),
    SiteDefinition(
        index=3,
        label="site_3",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=390.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=390.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["heating_sample"],
        parent_link="site_3",
        description="虚拟加热平台工位 3",
        meta_data={"site_id": 3, "role": "heating"},
    ),
]


@device(
    id="virtual_heating_platform",
    displayname="三工位虚拟加热平台",
    category=["virtual_device", "heating"],
    description="HostLink demo platform with three independently heated material sites",
    available_sites=VIRTUAL_HEATING_PLATFORM_SITES,
    supported_backends=["hostlink", "ros2"],
)
class VirtualHeatingPlatform:
    """A small deterministic simulator whose state remains materials-authoritative."""

    # Opt-in is intentionally explicit. HostLink still short-circuits every
    # physical driver while ``--test_mode`` is enabled.
    run_in_test_mode = True

    def __init__(
        self,
        device_id: str = "virtual_heating_platform",
        config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.device_id = device_id
        self.config = dict(config or {})
        self.update_interval_s = max(
            0.05, float(self.config.get("update_interval_s", 0.25))
        )
        self._lock = RLock()
        self._materials_lock = RLock()
        self._provision_lock = RLock()
        self._proof_started = False
        self._platform_uuid = ""
        self._sites: dict[int, dict[str, Any]] = {
            index: {
                "site_id": index,
                "site_uuid": "",
                "material_uuid": "",
                "material_name": "",
                "temperature_c": AMBIENT_TEMPERATURE_C,
                "target_temperature_c": AMBIENT_TEMPERATURE_C,
                "progress": 0.0,
                "state": "idle",
            }
            for index in range(1, 4)
        }

    @not_action
    def post_init(self, node: Any) -> None:
        """保存 HostLink/ROS2 共用节点，用于跨设备场景动作。"""

        self._device_node = node
        if node.backend_name == "ros2":
            self.initialize()

    @not_action
    def _start_scenario_proof(self) -> None:
        """物料初始化完成后至多启动一次 CI 场景证明。"""

        proof_file = os.environ.get("UNILABOS_HEATING_SCENARIO_PROOF_FILE")
        if not proof_file:
            return
        with self._lock:
            if self._proof_started:
                return
            self._proof_started = True
        threading.Thread(
            target=self._write_scenario_proof,
            args=(Path(proof_file),),
            name="heating-scenario-proof",
            daemon=True,
        ).start()

    @not_action
    def _write_scenario_proof(self, proof_file: Path) -> None:
        """为双 backend CI 依次执行三类场景并写出可机读终态。"""

        delay = float(os.environ.get("UNILABOS_HEATING_SCENARIO_START_DELAY", "1"))
        duration = float(os.environ.get("UNILABOS_HEATING_SCENARIO_DURATION", "0.1"))
        time.sleep(max(0.0, delay))
        try:
            results = {
                scenario_id: self.run_scenario(
                    scenario_id,
                    target,
                    duration,
                    ActionContext(action_id=f"scenario-proof:{scenario_id}"),
                )
                for scenario_id, target in (
                    ("single_sequential", 70.0),
                    ("parallel_three_site", 80.0),
                    ("cross_device_transfer", 90.0),
                )
            }
            proof = {
                "success": True,
                "backend": self._device_node.backend_name,
                "scenarios": results,
            }
        except Exception as exc:  # pragma: no cover - smoke 进程会回显日志
            logger.exception("三场景 smoke 执行失败")
            proof = {
                "success": False,
                "backend": self._device_node.backend_name,
                "error": f"{type(exc).__name__}: {exc}",
            }
        proof_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = proof_file.with_suffix(proof_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(proof, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(proof_file)

    @not_action
    def initialize(self) -> bool:
        try:
            self._provision_materials()
            self._start_scenario_proof()
        except LinkError as exc:
            # Demo Slave may intentionally start while the public Host is down.
            # HostLinkBackend calls on_hostlink_connected after reconnect.
            logger.info("[HeatingDemo] 物料初始化等待 HostLink 重连：%s", exc)
        return True

    @not_action
    def _provision_materials(self) -> None:
        with self._provision_lock, self._materials_lock:
            self._ensure_demo_materials()
            self._refresh_material_state()

    @not_action
    def on_hostlink_connected(self) -> None:
        """Idempotently materialize the demo after every HostLink reconnect."""

        self._provision_materials()
        self._start_scenario_proof()

    @staticmethod
    def _mutation(
        operation: str,
        effect_key: str,
        *,
        job_uuid: str | None = None,
    ) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=str(uuid4()),
            effect_key=effect_key,
            operation=operation,
            actor_type="virtual_device",
            actor_uuid="virtual_heating_platform",
            job_uuid=job_uuid,
            observed_at_ms=int(time.time() * 1000),
        )

    @staticmethod
    def _gateway() -> Any:
        from unilabos.resources.materials import resolve_materials_gateway

        return resolve_materials_gateway()

    @not_action
    def _ensure_template(
        self,
        name: str,
        *,
        category: list[str],
        available_sites: list[dict[str, Any]] | None = None,
    ) -> None:
        gateway = self._gateway()
        if any(template.name == name for template in gateway.list_templates()):
            return
        value = ResourceTemplateWrite(
            name=name,
            display_name=(
                "三工位虚拟加热平台"
                if name == "virtual_heating_platform"
                else "虚拟加热样品"
            ),
            class_name=(
                "VirtualHeatingPlatform"
                if name == "virtual_heating_platform"
                else "Resource"
            ),
            module_name=(
                "unilabos.devices.virtual.heating_platform"
                if name == "virtual_heating_platform"
                else None
            ),
            category=category,
            available_sites=list(available_sites or []),
            definition={"demo": "openlab-heating", "schema_version": 1},
        )
        gateway.create_template(
            self._mutation("put_template", f"ensure-template:{name}"), value
        )

    @not_action
    def _material_by_resource_id(self, resource_id: str) -> Any | None:
        try:
            return self._gateway().get_material_by_resource_id(resource_id)
        except (
            Exception
        ) as exc:  # different local/HTTP clients expose different 404 types
            if (
                "not found" in str(exc).lower()
                or getattr(exc, "status_code", None) == 404
            ):
                return None
            raise

    @not_action
    def _ensure_demo_materials(self) -> None:
        gateway = self._gateway()
        site_definitions = [
            {
                "schema_version": 1,
                "template_name": "virtual_heating_platform",
                "site_index": item.index,
                "label": item.label,
                "visible": item.visible,
                "pose": item.pose.model_dump(mode="json"),
                "allowed_resource_categories": item.allowed_resource_categories,
                "parent_link": item.parent_link,
                "description": item.description,
                "meta_data": item.meta_data,
                "extra": {},
            }
            for item in VIRTUAL_HEATING_PLATFORM_SITES
        ]
        self._ensure_template(
            "virtual_heating_platform",
            category=["virtual_device", "heating"],
            available_sites=site_definitions,
        )
        self._ensure_template("virtual_heating_sample", category=["heating_sample"])

        platform = self._material_by_resource_id(self.device_id)
        if platform is None:
            created = gateway.create_tree(
                self._mutation("create_material_tree", "create-platform"),
                MaterialTreeCreate(
                    nodes=[
                        MaterialNodeCreate(
                            client_ref="platform",
                            identity=MaterialIdentityWrite(
                                resource_id=self.device_id,
                                name="三工位虚拟加热平台",
                                description="OpenLab HostLink heating demo",
                                resource_type="resource",
                                class_name="VirtualHeatingPlatform",
                                template_name="virtual_heating_platform",
                                meta_data={"demo": "openlab-heating", "site_count": 3},
                            ),
                            data=MaterialDataWrite(
                                data={"serialized_state": {"site_count": 3}},
                                sites_initialized=True,
                                state_status="ready",
                            ),
                            sites=[
                                SiteCreate.model_validate(item)
                                for item in site_definitions
                            ],
                        )
                    ]
                ),
            )
            platform = created.data.nodes[0]
        self._platform_uuid = platform.material.material_uuid

        platform = gateway.get_material(self._platform_uuid)
        sites = sorted(platform.sites, key=lambda item: int(item.site_index))
        if len(sites) != 3:
            raise RuntimeError("virtual heating platform requires exactly three sites")

        samples: list[Any] = []
        for index in range(1, 4):
            sample_resource_id = f"{self.device_id}-sample-{index}"
            sample = self._material_by_resource_id(sample_resource_id)
            if sample is None:
                created = gateway.create_tree(
                    self._mutation(
                        "create_material_tree", f"create-sample:{sample_resource_id}"
                    ),
                    MaterialTreeCreate(
                        nodes=[
                            MaterialNodeCreate(
                                client_ref="sample",
                                identity=MaterialIdentityWrite(
                                    resource_id=sample_resource_id,
                                    name=f"加热样品 {index}",
                                    description=f"虚拟加热工位 {index} 的真实 materials.v1 物料",
                                    resource_type="resource",
                                    class_name="Resource",
                                    template_name="virtual_heating_sample",
                                    barcode=f"OPENLAB-HEAT-{index}",
                                    barcode_symbology="CODE128",
                                    meta_data={
                                        "demo": "openlab-heating",
                                        "site_id": index,
                                        "display_color": SAMPLE_DISPLAY_COLORS[
                                            index - 1
                                        ],
                                    },
                                ),
                                data=MaterialDataWrite(
                                    data={
                                        "temperature_c": AMBIENT_TEMPERATURE_C,
                                        "target_temperature_c": AMBIENT_TEMPERATURE_C,
                                        "temperature_source": {
                                            "device_id": self.device_id,
                                            "property": f"site_{index}_temperature_c",
                                        },
                                        "serialized_state": {
                                            "temperature_c": AMBIENT_TEMPERATURE_C,
                                            "state": "idle",
                                        },
                                    },
                                    state_status="ready",
                                ),
                                position=MaterialPosition(),
                            )
                        ]
                    ),
                )
                sample = created.data.nodes[0]
            samples.append(sample)

        # A previous demo may have left sample 1 on Site 3. Restore the
        # canonical all-sites layout in two phases so a stale Site snapshot or
        # a sample permutation cannot create a transient occupancy conflict.
        occupied_site_by_material = {
            site.occupied_material_uuid: site
            for site in sites
            if site.occupied_material_uuid
        }
        for index, sample in enumerate(samples, start=1):
            current = occupied_site_by_material.get(sample.material.material_uuid)
            if current is not None and int(current.site_index) != index:
                gateway.move_material(
                    self._mutation(
                        "move_material",
                        f"unmount-misplaced-sample:{index}:{uuid4().hex}",
                    ),
                    MaterialMove(material_uuid=sample.material.material_uuid),
                )

        platform = gateway.get_material(self._platform_uuid)
        sites = sorted(platform.sites, key=lambda item: int(item.site_index))
        for index, (sample, site) in enumerate(zip(samples, sites), start=1):
            if site.occupied_material_uuid != sample.material.material_uuid:
                if site.occupied_material_uuid is not None:
                    raise RuntimeError(f"heating site {index} is already occupied")
                gateway.move_material(
                    self._mutation(
                        "move_material", f"place-sample:{index}:{uuid4().hex}"
                    ),
                    MaterialMove(
                        material_uuid=sample.material.material_uuid,
                        destination_site_uuid=site.site_uuid,
                    ),
                )

    @not_action
    def _refresh_material_state(self) -> None:
        with self._materials_lock:
            self._refresh_material_state_locked()

    @not_action
    def _refresh_material_state_locked(self) -> None:
        if not self._platform_uuid:
            return
        gateway = self._gateway()
        platform = gateway.get_material(self._platform_uuid)
        with self._lock:
            for index, site in enumerate(
                sorted(platform.sites, key=lambda item: int(item.site_index)), start=1
            ):
                state = self._sites[index]
                state["site_uuid"] = site.site_uuid
                state["material_uuid"] = site.occupied_material_uuid or ""
                if site.occupied_material_uuid:
                    material = gateway.get_material(site.occupied_material_uuid)
                    state["material_name"] = material.material.name
                    state["temperature_c"] = float(
                        material.data.data.get("temperature_c", AMBIENT_TEMPERATURE_C)
                    )
                    state["target_temperature_c"] = float(
                        material.data.data.get(
                            "target_temperature_c", state["temperature_c"]
                        )
                    )
                    serialized = material.data.data.get("serialized_state", {})
                    if isinstance(serialized, dict):
                        state["state"] = str(serialized.get("state", state["state"]))
                else:
                    state.update(
                        {
                            "material_uuid": "",
                            "material_name": "",
                            "temperature_c": AMBIENT_TEMPERATURE_C,
                            "target_temperature_c": AMBIENT_TEMPERATURE_C,
                            "progress": 0.0,
                            "state": "idle",
                        }
                    )

    @action(
        description="重置演示物料库到单工位、三工位或转位场景的确定初态",
        goal_default={"preset": "all_sites"},
    )
    def reset_scenario(
        self,
        preset: str,
        action_context: ActionContext,
    ) -> dict[str, Any]:
        """通过 materials.v1 幂等恢复场景初态，不删除模板或历史账本。"""

        if preset not in {"single_site", "all_sites", "transfer"}:
            raise ValueError("preset must be single_site, all_sites or transfer")
        if not self._platform_uuid:
            self._provision_materials()
        gateway = self._gateway()
        platform = gateway.get_material(self._platform_uuid)
        sites = sorted(platform.sites, key=lambda item: int(item.site_index))
        samples = [
            gateway.get_material_by_resource_id(f"{self.device_id}-sample-{index}")
            for index in range(1, 4)
        ]
        desired_site_by_material = {
            sample.material.material_uuid: (
                sites[index - 1].site_uuid
                if preset == "all_sites" or index == 1
                else None
            )
            for index, sample in enumerate(samples, start=1)
        }
        occupied_site_by_material = {
            site.occupied_material_uuid: site
            for site in sites
            if site.occupied_material_uuid
        }

        # 先卸载不在目标位置的样品，再装载，避免临时占位冲突。
        for sample in samples:
            material_uuid = sample.material.material_uuid
            current = occupied_site_by_material.get(material_uuid)
            desired = desired_site_by_material[material_uuid]
            if current is not None and current.site_uuid != desired:
                gateway.move_material(
                    self._mutation(
                        "move_material",
                        f"scenario-reset-unmount:{preset}:{material_uuid}:{uuid4().hex}",
                        job_uuid=action_context.action_id,
                    ),
                    MaterialMove(material_uuid=material_uuid),
                )

        platform = gateway.get_material(self._platform_uuid)
        sites = sorted(platform.sites, key=lambda item: int(item.site_index))
        site_by_uuid = {site.site_uuid: site for site in sites}
        for sample in samples:
            material_uuid = sample.material.material_uuid
            desired = desired_site_by_material[material_uuid]
            if desired is None:
                continue
            destination = site_by_uuid[desired]
            if destination.occupied_material_uuid != material_uuid:
                gateway.move_material(
                    self._mutation(
                        "move_material",
                        f"scenario-reset-mount:{preset}:{material_uuid}:{uuid4().hex}",
                        job_uuid=action_context.action_id,
                    ),
                    MaterialMove(
                        material_uuid=material_uuid,
                        destination_site_uuid=desired,
                    ),
                )

        assignments: list[dict[str, Any]] = []
        for index, sample in enumerate(samples, start=1):
            current = gateway.get_material(sample.material.material_uuid)
            data = deepcopy(current.data.data)
            data.update(
                {
                    "temperature_c": AMBIENT_TEMPERATURE_C,
                    "target_temperature_c": AMBIENT_TEMPERATURE_C,
                    "temperature_observed_at_ms": int(time.time() * 1000),
                    "temperature_source": {
                        "device_id": self.device_id,
                        "property": f"site_{index}_temperature_c",
                    },
                    "serialized_state": {
                        "site_id": (
                            index
                            if desired_site_by_material[current.material.material_uuid]
                            else None
                        ),
                        "temperature_c": AMBIENT_TEMPERATURE_C,
                        "target_temperature_c": AMBIENT_TEMPERATURE_C,
                        "progress": 0.0,
                        "state": "idle",
                    },
                }
            )
            gateway.put_data(
                self._mutation(
                    "put_data",
                    f"scenario-reset-data:{preset}:{current.material.material_uuid}:{uuid4().hex}",
                    job_uuid=action_context.action_id,
                ),
                current.material.material_uuid,
                MaterialDataWrite(
                    data=data,
                    substances=current.data.substances,
                    sites_initialized=current.data.sites_initialized,
                    unknown_counter=current.data.unknown_counter,
                    state_status="ready",
                    source_event_uuid=current.data.source_event_uuid,
                    source_job_uuid=action_context.action_id,
                    observed_at_ms=int(time.time() * 1000),
                ),
            )
            assignments.append(
                {
                    "material_uuid": current.material.material_uuid,
                    "resource_id": current.material.resource_id,
                    "site_uuid": desired_site_by_material[
                        current.material.material_uuid
                    ],
                }
            )
        self._refresh_material_state()
        return {
            "success": True,
            "preset": preset,
            "assignments": assignments,
        }

    @action(
        description="把样品从一个加热工位权威转位到另一个空工位",
        goal_default={"source_site_id": 1, "target_site_id": 3},
    )
    def transfer_site(
        self,
        source_site_id: int,
        target_site_id: int,
        action_context: ActionContext,
    ) -> dict[str, Any]:
        if source_site_id not in self._sites or target_site_id not in self._sites:
            raise ValueError("site id must be 1, 2 or 3")
        if source_site_id == target_site_id:
            raise ValueError("source and target sites must differ")
        self._refresh_material_state()
        material_uuid = str(self._sites[source_site_id]["material_uuid"])
        if not material_uuid:
            raise RuntimeError(f"site {source_site_id} has no material")
        if self._sites[target_site_id]["material_uuid"]:
            raise RuntimeError(f"site {target_site_id} is occupied")
        result = self._gateway().move_material(
            self._mutation(
                "move_material",
                f"scenario-transfer:{source_site_id}:{target_site_id}:{uuid4().hex}",
                job_uuid=action_context.action_id,
            ),
            MaterialMove(
                material_uuid=material_uuid,
                destination_site_uuid=str(self._sites[target_site_id]["site_uuid"]),
            ),
        )
        self._refresh_material_state()
        return {
            "success": True,
            "material_uuid": material_uuid,
            "source_site_id": source_site_id,
            "target_site_id": target_site_id,
            "position_version": result.data.position_version,
        }

    @action(
        description="执行可重复的顺序、三工位并行或跨设备转位加热演示",
        goal_default={
            "scenario_id": "single_sequential",
            "target_temperature_c": 80.0,
            "duration_seconds": 0.8,
        },
        feedback_interval=0.25,
    )
    def run_scenario(
        self,
        scenario_id: str,
        target_temperature_c: float,
        duration_seconds: float,
        action_context: ActionContext,
    ) -> dict[str, Any]:
        """以同一动作输入供 HostLink/ROS2 运行三个端到端场景。"""

        if scenario_id not in {
            "single_sequential",
            "parallel_three_site",
            "cross_device_transfer",
        }:
            raise ValueError("unknown heating scenario")
        target = float(target_temperature_c)
        duration = float(duration_seconds)
        if not -20.0 <= target <= 250.0:
            raise ValueError("target_temperature_c must be between -20 and 250")
        if not 0.05 <= duration <= 3600.0:
            raise ValueError("duration_seconds must be between 0.05 and 3600")
        if scenario_id == "single_sequential":
            reset = self.reset_scenario("single_site", action_context)
            midpoint = round((AMBIENT_TEMPERATURE_C + target) / 2.0, 3)
            first = self.heat_site(1, midpoint, duration, action_context)
            second = self.heat_site(1, target, duration, action_context)
            steps: list[Any] = [reset, first, second]
        elif scenario_id == "parallel_three_site":
            reset = self.reset_scenario("all_sites", action_context)
            targets = [
                max(-20.0, min(250.0, target + offset)) for offset in (-10.0, 0.0, 10.0)
            ]
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(
                        self.heat_site,
                        site_id,
                        site_target,
                        duration,
                        action_context,
                    )
                    for site_id, site_target in enumerate(targets, start=1)
                ]
                parallel = [future.result() for future in futures]
            steps = [reset, *parallel]
        else:
            reset = self.reset_scenario("transfer", action_context)
            first_target = round((AMBIENT_TEMPERATURE_C + target) / 2.0, 3)
            peer = self._device_node.call_device_action(
                "virtual-workbench",
                "call_peer",
                {
                    "target_device": self.device_id,
                    "function_name": "heat_site",
                    "function_args": json.dumps(
                        {
                            "site_id": 1,
                            "target_temperature_c": first_target,
                            "duration_seconds": duration,
                        }
                    ),
                },
                timeout=max(15.0, duration + 10.0),
            )
            moved = self.transfer_site(1, 3, action_context)
            continued = self.heat_site(3, target, duration, action_context)
            steps = [reset, peer, moved, continued]
        return {
            "success": True,
            "scenario_id": scenario_id,
            "steps": steps,
            "final_state": self.serialize(),
        }

    @not_action
    def _write_material_temperature(
        self,
        site_id: int,
        *,
        temperature_c: float,
        target_temperature_c: float,
        state: str,
        progress: float,
        job_uuid: str,
    ) -> None:
        with self._materials_lock:
            self._write_material_temperature_locked(
                site_id,
                temperature_c=temperature_c,
                target_temperature_c=target_temperature_c,
                state=state,
                progress=progress,
                job_uuid=job_uuid,
            )

    @not_action
    def _write_material_temperature_locked(
        self,
        site_id: int,
        *,
        temperature_c: float,
        target_temperature_c: float,
        state: str,
        progress: float,
        job_uuid: str,
    ) -> None:
        gateway = self._gateway()
        material_uuid = str(self._sites[site_id]["material_uuid"])
        aggregate = gateway.get_material(material_uuid)
        now_ms = int(time.time() * 1000)
        rounded_temperature = round(float(temperature_c), 3)
        rounded_target = round(float(target_temperature_c), 3)

        # First update the simulated instrument measurement. Device telemetry
        # samples these scalar properties; the material write below is only a
        # passive latest-value projection and is not the chart history source.
        with self._lock:
            self._sites[site_id].update(
                {
                    "temperature_c": temperature_c,
                    "target_temperature_c": target_temperature_c,
                    "progress": progress,
                    "state": state,
                }
            )
        data = deepcopy(aggregate.data.data)
        data.pop("temperature_history", None)
        data.update(
            {
                "temperature_c": rounded_temperature,
                "target_temperature_c": rounded_target,
                "temperature_observed_at_ms": now_ms,
                "temperature_source": {
                    "device_id": self.device_id,
                    "property": f"site_{site_id}_temperature_c",
                },
                "serialized_state": {
                    "site_id": site_id,
                    "temperature_c": rounded_temperature,
                    "target_temperature_c": rounded_target,
                    "progress": round(float(progress), 3),
                    "state": state,
                    "observed_at_ms": now_ms,
                },
            }
        )
        gateway.put_data(
            self._mutation(
                "put_data",
                f"temperature:{site_id}:{now_ms}:{uuid4().hex}",
                job_uuid=job_uuid,
            ),
            material_uuid,
            MaterialDataWrite(
                data=data,
                substances=aggregate.data.substances,
                sites_initialized=aggregate.data.sites_initialized,
                unknown_counter=aggregate.data.unknown_counter,
                state_status=state,
                source_event_uuid=aggregate.data.source_event_uuid,
                source_job_uuid=job_uuid,
                observed_at_ms=now_ms,
            ),
        )

    @action(
        description="按工位、目标温度和时长加热；实时写回物料 data.temperature_c",
        goal_default={
            "site_id": 1,
            "target_temperature_c": 80.0,
            "duration_seconds": 8.0,
        },
        feedback_interval=0.25,
    )
    def heat_site(
        self,
        site_id: int,
        target_temperature_c: float,
        duration_seconds: float,
        action_context: ActionContext,
    ) -> dict[str, Any]:
        if site_id not in self._sites:
            raise ValueError("site_id must be 1, 2 or 3")
        if not -20.0 <= float(target_temperature_c) <= 250.0:
            raise ValueError("target_temperature_c must be between -20 and 250")
        if not 0.05 <= float(duration_seconds) <= 3600.0:
            raise ValueError("duration_seconds must be between 0.05 and 3600")

        if not self._platform_uuid:
            self._provision_materials()
        else:
            self._refresh_material_state()
        material_uuid = str(self._sites[site_id]["material_uuid"])
        if not material_uuid:
            raise RuntimeError(f"site {site_id} has no material")
        start_temperature = float(self._sites[site_id]["temperature_c"])
        steps = max(1, min(120, int(duration_seconds / self.update_interval_s) + 1))
        delay = float(duration_seconds) / steps
        for step in range(1, steps + 1):
            action_context.raise_if_cancelled()
            ratio = step / steps
            temperature = (
                start_temperature
                + (float(target_temperature_c) - start_temperature) * ratio
            )
            state = "completed" if step == steps else "heating"
            self._write_material_temperature(
                site_id,
                temperature_c=temperature,
                target_temperature_c=float(target_temperature_c),
                state=state,
                progress=ratio * 100.0,
                job_uuid=action_context.action_id,
            )
            action_context.publish_feedback(
                {
                    "site_id": site_id,
                    "material_uuid": material_uuid,
                    "temperature_c": round(temperature, 3),
                    "target_temperature_c": float(target_temperature_c),
                    "progress": round(ratio * 100.0, 3),
                }
            )
            if step < steps:
                time.sleep(delay)
        return {
            "success": True,
            "site_id": site_id,
            "material_uuid": material_uuid,
            "temperature_c": round(float(target_temperature_c), 3),
            "duration_seconds": float(duration_seconds),
        }

    @not_action
    def serialize(self) -> dict[str, Any]:
        """Return the complete JSON-safe simulated state.

        Material temperature remains authoritative in ``materials.v1``. This
        serialized projection is copied into each material's
        ``data.serialized_state`` during actions and is useful to adapters that
        need a complete device snapshot.
        """

        with self._lock:
            return {
                "device_id": self.device_id,
                "platform_material_uuid": self._platform_uuid,
                "sites": [deepcopy(self._sites[index]) for index in range(1, 4)],
            }

    @property
    @topic_config(period=0.25)
    def status(self) -> str:
        with self._lock:
            return (
                "heating"
                if any(item["state"] == "heating" for item in self._sites.values())
                else "ready"
            )

    @property
    @topic_config(period=0.25)
    def site_1_temperature_c(self) -> float:
        return float(self._sites[1]["temperature_c"])

    @property
    @topic_config(period=0.25)
    def site_2_temperature_c(self) -> float:
        return float(self._sites[2]["temperature_c"])

    @property
    @topic_config(period=0.25)
    def site_3_temperature_c(self) -> float:
        return float(self._sites[3]["temperature_c"])

    @property
    @topic_config(period=0.25)
    def site_1_state(self) -> str:
        return str(self._sites[1]["state"])

    @property
    @topic_config(period=0.25)
    def site_2_state(self) -> str:
        return str(self._sites[2]["state"])

    @property
    @topic_config(period=0.25)
    def site_3_state(self) -> str:
        return str(self._sites[3]["state"])

    @property
    @topic_config(period=0.25)
    def serialized_state(self) -> dict[str, Any]:
        """供 HostLink/telemetry 读取的完整 JSON 状态投影。"""

        return self.serialize()


__all__ = [
    "AMBIENT_TEMPERATURE_C",
    "SAMPLE_DISPLAY_COLORS",
    "VIRTUAL_HEATING_PLATFORM_SITES",
    "VirtualHeatingPlatform",
]
