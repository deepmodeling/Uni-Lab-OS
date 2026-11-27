"""Shared native USD packaging for source adapters and VC/RoboDK conversions."""
import uuid
from pathlib import Path
from core import read,write,digest,canonical,sha
from build_native_library import export_package,slug,usd_closure
from build_minimal_library import semantics,clean_claims

def deliver_payload(work,destination,body,table=None,thumbnail=None):
    """Shared boundary for every source family; identities are supplied by caller."""
    from source_semantics import ensure_cad_entry,extract
    from inspect_usd import inspect
    work=Path(work).resolve();body=__import__('copy').deepcopy(body)
    body['metadata_revision']='source-adapters/1'
    uuid.UUID(body['asset_id'])
    from build_native_library import CATEGORIES
    if body['reuse_kind'] not in CATEGORIES:raise ValueError('Unknown asset kind')
    ensure_cad_entry(work,body)
    check=inspect(work/body['entrypoints']['usd'],work)
    if check['status']!='passed':raise ValueError('Source USD failed inspection: '+str(check['failures']))
    body.setdefault('parameters',{}).update(extract(work,body))
    items=[x['path'] for x in usd_closure(work/body['entrypoints']['usd'],work)]
    items += [x['path'] for x in body.get('sources',[])]+list(body['entrypoints'].values())
    for d in body.get('definitions_data',{}).values():
        items += [d[k]['path'] for k in ('source','native') if k in d]
    items += body.pop('support_files',[])
    if thumbnail:items.append(thumbnail)
    if table is None:
        table={'schema':'unilab.asset-readiness/1','title':body['title'],'binding':{},'evidence':[],
          'groups':[{'id':key,'label':label,'items':[{'id':key,'label':label,'status':'passed' if key=='internal' else 'not_verified','summary':check['scope'] if key=='internal' else 'No qualification granted by format conversion','source_type':'source_conversion','evidence':[]}]} for key,label in [('internal','内部检查'),('physical_data','物性与参数资料'),('nvidia_static','NVIDIA 官方静态规则'),('dynamics','实际动力学')]]}
    result,mapping=export_package(work,destination,body,table,list(dict.fromkeys(items)),thumbnail)
    return result

def deliver(work,source,family,name,title=None,manufacturer=None,thumbnail=None):
    work=Path(work).resolve();source=Path(source).resolve()
    if family=='vc':
        result=read(work/'result.json');usd=Path(result['usd'])
        if any(result.get(k) for k in ('feature_decoder_gaps','archive_gaps','kinematic_decoder_gaps')):raise ValueError('Incomplete conversion is not admitted to native delivery')
        native=work/'sources'/source.name
    elif family=='robodk':
        result=read(work/'conversion.json');usd=Path(result['entry']);native=work/'source'/source.name
        if result['status']!='passed_offline_joint_assembly':raise ValueError('Failed joint conversion is not admitted')
    else:raise ValueError('Unsupported source family')
    if native.read_bytes()!=source.read_bytes():raise ValueError('Source copy differs')
    source_sha=digest(source);asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL,family+':'+source_sha))
    p={'asset_id':asset_id,'title':title or source.stem,'profile':'imported_candidate','reuse_kind':'equipment',
       'configuration':'source-default','definition_revision':source_sha,'manufacturer':{'name':manufacturer or 'unknown','basis':'caller' if manufacturer else 'unknown'},
       'entrypoints':{'usd':usd.relative_to(work).as_posix()},'sources':[{'path':native.relative_to(work).as_posix(),'sha256':source_sha,'original_name':source.name}],
       'parameters':clean_claims(semantics(work,{})),'qualification':{'scope':'offline source conversion; no native solver or dynamics claim'},
       'conversion':{'source_family':family,'converter_status':result['status'],'source_sha256':source_sha}}
    items=[x['path'] for x in usd_closure(usd,work)]+[native.relative_to(work).as_posix()]
    preview=None
    if thumbnail:
        from shutil import copyfile
        preview='preview.png';copyfile(thumbnail,work/preview);items.append(preview)
    table={'schema':'unilab.asset-readiness/1','title':p['title'],'binding':{},'evidence':[],
      'groups':[{'id':key,'label':label,'items':[{'id':key,'label':label,'status':'passed' if key=='internal' else 'not_verified',
        'summary':'Offline USD dependency/readback check' if key=='internal' else 'Not qualified by conversion; inspect source parameters',
        'source_type':'source_conversion','evidence':[]}]} for key,label in [('internal','内部检查'),('physical_data','物性与参数资料'),('nvidia_static','NVIDIA 官方静态规则'),('dynamics','实际动力学')]]}
    dest=work/'delivery'/slug(name)
    export_package(work,dest,p,table,list(dict.fromkeys(items)),preview)
    return {'status':'passed','package':str(dest),'asset_id':asset_id,'source_sha256':source_sha,'scope':'native USD packaging; no full SimReady qualification'}
