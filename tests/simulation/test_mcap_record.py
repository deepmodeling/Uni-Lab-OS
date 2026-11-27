"""MCAP 高频轨迹：与回放记录互转一致，并能和低频状态记录合并播放。"""

import json
import os

import pytest

pytest.importorskip("mcap")

from unilabos.simulation.mcap_record import read_mcap, write_mcap  # noqa: E402
from unilabos.simulation.replay_feed import command_index, load_sources, state_at  # noqa: E402


def test_mcap_round_trip_and_merge_with_state_record(tmp_path):
    joints = {"lift_joint": {"semantic": "observed", "unit": "m", "frame": "joint"}}
    header = {"schema": "unilab.replay-bundle/1", "select_by": "id", "devices": {"arm": ["lift_joint", "world_matrix"]},
              "descriptors": {"arm": {"version": "v1", "properties": {**joints, "world_matrix": {
                  "semantic": "observed", "dtype": "matrix4", "unit": "m", "frame": "world"}}}},
              "commands": [{"id": "5", "t_start": 0.5, "t_end": 1.0}]}
    pose = [1.0, 0, 0, 0.1, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    rows = [{"t": 0.0, "event": "idle", "values": {"arm": {"lift_joint": 0.0, "world_matrix": pose}}},
            {"t": 0.5, "event": "command_5", "values": {"arm": {"lift_joint": 0.25}}},
            {"t": 1.0, "event": "command_5", "values": {"arm": {"lift_joint": 0.5}}}]
    path = tmp_path / "run.mcap"
    assert write_mcap(header, rows, path) == 3
    back_header, back_rows = read_mcap(path)
    assert back_header["devices"] == header["devices"] and back_header["commands"] == header["commands"]
    assert [(row["t"], row["event"]) for row in back_rows] == [(row["t"], row["event"]) for row in rows]
    assert state_at(back_rows, len(back_rows)) == state_at(rows, len(rows))

    state = tmp_path / "state.jsonl"
    state_rows = [{"devices": {"plate": ["parent"]}, "descriptors": {"plate": {"version": "s1", "properties": {
        "parent": {"semantic": "observed", "dtype": "string", "unit": "-", "frame": "material"}}}}},
                  {"t": 0.0, "event": "idle", "values": {"plate": {"parent": "deck"}}},
                  {"t": 0.75, "event": "command_5", "values": {"plate": {"parent": "arm"}}}]
    state.write_text("\n".join(json.dumps(item) for item in state_rows) + "\n", encoding="utf-8")
    merged_header, merged_rows = load_sources(f"{path}{os.pathsep}{state}")
    assert set(merged_header["devices"]) == {"arm", "plate"}
    assert state_at(merged_rows, len(merged_rows)) == {"arm": {"lift_joint": 0.5, "world_matrix": pose},
                                                       "plate": {"parent": "arm"}}
    assert command_index(merged_header, merged_rows) == [{"id": "5", "t_start": 0.5, "t_end": 1.0}]


def test_merge_aligns_records_by_wall_clock_origin():
    from unilabos.simulation.mcap_record import merge_records

    high = ({"devices": {"arm": ["q"]}, "origin_ms": 1000.0}, [{"t": 0.0, "event": "", "values": {"arm": {"q": 1}}}])
    low = ({"devices": {"plate": ["parent"]}, "origin_ms": 3000.0,
            "commands": [{"id": "job", "t_start": 0.0, "t_end": 1.0}]},
           [{"t": 0.5, "event": "job", "values": {"plate": {"parent": "arm"}}}])
    header, rows = merge_records(high, low)
    assert [row["t"] for row in rows] == [0.0, 2.5]
    assert header["commands"] == [{"id": "job", "t_start": 2.0, "t_end": 3.0}] and header["origin_ms"] == 1000.0
