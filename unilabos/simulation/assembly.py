"""编译包声明的来源与完成依赖；设备公式和采集实现留在设备包。"""
from dataclasses import dataclass
from types import MappingProxyType

from .sources import SourceBarrier, SourcePort


@dataclass(frozen=True)
class Provider:
    """不可变的端口提供者声明。

    source/ref 标识实际生产端；writer 标识唯一写入者，period_ticks 与
    ttl_ticks 使用确认扫描计数。completion=False 的来源仅供观察，
    不得作为完成策略的必需证据；本声明不负责执行采样。"""
    source: str
    ref: str
    unit: str
    value_type: type
    writer: str
    period_ticks: int = 1
    ttl_ticks: int = 2
    completion: bool = True

    def __post_init__(self) -> None:
        """校验来源合同、正整数采样周期和完成用途；非法声明抛出 ValueError。"""
        SourcePort(self.source,self.unit,self.value_type,self.ttl_ticks,self.ref)
        if not self.writer or type(self.period_ticks) is not int or self.period_ticks<1 or type(self.completion) is not bool:
            raise ValueError('provider_contract_invalid')


class SourceCatalog:
    """设备包提供的端口候选目录，集中拒绝歧义来源、冲突合同和重复写入者。"""
    def __init__(self, options: dict[str, list[Provider]]) -> None:
        """冻结各端口候选序列，并检查同端口类型／单位和跨端口 ref 的一致性。"""
        self.options={name:tuple(choices) for name,choices in options.items()}
        refs={}
        for name,choices in self.options.items():
            if not name or not choices or any(not isinstance(p,Provider) for p in choices):
                raise ValueError('source_catalog_invalid')
            if len({(p.source,p.ref) for p in choices})!=len(choices):
                raise ValueError('source_multiple_writers: '+name)
            if len({(p.unit,p.value_type) for p in choices})!=1:
                raise ValueError('source_contract_conflict: '+name)
            for p in choices:
                if p.ref in refs and refs[p.ref]!=p:raise ValueError('source_ref_conflict: '+p.ref)
                refs[p.ref]=p

    def select(self, overrides: dict, defaults: dict) -> dict[str, Provider]:
        """合并默认选择与逐端口覆盖，返回端口到 Provider 的映射。

        字符串必须唯一匹配 source；字典必须恰含 source/ref。未知端口、
        缺失默认值或零个／多个匹配均抛出 ValueError，不尝试其他来源。"""
        if not isinstance(overrides,dict) or set(overrides)-set(self.options) or set(defaults)!=set(self.options):
            raise ValueError('source_ports_invalid')
        result={}
        for name,choices in self.options.items():
            selected=overrides.get(name,defaults[name])
            if isinstance(selected,str):matches=[p for p in choices if p.source==selected]
            elif isinstance(selected,dict) and set(selected)=={'source','ref'}:
                matches=[p for p in choices if (p.source,p.ref)==(selected['source'],selected['ref'])]
            else:raise ValueError('source_selection_invalid: '+name)
            if len(matches)!=1:raise ValueError('source_unknown_or_ambiguous: '+name)
            result[name]=matches[0]
        return result


class SourceAssembly:
    """读所选提供者的完整样本；不补写、刷新或重标提供者元数据。"""
    def __init__(self, selected: dict[str, Provider], readers: dict, identity: dict) -> None:
        """绑定选中提供者的读取函数与世界身份；缺少可调用读取端时拒绝装配。"""
        self.selected=MappingProxyType(dict(selected))
        if any(not callable(readers.get(p.ref)) for p in selected.values()):
            raise ValueError('source_reader_not_ready')
        self.readers={p.ref:readers[p.ref] for p in selected.values()}
        self.barrier=SourceBarrier({name:SourcePort(p.source,p.unit,p.value_type,p.ttl_ticks,p.ref)
            for name,p in selected.items()},identity)

    def read(self, context: dict, *, tick: int) -> dict:
        """逐端口采样并以 tick 校验整帧，返回值映射；保留生产端原始来源和采集时间。"""
        frame={name:self.readers[p.ref](context) for name,p in self.selected.items()}
        return self.barrier.consume(frame,tick=tick)


def resolve_completion(selection, policies: dict[str, set[str]], ports: dict[str, Provider]) -> tuple[str, tuple[str,...]]:
    """解析策略名及必需端口，返回策略名和排序后的端口元组。

    允许追加已登记、可用于完成判定的端口；不得删除策略固有依赖。
    未知策略、重复依赖或显示专用来源均抛出 ValueError。"""
    if isinstance(selection,str):name=selection;required=policies.get(name)
    elif isinstance(selection,dict) and set(selection)=={'policy','required_ports'}:
        name=selection['policy'];required=selection['required_ports']
        if not isinstance(required,list) or any(not isinstance(p,str) for p in required) or len(required)!=len(set(required)):
            raise ValueError('completion_ports_invalid')
    else:raise ValueError('completion_selection_invalid')
    if not isinstance(name,str) or name not in policies or required is None:
        raise ValueError('completion_policy_unknown')
    required=set(required)
    if not policies[name]<=required or not required<=set(ports) or any(not ports[p].completion for p in required):
        raise ValueError('completion_dependencies_incompatible')
    return name,tuple(sorted(required))
