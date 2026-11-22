"""三工位虚拟加热平台。

驱动刻意走与真实驱动相同的边界：

* 动作经 HostLink 派发；
* 标量状态走常规的设备状态通道发布；
* 平台是温度的测量来源，按工位发布遥测；
* 样品是 PLR 物料（:class:`VirtualHeatingSample`），经 client 物料服务创建并原子地放上设备自己的
  工位（``materials.create(..., node=, site=)``）；温度是样品自己的 PLR 状态，经
  ``serialize_state`` / ``load_state`` 往返权威，每次变化经 ``materials.update`` 回报。驱动不直接写
  物料权威，也没有私有的演示存储。

设备重启时，工位上的样品由框架按权威反序列化成 PLR 实例挂回设备；驱动按权威的工位占用
（Site uuid → 物料 uuid）在 tracker 里找到同一份实例接管，不重复创建。

加热时长是模拟时间：真实等待 = ``duration_seconds`` × 时间倍率（图节点 ``config.time_scale``，
没配时取设备类自带的默认值 0.001，很快）；示例图把倍率设为 1，按真实时长画出升温曲线。
"""

from __future__ import annotations

from copy import deepcopy
import time
from threading import RLock
from typing import Any, Mapping, Optional

from pylabrobot.resources import Resource
from pylabrobot.resources.barcode import Barcode

from unilabos.client.host.runtime.action import ActionContext
from unilabos.client.host.hostlink.protocol import LinkError
from unilabos.devices.virtual.simulation import DEFAULT_TIME_SCALE, resolve_time_scale
from unilabos.registry.decorators import action, device, not_action, resource, topic_config
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionSize,
)
from unilabos.resources.objects.resource import EXTRA_RESOURCE_META_DATA
from unilabos.resources.objects.site import SiteDefinition
from unilabos.utils import logger


AMBIENT_TEMPERATURE_C = 25.0
SAMPLE_DISPLAY_COLORS = ("#2F80ED", "#9B51E0", "#27AE60")
#: 样品的注册表资源 id，也是它在物料权威里的资源模板名
SAMPLE_TEMPLATE = "virtual_heating_sample"
SITE_IDS = (1, 2, 3)


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


def _site_label(site_id: int) -> str:
    return f"site_{site_id}"


@resource(
    # 注册表 AST 扫描只认字面量：与 SAMPLE_TEMPLATE 保持一致
    id="virtual_heating_sample",
    category=["heating_sample"],
    display_name="虚拟加热样品",
    description="三工位虚拟加热平台上的样品；温度与加热进度是它自己的 PLR 状态",
)
class VirtualHeatingSample(Resource):
    """虚拟加热平台上的样品。

    温度、目标温度与加热进度是样品自己的状态：``serialize_state`` 输出的就是物料权威里的
    ``data``（``temperature_c`` / ``target_temperature_c`` / ``temperature_observed_at_ms`` /
    ``temperature_source`` / ``serialized_state``），``load_state`` 原样读回，设备重启后温度不丢。
    """

    def __init__(
        self,
        name: str,
        size_x: float = 60.0,
        size_y: float = 60.0,
        size_z: float = 40.0,
        rotation: Any = None,
        category: Optional[str] = "heating_sample",
        model: Optional[str] = None,
        barcode: Optional[Barcode] = None,
        preferred_pickup_location: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            rotation=rotation,
            category=category,
            model=model,
            barcode=barcode,
            preferred_pickup_location=preferred_pickup_location,
            metadata=metadata,
        )
        self.temperature_c = AMBIENT_TEMPERATURE_C
        self.target_temperature_c = AMBIENT_TEMPERATURE_C
        self.temperature_observed_at_ms = 0
        self.temperature_source: dict[str, Any] = {}
        self.heating_state: dict[str, Any] = {
            "temperature_c": AMBIENT_TEMPERATURE_C,
            "state": "idle",
        }

    @property
    def state(self) -> str:
        return str(self.heating_state.get("state", "idle"))

    def serialize_state(self) -> dict[str, Any]:
        state = {
            **super().serialize_state(),
            "temperature_c": self.temperature_c,
            "target_temperature_c": self.target_temperature_c,
            "temperature_source": deepcopy(self.temperature_source),
            "serialized_state": deepcopy(self.heating_state),
        }
        if self.temperature_observed_at_ms:
            state["temperature_observed_at_ms"] = self.temperature_observed_at_ms
        return state

    def load_state(self, state: dict[str, Any]) -> None:
        super().load_state(state)
        self.temperature_c = float(state.get("temperature_c", AMBIENT_TEMPERATURE_C))
        self.target_temperature_c = float(
            state.get("target_temperature_c", self.temperature_c)
        )
        self.temperature_observed_at_ms = int(state.get("temperature_observed_at_ms") or 0)
        source = state.get("temperature_source")
        self.temperature_source = dict(source) if isinstance(source, Mapping) else {}
        heating = state.get("serialized_state")
        self.heating_state = (
            dict(heating)
            if isinstance(heating, Mapping)
            else {"temperature_c": self.temperature_c, "state": "idle"}
        )

    def record_temperature(
        self,
        *,
        site_id: int,
        source: Mapping[str, Any],
        temperature_c: float,
        target_temperature_c: float,
        progress: float,
        state: str,
        observed_at_ms: int,
    ) -> None:
        """记一次测量：被动投影最新值（遥测历史由设备状态通道负责，这里不攒历史）。

        只改实例状态，不触发 PLR 状态回调：平台随即经 ``materials.update`` 同步回报这份
        状态（失败直接让动作失败），再排一份观察者快照只会是重复写入。
        """

        self.temperature_c = round(float(temperature_c), 3)
        self.target_temperature_c = round(float(target_temperature_c), 3)
        self.temperature_observed_at_ms = int(observed_at_ms)
        self.temperature_source = dict(source)
        self.heating_state = {
            "site_id": site_id,
            "temperature_c": self.temperature_c,
            "target_temperature_c": self.target_temperature_c,
            "progress": round(float(progress), 3),
            "state": state,
            "observed_at_ms": int(observed_at_ms),
        }


@device(
    id="virtual_heating_platform",
    display_name="三工位虚拟加热平台",
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
    #: 图节点没配 time_scale 时的时间倍率（设备类自带）
    default_time_scale: float = DEFAULT_TIME_SCALE

    def __init__(
        self,
        device_id: str = "virtual_heating_platform",
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.device_id = device_id
        # 两种 backend 的传参形状都接受：config 字典或平铺的构造参数（config 优先）
        self.config = {**kwargs, **(config or {})}
        self.update_interval_s = max(
            0.05, float(self.config.get("update_interval_s", 0.25))
        )
        # 模拟时间倍率：真实等待 = duration_seconds × time_scale
        self.time_scale = resolve_time_scale(self.config.get("time_scale"), self.default_time_scale)
        self._lock = RLock()
        self._provision_lock = RLock()
        self._node: Any = None
        self._platform_uuid = ""
        #: 工位号 → 设备库位（``post_init`` 给的 ``ResourceSite``）
        self._device_sites: dict[int, Any] = {}
        #: 工位号 → 工位上的样品实例（与设备 tracker 里登记的是同一份）
        self._samples: dict[int, VirtualHeatingSample] = {}
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
            for index in SITE_IDS
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @not_action
    def post_init(
        self,
        node: Any,
        sites: Mapping[str, Any] | None = None,
        site_resources: Mapping[str, Any] | None = None,
    ) -> None:
        """接住设备节点、设备自己的工位与工位上已有的样品（重启时由框架从权威挂回）。"""

        self._node = node
        self._platform_uuid = str(getattr(node, "resource_uuid", "") or "")
        for index in SITE_IDS:
            label = _site_label(index)
            site = (sites or {}).get(label)
            if site is not None:
                self._device_sites[index] = site
                with self._lock:
                    self._sites[index]["site_uuid"] = str(site.uuid)
            occupant = (site_resources or {}).get(label)
            if occupant is not None:
                self._adopt(index, occupant)

    @not_action
    def initialize(self) -> bool:
        try:
            self._provision_materials()
        except LinkError as exc:
            # Demo Slave may intentionally start while the public Host is down.
            # HostLinkBackend calls on_hostlink_connected after reconnect.
            logger.info("[HeatingDemo] 物料初始化等待 HostLink 重连：%s", exc)
        return True

    @not_action
    def on_hostlink_connected(self) -> None:
        """Idempotently materialize the demo after every HostLink reconnect."""

        self._provision_materials()

    # ------------------------------------------------------------------
    # 样品：uuid ↔ 实例
    # ------------------------------------------------------------------

    @not_action
    def _require_node(self) -> Any:
        if self._node is None:
            raise RuntimeError(
                f"虚拟加热平台 {self.device_id} 还没有设备节点（post_init 未调用）：样品要建在设备自己的工位上"
            )
        return self._node

    @staticmethod
    def _gateway() -> Any:
        from unilabos.resources.materials import resolve_materials_gateway

        return resolve_materials_gateway()

    @not_action
    def _display_name(self, material_uuid: str, fallback: str) -> str:
        try:
            return str(self._gateway().get_material(material_uuid).material.display_name or fallback)
        except Exception:  # noqa: BLE001 - 展示名只用于状态投影，读不到就用 PLR 名
            return fallback

    @not_action
    def _adopt(self, site_id: int, occupant: Any, *, display_name: str = "") -> None:
        """登记工位上的物料；只有 :class:`VirtualHeatingSample` 能加热。"""

        material_uuid = str(getattr(occupant, "unilabos_uuid", "") or "")
        with self._lock:
            state = self._sites[site_id]
            state["material_uuid"] = material_uuid
            state["material_name"] = display_name or (
                self._display_name(material_uuid, occupant.name) if material_uuid else occupant.name
            )
            if not isinstance(occupant, VirtualHeatingSample):
                self._samples.pop(site_id, None)
                logger.warning(
                    "[HeatingDemo] %s 工位 %s 上的 %s 不是加热样品（%s），不能加热",
                    self.device_id,
                    site_id,
                    occupant.name,
                    type(occupant).__name__,
                )
                return
            self._samples[site_id] = occupant
            state.update(
                {
                    "temperature_c": occupant.temperature_c,
                    "target_temperature_c": occupant.target_temperature_c,
                    "progress": float(occupant.heating_state.get("progress", 0.0) or 0.0),
                    "state": occupant.state,
                }
            )

    @not_action
    def _refresh_sites(self) -> None:
        """按权威的工位占用（Site uuid → 物料 uuid）在设备 tracker 里找到同一份实例。

        样品被转运走（不在本设备 tracker 里）就不再登记；别处转运进来的物料按占用接管。
        """

        node = self._require_node()
        if not self._platform_uuid:
            return
        platform = self._gateway().get_material(self._platform_uuid)
        by_label = {str(site.label): site for site in platform.sites}
        tracker = node.resource_tracker
        for index in SITE_IDS:
            site = by_label.get(_site_label(index))
            occupied = str(getattr(site, "occupied_material_uuid", "") or "") if site is not None else ""
            occupant = tracker.uuid_to_resources.get(occupied) if occupied else None
            current = self._samples.get(index)
            if occupant is not None:
                if occupant is not current:
                    self._adopt(index, occupant)
                continue
            self._samples.pop(index, None)
            with self._lock:
                self._sites[index]["material_uuid"] = occupied
                self._sites[index]["material_name"] = ""
            if occupied:
                logger.warning(
                    "[HeatingDemo] %s 工位 %s 被物料 %s 占用，但设备本地没有它的 PLR 实例",
                    self.device_id,
                    index,
                    occupied,
                )

    @not_action
    def _new_sample(self, site_id: int) -> VirtualHeatingSample:
        from unilabos.resources.resource_tracker import set_plr_template_name

        sample = VirtualHeatingSample(
            f"{self.device_id}-sample-{site_id}",
            barcode=Barcode(
                data=f"OPENLAB-HEAT-{site_id}",
                symbology="CODE128",
                position_on_resource="front",
            ),
        )
        set_plr_template_name(sample, SAMPLE_TEMPLATE)
        sample.unilabos_extra = {
            **(getattr(sample, "unilabos_extra", None) or {}),
            EXTRA_RESOURCE_META_DATA: {
                "demo": "openlab-heating",
                "site_id": site_id,
                "display_color": SAMPLE_DISPLAY_COLORS[site_id - 1],
            },
        }
        sample.temperature_source = self._temperature_source(site_id)
        return sample

    @not_action
    def _provision_materials(self) -> None:
        """空工位放上演示样品：建树与占用工位在权威的同一事务里完成（失败整体回滚）。"""

        from unilabos.resources import materials

        with self._provision_lock:
            node = self._require_node()
            self._refresh_sites()
            for index in SITE_IDS:
                if index in self._samples or self._sites[index]["material_uuid"]:
                    continue
                site = self._device_sites.get(index) or _site_label(index)
                display_name = f"加热样品 {index}"
                try:
                    created = materials.create(
                        self._new_sample(index),
                        node=node,
                        site=site,
                        display_name=display_name,
                    )
                except LinkError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 一个工位放不上不影响其它工位与设备启动
                    logger.warning(
                        "[HeatingDemo] %s 工位 %s 放置演示样品失败：%s", self.device_id, index, exc
                    )
                    continue
                self._adopt(index, created, display_name=display_name)

    # ------------------------------------------------------------------
    # 加热
    # ------------------------------------------------------------------

    @not_action
    def _temperature_source(self, site_id: int) -> dict[str, Any]:
        return {"device_id": self.device_id, "property": f"site_{site_id}_temperature_c"}

    @not_action
    def _sample(self, site_id: int) -> VirtualHeatingSample:
        sample = self._samples.get(site_id)
        node = self._require_node()
        if sample is not None and node.resource_tracker.uuid_to_resources.get(sample.unilabos_uuid) is sample:
            return sample
        self._provision_materials()
        sample = self._samples.get(site_id)
        if sample is None:
            occupied = self._sites[site_id]["material_uuid"]
            raise RuntimeError(
                f"site {site_id} has no heating sample"
                + (f" (occupied by {occupied})" if occupied else "")
            )
        return sample

    @not_action
    def _write_material_temperature(
        self,
        site_id: int,
        *,
        temperature_c: float,
        target_temperature_c: float,
        state: str,
        progress: float,
    ) -> None:
        """先更新模拟仪器的测量值（遥测按设备状态采样），再把最新值投影到样品并回报权威。"""

        from unilabos.resources import materials

        sample = self._sample(site_id)
        with self._lock:
            self._sites[site_id].update(
                {
                    "temperature_c": temperature_c,
                    "target_temperature_c": target_temperature_c,
                    "progress": progress,
                    "state": state,
                }
            )
        sample.record_temperature(
            site_id=site_id,
            source=self._temperature_source(site_id),
            temperature_c=temperature_c,
            target_temperature_c=target_temperature_c,
            progress=progress,
            state=state,
            observed_at_ms=int(time.time() * 1000),
        )
        materials.update(self._node, sample)

    @action(
        display_name="工位加热",
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

        with self._provision_lock:
            self._refresh_sites()
        sample = self._sample(site_id)
        material_uuid = str(sample.unilabos_uuid)
        start_temperature = float(sample.temperature_c)
        wall_seconds = float(duration_seconds) * self.time_scale
        steps = max(1, min(120, int(wall_seconds / self.update_interval_s) + 1))
        delay = wall_seconds / steps
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
            if step < steps and delay > 0:
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

        Material temperature remains authoritative in ``materials.v1`` (each
        sample's own PLR state). This serialized projection mirrors it per site
        and is useful to adapters that need a complete device snapshot.
        """

        with self._lock:
            return {
                "device_id": self.device_id,
                "platform_material_uuid": self._platform_uuid,
                "sites": [deepcopy(self._sites[index]) for index in SITE_IDS],
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
    "SAMPLE_TEMPLATE",
    "VIRTUAL_HEATING_PLATFORM_SITES",
    "VirtualHeatingPlatform",
    "VirtualHeatingSample",
]
