"""用于电导工站联调的最小 UniLab Job API 模拟网关。

该网关只用于没有编译 ``unilabos_msgs`` 的本地开发环境。平台仍使用正式的
UniLab Job API 契约；网关把 Job 动作分发到 ``ConductivityStation`` 注册设备，
设备再通过真实 TCP/CRLF 协议访问模拟工站。
"""

from __future__ import annotations

import argparse
import html
import inspect
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .conductivity_station import ConductivityStation
from .mock_server import MockConductivityServer, MockConductivityState


class MockUniLabJobState:
    """线程安全的 Job 存储与动作分发器。"""

    def __init__(
        self,
        device: ConductivityStation,
        device_id: str = "CONDUCTIVITY_STATION",
    ) -> None:
        self.device = device
        self.device_id = device_id
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _json_type(annotation: Any) -> str:
        """将动作参数注解转换为页面与 API 使用的简化 JSON Schema 类型。"""
        text = str(annotation).lower()
        if annotation is int or text in {"int", "<class 'int'>"}:
            return "integer"
        if annotation is float or text in {"float", "<class 'float'>"}:
            return "number"
        if annotation is bool or text in {"bool", "<class 'bool'>"}:
            return "boolean"
        if "list" in text or "tuple" in text or "set" in text:
            return "array"
        if "dict" in text:
            return "object"
        return "string"

    def actions(self) -> dict[str, dict[str, Any]]:
        """从实际设备类的 ``@action`` 元数据动态生成动作目录。"""
        catalog: dict[str, dict[str, Any]] = {}
        for name, method in inspect.getmembers(type(self.device), inspect.isfunction):
            meta = getattr(method, "_action_registry_meta", None)
            if not isinstance(meta, dict):
                continue
            signature = inspect.signature(method)
            properties: dict[str, dict[str, Any]] = {}
            required: list[str] = []
            parameters: list[dict[str, Any]] = []
            for parameter in signature.parameters.values():
                if parameter.name == "self":
                    continue
                parameter_type = self._json_type(parameter.annotation)
                is_required = parameter.default is inspect.Parameter.empty
                item: dict[str, Any] = {
                    "name": parameter.name,
                    "type": parameter_type,
                    "required": is_required,
                }
                schema_item: dict[str, Any] = {"type": parameter_type}
                if is_required:
                    required.append(parameter.name)
                else:
                    item["default"] = parameter.default
                    schema_item["default"] = parameter.default
                parameters.append(item)
                properties[parameter.name] = schema_item
            schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            if required:
                schema["required"] = required
            catalog[name] = {
                "name": name,
                "description": str(meta.get("description") or ""),
                "always_free": bool(meta.get("always_free")),
                "parameters": parameters,
                "schema": schema,
            }
        return dict(sorted(catalog.items()))

    def submit(
        self, device_id: str, action_name: str, action_args: dict[str, Any]
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        if device_id != self.device_id:
            result = {
                "jobId": job_id,
                "status": 6,
                "result": {"error": f"Device not found: {device_id}"},
            }
            with self.lock:
                self.jobs[job_id] = result
            return result
        method = getattr(self.device, action_name, None)
        if action_name not in self.actions() or method is None:
            result = {
                "jobId": job_id,
                "status": 6,
                "result": {"error": f"Action not found: {action_name}"},
            }
            with self.lock:
                self.jobs[job_id] = result
            return result
        try:
            return_value = method(**action_args)
            result = {
                "jobId": job_id,
                "status": 4,
                "result": {"return_value": return_value},
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "jobId": job_id,
                "status": 6,
                "result": {"error": str(exc)},
            }
        with self.lock:
            self.jobs[job_id] = result
        return result

    def status(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return self.jobs.get(
                job_id,
                {
                    "jobId": job_id,
                    "status": 6,
                    "result": {"error": "Job not found"},
                },
            )


class _JobApiHandler(BaseHTTPRequestHandler):
    server_version = "MockUniLab/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def state(self) -> MockUniLabJobState:
        return self.server.state  # type: ignore[attr-defined,no-any-return]

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _send(self, data: Any, *, code: int = 0, message: str = "success") -> None:
        payload = json.dumps(
            {"code": code, "data": data, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, content: str) -> None:
        payload = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _status_page(self) -> str:
        action_rows = []
        for action in self.state.actions().values():
            parameters = ", ".join(
                f"{item['name']}: {item['type']}"
                for item in action["parameters"]
            ) or "无"
            mode = "只读/免排队" if action["always_free"] else "控制动作"
            action_rows.append(
                "<tr>"
                f"<td><code>{html.escape(action['name'])}</code></td>"
                f"<td>{html.escape(parameters)}</td>"
                f"<td>{html.escape(action['description'])}</td>"
                f"<td><span class='tag'>{mode}</span></td>"
                "</tr>"
            )
        rows = "".join(action_rows)
        device_id = html.escape(self.state.device_id)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mock UniLab 主机状态</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, "Microsoft YaHei", sans-serif; }}
    body {{ margin: 0; background: #09111f; color: #e8eef8; }}
    header {{ padding: 22px 32px; border-bottom: 1px solid #25324a; background: #101b2e; }}
    header h1 {{ margin: 0 0 6px; font-size: 24px; }}
    header p {{ margin: 0; color: #9fb0c9; }}
    .warning {{ margin: 24px auto 0; max-width: 1120px; padding: 14px 18px; border: 1px solid #d29b36;
      border-radius: 10px; background: #2b2112; color: #ffd88a; }}
    main {{ max-width: 1120px; margin: 20px auto 48px; padding: 0 20px; }}
    .card {{ background: #101b2e; border: 1px solid #25324a; border-radius: 12px; padding: 20px; margin-bottom: 18px; }}
    .device {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .online {{ color: #72e6a1; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: #42d883; display: inline-block; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
    th, td {{ text-align: left; padding: 12px 10px; border-bottom: 1px solid #25324a; vertical-align: top; }}
    th {{ color: #9fb0c9; font-weight: 600; }}
    code {{ color: #8cc8ff; }}
    .tag {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: #203554; color: #b8d9ff; white-space: nowrap; }}
    a {{ color: #8cc8ff; }}
  </style>
</head>
<body>
  <header>
    <h1>UniLab 主机状态 · Mock</h1>
    <p>电导工站本地接口模拟与动作注册展示</p>
  </header>
  <div class="warning"><strong>模拟环境：</strong>此页面不是完整 UniLab/ROS 运行时，仅用于验证驱动动作发现和 Job API 联调。</div>
  <main>
    <section class="card">
      <div class="device"><span class="dot"></span><strong>{device_id}</strong><span class="online">Online</span><span>local-mock</span></div>
      <p>设备类：<code>conductivity_station</code>　动作数量：{len(action_rows)}</p>
    </section>
    <section class="card">
      <h2>已发现的注册动作</h2>
      <table>
        <thead><tr><th>动作</th><th>参数</th><th>说明</th><th>模式</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    <section class="card">
      <strong>动作 API：</strong>
      <a href="/api/v1/devices/{device_id}/actions">/api/v1/devices/{device_id}/actions</a>
    </section>
  </main>
</body>
</html>"""

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/v1/job/add":
            self._send({}, code=1, message="not found")
            return
        try:
            body = self._json_body()
            device_id = str(body.get("device_id") or "")
            action_name = str(body.get("action") or "")
            action_args = body.get("action_args") or {}
            if not isinstance(action_args, dict):
                raise ValueError("action_args 必须是对象")
            job = self.state.submit(device_id, action_name, action_args)
            self._send({"jobId": job["jobId"], "status": 1})
        except Exception as exc:  # noqa: BLE001
            self._send({}, code=1, message=str(exc))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/status", "/registry-editor"}:
            self._send_html(self._status_page())
            return
        if path == "/api/v1/online-devices":
            self._send(
                {
                    "online_devices": {
                        self.state.device_id: {
                            "device_key": f"/devices/{self.state.device_id}",
                            "namespace": "/devices",
                            "machine_name": "local-mock",
                        }
                    },
                    "total_count": 1,
                }
            )
            return
        action_prefix = f"/api/v1/devices/{self.state.device_id}/actions"
        if path == action_prefix:
            actions = self.state.actions()
            self._send(
                {
                    "device_id": self.state.device_id,
                    "actions": actions,
                    "total_count": len(actions),
                    "environment": "mock",
                }
            )
            return
        schema_marker = action_prefix + "/"
        schema_suffix = "/schema"
        if path.startswith(schema_marker) and path.endswith(schema_suffix):
            action_name = path[len(schema_marker) : -len(schema_suffix)]
            action = self.state.actions().get(action_name)
            if action is None:
                self._send({}, code=1, message=f"Action not found: {action_name}")
            else:
                self._send(
                    {
                        "device_id": self.state.device_id,
                        "action": action_name,
                        "schema": action["schema"],
                    }
                )
            return
        if path == "/api/v1/actions":
            actions = self.state.actions()
            self._send(
                {
                    "devices": {self.state.device_id: actions},
                    "total_count": len(actions),
                    "environment": "mock",
                }
            )
            return
        prefix, suffix = "/api/v1/job/", "/status"
        if path.startswith(prefix) and path.endswith(suffix):
            job_id = path[len(prefix) : -len(suffix)]
            self._send(self.state.status(job_id))
            return
        self._send({}, code=1, message="not found")


class MockUniLabJobServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        state: MockUniLabJobState,
    ) -> None:
        self.state = state
        super().__init__(address, _JobApiHandler)


class ConductivityIntegrationMock:
    """同时管理模拟工站和模拟 UniLab Job API，供测试及演示使用。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        api_port: int = 18002,
        station_port: int = 19091,
        step_interval: float = 0.1,
        failure_sample: int | None = None,
        failure_step: int = 10,
    ) -> None:
        station_state = MockConductivityState(
            step_interval=step_interval,
            failure_sample=failure_sample,
            failure_step=failure_step,
        )
        self.station_server = MockConductivityServer(
            (host, station_port), state=station_state
        )
        actual_station_port = int(self.station_server.server_address[1])
        self.device = ConductivityStation(ip=host, port=actual_station_port)
        self.job_state = MockUniLabJobState(self.device)
        self.api_server = MockUniLabJobServer((host, api_port), state=self.job_state)
        self._threads: list[threading.Thread] = []

    @property
    def api_port(self) -> int:
        return int(self.api_server.server_address[1])

    @property
    def station_port(self) -> int:
        return int(self.station_server.server_address[1])

    def start(self) -> None:
        for server, name in (
            (self.station_server, "mock-conductivity-station"),
            (self.api_server, "mock-unilab-job-api"),
        ):
            thread = threading.Thread(
                target=server.serve_forever, name=name, daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self.device.close()
        for server in (self.api_server, self.station_server):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    def __enter__(self) -> "ConductivityIntegrationMock":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="电导工站 UniLab 全链路模拟器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=18002)
    parser.add_argument("--station-port", type=int, default=19091)
    parser.add_argument("--step-interval", type=float, default=0.5)
    parser.add_argument("--failure-sample", type=int)
    parser.add_argument("--failure-step", type=int, default=10)
    args = parser.parse_args()
    mock = ConductivityIntegrationMock(
        host=args.host,
        api_port=args.api_port,
        station_port=args.station_port,
        step_interval=args.step_interval,
        failure_sample=args.failure_sample,
        failure_step=args.failure_step,
    )
    mock.start()
    print(
        f"模拟 UniLab Job API: http://{args.host}:{mock.api_port}/api/v1\n"
        f"模拟电导工站 TCP: {args.host}:{mock.station_port}",
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        mock.close()


if __name__ == "__main__":
    main()
