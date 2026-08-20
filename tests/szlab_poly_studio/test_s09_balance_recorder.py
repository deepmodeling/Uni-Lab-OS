from __future__ import annotations

import csv
import io

from scripts.s09_balance_recorder import monitor


class FakeClient:
    def __init__(self) -> None:
        self.done_values = iter([0, 8, 8, 6, 8])
        self.weights = iter([1.25, 2.5])

    def read(self, name: str, use_cache: bool = False):
        del use_cache
        if name == "S09工艺完成":
            return next(self.done_values)
        if name == "S09天平读数稳定":
            return True
        if name == "S09天平读数":
            return next(self.weights)
        raise KeyError(name)


def test_monitor_records_only_new_dispense_completion_edges() -> None:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=("timestamp", "sample_no", "addition_no", "weight_g"),
    )
    writer.writeheader()

    count = monitor(
        FakeClient(),
        writer,
        output,
        sample_count=1,
        additions_per_sample=2,
        poll_interval=0.01,
        stable_timeout=1,
        sleep=lambda _: None,
    )

    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert count == 2
    assert [(row["sample_no"], row["addition_no"]) for row in rows] == [("1", "1"), ("1", "2")]
    assert [row["weight_g"] for row in rows] == ["1.25", "2.5"]
