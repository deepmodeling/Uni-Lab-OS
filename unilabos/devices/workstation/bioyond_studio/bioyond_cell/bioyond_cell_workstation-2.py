# -*- coding: utf-8 -*-
from cgi import print_arguments
from doctest import debug
from typing import Dict, Any, List, Optional
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
from unilabos.devices.workstation.bioyond_studio.config import (
    API_CONFIG, MATERIAL_TYPE_MAPPINGS, WAREHOUSE_MAPPING, SOLID_LIQUID_MAPPINGS
)
from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPService
from unilabos.resources.bioyond.decks import BIOYOND_YB_Deck
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

    def __init__(self, config: dict = None, deck=None, protocol_type=None, **kwargs):

        # 使用统一配置，支持自定义覆盖, 从 config.py 加载完整配置
        self.bioyond_config ={
            **API_CONFIG,
            "material_type_mappings": MATERIAL_TYPE_MAPPINGS,
            "warehouse_mapping": WAREHOUSE_MAPPING,
            "debug_mode": False
    }
          
            # "material_type_mappings": MATERIAL_TYPE_MAPPINGS
            # "warehouse_mapping": WAREHOUSE_MAPPING
        if deck is None and config:
            deck = config.get('deck')
        # print(self.bioyond_config)
        self.debug_mode = self.bioyond_config["debug_mode"]
        self.http_service_started = self.debug_mode
        self._device_id = "bioyond_cell_workstation"  # 默认值，后续会从_ros_node获取
        super().__init__(bioyond_config=config, deck=deck)
        self.update_push_ip() #直接修改奔耀端的报送ip地址
        logger.info("已更新奔耀端推送 IP 地址")

        # 启动 HTTP 服务线程
        t = threading.Thread(target=self._start_http_service, daemon=True, name="unilab_http")
        t.start()
        logger.info("HTTP 服务线程已启动")
        # 等到任务报送成功
        self.order_finish_event = threading.Event()
        self.last_order_status = None
        self.last_order_code = None
        logger.info(f"Bioyond工作站初始化完成 (debug_mode={self.debug_mode})")

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
        logger.info(f"report_request: {report_request}")
        logger.info(f"任务完成: {order_code}, status={status}")

        # 保存完整报文
        self.last_order_report = report_request.data
        # 如果是当前等待的订单，触发事件
        if self.last_order_code == order_code:
            self.order_finish_event.set()

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
            return {"status": "manual_stop", "report": report}
        else:
            logger.warning(f"任务未知状态 ({status}) (orderCode={order_code})")
            return {"status": f"unknown_{status}", "report": report}


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
        xlsx_path: Optional[str] = "/Users/sml/work/Unilab/unilabos/devices/workstation/bioyond_studio/bioyond_cell/material_template.xlsx",
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
        order_code = response.get("data", {}).get("orderCode")
        if not order_code:
            logger.error("上料任务未返回有效 orderCode！")
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

    # 2.14 新建实验
    def create_orders(self, xlsx_path: str) -> Dict[str, Any]:
        """
        从 Excel 解析并创建实验（2.14）
        约定：
        - batchId = Excel 文件名（不含扩展名）
        - 物料列：所有以 "(g)" 结尾（不再读取“总质量(g)”列）
        - totalMass 自动计算为所有物料质量之和
        - createTime 缺失或为空时自动填充为当前日期（YYYY/M/D）
        """
        default_path = Path("/Users/sml/work/Unilab/unilabos/devices/workstation/bioyond_studio/bioyond_cell/2025092701.xlsx")
        path = Path(xlsx_path) if xlsx_path else default_path
        print(f"[create_orders] 使用 Excel 路径: {path}")
        if path != default_path:
            print("[create_orders] 来源: 调用方传入自定义路径")
        else:
            print("[create_orders] 来源: 使用默认模板路径")

        if not path.exists():
            print(f"[create_orders] ⚠️ Excel 文件不存在: {path}")
            raise FileNotFoundError(f"未找到 Excel 文件：{path}")

        try:
            df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"读取 Excel 失败：{e}")
        print(f"[create_orders] Excel 读取成功，行数: {len(df)}, 列: {list(df.columns)}")

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
        print("[create_orders] 列匹配结果:", {
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
        print(f"[create_orders] 识别到的物料列: {material_cols}")
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
                        print(f"[create_orders] 第 {idx+1} 行物料 {mcol} 数值为负数: {mass}")

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
            print(f"[create_orders] 第 {idx+1} 行解析结果: orderName={order_data['orderName']}, "
                  f"loadShedding={order_data['loadSheddingInfo']}, pouchCell={order_data['pouchCellInfo']}, "
                  f"conductivity={order_data['conductivityInfo']}, totalMass={order_data['totalMass']}, "
                  f"material_count={len(mats)}")

            if order_data["totalMass"] <= 0:
                print(f"[create_orders] ⚠️ 第 {idx+1} 行总质量 <= 0，可能导致 LIMS 校验失败")
            if not mats:
                print(f"[create_orders] ⚠️ 第 {idx+1} 行未找到有效物料")

            orders.append(order_data)
        print("================================================")
        print("orders:", orders)

        print(f"[create_orders] 即将提交订单数量: {len(orders)}")
        response = self._post_lims("/api/lims/order/orders", orders)
        print(f"[create_orders] 接口返回: {response}")
        # 等待任务报送成功
        data_list = response.get("data", [])
        if data_list:
            order_code = data_list[0].get("orderCode")
        else:
            order_code = None

        if not order_code:
            logger.error("上料任务未返回有效 orderCode！")
            return response
          # 等待完成报送
        result = self.wait_for_order_finish(order_code)
        print("实验记录本========================create_orders========================")
        print(result)
        print("========================")
        return result

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

    # 3.35 1→2 物料转运
    def transfer_1_to_2(self) -> Dict[str, Any]:
        """
        1→2 物料转运
        URL: /api/lims/order/transfer-task1To2
        只需要 apiKey 和 requestTime
        """
        response = self._post_lims("/api/lims/order/transfer-task1To2")
                # 等待任务报送成功
        order_code = response.get("data", {}).get("orderCode")
        if not order_code:
            logger.error("上料任务未返回有效 orderCode！")
            return response
          # 等待完成报送
        result = self.wait_for_order_finish(order_code)
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
        # 优先从 SOLID_LIQUID_MAPPINGS 中获取模板数据
        template = SOLID_LIQUID_MAPPINGS.get(material_name)
        if not template:
            raise ValueError(f"在配置中未找到物料 {material_name} 的模板，请检查 SOLID_LIQUID_MAPPINGS。")
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
                board_type = None
                for key, (moudle_name,moudle_uuid) in MATERIAL_TYPE_MAPPINGS.items():
                    if plr_model == moudle_name:
                        board_type = key
                        break
                if board_type is None:
                    pass
                bottle1 = plr_resource.children[0]

                bottle_moudle = bottle1.model
                bottle_type = None
                for key, (moudle_name, moudle_uuid) in MATERIAL_TYPE_MAPPINGS.items():
                    if bottle_moudle == moudle_name:
                        bottle_type = key
                        break
                
                # 从 parent_resource 获取仓库名称
                warehouse_name = parent_resource.name if parent_resource else "手动堆栈"
                logger.info(f"拖拽上料: {plr_resource.name} -> {warehouse_name} / {site}")
                
                self.create_sample(plr_resource.name, board_type, bottle_type, site, warehouse_name)
                return
        self.lab_logger().warning(f"无库位的上料，不处理，{plr_resource} 挂载到 {parent_resource}")

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
        carrier_type_id = MATERIAL_TYPE_MAPPINGS[board_type][1]
        bottle_type_id  = MATERIAL_TYPE_MAPPINGS[bottle_type][1]
        
        # 从指定仓库获取库位UUID
        if warehouse_name not in WAREHOUSE_MAPPING:
            logger.error(f"未找到仓库: {warehouse_name}，回退到手动堆栈")
            warehouse_name = "手动堆栈"
        
        if location_code not in WAREHOUSE_MAPPING[warehouse_name]["site_uuids"]:
            logger.error(f"仓库 {warehouse_name} 中未找到库位 {location_code}")
            raise ValueError(f"库位 {location_code} 在仓库 {warehouse_name} 中不存在")
        
        location_id = WAREHOUSE_MAPPING[warehouse_name]["site_uuids"][location_code]
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
    deck = BIOYOND_YB_Deck(setup=True)
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
