"""Read back native mate-driven positions; a saved assembly alone is not a pass."""
import json, math
from pathlib import Path


def verify(sw, folder, groups=None):
    import pythoncom
    from win32com.client import VARIANT,Dispatch
    folder=Path(folder);build=json.loads((folder/'build.json').read_text())
    doc=sw.ActiveDoc
    assert Path(doc.GetPathName).name==build['entry']
    components={c.Name2.rsplit('-',1)[0]:c for c in doc.GetComponents(True)
                if not c.Name2.startswith('joint_frame-')}
    rows=[];utility=sw.GetMathUtility
    recipe=json.loads((folder/'recipe.json').read_text('utf8'))
    definitions={g['id']:g for g in recipe['groups']}
    def pose(key):return list(components[key].Transform2.ArrayData)
    def transform(values):
        return Dispatch(utility._oleobj_.InvokeTypes(utility._oleobj_.GetIDsOfNames('CreateTransform'),
            0,pythoncom.DISPATCH_METHOD,(pythoncom.VT_DISPATCH,0),((pythoncom.VT_VARIANT,1),),
            VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,values)))
    selected=[d for d in build['drives'] if groups is None or d['group'] in groups]
    for index,drive in enumerate(selected):
        (folder/'motion-progress.json').write_text(json.dumps(dict(status='running',completed=index,total=len(selected),group=drive['group'])))
        name=drive['group'];before=pose(name)
        followers=[g['id'] for g in recipe['groups'] if g.get('parent')==name and g.get('joint',{}).get('kind')=='fixed']
        follower_poses={key:pose(key) for key in followers}
        if drive['kind']=='prismatic':
            parameter=doc.Parameter(drive['parameter']);initial=parameter.SystemValue
            limits=drive['limits'];axis='XYZ'.index(drive['axis'])
            trials=[]
            for target in [limits[0],sum(limits)/2,limits[1]]:
                parameter.SystemValue=target-drive['offset_m'];rebuilt=doc.EditRebuild3
                actual=pose(name)
                expected=[before[9+i]+(target if i==axis else 0.) for i in range(3)]
                residual=max(abs(actual[9+i]-expected[i]) for i in range(3))
                for key,previous in follower_poses.items():
                    follower=pose(key)
                    residual=max(residual,max(abs(follower[9+i]-previous[9+i]-(target if i==axis else 0.)) for i in range(3)))
                trials.append(dict(target_m=target,actual_m=actual[9:12],residual_m=residual,rebuild=rebuilt))
            parameter.SystemValue=initial;doc.EditRebuild3
            rows.append(dict(group=name,path=definitions[name].get('path'),kind=drive['kind'],fixed_followers=followers,trials=trials,passed=all(t['rebuild'] and t['residual_m']<1e-6 for t in trials)))
        else:
            angle=math.pi/6;c=math.cos(angle);s=math.sin(angle)
            rotations={'X':[1.,0.,0.,0.,c,s,0.,-s,c],
                       'Y':[c,0.,-s,0.,1.,0.,s,0.,c],
                       'Z':[c,s,0.,-s,c,0.,0.,0.,1.]}
            target=[*rotations[drive['axis']],*before[9:12],1.,0.,0.,0.]
            solved=components[name].SetTransformAndSolve2(transform(target))
            doc.EditRebuild3;actual=pose(name)
            residual=max(abs(actual[i]-target[i]) for i in range(12))
            rows.append(dict(group=name,path=definitions[name].get('path'),kind=drive['kind'],axis=drive['axis'],target_degrees=30,solved=solved,
                residual=residual,passed=bool(solved) and residual<1e-6))
            components[name].SetTransformAndSolve2(transform(before));doc.EditRebuild3
    result=dict(status='passed' if all(r['passed'] for r in rows) else 'failed',
        assembly=build['entry'],joint_checks=rows,coverage='all' if groups is None else 'selected',parts=build['part_occurrences'],mates=build['mates'],
        limits=['Kinematic mate solve only; no interference, force or manufacturing qualification'])
    (folder/'motion-verification.json').write_text(json.dumps(result,indent=2))
    (folder/'motion-progress.json').write_text(json.dumps(dict(status=result['status'],completed=len(rows),total=len(selected))))
    print(result['status'],[(r['group'],r['passed']) for r in rows])
    return result
