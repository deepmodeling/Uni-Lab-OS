from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import wait_variable_true

from .sensors import (
    PHOTO_RESULT_LABELS,
    S05_DONE,
    S05_RESULT,
)

DEFAULT_OPCUA_URL = os.environ.get(
    "UNILABOS_SZLAB_MIXER_OPCUA_URL",
    "opc.tcp://jdht1471820.bohrium.tech:50001",
)


@device(
    id="szlab_mixer_photoshotting",
    display_name="SZLab 拍照检测",
    category=["camera"],
    description="SZLab Poly Studio S05 拍照检测工位设备",
)
class SzlabMixerPhotoShottingDevice:
    def __init__(
        self,
        url: str = DEFAULT_OPCUA_URL,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = "szlab_plc_0623.csv",
        timeout: float = 300.0,
        save_dir: str = "unilabos_data/szlab_poly_studio/s05_photoshotting/photos",
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        opcua_node_id_map: dict[str, str] | None = None,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self.save_dir = save_dir
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        client_kwargs: dict[str, Any] = {
            "url": url,
            "username": username,
            "password": password,
            "timeout": timeout,
            "auto_connect": auto_connect,
        }
        if csv_path is not None:
            client_kwargs["csv_path"] = csv_path
        if opcua_node_id_map is not None:
            client_kwargs["opcua_node_id_map"] = opcua_node_id_map
        if use_plc_gateway:
            self._client = None
        else:
            from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

            self._client = SZLabPolyPLCDevice(**client_kwargs)
        self._status = "Idle"
        self._last_photo_path = ""
        self._last_result = "UNKNOWN"
        self._last_dual_view_result: dict[str, Any] = {}

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @property
    @topic_config()
    def last_photo_path(self) -> str:
        return self._last_photo_path

    @property
    @topic_config()
    def last_result(self) -> str:
        return self._last_result

    @not_action
    def disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect()

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        if getattr(self, "_plc_gateway", None) is not None:
            values = {}
            for name in variable_names:
                try:
                    values[name] = {"success": True, "value": self._read_variable(name, use_cache=use_cache)}
                except Exception as exc:
                    values[name] = {"success": False, "error": str(exc)}
            return values
        return self._client.get_variables(variable_names, use_cache=use_cache)

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        if self._client is None:
            return variable_name, None
        return self._client.get_opc_variable_metadata(variable_name)

    @not_action
    def _read_variable(self, name: str, use_cache: bool = False) -> Any:
        if getattr(self, "_plc_gateway", None) is not None:
            return self._plc_gateway.read_variable(name, use_cache=use_cache)
        return self._client.read(name)

    @not_action
    def _build_photo_path(self, sample_id: str = "", view: str = "photo") -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{sample_id}" if sample_id else ""
        return str(Path(self.save_dir) / f"s05_{view}{suffix}_{timestamp}.jpg")

    @not_action
    def _capture_photo(self, photo_path: str, sample_id: str = "") -> dict[str, Any]:
        return {
            "success": True,
            "photo_path": photo_path,
            "sample_id": sample_id,
            "captured": False,
            "message": "拍照接口未接入，已记录预留照片路径",
        }

    @not_action
    def _call_algorithm_service(
        self,
        algorithm_url: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            algorithm_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @not_action
    def _normalize_algorithm_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            dissolved = result.get("dissolved", result.get("success", "unknown"))
            if dissolved is True:
                status = True
            elif dissolved is False:
                status = False
            else:
                status = "unknown"
            return {
                "dissolved": status,
                "confidence": result.get("confidence"),
                "raw_result": result,
            }
        if isinstance(result, str) and result:
            lowered = result.lower()
            if lowered in {"ok", "success", "true", "dissolved"}:
                return {"dissolved": True, "confidence": None, "raw_result": result}
            if lowered in {"ng", "fail", "false", "undissolved"}:
                return {"dissolved": False, "confidence": None, "raw_result": result}
        return {"dissolved": "unknown", "confidence": None, "raw_result": result}

    @not_action
    def _run_inspection(
        self,
        photo_path: str,
        inspection_result: str = "",
        algorithm_url: str = "",
        algorithm_timeout: float = 10.0,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if inspection_result:
            normalized = self._normalize_algorithm_result(inspection_result)
            return {"success": True, "status": "provided", "result": inspection_result, "photo_path": photo_path, **normalized}
        if algorithm_url:
            payload = {"photo_path": photo_path}
            if extra_payload:
                payload.update(extra_payload)
            try:
                raw = self._call_algorithm_service(algorithm_url, payload, algorithm_timeout)
            except Exception as exc:
                return {
                    "success": False,
                    "status": "algorithm_error",
                    "message": str(exc),
                    "photo_path": photo_path,
                    "dissolved": "unknown",
                    "confidence": None,
                    "raw_result": "",
                }
            normalized = self._normalize_algorithm_result(raw)
            return {"success": True, "status": "algorithm", "photo_path": photo_path, **normalized}
        return {
            "success": True,
            "status": "not_configured",
            "dissolved": "unknown",
            "confidence": None,
            "raw_result": "",
            "photo_path": photo_path,
        }

    @not_action
    def _result_label(self, result_code: Any) -> str:
        try:
            return PHOTO_RESULT_LABELS.get(int(result_code), "UNKNOWN")
        except (TypeError, ValueError):
            return "UNKNOWN"

    @not_action
    def _fetch_photo_url(self, sample_id: str = "") -> str:
        del sample_id
        return ""

    @not_action
    def _wait_photo_done(self) -> bool:
        waiter = getattr(self._plc_gateway, "wait_variable_true", None) if self._plc_gateway is not None else None
        if callable(waiter):
            return waiter(S05_DONE, timeout=self.timeout, interval=1.0)
        reader = self._plc_gateway if self._plc_gateway is not None else self._client
        return wait_variable_true(reader, S05_DONE, timeout=self.timeout, interval=1.0)

    @not_action
    def _wait_photo_result_code(self) -> tuple[Any, str]:
        deadline = time.time() + self.timeout
        last_code: Any = 0
        last_label = "UNKNOWN"
        while True:
            last_code = self._read_variable(S05_RESULT, use_cache=False)
            last_label = self._result_label(last_code)
            if last_label != "UNKNOWN":
                return last_code, last_label
            remaining = deadline - time.time()
            if remaining <= 0:
                return last_code, last_label
            time.sleep(min(1.0, remaining))

    @action(auto_prefix=True, description="执行烧杯姿势拍照检测")
    def take_photo(
        self,
        sample_id: str = "",
        photo_path: str = "",
        inspection_result: str = "",
        require_material: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            sample_id[样品ID]: 用于生成照片文件名和结果记录的样品标识。
            photo_path[照片路径]: 保留参数；相机照片链接接口接入后由设备侧获取。
            inspection_result[算法结果]: 保留参数；S05 当前按 PLC 拍照结果判断。
            require_material[要求有料]: 兼容旧工作流参数；S05 最新变量表不再提供物料检测，当前不使用。
        """
        del inspection_result, require_material
        self._status = "Running"
        if not self._wait_photo_done():
            self._status = "Error"
            return {"success": False, "message": "S05 拍照完成等待超时"}

        result_code, result_label = self._wait_photo_result_code()
        photo_url = self._fetch_photo_url(sample_id) if result_label == "OK" else ""
        self._status = "Idle"
        self._last_photo_path = photo_path
        self._last_result = result_label
        data = {
            "sample_id": sample_id,
            "photo_path": photo_path,
            "photo_url": photo_url,
            "result_code": result_code,
            "result": result_label,
        }
        if result_label == "UNKNOWN":
            self._status = "Error"
            return {
                "success": False,
                "message": f"S05 拍照结果等待超时（last_value={result_code}）",
                "data": data,
            }
        if result_label != "OK":
            self._status = "Error"
            return {
                "success": False,
                "message": f"S05 拍照检测 {result_label}",
                "data": data,
            }
        return {
            "success": True,
            "message": f"S05 拍照检测完成，结果 {result_label}",
            "data": data,
        }

    @not_action
    def take_dual_view_photos(
        self,
        sample_id: str = "",
        top_photo_path: str = "",
        side_photo_path: str = "",
        algorithm_url: str = "",
        algorithm_timeout: float = 10.0,
        require_material: bool = False,
    ) -> dict[str, Any]:
        self._status = "Running"
        if not self._wait_photo_done():
            self._status = "Error"
            return {"success": False, "message": "S05 拍照完成等待超时"}

        top_photo_path = top_photo_path or self._build_photo_path(sample_id, view="top")
        side_photo_path = side_photo_path or self._build_photo_path(sample_id, view="side")
        top_capture = self._capture_photo(photo_path=top_photo_path, sample_id=sample_id)
        side_capture = self._capture_photo(photo_path=side_photo_path, sample_id=sample_id)
        if not top_capture.get("success", False) or not side_capture.get("success", False):
            self._status = "Error"
            return {
                "success": False,
                "message": "双视角拍照失败",
                "data": {
                    "sample_id": sample_id,
                    "top_photo_path": top_photo_path,
                    "side_photo_path": side_photo_path,
                    "top_capture": top_capture,
                    "side_capture": side_capture,
                },
            }

        algorithm_result = self._run_inspection(
            photo_path=top_photo_path,
            algorithm_url=algorithm_url,
            algorithm_timeout=algorithm_timeout,
            extra_payload={"sample_id": sample_id, "top_photo_path": top_photo_path,
                           "side_photo_path": side_photo_path},
        )
        if not algorithm_result.get("success", False):
            self._status = "Error"
            return {
                "success": False,
                "message": algorithm_result.get("message", "溶解性算法检测失败"),
                "data": {
                    "sample_id": sample_id,
                    "top_photo_path": top_photo_path,
                    "side_photo_path": side_photo_path,
                    "top_capture": top_capture,
                    "side_capture": side_capture,
                    "dissolution": algorithm_result,
                },
            }

        result_code = self._read_variable(S05_RESULT, use_cache=False)
        result_label = self._result_label(result_code)
        result = {
            "success": True,
            "message": f"S05 双视角拍照完成，溶解性 {algorithm_result['dissolved']}，姿态 {result_label}",
            "data": {
                "sample_id": sample_id,
                "top_photo_path": top_photo_path,
                "side_photo_path": side_photo_path,
                "top_capture": top_capture,
                "side_capture": side_capture,
                "dissolution": algorithm_result,
                "result_code": result_code,
                "result": result_label,
                "pose_ok": result_label == "OK",
            },
        }
        self._status = "Idle"
        self._last_dual_view_result = result["data"]
        self._last_photo_path = top_photo_path
        self._last_result = result_label
        return result


if __name__ == "__main__":
    import runpy

    runpy.run_module("unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.debug_photoshotting",
                     run_name="__main__")
