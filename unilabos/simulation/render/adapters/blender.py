"""Blender 适配器：分片并行出 PNG → 合并 → Blender 自带编码成 MP4 → 读回帧数做验收。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from unilabos.simulation.render.plan import accept

ADAPTER_VERSION = "blender-usd-joints/1"
JOB_SCRIPT = Path(__file__).with_name("blender_job.py")


def _run(blender: str, job: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    log = path.with_suffix(".log")
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run([blender, "-b", "--factory-startup", "--python", str(JOB_SCRIPT), "--", str(path)],
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"Blender 退出码 {result.returncode}，见 {log}")


def render_video(*, blender: str, contract_body: dict[str, Any], timeline_path: Path, scene_path: Path,
                 out_dir: Path, render_engine: str = "BLENDER_WORKBENCH", workers: int | None = None) -> dict[str, Any]:
    out_dir, scene_path, timeline_path = out_dir.resolve(), scene_path.resolve(), timeline_path.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    common = {"scene": str(scene_path), "timeline": str(timeline_path), "shots": contract_body["shots"],
              "resolution": contract_body["resolution"], "render_engine": render_engine}

    def shard_job(shard: dict[str, int]) -> int:
        directory = out_dir / f"shard_{shard['index']:03d}"
        _run(blender, {**common, "mode": "render", "shard": shard, "out_dir": str(directory)},
             out_dir / f"shard_{shard['index']:03d}.job.json")
        return json.loads((directory / "shard.json").read_text(encoding="utf-8"))["frames"]

    shards = contract_body["shards"]
    with ThreadPoolExecutor(max_workers=workers or len(shards)) as pool:
        written = dict(zip((s["index"] for s in shards), pool.map(shard_job, shards)))
    frames_dir = out_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir()
    for shard in shards:
        for item in sorted((out_dir / f"shard_{shard['index']:03d}").glob("frame_*.png")):
            os.link(item, frames_dir / item.name) if hasattr(os, "link") else shutil.copy2(item, frames_dir / item.name)
    video = out_dir / "video.mp4"
    _run(blender, {"mode": "encode", "frames_dir": str(frames_dir), "fps": contract_body["fps"],
                   "resolution": contract_body["resolution"], "output": str(video)}, out_dir / "encode.job.json")
    probe_result = out_dir / "probe.json"
    _run(blender, {"mode": "probe", "video": str(video), "result": str(probe_result)}, out_dir / "probe.job.json")
    decoded = json.loads(probe_result.read_text(encoding="utf-8"))
    verdict = accept(contract_body, decoded_frames=int(decoded["frames"]), decoded_fps=float(decoded["fps"]),
                     frame_files=written)
    receipt = {"video": str(video), "decoded": decoded, "verdict": verdict, "adapter_version": ADAPTER_VERSION}
    (out_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
