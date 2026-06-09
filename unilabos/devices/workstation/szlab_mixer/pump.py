from __future__ import annotations

from typing import Any, Literal

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config

from .opcua_client import SzlabMixerOpcUaClient


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
        timeout: float = 300.0,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self._client = SzlabMixerOpcUaClient(url=url, username=username, password=password)
        self._status = "Idle"

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @not_action
    def disconnect(self) -> None:
        self._client.disconnect()

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        return self._client.get_variables(variable_names, use_cache=use_cache)

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return self._client.get_opc_variable_metadata(variable_name)

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
        if not bool(self._client.read("S06允许加工")):
            return {"success": False, "message": "S06 不允许加工"}

        self._status = "Running"
        self._client.write("S06注射泵选择", int(pump))
        if direction == "aspirate":
            self._client.write(f"S06注射泵{pump}抽液", int(volume))
        else:
            self._client.write(f"S06注射泵{pump}排液", int(volume))
        self._client.pulse("S06参数写入完成")

        if not self._client.wait_new_cycle_done("S06加工完成", timeout=self.timeout):
            self._status = "Error"
            return {"success": False, "message": "S06 加工完成等待超时"}
        self._status = "Idle"
        return {
            "success": True,
            "message": f"注射泵 {pump} {direction} 完成",
            "data": {"pump": pump, "volume": volume, "direction": direction},
        }
