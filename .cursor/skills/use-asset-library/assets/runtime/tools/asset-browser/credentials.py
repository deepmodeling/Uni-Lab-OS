"""Optional Windows per-user DPAPI storage; never ship credentials with the add-on."""
import os,json,ctypes
from access_guidance import AccessSetupError, GUIDE, SETUP
from pathlib import Path
KEYS=('OSS_ACCESS_KEY_ID','OSS_ACCESS_KEY_SECRET')
def credential_path():return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'UniLab/AssetBrowser/oss.dpapi'
def transform(data,decrypt=False):
    if os.name!='nt':raise ValueError('此平台请通过环境变量提供 OSS 凭据')
    class Blob(ctypes.Structure):_fields_=[('cbData',ctypes.c_ulong),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data);source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));result=Blob()
    crypt=ctypes.windll.crypt32
    function=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    ok=function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result))
    if not ok:raise ValueError('系统凭据保护失败')
    try:return ctypes.string_at(result.pbData,result.cbData)
    finally:ctypes.windll.kernel32.LocalFree(result.pbData)
def save_credentials():
    if not all(os.environ.get(k) for k in KEYS):raise ValueError('请先设置 OSS 凭据环境变量')
    path=credential_path();path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(transform(json.dumps({k:os.environ[k] for k in KEYS}).encode()))
    return {'status':'passed','storage':'Windows DPAPI / current user','path':str(path)}
def load_credentials():
    if all(os.environ.get(k) for k in KEYS):return
    path=credential_path()
    if not path.exists():raise AccessSetupError('oss_credentials_missing','未找到 OSS 凭据。本地完整资产仍可离线使用。'+GUIDE+' '+SETUP)
    try:values=json.loads(transform(path.read_bytes(),True))
    except (ValueError,OSError):raise AccessSetupError('oss_credentials_unreadable','无法读取当前用户保存的 OSS 凭据。'+SETUP) from None
    for k in KEYS:os.environ[k]=values[k]
