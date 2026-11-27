"""Portable NVIDIA package/BOM reader; also shipped unchanged with Blender.

Uses the 2026.8 PKG.HASH.001 byte algorithms. This is integrity checking,
not a NVIDIA conformance stamp or a physics qualification.
"""
import hashlib,json,os
from pathlib import Path

MANIFEST='com.nvidia.simready.packaging.json'
BOM='.metadata/com.nvidia.simready.packaging.bom.json'
ROOTS='.metadata/com.nvidia.simready.root_usds.json'
ADAPTER='simready_usd/unilab.json'
READINESS='simready_usd/readiness.json'
INDEX_SCHEMA='unilab.usd-library/1'

def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(b):return hashlib.sha256(b).hexdigest()
def digest(p):return sha(Path(p).read_bytes())
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(canonical(v))
def inside(root,rel):
    root=Path(root).resolve();p=(root/rel).resolve()
    if not isinstance(rel,str) or '\\' in rel or any(x in ('.','..','') for x in rel.split('/')) or Path(rel).is_absolute() or p==root or not p.is_relative_to(root):raise ValueError('Unsafe package path')
    return p
def content_hash(items):
    return sha(b''.join(x['relative_path'].encode('utf-8')+b'\0'+bytes.fromhex(x['hash']['sha256']) for x in sorted(items,key=lambda x:x['relative_path'].encode('utf-8'))))
def package_hash(p):
    return sha(p['package_id'].encode()+b'\0'+p['license'].encode()+b'\0'+bytes.fromhex(p['content_hash']['sha256'])+
               b''.join(x['name'].encode()+b'\0'+bytes.fromhex(x['hash']['sha256']) for x in sorted(p['metadata'],key=lambda x:x['name'].encode())))
def file_rows(root):
    root=Path(root);p=read(root/MANIFEST);b=read(root/BOM)
    return [{'path':x['relative_path'],'sha256':x['hash']['sha256'],'bytes':x['size']} for x in b['items']]+[
        {'path':'.metadata/'+x['name'],'sha256':x['hash']['sha256'],'bytes':(root/'.metadata'/x['name']).stat().st_size} for x in p['metadata']]
def package(root):
    p=read(Path(root)/ADAPTER);p['files']=file_rows(root);return p
def verify(root,expected=None):
    root=Path(root).resolve()
    # Blender's embedded Python can enumerate a 260-character filename but
    # report is_file()==False unless Windows extended-length syntax is used.
    # Keep strict BOM equality; fix filesystem access instead of skipping files.
    if os.name=='nt' and not str(root).startswith('\\\\?\\'):
        value=str(root);root=Path('\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value)
    m=read(root/MANIFEST);h=digest(root/MANIFEST)
    if expected and h!=expected:raise ValueError('Package manifest identity mismatch')
    if m.get('format_version')!='1.0' or not m.get('package_id') or not m.get('license'):raise ValueError('Invalid package definition')
    for entry in m['metadata']:
        f=inside(root,'.metadata/'+entry['name'])
        if digest(f)!=entry['hash']['sha256']:raise ValueError('Metadata hash mismatch')
    b=read(root/BOM)
    if content_hash(b['items'])!=m['content_hash']['sha256'] or package_hash(m)!=m['package_hash']['sha256']:raise ValueError('Aggregate package hash mismatch')
    rows=file_rows(root);paths=[x['path'] for x in rows]
    if len(paths)!=len(set(paths)) or len(paths)!=len(set(x.casefold() for x in paths)) or MANIFEST in paths:raise ValueError('Duplicate/ambiguous package file')
    if {x.relative_to(root).as_posix() for x in root.rglob('*') if x.is_file()}!=set(paths)|{MANIFEST}:raise ValueError('Missing or extra package file')
    for x in rows:
        f=inside(root,x['path'])
        if f.stat().st_size!=x['bytes'] or digest(f)!=x['sha256']:raise ValueError('Package file changed: '+x['path'])
    p=read(root/ADAPTER)
    if p.get('metadata_revision')=='source-adapters/1':
        cad_ext={'.sldprt','.sldasm','.step','.stp','.iges','.igs','.x_t','.x_b'}
        if any(Path(s['path']).suffix.lower() in cad_ext for s in p.get('sources',[])) and not p['entrypoints'].get('cad'):
            raise ValueError('Preserved CAD requires an explicit CAD entrypoint')
    if p['asset_id']!=m['package_id']:raise ValueError('Package and engineering identities differ')
    usd=p['entrypoints']['usd'];entry=inside(root,usd)
    if entry.parent!=root/'simready_usd' or root.name.lower() not in entry.stem.lower():raise ValueError('NP.005 asset entry structure')
    if len([f for f in entry.parent.iterdir() if f.suffix.lower() in ('.usd','.usdc','.usda','.usdz')])!=1:raise ValueError('Multiple root USD entries')
    if read(root/ROOTS)['entries']!=[usd]:raise ValueError('Root USD declaration differs')
    for path in p['entrypoints'].values():
        if path not in paths:raise ValueError('Undeclared entrypoint')
    for d in p.get('definitions_data',{}).values():
        for k in ('source','native'):
            if k in d and digest(inside(root,d[k]['path']))!=d[k]['sha256']:raise ValueError('CAD mapping mismatch')
    table=read(root/READINESS)
    if table['binding']['source_usd_sha256']!=digest(entry):raise ValueError('Readiness root mismatch')
    closure=p['usd_dependencies']
    for row in closure:
        if digest(inside(root,row['path']))!=row['sha256']:raise ValueError('Readiness dependency changed')
    if table['binding']['usd_closure_sha256']!=sha(canonical(closure)):raise ValueError('Readiness closure mismatch')
    return {'status':'passed','package_digest':h,'files':len(paths)+1,'asset_id':p['asset_id']}
def finalize(root,p):
    root=Path(root);p=dict(p);p.pop('files',None);p['schema']='unilab.usd-adapter/1';write(root/ADAPTER,p)
    items=[{'relative_path':f.relative_to(root).as_posix(),'size':f.stat().st_size,'hash':{'sha256':digest(f)}}
           for f in sorted((root/'simready_usd').rglob('*')) if f.is_file()]
    write(root/BOM,{'format_version':'1.0','items':items});write(root/ROOTS,{'format_version':'1.0','entries':[p['entrypoints']['usd']]})
    m={'format_version':'1.0','package_id':p['asset_id'],'license':p.get('license') or 'NOASSERTION',
       'description':p['title'][:500],'content_hash':{'sha256':content_hash(items)},
       'metadata':[{'name':Path(path).name,'hash':{'sha256':digest(root/path)}} for path in (BOM,ROOTS)]}
    m['package_hash']={'sha256':package_hash(m)};write(root/MANIFEST,m);return verify(root)
