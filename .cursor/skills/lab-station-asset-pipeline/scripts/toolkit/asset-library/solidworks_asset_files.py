"""Materialize standalone CAD only for definitions not composed from other assets."""
import json,time,traceback,hashlib
from pathlib import Path


def run(sw,jobs_file,output,seconds=180):
    import pythoncom
    from win32com.client import VARIANT,Dispatch
    from solidworks_assembly import build
    out=Path(output);out.mkdir(exist_ok=True,parents=True);record=out/'completed.json'
    done=json.loads(record.read_text('utf8')) if record.exists() else {}
    jobs=json.loads(Path(jobs_file).read_text('utf8'));started=time.monotonic()
    empty=VARIANT(pythoncom.VT_DISPATCH,None);error=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0);warning=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)
    sw.DocumentVisible(False,1);sw.DocumentVisible(False,2)
    try:
        for job in jobs:
            if job['composed'] or job['asset_id'] in done:continue
            (out/'progress.json').write_text(json.dumps(job))
            recipe=json.loads(Path(job['recipe']).read_text('utf8'));folder=out/job['slug']
            if job['parts']==1 and job['joints']==0:
                folder.mkdir(exist_ok=False);row=recipe['groups'][0]['parts'][0];error.value=warning.value=0
                source=sw.OpenDoc6(row['native'],1,1,'',error,warning);assert source and error.value==0
                values=row['transform'];flat=[float(values[i][j]) for i in range(3) for j in range(3)]+[float(x) for x in values[3][:3]]+[1.,0.,0.,0.]
                utility=sw.GetMathUtility
                transform=Dispatch(utility._oleobj_.InvokeTypes(utility._oleobj_.GetIDsOfNames('CreateTransform'),0,
                    pythoncom.DISPATCH_METHOD,(pythoncom.VT_DISPATCH,0),((pythoncom.VT_VARIANT,1),),VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,flat)))
                bodies=[b.Copy() for b in source.GetBodies2(0,False)]
                part=sw.NewDocument(sw.GetUserPreferenceStringValue(8),0,0.,0.)
                for body in bodies:
                    assert body.ApplyTransform(transform);assert part.CreateFeatureFromBody3(body,False,0)
                entry=folder/(job['slug']+'.SLDPRT');assert part.Extension.SaveAs(str(entry),0,1,empty,error,warning);assert error.value==0
            else:
                build(sw,job['recipe'],folder)
                entry=folder/(recipe['name']+'.SLDASM')
            done[job['asset_id']]=dict(entry=str(entry),folder=str(folder),sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),parts=job['parts'],joints=job['joints'])
            record.write_text(json.dumps(done,ensure_ascii=False,indent=2),encoding='utf8')
            titles=[d.GetTitle for d in (sw.GetDocuments or [])]
            for title in titles:sw.CloseDoc(title)
            if time.monotonic()-started>seconds:break
    except Exception:
        (out/'error.txt').write_text(traceback.format_exc(),encoding='utf8');raise
    finally:sw.DocumentVisible(True,1);sw.DocumentVisible(True,2)
    print('Native asset files',len(done),'of',sum(not j['composed'] for j in jobs))
