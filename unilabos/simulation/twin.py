"""数字孪生跟随的公共层：与传输协议无关（TCP、PLC、串口……）。

三种整站模式共用设备包的同一套“行为表”：

* 纯仿真（sim）：驱动连该协议的模拟端（TCP 模拟服务器、PLC 仿真、虚拟串口……），模拟端收到命令后
  调行为表出运动、按标称时长应答；
* 孪生（twin）：驱动连真机；驱动把每条命令的“发出 / 应答”写进命令日志，本模块读日志得到
  :class:`CommandEvent`，交给同一套行为表演出运动，真机应答成功才提交状态变化。只读，不发命令；
* 真机（real）：驱动连真机，不出运动。

协议相关的只有两处，由设备包按协议实现：

* :class:`JournalSource` —— 把该协议驱动的命令日志读成 :class:`CommandEvent`（Bioyond TCP 读 sqlite 命令
  日志的发送原文；PLC 可读写寄存器的记录；串口可读收发帧记录）；
* :class:`BehaviourModel` —— 行为表入口：``begin(event)`` 排入运动并返回“应答成功后提交状态”的回调。

运动输出与模拟端相同（设备包的运动流 → ``UNILABOS_OBSERVATION_FEED_FILE``），孪生时把
``UNILABOS_OBSERVATION_FEED_PROVIDER`` 设为 ``twin-feed`` 以便区分来源。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandEvent:
    """协议无关的一条设备命令：谁、做什么、参数、何时发出、何时有结果、成没成。"""

    command_id: str
    device_id: str
    action: str
    params: Mapping[str, Any]
    sent_at: str | None
    finished_at: str | None = None
    ok: bool | None = None          # None = 还没有结果
    raw: Any = None                  # 协议原文（行为表需要时用，例如 Bioyond 的执行帧）


class JournalSource(Protocol):
    """把某协议驱动的命令日志读成事件；每次调用返回当前全部已知命令（可重复，按 command_id 去重）。"""

    def events(self) -> Iterable[CommandEvent]: ...


class BehaviourModel(Protocol):
    """设备包的行为表：开始一条命令时排入运动，返回“成功后提交状态”的回调（可为 None）。"""

    def begin(self, event: CommandEvent) -> Callable[[], None] | None: ...

    def commit(self, after: Callable[[], None]) -> None: ...


@dataclass
class _Tracked:
    started: bool = False
    finished: bool = False
    after: Callable[[], None] | None = None


@dataclass
class TwinFollower:
    """轮询命令日志：新发出的命令交给行为表演运动，成功应答后提交状态。``step()`` 处理一轮。"""

    source: JournalSource
    behaviours: BehaviourModel
    poll_s: float = 0.1
    tracked: dict[str, _Tracked] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def step(self) -> int:
        changes = 0
        try:
            events = list(self.source.events())
        except Exception as error:  # noqa: BLE001 - 日志正被驱动写入时可能短暂不可读
            logger.debug("[Twin] journal not readable: %s", error)
            return 0
        for event in events:
            tracked = self.tracked.setdefault(event.command_id, _Tracked())
            if tracked.finished:
                continue
            if not tracked.started and event.sent_at:
                tracked.started = True
                try:
                    tracked.after = self.behaviours.begin(event)
                except Exception as error:  # noqa: BLE001 - 行为表不认识的命令只记录，不出运动
                    logger.info("[Twin] no behaviour for %s %s: %s", event.device_id, event.action, error)
                self.history.append({"command_id": event.command_id, "device_id": event.device_id,
                                     "action": event.action, "kind": "started", "at": event.sent_at})
                changes += 1
            if tracked.started and event.ok is not None:
                tracked.finished = True
                if event.ok and tracked.after is not None:
                    self.behaviours.commit(tracked.after)
                self.history.append({"command_id": event.command_id, "device_id": event.device_id,
                                     "action": event.action, "kind": "finished", "ok": event.ok,
                                     "at": event.finished_at})
                changes += 1
        return changes

    def start(self) -> "TwinFollower":
        self._thread = threading.Thread(target=self._run, name="twin-follower", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.poll_s)
