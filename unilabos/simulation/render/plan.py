"""出片计划：镜头（活跃设备跟随）、分片、版本合同与成片校验。

镜头规则只依赖冻结时间线和工站给的设备位置：每条作业期间对准执行设备，
没有作业时保持上一镜头；相邻同设备的镜头合并。工站配置可以覆盖默认机位
（俯角、方位角、距离）和“重点设备”（只给重点设备切近景，其余用全景）。
"""

from __future__ import annotations

from typing import Any

from unilabos.simulation.render.timeline import sha256, verify

CONTRACT_SCHEMA = "unilab.render-contract/1"
DEFAULT_VIEW = {"distance_mm": 3200.0, "elevation_deg": 28.0, "azimuth_deg": 225.0}
# 设备原点多在底座上，镜头对准点抬高一些，免得只拍到底座。
TARGET_LIFT_MM = 450.0
WIDE_VIEW = {"distance_mm": 9000.0, "elevation_deg": 40.0, "azimuth_deg": 210.0}


def camera_shots(timeline: dict[str, Any], positions: dict[str, list[float]], *,
                 station: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """每段镜头：[f_start, f_end) 对准 ``target_mm``。``positions`` 是设备世界坐标（mm）。"""
    station = station or {}
    focus = set(station.get("focus_devices") or positions)
    view = {**DEFAULT_VIEW, **(station.get("view") or {})}
    wide = {**WIDE_VIEW, **(station.get("wide_view") or {})}
    centre = station.get("centre_mm") or ([sum(p[i] for p in positions.values()) / len(positions) for i in range(3)]
                                          if positions else [0.0, 0.0, 0.0])
    shots: list[dict[str, Any]] = []
    for command in sorted(timeline["commands"], key=lambda item: item["f_start"]):
        device = command.get("device")
        close = device in positions and device in focus
        lift = float(station.get("target_lift_mm", TARGET_LIFT_MM))
        target = [positions[device][0], positions[device][1], positions[device][2] + lift] if close else list(centre)
        shot = {"f_start": command["f_start"], "node": device if close else None, "target_mm": target,
                **(view if close else wide)}
        if shots and shots[-1]["node"] == shot["node"]:
            continue
        if shots and shot["f_start"] <= shots[-1]["f_start"]:
            shots[-1] = shot
        else:
            shots.append(shot)
    if not shots or shots[0]["f_start"] > 0:
        shots.insert(0, {"f_start": 0, "node": None, "target_mm": list(centre), **wide})
    for current, following in zip(shots, shots[1:] + [None]):
        current["f_end"] = following["f_start"] if following else timeline["frame_count"]
    return [shot for shot in shots if shot["f_end"] > shot["f_start"]]


def shards(frame_count: int, count: int) -> list[dict[str, int]]:
    """把 [0, frame_count) 切成 ``count`` 段，供多个引擎进程并行出图。"""
    count = max(1, min(count, frame_count))
    size, extra = divmod(frame_count, count)
    out, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        out.append({"index": index, "f_start": start, "f_end": end})
        start = end
    return out


def contract(timeline: dict[str, Any], *, engine: str, adapter_version: str, scene: dict[str, Any],
             shots: list[dict[str, Any]], resolution: tuple[int, int] = (1280, 720), shard_count: int = 1) -> dict[str, Any]:
    """出片合同：时间线摘要、场景摘要、引擎与适配器版本、镜头、分片。成片回挂时按它校验。"""
    verify(timeline)
    body = {
        "schema": CONTRACT_SCHEMA,
        "timeline_sha256": timeline["timeline_sha256"],
        "source": timeline["source"],
        "fps": timeline["fps"], "frame_count": timeline["frame_count"], "duration_s": timeline["duration_s"],
        "engine": engine, "adapter_version": adapter_version,
        "scene_sha256": sha256(scene), "resolution": list(resolution),
        "shots": shots, "shards": shards(timeline["frame_count"], shard_count),
    }
    body["contract_sha256"] = sha256(body)
    return body


def accept(contract_body: dict[str, Any], *, decoded_frames: int, decoded_fps: float,
           frame_files: dict[int, int] | None = None) -> dict[str, Any]:
    """成片验收：解码帧数、帧率与合同一致；``frame_files`` 给出各分片实际出图数时一并核对。"""
    problems = []
    if decoded_frames != contract_body["frame_count"]:
        problems.append(f"解码帧数 {decoded_frames} ≠ 合同 {contract_body['frame_count']}")
    if abs(decoded_fps - contract_body["fps"]) > 1e-3:
        problems.append(f"帧率 {decoded_fps} ≠ 合同 {contract_body['fps']}")
    for shard in contract_body["shards"]:
        if frame_files is not None and frame_files.get(shard["index"]) != shard["f_end"] - shard["f_start"]:
            problems.append(f"分片 {shard['index']} 出图 {frame_files.get(shard['index'])} 张，应为 "
                            f"{shard['f_end'] - shard['f_start']}")
    return {"accepted": not problems, "problems": problems, "contract_sha256": contract_body["contract_sha256"],
            "timeline_sha256": contract_body["timeline_sha256"]}
