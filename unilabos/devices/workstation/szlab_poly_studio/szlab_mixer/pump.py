from __future__ import annotations

from typing import Any, Literal

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice


@device(
    id="szlab_mixer_pump",
    display_name="SZLab 注射泵",
    category=["pump_and_valve"],
    description="SZLab VirtualMixer 注射泵设备",
)
class SzlabMixerPumpDevice:
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = None,
        timeout: float = 300.0,
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
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

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

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
    def _write_variable(self, name: str, value: Any) -> None:
        if getattr(self, "_plc_gateway", None) is not None:
            self._plc_gateway.write_variable(name, value)
            return
        self._client.write(name, value)

    @not_action
    def _reset_and_pulse(self, name: str) -> None:
        if getattr(self, "_plc_gateway", None) is not None:
            self._plc_gateway.write_variable(name, False)
            self._plc_gateway.write_variable(name, True)
            self._plc_gateway.write_variable(name, False)
            return
        self._client.reset_and_pulse(name)

    @not_action
    def _wait_new_cycle_done(self, name: str) -> bool:
        if self._client is not None and hasattr(self._client, "wait_new_cycle_done"):
            return self._client.wait_new_cycle_done(name, timeout=self.timeout)
        if self._client is not None:
            if bool(self._client.read(name)):
                if not self._client.wait_equal(name, False, timeout=self.timeout):
                    return False
            return self._client.wait_equal(name, True, timeout=self.timeout)
        return bool(self._read_variable(name, use_cache=False))

    @action(
        auto_prefix=True,
        description="执行注射泵转液",
        handles=[
            ActionInputHandle(
                key="pump_index",
                data_type="szlab_mixer_pump_index",
                label="注射泵编号",
                data_key="pump",
                data_source=DataSource.HANDLE,
                description="注射泵编号，范围 1-2",
            )
        ],
    )
    def transfer_liquid(
        self,
        pump: int = 1,
        volume: int = 1,
        direction: Literal["aspirate", "dispense"] = "aspirate",
    ) -> dict[str, Any]:
        if pump not in (1, 2):
            return {"success": False, "message": "注射泵编号必须为 1 或 2"}
        if volume <= 0:
            return {"success": False, "message": "转液体积必须大于 0"}
        if direction not in ("aspirate", "dispense"):
            return {"success": False, "message": "direction 必须为 aspirate 或 dispense"}
        if not bool(self._read_variable("S06允许加工", use_cache=False)):
            return {"success": False, "message": "S06 不允许加工"}

        self._status = "Running"
        self._write_variable("S06注射泵选择", int(pump))
        if direction == "aspirate":
            self._write_variable(f"S06注射泵{pump}抽液", int(volume))
        else:
            self._write_variable(f"S06注射泵{pump}排液", int(volume))
        self._reset_and_pulse("S06参数写入完成")

        if not self._wait_new_cycle_done("S06加工完成"):
            self._status = "Error"
            return {"success": False, "message": "S06 加工完成等待超时"}
        self._status = "Idle"
        return {
            "success": True,
            "message": f"注射泵 {pump} {direction} 完成",
            "data": {"pump": pump, "volume": volume, "direction": direction},
        }
