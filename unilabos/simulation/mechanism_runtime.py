"""设备运行时（Edge）里的机构运行时：按工站仿真装配给每个设备节点打开资产实例，执行或跟随动作，并写观测流。

工站仿真装配（``unilab.station-simulation/1``，环境变量 ``UNILABOS_SIMULATION_COMPOSITION`` 指向）给每台设备写
OS 仿真执行配置的 ``composition`` 片段，由 :func:`compile_composition` 编译和校验；本运行时是其中
``feedback: kinematic`` 一档的实现：资产 USD 在进程内的运动学场景里按确认步摆位、读回关节，读回与计划值在容差内
才算到位（不做刚体动力学和接触力）；搬运在所在段的确认步核对两端库位并记下实际位置：

* ``attachment: direct``：设备不构造驱动、不连协议端点；动作由 :meth:`MechanismRuntime.run` 按行为类执行到完成。
* ``attachment: protocol``：设备照常由驱动执行（连协议模拟端，或孪生时连真机）；动作开始时 :meth:`begin`、
  结束时 :meth:`finish`，画面按同一行为类推演。推演不影响动作本身的成败。

孪生（``UNILABOS_OBSERVATION_FEED_PROVIDER=twin-feed``）下不允许 ``direct``：真机在场时不能绕开驱动。

配置由工具从设备图、资产锁和驱动声明生成，本模块不猜对应关系：设备动作 → 机构动作、参数换算都必须写在配置里。

配置可以随领域包提交：设备的资产包写成资产锁里的键（``"asset": 键``，顶层 ``"assets": {"lock": 锁文件}``），
按锁行的 ``library_path`` 在本机资产缓存里找包，并核对包清单摘要；缓存目录取环境变量 ``UNILABOS_ASSET_CACHE``
或配置的 ``assets.cache``。配置里的相对路径（锁、步骤表、资源图、场景图）都相对配置文件所在目录；观测流路径
可由 ``UNILABOS_MECHANISM_FEED`` 覆盖，运行目录不必写进配置。观测流沿用后端 ``observation_feed`` 跟随的
JSONL 格式。运动是几何运动学，不是物理或真机证据。
"""
from __future__ import annotations

import copy
import json
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .mechanism import (
    Mechanism,
    MechanismError,
    Plan,
    Playback,
    action_parameters,
    evaluate_expression,
    mechanism_actions,
    plan_action,
)

SCHEMA = "unilab.station-simulation/1"
FEED_SCHEMA = "unilab.mechanism-feed/1"
ENV_CONFIG = "UNILABOS_SIMULATION_COMPOSITION"
FEEDBACK = "kinematic"   # 资产行为类涉及运动，按运动学求值读回作为到位证据；physics / resource_state 要对应引擎或库存适配
_ATTACHMENTS = {"direct": "direct", "protocol": "follow"}


def compile_device_composition(node: str, composition: Any):
    """用 OS 的 compile_composition 编译一台设备的装配；只接受本运行时实现的组合，其余在启动前报错。"""
    from .assembly import SourceCatalog
    from .composition import compile_composition

    if not isinstance(composition, Mapping):
        raise ValueError(f"{node} 缺少 composition 片段")
    try:
        compiled = compile_composition(dict(composition), catalog=SourceCatalog({}), defaults={},
                                       policies={"feedback": set()}, aliases={}, completion_modes={"feedback": "feedback"})
    except ValueError as error:
        raise ValueError(f"{node} 的 composition 无效：{error}") from error
    if compiled.feedback != FEEDBACK:
        raise ValueError(f"{node} 的 feedback={compiled.feedback}：资产行为运行时实现的是 {FEEDBACK}；"
                         "analytic 不读回、不能作为机构动作的证据，physics 要起物理引擎，resource_state 要配 committed_effect 适配")
    if compiled.scheduler != "coupled":
        raise ValueError(f"{node} 的 scheduler 必须是 coupled")
    if composition.get("sources"):
        raise ValueError(f"{node} 的 sources：资产行为运行时没有可选的来源端口")
    return compiled
ENV_ASSET_CACHE = "UNILABOS_ASSET_CACHE"
ENV_FEED = "UNILABOS_MECHANISM_FEED"
PACKAGE_MANIFEST = "com.nvidia.simready.packaging.json"


class _LockedPackages:
    """按资产锁的键找本机缓存里的固定包：路径取锁行的 library_path，身份以包清单摘要为准（不逐文件重算）。"""

    def __init__(self, lock: Path, cache: Path):
        self.cache = cache
        rows = json.loads(lock.read_text(encoding="utf8")).get("dependencies") or []
        self.rows = {row.get("key") or row.get("owner_registry_id") or row["asset_id"]: row for row in rows}

    def path(self, key: str) -> Path:
        import hashlib

        row = self.rows.get(key)
        if row is None:
            raise ValueError(f"资产锁里没有键 {key}")
        root = (self.cache / row["library_path"]).resolve()
        manifest = root / PACKAGE_MANIFEST
        if not manifest.is_file():
            raise ValueError(f"本机资产缓存里没有 {key}（{root}）；先按锁拉取")
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != row["package_digest"]:
            raise ValueError(f"{key} 的缓存包与锁的摘要不一致（{root}）")
        return root


def property_name(joint: str) -> str:
    """关节的观测属性名：实例根下的相对路径，``/`` 换成 ``.``（同一资产里叶名可能重复）。"""
    return joint.replace("/", ".")


@dataclass
class _Device:
    node: str
    attachment: str
    mechanism: Any
    actions: dict[str, dict[str, Any]]
    lock: threading.Lock
    composition_digest: str = ""
    busy: bool = False


class _Following:
    def __init__(self, device, playback, started_at, event):
        self.device, self.playback, self.started_at, self.event = device, playback, started_at, event
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


class MechanismRuntime:
    """持有各设备节点的资产实例；线程安全，同一设备同一时刻只执行一个动作。"""

    def __init__(self, config: Mapping[str, Any], *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, base: str | Path | None = None,
                 environ: Mapping[str, str] | None = None):
        import os

        from .mechanism_try import load_behavior, open_instance, package_behavior

        if config.get("schema") != SCHEMA:
            raise ValueError("机构运行时配置的 schema 必须是 " + SCHEMA)
        environ = os.environ if environ is None else environ
        twin = environ.get("UNILABOS_OBSERVATION_FEED_PROVIDER", "").strip() == "twin-feed"
        base_dir = Path(base) if base is not None else Path.cwd()

        def local(value: str | Path) -> Path:
            path = Path(value).expanduser()
            return path if path.is_absolute() else base_dir / path

        locked: _LockedPackages | None = None
        assets = config.get("assets") or {}
        if assets.get("lock"):
            cache = environ.get(ENV_ASSET_CACHE, "").strip() or assets.get("cache")
            locked = _LockedPackages(local(assets["lock"]), local(cache)) if cache else None
        self.clock, self.sleep = clock, sleep
        self.rate_hz = float(config.get("rate_hz", 20.0))
        self.time_scale = float(config.get("time_scale", 1.0))
        if self.rate_hz <= 0 or self.time_scale <= 0:
            raise ValueError("rate_hz 与 time_scale 必须为正数")
        self.devices: dict[str, _Device] = {}
        self.rejections: list[dict[str, Any]] = []
        self._following: dict[str, _Following] = {}
        self._write_lock = threading.Lock()
        for node, entry in (config.get("devices") or {}).items():
            if "attachment" in entry:
                raise ValueError(f"{node}：入口写在 composition.attachment（direct / protocol），不另写 attachment")
            compiled = compile_device_composition(node, entry.get("composition"))
            attachment = _ATTACHMENTS[compiled.attachment]
            if attachment == "direct" and twin:
                raise ValueError(f"{node} 是 direct：孪生下真机在场，设备必须经驱动（protocol）")
            if entry.get("asset"):
                if locked is None:
                    raise ValueError(f"{node} 按资产锁引用包，需要配置 assets.lock 和资产缓存（{ENV_ASSET_CACHE}）")
                package = locked.path(entry["asset"])
            else:
                package = local(entry["package"]).resolve()
            cls = load_behavior(entry.get("behavior") or package_behavior(package))
            stage, root = open_instance(package, (0.0, 0.0, 0.0, 0.0))
            resources, scene = None, None
            if isinstance(entry.get("resources"), dict):
                # 场景图：节点位姿 × 耗材资产自带的孔位、吸头位。台面基准库位的位置要等机构建好才知道，见下。
                scene = dict(entry["resources"])
                if scene.get("scene"):
                    scene["scene"] = str(local(scene["scene"]))
            elif entry.get("resources"):
                # 资源图：行为类用 materials.point(资源名) 时沿它算台面坐标系里的点（目前取图里的静态位置）。
                from .mechanism_resources import ResourcePoints

                resources = ResourcePoints.from_graph(local(entry["resources"]))
            mechanism = Mechanism(stage, root, cls(), resources)
            if scene is not None:
                from .mechanism_resources import attach_scene

                attach_scene(mechanism, scene, node)
            available = mechanism_actions(mechanism)
            actions = {}
            def step(binding, where):
                target = binding.get("mechanism")
                if target not in available:
                    raise ValueError(f"{where} 绑定的机构动作 {target} 不存在（可用：{sorted(available)}）")
                return {"mechanism": target, "args": dict(binding.get("args") or {}),
                        "parameters": tuple(action_parameters(available[target]))}

            for name, binding in (entry.get("actions") or {}).items():
                if "table" in binding:
                    # 协议级动作：工站数据里的步骤表把它展开成一串基本机构动作，按 key 模板（例如 "{protocol}"）选一条。
                    table = json.loads(local(binding["table"]).read_text(encoding="utf8"))
                    actions[name] = {"key": binding.get("key", "{protocol}"), "table": {
                        key: [step(item, f"{node}.{name}[{key}]") for item in steps] for key, steps in table.items()}}
                elif "steps" in binding:
                    actions[name] = {"steps": [step(item, f"{node}.{name}") for item in binding["steps"]]}
                else:
                    actions[name] = step(binding, f"{node}.{name}")
            self.devices[node] = _Device(node, attachment, mechanism, actions, threading.Lock(), compiled.digest)
        self._stream = None
        feed = environ.get(ENV_FEED, "").strip() or config.get("feed")
        if feed:
            path = local(feed)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("w", encoding="utf-8")
            self._write({"schema": FEED_SCHEMA, "rate_hz": self.rate_hz,
                         "devices": {node: sorted(self._values(device)) for node, device in self.devices.items()},
                         "descriptors": {node: self._descriptor(device) for node, device in self.devices.items()}})
            self._write_row(self.clock(), "initial", {node: self._values(d) for node, d in self.devices.items()})

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "MechanismRuntime":
        path = Path(path)
        kwargs.setdefault("base", path.resolve().parent)
        return cls(json.loads(path.read_text(encoding="utf8")), **kwargs)

    # ------------------------------------------------------------------ 查询
    def simulated(self, node: str) -> bool:
        """该设备是否不构造驱动、由行为类直接执行。"""
        device = self.devices.get(node)
        return device is not None and device.attachment == "direct"

    def follows(self, node: str) -> bool:
        device = self.devices.get(node)
        return device is not None and device.attachment == "follow"

    def has(self, node: str, action: str) -> bool:
        device = self.devices.get(node)
        return device is not None and action in device.actions

    # ------------------------------------------------------------------ 观测流
    def _values(self, device: _Device) -> dict[str, float]:
        return {property_name(name): value for name, value in device.mechanism.positions.items()}

    def _descriptor(self, device: _Device) -> dict[str, Any]:
        properties, joints = {}, {}
        for name, joint in device.mechanism.joints.items():
            spec = {"semantic": "observed", "dtype": "number", "unit": "m" if joint.kind == "prismatic" else "rad",
                    "frame": "joint", "ttl_seconds": 5.0}
            for side in ("lower", "upper"):
                if getattr(joint, side) is not None:
                    spec[side] = getattr(joint, side)
            properties[property_name(name)] = spec
            joints[property_name(name)] = name.rsplit("/", 1)[-1]
        bound = ",".join(f"{k}>{v}" for k, v in sorted(joints.items()))
        version = "mechanism-" + device.node + "-" + format(zlib.crc32(bound.encode("utf-8")), "08x")
        return {"version": version, "properties": properties, "joints": joints}

    def _write(self, obj: Mapping[str, Any]) -> None:
        with self._write_lock:
            if self._stream is not None:
                self._stream.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                self._stream.flush()

    def _write_row(self, t: float, event: str, values: Mapping[str, Mapping[str, float]]) -> None:
        self._write({"t": round(t, 4), "event": event,
                     "values": {node: {k: round(v, 6) for k, v in props.items()} for node, props in values.items()}})

    # ------------------------------------------------------------------ 动作 → 计划
    def _plan(self, device: _Device, action: str, parameters: Mapping[str, Any]):
        binding = device.actions[action]
        if "table" in binding:
            key = self._argument(binding["key"], parameters)
            if key not in binding["table"]:
                raise MechanismError("protocol_unknown", f"{action} 的步骤表里没有 {key!r}（有：{sorted(binding['table'])}）")
            steps = binding["table"][key]
        else:
            steps = binding.get("steps") or [binding]
        if len(steps) == 1:
            return self._plan_step(device, steps[0], parameters)
        # 行为对象在算计划时会更新自己的状态（例如哪个通道装着吸头）；中途被拒绝要整体退回。
        mechanism, saved = device.mechanism, copy.deepcopy(device.mechanism.behavior)
        segments, state, carries, clock = [], {}, [], 0.0
        try:
            for item in steps:
                plan = self._plan_step(device, item, parameters)
                segments.extend(plan.segments)
                state.update(plan.state)
                carries.extend((clock + t, source, target) for t, source, target in plan.carries)
                clock += plan.duration
        except Exception:
            mechanism.behavior = saved
            raise
        return Plan(action, tuple(segments), clock, state, tuple(carries))

    def _plan_step(self, device: _Device, binding: Mapping[str, Any], parameters: Mapping[str, Any]):
        if binding["args"]:
            arguments = {name: self._argument(expression, parameters) for name, expression in binding["args"].items()}
        else:
            # 没有显式换算：只传机构动作声明过的同名参数（驱动独有的参数如超时不传）。
            arguments = {name: parameters[name] for name in binding["parameters"] if name in parameters}
        return plan_action(device.mechanism, binding["mechanism"], arguments)

    @staticmethod
    def _argument(expression: Any, parameters: Mapping[str, Any]) -> Any:
        """换算一个机构动作入参：数值表达式，或带 ``{入参}`` 的文字模板（例如库位名 ``column_{posY:02d}/level_{posX}``）。"""
        def template(text):
            try:
                return text.format(**parameters)
            except (KeyError, IndexError, ValueError) as error:
                raise MechanismError("argument_template_invalid", f"{text}: {error}") from error

        def nested(value):  # 列表、字典里的文字只做模板替换，不当表达式算（里面多是资源名）
            if isinstance(value, str):
                return template(value) if "{" in value else value
            if isinstance(value, list):
                return [nested(item) for item in value]
            if isinstance(value, dict):
                return {key: nested(item) for key, item in value.items()}
            return value

        if not isinstance(expression, str):
            return nested(expression)
        if "{" in expression:
            return template(expression)
        return evaluate_expression(expression, parameters)

    @staticmethod
    def _evidence(device: _Device, result: dict[str, Any]) -> dict[str, Any]:
        """证据按装配的反馈档标注（kinematic：各确认步读回到位），附装配摘要。"""
        return dict(result, evidence=FEEDBACK, composition_digest=device.composition_digest)

    def _acquire(self, device: _Device) -> None:
        with device.lock:
            if device.busy:
                raise MechanismError("device_busy", device.node)
            device.busy = True

    def _release(self, device: _Device) -> None:
        with device.lock:
            device.busy = False

    def _play(self, device: _Device, playback: Playback, started_at: float, event: str,
              stop: threading.Event | None = None) -> None:
        """按墙钟推进到计划结束或被要求停止；每个采样点摆位并写一行。"""
        period = 1.0 / self.rate_hz
        while stop is None or not stop.is_set():
            now = self.clock()
            elapsed = max(0.0, (now - started_at) * self.time_scale)
            if elapsed >= playback.plan.duration:
                return
            playback.advance(elapsed)  # 帧间只记关节值；越过确认步时摆位读回并核对到位，不到位抛 readback_mismatch
            self._write_row(now, event, {device.node: self._values(device)})
            wait = max(0.0, period - (self.clock() - now))  # 扣掉本帧耗时，采样率按墙钟保持 rate_hz
            if stop is not None:
                if stop.wait(wait):
                    return
            else:
                self.sleep(wait)

    # ------------------------------------------------------------------ direct：执行到完成
    def run(self, node: str, action: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按行为类把动作执行到完成并返回证据；被资产拒绝时抛 MechanismError，stage 不变。"""
        device = self.devices[node]
        self._acquire(device)
        try:
            playback = Playback(device.mechanism, self._plan(device, action, parameters or {}))
            started = self.clock()
            self._play(device, playback, started, action)
            result = self._evidence(device, playback.finish())
            self._write_row(self.clock(), action, {node: self._values(device)})
            return result
        finally:
            self._release(device)

    # ------------------------------------------------------------------ follow：跟随驱动的动作
    def begin(self, job: str, node: str, action: str, parameters: Mapping[str, Any] | None = None, *,
              started_at: float | None = None) -> bool:
        """驱动开始执行动作时调用；按行为类开始推演。返回是否开始了推演（被拒绝或设备忙时不推演）。"""
        device = self.devices[node]
        try:
            self._acquire(device)
        except MechanismError as error:
            self.rejections.append({"job": job, "node": node, "action": action, "code": error.code, "reason": str(error)})
            return False
        try:
            playback = Playback(device.mechanism, self._plan(device, action, parameters or {}))
        except Exception as error:  # noqa: BLE001 - 行为源码的任何异常都只让推演不开始，且必须释放设备
            self._release(device)
            self.rejections.append({"job": job, "node": node, "action": action,
                                    "code": getattr(error, "code", "parameters_invalid"), "reason": str(error)})
            return False
        following = _Following(device, playback, self.clock() if started_at is None else started_at, action)
        following.thread = threading.Thread(
            target=self._play, args=(device, playback, following.started_at, action, following.stop),
            name="mechanism-follow-" + node, daemon=True)
        self._following[job] = following
        following.thread.start()
        return True

    def finish(self, job: str, success: bool) -> dict[str, Any] | None:
        """驱动给出动作结果时调用：成功则摆到终态并写入语义状态；失败则停在当前位置，不写状态。"""
        following = self._following.pop(job, None)
        if following is None:
            return None
        following.stop.set()
        if following.thread is not None:
            following.thread.join(timeout=2.0)
        device = following.device
        try:
            if not success:
                return {"action": following.event, "completed": False, "positions_si": device.mechanism.read()}
            result = following.playback.finish()
            self._write_row(self.clock(), following.event, {device.node: self._values(device)})
            return dict(self._evidence(device, result), completed=True)
        finally:
            self._release(device)

    def close(self) -> None:
        for job in list(self._following):
            self.finish(job, False)
        with self._write_lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


def runtime_from_environment(environ: Mapping[str, str] | None = None) -> MechanismRuntime | None:
    """按 ``UNILABOS_MECHANISM_RUNTIME`` 打开机构运行时；未设置时返回 None（行为与未启用完全相同）。"""
    import os

    path = (os.environ if environ is None else environ).get(ENV_CONFIG, "").strip()
    return MechanismRuntime.from_file(path) if path else None


class EdgeMechanisms:
    """设备运行时（HostNode）调用的薄封装：只做线程编排和容错，不依赖 ROS。

    * 不构造驱动的设备（direct）：:meth:`execute` 在工作线程里把动作执行到完成，再回调结果。
    * 由驱动执行的设备（follow）：:meth:`job_started` / :meth:`job_finished` 让画面跟着动作推演；
      这里的任何异常都只记录，不影响真实动作。
    """

    def __init__(self, runtime: MechanismRuntime, *, log: Callable[[str], None] = lambda message: None):
        self.runtime, self.log = runtime, log
        self._pending: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def simulated(self, device_id: str) -> bool:
        return self.runtime.simulated(device_id)

    def has(self, device_id: str, action: str) -> bool:
        return self.runtime.has(device_id, action)

    def execute(self, device_id: str, action: str, parameters: Mapping[str, Any], *,
                on_success: Callable[[dict[str, Any]], None], on_failure: Callable[[str, str], None]) -> threading.Thread:
        """在工作线程里执行；成功回调完成证据，被资产拒绝或出错时回调 (原因码, 说明)。"""

        def work():
            try:
                result = self.runtime.run(device_id, action, parameters)
            except MechanismError as error:
                on_failure(error.code, str(error))
            except Exception as error:  # noqa: BLE001 - 行为源码的任何异常都要变成动作失败，而不是悬挂的作业
                on_failure("mechanism_failed", f"{type(error).__name__}: {error}")
            else:
                on_success(result)

        thread = threading.Thread(target=work, name=f"mechanism-{device_id}", daemon=True)
        thread.start()
        return thread

    def remember(self, job_id: str, device_id: str, action: str, parameters: Mapping[str, Any]) -> None:
        """派发前记下动作；只记配置里声明要跟随、且绑定了机构动作的。"""
        if self.runtime.follows(device_id) and self.runtime.has(device_id, action):
            with self._lock:
                self._pending[job_id] = (device_id, action, dict(parameters))

    def job_started(self, job_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(job_id, None)
        if pending is None:
            return
        try:
            if not self.runtime.begin(job_id, *pending):
                self.log(f"机构推演未开始：{self.runtime.rejections[-1]}")
        except Exception as error:  # noqa: BLE001 - 推演不得影响真实动作
            self.log(f"机构推演出错（已忽略）：{type(error).__name__}: {error}")

    def job_finished(self, job_id: str, success: bool) -> None:
        with self._lock:
            self._pending.pop(job_id, None)
        try:
            self.runtime.finish(job_id, success)
        except Exception as error:  # noqa: BLE001
            self.log(f"机构推演收尾出错（已忽略）：{type(error).__name__}: {error}")


def edge_mechanisms(*, log: Callable[[str], None] = lambda message: None) -> EdgeMechanisms | None:
    runtime = runtime_from_environment()
    return EdgeMechanisms(runtime, log=log) if runtime is not None else None
