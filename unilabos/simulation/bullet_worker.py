"""Blender 内置 Bullet 的公共 worker；由可信包提供设备模型。"""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from unilabos.simulation.physics import ModelSession
from unilabos.simulation.physics_worker import serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="受信设备包 module:class")
    for name in ("model-config", "socket", "output", "headers", "bridge-cache"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--epoch", type=int, default=0)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None)
    if args.socket.exists():
        raise FileExistsError(args.socket)
    args.output.mkdir(parents=True, exist_ok=False)
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    os.environ["UNILABOS_BULLET_HEADERS"] = str(args.headers.resolve(strict=True))
    os.environ["UNILABOS_BULLET_CACHE"] = str(args.bridge_cache.resolve())
    from unilabos.simulation import bullet_native

    backend = getattr(bullet_native, "BACKEND", "native")
    native_limitations = ["pinned Blender ABI", "GImpact/convex collision; not PhysX SDF",
                          "device-specific engineering calibration; not hardware qualification"]
    capabilities = {
        "backend": {"native": "blender_bullet", "standalone": "bullet_standalone"}.get(
            backend, "blender_bpy_rigidbody"),
        "backend_detail": backend,
        "backend_pin": bullet_native.PIN,
        "blender_commit": bullet_native.PIN if backend in ("native", "bpy") else None,
        "bridge": json.loads(bullet_native.MANIFEST.read_text()),
        "physics_step_seconds": 1 / 240, "substeps": 12,
        "contacts": {"force_unit": "N", "force_basis": "normal_impulse / physics_step_seconds",
                     "position_unit": "m",
                     "source": {"native": "current native Bullet manifolds",
                                "standalone": "standalone Bullet manifold impulses"}.get(
                                    backend, "BVH overlap/proximity with approximate normal impulse")},
        "limitations": native_limitations if backend == "native" else list(getattr(bullet_native, "LIMITATIONS", ())),
    }
    (args.output / "capabilities.json").write_text(json.dumps(capabilities, indent=2))
    module, name = args.model.split(":", 1)
    model = getattr(importlib.import_module(module), name)(**json.loads(args.model_config.read_text()))
    try:
        session = ModelSession(model, args.world_id, args.session_id, epoch=args.epoch)
        def ready():
            (args.output / "ready.json").write_text(json.dumps({k: v for k, v in session.observe().items() if k != "observation"}))
            print("UNILAB_BULLET_READY", flush=True)
        with (args.output / "steps.jsonl").open("x") as trace:
            serve(session, args.socket, trace, ready=ready)
    finally:
        model.close()


if __name__ == "__main__":
    main()
