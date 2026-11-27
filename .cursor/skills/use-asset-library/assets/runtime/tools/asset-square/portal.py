"""Unified discovery records; identities and qualification remain source-scoped."""
import json,hashlib,threading
from collections import Counter,defaultdict
from pathlib import Path
from catalog import inside
from unified import UnifiedStore, RESOURCE_TYPES
from discovery_fields import STAGES, canonical_stage
from scene_catalog import binding_matches

DATA=Path(__file__).parent/'data'
DOMAINS=['生物','化学','环境','材料','物理','自动化机械与物流','耗材与工件','待归类']
LABELS={'experimental_domain':'实验范式','experimental_step':'实验阶段','experimental_scene':'实验场景','device_template_tag':'历史设备类型','canonical_category':'规范分类'}
CONSUMABLES={'plates':'孔板','plate':'孔板','tube_racks':'管架','tip_racks':'吸头架','tip_rack':'吸头架','reservoirs':'储液槽','bottles':'瓶与容器','bottle_carriers':'瓶托架','plate_adapters':'孔板适配器','deck':'台面与载台','flasks':'烧瓶','reactors':'反应容器','tip_boxes':'吸头盒'}

def type_matrix(rows, types, args=None):
    """Count matching records per cell; don't cross-join category metadata."""
    cells=defaultdict(Counter);assigned=defaultdict(set)
    for row in rows:
        if row.get('resource_type') not in (None,'device'):continue
        if 'scene_bindings' in row:
            bindings=row['scene_bindings'] or [{'domain':'待归类','scene':'','stage':'待归类'}]
            coords={(b['domain'],b['stage']) for b in bindings if binding_matches(b,args or {})}
        else:coords={(d,s) for d in row['domains'] for s in row['stages']}
        for domain,stage in coords:
                assigned[(domain,stage)].update(row['types'])
                for key in set(row.get('category_ancestors',row['types'])):
                    if key in types:cells[(domain,stage)][key]+=1
    return [{'domain':d,'stage':s,'types':[{'id':k,'count':counts[k]} for k in sorted(assigned[(d,s)]) if k in types]}
        for (d,s),counts in sorted(cells.items())]


class Portal:
    def __init__(self,catalog,use_unified=True):
        self.catalog=catalog
        unified_root=catalog.root/'统一目录'
        self.store=UnifiedStore(unified_root) if use_unified and (unified_root/'library.json').is_file() else None
        source_path=DATA/'portal-source.json'
        if source_path.is_file():
            self.source=json.loads(source_path.read_text(encoding='utf-8'))
        elif self.store:
            # A current-library consumer does not need the original migration dataset.
            self.source={'source_root':str(unified_root),'tags':[],'web':{'EQUIP':[],'MODELS':[]},'rows':[],'raw_index':{}}
        else:
            raise FileNotFoundError('需要统一目录/library.json；旧目录模式还需要 data/portal-source.json')
        self.root=Path(self.source['source_root']).resolve();self.lock=threading.RLock();self.digest=None
    def load(self):
        with self.lock:
            if self.store:
                index=self.store.load()
                engineering=self.catalog.load()
                if self.digest==self.store.digest and getattr(self,'catalog_digest',None)==engineering['digest']:return
                self.catalog_digest=engineering['digest']
                project_topics=defaultdict(set)
                for record in engineering['records'].values():
                    owner=record.get('owner')
                    if not owner:continue
                    key=index.get('aliases',{}).get(record['catalog_id'],record['catalog_id'])
                    project_topics[key].add('项目 · '+owner)
                self.source['tags']=self.store.tags
                self.entries=[dict(r) for r in index['entries']]
                canonical=bool(index.get('dictionaries'))
                aliases_by_id=defaultdict(list)
                for alias,target in index.get('aliases',{}).items():aliases_by_id[target].append(alias)
                for r in self.entries:
                    r['topics']=sorted(set('场景 · '+s for s in r.get('scenes',[]))|project_topics[r['id']])
                    if not canonical:r['tag_ids']=[t['key'] for t in self.store.tags if t['name'] in r['tags']]
                    r['_search']=(' '.join(str(r.get(k,'')) for k in ['id','title','name','manufacturer','manufacturer_aliases','types','category_paths','tag_labels','tags','scenes','description','search_aliases'])+' '+' '.join(aliases_by_id[r['id']])).casefold()
                self.byid={r['id']:r for r in self.entries};self.types=index['types'];self.digest=self.store.digest
                self.updated=str((self.store.root/'library.json').stat().st_mtime)
                return
            snap=self.catalog.load()
            if self.digest==snap['digest']:return
            src=self.source;types={t['n']:{**t,'major':t['cl'],'tags':[]} for t in src['web']['EQUIP']};tags=src['tags'];byname=defaultdict(list)
            for t in tags:byname[t['name']].append(t)
            for t in tags:
                if t['type']=='device_template_tag' and t['name'] not in types:types[t['name']]={'n':t['name'],'c':'','cl':'历史设备分类','major':'历史设备分类','d':['待归类'],'s':['待归类'],'p':'历史分类，领域与阶段尚未映射','st':'unknown','tags':[t['key']]}
            entries=[];raw={}
            def add(row,detail):
                row.setdefault('tags',[]);row.setdefault('scenes',[]);row.setdefault('domains',[]);row.setdefault('stages',[])
                row['tag_ids']=list(dict.fromkeys(t['key'] for label in row['tags'] for t in byname[label]))
                row['_search']=' '.join(str(row.get(k,'')) for k in ['title','manufacturer','types','tags','scenes','description']).casefold();entries.append(row);raw[row['id']]=detail
            for i,m in enumerate(src['web']['MODELS']):
                typ=types[m['t']];labels=[m['t'],*typ['d'],*m.get('p',[]),*typ['s'],*(x+'设备' for x in typ['s'])]
                add({'id':'web:'+str(i),'source':'原设备页','square':'equipment','title':m['m'],'manufacturer':m['v'],'types':[m['t']],'majors':[typ['major']],'domains':typ['d'],'stages':typ['s'],'tags':labels,'scenes':m.get('p',[])+m.get('pj',[]),'status':m['st'],'description':m['u'],'image':'/reference/'+m['img'] if m.get('img','').startswith('images/') else None,'image_note':m.get('imgNote',''),'simulation':m.get('sim',''),'evidence':m.get('ev','')},m)
            for r in src['rows']:
                square='consumables' if r['resource_type']=='resource' else 'equipment'
                labels=r['tags'];names=[n for n in labels if n in types]
                if square=='consumables':names=[CONSUMABLES[x] for x in labels if x in CONSUMABLES] or ['其他资源与耗材']
                if not names:names=['历史未分类设备']
                for n in names:
                    if n not in types:types[n]={'n':n,'major':'工件与耗材' if square=='consumables' else '历史设备分类','cl':'','d':['耗材与工件' if square=='consumables' else '待归类'],'s':['待归类'],'p':'原始来源分类','st':'unknown'}
                ts=[types[n] for n in names];ds=list(dict.fromkeys(x for t in ts for x in t['d']));ss=list(dict.fromkeys(x for t in ts for x in t['s']))
                explicit_stages=[x['name'].removesuffix('设备') for label in labels for x in byname[label] if x['type']=='experimental_step']
                if explicit_stages:ss=list(dict.fromkeys(explicit_stages))
                scene_text=json.dumps(r.get('scene'),ensure_ascii=False);scenes=[t['name'] for t in tags if t['type'] in ('experimental_scene','experimental_domain') and (t['name'] in labels or t['name'] in scene_text)]
                add({'id':r['id'],'source':r['source'],'square':square,'title':r['title'],'manufacturer':r['manufacturer'],'types':names,'majors':list(dict.fromkeys(t['major'] for t in ts)),'domains':ds,'stages':ss,'tags':labels+scenes,'scenes':scenes,'status':'registered','description':r['description'],'image':'/reference/'+r['image'] if r['image'] else None,'module':r['module'],'parse_errors':r['parse_errors'],'resource_type':r['resource_type']},None)
            for r in snap['rows']:
                tname=r['category'];major=r['major'];d=['耗材与工件'] if r['square']=='consumables' else ['自动化机械与物流'] if major=='自动化机械与物流' else ['待归类'];stage=['物流/机械'] if major=='自动化机械与物流' else ['待归类']
                if tname not in types:types[tname]={'n':tname,'major':major,'cl':major,'d':d,'s':stage,'p':'当前工程资产库','st':'unknown'}
                original=snap['records'][r['id']]
                add({'id':'asset:'+r['id'],'asset_id':r['id'],'source':'工程资产库','square':r['square'],'title':r['title'],'manufacturer':r['manufacturer'],'types':[tname],'majors':[major],'domains':d,'stages':stage,'tags':original.get('tags',[])+[tname],'scenes':r['scenes'],'status':'asset','description':r['summary'],'image':r['thumbnail'],'image_note':'当前版本几何预览','configuration':r['configuration'],'has_cad':r['has_cad']},None)
            for row in entries:
                for name in row['types']:
                    for field,source in [('d','domains'),('s','stages')]:types[name][field]=list(dict.fromkeys(types[name][field]+row[source]))
            self.entries=entries;self.byid={r['id']:r for r in entries};self.raw=raw;self.types=types;self.digest=snap['digest'];self.updated=snap['updated']
    def matching(self,args):
        self.load();square=args.get('square','equipment');q=args.get('q','').casefold().split();out=[]
        args=dict(args)
        if args.get('stage'):args['stage']=canonical_stage(args['stage'])
        if self.store and args.get('tag'):args['tag']=self.store.index.get('facet_aliases',{}).get(args['tag'],args['tag'])
        for r in self.entries:
            if square!='all' and r['square']!=square:continue
            if args.get('topic') and args['topic'] not in r.get('topics',r.get('scenes',[])):continue
            if args.get('type') and args['type'] not in r.get('category_ancestors',r['types']) and not any(self.types[t]['n']==args['type'] for t in r['types']):continue
            if any(args.get(k) and args[k] not in r[f] for k,f in [('major','majors'),('tag','tag_ids')]):continue
            if 'scene_bindings' in r:
                bindings=r['scene_bindings'] or [{'domain':'待归类','scene':'','stage':'待归类'}]
                if not any(binding_matches(b,args) for b in bindings):continue
            elif any(args.get(k) and args[k] not in r[f] for k,f in [('domain','domains'),('stage','stages'),('scene','scenes')]):continue
            if args.get('classification_status') and args['classification_status']!=r.get('classification_status'):continue
            if args.get('has_3d_model') in ('true','false') and bool(r.get('has_3d_model'))!=(args['has_3d_model']=='true'):continue
            if args.get('has_os_module') in ('true','false') and bool(r.get('has_os_module'))!=(args['has_os_module']=='true'):continue
            if args.get('related_to') and args['related_to'] not in r.get('related_ids',[]):continue
            if args.get('source') and args['source'] not in r.get('sources',[r['source']]):continue
            maker_filter=args.get('manufacturer')
            if maker_filter=='__unknown__':
                if r.get('manufacturer_id') is not None:continue
            elif maker_filter and maker_filter not in (r.get('manufacturer_id'),r.get('manufacturer')):continue
            if any(args.get(k) and args[k]!=r.get(f) for k,f in [('status','status'),('resource_type','resource_type')]):continue
            if not all(term in r['_search'] for term in q):continue
            out.append(r)
        return out
    def filter_options(self,args,rows):
        """切换某一轴时仅忽略该轴；结果集仍使用全部条件。"""
        def candidates(*keys):
            return self.matching({k:v for k,v in args.items() if k not in keys}) if any(args.get(k) for k in keys) else rows
        category_rows=candidates('type','major')
        counts=Counter(k for r in category_rows for k in set(r.get('category_ancestors',r['types'])))
        types=[{**t,'count':counts[k]} for k,t in self.types.items() if counts[k]]
        facets={}
        for key,field in [('major','majors'),('source','sources'),('manufacturer','manufacturer'),
                          ('status','status'),('classification_status','classification_status'),
                          ('has_3d_model','has_3d_model'),('has_os_module','has_os_module')]:
            selected=category_rows if key=='major' else candidates(key)
            if key=='manufacturer' and self.store and self.store.index.get('dictionaries'):
                makers={r.get('manufacturer_id') or '__unknown__':r['manufacturer'] for r in selected}
                facets[key]=[{'value':k,'text':v} for k,v in sorted(makers.items(),key=lambda x:x[1].casefold())]
            elif key in ('has_3d_model','has_os_module'):
                facets[key]=sorted({'true' if r.get(field) else 'false' for r in selected})
            else:
                values={v for r in selected for v in (r.get(field,[r['source']]) if key=='source' else r[field] if key=='major' else [r.get(field)]) if v is not None}
                facets[key]=sorted(values)
        if self.store and self.store.index.get('dictionaries'):
            facets['major']=[{'value':k,'text':' / '.join(self.types[k]['path'])} for k in facets['major']]
        for key,field in [('domain','domains'),('scene','scenes'),('stage','stages')]:
            relaxed={k:v for k,v in args.items() if k!=key}
            values=set()
            for r in candidates(key):
                if 'scene_bindings' in r:
                    for b in r['scene_bindings'] or [{'domain':'待归类','scene':'','stage':'待归类'}]:
                        if b.get(key) and binding_matches(b,relaxed):values.add(b[key])
                else:values.update(r.get(field,[]))
            facets[key]=sorted(values)
        tag_counts=Counter(k for r in candidates('tag') for k in set(r['tag_ids']))
        tags=[{**t,'label':LABELS.get(t['type'],t['type']),'count':tag_counts[t['key']]} for t in self.source['tags'] if tag_counts[t['key']]]
        return {'types':types,'facets':facets,'tags':tags,
                'category_scope':{'total':len(category_rows),'classified':sum(bool(r['types']) for r in category_rows),'unclassified':sum(not r['types'] for r in category_rows)}}

    def summary(self,args):
        self.load();base=self.matching({'square':args.get('square','equipment')});rows=self.matching(args);cnt=Counter(n for r in rows for n in set(r.get('category_ancestors',r['types'])));basecnt=Counter(n for r in base for n in set(r.get('category_ancestors',r['types'])))
        canonical=bool(self.store and self.store.index.get('dictionaries'))
        declared={t['name'] for t in self.source['tags'] if t['type']=='device_template_tag'}|{t['n'] for t in self.source['web']['EQUIP']}
        active_types=[{**t,'count':cnt[n],'total':basecnt[n]} for n,t in self.types.items() if (canonical and (args.get('square')=='all' or (t['resource_type']=='resource')==(args.get('square','equipment')=='consumables'))) or (not canonical and (basecnt[n] or args.get('square','equipment')=='equipment' and n in declared))]
        facets={k:sorted({v for r in base for v in r[f]}) for k,f in [('major','majors'),('domain','domains'),('stage','stages'),('scene','scenes')]}
        facets['stage'].sort(key=lambda v:(STAGES.index(v) if v in STAGES else len(STAGES),v))
        facets['has_3d_model']=[{'value':'true','text':'含 3D 模型'},{'value':'false','text':'不含 3D 模型'}]
        for k in ['manufacturer','status']:facets[k]=sorted({r[k] for r in base})
        if canonical:
            makers={r['manufacturer_id']:r['manufacturer'] for r in base if r.get('manufacturer_id')}
            facets['manufacturer']=[{'value':k,'text':v} for k,v in sorted(makers.items(),key=lambda x:x[1].casefold())]
            if any(r.get('manufacturer_id') is None for r in base):facets['manufacturer'].append({'value':'__unknown__','text':'未知厂家'})
            facets['major']=[{'value':k,'text':' / '.join(self.types[k]['path'])} for k in facets['major']]
        facets['source']=sorted({s for r in base for s in r.get('sources',[r['source']])})
        facets['resource_type']=list(RESOURCE_TYPES)
        plan=self.store.index.get('scene_plan') if self.store else None
        if plan:
            facets['domain']=plan['domains']
            facets['scene']=[r['name'] for r in plan['records'] if not args.get('domain') or r['domain']==args['domain']]
            facets['stage']=plan['stages']+['待归类']
        # Browsing facets describe the current result set, not the dictionary.
        active_types=[t for t in active_types if t['count']>0]
        if canonical:
            present={v for r in rows for v in r['majors']}
            facets['major']=[v for v in facets['major'] if v['value'] in present]
        tag_counts=Counter(t for r in rows for t in set(r['tag_ids']))
        tags=[{**t,'label':LABELS.get(t['type'],t['type']),'count':tag_counts[t['key']]} for t in self.source['tags'] if not canonical or not t.get('resource_type') or args.get('square')=='all' or (t['resource_type']=='resource')==(args.get('square','equipment')=='consumables')]
        matrix=type_matrix(rows,self.types,args)
        topics=[{'name':name,'count':count} for name,count in sorted(Counter(s for r in rows for s in set(r.get('topics',r.get('scenes',[])))).items())]
        return {'filter_options':self.filter_options(args,rows),'matched_source_counts':dict(Counter(s for r in rows for s in r.get('sources',[r['source']]))),'topics':topics,'matrix':matrix,'matched_unclassified':sum(not r['types'] for r in rows),'matched_type_count':len({t['id'] for cell in matrix for t in cell['types']}),'total':len(rows),'base_total':len(base),'counts':dict(Counter(s for r in base for s in r.get('sources',[r['source']]))),'all_counts':dict(Counter(s for r in self.entries for s in r.get('sources',[r['source']]))),'types':active_types,'facets':facets,'tags':tags,'domains':facets['domain'] if canonical else DOMAINS,'stages':facets['stage'] if canonical else STAGES,'updated':self.updated,'library_digest':self.digest,'all_total':len(self.entries),'catalog_mode':'unified' if self.store else 'legacy','canonical_taxonomy':canonical,'unclassified':sum(not r['types'] for r in base),'resource_type_counts':dict(Counter(r.get('resource_type','') for r in self.entries))}
    def query(self,args):
        rows=self.matching(args);size=min(96,max(1,int(args.get('page_size','24'))));pages=max(1,(len(rows)+size-1)//size);page=min(pages,max(1,int(args.get('page','1'))))
        return {'total':len(rows),'page':page,'pages':pages,'entries':[{k:v for k,v in r.items() if k!='_search'} for r in rows[(page-1)*size:page*size]]}
    def detail(self,key):
        self.load()
        if self.store:return self.store.source(key)
        if key not in self.byid:raise KeyError(key)
        if key.startswith('asset:'):return self.catalog.detail(key[6:])
        if key.startswith('web:'):return {'source':'index.html MODELS','data':self.raw[key]}
        offset,size=self.source['raw_index'][key]
        with (DATA/'raw-rows.jsonl').open('rb') as f:f.seek(offset);data=json.loads(f.read(size))
        return {**data,'parse_errors':self.byid[key].get('parse_errors',[]),'note':'原始单元格按导出值保留，截断 JSON 未修补'}
