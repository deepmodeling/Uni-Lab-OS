"""无求解器 Blender 模型入口，复用 OS 确认步和 RPC，不加载 Bullet。"""
import argparse
import importlib
import json
import sys
from pathlib import Path

from unilabos.simulation.physics import ModelSession
from unilabos.simulation.physics_worker import serve


def main():
    """加载受信领域模型，声明运动学能力并保存每个确认步。"""
    parser=argparse.ArgumentParser()
    for key in ('model','world-id','session-id'):
        parser.add_argument('--'+key,required=True)
    for key in ('model-config','socket','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    if args.socket.exists():raise FileExistsError(args.socket)
    args.output.mkdir(parents=True,exist_ok=False)
    module,name=args.model.split(':',1)
    model=getattr(importlib.import_module(module),name)(**json.loads(args.model_config.read_text()))
    capabilities=dict(backend='blender_kinematic',solver=None,contacts=False,rigid_body_world=False,
        step_seconds=.05,interpolation_substeps=12,physical_qualified=False)
    (args.output/'capabilities.json').write_text(json.dumps(capabilities,indent=2))
    try:
        session=ModelSession(model,args.world_id,args.session_id)
        def ready():
            """保存会话身份及零步就绪记录。"""
            (args.output/'ready.json').write_text(json.dumps(session.observe()))
        with (args.output/'steps.jsonl').open('x') as trace:
            serve(session,args.socket,trace,ready=ready)
    finally:
        model.close()


if __name__=='__main__':main()
