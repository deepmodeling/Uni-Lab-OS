"""Usage-only assets: one manifest, one readiness table, native/runtime files."""
import sys
sys.dont_write_bytecode=True
import json,shutil,uuid,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from core import read,write,sha,digest,inside

SCHEMA='unilab.usage-asset/1'
INDEX_SCHEMA='unilab.usage-library/1'
SUPPORTED_SCHEMAS=(INDEX_SCHEMA,'unilab.usd-library/1')

def manifest_name(root):
    from native_package import MANIFEST
    return MANIFEST if (Path(root)/MANIFEST).exists() else 'asset.json'

def package(root):
    root=Path(root)
    from native_package import MANIFEST
    if (root/MANIFEST).exists():
        from native_package import package as native
        return native(root)
    return read(root/'asset.json') if (root/'asset.json').exists() else read(root/'package.json')

def verify(root,expected=None):
    root=Path(root).resolve()
    from native_package import MANIFEST
    if (root/MANIFEST).exists():
        from native_package import verify as native
        return native(root,expected)
    p=read(root/'asset.json');h=digest(root/'asset.json')
    if p.get('schema')!=SCHEMA or (expected and expected!=h):raise ValueError('Asset manifest identity mismatch')
    files=p['files'];paths=[x['path'] for x in files]
    if len(paths)!=len(set(paths)) or 'asset.json' in paths:raise ValueError('Invalid asset file manifest')
    actual={x.relative_to(root).as_posix() for x in root.rglob('*') if x.is_file() and not x.name.startswith('~$')}
    if actual!=set(paths)|{'asset.json'}:raise ValueError('Asset has missing or extra files')
    for row in files:
        f=inside(root,row['path'])
        if f.stat().st_size!=row['bytes'] or digest(f)!=row['sha256']:raise ValueError('Asset file content mismatch: '+row['path'])
    if not p.get('entrypoints'):raise ValueError('Asset has no usable entrypoint')
    for path in p['entrypoints'].values():
        if path not in paths:raise ValueError('Entrypoint not declared')
    for d in p.get('definitions_data',{}).values():
        for k in ('source','native'):
            if k in d and (d[k]['path'] not in paths or digest(root/d[k]['path'])!=d[k]['sha256']):raise ValueError('CAD correspondence changed')
    table=read(root/'readiness.json');usd=p['entrypoints'].get('usd')
    if usd and table['binding'].get('source_usd_sha256')!=digest(root/usd):raise ValueError('Readiness belongs to another USD')
    return {'status':'passed','package_digest':h,'files':len(files)+1,'asset_id':p['asset_id']}

def finalize(root,p):
    root=Path(root)
    p['schema']=SCHEMA
    p['files']=[{'path':f.relative_to(root).as_posix(),'sha256':digest(f),'bytes':f.stat().st_size}
                for f in sorted(root.rglob('*')) if f.is_file() and f.name!='asset.json']
    write(root/'asset.json',p)
    return verify(root)

def index(store):
    if getattr(store,'_usage_index',None) is None:
        data=json.loads(store.get('library.json'))
        if data.get('schema') not in SUPPORTED_SCHEMAS:raise ValueError('Unknown usage library')
        store._usage_index=data
    return store._usage_index

def retry(operation):
    from publish_incremental import transient_transfer_error
    for attempt in range(5):
        try:return operation()
        except Exception as exc:
            if attempt==4 or not transient_transfer_error(exc):raise
            time.sleep(min(8,2**attempt))

def fetch(store,asset_id,package_digest,cache):
    data=index(store)
    if data['schema']=='unilab.usd-library/1':
        from native_transfer import fetch as native
        return native(store,asset_id,package_digest,cache)
    row=next((x for x in data['packages'] if x['asset_id']==asset_id and x['package_digest']==package_digest),None)
    if not row:raise ValueError('Pinned version is not in the usage library; migrate the project lock explicitly')
    cache=Path(cache).resolve();dest=inside(cache,row['path'])
    if dest.exists():verify(dest,package_digest);return dest,{'status':'passed','action':'cache_hit','path':str(dest)}
    raw=retry(lambda:store.get(row['path']+'/asset.json'))
    if sha(raw)!=package_digest:raise ValueError('Downloaded asset manifest mismatch')
    p=json.loads(raw);temp=cache/'.staging'/uuid.uuid4().hex;temp.mkdir(parents=True)
    def download(item):
        content=retry(lambda:store.get(row['path']+'/'+item['path']))
        if len(content)!=item['bytes'] or sha(content)!=item['sha256']:raise ValueError('Downloaded asset bytes mismatch')
        f=inside(temp,item['path']);f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(content)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(download,p['files']))
    (temp/'asset.json').write_bytes(raw);verify(temp,package_digest);dest.parent.mkdir(parents=True,exist_ok=True);temp.rename(dest)
    return dest,{'status':'passed','action':'downloaded','resumed':0,'downloaded':len(p['files'])+1,'path':str(dest)}

def load_library(lib):
    data=read(lib.root/'library.json')
    if data.get('schema') not in SUPPORTED_SCHEMAS:raise ValueError('Unknown library schema')
    lib.manifest=data;lib.files={f['path']:f for f in data['metadata_files']}
    lib.records={r['catalog_id']:r for r in data['records']};lib.presentation=data.get('presentation',{})
    lib.collections=data.get('collections',[]);lib.source_records={};lib.all_source_records={}

def availability(lib,target,full=False):
    root=inside(lib.root,target['package_path']);f=root/manifest_name(root)
    if not f.exists():return 'remote'
    try:
        if not full and f.name=='com.nvidia.simready.packaging.json':
            # A browse hint, never admission: avoid parsing engineering bodies
            # and resolving every dependency through Windows junction APIs.
            # prepare() still resolves boundaries and verifies all file bytes.
            from native_package import BOM
            raw=f.read_bytes()
            if sha(raw)!=target['package_digest']:return 'damaged'
            m=json.loads(raw);bom=read(root/BOM)
            paths=[x['relative_path'] for x in bom['items']]+['.metadata/'+x['name'] for x in m['metadata']]
            for rel in paths:
                if not isinstance(rel,str) or '\\' in rel or Path(rel).drive or Path(rel).is_absolute() or any(x in ('','.','..') for x in rel.split('/')):return 'damaged'
                if not (root/rel).is_file():return 'remote'
            return 'local'
        if digest(f)!=target['package_digest']:return 'damaged'
        if any(not inside(root,x['path']).is_file() for x in package(root)['files']):return 'remote'
        if full:verify(root,target['package_digest'])
        return 'local'
    except (ValueError,OSError,KeyError):return 'damaged'

def prepare(lib,catalog_id):
    import threading
    r=lib.records[catalog_id];t=r['targets'][0]
    with lib.lock:lock=lib.asset_locks.setdefault(t['package_digest'],threading.Lock())
    with lock:
        state=availability(lib,t,True);dest=inside(lib.root,t['package_path'])
        if state=='damaged':raise ValueError('Local asset changed; refusing to replace it')
        if state=='remote':
            # Metadata-only mirrors can have asset.json/readiness/preview already.
            cached,_=fetch(lib.store,t['asset_id'],t['package_digest'],lib.state/'download-cache')
            for f in cached.rglob('*'):
                if f.is_file():
                    target=dest/f.relative_to(cached);target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists() and digest(target)!=digest(f):raise ValueError('Local partial asset conflict')
                    if not target.exists():shutil.copyfile(f,target)
        verify(dest,t['package_digest'])
        return {'catalog_id':r['catalog_id'],'title':r['title'],'reuse_kind':r['reuse_kind'],'configuration':r['configuration'],
                'target':t,'package_root':str(dest),'downloaded':state=='remote','qualification':r['qualification']}

def sync(lib,full=False):
    raw=retry(lambda:lib.store.get('library.json'));data=json.loads(raw)
    if data.get('schema') not in SUPPORTED_SCHEMAS:raise ValueError('Unknown library schema')
    rows=data['metadata_files'];changed=[]
    def download(row):
        target=inside(lib.root,row['path'])
        if target.exists() and digest(target)==row['sha256']:return
        if target.exists() and (row['path'] not in lib.files or digest(target)!=lib.files[row['path']]['sha256']):raise ValueError('Local modified metadata')
        content=retry(lambda:lib.store.get(row['path']))
        if sha(content)!=row['sha256'] or len(content)!=row['bytes']:raise ValueError('Metadata download mismatch')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content);changed.append(row['path'])
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(download,rows))
    lib.root.mkdir(parents=True,exist_ok=True);tmp=lib.root/'.library-download';tmp.write_bytes(raw);tmp.replace(lib.root/'library.json')
    lib.reload();lib.store._usage_index=data
    if full:
        for r in lib.records:prepare(lib,r)
    return {'status':'passed','records':len(lib.records),'downloaded':len(changed),'mode':'full' if full else 'catalog_and_previews'}

def readiness(lib,catalog_id):
    import copy
    r=lib.records[catalog_id];t=r['targets'][0]
    from native_package import READINESS
    path=t['package_path']+'/'+(READINESS if lib.manifest.get('schema')=='unilab.usd-library/1' else 'readiness.json')
    table=copy.deepcopy(lib.checked_read(path));table['catalog_id']=catalog_id;table['title']=r['title']
    table['binding']['package_digest']=t['package_digest'];table['binding']['spatial_path']=t['entrypoints'].get('spatial',{}).get('path')
    table['evidence']=[]
    return table

def reseal_staging(root):
    """Refresh an unpublished staging index after explicit metadata edits."""
    root=Path(root);data=read(root/'library.json');rows=data['packages'];by_id={x['asset_id']:x for x in rows};metadata=[]
    rows.sort(key=lambda x:read(root/x['path']/'asset.json').get('profile')=='station_layout')
    for row in rows:
        folder=root/row['path'];p=read(folder/'asset.json')
        if p.get('profile')=='station_layout':
            graph=read(folder/p['entrypoints']['os_graph']);lock=read(folder/p['entrypoints']['os_lock'])
            for dep in p['dependencies']:
                q=by_id[dep['asset_id']];body=read(root/q['path']/'asset.json')
                dep.update(package_digest=q['package_digest'],path='library/'+Path(q['path']).name,entrypoints=body['entrypoints'])
                graph['scene']['node_bindings'][dep['node_id']]['path']=dep['path']+'/'+body['entrypoints']['usd']
            write(folder/p['entrypoints']['os_graph'],graph);lock['dependencies']=p['dependencies'];lock['deployment_graph']['sha256']=digest(folder/p['entrypoints']['os_graph']);write(folder/p['entrypoints']['os_lock'],lock)
        row['package_digest']=finalize(folder,p)['package_digest']
        for record in data['records']:
            t=record['targets'][0]
            if t['package_path']!=row['path']:continue
            t['package_digest']=row['package_digest']
            for name,ep in t['entrypoints'].items():
                if name=='engineering' and p.get('definitions_data'):ep['path']=p['definitions_data'][t['definition_id']]['native']['path']
                ep['sha256']=digest(folder/ep['path'])
        for f in folder.iterdir():
            if f.name in ('asset.json','readiness.json') or f.name.startswith('preview'):
                metadata.append({'path':f.relative_to(root).as_posix(),'sha256':digest(f),'bytes':f.stat().st_size})
    data['metadata_files']=metadata;normalize_index_paths(root,data);write(root/'library.json',data);return digest(root/'library.json')

def normalize_index_paths(root,data):
    root=Path(root).resolve()
    def actual(key):
        p=inside(root,key)
        if not p.exists():raise ValueError('Index path missing: '+key)
        return p.relative_to(root).as_posix()
    for row in data['packages']:row['path']=actual(row['path'])
    for row in data['metadata_files']:row['path']=actual(row['path'])
    for r in data['records']:
        for t in r['targets']:t['package_path']=actual(t['package_path'])
        if r.get('supplemental'):r['supplemental']['metadata_path']=actual(r['supplemental']['metadata_path'])
        vendor=Path(r['targets'][0]['package_path']).parts[1]
        if r['manufacturer']['name'].casefold()==vendor.casefold() and r['manufacturer']['name']!=vendor:
            r['manufacturer']['aliases']=sorted(set(r['manufacturer'].get('aliases',[]))|{r['manufacturer']['name'],vendor});r['manufacturer']['name']=vendor
    for display in data['presentation'].values():
        if display.get('thumbnail'):display['thumbnail']['path']=actual(display['thumbnail']['path'])
