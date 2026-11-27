"""Six reuse classes, shared metadata and readable views over pinned releases."""
import sys
sys.dont_write_bytecode=True
import argparse,json,re,unicodedata
from pathlib import Path
from core import *

CATEGORIES={'component':'01_零部件','workpiece_consumable':'02_工件与耗材','mechanism':'03_机构模块',
            'equipment':'04_设备','station_template':'05_工站与布局模板','engineering_knowledge':'06_工程知识与证据'}

def segment(value,limit=28):
    value=unicodedata.normalize('NFC',str(value));value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value).rstrip(' .')
    if not value:value='未命名'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',value):value='_'+value
    return value if len(value)<=limit else value[:limit-9]+'-'+sha(value.encode())[:8]

def reference(p,result):return {'asset_id':p['asset_id'],'package_digest':result['package_digest'],'definition_revision':p['definition_revision'],'configuration':p['configuration']}

def model_targets(package,p,result):
    defs=read(package/p['definitions'])['definitions'];rows=read(package/p['assembly'])['occurrences'];byid={r['id']:r for r in rows};paths={}
    def prim(row):
        if row['id'] not in paths:paths[row['id']]=('/Asset' if row['parent'] is None else prim(byid[row['parent']]))+'/o_'+row['id'].replace('-','_')
        return paths[row['id']]
    for did,d in defs.items():
        isroot=did==p['root_definition'];target=reference(p,result)
        target.update(definition_id=did,definition_revision=d['revision'],configuration=d['configuration'],member=not isroot,
                      entrypoints={'engineering':{'path':d['native']['path'],'sha256':d['native']['sha256'],'format':Path(d['native']['path']).suffix.lstrip('.')}},exports={})
        if 'usd' in p['entrypoints']:
            target['entrypoints']['spatial']={'path':p['entrypoints']['usd'],'sha256':digest(package/p['entrypoints']['usd']),'format':'usd',
                                            'prim_paths':['/Asset'] if isroot else [prim(r) for r in rows if r['definition_id']==did],
                                            'placement_scope':'asset_local' if isroot else 'embedded_in_parent_assembly'}
        if isroot and 'blender' in p['entrypoints']:target['exports']['blender']={'path':p['entrypoints']['blender'],'sha256':digest(package/p['entrypoints']['blender'])}
        yield did,d,target

def select(records,filters):
    allowed={'reuse_kind','manufacturer','query','tag','state'}
    if set(filters)-allowed:raise ValueError('不支持的筛选字段，不能静默忽略')
    found=[]
    for r in records:
        if filters.get('reuse_kind') and r['reuse_kind']!=filters['reuse_kind']:continue
        if filters.get('manufacturer') and filters['manufacturer'].casefold() not in [r['manufacturer']['name'].casefold(),r['manufacturer']['id'].casefold()]:continue
        if filters.get('state') and r['state']!=filters['state']:continue
        if filters.get('tag') and filters['tag'] not in r['tags']:continue
        if filters.get('query') and filters['query'].casefold() not in json.dumps([r['title'],r['original_name'],r['model'],r['tags']],ensure_ascii=False).casefold():continue
        found.append(r)
    return found

def validate_record(r):
    if r['reuse_kind'] not in CATEGORIES:raise ValueError('未知复用类别')
    if not r['title'] or not r['configuration'] or not r['manufacturer'].get('status'):raise ValueError('编目关键字段缺失')
    if not r.get('provenance'):raise ValueError('编目缺少字段依据')
    if not r['targets']:raise ValueError('编目没有固定内容')
    for t in r['targets']:
        if not re.fullmatch('[0-9a-f]{64}',t['package_digest']):raise ValueError('编目包摘要非法')
        for ep in list(t['entrypoints'].values())+list(t.get('exports',{}).values()):
            inside(Path.cwd(),ep['path'])
            if not re.fullmatch('[0-9a-f]{64}',ep['sha256']):raise ValueError('入口缺少摘要')
    if r['primary_purpose'] not in r['targets'][0]['entrypoints']:raise ValueError('主要入口不存在')

def build(config_path,output):
    cfg=read(config_path);output=Path(output).resolve()
    if output.exists():raise ValueError('编目输出不可覆盖')
    output.mkdir(parents=True);curation=read(cfg['curation']);records={};locations={};packages=[]
    for source in cfg['packages']:
        root=Path(source).resolve();result=verify(root);p=read(root/'package.json');packages.append(reference(p,result));locations[result['package_digest']]=str(root)
        if 'definitions' in p:
            for did,d,target in model_targets(root,p,result):
                if did not in curation['definitions']:raise ValueError('定义尚未编目 '+did)
                meta=curation['definitions'][did]
                if did not in records:
                    records[did]={'schema':'unilab.catalog-record/1','catalog_id':did,**meta,'configuration':d['configuration'],'original_name':d['display_name'],
                                  'record_kind':'package_member','primary_purpose':'engineering','state':'listed','targets':[],
                                  'qualification':{'scope':'within_owning_package','independent_reuse':'not_run','interfaces':'not_authored','physics':'not_run'},
                                  'provenance':[{'field':'identity/configuration/original_name','package_digest':result['package_digest'],'path':p['definitions'],'sha256':digest(root/p['definitions'])},
                                                {'field':'title/reuse_kind/model','kind':'curated_from_source_names','curation_sha256':digest(cfg['curation'])}]}
                records[did]['targets'].append(target)
                if not target['member']:records[did]['record_kind']='package';records[did]['qualification']['independent_reuse']='native_geometry_validated'
        else:
            meta=curation['standalone'][p['asset_id']];purpose='deployment' if p['profile']=='station_layout' else 'document'
            names={'os_graph':'deployment','os_lock':'lock','usd':'spatial','document':'document'};target=reference(p,result)
            target.update(member=False,entrypoints={names[k]:{'path':v,'sha256':digest(root/v),'format':Path(v).suffix.lstrip('.')} for k,v in p['entrypoints'].items()},exports={})
            records[p['asset_id']]={'schema':'unilab.catalog-record/1','catalog_id':p['asset_id'],**meta,'original_name':p['title'],'configuration':p['configuration'],
                                    'primary_purpose':purpose,'record_kind':'package','state':'layout_candidate' if p['profile']=='station_layout' else 'listed','targets':[target],
                                    'qualification':{'scope':p.get('qualification_scope',p['profile']),'physics':'not_run','cad_roundtrip':'not_run' if p['profile']=='station_layout' else 'not_applicable'},
                                    'provenance':[{'field':'all','package_digest':result['package_digest'],'path':'package.json','sha256':digest(root/'package.json')}]}
    records=list(records.values())
    for r in records:
        # Prefer independently published roots, then v2 input order. New v3
        # versions remain selectable without silently upgrading old projects.
        r['targets'].sort(key=lambda t:t['member']);validate_record(r)
    records.sort(key=lambda r:r['catalog_id']);index_entries=[]
    for r in records:
        h=content_id(r);key=f'catalog/records/{r["catalog_id"]}/{h}.json';write(output/key,r)
        index_entries.append({k:r[k] for k in ('catalog_id','title','original_name','reuse_kind','manufacturer','model','configuration','tags','state','record_kind','primary_purpose')}|{'record_key':key,'record_digest':h})
    index={'schema':'unilab.discovery-index/1','scope':cfg['scope'],'entries':index_entries,'packages':packages,'source_config_sha256':digest(config_path),'curation_sha256':digest(cfg['curation'])}
    ih=content_id(index);indexkey=f'index/discovery/{ih}.json';write(output/indexkey,index);used=set();views=[]
    for r,i in zip(records,index_entries):
        category=CATEGORIES[r['reuse_kind']];maker=segment(r['manufacturer']['name']);name=segment(r['title']);model=segment(r['model']['value'] or '型号待核对');leaf=segment(r['configuration'],12)+'--'+r['catalog_id'][:8]+'--'+i['record_digest'][:8]
        suffix='/'.join((name,model,leaf))
        for base in (f'browse/按类别/{category}/{maker}/{suffix}',f'browse/按厂家/{maker}/{category}/{suffix}'):
            if base.casefold() in used:raise ValueError('目录消歧失败')
            used.add(base.casefold());ref={'schema':'unilab.asset-reference/1','catalog_id':r['catalog_id'],'record_key':i['record_key'],'record_digest':i['record_digest'],'index_key':indexkey,'index_digest':ih,'primary_purpose':r['primary_purpose']}
            write(output/(base+'/asset.ref.json'),ref);write(output/(base+'/versions.json'),{'targets':r['targets'],'automatic_project_upgrade':False})
            card=f"# {r['title']}\n\n原名：{r['original_name']}\n\n类别：{category}；条目：{r['record_kind']}；状态：{r['state']}\n\n厂家：{r['manufacturer']['name']}（{r['manufacturer']['status']}）\n\n来源型号/图号标识：{r['model']['value']}（{r['model']['status']}）\n\n配置：{r['configuration']}；主入口：{r['primary_purpose']}\n\n资格范围：{json.dumps(r['qualification'],ensure_ascii=False)}\n\n[固定引用](asset.ref.json) · [可选固定版本](versions.json)\n\nID：{r['catalog_id']}\n\n说明：包内成员的空间入口保留父装配坐标，不能当成已重定原点的独立资产。未知字段不会因编目而变成已确认。\n"
            (output/(base+'/README.md')).write_text(card,encoding='utf8');views.append({'path':base+'/asset.ref.json','catalog_id':r['catalog_id'],'record_digest':i['record_digest']})
    for kind,label in CATEGORIES.items():
        rows=[r for r in records if r['reuse_kind']==kind];path=output/'browse/按类别'/label/'README.md';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(f'# {label}\n\n本固定快照共 {len(rows)} 个条目。'+('暂无已编目条目；未生成虚构设备。' if not rows else '含候选或包内成员，使用时查看各自资格。')+'\n',encoding='utf8')
    collections=[]
    for c in cfg['collections']:
        selected=select(records,c['filters']);body={'schema':'unilab.collection/1','name':c['name'],'filters':c['filters'],'index_key':indexkey,'index_digest':ih,'qualification':'discovery_only_not_a_validated_solution',
                                                 'members':[{'catalog_id':r['catalog_id'],'record_digest':content_id(r)} for r in selected]}
        key='browse/专题集合/'+segment(c['name'])+'/'+content_id(body)[:12]+'/collection.json';write(output/key,body)
        (output/key).with_name('README.md').write_text(f"# {c['name']}\n\n固定索引筛选结果：{len(selected)} 项。\n\n此集合是检索结果，不是已验证的方案。\n\n[集合与固定版本](collection.json)\n",encoding='utf8');collections.append(key)
    (output/'browse/README.md').write_text('# 内部机械资产库\n\n[按类别](按类别/) · [按厂家](按厂家/) · [专题集合](专题集合/)\n\n同一资产的多个入口指向同一固定包；未知厂家和未验证用途保持未知。\n\n固定索引：'+indexkey+'\n',encoding='utf8')
    manifest={'schema':'unilab.catalog-build/1','index_key':indexkey,'index_digest':ih,'records':len(records),'package_roots':sum(r['record_kind']=='package' for r in records),'members':sum(r['record_kind']=='package_member' for r in records),'views':views,'collections':collections,'counts':{k:sum(r['reuse_kind']==k for r in records) for k in CATEGORIES},'files':file_entries(output)}
    write(output/'catalog-build.json',manifest)
    # Local-only lookup kept OUTSIDE the publishing tree.
    write(output.parent/(output.name+'-locations.json'),locations)
    return manifest

def verify_catalog(root):
    root=Path(root);m=read(root/'catalog-build.json')
    actual=[x for x in file_entries(root) if x['path']!='catalog-build.json']
    if actual!=m['files']:raise ValueError('编目文件摘要不符')
    index=read(inside(root,m['index_key']))
    if content_id(index)!=m['index_digest']:raise ValueError('索引摘要不符')
    rows={}
    for e in index['entries']:
        r=read(inside(root,e['record_key']));validate_record(r)
        if content_id(r)!=e['record_digest'] or r['catalog_id']!=e['catalog_id']:raise ValueError('编目记录不一致')
        rows[r['catalog_id']]=r
    seen={k:0 for k in rows}
    for view in m['views']:
        ref=read(inside(root,view['path']))
        if ref['index_digest']!=m['index_digest'] or ref['record_digest']!=content_id(rows[ref['catalog_id']]):raise ValueError('浏览引用漂移')
        seen[ref['catalog_id']]+=1
    if any(n!=2 for n in seen.values()):raise ValueError('分类/厂家视图覆盖不一致')
    for key in m['collections']:
        c=read(inside(root,key));expected=[{'catalog_id':r['catalog_id'],'record_digest':content_id(r)} for r in select(list(rows.values()),c['filters'])]
        if c['members']!=expected or c['index_digest']!=m['index_digest']:raise ValueError('专题筛选结果漂移')
    return {'status':'passed','records':len(rows),'views':len(m['views']),'collections':len(m['collections']),'counts':m['counts']}

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='action',required=True)
    b=sub.add_parser('build');b.add_argument('--config',required=True);b.add_argument('--output',required=True)
    v=sub.add_parser('verify');v.add_argument('--catalog',required=True)
    q=sub.add_parser('find');q.add_argument('--catalog',required=True);q.add_argument('--query');q.add_argument('--kind');q.add_argument('--manufacturer');q.add_argument('--tag')
    a=ap.parse_args()
    if a.action=='build':m=build(a.config,a.output);print(json.dumps({k:v for k,v in m.items() if k not in ('files','views')},ensure_ascii=True))
    elif a.action=='verify':print(json.dumps(verify_catalog(a.catalog),ensure_ascii=True))
    else:
        verify_catalog(a.catalog);root=Path(a.catalog);idx=read(root/read(root/'catalog-build.json')['index_key'])
        records=[read(root/e['record_key']) for e in idx['entries']];filters={k:v for k,v in {'query':a.query,'reuse_kind':a.kind,'manufacturer':a.manufacturer,'tag':a.tag}.items() if v}
        print(json.dumps(select(records,filters),ensure_ascii=True))

if __name__=='__main__':main()
