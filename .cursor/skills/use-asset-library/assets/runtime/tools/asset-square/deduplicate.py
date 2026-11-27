"""Conservative, auditable deduplication of the original web and selected XLSX."""
import argparse
import copy
import json
import re
import unicodedata
import shutil
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path

from unified import UnifiedStore, encoded, atomic, digest, validate

WEB='原设备页'
SHEET='执行结果1 2.xlsx'
DECISIONS=Path(__file__).parent/'data'/'dedup-decisions.json'


def normalized(value):
    return re.sub(r'[\s_–—-]+','',unicodedata.normalize('NFKC',str(value or '')).casefold())


def vendors(value):
    if not value or value=='未知厂家':return set()
    value=unicodedata.normalize('NFKC',value)
    return {normalized(v) for v in [value,*re.split(r'[()]',value)] if len(normalized(v))>=2}


def codes(value):
    text=unicodedata.normalize('NFKC',value).casefold()
    return {normalized(t) for t in re.findall(r'[a-z0-9]+(?:[-_./+][a-z0-9]+)*',text)
            if any(c.isdigit() for c in t)}


def bounded(short,long):
    # Preserve word boundaries while ignoring spaces/hyphens *inside* a model.
    text=unicodedata.normalize('NFKC',long).casefold()
    positions=[i for i,c in enumerate(text) if not re.match(r'[\s_–—-]',c)]
    compact=''.join(text[i] for i in positions);needle=normalized(short)
    start=compact.find(needle)
    while needle and start>=0:
        first,last=positions[start],positions[start+len(needle)-1]
        before=text[first-1:first] if first else ''
        after=text[last+1:last+2]
        if not (before and before.isascii() and before.isalnum()) and not (after and after.isascii() and after.isalnum()):return True
        start=compact.find(needle,start+1)
    return False


def reason(a,b):
    av,bv=a['_vendors'],b['_vendors'];an,bn=a['_name'],b['_name']
    if av and bv and not av&bv:return None,'厂家冲突'
    same_vendor=bool(av&bv)
    vendor_in_title=any(len(v)>=3 and v in bn for v in av)
    same_codes=a['_codes']==b['_codes'] and bool(a['_codes'])
    if an==bn and len(an)>=5:
        if same_vendor or same_codes and (len(an)>=10 or set(a['types'])&set(b['types'])):
            return '完整名称一致',None
    if len(an)>=4 and bounded(a['title'],b['title']) and a['_codes']==b['_codes'] and (same_vendor or vendor_in_title):
        return '完整型号边界一致且厂家有依据',None
    # A code alone can identify both a detector and its readout subsystem.
    # Translation/abbreviation matches remain explicit review candidates.
    return None,'证据不足或型号后缀不同'


def plan(store):
    index=store.load();rows=copy.deepcopy(index['entries']);entries={r['id']:store.entry(r['id']) for r in rows if r['source'] in (WEB,SHEET)}
    by_manufacturer=defaultdict(set)
    for r in rows:
        if r['source']==SHEET:
            mid=entries[r['id']]['manufacturer_id']
            if mid and r['manufacturer']!='未知厂家':by_manufacturer[mid].add(r['manufacturer'])
    for r in rows:
        r['_name']=normalized(r['title']);r['_codes']=codes(r['title']);r['_vendors']=vendors(r['manufacturer'])
        if r['source']==SHEET and not r['_vendors']:
            names=by_manufacturer.get(entries[r['id']]['manufacturer_id'],set())
            if len(names)==1:r['_vendors']=vendors(next(iter(names)))
    web=[r for r in rows if r['source']==WEB]
    sheet=[r for r in rows if r['source']==SHEET and r['resource_type']=='device']
    edges=[];review=[];byid={r['id']:r for r in rows}
    for a in web:
        for b in sheet:
            if not (a['_name']==b['_name'] or a['_name'] in b['_name'] or a['_codes'] and a['_codes']==b['_codes']):continue
            why,problem=reason(a,b)
            if why:edges.append({'web':a['id'],'sheet':b['id'],'reason':why})
            else:review.append({'web':a['id'],'sheet':b['id'],'web_name':a['title'],'sheet_name':b['title'],'reason':problem})
    parent={}
    def find(x):
        parent.setdefault(x,x)
        if parent[x]!=x:parent[x]=find(parent[x])
        return parent[x]
    for edge in edges:parent[find(edge['web'])]=find(edge['sheet'])
    groups=defaultdict(list)
    for key in list(parent):groups[find(key)].append(key)
    decisions=[]
    for members in groups.values():
        sheets=[k for k in members if byid[k]['source']==SHEET]
        modules={entries[k]['module'] for k in sheets if entries[k]['module']}
        if len(modules)>1:
            review.append({'members':members,'names':[byid[k]['title'] for k in members],'reason':'表格驱动入口冲突，保留分开条目'})
            continue
        # A connected component must not bridge conflicting models/vendors.
        ws=[k for k in members if byid[k]['source']==WEB]
        if any(reason(byid[a],byid[b])[0] is None for a in ws for b in sheets):
            review.append({'members':members,'reason':'传递匹配存在歧义，保留分开条目'})
            continue
        winner=max(sheets,key=lambda k:(bool(entries[k]['source_registry']),bool(entries[k]['module']),bool(entries[k]['device_params']),len(encoded(entries[k])),k))
        decisions.append({'keep':winner,'merge':[k for k in members if k!=winner],
                          'names':[byid[k]['title'] for k in members],
                          'original_sha256':{k:digest(encoded(entries[k])) for k in members},
                          'evidence':[e for e in edges if e['web'] in members and e['sheet'] in members]})
    return {'schema':'unilab.catalog-dedup/1','scope':[WEB,SHEET],'catalog_sha256':store.digest,'groups':decisions,'review':review,
            'summary':{'groups':len(decisions),'removed':sum(len(g['merge']) for g in decisions),'review_candidates':len(review)}}


def merge_records(entries,rows,origins,aliases,decisions):
    """Shared by first migration and current-directory application; old IDs resolve."""
    byid={r['id']:r for r in rows};originmap=dict(origins);removed=set();conflicts=[]
    for group in decisions['groups']:
        keep=group['keep'];others=[k for k in group['merge'] if k in entries]
        if not others:continue
        if keep not in entries:raise ValueError('去重主条目缺失: '+keep)
        for key,expected in group.get('original_sha256',{}).items():
            if key in entries and digest(encoded(entries[key]))!=expected:
                legacy={k:v for k,v in entries[key].items() if k!='relations'}
                if entries[key].get('relations')!=[] or digest(encoded(legacy))!=expected:
                    raise ValueError('去重依据已变化，需重新核对: '+key)
        entry=entries[keep];row=byid[keep];members=[keep,*others]
        records=[byid[k] for k in members]
        source_records=[originmap[k] for k in members]
        for other in others:
            candidate=entries[other]
            for field,value in candidate.items():
                if field in ('uuid','name','display_name','resource_type'):continue
                if field=='tags':entry[field]=list(dict.fromkeys(entry[field]+value));continue
                if entry[field] in (None,'',{},[]):entry[field]=copy.deepcopy(value)
                elif value not in (None,'',{},[]) and entry[field]!=value:
                    conflicts.append({'keep':keep,'other':other,'field':field,'resolution':'保留表格主值，双方原值存来源'})
            removed.add(other)
        for field in ('types','majors','domains','stages','scenes'):
            row[field]=list(dict.fromkeys(v for r in records for v in r.get(field,[])))
        row['sources']=list(dict.fromkeys(s for r in records for s in r.get('sources',[r['source']])))
        row['tags']=entry['tags'];row['description']=entry['description'] or '';row['module']=entry['module']
        if row.get('manufacturer')=='未知厂家':row['manufacturer']=next((r['manufacturer'] for r in records if r['manufacturer']!='未知厂家'),'未知厂家')
        if not row.get('image'):row['image']=next((r['image'] for r in records if r.get('image')),None)
        web=[r for r in records if WEB in r.get('sources',[r['source']])]
        if web:
            preferred=next((r for r in web if r['status']=='verified'),web[0])
            for field in ('status','simulation','evidence','image_note'):
                if preferred.get(field):row[field]=preferred[field]
        originmap[keep]={'legacy_id':originmap[keep].get('legacy_id'),'merged_sources':source_records,'dedup_evidence':group['evidence']}
        for key,value in list(aliases.items()):
            if value in others:aliases[key]=keep
        for key in others:aliases[key]=keep
    for key in removed:entries.pop(key);originmap.pop(key)
    return entries,[r for r in rows if r['id'] not in removed],list(originmap.items()),aliases,conflicts


def apply(store,decisions):
    """Build and validate a complete candidate, then swap; caller stops its service."""
    index=copy.deepcopy(store.load());before_digest=store.digest
    if index.get('dedup_decisions_sha256')==digest(encoded(decisions)):
        return {'unchanged':True,'records':len(index['entries'])}
    if before_digest!=decisions['catalog_sha256']:raise ValueError('目录已改变，请重新生成去重计划')
    entries={k:store.entry(k) for k in index['files']}
    origins=[(k,store.source(k)) for k in entries]
    entries,rows,origins,aliases,conflicts=merge_records(entries,index['entries'],origins,index['aliases'],decisions)
    data=bytearray();offsets={}
    for key,origin in origins:
        b=encoded(origin);offsets[key]=[len(data),len(b)];data.extend(b)
    root=store.root;stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    stage=root.with_name(root.name+'-去重候选-'+stamp)
    archive=root.parent/'归档'/('统一目录-网页表格去重-'+stamp)
    if not stage.is_relative_to(root.parent) or not archive.is_relative_to(root.parent):raise ValueError('目录越界')
    shutil.copytree(root,stage)
    for key,entry in entries.items():
        validate(entry);body=encoded(entry)
        if digest(body)!=index['files'][key]['sha256']:atomic(stage/'条目'/f'{key}.json',body)
    removed=set(index['files'])-entries.keys()
    for key in removed:
        path=(stage/'条目'/f'{key}.json').resolve()
        if not path.is_relative_to(stage.resolve()):raise ValueError('条目路径越界')
        path.unlink()  # Only the disposable candidate; original is archived intact.
    index.update(entries=rows,aliases=aliases,source_offsets=offsets,
                 files={k:{'path':f'条目/{k}.json','sha256':digest(encoded(e))} for k,e in entries.items()},
                 dedup_decisions_sha256=digest(encoded(decisions)))
    receipt={'before':len(store.index['entries']),'after':len(rows),'removed':len(removed),
             'groups':len(decisions['groups']),'review_candidates':len(decisions.get('review',[])),
             'resource_types':dict(Counter(r['resource_type'] for r in rows)),
             'squares':dict(Counter(r['square'] for r in rows)),
             'source_counts':dict(Counter(s for r in rows for s in r.get('sources',[r['source']]))),
             'conflicts':conflicts,'archive':str(archive),'before_sha256':before_digest,
             'after_sha256':digest(encoded(index)),'oss_published':False}
    migration=json.loads((stage/'migration.json').read_text(encoding='utf8'))
    migration.update(records=len(rows),resource_types=receipt['resource_types'],source_counts=receipt['source_counts'],
                     sources_sha256=digest(bytes(data)),deduplication={k:v for k,v in receipt.items() if k!='conflicts'})
    atomic(stage/'sources.jsonl',bytes(data));atomic(stage/'migration.json',encoded(migration))
    atomic(stage/'library.json',encoded(index));atomic(stage/'deduplication.json',encoded(receipt))
    candidate=UnifiedStore(stage);candidate.load()
    for key in entries:
        candidate.entry(key);candidate.source(key)
    if set(p.stem for p in (stage/'条目').glob('*.json'))!=set(entries):raise ValueError('候选目录有额外条目')
    if any(target not in entries for target in aliases.values()):raise ValueError('别名目标缺失')
    if digest((root/'library.json').read_bytes())!=before_digest:raise ValueError('目录在生成候选期间变化')
    # Verify manual entry edits were not made while constructing the candidate.
    for key,ref in store.index['files'].items():
        if digest((root/ref['path']).read_bytes())!=ref['sha256']:raise ValueError('条目在生成候选期间变化: '+key)
    archive.mkdir(parents=True)
    root.rename(archive/'before')
    try:stage.rename(root)
    except BaseException:
        (archive/'before').rename(root)
        raise
    atomic(archive/'decisions.json',encoded(decisions));atomic(archive/'receipt.json',encoded(receipt))
    return receipt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--catalog',default='D:/Asset_library/统一目录');parser.add_argument('--plan',required=True)
    parser.add_argument('--apply',action='store_true',help='应用已有计划；请先停止读取该目录的服务')
    args=parser.parse_args()
    if args.apply:
        result=apply(UnifiedStore(args.catalog),json.loads(Path(args.plan).read_text(encoding='utf8')))
        print(json.dumps({k:v for k,v in result.items() if k!='conflicts'},ensure_ascii=False))
    else:
        result=plan(UnifiedStore(args.catalog));atomic(Path(args.plan),encoded(result));print(json.dumps(result['summary'],ensure_ascii=False))


if __name__=='__main__':main()
