"""将入口、来源目录及完成依赖编译为可复用的不可变装配。"""
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

from .assembly import resolve_completion


@dataclass(frozen=True)
class Composition:
    """保存已编译提供者，运行时直接绑定读端，不重复解析来源选择。"""
    attachment: str
    feedback: str
    completion: str
    selected: object
    scheduler: str
    policy: str
    required_ports: tuple

    def __post_init__(self):
        """复制并冻结提供者映射，隔离调用方后续配置修改。"""
        object.__setattr__(self, 'selected', MappingProxyType(dict(self.selected)))

    @property
    def sources(self):
        """返回可序列化的来源描述，不暴露读取函数或可变内部状态。"""
        return {name:dict(source=p.source,ref=p.ref) for name,p in self.selected.items()}

    def describe(self):
        """生成与既有证据格式一致的七字段装配记录。"""
        return dict(attachment=self.attachment,feedback=self.feedback,completion=self.completion,
            sources=self.sources,scheduler=self.scheduler,policy=self.policy,required_ports=self.required_ports)

    @property
    def digest(self):
        """计算稳定 JSON 摘要，将配置与运行证据关联。"""
        return hashlib.sha256(json.dumps(self.describe(),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def compile_composition(config, *, catalog, defaults, policies, aliases, completion_modes):
    """一次选择来源并校验完成依赖；设备语义和允许模式由声明提供。"""
    if not isinstance(config,dict) or set(config)-{'attachment','feedback','completion','sources','scheduler'}:
        raise ValueError('composition_fields_invalid')
    attachment=config.get('attachment','direct')
    scheduler=config.get('scheduler','coupled')
    if attachment not in ('direct','protocol'):
        raise ValueError('composition_unsupported')
    if scheduler not in ('legacy','coupled'):
        raise ValueError('scheduler_unsupported')
    selected=catalog.select(config.get('sources',{}),defaults)
    choice=config.get('completion','feedback')
    choice=aliases.get(choice,choice) if isinstance(choice,str) else choice
    policy,required=resolve_completion(choice,policies,selected)
    return Composition(attachment,config.get('feedback','physics'),completion_modes[policy],selected,scheduler,policy,required)
