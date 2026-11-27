"""出片公共层：冻结时间线、镜头、分片、合同与验收（不需要渲染引擎）。"""

import pytest

from unilabos.simulation.render.plan import accept, camera_shots, contract, shards
from unilabos.simulation.render.timeline import derive_commands, freeze, state_at, verify


def _bundle():
    header = {"schema": "unilab.replay-bundle/1", "devices": {"arm": ["lift_joint"], "table": ["rotate_joint"]},
              "commands": [{"id": "j1", "t_start": 0.0, "t_end": 2.0, "label": "arm · pick"},
                           {"id": "j2", "t_start": 12.0, "t_end": 13.0, "label": "table · rotate"}],
              "source": {"kind": "test"}, "duration_s": 13.0}
    rows = [{"t": 0.0, "event": "j1", "values": {"arm": {"lift_joint": 0.0}, "table": {"rotate_joint": 0.0}}},
            {"t": 1.0, "event": "j1", "values": {"arm": {"lift_joint": 0.2}}},
            {"t": 2.0, "event": "idle", "values": {"arm": {"lift_joint": 0.3}}},
            {"t": 12.0, "event": "j2", "values": {"table": {"rotate_joint": 1.57}}},
            {"t": 13.0, "event": "idle", "values": {"table": {"rotate_joint": 3.14}}}]
    return header, rows


def test_focus_policy_compresses_idle_gaps_and_keeps_command_frames():
    header, rows = _bundle()
    log = freeze(header, rows, fps=10, policy="log")
    focus = freeze(header, rows, fps=10, policy="focus", idle_cap_s=1.0)
    assert log["duration_s"] == 13.0 and focus["duration_s"] == 4.0          # 2→12 的 10 s 空闲压成 1 s
    assert [c["f_start"] for c in focus["commands"]] == [0, 30]
    assert focus["frame_count"] == 41 and state_at(focus, 40)["table"]["rotate_joint"] == 3.14
    assert state_at(focus, 15)["arm"]["lift_joint"] == 0.2 and state_at(focus, 25)["arm"]["lift_joint"] == 0.3
    verify(focus)
    tampered = {**focus, "fps": 11}
    with pytest.raises(ValueError):
        verify(tampered)


def test_same_recording_freezes_to_the_same_digest():
    header, rows = _bundle()
    assert freeze(header, rows)["timeline_sha256"] == freeze(header, list(reversed(rows)))["timeline_sha256"]


def test_segments_are_derived_from_motion_events_when_the_header_has_no_jobs():
    _, rows = _bundle()
    spans = derive_commands(rows)
    assert [(s["label"], s["device"]) for s in spans] == [("j1", "arm"), ("j2", "table")]


def test_shots_follow_the_active_device_and_contract_acceptance():
    header, rows = _bundle()
    timeline = freeze(header, rows, fps=10, policy="focus", idle_cap_s=1.0)
    shots = camera_shots(timeline, {"arm": [0, 0, 0], "table": [1000, 0, 0]})
    assert [s["node"] for s in shots] == ["arm", "table"] and shots[-1]["f_end"] == timeline["frame_count"]
    assert sum(s["f_end"] - s["f_start"] for s in shards(timeline["frame_count"], 3)) == timeline["frame_count"]
    body = contract(timeline, engine="blender", adapter_version="x", scene={"nodes": []}, shots=shots, shard_count=2)
    files = {s["index"]: s["f_end"] - s["f_start"] for s in body["shards"]}
    assert accept(body, decoded_frames=timeline["frame_count"], decoded_fps=10, frame_files=files)["accepted"]
    bad = accept(body, decoded_frames=timeline["frame_count"] - 1, decoded_fps=10)
    assert not bad["accepted"] and bad["problems"]
