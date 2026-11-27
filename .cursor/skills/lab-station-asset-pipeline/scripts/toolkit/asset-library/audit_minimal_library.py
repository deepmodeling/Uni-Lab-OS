"""Independent file/route/readiness/dependency audit of a usage-only library."""
import sys
sys.dont_write_bytecode=True
import argparse,json
from pathlib import Path
from core import read,digest,inside,write
from minimal_format import verify,SUPPORTED_SCHEMAS,package,manifest_name

def audit(root):
    root=Path(root).resolve();index=read(root/'library.json')
    if index.get('schema') not in SUPPORTED_SCHEMAS:raise ValueError('Unknown library')
    expected={'library.json'};packages={};counts={'files':1,'native_sources':0,'usd':0};source_only=0
    for row in index['packages']:
        p=root/row['path'];verify(p,row['package_digest']);body=package(p);packages[(row['asset_id'],row['package_digest'])]=body
        if index['schema']=='unilab.usd-library/1':
            from build_native_library import CATEGORIES
            if Path(row['path']).parts[0]!=CATEGORIES.get(body.get('reuse_kind')):raise ValueError('Native package must use the selected Chinese category')
        if len(Path(row['path']).parts)!=3:raise ValueError('Asset directory must have three readable levels')
        expected.add(row['path']+'/'+manifest_name(p))
        for f in body['files']:expected.add(row['path']+'/'+f['path'])
        counts['native_sources']+=len(body.get('sources',[]));counts['usd']+=bool(body['entrypoints'].get('usd'))
        if not body['entrypoints'].get('usd'):raise ValueError('Source-only/failed entry in model library')
        from pxr import UsdUtils
        layers,assets,missing=UsdUtils.ComputeAllDependencies(str(p/body['entrypoints']['usd']))
        if missing:raise ValueError('USD dependency missing')
        for value in [x.realPath for x in layers if x.realPath]+list(assets):
            if Path(value).resolve().relative_to(root).as_posix() not in expected and not Path(value).resolve().is_relative_to(p):raise ValueError('Undeclared external USD dependency')
    for record in index['records']:
        from catalog import CATEGORIES as KIND_CODES
        if record.get('reuse_kind') not in KIND_CODES:raise ValueError('Invalid category code; display names must not replace stable codes')
        if not record.get('targets') or record.get('record_kind')=='source_record':raise ValueError('Failed source card included')
        for t in record['targets']:
            if (t['asset_id'],t['package_digest']) not in packages:raise ValueError('Catalog route not pinned')
            for ep in t['entrypoints'].values():
                if digest(inside(root,t['package_path']+'/'+ep['path']))!=ep['sha256']:raise ValueError('Entrypoint hash mismatch')
    for p in packages.values():
        for dep in [*p.get('dependencies',[]),*p.get('assembly',{}).get('dependencies',[])]:
            if (dep['asset_id'],dep['package_digest']) not in packages:raise ValueError('OS dependency not in library')
    for row in index['metadata_files']:
        f=inside(root,row['path'])
        if f.stat().st_size!=row['bytes'] or digest(f)!=row['sha256']:raise ValueError('Browse metadata mismatch')
    for d in index['presentation'].values():
        if d.get('thumbnail') and digest(root/d['thumbnail']['path'])!=d['thumbnail']['sha256']:raise ValueError('Preview mismatch')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual!=expected:raise ValueError('Unused or missing library files: '+str(list(actual^expected)[:5]))
    if any('/归档/' in p or p.endswith(('.log','.pyc','.blend')) for p in actual):raise ValueError('Archive/cache/export residue')
    counts.update(status='passed',files=len(actual),records=len(index['records']),packages=len(packages),source_only_records=0,index_sha256=digest(root/'library.json'),bytes=sum((root/p).stat().st_size for p in actual))
    return counts

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--output',required=True);a=p.parse_args();result=audit(a.root);write(a.output,result);print(json.dumps(result))
