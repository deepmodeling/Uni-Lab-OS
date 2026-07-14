"""Resource models for the scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeviceInstance:
    """A single device instance."""

    instance_id: str
    device_type: str
    available_from: int = 0
    batch_capacity: int = 1


@dataclass
class DevicePool:
    """Manages a pool of device instances by type."""

    instances: dict[str, DeviceInstance] = field(default_factory=dict)
    _type_index: dict[str, list[str]] = field(default_factory=dict)

    def add_device_type(self, device_type: str, count: int, batch_capacity: int = 1) -> None:
        for i in range(count):
            iid = f"{device_type}_{i}"
            self.instances[iid] = DeviceInstance(
                instance_id=iid,
                device_type=device_type,
                batch_capacity=batch_capacity,
            )
            self._type_index.setdefault(device_type, []).append(iid)

    def get_available(self, device_type: str, at_time: int) -> str | None:
        """返回最早可用的设备实例 ID, 若无可用返回 None."""
        candidates = self._type_index.get(device_type, [])
        best: str | None = None
        best_time = float("inf")
        for iid in candidates:
            inst = self.instances[iid]
            if inst.available_from <= at_time and inst.available_from < best_time:
                best = iid
                best_time = inst.available_from
        return best

    def get_earliest(self, device_type: str) -> tuple[str, int] | None:
        """返回 (instance_id, earliest_available_time), 若无设备返回 None."""
        candidates = self._type_index.get(device_type, [])
        if not candidates:
            return None
        best_iid = min(candidates, key=lambda iid: self.instances[iid].available_from)
        return best_iid, self.instances[best_iid].available_from

    def allocate(self, instance_id: str, until: int) -> None:
        self.instances[instance_id].available_from = until

    def utilization(self, total_time: int) -> dict[str, float]:
        """计算各设备类型的利用率."""
        if total_time <= 0:
            return {}
        result: dict[str, float] = {}
        for dtype, iids in self._type_index.items():
            # 简化: 利用率 = 1 - (总空闲时间 / 总可用时间)
            # 精确计算需要记录分配历史, 这里用 available_from 近似
            result[dtype] = 0.0  # 占位, 精确计算在 base.py 中
        return result
