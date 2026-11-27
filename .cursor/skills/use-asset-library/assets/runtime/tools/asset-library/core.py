"""不可变工程资产包的通用文件合同；不依赖 CAD/云运行时。"""
import hashlib
import json
import uuid
import math
from pathlib import Path

SCHEMA='unilab.engineering-asset/2'
NS=uuid.UUID('2e70ff7d-ce21-49a9-97ad-bc972667c566')
IDENTITY=[[1.,0,0,0],[0,1.,0,0],[0,0,1.,0],[0,0,0,1.]]

def uid(value):return str(uuid.uuid5(NS,value))
def sha(data):return hashlib.sha256(data).hexdigest()
def digest(path):return sha(Path(path).read_bytes())
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
def content_id(value):return sha(canonical(value))
def read(path):return json.loads(Path(path).read_text(encoding='utf8'))

def load_package(root):
    root=Path(root)
    from native_package import MANIFEST
    if (root/MANIFEST).exists():
        from native_package import package
        return package(root)
    return read(root/('asset.json' if (root/'asset.json').exists() else 'package.json'))
def write(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(canonical(value))
def inside(root,relative):
    root=Path(root).resolve();p=(root/relative).resolve()
    if Path(relative).is_absolute() or not p.is_relative_to(root) or p==root:raise ValueError('包内路径越界')
    return p
def matrix(a):return [[a[i+3*j] for j in range(3)]+[a[9+i]] for i in range(3)]+[[0.,0,0,1.]]
def mul(a,b):return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def inverse(m):
    r=[[m[j][i] for j in range(3)] for i in range(3)]
    return [r[i]+[-sum(r[i][j]*m[j][3] for j in range(3))] for i in range(3)]+[[0.,0,0,1.]]
def error(a,b):return max(abs(a[i][j]-b[i][j]) for i in range(4) for j in range(4))

def rigid(m):
    if len(m)!=4 or any(len(r)!=4 for r in m) or not all(math.isfinite(v) for r in m for v in r):raise ValueError('非法矩阵')
    if max(abs(m[3][i]-IDENTITY[3][i]) for i in range(4))>2e-7:raise ValueError('非仿射矩阵')
    for i in range(3):
        for j in range(3):
            if abs(sum(m[k][i]*m[k][j] for k in range(3))-(i==j))>2e-7:raise ValueError('非刚性矩阵')
    a,b,c=m[0][:3];d,e,f=m[1][:3];g,h,i=m[2][:3]
    if a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g)<0:raise ValueError('镜像矩阵')

def revisions(defs,rows):
    result={};busy=set()
    def revision(key):
        if key in result:return result[key]
        if key in busy:raise ValueError('定义递归依赖')
        busy.add(key);d=defs[key];value={'source_sha256':d['source']['sha256'],'configuration':d['configuration']}
        if d['kind']=='assembly':
            contexts=[]
            for occurrence in (r for r in rows if r['definition_id']==key):
                children=[{'definition_id':r['definition_id'],'revision':revision(r['definition_id']),'configuration':r['configuration'],'local_matrix':r['local_matrix']} for r in rows if r['parent']==occurrence['id']]
                contexts.append(sorted(children,key=canonical))
            if any(x!=contexts[0] for x in contexts[1:]):raise ValueError('同一定义存在不同内部布局；需配置/柔性实例建模')
            value['children']=contexts[0] if contexts else []
        result[key]=content_id(value);busy.remove(key);return result[key]
    for key in defs:revision(key)
    return result

def validate(root):
    root=Path(root).resolve();p=read(root/'package.json')
    legacy=p['schema']==SCHEMA
    if not legacy:
        from profiles import validate_profile
        profile=validate_profile(root,p)
        if p['profile'] in ('knowledge','station_layout','imported_candidate'):return profile
    if str(uuid.UUID(p['asset_id']))!=p['asset_id'] or (legacy and p['asset_id']!=p['root_definition']):raise ValueError('资产入口身份不符')
    if legacy and not {'cad','blender','usd'}.issubset(p['entrypoints']):raise ValueError('缺少必需的表示入口')
    defs=read(inside(root,p['definitions']))['definitions'];tree=read(inside(root,p['assembly']))['occurrences']
    if p['entrypoints']['cad']!=defs[p['root_definition']]['native']['path']:raise ValueError('CAD 入口不是根定义')
    by_id={x['id']:x for x in tree}
    if len(by_id)!=len(tree) or not tree:raise ValueError('实例缺失或重复')
    if set(defs)!={r['definition_id'] for r in tree}:raise ValueError('定义与实例覆盖不一致')
    roots=[x for x in tree if x['parent'] is None]
    if len(roots)!=1 or roots[0]['definition_id']!=p['root_definition']:raise ValueError('可复用根不唯一')
    if error(roots[0]['world_matrix'],IDENTITY)>2e-7:raise ValueError('库资产根必须在局部原点')
    counts={}
    for row in tree:
        rigid(row['local_matrix']);rigid(row['world_matrix'])
        d=defs[row['definition_id']]
        if row['definition_revision']!=d['revision'] or row['configuration']!=d['configuration']:raise ValueError('定义修订/配置漂移')
        seen={row['id']};parent=row['parent']
        while parent is not None:
            if parent not in by_id or parent in seen:raise ValueError('父链缺失或有环')
            seen.add(parent);parent=by_id[parent]['parent']
        world=row['local_matrix'] if row['parent'] is None else mul(by_id[row['parent']]['world_matrix'],row['local_matrix'])
        if error(world,row['world_matrix'])>2e-7:raise ValueError('局部与世界位姿不一致')
        if d['kind']=='part':counts[d['id']]=counts.get(d['id'],0)+1
    for d in defs.values():
        if d['kind'] not in ('part','assembly') or not d['configuration']:raise ValueError('定义类型/配置不完整')
        for field in ('source','native'):
            x=d[field]
            if digest(inside(root,x['path']))!=x['sha256']:raise ValueError('CAD 内容变更')
        if d['kind']=='part' and (legacy or 'mesh' in d):
            mesh=d['mesh']
            if digest(inside(root,mesh['path']))!=mesh['sha256']:raise ValueError('网格内容变更')
            if len(read(root/mesh['path'])['triangles_xyz_m'])!=mesh['triangles']*9:raise ValueError('网格数量变更')
    calculated=revisions(defs,tree)
    if any(d['id']!=k or d['revision']!=calculated[k] for k,d in defs.items()):raise ValueError('工程修订摘要漂移')
    if p['definition_revision']!=defs[p['root_definition']]['revision'] or p['configuration']!=defs[p['root_definition']]['configuration']:raise ValueError('根配置修订不匹配')
    bom=read(inside(root,p['bom']))['items']
    if len(bom)!=len({x['definition_id'] for x in bom}):raise ValueError('重复 BOM 行')
    if counts!={x['definition_id']:x['quantity'] for x in bom}:raise ValueError('工程 BOM 不一致')
    for name,path in p['entrypoints'].items():
        if not inside(root,path).is_file():raise ValueError('缺少入口 '+name)
    for rep in p['representations']:
        if digest(inside(root,rep['path']))!=rep['sha256']:raise ValueError('表示摘要不一致')
    capabilities=('native_reopen','blender_reopen','usd_reopen') if legacy else ('native_reopen',)
    for capability in capabilities:
        if p['capabilities'][capability]['status']!='passed':raise ValueError('发布前验收未通过 '+capability)
        proof=read(inside(root,p['capabilities'][capability]['evidence']))
        if proof['status']!='passed':raise ValueError('验收证据未通过')
    return {'status':'passed','definitions':len(defs),'occurrences':len(tree),'leaf_occurrences':sum(counts.values()),'root_definition':p['root_definition']}

def file_entries(root):
    root=Path(root)
    if any(p.is_symlink() for p in root.rglob('*')):raise ValueError('包内不允许符号链接')
    return [{'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)}
            for p in sorted(root.rglob('*')) if p.is_file() and p!=root/'bundle.sha256.json' and not p.name.startswith('~$')]

def seal(root):
    root=Path(root);validate(root)
    if (root/'bundle.sha256.json').exists():raise ValueError('不得覆盖封存')
    write(root/'bundle.sha256.json',{'schema':'unilab.bundle/2','files':file_entries(root)})
    return verify(root)

def verify(root,expected=None):
    from native_package import MANIFEST
    if (Path(root)/MANIFEST).exists():
        from native_package import verify as native
        return native(root,expected)
    root=Path(root)
    if (root/'asset.json').exists():
        from minimal_format import verify as verify_usage
        return verify_usage(root,expected)
    path=root/'bundle.sha256.json';manifest=read(path)
    if manifest.get('schema')!='unilab.bundle/2':raise ValueError('未知封存格式')
    if expected and digest(path)!=expected:raise ValueError('发布包身份不符')
    actual=file_entries(root)
    if actual!=manifest['files']:raise ValueError('文件清单/摘要不一致')
    result=validate(root);result.update(package_digest=digest(path),files=len(actual))
    return result
