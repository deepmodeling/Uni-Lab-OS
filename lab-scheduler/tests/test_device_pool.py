"""Tests for DevicePool and build_device_pool."""

from __future__ import annotations

from scheduler.core.device_pool import build_device_pool
from scheduler.models.resources import DevicePool


class TestDevicePool:
    def test_add_device_type(self):
        pool = DevicePool()
        pool.add_device_type("A", 3)
        assert len(pool.instances) == 3
        assert "A_0" in pool.instances
        assert "A_1" in pool.instances
        assert "A_2" in pool.instances
        for iid, inst in pool.instances.items():
            assert inst.device_type == "A"
            assert inst.available_from == 0

    def test_multiple_types(self):
        pool = DevicePool()
        pool.add_device_type("A", 2)
        pool.add_device_type("B", 1)
        assert len(pool.instances) == 3
        assert pool.instances["A_0"].device_type == "A"
        assert pool.instances["B_0"].device_type == "B"

    def test_get_available_returns_free_device(self):
        pool = DevicePool()
        pool.add_device_type("A", 2)
        result = pool.get_available("A", at_time=0)
        assert result is not None
        assert result.startswith("A_")

    def test_get_available_respects_allocation(self):
        pool = DevicePool()
        pool.add_device_type("A", 1)
        pool.allocate("A_0", until=10)
        # 在 time=5 时设备还不可用
        assert pool.get_available("A", at_time=5) is None
        # 在 time=10 时设备可用
        assert pool.get_available("A", at_time=10) == "A_0"

    def test_get_available_unknown_type(self):
        pool = DevicePool()
        pool.add_device_type("A", 1)
        assert pool.get_available("X", at_time=0) is None

    def test_get_earliest(self):
        pool = DevicePool()
        pool.add_device_type("A", 2)
        pool.allocate("A_0", until=20)
        pool.allocate("A_1", until=10)
        result = pool.get_earliest("A")
        assert result is not None
        iid, earliest_time = result
        assert iid == "A_1"
        assert earliest_time == 10

    def test_get_earliest_no_device(self):
        pool = DevicePool()
        assert pool.get_earliest("A") is None

    def test_allocate_updates_available_from(self):
        pool = DevicePool()
        pool.add_device_type("A", 1)
        assert pool.instances["A_0"].available_from == 0
        pool.allocate("A_0", until=15)
        assert pool.instances["A_0"].available_from == 15

    def test_allocate_sequential(self):
        pool = DevicePool()
        pool.add_device_type("A", 1)
        pool.allocate("A_0", until=10)
        pool.allocate("A_0", until=25)
        assert pool.instances["A_0"].available_from == 25


class TestBuildDevicePool:
    def test_from_machine_specs(self):
        specs = [{"type": "Heater", "count": 2}, {"type": "Mixer", "count": 1}]
        pool = build_device_pool(specs)
        assert len(pool.instances) == 3
        assert "Heater_0" in pool.instances
        assert "Heater_1" in pool.instances
        assert "Mixer_0" in pool.instances

    def test_empty_specs(self):
        pool = build_device_pool([])
        assert len(pool.instances) == 0
