"""用同一 graph 和输入启动 HostLink/ROS2 三场景 smoke。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any


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


def _assert_proof(proof: dict[str, Any], backend: str) -> None:
    assert proof["success"] is True
    assert proof["backend"] == backend
    scenarios = proof["scenarios"]
    assert set(scenarios) == {
        "single_sequential",
        "parallel_three_site",
        "cross_device_transfer",
    }
    sequential_sites = scenarios["single_sequential"]["final_state"]["sites"]
    assert sequential_sites[0]["temperature_c"] == 70.0
    assert sequential_sites[0]["material_uuid"]
    assert not sequential_sites[1]["material_uuid"]
    assert not sequential_sites[2]["material_uuid"]

    parallel_sites = scenarios["parallel_three_site"]["final_state"]["sites"]
    assert [item["temperature_c"] for item in parallel_sites] == [70.0, 80.0, 90.0]
    assert all(item["material_uuid"] for item in parallel_sites)

    transfer = scenarios["cross_device_transfer"]
    assert transfer["steps"][1]["target_device"] == "virtual-heater"
    transfer_sites = transfer["final_state"]["sites"]
    assert not transfer_sites[0]["material_uuid"]
    assert transfer_sites[2]["temperature_c"] == 90.0
    assert transfer_sites[2]["material_uuid"] == sequential_sites[0]["material_uuid"]


def run_heating_scenario_smoke(
    backend: str,
    timeout: float = 45.0,
) -> dict[str, Any]:
    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repository_root = Path(__file__).resolve().parents[2]
    graph = (
        repository_root
        / "unilabos"
        / "test"
        / "experiments"
        / "virtual_heating_platform_demo.json"
    )
    config = repository_root / "unilabos" / "config" / "example_config.py"
    with tempfile.TemporaryDirectory(
        prefix=f"heating-scenarios-{backend}-"
    ) as directory:
        root = Path(directory)
        proof_path = root / "proof.json"
        log_path = root / "runtime.log"
        hostlink_port = _free_port()
        environment = os.environ.copy()
        environment.update(
            {
                "UNILABOS_HEATING_SCENARIO_PROOF_FILE": str(proof_path),
                "UNILABOS_HEATING_SCENARIO_START_DELAY": (
                    "2.0" if backend == "ros2" else "0.2"
                ),
                "UNILABOS_HEATING_SCENARIO_DURATION": "0.05",
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = [
            sys.executable,
            "-m",
            "unilabos",
            "--backend",
            backend,
            "--skip_env_check",
            "--test_mode",
            "--devices",
            str(repository_root / "unilabos" / "devices" / "virtual"),
            "--external_devices_only",
            "--visual",
            "disable",
            "--disable_browser",
            "--port",
            str(_free_port()),
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
            command += [
                "--disable_hostlink",
                "--ros_domain_id",
                domain_id,
            ]

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
                while time.monotonic() < deadline:
                    if proof_path.is_file():
                        proof = json.loads(proof_path.read_text(encoding="utf-8"))
                        if proof.get("success") is not True:
                            raise RuntimeError(
                                f"{backend} scenario smoke failed: {proof}\n"
                                + log_path.read_text(encoding="utf-8", errors="replace")
                            )
                        _assert_proof(proof, backend)
                        return proof
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                raise RuntimeError(
                    f"{backend} scenario smoke did not complete within {timeout}s\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")
                )
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
