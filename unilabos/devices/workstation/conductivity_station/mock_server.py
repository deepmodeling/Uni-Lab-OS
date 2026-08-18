"""电导工站 TCP 协议模拟服务端。"""

from __future__ import annotations

import argparse
import copy
import json
import math
import socketserver
import threading
import time
from datetime import datetime
from typing import Any


STEP_NAMES = {
    1: "模具拆解",
    2: "模具测厚",
    3: "模具转移",
    4: "漏斗转移",
    5: "烧结料瓶扫码与转移",
    6: "加粉",
    7: "烧结料瓶暂存",
    8: "模具组装",
    9: "模具转移",
    10: "EIS测试",
    11: "模具测厚",
    12: "电子电导率测试",
    13: "测试物料转移至托盘",
}

# 合作方高保真模型确认使用直径 1 cm 圆片：A = π × (0.5 cm)²。
DISC_AREA_CM2 = math.pi * 0.5**2


class MockConductivityState:
    """线程安全的批次模拟状态。"""

    def __init__(
        self,
        *,
        step_interval: float = 0.1,
        failure_sample: int | None = None,
        failure_step: int = 10,
    ) -> None:
        self.step_interval = max(0.01, float(step_interval))
        self.failure_sample = failure_sample
        self.failure_step = int(failure_step)
        self.lock = threading.RLock()
        self.started_monotonic: float | None = None
        self.stop_requested = False
        self.stop_after_sample: int | None = None
        self.batch: dict[str, Any] | None = None
        self.history: dict[str, dict[str, Any]] = {}
        self.materials = {
            "bottle": [1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
            "mold": [0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            "funnel": [0, 0, 0, 0, 0, 0, 1, 1, 1, 0],
        }

    @staticmethod
    def _positions(values: list[int]) -> list[int]:
        return [index for index, occupied in enumerate(values, start=1) if occupied]

    def _new_test(self, index: int, bottle: int, mold: int, funnel: int) -> dict[str, Any]:
        return {
            "state": "not_started",
            "test_time": "",
            "bottle": bottle,
            "mold": mold,
            "funnel": funnel,
            "step": 0,
            "bottle_code": "",
            "recipe": "",
            "formula": "",
            "temperature": 0.0,
            "due_pressure": 3,
            "pressure_time": 10,
            "R": 0.0,
            "h1": 0.0,
            "h2": 0.0,
            "area": DISC_AREA_CM2,
            "ion_conductivity": 0.0,
            "elec_conductivity_tested": False,
            "elec_R": 0.0,
            "elec_conductivity": 0.0,
            "_sample_index": index,
        }

    def _update(self) -> None:
        if not self.batch or self.started_monotonic is None or not self.batch["running"]:
            return
        tests = self.batch["tests"]
        elapsed_steps = int((time.monotonic() - self.started_monotonic) / self.step_interval)
        target_completed = elapsed_steps // 13
        active_step = elapsed_steps % 13 + 1

        if self.stop_after_sample is not None:
            target_completed = min(target_completed, self.stop_after_sample)

        for index, test in enumerate(tests, start=1):
            if test["state"] in {"finished", "failed"}:
                continue
            failure_reached = (
                index == self.failure_sample
                and elapsed_steps >= (index - 1) * 13 + self.failure_step - 1
            )
            if failure_reached:
                if not test["test_time"]:
                    test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test["state"] = "failed"
                test["step"] = self.failure_step
                test["error_code"] = "MOCK_STEP_FAILED"
                test["error_message"] = f"模拟失败：{STEP_NAMES[self.failure_step]}"
                self.batch["running"] = False
                self.batch["state"] = "failed"
                break
            if index <= target_completed:
                self._finish_test(test)
            elif index == target_completed + 1 and not (
                self.stop_after_sample is not None
                and index > self.stop_after_sample
            ):
                if not test["test_time"]:
                    test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test["state"] = "in_progress"
                test["step"] = active_step
                break

        finished = sum(item["state"] == "finished" for item in tests)
        failed = sum(item["state"] == "failed" for item in tests)
        self.batch["finished_count"] = finished
        self.batch["failed_count"] = failed
        active = next(
            (item for item in tests if item["state"] == "in_progress"), None
        )
        if active:
            self.batch["current_test"] = active["_sample_index"]
            self.batch["current_test_step"] = active["step"]
        if (
            self.stop_after_sample is not None
            and finished >= self.stop_after_sample
        ):
            self.batch["running"] = False
            self.batch["state"] = "stopped"
        elif finished + failed == len(tests):
            self.batch["running"] = False
            self.batch["state"] = "completed" if failed == 0 else "failed"
            self.batch["current_test"] = len(tests)
            self.batch["current_test_step"] = tests[-1]["step"] if tests else 0
        self.history[self.batch["batch_id"]] = copy.deepcopy(self.batch)

    @staticmethod
    def _finish_test(test: dict[str, Any]) -> None:
        index = int(test["_sample_index"])
        if not test["test_time"]:
            test["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        resistance_ohm = round(80.0 + index * 4.25, 3)
        h1_mm = 8.0
        h2_mm = round(8.8 + index * 0.02, 3)
        thickness_cm = (h2_mm - h1_mm) / 10.0
        ionic_conductivity_ms_cm = (
            thickness_cm / (resistance_ohm * DISC_AREA_CM2) * 1000.0
        )
        test.update(
            {
                "state": "finished",
                "step": 13,
                "bottle_code": f"LOT-260812-{index:03d}-CRU-S-{index:03d}-260812",
                "recipe": "R-202608-001",
                "formula": "Li6PS5Cl",
                "temperature": round(26.5 + index * 0.1, 2),
                "R": resistance_ohm,
                "h1": h1_mm,
                "h2": h2_mm,
                "area": DISC_AREA_CM2,
                "ion_conductivity": round(ionic_conductivity_ms_cm, 6),
            }
        )

    @staticmethod
    def _public(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: MockConductivityState._public(item)
                for key, item in value.items()
                if not key.startswith("_")
            }
        if isinstance(value, list):
            return [MockConductivityState._public(item) for item in value]
        return value

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        action = request.get("action")
        param = request.get("param") or {}
        with self.lock:
            self._update()
            if action == "station_status":
                return self._response(
                    request_id,
                    data={
                        "robot_arm": 1,
                        "scanner": 1,
                        "lid_open_close_mechanism": 1,
                        "powder_adding_mechanism": 1,
                        "tablet_pressing_mechanism": 1,
                        "electrochemical_workstation": 1,
                        "stack_rack": 1,
                    },
                )
            if action == "material_status":
                return self._response(request_id, data=copy.deepcopy(self.materials))
            if action == "batch_status":
                if not self.batch:
                    return self._response(request_id, data={"running": False, "batch": None})
                summary_keys = (
                    "batch_id",
                    "test_round",
                    "bottle",
                    "mold",
                    "funnel",
                    "current_test",
                    "current_test_step",
                    "finished_count",
                    "failed_count",
                    "state",
                )
                summary = {key: self.batch.get(key) for key in summary_keys}
                return self._response(
                    request_id,
                    data={"running": self.batch["running"], "batch": summary},
                )
            if action == "batch_result":
                batch_id = str(param.get("batch_id") or "")
                batch = self.history.get(batch_id)
                if not batch:
                    return self._response(request_id, result=9)
                return self._response(
                    request_id,
                    data={
                        "batch": {
                            "batch_id": batch_id,
                            "test_round": batch["test_round"],
                            "state": batch["state"],
                            "tests": self._public(copy.deepcopy(batch["tests"])),
                        }
                    },
                )
            if action == "start_batch":
                if self.batch and self.batch["running"]:
                    return self._response(request_id, result=4)
                positions = [self._positions(self.materials[key]) for key in ("bottle", "mold", "funnel")]
                if not positions[0] or len({len(items) for items in positions}) != 1:
                    return self._response(request_id, result=2)
                batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
                tests = [
                    self._new_test(index, bottle, mold, funnel)
                    for index, (bottle, mold, funnel) in enumerate(zip(*positions), start=1)
                ]
                self.batch = {
                    "batch_id": batch_id,
                    "test_round": len(tests),
                    "bottle": positions[0],
                    "mold": positions[1],
                    "funnel": positions[2],
                    "current_test": 1,
                    "current_test_step": 1,
                    "finished_count": 0,
                    "failed_count": 0,
                    "running": True,
                    "state": "running",
                    "tests": tests,
                }
                self.started_monotonic = time.monotonic()
                self.stop_requested = False
                self.stop_after_sample = None
                self.history[batch_id] = copy.deepcopy(self.batch)
                return self._response(request_id, data={"batch_id": batch_id})
            if action == "stop_current_batch":
                if not self.batch or not self.batch["running"]:
                    return self._response(request_id, result=3)
                self.stop_requested = True
                self.stop_after_sample = int(self.batch.get("current_test") or 1)
                self.batch["state"] = "stopping"
                return self._response(request_id)
            if action == "manual_run":
                if not self.batch or not self.batch["running"]:
                    return self._response(request_id, result=3)
                step = int(param.get("step") or 0)
                if not 1 <= step <= 13:
                    return self._response(request_id, result=5)
                active = next(
                    (item for item in self.batch["tests"] if item["state"] == "in_progress"),
                    self.batch["tests"][0],
                )
                active["state"] = "in_progress"
                active["step"] = step
                if not active["test_time"]:
                    active["test_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return self._response(
                    request_id,
                    data={
                        "batch_id": self.batch["batch_id"],
                        "test_time": active["test_time"],
                    },
                )
            if action == "clear_current_batch":
                if self.batch and self.batch["running"]:
                    return self._response(request_id, result=4)
                self.batch = None
                self.started_monotonic = None
                self.stop_requested = False
                self.stop_after_sample = None
                return self._response(request_id)
            return self._response(request_id, result=8)

    @staticmethod
    def _response(
        request_id: Any, result: int = 0, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response: dict[str, Any] = {"request_id": request_id, "result": result}
        if data is not None:
            response["data"] = data
        return response


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        state: MockConductivityState = self.server.state  # type: ignore[attr-defined]
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            try:
                request = json.loads(raw.decode("utf-8").strip())
                if not isinstance(request, dict):
                    raise ValueError("请求必须是 JSON 对象")
                response = state.handle(request)
            except Exception as exc:  # noqa: BLE001
                response = {"request_id": None, "result": 7, "message": str(exc)}
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write((payload + "\r\n").encode("utf-8"))
            self.wfile.flush()


class MockConductivityServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int] = ("127.0.0.1", 19091),
        *,
        state: MockConductivityState | None = None,
    ) -> None:
        self.state = state or MockConductivityState()
        super().__init__(address, _Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="电导工站 TCP 协议模拟服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--step-interval", type=float, default=0.5)
    parser.add_argument("--failure-sample", type=int)
    parser.add_argument("--failure-step", type=int, default=10)
    args = parser.parse_args()
    state = MockConductivityState(
        step_interval=args.step_interval,
        failure_sample=args.failure_sample,
        failure_step=args.failure_step,
    )
    with MockConductivityServer((args.host, args.port), state=state) as server:
        print(f"模拟电导工站监听 {args.host}:{server.server_address[1]}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
