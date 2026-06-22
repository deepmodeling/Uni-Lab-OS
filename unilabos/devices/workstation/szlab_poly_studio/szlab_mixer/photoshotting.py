from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice


S05_READY_SIGNAL = "S05准备信号"
S05_MATERIAL_SENSOR = "传感器状态_上位机[3].NO[0]"
S05_RESULT = "S05拍照结果"
S05_DONE = "S05加工完成"

PHOTO_RESULT_LABELS = {
    1: "OK",
    2: "NG",
}


@device(
    id="szlab_mixer_photoshotting",
    display_name="SZLab 拍照检测",
    category=["camera"],
    description="SZLab VirtualMixer S05 拍照检测工位设备",
)
class SzlabMixerPhotoShottingDevice:
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = None,
        timeout: float = 300.0,
        save_dir: str = "unilabos_data/szlab_mixer/photos",
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
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
        self._client = None if use_plc_gateway else SZLabPolyPLCDevice(**client_kwargs)
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
    def _wait_new_cycle_done(self, name: str) -> bool:
        if getattr(self, "_plc_gateway", None) is not None and hasattr(self._plc_gateway, "wait_new_cycle_done"):
            return self._plc_gateway.wait_new_cycle_done(name, timeout=self.timeout)
        if self._client is not None:
            return self._client.wait_new_cycle_done(name, timeout=self.timeout)
        return bool(self._read_variable(name, use_cache=False))

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
            return {
                "success": True,
                "status": "provided",
                "result": inspection_result,
                "photo_path": photo_path,
                **normalized,
            }
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
            photo_path[照片路径]: 外部相机或算法保存照片后的路径；为空时自动生成预留路径。
            inspection_result[算法结果]: 算法尚未接入时可手动传入的结果记录。
            require_material[要求有料]: 是否检查拍照有料检测传感器，联调阶段默认不强制检查。
        """
        if not bool(self._read_variable(S05_READY_SIGNAL, use_cache=False)):
            return {"success": False, "message": "S05 拍照工位未准备就绪"}
        if require_material and not bool(self._read_variable(S05_MATERIAL_SENSOR, use_cache=False)):
            return {"success": False, "message": "S05 拍照工位未检测到物料"}

        self._status = "Running"
        photo_path = photo_path or self._build_photo_path(sample_id)
        capture_result = self._capture_photo(photo_path=photo_path, sample_id=sample_id)
        if not capture_result.get("success", False):
            self._status = "Error"
            return {
                "success": False,
                "message": capture_result.get("message", "拍照失败"),
                "data": capture_result,
            }

        algorithm_result = self._run_inspection(photo_path=photo_path, inspection_result=inspection_result)
        if not algorithm_result.get("success", False):
            self._status = "Error"
            return {
                "success": False,
                "message": algorithm_result.get("message", "算法检测失败"),
                "data": {
                    "photo_path": photo_path,
                    "capture": capture_result,
                    "inspection_result": algorithm_result,
                },
            }

        if not self._wait_new_cycle_done(S05_DONE):
            self._status = "Error"
            return {
                "success": False,
                "message": "S05 拍照完成等待超时",
                "data": {
                    "photo_path": photo_path,
                    "capture": capture_result,
                    "inspection_result": algorithm_result,
                },
            }

        result_code = self._read_variable(S05_RESULT, use_cache=False)
        result_label = self._result_label(result_code)
        self._status = "Idle"
        self._last_photo_path = photo_path
        self._last_result = result_label
        return {
            "success": True,
            "message": f"S05 拍照检测完成，结果 {result_label}",
            "data": {
                "sample_id": sample_id,
                "photo_path": photo_path,
                "result_code": result_code,
                "result": result_label,
                "capture": capture_result,
                "inspection_result": algorithm_result,
            },
        }

    @action(auto_prefix=True, description="执行顶面和侧面双视角拍照检测")
    def take_dual_view_photos(
        self,
        sample_id: str = "",
        top_photo_path: str = "",
        side_photo_path: str = "",
        algorithm_url: str = "",
        algorithm_timeout: float = 10.0,
        require_material: bool = False,
    ) -> dict[str, Any]:
        if not bool(self._read_variable(S05_READY_SIGNAL, use_cache=False)):
            return {"success": False, "message": "S05 拍照工位未准备就绪"}
        if require_material and not bool(self._read_variable(S05_MATERIAL_SENSOR, use_cache=False)):
            return {"success": False, "message": "S05 拍照工位未检测到物料"}

        self._status = "Running"
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
            extra_payload={
                "sample_id": sample_id,
                "top_photo_path": top_photo_path,
                "side_photo_path": side_photo_path,
            },
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

        if not self._wait_new_cycle_done(S05_DONE):
            self._status = "Error"
            return {
                "success": False,
                "message": "S05 拍照完成等待超时",
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

    @action(auto_prefix=True, description="读取拍照工位占用状态")
    def photo_station_occupied(self) -> dict[str, Any]:
        try:
            return {
                "success": True,
                "occupied": bool(self._read_variable(S05_MATERIAL_SENSOR, use_cache=False)),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}
