"""Shared USD-native delivery stage and migration of accepted library assets.

Geometry decoders need only supply a USD entry, useful engineering data,
source dependencies and readiness; temporary conversion evidence is excluded.
"""
import sys
sys.dont_write_bytecode=True
import argparse,copy,json,os,re,shutil
from pathlib import Path
from core import read,write,digest,sha,canonical,inside
import native_package as native

CATEGORIES={'component':'01_零部件','workpiece_consumable':'02_工件与耗材','mechanism':'03_机构模块','equipment':'04_设备','station_template':'05_工站与布局模板','engineering_knowledge':'06_工程知识与规则'}
def slug(text):
    value=re.sub(r'[^\w.-]+','_',str(text),flags=re.UNICODE).strip('._').lower()
    return value[:72].rstrip('._') or 'unnamed'
def link(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    if dst.exists():raise ValueError('Output file collision: '+str(dst))
    try:os.link(src,dst)
    except OSError:shutil.copyfile(src,dst)
def remap(value,mapping):
    if isinstance(value,str):return mapping.get(value,value)
    if isinstance(value,list):return [remap(x,mapping) for x in value]
    if isinstance(value,dict):return {k:remap(v,mapping) for k,v in value.items()}
    return value
def usd_closure(entry,root):
    from pxr import UsdUtils,Usd
    if not Usd.Stage.Open(str(entry)):raise ValueError('USD cannot reopen')
    layers,assets,missing=UsdUtils.ComputeAllDependencies(str(entry))
    if missing:raise ValueError('Unresolved USD dependencies: '+str(missing[:4]))
    paths={Path(x.realPath).resolve() for x in layers if x.realPath}|{Path(x).resolve() for x in assets}
    root=Path(root).resolve()
    if any(not f.is_relative_to(root) for f in paths):raise ValueError('External USD dependency')
    return [{'path':f.relative_to(root).as_posix(),'sha256':digest(f)} for f in sorted(paths)]

def export_package(source,destination,body,table,files,thumbnail='preview.png'):
    """Common authoring boundary. Paths in body/files are relative to source.

    Copies only the declared usable files, preserves authored USD bytes/layers
    and native CAD names. Never enables physics or invents missing properties.
    """
    source=Path(source).resolve();dest=Path(destination).resolve()
    if dest.exists():raise ValueError('Use a fresh delivery directory')
    usd=body['entrypoints'].get('usd')
    if not usd:raise ValueError('Conversion without usable USD is not admitted')
    before=usd_closure(inside(source,usd),source)
    declared=set(files)
    if any(x['path'] not in declared for x in before):raise ValueError('Declared delivery omits USD dependencies')
    prefix=Path(usd).parent.as_posix();prefix='' if prefix=='.' else prefix+'/'
    mapping={}
    entry='simready_usd/'+dest.name+Path(usd).suffix
    for rel in files:
        if rel in ('asset.json','readiness.json'):continue
        if rel==usd:new=entry
        elif rel==thumbnail:new='simready_usd/.thumbs/256x256/'+Path(entry).name+'.png'
        elif Path(rel).name.startswith('preview') and Path(rel).suffix.lower()=='.png':new='simready_usd/.thumbs/256x256/'+Path(rel).name
        elif prefix and rel.startswith(prefix):new='simready_usd/'+rel[len(prefix):]
        else:new='simready_usd/'+rel
        if new in mapping.values():raise ValueError('Delivery mapping collision')
        mapping[rel]=new
    dest.mkdir(parents=True)
    for old,new in mapping.items():
        src=inside(source,old);dst=inside(dest,new)
        if '/.thumbs/' in new:
            from PIL import Image
            dst.parent.mkdir(parents=True,exist_ok=True)
            with Image.open(src) as im:
                im.thumbnail((256,256));canvas=Image.new('RGBA',(256,256),(0,0,0,0));canvas.alpha_composite(im.convert('RGBA'),((256-im.width)//2,(256-im.height)//2));canvas.save(dst)
        else:link(src,dst)
    p=remap(copy.deepcopy(body),mapping);p.pop('files',None)
    p['entrypoints']['usd']=entry;p['usd_dependencies']=usd_closure(dest/entry,dest)
    # Relocation must preserve every authored runtime dependency byte.
    old_hashes=sorted(x['sha256'] for x in before);new_hashes=sorted(x['sha256'] for x in p['usd_dependencies'])
    if old_hashes!=new_hashes:raise ValueError('USD dependency closure changed during relocation')
    p['delivery']={'layout':'nvidia-usd/1','foundation_version':'2026.8.0','physics_modified':False,'license_status':'unasserted' if not p.get('license') else 'source_declared'}
    t=remap(copy.deepcopy(table),mapping);t['binding'].pop('package_digest',None)
    t['binding'].update(source_usd_sha256=digest(dest/entry),spatial_path=entry,usd_closure_sha256=sha(canonical(p['usd_dependencies'])))
    t['evidence']=[]
    write(dest/native.READINESS,t);native.finalize(dest,p)
    return p,mapping

def rebuild_index(root,index):
    metadata=[]
    for row in index['packages']:
        folder=root/row['path'];p=native.package(folder)
        row['package_digest']=digest(folder/native.MANIFEST)
        for r in index['records']:
            t=r['targets'][0]
            if t['package_path']!=row['path']:continue
            t['package_digest']=row['package_digest']
            for e in t['entrypoints'].values():e['sha256']=digest(folder/e['path'])
        for rel in [native.MANIFEST,native.BOM,native.ROOTS,native.ADAPTER,native.READINESS]+[f['path'] for f in p['files'] if '/.thumbs/' in f['path']]:
            f=folder/rel;metadata.append({'path':f.relative_to(root).as_posix(),'sha256':digest(f),'bytes':f.stat().st_size})
    index['metadata_files']=metadata;write(root/'library.json',index)
    return digest(root/'library.json')

def build(source,destination,output,selected=None):
    from minimal_format import verify
    src=Path(source).resolve();dest=Path(destination).resolve();out=Path(output).resolve()
    if dest.exists():raise ValueError('Use a fresh destination')
    old=read(src/'library.json');chosen=set(selected or []);rows=[r for r in old['packages'] if not chosen or r['asset_id'] in chosen]
    dest.mkdir(parents=True);built={};filemap={};used=set();by_id={}
    for i,row in enumerate(rows):
        folder=src/row['path'];verify(folder,row['package_digest']);p=read(folder/'asset.json')
        category=CATEGORIES[p['reuse_kind']];parts=Path(row['path']).parts
        rel=category+'/'+slug(parts[1])+'/'+slug(parts[2])
        if rel.casefold() in used:rel+='_'+str(i+1)
        used.add(rel.casefold());root=dest/rel
        q,mapping=export_package(folder,root,p,read(folder/'readiness.json'),[f['path'] for f in p['files']])
        built[row['path']]={'path':rel,'body':q,'mapping':mapping};by_id[p['asset_id']]=built[row['path']]
        for oldpath,newpath in mapping.items():filemap[row['path']+'/'+oldpath]=rel+'/'+newpath
        print(json.dumps({'built':i+1,'total':len(rows),'asset_id':p['asset_id']}),flush=True) if (i+1)%100==0 else None
    # Explicitly rebind native OS graph dependencies after every target exists.
    for b in built.values():
        p=b['body'];folder=dest/b['path']
        if p.get('profile')!='station_layout':continue
        graphpath=folder/p['entrypoints']['os_graph'];lockpath=folder/p['entrypoints']['os_lock']
        graph=read(graphpath);lock=read(lockpath)
        # Break hardlinks before editing migration output.
        graphpath.unlink();lockpath.unlink()
        for dep in p['dependencies']:
            other=by_id[dep['asset_id']];q=other['body'];pd=digest(dest/other['path']/native.MANIFEST)
            dep.update(package_digest=pd,path='library/'+Path(other['path']).name,entrypoints=q['entrypoints'])
            binding=graph['scene']['node_bindings'][dep['node_id']]
            binding.update(path=dep['path']+'/'+q['entrypoints']['usd'],sha256=digest(dest/other['path']/q['entrypoints']['usd']))
        write(graphpath,graph);lock['dependencies']=p['dependencies'];lock['deployment_graph']['sha256']=digest(graphpath);write(lockpath,lock);native.finalize(folder,p)
    records=[]
    for r in old['records']:
        oldpath=r['targets'][0]['package_path']
        if oldpath not in built:continue
        b=built[oldpath];r=remap(copy.deepcopy(r),filemap);t=r['targets'][0];t['package_path']=b['path']
        for ep in t['entrypoints'].values():ep['path']=b['mapping'][ep['path']]
        r['supplemental']={'metadata_path':b['path']+'/'+native.ADAPTER};records.append(r)
    ids={r['catalog_id'] for r in records};presentation=remap({k:v for k,v in old['presentation'].items() if k in ids},filemap)
    for d in presentation.values():
        if d.get('thumbnail'):
            f=dest/d['thumbnail']['path'];d['thumbnail']['sha256']=digest(f)
    index={'schema':native.INDEX_SCHEMA,'records':records,'packages':[{'asset_id':b['body']['asset_id'],'path':b['path']} for b in built.values()],
           'presentation':presentation,'collections':[dict(c,ids=[x for x in c['ids'] if x in ids]) for c in old['collections']]}
    h=rebuild_index(dest,index)
    result={'status':'passed','records':len(records),'packages':len(built),'index_sha256':h,'failed_source_records':0}
    write(out/'build.json',result);return result

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--destination',required=True);ap.add_argument('--output',required=True);ap.add_argument('--asset',action='append');a=ap.parse_args()
    print(json.dumps(build(a.source,a.destination,a.output,a.asset)))
