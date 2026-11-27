bl_info={'name':'UniLab 设备广场','author':'UniLab','version':(0,3,0),'blender':(5,1,0),'location':'3D View > Sidebar > UniLab','description':'本地 / OSS 资产目录、缩略图与固定版本实例化','category':'3D View'}
import bpy,json,threading,queue,urllib.request,urllib.error,time,webbrowser,os,subprocess
import bpy.utils.previews as preview_api
from pathlib import Path
from bpy.props import StringProperty,BoolProperty,EnumProperty,IntProperty,PointerProperty
from .placement import insert_asset,make_editable,export_instances,scene_context
from . import browser_model as catalog_model
from . import square_model

STATE={'records':[],'status':'连接资产目录…','items':[],'suppress':False,'busy':False,'last_error':'','client_id':'','collections':[],'preview_paths':{},'draw_regions':{}};RESULTS=queue.Queue();PREVIEWS=None
def preferences():return bpy.context.preferences.addons[__package__].preferences
def api(path,data=None):
    req=urllib.request.Request(preferences().service_url.rstrip('/')+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json','X-UniLab-Client':'blender'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        try:raise RuntimeError(json.load(e).get('error','目录请求失败'))
        except (ValueError,KeyError):raise RuntimeError('目录请求失败 '+str(e.code))
def worker(kind,fn):
    if STATE['busy']:return
    STATE['busy']=True;STATE['status']='正在处理…'
    # Never read or write bpy inside the worker, including AddonPreferences.
    url=preferences().service_url.rstrip('/')
    square_url=preferences().square_url.rstrip('/')
    defaults=Path(__file__).with_name('runtime.json')
    runtime=json.loads(defaults.read_text(encoding='utf8')) if defaults.exists() else None
    def request(path,data=None):
        req=urllib.request.Request((square_url+path[len("square:"):] if path.startswith("square:") else url+path),data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json','X-UniLab-Client':'blender'})
        try:
            with urllib.request.urlopen(req,timeout=45) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            value=json.load(e);raise RuntimeError(value.get('error','请求失败'))
    def run():
        try:
            try:request('/api/health')
            except urllib.error.URLError:
                if not runtime:raise RuntimeError('目录服务未启动，请先运行服务或配置 runtime.json')
                logs=Path(runtime['config']).parent/'service.log';log=logs.open('ab')
                subprocess.Popen([runtime['python'],'-B',runtime['script'],'--config',runtime['config']],stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close()
                for attempt in range(30):
                    try:request('/api/health');break
                    except urllib.error.URLError:time.sleep(.2)
            RESULTS.put((kind,fn(request),None))
        except Exception as e:RESULTS.put((kind,None,str(e)))
    threading.Thread(target=run,daemon=True).start()
def wait_job(request,kind,data):
    job=request('/api/jobs',{'kind':kind,**data})
    while True:
        result=request('/api/jobs/'+job['id'])
        if result['state']=='done':return result['result']
        if result['state']=='failed':raise RuntimeError(result['error'])
        time.sleep(.2)
def load_square(req):
    try:req('square:/api/health')
    except urllib.error.URLError:
        path=Path(__file__).with_name('runtime.json')
        runtime=json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        if not runtime.get('square_script'):raise RuntimeError('设备广场服务未启动，请启动 8766 服务或配置 runtime.json 的 square_script')
        logs=Path(runtime['config']).parent/'square.log'
        with logs.open('ab') as log:
            subprocess.Popen([runtime.get('square_python',runtime['python']),'-B',runtime['square_script'],'--root',runtime['library_root'],'--port',str(runtime.get('square_port',8766))],stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for attempt in range(30):
            try:req('square:/api/health');break
            except urllib.error.URLError:time.sleep(.2)
    return {'engineering':req('/api/assets'),'square':req('square:/api/portal/blender')}

def filters(settings):
    return {key:getattr(settings,'square_'+key) for key in square_model.FILTERS}

def facet(key):
    def items(self,context):
        return STATE.get('enum_cache',{}).get(key,[('ALL','全部','',0)])
    return items

def rebuild_facets(settings):
    if STATE.get('suppress'):return
    rows=square_model.matching(STATE['records'],filters(settings),settings.search,settings.availability)
    cache={}
    ids=STATE.setdefault('enum_ids',{})
    for key in square_model.FILTERS:
        mapping=ids.setdefault(key,{'ALL':0})
        result=[]
        for value,label,description in square_model.choices(rows,key,STATE.get('types',{})):
            if value not in mapping:mapping[value]=len(mapping)
            result.append((value,label,description,mapping[value]))
        cache[key]=result
    STATE['enum_cache']=cache

def selected():
    value=bpy.context.scene.unilab_assets.asset_pick
    return next((r for r in STATE['records'] if r['catalog_id']==value),None)
def request_insert(asset_id):
    r=next((r for r in STATE['records'] if r['catalog_id']==asset_id),None)
    if r and r['insertable']:worker('insert',lambda req:wait_job(req,'prepare',{'catalog_id':asset_id}))
def picked(self,context):
    if self.one_click and not STATE['suppress']:request_insert(self.asset_pick)
def reset_page(self,context):
    self.page=0
    rebuild_facets(self)
    draw_refresh()

def reset_route(self,context):
    self.folder_path='[]';self.page=0
    draw_refresh()

def view(settings):
    return square_model.browse(STATE['records'],filters(settings),settings.search,settings.availability,settings.page,int(settings.page_size))

def draw_refresh():
    # Popovers are temporary regions outside screen.areas; background results and
    # navigation must rebuild those regions as well as the permanent sidebar.
    for key,region in list(STATE['draw_regions'].items()):
        try:
            if region.type=='TEMPORARY':region.tag_refresh_ui()
            region.tag_redraw()
        except ReferenceError:STATE['draw_regions'].pop(key,None)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':area.tag_redraw()
def thumbnail_icon(record):
    # Load only images actually drawn, not the entire catalog at refresh.
    rid=record['catalog_id'];path=record.get('thumbnail_local')
    if PREVIEWS is None or not path:return 0
    try:
        stat=Path(path).stat();signature=(path,stat.st_mtime_ns,stat.st_size)
        if STATE['preview_paths'].get(rid)!=signature:
            if rid in PREVIEWS:del PREVIEWS[rid]
            PREVIEWS.load(rid,path,'IMAGE');STATE['preview_paths'][rid]=signature
        return PREVIEWS[rid].icon_id
    except (OSError,ValueError,RuntimeError):return 0

def install_catalog(data):
    STATE['records']=square_model.merge(data['engineering'],data['square']);STATE['types']=data['square']['types'];STATE['collections']=[];STATE['suppress']=True
    live={r['catalog_id'] for r in STATE['records']}
    for rid in list(STATE['preview_paths']):
        if rid not in live:
            if rid in PREVIEWS:del PREVIEWS[rid]
            del STATE['preview_paths'][rid]
    settings=bpy.context.scene.unilab_assets
    if settings.browse_mode not in catalog_model.MODES:
        settings.browse_mode='category';settings.folder_path='[]';settings.page=0
    if STATE['records'] and not selected():settings.asset_pick=STATE['records'][0]['catalog_id']
    STATE['suppress']=False;rebuild_facets(settings);STATE['status']=f"{len(STATE['records'])} 条广场资产 · 固定版本插入";draw_refresh()
def pump():
    while not RESULTS.empty():
        kind,result,error=RESULTS.get();STATE['busy']=False
        if error:STATE['status']='操作失败';STATE['last_error']=error;draw_refresh();continue
        STATE['last_error']=''
        try:
            if kind=='catalog':install_catalog(result)
            elif kind=='insert':
                STATE['pending_insert']=result
                with scene_context():bpy.ops.unilab.insert_prepared('EXEC_DEFAULT')
                for record in STATE['records']:
                    if record.get('targets') and record['targets'][0]['package_digest']==result['target']['package_digest']:record['availability']='local'
                STATE['status']='已插入：'+result['title']
            elif kind=='sync':STATE['status']='同步完成';worker('catalog',load_square)
            elif kind=='readiness':STATE.setdefault('readiness',{})[result['catalog_id']]=result;STATE['status']='逐项验收已读取'
        except Exception as exc:STATE['last_error']=str(exc);STATE['status']='插入失败'
        draw_refresh()
    return .2

class UniLabPreferences(bpy.types.AddonPreferences):
    bl_idname=__package__
    service_url:StringProperty(name='目录服务',default='http://127.0.0.1:8765')
    square_url:StringProperty(name='设备广场服务',default='http://127.0.0.1:8766')
    def draw(self,context):
        self.layout.prop(self,'square_url');self.layout.prop(self,'service_url');self.layout.label(text='本机服务负责 OSS 访问；插件不保存云密钥。')
class UniLabSettings(bpy.types.PropertyGroup):
    search:StringProperty(name='搜索本目录',default='',update=reset_page)
    browse_mode:EnumProperty(name='浏览方式',items=[(k,v,'') for k,v in catalog_model.MODES.items()],update=reset_route)
    folder_path:StringProperty(default='[]')
    asset_pick:StringProperty(name='资产',default='',update=picked)
    one_click:BoolProperty(name='点击资产名称即插入',default=True)
    availability:EnumProperty(name='本地状态',items=[('all','所有状态',''),('local','本地可用',''),('remote','从 OSS 下载',''),('damaged','需要检查',''),('source_only','仅原件（未转换）','')],update=reset_page)
    page:IntProperty(default=0,min=0)
    page_size:EnumProperty(name='每页',items=[('6','6 项',''),('12','12 项',''),('24','24 项','')],default='12',update=reset_page)
    columns:IntProperty(name='面板列数',default=2,min=1,max=6)

for _key,_label in square_model.FILTERS.items():
    UniLabSettings.__annotations__['square_'+_key]=EnumProperty(name=_label,items=facet(_key),update=reset_page)

class UNILAB_OT_clear_square(bpy.types.Operator):
    bl_idname='unilab.clear_square';bl_label='清除筛选'
    def execute(self,context):
        s=context.scene.unilab_assets
        for k in square_model.FILTERS:setattr(s,'square_'+k,'ALL')
        s.search='';s.availability='all';s.page=0;draw_refresh();return {'FINISHED'}

class UNILAB_OT_folder(bpy.types.Operator):
    bl_idname='unilab.folder';bl_label='打开文件夹';bl_description='进入此目录；不会插入模型'
    path:StringProperty(default='[]')
    def execute(self,context):
        s=context.scene.unilab_assets;s.folder_path=self.path;s.page=0;draw_refresh();return {'FINISHED'}
class UNILAB_OT_page(bpy.types.Operator):
    bl_idname='unilab.page';bl_label='切换页面'
    offset:IntProperty(default=1)
    def execute(self,context):
        s=context.scene.unilab_assets;v=view(s);s.page=max(0,min(v['pages']-1,v['page']+self.offset));draw_refresh();return {'FINISHED'}
class UNILAB_OT_card(bpy.types.Operator):
    bl_idname='unilab.card';bl_label='选择资产'
    catalog_id:StringProperty();details:BoolProperty(default=False)
    @classmethod
    def description(cls,context,properties):
        r=next((r for r in STATE['records'] if r['catalog_id']==properties.catalog_id),None)
        return r['title']+'\n'+r.get('summary','') if r else '查看资产'
    def execute(self,context):
        s=context.scene.unilab_assets
        STATE['suppress']=True;s.asset_pick=self.catalog_id;STATE['suppress']=False
        if s.one_click and not self.details:request_insert(self.catalog_id)
        draw_refresh();return {'FINISHED'}
class UNILAB_OT_browser(bpy.types.Operator):
    bl_idname='unilab.browser';bl_label='UniLab 设备广场';bl_description='打开宽幅多资产目录窗口，按文件夹逐层浏览'
    def execute(self,context):
        with scene_context():bpy.ops.wm.call_panel(name='UNILAB_PT_browser_popup',keep_open=True)
        return {'FINISHED'}

class UNILAB_OT_refresh(bpy.types.Operator):
    bl_idname='unilab.refresh';bl_label='刷新目录'
    def execute(self,context):worker('catalog',load_square);return {'FINISHED'}
class UNILAB_OT_sync(bpy.types.Operator):
    bl_idname='unilab.sync';bl_label='同步 OSS 目录'
    def execute(self,context):worker('sync',lambda req:wait_job(req,'sync',{}));return {'FINISHED'}
class UNILAB_OT_insert(bpy.types.Operator):
    bl_idname='unilab.insert';bl_label='插入到 3D 光标'
    def execute(self,context):
        r=selected()
        if r:request_insert(r['catalog_id'])
        return {'FINISHED'}
class UNILAB_OT_insert_prepared(bpy.types.Operator):
    bl_idname='unilab.insert_prepared';bl_label='插入 UniLab 资产';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        request=STATE.pop('pending_insert',None)
        if not request:return {'CANCELLED'}
        insert_asset(request);return {'FINISHED'}
class UNILAB_OT_editable(bpy.types.Operator):
    bl_idname='unilab.make_editable';bl_label='转为可编辑副本';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:make_editable(context.active_object)
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return {'FINISHED'}
class UNILAB_OT_export(bpy.types.Operator):
    bl_idname='unilab.export_instances';bl_label='导出实例清单'
    filepath:StringProperty(subtype='FILE_PATH',default='asset-instances.json')
    def invoke(self,context,event):context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}
    def execute(self,context):
        try:export_instances(self.filepath)
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        return {'FINISHED'}
class UNILAB_OT_web(bpy.types.Operator):
    bl_idname='unilab.open_web';bl_label='打开网页目录'
    def execute(self,context):webbrowser.open(preferences().square_url);return {'FINISHED'}
def wrapped(layout,text,width=26):
    # Character chunks also wrap Chinese text, which has no whitespace boundaries.
    for line in str(text).splitlines():
        for start in range(0,len(line),width):layout.label(text=line[start:start+width])

def draw_folders(layout,settings,listing):
    row=layout.row(align=True)
    row.operator('unilab.folder',text='库根',icon='HOME').path='[]'
    path=catalog_model.decode(settings.folder_path)
    back=row.row();back.enabled=bool(path);back.operator('unilab.folder',text='上一级',icon='BACK').path=catalog_model.encode(path[:-1])
    for title,target in listing['breadcrumbs']:
        layout.operator('unilab.folder',text=title,icon='TRIA_RIGHT').path=catalog_model.encode(target)
    layout.separator()
    for folder in listing['folders']:
        row=layout.row();row.enabled=folder['count']>0
        row.operator('unilab.folder',text=f"{folder['label']}  ({folder['count']})",icon='FILE_FOLDER').path=catalog_model.encode([*path,folder['value']])
    if not listing['folders']:layout.label(text='已到配置目录',icon='CHECKMARK')

class UNILAB_OT_readiness(bpy.types.Operator):
    bl_idname='unilab.readiness';bl_label='读取逐项验收表'
    def execute(self,context):
        record=selected()
        if record:
            rid=record['catalog_id']
            worker('readiness',lambda req:req('/api/readiness?id='+rid))
        return {'FINISHED'}

def draw_details(layout,settings):
    r=selected()
    if not r:return
    box=layout.box();wrapped(box,r['title'],24)
    wrapped(box,r['manufacturer']['name']+' · '+r['configuration'],26)
    if catalog_model.source_type(r):wrapped(box,'来源类型：'+catalog_model.source_type(r),26)
    for path in catalog_model.supplier_paths(r):
        wrapped(box,'厂家分类：'+' › '.join(path),26)
    box.label(text={'local':'本地可用','remote':'插入时从 OSS 下载','damaged':'本地文件需要检查','source_only':'仅保留原件，暂无可插入模型'}.get(r['availability'],'未知状态'))
    wrapped(box,r.get('summary',''),28)
    qualification=r.get('qualification',{});official=qualification.get('nvidia_simready')
    if official:wrapped(box,'Sim-Ready: '+{'failed':'未通过','passed':'静态检查通过'}.get(official['status'],official['status']),28)
    if qualification.get('cad_correspondence')=='absent':box.label(text='缺少原始 CAD',icon='INFO')
    row=box.row();row.enabled=r['insertable'] and not STATE['busy'];row.operator('unilab.insert',icon='ADD')
    if not r['insertable']:box.label(text='来源与转换记录请在网页阅读' if r.get('record_kind')=='source_record' else '知识资料请在网页阅读',icon='INFO')
    action=box.row();action.enabled=bool(r.get('targets')) and not STATE['busy'];action.operator('unilab.readiness',icon='VIEWZOOM')
    detail=STATE.get('readiness',{}).get(r['catalog_id'])
    expected=(r.get('targets') or [{}])[0].get('package_digest')
    if detail and detail['binding'].get('package_digest')==expected:
        labels={'passed':'通过','failed':'失败','not_verified':'未验证','missing_data':'缺资料','not_applicable':'不适用'}
        for group in detail['groups']:
            section=box.box();section.label(text=group['label'])
            for item in group['items']:
                wrapped(section,item['label']+'：'+labels.get(item['status'],item['status']),28)
                if item.get('issue'):wrapped(section,item['issue'],30)
        wrapped(box,'提取到参数不代表动态验收通过；数值和证据可在网页查看。',28)

def draw_catalog(layout,context,wide=False):
    if context.region:STATE['draw_regions'][context.region.as_pointer()]=context.region
    popup=getattr(context,'region_popup',None)
    if popup:STATE['draw_regions'][popup.as_pointer()]=popup
    s=context.scene.unilab_assets;listing=view(s)
    row=layout.row(align=True);row.enabled=not STATE['busy'];row.operator('unilab.refresh',text='',icon='FILE_REFRESH');row.operator('unilab.sync',icon='IMPORT');row.operator('unilab.open_web',text='',icon='URL')
    if not wide:layout.operator('unilab.browser',text='打开宽幅设备广场',icon='WINDOW')
    if wide:
        split=layout.split(factor=.25);left=split.column();right=split.column()
    else:left=layout;right=layout
    left.operator('unilab.clear_square',text='清除筛选',icon='X')
    for key,label in square_model.FILTERS.items():left.prop(s,'square_'+key,text=label)
    right.prop(s,'search',text='',icon='VIEWZOOM')
    row=right.row(align=True);row.prop(s,'availability',text='');row.prop(s,'page_size',text='每页')
    if not wide:row.prop(s,'columns',text='列')
    right.prop(s,'one_click')
    row=right.row();row.label(text=f"{listing['total']} 项 · 第 {listing['page']+1}/{listing['pages']} 页")
    nav=row.row(align=True);prev=nav.row();prev.enabled=listing['page']>0;prev.operator('unilab.page',text='',icon='TRIA_LEFT').offset=-1
    nxt=nav.row();nxt.enabled=listing['page']+1<listing['pages'];nxt.operator('unilab.page',text='',icon='TRIA_RIGHT').offset=1
    grid=right.grid_flow(row_major=True,columns=4 if wide else s.columns,even_columns=True,even_rows=True,align=False)
    for r in listing['rows']:
        box=grid.box();icon=thumbnail_icon(r)
        if icon:box.template_icon(icon_value=icon,scale=5 if wide else 4)
        else:box.label(text='暂无预览',icon='OBJECT_DATA')
        name=r['title'];name=name[:13]+'…' if len(name)>14 else name
        button=box.row();button.enabled=not STATE['busy']
        op=button.operator('unilab.card',text=name,depress=s.asset_pick==r['catalog_id']);op.catalog_id=r['catalog_id']
        model=r.get('model',{}).get('value','');box.label(text=model[:20])
        if catalog_model.source_type(r):box.label(text=catalog_model.source_type(r)[:24])
        row=box.row(align=True);row.label(text={'local':'本地','remote':'OSS','damaged':'检查','source_only':'仅原件'}.get(r['availability'],'未知'),icon='CHECKMARK' if r['availability']=='local' else 'IMPORT')
        op=row.operator('unilab.card',text='',icon='INFO');op.catalog_id=r['catalog_id'];op.details=True
    if not listing['rows']:right.label(text='此目录没有匹配资产；清除搜索或返回上一级',icon='INFO')
    right.label(text=STATE['status'])
    if STATE['last_error']:wrapped(right,STATE['last_error'],60 if wide else 28)
    if wide:
        left.separator();draw_details(left,s)
    else:draw_details(right,s)
    row=right.row(align=True);row.operator('unilab.make_editable');row.operator('unilab.export_instances')

class UNILAB_PT_assets(bpy.types.Panel):
    bl_label='UniLab · 设备广场';bl_idname='UNILAB_PT_assets';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='UniLab';bl_ui_units_x=28
    def draw(self,context):draw_catalog(self.layout,context)
class UNILAB_PT_browser_popup(bpy.types.Panel):
    bl_label='UniLab · 设备广场';bl_idname='UNILAB_PT_browser_popup';bl_space_type='VIEW_3D';bl_region_type='WINDOW';bl_ui_units_x=66
    def draw(self,context):draw_catalog(self.layout,context,wide=True)
CLASSES=(UniLabPreferences,UniLabSettings,UNILAB_OT_clear_square,UNILAB_OT_folder,UNILAB_OT_page,UNILAB_OT_card,UNILAB_OT_browser,UNILAB_OT_refresh,UNILAB_OT_sync,UNILAB_OT_insert,UNILAB_OT_insert_prepared,UNILAB_OT_editable,UNILAB_OT_export,UNILAB_OT_web,UNILAB_OT_readiness,UNILAB_PT_assets,UNILAB_PT_browser_popup)

def initial_refresh():
    try:bpy.ops.unilab.refresh()
    except Exception:pass

def register():
    global PREVIEWS
    PREVIEWS=preview_api.new()
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.unilab_assets=PointerProperty(type=UniLabSettings)
    bpy.app.timers.register(pump,persistent=True)
    bpy.app.timers.register(initial_refresh,first_interval=1.,persistent=True)
def unregister():
    global PREVIEWS
    if bpy.app.timers.is_registered(pump):bpy.app.timers.unregister(pump)
    if bpy.app.timers.is_registered(initial_refresh):bpy.app.timers.unregister(initial_refresh)
    del bpy.types.Scene.unilab_assets
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
    STATE['draw_regions'].clear()
    if PREVIEWS is not None:
        preview_api.remove(PREVIEWS);PREVIEWS=None
