import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import OrderedDict
from datetime import datetime

from ..config.constants import TaskStatus,StationState,DeviceModuleStatus, ResourceCode, TraySpec,TRAY_CODE_DISPLAY_NAME
from ..config.setting import Settings, configure_logging
from ..driver.api_client import ApiClient
from ..driver.exceptions import AuthorizationExpiredError, ValidationError
from ..data.data_manager import DataManager
import re
import math

import uuid

JsonDict = Dict[str, Any]

class SynthesisStationController:
    """
    功能:
        【上层逻辑】面向用户的控制器，提供自动登录、401 自动重登、状态轮询等。
    """

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.from_env()
        self._client = ApiClient(self._settings)
        self._logger = logging.getLogger(self.__class__.__name__)

        # 初始化数据管理器
        if self._settings.enable_data_logging:
            self._data_manager = DataManager(self._settings.data_dir)
            self._logger.debug("数据存储已启用，数据目录: %s", self._settings.data_dir)
        else:
            self._data_manager = None
            self._logger.debug("数据存储已禁用")

    @property
    def client(self) -> ApiClient:
        return self._client
    
    # ---------- 自动登录 -------------
    def login(self) -> Tuple[str, str]:
        """
        功能:
            登录并缓存 token.
        参数:
            无.
        返回:
            (token_type, access_token).
        """
        resp = self._client.login(self._settings.username, self._settings.password)
        token_type = str(resp.get("token_type", "Bearer"))
        access_token = str(resp.get("access_token", ""))
        self._client.set_token(token_type, access_token)
        self._logger.debug("登录成功, token_type=%s", token_type)
        return token_type, access_token

    def ensure_login(self) -> None:
        """
        功能:
            确保已登录，未登录则自动登录。
        参数:
            无.
        返回:
            无.
        """
        if not self._client.access_token:
            self.login()

    def _call_with_relogin(self, func: Callable, *args, **kwargs):
        """
        功能:
            捕获 401，自动重登后重试一次。
        参数:
            func: 需要包装的函数.
            *args, **kwargs: 透传参数.
        返回:
            func 的返回值.
        """
        self.ensure_login()
        try:
            return func(*args, **kwargs)
        except AuthorizationExpiredError:
            self._logger.warning("检测到登录失效, 自动重新登录并重试一次")
            self.login()
            return func(*args, **kwargs)
        
    def _extract_station_state(self, state_info: JsonDict) -> Optional[int]:
        """
        功能:
            从站点状态响应中提取状态码。
        参数:
            state_info: station_state 响应。
        返回:
            Optional[int], 状态码。
        """
        for key in ("state", "status"):
            if key in state_info and isinstance(state_info.get(key), int):
                return int(state_info[key])
        for outer in ("result", "data"):
            obj = state_info.get(outer)
            if isinstance(obj, dict):
                for key in ("state", "status"):
                    if isinstance(obj.get(key), int):
                        return int(obj[key])
        return None

    def get_setup_params(self) -> JsonDict:
        """
        功能:
            调用 GetSetUp 接口, 提取工站配置中的 addition_timeout、accuracy、liquid_threshold、substance_shortage_nums。
        参数:
            无
        返回:
            Dict[str, Any], 包含四个配置值。
        """
        resp = self._call_with_relogin(self._client.get_set_up)
        # 兼容返回体可能的 result/data 包裹
        data_container = resp.get("result") or resp.get("data") or resp

        required_keys = [
            "addition_timeout",
            "accuracy",
            "liquid_threshold",
            "substance_shortage_nums",
        ]
        missing = [k for k in required_keys if k not in data_container]
        if missing:
            raise ValidationError(f"GetSetUp 返回缺少字段: {missing}, resp={resp}")

        result = {k: data_container[k] for k in required_keys}
        self._logger.debug(
            "获取工站配置成功 addition_timeout=%s accuracy=%s liquid_threshold=%s substance_shortage_nums=%s",
            result["addition_timeout"],
            result["accuracy"],
            result["liquid_threshold"],
            result["substance_shortage_nums"],
        )
        return result
    
    # ---------- 设备初始化 ----------
    def device_init(
        self,
        device_id: Optional[List[str]] = None,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 600.0,
    ) -> JsonDict:
        """
        功能:
            触发设备初始化, 然后轮询站点状态直到空闲。
            初始化完成后自动检测W-1-1到W-1-4上的125mL溶剂瓶, 如有则执行复位操作。
        参数:
            device_id: 设备id列表, 当前忽略, 始终传空JSON。
            poll_interval_s: 轮询间隔秒数。
            timeout_s: 超时秒数, 超时抛出 TimeoutError。
        返回:
            Dict, 初始化接口原始响应。
        """
        resp = self._call_with_relogin(self._client.device_init, {})  # 传空JSON
        self._logger.info("设备开始初始化")
        start_ts = time.time()

        while True:
            state = self.station_state()
            if state is not None:
                if state == int(StationState.IDLE):
                    self._logger.info("设备初始化完成")

                    # 初始化完成后检测W-1-1到W-1-4上的125mL溶剂瓶
                    self._check_and_reset_w1_shelves()

                    return resp
            else:
                self._logger.warning("无法解析站点状态, resp=%s", state)

            if time.time() - start_ts > timeout_s:
                raise TimeoutError(f"设备初始化等待空闲超时, last_state={state}")

            time.sleep(poll_interval_s)

    def _check_and_reset_w1_shelves(self) -> None:
        """
        功能:
            检测W-1-1到W-1-4上是否有125mL溶剂瓶, 如有则自动执行复位操作
        参数:
            无
        返回:
            无
        """
        try:
            self._logger.info("开始检测W-1-1到W-1-4上的125mL溶剂瓶")

            # 获取资源信息
            resources = self.get_resource_info()

            # 定义需要检测的位置和对应的控制位置
            check_positions = {
                "W-1-1": "W-1-1",  # W-1-1和W-1-2由W-1-1控制
                "W-1-2": "W-1-1",
                "W-1-3": "W-1-3",  # W-1-3和W-1-4由W-1-3控制
                "W-1-4": "W-1-3"
            }

            # 125mL试剂瓶托盘编码
            bottle_125ml_tray_code = int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML)

            # 记录需要复位的控制位置
            positions_to_reset = set()

            # 遍历资源信息检查是否有125mL溶剂瓶
            for resource in resources:
                layout_code = resource.get("layout_code", "")
                resource_type = resource.get("resource_type")

                # 检查是否是目标位置且是125mL试剂瓶托盘
                if layout_code in check_positions and resource_type == bottle_125ml_tray_code:
                    control_position = check_positions[layout_code]
                    positions_to_reset.add(control_position)
                    self._logger.info("检测到%s位置有125mL溶剂瓶托盘", layout_code)

            # 执行复位操作
            if positions_to_reset:
                self._logger.info("开始对检测到的货架执行复位操作")
                for position in sorted(positions_to_reset):
                    try:
                        self._logger.info("正在复位%s货架", position)
                        self.control_w1_shelf(position, "home")
                        self._logger.info("%s货架复位成功", position)
                    except Exception as e:
                        self._logger.error("复位%s货架失败: %s", position, str(e))
            else:
                self._logger.info("W-1-1到W-1-4上未检测到125mL溶剂瓶, 无需复位")

        except Exception as e:
            self._logger.error("检测和复位W1货架时发生错误: %s", str(e))

    # ---------- 获取设备状态 ------------
    def station_state(self) -> int:
        """
        功能:
            获取工站整体状态码.
        参数:
            无.
        返回:
            int, 工站状态码.
        """
        resp = self._call_with_relogin(self._client.station_state)
        # 兼容不同层级的状态字段
        for key in ("state", "status"):
            if isinstance(resp.get(key), int):
                return int(resp[key])
        for outer in ("result", "data"):
            obj = resp.get(outer)
            if isinstance(obj, dict):
                for key in ("state", "status"):
                    if isinstance(obj.get(key), int):
                        state_code = int(obj[key])

                        # 自动保存工站状态快照
                        if self._data_manager:
                            state_name = (
                                StationState(state_code).name
                                if state_code in StationState._value2member_map_
                                else "UNKNOWN"
                            )
                            self._data_manager.save_station_state({
                                "timestamp": datetime.now().isoformat(),
                                "state": state_name,
                                "state_code": state_code
                            })

                        return state_code

        raise ValidationError(f"无法解析站点状态码, resp={resp}")

    def get_glovebox_env(self) -> JsonDict:
        """
        功能:
            调用 batch_list_device_runtimes 获取手套箱环境数据，并提取时间、箱压、水值、氧值
        参数:
            无
        返回:
            Dict[str, Any], 包含 time、box_pressure、water_content、oxygen_content
        """
        # 固定查询设备代码 352（手套箱环境）
        resp = self._call_with_relogin(self._client.batch_list_device_runtimes, ["352"])
        data_container = resp.get("result") or resp.get("data") or resp

        if not isinstance(data_container, list) or len(data_container) == 0:
            raise ValidationError(f"响应缺少环境数据, resp={resp}")

        first_item = data_container[0]  # 多组相同数据，取第一组
        time_val = first_item.get("time")
        box_pressure = first_item.get("box_pressure")
        water_content = first_item.get("water_content")
        oxygen_content = first_item.get("oxygen_content")

        result = {
            "time": time_val,
            "box_pressure": box_pressure,
            "water_content": water_content,
            "oxygen_content": oxygen_content,
        }
        self._logger.debug("手套箱环境数据: %s", result)

        # 自动保存手套箱环境快照
        if self._data_manager:
            self._data_manager.save_glovebox_env({
                "timestamp": datetime.now().isoformat(),
                "pressure_pa": box_pressure,
                "humidity_ppm": water_content,
                "oxygen_ppm": oxygen_content
            })

        return result

    def list_device_info(self) -> JsonDict:
        """
        功能:
            获取站点设备模块列表。暂不使用,使用get_all_device_info.
        参数:
            无.
        返回:
            Dict, 接口响应.
        """
        resp = self._call_with_relogin(self._client.list_device_info)
        return resp

    def get_all_device_info(self) -> JsonDict:
        """
        功能:
            获取全部设备信息，仅返回 station_data 字段
        参数:
            无
        返回:
            Dict[str, Any], 包含 station_data 的字典
        """
        resp = self._call_with_relogin(self._client.get_all_device_info)

        station_data = resp.get("station_data") or resp.get("data") or resp.get("result")
        if not isinstance(station_data, list):
            raise ValidationError(f"响应缺少 station_data, resp={resp}")

        return {"station_data": station_data}
    
    def list_device_status(self) -> List[JsonDict]:
        """
        功能:
            基于 get_all_device_info 提取设备名称与状态(状态名替换数值).
        参数:
            无.
        返回:
            List[Dict], 包含 device_name、status(名称)、status_code(数值).
        """
        raw = self.get_all_device_info()
        station_list = raw.get("station_data") or raw.get("data") or raw.get("result") or []
        if not isinstance(station_list, list):
            raise ValidationError(f"station_data 格式异常, resp={raw}")

        device_status: List[JsonDict] = []
        for station_item in station_list:
            for dev in station_item.get("device_info", []):
                status_val = dev.get("status")
                status_name = (
                    DeviceModuleStatus(status_val).name
                    if isinstance(status_val, int) and status_val in DeviceModuleStatus._value2member_map_
                    else "UNKNOWN"
                )
                device_status.append(
                    {
                        "device_name": dev.get("device_name"),
                        "status": status_name,       # 如 AVAILABLE
                        "status_code": status_val,   # 数值保留以便排查
                    }
                )
        self._logger.debug("设备状态汇总完成, 数量=%s", len(device_status))

        # 自动保存设备状态快照
        if self._data_manager:
            self._data_manager.save_device_status({
                "timestamp": datetime.now().isoformat(),
                "devices": device_status
            })

        return device_status

    def wait_idle(
        self,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 600.0,
        stage: str = "",
    ) -> None:
        """
        功能:
            轮询站点状态直到空闲，超时则抛出 TimeoutError。
        参数:
            poll_interval_s: 轮询间隔秒数.
            timeout_s: 等待空闲的超时时长.
            stage: 日志阶段提示文本.
        返回:
            None.
        """
        start_ts = time.time()
        last_state: Optional[int] = None
        while True:
            state = self.station_state()
            if state != last_state:
                stage_text = stage if stage != "" else "等待空闲"
                self._logger.debug("%s最新状态: state=%s", stage_text, state)
                last_state = state
            if state == int(StationState.IDLE):
                return
            if time.time() - start_ts > timeout_s:
                stage_text = stage if stage != "" else "等待空闲"
                raise TimeoutError(f"{stage_text} 超时, last_state={state}")
            time.sleep(poll_interval_s)

    # ---------- 获取站内资源信息 ----------
    def get_resource_info(self) -> List[JsonDict]:
        """
        功能:
            调用资源详情接口, 将打平的 resource_list 聚合为按资源位置展示的表格行.
            聚合协议:
                1) 资源位置: 取 layout_code 或 source_layout_code 的冒号前缀, 例如 "N-1:-1" 与 "N-1:0" 聚合为 "N-1".
                2) slot == -1: 表示托盘本体, 读取 resource_type 作为托盘型号, 并补充 resource_type_name.
                3) slot != -1: 表示托盘坑位, 统计数量作为 count.
                4) substance_details: 仅收集 substance 非空的坑位, 使用列表返回; 若无物质, 返回空列表.
                   每个元素包含 slot, well, substance, value 字段.
        参数:
            无.
        返回:
            List[JsonDict], 每个元素结构如下:
                {
                    "layout_code": str, 资源位置,
                    "count": int, 坑位数量,
                    "substance_details": List[Dict], 物质详情列表, 无则 [],
                    "resource_type": int or None, 托盘型号编码,
                    "resource_type_name": str, 托盘型号中文名,
                }
        """
        response = self._call_with_relogin(self._client.get_resource_info, {})
        resource_list = self._extract_resource_list(response)
        rows = self._format_resource_rows(resource_list)

        self._logger.debug("获取资源信息成功")

        # 自动保存物料资源快照
        if self._data_manager:
            self._data_manager.save_resource_info({
                "timestamp": datetime.now().isoformat(),
                "resources": rows
            })

        return rows

    def _extract_resource_list(self, response: JsonDict) -> List[JsonDict]:
        """
        功能:
            从不同响应包裹层中提取 resource_list, 兼容直接返回或嵌套在 result/data 中的情况.
        参数:
            response: JsonDict, 接口原始响应.
        返回:
            List[JsonDict], 资源明细列表.
        """
        if "resource_list" in response:
            resource_list = response.get("resource_list")
            if resource_list is not None:
                return resource_list

        for outer_key in ("result", "data"):
            outer_obj = response.get(outer_key)
            if isinstance(outer_obj, dict) and "resource_list" in outer_obj:
                resource_list = outer_obj.get("resource_list")
                if resource_list is not None:
                    return resource_list

        raise ValidationError(f"响应缺少 resource_list 字段, resp={response}")

    def _format_resource_rows(self, resource_list: List[JsonDict]) -> List[JsonDict]:
        """
        功能:
            归整资源清单, 校验布局编码并按耗材类型汇总可用数量与展示明细.
        参数:
            resource_list: List[JsonDict], 接口返回的资源明细列表.
        返回:
            List[JsonDict], 每个元素包含布局编码、数量、托盘类型和物料明细.
        """
        def _pick_amount_value(media_item: JsonDict, field_names: List[str]) -> Tuple[Any, Optional[str]]:
            """
            功能:
                按字段优先级获取首个可用数值.
            参数:
                media_item: JsonDict, 当前资源明细.
                field_names: List[str], 按优先级排列的字段名.
            返回:
                Tuple[Any, Optional[str]], 包含首个非空值及字段名, 若不存在则为(None, None).
            """
            for field_name in field_names:
                if field_name in media_item:
                    candidate_value = media_item.get(field_name)
                    if candidate_value is not None and str(candidate_value).strip() != "":
                        return candidate_value, field_name
            return None, None

        def _format_amount_text(raw_value: Any, media_item: JsonDict, kind: str) -> str:
            """
            功能:
                依据数值类型和单位拼接展示文本, 数值为0时仅返回数值.
            参数:
                raw_value: Any, 原始数值.
                media_item: JsonDict, 当前资源明细.
                kind: str, 数值类型标记, 取值为weight或volume.
            返回:
                str, 拼接单位后的展示文本.
            """
            formatted_value = self._format_number(raw_value)
            numeric_value = 0.0
            try:
                numeric_value = float(raw_value)
            except Exception:
                numeric_value = 0.0

            if abs(numeric_value) < 1e-9:
                return formatted_value

            raw_unit = str(media_item.get("unit") or "").strip()
            unit_lower = raw_unit.lower()
            if kind == "weight":
                if unit_lower == "g" or unit_lower == "mg":
                    final_unit = raw_unit
                else:
                    final_unit = "mg"
            elif kind == "volume":
                if unit_lower == "ml" or unit_lower == "l" or unit_lower == "ul":
                    final_unit = raw_unit
                else:
                    final_unit = "mL"
            else:
                final_unit = raw_unit

            if final_unit == "":
                return formatted_value
            return f"{formatted_value}{final_unit}"

        grouped_by_layout: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for item in resource_list:
            layout_code = self._get_layout_code(item)
            if layout_code is None:
                continue
            if layout_code == "":
                continue
            if ":" not in layout_code:
                continue

            layout_prefix, slot_text = layout_code.split(":", 1)
            if layout_prefix == "":
                continue
            if layout_prefix[0].isalpha() is False:
                self._logger.debug("layout_code不合规 %s", layout_code)
                continue

            slot_index = self._safe_int(slot_text)
            if slot_index is None:
                continue

            if layout_prefix not in grouped_by_layout:
                grouped_by_layout[layout_prefix] = {"tray_item": None, "media_items": []}

            if slot_index == -1:
                grouped_by_layout[layout_prefix]["tray_item"] = item
            else:
                grouped_by_layout[layout_prefix]["media_items"].append(item)

        consumable_codes = {
            ResourceCode.TEST_TUBE_MAGNET_2ML,
            ResourceCode.REACTION_SEAL_CAP,
            ResourceCode.FLASH_FILTER_INNER_BOTTLE,
            ResourceCode.TIP_1ML,
            ResourceCode.TIP_5ML,
            ResourceCode.TIP_50UL,
        }
        detail_codes = {
            ResourceCode.REACTION_TUBE_2ML,
            ResourceCode.FLASH_FILTER_OUTER_BOTTLE,
        }
        substance_codes = {
            ResourceCode.POWDER_BUCKET_30ML,
            ResourceCode.REAGENT_BOTTLE_2ML,
            ResourceCode.REAGENT_BOTTLE_8ML,
            ResourceCode.REAGENT_BOTTLE_40ML,
            ResourceCode.REAGENT_BOTTLE_125ML,
        }
        powder_tray_code = int(ResourceCode.POWDER_BUCKET_TRAY_30ML)
        bottle_tray_codes = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML),
        }

        rows: List[JsonDict] = []
        for layout_prefix, group in grouped_by_layout.items():
            tray_item = group.get("tray_item")
            media_items = group.get("media_items", [])

            tray_code = self._get_tray_code(tray_item)
            tray_name = self._get_tray_name(tray_code)
            tray_spec = self._get_tray_spec(tray_code) if tray_code is not None else None

            count = 0
            substance_details: List[JsonDict] = []

            for media in media_items:
                resource_type_val = self._safe_int(media.get("resource_type"))
                if resource_type_val is None:
                    continue
                try:
                    resource_code = ResourceCode(resource_type_val)
                except Exception:
                    continue

                slot_index = self._extract_slot_index(media)
                used_flag = bool(media.get("used"))

                if resource_code in consumable_codes:
                    # 未使用的耗材才计数量
                    if used_flag is False:
                        count += 1
                    continue

                if resource_code in detail_codes:
                    # 已使用耗材输出当前剩余量
                    if used_flag is True:
                        substance_details.append(
                            {
                                "slot": slot_index,
                                "well": self._slot_to_well_text(slot_index, tray_spec),
                                "cur_volume": _format_amount_text(media.get("cur_volume"), media, "volume"),
                                "cur_weight": _format_amount_text(media.get("cur_weight"), media, "weight"),
                            }
                        )
                    else:
                        count += 1
                    continue

                if resource_code in substance_codes:
                    # 试剂类计数并附带物料明细
                    count += 1
                    substance_text = str(media.get("substance") or "").strip()
                    if substance_text != "":
                        if tray_code == powder_tray_code:
                            amount_value, amount_field = _pick_amount_value(
                                media,
                                ["cur_weight", "available_weight", "initial_weight"],
                            )
                            default_kind = "weight"
                        elif tray_code in bottle_tray_codes:
                            amount_value, amount_field = _pick_amount_value(
                                media,
                                ["cur_volume", "available_volume", "initial_volume"],
                            )
                            default_kind = "volume"
                        else:
                            amount_value, amount_field = _pick_amount_value(
                                media,
                                [
                                    "cur_weight",
                                    "cur_volume",
                                    "available_weight",
                                    "available_volume",
                                    "initial_weight",
                                    "initial_volume",
                                ],
                            )
                            default_kind = "volume"
                        if amount_field is not None and "weight" in amount_field:
                            amount_kind = "weight"
                        elif amount_field is not None and "volume" in amount_field:
                            amount_kind = "volume"
                        else:
                            amount_kind = default_kind

                        substance_details.append(
                            {
                                "slot": slot_index,
                                "well": self._slot_to_well_text(slot_index, tray_spec),
                                "substance": substance_text,
                                "value": _format_amount_text(amount_value, media, amount_kind),
                            }
                        )
                    continue

            rows.append(
                {
                    "layout_code": layout_prefix,
                    "count": count,
                    "resource_type": tray_code,
                    "resource_type_name": tray_name,
                    "substance_details": substance_details,
                }
            )

        return rows

    def _get_layout_code(self, item: JsonDict) -> Optional[str]:
        """
        功能:
            获取明细中的布局编码字段, 兼容 layout_code 与 source_layout_code.
        参数:
            item: JsonDict, 单条资源明细.
        返回:
            Optional[str], 布局编码字符串.
        """
        layout_code = item.get("layout_code")
        if layout_code is not None and str(layout_code) != "":
            return str(layout_code)

        source_layout_code = item.get("source_layout_code")
        if source_layout_code is not None and str(source_layout_code) != "":
            return str(source_layout_code)

        return None

    def _get_tray_code(self, tray_item: Optional[JsonDict]) -> Optional[int]:
        """
        功能:
            从托盘本体记录(slot == -1)中解析托盘编码(resource_type).
        参数:
            tray_item: Optional[JsonDict], 托盘本体记录.
        返回:
            Optional[int], 托盘编码.
        """
        if tray_item is None:
            return None
        return self._safe_int(tray_item.get("resource_type"))

    def _get_tray_name(self, tray_code: Optional[int]) -> str:
        """
        功能:
            根据托盘编码获取中文名称, 未命中时返回空字符串.
        参数:
            tray_code: Optional[int], 托盘编码.
        返回:
            str, 托盘中文名.
        """
        if tray_code is None:
            return ""
        tray_name = TRAY_CODE_DISPLAY_NAME.get(tray_code)
        if tray_name is None:
            return ""
        return tray_name

    def _build_substance_details(self, tray_code: Optional[int], media_items: List[JsonDict]) -> List[JsonDict]:
        """
        功能:
            生成 substance_details 列表, 每个元素表示一个有物质的坑位.
            若无物质, 返回空列表.
        参数:
            tray_code: Optional[int], 托盘编码, 用于 slot 到 well 的映射.
            media_items: List[JsonDict], 坑位明细列表.
        返回:
            List[JsonDict], 物质详情列表, 结构:
                {
                    "slot": int or None,
                    "well": str,
                    "substance": str,
                    "value": str,
                }
        """
        if tray_code is None:
            return []

        tray_spec = self._get_tray_spec(tray_code)
        details: List[JsonDict] = []

        for item in media_items:
            # 只输出实际有物质的坑位, 避免空坑位污染返回数据.
            substance = item.get("substance")
            if substance is None:
                continue
            if str(substance).strip() == "":
                continue

            slot_index = self._extract_slot_index(item)
            well_text = self._slot_to_well_text(slot_index, tray_spec)
            value_text = self._extract_amount_with_unit(item, tray_code) 

            details.append(
                {
                    "slot": slot_index,
                    "well": well_text,
                    "substance": str(substance).strip(),
                    "value": value_text,
                }
            )

        return details

    def _get_tray_spec(self, tray_code: int) -> Optional[Tuple[int, int]]:
        """
        功能:
            根据托盘编码获取 TraySpec 中的规格定义, 规格格式为 (col, row).
        参数:
            tray_code: int, 托盘编码.
        返回:
            Optional[Tuple[int, int]], 托盘规格(列数, 行数), 未匹配则 None.
        """
        try:
            enum_name = ResourceCode(tray_code).name
        except Exception:
            return None

        tray_spec = getattr(TraySpec, enum_name, None)
        if tray_spec is None:
            return None
        return tray_spec

    def _extract_slot_index(self, item: JsonDict) -> Optional[int]:
        """
        功能:
            从 layout_code/source_layout_code 中提取 slot 序号.
        参数:
            item: JsonDict, 单条坑位明细.
        返回:
            Optional[int], slot 序号.
        """
        layout_code = self._get_layout_code(item)
        if layout_code is None:
            return None
        if layout_code == "":
            return None
        if ":" not in layout_code:
            return None

        _, slot_text = layout_code.split(":", 1)
        return self._safe_int(slot_text)

    def _slot_to_well_text(self, slot_index: Optional[int], tray_spec: Optional[Tuple[int, int]]) -> str:
        """
        功能:
            将 slot 序号按行优先映射为井位文本, 规则 A1, A2...B1.
        参数:
            slot_index: Optional[int], slot 序号.
            tray_spec: Optional[Tuple[int, int]], (col, row) 托盘规格.
        返回:
            str, 井位文本, 无法映射时返回 "-".
        """
        if slot_index is None:
            return "-"
        if tray_spec is None:
            return str(slot_index)

        col_count, row_count = tray_spec
        if col_count <= 0:
            return str(slot_index)
        if row_count <= 0:
            return str(slot_index)

        row_index = slot_index // col_count
        col_index = slot_index % col_count + 1

        if row_index >= row_count:
            return str(slot_index)

        return f"{chr(ord('A') + row_index)}{col_index}"

    def _extract_amount_with_unit(self, item: JsonDict, tray_code: Optional[int] = None) -> str:
        """
        功能:
            根据托盘类型提取可展示的数值与单位; 粉桶托盘优先读取重量, 试剂瓶托盘优先读取体积, 其他类型按通用顺序。
        参数:
            item: JsonDict, 单条槽位明细.
            tray_code: Optional[int], 托盘资源编码, 用于确定重量或体积优先级。
        返回:
            str, 数值与单位拼接后的字符串, 例如 "5000mg".
        """
        unit = item.get("unit")
        unit_text = ""
        if unit is not None:
            if str(unit).strip() != "":
                unit_text = str(unit).strip()

        powder_tray_code = int(ResourceCode.POWDER_BUCKET_TRAY_30ML)
        bottle_tray_codes = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML),
        }

        if tray_code is not None and tray_code == powder_tray_code:
            amount_fields = (
                "cur_weight",
                "available_weight",
                "initial_weight"
            )
        elif tray_code is not None and tray_code in bottle_tray_codes:
            amount_fields = (
                "cur_volume",
                "available_volume",
                "initial_volume",
            )
        else:
            amount_fields = (
                "cur_weight",
                "cur_volume",
                "available_weight",
                "available_volume",
                "initial_weight",
                "initial_volume",
            )

        amount_value = None
        for field_name in amount_fields:
            if field_name in item:
                candidate = item.get(field_name)
                if candidate is not None:
                    amount_value = candidate
                    break

        amount_text = self._format_number(amount_value)
        if unit_text == "":
            return amount_text
        return f"{amount_text}{unit_text}"

    def _format_number(self, value: Any) -> str:
        """
        功能:
            将数值格式化为展示字符串, 整数不带小数, 小数去除尾随 0.
        参数:
            value: Any, 输入数值.
        返回:
            str, 格式化后的数字字符串, value 为空时返回 "0".
        """
        if value is None:
            return "0"

        try:
            number_value = float(value)
        except Exception:
            return str(value)

        if abs(number_value - round(number_value)) < 1e-9:
            return str(int(round(number_value)))

        text = f"{number_value:.6f}".rstrip("0").rstrip(".")
        if text == "":
            return "0"
        return text

    def _safe_int(self, value: Any) -> Optional[int]:
        """
        功能:
            安全转换为 int, 转换失败返回 None.
        参数:
            value: Any, 输入值.
        返回:
            Optional[int], 转换结果.
        """
        try:
            return int(value)
        except Exception:
            return None

    # ---------- 编辑站内化学品 ----------
    def get_chemical_list(
        self,
        *,
        query_key: Optional[str] = None,
        limit: int = 20,
    ) -> JsonDict:
        """
        功能:
            调用底层接口获取化学品列表，返回 chemical_sums 和 chemical_list
        参数:
            query_key: 可选字符串，用于模糊查询
            limit: 返回条数，传负数则不传该参数
        返回:
            Dict[str, Any]，包含 chemical_sums 和 chemical_list
        """
        params = {
            "query_key": query_key,
            "sort": "desc",   # 固定默认排序
            "offset": 0,      # 固定起始偏移
            "limit": limit,
        }
        filtered_params = {
            key: val
            for key, val in params.items()
            if val is not None and not (key == "limit" and isinstance(val, int) and val < 0)
        }

        resp = self._call_with_relogin(self._client.get_chemical_list, **filtered_params)
        data = resp if "chemical_list" in resp else (resp.get("result") or resp.get("data") or resp)

        chemical_sums = data.get("chemical_sums")
        chemical_list = data.get("chemical_list", [])
        return {"chemical_sums": chemical_sums, "chemical_list": chemical_list}

    def get_all_chemical_list(self) -> JsonDict:
        """
        功能:
            一次性获取全部化学品列表并返回包含 chemical_sums 和 chemical_list 的字典
        参数:
            无
        返回:
            Dict[str, Any], 包含 chemical_sums 和 chemical_list
        """
        first_resp = self.get_chemical_list()  # 默认 limit=20
        first_data = first_resp.get("result") or first_resp.get("data") or first_resp
        total = first_data.get("chemical_sums")
        if not isinstance(total, int):
            raise ValidationError(f"响应缺少 chemical_sums, resp={first_resp}")

        first_list = first_data.get("chemical_list") or []
        if len(first_list) >= total:
            return {"chemical_sums": total, "chemical_list": first_list}

        full_resp = self.get_chemical_list(limit=total)  # 用总数作为 limit
        full_data = full_resp.get("result") or full_resp.get("data") or full_resp
        full_list = full_data.get("chemical_list") or []
        return {"chemical_sums": total, "chemical_list": full_list}

    def add_chemical(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.add_chemical, payload)

    def update_chemical(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.update_chemical, payload)
    
    def delete_chemical(self, chemical_id: int) -> JsonDict:
        """
        功能:
            删除单个化学品
        参数:
            chemical_id: int, 化学品 id
        返回:
            Dict[str, Any], 接口响应
        """
        resp = self._call_with_relogin(self._client.delete_chemical, chemical_id)
        try:
            self._logger.info("删除化学品成功: chemical_id=%s, resp=%s", chemical_id, resp)
        except ValidationError as exc:
            self._logger.info("删除化学品失败: chemical_id=%s, resp=%s", chemical_id, resp)
            raise exc
        return resp

    def sync_chemicals_from_data(self, items: List[JsonDict], *, overwrite: bool = False, limit: int = 20000) -> None:
        """
        功能:
            接收化学品数据列表，逐条查询是否存在，按需新增或更新
        参数:
            items: List[Dict], 包含 name, cas, state 等字段的字典列表
            overwrite: Bool, 是否覆盖更新
            limit: 查询 limit
        返回:
            None
        """
        if not items:
            self._logger.info("输入数据为空, 退出同步")
            return

        for item in items:
            name = item.get("name")
            cas = item.get("cas")
            candidates: List[JsonDict] = []

            # 先按名称查询
            if name:
                name_resp = self.get_chemical_list(query_key=name, limit=limit)
                candidates.extend(name_resp.get("chemical_list", []))

            # 如有 CAS 再按 CAS 查询
            if cas:
                cas_resp = self.get_chemical_list(query_key=cas, limit=limit)
                candidates.extend(cas_resp.get("chemical_list", []))

            # 按 fid 去重
            unique = {}
            for chem in candidates:
                fid = chem.get("fid")
                if fid is not None:
                    unique[fid] = chem
            matched = list(unique.values())

            if not matched:
                self.add_chemical(item)
                self._logger.info("新增化学品: %s", name)
                continue

            if not overwrite:
                self._logger.info("已存在化学品, 跳过: %s", name)
                continue

            target = matched[0]
            payload = dict(item)
            payload["fid"] = target["fid"]
            self.update_chemical(payload)
            self._logger.info("已覆盖更新化学品: %s, fid=%s", name, target["fid"])

    # ---------- 化合物库管理 ----------
    def check_chemical_library_data(self, rows: List[JsonDict], headers: List[str]) -> Dict[str, List[str]]:
        """
        功能:
            校验化学库数据的表头、名称唯一性、状态字段和形态依赖字段，输出错误与警告
        参数:
            rows: List[Dict[str, Any]], 数据行列表，键名需与表头一致
            headers: List[str], 原始表头列表
        返回:
            Dict[str, List[str]], 包含 errors 与 warnings
        """
        required_headers = [
            "cas_number",
            "chemical_id",
            "substance_english_name",
            "substance_chinese_name",
            "molecular_weight",
            "density (g/mL)",
            "physical_state",
            "physical_form",
            "active_content(mol/L or wt%)"
        ]
        normalized_headers = [str(col).strip() for col in headers]

        errors: List[str] = []
        warnings: List[str] = []

        missing_headers = [col for col in required_headers if col not in normalized_headers]
        if len(missing_headers) > 0:
            warnings.append(f"表头缺少字段: {', '.join(missing_headers)}")

        english_name_count: Dict[str, int] = {}
        chinese_name_count: Dict[str, int] = {}
        missing_name_rows: List[str] = []
        invalid_state_rows: List[str] = []
        missing_form_rows: List[str] = []
        missing_mw_neat: List[str] = []
        missing_density_neat_liquid: List[str] = []
        missing_content_solution: List[str] = []
        missing_fields_beads: List[str] = []
        allowed_states = {"solid", "liquid", "gas"}

        for idx, row in enumerate(rows, start=2):
            english_name = str(row.get("substance_english_name") or "").strip()
            chinese_name = str(row.get("substance_chinese_name") or "").strip()
            cas_number = str(row.get("cas_number") or "").strip()
            chemical_id = str(row.get("chemical_id") or "").strip()
            molecular_weight = str(row.get("molecular_weight") or "").strip()
            density = str(row.get("density (g/mL)") or "").strip()
            physical_state_raw = str(row.get("physical_state") or "").strip()
            physical_form_raw = str(row.get("physical_form") or "").strip()
            active_content = str(row.get("active_content(mol/L or wt%)") or "").strip()

            row_label = english_name or chinese_name or cas_number or chemical_id or f"第{idx}行"  # 标记行用于提示

            if english_name != "":
                english_name_count[english_name] = english_name_count.get(english_name, 0) + 1
            if chinese_name != "":
                chinese_name_count[chinese_name] = chinese_name_count.get(chinese_name, 0) + 1
            if english_name == "" and chinese_name == "":
                missing_name_rows.append(row_label)

            physical_state = physical_state_raw.lower()
            if physical_state == "" or physical_state not in allowed_states:
                invalid_state_rows.append(row_label)

            physical_form = physical_form_raw.lower()
            if physical_form == "":
                missing_form_rows.append(row_label)

            if physical_form == "neat":
                if molecular_weight == "":
                    missing_mw_neat.append(row_label)
                if physical_state == "liquid" and density == "":
                    missing_density_neat_liquid.append(row_label)
            if physical_form == "solution":
                if active_content == "":
                    missing_content_solution.append(row_label)
            if physical_form == "beads":
                missing_items = []
                if molecular_weight == "":
                    missing_items.append("molecular_weight")
                if active_content == "":
                    missing_items.append("active_content(mol/L or wt%)")
                if len(missing_items) > 0:
                    missing_fields_beads.append(f"{row_label} 缺少 {', '.join(missing_items)}")

        duplicated_english = [name for name, count in english_name_count.items() if count > 1]
        duplicated_chinese = [name for name, count in chinese_name_count.items() if count > 1]
        if len(duplicated_english) > 0:
            errors.append(f"substance_english_name 出现重复: {', '.join(duplicated_english)}")
        if len(duplicated_chinese) > 0:
            errors.append(f"substance_chinese_name 出现重复: {', '.join(duplicated_chinese)}")
        if len(missing_name_rows) > 0:
            warnings.append(f"至少填写中文名或英文名: {', '.join(missing_name_rows)}")
        if len(invalid_state_rows) > 0:
            warnings.append(f"physical_state 缺失或非法(仅支持 solid/liquid/gas): {', '.join(invalid_state_rows)}")
        if len(missing_form_rows) > 0:
            warnings.append(f"physical_form 缺失: {', '.join(missing_form_rows)}")
        if len(missing_mw_neat) > 0:
            warnings.append(f"physical_form 为 neat 需填写 molecular_weight: {', '.join(missing_mw_neat)}")
        if len(missing_density_neat_liquid) > 0:
            warnings.append(f"neat 且 physical_state 为 liquid 需填写 density (g/mL): {', '.join(missing_density_neat_liquid)}")
        if len(missing_content_solution) > 0:
            warnings.append(f"physical_form 为 solution 需填写 active_content(mol/L or wt%): {', '.join(missing_content_solution)}")
        if len(missing_fields_beads) > 0:
            warnings.append(f"physical_form 为 beads 需填写 molecular_weight 和 active_content(mol/L or wt%): {', '.join(missing_fields_beads)}")

        if len(errors) > 0:
            self._logger.error("化学库校验失败, 错误=%s, 警告=%s", len(errors), len(warnings))
        else:
            self._logger.info("化学库校验完成, 警告=%s", len(warnings))

        return {"errors": errors, "warnings": warnings}
    
    def deduplicate_chemical_library_data(self, rows: List[JsonDict], headers: List[str]) -> List[JsonDict]:
        """
        功能:
            按 substance 聚合化合物行数据, 合并品牌类字段和其他多值字段
        参数:
            rows: List[Dict[str, Any]], 表格行数据, 键名与表头一致
            headers: List[str], 原始表头列表, 用于保持输出列顺序
        返回:
            List[Dict[str, Any]], 去重后的数据
        """
        normalized_headers = [str(h).strip().lower() for h in headers]
        if len(headers) == 0:
            self._logger.error("表头为空, 无法去重")
            return rows
        if "substance" not in normalized_headers:
            self._logger.error("表头缺少 substance, 无法按物质去重")
            return rows

        brand_fields = {"brand", "package_size", "storage_location"}
        header_info = [(str(h).strip(), str(h).strip().lower()) for h in headers]
        substance_field = next((orig for orig, lower in header_info if lower == "substance"), "substance")
        dedup_substance: Dict[str, Dict[str, List[str]]] = {}
        substance_order: List[str] = []
        no_substance_stores: List[Dict[str, List[str]]] = []

        def _clean(val: Any) -> str:
            if val is None:
                return ""
            if isinstance(val, float):
                if math.isnan(val):
                    return ""
                if val.is_integer():
                    return str(int(val))
                return str(val).rstrip("0").rstrip(".")
            if isinstance(val, int):
                return str(val)
            text = str(val).strip()
            return "" if text.lower() == "nan" else text

        def _init_store() -> Dict[str, List[str]]:
            return {info[1]: [] for info in header_info}

        def _merge_row(store: Dict[str, List[str]], row_data: JsonDict) -> None:
            for orig, key in header_info:
                val = _clean(row_data.get(orig))
                if val == "":
                    continue
                if val not in store[key]:
                    store[key].append(val)

        # 按 substance 聚合行并记录顺序
        for row in rows:
            substance_key = _clean(row.get(substance_field))
            if substance_key != "":
                if substance_key not in dedup_substance:
                    dedup_substance[substance_key] = _init_store()
                    substance_order.append(substance_key)
                _merge_row(dedup_substance[substance_key], row)
            else:
                tmp_store = _init_store()
                _merge_row(tmp_store, row)
                no_substance_stores.append(tmp_store)

        # 生成输出, 品牌类字段用分号拼接, 其他多值加括号
        def _build_output(store: Dict[str, List[str]]) -> JsonDict:
            out_row: JsonDict = {}
            for orig, field_key in header_info:
                values = store.get(field_key, [])
                if field_key in brand_fields:
                    out_row[orig] = ";".join(values)
                else:
                    if len(values) == 0:
                        out_row[orig] = ""
                    elif len(values) == 1:
                        out_row[orig] = values[0]
                    else:
                        out_row[orig] = f"({';'.join(values)})"
            return out_row

        result: List[JsonDict] = []
        for key in substance_order:
            result.append(_build_output(dedup_substance[key]))
        for store in no_substance_stores:
            result.append(_build_output(store))

        self._logger.info("化合物库去重完成, 原始行数=%s, 去重后=%s", len(rows), len(result))
        return result

    def align_chemicals_from_data(self, rows: List[JsonDict], *, auto_delete: bool = False) -> List[JsonDict]:
        """
        功能:
            对齐工站化学品: 以传入数据为准校正工站数据，并回填 chemical_id/fid 到返回数据中
        参数:
            rows: List[Dict], 包含 substance/name, cas, physical_state 等字段
            auto_delete: bool, 是否删除工站内多余的化学品
        返回:
            List[Dict]: 更新后的数据列表 (包含回填的 ID)
        """
        if not rows:
            return []

        # 获取工站所有数据
        station_data = self.get_all_chemical_list()
        station_list = station_data.get("chemical_list") or []
        station_by_name = {
            str(item.get("name") or "").strip(): item for item in station_list if item.get("name")
        }

        updated = 0
        added = 0
        fid_by_substance: Dict[str, int] = {}

        # 遍历输入行进行同步
        for row in rows:
            # 兼容字段名：substance 或 name
            name = str(row.get("substance") or row.get("name") or "").strip()
            if not name:
                continue
            
            cas_file = str(row.get("cas_number") or row.get("cas") or "").strip()
            state_file = str(row.get("physical_state") or row.get("state") or "").strip()

            existing = station_by_name.get(name)
            
            # Case A: 工站不存在 -> 新增
            if existing is None:
                payload = {"name": name}
                if cas_file: payload["cas"] = cas_file
                if state_file: payload["state"] = state_file
                
                try:
                    resp = self.add_chemical(payload)
                    fid = resp.get("fid") or resp.get("chemical_id")
                    if isinstance(fid, int):
                        fid_by_substance[name] = fid
                    added += 1
                except Exception as e:
                    self._logger.error(f"新增失败 {name}: {e}")
                continue

            # Case B: 工站存在 -> 检查更新
            fid = existing.get("fid")
            fid_by_substance[name] = fid if isinstance(fid, int) else None

            payload = {k: v for k, v in existing.items() if v is not None}
            payload["fid"] = fid
            payload["name"] = existing.get("name") # 保持原名

            need_update = False
            cas_station = str(existing.get("cas") or "").strip()
            if cas_file and cas_file != cas_station:
                payload["cas"] = cas_file
                need_update = True

            state_station = str(existing.get("state") or "").strip()
            if state_file and state_file != state_station:
                payload["state"] = state_file
                need_update = True

            if need_update:
                self.update_chemical(payload)
                updated += 1

        # 自动删除逻辑
        if auto_delete:
            file_names = {
                str(r.get("substance") or r.get("name") or "").strip() for r in rows
            }
            for item in station_list:
                s_name = str(item.get("name") or "").strip()
                s_fid = item.get("fid")
                if s_name and s_name not in file_names and isinstance(s_fid, int):
                    self.delete_chemical(s_fid)

        # 回写 ID 到原数据结构
        result_rows = []
        for row in rows:
            new_row = row.copy()
            name = str(new_row.get("substance") or new_row.get("name") or "").strip()
            fid = fid_by_substance.get(name)
            if isinstance(fid, int):
                new_row["chemical_id"] = fid # 统一回填到 chemical_id 字段
            result_rows.append(new_row)

        self._logger.info("化学品对齐逻辑执行完毕: 更新=%s, 新增=%s", updated, added)
        return result_rows
    
    # ---------- 上料函数 ----------
    def _well_to_slot_index(self, well: str, tray_spec: Optional[Tuple[int, int]]) -> Optional[int]:
        """
        功能:
            将井位文本（如 A1、B2）转换为 slot 序号，按行优先编号。
        参数:
            well: str, 井位文本，格式 字母+数字，如 A1。
            tray_spec: Optional[Tuple[int, int]], (列数, 行数)。
        返回:
            Optional[int], 对应的 slot 序号，无法解析时返回 None。
        """
        if tray_spec is None:
            return None
        if not well or len(well) < 2:
            return None
        row_char = well[0].upper()
        if not row_char.isalpha():
            return None
        try:
            col_count, row_count = tray_spec
            col_index = int(well[1:])  # 1-based
            row_index = ord(row_char) - ord("A")  # 0-based
            if col_index < 1 or col_index > col_count:
                return None
            if row_index < 0 or row_index >= row_count:
                return None
            return row_index * col_count + (col_index - 1)
        except Exception:
            return None

    def _normalize_tray_code_text(self, raw: Any) -> str:
        """
        功能:
            从类似“50 μL Tip 头托盘(201000815)”提取括号内的编码; 如果没有括号则返回原文本.
        参数:
            raw: Any, 单元格原始值.
        返回:
            str, 纯数字编码字符串或原文本.
        """
        if raw is None:
            return ""
        text = str(raw).strip()
        if "(" in text and ")" in text:
            inside = text[text.rfind("(") + 1:text.rfind(")")]
            digits = "".join(ch for ch in inside if ch.isdigit())
            if digits != "":
                return digits
        return text

    def _split_amount_unit(self, text: str) -> Tuple[float, str]:
        """
        功能:
            将类似 '2mL' 或 '500mg' 的文本拆为数值和单位.
        参数:
            text: str, 输入文本.
        返回:
            (float, str), 数值与单位, 无法解析数值时返回 0.
        """
        number_part = ""
        unit_part = ""
        for ch in str(text):
            if ch.isdigit() or ch == ".":
                number_part += ch
            else:
                unit_part += ch
        try:
            value = float(number_part) if number_part != "" else 0.0
        except Exception:
            value = 0.0
        # 统一单位：把 MICRO SIGN(U+00B5) 归一成 GREEK MU(U+03BC)
        unit_norm = unit_part.strip().replace("µ", "μ")
        unit = unit_norm if unit_norm else "mL"
        return value, unit

    def batch_in_tray(
        self,
        resource_req_list: List[JsonDict],
        *,
        task_id: Optional[int] = None,
        poll_interval_s: float = 1.0,
        timeout_s: float = 900.0,
    ) -> JsonDict:
        """
        功能:
            批量上料，执行前后均等待设备空闲，确保空闲状态下触发上料并等待恢复空闲。
        参数:
            resource_req_list: 批量上料的资源信息列表。
            task_id: int, 可选, 任务ID (用于记录).
            poll_interval_s: 轮询间隔秒数.
            timeout_s: 等待空闲的超时时长.
        返回:
            Dict, 执行 batch_in_tray 的接口响应.
        """
        if resource_req_list is None or len(resource_req_list) == 0:
            raise ValidationError("resource_req_list 不能为空")

        start_time = datetime.now().isoformat()

        self.wait_idle(stage="上料前", poll_interval_s=poll_interval_s, timeout_s=timeout_s)
        self._logger.info("仪器空闲，开始上料")
        resp = self._call_with_relogin(self._client.batch_in_tray, resource_req_list)
        self._logger.info("上料指令已发送，等待仪器回到空闲")
        self.wait_idle(stage="上料后", poll_interval_s=poll_interval_s, timeout_s=timeout_s)
        self._logger.info("上料完成")

        end_time = datetime.now().isoformat()

        # 保存上料日志
        if self._data_manager:
            # 构造日志记录格式
            log_resources: List[JsonDict] = []
            for req in resource_req_list:
                layout_code = req.get("layout_code", "")
                resource_type = req.get("resource_type")
                resource_type_name = req.get("resource_type_name", "")

                # 从 substance_list 构造 substance_details
                substance_details = []
                substance_list = req.get("substance_list", [])
                for sub in substance_list:
                    substance_details.append({
                        "slot": sub.get("slot", 0),
                        "well": sub.get("well", ""),
                        "substance": sub.get("substance", ""),
                        "value": sub.get("value", "")
                    })

                count = len(substance_list) if substance_list else req.get("count", 0)

                log_resource = {
                    "layout_code": layout_code,
                    "count": count,
                    "resource_type": resource_type,
                    "resource_type_name": resource_type_name,
                    "substance_details": substance_details,
                    "task_id": task_id
                }
                log_resources.append(log_resource)

            log_data = {
                "start_time": start_time,
                "end_time": end_time,
                "resources": log_resources
            }
            self._data_manager.save_batch_in_tray_log(log_data)

        return resp
    
    def build_batch_in_tray_payload(self, rows: List[Tuple[str, str, str]]) -> List[JsonDict]:
        """
        功能:
            将上料表格行转换为批量上料的payload, 校验托盘点位或耗材数量, 并统一用量单位为mg和mL
        参数:
            rows: List[Tuple[str, str, str]], 每行依次为托盘位置、托盘类型文本、槽位/物质/用量描述
        返回:
            List[JsonDict], 符合 batch_in_tray 接口要求的 resource_req_list
        """
        tray_to_media = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): (str(int(ResourceCode.REAGENT_BOTTLE_2ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): (str(int(ResourceCode.REAGENT_BOTTLE_8ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): (str(int(ResourceCode.REAGENT_BOTTLE_40ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): (str(int(ResourceCode.REAGENT_BOTTLE_125ML)), True, "volume", "mL"),
            int(ResourceCode.POWDER_BUCKET_TRAY_30ML): (str(int(ResourceCode.POWDER_BUCKET_30ML)), False, "weight", "mg"),
        }
        no_substance_trays = {
            int(ResourceCode.TIP_TRAY_50UL), int(ResourceCode.TIP_TRAY_1ML), int(ResourceCode.TIP_TRAY_5ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY), int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML), int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML),
        }
        consumable_map = {
            int(ResourceCode.TIP_TRAY_50UL): int(ResourceCode.TIP_50UL),
            int(ResourceCode.TIP_TRAY_1ML): int(ResourceCode.TIP_1ML),
            int(ResourceCode.TIP_TRAY_5ML): int(ResourceCode.TIP_5ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY): int(ResourceCode.REACTION_SEAL_CAP),
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML): int(ResourceCode.REACTION_TUBE_2ML),
            int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): int(ResourceCode.TEST_TUBE_MAGNET_2ML),
        }

        chem_cache: Dict[str, Optional[int]] = {}

        def _resolve_fid(sub_name: str) -> int:
            if sub_name in chem_cache:
                if chem_cache[sub_name] is None:
                    raise ValidationError(f"未找到化学品: {sub_name}")
                return chem_cache[sub_name]
            resp = self.get_chemical_list(query_key=sub_name, limit=10)
            lst = resp.get("chemical_list", [])
            for c in lst:
                if str(c.get("name")).strip() == sub_name:
                    fid = c.get("fid") or c.get("chemical_id")
                    chem_cache[sub_name] = fid
                    return fid
            if len(lst) > 0:
                fid = lst[0].get("fid") or lst[0].get("chemical_id")
                chem_cache[sub_name] = fid
                return fid
            chem_cache[sub_name] = None
            raise ValidationError(f"未找到化学品: {sub_name}")

        def _normalize_amount(value: float, unit_text: str, amount_kind: str, default_unit: str) -> Tuple[float, str]:
            if unit_text is None:
                unit_text = ""
            unit_text = str(unit_text).strip()
            if unit_text == "":
                unit_text = default_unit
            unit_lower = unit_text.lower().replace("Âµ", "μ").replace("æ¸", "μ")
            if amount_kind == "volume":
                if unit_lower == "l":
                    return value * 1000.0, "mL"
                if unit_lower == "ml":
                    return value, "mL"
                if unit_lower in ("μl", "Âµl", "ul"):
                    return value / 1000.0, "mL"
                return value, "mL"
            if amount_kind == "weight":
                if unit_lower == "g":
                    return value * 1000.0, "mg"
                if unit_lower == "mg":
                    return value, "mg"
                return value, "mg"
            return value, default_unit

        def _validate_slot_index(slot_idx: Optional[int], tray_layout: str, tray_spec: Optional[Tuple[int, int]]) -> int:
            if slot_idx is None:
                raise ValidationError(f"托盘 {tray_layout} 的点位填写有误")
            if tray_spec is None:
                self._logger.warning("托盘 %s 缺少规格定义, 跳过点位范围校验", tray_layout)
                return slot_idx
            col_count, row_count = tray_spec
            max_slot = col_count * row_count
            if slot_idx < 0 or slot_idx >= max_slot:
                raise ValidationError(f"托盘 {tray_layout} 点位 {slot_idx} 超出有效范围 0~{max_slot - 1}")
            return slot_idx

        def _validate_consumable_qty(qty: Optional[int], tray_layout: str, tray_spec: Optional[Tuple[int, int]]) -> int:
            if qty is None:
                raise ValidationError(f"托盘 {tray_layout} 填写的耗材数量为空")
            if qty <= 0:
                raise ValidationError(f"托盘 {tray_layout} 填写的耗材数量必须大于0")
            if tray_spec is None:
                self._logger.warning("托盘 %s 缺少规格定义, 跳过容量校验", tray_layout)
                return qty
            capacity = tray_spec[0] * tray_spec[1]
            if qty > capacity:
                raise ValidationError(f"托盘 {tray_layout} 填写的耗材数量 {qty} 超出容量 {capacity}")
            return qty

        resource_req_list: List[JsonDict] = []

        for position, tray_type_raw, content in rows:
            tray_layout = str(position).strip()
            if tray_layout == "":
                continue

            tray_code_text = self._normalize_tray_code_text(tray_type_raw)
            tray_code_int = self._safe_int(tray_code_text)
            if tray_code_int is None:
                continue

            tray_spec = self._get_tray_spec(tray_code_int)

            resource_list: List[JsonDict] = []
            resource_list.append({
                "layout_code": f"{tray_layout}:-1",
                "resource_type": str(tray_code_int),
            })

            if tray_code_int in no_substance_trays:
                qty = self._safe_int(content)
                qty = _validate_consumable_qty(qty, tray_layout, tray_spec)  # 校验耗材数量是否在容量内
                res_type = str(consumable_map.get(tray_code_int, tray_code_int))
                for idx in range(qty):
                    resource_list.append({
                        "layout_code": f"{tray_layout}:{idx}",
                        "resource_type": res_type,
                        "with_cap": False
                    })
            else:
                entries = [seg.strip() for seg in str(content).split(";") if seg.strip() != ""]
                media_code, with_cap, amt_kind, def_unit = tray_to_media.get(
                    tray_code_int, (str(tray_code_int), False, "volume", "mL")
                )

                for seg in entries:
                    parts = [p.strip() for p in seg.split("|")]
                    if len(parts) < 3:
                        continue

                    slot_raw, substance, amt_str = parts[0], parts[1], parts[2]

                    slot_idx = self._safe_int(slot_raw)
                    if slot_idx is None:
                        slot_idx = self._well_to_slot_index(slot_raw, tray_spec)
                    slot_idx = _validate_slot_index(slot_idx, tray_layout, tray_spec)  # 校验点位是否在托盘范围内

                    raw_value, raw_unit = self._split_amount_unit(amt_str)
                    norm_value, norm_unit = _normalize_amount(raw_value, raw_unit, amt_kind, def_unit)

                    fid = _resolve_fid(substance)

                    media_item = {
                        "layout_code": f"{tray_layout}:{slot_idx}",
                        "resource_type": media_code,
                        "with_cap": with_cap,
                        "substance": substance,
                        "unit": norm_unit,
                        "chemical_id": fid
                    }

                    if amt_kind == "volume":
                        media_item["initial_volume"] = norm_value
                    else:
                        media_item["initial_weight"] = norm_value

                    resource_list.append(media_item)

            resource_req_list.append({
                "remark": "",
                "resource_list": resource_list
            })

        self._logger.info("已解析上料信息, 包含 %s 个托盘", len(resource_req_list))
        return resource_req_list

    # ---------- 下料函数 ----------
    def batch_out_tray(
        self,
        layout_list: List[JsonDict],
        move_type: str = "main_out",
        *,
        task_id: Optional[int] = None,
        poll_interval_s: float = 1.0,
        timeout_s: float = 900.0,
    ) -> JsonDict:
        """
        功能:
            批量下料，执行前后均等待设备空闲，确保空闲状态下触发下料并等待恢复空闲。
        参数:
            layout_list: List[Dict], 资源位置信息列表, 每项包含:
                - layout_code: str, 源位置编码, 如 "N-1-1"
                - resource_type: str, 可选, 资源类型 (不需要传入, 会自动获取)
                - dst_layout_code: str, 可选, 目标下料位置, 如 "TB-1-1"
                  如果未指定, 则按 TB-2-1 到 TB-2-4, 再 TB-1-1 到 TB-1-4 的顺序自动分配
                - task_id: int, 可选, 任务ID (用于记录, 如果在 layout_list 中指定则优先使用)
            move_type: str, 下料方式, 默认 "main_out".
            task_id: int, 可选, 默认任务ID (如果 layout_list 中的项没有指定 task_id 则使用此值).
            poll_interval_s: 轮询间隔秒数.
            timeout_s: 等待空闲的超时时长.
        返回:
            Dict, 接口响应.
        示例:
            layout_list = [
                {"layout_code": "N-1-1", "dst_layout_code": "TB-1-1", "task_id": 123},
                {"layout_code": "N-1-2"}  # 自动分配下料位置, 使用默认 task_id
            ]
        """
        
        if layout_list is None:
            raise ValidationError("layout_list 不能为空")
        if len(layout_list) == 0:
            raise ValidationError("layout_list 不能为空")

        start_time = datetime.now().isoformat()

        # 获取资源信息用于查询 resource_type
        resource_info = self.get_resource_info()
        resource_map = {item["layout_code"]: item for item in resource_info}

        # 定义默认下料位置顺序: TB-2-1 到 TB-2-4, 然后 TB-1-1 到 TB-1-4
        default_dst_positions = [
            "TB-2-1", "TB-2-2", "TB-2-3", "TB-2-4",
            "TB-1-1", "TB-1-2", "TB-1-3", "TB-1-4"
        ]
        dst_position_index = 0

        processed_layout_list: List[JsonDict] = []
        log_resources: List[JsonDict] = []

        for item in layout_list:
            if item is None:
                continue

            layout_code = str(item.get("layout_code", "")).strip()
            if layout_code == "":
                continue

            # 确定目标下料位置
            dst_layout_code = item.get("dst_layout_code")
            if dst_layout_code:
                dst_layout_code = str(dst_layout_code).strip()
            else:
                # 自动分配下料位置
                if dst_position_index >= len(default_dst_positions):
                    raise ValidationError(
                        f"下料位置不足, 最多支持 {len(default_dst_positions)} 个托盘下料"
                    )
                dst_layout_code = default_dst_positions[dst_position_index]
                dst_position_index += 1

            # 从资源信息中获取源位置的 resource_type
            resource_type = None
            resource_type_name = ""
            count = 0
            substance_details = []

            if layout_code in resource_map:
                resource_data = resource_map[layout_code]
                resource_type = resource_data.get("resource_type")
                resource_type_name = resource_data.get("resource_type_name", "")
                count = resource_data.get("count", 0)
                substance_details = resource_data.get("substance_details", [])

            if resource_type is None:
                self._logger.warning(
                    "无法从资源信息中获取 layout_code=%s 的 resource_type, 将使用 None",
                    layout_code
                )

            # 获取 task_id (优先使用 item 中的, 否则使用参数中的)
            item_task_id = item.get("task_id", task_id)

            # 构造 API 需要的格式
            processed_item = {
                "layout_code": layout_code,
                "resource_type": resource_type,
                "dst_layout_code": dst_layout_code
            }
            processed_layout_list.append(processed_item)

            # 构造日志记录格式
            log_resource = {
                "layout_code": layout_code,
                "count": count,
                "resource_type": resource_type,
                "resource_type_name": resource_type_name,
                "substance_details": substance_details,
                "task_id": item_task_id,
                "dst_layout_code": dst_layout_code
            }
            log_resources.append(log_resource)

            self._logger.debug(
                "下料配置: layout_code=%s, dst_layout_code=%s, resource_type=%s, task_id=%s",
                layout_code, dst_layout_code, resource_type, item_task_id
            )

        if len(processed_layout_list) == 0:
            raise ValidationError("layout_list 解析后为空")
        
        self.wait_idle(stage="下料前", poll_interval_s=poll_interval_s, timeout_s=timeout_s)
        self._logger.info("仪器空闲，开始下料")
        resp = self._call_with_relogin(self._client.batch_out_tray, processed_layout_list, move_type)
        self._logger.info("下料指令已发送，等待仪器回到空闲")
        self.wait_idle(stage="下料后", poll_interval_s=poll_interval_s, timeout_s=timeout_s)
        self._logger.info("下料完成，共 %d 个托盘", len(processed_layout_list))

        end_time = datetime.now().isoformat()

        # 保存下料日志
        if self._data_manager:
            log_data = {
                "start_time": start_time,
                "end_time": end_time,
                "resources": log_resources
            }
            self._data_manager.save_batch_out_tray_log(log_data)

        return resp

    def get_task_tray_mapping(self, task_id: int) -> JsonDict:
        """
        功能:
            获取指定任务的托盘编号信息, 提取反应试管托盘与样品托盘.
        参数:
            task_id: 任务 id.
        返回:
            Dict[str, Any], 包含 reaction_trays 与 sampling_trays 两个列表.
        """
        resp = self._call_with_relogin(self._client.get_task_info, task_id)
        data = resp.get("result") or resp.get("data") or resp

        units = data.get("layout_list") or data.get("unit_list") or []
        if not isinstance(units, list) or not units:
            raise ValidationError(f"任务 {task_id} 缺少 unit_list/layout_list, resp={resp}")

        reaction_trays: set[str] = set()
        sampling_trays: set[str] = set()

        for unit in units:
            layout_code = str(unit.get("layout_code") or "")
            if ":" in layout_code:
                reaction_trays.add(layout_code.split(":", 1)[0])  # 反应试管托盘

            if str(unit.get("unit_type")) == "exp_filtering_sample":
                process_json = unit.get("process_json") or {}
                sampling_layout_code = str(process_json.get("sampling_layout_code") or "")
                if ":" in sampling_layout_code:
                    sampling_trays.add(sampling_layout_code.split(":", 1)[0])  # 样品托盘

        result = {
            "task_id": task_id,
            "reaction_trays": sorted(reaction_trays),
            "sampling_trays": sorted(sampling_trays),
        }
        self._logger.debug(
            "任务 %s 托盘提取完成 reaction_trays=%s sampling_trays=%s",
            task_id,
            result["reaction_trays"],
            result["sampling_trays"],
        )
        return result
    
    def list_empty_trays(self) -> List[JsonDict]:
        """
        功能:
            查询资源信息并返回可用数量为0的托盘位置.
        参数:
            无
        返回:
            List[Dict[str, Any]], 每项包含托盘布局编码及托盘类型信息.
        """
        rows = self.get_resource_info()
        empty_trays: List[JsonDict] = []

        for row in rows:
            count_val = row.get("count")
            if isinstance(count_val, int) and count_val == 0:
                empty_trays.append(
                    {
                        "layout_code": row.get("layout_code"),
                        "resource_type": row.get("resource_type"),
                        "resource_type_name": row.get("resource_type_name", ""),
                    }
                )

        self._logger.debug("可用资源为0的托盘数量=%s", len(empty_trays))
        return empty_trays

    def batch_out_task_and_empty_trays(self, task_id: Optional[int] = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out") -> JsonDict:
        """
        功能:
            汇总指定或自动选择的已完成任务涉及托盘与当前空托盘, 校验存在性后执行批量下料
        参数:
            task_id: 任务 id, None 时自动选择最近完成的任务
            poll_interval_s: 轮询空闲状态的时间间隔(秒)
            ignore_missing: True 时忽略未在资源列表中的托盘并记录 warning, False 时抛错终止
            timeout_s: 等待空闲的超时时间(秒)
            move_type: 下料方式, 默认 "main_out"
        返回:
            Dict, 执行 batch_out_tray 的接口响应
        """
        self.wait_idle(stage="等待任务结束", poll_interval_s=poll_interval_s, timeout_s=timeout_s)   #等待任务结束后再提取托盘信息，否则会提取不到

        target_task_id = task_id
        if target_task_id is None:
            tasks_resp = self.get_task_list(sort="desc", offset=0, limit=50)
            task_list = tasks_resp.get("task_list") or tasks_resp.get("result", {}).get("task_list") or tasks_resp.get("data", {}).get("task_list")
            completed_ids: List[int] = []
            if isinstance(task_list, list):
                for item in task_list:
                    cur_id = item.get("task_id")
                    cur_status = item.get("status")
                    if isinstance(cur_id, int) and cur_status == int(TaskStatus.COMPLETED):
                        completed_ids.append(cur_id)
            if len(completed_ids) == 0:
                raise ValidationError("未找到下料的已完成任务")
            target_task_id = max(completed_ids)
            self._logger.info("未传入 task_id, 自动选择最近完成的任务: %s", target_task_id)

        self._logger.info("准备下料")

        mapping = self.get_task_tray_mapping(target_task_id)
        reaction_trays = mapping.get("reaction_trays") or []
        sampling_trays = mapping.get("sampling_trays") or []

        skip_prefixes = ("MSB", "MS", "AS", "TS")

        def _is_excluded_code(code: Any) -> bool:
            if code is None:
                return False
            text = str(code).strip().upper()
            return any(text.startswith(prefix) for prefix in skip_prefixes)

        raw_task_tray_codes = {(code or "").strip() for code in reaction_trays if (code or "").strip()}
        raw_task_tray_codes |= {(code or "").strip() for code in sampling_trays if (code or "").strip()}

        empty_trays = self.list_empty_trays()
        raw_empty_codes = {str(item.get("layout_code")).strip() for item in empty_trays if item.get("layout_code")}

        excluded_codes = {code for code in (raw_task_tray_codes | raw_empty_codes) if _is_excluded_code(code)}
        if excluded_codes:
            self._logger.info("按前缀规则忽略托盘: %s", sorted(excluded_codes))

        task_tray_codes = {code for code in raw_task_tray_codes if code not in excluded_codes}
        empty_codes = {code for code in raw_empty_codes if code not in excluded_codes}

        target_codes = {*(task_tray_codes or []), *empty_codes}
        target_codes = {str(code).strip() for code in target_codes if str(code).strip()}

        if not target_codes:
            raise ValidationError(f"任务 {target_task_id} 未找到需要下料的托盘位置")

        resource_rows = self.get_resource_info()
        existing_codes = {str(row.get("layout_code") or "").strip() for row in resource_rows if row.get("layout_code")}
        missing_codes = sorted(code for code in target_codes if code not in existing_codes)

        if missing_codes:
            if ignore_missing:
                self._logger.warning("下料位置未在资源列表, 已忽略: %s", missing_codes)
                target_codes = {code for code in target_codes if code not in missing_codes}
            else:
                raise ValidationError(f"下料位置不存在: {missing_codes}")

        if not target_codes:
            raise ValidationError("可执行下料的位置为空, 请检查输入或资源列表")

        empty_set = {code for code in empty_codes if code}
        empty_only = sorted(empty_set - task_tray_codes)

        self._logger.info(
            "准备批量下料 task_id=%s  任务托盘=%s  空托盘=%s  实际下料位置=%s",
            target_task_id,
            sorted(task_tray_codes),
            empty_only,
            sorted(target_codes),
        )

        # 构造新的 layout_list 格式，任务托盘附上 task_id，空托盘不附
        layout_list = []
        for code in sorted(target_codes):
            item = {"layout_code": code}
            if code in task_tray_codes:
                item["task_id"] = target_task_id
            layout_list.append(item)

        resp = self.batch_out_tray(
            layout_list,
            move_type=move_type,
            task_id=None,  # 已在 layout_list 中指定
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s
        )

        return resp

    def batch_out_task_trays(self, task_id: Optional[int] = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out") -> JsonDict:
        """
        功能:
            下料指定或自动选择的已完成任务涉及的物料托盘
        参数:
            task_id: 任务 id, None 时自动选择最近完成的任务
            poll_interval_s: 轮询空闲状态的时间间隔(秒)
            ignore_missing: True 时忽略未在资源列表中的托盘并记录 warning, False 时抛错终止
            timeout_s: 等待空闲的超时时间(秒)
            move_type: 下料方式, 默认 "main_out"
        返回:
            Dict, 执行 batch_out_tray 的接口响应
        """

        self.wait_idle(stage="等待任务结束", poll_interval_s=poll_interval_s, timeout_s=timeout_s)   #等待任务结束后再提取托盘信息，否则会提取不到

        target_task_id = task_id
        if target_task_id is None:
            tasks_resp = self.get_task_list(sort="desc", offset=0, limit=50)
            task_list = tasks_resp.get("task_list") or tasks_resp.get("result", {}).get("task_list") or tasks_resp.get("data", {}).get("task_list")
            completed_ids: List[int] = []
            if isinstance(task_list, list):
                for item in task_list:
                    cur_id = item.get("task_id")
                    cur_status = item.get("status")
                    if isinstance(cur_id, int) and cur_status == int(TaskStatus.COMPLETED):
                        completed_ids.append(cur_id)
            if len(completed_ids) == 0:
                raise ValidationError("未找到下料的已完成任务")
            target_task_id = max(completed_ids)
            self._logger.info("未传入 task_id, 自动选择最近完成的任务: %s", target_task_id)

        self._logger.info("准备下料任务物料托盘")

        mapping = self.get_task_tray_mapping(target_task_id)
        reaction_trays = mapping.get("reaction_trays") or []
        sampling_trays = mapping.get("sampling_trays") or []

        skip_prefixes = ("MSB", "MS", "AS", "TS")

        def _is_excluded_code(code: Any) -> bool:
            if code is None:
                return False
            text = str(code).strip().upper()
            return any(text.startswith(prefix) for prefix in skip_prefixes)

        raw_task_tray_codes = {(code or "").strip() for code in reaction_trays if (code or "").strip()}
        raw_task_tray_codes |= {(code or "").strip() for code in sampling_trays if (code or "").strip()}

        excluded_codes = {code for code in raw_task_tray_codes if _is_excluded_code(code)}
        if excluded_codes:
            self._logger.info("按前缀规则忽略托盘: %s", sorted(excluded_codes))

        task_tray_codes = {code for code in raw_task_tray_codes if code not in excluded_codes}
        target_codes = {str(code).strip() for code in task_tray_codes if str(code).strip()}

        if not target_codes:
            raise ValidationError(f"任务 {target_task_id} 未找到需要下料的物料托盘位置")

        resource_rows = self.get_resource_info()
        existing_codes = {str(row.get("layout_code") or "").strip() for row in resource_rows if row.get("layout_code")}
        missing_codes = sorted(code for code in target_codes if code not in existing_codes)

        if missing_codes:
            if ignore_missing:
                self._logger.warning("下料位置未在资源列表, 已忽略: %s", missing_codes)
                target_codes = {code for code in target_codes if code not in missing_codes}
            else:
                raise ValidationError(f"下料位置不存在: {missing_codes}")

        if not target_codes:
            raise ValidationError("可执行下料的位置为空, 请检查输入或资源列表")

        self._logger.info(
            "准备批量下料任务物料托盘 task_id=%s  任务托盘=%s  实际下料位置=%s",
            target_task_id,
            sorted(task_tray_codes),
            sorted(target_codes),
        )

        # 构造新的 layout_list 格式，附上 task_id
        layout_list = [{"layout_code": code, "task_id": target_task_id} for code in sorted(target_codes)]
        resp = self.batch_out_tray(
            layout_list,
            move_type=move_type,
            task_id=target_task_id,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s
        )

        return resp

    def batch_out_empty_trays(self, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out") -> JsonDict:
        """
        功能:
            下料当前所有空托盘
        参数:
            poll_interval_s: 轮询空闲状态的时间间隔(秒)
            ignore_missing: True 时忽略未在资源列表中的托盘并记录 warning, False 时抛错终止
            timeout_s: 等待空闲的超时时间(秒)
            move_type: 下料方式, 默认 "main_out"
        返回:
            Dict, 执行 batch_out_tray 的接口响应
        """
        self.wait_idle(stage="等待任务结束", poll_interval_s=poll_interval_s, timeout_s=timeout_s)   #等待任务结束后再提取托盘信息，否则会提取不到

        self._logger.info("准备下料空托盘")

        skip_prefixes = ("MSB", "MS", "AS", "TS")

        def _is_excluded_code(code: Any) -> bool:
            if code is None:
                return False
            text = str(code).strip().upper()
            return any(text.startswith(prefix) for prefix in skip_prefixes)

        empty_trays = self.list_empty_trays()
        raw_empty_codes = {str(item.get("layout_code")).strip() for item in empty_trays if item.get("layout_code")}

        excluded_codes = {code for code in raw_empty_codes if _is_excluded_code(code)}
        if excluded_codes:
            self._logger.info("按前缀规则忽略托盘: %s", sorted(excluded_codes))

        empty_codes = {code for code in raw_empty_codes if code not in excluded_codes}
        target_codes = {str(code).strip() for code in empty_codes if str(code).strip()}

        if not target_codes:
            raise ValidationError("未找到需要下料的空托盘位置")

        resource_rows = self.get_resource_info()
        existing_codes = {str(row.get("layout_code") or "").strip() for row in resource_rows if row.get("layout_code")}
        missing_codes = sorted(code for code in target_codes if code not in existing_codes)

        if missing_codes:
            if ignore_missing:
                self._logger.warning("下料位置未在资源列表, 已忽略: %s", missing_codes)
                target_codes = {code for code in target_codes if code not in missing_codes}
            else:
                raise ValidationError(f"下料位置不存在: {missing_codes}")

        if not target_codes:
            raise ValidationError("可执行下料的位置为空, 请检查输入或资源列表")

        # 构造新的 layout_list 格式，空托盘不附 task_id
        layout_list = [{"layout_code": code} for code in sorted(target_codes)]

        # 构建源位置到目标过渡舱位置的映射，用于日志显示
        default_dst_positions = [
            "TB-2-1", "TB-2-2", "TB-2-3", "TB-2-4",
            "TB-1-1", "TB-1-2", "TB-1-3", "TB-1-4"
        ]
        tray_to_dst_map = {}
        for idx, item in enumerate(layout_list):
            if idx < len(default_dst_positions):
                tray_to_dst_map[item["layout_code"]] = default_dst_positions[idx]

        # 格式化显示：源位置 -> 目标位置
        dst_mapping_str = ", ".join([f"{src}->{dst}" for src, dst in sorted(tray_to_dst_map.items())])

        self._logger.info(
            "准备批量下料空托盘  空托盘=%s  实际下料位置=%s",
            sorted(empty_codes),
            dst_mapping_str,
        )
        resp = self.batch_out_tray(
            layout_list,
            move_type=move_type,
            task_id=None,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s
        )

        return resp

    # ---------- 清空站内资源（慎用） ----------
    def clear_tray_shelf(self) -> JsonDict:
        """
        功能:
            清空站内托盘货架.
        参数:
            无.
        返回:
            Dict, 接口响应.
        """
        return self._call_with_relogin(self._client.clear_tray_shelf)

    # ---------- 开关外舱门 ----------
    def open_close_door(self, op: str, *, station: str = "FSY", door_num: int = 0) -> JsonDict:
        """
        功能:
            打开/关闭过渡舱门
        参数:
            op: "open" 或 "close".
            station: 站点编码，默认 "FSY".
            door_num: 门编号，默认 0.
        返回:
            Dict, 接口响应.
        """
        return self._call_with_relogin(self._client.open_close_door, station, op, door_num)

    # ---------- 控制W1货架 ----------
    def control_w1_shelf(self, position: str, action: str, *, station: str = "FSY") -> JsonDict:
        """
        功能:
            控制W1货架的推出或复位操作
        参数:
            position: str, 货架位置, 可选值: "W-1-1", "W-1-3", "W-1-5", "W-1-7"
                     W-1-1 控制 W-1-1 和 W-1-2
                     W-1-3 控制 W-1-3 和 W-1-4
                     W-1-5 控制 W-1-5 和 W-1-6
                     W-1-7 控制 W-1-7 和 W-1-8
            action: str, 动作类型, "home" 表示复位, "outside" 表示推出
            station: str, 站点编码, 默认 "FSY"
        返回:
            Dict, 接口响应
        """
        # 位置映射到num参数
        position_map = {
            "W-1-1": 1,
            "W-1-3": 3,
            "W-1-5": 5,
            "W-1-7": 7
        }

        if position not in position_map:
            raise ValidationError(f"position 必须是 {list(position_map.keys())} 之一")
        if action not in ("home", "outside"):
            raise ValidationError("action 必须是 'home' 或 'outside'")

        num = position_map[position]
        op = action  # op与action保持一致

        self._logger.info("控制W1货架, position=%s, action=%s, station=%s", position, action, station)
        return self._call_with_relogin(self._client.single_control_w1_shelf, station, action, op, num)

    # ---------- 任务模块 ----------  未完成
    def add_task(self, payload: JsonDict) -> JsonDict:
        resp = self._call_with_relogin(self._client.add_task, payload)

        # 自动保存任务记录
        if self._data_manager:
            # 从响应中提取 task_id
            task_id = resp.get("task_id") or resp.get("result", {}).get("task_id") or resp.get("data", {}).get("task_id")
            if task_id:
                task_id_str = str(task_id)
                self._data_manager.create_task_record(task_id_str, {
                    "task_id": task_id_str,
                    "status": "UNSTARTED",
                    "created_at": datetime.now().isoformat()
                })
                # 保存任务 Payload
                self._data_manager.save_task_payload(task_id_str, payload)

        return resp

    def start_task(self, task_id: Optional[int] = None, *, check_glovebox_env: bool = True, water_limit_ppm: float = 10.0, oxygen_limit_ppm: float = 10.0) -> JsonDict:
        """
        功能:
            确认设备空闲且手套箱环境达标后, 启动指定任务或task_id最大的任务
        参数:
            task_id: 可选int, 指定任务id, None时自动查找task_id最大的任务
            check_glovebox_env: bool, 启动前是否校验手套箱水氧
            water_limit_ppm: float, 手套箱水含量上限(ppm)
            oxygen_limit_ppm: float, 手套箱氧含量上限(ppm)
        返回:
            Dict[str, Any], StartTask接口的响应结果
        """
        state = self.station_state()
        if state != int(StationState.IDLE):
            raise ValidationError("设备未处于空闲状态, 暂不可启动任务")

        if check_glovebox_env is True:
            env_info = self.get_glovebox_env()
            water_raw = env_info.get("water_content")
            oxygen_raw = env_info.get("oxygen_content")
            if water_raw is None or oxygen_raw is None:
                raise ValidationError("手套箱水氧数据缺失, 已停止启动流程")

            def _to_float(value: Any) -> float:
                if isinstance(value, (int, float)):
                    return float(value)
                if isinstance(value, str):
                    text = value.strip()
                    if text != "":
                        try:
                            return float(text)
                        except Exception:
                            pass
                raise ValidationError(f"手套箱环境数值格式异常 {value}")

            water_ppm = _to_float(water_raw)
            oxygen_ppm = _to_float(oxygen_raw)

            if water_ppm >= float(water_limit_ppm):
                raise ValidationError(f"手套箱水含量超限, 当前={water_ppm}ppm, 阈值={water_limit_ppm}ppm")
            if oxygen_ppm >= float(oxygen_limit_ppm):
                raise ValidationError(f"手套箱氧含量超限, 当前={oxygen_ppm}ppm, 阈值={oxygen_limit_ppm}ppm")

        target_task_id = task_id
        if target_task_id is None:
            # 通过任务列表取task_id最大的一条
            first_resp = self.get_task_list(sort="desc", offset=0, limit=1)
            task_sums = self._extract_task_sums(first_resp)
            if task_sums is None:
                task_sums = 50  # 回退拉取最近任务, 避免列表为空
            tasks_resp = self.get_task_list(sort="desc", offset=0, limit=task_sums)
            task_list = (
                tasks_resp.get("task_list")
                or tasks_resp.get("result", {}).get("task_list")
                or tasks_resp.get("data", {}).get("task_list")
            )
            if task_list is None or len(task_list) == 0:
                raise ValidationError("未找到任务记录, 请先创建任务")

            valid_tasks: List[JsonDict] = []
            for item in task_list:
                cur_id = item.get("task_id")
                if isinstance(cur_id, int):
                    valid_tasks.append(item)

            if len(valid_tasks) == 0:
                raise ValidationError("任务列表缺少有效的task_id字段")

            latest_task = max(valid_tasks, key=lambda x: x.get("task_id", -1))
            target_task_id = int(latest_task["task_id"])

            def _parse_status(value: Any) -> Optional[int]:
                if isinstance(value, int):
                    return value
                if isinstance(value, str):
                    text = value.strip()
                    if text != "":
                        try:
                            return int(text)
                        except Exception:
                            return None
                return None

            latest_status = _parse_status(latest_task.get("status"))
            if latest_status is None:
                raise ValidationError(f"任务{target_task_id}缺少有效状态码, 无法自动启动")
            if latest_status != int(TaskStatus.UNSTARTED):
                raise ValidationError(f"task_id={target_task_id}状态非未运行, 不允许自动启动")

        self._logger.info("准备启动任务 task_id=%s", target_task_id)
        resp = self._call_with_relogin(self._client.start_task, int(target_task_id))
        self._logger.info("任务启动请求已提交 task_id=%s", target_task_id)

        # 自动更新任务状态
        if self._data_manager:
            self._data_manager.update_task_status(
                str(target_task_id),
                "RUNNING",
                started_at=datetime.now().isoformat()
            )

        return resp

    def stop_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.stop_task, task_id)

    def cancel_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.cancel_task, task_id)

    def delete_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.delete_task, task_id)
    
    def get_task_info(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.get_task_info, task_id)

    def get_task_list(
        self,
        *,
        sort: str = "desc",
        offset: int = 0,
        limit: int = 20,
        status: Optional[List[int]] = None,
    ) -> JsonDict:
        """
        功能:
            获取任务列表, 对应 GetTaskList
        参数:
            sort: 排序方式, 默认按创建时间倒序
            offset: 数据起点
            limit: 数据限制
            status: 任务状态列表, 例如 [0, 1]
        返回:
            Dict, 接口响应
        """
        body: JsonDict = {
            "sort": sort,
            "offset": offset,
            "limit": limit,
        }
        if status is not None:
            body["status"] = status  # 传递状态过滤
        return self._call_with_relogin(self._client.get_task_list, body)

    def _extract_task_sums(self, resp: JsonDict) -> Optional[int]:
        """
        功能:
            从任务列表响应中提取 task_sums 总数
        参数:
            resp: 任务列表接口响应
        返回:
            Optional[int], 任务总数
        """
        if "task_sums" in resp and isinstance(resp.get("task_sums"), int):
            return int(resp["task_sums"])
        for outer in ("result", "data"):
            outer_obj = resp.get(outer)
            if isinstance(outer_obj, dict) and isinstance(outer_obj.get("task_sums"), int):
                return int(outer_obj["task_sums"])
        return None

    def get_all_tasks(self) -> JsonDict:
        """
        功能:
            获取全部任务列表, 先用一次 GetTaskList 读取 task_sums, 再用 limit 拉全量
        参数:
            无
        返回:
            Dict, 包含完整任务列表
        """
        first_resp = self.get_task_list(limit=1, offset=0, sort="desc")
        task_sums = self._extract_task_sums(first_resp)
        if task_sums is None:
            raise ValidationError(f"GetTaskList 未返回 task_sums, resp={first_resp}")
        self._logger.info("开始获取全部任务列表, total=%s", task_sums)  # 记录预期条数
        return self.get_task_list(limit=task_sums, offset=0, sort="desc")

    def _extract_task_status(self, task_info: JsonDict) -> Optional[int]:
        """
        功能:
            从 GetTaskInfo 返回中提取 status, 兼容不同字段层级。
        参数:
            task_info: 任务详情响应.
        返回:
            Optional[int], 解析出的状态码.
        """
        if "status" in task_info and isinstance(task_info.get("status"), int):
            return int(task_info["status"])

        for key in ("result", "data"):
            obj = task_info.get(key)
            if isinstance(obj, dict) and isinstance(obj.get("status"), int):
                return int(obj["status"])

        return None

    def wait_task_with_ops(self, task_id: Optional[int] = None, *, poll_interval_s: float = 2.0) -> int:
        """
        功能:
            轮询指定或自动选取的运行中任务直至完成, 并按新增步骤增量输出操作进度
        参数:
            task_id: 可选int, 指定任务id, None时自动选择状态为RUNNING的任务
            poll_interval_s: float, 轮询间隔秒
        返回:
            int, 任务最终状态码
        """
        target_task_id = task_id
        if target_task_id is None:
            retry_count = 0
            running_ids: List[int] = []

            while retry_count < 3:
                running_info = self.get_task_list(status=[int(TaskStatus.RUNNING)], sort="desc", offset=0, limit=20)
                task_list = (
                    running_info.get("task_list")
                    or running_info.get("result", {}).get("task_list")
                    or running_info.get("data", {}).get("task_list")
                )

                running_ids.clear()
                if isinstance(task_list, list) is True:
                    for item in task_list:
                        cur_id = item.get("task_id")
                        cur_status = item.get("status")
                        if isinstance(cur_id, int) is True and cur_status == int(TaskStatus.RUNNING):
                            running_ids.append(cur_id)

                if len(running_ids) > 0:
                    target_task_id = max(running_ids)
                    break

                retry_count += 1
                if retry_count < 3:
                    time.sleep(10)

            if target_task_id is None:
                msg = "未找到运行中的任务"
                self._logger.error(msg)
                raise ValidationError(msg)

        self._logger.info("开始监控任务 %s 运行进度", target_task_id)

        start_ts = time.time()
        seen_steps: Dict[str, bool] = {}
        first_output = True

        def _format_steps(op_info: JsonDict) -> List[str]:
            result_steps: List[str] = []
            if isinstance(op_info, dict) is False:
                return result_steps
            for key in ("done_units", "running_units"):
                units = op_info.get(key)
                if isinstance(units, list) is False:
                    continue
                for unit_obj in units:
                    if isinstance(unit_obj, dict) is False:
                        continue
                    for unit_name, steps in unit_obj.items():
                        if isinstance(steps, list) is False:
                            continue
                        for step_item in steps:
                            if isinstance(step_item, list) is False:
                                continue
                            action = step_item[0] if len(step_item) >= 1 else ""
                            target = step_item[1] if len(step_item) >= 2 else ""
                            action_text = str(action).strip()
                            target_text = str(target).strip()
                            if target_text != "":
                                result_steps.append(f"{unit_name}: {action_text} -> {target_text}")
                            else:
                                result_steps.append(f"{unit_name}: {action_text}")
            return result_steps

        while True:
            info = self.get_task_info(int(target_task_id))
            status = self._extract_task_status(info)
            if status == int(TaskStatus.COMPLETED):
                self._logger.info("任务 %s 已完成", target_task_id)

                # 自动更新任务状态为完成
                if self._data_manager:
                    self._data_manager.update_task_status(
                        str(target_task_id),
                        "COMPLETED",
                        completed_at=datetime.now().isoformat()
                    )

                return int(status)
            if status in (int(TaskStatus.FAILED), int(TaskStatus.STOPPED)):
                self._logger.warning("任务 %s 已结束但未完成, status=%s", target_task_id, status)

                # 自动更新任务状态为失败或停止
                if self._data_manager:
                    status_name = TaskStatus(status).name if status in TaskStatus._value2member_map_ else "UNKNOWN"
                    self._data_manager.update_task_status(
                        str(target_task_id),
                        status_name,
                        completed_at=datetime.now().isoformat()
                    )

                return int(status)

            op_info = self._call_with_relogin(self._client.get_task_op_info, int(target_task_id))
            step_texts = _format_steps(op_info)
            new_steps: List[str] = []
            for step in step_texts:
                if step not in seen_steps:
                    new_steps.append(step)
                    seen_steps[step] = True

            if first_output is True:
                if len(step_texts) > 0:
                    self._logger.info("任务 %s 已执行步骤:", target_task_id)
                    for text in step_texts:
                        self._logger.info("%s", text)
                first_output = False
            else:
                if len(new_steps) > 0:
                    for text in new_steps:
                        self._logger.info("%s", text)

            time.sleep(poll_interval_s)

    # ---------- 任务json生成  ----------
    def build_task_payload(
        self,
        params: Dict[str, Any],
        headers: List[str],
        data_rows: List[List[Any]],
        chemical_db: Dict[str, Any]
    ) -> JsonDict:
        """
        功能:
            将结构化的实验数据转换为 AddTask API 请求体.
            兼容同一试剂列中同时出现固体与液体的情况: 若检测到混用, 自动拆分为多个虚拟列(固体/液体/其他, 可选磁子列),
            以保证后续加料排序与布局行号映射正确.
        参数:
            params: Dict[str, Any], 实验全局参数(反应时间、温度、反应规模、反应器类型、内标信息等).
            headers: List[str], 实验数据表头列表.
            data_rows: List[List[Any]], 实验数据行(每行为值列表).
            chemical_db: Dict[str, Any], 化学品信息字典.
        返回:
            JsonDict, AddTask 请求体.
        """

        def _safe_float(value: Any, default_val: float) -> float:
            try:
                return float(str(value).replace("%", "").replace("mmol", "").strip())
            except Exception:
                return default_val

        def _chem_kind(chem_info: Dict[str, Any]) -> str:
            # 统一将物态归一为 liquid/solid/other, 便于拆列与排序策略一致.
            state_text = str(chem_info.get("physical_state", "")).lower()
            if "liquid" in state_text:
                return "liquid"
            if "solid" in state_text:
                return "solid"
            return "other"

        def _to_ml(amount_val: float, amount_unit: str) -> float:
            # 仅在单位是体积时换算为 mL, 用于液体列按最大体积排序.
            unit_text = str(amount_unit).strip().lower()
            if unit_text == "ml":
                return amount_val
            if unit_text in ["ul", "µl"]:
                return amount_val / 1000.0
            return 0.0

        weighing_error_pct = _safe_float(params.get("称量误差(%)", 1), 1.0)
        max_error_mg = _safe_float(params.get("最大称量误差(mg)", 1), 1.0)
        reaction_scale_mmol = _safe_float(params.get("反应规模(mmol)", 0), 0.0)
        reactor_type = str(params.get("反应器类型", "")).strip()

        auto_magnet = str(params.get("自动加磁子", "是")).strip() == "是"
        fixed_order = str(params.get("固定加料顺序", "否")).strip() == "是"
        exp_count = len(data_rows)

        if exp_count not in [12, 24, 36, 48]:
            self._logger.warning(f"实验数量 {exp_count} 非标准(12/24/36/48).")

        col_metadata: List[Dict[str, Any]] = []
        col_idx = 0
        next_virtual_col_idx = -1000  # 负数虚拟列索引, 避免与真实列冲突.

        while col_idx < len(headers):
            header_text = str(headers[col_idx])

            if "试剂" in header_text:
                name_col_idx = col_idx
                amt_col_idx = col_idx + 1 if (col_idx + 1) < len(headers) else None

                has_liquid = False
                has_solid = False
                has_other = False
                has_magnet_manual = False
                max_liquid_vol_ml = 0.0

                for row_vals in data_rows:
                    if name_col_idx >= len(row_vals):
                        continue

                    chem_name = str(row_vals[name_col_idx]).strip()
                    if chem_name == "" or chem_name == "0":
                        continue

                    if chem_name == "加磁子":
                        has_magnet_manual = True
                        continue

                    if chem_name not in chemical_db:
                        # 此处仅用于列级扫描, 不直接抛错, 具体实验行处理时再校验并报错更准确.
                        has_other = True
                        continue

                    chem_info = chemical_db[chem_name]
                    kind = _chem_kind(chem_info)

                    if kind == "liquid":
                        has_liquid = True
                        amt_text = "0"
                        if amt_col_idx is not None and amt_col_idx < len(row_vals):
                            amt_text = str(row_vals[amt_col_idx])

                        amt_val, amt_unit = self._split_amount_unit(amt_text)
                        vol_ml = _to_ml(amt_val, amt_unit)
                        if vol_ml > max_liquid_vol_ml:
                            max_liquid_vol_ml = vol_ml

                    elif kind == "solid":
                        has_solid = True
                    else:
                        has_other = True

                if has_liquid is True and has_solid is True:
                    self._logger.debug(f"检测到试剂列混用固体与液体, 将拆分虚拟列, 原列索引={name_col_idx}.")

                    # 固体虚拟列.
                    col_metadata.append({
                        "col_idx": next_virtual_col_idx,
                        "src_col_idx": name_col_idx,
                        "src_amt_idx": amt_col_idx,
                        "type": "solid",
                        "split_kind": "solid",
                        "max_vol": 0.0,
                        "is_reagent_group": True,
                        "is_magnet_only": False,
                    })
                    next_virtual_col_idx -= 1

                    # 磁子虚拟列(仅在该试剂列出现过“加磁子”时创建, 避免重复添加).
                    if has_magnet_manual is True:
                        col_metadata.append({
                            "col_idx": next_virtual_col_idx,
                            "src_col_idx": name_col_idx,
                            "src_amt_idx": None,
                            "type": "magnet_manual",
                            "split_kind": None,
                            "max_vol": 0.0,
                            "is_reagent_group": False,
                            "is_magnet_only": True,
                        })
                        next_virtual_col_idx -= 1

                    # 液体虚拟列.
                    col_metadata.append({
                        "col_idx": next_virtual_col_idx,
                        "src_col_idx": name_col_idx,
                        "src_amt_idx": amt_col_idx,
                        "type": "liquid",
                        "split_kind": "liquid",
                        "max_vol": max_liquid_vol_ml,
                        "is_reagent_group": True,
                        "is_magnet_only": False,
                    })
                    next_virtual_col_idx -= 1

                    # 其他虚拟列(兜底未知物态, 避免数据丢失).
                    if has_other is True:
                        col_metadata.append({
                            "col_idx": next_virtual_col_idx,
                            "src_col_idx": name_col_idx,
                            "src_amt_idx": amt_col_idx,
                            "type": "other",
                            "split_kind": "other",
                            "max_vol": 0.0,
                            "is_reagent_group": True,
                            "is_magnet_only": False,
                        })
                        next_virtual_col_idx -= 1

                else:
                    # 未混用时保持原逻辑, 仍可基于列内最大体积做液体排序.
                    final_type = "other"
                    if has_magnet_manual is True:
                        final_type = "magnet_manual"
                    elif has_liquid is True:
                        final_type = "liquid"
                    elif has_solid is True:
                        final_type = "solid"

                    col_metadata.append({
                        "col_idx": name_col_idx,
                        "src_col_idx": name_col_idx,
                        "src_amt_idx": amt_col_idx,
                        "type": final_type,
                        "split_kind": None,
                        "max_vol": max_liquid_vol_ml if final_type == "liquid" else 0.0,
                        "is_reagent_group": True,
                        "is_magnet_only": False,
                    })

                # 试剂列默认占用(名称+用量)两列.
                col_idx += 2
                continue

            if "加磁子" in header_text:
                col_metadata.append({
                    "col_idx": col_idx,
                    "src_col_idx": col_idx,
                    "src_amt_idx": None,
                    "type": "magnet_manual",
                    "split_kind": None,
                    "max_vol": 0.0,
                    "is_reagent_group": False,
                    "is_magnet_only": True,
                })
                col_idx += 1
                continue

            col_idx += 1

        VIRTUAL_MAGNET_COL_IDX = -999
        ordered_cols: List[Dict[str, Any]] = []

        if fixed_order is False:
            solids = [c for c in col_metadata if c["type"] == "solid"]
            manual_magnets = [c for c in col_metadata if c["type"] == "magnet_manual"]
            liquids = [c for c in col_metadata if c["type"] == "liquid"]
            liquids.sort(key=lambda x: x.get("max_vol", 0.0), reverse=True)
            others = [c for c in col_metadata if c["type"] not in ["solid", "liquid", "magnet_manual"]]

            ordered_cols.extend(solids)

            if auto_magnet is True:
                ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})

            ordered_cols.extend(manual_magnets)
            ordered_cols.extend(liquids)
            ordered_cols.extend(others)

        else:
            inserted_magnet = False
            for col_item in col_metadata:
                if auto_magnet is True and inserted_magnet is False and col_item["type"] == "liquid":
                    ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})
                    inserted_magnet = True
                ordered_cols.append(col_item)

            if auto_magnet is True and inserted_magnet is False:
                ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})

        col_to_row_map: Dict[int, int] = {}
        curr_row = 0
        for col_item in ordered_cols:
            col_to_row_map[col_item["col_idx"]] = curr_row
            curr_row += 1

        ROW_IDX_REACTION = curr_row + 1
        ROW_IDX_INT_STD = curr_row + 2
        ROW_IDX_STIR_AFTER = curr_row + 3
        ROW_IDX_FILTER = curr_row + 4

        layout_list: List[Dict[str, Any]] = []
        common_fields = {
            "layout_code": "",
            "src_layout_code": "",
            "resource_type": "551000502",
            "status": 0,
            "tray_QR_code": "",
            "QR_code": "",
        }

        for exp_idx, row_vals in enumerate(data_rows):
            unit_column = exp_idx

            for col_item in ordered_cols:
                col_key = col_item["col_idx"]
                target_row = col_to_row_map[col_key]

                if col_key == VIRTUAL_MAGNET_COL_IDX:
                    # 自动磁子: 仅当该实验行未显式写“加磁子”时才补一个.
                    has_explicit = False
                    for raw_val in row_vals:
                        if str(raw_val).strip() == "加磁子":
                            has_explicit = True
                            break
                    if has_explicit is False:
                        self._add_unit_magnet(layout_list, common_fields, unit_column, target_row)
                    continue

                src_col_idx = col_item.get("src_col_idx", None)
                if src_col_idx is None:
                    continue
                if src_col_idx >= len(row_vals):
                    continue

                val_name = str(row_vals[src_col_idx]).strip()
                if val_name == "" or val_name == "0":
                    continue

                if val_name == "加磁子":
                    # 拆列后仅由磁子专用虚拟列处理, 避免同一行重复加磁子.
                    if col_item.get("is_magnet_only", False) is True:
                        self._add_unit_magnet(layout_list, common_fields, unit_column, target_row)
                    elif col_item.get("split_kind", None) is None:
                        self._add_unit_magnet(layout_list, common_fields, unit_column, target_row)
                    continue

                if col_item.get("is_magnet_only", False) is True:
                    continue

                if val_name not in chemical_db:
                    raise ValidationError(f"实验 {exp_idx + 1}: 未知化学品 '{val_name}'.")

                chem_info = chemical_db[val_name]
                chem_kind = _chem_kind(chem_info)

                split_kind = col_item.get("split_kind", None)
                if split_kind is not None and chem_kind != split_kind:
                    continue

                if col_item.get("is_reagent_group", False) is True:
                    src_amt_idx = col_item.get("src_amt_idx", None)
                    amt_text = "0"
                    if src_amt_idx is not None and src_amt_idx < len(row_vals):
                        amt_text = str(row_vals[src_amt_idx])

                    amt_val, amt_unit = self._split_amount_unit(amt_text)
                    if amt_val > 0:
                        self._add_reagent_unit(
                            layout_list,
                            common_fields,
                            unit_column,
                            target_row,
                            val_name,
                            chem_info,
                            amt_val,
                            amt_unit,
                            weighing_error_pct,
                            max_error_mg,
                            reaction_scale_mmol,
                        )

            if reactor_type != "":
                self._add_reaction_unit(layout_list, common_fields, unit_column, ROW_IDX_REACTION, params)
            else:
                # 反应器未配置时跳过反应搅拌单元.
                self._logger.debug("反应器类型为空, 跳过反应搅拌单元 exp=%s", exp_idx + 1)

            std_name = str(params.get("内标种类", "")).strip()
            if std_name != "":
                self._add_internal_std_unit(
                    layout_list,
                    common_fields,
                    unit_column,
                    ROW_IDX_INT_STD,
                    std_name,
                    chemical_db,
                    params,
                    weighing_error_pct,
                    max_error_mg,
                )

                stir_t = str(params.get("加入内标后搅拌时间(min)", "")).strip()
                if stir_t != "":
                    self._add_stir_unit(
                        layout_list,
                        common_fields,
                        unit_column,
                        ROW_IDX_STIR_AFTER,
                        float(stir_t),
                        params,
                    )
            else:
                # 内标未配置时不做内标后搅拌.
                self._logger.debug("内标种类为空, 跳过内标后搅拌 exp=%s", exp_idx + 1)

            dil_name = str(params.get("稀释液种类", "")).strip()
            if dil_name != "":
                self._add_filter_unit(
                    layout_list,
                    common_fields,
                    unit_column,
                    ROW_IDX_FILTER,
                    dil_name,
                    chemical_db,
                    params,
                )

        return {
            "task_id": 0,
            "task_name": str(params.get("实验名称", "AutoTask")),
            "layout_list": layout_list,
            "task_setup": {
                "subtype": None,
                "experiment_num": exp_count,
                "vessel": "551000502",
                "added_slots": "",
            },
            "is_audit_log": 1,
            "is_copy": False,
        }

    # ---------- 任务生成辅助函数：添加各类 Unit----------
    def _add_reagent_unit(self, layout_list: List[Dict], common_fields: Dict, col: int, row: int, 
                          name: str, chem_info: Dict, amt_val: float, amt_unit: str, error_pct: float, max_error_mg: float, reaction_scale_mmol: float) -> None:
        """
        功能:
            添加加粉或加液操作单元, 支持 eq 换算, 溶液/树脂的 active_content 换算.
        参数:
            layout_list: List[Dict], 任务布局列表.
            common_fields: Dict, 通用字段模板.
            col: int, 单元所在列号.
            row: int, 单元所在行号.
            name: str, 试剂名称.
            chem_info: Dict, 化学品信息.
            amt_val: float, 配方数值.
            amt_unit: str, 配方单位(eq/mmol/mg/mL等).
            error_pct: float, 称量误差百分比.
            max_error_mg: float, 最大偏差 mg.
            reaction_scale_mmol: float, 反应规模 mmol, eq 换算所需.
        返回:
            无.
        """
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}"
        })

        state = chem_info.get('physical_state', 'unknown').lower()
        physical_form = str(chem_info.get('physical_form', '') or '').lower()
        active_content = chem_info.get('active_content')
        mw = float(chem_info.get('molecular_weight', 0) or 0)
        density = float(chem_info.get('density (g/mL)', 0) or 0)
        
        amt_unit_lower = str(amt_unit or "").lower()
        target_mmol = None

        if amt_unit_lower == "eq":
            if reaction_scale_mmol <= 0:
                self._logger.error("反应规模(mmol)未填写, 无法换算eq: %s", name)
                raise ValidationError("反应规模(mmol)未填写, 无法换算eq当量")
            target_mmol = amt_val * reaction_scale_mmol
            amt_val = target_mmol
            amt_unit_lower = "mmol"
        elif amt_unit_lower == "mmol":
            target_mmol = amt_val

        # 溶液: active_content 视为 mmol/mL, 换算体积
        if target_mmol is not None and physical_form == "solution":
            volume_ml = self._convert_active_content_to_volume(
                target_mmol=target_mmol,
                active_content=active_content,
                molecular_weight=mw,
                density=density,
                physical_form=physical_form,
                substance=name
            )
            unit_dict.update({
                "unit_type": "exp_pipetting",
                "process_json": {
                    "custom": {"unit": "mL", "unitOptions": ["mL", "L"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_volume": round(volume_ml, 3)
                }
            })
            layout_list.append(unit_dict)
            return

        # 树脂(beads): active_content 视为 wt%，按摩尔需求折算出总质量，走加粉
        if target_mmol is not None and physical_form == "beads":
            content_type, content_value = self._parse_active_content(active_content, physical_form)
            if content_type != "wt_percent" or content_value <= 0:
                self._logger.error("试剂 %s 的active_content(wt%%)无效", name)
                raise ValidationError(f"{name} 的active_content(wt%)无效")
            if mw <= 0:
                self._logger.error("试剂 %s 缺少分子量, 无法按wt%%换算质量", name)
                raise ValidationError(f"{name} 缺少分子量, 无法按wt%换算质量")
            active_mass_mg = target_mmol * mw
            total_mass_mg = active_mass_mg / (content_value / 100.0)
            calc_offset = total_mass_mg * (error_pct / 100.0)
            final_offset = max(0.1, min(calc_offset, max_error_mg))
            unit_dict.update({
                "unit_type": "exp_add_powder",
                "process_json": {
                    "offset": round(final_offset, 1),
                    "custom": {"unit": "mg", "unitOptions": ["mg", "g"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_weight": round(total_mass_mg, 1)
                }
            })
            layout_list.append(unit_dict)
            return
        
        # 原有固体逻辑
        if 'solid' in state:
            target_mg = 0.0
            if amt_unit_lower == 'mmol':
                target_mg = amt_val * mw
            elif amt_unit_lower == 'g':
                target_mg = amt_val * 1000.0
            elif amt_unit_lower == 'mg':
                target_mg = amt_val
            
            calc_offset = target_mg * (error_pct / 100.0)
            final_offset = max(0.1, min(calc_offset, max_error_mg))

            unit_dict.update({
                "unit_type": "exp_add_powder",
                "process_json": {
                    "offset": round(final_offset, 1),
                    "custom": {"unit": "mg", "unitOptions": ["mg", "g"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_weight": round(target_mg, 1)
                }
            })
            
        elif 'liquid' in state:
            target_vol_ml = 0.0
            if amt_unit_lower == 'mmol':
                mass_mg = amt_val * mw
                if density > 0:
                    target_vol_ml = (mass_mg / 1000.0) / density
            elif amt_unit_lower == 'ml':
                target_vol_ml = amt_val
            elif amt_unit_lower in ('μl', 'ul'):
                target_vol_ml = amt_val / 1000.0

            unit_dict.update({
                "unit_type": "exp_pipetting",
                "process_json": {
                    "custom": {"unit": "mL","unitOptions": ["mL","L"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_volume": round(target_vol_ml, 3)
                }
            })
        layout_list.append(unit_dict)

    def _parse_active_content(self, value: Any, physical_form: str) -> Tuple[str, float]:
        """
        功能:
            解析 active_content 字段, 结合物理形态区分 mmol/mL 与 wt%.
        参数:
            value: Any, active_content 原始值.
            physical_form: str, 物理形态(solution/beads等).
        返回:
            Tuple[str, float], (类型标记, 数值), 无法解析返回 ("", 0.0).
        """
        form = (physical_form or "").lower().strip()
        if value is None:
            return "", 0.0
        text_raw = str(value).strip()
        if text_raw == "":
            return "", 0.0

        try:
            num_val = float(value)
        except Exception:
            text_norm = text_raw.lower()
            numbers = re.findall(r"[0-9]+(?:\\.[0-9]+)?", text_norm)
            num_val = float(numbers[0]) if len(numbers) > 0 else 0.0

        if form == "solution":
            return "mmol_per_ml", num_val
        if form == "beads":
            return "wt_percent", num_val

        text = text_raw.lower()
        if "mmol/ml" in text or "mmol per ml" in text or "mmolml" in text:
            return "mmol_per_ml", num_val
        if "wt%" in text or "wt percent" in text or "wt" in text:
            return "wt_percent", num_val
        if num_val > 0:
            return "mmol_per_ml", num_val
        return "", 0.0

    def _convert_active_content_to_volume(self, target_mmol: float, active_content: Any, molecular_weight: float, density: float, physical_form: str, substance: str) -> float:
        """
        功能:
            按 active_content 换算所需移取体积(mL).
        参数:
            target_mmol: float, 目标有效摩尔数.
            active_content: Any, active_content 原始值.
            molecular_weight: float, 分子量, wt% 换算时使用.
            density: float, 密度(g/mL), wt% 换算体积时使用.
            physical_form: str, 物理形态(solution/beads等).
            substance: str, 试剂名称, 用于提示.
        返回:
            float, 需要的体积(mL).
        """
        content_type, content_value = self._parse_active_content(active_content, physical_form)
        if content_type == "":
            self._logger.error("试剂 %s 缺少active_content, 无法按mmol换算体积", substance)
            raise ValidationError(f"{substance} 缺少active_content, 无法按mmol换算体积")

        if content_type == "mmol_per_ml":
            if content_value <= 0:
                self._logger.error("试剂 %s 的active_content(mmol/mL)无效", substance)
                raise ValidationError(f"{substance} 的active_content(mmol/mL)无效")
            return target_mmol / content_value

        if content_type == "wt_percent":
            if molecular_weight <= 0:
                self._logger.error("试剂 %s 缺少分子量, 无法按wt%%换算", substance)
                raise ValidationError(f"{substance} 缺少分子量, 无法按wt%换算")
            if density <= 0:
                self._logger.error("试剂 %s 缺少密度, 无法按wt%%换算体积", substance)
                raise ValidationError(f"{substance} 缺少密度, 无法按wt%换算体积")
            active_mass_mg = target_mmol * molecular_weight
            total_mass_mg = active_mass_mg / (content_value / 100.0)
            return total_mass_mg / 1000.0 / density

        self._logger.error("试剂 %s 的active_content单位未支持: %s", substance, active_content)
        raise ValidationError(f"{substance} 的active_content单位未支持: {active_content}")

    def _add_unit_magnet(self, layout_list, common_fields, col, row):
        """功能: 添加加磁子操作"""
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_add_magnet",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {"custom": {"unit": ""}}
        })
        layout_list.append(unit_dict)

    def _add_reaction_unit(self, layout_list: List[JsonDict], common_fields: JsonDict, col: int, row: int, params: JsonDict) -> None:
        """
        功能:
            添加反应操作单元 (Unit), 处理温度与加热状态.
        参数:
            layout_list: List[JsonDict], 任务布局列表.
            common_fields: JsonDict, 通用字段模板.
            col: int, 单元所在列号.
            row: int, 单元所在行号.
            params: JsonDict, 实验参数字典.
        返回:
            无.
        """
        rxn_temp_raw = params.get("反应温度(°C)")
        
        # Determine target temperature from params
        tgt_temp_raw = None
        for key in params.keys():
            if "搅拌后" in str(key) and "温度" in str(key):
                tgt_temp_raw = params[key]
                break

        rxn_time_h = float(params.get("反应时间(h)", 0))
        rxn_rpm = int(params.get("转速(rpm)", 0))
        is_wait = str(params.get("等待目标温度", "否")) == "是"

        process_data = {
            "rotation_speed": rxn_rpm,
            "reaction_duration": int(rxn_time_h * 3600),
            "is_wait": is_wait,
            "custom": {"unit": ""}
        }

        # 1. Reaction Temperature logic: Replace pd.isna with None/Empty check
        if rxn_temp_raw is None or str(rxn_temp_raw).strip() == "":
             process_data["temperature"] = 25
        else:
             process_data["temperature"] = float(rxn_temp_raw)

        # 2. Target Temperature logic: Replace pd.isna with None/Empty check
        if tgt_temp_raw is None or str(tgt_temp_raw).strip() == "":
            process_data["is_heating"] = False
        else:
            try:
                target_t = float(tgt_temp_raw)
                process_data["is_heating"] = True
                process_data["target_temperature"] = target_t
            except ValueError:
                process_data["is_heating"] = False

        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_magnetic_stirrer",
            "unit_column": col,
            "unit_row": row,
            "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": process_data
        })
        layout_list.append(unit_dict)

    def _add_internal_std_unit(self, layout_list: List[JsonDict], common_fields: JsonDict, col: int, row: int, name: str, db: Dict[str, Any], params: JsonDict, error_pct: float, max_error_mg: float) -> None:
        """
        功能:
            添加内标加料单元.
        参数:
            layout_list: List[JsonDict], 任务布局列表.
            common_fields: JsonDict, 通用字段模板.
            col/row: int, 坐标.
            name: str, 内标物质名称.
            db: Dict, 化学品数据库.
            params: JsonDict, 参数.
            error_pct: float, 允许误差百分比.
        返回:
            无.
        """
        if name not in db:
            return
            
        chem_info = db[name]
        state = chem_info.get('physical_state', 'unknown').lower()
        chem_id = chem_info['chemical_id']
        
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_column": col,
            "unit_row": row,
            "unit_id": f"unit-{uuid.uuid4().hex[:8]}"
        })

        if 'solid' in state:
            target_mg = float(params.get("内标用量(μL/mg)", 10.0))
            calc_offset = target_mg * (error_pct / 100.0)
            final_offset = max(0.1, min(calc_offset, min(calc_offset, max_error_mg)))
            
            unit_dict.update({
                "unit_type": "exp_add_powder",
                "process_json": {
                    "offset": round(final_offset, 1),
                    "custom": {"unit": "mg", "unitOptions": ["mg", "g"]},
                    "substance": name,
                    "chemical_id": chem_id,
                    "add_weight": round(target_mg, 1)
                }
            })
        elif 'liquid' in state:
            target_vol_ml = 0.1 
            
            # Replacement for pd.notna: Check if key exists and value is not empty
            val_ul = params.get("内标用量(μL/mg)")

            if val_ul is not None and str(val_ul).strip() != "":
                target_vol_ml = float(val_ul) / 1000.0

            unit_dict.update({
                "unit_type": "exp_pipetting",
                "process_json": {
                    "custom": {"unit": "mL","unitOptions": ["mL","µL","L"]},
                    "substance": name,
                    "chemical_id": chem_id,
                    "add_volume": round(target_vol_ml,3)
                }
            })
        layout_list.append(unit_dict)

    def _add_stir_unit(self, layout_list, common_fields, col, row, time_min, params):
        """功能: 添加搅拌"""
        rxn_rpm = int(params.get("转速(rpm)", 600))
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_magnetic_stirrer",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {
                "temperature": 25, "rotation_speed": rxn_rpm, "reaction_duration": int(time_min * 60),
                "is_wait": False, "is_heating": False, "target_temperature": 25, "custom": {"unit": ""}
            }
        })
        layout_list.append(unit_dict)

    def _add_filter_unit(self, layout_list, common_fields, col, row, diluent_name, db, params):
        """功能: 添加过滤"""
        if diluent_name not in db: return
        chem_id = db[diluent_name]['chemical_id']
        dilution_vol_ul = float(params.get("稀释量(μL)", 0))
        sample_vol_ul = float(params.get("取样量(μL)", 0))

        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_filtering_sample",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {
                "single_press_num": 6, "substance": diluent_name, "chemical_id": chem_id,
                "add_volume": dilution_vol_ul/1000, "sampling_volume": sample_vol_ul/1000 
            }
        })
        layout_list.append(unit_dict)
  
    def _parse_amount_string(self, amt_str: Any) -> Tuple[float, str]:
        """
        功能:
            解析 '100mg', '5 mmol' 等字符串, 分离数值与单位.
        参数:
            amt_str: Any, 输入的金额字符串或数值.
        返回:
            Tuple[float, str], (数值, 单位).
        """
        # Replacement for pd.isna: check None or empty string
        if amt_str is None:
            return 0, ""
        
        text = str(amt_str).strip().lower()
        if text == "" or text == "0":
            return 0, ""
        
        # Regex matching number + unit
        match = re.match(r"([0-9.]+)\s*([a-z%]+)", text)
        if match:
            return float(match.group(1)), match.group(2)
        
        try:
            return float(text), "unknown"
        except Exception:
            return 0, "error"

    # ---------- 消息通知与故障恢复 ----------
    def notice(self, types: Optional[List[int]] = None) -> JsonDict:
        return self._call_with_relogin(self._client.notice, types)

    def fault_recovery(
        self,
        *,
        ids: Optional[List[int]] = None,
        recovery_type: int = 0,
        resume_task: int = 1,
    ) -> JsonDict:
        return self._call_with_relogin(
            self._client.fault_recovery,
            ids=ids,
            recovery_type=recovery_type,
            resume_task=resume_task,
        )

    # ---------- 方法模块 ---------- 弃用
    def create_method(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.create_method, payload)

    def update_method(self, task_template_id: int, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.update_method, task_template_id, payload)

    def delete_method(self, task_template_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.delete_method, task_template_id)

    def get_method_detail(self, task_template_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.get_method_detail, task_template_id)

    def get_method_list(self, *, limit: int = 20, offset: int = 0, sort: str = "desc") -> JsonDict:
        return self._call_with_relogin(self._client.get_method_list, limit=limit, offset=offset, sort=sort)

    def get_latest_method_detail(self) -> JsonDict:
        """
        功能:
            获取最近一个方法详情, 通过 list(limit=1) 再 detail 的方式实现。
        参数:
            无.
        返回:
            Dict, 方法详情.
        """
        lst = self.get_method_list(limit=1, offset=0, sort="desc")
        data = lst.get("result") or lst.get("data") or lst
        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            raise ValidationError(f"方法列表为空, resp={lst}")
        tid = items[0].get("task_template_id")
        if not isinstance(tid, int):
            raise ValidationError(f"无法解析 task_template_id, item={items[0]}")
        return self.get_method_detail(int(tid))

    #---------- 资源核实 ---------- 
    def analyze_resource_readiness(
        self,
        task_payload: JsonDict,
        resource_rows: List[JsonDict],
        chemical_db: Dict[str, Dict[str, Any]],
        task_id: Optional[int] = None,
    ) -> JsonDict:
        """
        功能:
            基于任务配置与站内资源统计药品与耗材需求, 对比库存给出缺口与冗余, 并跳过以 TB 开头的过渡舱资源.
        参数:
            task_payload: Dict, build_task_payload 生成的任务数据.
            resource_rows: List[Dict], get_resource_info 的返回值.
            chemical_db: Dict[str, Dict[str, Any]], 化学品密度与物态数据, 用于单位换算.
            task_id: Optional[int], 实验ID, 用于二次校验任务资源, 默认为 None.
        返回:
            Dict[str, Any], 包含耗材需求、药品需求、库存差值、缺失与冗余列表.
        """
        layout_list = task_payload.get("layout_list") or []
        experiment_num = int(task_payload.get("task_setup", {}).get("experiment_num", 0))

        def _safe_float(val: Any) -> float:
            try:
                return float(val)
            except Exception:
                return 0.0

        def _normalize_unit(value: float, unit: str, state: str) -> Tuple[str, float]:
            unit_l = (unit or "").lower().strip()
            if unit_l in ("mg", "g"):
                if unit_l == "g":
                    return "mg", value * 1000
                return "mg", value
            if unit_l in ("l", "ml", "ul", "μl", "µl"):
                if unit_l == "l":
                    return "ml", value * 1000
                if unit_l in ("ul", "μl", "µl"):
                    return "ml", value / 1000
                return "ml", value
            if "liquid" in state:
                return "ml", value
            if "solid" in state:
                return "mg", value
            return "", value

        def _convert_amount(from_kind: str, to_kind: str, value: float, density: Any) -> float:
            try:
                dens = float(density)
            except Exception:
                dens = 0.0
            if dens <= 0:
                return 0.0
            if from_kind == "mg" and to_kind == "ml":
                return value / 1000.0 / dens
            if from_kind == "ml" and to_kind == "mg":
                return value * dens * 1000.0
            return 0.0

        def _pick_amount(detail: JsonDict, state: str) -> Tuple[str, float]:
            for key in (
                "available_weight",
                "cur_weight",
                "initial_weight",
                "available_volume",
                "cur_volume",
                "initial_volume",
                "value",
            ):
                if key not in detail:
                    continue
                raw = detail.get(key)
                if raw is None or str(raw).strip() == "":
                    continue
                num, unit = self._parse_amount_string(raw)
                if unit == "":
                    if "weight" in key:
                        unit = "mg"
                    elif "volume" in key:
                        unit = "mL"
                kind, val = _normalize_unit(num, unit, state)
                if kind != "":
                    return kind, val
            return "", 0.0

        def _tip_usage(volume_ml: float) -> Dict[int, int]:
            usable_50ul = 0.05 * 0.7
            usable_1ml = 1.0 * 0.7
            usable_5ml = 5.0 * 0.7
            if volume_ml <= usable_50ul:
                return {int(ResourceCode.TIP_50UL): 1}
            if volume_ml <= usable_1ml:
                return {int(ResourceCode.TIP_1ML): 1}
            # 1 mL 及以上统一用 5 mL 枪头, 每根最大 3.5 mL
            count = math.ceil(volume_ml / usable_5ml)
            return {int(ResourceCode.TIP_5ML): count}

        # 按试剂瓶规格定义液体死体积(mL)
        container_dead_volume_map = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): 0.1,
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): 1.0,
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): 4.0,
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): 14.0,
        }
        # 固定粉末死体积(mg)
        powder_dead_weight = 20.0
        self._logger.debug("冗余配置: 粉末死体积 %s mg, 液体死体积按容器映射", powder_dead_weight)

        # 识别磁力搅拌相关操作, 无磁力搅拌则不需要反应盖板
        magnet_related_types = {"exp_magnetic_stirrer"}
        has_magnetic_operation = any(
            str(unit.get("unit_type") or "").strip() in magnet_related_types
            for unit in layout_list
        )
        if has_magnetic_operation is False:
            reaction_seal_cap_need = 0
            self._logger.debug("任务未包含磁力搅拌相关操作, 反应盖板需求设为 0")
        else:
            reaction_seal_cap_need = math.ceil(experiment_num / 24) if experiment_num else 0

        # 1) 需求统计
        reagent_need: Dict[str, Dict[str, float]] = {}
        reagent_kind: Dict[str, str] = {}
        consumable_need: Dict[int, int] = {
            int(ResourceCode.TIP_50UL): 0,
            int(ResourceCode.TIP_1ML): 0,
            int(ResourceCode.TIP_5ML): 0,
            int(ResourceCode.TEST_TUBE_MAGNET_2ML): 0,
            int(ResourceCode.REACTION_TUBE_2ML): experiment_num,
            int(ResourceCode.REACTION_SEAL_CAP): reaction_seal_cap_need,
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE): 0,
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE): 0,
        }
        magnet_from_unit = 0
        pipetting_tip_plan: Dict[Tuple[Optional[int], str], float] = {}
        filtering_rows = set()
        filtering_diluent_tip_plan: Dict[str, float] = {}

        for unit in layout_list:
            utype = str(unit.get("unit_type") or "").strip()
            process_json = unit.get("process_json") or {}
            substance = str(process_json.get("substance") or "").strip()
            row_index = self._safe_int(unit.get("unit_row"))

            if utype == "exp_add_powder" and substance:
                add_weight = _safe_float(process_json.get("add_weight"))
                reagent_need.setdefault(substance, {"mg": 0.0, "ml": 0.0})
                reagent_need[substance]["mg"] += add_weight
                reagent_kind[substance] = "solid"
                continue

            if utype == "exp_pipetting" and substance:
                add_volume = _safe_float(process_json.get("add_volume"))
                reagent_need.setdefault(substance, {"mg": 0.0, "ml": 0.0})
                reagent_need[substance]["ml"] += add_volume
                reagent_kind[substance] = "liquid"
                if add_volume > 0:
                    tip_key = (row_index, substance)
                    current_max = pipetting_tip_plan.get(tip_key, 0.0)
                    if add_volume > current_max:
                        pipetting_tip_plan[tip_key] = add_volume
                continue

            if utype == "exp_add_magnet":
                magnet_from_unit += 1
                continue

            if utype == "exp_filtering_sample" and substance:
                add_volume = _safe_float(process_json.get("add_volume"))
                reagent_need.setdefault(substance, {"mg": 0.0, "ml": 0.0})
                reagent_need[substance]["ml"] += add_volume
                reagent_kind[substance] = "liquid"
                consumable_need[int(ResourceCode.FLASH_FILTER_INNER_BOTTLE)] += 1
                consumable_need[int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE)] += 1
                filtering_rows.add(row_index)
                if add_volume > 0:
                    current_max = filtering_diluent_tip_plan.get(substance, 0.0)
                    if add_volume > current_max:
                        filtering_diluent_tip_plan[substance] = add_volume
                continue

        if magnet_from_unit > consumable_need[int(ResourceCode.TEST_TUBE_MAGNET_2ML)]:
            consumable_need[int(ResourceCode.TEST_TUBE_MAGNET_2ML)] = magnet_from_unit

        for max_volume in pipetting_tip_plan.values():
            if max_volume <= 0:
                continue
            tip_dict = _tip_usage(max_volume)
            for code in tip_dict.keys():
                consumable_need[code] = consumable_need.get(code, 0) + 1

        if len(filtering_rows) > 0 and experiment_num > 0:
            sample_tip_need = len(filtering_rows) * experiment_num
            consumable_need[int(ResourceCode.TIP_50UL)] += sample_tip_need

        for _substance, max_volume in filtering_diluent_tip_plan.items():
            if max_volume <= 0:
                continue
            consumable_need[int(ResourceCode.TIP_5ML)] = consumable_need.get(int(ResourceCode.TIP_5ML), 0) + 1

        # 2) 库存统计
        filtered_resource_rows: List[JsonDict] = []
        for row in resource_rows:
            layout_code_text = str(row.get("layout_code") or "").strip()
            if layout_code_text.upper().startswith("TB"):
                self._logger.debug("跳过过渡舱资源 %s", layout_code_text)
                continue
            filtered_resource_rows.append(row)

        tray_to_consumable = {
            int(ResourceCode.TIP_TRAY_50UL): int(ResourceCode.TIP_50UL),
            int(ResourceCode.TIP_TRAY_1ML): int(ResourceCode.TIP_1ML),
            int(ResourceCode.TIP_TRAY_5ML): int(ResourceCode.TIP_5ML),
            int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): int(ResourceCode.TEST_TUBE_MAGNET_2ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY): int(ResourceCode.REACTION_SEAL_CAP),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML): int(ResourceCode.REACTION_TUBE_2ML),
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
        }

        consumable_stock: Dict[int, int] = {}
        reagent_stock: Dict[str, Dict[str, float]] = {}
        substance_dead_volume: Dict[str, float] = {}

        for row in filtered_resource_rows:
            tray_code = self._safe_int(row.get("resource_type"))
            if tray_code is not None and tray_code in tray_to_consumable:
                consumable_code = tray_to_consumable[tray_code]
                consumable_stock[consumable_code] = consumable_stock.get(consumable_code, 0) + int(row.get("count", 0) or 0)

            bottle_dead_volume = 0.0
            if tray_code is not None and tray_code in container_dead_volume_map:
                bottle_dead_volume = container_dead_volume_map[tray_code]

            for detail in row.get("substance_details") or []:
                name = str(detail.get("substance") or "").strip()
                if name == "":
                    continue
                state = str(chemical_db.get(name, {}).get("physical_state", "") or "").lower()
                kind, val = _pick_amount(detail, state)
                if kind == "" or val <= 0:
                    continue
                reagent_stock.setdefault(name, {"mg": 0.0, "ml": 0.0})
                reagent_stock[name][kind] += val
                if bottle_dead_volume > 0:
                    # 按试剂瓶类型记录液体死体积, 取较大值保证保守
                    previous_dead_volume = substance_dead_volume.get(name)
                    if previous_dead_volume is None or bottle_dead_volume > previous_dead_volume:
                        substance_dead_volume[name] = bottle_dead_volume

        if len(substance_dead_volume) == 0:
            self._logger.debug("未找到液体死体积映射, 按默认只计需求")
        else:
            self._logger.debug("液体死体积映射 %s", substance_dead_volume)

        # 3) 对比
        reagent_report: List[JsonDict] = []
        missing_items: List[str] = []
        redundant_items: List[str] = []
        need_reagents: List[JsonDict] = []

        for name, need_map in reagent_need.items():
            base_need_mg = need_map.get("mg", 0.0)
            base_need_ml = need_map.get("ml", 0.0)

            if base_need_mg > 0:
                need_mg = base_need_mg + powder_dead_weight
            else:
                need_mg = base_need_mg

            if base_need_ml > 0:
                bottle_dead_volume = substance_dead_volume.get(name, 0.0)
                need_ml = base_need_ml + bottle_dead_volume
            else:
                bottle_dead_volume = 0.0
                need_ml = base_need_ml

            state = reagent_kind.get(name, str(chemical_db.get(name, {}).get("physical_state", "") or "")).lower()
            stock = reagent_stock.get(name, {"mg": 0.0, "ml": 0.0})
            avail_mg = stock.get("mg", 0.0)
            avail_ml = stock.get("ml", 0.0)
            density_val = chemical_db.get(name, {}).get("density (g/mL)")

            diff_text = ""
            status_text = "冗余满足"

            need_reagents.append(
                {
                    "substance": name,
                    "need_mg": round(need_mg, 1),
                    "need_ml": round(need_ml, 3),
                    "base_need_mg": round(base_need_mg, 1),
                    "base_need_ml": round(base_need_ml, 3),
                }
            )

            if need_ml > 0:
                total_ml = avail_ml
                if total_ml < need_ml and avail_mg > 0:
                    total_ml += _convert_amount("mg", "ml", avail_mg, density_val)
                diff_val = total_ml - need_ml
                diff_text = f"{diff_val:.3f}mL"
                if diff_val < 0:
                    status_text = "缺少"
                    missing_items.append(f"{name}:{abs(diff_val):.3f}mL")
                else:
                    redundant_items.append(f"{name}:{diff_val:.3f}mL")

            elif need_mg > 0:
                total_mg = avail_mg
                if total_mg < need_mg and avail_ml > 0:
                    total_mg += _convert_amount("ml", "mg", avail_ml, density_val)
                diff_val = total_mg - need_mg
                diff_text = f"{diff_val:.1f}mg"
                if diff_val < 0:
                    status_text = "缺少"
                    missing_items.append(f"{name}:{abs(diff_val):.1f}mg")
                else:
                    redundant_items.append(f"{name}:{diff_val:.1f}mg")

            reagent_report.append(
                {
                    "substance": name,
                    "need_mg": round(need_mg, 1),
                    "need_ml": round(need_ml, 3),
                    "available_mg": round(avail_mg, 1),
                    "available_ml": round(avail_ml, 3),
                    "status": status_text,
                    "diff": diff_text,
                    "base_need_mg": round(base_need_mg, 1),
                    "base_need_ml": round(base_need_ml, 3),
                }
            )

        consumable_report: List[JsonDict] = []
        consumable_name_map = {
            int(ResourceCode.TIP_50UL): "50uL枪头",
            int(ResourceCode.TIP_1ML): "1mL枪头",
            int(ResourceCode.TIP_5ML): "5mL枪头",
            int(ResourceCode.TEST_TUBE_MAGNET_2ML): "2mL反应管磁子",
            int(ResourceCode.REACTION_TUBE_2ML): "2mL反应管",
            int(ResourceCode.REACTION_SEAL_CAP): "反应盖板",
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE): "闪滤内瓶",
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE): "闪滤外瓶",
        }
        need_consumables: List[JsonDict] = []

        for code, need_cnt in consumable_need.items():
            avail_cnt = consumable_stock.get(code, 0)
            diff_cnt = avail_cnt - need_cnt
            if diff_cnt < 0:
                missing_items.append(f"{consumable_name_map.get(code, code)}:{abs(diff_cnt)}件")
                status = "lack"
            else:
                status = "satisfy"
                redundant_items.append(f"{consumable_name_map.get(code, code)}:{diff_cnt}件")

            need_consumables.append(
                {
                    "code": code,
                    "name": consumable_name_map.get(code, str(code)),
                    "need": int(need_cnt),
                }
            )
            consumable_report.append(
                {
                    "code": code,
                    "name": consumable_name_map.get(code, str(code)),
                    "need": int(need_cnt),
                    "available": int(avail_cnt),
                    "diff": int(diff_cnt),
                    "status": status,
                }
            )

        ready_flag = len([item for item in missing_items if item]) == 0
        result = {
            "ready": ready_flag,
            "reagents": reagent_report,
            "consumables": consumable_report,
            "need_reagents": need_reagents,
            "need_consumables": need_consumables,
            "missing": missing_items,
            "redundant": redundant_items,
        }

        if ready_flag:
            self._logger.info("资源核查通过")
            
            # 如果提供了task_id, 进行二次校验
            if task_id is not None:
                self._logger.info("开始二次校验, 任务ID: %d", task_id)
                try:
                    check_result = self._client.check_task_resource(task_id)
                    code = check_result.get("code")
                    
                    if code == 200:
                        self._logger.info("二次校验通过")
                    elif code == 1200:
                        # 资源不足, 校验失败
                        msg = check_result.get("msg", "")
                        prompt_msg = check_result.get("prompt_msg", {})
                        resource_type = prompt_msg.get("resource_type", "未知资源")
                        number = prompt_msg.get("number", 0)
                        
                        self._logger.error("二次校验失败: %s, 资源类型: %s, 缺少数量: %s", msg, resource_type, number)
                        
                        # 更新结果状态为未通过
                        result["ready"] = False
                        result["secondary_check_failed"] = True
                        result["secondary_check_message"] = f"二次校验失败: {resource_type} 缺少 {number}"
                        
                        return result
                    else:
                        # 其他错误码
                        self._logger.warning("二次校验返回异常代码: %d, 消息: %s", code, check_result.get("msg", ""))
                except Exception as e:
                    self._logger.error("二次校验过程中发生异常: %s", str(e))
                    # 二次校验异常不影响主流程, 仅记录日志
        else:
            self._logger.warning("资源核查未通过, 缺失项 %s", missing_items)

        return result

if __name__ == "__main__":

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    logger = logging.getLogger("station_controller.main")

    controller = SynthesisStationController()

    #设备输初始化
    controller.device_init()

    #获取资源列表
    resource_info = controller.get_resource_info()

    # #获取工站内设备所有信息
    # device_info = controller.list_device_status()
    # print(device_info)
    
    # #获取工站化合物库信息：
    # chemical_info = controller.get_all_chemical_list()
    # print(chemical_info)
    # output_csv = controller.export_chemical_list_to_csv(chemical_info, Path("station_chemical_list.csv"))

    # #从csv文件中新增化合物
    # controller.sync_chemicals_from_csv(Path("add_chemical_list.csv"), overwrite=False)

    # 删除特定ID的化合物
    # controller.delete_chemical(363)

    # #获取手套箱内气体氛围的情况
    # device_info = controller.get_glovebox_env()
    # print(device_info)

    # #批量下料测试
    # out_resp = controller.batch_out_tray(["N-4", "W-2-1","W-2-5","W-3-2"])

    # # 批量上料测试
    # resource_req_list = controller.build_batch_in_tray_payload_from_sheet("batch_in_tray.xlsx")
    # out_path = Path("resource_req_list.json")
    # out_path.write_text(json.dumps(resource_req_list, ensure_ascii=False, indent=2), encoding="utf-8")
    # result = controller.batch_in_tray(resource_req_list)

    #————————————————————————化合物库对齐————————————————————————

    # #检查化学品列表的合理性
    # controller.check_chemical_list_file("chemical_list.xlsx")

    # # 对齐化合物库和站内化学品列表
    # controller.align_chemicals_from_file("chemical_list.xlsx")

    #————————————————————————创建任务————————————————————————

    # #获取所有任务信息
    # result = controller.get_all_tasks()
    # out_path = Path("task_list.json")
    # out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # #从表格中创建任务
    # result = controller.create_task_from_template("reeaction_template.xlsx")
    # result2 = controller.add_task(result)
    # print(result2)
    # # controller.delete_task(571)

    # out_path = Path("reaction_template.json")
    # out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 复位 W-1-1 和 W-1-2
    # controller.control_w1_shelf("W-1-1", "home")




