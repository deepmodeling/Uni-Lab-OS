"""Single-writer readable-mirror publication, preserving every existing sealed package."""
import sys
sys.dont_write_bytecode=True
import argparse,shutil,json,threading,time,os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from core import *
from readable_library import verify_local

def transient_transfer_error(exc):
    status=getattr(exc,'status',None)
    if type(exc).__module__.startswith('oss2'):
        wrapped=getattr(exc,'exception',None)
        if wrapped is not None and wrapped is not exc:
            return transient_transfer_error(wrapped)
        return status is None or status==429 or status>=500
    # Streaming response reads may escape the OSS wrapper as requests errors.
    # Integrity mismatches and authorization errors must never be retried here.
    return any(c.__module__.startswith(('requests.', 'urllib3.', 'http.client'))
               and c.__name__ in ('ConnectionError','Timeout','ChunkedEncodingError',
                                 'ProtocolError','IncompleteRead')
               for c in type(exc).__mro__)

def publish(store,staging,local,baseline,output,workers=6,local_materialization='copy'):
    if not 1<=workers<=128:raise ValueError('Publication workers must be between 1 and 128')
    if local_materialization not in ('copy','link-immutable'):raise ValueError('Unsupported local materialization')
    staging=Path(staging).resolve();local=Path(local).resolve();baseline=Path(baseline)
    verify_local(staging);old_bytes=baseline.read_bytes();new_bytes=(staging/'library.manifest.json').read_bytes()
    if (local/'library.manifest.json').read_bytes() not in (old_bytes,new_bytes) or store.get('library.manifest.json') not in (old_bytes,new_bytes):raise ValueError('本地/远端基线改变，不能发布')
    old=read(baseline);new=read(staging/'library.manifest.json');before={f['path']:f for f in old['files']};after={f['path']:f for f in new['files']}
    if not set(before)<=set(after):raise ValueError('增量发布不得删除现有文件')
    actual={f['path']:f for f in file_entries(local) if f['path']!='library.manifest.json'}
    if not set(actual)<=set(after) or not set(before)<=set(actual):raise ValueError('本地有未登记文件或缺失旧文件')
    for key,f in actual.items():
        if f not in (before.get(key),after.get(key)):raise ValueError('本地文件存在外部修改 '+key)
    # Derive immutable boundaries from the trusted baseline, never the edited registry.
    # A sealed package can retain another seal as test/source evidence. Only
    # outermost baseline seals are release boundaries; their entire subtrees,
    # including nested evidence seals, remain immutable. Never derive this from
    # the staged registry, which could otherwise remove a protection boundary.
    seal_candidates=sorted((key[:-len('bundle.sha256.json')] for key in before
                            if key.endswith('/bundle.sha256.json')),key=lambda p:(len(p),p))
    sealed=[]
    for prefix in seal_candidates:
        if not any(prefix.startswith(parent) for parent in sealed):sealed.append(prefix)
    releases=[r for r in read(staging/'索引/发布位置.json')['releases'] if r['path']+'/' in sealed]
    if {r['path']+'/' for r in releases}!=set(sealed):raise ValueError('禁止移除旧发布包登记')
    changed=[k for k in after if k not in before or before[k]!=after[k]]
    for key in changed:
        if key in before and any(key.startswith(prefix) for prefix in sealed):raise ValueError('禁止修改已封存发布包 '+key)
        if key in before and not (key.startswith(('索引/','按厂家/','专题集合/')) or key.endswith('/README.md') or key.endswith('/asset.ref.json')):raise ValueError('现有非目录文件不可覆盖 '+key)
    fresh=[k for k in changed if k not in before]
    progress=Path(str(output)+'.progress.json');progress_lock=threading.Lock();completed=0;started=time.monotonic();retry_events=[]
    def checkpoint(phase):
        write(progress,{'phase':phase,'immutable_completed':completed,'immutable_total':len(fresh),'workers':workers,'transient_retries':len(retry_events),'elapsed_seconds':round(time.monotonic()-started,2)})
    def transfer(key,operation):
        for attempt in range(5):
            try:return operation()
            except Exception as exc:
                if not transient_transfer_error(exc) or attempt==4:raise
                with progress_lock:
                    retry_events.append({'key':key,'attempt':attempt+1,'error_type':type(exc).__name__,
                                         'wrapped_error_type':type(getattr(exc,'exception',None)).__name__,
                                         'status':getattr(exc,'status',None)})
                    write(Path(str(output)+'.retries.json'),retry_events)
                time.sleep(min(8,2**attempt))
    # Bound file payload memory independently from request concurrency. This
    # permits parallel small-object transfers without loading many large CADs.
    memory_condition=threading.Condition();memory_available=512*1024**2
    def immutable(key):
        nonlocal completed
        nonlocal memory_available
        weight=min(after[key]['bytes'],512*1024**2)
        with memory_condition:
            memory_condition.wait_for(lambda:memory_available>=weight)
            memory_available-=weight
        try:
            data=(staging/key).read_bytes()
            result=transfer(key,lambda:store.put_immutable(key,data))
        finally:
            with memory_condition:
                memory_available+=weight;memory_condition.notify_all()
        with progress_lock:
            completed+=1
            if completed%100==0 or completed==len(fresh):checkpoint('immutable_upload_and_readback')
        return result
    checkpoint('immutable_upload_and_readback')
    with ThreadPoolExecutor(max_workers=workers) as pool:created=list(pool.map(immutable,fresh))
    checkpoint('directory_metadata_and_manifest')
    replacements=[]
    for key in [k for k in changed if k in before]:
        current=sha(transfer(key,lambda:store.get(key)));data=(staging/key).read_bytes()
        if current not in (before[key]['sha256'],sha(data)):raise ValueError('远端元数据并发变动 '+key)
        if current!=sha(data):transfer(key,lambda:store.bucket.put_object(store.prefix+key,data,headers={'x-oss-object-acl':'private','x-oss-meta-sha256':sha(data)}))
        if transfer(key,lambda:store.get(key))!=data:raise ValueError('元数据读回不符 '+key)
        replacements.append(key)
    if transfer('library.manifest.json',lambda:store.get('library.manifest.json')) not in (old_bytes,new_bytes):raise ValueError('提交前远端基线变化')
    content=(staging/'library.manifest.json').read_bytes();transfer('library.manifest.json',lambda:store.bucket.put_object(store.prefix+'library.manifest.json',content,headers={'x-oss-object-acl':'private','x-oss-meta-sha256':sha(content)}))
    if transfer('library.manifest.json',lambda:store.get('library.manifest.json'))!=content:raise ValueError('新清单读回失败')
    linked=0;copied=0
    for key in changed:
        dest=inside(local,key);dest.parent.mkdir(parents=True,exist_ok=True)
        if actual.get(key)==after[key]:continue
        mutable=key.startswith(('索引/','按厂家/','专题集合/')) or key.endswith(('/README.md','/asset.ref.json')) or key=='README.md'
        if local_materialization=='link-immutable' and key not in before and not mutable and not dest.exists():
            # Only new immutable bytes share storage with the verified staging
            # mirror. Original vendor inputs, old releases and mutable indexes
            # are never relinked. Cross-volume/unsupported filesystems copy.
            try:os.link(staging/key,dest);linked+=1;continue
            except OSError:pass
        shutil.copyfile(staging/key,dest);copied+=1
    (local/'library.manifest.json').write_bytes(content);checked=verify_local(local)
    expected=set(after)|{'library.manifest.json'}
    if set(transfer('remote-key-set',lambda:store.list('')))!=expected:raise ValueError('OSS 文件集合与正式镜像不符')
    receipt={'status':'passed','local':checked,'created':created,'replaced_catalog_metadata':replacements,'existing_sealed_packages_preserved':len(releases),'baseline_manifest_sha256':sha(old_bytes),'published_manifest_sha256':sha(content),'old_objects_deleted':0,
             'transient_retries':retry_events,'local_materialization':{'mode':local_materialization,'new_immutable_hardlinks':linked,'files_copied':copied,'upstream_vendor_paths_linked':0}}
    write(output,receipt);checkpoint('passed');return {'status':'passed','files':checked['files'],'new':len(created),'metadata_updated':len(replacements)}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--staging',required=True);ap.add_argument('--local',required=True);ap.add_argument('--baseline',required=True);ap.add_argument('--config',required=True);ap.add_argument('--output',required=True);ap.add_argument('--workers',type=int,default=6);ap.add_argument('--local-materialization',choices=['copy','link-immutable'],default='copy');a=ap.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'asset-browser'))
    from credentials import load_credentials
    load_credentials()
    from oss_store import Store
    try:print(json.dumps(publish(Store(a.config),a.staging,a.local,a.baseline,a.output,a.workers,a.local_materialization)))
    except Exception as exc:
        r={'status':'failed','error_type':type(exc).__name__}
        if getattr(exc,'exception',None) is not None:
            r['wrapped_error_type']=type(exc.exception).__name__
            r['http_status']=getattr(exc,'status',None)
        if not type(exc).__module__.startswith('oss2'):r['reason']=str(exc)
        write(a.output,r);print(json.dumps(r));sys.exit(1)
