"""机器人转运调度: 贪心批量 + 路径优化."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from scheduler.api.schemas import Robot, TransferEntry
from scheduler.core.sample_flow import TransferRequest


@dataclass
class RobotInstance:
    robot_id: str
    location: str
    capacity: int
    available_from: int = 0


class TransferScheduler:
    """贪心批量转运调度器."""

    def __init__(
        self,
        robots: list[Robot],
        device_distances: dict[tuple[str, str], int] | None = None,
        default_travel_time: int = 5,
    ) -> None:
        self.robots = [
            RobotInstance(
                robot_id=r.robot_id,
                location=r.location,
                capacity=r.capacity,
                available_from=r.available_from,
            )
            for r in robots
        ]
        self.distances = device_distances or {}
        self.default_travel_time = default_travel_time

    def _travel_time(self, from_dev: str, to_dev: str) -> int:
        if from_dev == to_dev:
            return 0
        return self.distances.get(
            (from_dev, to_dev), self.default_travel_time
        )

    def _get_earliest_robot(self, at_time: int) -> RobotInstance | None:
        available = [r for r in self.robots if r.available_from <= at_time]
        if available:
            return min(available, key=lambda r: r.available_from)
        if self.robots:
            return min(self.robots, key=lambda r: r.available_from)
        return None

    def schedule_transfers(
        self, requests: list[TransferRequest]
    ) -> list[TransferEntry]:
        """贪心批量转运调度.

        1. 按 ready_time 排序
        2. 时间窗口内合并同方向请求 (不超过 capacity)
        3. 分配最早可用机器人
        4. 计算路径和时间
        """
        if not requests or not self.robots:
            return []

        sorted_reqs = sorted(requests, key=lambda r: r.ready_time)
        results: list[TransferEntry] = []
        used: set[int] = set()

        for i, req in enumerate(sorted_reqs):
            if i in used:
                continue

            # 收集可合并的请求 (同方向, 同时间窗口, 不超 capacity)
            batch_samples = [req.sample_id]
            batch_reqs = [req]
            used.add(i)

            robot = self._get_earliest_robot(req.ready_time)
            if robot is None:
                continue

            window = max(self.default_travel_time, 5)
            for j in range(i + 1, len(sorted_reqs)):
                if j in used:
                    continue
                other = sorted_reqs[j]
                if other.ready_time - req.ready_time > window:
                    break
                if (
                    other.from_device == req.from_device
                    and other.to_device == req.to_device
                    and len(batch_samples) < robot.capacity
                ):
                    batch_samples.append(other.sample_id)
                    batch_reqs.append(other)
                    used.add(j)

            # 计算路径和时间
            start_time = max(req.ready_time, robot.available_from)
            travel = self._travel_time(req.from_device, req.to_device)
            # 路径: robot_location → from_device → to_device
            approach = self._travel_time(robot.location, req.from_device)
            total_time = approach + travel
            end_time = start_time + total_time

            path = [robot.location, req.from_device, req.to_device]
            # 去重连续相同位置
            clean_path = [path[0]]
            for p in path[1:]:
                if p != clean_path[-1]:
                    clean_path.append(p)

            robot.available_from = end_time
            robot.location = req.to_device

            results.append(
                TransferEntry(
                    transfer_id=str(uuid.uuid4()),
                    samples=batch_samples,
                    start=start_time,
                    end=end_time,
                    robot_id=robot.robot_id,
                    path=clean_path,
                )
            )

        return results
