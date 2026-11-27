"""Exact composed USD runtime-content comparison for explicit lock migration."""
from core import canonical,sha,digest

def value(x):
    if x is None or isinstance(x,(bool,int,float,str)):return x
    if hasattr(x,'GetReal') and hasattr(x,'GetImaginary'):return [value(x.GetReal()),value(x.GetImaginary())]
    try:return [value(v) for v in x]
    except TypeError:return str(x)

def signature(path):
    from pxr import Usd,UsdGeom
    stage=Usd.Stage.Open(str(path))
    if not stage:raise ValueError('USD cannot open')
    rows={'stage':{'meters':UsdGeom.GetStageMetersPerUnit(stage),'up':str(UsdGeom.GetStageUpAxis(stage)),
                   'fps':stage.GetFramesPerSecond(),'tcps':stage.GetTimeCodesPerSecond(),'default':str(stage.GetDefaultPrim().GetPath())}}
    for prim in stage.TraverseAll():
        key=str(prim.GetPath());rows[key]={'type':prim.GetTypeName(),'active':prim.IsActive(),'instanceable':prim.IsInstanceable(),'schemas':prim.GetAppliedSchemas()}
        for a in prim.GetAttributes():
            rows[str(a.GetPath())]={'type':str(a.GetTypeName()),'default':value(a.Get()),'samples':[[t,value(a.Get(t))] for t in a.GetTimeSamples()]}
        for rel in prim.GetRelationships():rows[str(rel.GetPath())]={'targets':[str(x) for x in rel.GetTargets()]}
    return {k:sha(canonical(v)) for k,v in rows.items()}

def compare(old,new):
    a=signature(old);b=signature(new);diff=[k for k in a.keys()|b.keys() if a.get(k)!=b.get(k)]
    if diff:raise ValueError('USD runtime content differs: '+str(diff[:8]))
    return {'status':'passed','old_usd_sha256':digest(old),'new_usd_sha256':digest(new),'composed_runtime_signature':sha(canonical(a)),
            'scope':'Exact composed attributes/time samples, relationships, schemas, types, activation, instanceability and stage units; not custom provenance metadata or new dynamics'}
