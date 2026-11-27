"""Pure catalog navigation; paths use metadata values, never mutable filesystem paths."""
import json,math
KINDS={'component':'零部件','workpiece_consumable':'工件与耗材','mechanism':'机构模块','equipment':'设备','station_template':'工站与布局模板','engineering_knowledge':'工程知识与规则'}
MODES={'category':'按六类资产','manufacturer':'按厂家','source_type':'按来源类型（VC 等）','supplier_path':'厂家原始分类'}

def supplier_paths(row):
    catalog=row.get('source_catalog',{})
    trails=catalog.get('source_paths')
    if trails and catalog.get('source_code_paths'):
        return [[f"{n['name']}〔{n['directory']}〕" if n['label_status']=='source_series_title' else n['name']
                 for n in trail] for trail in trails]
    return catalog.get('category_paths',[])


def category_terms(row):
    catalog=row.get('source_catalog',{})
    return [s for path in supplier_paths(row)+catalog.get('source_code_paths',[]) for s in path]


def source_type(row):return row.get('source_type') or row.get('source_catalog',{}).get('source_type') or ''

def value(row,key):
    if key=='source_type':return source_type(row)
    if key=='maker':return row.get('manufacturer',{}).get('name') or '未知厂家'
    if key=='model':return row.get('model',{}).get('value') or '未注明型号'
    return str(row.get(key) or '未注明')

def fields(mode):return {'category':['reuse_kind','maker','title','model','configuration'],'source_type':['source_type','maker','title','model','configuration'],'manufacturer':['maker','reuse_kind','title','model','configuration'],'collection':['collection','reuse_kind','maker','title','model','configuration']}[mode]
def decode(path):
    try:
        v=json.loads(path or '[]');return v if isinstance(v,list) and all(isinstance(s,str) for s in v) else []
    except (TypeError,ValueError):return []
def encode(path):return json.dumps(path,ensure_ascii=False)
def label(key,text):return KINDS.get(text,text) if key=='reuse_kind' else text

def scoped(records,mode,path,collections):
    rows=[r for r in records if source_type(r)] if mode=='source_type' else list(records)
    for key,part in zip(fields(mode),path):
        if key=='collection':
            ids=set(next((c['ids'] for c in collections if c['name']==part),[]));rows=[r for r in rows if r['catalog_id'] in ids]
        else:rows=[r for r in rows if value(r,key)==part]
    return rows

def browse(records,mode,path,collections,search='',availability='all',page=0,page_size=12):
    if mode=='supplier_path':
        keys=['supplier_path']*(len(path)+1)
        rows=[r for r in records if any(p[:len(path)]==path for p in supplier_paths(r))]
    else:
        keys=fields(mode);path=path[:len(keys)];rows=scoped(records,mode,path,collections)
    needle=search.strip().casefold()
    if needle:rows=[r for r in rows if needle in ' '.join([r.get('title',''),r.get('original_name',''),value(r,'model'),value(r,'maker'),source_type(r),r.get('summary',''),*r.get('tags',[]),*category_terms(r)]).casefold()]
    if availability!='all':rows=[r for r in rows if r.get('availability')==availability]
    folders=[]
    if len(path)<len(keys):
        key=keys[len(path)]
        if key=='supplier_path':
            counts={}
            for r in rows:
                children={p[len(path)] for p in supplier_paths(r) if p[:len(path)]==path and len(p)>len(path)}
                for child in children:
                    counts[child]=counts.get(child,0)+1
            folders=[{'value':v,'label':v,'count':n} for v,n in counts.items()]
        elif key=='collection':
            rowids={r['catalog_id'] for r in rows}
            folders=[{'value':c['name'],'label':c['name'],'count':len(rowids.intersection(c['ids']))} for c in collections]
        else:
            counts={}
            for r in rows:counts[value(r,key)]=counts.get(value(r,key),0)+1
            folders=[{'value':v,'label':label(key,v),'count':n} for v,n in counts.items()]
        folders=[folder for folder in folders if folder['count']>0]
        if not path and mode=='category':
            order={name:i for i,name in enumerate(KINDS)}
            folders.sort(key=lambda f:(order.get(f['value'],len(order)),f['label'].casefold()))
        else:folders.sort(key=lambda f:f['label'].casefold())
    rows.sort(key=lambda r:(r.get('title','').casefold(),value(r,'model'),r['catalog_id']))
    pages=max(1,math.ceil(len(rows)/max(1,page_size)));page=max(0,min(page,pages-1))
    return {'folders':folders,'rows':rows[page*page_size:(page+1)*page_size],'total':len(rows),'page':page,'pages':pages,'breadcrumbs':[(label(k,p),path[:i+1]) for i,(k,p) in enumerate(zip(keys,path))]}
