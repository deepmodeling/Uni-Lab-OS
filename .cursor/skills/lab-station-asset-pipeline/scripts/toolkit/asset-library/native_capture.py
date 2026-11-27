"""Generic native SolidWorks capture. Run via MCP globals sw, SPEC and OUTPUT.

Only original documents opened read-only; relinking applies to newly copied CAD.
One configured root per process/tool call; an external supervisor provides timeout.
"""
import sys
sys.dont_write_bytecode=True
import shutil,time,traceback
import pythoncom
from pathlib import Path
from win32com.client import VARIANT
from core import *
from catalog import segment

def v(o,n,*a):
    x=getattr(o,n)
    if hasattr(x,'_oleobj_'):return x
    return x(*a) if callable(x) else x

def br():return VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)

def capture(sw,spec,out):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    source_root=Path(spec['source_root']).resolve();source=inside(source_root,spec['path'])
    source_hash=digest(source);audit=[];owned=[]
    def opened(path,configuration='',readonly=True):
        existing=sw.GetOpenDocumentByName(str(path))
        if existing is not None:raise ValueError('输入已打开；避免改变现有文档配置 '+str(path))
        e,w=br(),br();d=sw.OpenDoc6(str(path),2 if path.suffix.lower()=='.sldasm' else 1,3 if readonly else 1,configuration,e,w)
        audit.append({'event':'open','path':str(path),'error':e.value,'warning':w.value})
        write(out/'evidence/native-progress.json',audit)
        if d is None or e.value or w.value:raise ValueError('CAD 打开错误/警告 '+str((e.value,w.value)))
        owned.append(path)
        if Path(v(d,'GetPathName')).resolve()!=path:raise ValueError('原生打开了错误路径')
        cfg=v(d.ConfigurationManager,'ActiveConfiguration').Name
        if configuration and cfg!=configuration:raise ValueError('配置与请求不符')
        return d,cfg
    defs={};paths={};rows=[]
    def definition(path,cfg,model):
        path=Path(path).resolve()
        if not path.is_relative_to(source_root):raise ValueError('依赖超出声明来源根 '+str(path))
        logical=path.relative_to(source_root).as_posix()+'#'+cfg
        identity=spec.get('identity_map',{}).get(logical,uid(spec['namespace']+'/'+logical))
        if str(__import__('uuid').UUID(identity))!=identity:raise ValueError('非法显式定义身份')
        if identity in defs:
            if paths[identity]!=path or defs[identity]['configuration']!=cfg:raise ValueError('显式身份重复绑定不同 CAD/配置')
            return identity
        h=digest(path);rel=path.relative_to(source_root).as_posix();target=out/'source/files'/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        # File names are for people; stable UUIDs remain in definitions/locks.
        stem=segment(spec.get('file_names',{}).get(identity,path.stem),48)
        taken={Path(x['native']['path']).stem.casefold() for x in defs.values()}
        if stem.casefold() in taken:stem=segment(stem+'-'+cfg,48)
        if stem.casefold() in taken:stem=segment(stem,38)+'-'+identity[:8]
        native='engineering/cad/'+stem+path.suffix.lower();(out/native).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,out/native)
        d={'id':identity,'display_name':path.stem,'kind':'assembly' if path.suffix.lower()=='.sldasm' else 'part','configuration':cfg,
           'revision':content_id({'source_sha256':h,'configuration':cfg}),
           'source':{'path':'source/files/'+rel,'sha256':h,'original_uri':str(path)},'native':{'path':native,'sha256':h},
           'configuration_scope':'selected_configuration','available_configurations':list(v(model,'GetConfigurationNames') or [])}
        # Preserve CAD calculations as declarations, never as certified dynamics.
        try:
            mp=v(model.Extension,'CreateMassProperty');properties={}
            for field in ('Mass','Volume','SurfaceArea','Density','CenterOfMass','PrincipleMomentsOfInertia'):
                raw=v(mp,field);properties[field]=list(raw) if isinstance(raw,(tuple,list)) else raw
            d['calculated_properties']={'values':properties,'status':'unverified_material_not_runtime_physics','units':'SolidWorks SI; kg, m, kg*m^2','source_sha256':h}
        except Exception as exc:d['calculated_properties']={'status':'unavailable','reason':type(exc).__name__}
        try:
            appearance=list(v(model,'MaterialPropertyValues') or [])
            if len(appearance)>=3:d['appearance']={'rgb':appearance[:3],'scope':'document material only; per-face overrides not captured'}
        except Exception:pass
        if d['kind']=='part':
            if v(model.ConfigurationManager,'ActiveConfiguration').Name!=cfg:raise ValueError('零件活动配置不匹配')
            tri=list(model.GetTessTriangles(True) or [])
            if not tri or len(tri)%9:raise ValueError('零件缺失三角网格')
            mesh='representations/mesh/'+stem+'.json';write(out/mesh,{'triangles_xyz_m':tri,'recipe':'SolidWorks.GetTessTriangles(True)','source_sha256':h,'configuration':cfg})
            d['mesh']={'path':mesh,'sha256':digest(out/mesh),'triangles':len(tri)//9}
            d['body_count']=len(model.GetBodies2(0,False) or [])
        defs[identity]=d;paths[identity]=path;return identity
    try:
        doc,cfg=opened(source,spec.get('configuration',''))
        rootdef=definition(source,cfg,doc);rootid=uid(rootdef+'/root')
        rows=[{'id':rootid,'parent':None,'native_name':source.stem,'definition_id':rootdef,'definition_revision':defs[rootdef]['revision'],'configuration':cfg,'local_matrix':IDENTITY,'world_matrix':IDENTITY}]
        comps=list(doc.GetComponents(False) or []) if source.suffix.lower()=='.sldasm' else []
        name_ids={c.Name2:uid(rootdef+'/'+c.Name2) for c in comps}
        for c in comps:
            if v(c,'GetSuppression')!=2:raise ValueError('未解析或抑制实例 '+c.Name2)
            model=v(c,'GetModelDoc2')
            if model is None:raise ValueError('组件无已解析文档')
            did=definition(v(c,'GetPathName'),c.ReferencedConfiguration,model)
            parent=c.Name2.rsplit('/',1)[0] if '/' in c.Name2 else None
            rows.append({'id':name_ids[c.Name2],'parent':name_ids[parent] if parent else rootid,'native_name':c.Name2,'definition_id':did,'definition_revision':defs[did]['revision'],'configuration':c.ReferencedConfiguration,'world_matrix':matrix(list(v(c.Transform2,'ArrayData'))),'suppression':v(c,'GetSuppression'),'fixed':bool(v(c,'IsFixed'))})
        byid={r['id']:r for r in rows}
        for r in rows[1:]:r['local_matrix']=mul(inverse(byid[r['parent']]['world_matrix']),r['world_matrix'])
        calculated=revisions(defs,rows)
        for k,d in defs.items():d['revision']=calculated[k]
        for r in rows:r['definition_revision']=calculated[r['definition_id']]
        # Close the opened root only; never save an original document.
        sw.CloseDoc(source.name)
        for d in defs.values():
            if d['kind']!='assembly':continue
            for child in defs.values():
                if child['id']==d['id']:continue
                ok=sw.ReplaceReferencedDocument(str(out/d['native']['path']),str(paths[child['id']]),str(out/child['native']['path']))
                audit.append({'event':'relink','assembly':d['id'],'dependency':child['id'],'replaced':bool(ok)})
            d['native']['sha256']=digest(out/d['native']['path'])
        native_doc,native_cfg=opened(out/defs[rootdef]['native']['path'],cfg)
        observed=[]
        if comps:
            native_components=list(native_doc.GetComponents(False) or [])
            if len(native_components)!=len(comps):raise ValueError('重开实例数量变化')
            # Native component names may change with renamed filenames; match identity by relocated file + parent lineage.
            lookup={str((out/d['native']['path']).resolve()).lower():d for d in defs.values()}
            for c in native_components:
                p=Path(v(c,'GetPathName')).resolve()
                if str(p).lower() not in lookup:raise ValueError('重开仍有外部 CAD 引用 '+str(p))
                d=lookup[str(p).lower()]
                if c.ReferencedConfiguration!=d['configuration'] or v(c,'GetSuppression')!=2:raise ValueError('重开配置/解析状态变化')
                observed.append({'definition_id':d['id'],'configuration':c.ReferencedConfiguration,'world_matrix':matrix(list(v(c.Transform2,'ArrayData'))),'native_name':c.Name2,'path':p.relative_to(out).as_posix()})
            remaining=list(observed)
            for r in rows[1:]:
                matches=[o for o in remaining if o['definition_id']==r['definition_id'] and error(o['world_matrix'],r['world_matrix'])<2e-7]
                if len(matches)!=1:raise ValueError('重开实例无法唯一对应')
                remaining.remove(matches[0]);r['relocated_native_name']=matches[0]['native_name']
        else:
            tri=list(native_doc.GetTessTriangles(True) or [])
            if not tri:raise ValueError('重开零件几何缺失')
            observed=[{'definition_id':rootdef,'triangles':len(tri)//9,'configuration':native_cfg}]
        sw.CloseDoc((out/defs[rootdef]['native']['path']).name)
        for d in defs.values():
            if digest(paths[d['id']])!=d['source']['sha256']:raise ValueError('原始 CAD 被修改')
            if digest(out/d['native']['path'])!=d['native']['sha256']:raise ValueError('只读重开修改了工作 CAD')
        counts={}
        for r in rows:
            if defs[r['definition_id']]['kind']=='part':counts[r['definition_id']]=counts.get(r['definition_id'],0)+1
        write(out/'definitions.json',{'definitions':defs})
        write(out/'assembly.json',{'units':'m','matrix_convention':'row-major; column vectors; local=T_parent_child','occurrences':rows})
        write(out/'bom.json',{'scope':'selected_configuration; unsuppressed parts','items':[{'definition_id':k,'configuration':defs[k]['configuration'],'quantity':n} for k,n in sorted(counts.items())]})
        proof={'status':'passed','software':'SolidWorks '+str(v(sw,'RevisionNumber')),'observed':observed,'originals_byte_preserved':True,'audit':audit}
        write(out/'evidence/native-reopen.json',proof)
        write(out/'interfaces.json',{'status':'not_authored','coordinate_frame':'native CAD local frame in meters','interfaces':[],'reason':'No inferred mounting/contact interface is certified by tessellation.'})
        write(out/'package.json',{'schema':SCHEMA,'asset_id':rootdef,'title':spec.get('title',source.stem),'definition_revision':defs[rootdef]['revision'],'configuration':cfg,'root_definition':rootdef,'definitions':'definitions.json','assembly':'assembly.json','bom':'bom.json','interfaces':'interfaces.json','entrypoints':{'cad':defs[rootdef]['native']['path']},'representations':[],
          'capabilities':{'native_reopen':{'status':'passed','evidence':'evidence/native-reopen.json'},'blender_reopen':{'status':'not_run'},'usd_reopen':{'status':'not_run'},'kinematics':{'status':'not_run'},'contact':{'status':'not_run'},'task':{'status':'not_run'}},'source_spec':spec})
        return {'status':'passed','definitions':len(defs),'occurrences':len(rows),'package':str(out)}
    except Exception as exc:
        write(out/'evidence/failure.json',{'status':'failed','error':str(exc),'traceback':traceback.format_exc(),'audit':audit});raise

if __name__=='__main__':
    import argparse,win32com.client
    ap=argparse.ArgumentParser();ap.add_argument('--spec',required=True);ap.add_argument('--output',required=True);ap.add_argument('--pid',type=int,required=True);a=ap.parse_args()
    rot=pythoncom.GetRunningObjectTable();ctx=pythoncom.CreateBindCtx(0)
    moniker=next(m for m in rot.EnumRunning() if m.GetDisplayName(ctx,None)=='SolidWorks_PID_'+str(a.pid))
    sw=win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
    print(capture(sw,read(a.spec),a.output),flush=True)
