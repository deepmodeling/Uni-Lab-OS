"""供正式 Edge 驱动路径使用的 PLC Sim 验收设备。"""
from __future__ import annotations
import time
from typing import TypedDict
from opcua import Client, ua
from unilabos.registry.decorators import device, action

class Result(TypedDict):
    success: bool
    message: str
    done: bool
    next_iteration: int

class Channel:
    """每次动作读取实际模拟器完成位，最长等待30秒。"""
    def __init__(self, channel: str = 'S06', endpoint: str = 'opc.tcp://127.0.0.1:24855/xuse_sim/'):
        self.channel=channel
        self.endpoint=endpoint

    @action(displayname='模拟工艺', description='发送模拟工艺，等待真实完成位并复位；循环结果按输入轮次产生')
    def run(self, label: str = '工艺', iteration: int = 1, stop_after: int = 3, hold_seconds: float = 2.0, dependency: str = '', dependency2: str = '') -> Result:
        """命令执行期间保留设备，方便网页观察资源争用。"""
        if not 0 <= hold_seconds <= 10:
            raise ValueError('hold_seconds 必须在0到10秒之间')
        client=Client(self.endpoint,timeout=5)
        client.connect()
        try:
            def read(suffix: str):
                return client.get_node('ns=4;s=上位机通讯|'+self.channel+suffix).get_value()
            def write(suffix: str, value):
                n=client.get_node('ns=4;s=上位机通讯|'+self.channel+suffix)
                n.set_value(ua.DataValue(ua.Variant(value,n.get_data_type_as_variant_type())))
            done='加工完成' if self.channel=='S06' else '工艺完成'
            if not read('允许加工') or read(done):
                raise RuntimeError('PLC 模拟工站不空闲')
            write('工艺选择',1)
            write('参数写入完成',True)
            deadline=time.monotonic()+30
            while not read(done):
                if time.monotonic()>deadline:
                    raise TimeoutError('PLC 完成状态未知，需检查模拟工站')
                time.sleep(.05)
            time.sleep(hold_seconds)
            write('参数写入完成',False)
            write('工艺选择',0)
            while read(done) or not read('允许加工'):
                if time.monotonic()>deadline:
                    raise TimeoutError('PLC 复位状态未知')
                time.sleep(.05)
            return {'success':True,'message':f'{self.channel} {label} 完成','done':iteration>=stop_after,'next_iteration':iteration+1}
        finally:
            client.disconnect()

@device(id='channel_a',display_name='并行验收 S06',category=['simulation'])
class ChannelA(Channel):
    """S06 的独立注册类型。"""
    @action(displayname='模拟工艺')
    def run(self, label: str = '工艺', iteration: int = 1, stop_after: int = 3, hold_seconds: float = 2.0, dependency: str = '', dependency2: str = '') -> Result:
        """调用公共 PLC Sim 执行实现。"""
        return super().run(label, iteration, stop_after, hold_seconds, dependency, dependency2)


@device(id='channel_b',display_name='并行验收 S07',category=['simulation'])
class ChannelB(Channel):
    """S07 的独立注册类型。"""
    @action(displayname='模拟工艺')
    def run(self, label: str = '工艺', iteration: int = 1, stop_after: int = 3, hold_seconds: float = 2.0, dependency: str = '', dependency2: str = '') -> Result:
        """调用公共 PLC Sim 执行实现。"""
        return super().run(label, iteration, stop_after, hold_seconds, dependency, dependency2)


@device(id='region',display_name='共同工作流资源范围',category=['simulation'])
class Region:
    """仅用于工作流范围占用的资源身份。"""
    @action(displayname='范围标记')
    def marker(self) -> Result:
        """返回标记；综合流程不调用此动作。"""
        return {'success':True,'message':'范围标记','done':True,'next_iteration':1}
