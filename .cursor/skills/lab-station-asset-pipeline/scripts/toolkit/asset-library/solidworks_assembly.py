"""Build portable native assemblies from explicit CAD occurrences and joint recipes.

Execute build(sw, recipe, output) in the existing SolidWorks connector session.
The recipe supplies geometry; this module does not infer mechanisms from shape.
"""
import hashlib
import json
import math
import shutil
from pathlib import Path


PLANES = {'X': '右视基准面', 'Y': '上视基准面', 'Z': '前视基准面'}


def build(sw, recipe_file, output, reuse_groups=False):
    import pythoncom
    from win32com.client import VARIANT, Dispatch
    recipe = json.loads(Path(recipe_file).read_text('utf8'))
    out = Path(output)
    out.mkdir(parents=True, exist_ok=reuse_groups)
    (out/'parts').mkdir(exist_ok=reuse_groups); (out/'groups').mkdir(exist_ok=reuse_groups)
    empty = VARIANT(pythoncom.VT_DISPATCH, None)
    error = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warning = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    math_util = sw.GetMathUtility
    documents = []

    def progress(phase, **values):
        (out/'progress.json').write_text(json.dumps(dict(phase=phase, **values)))

    def matrix(values):
        array=VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [float(values[i][j]) for i in range(3) for j in range(3)] +
            [float(v) for v in values[3][:3]] + [1.,0.,0.,0.])
        dispatch=math_util._oleobj_.InvokeTypes(math_util._oleobj_.GetIDsOfNames('CreateTransform'),
            0,pythoncom.DISPATCH_METHOD,(pythoncom.VT_DISPATCH,0),((pythoncom.VT_VARIANT,1),),array)
        return Dispatch(dispatch)

    def noargs(obj, name):
        return obj._oleobj_.Invoke(obj._oleobj_.GetIDsOfNames(name),0,pythoncom.DISPATCH_METHOD,True)

    def translation(origin):
        return [[1.,0.,0.,0.],[0.,1.,0.,0.],[0.,0.,1.,0.],[*origin,1.]]

    def save(doc, path):
        error.value=warning.value=0
        doc.ClearSelection2(True)
        assert doc.Extension.SaveAs(str(path),0,1,empty,error,warning),str(path)
        assert error.value==0,(str(path),error.value)
        documents.append(str(path))

    def new_assembly():
        return sw.NewDocument(sw.GetUserPreferenceStringValue(9),0,0.,0.)

    def activate(doc):
        error.value=0
        sw.ActivateDoc3(doc.GetTitle,False,0,error)

    def axes(doc):
        for axis, plane_names in [('X',['前视基准面','上视基准面']),
                                  ('Y',['前视基准面','右视基准面']),
                                  ('Z',['上视基准面','右视基准面'])]:
            doc.ClearSelection2(True)
            for name in plane_names:
                assert doc.Extension.SelectByID2(name,'PLANE',0.,0.,0.,True,0,empty,0)
            assert doc.InsertAxis2(True)
            feature=doc.FirstFeature
            last_axis=None
            while feature:
                if feature.GetTypeName2=='RefAxis':last_axis=feature
                feature=feature.GetNextFeature
            assert last_axis is not None
            last_axis.Name='JointAxis_'+axis
        doc.ClearSelection2(True)

    def component(doc, path, pose, fixed=False):
        activate(doc)
        comp=doc.AddComponent5(str(path),0,'',False,'',0.,0.,0.)
        assert comp is not None,str(path)
        comp.Transform2=matrix(pose)
        doc.ClearSelection2(True)
        assert comp.Select4(False,empty,False)
        if fixed: noargs(doc,'FixComponent')
        elif comp.IsFixed: noargs(doc,'UnfixComponent')
        doc.ClearSelection2(True)
        return comp

    def select(doc, comp, feature=None, kind='PLANE'):
        if feature is None:
            data=doc.SelectionManager.CreateSelectData;data.Mark=1
            assert comp.Select4(True,data,False)
        else:
            title=Path(doc.GetTitle).stem
            assert doc.Extension.SelectByID2(feature+'@'+comp.Name2+'@'+title,
                kind,0.,0.,0.,True,1,empty,0),(feature,comp.Name2,title)

    def mate(doc, left, right, kind, name, feature=None, distance=0., angle=0.):
        doc.ClearSelection2(True)
        for comp in (left,right):select(doc,comp,feature,'AXIS' if feature and feature.startswith('JointAxis_') else 'PLANE')
        error.value=0
        obj=doc.AddMate5(kind,0,False,distance,distance,distance,0.,0.,angle,angle,angle,False,False,0,error)
        assert obj is not None and error.value==1,(name,error.value)
        obj.Name=name
        doc.ClearSelection2(True)
        return name

    progress('copying_native_parts')
    definitions={}
    for group in recipe['groups']:
        for row in group['parts']:
            source=Path(row['native'])
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            if digest not in definitions:
                target=out/'parts'/(digest[:20]+'.SLDPRT');shutil.copy2(source,target)
                definitions[digest]=target;documents.append(str(target))
            row['_native']=str(definitions[digest])
    frame_path=out/'parts/joint_frame.SLDPRT'
    if not reuse_groups:
        frame=sw.NewDocument(sw.GetUserPreferenceStringValue(8),0,0.,0.)
        axes(frame);save(frame,frame_path)
        sw.CloseDoc(frame.GetTitle)
    group_files={}
    for index, group in enumerate(recipe['groups']):
        path=out/'groups'/(group['id']+'.SLDASM')
        if reuse_groups and path.is_file():
            group_files[group['id']]=path
            continue
        progress('rigid_groups',completed=index,total=len(recipe['groups']))
        doc=new_assembly();axes(doc)
        paths=[];transforms=[]
        for row in group['parts']:
            pose=[r[:] for r in row['transform']]
            pose[3][:3]=[pose[3][i]-group['origin_m'][i] for i in range(3)]
            paths.append(row['_native'])
            transforms.extend([float(pose[i][j]) for i in range(3) for j in range(3)]+
                              [float(v) for v in pose[3][:3]]+[1.,0.,0.,0.])
        progress('adding_rigid_group',group=group['id'],parts=len(paths))
        if paths:
            components=doc._oleobj_.InvokeTypes(doc._oleobj_.GetIDsOfNames('AddComponents3'),
                0,pythoncom.DISPATCH_METHOD,(pythoncom.VT_VARIANT,0),((pythoncom.VT_VARIANT,1),)*3,
                VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_BSTR,paths),
                VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,transforms),
                VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_BSTR,['']*len(paths)))
            assert components is not None and len(components)==len(paths)
            doc.ClearSelection2(True)
            for index, raw in enumerate(components):
                comp=Dispatch(raw)
                assert Path(comp.GetPathName).resolve()==Path(paths[index]).resolve(),('CAD document name collision',comp.GetPathName,paths[index])
                expected=transforms[index*16:(index+1)*16]
                assert max(abs(x-y) for x,y in zip(comp.Transform2.ArrayData,expected))<1e-6,(group['id'],index,'placement changed')
                assert comp.Select4(True,empty,False)
            noargs(doc,'FixComponent');doc.ClearSelection2(True)
            assert all(Dispatch(c).IsFixed for c in components)
        path=out/'groups'/(group['id']+'.SLDASM');save(doc,path)
        group_files[group['id']]=path
        sw.CloseDoc(doc.GetTitle)
    progress('top_assembly')
    assembly=new_assembly();save(assembly,out/(recipe['name']+'.SLDASM'))
    # The API name is counterintuitive: True suspends automatic rebuilds.
    # Rebuild once after all native constraints have been added.
    assembly.EnableAssemblyRebuild=True
    instances={}
    by_id={g['id']:g for g in recipe['groups']}
    for group in recipe['groups']:
        loaded=sw.OpenDoc6(str(group_files[group['id']]),2,1,'',error,warning)
        assert loaded is not None and error.value==0,(group['id'],error.value)
        instances[group['id']]=component(assembly,group_files[group['id']],
                                        translation(group['origin_m']),group.get('parent') is None)
    drives=[];mate_names=[]
    loaded=sw.OpenDoc6(str(frame_path),1,1,'',error,warning)
    assert loaded is not None and error.value==0
    for index, group in enumerate(recipe['groups']):
        if group.get('parent') is None:continue
        progress('native_mates',completed=index,total=len(recipe['groups']))
        parent=instances[group['parent']];child=instances[group['id']]
        joint=group['joint']
        if joint['kind']=='fixed':
            mate_names.append(mate(assembly,parent,child,16,group['id']+'_mount'))
            continue
        axis=joint['axis'];axis_index='XYZ'.index(axis)
        anchor=list(group['origin_m'])
        offset=min(joint.get('limits',[0.]))-.001 if joint['kind']=='prismatic' else 0.
        anchor[axis_index]+=offset
        marker=component(assembly,frame_path,translation(anchor))
        marker.ExcludeFromBOM=True
        mate_names.append(mate(assembly,parent,marker,16,group['id']+'_mount'))
        if joint['kind']=='prismatic':
            for other in 'XYZ':
                if other!=axis:
                    mate_names.append(mate(assembly,marker,child,0,group['id']+'_guide_'+other,PLANES[other]))
            name=mate(assembly,marker,child,5,group['id']+'_drive',PLANES[axis],distance=-offset)
        else:
            # Reference axes use coincident, not cylindrical-face concentric.
            mate_names.append(mate(assembly,marker,child,0,group['id']+'_axis','JointAxis_'+axis))
            mate_names.append(mate(assembly,marker,child,0,group['id']+'_seat',PLANES[axis]))
            # Rotation remains a genuine free DOF; a native angle mate can be
            # added by the consumer for a finite-angle position study.
            name=None
        if name:mate_names.append(name)
        drives.append(dict(group=group['id'],kind=joint['kind'],axis=axis,
                           parameter='D1@'+name if name else None,offset_m=offset,
                           parent=group['parent'],limits=joint.get('limits')))
    assembly.EnableAssemblyRebuild=False
    assert assembly.EditRebuild3
    save(assembly,out/(recipe['name']+'.SLDASM'))
    pose_rows={key:list(comp.Transform2.ArrayData) for key,comp in instances.items()}
    for group in recipe['groups']:
        actual=pose_rows[group['id']]
        assert max(abs(a-b) for a,b in zip(actual[9:12],group['origin_m']))<1e-6,(group['id'],'mate changed initial position')
    receipt=dict(status='built',native_parts=len(definitions),
        part_occurrences=sum(len(g['parts']) for g in recipe['groups']),
        groups=len(instances),mates=len(mate_names),drives=drives,poses=pose_rows,
        entry=recipe['name']+'.SLDASM',documents=documents,solidworks=sw.RevisionNumber)
    (out/'build.json').write_text(json.dumps(receipt,indent=2))
    (out/'recipe.json').write_text(json.dumps(recipe,ensure_ascii=False,indent=2),encoding='utf8')
    progress('built',parts=receipt['part_occurrences'],mates=len(mate_names))
    noargs(assembly,'ViewZoomtofit2')
    print('NATIVE ASSEMBLY',receipt['entry'],receipt['part_occurrences'],'parts',receipt['mates'],'mates')


if __name__=='__main__':
    import argparse
    import win32com.client
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:application=win32com.client.GetActiveObject('SldWorks.Application')
    except Exception:application=win32com.client.Dispatch('SldWorks.Application')
    application.Visible=True
    application.DocumentVisible(False,1);application.DocumentVisible(False,2)
    try:build(application,args.recipe,args.output)
    finally:application.DocumentVisible(True,1);application.DocumentVisible(True,2)
