"""Read standard package metadata first; materialize verified content on demand."""
import json,uuid,shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from core import inside,sha
import native_package as native

def fetch(store,asset_id,package_digest,cache):
    from minimal_format import index,retry
    row=next((x for x in index(store)['packages'] if x['asset_id']==asset_id and x['package_digest']==package_digest),None)
    if not row:raise ValueError('Pinned package absent; explicit project migration required')
    cache=Path(cache).resolve();dest=inside(cache,row['path'])
    if dest.exists():native.verify(dest,package_digest);return dest,{'status':'passed','action':'cache_hit','path':str(dest)}
    raw=retry(lambda:store.get(row['path']+'/'+native.MANIFEST))
    if sha(raw)!=package_digest:raise ValueError('Package definition digest mismatch')
    m=json.loads(raw)
    if m['package_id']!=asset_id:raise ValueError('Downloaded package identity differs')
    temp=cache/'.staging'/uuid.uuid4().hex;temp.mkdir(parents=True)
    try:
        (temp/native.MANIFEST).write_bytes(raw)
        for meta in m['metadata']:
            rel='.metadata/'+meta['name'];f=inside(temp,rel);data=retry(lambda:store.get(row['path']+'/'+rel))
            if sha(data)!=meta['hash']['sha256']:raise ValueError('Metadata download mismatch')
            f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(data)
        b=native.read(temp/native.BOM)
        if native.content_hash(b['items'])!=m['content_hash']['sha256'] or native.package_hash(m)!=m['package_hash']['sha256']:raise ValueError('Package aggregate mismatch')
        paths=[x['relative_path'] for x in b['items']]
        if len(paths)!=len(set(x.casefold() for x in paths)) or any(not x.startswith('simready_usd/') for x in paths):raise ValueError('Invalid/duplicate delivery path')
        # Resolve and create parents before concurrent writes: on Windows a
        # partially-created directory can resolve differently during creation.
        targets={rel:native.inside(temp,rel) for rel in paths}
        for f in targets.values():f.parent.mkdir(parents=True,exist_ok=True)
        def download(item):
            rel=item['relative_path'];f=targets[rel];data=retry(lambda:store.get(row['path']+'/'+rel))
            if len(data)!=item['size'] or sha(data)!=item['hash']['sha256']:raise ValueError('Asset download mismatch')
            f.write_bytes(data)
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(download,b['items']))
        native.verify(temp,package_digest) if temp.name==dest.name else _verify_named(temp,dest,package_digest)
        dest.parent.mkdir(parents=True,exist_ok=True);temp.rename(dest)
        return dest,{'status':'passed','action':'downloaded','path':str(dest),'downloaded':len(b['items'])+len(m['metadata'])+1}
    except Exception:
        if temp.exists() and temp.resolve().is_relative_to(cache/'.staging'):shutil.rmtree(temp)
        raise

def _verify_named(temp,dest,digest):
    # NP.005 observes the asset directory's name, even during download staging.
    named=temp.parent/(temp.name+'-package')/dest.name;named.parent.mkdir();temp.rename(named)
    try:native.verify(named,digest)
    finally:named.rename(temp);named.parent.rmdir()


def fetch_closure(store, roots, cache):
    """Fetch pinned asset and engineering dependencies into an empty/cache library."""
    pending=list(roots);done={}
    while pending:
        dep=pending.pop();key=(dep['asset_id'],dep['package_digest'])
        if key in done:continue
        folder,receipt=fetch(store,*key,cache)
        body=native.read(folder/native.ADAPTER)
        done[key]=dict(receipt,asset_id=key[0],package_digest=key[1])
        pending.extend([*body.get('dependencies',[]),*body.get('assembly',{}).get('dependencies',[])])
    return {'status':'passed','packages':len(done),'receipts':list(done.values())}
