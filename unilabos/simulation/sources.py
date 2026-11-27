"""逐端口来源屏障；消费真实采集身份，不生成传感值或完成判据。"""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePort:
    """单个输入端口的来源、单位、精确值类型和采样有效期合同。"""
    source: str
    unit: str
    value_type: type
    ttl_ticks: int = 2
    ref: str | None = None

    def __post_init__(self):
        """拒绝空来源、非法值类型、负有效期及无效 ref；有效期以确认步计数。"""
        if (not isinstance(self.source,str) or not self.source
                or not self.unit or self.value_type not in (bool,str,int,float)
                or type(self.ttl_ticks) is not int or self.ttl_ticks<0
                or self.ref is not None and (not isinstance(self.ref,str) or not self.ref)):
            raise ValueError('invalid_source_contract')


class SourceBarrier:
    """按世界身份逐帧验证来源样本，整帧通过后才更新可见证据。"""
    def __init__(self, ports, identity):
        """复制非空端口合同及 world_id/session_id/epoch，初始化空的已确认帧。"""
        if not ports or any(not isinstance(p,SourcePort) for p in ports.values()):
            raise ValueError('invalid_source_contract')
        self.ports=dict(ports)
        self.identity=dict(identity)
        if set(self.identity)!={'world_id','session_id','epoch'}:
            raise ValueError('source_identity_required')
        self.last={}

    def consume(self, frame, *, tick):
        """校验整帧并返回端口值副本。

        来源、ref、单位、质量、精确类型、代际、序号及采集 tick 必须满足
        合同；过期、时间倒退或相同序号改值均抛出 ValueError。坏帧不会
        部分更新 last；合法重复样本只在原有效期内可用。"""
        if type(tick) is not int or tick<0 or set(frame)!=set(self.ports):
            raise ValueError('source_frame_incomplete')
        accepted={}
        for name,port in self.ports.items():
            sample=frame[name]
            keys={'source','unit','value','quality','captured_tick','sequence',*self.identity}
            if port.ref is not None:keys.add('source_ref')
            if (set(sample)!=keys or port.ref is not None and sample.get('source_ref')!=port.ref
                    or any(sample[k]!=v for k,v in self.identity.items())
                    or sample['source']!=port.source or sample['unit']!=port.unit
                    or sample['quality']!='good' or type(sample['captured_tick']) is not int
                    or type(sample['sequence']) is not int or sample['sequence']<0
                    or not 0<=sample['captured_tick']<=tick
                    or tick-sample['captured_tick']>port.ttl_ticks):
                raise ValueError('source_sample_invalid: '+name)
            value=sample['value']
            if (type(value) is not port.value_type
                    or type(value) in (int,float) and not math.isfinite(value)):
                raise ValueError('source_value_invalid: '+name)
            old=self.last.get(name)
            if old and (sample['sequence']<old['sequence'] or sample['captured_tick']<old['captured_tick']
                    or sample['sequence']==old['sequence'] and sample!=old):
                raise ValueError('source_sample_relabelled: '+name)
            accepted[name]=deepcopy(value)
        # 整帧有效后再更新，坏帧不能局部刷新来源。
        self.last=deepcopy(frame)
        return accepted


def source_sample(identity, source, ref, unit, value, tick):
    """按生产端提供的采集 tick 封装样本，不改写来源、身份或采样时间。"""
    return dict(source=source,source_ref=ref,unit=unit,value=value,quality='good',
        captured_tick=tick,sequence=tick,**identity)
