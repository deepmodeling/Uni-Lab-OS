"""可选：在后端进程内跟随一个实时写入的设备观测流（JSONL），发布为 observed 样本。

与 :mod:`replay_feed`（按记录时间回放、可循环）不同，这里的写入端是正在运行的模拟器
（例如奔曜 TCP 模拟器的行为层）：它在动作执行期间按 20 Hz 追加行，本模块边读边发布。

设置 ``UNILABOS_OBSERVATION_FEED_FILE`` 指向该文件并启用设备观测
（``UNILABOS_OBSERVATIONS_ENABLED=true``）。格式：

* 第一行表头 ``{"schema": ..., "devices": {node_id: [property, ...]},
  "descriptors": {node_id: descriptor}}``；``descriptors`` 经可信包发现路径
  ``describe`` 登记，设备模板无需声明 ``metadata.observations``；
* 其后每行 ``{"t": 墙钟秒, "event": 来源动作, "values": {node_id: {property: value}}}``。

空闲时每 ``UNILABOS_OBSERVATION_FEED_HEARTBEAT``（默认 1 s）把各设备当前值再发布一次，
避免 TTL 过期。写入端重开文件（截断）时重新读取表头。来源组名由
``UNILABOS_OBSERVATION_FEED_GROUP``（默认 ``simulation-feed``）指定。

来源组模式为 ``observation``、provider 为 ``simulation-feed``：组内设备的动作仍走既有原生
派发链（真机驱动 → 模拟器），由 :func:`register_native_route` 在 ``SourceDispatcher`` 上登记
直通路由；本入口自身不执行动作、不结算库存，观测结果只用于显示。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from unilabos.runtime.observations import Descriptor, Sample, Selector, StateError

logger = logging.getLogger(__name__)

ENV_FILE = "UNILABOS_OBSERVATION_FEED_FILE"
ENV_GROUP = "UNILABOS_OBSERVATION_FEED_GROUP"
ENV_HEARTBEAT = "UNILABOS_OBSERVATION_FEED_HEARTBEAT"
# 另外跟随的观测流文件（用系统路径分隔符隔开），每个文件一个来源组 ``<主组名>-extra<N>``。
# 用于逐台迁移：一部分设备的画面由设备运行时里的机构运行时写，其余仍由协议模拟端写。
# 各文件表头里的设备必须互不重叠（一台设备只能属于一个来源组）。
ENV_EXTRA_FILES = "UNILABOS_OBSERVATION_FEED_EXTRA_FILES"
PROVIDER = "simulation-feed"
MODE = "observation"
# 观测流的来源标识：纯仿真为 simulation-feed（默认），数字孪生跟随器写的流设为 twin-feed，
# 让前端与快照能区分“模拟器演的”与“跟着真机演的”。只影响来源组的 provider 名，不影响派发。
ENV_PROVIDER = "UNILABOS_OBSERVATION_FEED_PROVIDER"


# 样本的 basis：孪生是按真机命令推演的估计值，纯仿真是模拟值；可用 _BASIS 显式覆盖。
ENV_BASIS = "UNILABOS_OBSERVATION_FEED_BASIS"
BASIS_BY_PROVIDER = {"twin-feed": "estimated", PROVIDER: "simulated"}


def provider_name() -> str:
    return os.environ.get(ENV_PROVIDER, "").strip() or PROVIDER


def basis() -> str:
    return os.environ.get(ENV_BASIS, "").strip() or BASIS_BY_PROVIDER.get(provider_name(), "simulated")


class FeedProvider:
    """与 ReplayProvider 同一发布路径，但来源模式为 observation，执行仍走原生派发。"""

    def __init__(self, observations, sources, group: str, selectors: list[Selector]):
        self.observations, self.sources, self.group = observations, sources, group
        self.nodes = {selector.value: observations.resolve(selector) for selector in selectors}
        self.sources.configure_group(group, selectors)
        self.source = None
        self.sequence = 0
        self.versions: dict[str, str] = {}

    def start(self, *, operation_id: str, source_session: str, quiescent):
        self.source = self.sources.switch(
            self.group, operation_id=operation_id, mode=MODE, provider=provider_name(),
            source_session=source_session, prepare=lambda: None, quiescent=quiescent,
        )
        for node in self.nodes.values():
            self.sources.ready(self.group, node["uuid"], self.source["epoch"], source_session)
        snapshot = self.observations.snapshot()
        self.versions = {
            node["uuid"]: node["descriptor"]["version"]
            for node in snapshot["nodes"] if node["uuid"] in self.source["members"]
        }
        self.sequence = 0

    def feed(self, sampled_at: float, values: dict[str, dict], *, sample_group: str):
        if self.source is None:
            raise StateError("group_not_ready")
        samples = []
        for identifier, properties in values.items():
            node = self.nodes[identifier]
            for name, value in properties.items():
                samples.append(Sample(
                    material_uuid=node["uuid"], generation=node["generation"],
                    descriptor_version=self.versions[node["uuid"]], group=self.group,
                    epoch=self.source["epoch"], runtime_session=self.observations.session,
                    source_session=self.source["source_session"], provider=provider_name(), property=name,
                    seq=self.sequence, sampled_at=sampled_at, clock="wall",
                    quality="unknown" if value is None else "good", basis=basis(), value=value,
                    sample_group=sample_group,
                ))
        result = self.observations.publish_batch(samples)
        self.sequence += 1
        return result


class ObservationFollowFeed:
    """跟随 JSONL 观测流；``stop`` 线程安全。"""

    def __init__(self, observations, sources, path: Path, *, group: str = "simulation-feed",
                 heartbeat_s: float = 30.0, poll_s: float = 0.02, wait_interval_s: float = 2.0):
        self.observations = observations
        self.sources = sources
        self.path = Path(path)
        self.group = group
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.wait_interval_s = wait_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rows_published = 0
        self.header: dict[str, Any] | None = None
        self.values: dict[str, dict[str, Any]] = {}
        self._last_t = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="observation-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 准备
    def _register(self, header: dict[str, Any]):
        by = header.get("select_by", "id")
        selectors = [Selector(by=by, value=node_id) for node_id in header["devices"]]
        for node_id, body in (header.get("descriptors") or {}).items():
            self.observations.describe(Selector(by=by, value=node_id), Descriptor.model_validate(body))
        # 行内按表头键（id 或 name）写值。
        provider = FeedProvider(self.observations, self.sources, self.group, selectors)
        provider.start(
            operation_id=f"feed-{uuid.uuid4()}",
            source_session=f"feed-{uuid.uuid4()}",
            quiescent=lambda: True,
        )
        return provider

    def _recover(self) -> bool:
        try:
            self.sources.recover(self.group, quiescent=lambda: True)
        except StateError as error:
            logger.info("[ObservationFeed] recovery refused: %s", error.reason)
            return False
        return True

    def _wait_header(self) -> dict[str, Any] | None:
        while not self._stop.is_set():
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    line = stream.readline()
                if line.endswith("\n"):
                    header = json.loads(line)
                    if isinstance(header.get("devices"), dict) and header["devices"]:
                        return header
            except (OSError, ValueError):
                pass
            self._stop.wait(self.wait_interval_s)
        return None

    def _wait_provider(self, header: dict[str, Any]):
        while not self._stop.is_set():
            try:
                return self._register(header)
            except StateError as error:
                if error.reason == "group_busy" and self._recover():
                    continue
                logger.info("[ObservationFeed] waiting for devices: %s", error.reason)
            self._stop.wait(self.wait_interval_s)
        return None

    # ------------------------------------------------------------------ 主循环
    def _run(self) -> None:
        while not self._stop.is_set():
            header = self._wait_header()
            if header is None:
                return
            provider = self._wait_provider(header)
            if provider is None:
                return
            self.header = header
            self.values = {}
            logger.info("[ObservationFeed] following %s (%d devices)", self.path, len(header["devices"]))
            try:
                self._follow(provider)
            except StateError as error:
                logger.warning("[ObservationFeed] source rejected (%s); restarting", error.reason)
        logger.info("[ObservationFeed] stopped after %d rows", self.rows_published)

    def _publish(self, provider, t: float, values: dict[str, dict], event: str) -> None:
        # 采样时间单调：写入端时钟回拨或心跳交错时取上一次之后的时刻。
        t = max(t, self._last_t + 1e-4)
        self._last_t = t
        provider.feed(t, values, sample_group=event or "feed")
        self.rows_published += 1

    def _follow(self, provider) -> None:
        stream = self.path.open("r", encoding="utf-8")
        try:
            stream.readline()  # header
            identity = os.fstat(stream.fileno())
            buffer = ""
            last_publish = 0.0
            while not self._stop.is_set():
                chunk = stream.readline()
                if chunk:
                    buffer += chunk
                    if not buffer.endswith("\n"):
                        continue
                    line, buffer = buffer, ""
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    values = {node: props for node, props in (row.get("values") or {}).items()
                              if node in self.header["devices"]}
                    if not values:
                        continue
                    for node, props in values.items():
                        self.values.setdefault(node, {}).update(props)
                    self._publish(provider, float(row.get("t", time.time())), values, row.get("event", ""))
                    last_publish = time.monotonic()
                    continue
                # 文件被写入端重开（截断/替换）时重新读表头
                try:
                    current = os.stat(self.path)
                    if (current.st_size < stream.tell()
                            or getattr(current, "st_ino", 0) != getattr(identity, "st_ino", 0)):
                        logger.info("[ObservationFeed] feed file reopened; reloading header")
                        return
                except OSError:
                    return
                if self.values and time.monotonic() - last_publish >= self.heartbeat_s:
                    self._publish(provider, time.time(), self.values, "heartbeat")
                    last_publish = time.monotonic()
                self._stop.wait(self.poll_s)
        finally:
            stream.close()


_feed: ObservationFollowFeed | None = None
_extra_feeds: list[ObservationFollowFeed] = []


def register_native_route(dispatcher) -> bool:
    """在 SourceDispatcher 上登记 ``simulation-feed`` 直通路由：组内设备动作交回原生派发链。

    仅当配置了 ``UNILABOS_OBSERVATION_FEED_FILE`` 时登记；返回是否登记。
    """
    if not os.environ.get(ENV_FILE, "").strip():
        return False
    register = getattr(dispatcher, "register_simulation_route", None)
    delegate = getattr(dispatcher, "delegate", None)
    if register is None or delegate is None:
        return False
    register(provider_name(), lambda payload, admission: delegate.dispatch(payload))
    return True


def start_from_environment(observations, sources) -> ObservationFollowFeed | None:
    """按环境变量启动跟随；未配置时返回 None。"""
    global _feed
    path = os.environ.get(ENV_FILE, "").strip()
    if not path:
        return None
    if observations is None or sources is None:
        raise ValueError(f"{ENV_FILE} 需要先启用设备观测 UNILABOS_OBSERVATIONS_ENABLED=true")
    group = os.environ.get(ENV_GROUP, "simulation-feed").strip() or "simulation-feed"
    heartbeat = float(os.environ.get(ENV_HEARTBEAT, "30.0") or 30.0)
    _feed = ObservationFollowFeed(observations, sources, Path(path), group=group, heartbeat_s=heartbeat)
    _feed.start()
    logger.info("[ObservationFeed] following %s (group=%s)", path, group)
    extras = [item.strip() for item in os.environ.get(ENV_EXTRA_FILES, "").split(os.pathsep) if item.strip()]
    for index, extra in enumerate(extras, start=1):
        follower = ObservationFollowFeed(observations, sources, Path(extra), group=f"{group}-extra{index}",
                                         heartbeat_s=heartbeat)
        follower.start()
        _extra_feeds.append(follower)
        logger.info("[ObservationFeed] also following %s (group=%s)", extra, follower.group)
    return _feed


def current_group() -> str | None:
    return _feed.group if _feed is not None else None


def stop() -> None:
    global _feed
    if _feed is not None:
        _feed.stop()
        _feed = None
    while _extra_feeds:
        _extra_feeds.pop().stop()
