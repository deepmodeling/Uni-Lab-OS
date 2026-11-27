"""Unified public records on the existing asset filesystem."""
import argparse
import copy
import hashlib
import json
import os
import uuid
from collections import Counter
from pathlib import Path

from catalog import inside

FIELDS = ('uuid','name','display_name','resource_type','tags','manufacturer_id',
          'description','cover','device_params','model','module','source_registry','relations')
RELATION_TYPES = ('related', 'accessory_for', 'compatible_with')
RESOURCE_TYPES = ('component','mechanism','device','resource','workcell','tool')
TYPE_LABELS = dict(zip(RESOURCE_TYPES, ('零件','机构','设备','耗材与资源','工站','工具')))
TYPE_MAP = {**{t:t for t in RESOURCE_TYPES}, 'equipment':'device',
            'workpiece_consumable':'resource', 'station_template':'workcell'}
SCHEMA = 'unilab.unified-catalog/1'
NAMESPACE = uuid.UUID('50cd37a7-7d90-51c9-9472-6153bf81f13c')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))+'\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_bytes(data)
    os.replace(temp,path)


def resource_type(value):
    if value not in TYPE_MAP:
        raise ValueError(f'未映射 resource_type: {value!r}')
    return TYPE_MAP[value]


def decoded(value, field, errors):
    if not isinstance(value,str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        errors.append(field)
        return None


def validate(entry):
    if set(entry)!=set(FIELDS):
        raise ValueError('条目必须恰好包含 13 个统一字段')
    uuid.UUID(entry['uuid'])
    if entry['resource_type'] not in RESOURCE_TYPES:
        raise ValueError('不支持的 resource_type')
    if not isinstance(entry['tags'],list) or not all(isinstance(t,str) for t in entry['tags']):
        raise ValueError('tags 必须为字符串列表')
    for field in ('name','display_name'):
        if not isinstance(entry[field],str) or not entry[field].strip():
            raise ValueError(f'{field} 不能为空')
    if not isinstance(entry['relations'], list):
        raise ValueError('relations 必须为列表')
    seen=set()
    for relation in entry['relations']:
        if not isinstance(relation,dict) or not {'target_uuid','type'} <= set(relation) or set(relation)-{'target_uuid','type','note'}:
            raise ValueError('关联需要 target_uuid、type，允许 note')
        uuid.UUID(relation['target_uuid'])
        pair=(relation['target_uuid'],relation['type'])
        if relation['type'] not in RELATION_TYPES or pair in seen or relation['target_uuid']==entry['uuid']:
            raise ValueError('关联类型错误、重复或自关联')
        if 'note' in relation and not isinstance(relation['note'],str):
            raise ValueError('关联 note 必须为字符串')
        seen.add(pair)


def from_table(fields):
    errors=[]
    e={key:fields.get(key) for key in FIELDS}
    e['name']=fields['name']
    e['display_name']=fields.get('display_name') or fields['name']
    e['resource_type']=resource_type(fields['resource_type'])
    for key in ('model','device_params','source_registry','tags'):
        e[key]=decoded(fields.get(key),key,errors)
    if isinstance(e['tags'],str):
        e['tags']=[e['tags']]
    e['tags']=e['tags'] or []
    e['cover']=fields.get('cover') or fields.get('icon')
    e['relations']=decoded(fields.get('relations',[]),'relations',errors)
    validate(e)
    return e,errors


def from_asset(record, display, adapter, presentation):
    manufacturer=record.get('manufacturer',{})
    model=copy.deepcopy(record.get('model') or {})
    if not isinstance(model,dict):
        raise ValueError('工程 model 必须是对象；拒绝覆盖原值')
    if 'targets' in model:
        raise ValueError('工程 model 已有 targets，需核对后迁移')
    model['targets']=copy.deepcopy(record['targets'])
    thumb=presentation.get('thumbnail',{})
    valid_thumb=thumb.get('source_package_digest')==record['targets'][0].get('package_digest')
    e=dict(zip(FIELDS,[record['catalog_id'],record.get('original_name') or record['title'],
        record['title'],resource_type(record['reuse_kind']),
        list(dict.fromkeys(record.get('tags',[])+[display['major'],display['category']]+display['scenes'])),
        manufacturer.get('id') if manufacturer.get('status')!='unknown' and manufacturer.get('name')!='未知厂家' else None,
        display['summary'],thumb.get('path') if valid_thumb else None,
        adapter.get('parameters',{}),model,adapter.get('module'),adapter.get('source_registry'),[]]))
    validate(e)
    return e


def from_web(model, display, occurrence):
    identity=json.dumps([model['v'],model['m'],model['t'],occurrence],ensure_ascii=False)
    e=dict(zip(FIELDS,[str(uuid.uuid5(NAMESPACE,identity)),model['m'],model['m'],'device',
        list(dict.fromkeys(display['tags']+display['scenes'])),
        'name:'+model['v'] if model.get('v') and model['v']!='未知厂家' else None,
        model.get('u'),model.get('img'),{}, {'name':model['m']},None,None,[]]))
    validate(e)
    return e


def project(entry, previous):
    """UI fields are derived; full model/parameters/registry stay in the entry file."""
    row={k:v for k,v in previous.items() if k not in ('_search','reuse_kind','resource_type','module')}
    row.update(id=entry['uuid'],uuid=entry['uuid'],title=entry['display_name'],
               resource_type=entry['resource_type'],tags=entry['tags'],
               description=entry.get('description') or '',module=entry.get('module'),
               square='consumables' if entry['resource_type']=='resource' else 'equipment')
    if previous.get('manufacturer_id',entry['manufacturer_id'])!=entry['manufacturer_id']:
        params=entry.get('device_params') or {}
        row['manufacturer']=(params.get('品牌') if isinstance(params,dict) else None) or (
            entry['manufacturer_id'][5:] if isinstance(entry['manufacturer_id'],str) and entry['manufacturer_id'].startswith('name:') else '未知厂家')
    row['manufacturer_id']=entry['manufacturer_id']
    return row


def migrate(root, destination=None):
    from catalog import Catalog
    from portal import Portal
    root=Path(root).resolve();dest=Path(destination).resolve() if destination else root/'统一目录'
    if dest==root:
        raise ValueError('统一目录必须独立于原 library.json 所在目录')
    cat=Catalog(root);portal=Portal(cat,use_unified=False);portal.load();snap=cat.load()
    entries={};rows=[];origins=[];aliases={};occurrences=Counter();bad=Counter()
    for row in portal.entries:
        old_id=row['id'];errors=[]
        if old_id.startswith('asset:'):
            record=snap['records'][row['asset_id']]
            adapter_path=inside(root,record['targets'][0]['package_path']+'/simready_usd/unilab.json')
            adapter=read(adapter_path) if adapter_path.is_file() else {}
            # Existing square projection carries the exact taxonomy and thumbnail binding.
            display={**row,'major':row['majors'][0],'category':row['types'][0],'summary':row['description']}
            entry=from_asset(record,display,adapter,snap['data'].get('presentation',{}).get(row['asset_id'],{}))
            origin={'source':'工程资产库','record':record,'library_sha256':snap['digest']}
        elif old_id.startswith('web:'):
            model=portal.raw[old_id];key=(model['v'],model['m'],model['t'])
            entry=from_web(model,row,occurrences[key]);occurrences[key]+=1
            origin={'source':'index.html MODELS','data':model}
        else:
            origin=portal.detail(old_id)
            entry,errors=from_table(origin['fields'])
        if entry['uuid'] in entries:
            raise ValueError('身份冲突，拒绝自动合并: '+entry['uuid'])
        entries[entry['uuid']]=entry
        aliases[old_id]=entry['uuid'];aliases[entry['uuid']]=entry['uuid']
        origins.append((entry['uuid'],{'legacy_id':old_id,'parse_errors':errors,'original':origin}))
        bad.update(errors);rows.append(project(entry,row))
    for alias,target in snap['data'].get('catalog_aliases',{}).items():
        if isinstance(target,str) and 'asset:'+target in aliases:
            aliases.setdefault('asset:'+alias,aliases['asset:'+target])
            aliases.setdefault(alias,aliases['asset:'+target])
    from deduplicate import DECISIONS, merge_records
    decisions=read(DECISIONS) if DECISIONS.is_file() else None
    if decisions:
        entries,rows,origins,aliases,_=merge_records(entries,rows,origins,aliases,decisions)
    # Check every destination before writing any entry; preserve human edits.
    for key,entry in entries.items():
        path=dest/'条目'/f'{key}.json'
        if path.exists() and path.read_bytes()!=encoded(entry):
            raise ValueError('拒绝覆盖已有不同条目，请使用 reindex 或新迁移目录: '+str(path))
    data=bytearray();offsets={}
    for key,origin in origins:
        b=encoded(origin);offsets[key]=[len(data),len(b)];data.extend(b)
    if digest((root/'library.json').read_bytes())!=snap['digest']:
        raise ValueError('迁移期间源索引变化，请重试')
    for key,entry in entries.items():
        path=dest/'条目'/f'{key}.json'
        if not path.exists():atomic(path,encoded(entry))
    atomic(dest/'sources.jsonl',bytes(data));atomic(dest/'tags.json',encoded(portal.source['tags']))
    index={'schema':SCHEMA,'entries':rows,'aliases':aliases,'source_offsets':offsets,
           'types':portal.types,'source_library_digest':snap['digest'],
           'files':{k:{'path':f'条目/{k}.json','sha256':digest(encoded(e))} for k,e in entries.items()}}
    if decisions:index['dedup_decisions_sha256']=digest(encoded(decisions))
    receipt={'schema':SCHEMA,'records':len(entries),'resource_types':dict(Counter(e['resource_type'] for e in entries.values())),
             'source_counts':dict(Counter(s for r in rows for s in r.get('sources',[r['source']]))),'parse_errors':dict(bad),
             'source_library_sha256':snap['digest'],'source_hashes':portal.source['sources'],
             'converter_sha256':digest(Path(__file__).read_bytes()),'sources_sha256':digest(bytes(data)),
             'type_mapping':TYPE_MAP,'oss_published':False}
    atomic(dest/'migration.json',encoded(receipt))
    atomic(dest/'library.json',encoded(index))  # Discoverable only after all entries exist.
    return receipt


class UnifiedStore:
    def __init__(self,path):
        self.root=Path(path).resolve();self.stamp=None

    def load(self):
        path=self.root/'library.json';stat=path.stat();stamp=(stat.st_mtime_ns,stat.st_size)
        if stamp!=self.stamp:
            data=path.read_bytes();index=json.loads(data)
            if index.get('schema')!=SCHEMA:raise ValueError('不支持的统一目录格式')
            self.index=index;self.tags=index['tag_definitions'] if 'tag_definitions' in index else read(self.root/'tags.json');self.digest=digest(data);self.stamp=stamp
        return self.index

    def key(self,value):
        index=self.load();key=index['aliases'].get(value,value)
        if key not in index['files']:raise KeyError(value)
        return key

    def entry(self,value):
        key=self.key(value);ref=self.index['files'][key];data=inside(self.root,ref['path']).read_bytes()
        if digest(data)!=ref['sha256']:raise ValueError('条目已修改，请运行 unified.py reindex')
        e=json.loads(data);validate(e);return e

    def source(self,value):
        key=self.key(value)
        if key not in self.index['source_offsets']:
            result={'source':'统一目录新增条目','uuid':key}
        else:
            offset,size=self.index['source_offsets'][key]
            with (self.root/'sources.jsonl').open('rb') as f:
                f.seek(offset);result=json.loads(f.read(size))
        if key in self.index.get('backfill_offsets',{}):
            offset,size=self.index['backfill_offsets'][key]
            with (self.root/'backfill-mappings.jsonl').open('rb') as f:
                f.seek(offset);result['catalog_backfill']=json.loads(f.read(size))
        return result

    def reference(self,value,version=None):
        entry=self.entry(value);model=entry.get('model') or {}
        targets=model.get('targets',[]) if isinstance(model,dict) else []
        targets=[t for t in targets if not version or t.get('package_digest')==version]
        if not targets:raise KeyError('该条目没有对应的工程模型包')
        return {'catalog_id':entry['uuid'],**targets[0]}


def reindex(destination):
    store=UnifiedStore(destination);index=copy.deepcopy(store.load());rows=[];files={}
    if (store.root/'分类/category-index.json').is_file():
        from catalog_dictionary import rebuild
        return rebuild(store.root,index)
    previous={r['uuid']:r for r in index['entries']}
    for path in sorted((store.root/'条目').glob('*.json')):
        data=path.read_bytes();entry=json.loads(data);validate(entry);key=entry['uuid']
        if path.stem!=key:raise ValueError('文件名必须与 uuid 相同，不通过编辑字段更改身份')
        old=previous.get(key)
        if old is None:
            old={'source':'统一目录','manufacturer':'未知厂家','manufacturer_id':None,
                 'status':'registered','image':None,'image_note':'','tags':[],
                 'types':[],'majors':[],'domains':[],'stages':[],'scenes':[]}
        row=project(entry,old)
        names=[t for t in entry['tags'] if t in index['types']]
        if names:
            row['types']=names
            for output,source in [('majors','major'),('domains','d'),('stages','s')]:
                values=[index['types'][t][source] for t in names]
                row[output]=list(dict.fromkeys(v for value in values for v in (value if isinstance(value,list) else [value])))
        elif not old['types'] or entry['tags']!=old['tags']:
            name=TYPE_LABELS[entry['resource_type']]
            major='工件与耗材' if entry['resource_type']=='resource' else '自动化机械与物流'
            index['types'].setdefault(name,{'n':name,'major':major,'d':['待归类'],'s':['待归类'],'p':'','st':'unknown'})
            row.update(types=[name],majors=[major],domains=['待归类'],stages=['待归类'])
        rows.append(row);files[key]={'path':path.relative_to(store.root).as_posix(),'sha256':digest(data)}
        index['aliases'][key]=key
    index['entries']=rows;index['files']=files
    atomic(store.root/'library.json',encoded(index))
    return {'records':len(rows),'resource_types':dict(Counter(r['resource_type'] for r in rows))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['migrate','reindex'])
    parser.add_argument('--root',default='D:/Asset_library')
    parser.add_argument('--output',help='默认在资产库根的 统一目录；可指定候选输出目录')
    args=parser.parse_args()
    result=migrate(args.root,args.output) if args.command=='migrate' else reindex(args.output or Path(args.root)/'统一目录')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
