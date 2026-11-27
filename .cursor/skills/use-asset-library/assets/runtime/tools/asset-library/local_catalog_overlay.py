"""Keep explicitly selected local-only assets and exact-version preview sidecars."""
import copy
from core import inside,digest
from minimal_format import catalog_digest_bindings,verify


def merge(root,current,incoming,selection):
    data=copy.deepcopy(incoming)
    old_records={r['catalog_id']:r for r in catalog_digest_bindings(current)[0]}
    new_records={r['catalog_id']:r for r in catalog_digest_bindings(incoming)[0]}
    chosen={(r['asset_id'],r['package_digest']) for r in selection.get('packages',[])}
    remote={(r['asset_id'],r['package_digest']) for r in data['packages']}
    paths=[]
    for package in current['packages']:
        key=package['asset_id'],package['package_digest']
        if key in chosen and key not in remote:
            verify(inside(root,package['path']),package['package_digest'])
            data['packages'].append(copy.deepcopy(package));paths.append(package['path']+'/')
    identities={(r['asset_id'],r['package_digest']) for r in data['packages']}
    local_ids=set(selection.get('catalog_ids',[]))
    for record in current['records']:
        rid=record['catalog_id']
        if rid in local_ids and rid not in new_records:
            bound=old_records[rid]
            if not all((t['asset_id'],t['package_digest']) in identities for t in bound['targets']):
                raise ValueError('Local catalog package binding missing')
            data['records'].append(copy.deepcopy(record));new_records[rid]=bound
            if rid in current['presentation']:data['presentation'][rid]=copy.deepcopy(current['presentation'][rid])
    metadata={r['path']:r for r in data['metadata_files']}
    for row in current['metadata_files']:
        if any(row['path'].startswith(p) for p in paths):
            if digest(inside(root,row['path']))!=row['sha256']:raise ValueError('Local-only metadata changed')
            metadata[row['path']]=copy.deepcopy(row)
    for rid,record in new_records.items():
        thumb=current.get('presentation',{}).get(rid,{}).get('thumbnail',{})
        if not thumb.get('path','').startswith('预览/'):continue
        if thumb.get('source_package_digest')!=record['targets'][0]['package_digest']:continue
        path=inside(root,thumb['path'])
        if not path.is_file() or digest(path)!=thumb['sha256']:raise ValueError('Local preview changed')
        data['presentation'].setdefault(rid,{})['thumbnail']=copy.deepcopy(thumb)
        metadata[thumb['path']]={'path':thumb['path'],'sha256':thumb['sha256'],'bytes':path.stat().st_size}
    data['metadata_files']=list(metadata.values())
    if selection:data['local_catalog']=copy.deepcopy(selection)
    return data
