"""Spreadsheet-backed domain/scene/stage requirements and catalog bindings."""
import hashlib
from pathlib import Path
from discovery_fields import canonical_stage


def stable_id(kind,name):
    return kind+':'+hashlib.sha256(name.encode('utf8')).hexdigest()[:18]


def compile_sheet(path,nodes):
    from openpyxl import load_workbook
    book=load_workbook(path,read_only=True,data_only=True);sheet=book.active
    # Feishu export has an incorrect A1 dimension despite containing 37 rows.
    sheet.reset_dimensions();rows=list(sheet.values);header=rows[0]
    assert header[0]=='场景&项目' and header[1]=='实验范式'
    lookup={}
    for n in nodes:
        for name in [n['name'],*n.get('aliases',[])]:
            if isinstance(name,str):lookup.setdefault(name,[]).append(n)
    records=[];mappings=[]
    for rownum,row in enumerate(rows[1:],2):
        if not row[0]:continue
        name=str(row[0]).strip();domain=str(row[1]).strip() if row[1] else '待归类'
        scene={'id':stable_id('scene',name),'name':name,'domain':domain,'row':rownum,'requirements':[]}
        for col in range(2,8):
            for raw in str(row[col] or '').split(','):
                raw=raw.strip()
                if not raw:continue
                alias={'机械臂':'机器人','AGV':'移动机器人'}.get(raw,raw)
                candidates=lookup.get(alias,[])
                # The sheet's equipment stage refers to the laboratory filter,
                # not the same-named mechanical component.
                if raw=='过滤器':candidates=[n for n in candidates if n['resource_type']=='device']
                target_ids=[n['id'] for n in candidates] if len(candidates)==1 else []
                req={'source_name':raw,'stage':canonical_stage(header[col]),'category_ids':target_ids,
                    'cell':f'{chr(65+col)}{rownum}','status':'mapped' if target_ids else 'unresolved'}
                if raw=='AGV':req['name_requires']='agv'
                scene['requirements'].append(req)
                mappings.append({'scene':name,'domain':domain,**req})
        records.append(scene)
    return {'schema':'unilab.scene-catalog/1','source':str(Path(path).resolve()),
        'source_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),'sheet':sheet.title,
        'stages':[canonical_stage(s) for s in header[2:8]],
        'domains':list(dict.fromkeys(r['domain'] for r in records)),
        'records':records,'mappings':mappings}


def bind_scenes(entry,ancestry,plan):
    result=[];keys=set(ancestry)
    evidence=' '.join(str(entry.get(k) or '') for k in ('name','display_name','model')).casefold()
    for scene in plan['records']:
        stages=[]
        for req in scene['requirements']:
            if not keys.intersection(req['category_ids']):continue
            if req.get('name_requires') and req['name_requires'] not in evidence:continue
            if req['stage'] not in stages:stages.append(req['stage'])
        for stage in stages:result.append({'domain':scene['domain'],'scene':scene['name'],'scene_id':scene['id'],'stage':stage})
    return result


def binding_matches(binding,args):
    return all(not args.get(key) or args[key]==binding[key] for key in ('domain','scene','stage'))


def has_os_module(value):
    return isinstance(value,str) and value.strip().casefold() not in ('','none','null','nan')


if __name__=='__main__':
    import argparse,json,shutil
    from unified import encoded,atomic
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--root',default='D:/Asset_library')
    args=parser.parse_args();root=Path(args.root)/'统一目录'
    nodes=json.loads((root/'分类/category-index.json').read_text('utf8'))['canonical_nodes']
    plan=compile_sheet(args.source,nodes)
    archive=root/'分类/scene-sources';archive.mkdir(exist_ok=True)
    shutil.copy2(args.source,archive/(plan['source_sha256']+'.xlsx'))
    atomic(root/'分类/scene-index.json',encoded(plan))
    print(json.dumps({'scenes':len(plan['records']),'requirements':len(plan['mappings']),'next':'unified.py reindex'},ensure_ascii=False))
