from __future__ import annotations

from math import e
import re
import time
import traceback
from collections import Counter
from typing import List, Sequence, Optional, Literal, Union, Iterator, Dict, Any, Callable, Set, cast

from pylabrobot.liquid_handling import LiquidHandler, LiquidHandlerBackend, LiquidHandlerChatterboxBackend, Strictness
from pylabrobot.liquid_handling.liquid_handler import TipPresenceProbingMethod
from pylabrobot.liquid_handling.standard import GripDirection
from pylabrobot.resources.errors import TooLittleLiquidError, TooLittleVolumeError
from pylabrobot.resources.volume_tracker import no_volume_tracking
from pylabrobot.resources import (
    Resource,
    TipRack,
    Container,
    Coordinate,
    Well,
    Deck,
    TipSpot,
    Plate,
    ResourceStack,
    ResourceHolder,
    Lid,
    Trash,
    Tip, TubeRack,
)
from typing_extensions import TypedDict

from unilabos.devices.liquid_handling.liquid_history import (
    LiquidHistoryEntry,
    append_liquid_history as _append_liquid_history,
    capture_tip_liquid_name as _capture_tip_liquid_name,
    normalize_liquid_history as _normalize_liquid_history,
    patch_unknown_history_last as _patch_unknown_history_last,
    same_liquid_via_liquids as _same_liquid_via_liquids,
    same_liquid_via_liquids_pair as _same_liquid_via_liquids_pair,
    well_current_liquid_name as _well_current_liquid_name,
)
from unilabos.devices.liquid_handling.rviz_backend import UniLiquidHandlerRvizBackend
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import (
    ResourceTreeSet,
    ResourceDict,
    EXTRA_SAMPLE_UUID,
    EXTRA_UNILABOS_SAMPLE_UUID,
)
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, ROS2DeviceNode


class SimpleReturn(TypedDict):
    samples: List[List[ResourceDict]]
    volumes: List[float]


class SetLiquidReturn(TypedDict):
    wells: List[List[ResourceDict]]
    volumes: List[float]


class SetLiquidFromPlateReturn(TypedDict):
    plate: List[List[ResourceDict]]
    wells: List[List[ResourceDict]]
    volumes: List[float]


class TransferLiquidReturn(TypedDict):
    sources: List[List[ResourceDict]]
    targets: List[List[ResourceDict]]


class LiquidHandlerMiddleware(LiquidHandler):
    _ros_node: ROS2DeviceNode
    def __init__(
        self, backend: LiquidHandlerBackend, deck: Deck, simulator: bool = False, channel_num: int = 8, **kwargs
    ):
        self._simulator = simulator
        self.channel_num = channel_num
        self.pending_liquids_dict = {}
        joint_config = kwargs.get("joint_config", None)
        if simulator:
            if joint_config:
                self._simulate_backend = UniLiquidHandlerRvizBackend(
                    channel_num, kwargs["total_height"], joint_config=joint_config, lh_device_id=deck.name,
                    simulate_rviz=kwargs.get("simulate_rviz", True)
                )
            else:
                self._simulate_backend = LiquidHandlerChatterboxBackend(channel_num)
            self._simulate_handler = LiquidHandlerAbstract(self._simulate_backend, deck, False)
        super().__init__(backend, deck)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node
        if getattr(self, "_simulator", False) and getattr(self, "_simulate_handler", None) is not None:
            self._simulate_handler._ros_node = ros_node

    async def setup(self, **backend_kwargs):
        if self._simulator:
            return await self._simulate_handler.setup(**backend_kwargs)
        return await super().setup(**backend_kwargs)

    def serialize_state(self) -> Dict[str, Any]:
        if self._simulator:
            self._simulate_handler.serialize_state()
        return super().serialize_state()

    def load_state(self, state: Dict[str, Any]):
        if self._simulator:
            self._simulate_handler.load_state(state)
        super().load_state(state)

    def update_head_state(self, state: Dict[int, Optional[Tip]]):
        if self._simulator:
            self._simulate_handler.update_head_state(state)
        super().update_head_state(state)

    def clear_head_state(self):
        if self._simulator:
            self._simulate_handler.clear_head_state()
        super().clear_head_state()

    def _run_async_in_thread(self, func, *args, **kwargs):
        super()._run_async_in_thread(func, *args, **kwargs)

    def _send_assigned_resource_to_backend(self, resource: Resource):
        if self._simulator:
            self._simulate_handler._send_assigned_resource_to_backend(resource)
        super()._send_assigned_resource_to_backend(resource)

    def _send_unassigned_resource_to_backend(self, resource: Resource):
        if self._simulator:
            self._simulate_handler._send_unassigned_resource_to_backend(resource)
        super()._send_unassigned_resource_to_backend(resource)

    def summary(self):
        if self._simulator:
            self._simulate_handler.summary()
        super().summary()

    def _assert_positions_unique(self, positions: List[str]):
        super()._assert_positions_unique(positions)

    def _assert_resources_exist(self, resources: Sequence[Resource]):
        super()._assert_resources_exist(resources)

    def _check_args(
        self, method: Callable, backend_kwargs: Dict[str, Any], default: Set[str], strictness: Strictness
    ) -> Set[str]:
        return super()._check_args(method, backend_kwargs, default, strictness)

    def _make_sure_channels_exist(self, channels: List[int]):
        super()._make_sure_channels_exist(channels)

    def _format_param(self, value: Any) -> Any:
        return super()._format_param(value)

    def _log_command(self, name: str, **kwargs) -> None:
        super()._log_command(name, **kwargs)

    async def pick_up_tips(
        self,
        tip_spots: List[TipSpot],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):

        if self._simulator:
            return await self._simulate_handler.pick_up_tips(tip_spots, use_channels, offsets, **backend_kwargs)
        # 让 PLR 走标准链路：tracker.remove_tip -> 成功 commit / 失败 rollback，
        # 由此 TipSpot.has_tip() 自动反映为 False，符合 LiquidHandler 规范。
        result = await super().pick_up_tips(tip_spots, use_channels, offsets, **backend_kwargs)
        for tip_spot in tip_spots:
            tip_spot.empty()
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            task = ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{"resources": tip_spots})
            submit_time = time.time()
            while not task.done():
                if time.time() - submit_time > 10:
                    self._ros_node.lab_logger().info(f"pick_up_tips {tip_spots} 超时")
                    break
                time.sleep(0.01)
        return result

    async def drop_tips(
        self,
        tip_spots: Sequence[Union[TipSpot, Trash]],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        allow_nonzero_volume: bool = False,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.drop_tips(
                tip_spots, use_channels, offsets, allow_nonzero_volume, **backend_kwargs
            )
        await super().drop_tips(tip_spots, use_channels, offsets, allow_nonzero_volume, **backend_kwargs)
        self.pending_liquids_dict = {}
        return

    async def return_tips(
        self, use_channels: Optional[list[int]] = None, allow_nonzero_volume: bool = False, **backend_kwargs
    ):
        if self._simulator:
            return await self._simulate_handler.return_tips(use_channels, allow_nonzero_volume, **backend_kwargs)
        return await super().return_tips(use_channels, allow_nonzero_volume, **backend_kwargs)

    async def discard_tips(
        self,
        use_channels: Optional[List[int]] = None,
        allow_nonzero_volume: bool = True,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        # 如果 use_channels 为 None，使用默认值（所有通道）
        if use_channels is None:
            use_channels = list(range(self.channel_num))
        if not offsets or (isinstance(offsets, list) and len(offsets) != len(use_channels)):
            offsets = [Coordinate.zero()] * len(use_channels)
        if self._simulator:
            return await self._simulate_handler.discard_tips(
                use_channels, allow_nonzero_volume, offsets, **backend_kwargs
            )
        await super().discard_tips(use_channels, allow_nonzero_volume, offsets, **backend_kwargs)
        self.pending_liquids_dict = {}
        return

    def _check_containers(self, resources: Sequence[Resource]):
        super()._check_containers(resources)

    async def aspirate(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "custom",
        **backend_kwargs,
    ):
        if spread == "":
            spread = "custom"

        # P9 — 在 super().aspirate 之前**预读**每个 source well 的液体名（用于 history 写入）；
        # super().aspirate 会消费 tracker.liquids，aspirate 后再读会拿不到液体身份。
        # 详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.2。
        liquid_names_before_aspirate: List[str] = [
            _well_current_liquid_name(res) for res in resources
        ]

        # 同一 source 可能在 8 通道中重复出现（reservoir 广播），先按资源聚合总需求，
        # 避免逐条判断时重复用同一个 ``used`` 导致补液不足。
        required_by_res: Dict[int, Dict[str, Any]] = {}
        for i, res in enumerate(resources):
            tracker = getattr(res, "tracker", None)
            if tracker is None or getattr(tracker, "is_disabled", False):
                continue
            need = float(vols[i]) if i < len(vols) else 0.0
            if blow_out_air_volume and i < len(blow_out_air_volume) and blow_out_air_volume[i] is not None:
                need += float(blow_out_air_volume[i] or 0.0)
            if need <= 0:
                continue
            key = id(res)
            slot = required_by_res.setdefault(
                key,
                {"resource": res, "tracker": tracker, "need": 0.0},
            )
            slot["need"] = float(slot["need"]) + need

        for slot in required_by_res.values():
            res = cast(Container, slot["resource"])
            tracker = slot["tracker"]
            need_total = float(slot["need"] or 0.0)
            if need_total <= 0:
                continue
            try:
                used = float(tracker.get_used_volume())
            except Exception:
                used = 0.0
            if used >= need_total:
                continue
            mv = float(getattr(tracker, "max_volume", 0) or 0)
            if used <= 0:
                # 与旧逻辑一致：空孔优先加满（或极大默认），避免仅有 history 记录但 used=0 时不补液
                fill_vol = mv if mv > 0 else max(need_total, 50000.0)
            else:
                fill_vol = need_total - used
                if mv > 0:
                    fill_vol = min(fill_vol, max(0.0, mv - used))
            try:
                tracker.add_liquid(fill_vol)
            except Exception:
                try:
                    tracker.add_liquid(max(need_total - used, 1.0))
                except Exception:
                    # P9 — 旧版 v2 tuple ``("auto_init", vol)`` 写入升级为 v3 dict，
                    # 与 ``_append_liquid_history`` 写入形态保持一致。
                    _append_liquid_history(
                        res,
                        "auto_init",
                        float(max(fill_vol, need_total, 1.0)),
                        "auto_init",
                    )

        if self._simulator:
            try:
                return await self._simulate_handler.aspirate(
                    resources,
                    vols,
                    use_channels,
                    flow_rates,
                    offsets,
                    liquid_height,
                    blow_out_air_volume,
                    spread,
                    **backend_kwargs,
                )
            except (TooLittleLiquidError, TooLittleVolumeError) as e:
                tracker_info = []
                for r in resources:
                    t = r.tracker
                    tracker_info.append(
                        f"{r.name}(used={t.get_used_volume():.1f}, "
                        f"free={t.get_free_volume():.1f}, max={r.max_volume})"
                    )
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[aspirate] volume tracker error, bypassing tracking. "
                        f"error={e}, vols={vols}, trackers={tracker_info}"
                    )
                disabled_trackers: List[Any] = []
                seen_trackers: Set[int] = set()
                for r in resources:
                    t = getattr(r, "tracker", None)
                    if t is None or not hasattr(t, "disable"):
                        continue
                    tid = id(t)
                    if tid in seen_trackers:
                        continue
                    seen_trackers.add(tid)
                    t.disable()
                    disabled_trackers.append(t)
                try:
                    return await self._simulate_handler.aspirate(
                        resources,
                        vols,
                        use_channels,
                        flow_rates,
                        offsets,
                        liquid_height,
                        blow_out_air_volume,
                        spread,
                        **backend_kwargs,
                    )
                finally:
                    for t in disabled_trackers:
                        try:
                            t.enable()
                        except Exception:
                            pass
        try:
            await super().aspirate(
                resources,
                vols,
                use_channels,
                flow_rates,
                offsets,
                liquid_height,
                blow_out_air_volume,
                spread,
                **backend_kwargs,
            )
        except (TooLittleLiquidError, TooLittleVolumeError) as e:
            tracker_info = []
            for r in resources:
                t = getattr(r, "tracker", None)
                if t is None:
                    tracker_info.append(f"{r.name}(no_tracker)")
                else:
                    try:
                        tracker_info.append(
                            f"{r.name}(used={t.get_used_volume():.1f}, "
                            f"free={t.get_free_volume():.1f}, max={getattr(r, 'max_volume', '?')})"
                        )
                    except Exception:
                        tracker_info.append(f"{r.name}(tracker_err)")
            if hasattr(self, "_ros_node") and self._ros_node is not None:
                self._ros_node.lab_logger().warning(
                    f"[aspirate] hardware tracker shortfall, retry without volume tracking. "
                    f"error={e}, vols={vols}, trackers={tracker_info}"
                )
            disabled_trackers: List[Any] = []
            seen_trackers: Set[int] = set()
            for r in resources:
                t = getattr(r, "tracker", None)
                if t is None or not hasattr(t, "disable"):
                    continue
                tid = id(t)
                if tid in seen_trackers:
                    continue
                seen_trackers.add(tid)
                t.disable()
                disabled_trackers.append(t)
            try:
                try:
                    await super().aspirate(
                        resources,
                        vols,
                        use_channels,
                        flow_rates,
                        offsets,
                        liquid_height,
                        blow_out_air_volume,
                        spread,
                        **backend_kwargs,
                    )
                except (TooLittleLiquidError, TooLittleVolumeError) as retry_e:
                    tip_channels = list(use_channels) if use_channels is not None else [0] * len(resources)
                    disabled_tip_trackers: List[Any] = []
                    seen_tip_trackers: Set[int] = set()
                    for channel in tip_channels:
                        try:
                            tip = self.head[channel].get_tip()  # type: ignore[index]
                        except Exception:
                            tip = None
                        tip_tracker = getattr(tip, "tracker", None) if tip is not None else None
                        if tip_tracker is None or not hasattr(tip_tracker, "disable"):
                            continue
                        tid = id(tip_tracker)
                        if tid in seen_tip_trackers:
                            continue
                        seen_tip_trackers.add(tid)
                        tip_tracker.disable()
                        disabled_tip_trackers.append(tip_tracker)
                    if hasattr(self, "_ros_node") and self._ros_node is not None:
                        self._ros_node.lab_logger().warning(
                            f"[aspirate] retry with tip tracker disabled. error={retry_e}, vols={vols}"
                        )
                    try:
                        try:
                            await super().aspirate(
                                resources,
                                vols,
                                use_channels,
                                flow_rates,
                                offsets,
                                liquid_height,
                                blow_out_air_volume,
                                spread,
                                **backend_kwargs,
                            )
                        except (TooLittleLiquidError, TooLittleVolumeError) as final_e:
                            if hasattr(self, "_ros_node") and self._ros_node is not None:
                                self._ros_node.lab_logger().warning(
                                    f"[aspirate] final fallback no_volume_tracking. error={final_e}, vols={vols}"
                                )
                            with no_volume_tracking():
                                await super().aspirate(
                                    resources,
                                    vols,
                                    use_channels,
                                    flow_rates,
                                    offsets,
                                    liquid_height,
                                    blow_out_air_volume,
                                    spread,
                                    **backend_kwargs,
                                )
                    finally:
                        for tip_tracker in disabled_tip_trackers:
                            try:
                                tip_tracker.enable()
                            except Exception:
                                pass
            finally:
                for t in disabled_trackers:
                    try:
                        t.enable()
                    except Exception:
                        pass
        except ValueError as e:
            if "Resource is too small to space channels" in str(e) and spread != "custom":
                await self.aspirate(
                    resources,
                    vols,
                    use_channels,
                    flow_rates,
                    offsets,
                    liquid_height,
                    blow_out_air_volume,
                    spread="custom",
                    **backend_kwargs,
                )
            else:
                raise

        res_samples = []
        res_volumes = []
        if use_channels is None:
            channels_to_use = [0] * len(resources)
        else:
            channels_to_use = use_channels

        for i, (resource, volume, channel) in enumerate(zip(resources, vols, channels_to_use)):
            sample_uuid_value = getattr(resource, "unilabos_extra", {}).get(EXTRA_SAMPLE_UUID, None)
            res_samples.append({"name": resource.name, "sample_uuid": sample_uuid_value})
            res_volumes.append(volume)
            name_before = liquid_names_before_aspirate[i] if i < len(liquid_names_before_aspirate) else ""
            # 把 source 当前液体名挂到 channel 元数据,作为 dispense 时 target 末条
            # 改名的权威来源(避免读 tip 历史拿到 PLR 写下的 ``Unknown<n>`` 占位名)。
            self.pending_liquids_dict[channel] = {
                EXTRA_SAMPLE_UUID: sample_uuid_value,
                "volume": volume,
                "liquid_name": str(name_before or ""),
            }
            # P9 — aspirate history 由 PLR ``ContainerVolumeTracker.remove_liquid`` 自动写入。
            # 注（2026-05-28 debug 实证）：installed PLR ``remove_liquid`` 写的是 **2-tuple**
            # ``(None, -vol)``（不是 RESOLUTION-2026-05-28 文档曾假设的 3-tuple ``(None, -vol, "ul")``）。
            # 这里用 P9 已预读的 ``name_before`` 把 PLR 末条的 ``None`` 修补成
            # ``(name_before, -vol)`` 2-tuple —— 保留对称的 "sum(history.volume) ≈ 残量" 语义，
            # 且必须保持 2-tuple 形态，否则 PLR ``VolumeTracker.current_liquids`` 在
            # ``for name, vol in self.liquid_history`` 时会 ValueError：too many values to unpack。
            # 详见 ``RESOLUTION-2026-05-28-plr-liquid-history-double-write.md`` §3 改动 2 + 本次 debug session dc5aa5。
            name_before = liquid_names_before_aspirate[i] if i < len(liquid_names_before_aspirate) else ""
            tracker = getattr(resource, "tracker", None)
            hist = getattr(tracker, "liquid_history", None) if tracker is not None else None
            if isinstance(hist, list) and hist:
                last = hist[-1]
                # 只在 name 缺失（None / "" / 旧 3-tuple 同样 name 缺失）时才覆盖，避免误改下游用户写入项
                if isinstance(last, (list, tuple)) and len(last) >= 2 and (last[0] is None or last[0] == ""):
                    try:
                        last_vol = float(last[1])
                    except (TypeError, ValueError):
                        last_vol = -float(volume or 0.0)
                    # 关键：写 2-tuple ``(name, vol)``，与 PLR 现行 history schema 一致
                    hist[-1] = (str(name_before or ""), last_vol)
            # P9 / B2 —— 修复 PLR aspirate 路径丢失液体身份:
            # 1. ``ContainerVolumeTracker.remove_liquid(vol)`` → source 末条 ``(None, -vol, "ul")``;
            # 2. ``LiquidHandler.aspirate`` 调 ``op.tip.tracker.add_liquid(volume=op.volume)``
            #    没传 liquid 名 → tip 末条 ``(Unknown<n>, +vol, "ul")``,bump tip ``unknown_counter``。
            # 这里用 P9 预读的 ``name_before`` 把两侧末条的占位 name 就地改写为真实化学名,
            # **不增减 entry**(与 ``RESOLUTION-2026-05-28-plr-liquid-history-double-write.md`` §3
            # B1 修复"PLR 当 history 单一真相源"原则一致)。
            _patch_unknown_history_last(getattr(resource, "tracker", None), str(name_before or ""))
            # tip 末条改名:取本通道实际承载的 tip(``self.head[channel].get_tip()``)。
            try:
                tip = self.head[channel].get_tip()  # type: ignore[index]
            except Exception:
                tip = None
            _patch_unknown_history_last(getattr(tip, "tracker", None), str(name_before or ""))

        if hasattr(self, "_ros_node") and self._ros_node is not None:
            unique_resources: List[Container] = []
            seen_resource_ids: Set[int] = set()
            for r in resources:
                rid = id(r)
                if rid in seen_resource_ids:
                    continue
                seen_resource_ids.add(rid)
                unique_resources.append(r)
            task = ROS2DeviceNode.run_async_func(
                self._ros_node.update_resource,
                True,
                **{"resources": unique_resources},
            )
            submit_time = time.time()
            while not task.done():
                if time.time() - submit_time > 10:
                    self._ros_node.lab_logger().info(f"aspirate {resources} 超时")
                    break
                time.sleep(0.01)

        return SimpleReturn(samples=res_samples, volumes=res_volumes)

    async def dispense(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        **backend_kwargs,
    ) -> SimpleReturn:
        if spread == "":
            spread = "wide"
        super_dispense = super().dispense

        def _safe_dispense_volumes(_resources: Sequence[Container], _vols: List[float]) -> List[float]:
            """将 dispense 体积裁剪到目标容器可用体积范围内，避免 volume tracker 报错。"""
            safe: List[float] = []
            free_by_resource: Dict[int, float] = {}
            for res, vol in zip(_resources, _vols):
                req = max(float(vol), 0.0)
                rid = id(res)
                if rid in free_by_resource:
                    free_volume = free_by_resource[rid]
                else:
                    free_volume = None
                    try:
                        tracker = getattr(res, "tracker", None)
                        get_free = getattr(tracker, "get_free_volume", None)
                        if callable(get_free):
                            free_volume = get_free()
                    except Exception:
                        free_volume = None
                    if isinstance(free_volume, (int, float)):
                        free_by_resource[rid] = max(float(free_volume), 0.0)
                if isinstance(free_volume, (int, float)):
                    req = min(req, max(float(free_volume), 0.0))
                    free_by_resource[rid] = max(float(free_volume) - req, 0.0)
                safe.append(req)
            return safe

        actual_vols = _safe_dispense_volumes(resources, vols)
        if use_channels is None:
            channels_to_use = [0] * len(resources)
        else:
            channels_to_use = use_channels

        def _pick_seq_value(val: Any, idx: int) -> Any:
            if val is None:
                return None
            if isinstance(val, (list, tuple)):
                if len(val) == 0:
                    return None
                return [val[idx] if idx < len(val) else val[-1]]
            return [val]

        def _has_mixed_positive_volumes(values: List[float]) -> bool:
            positives = [round(float(v), 6) for v in values if float(v) > 0.0]
            return len(positives) > 1 and len(set(positives)) > 1

        async def _dispense_split_single_channel() -> None:
            for idx, (resource, vol, channel) in enumerate(zip(resources, actual_vols, channels_to_use)):
                if float(vol) <= 0.0:
                    continue
                await super_dispense(
                    [resource],
                    [float(vol)],
                    [channel],
                    _pick_seq_value(flow_rates, idx),
                    _pick_seq_value(offsets, idx),
                    _pick_seq_value(liquid_height, idx),
                    _pick_seq_value(blow_out_air_volume, idx),
                    spread,
                    **backend_kwargs,
                )

        if self._simulator:
            try:
                return await self._simulate_handler.dispense(
                    resources,
                    actual_vols,
                    use_channels,
                    flow_rates,
                    offsets,
                    liquid_height,
                    blow_out_air_volume,
                    spread,
                    **backend_kwargs,
                )
            except (TooLittleLiquidError, TooLittleVolumeError) as e:
                tracker_info = []
                for r in resources:
                    t = r.tracker
                    tracker_info.append(
                        f"{r.name}(used={t.get_used_volume():.1f}, "
                        f"free={t.get_free_volume():.1f}, max={r.max_volume})"
                    )
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[dispense] volume tracker error, bypassing tracking. "
                        f"error={e}, vols={actual_vols}, trackers={tracker_info}"
                    )
                with no_volume_tracking():
                    return await self._simulate_handler.dispense(
                        resources,
                        actual_vols,
                        use_channels,
                        flow_rates,
                        offsets,
                        liquid_height,
                        blow_out_air_volume,
                        spread,
                        **backend_kwargs,
                    )
        try:
            if _has_mixed_positive_volumes(actual_vols):
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[dispense] mixed per-channel volumes={actual_vols}, fallback to single-channel split."
                    )
                await _dispense_split_single_channel()
            else:
                await super().dispense(
                    resources,
                    actual_vols,
                    use_channels,
                    flow_rates,
                    offsets,
                    liquid_height,
                    blow_out_air_volume,
                    spread,
                    **backend_kwargs,
                )
        except ValueError as e:
            err = str(e)
            if "Resource is too small to space channels" in err:
                # 先试 custom 间距；仍报"排不开"（或本就是 custom）则降级单通道串行——
                # 单通道每次只下 1 个 tip，无需 8 道间距，从根本上规避该限制。
                spacing_done = False
                if spread != "custom":
                    try:
                        await super().dispense(
                            resources,
                            actual_vols,
                            use_channels,
                            flow_rates,
                            offsets,
                            liquid_height,
                            blow_out_air_volume,
                            "custom",
                            **backend_kwargs,
                        )
                        spacing_done = True
                    except ValueError as e2:
                        if "Resource is too small to space channels" not in str(e2):
                            raise
                if not spacing_done:
                    if hasattr(self, "_ros_node") and self._ros_node is not None:
                        self._ros_node.lab_logger().warning(
                            f"[dispense] cannot space 8 channels, fallback to single-channel. error={e}"
                        )
                    await _dispense_split_single_channel()
            elif (
                "All dispense volumes must be the same" in err
                or "must be from the same tip column" in err
            ):
                # 非均匀体积 / 跨 tip 列：PRCXI 八连排要求整列同量同列；降级单通道串行。
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[dispense] backend column constraint, split retry. error={e}"
                    )
                await _dispense_split_single_channel()
            else:
                raise
        except (TooLittleLiquidError, TooLittleVolumeError):
            # 再兜底一次：按实时 free volume 重新裁剪后重试，避免并发状态更新导致的瞬时超量
            retry_vols = _safe_dispense_volumes(resources, actual_vols)
            if any(v > 0 for v in retry_vols):
                actual_vols = retry_vols
                try:
                    if _has_mixed_positive_volumes(retry_vols):
                        await _dispense_split_single_channel()
                    else:
                        await super().dispense(
                            resources,
                            retry_vols,
                            use_channels,
                            flow_rates,
                            offsets,
                            liquid_height,
                            blow_out_air_volume,
                            spread,
                            **backend_kwargs,
                        )
                except (TooLittleLiquidError, TooLittleVolumeError) as retry_e:
                    # source/target 已做体积裁剪后仍失败，多数是 tip tracker 与实际动作短暂失配；
                    # 只临时禁用当前通道 tip tracker，再执行一次，避免把 source/target tracker 全禁用。
                    disabled_tip_trackers: List[Any] = []
                    seen_tip_trackers: Set[int] = set()
                    for channel in channels_to_use:
                        try:
                            tip = self.head[channel].get_tip()  # type: ignore[index]
                        except Exception:
                            tip = None
                        tip_tracker = getattr(tip, "tracker", None) if tip is not None else None
                        if tip_tracker is None or not hasattr(tip_tracker, "disable"):
                            continue
                        tid = id(tip_tracker)
                        if tid in seen_tip_trackers:
                            continue
                        seen_tip_trackers.add(tid)
                        tip_tracker.disable()
                        disabled_tip_trackers.append(tip_tracker)
                    if hasattr(self, "_ros_node") and self._ros_node is not None:
                        self._ros_node.lab_logger().warning(
                            f"[dispense] retry with tip tracker disabled. error={retry_e}, vols={retry_vols}"
                        )
                    try:
                        try:
                            if _has_mixed_positive_volumes(retry_vols):
                                await _dispense_split_single_channel()
                            else:
                                await super().dispense(
                                    resources,
                                    retry_vols,
                                    use_channels,
                                    flow_rates,
                                    offsets,
                                    liquid_height,
                                    blow_out_air_volume,
                                    spread,
                                    **backend_kwargs,
                                )
                        except (TooLittleLiquidError, TooLittleVolumeError) as final_e:
                            if hasattr(self, "_ros_node") and self._ros_node is not None:
                                self._ros_node.lab_logger().warning(
                                    f"[dispense] final fallback no_volume_tracking. error={final_e}, vols={retry_vols}"
                                )
                            with no_volume_tracking():
                                if _has_mixed_positive_volumes(retry_vols):
                                    await _dispense_split_single_channel()
                                else:
                                    await super().dispense(
                                        resources,
                                        retry_vols,
                                        use_channels,
                                        flow_rates,
                                        offsets,
                                        liquid_height,
                                        blow_out_air_volume,
                                        spread,
                                        **backend_kwargs,
                                    )
                    finally:
                        for tracker in disabled_tip_trackers:
                            try:
                                tracker.enable()
                            except Exception:
                                pass
            else:
                actual_vols = retry_vols
        res_samples = []
        res_volumes = []
        # === [D-DBG] dispense 入口逐次抓取 resources/channels（候选 C/B）===
        # 单通道每次 len(resources) 应 == 1；==2 且两通道对同 well → 候选 C；
        # 与 [T-DBG] 配对：一条 transfer 的 [D-DBG] 出现次数应 == num_targets，
        # 若 > num_targets 或同一 well 被记录 2 次 → 候选 B 或候选 D。
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            try:
                _res_names = [f"{getattr(r.parent, 'name', '?')}/{r.name}" for r in resources]
                self._ros_node.lab_logger().info(
                    f"[D-DBG] dispense handler={id(self):x} "
                    f"n_res={len(resources)} channels={list(channels_to_use)} "
                    f"vols={list(actual_vols)} resources={_res_names}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[D-DBG] log failed: {_e}")
        for resource, volume, channel in zip(resources, actual_vols, channels_to_use):
            res_uuid = self.pending_liquids_dict[channel][EXTRA_SAMPLE_UUID]
            self.pending_liquids_dict[channel]["volume"] -= volume
            resource.unilabos_extra[EXTRA_SAMPLE_UUID] = res_uuid
            res_samples.append({"name": resource.name, EXTRA_SAMPLE_UUID: res_uuid})
            res_volumes.append(volume)
            # === [U2-DBG] PLR add_liquid 自带 history append 验证（双写根因锁定）===
            # 时机：super().dispense() 已返回；下面不再调 _append_liquid_history；
            # B1 修复后 history_pre_append_len 期望 == 2（PLR add_liquid 写了一条），
            # 与 [U-DBG] origin=dispense 的 history_lens 应保持一致 → 不再双写。
            if hasattr(self, "_ros_node") and self._ros_node is not None:
                try:
                    _pre_hist = list(getattr(getattr(resource, "tracker", None), "liquid_history", []) or [])
                    self._ros_node.lab_logger().info(
                        f"[U2-DBG] dispense pre_append "
                        f"name={getattr(resource.parent, 'name', '?')}/{resource.name} "
                        f"history_pre_append_len={len(_pre_hist)} "
                        f"history_pre_append={_pre_hist}"
                    )
                except Exception as _e:
                    self._ros_node.lab_logger().warning(f"[U2-DBG] log failed: {_e}")
            # P9 dispense history 由 PLR ``ContainerVolumeTracker.add_liquid`` 自动写入
            # （三元组 ``(name, vol, "ul")``，含液体身份），Uni-Lab 不再重复 append。
            # 详见 ``RESOLUTION-2026-05-28-plr-liquid-history-double-write.md`` §3 改动 1。

            # B2 —— 修复 PLR dispense 路径丢失液体身份:
            # ``LiquidHandler.dispense`` 调 ``op.resource.tracker.add_liquid(volume=op.volume)``
            # 没传 liquid 名 → target 末条 ``(Unknown<n>, +vol, "ul")``,bump target
            # tracker 的 ``unknown_counter``。这里从 aspirate 时挂在 channel 上的
            # ``pending_liquids_dict[channel]["liquid_name"]`` 取出权威液体名,
            # 把 target 末条改名为真实化学名(同 aspirate 端 ``_patch_unknown_history_last``
            # 策略),不增减 entry,不影响 PLR ``current_liquids`` / ``volume`` 的反推。
            channel_name = ""
            try:
                channel_meta = self.pending_liquids_dict.get(channel) if isinstance(self.pending_liquids_dict, dict) else None
                if isinstance(channel_meta, dict):
                    channel_name = str(channel_meta.get("liquid_name") or "")
            except Exception:
                channel_name = ""
            if not channel_name:
                # 兜底:从 tip 现有历史读取(aspirate 改名补丁已经把 tip 末条改成真实名)
                try:
                    tip = self.head[channel].get_tip()  # type: ignore[index]
                except Exception:
                    tip = None
                channel_name = _well_current_liquid_name(tip) if tip is not None else ""
            if channel_name:
                _patch_unknown_history_last(getattr(resource, "tracker", None), channel_name)

        if hasattr(self, "_ros_node") and self._ros_node is not None:
            # === [U-DBG] dispense 后 update_resource 上行 payload 抓取（候选 E 判别）===
            # 同一 well 在 set + dispense 阶段各上传一次时，看 history_lens 形态：
            #   [1] 然后 [2] → OS 发全量，下游 merge 错（候选 E.cloud）
            #   [1] 然后 [1] → OS 发增量，下游 append 拼起来（候选 E.diff）
            #   [2] 两次相同 → OS 重复发同一份 payload（候选 E.os_dup）
            try:
                _u_names = [f"{getattr(r.parent, 'name', '?')}/{r.name}" for r in resources]
                _u_lens = [len(getattr(getattr(r, "tracker", None), "liquid_history", []) or []) for r in resources]
                _u_hist = [getattr(getattr(r, "tracker", None), "liquid_history", []) for r in resources]
                _u_vols = [getattr(getattr(r, "tracker", None), "_used_volume", None) for r in resources]
                self._ros_node.lab_logger().info(
                    f"[U-DBG] origin=dispense ts={time.time():.3f} "
                    f"names={_u_names} history_lens={_u_lens} "
                    f"used_vols={_u_vols} histories={_u_hist}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[U-DBG] log failed (dispense): {_e}")
            unique_resources: List[Container] = []
            seen_resource_ids: Set[int] = set()
            for r in resources:
                rid = id(r)
                if rid in seen_resource_ids:
                    continue
                seen_resource_ids.add(rid)
                unique_resources.append(r)
            task = ROS2DeviceNode.run_async_func(
                self._ros_node.update_resource,
                True,
                **{"resources": unique_resources},
            )
            submit_time = time.time()
            while not task.done():
                if time.time() - submit_time > 10:
                    self._ros_node.lab_logger().info(f"dispense {resources} 超时")
                    break
                time.sleep(0.01)

        return SimpleReturn(samples=res_samples, volumes=res_volumes)

    async def transfer(
        self,
        source: Well,
        targets: List[Well],
        source_vol: Optional[float] = None,
        ratios: Optional[List[float]] = None,
        target_vols: Optional[List[float]] = None,
        aspiration_flow_rate: Optional[float] = None,
        dispense_flow_rates: Optional[List[Optional[float]]] = None,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.transfer(
                source,
                targets,
                source_vol,
                ratios,
                target_vols,
                aspiration_flow_rate,
                dispense_flow_rates,
                **backend_kwargs,
            )
        return await super().transfer(
            source,
            targets,
            source_vol,
            ratios,
            target_vols,
            aspiration_flow_rate,
            dispense_flow_rates,
            **backend_kwargs,
        )

    def use_channels(self, channels: List[int]):
        if self._simulator:
            self._simulate_handler.use_channels(channels)
        return super().use_channels(channels)

    async def pick_up_tips96(self, tip_rack: TipRack, offset: Coordinate = Coordinate.zero(), **backend_kwargs):
        if self._simulator:
            return await self._simulate_handler.pick_up_tips96(tip_rack, offset, **backend_kwargs)
        return await super().pick_up_tips96(tip_rack, offset, **backend_kwargs)

    async def drop_tips96(
        self,
        resource: Union[TipRack, Trash],
        offset: Coordinate = Coordinate.zero(),
        allow_nonzero_volume: bool = False,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.drop_tips96(resource, offset, allow_nonzero_volume, **backend_kwargs)
        return await super().drop_tips96(resource, offset, allow_nonzero_volume, **backend_kwargs)

    def _get_96_head_origin_tip_rack(self) -> Optional[TipRack]:
        return super()._get_96_head_origin_tip_rack()

    async def return_tips96(self, allow_nonzero_volume: bool = False, **backend_kwargs):
        if self._simulator:
            return await self._simulate_handler.return_tips96(allow_nonzero_volume, **backend_kwargs)
        return await super().return_tips96(allow_nonzero_volume, **backend_kwargs)

    async def discard_tips96(self, allow_nonzero_volume: bool = True, **backend_kwargs):
        if self._simulator:
            return await self._simulate_handler.discard_tips96(allow_nonzero_volume, **backend_kwargs)
        return await super().discard_tips96(allow_nonzero_volume, **backend_kwargs)

    async def aspirate96(
        self,
        resource: Union[Plate, Container, List[Well]],
        volume: float,
        offset: Coordinate = Coordinate.zero(),
        flow_rate: Optional[float] = None,
        liquid_height: Optional[float] = None,
        blow_out_air_volume: Optional[float] = None,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.aspirate96(
                resource,
                volume,
                offset,
                flow_rate,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                **backend_kwargs,
            )
        return await super().aspirate96(
            resource,
            volume,
            offset,
            flow_rate=flow_rate,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            **backend_kwargs,
        )

    async def dispense96(
        self,
        resource: Union[Plate, Container, List[Well]],
        volume: float,
        offset: Coordinate = Coordinate.zero(),
        flow_rate: Optional[float] = None,
        liquid_height: Optional[float] = None,
        blow_out_air_volume: Optional[float] = None,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.dispense96(
                resource,
                volume,
                offset,
                flow_rate,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                **backend_kwargs,
            )
        return await super().dispense96(
            resource,
            volume,
            offset,
            flow_rate=flow_rate,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            **backend_kwargs,
        )

    async def stamp(
        self,
        source: Plate,
        target: Plate,
        volume: float,
        aspiration_flow_rate: Optional[float] = None,
        dispense_flow_rate: Optional[float] = None,
    ):
        if self._simulator:
            return await self._simulate_handler.stamp(source, target, volume, aspiration_flow_rate, dispense_flow_rate)
        return await super().stamp(source, target, volume, aspiration_flow_rate, dispense_flow_rate)

    async def pick_up_resource(
        self,
        resource: Resource,
        offset: Coordinate = Coordinate.zero(),
        pickup_distance_from_top: float = 0,
        direction: GripDirection = GripDirection.FRONT,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.pick_up_resource(
                resource, offset, pickup_distance_from_top, direction, **backend_kwargs
            )
        return await super().pick_up_resource(resource, offset, pickup_distance_from_top, direction, **backend_kwargs)

    async def move_picked_up_resource(
        self,
        to: Coordinate,
        offset: Coordinate = Coordinate.zero(),
        direction: Optional[GripDirection] = None,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.move_picked_up_resource(to, offset, direction, **backend_kwargs)
        return await super().move_picked_up_resource(to, offset, direction, **backend_kwargs)

    async def drop_resource(
        self,
        destination: Union[ResourceStack, ResourceHolder, Resource, Coordinate],
        offset: Coordinate = Coordinate.zero(),
        direction: GripDirection = GripDirection.FRONT,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.drop_resource(destination, offset, direction, **backend_kwargs)
        return await super().drop_resource(destination, offset, direction, **backend_kwargs)

    async def move_resource(
        self,
        resource: Resource,
        to: Union[ResourceStack, ResourceHolder, Resource, Coordinate],
        intermediate_locations: Optional[List[Coordinate]] = None,
        pickup_offset: Coordinate = Coordinate.zero(),
        destination_offset: Coordinate = Coordinate.zero(),
        pickup_distance_from_top: float = 0,
        pickup_direction: GripDirection = GripDirection.FRONT,
        drop_direction: GripDirection = GripDirection.FRONT,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.move_resource(
                resource,
                to,
                intermediate_locations,
                pickup_offset,
                destination_offset,
                pickup_distance_from_top,
                pickup_direction,
                drop_direction,
                **backend_kwargs,
            )
        return await super().move_resource(
            resource,
            to,
            intermediate_locations,
            pickup_offset,
            destination_offset,
            pickup_distance_from_top,
            pickup_direction,
            drop_direction,
            **backend_kwargs,
        )

    async def move_lid(
        self,
        lid: Lid,
        to: Union[Plate, ResourceStack, Coordinate],
        intermediate_locations: Optional[List[Coordinate]] = None,
        pickup_offset: Coordinate = Coordinate.zero(),
        destination_offset: Coordinate = Coordinate.zero(),
        pickup_direction: GripDirection = GripDirection.FRONT,
        drop_direction: GripDirection = GripDirection.FRONT,
        pickup_distance_from_top: float = 5.7 - 3.33,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.move_lid(
                lid,
                to,
                intermediate_locations,
                pickup_offset,
                destination_offset,
                pickup_direction,
                drop_direction,
                pickup_distance_from_top,
                **backend_kwargs,
            )
        return await super().move_lid(
            lid,
            to,
            intermediate_locations,
            pickup_offset,
            destination_offset,
            pickup_direction,
            drop_direction,
            pickup_distance_from_top,
            **backend_kwargs,
        )

    async def move_plate(
        self,
        plate: Plate,
        to: Union[ResourceStack, ResourceHolder, Resource, Coordinate, int],
        intermediate_locations: Optional[List[Coordinate]] = None,
        pickup_offset: Coordinate = Coordinate.zero(),
        destination_offset: Coordinate = Coordinate.zero(),
        drop_direction: GripDirection = GripDirection.FRONT,
        pickup_direction: GripDirection = GripDirection.FRONT,
        pickup_distance_from_top: float = 13.2 - 3.33,
        **backend_kwargs,
    ):
        if self._simulator:
            return await self._simulate_handler.move_plate(
                plate,
                to,
                intermediate_locations,
                pickup_offset,
                destination_offset,
                drop_direction,
                pickup_direction,
                pickup_distance_from_top,
                **backend_kwargs,
            )
        res = await super().move_plate(
            plate,
            to,
            intermediate_locations,
            pickup_offset,
            destination_offset,
            drop_direction,
            pickup_direction,
            pickup_distance_from_top,
            **backend_kwargs,
        )
                # 上行物料状态（push 整棵 deck，确保 parent/slot 结构变更同步）。
        if getattr(self, "_ros_node", None) is not None and isinstance(self.deck, Deck):
            ROS2DeviceNode.run_async_func(
                self._ros_node.update_resource, True, **{"resources": [plate]}
            )
        return res

    def serialize(self):
        if self._simulator:
            self._simulate_handler.serialize()
        return super().serialize()

    @classmethod
    def deserialize(cls, data: dict, allow_marshal: bool = False) -> LiquidHandler:
        return super().deserialize(data, allow_marshal)

    @classmethod
    def load(cls, path: str) -> LiquidHandler:
        return super().load(path)

    async def prepare_for_manual_channel_operation(self, channel: int):
        if self._simulator:
            return await self._simulate_handler.prepare_for_manual_channel_operation(channel)
        return await super().prepare_for_manual_channel_operation(channel)

    async def move_channel_x(self, channel: int, x: float):
        if self._simulator:
            return await self._simulate_handler.move_channel_x(channel, x)
        return await super().move_channel_x(channel, x)

    async def move_channel_y(self, channel: int, y: float):
        if self._simulator:
            return await self._simulate_handler.move_channel_y(channel, y)
        return await super().move_channel_y(channel, y)

    async def move_channel_z(self, channel: int, z: float):
        if self._simulator:
            return await self._simulate_handler.move_channel_z(channel, z)
        return await super().move_channel_z(channel, z)

    def assign_child_resource(self, resource: Resource, location: Optional[Coordinate], reassign: bool = True):
        if self._simulator:
            self._simulate_handler.assign_child_resource(resource, location, reassign)
        pass

    async def probe_tip_presence_via_pickup(
        self, tip_spots: List[TipSpot], use_channels: Optional[List[int]] = None
    ) -> Dict[str, bool]:
        if self._simulator:
            return await self._simulate_handler.probe_tip_presence_via_pickup(tip_spots, use_channels)
        return await super().probe_tip_presence_via_pickup(tip_spots, use_channels)

    async def probe_tip_inventory(
        self,
        tip_spots: List[TipSpot],
        probing_fn: Optional[TipPresenceProbingMethod] = None,
        use_channels: Optional[List[int]] = None,
    ) -> Dict[str, bool]:
        if self._simulator:
            return await self._simulate_handler.probe_tip_inventory(tip_spots, probing_fn, use_channels)
        return await super().probe_tip_inventory(tip_spots, probing_fn, use_channels)

    async def consolidate_tip_inventory(self, tip_racks: List[TipRack], use_channels: Optional[List[int]] = None):
        if self._simulator:
            return await self._simulate_handler.consolidate_tip_inventory(tip_racks, use_channels)
        return await super().consolidate_tip_inventory(tip_racks, use_channels)


class LiquidHandlerAbstract(LiquidHandlerMiddleware):
    """Extended LiquidHandler with additional operations."""

    support_touch_tip = True
    _ros_node: BaseROS2DeviceNode

    def __init__(
        self,
        backend: LiquidHandlerBackend,
        deck: Deck,
        simulator: bool = False,
        channel_num: int = 8,
        total_height: float = 310,
        **kwargs,
    ):
        """Initialize a LiquidHandler.

        Args:
          backend: Backend to use.
          deck: Deck to use.
        """
        backend_type = None
        if isinstance(backend, dict) and "type" in backend:
            backend_dict = backend.copy()
            type_str = backend_dict.pop("type")
            try:
                # Try to get class from string using globals (current module), or fallback to pylabrobot or unilabos namespaces
                backend_cls = None
                if type_str in globals():
                    backend_cls = globals()[type_str]
                else:
                    # Try resolving dotted notation, e.g. "xxx.yyy.ClassName"
                    components = type_str.split(".")
                    mod = None
                    if len(components) > 1:
                        module_name = ".".join(components[:-1])
                        try:
                            import importlib

                            mod = importlib.import_module(module_name)
                        except ImportError:
                            mod = None
                        if mod is not None:
                            backend_cls = getattr(mod, components[-1], None)
                    if backend_cls is None:
                        # Try pylabrobot style import (if available)
                        try:
                            import pylabrobot

                            backend_cls = getattr(pylabrobot, type_str, None)
                        except Exception:
                            backend_cls = None
                if backend_cls is not None and isinstance(backend_cls, type):
                    if simulator:
                        backend_type = LiquidHandlerChatterboxBackend(channel_num)
                    else:
                        init_kwargs = dict(backend_dict)
                        init_kwargs["total_height"] = total_height
                        init_kwargs.update(kwargs)
                        backend_type = backend_cls(**init_kwargs)
            except Exception as exc:
                raise RuntimeError(f"Failed to convert backend type '{type_str}' to class: {exc}")
        else:
            backend_type = backend
        self._simulator = simulator
        self.group_info = dict()
        # P10 v2 — Tip 复用判等开关；默认 on（pop 出 kwargs 避免污染父类签名）。
        # 详见 ``product_designs/protocol_convert/10-tip-reuse-by-liquid-history.md`` §3.6。
        self._tip_reuse_by_liquid_name: bool = bool(
            kwargs.pop("tip_reuse_by_liquid_name", True)
        )
        super().__init__(backend_type, deck, simulator, channel_num, total_height=total_height, **kwargs)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        super().post_init(ros_node)
        ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })

    async def _resolve_to_plr_resources(
        self,
        items: Sequence[Union[Container, TipRack, Dict[str, Any]]],
    ) -> List[Union[Container, TipRack]]:
        """将 dict 格式的资源解析为 PLR 实例。若全部已是 PLR，直接返回。"""
        # 容错：上游可能传入 None（例如某些 transfer 步骤缺省 sources/targets/tip_racks），
        # 直接当作空序列处理，避免 ``enumerate(None)`` 抛 ``TypeError: 'NoneType' object is not iterable``。
        if items is None:
            return []
        dict_items = [(i, x) for i, x in enumerate(items) if isinstance(x, dict)]
        if not dict_items:
            return list(items)
        if not hasattr(self, "_ros_node") or self._ros_node is None:
            raise ValueError(
                "传入 dict 格式的 sources/targets/tip_racks 时，需通过 post_init 注入 _ros_node，"
                "才能从物料系统按 uuid 解析为 PLR 资源。"
            )
        uuids = [x.get("uuid") or x.get("unilabos_uuid") for _, x in dict_items]
        if any(u is None for u in uuids):
            raise ValueError("dict 格式的资源必须包含 uuid 或 unilabos_uuid 字段")

        def _resolve_from_local_by_uuids() -> List[Union[Container, TipRack]]:
            resolved_locals: List[Union[Container, TipRack]] = []
            missing: List[str] = []
            for uid in uuids:
                matches = self._ros_node.resource_tracker.figure_resource({"uuid": uid}, try_mode=True)
                if matches:
                    resolved_locals.append(cast(Union[Container, TipRack], matches[0]))
                else:
                    missing.append(str(uid))
            if missing:
                raise ValueError(
                    f"远端资源树未返回且本地资源也未命中，缺失 UUID: {missing}"
                )
            return resolved_locals

        # 优先走远端资源树查询；若远端为空或 requested_uuids 无法解析，则降级到本地 tracker 按 UUID 解析。
        resolved = []
        try:
            resource_tree = await self._ros_node.get_resource(uuids)
            plr_list = resource_tree.to_plr_resources(requested_uuids=uuids)
            for uid, plr in zip(uuids, plr_list):
                local_matches = self._ros_node.resource_tracker.figure_resource({"uuid": uid}, try_mode=True)
                if local_matches:
                    local = cast(Union[Container, TipRack], local_matches[0])
                else:
                    local = cast(Union[Container, TipRack], plr)
                if hasattr(plr, "unilabos_extra") and hasattr(local, "unilabos_extra"):
                    local.unilabos_extra = getattr(plr, "unilabos_extra", {}).copy()
                if local is not plr and hasattr(plr, "tracker") and hasattr(local, "tracker"):
                    local_tracker = local.tracker
                    plr_tracker = plr.tracker
                    local_history = getattr(local_tracker, "liquid_history", None)
                    plr_history = getattr(plr_tracker, "liquid_history", None)
                    if (isinstance(local_history, list) and len(local_history) == 0
                            and isinstance(plr_history, list) and len(plr_history) > 0):
                        # P9 — 远端 history 归一为 v3 dict（plr_history 可能仍是 v2 tuple）
                        normalized_history = _normalize_liquid_history(plr_history)
                        local_tracker.liquid_history = normalized_history
                    elif (isinstance(local_history, list) and len(local_history) > 0
                            and isinstance(plr_history, list) and len(plr_history) == 0):
                        # 远端认为容器为空，重置本地 tracker 以保持同步
                        local_tracker.liquid_history = []
                resolved.append(local)
            if len(resolved) != len(uuids):
                raise ValueError(
                    f"远端资源解析数量不匹配: requested={len(uuids)}, resolved={len(resolved)}"
                )
        except Exception:
            resolved = _resolve_from_local_by_uuids()

        result = list(items)
        for (idx, orig_dict), res in zip(dict_items, resolved):
            if isinstance(orig_dict, dict) and hasattr(res, "tracker"):
                tracker = res.tracker
                local_history = getattr(tracker, "liquid_history", None)
                data = orig_dict.get("data") or {}
                dict_history = data.get("liquid_history")
                # P9 — 多形态升级：v3 dict / v2 tuple / list[str] 全归一为 v3 dict 列表。
                # 详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.4。
                if isinstance(local_history, list) and len(local_history) == 0:
                    if isinstance(dict_history, list) and len(dict_history) > 0:
                        normalized_history = _normalize_liquid_history(dict_history)
                        tracker.liquid_history = normalized_history
                # local 非空时永远保留 —— ``dict_history`` 仅是 caller 构造资源时的 snapshot，
                # caller 无法追踪 runtime 写入（aspirate/dispense/set_liquid），发 ``[]`` 并不代表容器真为空。
                # 以 caller snapshot 覆盖 live state 会把累积的所有 transfer / set_liquid 痕迹擦掉。
                # 详见 debug session dc5aa5（实证：一次 transfer 入口的 resolve 把整板 96 个 target wells
                # 的 history 全清，导致 ``liquids`` 派生为空）。
            result[idx] = res
        return result

    @classmethod
    def set_liquid(cls, wells: list[Well], liquid_names: list[str], volumes: list[float]) -> SetLiquidReturn:
        """Set the liquid in a well.

        如果 liquid_names 和 volumes 为空，但 wells 不为空，直接返回 wells。
        """
        res_volumes = []
        # 如果 liquid_names 和 volumes 都为空，直接返回 wells
        if not liquid_names and not volumes:
            return SetLiquidReturn(
                wells=ResourceTreeSet.from_plr_resources(wells, known_newly_created=False).dump(), volumes=res_volumes  # type: ignore
            )

        def _clamp_volume(resource: Union[Well, Container], volume: float) -> float:
            # 防止初始化液量超过容器容量，导致后续 dispense 时 free volume 为负
            clamped = max(float(volume), 0.0)
            max_volume = getattr(resource, "max_volume", None)
            if isinstance(max_volume, (int, float)) and max_volume > 0:
                clamped = min(clamped, float(max_volume))
            return clamped

        for well, liquid_name, volume in zip(wells, liquid_names, volumes):
            safe_volume = _clamp_volume(well, volume)
            tracker = getattr(well, "tracker", None)

            # 防御性跳过：set_liquid_from_plate 对 target 孔位用 **0 体积**做"占位初始化"。
            # 若该孔已被前序 stage 的 transfer 注入液体（used_volume > 0），再用 0 覆盖会令 PLR
            # ``set_liquids`` 删除旧液体，在 liquid_history 写入一条负记录（如 ``('', -25.6)``），
            # 把已移入的液体清零 —— 导致多 stage 累加丢失前序体积（最终只剩最后一次 dispense）。
            # 这里在"0 体积占位 + 孔已非空"时跳过破坏性覆盖，保证最终体积 = 各 stage dispense 之和。
            # 与「初始化前置」（workflow/common.py 让所有 set_liquid 排在首个 transfer 前）互为
            # 兜底：即便调度时序异常使某次占位初始化晚于一次 dispense，也不会再清零累积液体。
            if safe_volume <= 1e-9:
                try:
                    existing_used = float(tracker.get_used_volume()) if tracker is not None else 0.0
                except Exception:
                    existing_used = 0.0
                if existing_used > 1e-9:
                    res_volumes.append(existing_used)
                    continue

            # set_liquid 是 history 的"播种"入口（Stage 3 set_liquid_from_plate 节点会调到这里）。
            # PLR ``liquids.setter`` 在 ``abs(vol) > 1e-9`` 时会自动 append 一条 ``(name, vol, "ul")``；
            # 当 ``vol == 0`` 时 PLR 跳过，Uni-Lab 走 ``_append_liquid_history`` 兜底。
            # 注（2026-05-28 用户决策）：``_append_liquid_history`` 已在 helper 内对 0-vol 早 return，
            # 所以 ``set_liquid(name, 0)`` **不再产生 history 占位条目**；但 ``set_liquids`` 已把
            # ``(name, 0.0)`` 写入 ``tracker.liquids`` —— 液体身份仍保留（well_current_liquid_name /
            # tip-reuse 仍可用），仅审计日志不冗余 0-vol 噪声。
            # 详见 ``RESOLUTION-2026-05-28-plr-liquid-history-double-write.md`` §3 改动 3
            # + ``liquid_history.py:append_liquid_history`` 顶部 0-vol guard。
            hist_ref = getattr(tracker, "liquid_history", None) if tracker is not None else None
            len_before = len(hist_ref) if isinstance(hist_ref, list) else 0
            well.set_liquids([(liquid_name, safe_volume)])  # type: ignore
            len_after = len(hist_ref) if isinstance(hist_ref, list) else 0
            if len_after == len_before:
                _append_liquid_history(well, liquid_name, safe_volume, "set")
            res_volumes.append(safe_volume)

        return SetLiquidReturn(
            wells=ResourceTreeSet.from_plr_resources(wells, known_newly_created=False).dump(), volumes=res_volumes  # type: ignore
        )

    @staticmethod
    def _safe_get_well(plate, name: str):
        """取孔；对**单行载体**（trough / reservoir，``num_items_y == 1``）做容错。

        8 通道展开 / 跨板合并常把单行储液槽的孔位行扩成 A–H（如把 ``A1`` 误记成 ``F2``），
        而 trough 物理上只有一行：此处把行夹到 ``A``、列夹到容量内再取。
        **多行板不夹**（越界=真实错误，留给各自的几何修复，避免悄悄改写正常孔位）。
        """
        try:
            return plate.get_well(name)
        except Exception:
            ny = int(getattr(plate, "num_items_y", 0) or 0)
            if ny == 1:
                m = re.match(r"^([A-Za-z]+)(\d+)$", str(name).strip())
                if m:
                    nx = int(getattr(plate, "num_items_x", 0) or 0)
                    col = int(m.group(2))
                    if nx >= 1:
                        col = min(max(col, 1), nx)
                    return plate.get_well(f"A{col}")
            raise

    def _resolve_wells_from_plate(
        self,
        plate: Union[Plate, TubeRack, ResourceSlot],
        well_names: list[str],
    ) -> list[Well]:
        """旧签名兼容路径：plate + well_names → 顺序 Well 列表。"""
        assert issubclass(plate.__class__, Plate) or issubclass(plate.__class__, TubeRack), (
            f"plate must be a Plate or TubeRack, now: {type(plate)}"
        )
        if issubclass(plate.__class__, Plate):
            return [self._safe_get_well(plate, name) for name in well_names]  # type: ignore
        return [self._resolve_tube_compat(plate, name) for name in well_names]  # type: ignore

    @staticmethod
    def _resolve_tube_compat(rack: TubeRack, name: str) -> Container:
        """从 TubeRack 取液体容器（Tube），兼容新版 PLR 的 holder 模型。

        新版 PLR 把 ``TubeRack`` 定义为 ``ItemizedResource[ResourceHolder]``，其
        ``get_tube`` 返回 ``holder.resource``；而 Uni-Lab 的 ``PRCXI9300TubeRack``
        把 ``Tube`` 直接作为子资源（``get_item`` 即 Tube），导致 ``get_tube`` 恒返回
        ``None`` —— 进而让 set_liquid 收到 ``None`` well 并崩溃。这里优先 ``get_tube``，
        为 ``None`` 时回退到 ``get_item``（若子资源是 holder 再取 ``.resource``）。
        """
        tube = rack.get_tube(name)
        if tube is not None:
            return tube  # type: ignore[return-value]
        item = rack.get_item(name)
        return getattr(item, "resource", item)

    def _coerce_well(self, w: Union[Well, Dict[str, Any]]) -> Well:
        """dict → PLR Well：通过 self._ros_node.resource_tracker 同步解析；Well 原样返回。

        约定 dict 至少含 ``uuid`` 或 ``unilabos_uuid`` 字段，与
        ``_resolve_to_plr_resources`` 的入参 schema 对齐。
        """
        if isinstance(w, Well):
            return w
        if isinstance(w, dict):
            uid = w.get("uuid") or w.get("unilabos_uuid")
            if uid is None:
                # P3 fallback：无 uuid 时按 parent 物理板名 + 孔坐标从 resource_tracker 解析。
                # 转换层（workflow/common.py）已把 well.parent 写成物理板名 ``{class}_slot_{slot}``
                # （与 create_resource 的 res_id 对齐）；运行时 wells_identifier 边未覆盖 wells 时
                # （edge 未生效 / 旧 schema），仍可凭 parent + 孔坐标定位到已创建的板并取孔。
                resolved = self._resolve_well_by_parent_ref(w)
                if resolved is not None:
                    return resolved
                raise TypeError(
                    f"dict 格式的 well 无法解析：缺 uuid 且 parent={w.get('parent')!r} 未在 "
                    f"resource_tracker 中找到对应板（well={w.get('name')!r}）。请确认上游 "
                    f"create_resource 已创建该物理板，且转换层 wells.parent 使用物理板名"
                    f"（{{class}}_slot_{{slot}}）。原始: {w!r}"
                )
            if not hasattr(self, "_ros_node") or self._ros_node is None:
                raise ValueError(
                    "传入 dict 格式的 wells 时，需通过 post_init 注入 _ros_node，"
                    "才能从物料系统按 uuid 解析为 PLR Well。"
                )
            matches = self._ros_node.resource_tracker.figure_resource(
                {"uuid": uid}, try_mode=True
            )
            if not matches:
                raise ValueError(
                    f"无法解析 well: uuid={uid!r} 未在 resource_tracker 中找到（"
                    f"name={w.get('name')!r}, parent={w.get('parent')!r}）"
                )
            return cast(Well, matches[0])
        raise TypeError(f"无法解析 well: {w!r}")

    def _resolve_well_by_parent_ref(self, w: Dict[str, Any]) -> Optional[Well]:
        """无 uuid 的 well dict → PLR Well：按 ``parent`` 物理板名 + 孔坐标解析。

        ``w`` 形如 ``{"id": "<plate>/<well>", "name": "<plate>/<well>", "parent": "<plate>"}``，
        其中 ``<plate>`` 是物理板名（``{class}_slot_{slot}``，与 create_resource res_id 对齐）。
        在 ``resource_tracker`` 中按板名定位 Plate / TubeRack，再按孔坐标取孔；
        TubeRack 复用 :meth:`_resolve_tube_compat` 兼容新版 PLR holder 模型。
        解析不到返回 ``None``（由调用方决定是否抛错）。
        """
        tr = getattr(getattr(self, "_ros_node", None), "resource_tracker", None)
        if tr is None:
            return None
        parent_name = w.get("parent")
        ref_name = w.get("name") or w.get("id") or ""
        coord = ref_name.rsplit("/", 1)[-1] if isinstance(ref_name, str) and "/" in ref_name else ref_name
        if not parent_name or not coord:
            return None
        try:
            matches = tr.figure_resource({"name": parent_name}, try_mode=True)
        except Exception:
            return None
        plate = matches[0] if isinstance(matches, list) and matches else None
        if plate is None:
            return None
        try:
            if issubclass(plate.__class__, Plate):
                return cast(Well, self._safe_get_well(plate, coord))
            if issubclass(plate.__class__, TubeRack):
                return cast(Well, self._resolve_tube_compat(plate, coord))
        except Exception:
            return None
        return None

    def _set_liquid_grouped_by_plate(
        self,
        wells: list[Well],
        liquid_names: list[str],
        volumes: list[float],
    ) -> SetLiquidFromPlateReturn:
        """按 ``well.parent`` 分桶后多次 ``self.set_liquid``，最终按原顺序拼回 volumes。

        作为 ``set_liquid_from_plate`` 的唯一执行路径（新旧两条入口都收敛到这里）。
        """
        n = len(wells)

        # 收集涉及的 plate 实例（按首次出现顺序），用于返回 plate 字段
        plate_objs: List[Union[Plate, TubeRack]] = []
        seen_plates: Set[str] = set()
        for w in wells:
            parent = getattr(w, "parent", None)
            if parent is None:
                continue
            pname = getattr(parent, "name", None) or str(id(parent))
            if pname in seen_plates:
                continue
            seen_plates.add(pname)
            plate_objs.append(cast(Union[Plate, TubeRack], parent))

        # 早返回：liquid_names / volumes 均为空 → 仅回显 wells / plates
        if not liquid_names and not volumes:
            return SetLiquidFromPlateReturn(
                plate=ResourceTreeSet.from_plr_resources(plate_objs, known_newly_created=False).dump() if plate_objs else [],  # type: ignore
                wells=ResourceTreeSet.from_plr_resources(wells, known_newly_created=False).dump() if wells else [],  # type: ignore
                volumes=[],
            )

        if len(liquid_names) != n or len(volumes) != n:
            raise ValueError(
                f"set_liquid_from_plate: len(wells)={n}, len(liquid_names)={len(liquid_names)}, "
                f"len(volumes)={len(volumes)} 三者必须等长"
            )

        # 按 parent 分桶；记录原始 index 以便结果回拼
        buckets: Dict[str, List[int]] = {}
        for idx, w in enumerate(wells):
            parent = getattr(w, "parent", None)
            key = getattr(parent, "name", None) if parent is not None else None
            key = key if key is not None else "_orphan"
            buckets.setdefault(key, []).append(idx)

        res_volumes: List[float] = [0.0] * n

        # 按 plate 顺序串行 set_liquid（避免设备物理碰撞 / 同板批量处理）
        for plate_key, idxs in buckets.items():
            sub_wells = [wells[i] for i in idxs]
            sub_names = [liquid_names[i] for i in idxs]
            sub_vols = [volumes[i] for i in idxs]
            sub_ret = self.set_liquid(sub_wells, sub_names, sub_vols)
            sub_ret_volumes = sub_ret.get("volumes", []) if isinstance(sub_ret, dict) else getattr(sub_ret, "volumes", [])
            for local_idx, orig_idx in enumerate(idxs):
                if local_idx < len(sub_ret_volumes):
                    res_volumes[orig_idx] = float(sub_ret_volumes[local_idx])

        # 同步资源到 ROS（每板独立 wells 列表，但 update_resource 一次性提交更高效）
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            # === [U-DBG] set_liquid_from_plate 后 update_resource 上行 payload 抓取（候选 E 判别）===
            # 与 origin=dispense 那条配对看：同一 well 两次 history_lens 是 [1]→[2] / [1]→[1] / [2]→[2]，
            # 分别对应 E.cloud / E.diff / E.os_dup（详见文件顶部 dispense 处注释）。
            try:
                _u_names = [f"{getattr(w.parent, 'name', '?')}/{w.name}" for w in wells]
                _u_lens = [len(getattr(getattr(w, "tracker", None), "liquid_history", []) or []) for w in wells]
                _u_hist = [getattr(getattr(w, "tracker", None), "liquid_history", []) for w in wells]
                _u_vols = [getattr(getattr(w, "tracker", None), "_used_volume", None) for w in wells]
                self._ros_node.lab_logger().info(
                    f"[U-DBG] origin=set_liquid ts={time.time():.3f} "
                    f"plates={list(buckets.keys())} "
                    f"names={_u_names} history_lens={_u_lens} "
                    f"used_vols={_u_vols} histories={_u_hist}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[U-DBG] log failed (set_liquid): {_e}")
            task = ROS2DeviceNode.run_async_func(
                self._ros_node.update_resource, True, **{"resources": wells}
            )
            submit_time = time.time()
            while not task.done():
                if time.time() - submit_time > 10:
                    self._ros_node.lab_logger().info(
                        f"set_liquid_from_plate (grouped) 超时, plates={list(buckets.keys())}"
                    )
                    break
                time.sleep(0.01)

        return SetLiquidFromPlateReturn(
            plate=ResourceTreeSet.from_plr_resources(plate_objs, known_newly_created=False).dump() if plate_objs else [],  # type: ignore
            wells=ResourceTreeSet.from_plr_resources(wells, known_newly_created=False).dump(),  # type: ignore
            volumes=res_volumes,
        )

    def set_liquid_from_plate(
        self,
        wells: Optional[Sequence[Union[Well, Dict[str, Any]]]] = None,
        liquid_names: Optional[list[str]] = None,
        volumes: Optional[list[float]] = None,
        *,
        plate: Optional[Union[Plate, TubeRack, ResourceSlot]] = None,
        well_names: Optional[list[str]] = None,
    ) -> SetLiquidFromPlateReturn:
        """按孔批量设定液体（P3 框选化）。

        优先路径（新签名，推荐）：

            set_liquid_from_plate(
                wells=[well_obj_or_dict, ...],
                liquid_names=["...", ...],
                volumes=[v, ...],
            )

        ``wells`` 中元素既可以是 PLR ``Well`` 实例，也可以是含 ``uuid`` 字段的 dict
        （由 ``resource_tracker`` 同步解析）；允许跨多 plate，内部按 ``well.parent``
        分桶后多次调用 :meth:`set_liquid`。

        兼容路径（旧签名，仅在 ``wells`` 为 ``None`` 时启用）：

            set_liquid_from_plate(plate=plate, well_names=["A1","A2",...],
                                  liquid_names=[...], volumes=[...])

        Parameters
        ----------
        wells
            待设液的 Well 列表（含 PLR 实例或 dict 引用），跨板允许。
        liquid_names
            与 ``wells`` 等长的液体名列表。
        volumes
            与 ``wells`` 等长的体积列表（µL）；内部会按容器容量上限 clamp。
        plate, well_names
            旧调用约定，仅当 ``wells`` 未传时生效。
        """

        # ============================================================
        # P3 框选化兼容修复：上游 ROS placeholder 在解析
        # ``wells_identifier`` 边（create_resource.labware → 本节点）时，
        # 可能直接把单个 PLR Plate 资源 dict 写入 ``wells``，而非
        # ``list[Well]``。多入边时只保留最后一条（H7），导致 §14 跨板
        # merged 节点失去除最后 plate 外的入边。
        #
        # 检测此 schema 错位并按以下策略恢复：
        #   - 若 liquid_names 全相同 → 单 plate 场景，wells 视为该 plate
        #     走 plate + well_names 旧路径。
        #   - 若 liquid_names 含 distinct names（§14 merged 跨板场景）→
        #     按 liquid_names 逐个反查 resource_tracker 得到各自 plate，
        #     再用 well_names[i] 取 plate.get_well 构造跨板 wells 列表。
        # ============================================================
        if (
            isinstance(wells, dict)
            and "class" in wells
            and well_names is not None
            and (plate is None or (isinstance(plate, list) and len(plate) == 0))
        ):
            # 判别单 plate vs 跨 plate：liquid_names 是否含 distinct names
            _ln = list(liquid_names or [])
            _wn = list(well_names or [])
            distinct_liquids = set(_ln) if _ln else set()
            # P2 v2 §14 fix（2026-05-27）：well_names 全部含 "<plate_name>/<well>" prefix
            # 是 common.py merged 节点的权威信号 —— 即便 liquid_names 全相同
            # （同一 reagent 跨多板分装，例如 "agar" 同时写入 slot_3/5/6/7/13），
            # 也必须走 cross-plate 分支按 prefix 逐 well 定位 plate；否则会落到
            # 单 plate fallback，把 prefixed well_names 喂给错的 plate 触发
            # ``IndexError: 'PRCXI_..._slot_3/A5' does not exist on resource 'PRCXI_..._slot_5'``。
            has_prefixed_well_names = (
                len(_wn) > 0
                and all(isinstance(w, str) and "/" in w for w in _wn)
            )
            is_cross_plate = (
                has_prefixed_well_names
                or (
                    len(distinct_liquids) > 1
                    and len(_ln) == len(_wn)
                    and len(_wn) > 1
                )
            )

            if is_cross_plate:
                # 跨板 merged 场景：优先按 well_names 中的 "<plate_plr_name>/<well>" prefix
                # 拆解逐个查 plate（common.py §14 fix 把 plate name 编码进 well_names）。
                # 兜底：若 well_names 不含 "/"，按 liquid_names 当 reagent_key 查（通常 miss）。
                resolved_cross: list[Well] = []
                cross_resolve_errors: list[str] = []
                tracker = getattr(self, "_ros_node", None)
                tracker = tracker.resource_tracker if tracker is not None else None
                use_prefixed = all(isinstance(wn, str) and "/" in wn for wn in _wn)

                for idx, (reagent_key, w_name) in enumerate(zip(_ln, _wn)):
                    try:
                        plate_instance = None
                        if use_prefixed and "/" in w_name:
                            # 主路径（§14 fix）：well_names[i] = "<plate_plr_name>/<well>"
                            plate_plr_name, real_well_name = w_name.rsplit("/", 1)
                            if tracker is not None:
                                figured = tracker.figure_resource(
                                    {"name": plate_plr_name}, try_mode=True
                                )
                                if figured:
                                    plate_instance = figured[0]
                            actual_well_name = real_well_name
                        else:
                            # 兜底：legacy 形态（well_names 是纯 well 名）
                            actual_well_name = w_name
                            if tracker is not None:
                                figured = tracker.figure_resource(
                                    {"name": reagent_key}, try_mode=True
                                )
                                if figured:
                                    plate_instance = figured[0]
                                else:
                                    figured = tracker.figure_resource(
                                        {"id": reagent_key}, try_mode=True
                                    )
                                    if figured:
                                        plate_instance = figured[0]

                        if plate_instance is None:
                            cross_resolve_errors.append(
                                f"idx={idx} reagent_key={reagent_key!r} w_name={w_name!r}: resource_tracker miss"
                            )
                            continue
                        if not (
                            issubclass(plate_instance.__class__, Plate)
                            or issubclass(plate_instance.__class__, TubeRack)
                        ):
                            cross_resolve_errors.append(
                                f"idx={idx} reagent_key={reagent_key!r}: not Plate/TubeRack (got {type(plate_instance).__name__})"
                            )
                            continue
                        if issubclass(plate_instance.__class__, Plate):
                            resolved_cross.append(self._safe_get_well(plate_instance, actual_well_name))
                        else:
                            resolved_cross.append(
                                self._resolve_tube_compat(plate_instance, actual_well_name)
                            )
                    except Exception as _e:
                        cross_resolve_errors.append(
                            f"idx={idx} reagent_key={reagent_key!r} well={w_name!r}: {type(_e).__name__}: {_e}"
                        )

                if len(resolved_cross) == len(_wn):
                    return self._set_liquid_grouped_by_plate(
                        resolved_cross,
                        _ln,
                        list(volumes or []),
                    )
                # 跨板 fallback 解析失败 → 抛清晰错误，避免静默落回单 plate 单 well 错误降级。
                # 触发原因通常是 legacy 工作流图（common.py §14 fix 之前生成）的 well_names
                # 缺少 "<plate_plr_name>/<well>" prefix，导致 abstract 层无法跨板定位 plate。
                raise ValueError(
                    "set_liquid_from_plate: 检测到 P2 v2 跨板 merged 节点"
                    f"（liquid_names 含 {len(distinct_liquids)} 个 distinct names），"
                    "但 well_names 解析失败 / 缺少 '<plate_plr_name>/<well>' prefix。"
                    "这通常是 LEGACY 工作流图（在 §14 well_names prefix fix 之前生成）。"
                    "请用最新版 common.py 重新转换 + 重新上传协议到 Cloud Lab。"
                    f"\n  current well_names sample: {_wn[:3]}"
                    f"\n  current liquid_names sample: {_ln[:3]}"
                    f"\n  cross_resolve errors first3: {cross_resolve_errors[:3]}"
                )

            # 单 plate 兼容路径（或跨板解析失败 fallback）
            plate_data = wells
            wells = None  # 清空，让下面走旧路径
            try:
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    figured = self._ros_node.resource_tracker.figure_resource(
                        {"name": plate_data.get("name")}, try_mode=True
                    )
                    if figured:
                        plate = figured[0]
                if plate is None or (isinstance(plate, list) and len(plate) == 0):
                    from unilabos.resources.resource_tracker import ResourceTreeSet
                    fallback_tree = ResourceTreeSet.from_raw_dict_list([plate_data])
                    plr_list = fallback_tree.to_plr_resources() if len(fallback_tree.trees) > 0 else []
                    if plr_list:
                        plate = plr_list[0]
            except Exception:
                pass

        if wells is None:
            if plate is None or well_names is None or (isinstance(plate, list) and len(plate) == 0):
                raise ValueError(
                    "set_liquid_from_plate: 必须传 wells，或同时传 plate + well_names"
                )
            resolved_wells = self._resolve_wells_from_plate(plate, well_names)
        else:
            resolved_wells = [self._coerce_well(w) for w in wells]

        return self._set_liquid_grouped_by_plate(
            resolved_wells,
            list(liquid_names or []),
            list(volumes or []),
        )

    # ---------------------------------------------------------------
    # REMOVE LIQUID --------------------------------------------------
    # ---------------------------------------------------------------

    def set_group(self, group_name: str, wells: List[Well], volumes: List[float]):
        if self.channel_num == 8 and len(wells) != 8:
            raise RuntimeError(f"Expected 8 wells, got {len(wells)}")
        self.group_info[group_name] = wells
        self.set_liquid(wells, [group_name] * len(wells), volumes)

    async def transfer_group(self, source_group_name: str, target_group_name: str, unit_volume: float):

        source_wells = self.group_info.get(source_group_name, [])
        target_wells = self.group_info.get(target_group_name, [])

        rack_info = dict()
        for child in self.deck.children:
            if issubclass(child.__class__, TipRack):
                rack: TipRack = cast(TipRack, child)
                if "plate" not in rack.name.lower():
                    for tip in rack.get_all_tips():
                        if unit_volume > tip.maximal_volume:
                            break
                        else:
                            rack_info[rack.name] = (rack, tip.maximal_volume - unit_volume)

        if len(rack_info) == 0:
            raise ValueError(f"No tip rack can support volume {unit_volume}.")

        rack_info = sorted(rack_info.items(), key=lambda x: x[1][1])
        for child in self.deck.children:
            if child.name == rack_info[0][0]:
                target_rack = child
        target_rack = cast(TipRack, target_rack)
        available_tips = {}
        for idx, tipSpot in enumerate(target_rack.get_all_items()):
            if tipSpot.has_tip():
                available_tips[idx] = tipSpot
                continue
        # 一般移动液体有两种方式，一对多和多对多
        print("channel_num", self.channel_num)
        if self.channel_num == 8:

            tip_prefix = list(available_tips.values())[0].name.split("_")[0]
            colnum_list = [int(tip.name.split("_")[-1][1:]) for tip in available_tips.values()]
            available_cols = [colnum for colnum, count in dict(Counter(colnum_list)).items() if count == 8]
            available_cols.sort()
            available_tips_dict = {tip.name: tip for tip in available_tips.values()}
            tips_to_use = [available_tips_dict[f"{tip_prefix}_{chr(65 + i)}{available_cols[0]}"] for i in range(8)]
            print("tips_to_use", tips_to_use)
            await self.pick_up_tips(tips_to_use, use_channels=list(range(0, 8)))
            print("source_wells", source_wells)
            await self.aspirate(source_wells, [unit_volume] * 8, use_channels=list(range(0, 8)))
            print("target_wells", target_wells)
            await self.dispense(target_wells, [unit_volume] * 8, use_channels=list(range(0, 8)))
            await self.discard_tips(use_channels=list(range(0, 8)))

        elif self.channel_num == 1:

            for num_well in range(len(target_wells)):
                tip_to_use = available_tips[list(available_tips.keys())[num_well]]
                print("tip_to_use", tip_to_use)
                await self.pick_up_tips([tip_to_use], use_channels=[0])
                print("source_wells", source_wells)
                print("target_wells", target_wells)
                if len(source_wells) == 1:
                    await self.aspirate([source_wells[0]], [unit_volume], use_channels=[0])
                else:
                    await self.aspirate([source_wells[num_well]], [unit_volume], use_channels=[0])
                await self.dispense([target_wells[num_well]], [unit_volume], use_channels=[0])
                await self.discard_tips(use_channels=[0])

        else:
            raise ValueError(f"Unsupported channel number {self.channel_num}.")

    async def create_protocol(
        self,
        protocol_name: str,
        protocol_description: str,
        protocol_version: str,
        protocol_author: str,
        protocol_date: str,
        protocol_type: str,
        none_keys: List[str] = [],
    ):
        """Create a new protocol with the given metadata."""
        pass

    async def remove_liquid(
        self,
        vols: List[float],
        sources: Sequence[Container],
        waste_liquid: Optional[Container] = None,
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        delays: Optional[List[int]] = None,
        is_96_well: Optional[bool] = False,
        top: Optional[List[float]] = None,
        none_keys: List[str] = [],
    ):
        """A complete *remove* (aspirate → waste) operation."""

        try:
            if is_96_well:
                pass  # This mode is not verified.
            else:
                # 首先应该对任务分组，然后每次1个/8个进行操作处理
                if len(use_channels) == 1 and self.backend.num_channels == 1:

                    for _ in range(len(sources)):
                        tip = []
                        for __ in range(len(use_channels)):
                            tip.append(self._get_next_tip())
                        await self.pick_up_tips(tip)
                        await self.aspirate(
                            resources=[sources[_]],
                            vols=[vols[_]],
                            use_channels=use_channels,
                            flow_rates=[flow_rates[0]] if flow_rates else None,
                            offsets=[offsets[0]] if offsets else None,
                            liquid_height=[liquid_height[0]] if liquid_height else None,
                            blow_out_air_volume=[blow_out_air_volume[0]] if blow_out_air_volume else None,
                            spread=spread,
                        )
                        if delays is not None:
                            await self.custom_delay(seconds=delays[0])

                        await self.dispense(
                            resources=[waste_liquid],
                            vols=[vols[_]],
                            use_channels=use_channels,
                            flow_rates=[flow_rates[1]] if flow_rates else None,
                            offsets=[offsets[1]] if offsets else None,
                            blow_out_air_volume=[blow_out_air_volume[1]] if blow_out_air_volume else None,
                            liquid_height=[liquid_height[1]] if liquid_height else None,
                            spread=spread,
                        )
                        await self.discard_tips()

                elif len(use_channels) == 8 and self.backend.num_channels == 8:

                    # 对于8个的情况，需要判断此时任务是不是能被8通道移液站来成功处理
                    if len(sources) % 8 != 0:
                        raise ValueError(
                            f"Length of `sources` {len(sources)} must be a multiple of 8 for 8-channel mode."
                        )

                    # 8个8个来取任务序列

                    for i in range(0, len(sources), 8):
                        # 列式硬件做列对齐：当前列剩余不足整列时跳过残余、从下一整列开头取。
                        tip = self._acquire_tip_column(len(use_channels))
                        await self.pick_up_tips(tip)
                        current_targets = waste_liquid[i : i + 8]
                        current_reagent_sources = sources[i : i + 8]
                        current_asp_vols = vols[i : i + 8]
                        current_dis_vols = vols[i : i + 8]
                        current_asp_flow_rates = flow_rates[i : i + 8] if flow_rates else [None] * 8
                        current_dis_flow_rates = (
                            flow_rates[-i * 8 - 8 : len(flow_rates) - i * 8] if flow_rates else [None] * 8
                        )
                        current_asp_offset = offsets[i : i + 8] if offsets else [None] * 8
                        current_dis_offset = offsets[-i * 8 - 8 : len(offsets) - i * 8] if offsets else [None] * 8
                        current_asp_liquid_height = liquid_height[i : i + 8] if liquid_height else [None] * 8
                        current_dis_liquid_height = (
                            liquid_height[-i * 8 - 8 : len(liquid_height) - i * 8] if liquid_height else [None] * 8
                        )
                        current_asp_blow_out_air_volume = (
                            blow_out_air_volume[i : i + 8] if blow_out_air_volume else [None] * 8
                        )
                        current_dis_blow_out_air_volume = (
                            blow_out_air_volume[-i * 8 - 8 : len(blow_out_air_volume) - i * 8]
                            if blow_out_air_volume
                            else [None] * 8
                        )

                        await self.aspirate(
                            resources=current_reagent_sources,
                            vols=current_asp_vols,
                            use_channels=use_channels,
                            flow_rates=current_asp_flow_rates,
                            offsets=current_asp_offset,
                            liquid_height=current_asp_liquid_height,
                            blow_out_air_volume=current_asp_blow_out_air_volume,
                            spread=spread,
                        )
                        if delays is not None:
                            await self.custom_delay(seconds=delays[0])
                        await self.dispense(
                            resources=current_targets,
                            vols=current_dis_vols,
                            use_channels=use_channels,
                            flow_rates=current_dis_flow_rates,
                            offsets=current_dis_offset,
                            liquid_height=current_dis_liquid_height,
                            blow_out_air_volume=current_dis_blow_out_air_volume,
                            spread=spread,
                        )
                        if delays is not None and len(delays) > 1:
                            await self.custom_delay(seconds=delays[1])
                        await self.touch_tip(current_targets)
                        await self.discard_tips()

        except Exception as e:
            traceback.print_exc()
            raise RuntimeError(f"Liquid addition failed: {e}") from e

    # ---------------------------------------------------------------
    # ADD LIQUID -----------------------------------------------------
    # ---------------------------------------------------------------

    async def add_liquid(
        self,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        reagent_sources: Sequence[Container],
        targets: Sequence[Container],
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        is_96_well: bool = False,
        delays: Optional[List[int]] = None,
        mix_time: Optional[int] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        none_keys: List[str] = [],
    ):
        # """A complete *add* (aspirate reagent → dispense into targets) operation."""

        # # try:
        if is_96_well:
            pass  # This mode is not verified.
        else:
            if len(asp_vols) != len(targets):
                raise ValueError(f"Length of `asp_vols` {len(asp_vols)} must match `targets` {len(targets)}.")
            # 首先应该对任务分组，然后每次1个/8个进行操作处理
            if len(use_channels) == 1:
                for _ in range(len(targets)):
                    tip = []
                    for x in range(len(use_channels)):
                        tip.append(self._get_next_tip())
                    await self.pick_up_tips(tip)

                    await self.aspirate(
                        resources=[reagent_sources[_]],
                        vols=[asp_vols[_]],
                        use_channels=use_channels,
                        flow_rates=[flow_rates[0]] if flow_rates else None,
                        offsets=[offsets[0]] if offsets else None,
                        liquid_height=[liquid_height[0]] if liquid_height else None,
                        blow_out_air_volume=[blow_out_air_volume[0]] if blow_out_air_volume else None,
                        spread=spread,
                    )

                    if delays is not None:
                        await self.custom_delay(seconds=delays[0])
                    await self.dispense(
                        resources=[targets[_]],
                        vols=[dis_vols[_]],
                        use_channels=use_channels,
                        flow_rates=[flow_rates[1]] if flow_rates else None,
                        offsets=[offsets[1]] if offsets else None,
                        blow_out_air_volume=[blow_out_air_volume[1]] if blow_out_air_volume else None,
                        liquid_height=[liquid_height[1]] if liquid_height else None,
                        spread=spread,
                    )

                    if delays is not None and len(delays) > 1:
                        await self.custom_delay(seconds=delays[1])
                    # 只有在 mix_time 有效时才调用 mix
                    if mix_time is not None and mix_time > 0:
                        await self.mix(
                            targets=[targets[_]],
                            mix_time=mix_time,
                            mix_vol=mix_vol,
                            offsets=offsets if offsets else None,
                            height_to_bottom=mix_liquid_height if mix_liquid_height else None,
                            mix_rate=mix_rate if mix_rate else None,
                        )
                    if delays is not None and len(delays) > 1:
                        await self.custom_delay(seconds=delays[1])
                    await self.touch_tip(targets[_])
                    await self.discard_tips()

            elif len(use_channels) == 8:
                # 对于8个的情况，需要判断此时任务是不是能被8通道移液站来成功处理
                if len(targets) % 8 != 0:
                    raise ValueError(f"Length of `targets` {len(targets)} must be a multiple of 8 for 8-channel mode.")

                for i in range(0, len(targets), 8):
                    tip = []
                    for _ in range(len(use_channels)):
                        tip.append(self._get_next_tip())
                    await self.pick_up_tips(tip)
                    current_targets = targets[i : i + 8]
                    current_reagent_sources = reagent_sources[i : i + 8]
                    current_asp_vols = asp_vols[i : i + 8]
                    current_dis_vols = dis_vols[i : i + 8]
                    current_asp_flow_rates = flow_rates[i : i + 8] if flow_rates else [None] * 8
                    current_dis_flow_rates = (
                        flow_rates[-i * 8 - 8 : len(flow_rates) - i * 8] if flow_rates else [None] * 8
                    )
                    current_asp_offset = offsets[i : i + 8] if offsets else [None] * 8
                    current_dis_offset = offsets[-i * 8 - 8 : len(offsets) - i * 8] if offsets else [None] * 8
                    current_asp_liquid_height = liquid_height[i : i + 8] if liquid_height else [None] * 8
                    current_dis_liquid_height = (
                        liquid_height[-i * 8 - 8 : len(liquid_height) - i * 8] if liquid_height else [None] * 8
                    )
                    current_asp_blow_out_air_volume = (
                        blow_out_air_volume[i : i + 8] if blow_out_air_volume else [None] * 8
                    )
                    current_dis_blow_out_air_volume = (
                        blow_out_air_volume[-i * 8 - 8 : len(blow_out_air_volume) - i * 8]
                        if blow_out_air_volume
                        else [None] * 8
                    )

                    await self.aspirate(
                        resources=current_reagent_sources,
                        vols=current_asp_vols,
                        use_channels=use_channels,
                        flow_rates=current_asp_flow_rates,
                        offsets=current_asp_offset,
                        liquid_height=current_asp_liquid_height,
                        blow_out_air_volume=current_asp_blow_out_air_volume,
                        spread=spread,
                    )
                    if delays is not None:
                        await self.custom_delay(seconds=delays[0])
                    await self.dispense(
                        resources=current_targets,
                        vols=current_dis_vols,
                        use_channels=use_channels,
                        flow_rates=current_dis_flow_rates,
                        offsets=current_dis_offset,
                        liquid_height=current_dis_liquid_height,
                        blow_out_air_volume=current_dis_blow_out_air_volume,
                        spread=spread,
                    )
                    if delays is not None and len(delays) > 1:
                        await self.custom_delay(seconds=delays[1])

                    # 只有在 mix_time 有效时才调用 mix
                    if mix_time is not None and mix_time > 0:
                        await self.mix(
                            targets=current_targets,
                            mix_time=mix_time,
                            mix_vol=mix_vol,
                            offsets=offsets if offsets else None,
                            height_to_bottom=mix_liquid_height if mix_liquid_height else None,
                            mix_rate=mix_rate if mix_rate else None,
                        )
                    if delays is not None and len(delays) > 1:
                        await self.custom_delay(seconds=delays[1])
                    await self.touch_tip(current_targets)
                    await self.discard_tips()

    # except Exception as e:
    #     traceback.print_exc()
    #     raise RuntimeError(f"Liquid addition failed: {e}") from e

    # ---------------------------------------------------------------
    # TRANSFER LIQUID ------------------------------------------------
    # ---------------------------------------------------------------
    async def transfer_liquid(
        self,
        sources: Sequence[Union[Container, Dict[str, Any]]],
        targets: Sequence[Union[Container, Dict[str, Any]]],
        tip_racks: Sequence[Union[TipRack, Dict[str, Any]]],
        *,
        use_channels: Optional[List[int]] = None,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        blow_out_air_volume_before: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        is_96_well: bool = False,
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[int] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        delays: Optional[List[int]] = None,
        pre_aspirate_from_target: Optional[float] = None,
        none_keys: List[str] = [],
    ) -> TransferLiquidReturn:
        """Transfer liquid with automatic mode detection.
        """
        # 若传入 dict（含 uuid），解析为 PLR Container/TipRack
        sources = await self._resolve_to_plr_resources(sources)
        targets = await self._resolve_to_plr_resources(targets)
        tip_racks = list(await self._resolve_to_plr_resources(tip_racks))
        num_sources = len(sources)
        num_targets = len(targets)
        len_asp_vols = len(asp_vols)
        len_dis_vols = len(dis_vols)

        # === [T-DBG] 跨板 dispense 翻倍排查（候选 B）===
        # 51b9a5 协议每条 transfer 应有 num_targets == 9 且 9 个 well 名 distinct。
        # 若 num_targets >= 18 或 target_dup > 0 → 命中候选 B
        # （merged set_liquid_from_plate.output_wells → transfer.targets 把 wells 翻倍）。
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            try:
                _src_names = [f"{getattr(s.parent, 'name', '?')}/{s.name}" for s in sources]
                _tgt_names = [f"{getattr(t.parent, 'name', '?')}/{t.name}" for t in targets]
                _tgt_dup = len(_tgt_names) - len(set(_tgt_names))
                self._ros_node.lab_logger().info(
                    f"[T-DBG] transfer_liquid handler={id(self):x} "
                    f"num_sources={num_sources} num_targets={num_targets} "
                    f"len_asp_vols={len_asp_vols} len_dis_vols={len_dis_vols} "
                    f"target_dup={_tgt_dup} "
                    f"sources={_src_names} targets={_tgt_names} "
                    f"asp_vols={list(asp_vols)} dis_vols={list(dis_vols)} "
                    f"use_channels={use_channels} mix_stage={mix_stage}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[T-DBG] log failed: {_e}")

        # 输入完整性防护：避免后续 ``i % num_sources`` / ``i % num_targets`` / ``i % len_asp_vols``
        # 在空列表场景触发 ``ZeroDivisionError``，统一给出可定位的参数错误信息。
        if num_sources == 0:
            raise ValueError("transfer_liquid requires non-empty sources.")
        if num_targets == 0:
            raise ValueError("transfer_liquid requires non-empty targets.")
        if len_asp_vols == 0:
            raise ValueError("transfer_liquid requires non-empty asp_vols.")
        if len_dis_vols == 0:
            raise ValueError("transfer_liquid requires non-empty dis_vols.")
        # 确保 use_channels 有默认值
        if use_channels is None or len(use_channels) == 0:
            # 默认使用设备所有通道（例如 8 通道移液站默认就是 0-7）
            use_channels = list(range(self.channel_num)) if self.channel_num == 8 else [0]
        elif len(use_channels) == 8:
            if self.channel_num != 8:
                raise ValueError(f"if channel_num is 8, use_channels length must be 8, but got {len(use_channels)}")
            # P1 多通道约定（见 product_designs/protocol_convert/01-multi-channel-flatten.md §6.1）：
            # asp_vols/dis_vols 长度为 8×M（M=列锚条目数），sources/targets 可能是 8×M（plate 整列展开）
            # 或 M（reservoir/distribute/跨槽，每锚 1 个 well，运行时按通道复制）。
            # 核心约束：体积数组为 8 的倍数且 asp==dis；sources/targets 各自为 %8==0 或 ==M。
            if len_asp_vols % 8 != 0 or len_dis_vols % 8 != 0 or len_asp_vols != len_dis_vols:
                raise ValueError(
                    "if channel_num is 8, asp_vols and dis_vols length must be divisible by 8 and equal, "
                    f"but got asp_vols={len_asp_vols}, dis_vols={len_dis_vols}"
                )
            _M = len_asp_vols // 8
            # ``sources/targets`` 在真实工作流里可能是 1 / M / 8 / 8*M，也可能是其它长度
            # （例如跨阶段拼接后出现 17 这类长度）。后续 ``_resolve_per_channel`` 会统一规整到
            # 8*M 并按组切片处理，这里不再提前拒绝，避免把可执行输入误判为错误。
            if hasattr(self, "_ros_node") and self._ros_node is not None:
                try:
                    self._ros_node.lab_logger().debug(
                        "[T-DBG] 8ch shape accepted: "
                        f"sources={num_sources}, targets={num_targets}, M={_M}, total={8 * _M}"
                    )
                except Exception:
                    pass

        if is_96_well:
            # 96 孔整板模式：走 PLR 96 头 API（pickup96 →[mix]→ asp96 → disp96 →[mix]→ drop96）。
            return await self._transfer_liquid_96well(
                sources,
                targets,
                tip_racks,
                asp_vols=asp_vols,
                dis_vols=dis_vols,
                asp_flow_rates=asp_flow_rates,
                dis_flow_rates=dis_flow_rates,
                offsets=offsets,
                touch_tip=touch_tip,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                mix_stage=mix_stage,
                mix_times=mix_times,
                mix_vol=mix_vol,
                mix_rate=mix_rate,
                mix_liquid_height=mix_liquid_height,
            )
        # 转换体积参数为列表
        if isinstance(asp_vols, (int, float)):
            asp_vols = [float(asp_vols)]
        else:
            asp_vols = [float(v) for v in asp_vols]

        if isinstance(dis_vols, (int, float)):
            dis_vols = [float(dis_vols)]
        else:
            dis_vols = [float(v) for v in dis_vols]

        # 统一混合次数为标量，防止数组/列表与 int 比较时报错
        if mix_times is not None and not isinstance(mix_times, (int, float)):
            try:
                mix_times = mix_times[0] if len(mix_times) > 0 else None
            except Exception:
                try:
                    mix_times = next(iter(mix_times))
                except Exception:
                    pass
        if mix_times is not None:
            mix_times = int(mix_times)

        # 设置tip racks
        self.set_tiprack(tip_racks)

        # 识别传输模式（mix_times 为 None 也应该能正常移液，只是不做 mix）
        num_sources = len(sources)
        num_targets = len(targets)
        len_asp_vols = len(asp_vols)
        len_dis_vols = len(dis_vols)

        # if num_targets != 1 and num_sources != 1:
        #     if len_asp_vols != num_sources and len_asp_vols != num_targets:
        #         raise ValueError(f"asp_vols length must be equal to sources or targets length, but got {len_asp_vols} and {num_sources} and {num_targets}")
        #     if len_dis_vols != num_sources and len_dis_vols != num_targets:
        #         raise ValueError(f"dis_vols length must be equal to sources or targets length, but got {len_dis_vols} and {num_sources} and {num_targets}")

        # 辅助函数：
        # - wrap=True: 返回 [value]（用于 liquid_height 等列表参数）
        # - wrap=False: 返回 value（用于 mix_* 标量参数）
        def safe_get(value, idx, default=None, wrap: bool = True):
            if value is None:
                return default
            try:
                if isinstance(value, (list, tuple)):
                    if len(value) == 0:
                        return default
                    item = value[idx % len(value)]
                else:
                    item = value
                return [item] if wrap else item
            except Exception:
                return default

        # P10 v2 — 读取 tip 复用开关；测试 fixture 跳过 super().__init__ 时
        # 用 getattr fallback 到 True，保证默认行为一致。
        tip_reuse_by_liquid_name = bool(getattr(self, "_tip_reuse_by_liquid_name", True))

        if len(use_channels) != 8:
            max_len = max(num_sources, num_targets, len_asp_vols, len_dis_vols)
            prev_dropped = True  # 循环开始前通道上无 tip
            current_tip_liquid_name: Optional[str] = None  # P10 v2：tip 残液身份
            for i in range(max_len):

                # 动态构建参数字典，只传递实际提供的参数
                kwargs = {
                    'sources': [sources[i%num_sources]],
                    'targets': [targets[i%num_targets]],
                    'tip_racks': tip_racks,
                    'use_channels': use_channels,
                    'asp_vols': [asp_vols[i%len_asp_vols]],
                    'dis_vols': [dis_vols[i%len_dis_vols]],
                }

                # 条件性添加可选参数
                if asp_flow_rates is not None:
                    kwargs['asp_flow_rates'] = [asp_flow_rates[i%len_asp_vols]]
                if dis_flow_rates is not None:
                    kwargs['dis_flow_rates'] = [dis_flow_rates[i%len_dis_vols]]
                if offsets is not None:
                    kwargs['offsets'] = safe_get(offsets, i)
                if touch_tip is not None:
                    kwargs['touch_tip'] = touch_tip if touch_tip else False
                if liquid_height is not None:
                    kwargs['liquid_height'] = safe_get(liquid_height, i)
                if blow_out_air_volume is not None:
                    kwargs['blow_out_air_volume'] = safe_get(blow_out_air_volume, i)
                if blow_out_air_volume_before is not None:
                    kwargs['blow_out_air_volume_before'] = safe_get(blow_out_air_volume_before, i)
                if spread is not None:
                    kwargs['spread'] = spread
                if mix_stage is not None:
                    kwargs['mix_stage'] = safe_get(mix_stage, i, wrap=False)
                if mix_times is not None:
                    kwargs['mix_times'] = safe_get(mix_times, i, wrap=False)
                if mix_vol is not None:
                    kwargs['mix_vol'] = safe_get(mix_vol, i, wrap=False)
                if mix_rate is not None:
                    kwargs['mix_rate'] = safe_get(mix_rate, i, wrap=False)
                if mix_liquid_height is not None:
                    kwargs['mix_liquid_height'] = safe_get(mix_liquid_height, i, wrap=False)
                if delays is not None:
                    kwargs['delays'] = safe_get(delays, i)
                if pre_aspirate_from_target is not None:
                    kwargs['pre_aspirate_from_target'] = safe_get(pre_aspirate_from_target, i)

                cur_source = sources[i % num_sources]
                cur_target = targets[i % num_targets]

                # drop: identity-keep（同 PLR Well 对象）OR liquids-equivalence
                # （cur/next source ``tracker.liquids[-1]`` 同名）→ 任一命中即保留 tip。
                drop_tip = True
                if i < max_len - 1:
                    next_source = sources[(i + 1) % num_sources]
                    next_target = targets[(i + 1) % num_targets]
                    identity_keep = (cur_target is next_target) and (cur_source is next_source)
                    liquids_keep = (
                        tip_reuse_by_liquid_name
                        and _same_liquid_via_liquids_pair(cur_source, next_source)
                    )
                    if identity_keep or liquids_keep:
                        drop_tip = False

                # pick_up: identity-keep（同 PLR Well 对象）OR liquids-equivalence
                # （cur source ``tracker.liquids[-1]`` 与 tip 残液同名）→ 任一命中即复用 tip。
                pick_up_tip = True
                if i > 0 and not prev_dropped:
                    prev_source = sources[(i - 1) % num_sources]
                    identity_keep = (cur_source is prev_source)
                    liquids_keep = (
                        tip_reuse_by_liquid_name
                        and _same_liquid_via_liquids(cur_source, current_tip_liquid_name)
                    )
                    if identity_keep or liquids_keep:
                        pick_up_tip = False

                # P10 v2 时序：tip 残液名必须在 aspirate **之前**预读
                # （PLR aspirate 顶层归零时会 pop ``tracker.liquids`` 顶层）。
                pending_tip_name: Optional[str] = None
                if pick_up_tip:
                    pending_tip_name = _capture_tip_liquid_name(cur_source)

                prev_dropped = drop_tip

                kwargs['pick_up'] = pick_up_tip
                kwargs['drop'] = drop_tip

                await self._transfer_base_method(**kwargs)

                if pick_up_tip:
                    current_tip_liquid_name = pending_tip_name
                if drop_tip:
                    current_tip_liquid_name = None
        else:
            # len(use_channels) == 8：真 8 通道并行，按组（每 8 通道一组）调用 _transfer_base_method。
            # 用户决策：每组换新枪头（pick_up/drop=True），不做组间复用。
            # asp_vols/dis_vols 长度 = 8×M（M=列锚条目数，上方已校验）；sources/targets 为 8×M 或 M。
            # 设计见 product_designs/protocol_convert/01-multi-channel-flatten.md §6.1。
            M = len_asp_vols // 8

            def _resolve_per_channel(seq):
                """把序列规整为长度 8×M，供逐组 8 切片；空/None → None（可选参数未提供）。

                - 8×M：原样（plate 整列展开）
                - M（且 != 8）：每元素复制 8 次（reservoir/distribute，每锚共享）
                - 8 或 M==8：整列 tile M 次（单列源/目标逐组复用）
                - 1：广播为 8×M
                - 其它：
                  - n > 8 且末组不完整：仅用末组尾段循环补齐，避免跨组取模导致列混排
                  - 其余兜底：按 i%n 取模填满 8×M
                """
                if not seq:
                    return None
                n = len(seq)
                total = 8 * M
                if n == total:
                    return list(seq)
                if n == M and n != 8:
                    out = []
                    for w in seq:
                        out.extend([w] * 8)
                    return out
                if n == 8 or n == M:  # 单列（含 M==8 边界）→ 逐组 tile
                    return list(seq) * M
                if n == 1:
                    return [seq[0]] * total
                if n > total:
                    return list(seq[:total])
                if n < total and n > 8:
                    rem = n % 8
                    if rem != 0:
                        # 示例：17 -> 24 时，保留前 16（2 组完整）+ 用最后 1 个元素补齐第三组，
                        # 避免旧逻辑跨组循环成 [x, ... prev_group...] 造成 8 通道混列。
                        out = list(seq)
                        tail = list(seq[-rem:])
                        while len(out) < total:
                            out.append(tail[(len(out) - n) % rem])
                        return out
                return [seq[i % n] for i in range(total)]

            sources_r = _resolve_per_channel(sources)
            targets_r = _resolve_per_channel(targets)
            asp_vols_r = list(asp_vols)
            dis_vols_r = list(dis_vols)

            def _group_source_key(group_idx: int):
                lo2, hi2 = group_idx * 8, (group_idx + 1) * 8
                grp = sources_r[lo2:hi2]
                return tuple(f"{getattr(s.parent, 'name', '?')}/{s.name}" for s in grp)

            for k in range(M):
                lo, hi = k * 8, (k + 1) * 8
                # P10 v2（8 通道）：当相邻组 source 完全一致时复用枪头，缓解大批量流程 tip 耗尽。
                # 规则：
                # - 与前一组 source 一致 -> 本组不 pick_up
                # - 与后一组 source 一致 -> 本组不 drop
                # - 其它情况维持 pick/drop
                cur_key = _group_source_key(k)
                prev_key = _group_source_key(k - 1) if k > 0 else None
                next_key = _group_source_key(k + 1) if k + 1 < M else None
                if tip_reuse_by_liquid_name:
                    pick_up = not (prev_key is not None and cur_key == prev_key)
                    drop = not (next_key is not None and cur_key == next_key)
                else:
                    pick_up = True
                    drop = True
                kwargs = {
                    'sources': sources_r[lo:hi],
                    'targets': targets_r[lo:hi],
                    'tip_racks': tip_racks,
                    'use_channels': use_channels,
                    'asp_vols': asp_vols_r[lo:hi],
                    'dis_vols': dis_vols_r[lo:hi],
                    'pick_up': pick_up,
                    'drop': drop,
                }
                # 可选 per-well 参数：规整为 8×M 后取第 k 组（空 → 跳过）
                for key, src in (
                    ('asp_flow_rates', asp_flow_rates),
                    ('dis_flow_rates', dis_flow_rates),
                    ('offsets', offsets),
                    ('liquid_height', liquid_height),
                    ('blow_out_air_volume', blow_out_air_volume),
                    ('blow_out_air_volume_before', blow_out_air_volume_before),
                    ('delays', delays),
                    ('pre_aspirate_from_target', pre_aspirate_from_target),
                ):
                    rv = _resolve_per_channel(src)
                    if rv is not None:
                        kwargs[key] = rv[lo:hi]
                if touch_tip is not None:
                    kwargs['touch_tip'] = touch_tip if touch_tip else False
                if spread is not None:
                    kwargs['spread'] = spread
                # 标量 mix_* 取该组代表值（按组下标 k；越界由 safe_get 兜底）
                for key, src in (
                    ('mix_stage', mix_stage),
                    ('mix_times', mix_times),
                    ('mix_vol', mix_vol),
                    ('mix_rate', mix_rate),
                    ('mix_liquid_height', mix_liquid_height),
                ):
                    if src is not None:
                        kwargs[key] = safe_get(src, k, wrap=False)

                try:
                    await self._transfer_base_method(**kwargs)
                except ValueError as e:
                    # 384 孔板等狭小井距资源可能不支持 8 通道间距，降级为单通道串行执行该组。
                    if "Resource is too small to space channels." in str(e):
                        if hasattr(self, "_ros_node") and self._ros_node is not None:
                            self._ros_node.lab_logger().warning(
                                "[T-DBG] 8ch spacing unsupported, fallback to single channel for this group"
                            )
                        await self._transfer_group_single_channel_fallback(kwargs)
                    else:
                        raise


        return TransferLiquidReturn(
            sources=ResourceTreeSet.from_plr_resources(list(sources), known_newly_created=False).dump(),  # type: ignore
            targets=ResourceTreeSet.from_plr_resources(list(targets), known_newly_created=False).dump(),  # type: ignore
        )

    async def _transfer_liquid_96well(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        *,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[List[int]] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
    ) -> TransferLiquidReturn:
        """96 孔整板转移：pickup96 →[mix-before]→ aspirate96 → dispense96 →[mix-after]→ drop96。

        整板一次动作 = 单一体积 / 单一参数，各列表参数取首元素（``_first``）。full 参数按 96 头
        API 能力映射（offset / flow_rate / liquid_height / blow_out_air_volume / mix / touch_tip）。
        丢枪头优先送到 deck 上的 ``Trash``；无 trash 时回收到取头用的 tip_rack。
        """

        def _first(seq):
            if seq is None:
                return None
            if isinstance(seq, (list, tuple)):
                return seq[0] if len(seq) else None
            return seq

        def _resolve_plate(res) -> Plate:
            if isinstance(res, Plate):
                return res
            parent = getattr(res, "parent", None)
            if isinstance(parent, Plate):
                return parent
            return res  # 兜底：按 plate-like 处理

        source_plate = _resolve_plate(sources[0])
        target_plate = _resolve_plate(targets[0])
        tip_rack = tip_racks[0] if isinstance(tip_racks[0], TipRack) else tip_racks[0].parent

        self.set_tiprack(tip_racks)

        asp_vol = float(_first(asp_vols))
        dis_vol = float(_first(dis_vols))
        _offset = _first(offsets) or Coordinate.zero()
        _asp_rate = _first(asp_flow_rates)
        _dis_rate = _first(dis_flow_rates)
        _blow = _first(blow_out_air_volume)
        _liquid_h = _first(liquid_height)

        # mix 次数可能是 list / 标量，统一收敛为 int（0 表示不 mix）。
        _mt = _first(mix_times)
        try:
            _mix_times = int(_mt) if _mt is not None else 0
        except (TypeError, ValueError):
            _mix_times = 0
        _mix_vol = float(mix_vol) if mix_vol else None
        _do_mix_before = bool(mix_stage in ("before", "both") and _mix_times > 0 and _mix_vol)
        _do_mix_after = bool(mix_stage in ("after", "both") and _mix_times > 0 and _mix_vol)

        # 整板吸/放液的体积追踪兜底：与非 96 路径一致，PLR 会对每个孔做 remove_liquid，
        # 若源孔在 tracker 里未被 set_liquid 播种（记为 0），会抛 TooLittleLiquidError；
        # 此时改用 no_volume_tracking() 重试（真机不依赖 tracker，追踪仅尽力而为）。
        # aspirate/dispense 各自独立兜底：若 aspirate 走了 no_volume_tracking（tip 未记账），
        # dispense 的 tip.remove_liquid 也会失败并同样 fallback，从而两端保持一致。
        async def _asp96(plate: Plate, vol: float, **kw):
            try:
                await self.aspirate96(plate, vol, **kw)
            except (TooLittleLiquidError, TooLittleVolumeError) as e:
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[aspirate96] fallback no_volume_tracking. error={e}, vol={vol}"
                    )
                with no_volume_tracking():
                    await self.aspirate96(plate, vol, **kw)

        async def _disp96(plate: Plate, vol: float, **kw):
            try:
                await self.dispense96(plate, vol, **kw)
            except (TooLittleLiquidError, TooLittleVolumeError) as e:
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    self._ros_node.lab_logger().warning(
                        f"[dispense96] fallback no_volume_tracking. error={e}, vol={vol}"
                    )
                with no_volume_tracking():
                    await self.dispense96(plate, vol, **kw)

        async def _mix_plate(plate: Plate):
            for _ in range(_mix_times):
                await _asp96(
                    plate,
                    _mix_vol,
                    offset=_offset,
                    flow_rate=mix_rate,
                    liquid_height=mix_liquid_height,
                )
                await _disp96(
                    plate,
                    _mix_vol,
                    offset=_offset,
                    flow_rate=mix_rate,
                    liquid_height=mix_liquid_height,
                )

        # 1) 整板取枪头
        await self.pick_up_tips96(tip_rack, offset=_offset)

        # 2) mix-before（对源板整板混匀）
        if _do_mix_before:
            await _mix_plate(source_plate)

        # 3) 整板吸液
        await _asp96(
            source_plate,
            asp_vol,
            offset=_offset,
            flow_rate=_asp_rate,
            liquid_height=_liquid_h,
            blow_out_air_volume=_blow,
        )

        # 4) 整板放液
        await _disp96(
            target_plate,
            dis_vol,
            offset=_offset,
            flow_rate=_dis_rate,
            liquid_height=_liquid_h,
            blow_out_air_volume=_blow,
        )

        # 5) mix-after（对目标板整板混匀）
        if _do_mix_after:
            await _mix_plate(target_plate)

        # 6) touch_tip：native 靠壁由 backend 在 dispense96 内处理；此处覆盖 software/both 模式
        #    （backend 无 software 语义时子类 touch_tip 会自行短路，安全无副作用）。
        if touch_tip:
            _tt_targets = [c for c in targets if isinstance(c, Well)]
            if not _tt_targets:
                _tt_targets = list(getattr(target_plate, "children", []) or [])
            if _tt_targets:
                await self.touch_tip(_tt_targets)

        # 7) 丢枪头：优先 deck 上的 Trash，无 trash 回收到 tip_rack。
        drop_target: Union[TipRack, Trash] = tip_rack
        for child in getattr(self.deck, "children", []) or []:
            if isinstance(child, Trash):
                drop_target = child
                break
        await self.drop_tips96(drop_target)

        # 8) 同步孔板液体状态并上报 web：整板路径下 aspirate96/dispense96 不像单通道
        #    dispense 那样回推 update_resource，且体积追踪被 no_volume_tracking 兜底关掉，
        #    孔状态不会自动变化 → web 端看不到孔板更新。这里显式补齐（best-effort，不阻断主流程）。
        try:
            self._reflect_96well_transfer(source_plate, target_plate, dis_vol)
        except Exception as _e:
            if hasattr(self, "_ros_node") and self._ros_node is not None:
                self._ros_node.lab_logger().warning(f"[transfer96] 反映孔板状态/上报失败：{_e}")

        return TransferLiquidReturn(
            sources=ResourceTreeSet.from_plr_resources(list(sources), known_newly_created=False).dump(),  # type: ignore
            targets=ResourceTreeSet.from_plr_resources(list(targets), known_newly_created=False).dump(),  # type: ignore
        )

    @staticmethod
    def _plate_wells(plate) -> List[Well]:
        """取 plate 的全部孔（get_all_items 优先，回退 children 过滤 Well）。"""
        try:
            items = list(plate.get_all_items())
        except Exception:
            items = list(getattr(plate, "children", []) or [])
        return [w for w in items if isinstance(w, Well)]

    def _push_resource_update(self, resources: Sequence[Resource]) -> None:
        """把受影响资源（孔/板）主动上报 ROS/web；整板路径缺少单通道 dispense 的回推。"""
        if not (hasattr(self, "_ros_node") and self._ros_node is not None):
            return
        uniq: List[Resource] = []
        seen: Set[int] = set()
        for r in resources:
            if r is None:
                continue
            rid = id(r)
            if rid in seen:
                continue
            seen.add(rid)
            uniq.append(r)
        if not uniq:
            return
        task = ROS2DeviceNode.run_async_func(
            self._ros_node.update_resource, True, **{"resources": uniq}
        )
        submit_time = time.time()
        while not task.done():
            if time.time() - submit_time > 10:
                self._ros_node.lab_logger().info(f"[transfer96] update_resource 超时 resources={uniq}")
                break
            time.sleep(0.01)

    def _reflect_96well_transfer(self, source_plate, target_plate, volume: float) -> None:
        """整板移液后同步孔液体状态并上报：源孔按量扣减、目标孔登记来源液体，供 web 显示。

        用途仅为「让前端孔板视图反映整板结果」，非严格计量：源孔追踪为空时静默跳过扣减，
        目标孔以 ``set_liquids`` 播种来源液体名 + 本次体积（与本文件既有 set_liquid 写法一致）。
        """
        src_wells = self._plate_wells(source_plate)
        tgt_wells = self._plate_wells(target_plate)

        # 解析源液体名：取首个有液体的源孔的顶层液体名。
        liquid_name = ""
        for w in src_wells:
            liqs = getattr(getattr(w, "tracker", None), "liquids", None) or []
            if liqs:
                liquid_name = liqs[-1][0] or ""
                break

        # 源孔扣量（best-effort；追踪为空/不足则跳过，不阻断）。
        for w in src_wells:
            try:
                w.tracker.remove_liquid(volume=volume)
            except Exception:
                pass

        # 目标孔登记液体（供 web 显示 fill 结果）。
        for w in tgt_wells:
            try:
                w.set_liquids([(liquid_name, float(volume))])  # type: ignore[arg-type]
            except Exception:
                pass

        self._push_resource_update([source_plate, target_plate, *src_wells, *tgt_wells])

    async def _transfer_base_method(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        use_channels: List[int],
        asp_vols: List[float],
        dis_vols: List[float],
        pick_up: bool = True,
        drop: bool = True,
        **kwargs
    ):

        # === [B-DBG] _transfer_base_method 调用计数（候选 D）===
        # 每条 transfer 应调用 num_targets 次（51b9a5 → 9 次）；
        # 同一 (source, target) 出现 2 次 → 候选 D（同节点被 ROS 触发 2 次）。
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            try:
                _src_names = [f"{getattr(s.parent, 'name', '?')}/{s.name}" for s in sources]
                _tgt_names = [f"{getattr(t.parent, 'name', '?')}/{t.name}" for t in targets]
                self._ros_node.lab_logger().info(
                    f"[B-DBG] _transfer_base_method handler={id(self):x} "
                    f"pick_up={pick_up} drop={drop} use_channels={use_channels} "
                    f"asp_vols={list(asp_vols)} dis_vols={list(dis_vols)} "
                    f"sources={_src_names} targets={_tgt_names}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[B-DBG] log failed: {_e}")

        # 从kwargs中提取参数，提供默认值
        asp_flow_rates = kwargs.get('asp_flow_rates')
        dis_flow_rates = kwargs.get('dis_flow_rates')
        offsets = kwargs.get('offsets')
        touch_tip = kwargs.get('touch_tip', False)
        liquid_height = kwargs.get('liquid_height')
        blow_out_air_volume = kwargs.get('blow_out_air_volume')
        blow_out_air_volume_before = kwargs.get('blow_out_air_volume_before')
        spread = kwargs.get('spread', 'wide')
        mix_stage = kwargs.get('mix_stage')
        mix_times = kwargs.get('mix_times')
        mix_vol = kwargs.get('mix_vol')
        mix_rate = kwargs.get('mix_rate')
        mix_liquid_height = kwargs.get('mix_liquid_height')
        delays = kwargs.get('delays')
        pre_aspirate_from_target = kwargs.get('pre_aspirate_from_target')

        # P1 v4 多通道：当 use_channels 长度 > 1（如 8 通道）时，下层
        # PLR aspirate/dispense 接受「N 个 resources + N 个 vols + N
        # 个 use_channels」逐通道独立操作；单通道时仍按 `[sources[0]]`
        # / `[asp_vols[0]]` 单元素列表调用。
        multi_channel = isinstance(use_channels, (list, tuple)) and len(use_channels) > 1
        n_ch = len(use_channels) if multi_channel else 1

        def _pad_to_n(lst, n, default=None):
            """把 list 截/扩到长度 n；None / 空列表返回 None。"""
            if lst is None:
                return None
            if not isinstance(lst, (list, tuple)) or len(lst) == 0:
                return None
            if len(lst) >= n:
                return list(lst[:n])
            return list(lst) + [default if default is not None else lst[-1]] * (n - len(lst))

        if multi_channel:
            asp_resources = list(sources[:n_ch]) if len(sources) >= n_ch else list(sources)
            dis_resources = list(targets[:n_ch]) if len(targets) >= n_ch else list(targets)
            asp_vols_arg = list(asp_vols[:n_ch])
            dis_vols_arg = list(dis_vols[:n_ch])
            asp_flow_arg = _pad_to_n(asp_flow_rates, n_ch) if asp_flow_rates else None
            dis_flow_arg = _pad_to_n(dis_flow_rates, n_ch) if dis_flow_rates else None
            asp_liquid_h = _pad_to_n(liquid_height, n_ch) if liquid_height else None
            dis_liquid_h = _pad_to_n(liquid_height, n_ch) if liquid_height else None
            asp_offsets = _pad_to_n(offsets, n_ch) if offsets else None
            dis_offsets = _pad_to_n(offsets, n_ch) if offsets else None
            # mix 仍以 anchor well 调用，让 use_channels 在 PLR 内部并发列扩展
            mix_src_anchor = [sources[0]]
            mix_tgt_anchor = [targets[0]]
        else:
            asp_resources = [sources[0]]
            dis_resources = [targets[0]]
            asp_vols_arg = [asp_vols[0]]
            dis_vols_arg = [dis_vols[0]]
            asp_flow_arg = [asp_flow_rates[0]] if asp_flow_rates and len(asp_flow_rates) > 0 else None
            dis_flow_arg = [dis_flow_rates[0]] if dis_flow_rates and len(dis_flow_rates) > 0 else None
            asp_liquid_h = [liquid_height[0]] if liquid_height and len(liquid_height) > 0 else None
            dis_liquid_h = [liquid_height[0]] if liquid_height and len(liquid_height) > 0 else None
            asp_offsets = [offsets[0]] if offsets and len(offsets) > 0 else None
            dis_offsets = [offsets[0]] if offsets and len(offsets) > 0 else None
            mix_src_anchor = [sources[0]]
            mix_tgt_anchor = [targets[0]]

        # 通道级守恒：同一轮 transfer 中每通道 aspirate 体积不应大于后续 dispense。
        # 若 target free-volume 裁剪导致 dis_vol < asp_vol，直接把 asp_vol 同步裁剪，
        # 避免“吸 12 只放 2”造成 tip 残液累计，后续再 aspirate 触发 tip free-volume=0 报错。
        for i in range(min(len(asp_vols_arg), len(dis_vols_arg))):
            try:
                dis_v = max(float(dis_vols_arg[i]), 0.0)
            except Exception:
                dis_v = 0.0
            try:
                asp_v = max(float(asp_vols_arg[i]), 0.0)
            except Exception:
                asp_v = 0.0
            if dis_v < asp_v:
                asp_vols_arg[i] = dis_v
            else:
                asp_vols_arg[i] = asp_v
            dis_vols_arg[i] = dis_v

        tip = []
        if pick_up:
            # 多通道（如 8 通道）需取 n_ch 个 tip 工位（每通道一个）；单通道 n_ch=1 行为不变。
            # 列式硬件（_pickup_column_aligned）会做列对齐：当前列剩余不足整列时跳过残余、
            # 从下一整列开头取，保证 8 通道每次都是完整列。
            tip = self._acquire_tip_column(n_ch)
            await self.pick_up_tips(tip,use_channels=use_channels)
        # P1 v4：blow_before / blow_after 是每通道独立的，列表长度应为 n_ch。
        # 标量化处理（取 first 非零）用于决定是否触发 before-aspirate；下发到
        # PLR 时仍按通道列表传递。
        blow_before_list = _pad_to_n(blow_out_air_volume_before, n_ch) if blow_out_air_volume_before else None
        blow_after_list = _pad_to_n(blow_out_air_volume, n_ch) if blow_out_air_volume else None
        blow_out_air_volume_before_vol = float(blow_before_list[0] or 0.0) if blow_before_list else 0.0
        blow_out_air_volume_vol = float(blow_after_list[0] or 0.0) if blow_after_list else 0.0
        # PLR 的 blow_out_air_volume 是空气参数，不计入液体体积。
        # before 空气通过单独预吸实现，after 空气通过 blow_out_air_volume 参数实现。

        if mix_stage in ["before", "both"] and mix_times is not None and mix_times > 0:
            await self.mix(
                targets=mix_src_anchor,
                mix_time=mix_times,
                mix_vol=mix_vol,
                offsets=offsets if offsets else None,
                height_to_bottom=mix_liquid_height if mix_liquid_height else None,
                mix_rate=mix_rate if mix_rate else None,
                use_channels=use_channels,
            )

        if blow_out_air_volume_before_vol > 0:
            source_tracker = getattr(sources[0], "tracker", None)
            try:
                if source_tracker is not None and hasattr(source_tracker, "disable"):
                    source_tracker.disable()
                await self.aspirate(
                    resources=asp_resources,
                    vols=[0] * len(asp_resources),
                    use_channels=use_channels,
                    flow_rates=None,
                    offsets=[Coordinate(x=0, y=0, z=sources[0].get_size_z())] * len(asp_resources),
                    liquid_height=None,
                    blow_out_air_volume=(
                        blow_before_list if multi_channel
                        else [blow_out_air_volume_before_vol]
                    ),
                    spread="custom",
                )
            finally:
                if source_tracker is not None:
                    source_tracker.enable()

        await self.aspirate(
            resources=asp_resources,
            vols=asp_vols_arg,
            use_channels=use_channels,
            flow_rates=asp_flow_arg,
            offsets=asp_offsets,
            liquid_height=asp_liquid_h,
            blow_out_air_volume=(
                blow_after_list if (multi_channel and blow_after_list and any((v or 0) > 0 for v in blow_after_list))
                else ([blow_out_air_volume_vol] if blow_out_air_volume_vol > 0 else None)
            ),
            spread=spread,
        )
        if delays is not None and len(delays) > 0:
            await self.custom_delay(seconds=delays[0])
        # 合并 before/after 空气体积逐通道；dispense 时一次性吐回。
        if multi_channel:
            blow_for_dispense = [
                float(((blow_after_list[k] if blow_after_list else 0) or 0)
                      + ((blow_before_list[k] if blow_before_list else 0) or 0))
                for k in range(n_ch)
            ]
        else:
            blow_for_dispense = [blow_out_air_volume_vol + blow_out_air_volume_before_vol]
        await self.dispense(
            resources=dis_resources,
            vols=dis_vols_arg,
            use_channels=use_channels,
            flow_rates=dis_flow_arg,
            offsets=dis_offsets,
            blow_out_air_volume=blow_for_dispense,
            liquid_height=dis_liquid_h,
            spread=spread,
        )
        if delays is not None and len(delays) > 1:
            await self.custom_delay(seconds=delays[1])
        if mix_stage in ["after", "both"] and mix_times is not None and mix_times > 0:
            await self.mix(
                targets=mix_tgt_anchor,
                mix_time=mix_times,
                mix_vol=mix_vol,
                offsets=offsets if offsets else None,
                height_to_bottom=mix_liquid_height if mix_liquid_height else None,
                mix_rate=mix_rate if mix_rate else None,
                use_channels=use_channels,
            )
        if delays is not None and len(delays) > 1:
            await self.custom_delay(seconds=delays[0])
        await self.touch_tip(targets[0])
        if drop:
            await self.discard_tips(use_channels=use_channels)

    # except Exception as e:
    #     traceback.print_exc()
    #     raise RuntimeError(f"Liquid addition failed: {e}") from e

    async def _transfer_group_single_channel_fallback(self, kwargs: Dict[str, Any]) -> None:
        """8ch 井距不满足时，将当前 group 按单通道顺序执行。

        关键点：
        - 使用当前 group 的首个物理通道（保持左右轴一致，避免硬切到 channel 0）；
        - 1 个 group 的 pick_up / drop 仅在首尾子操作触发；
        - 每次子操作只执行 1 对 source/target，避免旧逻辑只跑第一个元素或错轴。
        """
        sources = list(kwargs.get("sources") or [])
        targets = list(kwargs.get("targets") or [])
        asp_vols = list(kwargs.get("asp_vols") or [])
        dis_vols = list(kwargs.get("dis_vols") or [])
        n = min(len(sources), len(targets), len(asp_vols), len(dis_vols))
        if n <= 0:
            return

        raw_channels = kwargs.get("use_channels")
        if isinstance(raw_channels, (list, tuple)) and len(raw_channels) > 0:
            fallback_channel = int(raw_channels[0])
        else:
            fallback_channel = 0

        def _pick_seq_value(val: Any, idx: int) -> Any:
            if val is None:
                return None
            if isinstance(val, (list, tuple)):
                if len(val) == 0:
                    return None
                return [val[idx] if idx < len(val) else val[-1]]
            return [val]

        seq_keys = (
            "asp_flow_rates",
            "dis_flow_rates",
            "offsets",
            "liquid_height",
            "blow_out_air_volume",
            "blow_out_air_volume_before",
            "delays",
        )

        for idx in range(n):
            sub = dict(kwargs)
            sub["sources"] = [sources[idx]]
            sub["targets"] = [targets[idx]]
            sub["asp_vols"] = [asp_vols[idx]]
            sub["dis_vols"] = [dis_vols[idx]]
            sub["use_channels"] = [fallback_channel]
            sub["pick_up"] = bool(kwargs.get("pick_up", True) and idx == 0)
            sub["drop"] = bool(kwargs.get("drop", True) and idx == (n - 1))

            for key in seq_keys:
                if key in kwargs:
                    sub[key] = _pick_seq_value(kwargs.get(key), idx)

            # group 级 mix / delay 只保留在首个子操作，避免 8 倍放大
            if idx > 0:
                sub["mix_stage"] = "none"
                sub["delays"] = None

            await self._transfer_base_method(**sub)

    # ---------------------------------------------------------------
    # Helper utilities
    # ---------------------------------------------------------------

    async def custom_delay(self, seconds=0, msg=None):
        """
        seconds: seconds to wait
        msg: information to be printed
        """
        if seconds != None and seconds > 0:
            if msg:
                print(f"Waiting time: {msg}")
                print(f"Current time: {time.strftime('%H:%M:%S')}")
                print(f"Time to finish: {time.strftime('%H:%M:%S', time.localtime(time.time() + seconds))}")
            # Use ROS node sleep if available, otherwise use asyncio.sleep
            if hasattr(self, '_ros_node') and self._ros_node is not None:
                await self._ros_node.sleep(seconds)
            else:
                import asyncio
                await asyncio.sleep(seconds)
            if msg:
                print(f"Done: {msg}")
                print(f"Current time: {time.strftime('%H:%M:%S')}")

    async def touch_tip(self, targets: Sequence[Container]):
        """Touch the tip to the side of the well."""

        if not self.support_touch_tip:
            return
        await self.aspirate(
            resources=[targets],
            vols=[0],
            use_channels=None,
            flow_rates=None,
            offsets=[Coordinate(x=-targets.get_size_x() / 2, y=0, z=0)],
            liquid_height=None,
            blow_out_air_volume=None,
        )
        # await self.custom_delay(seconds=1) # In the simulation, we do not need to wait
        await self.aspirate(
            resources=[targets],
            vols=[0],
            use_channels=None,
            flow_rates=None,
            offsets=[Coordinate(x=targets.get_size_x() / 2, y=0, z=0)],
            liquid_height=None,
            blow_out_air_volume=None,
        )

    async def mix(
        self,
        targets: Sequence[Container],
        mix_time: int = None,
        mix_vol: Optional[int] = None,
        height_to_bottom: Optional[float] = None,
        offsets: Optional[Coordinate] = None,
        mix_rate: Optional[float] = None,
        use_channels: Optional[List[int]] = None,
        none_keys: List[str] = [],
    ):
        if mix_time is None or mix_time <= 0:  # No mixing required
            return
        """Mix the liquid in the target wells."""
        if mix_vol is None:
            raise ValueError("`mix_vol` must be provided when `mix_time` is set.")

        targets_list: List[Container] = list(targets)
        if len(targets_list) == 0:
            return

        def _expand(value, count: int):
            if value is None:
                return [None] * count
            if isinstance(value, (list, tuple)):
                if len(value) != count:
                    raise ValueError("Length of per-target parameters must match targets.")
                return list(value)
            return [value] * count

        offsets_list = _expand(offsets, len(targets_list))
        heights_list = _expand(height_to_bottom, len(targets_list))
        rates_list = _expand(mix_rate, len(targets_list))

        for _ in range(mix_time):
            for idx, target in enumerate(targets_list):
                offset_arg = (
                    [offsets_list[idx]] if offsets_list[idx] is not None else None
                )
                height_arg = (
                    [heights_list[idx]] if heights_list[idx] is not None else None
                )
                rate_arg = [rates_list[idx]] if rates_list[idx] is not None else None

                await self.aspirate(
                    resources=[target],
                    vols=[mix_vol],
                    use_channels=use_channels,
                    flow_rates=rate_arg,
                    offsets=offset_arg,
                    liquid_height=height_arg,
                )
                await self.custom_delay(seconds=1)
                await self.dispense(
                    resources=[target],
                    vols=[mix_vol],
                    use_channels=use_channels,
                    flow_rates=rate_arg,
                    offsets=offset_arg,
                    liquid_height=height_arg,
                )

    def iter_tips(self, tip_racks: Sequence[TipRack]) -> Iterator[Resource]:
        """Yield tips from a list of TipRacks one-by-one until depleted."""
        for rack in tip_racks:
            yield from self._iter_tips_single_rack_or_spot(rack)

    def _iter_tips_single_rack_or_spot(self, rack: Resource) -> Iterator[Resource]:
        """单盒或单孔：与 ``iter_tips`` 中单项逻辑一致，供扁平池构建复用。"""
        if isinstance(rack, TipSpot):
            yield rack
        elif isinstance(rack, TipRack):
            for item in rack:
                if isinstance(item, list):
                    yield from item
                else:
                    yield item

    def _flatten_tips_from_one(self, rack: Resource) -> List[Resource]:
        """将单个 TipRack/TipSpot 展开为孔位列表（顺序与 ``iter_tips`` 一致）。"""
        return list(self._iter_tips_single_rack_or_spot(rack))

    # 列式 8 通道硬件（如 PRCXI）按整列取枪头：子类置 True 后，多通道取枪头会做列对齐。
    # 默认 False，不影响可任意取枪头的非列式设备。
    _pickup_column_aligned: bool = False

    def _tip_column_height(self, key) -> int:
        """该型号枪头列高 = 首个 rack 的 ``num_items_y``（列优先扁平池里每列的孔数）。"""
        racks = (getattr(self, "_tip_racks_by_type", None) or {}).get(key) or []
        if not racks:
            return 0
        ny = getattr(racks[0], "num_items_y", None)
        try:
            n = int(ny) if ny is not None else 0
        except (TypeError, ValueError):
            n = 0
        return n if n > 0 else 0

    def _acquire_tip_column(self, n_ch: int) -> List[Resource]:
        """取 ``n_ch`` 个枪头。

        列对齐（仅当 ``_pickup_column_aligned`` 且 ``n_ch > 1``）：若 ``_tip_next_index`` 不在列
        边界（当前列剩余不足以从列首取整列），跳过当前列残余枪头、对齐到下一整列开头再取。
        被跳过的残余枪头视为弃用（不再分配）。``n_ch == 1`` 或未开启时行为不变。
        """
        if getattr(self, "_pickup_column_aligned", False) and n_ch > 1:
            key = getattr(self, "_active_tip_type_key", None)
            flat = (getattr(self, "_tip_flat_spots", None) or {}).get(key) if key else None
            if key is not None and flat:
                ny = self._tip_column_height(key)
                if ny and ny > 0:
                    idx = self._tip_next_index.get(key, 0)
                    rem = idx % ny
                    if rem != 0:
                        # 跳过当前列残余，对齐到下一整列列首
                        self._tip_next_index[key] = idx + (ny - rem)
        return [self._get_next_tip() for _ in range(n_ch)]

    def _get_next_tip(self):
        """从按型号分组的扁平枪头池取下一孔；耗尽时抛出明确错误而非 StopIteration。"""
        key = getattr(self, "_active_tip_type_key", None)
        flat_map = getattr(self, "_tip_flat_spots", None)
        if key is not None and flat_map is not None:
            flat = flat_map.get(key)
            if flat is not None and len(flat) > 0:
                idx = self._tip_next_index.get(key, 0)
                if idx < len(flat):
                    self._tip_next_index[key] = idx + 1
                    return flat[idx]
                diag = (
                    f"active_type_key={key}, next_index={idx}, pool_len={len(flat)}; "
                    f"_tip_racks_by_type[{key}] count={len(self._tip_racks_by_type.get(key, []))}"
                )
                raise RuntimeError(
                    "Tip rack exhausted: no more tips available for this tip type. "
                    f"Diagnostics: {diag}"
                )

        if not hasattr(self, "current_tip"):
            raise RuntimeError(
                "No tip source: call set_tiprack with TipRack/TipSpot before picking tips."
            )
        try:
            return next(self.current_tip)
        except StopIteration as e:
            diag_parts = []
            tip_racks = getattr(self, "tip_racks", None)
            if tip_racks is not None:
                for idx, rack in enumerate(tip_racks):
                    r_name = getattr(rack, "name", "?")
                    r_type = type(rack).__name__
                    is_tr = isinstance(rack, TipRack)
                    is_ts = isinstance(rack, TipSpot)
                    n_children = len(getattr(rack, "children", []))
                    diag_parts.append(
                        f"rack[{idx}] name={r_name}, type={r_type}, "
                        f"is_TipRack={is_tr}, is_TipSpot={is_ts}, children={n_children}"
                    )
            else:
                diag_parts.append("tip_racks=None")
            by_type = getattr(self, "_tip_racks_by_type", {})
            diag_parts.append(f"_tip_racks_by_type keys={list(by_type.keys())}")
            active = getattr(self, "_active_tip_type_key", None)
            diag_parts.append(f"_active_tip_type_key={active}")
            raise RuntimeError(
                "Tip rack exhausted: no more tips available for transfer. "
                f"Diagnostics: {'; '.join(diag_parts)}"
            ) from e

    @staticmethod
    def _tip_type_key(rack: Resource) -> str:
        """生成枪头盒的分组键：优先用 model（区分 10uL/300uL 等），否则回退到类名。"""
        model = getattr(rack, "model", None)
        if model and str(model).strip():
            return str(model).strip()
        return type(rack).__name__

    def _register_rack(self, rack: Resource) -> None:
        """将单个 TipRack/TipSpot 注册到按型号分组的扁平池（去重、不重置已消耗下标）。"""
        if not isinstance(rack, (TipRack, TipSpot)):
            return
        rack_name = rack.name if hasattr(rack, "name") else str(id(rack))
        if rack_name in self._seen_rack_names:
            return
        self._seen_rack_names.add(rack_name)
        type_key = self._tip_type_key(rack)
        self._tip_racks_by_type.setdefault(type_key, []).append(rack)
        self._tip_flat_spots.setdefault(type_key, []).extend(self._flatten_tips_from_one(rack))
        self._tip_next_index.setdefault(type_key, 0)

    def _init_all_tip_pools(self) -> None:
        """首次调用：从 deck 上一次性扫描所有 TipRack/TipSpot，构建完整的按型号扁平池。"""
        self._tip_racks_by_type: Dict[str, List[TipRack]] = {}
        self._seen_rack_names: Set[str] = set()
        self._tip_flat_spots: Dict[str, List[Resource]] = {}
        self._tip_next_index: Dict[str, int] = {}
        self._tip_pools_initialized = True

        # 遍历 deck 直接子资源，收集所有 TipRack
        deck = getattr(self, "deck", None)
        if deck is not None:
            for child in deck.children:
                self._register_rack(child)

    def set_tiprack(self, tip_racks: Sequence[TipRack]):
        """设置当前 transfer 使用的枪头类型。

        首次调用时从 ``self.deck`` 一次性扫描所有 TipRack/TipSpot，按
        ``model``（或 ``type(rack).__name__``）分组构建扁平枪头池与消费下标。
        后续调用仅切换 ``_active_tip_type_key``，不重建池。

        同型号多次 transfer 时，游标接续（如 A1-A12 用完后继续 B1-B12），
        而非从新盒 A1 重新开始。
        """
        # —— 首次：全量初始化 ——
        if not getattr(self, "_tip_pools_initialized", False):
            self._init_all_tip_pools()

        # 将本次传入但 deck 上不存在的新盒也注册进去（兜底）
        for rack in tip_racks:
            self._register_rack(rack)

        # —— 切换当前激活的枪头类型（按 model 区分 10uL/300uL 等）——
        first_valid = next(
            (r for r in tip_racks if isinstance(r, (TipRack, TipSpot))),
            None,
        )
        self._active_tip_type_key = (
            self._tip_type_key(first_valid) if first_valid is not None else None
        )

        # 兼容旧路径（add_liquid / remove_liquid 等可能直接用 current_tip）
        self.tip_racks = tip_racks
        valid_racks = [r for r in tip_racks if isinstance(r, (TipRack, TipSpot))]
        if not valid_racks:
            valid_racks = [r for racks in self._tip_racks_by_type.values() for r in racks]
        self.current_tip = self.iter_tips(valid_racks)

    async def move_to(self, well: Well, dis_to_top: float = 0, channel: int = 0):
        """
        Move a single channel to a specific well with a given z-height.

        Parameters
        ----------
        well : Well
            The target well.
        dis_to_top : float
            Height in mm to move to relative to the well top.
        channel : int
            Pipetting channel to move (default: 0).
        """
        await self.prepare_for_manual_channel_operation(channel=channel)
        abs_loc = well.get_absolute_location()
        well_height = well.get_absolute_size_z()
        await self.move_channel_x(channel, abs_loc.x)
        await self.move_channel_y(channel, abs_loc.y)
        await self.move_channel_z(channel, abs_loc.z + well_height + dis_to_top)
