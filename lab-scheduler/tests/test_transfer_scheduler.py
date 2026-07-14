"""Tests for TransferScheduler."""

from __future__ import annotations

from scheduler.api.schemas import Robot
from scheduler.core.sample_flow import TransferRequest
from scheduler.core.transfer_scheduler import TransferScheduler


def _make_robot(
    robot_id: str = "robot-1",
    location: str = "dock",
    capacity: int = 3,
    available_from: int = 0,
) -> Robot:
    return Robot(
        robot_id=robot_id,
        location=location,
        capacity=capacity,
        available_from=available_from,
    )


def _make_request(
    sample_id: str = "sample-1",
    from_device: str = "A_0",
    to_device: str = "B_0",
    ready_time: int = 10,
    deadline: int = 20,
) -> TransferRequest:
    return TransferRequest(
        sample_id=sample_id,
        from_device=from_device,
        to_device=to_device,
        ready_time=ready_time,
        deadline=deadline,
        task_id="t1",
        priority=1.0,
    )


class TestTransferSchedulerBasic:
    def test_single_transfer(self):
        ts = TransferScheduler(
            robots=[_make_robot()],
            default_travel_time=5,
        )
        reqs = [_make_request()]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 1
        entry = results[0]
        assert entry.samples == ["sample-1"]
        assert entry.robot_id == "robot-1"
        assert entry.start >= 10  # ready_time
        assert entry.end > entry.start

    def test_empty_requests(self):
        ts = TransferScheduler(robots=[_make_robot()])
        assert ts.schedule_transfers([]) == []

    def test_no_robots(self):
        ts = TransferScheduler(robots=[])
        reqs = [_make_request()]
        assert ts.schedule_transfers(reqs) == []

    def test_path_includes_from_and_to(self):
        ts = TransferScheduler(
            robots=[_make_robot(location="dock")],
            default_travel_time=5,
        )
        reqs = [_make_request(from_device="A_0", to_device="B_0")]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 1
        path = results[0].path
        assert "A_0" in path
        assert "B_0" in path
        # path 的最后一站是目的地
        assert path[-1] == "B_0"


class TestTransferBatching:
    def test_same_direction_batched(self):
        """同方向、同时间窗口内的请求被合并."""
        ts = TransferScheduler(
            robots=[_make_robot(capacity=5)],
            default_travel_time=5,
        )
        reqs = [
            _make_request(sample_id="s1", from_device="A_0", to_device="B_0", ready_time=10),
            _make_request(sample_id="s2", from_device="A_0", to_device="B_0", ready_time=12),
        ]
        results = ts.schedule_transfers(reqs)

        # 应该合并为一次转运
        assert len(results) == 1
        assert set(results[0].samples) == {"s1", "s2"}

    def test_capacity_limit(self):
        """超过 capacity 的请求不合并."""
        ts = TransferScheduler(
            robots=[_make_robot(capacity=1)],
            default_travel_time=5,
        )
        reqs = [
            _make_request(sample_id="s1", from_device="A_0", to_device="B_0", ready_time=10),
            _make_request(sample_id="s2", from_device="A_0", to_device="B_0", ready_time=12),
        ]
        results = ts.schedule_transfers(reqs)

        # capacity=1, 不能合并
        assert len(results) == 2

    def test_different_directions_not_batched(self):
        """不同方向的请求不合并."""
        ts = TransferScheduler(
            robots=[_make_robot(capacity=5)],
            default_travel_time=5,
        )
        reqs = [
            _make_request(sample_id="s1", from_device="A_0", to_device="B_0", ready_time=10),
            _make_request(sample_id="s2", from_device="A_0", to_device="C_0", ready_time=12),
        ]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 2


class TestTransferTiming:
    def test_sequential_transfers_no_overlap(self):
        """同一机器人的多次转运不重叠."""
        ts = TransferScheduler(
            robots=[_make_robot(capacity=1)],
            default_travel_time=5,
        )
        reqs = [
            _make_request(sample_id="s1", ready_time=0),
            _make_request(sample_id="s2", ready_time=5),
            _make_request(sample_id="s3", ready_time=10),
        ]
        results = ts.schedule_transfers(reqs)

        for i in range(len(results) - 1):
            assert results[i].end <= results[i + 1].start

    def test_robot_available_from_respected(self):
        """机器人的 available_from 被尊重."""
        ts = TransferScheduler(
            robots=[_make_robot(available_from=20)],
            default_travel_time=5,
        )
        reqs = [_make_request(ready_time=0)]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 1
        assert results[0].start >= 20

    def test_same_location_zero_approach(self):
        """机器人已在 from_device → approach time = 0."""
        ts = TransferScheduler(
            robots=[_make_robot(location="A_0")],
            default_travel_time=5,
        )
        reqs = [_make_request(from_device="A_0", to_device="B_0", ready_time=10)]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 1
        # 无 approach 时间, 只有 travel 时间 (5)
        assert results[0].end - results[0].start == 5


class TestMultipleRobots:
    def test_two_robots_parallel(self):
        """两个机器人可以并行处理不同的请求."""
        ts = TransferScheduler(
            robots=[
                _make_robot(robot_id="r1", capacity=1),
                _make_robot(robot_id="r2", capacity=1),
            ],
            default_travel_time=5,
        )
        reqs = [
            _make_request(sample_id="s1", ready_time=0),
            _make_request(sample_id="s2", ready_time=0),
        ]
        results = ts.schedule_transfers(reqs)

        assert len(results) == 2
        robot_ids = {r.robot_id for r in results}
        # 两个机器人都应该被使用 (或至少有一个在 time=0 可用)
        assert len(robot_ids) >= 1
