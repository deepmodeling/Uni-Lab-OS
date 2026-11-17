# -*- coding: utf-8 -*-
"""配液溶解度列与扣电对中列的对齐规则。"""
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unilabos.devices.workstation.coin_cell_assembly.alignment_query import (
    ALIGNMENT_CSV_COLUMNS,
    AlignmentUnavailable,
    backfill_alignment_csv,
    parse_alignment_line,
    values_for_battery,
)
from unilabos.devices.workstation.mixing_judgment_query import (
    fetch_mixing_records,
    mixing_column_enabled,
    pick_latest_judgments,
    judgment_window,
)


TZ = timezone(timedelta(hours=8))


def test_observer_assembly_header_ends_with_alignment_columns():
    from unilabos.devices.workstation.coin_cell_assembly.coin_cell_assembly import (
        _OBSERVER_ASSEMBLY_HEADER,
        _OBSERVER_NEWARE_HEADER,
    )

    assert list(_OBSERVER_ASSEMBLY_HEADER[-4:]) == list(ALIGNMENT_CSV_COLUMNS)
    assert "coaxiality" not in _OBSERVER_NEWARE_HEADER


def test_mixing_column_only_on_real_host():
    assert mixing_column_enabled("http://172.16.28.128:44389")
    assert not mixing_column_enabled("http://172.16.28.127:44389")
    assert not mixing_column_enabled("http://172.16.28.1280:44389")
    assert not mixing_column_enabled("http://example.com/?x=172.16.28.128")
    assert not mixing_column_enabled("")


def test_judgment_follows_order_name_not_return_order():
    start, end = judgment_window(
        datetime(2026, 10, 28, 12, 0, tzinfo=TZ).timestamp(),
        now=datetime(2026, 10, 28, 13, 0, tzinfo=TZ),
    )
    records = [
        {"orderName": "DDCC1002", "judgment": 0, "processed_at": "2026-10-28T12:10:00+08:00"},
        {"orderName": "DDCC1001", "judgment": 1, "processed_at": "2026-10-28T12:05:00+08:00"},
        {"orderName": "DDCC1001", "judgment": -1, "processed_at": "2026-10-27T12:05:00+08:00"},
        {"orderName": "OTHER", "judgment": 1, "processed_at": "2026-10-28T12:06:00+08:00"},
        {"orderName": "DDCC1003", "judgment": 1, "processed_at": "not-a-time"},
    ]
    picked = pick_latest_judgments(records, ["DDCC1001", "DDCC1002", "DDCC1003"], start, end)
    assert picked["DDCC1001"] == "1"
    assert picked["DDCC1002"] == "0"
    assert "DDCC1003" not in picked
    assert "OTHER" not in picked


def test_latest_in_window_wins_even_if_older_record_is_last():
    start, end = judgment_window(
        datetime(2026, 10, 28, 12, 0, tzinfo=TZ).timestamp(),
        now=datetime(2026, 10, 28, 13, 0, tzinfo=TZ),
    )
    records = [
        {"orderName": "DDCC1008", "judgment": 1, "processed_at": "2026-10-28T12:20:00+08:00"},
        {"orderName": "DDCC1008", "judgment": 0, "processed_at": "2026-10-28T12:10:00+08:00"},
    ]
    assert pick_latest_judgments(records, ["DDCC1008"], start, end)["DDCC1008"] == "1"


def test_fetch_keeps_names_when_host_returns_reversed_rows(monkeypatch):
    frames = [
        {
            "type": "query_begin",
            "protocol": "dissolution-results-v1",
            "stream_id": "stream-1",
            "snapshot_seq": 3,
        },
        {
            "type": "result",
            "orderName": "DDCC1002",
            "judgment": 0,
            "processed_at": "2026-10-28T12:02:00+08:00",
        },
        {
            "type": "result",
            "orderName": "DDCC1001",
            "judgment": 1,
            "processed_at": "2026-10-28T12:01:00+08:00",
        },
        {
            "type": "query_end",
            "has_more": False,
            "next_after_seq": 3,
            "count": 2,
            "stream_id": "stream-1",
            "snapshot_seq": 3,
        },
    ]
    payload = ("\n".join(json.dumps(frame) for frame in frames) + "\n").encode("utf-8")

    class FakeConn:
        def __init__(self):
            self.buf = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, timeout):
            return None

        def sendall(self, data):
            return None

        def recv(self, size):
            chunk, self.buf = self.buf[:size], self.buf[size:]
            return chunk

    monkeypatch.setattr(
        "unilabos.devices.workstation.mixing_judgment_query.socket.create_connection",
        lambda *args, **kwargs: FakeConn(),
    )
    records = fetch_mixing_records(["DDCC1001", "DDCC1002"])
    by_name = {row["orderName"]: row["judgment"] for row in records}
    assert by_name == {"DDCC1001": 1, "DDCC1002": 0}


def test_seven_digit_code_with_trailing_register_five_still_matches():
    row = {
        "time": "20261030_152538",
        "code": "1503100",
        "values": ("0.286mm", "0.260mm", "0.026mm", "0.286mm"),
    }
    assert values_for_battery("15031005", "20261030_151227", row) == (
        "0.286mm", "0.260mm", "0.026mm", "0.286mm",
    )
    other = dict(row, code="1515426")
    assert values_for_battery("15031005", "20261030_151227", other) == ("", "", "", "")


def test_backfill_retries_without_trailing_register_five(tmp_path: Path):
    csv_path = tmp_path / "observe.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Time", "coin_cell_code", "coaxiality"])
        writer.writerow(["20261030_151227", "15031005", ""])
        writer.writerow(["20261030_151906", "YS807728", ""])
    seen = []

    def lookup(code):
        seen.append(code)
        if code == "15031005":
            return None
        if code == "1503100":
            return {
                "time": "20261030_152538",
                "code": "1503100",
                "values": ("0.286mm", "0.260mm", "0.026mm", "0.286mm"),
            }
        if code == "YS807728":
            return {
                "time": "20261030_151910",
                "code": "YS807728",
                "values": ("0.1mm", "0.2mm", "0.3mm", "0.3mm"),
            }
        raise AssertionError(code)

    backfill_alignment_csv(str(csv_path), lookup=lookup)
    assert seen == ["15031005", "1503100", "YS807728"]
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["coaxiality"] == "0.286mm"
    assert rows[0]["coin_cell_code"] == "15031005"
    assert rows[1]["coaxiality"] == "0.3mm"


def test_alignment_rejects_other_code_and_stale_time():
    fresh = {
        "time": "20261028_120500",
        "code": "YS1",
        "values": ("0.1mm", "0.2mm", "0.3mm", "0.3mm"),
    }
    assert values_for_battery("YS2", "20261028_120000", fresh) == ("", "", "", "")
    older = {
        "time": "20261028_100000",
        "code": "YS1",
        "values": ("0.1mm", "0.2mm", "0.3mm", "0.3mm"),
    }
    assert values_for_battery("YS1", "20261028_120500", older) == ("", "", "", "")
    assert values_for_battery("YS1", "20261028_120000", fresh) == ("0.1mm", "0.2mm", "0.3mm", "0.3mm")
    assert values_for_battery("N/A", "20261028_120000", fresh) == ("", "", "", "")


def test_parse_alignment_line():
    parsed = parse_alignment_line("ROW,20261027_153012,YS807728,0.086mm,0.190mm,0.285mm,0.285mm")
    assert parsed is not None
    assert parsed["code"] == "YS807728"
    assert parsed["values"] == ("0.086mm", "0.190mm", "0.285mm", "0.285mm")


def _write_assembly_csv(path: Path):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "Time", "coin_cell_code", "target_mass_ratio",
            "pos_sheet_to_pos_case", "neg_sheet_to_pos_case",
            "pos_sheet_to_neg_sheet", "coaxiality",
        ])
        writer.writerow(["20261028_120000", "YS1", '{"A":1,"B":2}', "", "", "", ""])
        writer.writerow(["20261028_120100", "YS2", '{"A":3,"B":4}', "", "", "", ""])
        writer.writerow(["20261028_130000", "YS1", "{}", "", "", "", ""])


def test_backfill_does_not_shift_rows_or_reuse_later_measurement(tmp_path: Path):
    csv_path = tmp_path / "coin.csv"
    _write_assembly_csv(csv_path)

    def lookup(code):
        if code == "YS2":
            return {
                "time": "20261028_120130",
                "code": "YS1",
                "values": ("9mm", "9mm", "9mm", "9mm"),
            }
        if code == "YS1":
            return {
                "time": "20261028_130200",
                "code": "YS1",
                "values": ("0.1mm", "0.2mm", "0.3mm", "0.3mm"),
            }
        raise AssertionError(code)

    filled = backfill_alignment_csv(str(csv_path), lookup=lookup)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert filled == 1
    assert rows[0]["coaxiality"] == ""
    assert rows[1]["coaxiality"] == ""
    assert rows[1]["target_mass_ratio"] == '{"A":3,"B":4}'
    assert rows[2]["coaxiality"] == "0.3mm"
    assert rows[2]["pos_sheet_to_pos_case"] == "0.1mm"
    assert rows[0]["target_mass_ratio"] == '{"A":1,"B":2}'


def test_backfill_stops_querying_after_connection_failure(tmp_path: Path):
    csv_path = tmp_path / "coin.csv"
    _write_assembly_csv(csv_path)
    seen = []

    def lookup(code):
        seen.append(code)
        raise AlignmentUnavailable("down")

    backfill_alignment_csv(str(csv_path), lookup=lookup)
    assert seen == ["YS1"]
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert all(row["coaxiality"] == "" for row in rows)


def test_each_battery_keeps_its_own_measurement(tmp_path: Path):
    csv_path = tmp_path / "coin.csv"
    _write_assembly_csv(csv_path)

    def lookup(code):
        table = {
            "YS1": ("20261028_120020", ("0.1mm", "0.2mm", "0.3mm", "0.3mm")),
            "YS2": ("20261028_120140", ("0.4mm", "0.5mm", "0.6mm", "0.6mm")),
        }
        stamp, values = table[code]
        return {"time": stamp, "code": code, "values": values}

    backfill_alignment_csv(str(csv_path), lookup=lookup)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["coaxiality"] == "0.3mm"
    assert rows[1]["coaxiality"] == "0.6mm"
    assert rows[2]["coaxiality"] == ""
