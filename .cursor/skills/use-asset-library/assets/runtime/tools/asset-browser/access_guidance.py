"""面向首次使用者的 OSS 诊断；只返回安全的错误类别和后续步骤。"""
import json
from pathlib import Path

GUIDE = '请按 use-asset-library 技能的 OSS 初始化指引完成配置。'
SETUP = '在本机安全配置 OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET；临时凭据还需 OSS_SESSION_TOKEN。不要把密钥粘贴到聊天、网页或技能包中。配置后重启桥接服务再重试。'


class AccessSetupError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_config(path):
    if not path or not Path(path).is_file():
        raise AccessSetupError('oss_not_configured', '尚未配置 OSS 地址。'+GUIDE)
    config=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if config.get('mode')=='local_only':
        raise AccessSetupError('local_only', '当前为离线模式，可继续使用完整的本地模型；缺失模型需要配置 OSS。'+GUIDE)
    if not all(config.get(k) for k in ('endpoint','region','bucket','prefix')):
        raise AccessSetupError('oss_not_configured', 'OSS 连接配置不完整，需要 endpoint、region、bucket、prefix。'+GUIDE)
    return config


def safe_error(exc):
    if isinstance(exc,AccessSetupError):return {'code':exc.code,'message':str(exc)}
    status=getattr(exc,'status',None)
    code=getattr(exc,'code','')
    if status in (401,403) or code in ('AccessDenied','InvalidAccessKeyId','SignatureDoesNotMatch','SecurityTokenExpired','InvalidSecurityToken'):
        return {'code':'oss_access_denied','message':'OSS 拒绝访问：凭据无效、已过期或没有目标前缀的读取权限。'+GUIDE+' '+SETUP}
    if status==404:
        return {'code':'oss_object_missing','message':'OSS 对象不存在。请核对 Bucket、prefix 和发布快照；这不一定是权限问题。'+GUIDE}
    if type(exc).__module__.startswith(('oss2','requests','urllib')):
        return {'code':'oss_unavailable','message':f'无法连接 OSS（HTTP {status}）。请先检查网络和 endpoint；网络恢复后重试。'}
    if isinstance(exc,ModuleNotFoundError) and exc.name in ('oss2','requests'):
        return {'code':'oss_dependency_missing','message':'请先运行 python -m pip install -r assets/requirements-oss.txt，再重试。'}
    return {'code':'operation_failed','message':str(exc)}
