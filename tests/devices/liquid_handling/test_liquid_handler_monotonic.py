"""set_liquid_from_plate 等物料回写的 10 s 上限按单调时钟计：墙钟前跳不提前判超时。

只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import itertools
import time
from types import SimpleNamespace

from pylabrobot.resources import Coordinate, Deck, set_volume_tracking
from pylabrobot.resources.corning.plates import cor_96_wellplate_2mL_Vb

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract
from unilabos.resources.resource_tracker import ResourceTreeSet


def _plate():
    deck = Deck(name="deck", size_x=600, size_y=400, size_z=100)
    deck.assign_child_resource(cor_96_wellplate_2mL_Vb("plate"), location=Coordinate(200, 10, 0))
    tree = ResourceTreeSet.from_plr_resources([deck], known_random_uuid=True).dump()
    return ResourceTreeSet.load(tree).to_plr_resources()[0].get_resource("plate")


class _Node:
    """回写任务 0.3 s 后完成（按单调时钟）；记录 info 日志。"""

    def __init__(self) -> None:
        self.logged: list[str] = []

    def update_resource(self, _wells):
        return None

    def create_task(self, _work):
        done_at = time.monotonic() + 0.3
        return SimpleNamespace(done=lambda: time.monotonic() >= done_at)

    def lab_logger(self):
        return SimpleNamespace(info=self.logged.append)


def test_set_liquid_from_plate_wait_ignores_forward_wall_clock_jumps(monkeypatch) -> None:
    plate = _plate()
    node = _Node()
    real_time = time.time
    reads = itertools.count(1)
    # 每读一次墙钟，它都比上一次再前跳 13 s
    monkeypatch.setattr(time, "time", lambda: real_time() + 13.0 * next(reads))

    set_volume_tracking(enabled=True)
    try:
        result = LiquidHandlerAbstract.set_liquid_from_plate(
            SimpleNamespace(_ros_node=node), plate, ["A1"], ["water"], [10.0]
        )
    finally:
        set_volume_tracking(enabled=False)

    assert result["volumes"] == [10.0]
    assert [line for line in node.logged if "超时" in line] == []
