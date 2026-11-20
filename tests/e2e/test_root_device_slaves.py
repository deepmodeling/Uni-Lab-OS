"""``--root_devices_as_slaves`` 的故障隔离端到端：一台设备出问题只动它自己的受管 Slave。

用一个极小的驱动包（写在临时目录里）起真实的 ``unilab`` Host，图里三个根设备：

- ``fault_boom``：构造函数直接抛异常。只有它的 Slave 失败，按 on-failure 策略退避重启，
  重启满 ``max_restarts`` 次后停在 crashed；权威 ``/devices`` 显示它不可用，原因带退出码、日志路径与
  日志里的异常；
- ``fault_steady``：全程不碰，必须一直在线、一直能执行动作；
- ``fault_ok``：动作 ``wait(seconds=-1)`` 永不返回（模拟卡死）。经权威端口重启它的 Slave，设备都恢复在线：
  已请求取消的作业落 canceled、动作锁随即释放，排在后面的同一动作若赶在新 Slave 接上之前派发，按 #384 在
  开始边界之前失败（未下发）并进入动作错误决策，重试即成功；未请求取消的落 execution_unknown 并开核实决策，
  锁保留到裁决，标记失败后排队的同一动作执行成功。之后新派发的动作照常执行。

另一张图里两个根设备与一个根级容器互相连着：每个受管 Slave 只装配自己的子树，经 HostLink 取 Host 的完整
物理拓扑图后，协议编译器在 Slave 里看到的连线与设备跑在 Host 里时一样。

进程拓扑 / backend 与 README demo e2e 相同，由 ``UNILABOS_E2E_TOPOLOGY`` / ``UNILABOS_E2E_BACKEND`` 选择；
``split`` 下全部请求（含 ``/device-processes/{id}/restart``）都经调度权威转发到 Host 子进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from tests.e2e.readme_demos import (
    E2E_BACKEND,
    api_request,
    assert_ros_network_transport,
    free_port,
    hostlink_port_open,
    ros_domain_id,
    runtime_command,
    stop_process,
    subprocess_env,
    wait_until,
)

PACKAGE = "fault_isolation_demo"
#: 生成规格的默认重启策略（server/services/device_processes.py）：on-failure，最多 5 次，退避 2+4+8+15+30 s
MAX_RESTARTS = 5
#: 构造失败的 Slave 走完全部退避重启的上限：退避共 59 s，加上 6 次进程启动
CRASH_LOOP_TIMEOUT = 240.0
STEP_TIMEOUT = 90.0 if E2E_BACKEND == "ros2" else 60.0

_DRIVER = '''"""故障隔离 e2e 的极小驱动：一台正常设备、一台构造即失败的设备。"""

import threading
import time
from typing import Any, Dict, Optional

from unilabos.registry.decorators import action, device


@device(
    id="fault_isolation_ok",
    display_name="故障隔离：正常设备",
    category=["virtual_device"],
    description="ping 立即返回；wait 等待给定秒数，秒数为负时永不返回",
    supported_backends=["hostlink", "ros2"],
)
class FaultIsolationOk:
    def __init__(self, device_id: Optional[str] = None, **kwargs: Any) -> None:
        self.device_id = device_id or "fault_isolation_ok"
        self._never = threading.Event()

    @action(display_name="探测", description="立即返回本设备 id", always_free=True)
    def ping(self) -> Dict[str, Any]:
        """立即返回本设备 id。"""
        return {"success": True, "device_id": self.device_id}

    @action(display_name="等待", description="等待 seconds 秒；seconds 小于 0 时永不返回")
    def wait(self, seconds: float = 0.0) -> Dict[str, Any]:
        """等待一段时间。

        Args:
            seconds[秒数]: 等待秒数；小于 0 表示永不返回。
        """
        if seconds < 0:
            self._never.wait()
        time.sleep(seconds)
        return {"success": True, "device_id": self.device_id, "seconds": seconds}


@device(
    id="fault_isolation_boom",
    display_name="故障隔离：构造失败的设备",
    category=["virtual_device"],
    description="构造函数直接抛异常",
    supported_backends=["hostlink", "ros2"],
)
class FaultIsolationBoom:
    def __init__(self, device_id: Optional[str] = None, **kwargs: Any) -> None:
        raise RuntimeError("fault_isolation_boom 构造时故意失败")

    @action(display_name="探测", description="构造失败，永远到不了这里")
    def ping(self) -> Dict[str, Any]:
        """构造失败，永远到不了这里。"""
        return {"success": True}
'''


def _device_node(device_id: str, template: str, x: int) -> dict[str, Any]:
    return {
        "id": device_id,
        "type": "device",
        "parent": None,
        "template_name": template,
        "display_name": device_id,
        "pose": {"position": {"x": x, "y": 0, "z": 0}},
        "config": {},
        "data": {},
    }


def _write_fixture(root: Path) -> tuple[Path, Path]:
    package_dir = root / "pkg" / PACKAGE
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "devices.py").write_text(_DRIVER, encoding="utf-8")
    graph = root / "fault_isolation.json"
    nodes = [
        _device_node("fault_ok", "fault_isolation_ok", 0),
        _device_node("fault_steady", "fault_isolation_ok", 300),
        _device_node("fault_boom", "fault_isolation_boom", 600),
    ]
    graph.write_text(json.dumps({"nodes": nodes, "links": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    return package_dir, graph


def _tail(path: Path, lines: int = 60) -> str:
    if not path.is_file():
        return "<no log>"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


class _OnlineSampler(threading.Thread):
    """后台每 0.3 s 记一次 ``/online-devices``：被观察的设备只要有一次不在线就记下时间。"""

    def __init__(self, port: int, device_id: str) -> None:
        super().__init__(name=f"online-sampler-{device_id}", daemon=True)
        self.port = port
        self.device_id = device_id
        self.samples = 0
        self.offline_at: list[float] = []
        self.errors: list[str] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(0.3):
            try:
                online = api_request(self.port, "/online-devices")["online_devices"]
            except Exception as exc:  # noqa: BLE001 - 记下来由断言判定
                self.errors.append(repr(exc))
                continue
            self.samples += 1
            if self.device_id not in online:
                self.offline_at.append(time.monotonic())

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def _start_action(port: int, device_id: str, action: str, param: dict[str, Any]) -> str:
    task = api_request(
        port,
        "/workflow-tasks",
        {"execution_kind": "ad_hoc_device_action", "device_id": device_id, "action_name": action, "param": param},
    )
    return str(task["uuid"])


def _task(port: int, task_uuid: str) -> dict[str, Any]:
    return api_request(port, f"/workflow-tasks/{task_uuid}")


def _job(port: int, task_uuid: str) -> Optional[dict[str, Any]]:
    jobs = api_request(port, f"/workflow-tasks/{task_uuid}/jobs")
    return jobs[-1] if jobs else None


def _wait_task(port: int, task_uuid: str, statuses: set[str], *, timeout: float, abort) -> dict[str, Any]:
    return wait_until(
        lambda: (task if (task := _task(port, task_uuid))["status"] in statuses else None),
        timeout=timeout,
        abort=abort,
        description=f"任务 {task_uuid} 进入 {sorted(statuses)}",
    )


def _wait_job(port: int, task_uuid: str, statuses: set[str], *, timeout: float, abort) -> dict[str, Any]:
    def ready():
        job = _job(port, task_uuid)
        return job if job is not None and job["status"] in statuses else None

    return wait_until(ready, timeout=timeout, abort=abort, description=f"任务 {task_uuid} 的作业进入 {sorted(statuses)}")


def _assert_queued(port: int, task_uuid: str, *, timeout: float, abort) -> dict[str, Any]:
    def queued():
        for request in api_request(port, "/scheduler/resources").get("requests", []):
            if request.get("task_uuid") == task_uuid and request.get("status") == "waiting" and request.get("blockers"):
                return request
        return None

    return wait_until(queued, timeout=timeout, abort=abort, description=f"任务 {task_uuid} 在动作锁上排队")


def _process(port: int, process_id: str) -> dict[str, Any]:
    return api_request(port, f"/device-processes/{process_id}")


def _device_entry(port: int, device_id: str) -> Optional[dict[str, Any]]:
    for entry in api_request(port, "/devices"):
        if entry["binding"]["local_id"] == device_id:
            return entry
    return None


def _settle_queued_behind_restart(port: int, task_uuid: str, *, abort) -> bool:
    """排在被取消的卡死作业后面的同一动作，锁一释放就派发：若设备的新 Slave 还没接上，按 #384 在开始边界
    之前失败（设备不可达、动作未下发）并进入普通的动作错误决策——设备上什么都没执行，重试安全。

    返回是否经历了这次重试；无论哪种，最终都必须执行成功。
    """

    def settled():
        task = _task(port, task_uuid)
        if task["status"] in {"succeeded", "failed"}:
            return "done", task
        for item in api_request(port, "/error-decisions")["decisions"]:
            if item.get("task_id") == task_uuid:
                return "decision", item
        return None

    kind, value = wait_until(settled, timeout=STEP_TIMEOUT, abort=abort, description=f"任务 {task_uuid} 收敛或进入决策")
    retried = kind == "decision"
    if retried:
        assert "未下发" in str(value.get("error_message") or value), value
        resolved = api_request(port, f"/error-decisions/{value['decision_id']}", {"action": "retry", "reason": "e2e：Slave 重启期间未下发，重试"})
        assert resolved["status"] == "delivered", resolved
        value = _wait_task(port, task_uuid, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=abort)
    assert value["status"] == "succeeded", value
    return retried


def _slave_peer(port: int, process_id: str) -> Optional[dict[str, Any]]:
    suffix = f"_managed_{process_id.replace('-', '_')}"
    for peer in api_request(port, "/hostlink/peers")["peers"]:
        if str(peer.get("machine_name") or "").endswith(suffix):
            return peer
    return None


def _restart_and_wait_online(port: int, process_id: str, device_id: str, *, abort) -> dict[str, Any]:
    """经管理 API 重启受管进程，等新 Slave 接上（新连接、设备已就绪）且权威再次看到设备在线。

    只看 ``/online-devices`` 不够：重启请求刚返回时快照可能还是旧 Slave 的。
    """

    before = _process(port, process_id)
    previous = (_slave_peer(port, process_id) or {}).get("connected_at")
    restarted = api_request(port, f"/device-processes/{process_id}/restart", {})
    assert restarted["status"] == "running" and restarted["pid"] and restarted["pid"] != before["pid"], restarted

    def back_online():
        peer = _slave_peer(port, process_id)
        if peer is None or not peer.get("online") or peer.get("connected_at") == previous:
            return False
        if not (peer.get("devices") or {}).get(device_id, {}).get("ready", True):
            return False
        return device_id in api_request(port, "/online-devices")["online_devices"]

    wait_until(back_online, timeout=STEP_TIMEOUT, abort=abort, description=f"{device_id} 在重启后的新 Slave 里重新上线")
    assert_ros_network_transport(port, {device_id}, abort=abort)
    return restarted


def test_failing_constructor_and_hung_action_stay_inside_their_slave(tmp_path: Path) -> None:
    package_dir, graph = _write_fixture(tmp_path)
    work = tmp_path / "run"
    work.mkdir()
    port = free_port()
    hostlink = free_port(exclude=(port,))
    env = subprocess_env({"ROS_DOMAIN_ID": ros_domain_id(hostlink)} if E2E_BACKEND == "ros2" else {})
    host_log = work / "host.log"
    handle = host_log.open("w", encoding="utf-8")
    host = subprocess.Popen(
        runtime_command(
            package_dir=package_dir,
            graph=graph,
            database_root=work / "host-db",
            management_port=port,
            hostlink_port=hostlink,
            is_slave=False,
            root_devices_as_slaves=True,
        ),
        cwd=tmp_path,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name != "nt",
    )
    host_exited = lambda: host.poll() is not None  # noqa: E731
    sampler: Optional[_OnlineSampler] = None
    try:
        wait_until(
            lambda: api_request(port, "/health").get("status") == "ok" and hostlink_port_open(hostlink),
            timeout=STEP_TIMEOUT,
            abort=host_exited,
            description="Host 管理 API 与 HostLink 端口就绪",
        )
        wait_until(
            lambda: {"fault_ok", "fault_steady"} <= set(api_request(port, "/online-devices")["online_devices"]),
            timeout=STEP_TIMEOUT,
            abort=host_exited,
            description="fault_ok / fault_steady 由各自的 Slave 接入",
        )
        processes = {item["id"]: item for item in api_request(port, "/device-processes")["processes"]}
        assert set(processes) == {"root-fault_ok", "root-fault_steady", "root-fault_boom"}, sorted(processes)
        assert_ros_network_transport(port, {"fault_ok", "fault_steady"}, abort=host_exited)
        sampler = _OnlineSampler(port, "fault_steady")
        sampler.start()

        # 1) 构造失败只停在它自己的 Slave：Host 与其它设备照常，权威给出可操作的原因
        def boom_reported():
            entry = _device_entry(port, "fault_boom")
            record = (entry or {}).get("unavailable") or {}
            return entry if record.get("managed_process", {}).get("restart_count", 0) >= 1 else None

        boom = wait_until(boom_reported, timeout=STEP_TIMEOUT, abort=host_exited, description="fault_boom 的 Slave 失败并开始按策略重启")
        assert boom["online"] is False and boom["edge_status"] == "unavailable", boom
        record = boom["unavailable"]
        assert "受管设备进程 fault_boom" in record["reason"] and "退出码 1" in record["reason"], record
        managed = record["managed_process"]
        assert managed["id"] == "root-fault_boom" and managed["restart_policy"] == "on-failure", managed
        assert Path(managed["log_path"]).is_file() and managed["log_path"] in record["reason"], managed
        assert any("构造时故意失败" in line for line in managed["log_tail"]), managed
        steady = _start_action(port, "fault_steady", "ping", {})
        assert _wait_task(port, steady, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "succeeded"

        # 2) 卡死的动作 + 已请求取消：重启它的 Slave 后作业落 canceled，动作锁释放，排队的同一动作随即执行
        hung = _start_action(port, "fault_ok", "wait", {"seconds": -1})
        _wait_job(port, hung, {"running"}, timeout=STEP_TIMEOUT, abort=host_exited)
        queued = _start_action(port, "fault_ok", "wait", {"seconds": 0.2})
        _assert_queued(port, queued, timeout=STEP_TIMEOUT, abort=host_exited)
        cancel = api_request(port, f"/workflow-tasks/{hung}/commands", {"type": "cancel", "idempotency_key": str(uuid.uuid4())})
        assert cancel["status"] == "succeeded", cancel
        assert _task(port, hung)["status"] == "canceling"
        _restart_and_wait_online(port, "root-fault_ok", "fault_ok", abort=host_exited)
        assert _wait_task(port, hung, {"canceled", "failed", "succeeded"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "canceled"
        assert _job(port, hung)["status"] == "canceled"
        _settle_queued_behind_restart(port, queued, abort=host_exited)
        later = _start_action(port, "fault_ok", "wait", {"seconds": 0.2})
        assert _wait_task(port, later, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "succeeded"

        # 3) 卡死的动作、没请求取消：作业落 execution_unknown 并开核实决策；锁保留到裁决，标记失败后释放
        hung = _start_action(port, "fault_ok", "wait", {"seconds": -1})
        _wait_job(port, hung, {"running"}, timeout=STEP_TIMEOUT, abort=host_exited)
        queued = _start_action(port, "fault_ok", "wait", {"seconds": 0.2})
        _assert_queued(port, queued, timeout=STEP_TIMEOUT, abort=host_exited)
        _restart_and_wait_online(port, "root-fault_ok", "fault_ok", abort=host_exited)
        assert _wait_job(port, hung, {"execution_unknown"}, timeout=STEP_TIMEOUT, abort=host_exited)
        decision = wait_until(
            lambda: next(
                (item for item in api_request(port, "/error-decisions")["decisions"] if item.get("task_id") == hung),
                None,
            ),
            timeout=STEP_TIMEOUT,
            abort=host_exited,
            description="execution_unknown 作业的核实决策",
        )
        assert decision["category"] == "execution_state_unknown", decision
        assert {option["action"] for option in decision["options"]} >= {"retry", "skip", "abort"}, decision
        # 结果未确认前锁不放：排队的同一动作仍在等
        _assert_queued(port, queued, timeout=STEP_TIMEOUT, abort=host_exited)
        resolved = api_request(port, f"/error-decisions/{decision['decision_id']}", {"action": "abort", "reason": "e2e：卡死动作随 Slave 重启丢失"})
        assert resolved["status"] == "delivered", resolved
        assert _wait_task(port, hung, {"failed", "succeeded", "canceled"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "failed"
        assert _wait_task(port, queued, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "succeeded"

        # 4) 构造失败的 Slave 重启满 max_restarts 次后停止看护，原因随之更新
        final = wait_until(
            lambda: (item if (item := _process(port, "root-fault_boom"))["status"] == "crashed"
                     and item["restart_count"] >= MAX_RESTARTS else None),
            timeout=CRASH_LOOP_TIMEOUT,
            abort=host_exited,
            description="fault_boom 的 Slave 重启满次数后停在 crashed",
        )
        assert final["restart_count"] == MAX_RESTARTS and final["last_exit_code"] == 1, final
        assert "停止看护" in (final["last_error"] or ""), final
        reason = wait_until(
            lambda: (entry["unavailable"]["reason"] if "停止看护" in (entry := _device_entry(port, "fault_boom"))
                     .get("unavailable", {}).get("reason", "") else None),
            timeout=STEP_TIMEOUT,
            abort=host_exited,
            description="fault_boom 的不可用原因更新为停止看护",
        )
        assert "修复后在设备进程页重启" in reason, reason

        # 5) 全程：Host 没退出，未被触碰的设备一次也没离线，其它 Slave 也没被重启
        sampler.stop()
        assert sampler.samples > 50 and not sampler.offline_at, (sampler.samples, sampler.offline_at, sampler.errors[-5:])
        assert _process(port, "root-fault_steady")["restart_count"] == 0
        assert _process(port, "root-fault_steady")["status"] == "running"
        for device_id in ("fault_steady", "fault_ok"):
            task = _start_action(port, device_id, "ping", {})
            assert _wait_task(port, task, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "succeeded"
        assert host.poll() is None
    except Exception as exc:  # noqa: BLE001 - 失败时附带 Host 与各受管 Slave 的日志
        logs = f"--- host log tail ---\n{_tail(host_log)}"
        for managed_log in sorted((work / "host-db" / "work" / "device_processes").glob("*/process.log")):
            logs += f"\n--- managed slave {managed_log.parent.name} log tail ---\n{_tail(managed_log, 30)}"
        pytest.fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}\n{logs}", pytrace=False)
    finally:
        if sampler is not None:
            sampler.stop()
        stop_process(host)
        handle.close()


# ── 物理拓扑图只经 HostLink 同步：Slave 看得到跨根设备的连线 ──────────────────

GRAPH_PACKAGE = "graph_sync_demo"

_GRAPH_DRIVER = '''"""物理拓扑图 e2e 的极小驱动：报告本进程协议编译器看到的物理拓扑图。"""

from typing import Any, Dict, Optional

from unilabos.registry.decorators import action, device


@device(
    id="graph_sync_probe",
    display_name="物理拓扑图探针",
    category=["virtual_device"],
    description="报告本进程协议编译器看到的物理拓扑图里某个节点的连线",
    supported_backends=["hostlink", "ros2"],
)
class GraphSyncProbe:
    def __init__(self, device_id: Optional[str] = None, **kwargs: Any) -> None:
        self.device_id = device_id or "graph_sync_probe"

    @action(display_name="相连节点", description="物理拓扑图里与 node_id 相连的节点", always_free=True)
    def linked(self, node_id: str) -> Dict[str, Any]:
        """物理拓扑图里与 node_id 相连的节点。

        Args:
            node_id[节点]: 物理拓扑图里的节点 id。
        """
        from unilabos.client.host.runtime.workstation_protocol import physical_setup_graph

        graph = physical_setup_graph()
        nodes = 0 if graph is None else graph.number_of_nodes()
        linked = sorted(str(item) for item in graph.neighbors(node_id)) if graph is not None and node_id in graph else []
        return {"success": True, "linked": linked, "nodes": nodes}
'''


def _write_graph_fixture(root: Path) -> tuple[Path, Path]:
    """两个根设备各在自己的受管 Slave 里，一个根级容器留在 Host；三条连线都跨出了 Slave 的子树。"""

    package_dir = root / "pkg" / GRAPH_PACKAGE
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "devices.py").write_text(_GRAPH_DRIVER, encoding="utf-8")
    waste = {
        "id": "waste",
        "type": "container",
        "parent": None,
        "class": "RegularContainer",
        "template_name": "container",
        "display_name": "waste",
        "pose": {"position": {"x": 600, "y": 0, "z": 0}},
        "config": {"type": "RegularContainer", "max_volume": 500.0},
        "data": {},
    }

    def link(source: str, target: str) -> dict[str, Any]:
        return {"id": f"{source}-{target}", "source": source, "target": target, "type": "fluid",
                "sourceHandle": "out", "targetHandle": "in"}

    graph = root / "graph_sync.json"
    nodes = [_device_node("probe_a", "graph_sync_probe", 0), _device_node("probe_b", "graph_sync_probe", 300), waste]
    links = [link("probe_a", "probe_b"), link("probe_a", "waste"), link("probe_b", "waste")]
    graph.write_text(json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8")
    return package_dir, graph


def test_root_device_slaves_compile_against_the_host_graph(tmp_path: Path) -> None:
    """每个受管 Slave 只装配自己根设备的子树，跨根设备的连线与根级物料只在 Host 的图里；Slave 经 HostLink
    取 Host 的完整物理拓扑图，协议编译器（这里是探针动作）看到的与设备跑在 Host 里时一样。"""

    package_dir, graph = _write_graph_fixture(tmp_path)
    work = tmp_path / "run"
    work.mkdir()
    port = free_port()
    hostlink = free_port(exclude=(port,))
    env = subprocess_env({"ROS_DOMAIN_ID": ros_domain_id(hostlink)} if E2E_BACKEND == "ros2" else {})
    host_log = work / "host.log"
    handle = host_log.open("w", encoding="utf-8")
    host = subprocess.Popen(
        runtime_command(
            package_dir=package_dir,
            graph=graph,
            database_root=work / "host-db",
            management_port=port,
            hostlink_port=hostlink,
            is_slave=False,
            root_devices_as_slaves=True,
        ),
        cwd=tmp_path,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name != "nt",
    )
    host_exited = lambda: host.poll() is not None  # noqa: E731
    try:
        wait_until(
            lambda: {"probe_a", "probe_b"} <= set(api_request(port, "/online-devices")["online_devices"]),
            timeout=STEP_TIMEOUT,
            abort=host_exited,
            description="probe_a / probe_b 由各自的受管 Slave 接入",
        )
        processes = {item["id"] for item in api_request(port, "/device-processes")["processes"]}
        assert processes == {"root-probe_a", "root-probe_b"}, sorted(processes)
        assert_ros_network_transport(port, {"probe_a", "probe_b"}, abort=host_exited)
        expected = {"probe_a": ["probe_b", "waste"], "probe_b": ["probe_a", "waste"]}
        for device_id, linked in expected.items():
            task = _start_action(port, device_id, "linked", {"node_id": device_id})
            assert _wait_task(port, task, {"succeeded", "failed"}, timeout=STEP_TIMEOUT, abort=host_exited)["status"] == "succeeded"
            value = (_job(port, task) or {}).get("return_info", {}).get("return_value")
            assert value == {"success": True, "linked": linked, "nodes": 3}, value
        for process_id in ("root-probe_a", "root-probe_b"):
            log = work / "host-db" / "work" / "device_processes" / process_id / "process.log"
            assert "已从 Host 取得物理拓扑图（3 个节点、3 条连线）" in log.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 失败时附带 Host 与各受管 Slave 的日志
        logs = f"--- host log tail ---\n{_tail(host_log)}"
        for managed_log in sorted((work / "host-db" / "work" / "device_processes").glob("*/process.log")):
            logs += f"\n--- managed slave {managed_log.parent.name} log tail ---\n{_tail(managed_log, 30)}"
        pytest.fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}\n{logs}", pytrace=False)
    finally:
        stop_process(host)
        handle.close()
