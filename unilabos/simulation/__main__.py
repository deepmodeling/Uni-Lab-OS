"""可选显示进程的正式命令入口：python -m unilabos.simulation。"""

import argparse
import json
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="UniLab OS 仿真工具：只读孪生观察端、资产行为试跑")
    sub = parser.add_subparsers(dest="command", required=True)
    display = sub.add_parser("display")
    display.add_argument("--url", required=True, help="OS /api/v1/device-state/observations 入口 URL")
    display.add_argument("--bindings", type=Path, required=True, help="原生模板 UUID 到 DisplayAsset 的 JSON")
    display.add_argument("--assets-root", type=Path, required=True)
    display.add_argument("--blender", required=True)
    display.add_argument("--output", type=Path, required=True)
    display.add_argument("--interval", type=float, default=0.05)
    display.add_argument("--frames", type=int, default=0, help="0 表示持续运行")
    attempt = sub.add_parser("try", help="单独试跑资产包里的机构动作（不需要驱动、设备图或工站）")
    attempt.add_argument("--package", type=Path, required=True, help="资产包目录（含 simready_usd/unilab.json）")
    attempt.add_argument("--behavior", help="开发中的行为类：文件.py:类名；缺省取资产包声明的行为类")
    attempt.add_argument("--action", action="append", help="名字 或 名字={JSON 参数}；可重复，按顺序执行；不给则只列出")
    attempt.add_argument("--material", action="append",
                         help="动作里物料参数的几何属性：参数名={JSON}，例如 plate={\"size_z\": 0.0144}；可重复")
    attempt.add_argument("--resources", type=Path,
                         help="资源图（OS 图 JSON）：行为类用 materials.point(资源名) 时沿它算台面坐标系里的点")
    attempt.add_argument("--scene", type=Path,
                         help="场景说明 JSON：{scene: OS 场景图, device: 本设备节点, frame_site: 台面基准库位, packages: {资产 ID: 包目录}}；"
                              "资源名写成 节点/库位，点位 = 节点位姿 × 耗材资产自带的孔位、吸头位")
    attempt.add_argument("--rate", type=float, default=20.0, help="采样率（Hz）")
    attempt.add_argument("--output", type=Path, help="完整报告（含逐帧轨迹）写到这个 JSON 文件")
    args = parser.parse_args()
    if args.command == "try":
        from .mechanism_try import main as try_main

        raise SystemExit(try_main(args))
    from .display import BlenderWorker, DisplayAsset, SceneMirror, StateClient

    if args.interval <= 0 or args.frames < 0:
        parser.error("interval 须为正数，frames 不得为负数")
    args.output.mkdir(parents=True, exist_ok=False)
    assets = {key: DisplayAsset.model_validate(value) for key, value in json.loads(args.bindings.read_text()).items()}
    mirror = SceneMirror(assets, args.assets_root)
    client = StateClient(args.url)
    worker = BlenderWorker(args.blender, args.output / "blender.log")
    try:
        with (args.output / "readback.jsonl").open("w") as stream:
            count = 0
            while args.frames == 0 or count < args.frames:
                started = time.monotonic()
                result = worker.call("apply", frame=mirror.frame(client.snapshot()))
                stream.write(json.dumps(result) + "\n")
                stream.flush()
                count += 1
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    finally:
        worker.close()


if __name__ == "__main__":
    main()
