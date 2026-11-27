"""Pinned OSS cache and OS-native deployment materialization. Credentials: environment only."""
import sys
sys.dont_write_bytecode=True
import argparse,json,re,shutil,uuid,os
from pathlib import Path
from core import *

def release_key(asset_id,package_digest):
    if not re.fullmatch(r'[0-9a-f-]{36}',asset_id) or not re.fullmatch(r'[0-9a-f]{64}',package_digest):raise ValueError('非法资产/摘要')
    return f'releases/{asset_id}/{package_digest}/'

def fetch(store,asset_id,package_digest,cache):
    if getattr(store,'config',{}).get('layout') in ('minimal/1','nvidia-usd/1'):
        from minimal_format import fetch as fetch_usage
        return fetch_usage(store,asset_id,package_digest,cache)
    base=release_key(asset_id,package_digest);cache=Path(cache).resolve();target=cache/asset_id/package_digest
    if target.exists():
        try:verify(target,package_digest)
        except Exception as exc:
            # Keep corrupt evidence; never silently trust or overwrite.
            raise ValueError('缓存损坏；保留原目录，请选择新缓存根重新获取') from exc
        return target,{'status':'passed','action':'cache_hit','path':str(target)}
    entry=json.loads(store.get(f'index/releases/{asset_id}/{package_digest}.json'))
    if entry['package_digest']!=package_digest or entry['asset_id']!=asset_id or entry['seal_key']!=base+'bundle.sha256.json':raise ValueError('发布登记不匹配')
    seal_bytes=store.get(entry['seal_key'])
    if sha(seal_bytes)!=package_digest:raise ValueError('服务端封存摘要不符')
    manifest=json.loads(seal_bytes);staging=cache/'.staging'/asset_id/package_digest;staging.mkdir(parents=True,exist_ok=True)
    paths=[x['path'] for x in manifest['files']]
    if len(paths)!=len(set(paths)) or 'bundle.sha256.json' in paths:raise ValueError('封存清单非法')
    downloaded=0;resumed=0
    for item in manifest['files']:
        dest=inside(staging,item['path']);dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists() and dest.stat().st_size==item['bytes'] and digest(dest)==item['sha256']:resumed+=1;continue
        data=store.get(base+item['path'])
        if len(data)!=item['bytes'] or sha(data)!=item['sha256']:raise ValueError('下载文件校验失败 '+item['path'])
        temp=dest.with_name(dest.name+'.download-'+uuid.uuid4().hex);temp.write_bytes(data);temp.replace(dest);downloaded+=1
    (staging/'bundle.sha256.json').write_bytes(seal_bytes);verify(staging,package_digest)
    target.parent.mkdir(parents=True,exist_ok=True);staging.rename(target)
    for path in target.rglob('*'):
        if path.is_file():path.chmod(0o444)
    return target,{'status':'passed','action':'downloaded','downloaded':downloaded,'resumed':resumed,'path':str(target)}


def usd_default_prim(package):
    """绑定真实 USD 根；资产根不要求叫 /Asset。"""
    from pxr import Usd
    package=Path(package);body=load_package(package)
    stage=Usd.Stage.Open(str(inside(package,body['entrypoints']['usd'])),load=Usd.Stage.LoadNone)
    if not stage or not stage.GetDefaultPrim():raise ValueError('资产缺少 USD default prim')
    return str(stage.GetDefaultPrim().GetPath())


def collision_binding(package,relative):
    """将已核验资产的现有 collision entrypoints 锁入 OS 节点绑定。"""
    package=Path(package);body=load_package(package)
    profiles=body.get('collision_profiles')
    if not profiles:return None
    value={}
    for mode in ('exact','outer'):
        path=body['entrypoints']['collision_'+mode]
        if profiles[mode]!=path:raise ValueError('碰撞入口不一致')
        file=inside(package,path)
        value[mode]={'path':relative+'/'+path,'sha256':digest(file)}
    return value

def materialize(packages,asset_root,project_lock,graph_path,existing_lock=None):
    asset_root=Path(asset_root).resolve();lockpath=Path(project_lock).resolve();graphpath=Path(graph_path).resolve()
    if lockpath.exists() or graphpath.exists():raise ValueError('项目锁/部署图已存在；先生成新修订')
    dependencies=[];nodes=[];bindings={};occurrences={}
    for i,package in enumerate(packages):
        package=Path(package).resolve();result=verify(package);p=load_package(package);pd=result['package_digest']
        if p.get('profile') in ('knowledge','station_layout') or 'usd' not in p['entrypoints']:
            raise ValueError('此入口只把空间资产加入验收图；CAD/知识直接打开，工站使用 fetch-project')
        rel=('library/'+package.parent.name+'/'+package.name) if (package/'com.nvidia.simready.packaging.json').exists() else f'library/{p["asset_id"]}/{pd}';dest=asset_root/rel
        if dest.exists():verify(dest,pd)
        else:
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(package,dest)
            for f in dest.rglob('*'):
                if f.is_file():f.chmod(0o644)
        verify(dest,pd)
        base='asset_'+p['asset_id'].replace('-','_');occurrences[base]=occurrences.get(base,0)+1
        node=base if occurrences[base]==1 else base+'__'+str(occurrences[base]);entry=rel+'/'+p['entrypoints']['usd']
        nodes.append({'id':node,'class':'postproc_station.library_asset','type':'device','parent':None,'children':[],'position':{'x':i*500.,'y':0.,'z':0.}})
        bindings[node]={'kind':'model','family':p['asset_id'],'path':entry,'prim_path':usd_default_prim(dest),'sha256':digest(asset_root/entry),'collision_role':'static_world'}
        collision=collision_binding(dest,rel)
        if collision:bindings[node]['collision_profiles']=collision
        dependencies.append({'asset_id':p['asset_id'],'definition_revision':p['definition_revision'],'configuration':p['configuration'],'package_digest':pd,'path':rel,'node_id':node,'entrypoints':p['entrypoints']})
    graph={'nodes':nodes,'scene':{'root_path':'/World','node_bindings':bindings}}
    write(graphpath,graph)
    lock={'schema':'unilab.asset-dependencies/1','path_basis':'asset_root','document_path_basis':'relative to this lock file','dependencies':dependencies,'deployment_graph':{'path':Path(os.path.relpath(graphpath,lockpath.parent)).as_posix(),'sha256':digest(graphpath)},'purpose':'library integration acceptance; not production station placement','physics_qualified':False}
    if existing_lock:lock['domain_asset_index']={'path':Path(os.path.relpath(Path(existing_lock).resolve(),lockpath.parent)).as_posix(),'sha256':digest(existing_lock)}
    write(lockpath,lock);return lock

def verify_project(asset_root,lockpath,graphpath):
    asset_root=Path(asset_root).resolve();lock=read(lockpath);graph=read(graphpath)
    lock_root=Path(lockpath).resolve().parent
    if (lock_root/lock['deployment_graph']['path']).resolve()!=Path(graphpath).resolve():raise ValueError('使用了不同的部署图路径')
    if digest(graphpath)!=lock['deployment_graph']['sha256']:raise ValueError('部署图与项目锁不一致')
    if lock.get('domain_asset_index') and digest(lock_root/lock['domain_asset_index']['path'])!=lock['domain_asset_index']['sha256']:raise ValueError('领域包原资产索引变化')
    expected=set()
    for d in lock['dependencies']:
        root=inside(asset_root,d['path']);verify(root,d['package_digest']);p=load_package(root)
        if any(d[k]!=p[k] for k in ('asset_id','definition_revision','configuration')):raise ValueError('项目定义修订漂移')
        b=graph['scene']['node_bindings'][d['node_id']];expected.add(d['node_id'])
        if b['prim_path']!=usd_default_prim(root):raise ValueError('场景 USD 根绑定漂移')
        collision=collision_binding(root,d['path'])
        if b.get('collision_profiles')!=collision:raise ValueError('场景碰撞绑定漂移')
        if b['path']!=d['path']+'/'+p['entrypoints']['usd'] or b['sha256']!=digest(asset_root/b['path']):raise ValueError('场景资产绑定漂移')
    if set(graph['scene']['node_bindings'])!=expected:raise ValueError('图资产覆盖不符')
    return {'status':'passed','dependencies':len(expected),'graph_sha256':digest(graphpath)}

def fetch_project(store,asset_id,package_digest,cache,destination):
    """Materialize a fixed station-layout package and its existing OS lock unchanged."""
    package,_=fetch(store,asset_id,package_digest,cache);p=load_package(package)
    if p.get('profile')!='station_layout':raise ValueError('不是 OS 布局包')
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('项目输出已存在')
    staging=destination.with_name(destination.name+'.staging-'+uuid.uuid4().hex)
    shutil.copytree(package,staging)
    for f in staging.rglob('*'):
        if f.is_file():f.chmod(0o644)
    assets=inside(staging,p['asset_root'])
    for dep in p['dependencies']:
        cached,_=fetch(store,dep['asset_id'],dep['package_digest'],cache)
        target=inside(assets,dep['path']);target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(cached,target)
        for f in target.rglob('*'):
            if f.is_file():f.chmod(0o644)
    result=verify_project(assets,inside(staging,p['entrypoints']['os_lock']),inside(staging,p['entrypoints']['os_graph']))
    # Original seal describes the transport package. Expanded project deliberately
    # adds dependencies; preserve seal as origin evidence, not a seal for expanded files.
    staging.rename(destination)
    return {'status':'passed','destination':str(destination),'origin_package_digest':package_digest,'project':result,'expanded_project':True}

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='action',required=True)
    f=sub.add_parser('fetch');f.add_argument('--config',required=True);f.add_argument('--asset',required=True);f.add_argument('--digest',required=True);f.add_argument('--cache',required=True);f.add_argument('--output',required=True)
    m=sub.add_parser('materialize');m.add_argument('--packages',nargs='+',required=True);m.add_argument('--asset-root',required=True);m.add_argument('--lock',required=True);m.add_argument('--graph',required=True);m.add_argument('--domain-lock')
    v=sub.add_parser('verify-project');v.add_argument('--asset-root',required=True);v.add_argument('--lock',required=True);v.add_argument('--graph',required=True)
    f=sub.add_parser('fetch-project');f.add_argument('--config',required=True);f.add_argument('--asset',required=True);f.add_argument('--digest',required=True);f.add_argument('--cache',required=True);f.add_argument('--destination',required=True);f.add_argument('--output',required=True)
    a=ap.parse_args()
    if a.action in ('fetch','fetch-project'):
        from oss_store import Store
        try:
            if a.action=='fetch':_,report=fetch(Store(a.config),a.asset,a.digest,a.cache)
            else:report=fetch_project(Store(a.config),a.asset,a.digest,a.cache,a.destination)
            write(a.output,report)
        except Exception as exc:
            import oss2
            report={'status':'failed','error_type':type(exc).__name__}
            if not isinstance(exc,oss2.exceptions.OssError):report['reason']=str(exc)
            write(a.output,report);print(json.dumps(report));sys.exit(1)
    elif a.action=='materialize':report=materialize(a.packages,a.asset_root,a.lock,a.graph,a.domain_lock)
    else:report=verify_project(a.asset_root,a.lock,a.graph)
    print(json.dumps(report,ensure_ascii=True))

if __name__=='__main__':main()
