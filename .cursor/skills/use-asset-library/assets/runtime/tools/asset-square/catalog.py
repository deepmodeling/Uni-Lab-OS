"""Read-only, rebuildable square projection over the existing library index."""
import copy
import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

KINDS = {'component':'零部件','mechanism':'机构模块','equipment':'设备','station_template':'工站与布局模板','workpiece_consumable':'工件与耗材','engineering_knowledge':'工程资料'}
AUTO = '自动化机械与物流'
ORDER = ['机械臂','直线模组与导轨','运动机构','变位机与旋转平台','末端工具与夹具','输送与上料','移动与转运','传感与视觉','机架与工作台','机械零部件','机器人配套','实验室自动化机构','工站与布局','控制与信号','包装机构','其他机构','其他自动化设备','人物与人体工学','仿真辅助组件']
APPLICATIONS = {'Material Handling':'搬运','Assembly':'装配','Remote TCP':'远端 TCP 加工','Dispensing':'点胶与分液','Additive Manufacturing':'增材制造','Welding':'焊接','Collaborative':'协作作业','Palletizing':'码垛','Finishing':'表面处理','Inspection':'检测','Tool Changing':'工具切换','Cutting':'切割','Drilling':'钻孔','Engraving':'雕刻','Machining':'机械加工','Sanding':'打磨','Calibration':'标定','Milling':'铣削','Conveying':'输送','Painting':'喷涂','Taping':'贴胶','Deburring':'去毛刺','Grinding':'研磨','Screwing':'拧紧','HMI':'人机交互'}
SOURCE_TYPES = {
    'robots': '机械臂', 'robot tools': '末端工具与夹具', 'tools': '末端工具与夹具',
    'robot workpiece positioners': '变位机与旋转平台', 'robot positioners': '变位机与旋转平台',
    'single axis linear actuators': '直线模组与导轨', 'advanced motion': '运动机构',
    'conveyors': '输送与上料', 'feeders': '输送与上料', 'conveyor utilities': '输送与上料',
    'mobile robot utilities': '移动与转运', 'navigation': '移动与转运', 'navigation (beta)': '移动与转运',
    'transport controllers': '控制与信号', 'signal utilities': '控制与信号', 'signal positioners': '控制与信号',
    'sensors': '传感与视觉', 'cameras': '传感与视觉', 'fixtures': '末端工具与夹具',
    'robot pedestals': '机架与工作台', 'facilities - factory': '机架与工作台',
    'facilities - interior': '机架与工作台', 'facilities - robot': '机器人配套',
    'robot cabling': '机器人配套', 'characters': '人物与人体工学', 'ergonomics': '人物与人体工学',
    'statistics': '仿真辅助组件', 'text and media': '仿真辅助组件', 'basic shapes': '仿真辅助组件',
    'physics': '仿真辅助组件', 'component templates': '仿真辅助组件', 'vr interaction': '仿真辅助组件',
    'resource tools': '仿真辅助组件', 'life science': '实验室自动化机构', 'packaging': '包装机构',
}

def classify(r):
    """Display taxonomy only: never changes the sealed package's reuse_kind."""
    name=' '.join(str(r.get(k,'')) for k in ('title','original_name','functional_category')).casefold()
    kind=r.get('reuse_kind')
    if kind=='workpiece_consumable':
        name=' '.join(str(r.get(k,'')) for k in ('title','original_name')).casefold()
        if re.search(r'带盖[瓶管]',str(r.get('title',''))):return 'consumables','工件与耗材','瓶管与玻璃器皿','现有耗材类别 + 整瓶名称'
        for pattern,sub in [(r'瓶盖|光盖|盖子|(^|[_\s/])cap([_\s/.]|$)','瓶盖与密封件'),(r'移液|吸头|tip','移液耗材'),(r'载架|瓶架|料架|rack|托盘|容器|箱|container|pallet|box','容器与承载件'),(r'孔板|微孔|plate','孔板与板式耗材'),(r'瓶|vial|bottle|flask|tube','瓶管与玻璃器皿')]:
            if re.search(pattern,name):return 'consumables','工件与耗材',sub,'现有耗材类别 + 名称规则'
        return 'consumables','工件与耗材','其他工件与耗材','现有耗材类别'
    if kind=='engineering_knowledge':return 'equipment','工程资料','规则与证据','现有复用类别'
    if kind=='station_template':return 'equipment',AUTO,'工站与布局','现有复用类别'
    source=r.get('source_type') or r.get('source_catalog',{}).get('category') or ''
    st=str(source).casefold()
    if st in SOURCE_TYPES:return 'equipment',AUTO,SOURCE_TYPES[st],'来源类型映射：'+str(source)
    types=r.get('source_catalog',{}).get('source_types',{})
    mapped={SOURCE_TYPES[v.casefold()] for values in types.values() for v in values if isinstance(v,str) and v.casefold() in SOURCE_TYPES} if isinstance(types,dict) else set()
    if len(mapped)==1:return 'equipment',AUTO,next(iter(mapped)),'多来源类型一致映射'
    if kind=='equipment':
        for pattern,sub in [(r'天平|balance|色谱|质谱|光谱','分析与测量设备'),(r'旋蒸|旋转蒸发|rotavap|离心|centrifuge|移液|液体处理|萃取','样品处理设备')]:
            if re.search(pattern,name):return 'equipment','实验仪器与设备',sub,'名称规则，供浏览分类'
    # Use bounded names, never freeform provenance/reports (which mention unrelated devices).
    for pattern,sub in [(r'机械臂|\brobot\b|机器人','机械臂'),(r'夹爪|gripper|夹具|fixture|开盖|合盖|capper','末端工具与夹具'),(r'导轨|地轨|直线|linear|滑台|模组|滑块|actuator','直线模组与导轨'),(r'旋转|变位|rotary|positioner','变位机与旋转平台'),(r'传送|输送|conveyor|feeder','输送与上料'),(r'传感|sensor|相机|camera|光纤','传感与视觉'),(r'机架|底座|支架|工作台|frame|pedestal','机架与工作台')]:
        if re.search(pattern,name):return 'equipment',AUTO,sub,'名称规则，供浏览分类'
    fallback={'component':'机械零部件','mechanism':'其他机构','equipment':'其他自动化设备'}
    return 'equipment',AUTO,fallback.get(kind,'其他资产'),'现有复用类别，细类待核对'

def inside(root,relative):
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute():raise ValueError('无效文件引用')
    p=(root/relative).resolve()
    if not p.is_relative_to(root.resolve()) or p==root.resolve():raise ValueError('文件超出资产根目录')
    return p

class Catalog:
    def __init__(self,root):
        self.root=Path(root).resolve();self.lock=threading.RLock();self.stamp=None;self.snapshot=None
    def load(self):
        with self.lock:
            path=self.root/'library.json';s=path.stat();stamp=(s.st_mtime_ns,s.st_size)
            if stamp==self.stamp:return self.snapshot
            raw=path.read_bytes();after=path.stat()
            if stamp!=(after.st_mtime_ns,after.st_size):raise ValueError('索引正在更新，请稍后刷新')
            data=json.loads(raw)
            if data.get('schema') not in ('unilab.usd-library/1','unilab.usage-library/1'):raise ValueError('不支持的资产索引格式')
            scenes=defaultdict(list)
            for c in data.get('collections',[]):
                for cid in c.get('ids',[]):scenes[cid].append(c['name'])
            packages=defaultdict(list)
            for p in data.get('packages',[]):packages[(p['asset_id'],p['path'])].append(p)
            rows=[];byid={}
            for original in data['records']:
                r=copy.deepcopy(original)
                r.setdefault('targets', [])
                for t in r['targets']:
                    if not t.get('package_digest'):
                        matches=packages[(t['asset_id'],t['package_path'])]
                        if len(matches)==1:t['package_digest']=matches[0]['package_digest']
                cid=r['catalog_id'];byid[cid]=r;target=r['targets'][0] if r['targets'] else {}
                square,major,category,basis=classify(r)
                applications=r.get('source_catalog',{}).get('metadata',{}).get('AP',[])
                scenario_names=[APPLICATIONS.get(a,a) for a in applications if isinstance(a,str)]
                for token,label in [('旋蒸','旋蒸工站'),('开盖','开关盖'),('液体处理','液体处理工站'),('spe-station','固相萃取工站')]:
                    if any(token in name for name in scenes[cid]):scenario_names.append(label)
                display=data.get('presentation',{}).get(cid,{})
                thumb=display.get('thumbnail',{})
                if thumb.get('source_package_digest')!=target.get('package_digest'):thumb={}
                ep=target.get('entrypoints',{})
                item={'id':cid,'title':r['title'],'model':r.get('model',{}).get('value',''),'manufacturer':r.get('manufacturer',{}).get('name','未知厂家'),
                    'square':square,'major':major,'category':category,'classification_basis':basis,'classification_review':r.get('classification_status')=='needs_review' or '待核对' in basis,
                    'reuse_kind':kind if (kind:=r.get('reuse_kind')) else '', 'reuse_label':KINDS.get(kind,kind),
                    'summary':display.get('summary',r.get('summary','')),'scenes':list(dict.fromkeys(scenario_names+scenes[cid])),
                    'configuration':r.get('configuration',''),'versions':len(r['targets']),'source_type':r.get('source_type',''),
                    'has_cad':any(k=='cad' or v.get('format','').lower() in ('sldprt','sldasm','step','stp','iges','igs','x_t') for k,v in ep.items()),
                    'thumbnail':('/api/thumbnail?id='+cid+'&v='+thumb['sha256']) if thumb.get('path') and thumb.get('sha256') else None}
                item['_search']=' '.join([str(x) for x in [r['title'],r.get('original_name',''),item['model'],item['manufacturer'],major,category,*r.get('aliases',[]),*r.get('tags',[]),*item['scenes']]]).casefold()
                rows.append(item)
            rows.sort(key=lambda r:(r['major']!=AUTO, ORDER.index(r['category']) if r['category'] in ORDER else 99,r['classification_review'],r['manufacturer'].casefold(),r['title'].casefold()))
            self.snapshot={'data':data,'rows':rows,'records':byid,'digest':hashlib.sha256(raw).hexdigest(),'updated':datetime.fromtimestamp(s.st_mtime).astimezone().isoformat(),'bytes':len(raw)};self.stamp=stamp
            return self.snapshot
    def query(self,args):
        snap=self.load();square=args.get('square','equipment')
        if square not in ('equipment','consumables'):raise ValueError('未知广场')
        base=[r for r in snap['rows'] if r['square']==square]
        q=args.get('q','').strip().casefold()
        rows=[r for r in base if all(not args.get(k) or r[k]==args[k] for k in ('manufacturer','major','category')) and (not args.get('scene') or args['scene'] in r['scenes']) and all(term in r['_search'] for term in q.split())]
        page=max(1,int(args.get('page','1')));size=min(96,max(1,int(args.get('page_size','24'))));pages=max(1,(len(rows)+size-1)//size);page=min(page,pages)
        def clean(r):return {k:v for k,v in r.items() if k!='_search'}
        facets={k:[{'name':n,'count':v} for n,v in sorted(Counter(r[k] for r in base).items())] for k in ('manufacturer','major','category')}
        facets['scene']=[{'name':n,'count':v} for n,v in sorted(Counter(s for r in base for s in r['scenes']).items())]
        groups=[{'major':m,'categories':[{'name':c,'count':n} for c,n in Counter(r['category'] for r in base if r['major']==m).items()]} for m in dict.fromkeys(r['major'] for r in base)]
        return {'entries':[clean(r) for r in rows[(page-1)*size:page*size]],'total':len(rows),'page':page,'pages':pages,'page_size':size,'facets':facets,'groups':groups,
                'counts':dict(Counter(r['square'] for r in snap['rows'])),'square_count':len(base),'cad_count':sum(r['has_cad'] for r in base),'snapshot':{'sha256':snap['digest'],'updated':snap['updated']}}
    def detail(self,cid,version=None):
        snap=self.load();r=snap['records'].get(cid)
        if r is None:raise KeyError('没有找到该资产')
        candidates=[t for t in r['targets'] if not version or t.get('package_digest')==version]
        if not candidates:raise KeyError('该资产没有这个版本')
        target=candidates[0];root=inside(self.root,target['package_path']);metadata={}
        # Metadata read is not a whole-package integrity or physics certification.
        for key,rel in [('adapter','simready_usd/unilab.json'),('readiness','simready_usd/readiness.json')]:
            p=inside(root,rel)
            if p.is_file():metadata[key]=json.loads(p.read_text(encoding='utf-8'))
        return {'record':r,'target':target,'package_root':str(root),'metadata':metadata,'reference':{'catalog_id':cid,**target},'snapshot_sha256':snap['digest'],'integrity':'not_checked_by_square'}
    def thumbnail(self,cid,expected=None):
        snap=self.load();r=snap['records'].get(cid)
        if not r:raise KeyError('未知资产')
        thumb=snap['data'].get('presentation',{}).get(cid,{}).get('thumbnail',{})
        if not r.get('targets') or not thumb or thumb.get('source_package_digest')!=r['targets'][0].get('package_digest'):raise KeyError('没有当前版本的预览图')
        if expected and expected!=thumb['sha256']:raise KeyError('预览已更新')
        p=inside(self.root,thumb['path']);b=p.read_bytes()
        if hashlib.sha256(b).hexdigest()!=thumb['sha256']:raise ValueError('预览图摘要不匹配')
        return b
