"""Read a fixed vendor SolidWorks configuration in a private COM session."""
import argparse
from pathlib import Path
import subprocess
import time
import winreg
import psutil
import pythoncom
from contextlib import contextmanager
from win32com.client import Dispatch, VARIANT
from core import digest,read,write


def private_app(out):
    clsid=winreg.QueryValue(winreg.HKEY_CLASSES_ROOT,r'SldWorks.Application\CLSID')
    exe=Path(winreg.QueryValue(winreg.HKEY_CLASSES_ROOT,'CLSID\\'+clsid+r'\LocalServer32').strip('"'))
    if not exe.is_file():raise ValueError('SolidWorks executable missing')
    hidden=subprocess.STARTUPINFO();hidden.dwFlags|=subprocess.STARTF_USESHOWWINDOW
    proc=subprocess.Popen([str(exe),'/r','/b'],startupinfo=hidden)
    write(out.parent/'private-session.json',{'pid':proc.pid,'created':psutil.Process(proc.pid).create_time()})
    try:
        rot=pythoncom.GetRunningObjectTable();ctx=pythoncom.CreateBindCtx(0)
        until=time.monotonic()+90
        while time.monotonic()<until and proc.poll() is None:
            for m in rot.EnumRunning():
                if m.GetDisplayName(ctx,None)==f'SolidWorks_PID_{proc.pid}':
                    return Dispatch(rot.GetObject(m).QueryInterface(pythoncom.IID_IDispatch))
            time.sleep(.5)
        raise RuntimeError('Private SolidWorks session unavailable')
    except BaseException:
        if proc.poll() is None:proc.kill()
        raise


def close_private_app(sw,out):
    """Release only the private process identified by this invocation's receipt."""
    try:
        sw.CloseAllDocuments(True)
        sw.ExitApp()
    finally:
        owner=read(out.parent/'private-session.json')
        try:
            process=psutil.Process(owner['pid'])
            if process.name().lower()=='sldworks.exe' and process.create_time()==owner['created']:
                try:process.wait(timeout=10)
                except psutil.TimeoutExpired:process.kill()
        except psutil.NoSuchProcess:
            pass


@contextmanager
def private_session(out):
    """Serialize native CAD sessions owned by the converter, including alternate exchange."""
    import win32event,win32api
    lock=win32event.CreateMutex(None,False,'Local\\UniLab_Supplier_SolidWorks_Exporter')
    acquired=False
    try:
        result=win32event.WaitForSingleObject(lock,240000)
        if result not in (win32event.WAIT_OBJECT_0,win32event.WAIT_ABANDONED):
            raise TimeoutError('SolidWorks converter session is busy')
        acquired=True
        sw=private_app(out)
        try:yield sw
        finally:close_private_app(sw,out)
    finally:
        if acquired:win32event.ReleaseMutex(lock)
        win32api.CloseHandle(lock)


def capture_exchange(doc,out):
    """Preserve the part frame. Default STL SaveAs can translate to positive coordinates."""
    import struct
    import numpy as np
    bodies=list(doc.GetBodies2(0,False) or [])
    if len(bodies)!=1 or doc.GetBodies2(1,False):
        raise ValueError('Native exchange witness requires one solid and no sheet bodies')
    props=bodies[0].GetMassProperties(1)
    triangles=np.asarray(doc.GetTessTriangles(True),dtype=float).reshape(-1,9)*1000
    if not len(triangles) or not np.isfinite(triangles).all():
        raise ValueError('Invalid native part-frame tessellation')
    evidence=out.parent.parent/'runtime-evidence'
    evidence.mkdir(exist_ok=True)
    witness=evidence/'part-frame.stl'
    with witness.open('wb') as stream:
        stream.write(bytes(80));stream.write(struct.pack('<I',len(triangles)))
        for triangle in triangles:stream.write(struct.pack('<12fH',0,0,0,*triangle,0))
    xt=out.with_suffix('.x_t')
    err=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0);warn=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)
    doc.ClearSelection2(True)
    ok=doc.Extension.SaveAs(str(xt),0,1,VARIANT(pythoncom.VT_DISPATCH,None),err,warn)
    if not ok or err.value or warn.value or not xt.is_file() or not xt.stat().st_size:
        raise ValueError('Native part Parasolid export failed')
    def item(path):
        return {'path':path.relative_to(out.parent.parent).as_posix(),'sha256':digest(path),'original_name':path.name}
    return {'XT':item(xt),'STL':item(witness),'native_mass_properties':{
        'volume_mm3':props[3]*1e9,'area_mm2':props[4]*1e6},'witness_triangles':len(triangles)}


def export(path,code,root,out,require_named=False,recovery_exchange=False):
    path=path.resolve();root=root.resolve()
    def value(obj,name):
        v=getattr(obj,name)
        return v() if callable(v) and not hasattr(v,'_oleobj_') else v
    with private_session(out) as sw:
        sw.Visible=False
        err=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0);warn=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)
        asm=path.suffix.lower()=='.sldasm'
        doc=sw.OpenDoc6(str(path),2 if asm else 1,3,'',err,warn)
        if not doc or err.value or warn.value:raise ValueError(f'Native open error={err.value}, warning={warn.value}')
        if Path(value(doc,'GetPathName')).resolve()!=path:raise ValueError('Native CAD root mismatch')
        names=list(value(doc,'GetConfigurationNames') or [])
        matches=[x for x in names if x.casefold()==code.casefold()]
        write(out.parent/'configuration-list.json',{'source_sha256':digest(path),'names':names})
        if len(names)==1 and not require_named:cfg=names[0]
        elif len(matches)==1:cfg=matches[0]
        else:raise ValueError('Unsupported selection: no exact native SolidWorks configuration')
        if doc.ConfigurationManager.ActiveConfiguration.Name!=cfg and not doc.ShowConfiguration2(cfg):
            raise ValueError('Cannot activate selected configuration')
        if doc.ConfigurationManager.ActiveConfiguration.Name!=cfg:raise ValueError('Configuration mismatch')
        instances=[]
        if asm:
            for item in doc.GetComponents(False) or []:
                suppressed=bool(value(item,'IsSuppressed'));ref=value(item,'GetPathName')
                f=Path(ref).resolve() if ref else None
                virtual=bool(value(item,'IsVirtual'))
                if not virtual and (not f or not f.is_relative_to(root) or not f.is_file()):raise ValueError('Missing or external assembly dependency')
                if not suppressed and value(item,'GetModelDoc2') is None:raise ValueError('Unresolved assembly instance')
                instances.append(dict(instance=item.Name2,file=path.relative_to(root).as_posix() if virtual else f.relative_to(root).as_posix(),
                                      sha256=digest(path if virtual else f),virtual=virtual,
                                      configuration=item.ReferencedConfiguration,suppressed=suppressed))
            if not instances:raise ValueError('Empty native assembly')
        doc.ClearSelection2(True)
        ok=doc.Extension.SaveAs(str(out),0,1,VARIANT(pythoncom.VT_DISPATCH,None),err,warn)
        if not ok or err.value or warn.value or not out.is_file():raise ValueError(f'STEP export error={err.value}, warning={warn.value}')
        receipt=dict(configuration=cfg,configuration_names=names,instances=instances,step_sha256=digest(out))
        if recovery_exchange and not asm:
            try:
                if doc.ConfigurationManager.ActiveConfiguration.Name!=cfg:raise ValueError('Native exchange configuration changed')
                receipt['exchange']=capture_exchange(doc,out)
            except Exception as exc:
                receipt['optional_exchange_error']=str(exc)
        write(out.parent/'native.json',receipt)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('path',type=Path);p.add_argument('code')
    p.add_argument('root',type=Path);p.add_argument('out',type=Path)
    p.add_argument('--require-named',action='store_true')
    p.add_argument('--recovery-exchange',action='store_true')
    a=p.parse_args();export(a.path,a.code,a.root,a.out,a.require_named,a.recovery_exchange)
