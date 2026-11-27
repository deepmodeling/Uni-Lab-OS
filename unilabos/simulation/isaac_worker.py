"""无 GPU 的可选 Isaac worker；通过有代际的 RPC 明确推进一次确认扫描。"""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stage", type=Path)
    source.add_argument("--model", help="受信设备包的 module:class")
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cooking-cache", type=Path)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--epoch", type=int, default=0)
    parser.add_argument("--observe", action="append", default=[], help="显式 USD prim 路径")
    args = parser.parse_args()
    if args.socket.exists():
        raise FileExistsError(args.socket)
    args.output.mkdir(parents=True, exist_ok=False)
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    import carb
    import omni.kit.app

    framework = carb.get_framework()
    framework.load_plugins(
        loaded_file_wildcards=["omni.kit.app.plugin"], search_paths=[os.environ["CARB_APP_PATH"] + "/kernel/plugins"]
    )
    app = omni.kit.app.get_app()
    world = None
    model = None
    try:
        app.startup(
            "unilab-twin-physics",
            os.environ["CARB_APP_PATH"],
            [
                __file__,
                str(Path(__file__).with_name("cpu_physics.kit")),
                "--no-window",
                "--portable",
                "--portable-root",
                str(args.output / "kit"),
                "--/app/renderer/enabled=false",
                "--/renderer/enabled=false",
                "--/app/settings/persistent=false",
                "--/app/installSignalHandlers=0",
                "--/physics/cudaDevice=-1",
                "--/physics/cooking/ujitsoCollisionCooking=true",
                "--/physics/cooking/ujitsoRemoteCacheEnabled=false",
                "--/persistent/physics/cooking/ujitsoCookingMaxProcessCount=1",
                "--/UJITSO/datastore/allowHubDataStore=false",
                "--/UJITSO/datastore/allowGRPCDataStore=false",
                "--/UJITSO/datastore/localCachePath=" + str(args.cooking_cache or args.output / "sdf-cache"),
            ],
        )
        manager = app.get_extension_manager()
        if any(
            manager.get_enabled_extension_id(name)
            for name in ("omni.hydra.rtx", "omni.kit.renderer.core", "omni.kit.viewport.window")
        ):
            raise RuntimeError("CPU worker 意外启用渲染器")
        from types import SimpleNamespace

        from pxr import Usd, UsdGeom

        from .isaac import IsaacPhysicsWorld
        from .physics import ModelSession
        from .physics_worker import serve

        if args.model:
            import importlib

            module, name = args.model.split(":", 1)
            config = json.loads(args.model_config.read_text()) if args.model_config else {}
            model = getattr(importlib.import_module(module), name)(**config)
            stage = model.stage
            observe = lambda: model.state
        else:
            if not args.observe:
                raise ValueError("观测绑定缺失")
            stage = Usd.Stage.Open(str(args.stage.resolve(strict=True)))
            prims = {path: stage.GetPrimAtPath(path) for path in args.observe}
            if not all(prims.values()):
                raise ValueError("观测绑定缺失")

            def observe():
                # USD 行向量矩阵转公共合同列向量矩阵。
                return {path: [list(row) for row in UsdGeom.Xformable(prim)
                        .ComputeLocalToWorldTransform(Usd.TimeCode.Default()).GetTranspose()]
                        for path, prim in prims.items()}

        world = IsaacPhysicsWorld(stage, args.world_id, args.session_id, epoch=args.epoch)
        if model is None:
            # 裸 USD 观察入口也使用同一会话合同，不支持设备控制。
            def unsupported(name, parameters):
                raise ValueError("unsupported_operation")
            hooks = SimpleNamespace(state=observe(), command=unsupported,
                prepare_scan=lambda: None, before_substep=lambda index: None,
                after_substep=lambda index: None, finish_scan=observe)
        else:
            hooks = model
        def advance(index, duration, at):
            world.sim.simulate(duration, at)
            world.sim.fetch_results()
        session = ModelSession(hooks, args.world_id, args.session_id, epoch=args.epoch, advance_substep=advance)
        if model is None:
            # 裸场景的 observe 必须读当前 USD，不能停留在初态。
            def finish():
                hooks.state = observe()
                return hooks.state
            hooks.finish_scan = finish
        with (args.output / "steps.jsonl").open("x") as trace:
            serve(session, args.socket, trace, ready=lambda: print("UNILAB_ISAAC_READY", flush=True))
    finally:
        if model is not None:
            model.close()
        if world is not None:
            world.close()
        app.shutdown()
        framework.unload_all_plugins()


if __name__ == "__main__":
    main()
