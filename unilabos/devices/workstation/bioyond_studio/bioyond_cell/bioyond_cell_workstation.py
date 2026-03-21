# -*- coding: utf-8 -*-
from cgi import print_arguments
from doctest import debug
from typing import Dict, Any, List, Optional, Tuple, Union
import requests
from pylabrobot.resources.resource import Resource as ResourcePLR
from pathlib import Path
import pandas as pd
import time
from datetime import datetime, timedelta
import re
import threading
import json
from copy import deepcopy
from urllib3 import response
from unilabos.devices.workstation.bioyond_studio.station import BioyondWorkstation, BioyondResourceSynchronizer
# ⚠️ config.py 已废弃 - 所有配置现在从 JSON 文件加载
# from unilabos.devices.workstation.bioyond_studio.config import API_CONFIG, ...
from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPService
from unilabos.resources.bioyond.decks import BioyondElectrolyteDeck, bioyond_electrolyte_deck
from unilabos.utils.log import logger
from unilabos.registry.registry import lab_registry

def _iso_local_now_ms() -> str:
    # 文档要求：到毫秒 + Z，例如 2025-08-15T05:43:22.814Z
    dt = datetime.now()
    # print(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond/1000):03d}Z"


class BioyondCellWorkstation(BioyondWorkstation):
    """
    集成 Bioyond LIMS 的工作站示例，
    覆盖：入库(2.17/2.18) → 新建实验(2.14) → 启动调度(2.7) →
    运行中推送：物料变更(2.24)、步骤完成(2.21)、订单完成(2.23) →
    查询实验(2.5/2.6) → 3-2-1 转运(2.32) → 样品/废料取出(2.28)
    """

    def __init__(self, bioyond_config: dict = None, deck=None, protocol_type=None, **kwargs):
        """
        初始化 BioyondCellWorkstation
        
        Args:
            bioyond_config: 从 JSON 文件加载的 bioyond 配置字典
                           包含 api_host, api_key, HTTP_host, HTTP_port 等配置
            deck: Deck 配置（可选，会从 JSON 中自动处理）
            protocol_type: 协议类型（可选）
            **kwargs: 其他参数（如 children 等）
        """
        
        # ⚠️ 配置验证：确保传入了必需的配置
        if bioyond_config is None:
            raise ValueError(
                "BioyondCellWorkstation 需要 bioyond_config 参数！\n"
                "请在 JSON 配置文件的 config 中添加 bioyond_config 字段，例如：\n"
                "\"config\": {\n"
                "  \"bioyond_config\": {\n"
                "    \"api_host\": \"http://...\",\n"
                "    \"api_key\": \"...\",\n"
                "    ...\n"
                "  }\n"
                "}"
            )
        
        # 验证 bioyond_config 的类型
        if not isinstance(bioyond_config, dict):
            raise ValueError(
                f"bioyond_config 必须是字典类型，实际类型: {type(bioyond_config).__name__}"
            )
        
        # 保存配置
        self.bioyond_config = bioyond_config
        
        # 验证必需的配置参数
        required_keys = ['api_host', 'api_key', 'HTTP_host', 'HTTP_port', 
                        'material_type_mappings', 'warehouse_mapping']
        missing_keys = [key for key in required_keys if key not in self.bioyond_config]
        if missing_keys:
            raise ValueError(
                f"bioyond_config 缺少必需参数: {', '.join(missing_keys)}\n"
                f"请检查 JSON 配置文件中的 bioyond_config 字段"
            )
        
        logger.info("✅ 从 JSON 配置加载 bioyond_config 成功")
        logger.info(f"   API Host: {self.bioyond_config.get('api_host')}")
        logger.info(f"   HTTP Service: {self.bioyond_config.get('HTTP_host')}:{self.bioyond_config.get('HTTP_port')}")
        
        # 设置调试模式
        self.debug_mode = self.bioyond_config.get("debug_mode", False)
        self.http_service_started = self.debug_mode
        self._device_id = "bioyond_cell_workstation"  # 默认值，后续会从_ros_node获取
        
        # ⚠️ 关键：设置标志位，告诉父类不要在 post_init 中启动 HTTP 服务
        # 因为子类会在这里自己启动 HTTP 服务
        self.bioyond_config["_disable_auto_http_service"] = True
        logger.info("🔧 已设置 _disable_auto_http_service 标志，防止 HTTP 服务重复启动")
        
        # 调用父类初始化（传入完整的 bioyond_config）
        super().__init__(bioyond_config=self.bioyond_config, deck=deck, **kwargs)
        
        # 更新奔耀端的报送 IP 地址
        self.update_push_ip()
        logger.info("已更新奔耀端推送 IP 地址")

        # 启动 HTTP 服务线程（子类自己管理）
        t = threading.Thread(target=self._start_http_service, daemon=True, name="unilab_http")
        t.start()
        logger.info("HTTP 服务线程已启动")
        
        # 初始化订单报送事件
        self.order_finish_event = threading.Event()
        self.last_order_status = None
        self.last_order_code = None
        
        logger.info(f"✅ BioyondCellWorkstation 初始化完成 (debug_mode={self.debug_mode})")

    @property
    def device_id(self):
        """获取设备ID，优先从_ros_node获取，否则返回默认值"""
        if hasattr(self, '_ros_node') and self._ros_node is not None:
            return getattr(self._ros_node, 'device_id', self._device_id)
        return self._device_id

    def _start_http_service(self):
        """启动 HTTP 服务"""
        host = self.bioyond_config.get("HTTP_host", "")
        port = self.bioyond_config.get("HTTP_port", None)
        try:
            self.service = WorkstationHTTPService(self, host=host, port=port)
            self.service.start()
            self.http_service_started = True
            logger.info(f"WorkstationHTTPService 成功启动: {host}:{port}")
            while True:
                time.sleep(1) #一直挂着，直到进程退出
        except Exception as e:
            self.http_service_started = False
            logger.error(f"启动 WorkstationHTTPService 失败: {e}", exc_info=True)


    # http报送服务，返回数据部分
    def process_step_finish_report(self, report_request):
        stepId = report_request.data.get("stepId")
        logger.info(f"步骤完成: stepId: {stepId}, stepName:{report_request.data.get('stepName')}")
        return report_request.data.get('executionStatus')

    def process_sample_finish_report(self, report_request):
        logger.info(f"通量完成: {report_request.data.get('sampleId')}")
        return {"status": "received"}

    def process_order_finish_report(self, report_request, used_materials=None):
        order_code = report_request.data.get("orderCode")
        status = report_request.data.get("status")
        
        # 🔍 详细调试日志
        logger.info(f"[DEBUG] ========== 收到 order_finish 报送 ==========")
        logger.info(f"[DEBUG] 报送的 orderCode: '{order_code}' (type: {type(order_code).__name__})")
        logger.info(f"[DEBUG] 当前等待的 last_order_code: '{self.last_order_code}' (type: {type(self.last_order_code).__name__})")
        logger.info(f"[DEBUG] 报送状态: {status}")
        logger.info(f"[DEBUG] orderCode 是否匹配: {self.last_order_code == order_code}")
        logger.info(f"[DEBUG] Event 当前状态 (触发前): is_set={self.order_finish_event.is_set()}")
        logger.info(f"report_request: {report_request}")
        logger.info(f"任务完成: {order_code}, status={status}")

        # 保存完整报文
        self.last_order_report = report_request.data
        
        # 如果是当前等待的订单，触发事件
        if self.last_order_code == order_code:
            logger.info(f"[DEBUG] ✅ orderCode 匹配！触发 order_finish_event")
            self.order_finish_event.set()
            logger.info(f"[DEBUG] Event 状态 (触发后): is_set={self.order_finish_event.is_set()}")
        else:
            logger.warning(f"[DEBUG] ❌ orderCode 不匹配，不触发 event")
            logger.warning(f"[DEBUG]    期望: '{self.last_order_code}'")
            logger.warning(f"[DEBUG]    实际: '{order_code}'")
        
        logger.info(f"[DEBUG] ========================================")
        return {"status": "received"}

    def wait_for_order_finish(self, order_code: str, timeout: int = 36000) -> Dict[str, Any]:
        """
        等待指定 orderCode 的 /report/order_finish 报送。
        Args:
            order_code: 任务编号
            timeout: 超时时间（秒）
        Returns:
            完整的报送数据 + 状态判断结果
        """
        if not order_code:
            logger.error("wait_for_order_finish() 被调用，但 order_code 为空！")
            return {"status": "error", "message": "empty order_code"}

        self.last_order_code = order_code
        self.last_order_report = None
        self.order_finish_event.clear()

        logger.info(f"等待任务完成报送: orderCode={order_code} (timeout={timeout}s)")

        if not self.order_finish_event.wait(timeout=timeout):
            logger.error(f"等待任务超时: orderCode={order_code}")
            return {"status": "timeout", "orderCode": order_code}

        # 报送数据匹配验证
        report = self.last_order_report or {}
        report_code = report.get("orderCode")
        status = str(report.get("status", ""))

        if report_code != order_code:
            logger.warning(f"收到的报送 orderCode 不匹配: {report_code} ≠ {order_code}")
            return {"status": "mismatch", "report": report}

        if status == "30":
            logger.info(f"任务成功完成 (orderCode={order_code})")
            return {"status": "success", "report": report}
        elif status == "-11":
            logger.error(f"任务异常停止 (orderCode={order_code})")
            return {"status": "abnormal_stop", "report": report}
        elif status == "-12":
            logger.warning(f"任务人工停止 (orderCode={order_code})")
            return {" status": "manual_stop", "report": report}
        else:
            logger.warning(f"任务未知状态 ({status}) (orderCode={order_code})")
            return {"status": f"unknown_{status}", "report": report}

    def wait_for_order_finish_polling(self, order_code: str, timeout: int = 36000, poll_interval: float = 0.5) -> Dict[str, Any]:
        """
        等待指定 orderCode 的 /report/order_finish 报送（非阻塞轮询版本）。
        
        与 wait_for_order_finish 的区别：
        - 使用轮询而非阻塞等待，每隔 poll_interval 秒检查一次
        - 允许 ROS2 在等待期间处理 feedback 消息
        - 适用于长时间运行的 ROS2 Action
        
        Args:
            order_code: 任务编号
            timeout: 超时时间（秒）
            poll_interval: 轮询间隔（秒），默认 0.5 秒
        Returns:
            完整的报送数据 + 状态判断结果
        """
        if not order_code:
            logger.error("wait_for_order_finish_polling() 被调用，但 order_code 为空！")
            return {"status": "error", "message": "empty order_code"}

        self.last_order_code = order_code
        self.last_order_report = None
        self.order_finish_event.clear()

        logger.info(f"[轮询模式] 等待任务完成报送: orderCode={order_code} (timeout={timeout}s, poll_interval={poll_interval}s)")
        logger.info(f"[轮询模式] [DEBUG] last_order_code 已设置为: '{self.last_order_code}'")
        logger.info(f"[轮询模式] [DEBUG] Event 初始状态: is_set={self.order_finish_event.is_set()}")

        start_time = time.time()
        poll_count = 0
        while not self.order_finish_event.is_set():
            poll_count += 1
            elapsed = time.time() - start_time
            
            # 每 10 次轮询（约 5 秒）输出一次状态
            if poll_count % 10 == 0:
                logger.info(f"[轮询模式] [DEBUG] 轮询中... 已等待 {elapsed:.1f}s (第{poll_count}次检查)")
                logger.info(f"[轮询模式] [DEBUG] Event.is_set() = {self.order_finish_event.is_set()}")
            
            # 检查是否超时
            if elapsed > timeout:
                logger.error(f"[轮询模式] 等待任务超时: orderCode={order_code}")
                logger.error(f"[轮询模式] [DEBUG] 总共轮询了 {poll_count} 次，耗时 {elapsed:.1f}s")
                return {"status": "timeout", "orderCode": order_code}
            
            # 短暂 sleep，让出控制权给 ROS2 处理 feedback
            time.sleep(poll_interval)

        # 事件已触发，获取报送数据
        logger.info(f"[轮询模式] [DEBUG] ✅ Event 已触发！共轮询 {poll_count} 次")
        report = self.last_order_report or {}
        report_code = report.get("orderCode")
        status = str(report.get("status", ""))
        
        logger.info(f"[轮询模式] [DEBUG] 报送数据: orderCode='{report_code}', status={status}")

        # 报送数据匹配验证
        if report_code != order_code:
            logger.warning(f"[轮询模式] 收到的报送 orderCode 不匹配: {report_code} ≠ {order_code}")
            return {"status": "mismatch", "report": report}

        # 状态判断
        if status == "30":
            logger.info(f"[轮询模式] 任务成功完成 (orderCode={order_code})")
            return {"status": "success", "report": report}
        elif status == "-11":
            logger.error(f"[轮询模式] 任务异常停止 (orderCode={order_code})")
            return {"status": "abnormal_stop", "report": report}
        elif status == "-12":
            logger.warning(f"[轮询模式] 任务人工停止 (orderCode={order_code})")
            return {"status": "manual_stop", "report": report}
        else:
            logger.warning(f"[轮询模式] 任务未知状态 ({status}) (orderCode={order_code})")
            return {"status": f"unknown_{status}", "report": report}


    def get_material_info(self, material_id: str) -> Dict[str, Any]:
        """查询物料详细信息（物料详情接口）
        
        Args:
            material_id: 物料 ID (GUID)
            
        Returns:
            物料详情，包含 name, typeName, locations 等
        """
        result = self._post_lims("/api/lims/storage/material-info", material_id)
        return result.get("data", {})

    def _process_order_reagents(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """处理订单完成报文中的试剂数据，计算质量比
        
        Args:
            report: 订单完成推送的 report 数据
            
        Returns:
            {
                "real_mass_ratio": {"试剂A": 0.6, "试剂B": 0.4},
                "target_mass_ratio": {"试剂A": 0.6, "试剂B": 0.4},
                "reagent_details": [...]  # 详细数据
            }
        """
        used_materials = report.get("usedMaterials", [])
        
        # 1. 筛选试剂（typemode="2"，注意是小写且是字符串）
        reagents = [m for m in used_materials if str(m.get("typemode")) == "2"]
        
        if not reagents:
            logger.warning("订单完成报文中没有试剂（typeMode=2）")
            return {
                "real_mass_ratio": {},
                "target_mass_ratio": {},
                "reagent_details": []
            }
        
        # 2. 查询试剂名称
        reagent_data = []
        for reagent in reagents:
            material_id = reagent.get("materialId")
            if not material_id:
                continue
                
            try:
                info = self.get_material_info(material_id)
                name = info.get("name", f"Unknown_{material_id[:8]}")
                real_qty = float(reagent.get("realQuantity", 0.0))
                used_qty = float(reagent.get("usedQuantity", 0.0))
                
                reagent_data.append({
                    "name": name,
                    "material_id": material_id,
                    "real_quantity": real_qty,
                    "used_quantity": used_qty
                })
                logger.info(f"试剂: {name}, 目标={used_qty}g, 实际={real_qty}g")
            except Exception as e:
                logger.error(f"查询物料信息失败: {material_id}, {e}")
                continue
        
        if not reagent_data:
            return {
                "real_mass_ratio": {},
                "target_mass_ratio": {},
                "reagent_details": []
            }
        
        # 3. 计算质量比
        def calculate_mass_ratio(items: List[Dict], key: str) -> Dict[str, float]:
            total = sum(item[key] for item in items)
            if total == 0:
                logger.warning(f"总质量为0，无法计算{key}质量比")
                return {item["name"]: 0.0 for item in items}
            return {item["name"]: round(item[key] / total, 4) for item in items}
        
        real_mass_ratio = calculate_mass_ratio(reagent_data, "real_quantity")
        target_mass_ratio = calculate_mass_ratio(reagent_data, "used_quantity")
        
        logger.info(f"真实质量比: {real_mass_ratio}")
        logger.info(f"目标质量比: {target_mass_ratio}")
        
        return {
            "real_mass_ratio": real_mass_ratio,
            "target_mass_ratio": target_mass_ratio,
            "reagent_details": reagent_data
        }


    # -------------------- 基础HTTP封装 --------------------
    def _url(self, path: str) -> str:
        return f"{self.bioyond_config['api_host'].rstrip('/')}/{path.lstrip('/')}"

    def _post_lims(self, path: str, data: Optional[Any] = None) -> Dict[str, Any]:
        """LIMS API：大多数接口用 {apiKey/requestTime,data} 包装"""
        payload = {
            "apiKey": self.bioyond_config["api_key"],
            "requestTime": _iso_local_now_ms()
        }
        if data is not None:
            payload["data"] = data

        if self.debug_mode:
            # 模拟返回，不发真实请求
            logger.info(f"[DEBUG] POST {path} with payload={payload}")
            
            return {"debug": True, "url": self._url(path), "payload": payload, "status": "ok"}

        try:
            logger.info(json.dumps(payload, ensure_ascii=False))
            response = requests.post(
                self._url(path), 
                json=payload,
                timeout=self.bioyond_config.get("timeout", 30),
                headers={"Content-Type": "application/json"}
            ) # 拼接网址+post bioyond接口
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.info(f"{self.bioyond_config['api_host'].rstrip('/')}/{path.lstrip('/')}")
            logger.error(f"POST {path} 失败: {e}")
            return {"error": str(e)}

    def _put_lims(self, path: str, data: Optional[Any] = None) -> Dict[str, Any]:
        """LIMS API：PUT {apiKey/requestTime,data} 包装"""
        payload = {
            "apiKey": self.bioyond_config["api_key"],
            "requestTime": _iso_local_now_ms()
        }
        if data is not None:
            payload["data"] = data

        if self.debug_mode:
            logger.info(f"[DEBUG] PUT {path} with payload={payload}")
            return {"debug_mode": True, "url": self._url(path), "payload": payload, "status": "ok"}

        try:
            response = requests.put(
                self._url(path),
                json=payload,
                timeout=self.bioyond_config.get("timeout", 30),
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.info(f"{self.bioyond_config['api_host'].rstrip('/')}/{path.lstrip('/')}")
            logger.error(f"PUT {path} 失败: {e}")
            return {"error": str(e)}

    # -------------------- 3.36 更新推送 IP 地址 --------------------
    def update_push_ip(self, ip: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
        """
        3.36 更新推送 IP 地址接口（PUT）
        URL: /api/lims/order/ip-config
        请求体：{ apiKey, requestTime, data: { ip, port } }
        """
        target_ip = ip or self.bioyond_config.get("HTTP_host", "")
        target_port = int(port or self.bioyond_config.get("HTTP_port", 0))
        data = {"ip": target_ip, "port": target_port}

        # 固定接口路径，不做其他路径兼容
        path = "/api/lims/order/ip-config"
        return self._put_lims(path, data)

    # -------------------- 单点接口封装 --------------------
    # 2.17 入库物料（单个）
    def storage_inbound(self, material_id: str, location_id: str) -> Dict[str, Any]:
        return self._post_lims("/api/lims/storage/inbound", {
            "materialId": material_id,
            "locationId": location_id
        })

    # 2.18 批量入库（多个）
    def storage_batch_inbound(self, items: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        items = [{"materialId": "...", "locationId": "..."}, ...]
        """
        return self._post_lims("/api/lims/storage/batch-inbound", items)


    def auto_feeding4to3(
        self,
        # ★ 修改点：默认模板路径
        xlsx_path: Optional[str] = "D:\\UniLab\\Uni-Lab-OS\\unilabos\\devices\\workstation\\bioyond_studio\\bioyond_cell\\material_template.xlsx",
        # ---------------- WH4 - 加样头面 (Z=1, 12个点位) ----------------
        WH4_x1_y1_z1_1_materialName: str = "", WH4_x1_y1_z1_1_quantity: float = 0.0,
        WH4_x2_y1_z1_2_materialName: str = "", WH4_x2_y1_z1_2_quantity: float = 0.0,
        WH4_x3_y1_z1_3_materialName: str = "", WH4_x3_y1_z1_3_quantity: float = 0.0,
        WH4_x4_y1_z1_4_materialName: str = "", WH4_x4_y1_z1_4_quantity: float = 0.0,
        WH4_x5_y1_z1_5_materialName: str = "", WH4_x5_y1_z1_5_quantity: float = 0.0,
        WH4_x1_y2_z1_6_materialName: str = "", WH4_x1_y2_z1_6_quantity: float = 0.0,
        WH4_x2_y2_z1_7_materialName: str = "", WH4_x2_y2_z1_7_quantity: float = 0.0,
        WH4_x3_y2_z1_8_materialName: str = "", WH4_x3_y2_z1_8_quantity: float = 0.0,
        WH4_x4_y2_z1_9_materialName: str = "", WH4_x4_y2_z1_9_quantity: float = 0.0,
        WH4_x5_y2_z1_10_materialName: str = "", WH4_x5_y2_z1_10_quantity: float = 0.0,
        WH4_x1_y3_z1_11_materialName: str = "", WH4_x1_y3_z1_11_quantity: float = 0.0,
        WH4_x2_y3_z1_12_materialName: str = "", WH4_x2_y3_z1_12_quantity: float = 0.0,

        # ---------------- WH4 - 原液瓶面 (Z=2, 9个点位) ----------------
        WH4_x1_y1_z2_1_materialName: str = "", WH4_x1_y1_z2_1_quantity: float = 0.0, WH4_x1_y1_z2_1_materialType: str = "", WH4_x1_y1_z2_1_targetWH: str = "",
        WH4_x2_y1_z2_2_materialName: str = "", WH4_x2_y1_z2_2_quantity: float = 0.0, WH4_x2_y1_z2_2_materialType: str = "", WH4_x2_y1_z2_2_targetWH: str = "",
        WH4_x3_y1_z2_3_materialName: str = "", WH4_x3_y1_z2_3_quantity: float = 0.0, WH4_x3_y1_z2_3_materialType: str = "", WH4_x3_y1_z2_3_targetWH: str = "",
        WH4_x1_y2_z2_4_materialName: str = "", WH4_x1_y2_z2_4_quantity: float = 0.0, WH4_x1_y2_z2_4_materialType: str = "", WH4_x1_y2_z2_4_targetWH: str = "",
        WH4_x2_y2_z2_5_materialName: str = "", WH4_x2_y2_z2_5_quantity: float = 0.0, WH4_x2_y2_z2_5_materialType: str = "", WH4_x2_y2_z2_5_targetWH: str = "",
        WH4_x3_y2_z2_6_materialName: str = "", WH4_x3_y2_z2_6_quantity: float = 0.0, WH4_x3_y2_z2_6_materialType: str = "", WH4_x3_y2_z2_6_targetWH: str = "",
        WH4_x1_y3_z2_7_materialName: str = "", WH4_x1_y3_z2_7_quantity: float = 0.0, WH4_x1_y3_z2_7_materialType: str = "", WH4_x1_y3_z2_7_targetWH: str = "",
        WH4_x2_y3_z2_8_materialName: str = "", WH4_x2_y3_z2_8_quantity: float = 0.0, WH4_x2_y3_z2_8_materialType: str = "", WH4_x2_y3_z2_8_targetWH: str = "",
        WH4_x3_y3_z2_9_materialName: str = "", WH4_x3_y3_z2_9_quantity: float = 0.0, WH4_x3_y3_z2_9_materialType: str = "", WH4_x3_y3_z2_9_targetWH: str = "",

        # ---------------- WH3 - 人工堆栈 (Z=3, 15个点位) ----------------
        WH3_x1_y1_z3_1_materialType: str = "", WH3_x1_y1_z3_1_materialId: str = "", WH3_x1_y1_z3_1_quantity: float = 0,
        WH3_x2_y1_z3_2_materialType: str = "", WH3_x2_y1_z3_2_materialId: str = "", WH3_x2_y1_z3_2_quantity: float = 0,
        WH3_x3_y1_z3_3_materialType: str = "", WH3_x3_y1_z3_3_materialId: str = "", WH3_x3_y1_z3_3_quantity: float = 0,
        WH3_x1_y2_z3_4_materialType: str = "", WH3_x1_y2_z3_4_materialId: str = "", WH3_x1_y2_z3_4_quantity: float = 0,
        WH3_x2_y2_z3_5_materialType: str = "", WH3_x2_y2_z3_5_materialId: str = "", WH3_x2_y2_z3_5_quantity: float = 0,
        WH3_x3_y2_z3_6_materialType: str = "", WH3_x3_y2_z3_6_materialId: str = "", WH3_x3_y2_z3_6_quantity: float = 0,
        WH3_x1_y3_z3_7_materialType: str = "", WH3_x1_y3_z3_7_materialId: str = "", WH3_x1_y3_z3_7_quantity: float = 0,
        WH3_x2_y3_z3_8_materialType: str = "", WH3_x2_y3_z3_8_materialId: str = "", WH3_x2_y3_z3_8_quantity: float = 0,
        WH3_x3_y3_z3_9_materialType: str = "", WH3_x3_y3_z3_9_materialId: str = "", WH3_x3_y3_z3_9_quantity: float = 0,
        WH3_x1_y4_z3_10_materialType: str = "", WH3_x1_y4_z3_10_materialId: str = "", WH3_x1_y4_z3_10_quantity: float = 0,
        WH3_x2_y4_z3_11_materialType: str = "", WH3_x2_y4_z3_11_materialId: str = "", WH3_x2_y4_z3_11_quantity: float = 0,
        WH3_x3_y4_z3_12_materialType: str = "", WH3_x3_y4_z3_12_materialId: str = "", WH3_x3_y4_z3_12_quantity: float = 0,
        WH3_x1_y5_z3_13_materialType: str = "", WH3_x1_y5_z3_13_materialId: str = "", WH3_x1_y5_z3_13_quantity: float = 0,
        WH3_x2_y5_z3_14_materialType: str = "", WH3_x2_y5_z3_14_materialId: str = "", WH3_x2_y5_z3_14_quantity: float = 0,
        WH3_x3_y5_z3_15_materialType: str = "", WH3_x3_y5_z3_15_materialId: str = "", WH3_x3_y5_z3_15_quantity: float = 0,
    ):
        """
        自动化上料（支持两种模式）
        - Excel 路径存在 → 从 Excel 模板解析
        - Excel 路径不存在 → 使用手动参数
        """
        items: List[Dict[str, Any]] = []

        # ---------- 模式 1: Excel 导入 ----------
        if xlsx_path:
            path = Path(__file__).parent / Path(xlsx_path)
            if path.exists():   # ★ 修改点：路径存在才加载
                try:
                    df = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl")
                except Exception as e:
                    raise RuntimeError(f"读取 Excel 失败：{e}")

                # 四号手套箱加样头面
                for _, row in df.iloc[1:13, 2:7].iterrows():
                    if pd.notna(row[5]):
                        items.append({
                            "sourceWHName": "四号手套箱堆栈",
                            "posX": int(row[2]), "posY": int(row[3]), "posZ": int(row[4]),
                            "materialName": str(row[5]).strip(),
                            "quantity": float(row[6]) if pd.notna(row[6]) else 0.0,
                        })
                # 四号手套箱原液瓶面
                for _, row in df.iloc[14:23, 2:9].iterrows():
                    if pd.notna(row[5]):
                        items.append({
                            "sourceWHName": "四号手套箱堆栈",
                            "posX": int(row[2]), "posY": int(row[3]), "posZ": int(row[4]),
                            "materialName": str(row[5]).strip(),
                            "quantity": float(row[6]) if pd.notna(row[6]) else 0.0,
                            "materialType": str(row[7]).strip() if pd.notna(row[7]) else "",
                            "targetWH": str(row[8]).strip() if pd.notna(row[8]) else "",
                        })
                # 三号手套箱人工堆栈
                for _, row in df.iloc[25:40, 2:7].iterrows():
                    if pd.notna(row[5]) or pd.notna(row[6]):
                        items.append({
                            "sourceWHName": "三号手套箱人工堆栈",
                            "posX": int(row[2]), "posY": int(row[3]), "posZ": int(row[4]),
                            "materialType": str(row[5]).strip() if pd.notna(row[5]) else "",
                            "materialId": str(row[6]).strip() if pd.notna(row[6]) else "",
                            "quantity": 1
                        })
            else:
                logger.warning(f"未找到 Excel 文件 {xlsx_path}，自动切换到手动参数模式。")

        # ---------- 模式 2: 手动填写 ----------
        if not items:
            params = locals()
            for name, value in params.items():
                if name.startswith("四号手套箱堆栈") and "materialName" in name and value:
                    idx = name.split("_")
                    items.append({
                        "sourceWHName": "四号手套箱堆栈",
                        "posX": int(idx[1][1:]), "posY": int(idx[2][1:]), "posZ": int(idx[3][1:]),
                        "materialName": value,
                        "quantity": float(params.get(name.replace("materialName", "quantity"), 0.0))
                    })
                elif name.startswith("四号手套箱堆栈") and "materialType" in name and (value or params.get(name.replace("materialType", "materialName"), "")):
                    idx = name.split("_")
                    items.append({
                        "sourceWHName": "四号手套箱堆栈",
                        "posX": int(idx[1][1:]), "posY": int(idx[2][1:]), "posZ": int(idx[3][1:]),
                        "materialName": params.get(name.replace("materialType", "materialName"), ""),
                        "quantity": float(params.get(name.replace("materialType", "quantity"), 0.0)),
                        "materialType": value,
                        "targetWH": params.get(name.replace("materialType", "targetWH"), ""),
                    })
                elif name.startswith("三号手套箱人工堆栈") and "materialType" in name and (value or params.get(name.replace("materialType", "materialId"), "")):
                    idx = name.split("_")
                    items.append({
                        "sourceWHName": "三号手套箱人工堆栈",
                        "posX": int(idx[1][1:]), "posY": int(idx[2][1:]), "posZ": int(idx[3][1:]),
                        "materialType": value,
                        "materialId": params.get(name.replace("materialType", "materialId"), ""),
                        "quantity": int(params.get(name.replace("materialType", "quantity"), 1)),
                    })

        if not items:
            logger.warning("没有有效的上料条目，已跳过提交。")
            return {"code": 0, "message": "no valid items", "data": []}
        logger.info(items)
        response = self._post_lims("/api/lims/order/auto-feeding4to3", items)

        # 等待任务报送成功
        if response is None:
            logger.error("上料 API 返回了空响应（None），服务端可能因入参问题返回了 null body，请检查物料条目是否合法。")
            return {"code": -1, "message": "API returned None response"}
        order_code = (response.get("data") or {}).get("orderCode")
        if not order_code:
            logger.error(f"上料任务未返回有效 orderCode！完整响应：{response}")
            return response
          # 等待完成报送
        result = self.wait_for_order_finish(order_code)
        print("\n" + "="*60)
        print("实验记录本结果auto_feeding4to3")
        print("="*60)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("="*60 + "\n")
        return result
    
    def auto_batch_outbound_from_xlsx(self, xlsx_path: str) -> Dict[str, Any]:
        """
        3.31 自动化下料（Excel -> JSON -> POST /api/lims/storage/auto-batch-out-bound）
        """
        path = Path(xlsx_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到 Excel 文件：{path}")

        try:
            df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"读取 Excel 失败：{e}")

        def pick(names: List[str]) -> Optional[str]:
            for n in names:
                if n in df.columns:
                    return n
            return None

        c_loc = pick(["locationId", "库位ID", "库位Id", "库位id"])
        c_wh  = pick(["warehouseId", "仓库ID", "仓库Id", "仓库id"])
        c_qty = pick(["数量", "quantity"])
        c_x   = pick(["x", "X", "posX", "坐标X"])
        c_y   = pick(["y", "Y", "posY", "坐标Y"])
        c_z   = pick(["z", "Z", "posZ", "坐标Z"])

        required = [c_loc, c_wh, c_qty, c_x, c_y, c_z]
        if any(c is None for c in required):
            raise KeyError("Excel 缺少必要列：locationId/warehouseId/数量/x/y/z（支持多别名，至少要能匹配到）。")

        def as_int(v, d=0):
            try:
                if pd.isna(v): return d
                return int(v)
            except Exception:
                try:
                    return int(float(v))
                except Exception:
                    return d

        def as_float(v, d=0.0):
            try:
                if pd.isna(v): return d
                return float(v)
            except Exception:
                return d

        def as_str(v, d=""):
            if v is None or (isinstance(v, float) and pd.isna(v)): return d
            s = str(v).strip()
            return s if s else d

        items: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            items.append({
                "locationId": as_str(row[c_loc]),
                "warehouseId": as_str(row[c_wh]),
                "quantity": as_float(row[c_qty]),
                "x": as_int(row[c_x]),
                "y": as_int(row[c_y]),
                "z": as_int(row[c_z]),
            })

        response = self._post_lims("/api/lims/storage/auto-batch-out-bound", items)
        self.wait_for_response_orders(response, "auto_batch_outbound_from_xlsx")
        return response

    # -------------------- 订单提交/等待/后处理（公共逻辑） --------------------
    def _submit_and_wait_orders(self, orders: List[Dict[str, Any]], tag: str = "create_orders") -> Dict[str, Any]:
        """
        公共流程：提交 orders → 等待完成 → 计算质量比 → 提取分液瓶板 → 返回结果。
        由 create_orders / create_orders_formulation 调用。
        """
        logger.info(f"[{tag}] 即将提交 {len(orders)} 个订单")
        response = self._post_lims("/api/lims/order/orders", orders)
        logger.info(f"[{tag}] 接口返回: {response}")

        # 提取 orderCode
        data_list = response.get("data", [])
        if not data_list:
            logger.error("创建订单未返回有效数据！")
            return response

        order_codes = [item.get("orderCode") for item in data_list if item.get("orderCode")]
        if not order_codes:
            logger.error("未找到任何有效的 orderCode！")
            return response

        logger.info(f"[{tag}] 等待 {len(order_codes)} 个订单完成: {order_codes}")

        # ========== 等待所有订单完成 ==========
        all_reports = []
        for idx, order_code in enumerate(order_codes, 1):
            logger.info(f"[{tag}] 等待第 {idx}/{len(order_codes)} 个订单: {order_code}")
            result = self.wait_for_order_finish(order_code)
            if result.get("status") == "success":
                all_reports.append(result.get("report", {}))
                logger.info(f"[{tag}] ✓ 订单 {order_code} 完成")
            else:
                logger.warning(f"订单 {order_code} 状态异常: {result.get('status')}")
                all_reports.append({
                    "orderCode": order_code,
                    "status": result.get("status"),
                    "error": result.get("message", "未知错误"),
                })

        logger.info(f"[{tag}] 所有订单已完成，共收集 {len(all_reports)} 个报文")

        # ========== 计算质量比 ==========
        all_mass_ratios = []
        for idx, report in enumerate(all_reports, 1):
            order_code = report.get("orderCode", "N/A")
            if "error" not in report:
                try:
                    mass_ratios = self._process_order_reagents(report)
                    all_mass_ratios.append({
                        "orderCode": order_code,
                        "orderName": report.get("orderName", "N/A"),
                        "real_mass_ratio": mass_ratios.get("real_mass_ratio", {}),
                        "target_mass_ratio": mass_ratios.get("target_mass_ratio", {}),
                    })
                    logger.info(f"✓ 已计算订单 {order_code} 的试剂质量比")
                except Exception as e:
                    logger.error(f"计算订单 {order_code} 质量比失败: {e}")
                    all_mass_ratios.append({
                        "orderCode": order_code,
                        "orderName": report.get("orderName", "N/A"),
                        "real_mass_ratio": {},
                        "target_mass_ratio": {},
                        "error": str(e),
                    })
            else:
                all_mass_ratios.append({
                    "orderCode": order_code,
                    "orderName": report.get("orderName", "N/A"),
                    "real_mass_ratio": {},
                    "target_mass_ratio": {},
                    "error": "订单未成功完成",
                })

        logger.info(f"[{tag}] 质量比计算完成")

        # ========== 提取分液瓶板信息 + 创建资源树对象 ==========
        all_vial_plates = []
        processed_material_ids = set()
        for report in all_reports:
            vial_plate_info = self._extract_vial_plate_from_report(report)
            if vial_plate_info:
                material_id = vial_plate_info.get("materialId")
                all_vial_plates.append(vial_plate_info)
                if material_id in processed_material_ids:
                    logger.info(
                        f"[资源树] ℹ️ 瓶板资源已存在: materialId={material_id[:20]}..., "
                        f"orderCode={vial_plate_info.get('orderCode')} (共用同一瓶板，跳过重复创建)"
                    )
                    continue
                try:
                    self._create_vial_plate_resource(vial_plate_info)
                    processed_material_ids.add(material_id)
                    logger.info(
                        f"[资源树] ✅ 瓶板资源创建成功: orderCode={vial_plate_info.get('orderCode')}, "
                        f"materialId={material_id[:20]}..."
                    )
                except Exception as e:
                    logger.error(
                        f"[资源树] 创建失败: orderCode={vial_plate_info.get('orderCode')}, 错误={e}"
                    )

        logger.info(
            f"[{tag}] 提取到 {len(all_vial_plates)} 个订单的分液瓶板信息 "
            f"(对应 {len(processed_material_ids)} 个物理瓶板)"
        )

        # ========== 构造最终结果 ==========
        final_result = {
            "status": "all_completed",
            "total_orders": len(order_codes),
            "bottle_count": len(order_codes),
            "reports": all_reports,
            "mass_ratios": all_mass_ratios,
            "vial_plates": all_vial_plates,
            "original_response": response,
        }

        logger.info("=" * 80)
        logger.info(f"[{tag}] 返回报文数量: {len(all_reports)}, 分液瓶板数量: {len(all_vial_plates)}")
        for idx, vial_plate in enumerate(all_vial_plates, 1):
            logger.info(
                f"  [{idx}] orderCode={vial_plate.get('orderCode', 'N/A')}, "
                f"materialId={vial_plate.get('materialId', 'N/A')[:20]}..., "
                f"locationId={vial_plate.get('locationId', 'N/A')[:20]}..., "
                f"typeName={vial_plate.get('typeName', 'N/A')}"
            )
        logger.info("=" * 80)

        return final_result

    # -------------------- 2.14 新建实验（Excel 入口） --------------------
    def create_orders(self, xlsx_path: str) -> Dict[str, Any]:
        """
        从 Excel 解析并创建实验（2.14）- V2版本
        约定：
        - batchId = Excel 文件名（不含扩展名）
        - 物料列：所有以 "(g)" 结尾（不再读取"总质量(g)"列）
        - totalMass 自动计算为所有物料质量之和
        - createTime 缺失或为空时自动填充为当前日期（YYYY/M/D）
        """
        default_path = Path("D:\\UniLab\\Uni-Lab-OS\\unilabos\\devices\\workstation\\bioyond_studio\\bioyond_cell\\2025122301.xlsx")
        path = Path(xlsx_path) if xlsx_path else default_path
        print(f"[create_orders_v2] 使用 Excel 路径: {path}")
        if path != default_path:
            print("[create_orders_v2] 来源: 调用方传入自定义路径")
        else:
            print("[create_orders_v2] 来源: 使用默认模板路径")

        if not path.exists():
            print(f"[create_orders_v2] ⚠️ Excel 文件不存在: {path}")
            raise FileNotFoundError(f"未找到 Excel 文件：{path}")

        try:
            df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"读取 Excel 失败：{e}")
        print(f"[create_orders_v2] Excel 读取成功，行数: {len(df)}, 列: {list(df.columns)}")

        # 列名容错：返回可选列名，找不到则返回 None
        def _pick(col_names: List[str]) -> Optional[str]:
            for c in col_names:
                if c in df.columns:
                    return c
            return None

        col_order_name = _pick(["配方ID", "orderName", "订单编号"])
        col_create_time = _pick(["创建日期", "createTime"])
        col_bottle_type = _pick(["配液瓶类型", "bottleType"])
        col_mix_time = _pick(["混匀时间(s)", "mixTime"])
        col_load = _pick(["扣电组装分液体积", "loadSheddingInfo"])
        col_pouch = _pick(["软包组装分液体积", "pouchCellInfo"])
        col_cond = _pick(["电导测试分液体积", "conductivityInfo"])
        col_cond_cnt = _pick(["电导测试分液瓶数", "conductivityBottleCount"])
        print("[create_orders_v2] 列匹配结果:", {
            "order_name": col_order_name,
            "create_time": col_create_time,
            "bottle_type": col_bottle_type,
            "mix_time": col_mix_time,
            "load": col_load,
            "pouch": col_pouch,
            "conductivity": col_cond,
            "conductivity_bottle_count": col_cond_cnt,
        })

        # 物料列：所有以 (g) 结尾
        material_cols = [c for c in df.columns if isinstance(c, str) and c.endswith("(g)")]
        print(f"[create_orders_v2] 识别到的物料列: {material_cols}")
        if not material_cols:
            raise KeyError("未发现任何以“(g)”结尾的物料列，请检查表头。")

        batch_id = path.stem

        def _to_ymd_slash(v) -> str:
            # 统一为 "YYYY/M/D"；为空或解析失败则用当前日期
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                ts = datetime.now()
            else:
                try:
                    ts = pd.to_datetime(v)
                except Exception:
                    ts = datetime.now()
            return f"{ts.year}/{ts.month}/{ts.day}"

        def _as_int(val, default=0) -> int:
            try:
                if pd.isna(val):
                    return default
                return int(val)
            except Exception:
                return default

        def _as_float(val, default=0.0) -> float:
            try:
                if pd.isna(val):
                    return default
                return float(val)
            except Exception:
                return default

        def _as_str(val, default="") -> str:
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return default
            s = str(val).strip()
            return s if s else default

        orders: List[Dict[str, Any]] = []

        for idx, row in df.iterrows():
            mats: List[Dict[str, Any]] = []
            total_mass = 0.0

            for mcol in material_cols:
                val = row.get(mcol, None)
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    continue
                try:
                    mass = float(val)
                except Exception:
                    continue
                if mass > 0:
                    mats.append({"name": mcol.replace("(g)", ""), "mass": mass})
                    total_mass += mass
                else:
                    if mass < 0:
                        print(f"[create_orders_v2] 第 {idx+1} 行物料 {mcol} 数值为负数: {mass}")

            order_data = {
                "batchId": batch_id,
                "orderName": _as_str(row[col_order_name], default=f"{batch_id}_order_{idx+1}") if col_order_name else f"{batch_id}_order_{idx+1}",
                "createTime": _to_ymd_slash(row[col_create_time]) if col_create_time else _to_ymd_slash(None),
                "bottleType": _as_str(row[col_bottle_type], default="配液小瓶") if col_bottle_type else "配液小瓶",
                "mixTime": _as_int(row[col_mix_time]) if col_mix_time else 0,
                "loadSheddingInfo": _as_float(row[col_load]) if col_load else 0.0,
                "pouchCellInfo": _as_float(row[col_pouch]) if col_pouch else 0,
                "conductivityInfo": _as_float(row[col_cond]) if col_cond else 0,
                "conductivityBottleCount": _as_int(row[col_cond_cnt]) if col_cond_cnt else 0,
                "materialInfos": mats,
                "totalMass": round(total_mass, 4)  # 自动汇总
            }
            print(f"[create_orders_v2] 第 {idx+1} 行解析结果: orderName={order_data['orderName']}, "
                  f"loadShedding={order_data['loadSheddingInfo']}, pouchCell={order_data['pouchCellInfo']}, "
                  f"conductivity={order_data['conductivityInfo']}, totalMass={order_data['totalMass']}, "
                  f"material_count={len(mats)}")

            if order_data["totalMass"] <= 0:
                print(f"[create_orders_v2] ⚠️ 第 {idx+1} 行总质量 <= 0，可能导致 LIMS 校验失败")
            if not mats:
                print(f"[create_orders_v2] ⚠️ 第 {idx+1} 行未找到有效物料")

            orders.append(order_data)

        if not orders:
            logger.error("[create_orders] 没有有效的订单可提交")
            return {"status": "error", "message": "没有有效订单数据"}

        return self._submit_and_wait_orders(orders, tag="create_orders")
    
    def create_orders_formulation(
        self,
        formulation: List[Dict[str, Any]],
        batch_id: str = "",
        bottle_type: str = "配液小瓶",
        mix_time: int = 0,
        load_shedding_info: float = 0.0,
        pouch_cell_info: float = 0.0,
        conductivity_info: float = 0.0,
        conductivity_bottle_count: int = 0,
    ) -> Dict[str, Any]:
        """
        配方批量输入版本的 create_orders —— 等价于 create_orders，
        但参数来源于前端 FormulationBatchWidget，而非 Excel 文件。

        Args:
            formulation: 配方列表，每个元素代表一个订单（一瓶），格式：
                [
                    {
                        "order_name": "配方A",          # 可选，配方名称
                        "materials": [                   # 物料列表
                            {"name": "LiPF6", "mass": 12.5},
                            {"name": "EC",    "mass": 50.0},
                        ]
                    },
                    ...
                ]
            batch_id: 批次ID，若为空则用当前时间戳
            bottle_type: 配液瓶类型，默认 "配液小瓶"
            mix_time: 混匀时间(秒)
            load_shedding_info: 扣电组装分液体积
            pouch_cell_info: 软包组装分液体积
            conductivity_info: 电导测试分液体积
            conductivity_bottle_count: 电导测试分液瓶数

        Returns:
            与 create_orders 返回格式一致的结果字典
        """
        if not formulation:
            raise ValueError("formulation 参数不能为空")

        if not batch_id:
            batch_id = f"formulation_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        create_time = f"{datetime.now().year}/{datetime.now().month}/{datetime.now().day}"

        # 将 formulation 转换为 LIMS orders 格式（与 create_orders 中的格式一致）
        orders: List[Dict[str, Any]] = []
        for idx, item in enumerate(formulation):
            materials = item.get("materials", []) + item.get("liquids", [])  # 兼容两种物料列表命名
            order_name = item.get("order_name", f"{batch_id}_order_{idx + 1}")

            mats: List[Dict[str, Any]] = []
            total_mass = 0.0
            for mat in materials:
                name = mat.get("name", "")
                mass = float(mat.get("mass", mat.get("volume", 0.0)))
                if name and mass > 0:
                    mats.append({"name": name, "mass": mass})
                    total_mass += mass

            if not mats:
                logger.warning(f"[create_orders_formulation] 第 {idx + 1} 个配方无有效物料，跳过")
                continue

            logger.info(f"[create_orders_formulation] 第 {idx + 1} 个配方: orderName={order_name}, "
                        f"loadShedding={load_shedding_info}, pouchCell={pouch_cell_info}, "
                        f"conductivity={conductivity_info}, totalMass={total_mass}, "
                        f"material_count={len(mats)}")

            orders.append({
                "batchId": batch_id,
                "orderName": order_name,
                "createTime": create_time,
                "bottleType": bottle_type,
                "mixTime": mix_time,
                "loadSheddingInfo": load_shedding_info,
                "pouchCellInfo": pouch_cell_info,
                "conductivityInfo": conductivity_info,
                "conductivityBottleCount": conductivity_bottle_count,
                "materialInfos": mats,
                "totalMass": round(total_mass, 4),
            })

        if not orders:
            logger.error("[create_orders_formulation] 没有有效的订单可提交")
            return {"status": "error", "message": "没有有效配方数据"}

        return self._submit_and_wait_orders(orders, tag="create_orders_formulation")

    def _extract_vial_plate_from_report(self, report: Dict) -> Optional[Dict]:
        """
        从 order_finish 报文中提取分液瓶板信息
        
        Args:
            report: LIMS order_finish 报文
        
        Returns:
            {
                "materialId": "...",
                "locationId": "...",
                "orderCode": "...",
                "typeName": "5ml分液瓶板",  # 可选
                "barCode": "..."  # 可选
            }
        """
        order_code = report.get("orderCode", "N/A")
        used_materials = report.get("usedMaterials", [])
        
        # ========== 新增：调试日志 ==========
        logger.info(
            f"[提取分液瓶板] 开始处理订单 orderCode={order_code}, "
            f"物料数量={len(used_materials)}"
        )
        
        # 配置：自动堆栈-左的 locationId 前缀
        AUTO_STACK_LEFT_PREFIX = "3a19debc-84b5-"
        
        for idx, material in enumerate(used_materials):
            location_id = material.get("locationId", "")
            typemode = material.get("typemode", "")
            material_id = material.get("materialId", "")
            
            logger.debug(
                f"[提取分液瓶板] 物料 #{idx+1}: materialId={material_id[:20]}..., "
                f"locationId={location_id[:20] if location_id else 'None'}..., "
                f"typemode={typemode}"
            )
            
            # 判断条件：typemode=1 且 locationId 以自动堆栈-左前缀开头
            # ⚠️ 检查 location_id 不为 None
            if typemode == "1" and location_id and location_id.startswith(AUTO_STACK_LEFT_PREFIX):
                logger.info(
                    f"[提取分液瓶板] 找到候选物料: materialId={material_id}, "
                    f"locationId={location_id}"
                )
                
                # 可选：调用 LIMS API 2.4 获取详细信息
                try:
                    material_info = self._query_material_info(material_id)
                    type_name = material_info.get("typeName", "")
                    
                    # 确认是分液瓶板
                    if "分液瓶板" in type_name:
                        logger.info(
                            f"[提取分液瓶板] ✅ 确认为分液瓶板: orderCode={order_code}, "
                            f"materialId={material_id}, locationId={location_id}, "
                            f"typeName={type_name}"
                        )
                        return {
                            "materialId": material_id,
                            "locationId": location_id,
                            "orderCode": order_code,
                            "typeName": type_name,
                            "barCode": material_info.get("barCode")
                        }
                    else:
                        logger.warning(
                            f"[提取分液瓶板] ⚠️ 候选物料不是分液瓶板: typeName={type_name}, "
                            f"跳过并继续搜索"
                        )
                except Exception as e:
                    logger.warning(
                        f"[提取分液瓶板] ⚠️ 查询物料详情失败: materialId={material_id}, "
                        f"错误={str(e)}, 返回基本信息"
                    )
                    # 即使查询失败，也返回基本信息
                    return {
                        "materialId": material_id,
                        "locationId": location_id,
                        "orderCode": order_code
                    }
        
        logger.warning(f"[提取分液瓶板] ❌ 未找到分液瓶板: orderCode={order_code}")
        return None
    
    def _query_material_info(self, material_id: str) -> Dict:
        """
        调用 LIMS API 2.4 查询物料详情
        
        Args:
            material_id: 物料ID (materialId)
        
        Returns:
            {
                "typeName": "5ml分液瓶板",
                "barCode": "...",
                "name": "...",
                "detail": [...]
            }
        """
        # 从配置加载 api_key和api_host（用于日志）
        api_key = self.bioyond_config.get("api_key", "8A819E5C")
        api_host = self.bioyond_config.get("api_host", "UNKNOWN")
        
        # ========== 调试日志 ==========
        logger.info(
            f"[查询物料详情] 开始查询 materialId={material_id}, "
            f"api_host={api_host}, api_key={api_key[:4]}****"
        )
        
        try:
            # 直接传递 material_id，_post_lims 会自动包装为 {apiKey, requestTime, data}
            response = self._post_lims("/api/lims/storage/material-info", material_id)
            
            logger.debug(f"[查询物料详情] API响应: code={response.get('code')}, message={response.get('message')}")
            
            if response.get("code") == 1:
                data = response.get("data", {})
                logger.info(
                    f"[查询物料详情] ✅ 成功: materialId={material_id}, "
                    f"typeName={data.get('typeName')}, barCode={data.get('barCode')}"
                )
                return data
            else:
                error_msg = f"查询物料详情失败: {response.get('message')}"
                logger.warning(f"[查询物料详情] ❌ {error_msg}")
                raise ValueError(error_msg)
        except Exception as e:
            logger.error(
                f"[查询物料详情] ❌ 异常: materialId={material_id}, "
                f"错误类型={type(e).__name__}, 错误信息={str(e)}"
            )
            raise
    
    def _create_vial_plate_resource(self, vial_plate_info: Dict) -> None:
        """
        创建分液瓶板资源对象并添加到资源树
        
        Args:
            vial_plate_info: 分液瓶板元数据
                {
                    "materialId": "3a1f3df9-ddce-f544-bd48-07077ad87bc5",
                    "locationId": "3a19debc-84b5-4c1c-d3a1-26830cf273ff",
                    "orderCode": "BSO2026020500002",
                    "typeName": "5ml分液瓶板" 或 "20ml分液瓶板"
                }
        """
        from unilabos.resources.bioyond.YB_bottle_carriers import (
            YB_Vial_5mL_Carrier,
            YB_Vial_20mL_Carrier
        )
        
        material_id = vial_plate_info["materialId"]
        location_id = vial_plate_info["locationId"]
        order_code = vial_plate_info["orderCode"]
        type_name = vial_plate_info["typeName"]
        
        logger.info(
            f"[资源树] 开始创建分液瓶板: orderCode={order_code}, "
            f"typeName={type_name}"
        )
        
        # 1. 根据类型创建Carrier对象
        if "5ml" in type_name.lower() or "5mL" in type_name:
            vial_plate_obj = YB_Vial_5mL_Carrier(
                name=f"vial_plate_{order_code}"
            )
            logger.debug(f"[资源树] 创建 YB_Vial_5mL_Carrier: {vial_plate_obj.name}")
        elif "20ml" in type_name.lower() or "20mL" in type_name:
            vial_plate_obj = YB_Vial_20mL_Carrier(
                name=f"vial_plate_{order_code}"
            )
            logger.debug(f"[资源树] 创建 YB_Vial_20mL_Carrier: {vial_plate_obj.name}")
        else:
            logger.warning(
                f"[资源树] ⚠️ 未知的分液瓶板类型: {type_name}, 跳过创建"
            )
            return
        
        # ✅ 关键：分配 UUID（用于资源树转运）
        # 使用 materialId 作为 UUID，确保与LIMS系统一致
        vial_plate_obj.unilabos_uuid = material_id
        logger.debug(f"[资源树] 分配 UUID: {material_id[:30]}...")
        
        # ✅ 新增：查询并创建分液瓶板上的瓶子资源
        try:
            self._populate_vial_bottles(vial_plate_obj, material_id, order_code)
        except Exception as e:
            logger.warning(
                f"[资源树] ⚠️ 创建瓶子资源失败（继续创建瓶板）: {e}"
            )
        
        # 2. 解析位置 (locationId → warehouse + slot)
        wh_name, slot_name = self._get_warehouse_and_slot_from_location_id(
            location_id
        )
        
        if not wh_name or not slot_name:
            logger.warning(
                f"[资源树] ⚠️ 无法解析位置: locationId={location_id}, "
                f"wh_name={wh_name}, slot_name={slot_name}"
            )
            return
        
        logger.debug(
            f"[资源树] 解析位置: locationId={location_id[:20]}... → "
            f"{wh_name}[{slot_name}]"
        )
        
        # 3. 添加到资源树
        try:
            warehouse = self.deck.get_resource(wh_name)
            if not warehouse:
                logger.error(f"[资源树] ❌ 未找到仓库: {wh_name}")
                return
            
            # 使用直接槽位赋值
            # warehouse 的 sites 是一个 dict: {"A01": ResourceHolder, "A02": ...}
            # 直接通过 warehouse[slot_name] 访问槽位并赋值资源对象
            warehouse[slot_name] = vial_plate_obj
            
            logger.info(
                f"[资源树] ✅ 创建成功: {wh_name}[{slot_name}] = "
                f"{vial_plate_obj.name} (类型: {type_name})"
            )
        except Exception as e:
            logger.error(
                f"[资源树] ❌ 添加到资源树失败: {wh_name}[{slot_name}], "
                f"错误={e}"
            )
            raise
    
    def _populate_vial_bottles(
        self,
        vial_plate_obj,
        plate_material_id: str,
        order_code: str
    ) -> None:
        """
        查询分液瓶板的detail信息，创建瓶子资源并添加到瓶板
        
        Args:
            vial_plate_obj: 瓶板资源对象
            plate_material_id: 瓶板的materialId
            order_code: 订单号
        """
        logger.info(f"[资源树] 查询瓶板子物料: materialId={plate_material_id[:20]}...")
        
        # 1. 调用LIMS接口查询瓶板详情
        try:
            plate_detail = self.get_material_info(plate_material_id)
        except Exception as e:
            logger.error(f"[资源树] ❌ 查询瓶板详情失败: {e}")
            return
        
        # 2. 提取detail字段（包含所有瓶子信息）
        bottles_detail = plate_detail.get("detail", [])
        if not bottles_detail:
            logger.warning(f"[资源树] ⚠️ 瓶板无子物料信息")
            return
        
        logger.info(f"[资源树] 瓶板包含 {len(bottles_detail)} 个瓶子")
        
        # 3. 为每个瓶子创建资源
        from unilabos.resources.bioyond.YB_bottles import YB_Vial_5mL
        
        created_count = 0
        for idx, bottle_info in enumerate(bottles_detail, 1):
            try:
                bottle_material_id = bottle_info.get("detailMaterialId")
                bottle_code = bottle_info.get("code", f"bottle_{idx}")
                bottle_x = bottle_info.get("x", 0)
                bottle_y = bottle_info.get("y", 0)
                associate_id = bottle_info.get("associateId")  # 关联订单ID
                
                if not bottle_material_id:
                    logger.warning(f"  瓶子[{idx}]: 缺少materialId，跳过")
                    continue
                
                # ✅ 创建瓶子资源（使用工厂函数）
                bottle_obj = YB_Vial_5mL(
                    name=f"{vial_plate_obj.name}_vial_{bottle_code.replace(' ', '_')}",
                    diameter=20.0,
                    height=50.0,
                    max_volume=5000.0,  # 5mL
                    barcode=None
                )
                
                # ✅ 设置UUID（用于LIMS同步）
                bottle_obj.unilabos_uuid = bottle_material_id
                
                # ✅ 存储元数据（供扣电使用）
                bottle_obj._unilabos_state = {
                    "orderCode": order_code,
                    "materialId": bottle_material_id,
                    "code": bottle_code,
                    "position_x": bottle_x,
                    "position_y": bottle_y,
                    "associateId": associate_id
                }
                
                # ✅ 添加到瓶板（根据xy坐标计算索引）
                # 假设瓶板布局: x=1,2  y=1,2,3,4 (2x4布局)
                bottle_index = (bottle_x - 1) * 4 + (bottle_y - 1)
                
                if 0 <= bottle_index < len(vial_plate_obj.children):
                    vial_plate_obj.children[bottle_index] = bottle_obj
                    created_count += 1
                    logger.debug(
                        f"  瓶子[{idx}]: code={bottle_code}, "
                        f"位置=({bottle_x},{bottle_y}), 索引={bottle_index}"
                    )
                else:
                    logger.warning(
                        f"  瓶子[{idx}]: 索引超出范围 ({bottle_index} >= {len(vial_plate_obj.children)})"
                    )
                    
            except Exception as e:
                logger.warning(f"  瓶子[{idx}]: 创建失败 - {e}")
                continue
        
        logger.info(f"[资源树] ✅ 已创建 {created_count}/{len(bottles_detail)} 个瓶子资源")
    
    def transfer_3_to_2_to_1_auto(
        self,
        vial_plates: List[Dict],
        target_device: str = "BatteryStation",
        target_location: str = "bottle_rack_6x2",
        mass_ratios: List[Dict] = None,  # ✅ 新增：配方信息（用于瓶子放置位置映射）
        **kwargs  # 兼容性参数，捕获已废弃的 vial_plate_info 等参数
    ) -> Dict[str, Any]:
        """
        自动转运（从 create_orders 结果自动定位源位置）
        
        Args:
            vial_plates: 分液瓶板列表
                格式: [{"materialId": "...", "locationId": "...", "orderCode": "..."}, ...]
            target_device: 目标设备ID
            target_location: 目标资源名称
            mass_ratios: 配方信息列表（可选），用于确定瓶子在bottle_rack的位置
                格式: [{"orderCode": "...", "real_mass_ratio": {...}, ...}, ...]
            **kwargs: 兼容性参数，用于捕获已废弃的参数（如 vial_plate_info）
        
        Returns:
            {
                "total": 转运总数,
                "success": 成功数量,
                "failed": 失败数量,
                "results": [每个转运的详细结果]
            }
        """
        # 检查是否传递了已废弃的参数
        if kwargs:
            logger.warning(
                f"[transfer_3_to_2_to_1_auto] ⚠️ 检测到已废弃的参数: {list(kwargs.keys())}, "
                f"这些参数将被忽略"
            )
        
        # ========== 参数验证 ==========
        if not vial_plates:
            raise ValueError("vial_plates 参数不能为空")
        
        logger.info("=" * 80)
        logger.info(f"[transfer_3_to_2_to_1_auto] 接收到 {len(vial_plates)} 个分液瓶板")
        for idx, plate in enumerate(vial_plates, 1):
            logger.info(
                f"  [{idx}] orderCode={plate.get('orderCode', 'N/A')}, "
                f"materialId={plate.get('materialId', 'N/A')[:20]}..."
            )
        logger.info("=" * 80)
        
        # ========== 步骤2：依次转运每个分液瓶板（去重，同一瓶板只转运一次）==========
        results = []
        success_count = 0
        failed_count = 0
        transferred_material_ids = set()  # ✅ 记录已转运的materialId
        
        logger.info(
            f"[批量转运] 开始转运 {len(vial_plates)} 个订单的分液瓶板 → "
            f"{target_device}.{target_location}"
        )
        
        for idx, plate_info in enumerate(vial_plates, 1):
            try:
                # ✅ 检查 plate_info 是否有效
                if not plate_info or not isinstance(plate_info, dict):
                    logger.error(
                        f"[批量转运] ❌ [{idx}/{len(vial_plates)}] 分液瓶板信息无效: {plate_info}"
                    )
                    results.append({
                        "index": idx,
                        "orderCode": "N/A",
                        "materialId": "N/A",
                        "status": "failed",
                        "error": "分液瓶板信息无效或为空"
                    })
                    failed_count += 1
                    continue
                
                material_id = plate_info.get('materialId')
                order_code = plate_info.get('orderCode', 'N/A')
                
                logger.info(f"\n{'='*60}")
                logger.info(f"[批量转运] 处理 [{idx}/{len(vial_plates)}]")
                logger.info(f"  orderCode: {order_code}")
                logger.info(f"  materialId: {material_id[:20] if material_id else 'N/A'}...")
                
                # ✅ 检查是否已转运（同一物理瓶板只转运一次）
                if material_id in transferred_material_ids:
                    logger.info(
                        f"  ℹ️ 该瓶板已转运，跳过 (多订单共用同一瓶板)"
                    )
                    results.append({
                        "index": idx,
                        "orderCode": order_code,
                        "materialId": material_id,
                        "status": "skipped",
                        "message": "该瓶板已转运（共用瓶板）"
                    })
                    success_count += 1  # 视为成功
                    logger.info(f"{'='*60}")
                    continue
                
                logger.info(f"{'='*60}")
                
                # 调用单个转运逻辑
                result = self._transfer_single_vial_plate(
                    vial_plate_info=plate_info,
                    target_device=target_device,
                    target_location=target_location
                )
                
                transferred_material_ids.add(material_id)
                results.append({
                    "index": idx,
                    "orderCode": order_code,
                    "materialId": material_id,
                    "status": "success",
                    "result": result
                })
                success_count += 1
                logger.info(f"[批量转运] ✅ [{idx}/{len(vial_plates)}] 转运成功")
                
            except Exception as e:
                logger.error(
                    f"[批量转运] ❌ [{idx}/{len(vial_plates)}] 失败: {str(e)}"
                )
                results.append({
                    "index": idx,
                    "orderCode": plate_info.get("orderCode", "N/A") if plate_info else "N/A",
                    "materialId": plate_info.get("materialId", "N/A") if plate_info else "N/A",
                    "status": "failed",
                    "error": str(e)
                })
                failed_count += 1
        
        # ========== 步骤3：汇总结果 ==========
        summary = {
            "total": len(vial_plates),
            "success": success_count,
            "failed": failed_count,
            "results": results
        }
        
        logger.info(f"\n{'='*60}")
        logger.info(f"[批量转运] 完成汇总:")
        logger.info(f"  总数: {summary['total']}")
        logger.info(f"  成功: {summary['success']} ✅")
        logger.info(f"  失败: {summary['failed']} ❌")
        logger.info(f"{'='*60}\n")
        
        return summary
    
    def _transfer_single_vial_plate(
        self,
        vial_plate_info: Dict,
        target_device: str,
        target_location: str
    ) -> Dict[str, Any]:
        """
        转运单个分液瓶板（内部方法）
        
        Args:
            vial_plate_info: 单个分液瓶板信息
            target_device: 目标设备ID
            target_location: 目标资源名称
        
        Returns:
            LIMS转运结果
        """
        location_id = vial_plate_info["locationId"]
        material_id = vial_plate_info["materialId"]
        
        # 步骤1：locationId → warehouse名称 + 槽位名称
        wh_name, slot_name = self._get_warehouse_and_slot_from_location_id(location_id)
        
        if not wh_name or not slot_name:
            raise ValueError(f"无法从 locationId 解析仓库和槽位: {location_id}")
        
        logger.info(
            f"[自动转运] 分液瓶板位置: {wh_name}[{slot_name}], "
            f"materialId={material_id}"
        )
        
        # 步骤2：获取 warehouse_id
        warehouse_id = self._get_warehouse_id(wh_name)
        
        # 步骤3：槽位名称 → 坐标
        x, y, z = self._slot_to_coordinates(slot_name)
        logger.info(f"[自动转运] 坐标: ({x}, {y}, {z})")
        
        # 步骤4：调用物理转运
        lims_result = self.transfer_3_to_2_to_1(
            source_wh_id=warehouse_id,
            source_x=x,
            source_y=y,
            source_z=z
        )
        logger.info(f"[LIMS转运] 完成: {lims_result}")
        
        # 步骤5：资源树数字转运
        try:
            # 获取 warehouse 对象
            warehouse = self.deck.get_resource(wh_name)
            if not warehouse:
                raise ValueError(f"资源树中未找到仓库: {wh_name}")
            
            # 通过槽位名称直接访问
            vial_plate = warehouse[slot_name]
            
            if vial_plate:
                # ========== 获取目标资源对象 ==========
                logger.info(
                    f"[资源同步] 准备目标资源: {target_device}.{target_location}"
                )

                # 从目标设备的资源树中获取真实的接驳槽对象（electrolyte_buffer）
                target_resource_obj = self._get_resource_from_device(
                    device_id=target_device,
                    resource_name=target_location,
                )
                if target_resource_obj is None:
                    raise RuntimeError(
                        f"[资源同步] 目标设备 '{target_device}' 中未找到资源 '{target_location}'。"
                        f"请确认 YihuaCoinCellDeck.setup() 中已添加 electrolyte_buffer 槽位，"
                        f"且目标节点已启动并完成资源树初始化。"
                    )

                logger.info(
                    f"[资源同步] 找到目标资源: {target_resource_obj.name}, "
                    f"UUID={getattr(target_resource_obj, 'unilabos_uuid', 'N/A')}"
                )

                # 执行资源树转移
                self.transfer_resource_to_another(
                    resource=[vial_plate],
                    mount_resource=[target_resource_obj],
                    sites=["electrolyte_buffer"],
                    mount_device_id=f"/devices/{target_device}"
                )
                logger.info(
                    f"[资源同步] ✅ 成功: {vial_plate.name} → "
                    f"{target_device}.{target_location}"
                )
            else:
                logger.warning(
                    f"[资源同步] ⚠️ 警告: {wh_name}[{slot_name}] 槽位为空, "
                    f"可能资源树未及时更新"
                )
        except Exception as e:
            logger.error(f"[资源同步] ❌ 失败: {e}")
            # 不中断流程，物理转运已完成
        
        return lims_result
    
    def _get_resource_from_device(
        self,
        device_id: str,
        resource_name: str,
    ):
        """
        从指定设备的本地资源树中按名称查找 PLR 资源对象。

        Args:
            device_id: 目标设备 ID（如 "BatteryStation"）
            resource_name: 资源名称（如 "electrolyte_buffer"）

        Returns:
            找到的 PLR Resource 对象，未找到则返回 None
        """
        try:
            from unilabos.app.ros2_app import get_device_plr_resource_by_name
            return get_device_plr_resource_by_name(device_id, resource_name)
        except Exception:
            pass

        # 降级：遍历 workstation 已注册的 plr_resources 列表
        try:
            for res in getattr(self, "_plr_resources", []):
                if res.name == resource_name:
                    return res
                found = res.get_resource(resource_name) if hasattr(res, "get_resource") else None
                if found is not None:
                    return found
        except Exception:
            pass

        return None

    def _get_warehouse_and_slot_from_location_id(
        self,
        location_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        从 locationId 解析仓库名称和槽位名称
        
        Args:
            location_id: site_uuid, 例如 "3a19debc-84b5-4c1c-d3a1-26830cf273ff"
        
        Returns:
            (warehouse_name, slot_name)
            例如：("自动堆栈-左", "A01")
        """
        warehouse_mapping = self.bioyond_config.get("warehouse_mapping", {})
        
        for wh_name, wh_data in warehouse_mapping.items():
            site_uuids = wh_data.get("site_uuids", {})
            for slot_name, site_uuid in site_uuids.items():
                if site_uuid == location_id:
                    return (wh_name, slot_name)
        
        logger.error(f"未找到 locationId: {location_id}")
        return (None, None)
    
    def _get_warehouse_id(self, warehouse_name: str) -> str:
        """
        获取仓库的 warehouse_id (uuid)
        
        带降级逻辑：如果配置缺失，使用默认值（自动堆栈-左）
        
        Args:
            warehouse_name: 仓库名称，例如 "自动堆栈-左"
        
        Returns:
            warehouse_id
        """
        warehouse_mapping = self.bioyond_config.get("warehouse_mapping", {})
        wh_data = warehouse_mapping.get(warehouse_name, {})
        warehouse_id = wh_data.get("uuid")
        
        if not warehouse_id:
            # 降级：使用默认值
            default_uuid = "3a19debc-84b4-0359-e2d4-b3beea49348b"
            logger.warning(
                f"仓库 '{warehouse_name}' 的 uuid 未配置, "
                f"使用默认值: {default_uuid}"
            )
            warehouse_id = default_uuid
        
        return warehouse_id
    
    def _slot_to_coordinates(self, slot_name: str) -> Tuple[int, int, int]:
        """
        槽位名称 → LIMS坐标
        
        Args:
            slot_name: 槽位名称，例如 "A01", "B02", "E03"
        
        Returns:
            (x, y, z) 坐标元组
        
        转换规则：
            - 字母 → x (A=1, B=2, C=3...)
            - 数字 → y (01=1, 02=2, 03=3...)
            - z 固定为 1
        
        Examples:
            >>> _slot_to_coordinates("A01")
            (1, 1, 1)
            >>> _slot_to_coordinates("B02")
            (2, 2, 1)
            >>> _slot_to_coordinates("E03")
            (5, 3, 1)
        """
        if not slot_name or len(slot_name) < 2:
            raise ValueError(f"Invalid slot name: {slot_name}")
        
        letter = slot_name[0].upper()  # 'A', 'B', 'C'...
        number_str = slot_name[1:]     # '01', '02', '03'...
        
        # 字母 → x
        x = ord(letter) - ord('A') + 1
        
        # 数字 → y
        y = int(number_str)
        
        # z 固定为 1
        z = 1
        
        return (x, y, z)


    # 2.7 启动调度
    def scheduler_start(self) -> Dict[str, Any]:
        return self._post_lims("/api/lims/scheduler/start")
    # 3.10 停止调度
    def scheduler_stop(self) -> Dict[str, Any]:

        """
        停止调度 (3.10)
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/stop")
         
    # 2.9 继续调度
    def scheduler_continue(self) -> Dict[str, Any]:
        """
        继续调度 (2.9)
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/continue")
    def scheduler_reset(self) -> Dict[str, Any]:
        """
        复位调度 (2.11)
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/reset")

    def scheduler_start_and_auto_feeding(
        self,
        # ★ Excel路径参数
        xlsx_path: Optional[str] = "D:\\UniLab\\Uni-Lab-OS\\unilabos\\devices\\workstation\\bioyond_studio\\bioyond_cell\\material_template.xlsx",
        # ---------------- WH4 - 加样头面 (Z=1, 12个点位) ----------------
        WH4_x1_y1_z1_1_materialName: str = "", WH4_x1_y1_z1_1_quantity: float = 0.0,
        WH4_x2_y1_z1_2_materialName: str = "", WH4_x2_y1_z1_2_quantity: float = 0.0,
        WH4_x3_y1_z1_3_materialName: str = "", WH4_x3_y1_z1_3_quantity: float = 0.0,
        WH4_x4_y1_z1_4_materialName: str = "", WH4_x4_y1_z1_4_quantity: float = 0.0,
        WH4_x5_y1_z1_5_materialName: str = "", WH4_x5_y1_z1_5_quantity: float = 0.0,
        WH4_x1_y2_z1_6_materialName: str = "", WH4_x1_y2_z1_6_quantity: float = 0.0,
        WH4_x2_y2_z1_7_materialName: str = "", WH4_x2_y2_z1_7_quantity: float = 0.0,
        WH4_x3_y2_z1_8_materialName: str = "", WH4_x3_y2_z1_8_quantity: float = 0.0,
        WH4_x4_y2_z1_9_materialName: str = "", WH4_x4_y2_z1_9_quantity: float = 0.0,
        WH4_x5_y2_z1_10_materialName: str = "", WH4_x5_y2_z1_10_quantity: float = 0.0,
        WH4_x1_y3_z1_11_materialName: str = "", WH4_x1_y3_z1_11_quantity: float = 0.0,
        WH4_x2_y3_z1_12_materialName: str = "", WH4_x2_y3_z1_12_quantity: float = 0.0,

        # ---------------- WH4 - 原液瓶面 (Z=2, 9个点位) ----------------
        WH4_x1_y1_z2_1_materialName: str = "", WH4_x1_y1_z2_1_quantity: float = 0.0, WH4_x1_y1_z2_1_materialType: str = "", WH4_x1_y1_z2_1_targetWH: str = "",
        WH4_x2_y1_z2_2_materialName: str = "", WH4_x2_y1_z2_2_quantity: float = 0.0, WH4_x2_y1_z2_2_materialType: str = "", WH4_x2_y1_z2_2_targetWH: str = "",
        WH4_x3_y1_z2_3_materialName: str = "", WH4_x3_y1_z2_3_quantity: float = 0.0, WH4_x3_y1_z2_3_materialType: str = "", WH4_x3_y1_z2_3_targetWH: str = "",
        WH4_x1_y2_z2_4_materialName: str = "", WH4_x1_y2_z2_4_quantity: float = 0.0, WH4_x1_y2_z2_4_materialType: str = "", WH4_x1_y2_z2_4_targetWH: str = "",
        WH4_x2_y2_z2_5_materialName: str = "", WH4_x2_y2_z2_5_quantity: float = 0.0, WH4_x2_y2_z2_5_materialType: str = "", WH4_x2_y2_z2_5_targetWH: str = "",
        WH4_x3_y2_z2_6_materialName: str = "", WH4_x3_y2_z2_6_quantity: float = 0.0, WH4_x3_y2_z2_6_materialType: str = "", WH4_x3_y2_z2_6_targetWH: str = "",
        WH4_x1_y3_z2_7_materialName: str = "", WH4_x1_y3_z2_7_quantity: float = 0.0, WH4_x1_y3_z2_7_materialType: str = "", WH4_x1_y3_z2_7_targetWH: str = "",
        WH4_x2_y3_z2_8_materialName: str = "", WH4_x2_y3_z2_8_quantity: float = 0.0, WH4_x2_y3_z2_8_materialType: str = "", WH4_x2_y3_z2_8_targetWH: str = "",
        WH4_x3_y3_z2_9_materialName: str = "", WH4_x3_y3_z2_9_quantity: float = 0.0, WH4_x3_y3_z2_9_materialType: str = "", WH4_x3_y3_z2_9_targetWH: str = "",

        # ---------------- WH3 - 人工堆栈 (Z=3, 15个点位) ----------------
        WH3_x1_y1_z3_1_materialType: str = "", WH3_x1_y1_z3_1_materialId: str = "", WH3_x1_y1_z3_1_quantity: float = 0,
        WH3_x2_y1_z3_2_materialType: str = "", WH3_x2_y1_z3_2_materialId: str = "", WH3_x2_y1_z3_2_quantity: float = 0,
        WH3_x3_y1_z3_3_materialType: str = "", WH3_x3_y1_z3_3_materialId: str = "", WH3_x3_y1_z3_3_quantity: float = 0,
        WH3_x1_y2_z3_4_materialType: str = "", WH3_x1_y2_z3_4_materialId: str = "", WH3_x1_y2_z3_4_quantity: float = 0,
        WH3_x2_y2_z3_5_materialType: str = "", WH3_x2_y2_z3_5_materialId: str = "", WH3_x2_y2_z3_5_quantity: float = 0,
        WH3_x3_y2_z3_6_materialType: str = "", WH3_x3_y2_z3_6_materialId: str = "", WH3_x3_y2_z3_6_quantity: float = 0,
        WH3_x1_y3_z3_7_materialType: str = "", WH3_x1_y3_z3_7_materialId: str = "", WH3_x1_y3_z3_7_quantity: float = 0,
        WH3_x2_y3_z3_8_materialType: str = "", WH3_x2_y3_z3_8_materialId: str = "", WH3_x2_y3_z3_8_quantity: float = 0,
        WH3_x3_y3_z3_9_materialType: str = "", WH3_x3_y3_z3_9_materialId: str = "", WH3_x3_y3_z3_9_quantity: float = 0,
        WH3_x1_y4_z3_10_materialType: str = "", WH3_x1_y4_z3_10_materialId: str = "", WH3_x1_y4_z3_10_quantity: float = 0,
        WH3_x2_y4_z3_11_materialType: str = "", WH3_x2_y4_z3_11_materialId: str = "", WH3_x2_y4_z3_11_quantity: float = 0,
        WH3_x3_y4_z3_12_materialType: str = "", WH3_x3_y4_z3_12_materialId: str = "", WH3_x3_y4_z3_12_quantity: float = 0,
        WH3_x1_y5_z3_13_materialType: str = "", WH3_x1_y5_z3_13_materialId: str = "", WH3_x1_y5_z3_13_quantity: float = 0,
        WH3_x2_y5_z3_14_materialType: str = "", WH3_x2_y5_z3_14_materialId: str = "", WH3_x2_y5_z3_14_quantity: float = 0,
        WH3_x3_y5_z3_15_materialType: str = "", WH3_x3_y5_z3_15_materialId: str = "", WH3_x3_y5_z3_15_quantity: float = 0,
    ) -> Dict[str, Any]:
        """
        组合函数：先启动调度，然后执行自动化上料
        
        此函数简化了工作流操作，将两个有顺序依赖的操作组合在一起：
        1. 启动调度（scheduler_start）
        2. 自动化上料（auto_feeding4to3）
        
        参数与 auto_feeding4to3 完全相同，支持 Excel 和手动参数两种模式
        
        Returns:
            包含调度启动结果和上料结果的字典
        """
        logger.info("=" * 60)
        logger.info("开始执行组合操作：启动调度 + 自动化上料")
        logger.info("=" * 60)
        
        # 步骤1: 启动调度
        logger.info("【步骤 1/2】启动调度...")
        scheduler_result = self.scheduler_start()
        logger.info(f"调度启动结果: {scheduler_result}")
        
        # 检查调度是否启动成功
        if scheduler_result.get("code") != 1:
            logger.error(f"调度启动失败: {scheduler_result}")
            return {
                "success": False,
                "step": "scheduler_start",
                "scheduler_result": scheduler_result,
                "error": "调度启动失败"
            }
        
        logger.info("✓ 调度启动成功")
        
        # 步骤2: 执行自动化上料
        logger.info("【步骤 2/2】执行自动化上料...")
        feeding_result = self.auto_feeding4to3(
            xlsx_path=xlsx_path,
            WH4_x1_y1_z1_1_materialName=WH4_x1_y1_z1_1_materialName, WH4_x1_y1_z1_1_quantity=WH4_x1_y1_z1_1_quantity,
            WH4_x2_y1_z1_2_materialName=WH4_x2_y1_z1_2_materialName, WH4_x2_y1_z1_2_quantity=WH4_x2_y1_z1_2_quantity,
            WH4_x3_y1_z1_3_materialName=WH4_x3_y1_z1_3_materialName, WH4_x3_y1_z1_3_quantity=WH4_x3_y1_z1_3_quantity,
            WH4_x4_y1_z1_4_materialName=WH4_x4_y1_z1_4_materialName, WH4_x4_y1_z1_4_quantity=WH4_x4_y1_z1_4_quantity,
            WH4_x5_y1_z1_5_materialName=WH4_x5_y1_z1_5_materialName, WH4_x5_y1_z1_5_quantity=WH4_x5_y1_z1_5_quantity,
            WH4_x1_y2_z1_6_materialName=WH4_x1_y2_z1_6_materialName, WH4_x1_y2_z1_6_quantity=WH4_x1_y2_z1_6_quantity,
            WH4_x2_y2_z1_7_materialName=WH4_x2_y2_z1_7_materialName, WH4_x2_y2_z1_7_quantity=WH4_x2_y2_z1_7_quantity,
            WH4_x3_y2_z1_8_materialName=WH4_x3_y2_z1_8_materialName, WH4_x3_y2_z1_8_quantity=WH4_x3_y2_z1_8_quantity,
            WH4_x4_y2_z1_9_materialName=WH4_x4_y2_z1_9_materialName, WH4_x4_y2_z1_9_quantity=WH4_x4_y2_z1_9_quantity,
            WH4_x5_y2_z1_10_materialName=WH4_x5_y2_z1_10_materialName, WH4_x5_y2_z1_10_quantity=WH4_x5_y2_z1_10_quantity,
            WH4_x1_y3_z1_11_materialName=WH4_x1_y3_z1_11_materialName, WH4_x1_y3_z1_11_quantity=WH4_x1_y3_z1_11_quantity,
            WH4_x2_y3_z1_12_materialName=WH4_x2_y3_z1_12_materialName, WH4_x2_y3_z1_12_quantity=WH4_x2_y3_z1_12_quantity,
            WH4_x1_y1_z2_1_materialName=WH4_x1_y1_z2_1_materialName, WH4_x1_y1_z2_1_quantity=WH4_x1_y1_z2_1_quantity, 
            WH4_x1_y1_z2_1_materialType=WH4_x1_y1_z2_1_materialType, WH4_x1_y1_z2_1_targetWH=WH4_x1_y1_z2_1_targetWH,
            WH4_x2_y1_z2_2_materialName=WH4_x2_y1_z2_2_materialName, WH4_x2_y1_z2_2_quantity=WH4_x2_y1_z2_2_quantity, 
            WH4_x2_y1_z2_2_materialType=WH4_x2_y1_z2_2_materialType, WH4_x2_y1_z2_2_targetWH=WH4_x2_y1_z2_2_targetWH,
            WH4_x3_y1_z2_3_materialName=WH4_x3_y1_z2_3_materialName, WH4_x3_y1_z2_3_quantity=WH4_x3_y1_z2_3_quantity, 
            WH4_x3_y1_z2_3_materialType=WH4_x3_y1_z2_3_materialType, WH4_x3_y1_z2_3_targetWH=WH4_x3_y1_z2_3_targetWH,
            WH4_x1_y2_z2_4_materialName=WH4_x1_y2_z2_4_materialName, WH4_x1_y2_z2_4_quantity=WH4_x1_y2_z2_4_quantity, 
            WH4_x1_y2_z2_4_materialType=WH4_x1_y2_z2_4_materialType, WH4_x1_y2_z2_4_targetWH=WH4_x1_y2_z2_4_targetWH,
            WH4_x2_y2_z2_5_materialName=WH4_x2_y2_z2_5_materialName, WH4_x2_y2_z2_5_quantity=WH4_x2_y2_z2_5_quantity, 
            WH4_x2_y2_z2_5_materialType=WH4_x2_y2_z2_5_materialType, WH4_x2_y2_z2_5_targetWH=WH4_x2_y2_z2_5_targetWH,
            WH4_x3_y2_z2_6_materialName=WH4_x3_y2_z2_6_materialName, WH4_x3_y2_z2_6_quantity=WH4_x3_y2_z2_6_quantity, 
            WH4_x3_y2_z2_6_materialType=WH4_x3_y2_z2_6_materialType, WH4_x3_y2_z2_6_targetWH=WH4_x3_y2_z2_6_targetWH,
            WH4_x1_y3_z2_7_materialName=WH4_x1_y3_z2_7_materialName, WH4_x1_y3_z2_7_quantity=WH4_x1_y3_z2_7_quantity, 
            WH4_x1_y3_z2_7_materialType=WH4_x1_y3_z2_7_materialType, WH4_x1_y3_z2_7_targetWH=WH4_x1_y3_z2_7_targetWH,
            WH4_x2_y3_z2_8_materialName=WH4_x2_y3_z2_8_materialName, WH4_x2_y3_z2_8_quantity=WH4_x2_y3_z2_8_quantity, 
            WH4_x2_y3_z2_8_materialType=WH4_x2_y3_z2_8_materialType, WH4_x2_y3_z2_8_targetWH=WH4_x2_y3_z2_8_targetWH,
            WH4_x3_y3_z2_9_materialName=WH4_x3_y3_z2_9_materialName, WH4_x3_y3_z2_9_quantity=WH4_x3_y3_z2_9_quantity, 
            WH4_x3_y3_z2_9_materialType=WH4_x3_y3_z2_9_materialType, WH4_x3_y3_z2_9_targetWH=WH4_x3_y3_z2_9_targetWH,
            WH3_x1_y1_z3_1_materialType=WH3_x1_y1_z3_1_materialType, WH3_x1_y1_z3_1_materialId=WH3_x1_y1_z3_1_materialId, WH3_x1_y1_z3_1_quantity=WH3_x1_y1_z3_1_quantity,
            WH3_x2_y1_z3_2_materialType=WH3_x2_y1_z3_2_materialType, WH3_x2_y1_z3_2_materialId=WH3_x2_y1_z3_2_materialId, WH3_x2_y1_z3_2_quantity=WH3_x2_y1_z3_2_quantity,
            WH3_x3_y1_z3_3_materialType=WH3_x3_y1_z3_3_materialType, WH3_x3_y1_z3_3_materialId=WH3_x3_y1_z3_3_materialId, WH3_x3_y1_z3_3_quantity=WH3_x3_y1_z3_3_quantity,
            WH3_x1_y2_z3_4_materialType=WH3_x1_y2_z3_4_materialType, WH3_x1_y2_z3_4_materialId=WH3_x1_y2_z3_4_materialId, WH3_x1_y2_z3_4_quantity=WH3_x1_y2_z3_4_quantity,
            WH3_x2_y2_z3_5_materialType=WH3_x2_y2_z3_5_materialType, WH3_x2_y2_z3_5_materialId=WH3_x2_y2_z3_5_materialId, WH3_x2_y2_z3_5_quantity=WH3_x2_y2_z3_5_quantity,
            WH3_x3_y2_z3_6_materialType=WH3_x3_y2_z3_6_materialType, WH3_x3_y2_z3_6_materialId=WH3_x3_y2_z3_6_materialId, WH3_x3_y2_z3_6_quantity=WH3_x3_y2_z3_6_quantity,
            WH3_x1_y3_z3_7_materialType=WH3_x1_y3_z3_7_materialType, WH3_x1_y3_z3_7_materialId=WH3_x1_y3_z3_7_materialId, WH3_x1_y3_z3_7_quantity=WH3_x1_y3_z3_7_quantity,
            WH3_x2_y3_z3_8_materialType=WH3_x2_y3_z3_8_materialType, WH3_x2_y3_z3_8_materialId=WH3_x2_y3_z3_8_materialId, WH3_x2_y3_z3_8_quantity=WH3_x2_y3_z3_8_quantity,
            WH3_x3_y3_z3_9_materialType=WH3_x3_y3_z3_9_materialType, WH3_x3_y3_z3_9_materialId=WH3_x3_y3_z3_9_materialId, WH3_x3_y3_z3_9_quantity=WH3_x3_y3_z3_9_quantity,
            WH3_x1_y4_z3_10_materialType=WH3_x1_y4_z3_10_materialType, WH3_x1_y4_z3_10_materialId=WH3_x1_y4_z3_10_materialId, WH3_x1_y4_z3_10_quantity=WH3_x1_y4_z3_10_quantity,
            WH3_x2_y4_z3_11_materialType=WH3_x2_y4_z3_11_materialType, WH3_x2_y4_z3_11_materialId=WH3_x2_y4_z3_11_materialId, WH3_x2_y4_z3_11_quantity=WH3_x2_y4_z3_11_quantity,
            WH3_x3_y4_z3_12_materialType=WH3_x3_y4_z3_12_materialType, WH3_x3_y4_z3_12_materialId=WH3_x3_y4_z3_12_materialId, WH3_x3_y4_z3_12_quantity=WH3_x3_y4_z3_12_quantity,
            WH3_x1_y5_z3_13_materialType=WH3_x1_y5_z3_13_materialType, WH3_x1_y5_z3_13_materialId=WH3_x1_y5_z3_13_materialId, WH3_x1_y5_z3_13_quantity=WH3_x1_y5_z3_13_quantity,
            WH3_x2_y5_z3_14_materialType=WH3_x2_y5_z3_14_materialType, WH3_x2_y5_z3_14_materialId=WH3_x2_y5_z3_14_materialId, WH3_x2_y5_z3_14_quantity=WH3_x2_y5_z3_14_quantity,
            WH3_x3_y5_z3_15_materialType=WH3_x3_y5_z3_15_materialType, WH3_x3_y5_z3_15_materialId=WH3_x3_y5_z3_15_materialId, WH3_x3_y5_z3_15_quantity=WH3_x3_y5_z3_15_quantity,
        )
        
        logger.info("=" * 60)
        logger.info("组合操作完成")
        logger.info("=" * 60)
        
        return {
            "success": True,
            "scheduler_result": scheduler_result,
            "feeding_result": feeding_result
        }


    # 2.24 物料变更推送
    def report_material_change(self, material_obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        material_obj 按 2.24 的裸对象格式（包含 id/typeName/locations/detail 等）
        """
        return self._post_report_raw("/report/material_change", material_obj)

    # 2.32 3-2-1 物料转运
    def transfer_3_to_2_to_1(self,
                            #  source_wh_id: Optional[str] = None,
                            source_wh_id: Optional[str] = '3a19debc-84b4-0359-e2d4-b3beea49348b',
                             source_x: int = 1, source_y: int = 1, source_z: int = 1) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "sourcePosX": source_x, "sourcePosY": source_y, "sourcePosZ": source_z
        }
        if source_wh_id:
            payload["sourceWHID"] = source_wh_id

        response = self._post_lims("/api/lims/order/transfer-task3To2To1", payload)
        # 等待任务报送成功
        order_code = response.get("data", {}).get("orderCode")
        if not order_code:
            logger.error("上料任务未返回有效 orderCode！")
            return response
          # 等待完成报送
        result = self.wait_for_order_finish(order_code)
        return result

    def transfer_3_to_2(self,
                        source_wh_id: Optional[str] = '3a19debc-84b4-0359-e2d4-b3beea49348b',
                        source_x: int = 1, 
                        source_y: int = 1, 
                        source_z: int = 1) -> Dict[str, Any]:
        """
        2.34 3-2 物料转运接口
        
        新建从 3 -> 2 的搬运任务
        
        Args:
            source_wh_id: 来源仓库 Id (默认为3号仓库)
            source_x: 来源位置 X 坐标
            source_y: 来源位置 Y 坐标
            source_z: 来源位置 Z 坐标
            
        Returns:
            dict: 包含任务 orderId 和 orderCode 的响应
        """
        payload: Dict[str, Any] = {
            "sourcePosX": source_x, 
            "sourcePosY": source_y, 
            "sourcePosZ": source_z
        }
        if source_wh_id:
            payload["sourceWHID"] = source_wh_id

        logger.info(f"[transfer_3_to_2] 开始转运: 仓库={source_wh_id}, 位置=({source_x}, {source_y}, {source_z})")
        response = self._post_lims("/api/lims/order/transfer-task3To2", payload)
        
        # 等待任务报送成功
        order_code = response.get("data", {}).get("orderCode")
        if not order_code:
            logger.error("[transfer_3_to_2] 转运任务未返回有效 orderCode！")
            return response
        
        logger.info(f"[transfer_3_to_2] 转运任务已创建: {order_code}")
        # 等待完成报送
        result = self.wait_for_order_finish(order_code)
        logger.info(f"[transfer_3_to_2] 转运任务完成: {order_code}")
        return result

    # 3.35 1→2 物料转运
    def transfer_1_to_2(self) -> Dict[str, Any]:
        """
        1→2 物料转运
        URL: /api/lims/order/transfer-task1To2
        只需要 apiKey 和 requestTime
        """
        logger.info("[transfer_1_to_2] 开始 1→2 物料转运")
        response = self._post_lims("/api/lims/order/transfer-task1To2")
        logger.info(f"[transfer_1_to_2] API Response: {response}")
        
        # 等待任务报送成功 - 处理不同的响应格式
        order_code = None
        data_field = response.get("data")
        
        if isinstance(data_field, dict):
            order_code = data_field.get("orderCode")
        elif isinstance(data_field, str):
            # 某些接口可能直接返回 orderCode 字符串
            order_code = data_field
        
        if not order_code:
            logger.error(f"[transfer_1_to_2] 转运任务未返回有效 orderCode！响应: {response}")
            return response
        
        logger.info(f"[transfer_1_to_2] 转运任务已创建: {order_code}")
        # 等待完成报送
        result = self.wait_for_order_finish(order_code)
        logger.info(f"[transfer_1_to_2] 转运任务完成: {order_code}")
        return result
   
    # 2.5 批量查询实验报告(post过滤关键字查询)
    def order_list_v2(self,
                      timeType: str = "",
                      beginTime: str = "",
                      endTime: str = "",
                      status: str = "", # 60表示正在运行,80表示完成，90表示失败
                      filter: str = "",
                      skipCount: int = 0,
                      pageCount: int = 1, # 显示多少页数据
                      sorting: str = "") -> Dict[str, Any]:
        """
        批量查询实验报告的详细信息 (2.5)
        URL: /api/lims/order/order-list
        参数默认值和接口文档保持一致
        """
        data: Dict[str, Any] = {
            "timeType": timeType,
            "beginTime": beginTime,
            "endTime": endTime,
            "status": status,
            "filter": filter,
            "skipCount": skipCount,
            "pageCount": pageCount,
            "sorting": sorting
        }
        return self._post_lims("/api/lims/order/order-list", data)

    # 一直post执行bioyond接口查询任务状态
    def wait_for_transfer_task(self, timeout: int = 3000, interval: int = 5, filter_text: Optional[str] = None) -> bool:
        """
        轮询查询物料转移任务是否成功完成 (status=80)
        - timeout: 最大等待秒数 (默认600秒)
        - interval: 轮询间隔秒数 (默认3秒)
        返回 True 表示找到并成功完成，False 表示超时未找到
        """
        now = datetime.now()
        beginTime = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        endTime = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(beginTime, endTime)

        deadline = time.time() + timeout

        while time.time() < deadline:
            result = self.order_list_v2(
                timeType="",
                beginTime=beginTime,
                endTime=endTime,
                status="",
                filter=filter_text,
                skipCount=0,
                pageCount=1,
                sorting=""
            )
            print(result)

            items = result.get("data", {}).get("items", [])
            for item in items:
                name = item.get("name", "")
                status = item.get("status")
                # 改成用 filter_text 判断
                if (not filter_text or filter_text in name) and status == 80:
                    logger.info(f"硬件转移动作完成: {name}, status={status}")
                    return True

                logger.info(f"等待中: {name}, status={status}")
            time.sleep(interval)

        logger.warning("超时未找到成功的物料转移任务")
        return False

    def create_materials(self, mappings: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        将 SOLID_LIQUID_MAPPINGS 中的所有物料逐个 POST 到 /api/lims/storage/material
        """
        results = []

        for name, data in mappings.items():
            data = {
                "typeId": data["typeId"],
                "code": data.get("code", ""),
                "barCode": data.get("barCode", ""),
                "name": data["name"],
                "unit": data.get("unit", "g"),
                "parameters": data.get("parameters", ""),
                "quantity": data.get("quantity", ""),
                "warningQuantity": data.get("warningQuantity", ""),
                "details": data.get("details", [])
            }
            
            logger.info(f"正在创建第 {i}/{total} 个固体物料: {name}")
            result = self._post_lims("/api/lims/storage/material", material_data)
            
            if result and result.get("code") == 1:
                # data 字段可能是字符串（物料ID）或字典（包含id字段）
                data = result.get("data")
                if isinstance(data, str):
                    # data 直接是物料ID字符串
                    material_id = data
                elif isinstance(data, dict):
                    # data 是字典，包含id字段
                    material_id = data.get("id")
                else:
                    material_id = None
                
                if material_id:
                    created_materials.append({
                        "name": name,
                        "materialId": material_id,
                        "typeId": type_id
                    })
                    logger.info(f"✓ 成功创建物料: {name}, ID: {material_id}")
                else:
                    logger.error(f"✗ 创建物料失败: {name}, 未返回ID")
                    logger.error(f"  响应数据: {result}")
            else:
                error_msg = result.get("error") or result.get("message", "未知错误")
                logger.error(f"✗ 创建物料失败: {name}")
                logger.error(f"  错误信息: {error_msg}")
                logger.error(f"  完整响应: {result}")
                
            # 避免请求过快
            time.sleep(0.3)
        
        logger.info(f"物料创建完成，成功创建 {len(created_materials)}/{total} 个固体物料")
        return created_materials

    def _sync_materials_safe(self) -> bool:
        """仅使用 BioyondResourceSynchronizer 执行同步（与 station.py 保持一致）。"""
        if hasattr(self, 'resource_synchronizer') and self.resource_synchronizer:
            try:
                return bool(self.resource_synchronizer.sync_from_external())
            except Exception as e:
                logger.error(f"同步失败: {e}")
                return False
        logger.warning("资源同步器未初始化")
        return False

    def _load_warehouse_locations(self, warehouse_name: str) -> tuple[List[str], List[str]]:
        """从配置加载仓库位置信息
        
        Args:
            warehouse_name: 仓库名称
            
        Returns:
            (location_ids, position_names) 元组
        """
        warehouse_mapping = self.bioyond_config.get("warehouse_mapping", WAREHOUSE_MAPPING)
        
        if warehouse_name not in warehouse_mapping:
            raise ValueError(f"配置中未找到仓库: {warehouse_name}。可用: {list(warehouse_mapping.keys())}")
        
        site_uuids = warehouse_mapping[warehouse_name].get("site_uuids", {})
        if not site_uuids:
            raise ValueError(f"仓库 {warehouse_name} 没有配置位置")
        
        # 按顺序获取位置ID和名称
        location_ids = []
        position_names = []
        for key in sorted(site_uuids.keys()):
            location_ids.append(site_uuids[key])
            position_names.append(key)
        
        return location_ids, position_names


    def create_and_inbound_materials(
        self,
        material_names: Optional[List[str]] = None,
        type_id: str = "3a190ca0-b2f6-9aeb-8067-547e72c11469",
        warehouse_name: str = "粉末加样头堆栈"
    ) -> Dict[str, Any]:
        """
        传参与默认列表方式创建物料并入库（不使用CSV）。

        Args:
            material_names: 物料名称列表；默认使用 [LiPF6, LiDFOB, DTD, LiFSI, LiPO2F2]
            type_id: 物料类型ID
            warehouse_name: 目标仓库名（用于取位置信息）

        Returns:
            执行结果字典
        """
        logger.info("=" * 60)
        logger.info(f"开始执行：从参数创建物料并批量入库到 {warehouse_name}")
        logger.info("=" * 60)

        try:
            # 1) 准备物料名称（默认值）
            default_materials = ["LiPF6", "LiDFOB", "DTD", "LiFSI", "LiPO2F2"]
            mat_names = [m.strip() for m in (material_names or default_materials) if str(m).strip()]
            if not mat_names:
                return {"success": False, "error": "物料名称列表为空"}

            # 2) 加载仓库位置信息
            all_location_ids, position_names = self._load_warehouse_locations(warehouse_name)
            logger.info(f"✓ 加载 {len(all_location_ids)} 个位置 ({position_names[0]} ~ {position_names[-1]})")

            # 限制数量不超过可用位置
            if len(mat_names) > len(all_location_ids):
                logger.warning(f"物料数量超出位置数量，仅处理前 {len(all_location_ids)} 个")
                mat_names = mat_names[:len(all_location_ids)]

            # 3) 创建物料
            logger.info(f"\n【步骤1/3】创建 {len(mat_names)} 个固体物料...")
            created_materials = self.create_solid_materials(mat_names, type_id)
            if not created_materials:
                return {"success": False, "error": "没有成功创建任何物料"}

            # 4) 批量入库
            logger.info(f"\n【步骤2/3】批量入库物料...")
            location_ids = all_location_ids[:len(created_materials)]
            selected_positions = position_names[:len(created_materials)]

            inbound_items = [
                {"materialId": mat["materialId"], "locationId": loc_id}
                for mat, loc_id in zip(created_materials, location_ids)
            ]

            for material, position in zip(created_materials, selected_positions):
                logger.info(f"  - {material['name']} → {position}")

            result = self.storage_batch_inbound(inbound_items)
            if result.get("code") != 1:
                logger.error(f"✗ 批量入库失败: {result}")
                return {"success": False, "error": "批量入库失败", "created_materials": created_materials, "inbound_result": result}

            logger.info("✓ 批量入库成功")

            # 5) 同步
            logger.info(f"\n【步骤3/3】同步物料数据...")
            if self._sync_materials_safe():
                logger.info("✓ 物料数据同步完成")
            else:
                logger.warning("⚠ 物料数据同步未完成（可忽略，不影响已创建与入库的数据）")

            logger.info("\n" + "=" * 60)
            logger.info("流程完成")
            logger.info("=" * 60 + "\n")

            return {
                "success": True,
                "created_materials": created_materials,
                "inbound_result": result,
                "total_created": len(created_materials),
                "total_inbound": len(inbound_items),
                "warehouse": warehouse_name,
                "positions": selected_positions
            }

        except Exception as e:
            logger.error(f"✗ 执行失败: {e}")
            return {"success": False, "error": str(e)}

    def create_material(
        self,
        material_name: str,
        type_id: str,
        warehouse_name: str,
        location_name_or_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """创建单个物料并可选入库。
        Args:
            material_name: 物料名称（会优先匹配配置模板）。
            type_id: 物料类型 ID（若为空则尝试从配置推断）。
            warehouse_name: 需要入库的仓库名称；若为空则仅创建不入库。
            location_name_or_id: 具体库位名称（如 A01）或库位 UUID，由用户指定。
        Returns:
            包含创建结果、物料ID以及入库结果的字典。
        """
        material_name = (material_name or "").strip()

        resolved_type_id = (type_id or "").strip()
        # 优先从配置中获取模板数据
        template = self.bioyond_config.get('solid_liquid_mappings', {}).get(material_name)
        if not template:
            raise ValueError(f"在配置中未找到物料 {material_name} 的模板，请检查 bioyond_config.solid_liquid_mappings。")
        material_data: Dict[str, Any]
        material_data = deepcopy(template)
        # 最终确保 typeId 为调用方传入的值
        if resolved_type_id:
            material_data["typeId"] = resolved_type_id
        material_data["name"] = material_name
        # 生成唯一编码
        def _generate_code(prefix: str) -> str:
            normalized = re.sub(r"\W+", "_", prefix)
            normalized = normalized.strip("_") or "material"
            return f"{normalized}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not material_data.get("code"):
            material_data["code"] = _generate_code(material_name)
        if not material_data.get("barCode"):
            material_data["barCode"] = ""
        # 处理数量字段类型
        def _to_number(value: Any, default: float = 0.0) -> float:
            try:
                if value is None:
                    return default
                if isinstance(value, (int, float)):
                    return float(value)
                if isinstance(value, str) and value.strip() == "":
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default
        material_data["quantity"] = _to_number(material_data.get("quantity"), 1.0)
        material_data["warningQuantity"] = _to_number(material_data.get("warningQuantity"), 0.0)
        unit = material_data.get("unit") or "个"
        material_data["unit"] = unit
        if not material_data.get("parameters"):
            material_data["parameters"] = json.dumps({"unit": unit}, ensure_ascii=False)
        # 补充子物料信息
        details = material_data.get("details") or []
        if not isinstance(details, list):
            logger.warning("details 字段不是列表，已忽略。")
            details = []
        else:
            for idx, detail in enumerate(details, start=1):
                if not isinstance(detail, dict):
                    continue
                if not detail.get("code"):
                    detail["code"] = f"{material_data['code']}_{idx:02d}"
                if not detail.get("name"):
                    detail["name"] = f"{material_name}_detail_{idx:02d}"
                if not detail.get("unit"):
                    detail["unit"] = unit
                if not detail.get("parameters"):
                    detail["parameters"] = json.dumps({"unit": detail.get("unit", unit)}, ensure_ascii=False)
                if "quantity" in detail:
                    detail["quantity"] = _to_number(detail.get("quantity"), 1.0)
        material_data["details"] = details
        create_result = self._post_lims("/api/lims/storage/material", material_data)
        # 解析创建结果中的物料 ID
        material_id: Optional[str] = None
        if isinstance(create_result, dict):
            data_field = create_result.get("data")
            if isinstance(data_field, str):
                material_id = data_field
            elif isinstance(data_field, dict):
                material_id = data_field.get("id") or data_field.get("materialId")
        inbound_result: Optional[Dict[str, Any]] = None
        location_id: Optional[str] = None
        # 按用户指定位置入库
        if warehouse_name and material_id and location_name_or_id:
            try:
                location_ids, position_names = self._load_warehouse_locations(warehouse_name)
                position_to_id = {name: loc_id for name, loc_id in zip(position_names, location_ids)}
                target_location_id = position_to_id.get(location_name_or_id, location_name_or_id)
                if target_location_id:
                    location_id = target_location_id
                    inbound_result = self.storage_inbound(material_id, target_location_id)
                else:
                    inbound_result = {"error": f"未找到匹配的库位: {location_name_or_id}"}
            except Exception as exc:
                logger.error(f"获取仓库 {warehouse_name} 位置失败: {exc}")
                inbound_result = {"error": str(exc)}
        return {
            "success": bool(isinstance(create_result, dict) and create_result.get("code") == 1 and material_id),
            "material_name": material_name,
            "material_id": material_id,
            "warehouse": warehouse_name,
            "location_id": location_id,
            "location_name_or_id": location_name_or_id,
            "create_result": create_result,
            "inbound_result": inbound_result,
        }
    def resource_tree_transfer(self, old_parent: ResourcePLR, plr_resource: ResourcePLR, parent_resource: ResourcePLR):
        # ROS2DeviceNode.run_async_func(self._ros_node.resource_tree_transfer, True, **{
        #     "old_parent": old_parent,
        #     "plr_resource": plr_resource,
        #     "parent_resource": parent_resource,
        # })
        print("resource_tree_transfer", plr_resource, parent_resource)
        if hasattr(plr_resource, "unilabos_extra") and plr_resource.unilabos_extra:
            if "update_resource_site" in plr_resource.unilabos_extra:
                site = plr_resource.unilabos_extra["update_resource_site"]
                plr_model = plr_resource.model
                
                # 直接用 plr_model 作为键查找（配置现在使用英文model名作为键）
                board_type = plr_model if plr_model in self.bioyond_config['material_type_mappings'] else None
                
                if board_type is None:
                    logger.error(f"板类型 {plr_model} 不在 material_type_mappings 中")
                    return
                    
                bottle1 = plr_resource.children[0]
                bottle_moudle = bottle1.model
                
                # 直接用 bottle_moudle 作为键查找
                bottle_type = bottle_moudle if bottle_moudle in self.bioyond_config['material_type_mappings'] else None
                
                if bottle_type is None:
                    logger.error(f"瓶类型 {bottle_moudle} 不在 material_type_mappings 中")
                    return
                
                # 从 parent_resource 获取仓库名称
                warehouse_name = parent_resource.name if parent_resource else "手动堆栈"
                logger.info(f"拖拽上料: {plr_resource.name} -> {warehouse_name} / {site}")
                
                self.create_sample(plr_resource.name, board_type, bottle_type, site, warehouse_name)
                return
        self.lab_logger().warning(f"无库位的上料，不处理，{plr_resource} 挂载到 {parent_resource}")

    def _get_type_id_by_name(self, type_name: str) -> Optional[str]:
        """根据物料类型名称查找对应的 UUID。

        查找优先级：
        1. 直接以英文 model 名（如 "YB_Vial_5mL_Carrier"）作为 key 查找；
        2. 按中文名称（value[0]，如 "5ml分液瓶板"）遍历查找。

        Args:
            type_name: 物料类型名称，可以是英文 model key 或中文名称

        Returns:
            对应的 UUID，如果找不到则返回 None
        """
        mappings = self.bioyond_config['material_type_mappings']

        # 优先：直接 key 命中（英文 model 名）
        if type_name in mappings:
            value = mappings[type_name]
            logger.debug(f"[类型映射] 直接 key 命中: {type_name} → {value[1][:8]}...")
            return value[1]

        # 兜底：按中文名遍历（value 格式: [中文名称, UUID]）
        for key, value in mappings.items():
            if value[0] == type_name:
                logger.debug(f"[类型映射] 中文名匹配: {type_name} → {key} → {value[1][:8]}...")
                return value[1]

        logger.error(f"[类型映射] 未找到类型: {type_name}")
        logger.debug(f"[类型映射] 可用类型列表: {[v[0] for v in mappings.values()]}")
        return None
    
    def create_sample(
        self,
        name: str,
        board_type: str,
        bottle_type: str,
        location_code: str,
        warehouse_name: str = "手动堆栈"
    ) -> Dict[str, Any]:
        """创建配液板物料并自动入库。
        Args:
            name: 物料名称
            board_type: 板类型，如 "5ml分液瓶板"、"配液瓶(小)板"
            bottle_type: 瓶类型，如 "5ml分液瓶"、"配液瓶(小)"
            location_code: 库位编号，例如 "A01"
            warehouse_name: 仓库名称，默认为 "手动堆栈"，支持 "自动堆栈-左"、"自动堆栈-右" 等
        """
        # 使用反向查找获取 type_id
        carrier_type_id = self._get_type_id_by_name(board_type)
        bottle_type_id = self._get_type_id_by_name(bottle_type)
        
        if not carrier_type_id:
            raise ValueError(f"未找到板类型 '{board_type}' 的配置，请检查 material_type_mappings")
        if not bottle_type_id:
            raise ValueError(f"未找到瓶类型 '{bottle_type}' 的配置，请检查 material_type_mappings")
        
        # 从指定仓库获取库位UUID
        if warehouse_name not in self.bioyond_config['warehouse_mapping']:
            logger.error(f"未找到仓库: {warehouse_name}，回退到手动堆栈")
            warehouse_name = "手动堆栈"
        
        if location_code not in self.bioyond_config['warehouse_mapping'][warehouse_name]["site_uuids"]:
            logger.error(f"仓库 {warehouse_name} 中未找到库位 {location_code}")
            raise ValueError(f"库位 {location_code} 在仓库 {warehouse_name} 中不存在")
        
        location_id = self.bioyond_config['warehouse_mapping'][warehouse_name]["site_uuids"][location_code]
        logger.info(f"创建样品入库: {name} -> {warehouse_name}/{location_code} (UUID: {location_id})")

        # 新建小瓶
        details = []
        for y in range(1, 5):
            for x in range(1, 3):
                details.append({
                    "typeId": bottle_type_id,
                    "code": "",
                    "name": str(bottle_type) + str(x) + str(y),
                    "quantity": "1",
                    "x": x,
                    "y": y,
                    "z": 1,
                    "unit": "个",
                    "parameters": json.dumps({"unit": "个"}, ensure_ascii=False),
                })

        data = {
                "typeId": carrier_type_id,
                "code": "",
                "barCode": "",
                "name": name,
                "unit": "块",
                "parameters": json.dumps({"unit": "块"}, ensure_ascii=False),
                "quantity": "1",
                "details": details,
            }
        # print("xxx:",data)
        create_result = self._post_lims("/api/lims/storage/material", data)
        sample_uuid = create_result.get("data")

        final_result = self._post_lims("/api/lims/storage/inbound", {
            "materialId": sample_uuid,
            "locationId": location_id,
        })
        return final_result




if __name__ == "__main__":
    lab_registry.setup()
    deck = bioyond_electrolyte_deck(name="YB_Deck")
    ws = BioyondCellWorkstation(deck=deck)
    # ws.create_sample(name="test", board_type="配液瓶(小)板", bottle_type="配液瓶(小)", location_code="B01")
    # logger.info(ws.scheduler_stop())
    # logger.info(ws.scheduler_start())
    
    # 继续后续流程
    logger.info(ws.auto_feeding4to3()) #搬运物料到3号箱
    # # # 使用正斜杠或 Path 对象来指定文件路径
    # excel_path = Path("unilabos\\devices\\workstation\\bioyond_studio\\bioyond_cell\\2025092701.xlsx")
    # logger.info(ws.create_orders(excel_path))
    # logger.info(ws.transfer_3_to_2_to_1())

    # logger.info(ws.transfer_1_to_2())
    # logger.info(ws.scheduler_start())


    while True:
        time.sleep(1)
    # re=ws.scheduler_stop()
    # re = ws.transfer_3_to_2_to_1()

    # print(re)
    # logger.info("调度启动完成")

    # ws.scheduler_continue()
    # 3.30 上料：读取模板 Excel 自动解析并 POST
    # r1 = ws.auto_feeding4to3_from_xlsx(r"C:\ML\GitHub\Uni-Lab-OS\unilabos\devices\workstation\bioyond_cell\样品导入模板.xlsx")
    # ws.wait_for_transfer_task(filter_text="物料转移任务")
    # logger.info("4号箱向3号箱转运物料转移任务已完成")

    # ws.scheduler_start()
    # print(r1["payload"]["data"])   # 调试模式下可直接看到要发的 JSON items

    # # 新建实验
    # response = ws.create_orders("C:/ML/GitHub/Uni-Lab-OS/unilabos/devices/workstation/bioyond_cell/2025092701.xlsx")
    # logger.info(response)
    # data_list = response.get("data", [])
    # order_name = data_list[0].get("orderName", "")

    # ws.wait_for_transfer_task(filter_text=order_name)
    # ws.wait_for_transfer_task(filter_text='DP20250927001')
    # logger.info("3号站内实验完成")
    # # ws.scheduler_start()
    # # print(res)
    # ws.transfer_3_to_2_to_1()
    # ws.wait_for_transfer_task(filter_text="物料转移任务")
    # logger.info("3号站向2号站向1号站转移任务完成")
        # r321 = self.wait_for_transfer_task()
    #1号站启动
    # ws.transfer_1_to_2()
    # ws.wait_for_transfer_task(filter_text="物料转移任务")
    # logger.info("1号站向2号站转移任务完成")
    # logger.info("全流程结束")

    # 3.31 下料：同理
    # r2 = ws.auto_batch_outbound_from_xlsx(r"C:/path/样品导入模板 (8).xlsx")
    # print(r2["payload"]["data"])
