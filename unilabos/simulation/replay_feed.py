"""可选：在工站后端进程内按墙钟回放已记录的设备观测。

设置 ``UNILABOS_OBSERVATION_REPLAY_FILE`` 指向 JSONL 记录后，后端在启用设备观测
（``UNILABOS_OBSERVATIONS_ENABLED=true``）时启动一个守护线程：等待记录中的设备
在原生库存中出现，经 ``ReplayProvider``（来源模式 ``replay``，不执行任何动作、
不结算库存）把每行关节值作为 observed 样本发布，供 ``/device-state/observations``
的快照/SSE 读取。

记录格式：第一行为表头 ``{"schema": "...", "devices": {device_id: [property, ...]}}``，
表头可选 ``select_by``（``id`` 或 ``name``，默认 ``id``）；可选 ``descriptors``
（``{device_id: 观测描述}``）经 ``describe`` 登记，设备模板无需声明 ``metadata.observations``，
例如只有模型的场景节点回放 ``matrix4`` 世界位姿。
其后每行 ``{"t": 墙钟秒, "event": 来源动作, "values": {device_id: {property: value}}}``。
``UNILABOS_OBSERVATION_REPLAY_LOOP``（默认 true）控制播完后是否从头循环；
``UNILABOS_OBSERVATION_REPLAY_GROUP`` 指定来源组名（默认 ``replay``）。
``UNILABOS_OBSERVATION_REPLAY_FILE`` 也可以是 ``.mcap``（高频轨迹，见 ``mcap_record``），或用
``os.pathsep`` 分隔的多个源，按同一时间原点合并播放。
回放结果只用于显示，不能作为设备执行、碰撞或物理验收证据。

编译后回放（运动轨迹 + 物料状态）用同一格式：物料也是库存节点，物料状态就是物料节点上的
观测属性，例如 ``world_matrix``（matrix4，世界位姿）、``parent``（string，当前父节点 id）、
``site``（string，父节点上的库位）、``visible``（boolean）；非数值属性的 ``unit`` 写 ``-``。
暂停时不再有新样本，描述里的 ``ttl_seconds`` 要大于预期的暂停时长，否则读端会把值看成过期。表头可选 ``commands``
（``[{"id", "t_start", "t_end", "label"?}]``）给出每条命令的时间区间；缺省时从行的 ``event``
（如 ``command_109+110``）推出。加载时校验：每行只能写表头声明过的节点和属性，``t`` 不能倒退。

播放可控制（``/device-state/replay``）：暂停、继续、倍速、跳到某个时间或某条命令。跳转时
把目标时刻之前每个属性的最后一个值合成一批发布，所以跳转后关节和物料状态立即一致。
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from unilabos.runtime.observations import Descriptor, Selector, StateError

logger = logging.getLogger(__name__)

ENV_FILE = "UNILABOS_OBSERVATION_REPLAY_FILE"
ENV_LOOP = "UNILABOS_OBSERVATION_REPLAY_LOOP"
ENV_GROUP = "UNILABOS_OBSERVATION_REPLAY_GROUP"


def _read_header(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        header = json.loads(stream.readline())
    devices = header.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("回放记录表头必须声明 devices")
    return header


_COMMAND_EVENT = re.compile(r"^command_(\d+(?:\+\d+)*)$")


def load_record(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读入并校验整份记录；问题在启动时报出，而不是播放到一半让线程退出。"""
    header = _read_header(path)
    for device, body in (header.get("descriptors") or {}).items():
        try:
            Descriptor.model_validate(body)
        except ValueError as error:
            raise ValueError(f"回放记录表头里 {device} 的属性描述无效：{error}") from error
    declared = {device: set(properties) for device, properties in header["devices"].items()}
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    previous = float("-inf")
    with path.open("r", encoding="utf-8") as stream:
        stream.readline()
        for number, line in enumerate(stream, start=2):
            if not line.strip():
                continue
            row = json.loads(line)
            t = float(row["t"])
            if t < previous:
                problems.append(f"第 {number} 行 t={t} 早于上一行 {previous}")
            previous = t
            for device, properties in (row.get("values") or {}).items():
                if device not in declared:
                    problems.append(f"第 {number} 行写了表头未声明的节点 {device}")
                else:
                    unknown = set(properties) - declared[device]
                    if unknown:
                        problems.append(f"第 {number} 行节点 {device} 有未声明属性 {sorted(unknown)}")
            if len(problems) >= 10:
                break
            rows.append(row)
    if problems:
        raise ValueError("回放记录不一致：" + "；".join(problems))
    return header, rows


def load_sources(spec: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """一个或多个回放源（``os.pathsep`` 分隔）：``.mcap`` 为高频轨迹文件，其余为 JSONL 回放记录。

    多个源按同一时间原点合并（例如 MCAP 里的关节/位姿 + 由数据库生成的作业区间与物料状态），
    合并后按 JSONL 同样的规则校验。
    """
    from unilabos.simulation.mcap_record import merge_records, read_mcap

    paths = [Path(item) for item in str(spec).split(os.pathsep) if item.strip()]
    parts = [read_mcap(path) if path.suffix.lower() == ".mcap" else load_record(path) for path in paths]
    header, rows = parts[0] if len(parts) == 1 else merge_records(*parts)
    if not header["devices"]:
        raise ValueError("回放源里没有任何节点")
    declared = {device: set(names) for device, names in header["devices"].items()}
    for row in rows:
        for device, values in (row.get("values") or {}).items():
            if device not in declared or set(values) - declared[device]:
                raise ValueError(f"回放源合并后节点 {device} 的属性未在表头声明")
    return header, rows


def command_index(header: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条命令的时间区间：表头 ``commands`` 优先，否则按行 event 推出。"""
    declared = header.get("commands")
    if declared:
        return [{"id": str(item["id"]), "t_start": float(item["t_start"]), "t_end": float(item["t_end"]),
                 **({"label": item["label"]} if item.get("label") else {})} for item in declared]
    spans: dict[str, dict[str, Any]] = {}
    for row in rows:
        match = _COMMAND_EVENT.match(str(row.get("event") or ""))
        if not match:
            continue
        for command in match.group(1).split("+"):
            span = spans.setdefault(command, {"id": command, "t_start": float(row["t"]), "t_end": float(row["t"])})
            span["t_end"] = float(row["t"])
    return sorted(spans.values(), key=lambda item: (item["t_start"], item["id"]))


def state_at(rows: list[dict[str, Any]], end: int) -> dict[str, dict[str, Any]]:
    """``rows[:end]`` 播完后每个节点每个属性的最后一个值。"""
    state: dict[str, dict[str, Any]] = {}
    for row in rows[:end]:
        for device, properties in (row.get("values") or {}).items():
            state.setdefault(device, {}).update(properties)
    return state


LOG_INTERVAL_S = 5.0


def now_log(feed) -> bool:
    """每 ``LOG_INTERVAL_S`` 秒放行一条进度日志，便于从后端日志确认回放在推进。"""
    now = time.monotonic()
    if now - getattr(feed, "_logged_at", 0.0) < LOG_INTERVAL_S:
        return False
    feed._logged_at = now
    return True


class ObservationReplayFeed:
    """读取 JSONL 记录并经 ReplayProvider 发布；线程安全的停止由 ``stop`` 控制。"""

    def __init__(self, observations, sources, path: Path, *, group: str = "replay", loop: bool = True,
                 wait_interval_s: float = 2.0, group_members: list[str] | None = None,
                 allowed: dict[str, set[str]] | None = None):
        self.observations = observations
        self.sources = sources
        self.spec = str(path)
        # 接管已有来源组（例如仿真跟随组）时，组成员必须与原组一致：用原组成员 id 配置。
        self.group_members = group_members
        self.path = Path(self.spec.split(os.pathsep)[0])
        self.group = group
        self.loop = loop
        self.wait_interval_s = wait_interval_s
        self.header, self.rows = load_sources(self.spec)
        if allowed:
            self._restrict(allowed)
        self.times = [float(row["t"]) for row in self.rows]
        self.commands = command_index(self.header, self.rows)
        self.duration = self.times[-1] if self.times else 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rows_published = 0
        # 播放状态；_cond 保护下面几项，控制接口改完后 notify 播放线程重新对时。
        self._cond = threading.Condition()
        self.paused = False
        self.speed = 1.0
        self.position = 0.0
        self.event: str | None = None
        self.loops = 0
        self._seek: float | None = None
        self._reanchor = False

    def _restrict(self, allowed: dict[str, set[str]]) -> None:
        """只回放节点当前观测描述里声明过的属性（数据库属性历史里的驱动状态量不一定是观测属性）。"""
        dropped: dict[str, set[str]] = {}
        for node, names in list(self.header["devices"].items()):
            if node not in allowed:
                continue
            keep = [name for name in names if name in allowed[node]]
            dropped.update({node: set(names) - set(keep)} if set(names) - set(keep) else {})
            self.header["devices"][node] = keep
            if node in (self.header.get("descriptors") or {}):
                properties = self.header["descriptors"][node].get("properties") or {}
                self.header["descriptors"][node]["properties"] = {k: v for k, v in properties.items() if k in keep}
        self.header["devices"] = {node: names for node, names in self.header["devices"].items() if names}
        for row in self.rows:
            row["values"] = {node: {k: v for k, v in values.items() if node not in allowed or k in allowed[node]}
                             for node, values in row["values"].items()}
            row["values"] = {node: values for node, values in row["values"].items() if values}
        if dropped:
            logger.info("[ObservationReplay] skipped properties not declared as observations: %s",
                        {node: sorted(names) for node, names in dropped.items()})

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="observation-replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    # ---- 播放控制 ----
    def status(self) -> dict[str, Any]:
        with self._cond:
            return {"file": self.path.name, "sources": [Path(item).name for item in self.spec.split(os.pathsep) if item],
                    "schema": self.header.get("schema"), "duration_s": self.duration,
                    "position_s": round(self.position, 3), "event": self.event, "paused": self.paused,
                    "speed": self.speed, "loop": self.loop, "loops": self.loops, "rows": len(self.rows),
                    "rows_published": self.rows_published, "devices": sorted(self.header["devices"]),
                    "commands": self.commands,
                    # 记录表头里的展示辅助信息（如枪头位图的位序）原样带给前端。
                    **{key: self.header[key] for key in ("tip_order",) if key in self.header}}

    def pause(self) -> dict[str, Any]:
        with self._cond:
            self.paused = True
            self._cond.notify_all()
        return self.status()

    def resume(self) -> dict[str, Any]:
        with self._cond:
            self.paused = False
            self._reanchor = True
            self._cond.notify_all()
        return self.status()

    def set_speed(self, speed: float) -> dict[str, Any]:
        if not 0 < speed <= 100:
            raise ValueError("speed 须在 (0, 100] 内")
        with self._cond:
            self.speed = float(speed)
            self._reanchor = True
            self._cond.notify_all()
        return self.status()

    def seek(self, *, t: float | None = None, command: str | int | None = None) -> dict[str, Any]:
        """跳到时间 t（秒）或某条命令的开始；暂停状态保持不变。"""
        if command is not None:
            span = next((item for item in self.commands if item["id"] == str(command)), None)
            if span is None:
                raise KeyError(f"记录里没有命令 {command}")
            t = span["t_start"]
        if t is None:
            raise ValueError("seek 需要 t 或 command")
        with self._cond:
            self._seek = min(max(float(t), 0.0), self.duration)
            self._cond.notify_all()
        return self.status()

    def _provider(self):
        from unilabos.simulation.replay import ReplayProvider

        by = self.header.get("select_by", "id")
        selectors = [Selector(by=by, value=device_id) for device_id in self.header["devices"]]
        for device_id, body in (self.header.get("descriptors") or {}).items():
            self.observations.describe(Selector(by=by, value=device_id), Descriptor.model_validate(body))
        group_selectors = ([Selector(by="id", value=member) for member in self.group_members]
                           if self.group_members else selectors)
        provider = ReplayProvider(self.observations, self.group, group_selectors, sources=self.sources)
        # 记录按表头的设备键（id 或 name）写值；provider 内部按节点 id 索引，这里改为表头键。
        # 接管的组里没有录到的成员也要就绪，否则整组不可写（group_not_ready）。
        provider.nodes = {
            selector.value: self.observations.resolve(selector) for selector in [*group_selectors, *selectors]
        }
        provider.start(
            operation_id=f"replay-{uuid.uuid4()}",
            source_session=f"replay-{uuid.uuid4()}",
            quiescent=lambda: True,
        )
        return provider

    def _recover(self) -> bool:
        """后端重启后回放组需显式恢复；有未决作业或原生占用时 OS 会拒绝，这里不绕过。"""
        try:
            self.sources.recover(self.group, quiescent=lambda: True)
        except StateError as error:
            logger.info("[ObservationReplay] recovery refused: %s", error.reason)
            return False
        logger.info("[ObservationReplay] recovered source group %s after restart", self.group)
        return True

    def _wait_provider(self):
        while not self._stop.is_set():
            try:
                return self._provider()
            except StateError as error:
                if error.reason == "group_busy" and self._recover():
                    continue
                # 库存图在后端启动后才装配；设备未就绪时等待，而不是失败退出。
                logger.info("[ObservationReplay] waiting for devices: %s", error.reason)
            self._stop.wait(self.wait_interval_s)
        return None

    def _run(self) -> None:
        provider = self._wait_provider()
        while provider is not None and not self._stop.is_set():
            try:
                self._play(provider)
            except StateError as error:
                logger.warning("[ObservationReplay] source rejected (%s); restarting", error.reason)
                provider = self._wait_provider()
                continue
            if not self.loop:
                break
        logger.info("[ObservationReplay] stopped after %d rows", self.rows_published)

    def _publish(self, provider, values: dict[str, dict], event: str | None) -> None:
        if values:
            provider.feed(time.time(), values, sample_group=event)

    def _play(self, provider) -> None:
        """按记录时间放；暂停、倍速、跳转由控制接口改状态，这里每次醒来重新对时。"""
        index = 0
        with self._cond:
            self.position, self.event = 0.0, None
            anchor_wall, anchor_t = time.monotonic(), 0.0
        while not self._stop.is_set():
            with self._cond:
                if self._seek is not None:
                    target, self._seek = self._seek, None
                    index = bisect.bisect_right(self.times, target)
                    event = self.rows[index - 1].get("event") if index else None
                    self._publish(provider, state_at(self.rows, index), event)
                    self.position, self.event = target, event
                    anchor_wall, anchor_t = time.monotonic(), target
                    continue
                if self._reanchor:
                    self._reanchor = False
                    anchor_wall, anchor_t = time.monotonic(), self.position
                if self.paused:
                    self._cond.wait(0.2)
                    anchor_wall, anchor_t = time.monotonic(), self.position
                    continue
                if index >= len(self.rows):
                    self.loops += 1
                    return
                row = self.rows[index]
                delay = anchor_wall + (float(row["t"]) - anchor_t) / self.speed - time.monotonic()
                if delay > 0:
                    self._cond.wait(min(delay, 0.2))
                    continue
                self._publish(provider, row["values"], row.get("event"))
                self.position, self.event = float(row["t"]), row.get("event")
                index += 1
                self.rows_published += 1
            if now_log(self):
                logger.info("[ObservationReplay] t=%.1fs event=%s devices=%s rows=%d",
                            float(row["t"]), row.get("event"), ",".join(sorted(row["values"])[:4]),
                            self.rows_published)


_feed: ObservationReplayFeed | None = None


def start_from_environment(observations, sources) -> ObservationReplayFeed | None:
    """按环境变量启动回放；未配置时返回 None。"""

    global _feed
    configure(observations, sources)
    path = os.environ.get(ENV_FILE, "").strip()
    if not path:
        return None
    if observations is None or sources is None:
        raise ValueError(f"{ENV_FILE} 需要先启用设备观测 UNILABOS_OBSERVATIONS_ENABLED=true")
    loop = os.environ.get(ENV_LOOP, "true").strip().lower() != "false"
    group = os.environ.get(ENV_GROUP, "replay").strip() or "replay"
    _feed = ObservationReplayFeed(observations, sources, path, group=group, loop=loop)
    _feed.start()
    logger.info("[ObservationReplay] replaying %s (group=%s, loop=%s)", path, group, loop)
    return _feed


def current() -> ObservationReplayFeed | None:
    return _feed


# ---- 运行时加载 / 卸载（不重启后端）----
_context: dict[str, Any] = {}


def configure(observations, sources) -> None:
    """组合根在启用设备观测后调用，供运行时加载回放使用。"""
    _context.update(observations=observations, sources=sources)


def _covering_group(sources_context, nodes: set[str]) -> tuple[str, list[str]] | None:
    """已有来源组里覆盖这些节点的那个（例如仿真跟随组）：返回组名和全部成员 id。"""
    for row in sources_context.db.execute("SELECT name,body FROM observation_source_group").fetchall():
        body = json.loads(row["body"])
        members = [member for member in (body.get("member_ids") or {}).values() if member]
        if nodes and nodes <= set(members):
            return row["name"], members
    return None


def load(spec: str, *, loop: bool = False, group: str | None = None) -> ObservationReplayFeed:
    """运行时换成一份回放源（文件或 ``a.mcap:b.jsonl``）。

    记录里的节点若已属于某个来源组（例如仿真跟随组），回放就接管该组（来源模式切到 replay），
    并先停止仿真跟随；卸载时再按环境变量恢复跟随。否则新建 ``group``（默认 ``replay``）。
    """
    global _feed
    observations, sources = _context.get("observations"), _context.get("sources")
    if observations is None or sources is None:
        raise ValueError("设备观测未启用（UNILABOS_OBSERVATIONS_ENABLED=true）")
    header, _ = load_sources(spec)
    stop()
    covering = _covering_group(observations.context, set(header["devices"]))
    members = None
    if covering is not None:
        group, members = covering
        from unilabos.simulation import observation_feed

        if observation_feed.current_group() == group:
            observation_feed.stop()
            _context["follow_stopped"] = True
    allowed = {node["id"]: set(((node.get("descriptor") or {}).get("properties")) or {})
               for node in observations.snapshot()["nodes"] if node.get("id") and node.get("descriptor")}
    _feed = ObservationReplayFeed(observations, sources, spec, group=group or "replay", loop=loop,
                                  group_members=members, allowed=allowed)
    _feed.start()
    logger.info("[ObservationReplay] loaded %s (group=%s, loop=%s)", spec, _feed.group, loop)
    return _feed


def unload() -> bool:
    """停止运行时加载的回放；若加载时停过仿真跟随，按环境变量恢复。"""
    had = _feed is not None
    stop()
    if _context.pop("follow_stopped", False):
        from unilabos.simulation import observation_feed

        observation_feed.start_from_environment(_context["observations"], _context["sources"])
    return had


def stop() -> None:
    global _feed
    if _feed is not None:
        _feed.stop()
        _feed = None
