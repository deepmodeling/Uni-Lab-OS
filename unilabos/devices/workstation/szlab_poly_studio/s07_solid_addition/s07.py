"""SZLab S07 固体加料工位设备驱动。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unilabos.registry.decorators import action, device, not_action

from .balance_history import DEFAULT_BALANCE_HISTORY_DIR, S07BalanceHistoryRecorder
from .sensors import (
    NODE_ALLOW_PROCESS,
    NODE_BALANCE_READING,
    NODE_COARSE_POSITION,
    NODE_COARSE_SHAKE_MAX_SPEED,
    NODE_FINE_POSITION,
    NODE_FINE_SHAKE_MAX_SPEED,
    NODE_HOME,
    NODE_LOAD_POSITION,
    NODE_PARAMS_WRITTEN,
    NODE_PROCESS_COMPLETE,
    NODE_PROCESS_SELECT,
    NODE_TARGET_WEIGHT,
    POSITION_RANGE,
    PROCESS_DOSE_POWDER,
    PROCESS_ROTATE_TO_FEED,
    PROCESS_SCAN_CARTRIDGES,
    QR_CODE_LENGTH,
    iter_s07_powder_param_vars,
    normalize_powder_params,
    s07_powder_param_var,
    s07_qr_code_var,
)

DEFAULT_POWDER_PARAMS_PATH = Path(__file__).resolve().parent / "s07_powder_params.json"
DOSE_POSITION_RANGE = range(0, 11)


@device(
    id="szlab_s07_solid_addition",
    display_name="S07 固体加料工位",
    category=["workstation", "szlab"],
    description="苏州实验室 S07 固体加料工位，通过 szlab_poly_plc 转发 PLC 读写",
)
class SZLabS07SolidAdditionDevice:
    def __init__(
        self,
        plc_device_id: str = "szlab_poly_plc",
        poll_interval: float = 0.2,
        balance_poll_interval: float = 2.0,
        balance_record_interval: float = 0.2,
        balance_history_dir: str | None = None,
        enable_balance_history: bool = True,
        *args,
        **kwargs,
    ):
        self.plc_device_id = plc_device_id
        self.poll_interval = poll_interval
        self.balance_poll_interval = max(float(balance_poll_interval), float(poll_interval))
        self.balance_record_interval = max(float(balance_record_interval), float(poll_interval))
        self.balance_history_dir = Path(balance_history_dir or DEFAULT_BALANCE_HISTORY_DIR)
        self.enable_balance_history = bool(enable_balance_history)
        self._plc_gateway: Any = None
        self._balance_status_callback: Callable[[dict[str, Any]], None] | None = None

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @not_action
    def set_balance_status_callback(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._balance_status_callback = callback

    @not_action
    def _publish_balance_status(
        self,
        *,
        value: float | None,
        state: str,
        message: str | None = None,
    ) -> None:
        if self._balance_status_callback is None:
            return
        payload: dict[str, Any] = {
            "label": "S07 实时天平",
            "value": value,
            "unit": "g",
            "state": state,
            "updated_at": time.time(),
        }
        if message:
            payload["message"] = message
        try:
            self._balance_status_callback(payload)
        except Exception:
            # 实时展示异常不能影响正在执行的注粉工艺。
            pass

    @not_action
    def _plc(self):
        if self._plc_gateway is None:
            raise RuntimeError("S07 固体加料工位尚未绑定 szlab_poly_plc")
        return self._plc_gateway

    @not_action
    def _read_plc_variable(self, node_name: str) -> Any:
        return self._plc().read_variable(node_name, use_cache=False)

    @not_action
    def _write_plc_variable(self, node_name: str, value: Any) -> None:
        self._plc().write_variable(node_name, value)

    @not_action
    def _wait_plc_bool(self, node_name: str, expected: bool, description: str) -> bool:
        return self._wait_plc_equal(node_name, expected, description)

    @not_action
    def _wait_plc_equal(self, node_name: str, expected: Any, description: str) -> bool:
        plc = self._plc()
        if not hasattr(plc, "wait_variable_equal"):
            raise RuntimeError(f"{self.plc_device_id} 不支持 wait_variable_equal，S07 需要直接复用 plc.py 等待逻辑")
        return bool(plc.wait_variable_equal(node_name, expected, interval=self.poll_interval))

    @not_action
    def _wait_process_complete(self, expected: int) -> bool:
        return self._wait_plc_equal(NODE_PROCESS_COMPLETE, expected, "S07 工艺完成")

    @not_action
    def _reset_unilab_written_params(self) -> None:
        reset_values = [
            (NODE_PROCESS_SELECT, 0),
            (NODE_PARAMS_WRITTEN, False),
            (NODE_LOAD_POSITION, 0),
            (NODE_COARSE_POSITION, 0),
            (NODE_FINE_POSITION, 0),
            (NODE_TARGET_WEIGHT, 0.0),
            *iter_s07_powder_param_vars(),
        ]
        for node, value in reset_values:
            try:
                self._write_plc_variable(node, value)
            except Exception:
                # 清理阶段逐项尝试，避免单个变量失败阻断其余参数复位。
                continue

    @not_action
    def _run_s07_process(self, process_id: int) -> dict[str, Any]:
        try:
            if not self._wait_plc_bool(NODE_HOME, True, "S07 原点信号"):
                return {"success": False, "message": "等待 S07 原点信号失败"}
            if not self._wait_plc_bool(NODE_ALLOW_PROCESS, True, "S07 允许加工"):
                return {"success": False, "message": "等待 S07 允许加工失败"}
            self._write_plc_variable(NODE_PROCESS_SELECT, process_id)
            self._write_plc_variable(NODE_PARAMS_WRITTEN, True)
            if not self._wait_process_complete(process_id):
                return {"success": False, "message": f"等待 S07 工艺完成失败（期望 {process_id}）"}
            return {"success": True, "process_type": process_id, "status": {"process_complete": process_id}}
        finally:
            self._reset_unilab_written_params()
            # 等待 PLC 确认上一轮已复位，避免下一轮误用残留的允许加工信号。
            self._wait_process_complete(0)

    @not_action
    def _run_dose_process_with_balance(
        self,
        recorder: S07BalanceHistoryRecorder | None,
    ) -> dict[str, Any]:
        balance_reading: float | None = None
        balance_sample_count = 0
        try:
            if not self._wait_plc_bool(NODE_HOME, True, "S07 原点信号"):
                return {"success": False, "message": "等待 S07 原点信号失败"}
            if not self._wait_plc_bool(NODE_ALLOW_PROCESS, True, "S07 允许加工"):
                return {"success": False, "message": "等待 S07 允许加工失败"}
            self._write_plc_variable(NODE_PROCESS_SELECT, PROCESS_DOSE_POWDER)
            self._write_plc_variable(NODE_PARAMS_WRITTEN, True)
            started = time.monotonic()
            next_balance_record = started
            next_balance_publish = started
            process_complete = 0
            while True:
                abort_check = getattr(self._plc(), "_mixing_wait_should_abort", None)
                if callable(abort_check) and abort_check():
                    return {
                        "success": False,
                        "message": "PLC 报警已中止 S07 注粉工艺等待",
                        "process_type": PROCESS_DOSE_POWDER,
                    }
                process_complete = int(self._read_plc_variable(NODE_PROCESS_COMPLETE) or 0)
                if process_complete == PROCESS_DOSE_POWDER:
                    break
                now = time.monotonic()
                if now >= next_balance_record:
                    try:
                        balance_reading = float(self._read_plc_variable(NODE_BALANCE_READING))
                        balance_sample_count += 1
                        if recorder is not None:
                            try:
                                recorder.record(balance_reading)
                            except Exception:
                                # 调试文件写入失败不能伪装成 PLC 天平读取失败。
                                pass
                        if now >= next_balance_publish:
                            self._publish_balance_status(value=balance_reading, state="ok")
                            next_balance_publish = now + self.balance_poll_interval
                    except Exception as exc:
                        if now >= next_balance_publish:
                            self._publish_balance_status(
                                value=balance_reading,
                                state="error",
                                message=f"读取暂时失败: {exc}",
                            )
                            next_balance_publish = now + self.balance_poll_interval
                    next_balance_record = now + self.balance_record_interval
                time.sleep(self.poll_interval)
            else:
                return {
                    "success": False,
                    "message": "等待 S07 注粉工艺完成超时",
                    "process_type": PROCESS_DOSE_POWDER,
                }
            try:
                balance_reading = float(self._read_plc_variable(NODE_BALANCE_READING))
                balance_sample_count += 1
                if recorder is not None:
                    try:
                        recorder.record(balance_reading)
                    except Exception:
                        pass
                self._publish_balance_status(value=balance_reading, state="final")
            except Exception as exc:
                self._publish_balance_status(
                    value=balance_reading,
                    state="error",
                    message=f"最终读数读取失败: {exc}",
                )
                return {
                    "success": False,
                    "status": "verification_failed",
                    "message": f"S07 注粉已完成，但最终天平读数读取失败: {exc}",
                    "process_type": PROCESS_DOSE_POWDER,
                    "balance_reading": balance_reading,
                    "balance_sample_count": balance_sample_count,
                }
            return {
                "success": True,
                "process_type": PROCESS_DOSE_POWDER,
                "status": {"process_complete": process_complete},
                "balance_reading": balance_reading,
                "balance_sample_count": balance_sample_count,
            }
        finally:
            self._reset_unilab_written_params()
            # 等待 PLC 确认上一轮已复位，避免下一轮误用残留的允许加工信号。
            self._wait_process_complete(0)

    @not_action
    def _read_qr_codes(self) -> dict[int, list[int]]:
        return {
            position: [
                int(self._read_plc_variable(s07_qr_code_var(position, index)) or 0)
                for index in range(QR_CODE_LENGTH)
            ]
            for position in POSITION_RANGE
        }

    @not_action
    def _write_powder_params(self, kind: str, params: dict[str, Any], shake_node: str) -> None:
        normalized = normalize_powder_params(params)
        field_map = {
            "opening": "开口量",
            "feed_speed": "落粉匀速",
            "rotation_speed": "旋转速度",
            "stop_amount": "提请停止量",
        }
        for key, field in field_map.items():
            for index, value in enumerate(normalized[key]):  # type: ignore[index]
                self._write_plc_variable(s07_powder_param_var(kind, field, index), value)
        self._write_plc_variable(shake_node, normalized["shake_max_speed"])

    @not_action
    def _load_powder_params_from_json(self, params_json: str | None, recipe_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = DEFAULT_POWDER_PARAMS_PATH
        if params_json:
            candidate = Path(params_json)
            if candidate.is_file():
                path = candidate
            else:
                default_data = json.loads(DEFAULT_POWDER_PARAMS_PATH.read_text(encoding="utf-8"))
                if params_json in default_data:
                    recipe_name = params_json
                else:
                    path = candidate
        data = json.loads(path.read_text(encoding="utf-8"))
        if recipe_name not in data:
            raise ValueError(f"注粉参数 JSON 中未找到 recipe: {recipe_name}")
        recipe = data[recipe_name]
        return dict(recipe.get("coarse_params", {})), dict(recipe.get("fine_params", {}))

    @action(auto_prefix=True, description="S07 粉罐扫码盘点")
    def scan_powder_cartridges(self) -> dict[str, Any]:
        result = self._run_s07_process(PROCESS_SCAN_CARTRIDGES)
        if result.get("success"):
            result["qr_codes"] = self._read_qr_codes()
        return result

    @action(auto_prefix=True, description="读取 S07 实时天平")
    def read_s07_balance(self) -> dict[str, Any]:
        try:
            value = float(self._read_plc_variable(NODE_BALANCE_READING))
        except Exception as exc:
            return {"success": False, "message": f"读取 S07 天平失败: {exc}"}
        return {
            "success": True,
            "value": value,
            "variable": NODE_BALANCE_READING,
        }

    @action(auto_prefix=True, description="S07 替换粉罐旋转到进料位")
    def rotate_powder_cartridge_to_feed(self, position: int) -> dict[str, Any]:
        if position not in POSITION_RANGE:
            return {"success": False, "message": "position 必须在 1-10 范围内"}
        try:
            self._write_plc_variable(NODE_LOAD_POSITION, int(position))
        except Exception:
            self._reset_unilab_written_params()
            raise
        result = self._run_s07_process(PROCESS_ROTATE_TO_FEED)
        result["position"] = position
        return result

    @action(auto_prefix=True, description="S07 注粉")
    def dose_powder(
        self,
        coarse_position: int,
        fine_position: int,
        target_weight: float,
        params_json: str | None = None,
        recipe_name: str = "default",
        powder_count: int = 1,
        powder_additions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if powder_additions:
            if powder_count != len(powder_additions):
                return {"success": False, "message": "powder_count 与 powder_additions 数量不一致"}
            addition_results: list[dict[str, Any]] = []
            for index, addition in enumerate(powder_additions, start=1):
                try:
                    result = self.dose_powder(
                        coarse_position=int(addition["coarse_position"]),
                        fine_position=int(addition["fine_position"]),
                        target_weight=float(addition["target_weight"]),
                        params_json=None,
                        recipe_name=str(addition["recipe_name"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return {
                        "success": False,
                        "message": f"第 {index} 种粉末参数无效：{exc}",
                        "powder_results": addition_results,
                    }
                addition_results.append(result)
                if not result.get("success"):
                    return {
                        "success": False,
                        "message": f"第 {index} 种粉末加粉失败：{result.get('message') or '未知错误'}",
                        "powder_results": addition_results,
                    }
            return {
                "success": True,
                "message": f"S07 已完成 {len(addition_results)} 种粉末加粉",
                "display_message": f"S07 已依次完成 {len(addition_results)} 种粉末加粉",
                "powder_count": len(addition_results),
                "powder_results": addition_results,
            }
        if coarse_position not in DOSE_POSITION_RANGE or fine_position not in DOSE_POSITION_RANGE:
            return {
                "success": False,
                "message": "coarse_position/fine_position 必须在 0-10 范围内（0 表示跳过该罐位）",
            }
        coarse_params, fine_params = self._load_powder_params_from_json(params_json, recipe_name)
        try:
            self._write_plc_variable(NODE_COARSE_POSITION, int(coarse_position))
            self._write_plc_variable(NODE_FINE_POSITION, int(fine_position))
            self._write_plc_variable(NODE_TARGET_WEIGHT, float(target_weight))
            self._write_powder_params("粗注粉", coarse_params, NODE_COARSE_SHAKE_MAX_SPEED)
            self._write_powder_params("精注粉", fine_params, NODE_FINE_SHAKE_MAX_SPEED)
        except Exception:
            self._reset_unilab_written_params()
            raise
        recorder: S07BalanceHistoryRecorder | None = None
        history_error: str | None = None
        if self.enable_balance_history:
            try:
                recorder = S07BalanceHistoryRecorder(
                    output_dir=self.balance_history_dir,
                    target_weight=float(target_weight),
                    recipe_name=recipe_name,
                    coarse_position=coarse_position,
                    fine_position=fine_position,
                )
            except Exception as exc:
                # 调试记录失败不能阻断真实设备动作。
                history_error = str(exc)
        try:
            result = self._run_dose_process_with_balance(recorder=recorder)
        except Exception:
            if recorder is not None:
                try:
                    recorder.finish(status="error")
                except Exception:
                    pass
            raise
        if recorder is not None:
            try:
                result.update(
                    recorder.finish(
                        status="success" if result.get("success") else "failed",
                        final_weight=result.get("balance_reading"),
                    )
                )
            except Exception as exc:
                history_error = str(exc)
        if history_error:
            result["balance_history_error"] = history_error
        result["target_weight"] = target_weight
        result["recipe_name"] = recipe_name
        if result.get("success"):
            deviation = float(result["balance_reading"]) - float(target_weight)
            result["display_message"] = (
                f"S07 注粉完成：目标 {float(target_weight):.3f} g，"
                f"最终 {float(result['balance_reading']):.3f} g，"
                f"偏差 {deviation:+.3f} g"
            )
        return result
