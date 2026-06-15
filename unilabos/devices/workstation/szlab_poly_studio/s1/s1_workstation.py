from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.registry.decorators import action, device, not_action, topic_config


class UrllibS1Transport:
    """S1 HTTP 传输层，使用标准库便于在边缘环境运行。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        body = None if json is None else __import__("json").dumps(json).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", "ignore")
                return self._decode_response(payload, response.status)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "ignore")
            decoded = self._decode_response(payload, exc.code)
            decoded.setdefault("code", str(exc.code))
            decoded.setdefault("desc", exc.reason)
            return decoded

    @staticmethod
    def _decode_response(payload: str, status_code: int) -> Dict[str, Any]:
        if not payload:
            return {"code": str(status_code), "desc": "", "data": None}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return {"code": str(status_code), "desc": payload, "data": payload}
        if isinstance(decoded, dict):
            return decoded
        return {"code": str(status_code), "desc": "Succeed!", "data": decoded}


@device(
    id="s1_workstation",
    display_name="S1 连续流工作站",
    category=["workstation"],
    description="通过 HTTP API 对接的 S1 连续流工作站",
)
class S1Workstation(WorkstationBase):
    """S1 连续流工作站 UniLabOS 适配器。"""

    def __init__(
        self,
        device_id: Optional[str] = None,
        host: str = "192.168.43.141",
        port: int = 8055,
        scheme: str = "http",
        api_prefix: str = "/api/v1",
        username: str = "",
        password: str = "",
        timeout: float = 10.0,
        test_mode: bool = True,
        allow_hardware_action: bool = False,
        token: str = "",
        transport: Optional[Any] = None,
        deck: Optional[Any] = None,
        **kwargs: Any,
    ):
        """
        初始化 S1 工作站。

        Args:
            device_id[设备ID]: 设备实例 ID。
            host[设备IP]: S1 前端/后端服务 IP。
            port[端口]: S1 服务端口。
            scheme[协议]: HTTP 协议，通常为 http。
            api_prefix[API前缀]: S1 API 前缀，默认 /api/v1。
            username[用户名]: 登录用户名。
            password[密码]: 登录密码。
            timeout[超时时间(s)]: HTTP 请求超时时间。
            test_mode[测试模式]: 测试模式下拒绝真实硬件动作。
            allow_hardware_action[允许硬件动作]: 为 True 且 test_mode 为 False 时才执行危险动作。
            token[访问令牌]: 可选的初始 token。
        """
        super().__init__(deck=deck, **kwargs)
        self.device_id = device_id or "s1_workstation"
        self.host = host
        self.port = int(port)
        self.scheme = scheme
        self.api_prefix = self._normalize_api_prefix(api_prefix)
        self.base_url = f"{self.scheme}://{self.host}:{self.port}{self.api_prefix}"
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.test_mode = bool(test_mode)
        self.allow_hardware_action = bool(allow_hardware_action)
        self.token = token
        self.transport = transport or UrllibS1Transport()
        self._status = "Idle"
        self._last_response: Dict[str, Any] = {}
        self._material_info: Dict[str, Any] = {}
        self._workflow_sequence: List[Dict[str, Any]] = []

    @staticmethod
    def _normalize_api_prefix(api_prefix: str) -> str:
        normalized = "/" + api_prefix.strip("/")
        return "" if normalized == "/" else normalized

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @property
    @topic_config()
    def workflow_sequence(self) -> str:
        return json.dumps(self._workflow_sequence, ensure_ascii=False)

    @property
    @topic_config()
    def material_info(self) -> str:
        return json.dumps(self._material_info, ensure_ascii=False)

    @not_action
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @not_action
    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._headers() if auth else {}
        if data is not None:
            headers = {**headers, "Content-Type": "application/json"}
        response = self.transport.request(
            method.upper(),
            url,
            params=params,
            json=data,
            headers=headers,
            timeout=self.timeout,
        )
        self._last_response = response
        return response

    @not_action
    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, auth: bool = True) -> Dict[str, Any]:
        return self._request("GET", endpoint, params=params, data=None, auth=auth)

    @not_action
    def _post(self, endpoint: str, data: Optional[Any] = None, auth: bool = True) -> Dict[str, Any]:
        return self._request("POST", endpoint, data=data, auth=auth)

    @staticmethod
    def _is_success(response: Dict[str, Any]) -> bool:
        return str(response.get("code", "")) in {"0", "200"}

    @staticmethod
    def _message(response: Dict[str, Any]) -> str:
        return str(response.get("desc") or response.get("message") or response.get("msg") or "")

    @not_action
    def _wrap_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": self._is_success(response),
            "message": self._message(response),
            "data": response.get("data"),
            "raw": response,
        }

    @not_action
    def _guard_hardware_action(self, message: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if self.test_mode or not self.allow_hardware_action:
            return {"success": False, "message": message, "data": data or {}}
        return None

    @action(description="登录 S1 并缓存访问令牌")
    def login(self, username: str = "", password: str = "") -> Dict[str, Any]:
        """
        Args:
            username[用户名]: S1 用户名，留空时使用初始化配置。
            password[密码]: S1 密码，留空时使用初始化配置。
        """
        payload = {"username": username or self.username, "password": password or self.password}
        response = self._post("/auth/login", payload, auth=False)
        result = self._wrap_response(response)
        token_data = response.get("data")
        if result["success"]:
            if isinstance(token_data, str):
                self.token = token_data
            elif isinstance(token_data, dict) and token_data.get("token"):
                self.token = str(token_data["token"])
            self._status = "Ready"
        return result

    @action(description="同步 S1 物料库")
    def sync_materials(self, nameKey: str = "", pageNum: int = 1, pageSize: int = 10) -> Dict[str, Any]:
        """
        Args:
            nameKey[物料关键字]: 物料名称关键字。
            pageNum[页码]: 页码。
            pageSize[每页数量]: 每页数量。
        """
        response = self._get("/material/search", {"nameKey": nameKey, "pageNum": pageNum, "pageSize": pageSize})
        result = self._wrap_response(response)
        if result["success"]:
            self._material_info = {"materials": response.get("data")}
        return result

    @action(description="创建 S1 物料")
    def create_material(
        self,
        name: str,
        casNumber: str = "",
        chemicalFormula: str = "",
        appearance: str = "",
        density: float = 1.0,
        molarity: float = 1.0,
    ) -> Dict[str, Any]:
        payload = {
            "name": name,
            "casNumber": casNumber,
            "chemicalFormula": chemicalFormula,
            "appearance": appearance,
            "density": density,
            "molarity": molarity,
        }
        return self._wrap_response(self._post("/material/add", payload))

    @action(description="设置 S1 设备物料")
    def set_materials(self, materials_json: str = "[]") -> Dict[str, Any]:
        """
        Args:
            materials_json[物料配置JSON]: 物料配置 JSON 字符串。
        """
        materials = json.loads(materials_json)
        response = self._post("/preparation/setInfo", materials)
        result = self._wrap_response(response)
        if result["success"]:
            self._material_info = {"assigned_materials": materials}
        return result

    @action(description="创建 S1 实验订单")
    def create_order(self, order_json: str = "{}") -> Dict[str, Any]:
        """
        Args:
            order_json[实验订单JSON]: S1 实验订单 JSON 字符串。
        """
        order = json.loads(order_json)
        response = self._post("/experiment/add", order)
        result = self._wrap_response(response)
        if result["success"]:
            self._workflow_sequence.append({"action": "create_order", "data": response.get("data")})
        return result

    @action(description="启动 S1 实验")
    def scheduler_start(self, experiment_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Args:
            experiment_ids[实验ID列表]: 要启动的实验 ID 列表。
        """
        experiment_ids = experiment_ids or []
        blocked = self._guard_hardware_action(
            "Mock mode: experiment start rejected to avoid operating real hardware.",
            {"requestedExperimentIds": experiment_ids},
        )
        if blocked:
            return blocked
        self._status = "Running"
        return self._wrap_response(self._post("/experiment/start", experiment_ids))

    @action(description="停止 S1 实验通道")
    def scheduler_stop(self, channel: int = 1) -> Dict[str, Any]:
        """
        Args:
            channel[通道号]: 要停止的通道号。
        """
        blocked = self._guard_hardware_action(
            "Mock mode: experiment stop rejected to avoid operating real hardware.",
            {"channel": channel},
        )
        if blocked:
            return blocked
        response = self._get("/manualControl/stop", {"channel": channel})
        result = self._wrap_response(response)
        if result["success"]:
            self._status = "Stopped"
        return result

    @action(description="查询 S1 实验阶段状态")
    def query_experiment_status(self, experiment_id: int) -> Dict[str, Any]:
        """
        Args:
            experiment_id[实验ID]: 实验 ID。
        """
        return self._wrap_response(self._get("/experiment/getDEPhase", {"id": experiment_id}))

    @action(description="查询 S1 实时状态")
    def query_realtime_status(self, channel: int = 0) -> Dict[str, Any]:
        """
        Args:
            channel[通道号]: 通道号，0 表示查询全部通道。
        """
        if channel == 0:
            return self._wrap_response(self._get("/experimentInformation/Allchannel"))
        return self._wrap_response(self._get("/experimentInformation/channel", {"channel": channel}))

    @action(description="查询 S1 实验订单列表")
    def list_orders(
        self,
        status: str = "ready",
        pageNum: int = 1,
        pageSize: int = 10,
        name: str = "",
        startTime: str = "",
        endTime: str = "",
    ) -> Dict[str, Any]:
        endpoint_map = {
            "ready": "/experiment/listReady",
            "queue": "/experiment/listQueue",
            "done": "/experiment/listDone",
        }
        endpoint = endpoint_map.get(status, "/experiment/listReady")
        params = {"pageNum": pageNum, "pageSize": pageSize}
        if status == "done":
            params.update({"name": name, "startTime": startTime, "endTime": endTime})
        return self._wrap_response(self._get(endpoint, params))

    @action(description="查询 S1 历史日志")
    def query_logs(self, query_json: str = "{}") -> Dict[str, Any]:
        query = json.loads(query_json)
        return self._wrap_response(self._post("/logHistory/find", query))

    @action(description="查询 S1 当前准备信息")
    def query_current_info(self) -> Dict[str, Any]:
        return self._wrap_response(self._get("/preparation/getCurrentInfo"))

    @action(description="查询 S1 清洗状态")
    def query_wash_status(self) -> Dict[str, Any]:
        return self._wrap_response(self._get("/wash/washStatus"))

    @action(description="查询 S1 补液状态")
    def query_fill_status(self) -> Dict[str, Any]:
        return self._wrap_response(self._get("/fill/status"))

    @action(description="启动 S1 清洗")
    def start_wash(self, wash_json: str = "{}") -> Dict[str, Any]:
        blocked = self._guard_hardware_action(
            "Mock mode: wash command rejected to avoid operating real hardware.",
            {"status": "REJECTED"},
        )
        if blocked:
            return blocked
        return self._wrap_response(self._post("/wash/oneClickWash", json.loads(wash_json)))

    @action(description="启动 S1 补液")
    def start_fill(self, fill_json: str = "{}") -> Dict[str, Any]:
        blocked = self._guard_hardware_action(
            "Mock mode: fill command rejected to avoid operating real hardware.",
            {"status": "REJECTED"},
        )
        if blocked:
            return blocked
        return self._wrap_response(self._post("/fill/start", json.loads(fill_json)))
