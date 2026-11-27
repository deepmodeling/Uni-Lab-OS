"""Promote an unsealed native capture to a purpose-specific v3 package."""
import sys
sys.dont_write_bytecode=True
import argparse
from pathlib import Path
from core import *

def finalize(root,profile,kind):
    root=Path(root)
    if (root/'bundle.sha256.json').exists():raise ValueError('不能修改已封存包')
    p=read(root/'package.json')
    if 'blender' in p['entrypoints'] or 'usd' in p['entrypoints']:raise ValueError('只接收原生采集阶段')
    defs=read(root/p['definitions'])['definitions']
    p.update(schema='unilab.engineering-asset/3',profile=profile,reuse_kind=kind,exports=[])
    p['representations']=[{'kind':'cad','path':p['entrypoints']['cad'],'sha256':digest(root/p['entrypoints']['cad'])}]
    p['capabilities'].pop('blender_reopen',None)
    if profile=='cad':p['capabilities'].pop('usd_reopen',None)
    proofpath=root/p['capabilities']['native_reopen']['evidence'];proof=read(proofpath)
    files=[p['definitions'],p['assembly']]+[d['native']['path'] for d in defs.values()]
    proof['input_files']={path:digest(root/path) for path in files};proof['new_engine_execution']=True
    write(proofpath,proof);write(root/'package.json',p)
    if profile=='cad':return seal(root)
    return {'status':'awaiting_usd','profile':profile}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--package',required=True);ap.add_argument('--profile',choices=['cad','spatial'],required=True);ap.add_argument('--kind',required=True);a=ap.parse_args()
    print(finalize(a.package,a.profile,a.kind))
