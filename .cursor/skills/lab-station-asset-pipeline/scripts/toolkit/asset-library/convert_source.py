"""Explicit source -> USD -> native package CLI; no legacy run dependencies."""
import argparse,copy,json,subprocess,sys,shutil,uuid
from pathlib import Path
from core import read,write,digest,content_id
from build_native_library import usd_closure,slug
from deliver_conversion import deliver_payload

def convert(source,output,family,asset_id,title,kind,manufacturer='未知厂家',configuration='source-default',pid=None,spec=None,blender=None,expected_sha256=None):
    source=Path(source).resolve();output=Path(output).resolve();uuid.UUID(asset_id)
    if output.exists():raise ValueError('Use an empty new output directory')
    if expected_sha256 and digest(source)!=expected_sha256:raise ValueError('Input digest differs')
    output.mkdir(parents=True);work=output/'work';here=Path(__file__).resolve().parent
    if family=='step':
        if not blender:raise ValueError('SolidWorks-native CAD projection requires --blender')
        from native_cad_usd import convert as step
        payload,_,_=step(source,work,asset_id,blender,{},configuration,source.parent)
    elif family=='solidworks' and not pid and not spec:
        if not blender:raise ValueError('SolidWorks-native CAD projection requires --blender')
        from native_cad_usd import convert as native
        payload,_,_=native(source,work,asset_id,blender,{},configuration,source.parent)
    elif family=='solidworks':
        if not pid or not spec:raise ValueError('Provide both --pid and --spec for the explicit CAD-edit capture route')
        settings=read(spec)
        if (Path(settings['source_root'])/settings['path']).resolve()!=source:raise ValueError('Spec/source mismatch')
        subprocess.run([sys.executable,'-B',str(here/'native_capture.py'),'--pid',str(pid),'--spec',str(spec),'--output',str(work)],check=True)
        from finalize_capture import finalize
        from usd_derive import derive
        finalize(work,'spatial',kind);derive(work);p=read(work/'package.json');defs=read(work/p['definitions'])['definitions']
        if p['asset_id']!=asset_id:raise ValueError('Native identity differs; provide identity_map in spec')
        payload={'entrypoints':p['entrypoints'],'sources':[d['source'] for d in defs.values()],
          'definitions_data':defs,'assembly_data':read(work/p['assembly'])['occurrences'],'bom_data':read(work/p['bom']),'interfaces_data':read(work/p['interfaces']),
          'root_definition':p['root_definition'],'configuration':p['configuration'],'definition_revision':p['definition_revision'],'conversion':{'family':family,'native_reopen':'passed','scope':'selected native configuration; no mate/drive inference'}}
        for d in defs.values():d.pop('mesh',None)
    elif family in ('urdf','glb'):
        if not blender:raise ValueError('This route requires an explicit Blender executable')
        subprocess.run([str(blender),'--background','--factory-startup','--threads','2','--python-exit-code','1','--python',str(here/'historical_blender.py'),'--','--source',str(source),'--output',str(work),'--family',family],check=True)
        payload=read(work/'conversion.json')
    elif family=='usd':
        # Preserve the authored layer tree and relative paths. No flattening.
        closure=usd_closure(source,source.parent);work.mkdir()
        for row in closure:
            dest=work/row['path'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source.parent/row['path'],dest)
        payload={'entrypoints':{'usd':source.name},'sources':[{'path':r['path'],'sha256':r['sha256'],'original_name':Path(r['path']).name} for r in closure],
          'conversion':{'family':family,'scope':'existing USD dependency closure, unchanged bytes; no new dynamics'}}
    else:raise ValueError('Unsupported source family')
    body={'asset_id':asset_id,'title':title,'reuse_kind':kind,'profile':'imported_candidate','manufacturer':{'name':manufacturer,'status':'caller_declared' if manufacturer!='未知厂家' else 'unknown'},'configuration':configuration,
      'definition_revision':content_id({'source_sha256':digest(source),'configuration':configuration}),'qualification':{'scope':'source conversion only'},**{k:v for k,v in payload.items() if k not in ('status','source_sha256')}}
    destination=output/'delivery'/(slug(title)+'__r001');delivered=deliver_payload(work,destination,body)
    result={'status':'passed','asset_id':asset_id,'family':family,'source_sha256':digest(source),'package':str(destination),'usd':str(destination/delivered['entrypoints']['usd']),'scope':'native package, source conversion and USD readback; no full SimReady/dynamics qualification'}
    write(output/'result.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('source','output','asset-id','title','kind'):p.add_argument('--'+key,required=True)
    p.add_argument('--family',choices=['solidworks','step','usd','urdf','glb'],required=True);p.add_argument('--manufacturer',default='未知厂家');p.add_argument('--configuration',default='source-default');p.add_argument('--pid',type=int);p.add_argument('--spec');p.add_argument('--blender');p.add_argument('--expected-sha256');a=p.parse_args()
    try:print(json.dumps(convert(**vars(a)),ensure_ascii=False))
    except Exception as exc:
        write(Path(a.output)/'failure.json',{'status':'failed','family':a.family,'error':str(exc)});raise
