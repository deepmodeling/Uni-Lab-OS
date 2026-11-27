"""Append existing, explicit USD definitions to a native library staging tree.

Uses the established exporter and index layout. Domain definitions are supplied
as input, not inferred from geometry or converted into execution claims.
"""
import argparse,copy,json,os,shutil,uuid
from pathlib import Path
from pxr import Usd
import native_package as native
from build_native_library import CATEGORIES,export_package,slug


def ingest(manifest, library, stage, work):
    manifest=native.read(manifest);library=Path(library).resolve();stage=Path(stage).resolve();work=Path(work).resolve()
    if stage.exists():raise FileExistsError(stage)
    def link(src,dst):
        try:os.link(src,dst)
        except OSError:shutil.copyfile(src,dst)
    shutil.copytree(library,stage,copy_function=link)
    (stage/'library.json').unlink()
    index=native.read(library/'library.json');added=[]
    for spec in manifest['assets']:
        maker=spec.get('manufacturer',{'id':'unilab','name':'UniLab 内部设计','status':'internal_design'})
        aid=spec['asset_id'];rel=f"{CATEGORIES[spec['reuse_kind']]}/{slug(maker['id'])}/{spec['slug']}__{spec.get('revision','r001')}"
        if any(p['path']==rel for p in index['packages']):raise ValueError('Revision already exists: '+rel)
        source=work/spec['slug'];source.mkdir(parents=True,exist_ok=False)
        origin=Path(spec['source']);usd=Usd.Stage.Open(str(origin))
        if not usd or not usd.GetDefaultPrim():raise ValueError('Missing USD defaultPrim: '+str(origin))
        if spec.get('preserve_composition'):
            # Assembly assets retain local module references. Their complete
            # relative USD closure must be supplied in support_files and is
            # checked by the existing exporter before sealing.
            if origin.suffix.lower()!='.usdc':raise ValueError('Preserved assembly entry must be usdc')
            shutil.copyfile(origin,source/'asset.usdc')
        else:
            usd.Flatten().Export(str(source/'asset.usdc'))
        body=copy.deepcopy(spec.get('metadata',{}));body.update(
            asset_id=aid,title=spec['title'],configuration='design',reuse_kind=spec['reuse_kind'],
            definition_revision=native.digest(source/'asset.usdc'),entrypoints={'usd':'asset.usdc'},sources=[],
            manufacturer=maker,
            model=spec.get('model',{'value':spec['slug'],'status':'design_not_purchase_sku'}),
            provenance={'source':str(origin),'sha256':native.digest(origin)},
            qualification={'geometry':'usd_reopen','physics':'not_run','hardware':'not_run','behavior':'declared_not_executed'})
        table={'schema':'unilab.asset-readiness/1','title':spec['title'],'binding':{'catalog_id':aid,'default_target':True},'groups':[
            {'id':'internal','label':'入库检查','items':[{'id':'usd_reopen','label':'USD重开与闭包','status':'passed','issue':''}]},
            {'id':'dynamics','label':'动态资格','items':[{'id':'physics','label':'接触与动力学','status':'not_run','issue':'入库不授予任务或动力学资格'}]}],'evidence':[]}
        files=['asset.usdc']
        for name,path in spec.get('support_files',{}).items():
            dest=source/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest);files.append(name)
            # 行为源码由 behavior_binding 显式定位，不作为几何/数据入口。
            if Path(name).suffix != '.py':
                key=Path(name).stem
                if key=='usd':raise ValueError('Support file cannot replace the USD entrypoint')
                body['entrypoints'][key]=name
        p,_=export_package(source,stage/rel,body,table,files,thumbnail=None)
        root=stage/rel;digest=native.digest(root/native.MANIFEST)
        target={'asset_id':aid,'configuration':body['configuration'],'definition_revision':body['definition_revision'],
            'package_path':rel,'package_digest':digest,'member':False,'exports':{},'entrypoints':{
                'spatial':{'path':p['entrypoints']['usd'],'sha256':native.digest(root/p['entrypoints']['usd']),'format':'usdc','placement_scope':'asset_local'}}}
        record={'schema':'unilab.catalog-record/1','catalog_id':aid,'title':body['title'],'original_name':spec['slug'],
            'configuration':body['configuration'],'record_kind':'package','reuse_kind':body['reuse_kind'],
            'manufacturer':body['manufacturer'],'model':body['model'],'owner':'postproc','primary_purpose':'spatial',
            'state':'design_candidate','summary':spec.get('summary','postproc 固定来源设计资产；任务资格单独验收'),
            'qualification':body['qualification'],'tags':['postproc','组合设备包'],
            'targets':[target],'supplemental':{'metadata_path':rel+'/'+native.ADAPTER}}
        # A new revision changes the selected record but preserves previous pinned packages.
        index['records']=[r for r in index['records'] if r['catalog_id']!=aid]+[record]
        index['packages'].append({'asset_id':aid,'path':rel,'package_digest':digest})
        index['presentation'][aid]={'title':body['title'],'summary':record['summary']}
        for file in (native.MANIFEST,native.BOM,native.ROOTS,native.ADAPTER,native.READINESS):
            f=root/file;index['metadata_files'].append({'path':rel+'/'+file,'sha256':native.digest(f),'bytes':f.stat().st_size})
        added.append({'key':spec['key'],**target,'default_prim':str(usd.GetDefaultPrim().GetPath())})
        print('INGEST',spec['key'],flush=True)
    index['collections'].append({'name':manifest['collection'],'ids':[r['asset_id'] for r in added]})
    native.write(stage/'library.json',index);native.write(work/'ingest.json',{'status':'passed','assets':added})
    return added


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('manifest','library','stage','work'):p.add_argument('--'+k,required=True)
    a=p.parse_args();ingest(a.manifest,a.library,a.stage,a.work)
