"""Build a usage-only mirror from accepted defaults, without failed-source cards."""
import sys
sys.dont_write_bytecode=True
import argparse,copy,json,os,re,shutil,collections
from pathlib import Path
from core import read,write,digest,inside,canonical
from catalog import CATEGORIES
from minimal_format import finalize,INDEX_SCHEMA

def safe(value):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',str(value)).strip(' .')
    return value[:90].rstrip(' .') or '未命名'

def linked(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    if dst.exists():
        if digest(src)!=digest(dst):raise ValueError('Output collision '+str(dst))
        return
    try:os.link(src,dst)
    except OSError:shutil.copyfile(src,dst)

def clean_claims(value):
    if isinstance(value,list):return [clean_claims(x) for x in value]
    if isinstance(value,dict):return {k:clean_claims(v) for k,v in value.items() if k not in ('evidence','archive_ref','archive_path','original_package_path','metadata_path','input_files','stdout','stderr')}
    return value

def semantics(source,p):
    import gzip
    def payload(f):return json.loads(gzip.decompress(f.read_bytes())) if f.name.endswith('.gz') else read(f)
    result={}
    for f in sorted(list(source.rglob('*.json'))+list(source.rglob('*.json.gz'))):
        name=f.name[:-3] if f.name.endswith('.gz') else f.name
        if name=='source-parameters.json':result.setdefault('robot_parameters',payload(f))
        elif name=='kinematics.json':
            d=payload(f);result.setdefault('kinematics',{k:d[k] for k in ('schema','source_sha256','joints','frames','units','scope','gaps','status') if k in d})
        elif name=='mass-properties.json':
            d=payload(f);result.setdefault('mass_properties',{k:d[k] for k in ('schema','source_sha256','support_rules_sha256','entities','units','scope','status') if k in d})
        elif name=='physics-contract.json':
            d=payload(f);result.setdefault('source_physics',{k:d[k] for k in ('source_sha256','archive_member','rsc_sha256','entities','colliders','joints_and_drivers','controllers','scoped_physical_fields','source_parameters') if k in d})
            result['source_behavior']={'execution':'not_os_executable','archive_member':d.get('archive_member'),'scripts':[{k:v for k,v in x.items() if k not in ('path','code','text','content','source_file')} for x in d.get('embedded_scripts',[])]}
        elif name=='stored-content.json':
            d=payload(f);result['usd_content']={k:d[k] for k in ('source_usd_sha256','usd_authored','authored_field_names','scope') if k in d}
    return result

def build(source,destination,output,project_asset_root=None,resume=False):
    source=Path(source).resolve();dest=Path(destination).resolve();out=Path(output).resolve()
    if dest.exists() and not resume:raise ValueError('Use a fresh minimal destination')
    dest.mkdir(parents=True,exist_ok=True);out.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'asset-browser'))
    from library import Library
    from readiness import build as readiness
    lib=Library(source,None,out/'transient-state')
    records=[copy.deepcopy(x) for x in lib.records.values() if x['reuse_kind']!='engineering_knowledge']
    owners={}
    for r in records:
        old=r['targets'][0]['package_path']
        if old not in owners or not r['targets'][0].get('member'):owners[old]=r
    plans={};used=set();excluded=[]
    for old,r in sorted(owners.items()):
        p=read(source/old/'package.json')
        if not p.get('entrypoints',{}).get('usd'):
            excluded.append({'asset_id':p['asset_id'],'reason':'no usable spatial conversion'});continue
        if p.get('capabilities',{}).get('usd_reopen',{}).get('status')!='passed':
            excluded.append({'asset_id':p['asset_id'],'reason':'USD conversion/reopen not passed'});continue
        name=r['model']['value'];title=r['title']
        if p.get('profile')=='spatial' and title!=name:name=title+'_'+name
        base=CATEGORIES[r['reuse_kind']]+'/'+safe(r['manufacturer']['name'])+'/'+safe(name)
        number=1;rel=base+'__r001'
        while rel.casefold() in used:number+=1;rel=base+f'__款{number}__r001'
        used.add(rel.casefold());plans[old]={'path':rel,'old':old,'record':r,'package':p}
    # The station contains a standalone USD. Its OS dependencies are explicitly
    # migrated after all default engineering packages have been produced.
    order=sorted(plans,key=lambda x:plans[x]['package'].get('profile')=='station_layout')
    built={};by_id={};preview={};migration=[]
    from pxr import UsdUtils
    for number,old in enumerate(order,1):
        plan=plans[old];p=plan['package'];r=plan['record'];src=source/old;dst=dest/plan['path'];dst.mkdir(parents=True,exist_ok=True)
        plan['path']=dst.resolve().relative_to(dest).as_posix()
        filemap={};usd=p['entrypoints']['usd'];layers,assets,missing=UsdUtils.ComputeAllDependencies(str(src/usd))
        if missing:raise ValueError('Unresolved source USD: '+old)
        closure={Path(x.realPath).resolve() for x in layers if x.realPath}|{Path(x).resolve() for x in assets}
        if any(not x.is_relative_to(src) for x in closure):raise ValueError('USD dependency outside source package')
        common=Path(os.path.commonpath([str(x.parent) for x in closure]))
        for f in sorted(closure):
            key=f.relative_to(src).as_posix();new='asset'+f.suffix if len(closure)==1 else 'usd/'+f.relative_to(common).as_posix()
            linked(f,dst/new);filemap[key]=new
        defs=copy.deepcopy(read(src/p['definitions'])['definitions']) if p.get('definitions') else {}
        originals=[]
        if defs:
            is_assembly=any(d['kind']=='assembly' for d in defs.values())
            for d in defs.values():
                d.pop('mesh',None)
                for kind in ('source','native'):
                    item=d[kind];key=item['path']
                    if key not in filemap:
                        if not is_assembly:new='cad/'+Path(key).name
                        elif kind=='native':new='cad/工程/'+key.split('engineering/cad/')[-1]
                        else:new='cad/原件/'+key.split('source/files/')[-1]
                        if (dst/new).exists() and digest(dst/new)!=digest(src/key):new='cad/'+kind+'/'+Path(key).name
                        linked(src/key,dst/new);filemap[key]=(dst/new).resolve().relative_to(dst.resolve()).as_posix()
                    item['path']=filemap[key]
                originals.append(d['source'])
        elif p.get('source_manifest'):
            source_files=[item for item in read(src/p['source_manifest'])['files'] if Path(item['path']).name not in ('physics-rules.json','material-library-0.xml') and '/support/' not in item['path']]
            source_common=Path(os.path.commonpath([str(Path(x['path']).parent) for x in source_files])) if source_files else Path('.')
            for item in source_files:
                key=item['path'];name=Path(key).name
                if name in ('physics-rules.json','material-library-0.xml') or '/support/' in key:continue
                if key not in filemap:
                    new=name if len(source_files)==1 else 'source/'+Path(key).relative_to(source_common).as_posix()
                    if (dst/new).exists() and digest(dst/new)!=digest(src/key):new='source/'+key.split('sources/original/')[-1]
                    linked(src/key,dst/new);filemap[key]=(dst/new).resolve().relative_to(dst.resolve()).as_posix()
                originals.append({'path':filemap[key],'sha256':digest(src/key),'original_name':name})
        body={k:copy.deepcopy(p[k]) for k in ('asset_id','title','profile','reuse_kind','configuration','definition_revision','root_definition') if k in p}
        body.update(manufacturer=r['manufacturer'],model=r['model'],summary=r.get('summary',lib.summary(r)),qualification=clean_claims(r['qualification']),
                    entrypoints={'usd':filemap[usd]},sources=list({x['path']:x for x in originals}.values()),parameters=semantics(src,p))
        if defs:
            body['definitions_data']=defs;body['assembly_data']=read(src/p['assembly'])['occurrences'];body['bom_data']=read(src/p['bom']);body['interfaces_data']=read(src/p['interfaces']) if p.get('interfaces') else {}
            body['entrypoints']['cad']=filemap[p['entrypoints']['cad']]
        elif p.get('entrypoints',{}).get('cad'):
            key=p['entrypoints']['cad']
            if key in filemap:body['entrypoints']['cad']=filemap[key]
            else:
                # The same STEP bytes may already be retained as an original.
                matches=[x for x in originals if x['sha256']==digest(src/key)]
                if len(matches)!=1:raise ValueError('CAD entry not uniquely preserved')
                body['entrypoints']['cad']=matches[0]['path']
        for extra in p.get('import_metadata',{}).get('extras',[]):
            if extra.get('role')=='cad_usd_occurrence_mapping' and extra.get('path'):body['cad_mapping_data']=read(src/extra['path'])
        if p.get('profile')=='station_layout':
            graph=read(src/p['entrypoints']['os_graph']);lock=read(src/p['entrypoints']['os_lock']);dependencies=[]
            for dep in p['dependencies']:
                match=by_id.get(dep['asset_id'])
                if not match:raise ValueError('Station dependency not retained')
                q=match['body'];pd=match['digest'];d=copy.deepcopy(dep);d.update(package_digest=pd,entrypoints=q['entrypoints'],path='library/'+safe(q['title'])+'__'+pd[:12])
                node=graph['scene']['node_bindings'][d['node_id']]
                if node['sha256']!=digest(dest/match['path']/q['entrypoints']['usd']):
                    from usd_equivalence import compare
                    if not project_asset_root or dep['definition_revision']!=q['definition_revision']:raise ValueError('Station engineering revision changed')
                    original=Path(project_asset_root)/dep['path']/dep['entrypoints']['usd']
                    if digest(original)!=node['sha256']:raise ValueError('Original project USD differs from lock')
                    proof=compare(original,dest/match['path']/q['entrypoints']['usd']);d['explicit_layout_migration']=proof;node['sha256']=proof['new_usd_sha256']
                node['path']=d['path']+'/'+q['entrypoints']['usd'];dependencies.append(d)
            lock.update(dependencies=dependencies);lock['deployment_graph']={'path':'layout.json','sha256':__import__('hashlib').sha256(canonical(graph)).hexdigest()}
            lock.pop('domain_asset_index',None)
            write(dst/'layout.json',graph);write(dst/'assets.lock.json',lock)
            body.update(asset_root='assets',dependencies=dependencies);body['entrypoints'].update(os_graph='layout.json',os_lock='assets.lock.json')
            if p.get('cad_mapping'):body['cad_mapping_data']=read(src/p['cad_mapping'])
        if resume and (dst/'asset.json').exists():
            from minimal_format import verify
            verify(dst);previous=read(dst/'asset.json')
            if any(previous.get(k)!=p.get(k) for k in ('asset_id','definition_revision','configuration')):raise ValueError('Resume identity mismatch')
            table=read(dst/'readiness.json')
            if table['binding']['source_usd_sha256']!=digest(src/usd):raise ValueError('Resume USD changed')
        else:table=readiness(lib,r['catalog_id'])
        table['binding'].pop('package_digest',None);table['binding']['spatial_path']=filemap[usd];table['evidence']=[]
        table['scope']='逐项使用资格；保留绑定输入的结果摘要，详细运行记录不保留。文件精简不新增动力学或 NVIDIA 资格。'
        for g in table['groups']:
            for item in g['items']:
                item.pop('evidence',None);item.pop('evidence_ids',None);item['result_retention']='summary_only'
                if isinstance(item.get('binding'),dict):item['binding']={k:v for k,v in item['binding'].items() if k not in ('package_digest','spatial_path')}
                if g['id']=='nvidia_static' and item['status'] in ('passed','failed'):
                    item['previous_status']=item['status'];item['status']='not_verified';item['issue']='目录整理后未重跑官方检查；旧结果 '+item['previous_status']+'。'+item.get('issue','')
        write(dst/'readiness.json',table)
        display=copy.deepcopy(lib.presentation.get(r['catalog_id'],{}));thumb=display.get('thumbnail')
        if thumb:
            f=source/thumb['path']
            if digest(f)!=thumb['sha256']:raise ValueError('Thumbnail mismatch')
            linked(f,dst/'preview.png');thumb['path']=plan['path']+'/preview.png';thumb.pop('metadata_path',None)
        result=finalize(dst,body);entry={'body':body,'digest':result['package_digest'],'path':plan['path'],'filemap':filemap}
        built[old]=entry;by_id[p['asset_id']]=entry;preview[old]=display
        migration.append({'asset_id':p['asset_id'],'old_path':old,'old_digest':r['targets'][0]['package_digest'],'new_path':plan['path'],'new_digest':result['package_digest']})
        if number%100==0:print('built',number,'/',len(order),flush=True)
    final_records=[];presentations={}
    for r in records:
        old=r['targets'][0]['package_path']
        if old not in built:continue
        b=built[old];t=copy.deepcopy(r['targets'][0]);t.update(package_path=b['path'],package_digest=b['digest'],exports={})
        for kind,e in list(t['entrypoints'].items()):
            if e['path'] in b['filemap']:e['path']=b['filemap'][e['path']]
            elif kind=='deployment':e['path']='layout.json';e['sha256']=digest(dest/b['path']/'layout.json')
            else:del t['entrypoints'][kind]
        if not t['entrypoints']:raise ValueError('Record lost every usable entrypoint')
        r={k:v for k,v in r.items() if k not in ('provenance','catalog_evidence','supplemental')};r['qualification']=clean_claims(r['qualification']);r['targets']=[t]
        r['supplemental']={'metadata_path':b['path']+'/asset.json'};final_records.append(r)
        d=copy.deepcopy(lib.presentation.get(r['catalog_id'],{}))
        if d.get('thumbnail'):
            # Member previews belong to a different selection; retain them only
            # when different from the owning package's thumbnail.
            owner=preview[old].get('thumbnail',{});thumb=d['thumbnail']
            if thumb['sha256']==owner.get('sha256'):thumb['path']=owner['path']
            else:
                name='preview-'+safe(r['title'])+'.png';linked(source/thumb['path'],dest/b['path']/name);thumb['path']=b['path']+'/'+name
                # Additional member thumbnails are real browser dependencies.
            thumb.pop('metadata_path',None)
        presentations[r['catalog_id']]=d
    # Member preview additions change manifests; update every route consistently.
    for old,b in built.items():
        if b['body'].get('profile')=='station_layout':
            root=dest/b['path'];graph=read(root/'layout.json');lock=read(root/'assets.lock.json')
            for dep in b['body']['dependencies']:
                q=by_id[dep['asset_id']];dep.update(package_digest=q['digest'],path='library/'+safe(q['body']['title'])+'__'+q['digest'][:12])
                graph['scene']['node_bindings'][dep['node_id']]['path']=dep['path']+'/'+dep['entrypoints']['usd']
            lock['dependencies']=b['body']['dependencies'];write(root/'layout.json',graph);lock['deployment_graph']['sha256']=digest(root/'layout.json');write(root/'assets.lock.json',lock)
        h=finalize(dest/b['path'],b['body'])['package_digest'];b['digest']=h
        for r in final_records:
            if r['targets'][0]['package_path']==b['path']:r['targets'][0]['package_digest']=h
        for m in migration:
            if m['old_path']==old:m['new_digest']=h
        if b['body'].get('profile')=='station_layout':
            for r in final_records:
                if r['targets'][0]['package_path']==b['path'] and 'deployment' in r['targets'][0]['entrypoints']:r['targets'][0]['entrypoints']['deployment']['sha256']=digest(dest/b['path']/'layout.json')
    packages=[{'asset_id':b['body']['asset_id'],'package_digest':b['digest'],'path':b['path']} for b in built.values()]
    metadata=[]
    for row in packages:
        root=dest/row['path']
        for f in root.iterdir():
            if f.name in ('asset.json','readiness.json') or f.name.startswith('preview'):
                metadata.append({'path':f.relative_to(dest).as_posix(),'sha256':digest(f),'bytes':f.stat().st_size})
    ids={r['catalog_id'] for r in final_records}
    collections_=[{'name':c['name'],'ids':[x for x in c['ids'] if x in ids]} for c in lib.collections]
    data={'schema':INDEX_SCHEMA,'records':final_records,'packages':packages,'presentation':presentations,'collections':[c for c in collections_ if c['ids']],'metadata_files':metadata}
    write(dest/'library.json',data);write(out/'migration.json',migration)
    result={'status':'passed','records':len(final_records),'packages':len(packages),'files':sum(1 for p in dest.rglob('*') if p.is_file()),'failed_source_cards':0,'excluded':excluded,'index_sha256':digest(dest/'library.json')}
    write(out/'build.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--destination',required=True);p.add_argument('--output',required=True);p.add_argument('--project-asset-root');p.add_argument('--resume',action='store_true');a=p.parse_args();print(json.dumps(build(a.source,a.destination,a.output,a.project_asset_root,a.resume)))
