"""One human-readable directory tree, identical on disk and OSS."""
import sys
sys.dont_write_bytecode=True
import argparse,json,shutil,re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from core import *
from catalog import CATEGORIES,segment,model_targets,select

def build(config,root):
    cfg=read(config);root=Path(root).resolve();cur=read(cfg['curation']);records={};releases=[]
    if (root/'library.manifest.json').exists():raise ValueError('本地镜像已封存，不能覆盖')
    for source in cfg['packages']:
        package=Path(source);p=read(package/'package.json');v=verify(package)
        meta=cur['definitions'].get(p['asset_id']) or cur['standalone'][p['asset_id']]
        base='/'.join((CATEGORIES[meta['reuse_kind']],segment(meta['manufacturer']['name']),segment(meta['title']),segment(meta['model']['value']),segment(p['configuration']),'v0001'))
        dest=inside(root,base)
        if dest.exists():raise ValueError('可读名称冲突，需要明确区分型号/配置')
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(package,dest);verify(dest,v['package_digest'])
        virtual=f'releases/{p["asset_id"]}/{v["package_digest"]}/'
        entry={'schema':'unilab.asset-index-entry/1','asset_id':p['asset_id'],'title':p['title'],'definition_revision':p['definition_revision'],'configuration':p['configuration'],'package_digest':v['package_digest'],'package_key':virtual+'package.json','seal_key':virtual+'bundle.sha256.json','entrypoints':p['entrypoints'],'capabilities':p['capabilities'],'counts':v,'bucket':cfg['bucket'],'prefix':cfg['prefix']}
        releases.append({'asset_id':p['asset_id'],'package_digest':v['package_digest'],'path':base,'entry':entry})
        if 'definitions' in p:
            for did,d,t in model_targets(dest,p,v):
                t['package_path']=base
                if did not in records:records[did]={'catalog_id':did,**cur['definitions'][did],'original_name':d['display_name'],'configuration':d['configuration'],'record_kind':'package_member','state':'listed','primary_purpose':'engineering','targets':[],'qualification':{'physics':'not_run','interfaces':'not_authored','independent_reuse':'not_run'}}
                records[did]['targets'].append(t)
                if not t['member']:records[did]['record_kind']='package';records[did]['qualification']['independent_reuse']='native_geometry_validated'
        else:
            keys={'os_graph':'deployment','os_lock':'lock','usd':'spatial','document':'document'}
            t={'asset_id':p['asset_id'],'package_digest':v['package_digest'],'package_path':base,'member':False,'entrypoints':{keys[k]:{'path':path,'sha256':digest(dest/path)} for k,path in p['entrypoints'].items()}}
            records[p['asset_id']]={'catalog_id':p['asset_id'],**meta,'original_name':p['title'],'configuration':p['configuration'],'record_kind':'package','state':'layout_candidate' if p['profile']=='station_layout' else 'listed','primary_purpose':'deployment' if p['profile']=='station_layout' else 'document','targets':[t],'qualification':{'physics':'not_run','scope':p.get('qualification_scope','knowledge')}}
    records=sorted(records.values(),key=lambda r:r['title']);views=[]
    for r in records:
        r['targets'].sort(key=lambda t:t['member']);t=r['targets'][0]
        folder='/'.join((CATEGORIES[r['reuse_kind']],segment(r['manufacturer']['name']),segment(r['title']),segment(r['model']['value']),segment(r['configuration'])))
        # The asset card lives beside its immutable version, not inside its seal.
        card=inside(root,folder);card.mkdir(parents=True,exist_ok=True)
        ref={'schema':'unilab.readable-reference/1','catalog_id':r['catalog_id'],'primary_purpose':r['primary_purpose'],'target':t}
        write(card/'asset.ref.json',ref)
        ep=t['entrypoints'][r['primary_purpose']];rel=Path(__import__('os').path.relpath(root/t['package_path']/ep['path'],card)).as_posix()
        (card/'README.md').write_text(f"# {r['title']}\n\n厂家：{r['manufacturer']['name']}；来源型号：{r['model']['value']}；配置：{r['configuration']}。\n\n[打开主要文件](<{rel}>) · [固定引用](asset.ref.json)\n\n条目类型：{r['record_kind']}。资格：{json.dumps(r['qualification'],ensure_ascii=False)}。\n\n包内成员引用所属装配中的 CAD/USD；没有独立重新定原点。\n",encoding='utf8');views.append(folder+'/asset.ref.json')
    index={'schema':'unilab.readable-library/1','entries':records,'counts':{k:sum(r['reuse_kind']==k for r in records) for k in CATEGORIES},'scope':cfg['scope']}
    write(root/'索引/资产索引.json',index);write(root/'索引/发布位置.json',{'schema':'unilab.readable-releases/1','releases':releases})
    for kind,label in CATEGORIES.items():
        directory=root/label;directory.mkdir(parents=True,exist_ok=True)
        (directory/'README.md').write_text(f'# {label}\n\n共 {index["counts"][kind]} 条。'+('暂无完整合格条目。' if not index['counts'][kind] else '查看厂家、部件、型号和配置目录。')+'\n',encoding='utf8')
    makers=sorted({r['manufacturer']['name'] for r in records})
    for maker in makers:
        rows=[{'title':r['title'],'catalog_id':r['catalog_id'],'target':r['targets'][0]} for r in records if r['manufacturer']['name']==maker];path=root/'按厂家'/segment(maker);write(path/'目录.json',{'manufacturer':maker,'entries':rows})
        links=[f"- [{r['title']}](<../../{r['target']['package_path']}/{r['target']['entrypoints'][next(iter(r['target']['entrypoints']))]['path']}>)" for r in rows]
        (path/'README.md').write_text('# '+maker+'\n\n'+'\n'.join(links)+'\n',encoding='utf8')
    for c in cfg['collections']:
        rows=select(records,c['filters']);path=root/'专题集合'/segment(c['name']);write(path/'集合.json',{'name':c['name'],'filters':c['filters'],'members':[{'catalog_id':r['catalog_id'],'title':r['title'],'target':r['targets'][0]} for r in rows],'scope':'检索集合，非已验证工艺方案'})
        (path/'README.md').write_text(f'# {c["name"]}\n\n共 {len(rows)} 项；[固定引用](集合.json)。\n',encoding='utf8')
    archive=read(cfg['legacy_archive_report']);relative=Path(archive['archive']).resolve().relative_to(root).as_posix()
    write(root/'索引/历史兼容.json',{'schema':'unilab.legacy-compatibility/1','archive_path':relative,'sha256':archive['archive_sha256'],'files':archive['original_files'],'objects':archive['objects'],'legacy_prefix':'asset-library/v1/'})
    (root/'README.md').write_text('# 内部机械资产库\n\n本目录与 OSS `asset-library/` 按相对路径和文件摘要完全一致。模型实体位于可读类别/厂家/部件/型号/配置/版本目录；UUID 只用于清单身份。\n\n'+ '\n'.join(f'- [{label}]({label}/README.md)' for label in CATEGORIES.values())+'\n\n[厂家目录](按厂家/) · [专题集合](专题集合/) · [AI 索引](索引/资产索引.json) · [历史归档](90_历史归档/)\n\n原发布包和原项目锁按摘要保留在历史归档，使用支持此存储结构的工具读取；旧对象直链不再是正式入口。不要直接向本镜像写运行缓存或项目工作副本。\n',encoding='utf8')
    manifest={'schema':'unilab.readable-mirror/1','files':file_entries(root),'records':len(records),'releases':len(releases),'views':views,'counts':index['counts']}
    write(root/'library.manifest.json',manifest);return {'status':'passed','records':len(records),'releases':len(releases),'files':len(manifest['files'])+1,'root':str(root)}

def verify_local(root):
    root=Path(root);m=read(root/'library.manifest.json');actual=[f for f in file_entries(root) if f['path']!='library.manifest.json']
    if actual!=m['files']:raise ValueError('本地镜像有多余、缺失或变更文件')
    for r in read(root/'索引/发布位置.json')['releases']:verify(root/r['path'],r['package_digest'])
    bad=[f['path'] for f in m['files'] if re.search(r'(?:^|/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/|\.)|(?:^|/)[0-9a-f]{64}(?:/|\.)',f['path'])]
    if bad:raise ValueError('正式镜像仍含纯 UUID/摘要文件名 '+str(bad[:3]))
    return {'status':'passed','files':len(m['files'])+1,'pure_uuid_or_hash_names':0,'records':m['records'],'releases':m['releases']}

def publish(store,root,output):
    root=Path(root);checked=verify_local(root);m=read(root/'library.manifest.json')
    # Publish location registry last; before that historical clients still read v1.
    deferred={'索引/发布位置.json','library.manifest.json'}
    rows=[f for f in m['files'] if f['path'] not in deferred]
    def transfer(f):return store.put_immutable(f['path'],(root/f['path']).read_bytes())
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(transfer,rows))
    for key in ('索引/发布位置.json','library.manifest.json'):results.append(store.put_immutable(key,(root/key).read_bytes()))
    result={'status':'passed','local':checked,'prefix':store.prefix,'transfers':results};write(output,result);return {'status':'passed','files':len(results)}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['build','verify','publish','find']);ap.add_argument('--root',required=True);ap.add_argument('--config');ap.add_argument('--output');ap.add_argument('--query');ap.add_argument('--kind');ap.add_argument('--manufacturer');ap.add_argument('--tag');a=ap.parse_args()
    try:
        if a.action=='build':r=build(a.config,a.root)
        elif a.action=='verify':r=verify_local(a.root)
        elif a.action=='find':
            root=Path(a.root);manifest=read(root/'library.manifest.json');expected=next(f for f in manifest['files'] if f['path']=='索引/资产索引.json')
            if digest(root/'索引/资产索引.json')!=expected['sha256']:raise ValueError('检索索引与镜像清单不符')
            filters={k:v for k,v in {'query':a.query,'reuse_kind':a.kind,'manufacturer':a.manufacturer,'tag':a.tag}.items() if v}
            r={'status':'passed','entries':select(read(root/'索引/资产索引.json')['entries'],filters),'cad_opened':0}
        else:
            from oss_store import Store
            r=publish(Store(a.config),a.root,a.output)
        if a.output and a.action!='publish':write(a.output,r)
        print(json.dumps(r))
    except Exception as e:
        r={'status':'failed','error_type':type(e).__name__}
        if not type(e).__module__.startswith('oss2'):r['reason']=str(e)
        if a.output:write(a.output,r)
        print(json.dumps(r));sys.exit(1)
