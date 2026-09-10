"""Real HTTP smoke test for the compatibility Workflow BatchTask API.

This intentionally uses the same FastAPI application and SQLite store as the
backend contract, but no device scheduler, so it is safe to run on a laptop.
"""

from __future__ import annotations

import socket
import threading
import time
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

# Prefer this checkout over an editable Uni-LabOS install from another worktree.
# The smoke test is intentionally run from the repository and must exercise the
# same compatibility API that the local runtime launcher uses.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requests
import uvicorn

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
import unilabos.app.workflow_api as workflow_api


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.02)
    raise TimeoutError(f"HTTP server did not start on {host}:{port}")


def _free_port(host: str = "127.0.0.1") -> int:
    """Pick an unused loopback port so the smoke test cannot hit a stale server."""

    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("UNILAB_SMOKE_PORT", "0")) or _free_port(host)
    with TemporaryDirectory(prefix="unilab-batch-http-") as temp_dir:
        store = WorkflowStore(Path(temp_dir) / "workflow.db")
        server = uvicorn.Server(
            uvicorn.Config(
                create_workflow_app(WorkflowService(store)),
                host=host,
                port=port,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            _wait_for_port(host, port)
            base = f"http://{host}:{port}/api/v1"
            created = requests.post(
                f"{base}/workflows",
                json={"name": "batch smoke", "tags": [], "meta_data": {}},
                timeout=5,
            )
            created.raise_for_status()
            workflow_uuid = created.json()["data"]["uuid"]
            graph = requests.put(
                f"{base}/workflows/{workflow_uuid}/graph",
                json={
                    "revision": 1,
                    "nodes": [
                        {
                            "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                            "name": "approval",
                            "type": "manual_confirm",
                            "pose": {},
                            "param": {},
                            "execution_policy": {},
                            "disabled": False,
                            "minimized": False,
                            "meta_data": {},
                        }
                    ],
                    "edges": [],
                },
                timeout=5,
            )
            graph.raise_for_status()
            response = requests.post(
                f"{base}/workflow-batch-tasks",
                json={
                    "workflow_uuid": workflow_uuid,
                    "description": "HTTP batch smoke",
                    "meta_data": {"campaign": "smoke"},
                    "instances": [
                        {
                            "instance_id": "sample-001",
                            "input": {},
                            "meta_data": {"volume_ml": 100},
                        },
                        {
                            "instance_id": "sample-002",
                            "input": {},
                            "meta_data": {"volume_ml": 200},
                        },
                    ],
                },
                timeout=5,
            )
            if not response.ok:
                raise RuntimeError(
                    f"batch API returned HTTP {response.status_code}: {response.text}"
                )
            batch = response.json()["data"]
            fetched = requests.get(
                f"{base}/workflow-batch-tasks/{batch['uuid']}", timeout=5
            )
            fetched.raise_for_status()
            assert fetched.json()["data"] == batch
            assert batch["total_instances"] == 2
            assert [item["input"] for item in batch["instances"]] == [{}, {}]
            print(
                {
                    "batch_task_uuid": batch["uuid"],
                    "status": batch["status"],
                    "child_task_uuids": [
                        item["workflow_task_uuid"] for item in batch["instances"]
                    ],
                }
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            store.close()


if __name__ == "__main__":
    main()
