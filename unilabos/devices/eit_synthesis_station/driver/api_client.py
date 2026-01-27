import json
import logging
from typing import Any, Dict, List, Optional, Union

import requests

from ..config.setting import Settings, configure_logging
from .exceptions import (
    ApiError,
    AuthenticationError,
    AuthorizationExpiredError,
    ConfigError,
    RequestError,
    ResponseError,
    ValidationError,
)

JsonDict = Dict[str, Any]


class ApiClient:
    """
    功能:
        【底层驱动】封装 31 个基础 API 调用, 只做 HTTP 与错误处理, 不做业务流程.
    """

    def __init__(self, settings: Settings):
        if not settings.base_url:
            raise ConfigError("base_url 不能为空.")

        self._settings = settings
        self._session = requests.Session()
        self._logger = logging.getLogger(self.__class__.__name__)

        self._token_type: Optional[str] = None
        self._access_token: Optional[str] = None

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    def set_token(self, token_type: str, access_token: str) -> None:
        """
        功能:
            设置鉴权 token, 后续请求会自动带 Authorization 头.
        参数:
            token_type: 例如 "Bearer".
            access_token: token 字符串.
        返回:
            无.
        """
        self._token_type = token_type
        self._access_token = access_token

    def clear_token(self) -> None:
        """
        功能:
            清理本地缓存的 token, 用于主动退出或异常恢复
        参数:
            无
        返回:
            None
        """
        self._token_type = None
        self._access_token = None

    def _url(self, path: str) -> str:
        """
        功能:
            拼接基础地址与路径, 生成完整请求 URL
        参数:
            path: str, 目标接口路径, 允许带或不带前导斜杠
        返回:
            str, 完整的可访问 URL
        """
        base = self._settings.base_url.rstrip("/")
        p = path if path.startswith("/") else f"/{path}"
        return f"{base}{p}"

    def _mask_sensitive(self, obj: Any) -> Any:
        """
        功能:
            遍历对象并掩码敏感字段, 用于日志输出时避免泄露
        参数:
            obj: Any, 待处理的字典或列表对象
        返回:
            Any, 已对敏感字段替换为 *** 的同结构对象
        """
        if isinstance(obj, dict):
            masked = {}
            for k, v in obj.items():
                if str(k).lower() in ("password", "access_token", "authorization"):
                    masked[k] = "***"
                else:
                    masked[k] = self._mask_sensitive(v)
            return masked
        if isinstance(obj, list):
            return [self._mask_sensitive(x) for x in obj]
        return obj

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[JsonDict] = None,
        params: Optional[JsonDict] = None,
        timeout_s: Optional[float] = None,
    ) -> JsonDict:
        """
        功能:
            统一封装 HTTP 请求, 处理认证、超时与业务异常
        参数:
            method: str, HTTP 方法名称, 例如 GET 或 POST
            path: str, 接口路径, 允许带或不带前导斜杠
            json_body: Optional[JsonDict], 请求 JSON 体, 默认为 None
            params: Optional[JsonDict], 查询参数, 默认为 None
            timeout_s: Optional[float], 覆盖默认超时时间, 默认为 None 表示使用配置
        返回:
            JsonDict, 成功时的响应数据, 若响应为非字典则包装为 {"result": data}
        """
        url = self._url(path)
        timeout = timeout_s if timeout_s is not None else self._settings.timeout_s

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._access_token:
            # 附加授权头, 兼容缺省的 Bearer 类型
            token_type = self._token_type or "Bearer"
            headers["Authorization"] = f"{token_type} {self._access_token}"

        self._logger.debug(
            "HTTP request, method=%s, url=%s, params=%s, json=%s",
            method,
            url,
            self._mask_sensitive(params or {}),
            self._mask_sensitive(json_body or {}),
        )

        try:
            resp = self._session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
                verify=self._settings.verify_ssl,
            )
        except requests.Timeout as e:
            # 超时统一转为业务异常, 便于上层捕获
            raise RequestError(f"请求超时, url={url}") from e
        except requests.RequestException as e:
            # 其他请求异常统一包装
            raise RequestError(f"请求失败, url={url}, err={e}") from e

        if resp.status_code == 401:
            # 登录失效明确提示重新登录
            raise AuthorizationExpiredError("登录失效(401), 请重新登录.")   #失效登陆反馈
        if resp.status_code >= 400:
            # 其他 HTTP 错误统一抛出请求异常
            raise RequestError(
                f"HTTP错误, status={resp.status_code}, url={url}, body={resp.text}",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            # 返回体非 JSON 时抛出响应异常
            raise ResponseError(f"响应非JSON, url={url}, body={resp.text}") from e

        self._logger.debug("HTTP response, url=%s, json=%s", url, self._mask_sensitive(data))

        if isinstance(data, dict) and "code" in data:
            code = data.get("code")
            if code != 200:
                raise ApiError(code=code, msg=str(data.get("msg", "")), payload=data)

        return data if isinstance(data, dict) else {"result": data}

    # 1. 登录
    def login(self, username: str, password: str) -> JsonDict:
        """
        功能:
            登录获取 token.
        参数:
            username: 用户名.
            password: 密码.
        返回:
            Dict, 包含 access_token, token_type.
        """
        if not username or not password:
            raise ValidationError("username 与 password 不能为空.")
        data = self._request("POST", "/api/Token", json_body={"username": username, "password": password})
        if "access_token" not in data:
            raise AuthenticationError(f"登录失败, 响应缺少 access_token, resp={data}")
        return data

    # 2. 设备初始化
    def device_init(self, device_id: Optional[List[str]] = None) -> JsonDict:
        """
        功能:
            设备初始化(复位, 状态检测等), 支持全站或指定设备.
        参数:
            device_id: 设备 id 列表, None 表示全站初始化.
        返回:
            Dict.
        """
        body: JsonDict = {}
        if device_id:
            body["device_id"] = device_id
            print(body)
        return self._request("POST", "/api/DeviceInit", json_body=body)

    # 3. 获取资源
    def get_resource_info(self, filters: Optional[JsonDict] = None) -> JsonDict:
        """
        功能:
            获取资源详情, 参数为空则获取单站所有资源.
        参数:
            filters: 可选过滤字段, 不确定字段时可传 {}.
        返回:
            Dict.
        """
        return self._request("POST", "/api/GetResourceInfo", json_body=filters or {})

    # 4. 进料
    def in_tray(self, tray_qr_code: str, resource_list: List[JsonDict]) -> JsonDict:
        """
        功能:
            进料, 单盘进料.
        参数:
            tray_qr_code: 托盘二维码.
            resource_list: 资源列表.
        返回:
            Dict.
        """
        body = {"tray_QR_code": tray_qr_code, "resource_list": resource_list}
        return self._request("POST", "/api/InTray", json_body=body)

    # 5. 批量进料
    def batch_in_tray(self, resource_req_list: List[JsonDict]) -> JsonDict:
        """
        功能:
            批量进料, 支持多托盘.
        参数:
            resource_req_list: 每个托盘的进料请求列表.
        返回:
            Dict.
        """
        return self._request("POST", "/api/BatchInTray", json_body={"resource_req_list": resource_req_list})

    # 6. 下料
    def out_tray(self, layout_list: List[JsonDict]) -> JsonDict:
        """
        功能:
            出料, 目前只支持整盘出料.
        参数:
            layout_list: 资源列表, layout_code 支持 "All" 表示清空工站.
        返回:
            Dict.
        """
        return self._request("POST", "/api/OutTray", json_body={"layout_list": layout_list})
    
    # 7. 批量下料 
    def batch_out_tray(self, layout_list: List[JsonDict], move_type: str) -> JsonDict:
        """
        功能:
            批量下料, 对应 BatchOutTray.
        参数:
            layout_list: 资源列表.
            move_type: 下料方式, 例如 "main_out".
        返回:
            Dict.
        """
        body = {"layout_list": layout_list, "move_type": move_type}
        return self._request("POST", "/api/BatchOutTray", json_body=body)
    
    # 8. 获取化学品
    def get_chemical_list(
        self,
        *,
        query_key: Optional[str] = None,
        sort: str = "desc",
        offset: int = 0,
        limit: int = 20,
    ) -> JsonDict:
        """
        功能:
            获取化学品库列表, 对应 getChemicalList.
        参数:
            query_key: 查询字符串.
            sort: asc 或 desc.
            offset: 数据起点.
            limit: 数据限制.
        返回:
            Dict.
        """
        params: JsonDict = {"sort": sort, "offset": offset, "limit": limit}
        if query_key:
            params["query_key"] = query_key
        return self._request("GET", "/api/v1/knowledge/getChemicalList", params=params)

    # 9. 新增化学品
    def add_chemical(self, payload: JsonDict) -> JsonDict:
        """
        功能:
            新增单个化学品, 对应 addChemical.
        参数:
            payload: 化学品字段集合.
        返回:
            Dict.
        """
        return self._request("POST", "/api/v1/knowledge/addChemical", json_body=payload)

    # 10. 编辑化学品
    def update_chemical(self, payload: JsonDict) -> JsonDict:
        """
        功能:
            修改化学品信息, 对应 updateChemical.
        参数:
            payload: 至少包含 fid, 其余字段参考新增化学品.
        返回:
            Dict.
        """
        return self._request("POST", "/api/v1/knowledge/updateChemical", json_body=payload)

    # 11. 删除化学品
    def delete_chemical(self, chemical_id: int) -> JsonDict:
        """
        功能:
            根据 id 删除化学品, 对应 deleteChemical.
        参数:
            chemical_id: 化学品 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/v1/knowledge/deleteChemical", params={"chemical_id": chemical_id})
    
    # 12. 新建方法
    def create_method(self, payload: JsonDict) -> JsonDict:
        """
        功能:
            新建方法, 对应 task_templates POST.
        参数:
            payload: 方法字段集合, 例如 task_template_name, unit_save_json 等.
        返回:
            Dict.
        """
        return self._request("POST", "/api/v1/task_templates", json_body=payload)

    # 13. 编辑方法
    def update_method(self, task_template_id: int, payload: JsonDict) -> JsonDict:
        """
        功能:
            编辑某个方法, 对应 task_templates PUT.
        参数:
            task_template_id: 方法 id.
            payload: 方法字段集合, 参数同新建方法.
        返回:
            Dict.
        """
        return self._request("PUT", f"/api/v1/task_templates/{task_template_id}", json_body=payload)

    # 14. 删除方法
    def delete_method(self, task_template_id: int) -> JsonDict:
        """
        功能:
            删除某个方法, 对应 delete_template.
        参数:
            task_template_id: 方法 id.
        返回:
            Dict.
        """
        return self._request(
            "POST",
            "/api/v1/task_templates/delete_template",
            json_body={"task_template_id": task_template_id},
        )

    # 15. 获取单个方法详情
    def get_method_detail(self, task_template_id: int) -> JsonDict:
        """
        功能:
            获取单个方法详情, 对应 task_templates GET.
        参数:
            task_template_id: 方法 id.
        返回:
            Dict.
        """
        return self._request("GET", f"/api/v1/task_templates/{task_template_id}")

    # 16. 获取方法列表
    def get_method_list(self, *, limit: int = 20, offset: int = 0, sort: str = "desc") -> JsonDict:
        """
        功能:
            获取方法列表.
        参数:
            limit: 每页数量.
            offset: 偏移量.
            sort: asc 或 desc.
        返回:
            Dict.
        """
        return self._request("GET", "/api/v1/task_templates", params={"limit": limit, "offset": offset, "sort": sort})

    # 17. 创建任务
    def add_task(self, payload: JsonDict) -> JsonDict:
        """
        功能:
            创建或更新任务, 对应 AddTask.
        参数:
            payload: AddTask 完整请求体.
        返回:
            Dict.
        """
        return self._request("POST", "/api/AddTask", json_body=payload)

    # 18. 启动任务
    def start_task(self, task_id: int) -> JsonDict:
        """
        功能:
            启动任务. 请求超时时间改为30s.
        参数:
            task_id: 任务 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/StartTask", json_body={"task_id": task_id}, timeout_s=30)

    # 19. 暂停任务
    def stop_task(self, task_id: int) -> JsonDict:
        """
        功能:
            暂停任务, 对应 StopTask.
        参数:
            task_id: 任务 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/StopTask", json_body={"task_id": task_id})

    # 20. 取消任务
    def cancel_task(self, task_id: int) -> JsonDict:
        """
        功能:
            取消任务, 对应 CancelTask.
        参数:
            task_id: 任务 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/CancelTask", json_body={"task_id": task_id})

    # 21. 删除任务
    def delete_task(self, task_id: int) -> JsonDict:
        """
        功能:
            删除任务, 对应 DeleteTask.
        参数:
            task_id: 任务 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/DeleteTask", json_body={"task_id": task_id})

    # 22. 获取所有任务列表
    def get_task_list(self, payload: Optional[JsonDict] = None) -> JsonDict:
        """
        功能:
            获取任务列表, 对应 GetTaskList.
        参数:
            payload: 查询参数, 不确定时可传 {}.
        返回:
            Dict.
        """
        return self._request("POST", "/api/GetTaskList", json_body=payload or {})

    # 23. 获取单个任务详情
    def get_task_info(self, task_id: int) -> JsonDict:
        """
        功能:
            获取任务详情, 对应 GetTaskInfo.
        参数:
            task_id: 任务 id.
        返回:
            Dict.
        """
        return self._request("POST", "/api/GetTaskInfo", json_body={"task_id": task_id})

    # 24. 获取消息通知
    def notice(self, types: Optional[List[int]] = None) -> JsonDict:
        """
        功能:
            获取消息通知, 对应 Notice.
        参数:
            types: 可选, 例如 [1, 2] 表示故障与告警, None 表示全部.
        返回:
            Dict.
        """
        body: JsonDict = {}
        if types is not None:
            body["type"] = types
        return self._request("POST", "/api/Notice", json_body=body)

    # 25. 故障恢复
    def fault_recovery(
        self,
        *,
        ids: Optional[List[int]] = None,
        recovery_type: int = 0,
        resume_task: int = 1,
    ) -> JsonDict:
        """
        功能:
            故障恢复, 取消告警并可恢复任务运行.
        参数:
            ids: 告警 id 列表, None 表示清除本机所有通知告警.
            recovery_type: 恢复类型, 0..5.
            resume_task: 0 仅清除不恢复, 1 清除并恢复, 默认 1.
        返回:
            Dict.
        """
        body: JsonDict = {"type": recovery_type, "resume_task": resume_task}
        if ids is not None:
            body["id"] = ids
        return self._request("POST", "/api/Notice", json_body=body)

    # 26. 工站设备状态
    def station_state(self) -> JsonDict:
        """
        功能:
            获取工站设备状态.
        参数:
            无.
        返回:
            Dict.
        """
        return self._request("GET", "/api/station/state")

    # 27. 获取设备模块列
    def list_device_info(self) -> JsonDict:
        """
        功能:
            获取工站设备模块列表.
        参数:
            无.
        返回:
            Dict.
        """
        return self._request("POST", "/api/ListDeviceInfo", json_body={})
    
    # 28. 获取所有设备信息
    def get_all_device_info(self) -> JsonDict:
        """
        功能:
            获取所有设备信息。
        参数:
            无。
        返回:
            Dict.
        """
        return self._request("POST", "/api/getAllDeviceInfo", json_body={})
    
    # 29.清空站内所有资源 (ClearTrayShelf)
    def clear_tray_shelf(self) -> JsonDict:
        """
        功能:
            清空站内所有资源（需手动移除所有资源）。
        参数:
            无。
        返回:
            Dict.
        """
        return self._request("POST", "/api/ClearTrayShelf", json_body={})
    
    # 30.打开/关闭过渡舱外门 (OpenCloseDoor)
    def open_close_door(self, station: str, op: str, door_num: int) -> JsonDict:
        """
        功能:
            打开/关闭过渡舱外门。
        参数:
            station: 站点编码, 例如 "FSY".
            op: "open" 或 "close". 注意关门会自动置换气体！
            door_num: 门编号, 例如 0.
        返回:
            Dict.
        """
        body = {"op": op, "station": station, "door_num": door_num}
        return self._request(
            "POST",
            "/api/OpenCloseDoor",
            json_body=body,
            params={"station": station},
        )
    
    # 31. 批量查询设备运行状态
    def batch_list_device_runtimes(self, device_code_list: List[str]) -> JsonDict:
        """
        功能:
            批量查询设备运行状态, 对应 BatchListDeviceRuntimes
        参数:
            device_code_list: List[str], 设备代码列表, 例如 ["352", "304", "306"]
        返回:
            Dict[str, Any], 接口响应
        """
        if not device_code_list:
            raise ValidationError("device_code_list 不能为空")
        body = {"device_code_list": device_code_list}
        return self._request("POST", "/api/BatchListDeviceRuntimes", json_body=body)
    
    # 32. 查询资源设置参数
    def get_set_up(self) -> JsonDict:
        """
        功能:
            请求后台通用设置参数, 对应 GetSetUp 接口.
        参数:
            无
        返回:
            Dict, 接口原始响应.
        """
        return self._request("POST", "/api/GetSetUp", json_body={})
    
    # 33. 获取任务进度信息
    def get_task_op_info(self, task_id: int) -> JsonDict:
        """
        功能:
            获取任务的操作进度信息, 对应 GetTaskOpInfo
        参数:
            task_id: int, 任务id
        返回:
            Dict, 接口响应
        """
        return self._request("POST", "/api/GetTaskOpInfo", json_body={"task_id": task_id})
    
    # 34. 进行物料核算
    def check_task_resource(self, task_id: int) -> JsonDict:
        """
        功能:
            检查任务资源, 对应 CheckTaskResource. code=1200 时仅警告不抛异常
        参数:
            task_id: int, 任务id
        返回:
            Dict, 接口响应, 包含 code, msg 等字段. code=1200 时返回完整响应体
        """
        try:
            return self._request("POST", "/api/CheckTaskResource", json_body={"task_id": task_id})
        except ApiError as e:
            # code=1200 表示任务资源不足, 仅警告不抛异常, 返回完整响应
            if e.code == 1200:
                # 返回完整的响应体, 包含 code, msg, prompt_msg 等字段
                return e.payload if e.payload else {"code": e.code, "msg": e.msg}
            else:
                # 其他错误码继续抛出异常
                raise

    # 35. 单独控制W1货架
    def single_control_w1_shelf(self, station: str, action: str, op: str, num: int) -> JsonDict:
        """
        功能:
            单独控制W1货架的推出或复位操作
        参数:
            station: str, 站点编码, 例如 "FSY"
            action: str, 动作类型, "home" 表示复位, "outside" 表示推出
            op: str, 操作类型, 与action保持一致
            num: int, 货架编号, 1表示W-1-1和W-1-2, 3表示W-1-3和W-1-4, 5表示W-1-5和W-1-6, 7表示W-1-7和W-1-8
        返回:
            Dict, 接口响应
        """
        if action not in ("home", "outside"):
            raise ValidationError("action 必须是 'home' 或 'outside'")
        if num not in (1, 3, 5, 7):
            raise ValidationError("num 必须是 1, 3, 5 或 7")

        body = {"action": action, "op": op, "num": num}
        return self._request(
            "POST",
            "/api/SingleControlW1Shelf",
            json_body=body,
            params={"station": station},
        )

if __name__ == "__main__":

    settings = Settings.from_env()
    client  = ApiClient(settings)
    resp = client.login('admin','admin')
    token_type = str(resp.get("token_type", "Bearer"))
    access_token = str(resp.get("access_token", ""))
    client.set_token(token_type, access_token)
    resp  = client.check_task_resource(665)
    print(resp)