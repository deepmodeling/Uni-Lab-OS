"""将实际包控制 RPC 接入 PLC-Sim 单世界确认调度；不另建时钟。"""
from __future__ import annotations

import queue
import threading
import time
from copy import deepcopy
from dataclasses import asdict

from .execution import WorldClient


class _WorldPort:
    """把单世界命令／扫描接口适配为 PLC-Sim 的确认步接口。"""
    def __init__(self, world):
        """保存注入的世界对象；时钟和代际仍由该对象及 CoupledSession 管理。"""
        self.world=world

    def reset(self, token, sample_channels):
        """拒绝原地重置；新的世界代际必须通过新 worker 建立。"""
        raise RuntimeError('new_worker_required_for_world_reset')

    def step(self, token, commands, sample_channels):
        """校验下一步令牌、命令通道和采样通道，依序发送控制后返回确认的 state 帧。"""
        from plc_sim.cosimulation import PhysicsFrame, Sample
        expected={**self.world.identity,'tick':self.world.tick+1,'dt_ns':50_000_000}
        if asdict(token)!=expected or set(commands)!={'device_commands'} or sample_channels!=('state',):
            raise ValueError('scheduled_frame_mismatch')
        for name,parameters in commands['device_commands']:
            self.world.command(name,**parameters)
        state=self.world.scan()
        return PhysicsFrame(token,token.dt_ns,{'state':Sample(token.tick,token.tick,state,True)})


class ScheduledWorldClient:
    """与 WorldClient 同一设备接缝，实际输出在冻结扫描内发送。"""
    def __init__(self,path,identity,*,timeout=120,rate=None,record=None,world=None):
        """在第零步观测上接入确认调度，并登记每步运行的设备控制器。

        可注入解析世界或使用 socket 世界；初始身份和 tick 必须匹配。
        rate 只控制墙钟节奏，不改变固定 50 ms 的积分步长。"""
        from plc_sim.cosimulation import Channel, CoupledSession, PhysicsFrame, Sample, StepToken
        self.world=world if world is not None else WorldClient(path,identity,timeout=timeout)
        if self.world.identity!=identity or self.world.tick!=0:raise ValueError("initial_world_identity_mismatch")
        self.identity=dict(identity)
        self.record=record or (lambda event:None)
        self.commands=[]
        self.mailbox=queue.Queue()
        self.single_request=None
        self.owner=threading.get_ident()
        self.session=CoupledSession(_WorldPort(self.world),dt_ns=50_000_000,
            channels={'state':Channel(ttl_ticks=0)},writers={'device_commands':'device'},
            world_id=identity['world_id'],session_id=identity['session_id'])
        self.session.register_controller('device',1,self._controls,reset=lambda token:None)
        state=self.world.observe()
        token=StepToken(**identity,tick=0,dt_ns=50_000_000)
        self.session.attach(PhysicsFrame(token,0,{'state':Sample(0,0,state,True)}))
        self.paced=rate is not None
        if rate is not None:self.session.set_rate(rate)
        self.session.resume()

    @property
    def tick(self):
        """返回已确认调度步号，不使用墙钟时间推算进度。"""
        return self.session.tick

    def observe(self):
        """返回最近已确认状态的深拷贝；没有有效采样时抛出 RuntimeError。"""
        sample=self.session.observation('state')
        if sample is None:raise RuntimeError('scheduled_observation_unknown')
        return deepcopy(sample.value)

    def command(self,name,**parameters):
        """在可接收控制的状态下复制并排队命令，留到下一次确认扫描发送。"""
        self.session._idle()
        if self.session.state not in ('running','paused'):raise RuntimeError('scheduled_world_not_running')
        self.commands.append((name,deepcopy(parameters)))

    def _controls(self,scan):
        """确认 state 输入有效后一次性取走待发命令，避免在后续扫描重复发送。"""
        if scan.inputs['state'] is None:raise RuntimeError('scheduled_input_unknown')
        commands,self.commands=self.commands,[]
        return {'device_commands':commands}

    def control(self,request):
        """供外部线程提交带身份的控制请求，并等待最多十秒的确认。

        本方法只入队，不推进世界；超时表示确认未知，不能据此推断请求未执行。"""
        if request.get("identity")!=self.identity:raise ValueError("control_identity_mismatch")
        if request.get("op") not in ("pause","resume","step","rate","status"):raise ValueError("control_unsupported")
        event=threading.Event();slot={}
        self.mailbox.put((dict(request),event,slot))
        if not event.wait(10):raise TimeoutError("control_ack_unknown")
        if "error" in slot:raise RuntimeError(slot["error"])
        return slot["result"]

    def pump_controls(self):
        """由调度所属线程处理控制队列；单步请求延迟到实际扫描确认后应答。"""
        self.session._idle()
        while True:
            try:request,event,slot=self.mailbox.get_nowait()
            except queue.Empty:break
            try:
                op=request["op"]
                if op=="pause":self.pause()
                elif op=="resume":self.resume()
                elif op=="rate":self.set_rate(request["rate"])
                elif op=="step":
                    if self.session.state!="paused" or self.single_request is not None:
                        raise RuntimeError("single_step_requires_idle_pause")
                    self.single_request=(request,event,slot)
                    continue
                slot["result"]={"state":self.session.state,"token":asdict(self.session.token),"rate":self.session.rate}
            except Exception as error:slot["error"]=str(error)
            event.set()

    def scan(self,*,single=False):
        """处理控制后推进一个确认步，记录证据并返回观测。

        暂停时等待单步或恢复，超过 120 秒转为故障；扫描或记录异常也
        锁定调度并通知待确认的单步调用方。倍率仅影响非单步运行的墙钟等待。"""
        started=time.monotonic()
        self.pump_controls()
        while self.session.state=="paused" and not single and self.single_request is None:
            if time.monotonic()-started>120:
                self.session.state="faulted"
                raise TimeoutError("paused_world_health_timeout")
            time.sleep(.01)
            self.pump_controls()
        pending=self.single_request
        single=single or pending is not None
        try:
            frame=self.session.step(single=single)
            self.record({'event':'scheduled.scan','token':asdict(frame.token),
                'commands':deepcopy(self.session._commands),'samples':list(frame.samples)})
        except BaseException:
            self.session.state='faulted'
            if pending:
                pending[2]["error"]="single_step_result_unknown";pending[1].set();self.single_request=None
            raise
        if pending:
            pending[2]["result"]={"state":self.session.state,"token":asdict(self.session.token)}
            pending[1].set();self.single_request=None
        if self.paced and not single:
            time.sleep(max(0.,self.session.wall_delay()-(time.monotonic()-started)))
        return self.observe()

    def pause(self):
        """将确认调度置为暂停；状态转换约束由 CoupledSession 校验。"""
        self.session.pause()
    def resume(self):
        """恢复可运行的调度会话；故障会话不会因此重新取得推进许可。"""
        self.session.resume()
    def set_rate(self,rate):
        """设置墙钟运行倍率并启用节奏控制，保留积分步长及子步数量。"""
        self.session.set_rate(rate);self.paced=True

    def invalidate(self):
        """来源、完成或结算失败后撤销控制许可；已经发出的步不宣称回滚。"""
        self.session._idle()
        self.session.state='faulted'
        self.commands.clear()
        if self.single_request:
            _,event,slot=self.single_request
            slot['error']='single_step_result_unknown';event.set();self.single_request=None
        invalidate=getattr(self.world,'invalidate',None)
        if invalidate is not None:invalidate()
