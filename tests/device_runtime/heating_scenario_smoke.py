"""HostLink/ROS2 共用同一 canonical 多 Job Workflow 的进程级 smoke。"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from unilabos.server.demo.heating_scenarios import build_heating_scenario_graph


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    request = Request(
        base_url + path,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
        data=data,
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - 本机 smoke
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
    if isinstance(payload, dict) and "code" in payload:
        if payload["code"] != 0:
            raise RuntimeError(f"{method} {path} business error: {payload}")
        return payload.get("data")
    return payload


def _time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _run_scenario(
    base_url: str,
    scenario_id: str,
    *,
    target: float,
    duration: float,
) -> dict[str, Any]:
    environment = _request(
        base_url,
        "POST",
        f"/api/v1/materials/heating-demo/scenarios/{scenario_id}/reset",
        {
            "request_uuid": str(uuid4()),
            "source_device_id": "virtual-heater",
            "target_device_id": "virtual-heater-target",
        },
    )
    workflow = _request(
        base_url,
        "POST",
        "/api/v1/workflows",
        {
            "name": f"smoke:{scenario_id}:{uuid4().hex[:8]}",
            "tags": ["smoke", scenario_id],
            "meta_data": {"scenario_id": scenario_id},
        },
    )
    graph = build_heating_scenario_graph(
        scenario_id,  # type: ignore[arg-type]
        revision=int(workflow["revision"]),
        environment=environment,
        target_temperature_c=target,
        duration_seconds=duration,
    )
    saved = _request(
        base_url,
        "PUT",
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        graph,
    )
    task = _request(
        base_url,
        "POST",
        "/api/v1/workflow-tasks",
        {
            "workflow_uuid": workflow["uuid"],
            "run_mode": "normal",
            "meta_data": {"scenario_id": scenario_id},
        },
    )
    deadline = time.monotonic() + max(35.0, duration * 10)
    current = task
    while time.monotonic() < deadline:
        current = _request(
            base_url,
            "GET",
            f"/api/v1/workflow-tasks/{task['uuid']}",
        )
        if current["status"] in {
            "succeeded",
            "failed",
            "canceled",
            "timeout",
        }:
            break
        time.sleep(0.05)
    jobs = _request(
        base_url,
        "GET",
        f"/api/v1/workflow-tasks/{task['uuid']}/jobs",
    )
    assert current["status"] == "succeeded", current
    assert all(job["status"] == "succeeded" for job in jobs), jobs
    expected_jobs = {
        "single_sequential": 2,
        "parallel_three_site": 3,
        "cross_device_transfer": 3,
    }[scenario_id]
    assert len(jobs) == expected_jobs
    node_to_job = {job["workflow_node_uuid"]: job["uuid"] for job in jobs}
    assert set(node_to_job) == {node["uuid"] for node in saved["nodes"]}

    ordered = sorted(jobs, key=lambda item: item["topological_index"])
    if scenario_id == "single_sequential":
        assert _time(ordered[0]["finished_at"]) <= _time(ordered[1]["started_at"])
    elif scenario_id == "parallel_three_site":
        assert max(_time(job["started_at"]) for job in jobs) < min(
            _time(job["finished_at"]) for job in jobs
        )
    else:
        assert [job["executor_kind"] for job in ordered] == [
            "device_action",
            "tool_call",
            "device_action",
        ]
        assert _time(ordered[0]["finished_at"]) <= _time(ordered[1]["started_at"])
        assert _time(ordered[1]["finished_at"]) <= _time(ordered[2]["started_at"])
        transfer = ordered[1]["return_info"]["return_value"]
        assert transfer["data"]["material_uuids"] == [
            environment["transfer_material_uuid"]
        ]
        assert transfer["data"]["destination_site_uuids"] == [
            environment["transfer_target_site_uuid"]
        ]
        baseline_version = environment["material_versions"][
            environment["transfer_material_uuid"]
        ]
        affected_material = next(
            item
            for item in transfer["affected"]
            if item["aggregate_type"] == "material"
            and item["aggregate_uuid"] == environment["transfer_material_uuid"]
        )
        assert affected_material["version"] > baseline_version

    materials = _request(base_url, "GET", "/api/v1/materials/instances")
    by_uuid = {item["material"]["material_uuid"]: item for item in materials}
    if scenario_id == "single_sequential":
        material_uuid = next(
            value
            for value, site_uuid in environment["assignments"].items()
            if site_uuid is not None
        )
        assert by_uuid[material_uuid]["data"]["data"]["temperature_c"] == target
    elif scenario_id == "parallel_three_site":
        observed = sorted(
            by_uuid[material_uuid]["data"]["data"]["temperature_c"]
            for material_uuid, site_uuid in environment["assignments"].items()
            if site_uuid is not None
        )
        assert observed == [target - 10, target, target + 10]
    else:
        material = by_uuid[environment["transfer_material_uuid"]]
        assert material["material"]["version"] >= affected_material["version"]
        target_platform = by_uuid[environment["target_platform_uuid"]]
        target_site = next(
            item
            for item in target_platform["sites"]
            if item["site_uuid"] == environment["transfer_target_site_uuid"]
        )
        assert target_site["occupied_material_uuid"] == environment[
            "transfer_material_uuid"
        ]
        assert material["data"]["data"]["temperature_c"] == target

    return {
        "scenario_id": scenario_id,
        "workflow_uuid": workflow["uuid"],
        "task_uuid": task["uuid"],
        "job_count": len(jobs),
        "node_to_job": node_to_job,
        "jobs": jobs,
        "environment": environment,
    }


def run_heating_scenario_smoke(
    backend: str,
    timeout: float = 90.0,
) -> dict[str, Any]:
    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repository_root = Path(__file__).resolve().parents[2]
    graph = repository_root / "unilabos" / "test" / "experiments" / "virtual_heating_platform_demo.json"
    config = repository_root / "unilabos" / "config" / "example_config.py"
    with tempfile.TemporaryDirectory(prefix=f"heating-dag-{backend}-") as directory:
        root = Path(directory)
        log_path = root / "runtime.log"
        port = _free_port()
        hostlink_port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        command = [
            sys.executable,
            "-m",
            "unilabos",
            "--backend",
            backend,
            "--demo_mode",
            "--skip_env_check",
            "--test_mode",
            "--external_devices_only",
            "--visual",
            "disable",
            "--disable_browser",
            "--port",
            str(port),
            "--server_database_root",
            str(root / "db"),
            "--working_dir",
            str(root / "work"),
            "--config",
            str(config),
            "-g",
            str(graph),
        ]
        if backend == "hostlink":
            command += [
                "--hostlink_bind",
                "127.0.0.1",
                "--hostlink_port",
                str(hostlink_port),
            ]
        else:
            domain_id = str(10 + hostlink_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            command += ["--disable_hostlink", "--ros_domain_id", domain_id]

        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                cwd=repository_root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout
                readiness_error: Exception | None = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        health = _request(base_url, "GET", "/api/v1/health")
                        catalog = _request(
                            base_url,
                            "GET",
                            "/api/v1/workflow-template-catalog",
                        )
                        materials = _request(
                            base_url,
                            "GET",
                            "/api/v1/materials/instances",
                        )
                        resource_ids = {
                            item["material"]["resource_id"] for item in materials
                        }
                        if (
                            health["status"] == "ok"
                            and len(catalog["node_templates"]) >= 2
                            and {
                                "virtual-heater",
                                "virtual-heater-target",
                            }.issubset(resource_ids)
                        ):
                            break
                    except (OSError, RuntimeError, URLError, KeyError) as exc:
                        readiness_error = exc
                    time.sleep(0.25)
                else:
                    raise RuntimeError(
                        "demo microbackend did not become ready; "
                        f"last readiness error: {readiness_error!r}"
                    )

                scenarios = {
                    scenario_id: _run_scenario(
                        base_url,
                        scenario_id,
                        target=target,
                        duration=0.4,
                    )
                    for scenario_id, target in (
                        ("single_sequential", 70.0),
                        ("parallel_three_site", 80.0),
                        ("cross_device_transfer", 90.0),
                    )
                }
                telemetry = _request(
                    base_url,
                    "GET",
                    "/api/v1/telemetry/events?limit=500",
                )
                history = _request(
                    base_url,
                    "GET",
                    "/api/v1/history/events?limit=500",
                )
                assert telemetry, "telemetry.v1 must contain device samples"
                assert history, "history.v1 must contain workflow/job events"
                return {
                    "success": True,
                    "backend": backend,
                    "scenarios": scenarios,
                    "telemetry_event_count": len(telemetry),
                    "history_event_count": len(history),
                }
            except Exception as exc:
                logs = log_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(f"{backend} multi-job smoke failed: {exc}\n{logs}") from exc
            finally:
                _stop(process)


if __name__ == "__main__":
    selected_backend = sys.argv[1] if len(sys.argv) > 1 else "hostlink"
    print(
        json.dumps(
            run_heating_scenario_smoke(selected_backend),
            ensure_ascii=False,
            indent=2,
        )
    )
