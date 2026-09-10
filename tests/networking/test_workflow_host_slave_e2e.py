"""Host scheduler -> Slave device workflow E2E.

This is a process-level regression for the payload emitted by the Edge UI.  It
starts a host with the default scheduler microbackend and a slave containing a
real virtual pump, then verifies that an executor result is injected into the
next action through a workflow handle.

The test is intentionally gated with ``UNILAB_NETWORKING_TEST=1`` because it
starts two complete UniLabOS/ROS processes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("UNILAB_NETWORKING_TEST") != "1",
    reason="process networking test: set UNILAB_NETWORKING_TEST=1 to enable",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_GRAPH = REPO_ROOT / "unilabos" / "test" / "experiments" / "empty_devices.json"
SLAVE_GRAPH = (
    REPO_ROOT / "unilabos" / "test" / "experiments" / "mock_devices" / "mock_pump.json"
)
DEVICE_ID = "MockPump1"
# This machine's Fast DDS 2.6 installation cannot match independent local
# processes reliably.  The process E2E therefore uses another supported ROS 2
# RMW (Cyclone DDS) with a directed loopback peer.  Product-side Fast DDS
# Discovery Server lifecycle/port advertising is covered by unit tests.
ROS_DOMAIN = 0
CYCLONE_DIRECTED_LOOPBACK = (
    '<CycloneDDS><Domain><General><Interfaces><NetworkInterface address="127.0.0.1"/>'
    "</Interfaces><AllowMulticast>false</AllowMulticast></General><Discovery>"
    "<ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>20</MaxAutoParticipantIndex>"
    '<Peers><Peer Address="127.0.0.1"/></Peers></Discovery></Domain></CycloneDDS>'
)
STARTUP_TIMEOUT_S = 120.0
WORKFLOW_TIMEOUT_S = 45.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _spawn_unilab(
    args: List[str], env_extra: Dict[str, str], cwd: Path, log_path: Path
) -> subprocess.Popen:
    config_path = cwd / "local_config.py"
    config_path.write_text("# process E2E config: use defaults\n", encoding="utf-8")
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPO_ROOT), existing_pythonpath) if value
    )
    for key in (
        "ROS_DOMAIN_ID",
        "ROS_AUTOMATIC_DISCOVERY_RANGE",
        "ROS_STATIC_PEERS",
        "ROS_DISCOVERY_SERVER",
        "ROS_SUPER_CLIENT",
        "RMW_IMPLEMENTATION",
        "CYCLONEDDS_URI",
    ):
        env.pop(key, None)
    env.update(env_extra)
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "unilabos", "--config", str(config_path), *args],
        cwd=str(cwd),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    process._unilab_log = log_file  # type: ignore[attr-defined]
    return process


def _terminate(process: Optional[subprocess.Popen]) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log = getattr(process, "_unilab_log", None)
    if log is not None:
        log.close()


def _http_json(url: str, timeout: float = 4.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"POST {url} failed: HTTP {exc.code}: {body}") from exc


def _wait_for(
    predicate: Callable[[], Any], timeout_s: float, *, description: str
) -> Any:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout_s}s: {description}")


@pytest.fixture(scope="module")
def executable_host_slave(tmp_path_factory):
    pytest.importorskip("rclpy")
    assert HOST_GRAPH.exists()
    assert SLAVE_GRAPH.exists()

    link_port = _free_port()
    host_web = _free_port()
    slave_web = _free_port()
    work = tmp_path_factory.mktemp("workflow-host-slave-e2e")
    host_dir = work / "host"
    slave_dir = work / "slave"
    host_dir.mkdir()
    slave_dir.mkdir()
    host_log = work / "host.log"
    slave_log = work / "slave.log"

    common_args = [
        "--backend",
        "ros",
        "--disable_browser",
        "--app_bridges",
        "fastapi",
    ]
    directed_ros_env = {
        "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp",
        "CYCLONEDDS_URI": CYCLONE_DIRECTED_LOOPBACK,
        "UNILABOS_HOSTLINKCONFIG_ADVERTISE_IP": "127.0.0.1",
        # This E2E validates business traffic on ROS Action.  The managed Fast
        # DDS server itself is covered separately and is intentionally off when
        # running with Cyclone DDS.
        "UNILABOS_HOSTLINKCONFIG_ROS_DISCOVERY_SERVER": "off",
    }
    host = _spawn_unilab(
        [
            *common_args,
            "--graph",
            str(HOST_GRAPH),
            "--hostlink_addr",
            f"127.0.0.1:{link_port}",
            "--ros_domain_id",
            str(ROS_DOMAIN),
            "--port",
            str(host_web),
        ],
        directed_ros_env,
        host_dir,
        host_log,
    )
    slave: Optional[subprocess.Popen] = None
    try:
        _wait_for(
            lambda: (
                body
                if (body := _http_json(f"http://127.0.0.1:{host_web}/api/v1/health"))
                and body.get("scheduler") == "ready"
                else None
            ),
            STARTUP_TIMEOUT_S,
            description=f"host scheduler REST ready; log={host_log}",
        )

        slave = _spawn_unilab(
            [
                *common_args,
                "--graph",
                str(SLAVE_GRAPH),
                "--is_slave",
                "--hostlink_addr",
                f"127.0.0.1:{link_port}",
                "--port",
                str(slave_web),
            ],
            directed_ros_env,
            slave_dir,
            slave_log,
        )

        _wait_for(
            lambda: (
                body
                if (
                    body := _http_json(
                        f"http://127.0.0.1:{host_web}/api/v1/hostlink/peers"
                    )
                )
                and any(
                    peer.get("online") and DEVICE_ID in peer.get("device_ids", [])
                    for peer in body.get("peers", [])
                )
                else None
            ),
            STARTUP_TIMEOUT_S,
            description=f"slave connected to Host control plane; log={slave_log}",
        )

        devices_body = _wait_for(
            lambda: (
                body
                if (
                    body := _http_json(
                        f"http://127.0.0.1:{host_web}/api/v1/online-devices"
                    )
                )
                and DEVICE_ID in body.get("data", {}).get("online_devices", {})
                else None
            ),
            STARTUP_TIMEOUT_S,
            description=f"HostNode lists ROS-ready Slave device; host_log={host_log}",
        )
        assert devices_body["data"]["online_devices"][DEVICE_ID]["transport"] == "ros"

        yield {
            "base_url": f"http://127.0.0.1:{host_web}/api/v1",
            "host_log": host_log,
            "slave_log": slave_log,
        }
    finally:
        _terminate(slave)
        _terminate(host)


def test_ui_workflow_handle_roundtrip_through_host_and_slave(executable_host_slave):
    """UI wire payload -> scheduler -> Host -> Slave -> result -> next args."""

    base_url = executable_host_slave["base_url"]
    workflow_id = f"wf-host-slave-handle-{int(time.time() * 1000)}"
    payload = {
        "workflow_id": workflow_id,
        "nodes": [
            {
                "id": "source",
                "device_id": DEVICE_ID,
                "action_name": "set_position",
                "action_type": "SetPumpPosition",
                "param": {"position": 7.5, "max_velocity": 5.0},
            },
            {
                "id": "target",
                "device_id": DEVICE_ID,
                "action_name": "set_position",
                "action_type": "SetPumpPosition",
                # Sentinel: handle resolution must overwrite this with 7.5.
                "param": {"position": 1.0, "max_velocity": 5.0},
            },
        ],
        "edges": [
            {
                "uuid": "edge-source-target",
                "source_node_id": "source",
                "target_node_id": "target",
                "source_handle_uuid": "handle-source-final-position",
                "target_handle_uuid": "handle-target-position",
            }
        ],
        # This is the exact UUID-handle subset emitted by EditorView.vue.
        "handles": [
            {
                "uuid": "handle-source-final-position",
                "data_source": "executor",
                "handle_key": "out",
                "data_key": "final_position",
            },
            {
                "uuid": "handle-target-position",
                "data_source": "handle",
                "handle_key": "in",
                "data_key": "position",
            },
        ],
        "priority": "normal",
    }

    submitted = _post_json(f"{base_url}/workflows", payload)
    assert submitted["workflow_id"] == workflow_id
    assert submitted["state"] == "running"

    terminal = _wait_for(
        lambda: (
            body
            if (body := _http_json(f"{base_url}/workflows/{workflow_id}"))
            and body.get("state") in {"success", "failed"}
            else None
        ),
        WORKFLOW_TIMEOUT_S,
        description=(
            "workflow terminal; "
            f"host_log={executable_host_slave['host_log']} "
            f"slave_log={executable_host_slave['slave_log']}"
        ),
    )
    assert terminal["state"] == "success"
    assert terminal["nodes"]["source"]["state"] == "success"
    assert terminal["nodes"]["target"]["state"] == "success"

    history = _wait_for(
        lambda: (
            body
            if (body := _http_json(f"{base_url}/history/workflows/{workflow_id}"))
            and len(body.get("jobs", [])) == 2
            else None
        ),
        10.0,
        description="two completed jobs persisted in host workflow history",
    )
    jobs_by_node = {job["node_id"]: job for job in history["jobs"]}
    assert jobs_by_node["source"]["ret_value"]["final_position"] == pytest.approx(7.5)
    assert jobs_by_node["target"]["ret_value"]["final_position"] == pytest.approx(7.5)
    assert history["spec"]["nodes"][1]["param"]["position"] == pytest.approx(1.0)

    # HostNode's ROS result callback also feeds the legacy job-result cache for
    # existing HTTP consumers.
    target_job = _http_json(f"{base_url}/job/{jobs_by_node['target']['job_id']}/status")
    assert target_job is not None
    assert target_job["data"]["status"] == 4
    assert target_job["data"]["result"]["return_value"][
        "final_position"
    ] == pytest.approx(7.5)

    host_log = executable_host_slave["host_log"].read_text(
        encoding="utf-8", errors="replace"
    )
    slave_log = executable_host_slave["slave_log"].read_text(
        encoding="utf-8", errors="replace"
    )
    assert "[JobExecutionBackend] goal sent for job" in host_log
    assert "through HostLink" not in host_log
    assert "Slave node info registered through ROS" in slave_log
