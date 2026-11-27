"""出片命令行。

    # 1) 冻结时间线：回放源（任务 MCAP + 状态 JSONL，os.pathsep 分隔）→ timeline.json
    python -m unilabos.simulation.render freeze --sources a.mcap:a.state.jsonl --out timeline.json [--fps 24 --policy focus]
    # 2) 场景说明：统一图 + 运行中的 OS（模型与关节）→ scene.json
    python -m unilabos.simulation.render scene --backend URL/api/v1 --graph unified.json --package pylabrobot-unilab=/path --out scene.json
    # 3) 合同 + 出片 + 验收（Blender）
    python -m unilabos.simulation.render blender --timeline timeline.json --scene scene.json --blender /path/blender --out DIR [--shards 4]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from unilabos.simulation.render.plan import camera_shots, contract
from unilabos.simulation.render.timeline import freeze


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m unilabos.simulation.render")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--sources", required=True)
    f.add_argument("--out", required=True, type=Path)
    f.add_argument("--fps", type=int, default=24)
    f.add_argument("--policy", choices=["log", "focus"], default="focus")
    f.add_argument("--idle-cap", type=float, default=2.0)
    f.add_argument("--nodes", help="只保留这些节点（逗号分隔）")
    f.add_argument("--until", type=float, help="只取源时间 ≤ 这个秒数的部分")
    s = sub.add_parser("scene")
    s.add_argument("--backend", required=True)
    s.add_argument("--graph", required=True, type=Path)
    s.add_argument("--package", action="append", default=[], help="包名=本机目录")
    s.add_argument("--nodes")
    s.add_argument("--out", required=True, type=Path)
    b = sub.add_parser("blender")
    b.add_argument("--timeline", required=True, type=Path)
    b.add_argument("--scene", required=True, type=Path)
    b.add_argument("--blender", required=True)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--shards", type=int, default=1)
    b.add_argument("--resolution", default="1280x720")
    b.add_argument("--engine", default="BLENDER_WORKBENCH")
    args = parser.parse_args(argv)

    if args.command == "freeze":
        from unilabos.simulation.replay_feed import load_sources
        header, rows = load_sources(args.sources)
        timeline = freeze(header, rows, fps=args.fps, policy=args.policy, idle_cap_s=args.idle_cap,
                          nodes=set(args.nodes.split(",")) if args.nodes else None, until_s=args.until)
        _write(args.out, timeline)
        print(f"{args.out}: {timeline['frame_count']} 帧 / {timeline['duration_s']} s，{len(timeline['commands'])} 条作业，"
              f"timeline_sha256={timeline['timeline_sha256'][:12]}")
    elif args.command == "scene":
        from unilabos.simulation.render.scene import scene_from_backend
        roots = dict(item.split("=", 1) for item in args.package)
        spec = scene_from_backend(args.backend, args.graph, package_roots=roots,
                                  nodes=set(args.nodes.split(",")) if args.nodes else None)
        _write(args.out, spec)
        print(f"{args.out}: {len(spec['nodes'])} 个节点")
    else:
        from unilabos.simulation.render.adapters.blender import ADAPTER_VERSION, render_video
        timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
        spec = json.loads(args.scene.read_text(encoding="utf-8"))
        positions = {item["node"]: [row[3] for row in item["world_mm"][:3]] for item in spec["nodes"]}
        width, height = (int(v) for v in args.resolution.split("x"))
        body = contract(timeline, engine="blender", adapter_version=ADAPTER_VERSION, scene=spec,
                        shots=camera_shots(timeline, positions), resolution=(width, height), shard_count=args.shards)
        args.out.mkdir(parents=True, exist_ok=True)
        _write(args.out / "contract.json", body)
        receipt = render_video(blender=args.blender, contract_body=body, timeline_path=args.timeline,
                               scene_path=args.scene, out_dir=args.out, render_engine=args.engine)
        print(json.dumps(receipt["verdict"], ensure_ascii=False))
        return 0 if receipt["verdict"]["accepted"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
