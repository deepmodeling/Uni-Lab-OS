"""实际控制队列进入原 CoupledSession；控制线程不推进世界。"""
import threading
import time
from copy import deepcopy

import pytest

from unilabos.simulation.scheduled_world import ScheduledWorldClient

pytest.importorskip("plc_sim.cosimulation", reason="需安装可选 twin-simulation 依赖")

IDENTITY=dict(world_id='world',session_id='session',epoch=0)


class World:
    """可记录命令并注入确认丢失的内存世界，用于检验调度接口的外部效果。"""
    def __init__(self,path,identity,timeout=120):
        """初始化第零步、位置和命令记录，默认扫描确认正常。"""
        self.identity=identity;self.tick=0;self.applied=[];self.position=0;self.fail=False
    def observe(self):
        """返回当前步号与位置，不推进测试世界。"""
        return dict(tick=self.tick,position=self.position)
    def command(self,name,**params):
        """记录命令发生步并更新测试位置，使重复发送可以被检出。"""
        self.applied.append((self.tick,name,params));self.position=params['position']
    def scan(self):
        """推进一次测试步；启用故障时抛出确认未知错误。"""
        if self.fail:raise ConnectionError('unknown_ack')
        self.tick+=1
        return self.observe()


def create(monkeypatch,record=None):
    """替换 socket 世界为内存适配器，并创建使用真实 CoupledSession 的调度客户端。"""
    monkeypatch.setattr('unilabos.simulation.scheduled_world.WorldClient',World)
    return ScheduledWorldClient('unused',IDENTITY,record=record)


def test_commands_are_applied_once_inside_confirmed_scan(monkeypatch):
    """验证排队命令只在确认扫描内应用一次，日志与物理步号保持一致。"""
    records=[];w=create(monkeypatch,records.append)
    w.command('move',position=3)
    assert w.world.applied==[]
    assert w.scan()=={'tick':1,'position':3}
    w.scan()
    assert w.world.applied==[(0,'move',{'position':3})]
    assert records[0]['commands']=={'device_commands':[('move',{'position':3})]}
    assert w.tick==w.world.tick==2


def test_pause_single_resume_via_mailbox(monkeypatch):
    """验证跨线程暂停／单步／恢复的确认时序及错误代际拒绝。"""
    w=create(monkeypatch)
    def control(op):
        """在另一线程提交控制请求，等待入队后由所属线程处理并返回线程及结果槽。"""
        output=[]
        t=threading.Thread(target=lambda:output.append(w.control(dict(op=op,identity=IDENTITY))))
        t.start()
        deadline=time.monotonic()+2
        while w.mailbox.empty():
            assert time.monotonic()<deadline
            time.sleep(.001)
        w.pump_controls()
        return t,output
    t,result=control('pause');t.join()
    assert result[0]['state']=='paused' and w.world.tick==0
    t,result=control('step')
    assert t.is_alive() and w.world.tick==0
    w.scan(single=True);t.join()
    assert result[0]['token']['tick']==1 and w.session.state=='paused'
    t,result=control('resume');t.join()
    w.scan();assert w.tick==2
    with pytest.raises(ValueError,match='identity'):w.control(dict(op='pause',identity={**IDENTITY,'epoch':4}))


@pytest.mark.parametrize('failure',['ack','log'])
def test_unknown_or_log_failure_never_advances_again(monkeypatch,failure):
    """验证世界确认丢失或证据写入失败后不再发送命令或推进世界。"""
    def record(event):
        """在日志故障变体中抛出磁盘错误，检验记录失败的停机语义。"""
        if failure=='log':raise OSError('disk')
    w=create(monkeypatch,record);w.command('move',position=3)
    w.world.fail=failure=='ack'
    with pytest.raises((ConnectionError,OSError)):w.scan()
    count=len(w.world.applied);tick=w.world.tick
    with pytest.raises(RuntimeError):w.scan()
    assert w.world.tick==tick and len(w.world.applied)==count


def test_rate_changes_pacing_not_integration(monkeypatch):
    """验证倍率只改变墙钟等待，确认步计数和累计仿真时间不变。"""
    waits=[];w=create(monkeypatch)
    monkeypatch.setattr('unilabos.simulation.scheduled_world.time.sleep',waits.append)
    monkeypatch.setattr('unilabos.simulation.scheduled_world.time.monotonic',lambda:0.)
    for rate in (.25,1,4,5,10):w.set_rate(rate);w.scan()
    assert w.tick==5 and w.session.now()==.25
    assert waits==pytest.approx([.2,.05,.0125,.01,.005])


@pytest.mark.parametrize('failure',['source','receipt'])
def test_device_failure_also_revokes_control_step_and_resume(monkeypatch,failure):
    """验证来源或结算失败后，动作和外部单步／恢复入口同时失效。"""
    from unilabos.simulation.execution import DeviceWorldExecutor
    w=create(monkeypatch)
    class Model:
        """可在来源消费阶段失败的最小设备模型，用来观察执行器故障传播。"""
        def command(self,*args):
            """将一次移动命令排入真实调度队列。"""
            w.command('move',position=4)
        def accept_state(self,state):
            """在来源故障变体中拒绝已推进的状态。"""
            if failure=='source':raise ValueError('bad_source')
        def completion(self):
            """返回当前物理步证明，使非来源故障变体进入结算路径。"""
            return {'physical_tick':w.tick}
        def reset_command(self):
            """此最小模型没有协议请求位，清理操作无需副作用。"""
            pass
    def settle(*args):
        """模拟规范提交确认未知，强制检验结算异常后的控制撤权。"""
        raise RuntimeError('receipt_ack_unknown')
    executor=DeviceWorldExecutor(w,Model(),settle=settle,authorize=lambda r:None,record=lambda e:None)
    with pytest.raises((ValueError,RuntimeError)):
        executor.execute(dict(identity=dict(task_uuid='t',node_job_uuid='j'),operation='move',parameters={}))
    tick=w.world.tick
    assert tick>0 and w.session.state=='faulted' and not w.commands
    for op in ('resume','step'):
        errors=[]
        def call():
            """在外部线程发送控制请求并收集故障应答，避免异常丢失在线程中。"""
            try:w.control(dict(op=op,identity=IDENTITY))
            except RuntimeError as error:errors.append(str(error))
        thread=threading.Thread(target=call);thread.start()
        deadline=time.monotonic()+2
        while w.mailbox.empty():
            assert time.monotonic()<deadline
            time.sleep(.001)
        w.pump_controls();thread.join(timeout=2)
        assert errors and not thread.is_alive()
    with pytest.raises(RuntimeError):w.scan(single=True)
    assert w.world.tick==tick
