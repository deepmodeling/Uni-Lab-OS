"""Device-square projection and dependent facets, independent of bpy."""
import math

FILTERS={'resource_type':'条目类型','domain':'学科','scene':'场景','stage':'阶段','major':'大类','type':'分类','manufacturer':'厂家','has_3d_model':'3D 模型','has_os_module':'OS module'}
LABELS={'component':'零件','mechanism':'机构','device':'设备','resource':'耗材与资源','workcell':'工站','tool':'工具'}
FIELDS={'domain':'domains','scene':'scenes','stage':'stages','major':'majors','type':'category_ancestors','manufacturer':'manufacturer_id'}

def merge(engineering,square):
    byid={r['catalog_id']:r for r in engineering['entries']};rows=[]
    for r in square['entries']:
        base=byid.get(r.get('asset_id') or r['id']) or byid.get(r['id'])
        row=dict(base) if base else {'catalog_id':r['id'],'title':r['title'],'manufacturer':{'name':r.get('manufacturer','未知厂家')},'configuration':'产品登记','targets':[],'availability':'source_only','insertable':False,'model':{},'summary':r.get('description','')}
        row['square']=r;row['square_id']=r['id'];rows.append(row)
    return rows

def values(row,key):
    r=row['square']
    if key in ('has_3d_model','has_os_module'):return ['true' if r.get(key) else 'false']
    v=r.get(FIELDS.get(key,key),r.get('types',[]) if key=='type' else '')
    return v if isinstance(v,list) else [str(v or '__unknown__')]

def matching(rows,filters,search='',availability='all'):
    words=search.casefold().split();out=[]
    for r in rows:
        if availability!='all' and r.get('availability')!=availability:continue
        if any(v not in ('','ALL') and v not in values(r,k) for k,v in filters.items()):continue
        b={k:v for k,v in filters.items() if k in ('domain','scene','stage') and v not in ('','ALL')}
        if b and 'scene_bindings' in r['square'] and not any(all(x.get(k)==v for k,v in b.items()) for x in r['square']['scene_bindings']):continue
        hay=' '.join(str(r['square'].get(k,'')) for k in ('title','name','manufacturer','description','tag_labels','category_paths','scenes')).casefold()
        if not all(w in hay for w in words):continue
        out.append(r)
    return out

def choices(rows,key,types):
    counts={};labels={}
    for r in rows:
        for v in set(values(r,key)):
            counts[v]=counts.get(v,0)+1
            if key=='manufacturer':labels[v]=r['square'].get('manufacturer') or '未知厂家'
    def label(v):
        if key in ('type','major'):return ' / '.join(types.get(v,{}).get('path',[types.get(v,{}).get('n',v)]))
        if key=='resource_type':return LABELS.get(v,v)
        if v in ('true','false'):return '有' if v=='true' else '无'
        return labels.get(v,v)
    return [('ALL','全部','')]+[(v,f'{label(v)} ({counts[v]})','') for v in sorted(counts,key=label)]

def browse(rows,filters,search,availability,page,size):
    rows=matching(rows,filters,search,availability);pages=max(1,math.ceil(len(rows)/size));page=min(page,pages-1)
    return {'total':len(rows),'pages':pages,'page':page,'rows':rows[page*size:(page+1)*size],'folders':[],'breadcrumbs':[]}
