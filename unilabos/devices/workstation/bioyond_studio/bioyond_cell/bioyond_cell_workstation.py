# -*- coding: utf-8 -*-
from cgi import print_arguments
from doctest import debug
from typing import Dict, Any, List, Optional, Tuple, Union
import requests
from pylabrobot.resources.resource import Resource as ResourcePLR
from pylabrobot.resources.carrier import ResourceHolder
from pathlib import Path
import pandas as pd
import time
from datetime import datetime, timedelta
import re
import threading
import json
import csv
import os
import uuid
from copy import deepcopy
from urllib3 import response
from unilabos.devices.workstation.bioyond_studio.station import BioyondWorkstation, BioyondResourceSynchronizer
from unilabos.devices.workstation.bioyond_studio.bioyond_rpc import BioyondException
# ⚠️ config.py 已废弃 - 所有配置现在从 JSON 文件加载
# from unilabos.devices.workstation.bioyond_studio.config import API_CONFIG, ...
from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPService
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from unilabos.resources.bioyond.decks import BioyondElectrolyteDeck, bioyond_electrolyte_deck
from unilabos.resources.bioyond.YB_warehouse_material import (
    empty_stock_snapshot,
    slot_entry_from_snapshot,
)
from unilabos.utils.datacore import push_csv_by_config
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
        
        # 初始化订单报送事件（配液 wait_for_order_finish 单值机制，原样保留）
        self.order_finish_event = threading.Event()
        self.last_order_status = None
        self.last_order_code = None

        # ========== 订单 finish 缓存（配液 / 电导分库，防跨路径 pop 串扰）==========
        # 共享锁；配液走 _order_finish_*，电导走 _cond_finish_*，互不 pop。
        # process_order_finish_report 末尾旁路同时写入两套缓存。
        self._order_finish_lock = threading.Lock()
        self._order_finish_events: Dict[str, threading.Event] = {}
        self._order_finish_reports: Dict[str, Dict[str, Any]] = {}
        self._cond_finish_events: Dict[str, threading.Event] = {}
        self._cond_finish_reports: Dict[str, Dict[str, Any]] = {}

        # ========== 配液/分液进度统计（2026-07-15 追加）==========
        # 由 _submit_and_wait_orders 提交批次时重置，由 process_step_finish_report 累加。
        # 全部限定在本批 orderCode 集合内，避免跨批次/其他订单串入。
        # 另存 orderCode→orderId：真机/仿真机 orderCode 可能撞号，step 带 orderId 时双字段过滤。
        self._progress_lock = threading.Lock()
        # 本批订单编号集合，以及每单预期分液瓶数（扣电?1:0 + 软包?1:0 + 电导?bottleCount:0）
        self._batch_order_codes: set = set()
        self._batch_order_ids: Dict[str, str] = {}  # orderCode → orderId（建单返回）
        self._batch_order_dispense: Dict[str, int] = {}
        # 配液（按订单）：分母 N、已收到「开始混匀」的订单、已计完成（混匀→三轴取）的订单
        self._formulation_total: int = 0
        self._formulation_mixed: set = set()
        self._formulation_completed: set = set()
        # 分液（按瓶）：分母 Σ、已完成的 (orderCode, stepId) 集合
        self._dispense_total: int = 0
        self._dispense_done: set = set()

        # ========== 多任务互锁（wait_orderlist_idle）==========
        # 同一把锁保护下面四个字段，避免门控扫描与建单写缓存竞态。
        self._interlock_lock = threading.Lock()
        # 上一批已建成的订单：orderCode → orderId。门控核对它全部到 80/100 后才清空。
        self._interlock_batch: Dict[str, str] = {}
        # 本批附加信息：scenario / 写入时间 / 逐单最终状态（供下一轮 blocked 日志点名失败单）
        self._interlock_batch_meta: Dict[str, Any] = {}
        # 放行凭证：门控放行时占据，_submit_and_wait_orders 写缓存时接管清除。
        # 没有它，门控 return 后到 LIMS 把订单登记成 60 之间有一段窗口，
        # 第二个任务会看到「无 60 + 缓存空」而同样放行，两批同时建单击穿库位容量。
        self._interlock_claim: Optional[Dict[str, Any]] = None
        # 建单未全部成功时会主动 scheduler_pause()，这里记下原因。
        # 否则门控看到 Pause 会当成「人工临时暂停」一路轮询到 24h 超时，掩盖真实原因。
        self._interlock_last_failure: Optional[Dict[str, Any]] = None
        # 交接堆栈预约：stack_inquiry_2to1 放行时占据，transfer_3_to_2_to_1_auto 结束时释放。
        # 语义 =「本轮已查到 1号2号交接堆栈空、但 321 还没把板放进去」。共用 _interlock_lock。
        # 没有它，门控 return 后到 LIMS 库存显示占用之间有一段窗口（实测 380ms），
        # 下一轮会同样看到「物理空」而放行，其 321 必被 LIMS 拒（详见 stack_inquiry_2to1）。
        self._handoff_claim: Optional[Dict[str, Any]] = None

        # ========== 机器人坐标采集（2.43 关节 / 2.44 三轴，后台轮询）==========
        # 只读两个查询接口，不下发任何动作；缓存形如 {"items": [...], "ts": 秒}，None 表示无可信数据。
        # debug_mode 下 _post_lims 不发真实请求（只打日志），轮询没有意义还会每秒刷屏，故不启动。
        self._robot_pos_lock = threading.Lock()
        self._robot_joint_cache: Optional[Dict[str, Any]] = None
        self._gantry_pos_cache: Optional[Dict[str, Any]] = None
        if self.debug_mode:
            logger.info("debug_mode=True，不启动机器人坐标轮询（坐标属性返回 {}）")
        else:
            threading.Thread(
                target=self._robot_pos_poll_loop, daemon=True, name="bioyond-robot-pos-poller"
            ).start()
            logger.info(f"机器人坐标轮询线程已启动（每 {self._ROBOT_POS_PERIOD_S} 秒）")

        logger.info(f"✅ BioyondCellWorkstation 初始化完成 (debug_mode={self.debug_mode})")
        logger.info(
            "提示：真机与仿真机 orderCode 可能撞号；finish/进度以 orderCode+orderId 双字段判定。"
            "真机作业时请勿让仿真机 LIMS 推送到同一 HTTP 回调。"
        )

    # 奔曜全 0 GUID：电导建单偶发占位，此时无法用 orderId 强校验
    _EMPTY_ORDER_ID = "00000000-0000-0000-0000-000000000000"

    # 交接堆栈预约的兜底作废时长。预约只需覆盖「门控放行 → 321 放完板」这一段，
    # 实测约 7s（放行 17:09:41.047 → 321 下发 17:09:41.247 → 转运完成 17:09:48.095），
    # 10 分钟已是 80 倍余量。刻意不复用门控的 timeout(1h)：正常路径由
    # transfer_3_to_2_to_1_auto 的 finally 主动释放，作废只兜「321 压根没被下发」这类
    # 异常（如云端因别轮失败停发了后续节点），此时预约挂多久就白挡多久，越短越好。
    _HANDOFF_CLAIM_TTL_SECONDS = 600.0

    # ========== 仓库 whId 常量（来源：包含库位的仓库信息0610.json）==========
    # 只用 whId 定位仓库：code「0018」同时是「适配器块」的 materialTypeCode，用 code 匹配会撞。
    #
    # 3 号箱自动堆栈-左（name="自动堆栈-左", code="0008"），即 3→2→1 / 3→2 里的 "3"。
    # 转运接口只认这个仓库当来源，与报告 usedMaterials 里的 locationId（可能指向
    # 配液站内的临时槽位）无关。
    _WH_ID_AUTO_STACK_LEFT = "3a19debc-84b4-0359-e2d4-b3beea49348b"
    # 1号2号手套箱交接堆栈（code=0016）：**只有 1 个库位**，是 321 转运的瓶颈通道
    _WH_ID_TRANSFER_2TO1 = "3a1baa49-7f76-b88a-44d5-d478c48aae3e"
    # 2号手套箱内部堆栈（code=0017）：9 个库位，1→2 回流的落点，容量充足非瓶颈
    _WH_ID_STACK_2_INNER = "3a1baa4b-393d-ed16-5758-edec5cf20749"
    # 5号自动传递窗（code=0018）：**只有 4 个库位**，电导板一板一温度点，是限量通道
    _WH_ID_TRANSFER_WINDOW_5 = "3a1c68b5-65e1-f662-93bb-3c2c5b42744d"
    # 5号右侧手动堆栈（code=0027）：8 个库位，电导测完的落点，容量充足非瓶颈
    _WH_ID_MANUAL_STACK_5_RIGHT = "3a1c7325-8998-49f6-c5a4-d20c31d132e1"

    # 5 号自动传递窗物理库位数，电导一轮最多同时测这么多温度点
    _WINDOW_5_CAPACITY = 4

    # ========== 机器人坐标采集（2.43 关节 / 2.44 三轴）==========
    # 后台轮询间隔 = ROS topic 发布周期。走 LIMS HTTP 查询，比 DUCO 那种本地服务贵，
    # 故取 1 秒而非 0.5 秒；两者共用同一常量，要改频率只动这一处。
    _ROBOT_POS_PERIOD_S = 1.0
    # 连续失败次数达到该阈值后丢弃缓存（置 None），避免对外发布过期坐标
    _ROBOT_POS_MAX_FAIL = 3

    # ========== 场景键 → 配液类 workflowName 关键字（子串匹配）==========
    # ⚠️ 仅用于分场景统计/日志，**不可**用来收窄互锁的阻断范围。
    # 配液共用同一台奔曜分液硬件，两批配液物理上不可能并行；若拿 "coin" 收窄，
    # 扣电任务就会无视正在跑的大瓶配液单而放行，把第二批冲进同一台硬件。
    #
    # 命名陷阱：`配液分液(小瓶)(电导)` 里的「(电导)」是 LIMS 历史遗留，它其实是**扣电**路
    # （源料 V05ML66 @ 小分液瓶堆栈）。而 `配液分液(大瓶)` 一对三，覆盖软包/电导/扣电+电导——
    # 实测场景 4 的 8 单全是大瓶，一个大瓶同时分出扣电的 5ml 与电导的 20ml 瓶。
    # 因此**不能用 workflowName 推断板型或链路**，判断是否走扣电路要看板型或 coin_cell_volume。
    SCENARIO_ORDER_KEYWORDS: Dict[str, List[str]] = {
        "all":   ["配液"],              # 默认：站级全局，覆盖全部配液单
        "coin":  ["配液分液(小瓶)"],      # 场景1 配液+扣电
        "pouch": ["配液分液(大瓶)"],      # 场景2 配液+软包
        "cond":  ["配液分液(大瓶)"],      # 场景3 配液+电导（场景4 同此，不单列键）
    }

    # order-list **响应** items[].status：60 执行中 / 80 成功 / 90 失败 / 100 已取出
    _ORDER_STATUS_RUNNING_CODE = 60
    _ORDER_STATUS_FAILED_CODE = 90
    _ORDER_TERMINAL_OK = (80, 100)

    # ⚠️ order-list **请求** data.status 与上面**不是同一套枚举**：请求 60 = 未取出、
    # 请求 100 = 已取出，两者都可能对应响应里的 80 Succeed（实测见 `_paginate_order_list`）。
    # 故此处**故意不定义**请求侧状态常量——判状态一律请求侧传空串、只认响应 status 字段。

    @staticmethod
    def _normalize_order_id(order_id: Optional[str]) -> str:
        return (order_id or "").strip()

    def _is_usable_order_id(self, order_id: Optional[str]) -> bool:
        oid = self._normalize_order_id(order_id)
        return bool(oid) and oid != self._EMPTY_ORDER_ID

    def _report_matches_expected(
        self,
        order_code: str,
        expected_order_id: Optional[str],
        report: Optional[Dict[str, Any]],
    ) -> bool:
        """finish 接受条件：orderCode 必须一致；expected_order_id 可用时 orderId 也必须一致。"""
        if not report:
            return False
        if (report.get("orderCode") or "") != order_code:
            return False
        exp = self._normalize_order_id(expected_order_id)
        if not self._is_usable_order_id(exp):
            return True
        return self._normalize_order_id(report.get("orderId")) == exp

    # 三类分液的 step 名 → 对应订单字段（分液完成信号）
    # 三者均已从真机日志验证：电导分液 / 扣电分液见 0714~0722 批次；
    # 软包分液于 2026-07-23 (orderCode=BSO2026072300007) 验证命中并正确计数。
    _DISPENSE_STEP_NAMES = ("电导分液", "扣电分液", "软包分液")

    def _reset_progress_tracking(self, orders: List[Dict[str, Any]], order_codes: List[str], data_list: List[Dict[str, Any]]):
        """提交批次时重置配液/分液进度统计。

        按 orderName 把响应 data_list（含 orderCode）与入参 orders（含分液配置）关联，
        计算每单分液瓶数与两个分母。orderName 缺失时按索引兜底对齐。
        """
        # orderName → 订单分液配置
        cfg_by_name: Dict[str, Dict[str, Any]] = {}
        for od in orders:
            name = od.get("orderName")
            if name:
                cfg_by_name[name] = od

        def _num(v) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        batch_codes: set = set()
        batch_ids: Dict[str, str] = {}
        dispense_map: Dict[str, int] = {}
        for idx, item in enumerate(data_list):
            code = item.get("orderCode")
            if not code:
                continue
            name = item.get("orderName")
            cfg = cfg_by_name.get(name)
            if cfg is None and idx < len(orders):
                cfg = orders[idx]  # 索引兜底
            cfg = cfg or {}
            coin = 1 if _num(cfg.get("loadSheddingInfo")) > 0 else 0
            pouch = 1 if _num(cfg.get("pouchCellInfo")) > 0 else 0
            cond = int(_num(cfg.get("conductivityBottleCount"))) if _num(cfg.get("conductivityInfo")) > 0 else 0
            batch_codes.add(code)
            oid = self._normalize_order_id(item.get("orderId"))
            if self._is_usable_order_id(oid):
                batch_ids[code] = oid
            dispense_map[code] = coin + pouch + cond

        with self._progress_lock:
            self._batch_order_codes = batch_codes
            self._batch_order_ids = batch_ids
            self._batch_order_dispense = dispense_map
            self._formulation_total = len(order_codes)
            self._formulation_mixed = set()
            self._formulation_completed = set()
            self._dispense_total = sum(dispense_map.values())
            self._dispense_done = set()

        logger.info(
            f"[进度统计] 重置：配液分母={len(order_codes)} 单，分液分母={sum(dispense_map.values())} 瓶，"
            f"本批订单={sorted(batch_codes)}, 已登记orderId={len(batch_ids)}"
        )

    @property
    def device_id(self) -> str:
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


    def post_init(self, ros_node: ROS2WorkstationNode):
        # 父类负责 deck 初始化、同步器、各监控线程与 deck 上传，必须先跑完
        super().post_init(ros_node)

        # 机器人坐标：把发布周期从框架默认 5 秒调到与轮询一致的 _ROBOT_POS_PERIOD_S。
        # 单独 try 兜住，避免注册表没声明 status_types 时把父类已完成的初始化连带报错。
        try:
            for name in ("robot_joint_positions", "gantry_positions"):
                pub = ros_node._property_publishers.get(name)
                if pub is not None:
                    pub.change_frequency(self._ROBOT_POS_PERIOD_S)
                else:
                    logger.warning(f"[机器人坐标] 未找到属性发布者 {name}，请检查注册表 status_types 是否已声明")
        except Exception as e:
            logger.error(f"[机器人坐标] 调整发布频率失败: {e}")

    # ========== 配液/分液进度属性（前端可轮询，对齐扣电站 data_order_completion_percentage）==========
    @property
    def data_formulation_total_count(self) -> int:
        """本批配液订单总数（配液进度分母）"""
        return self._formulation_total

    @property
    def data_formulation_completed_count(self) -> int:
        """已完成配液（混匀→三轴取）的订单数（配液进度分子）"""
        return len(self._formulation_completed)

    @property
    def data_formulation_completion_percentage(self) -> float:
        """配液完成百分比 (%)"""
        try:
            total = self._formulation_total
            if total <= 0:
                return 0.0
            return round(len(self._formulation_completed) / total * 100.0, 2)
        except Exception as e:
            logger.warning(f"计算配液完成百分比失败，返回 0.0: {e}")
            return 0.0

    @property
    def data_dispense_total_bottles(self) -> int:
        """本批分液瓶总数 = Σ(扣电?1:0 + 软包?1:0 + 电导?bottleCount:0)（分液进度分母）"""
        return self._dispense_total

    @property
    def data_dispense_completed_bottles(self) -> int:
        """已完成分液的瓶数（分液进度分子）"""
        return min(len(self._dispense_done), self._dispense_total) if self._dispense_total > 0 else len(self._dispense_done)

    @property
    def data_dispense_completion_percentage(self) -> float:
        """分液完成百分比 (%)"""
        try:
            total = self._dispense_total
            if total <= 0:
                return 0.0
            return round(min(len(self._dispense_done), total) / total * 100.0, 2)
        except Exception as e:
            logger.warning(f"计算分液完成百分比失败，返回 0.0: {e}")
            return 0.0

    def _stock_slot_entry(self, kind: str, index: int) -> dict:
        snapshot = getattr(self, "_stock_snapshot", None) or empty_stock_snapshot()
        return slot_entry_from_snapshot(snapshot, getattr(self, "bioyond_config", None), kind, index)

    def _stock_slot_name(self, kind: str, index: int) -> str:
        return str(self._stock_slot_entry(kind, index).get("name") or "")

    def _stock_slot_qty(self, kind: str, index: int) -> float:
        try:
            return float(self._stock_slot_entry(kind, index).get("qty") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _stock_tip_count(self, key: str) -> int:
        tips = (getattr(self, "_stock_snapshot", None) or {}).get("tips") or {}
        try:
            return int(tips.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def data_prep_tip_5000uL_count(self) -> int:
        return self._stock_tip_count("prep_5000uL")

    @property
    def data_prep_tip_1000uL_count(self) -> int:
        return self._stock_tip_count("prep_1000uL")

    @property
    def data_prep_tip_50uL_count(self) -> int:
        return self._stock_tip_count("prep_50uL")

    @property
    def data_dispense_tip_5000uL_count(self) -> int:
        return self._stock_tip_count("dispense_5000uL")

    # ========== 机器人坐标（2.43 关节 / 2.44 三轴，只读轮询缓存）==========

    @staticmethod
    def _device_list_payload(resp: Any, api_name: str) -> Tuple[List[Any], Optional[float]]:
        """校验 2.43/2.44 响应，取出 data 数组与服务端时间戳（秒）。

        三种失败都抛 ValueError，交给轮询循环统一记 warning 并计入连续失败：
        `_post_lims` 自己吞掉网络异常后返回 {"error": ...}；LIMS 业务失败时 code=0
        且 message 有值；debug_mode 下拿到的是假响应（带 debug 标记）。
        """
        if not isinstance(resp, dict):
            raise ValueError(f"{api_name} 返回体不是对象: {type(resp).__name__}")
        if resp.get("error"):
            raise ValueError(f"{api_name} 请求失败: {resp['error']}")
        if resp.get("debug"):
            raise ValueError(f"{api_name} 处于 debug_mode，无真实数据")
        if resp.get("code") != 1:
            raise ValueError(f"{api_name} code={resp.get('code')}, message={resp.get('message')!r}")
        data = resp.get("data")
        if not isinstance(data, list):
            raise ValueError(f"{api_name} data 不是数组: {type(data).__name__}")
        # 文档里 timestamp 是毫秒整数，统一换成秒，与扣电站 DUCO 的 ts 口径一致
        ts_ms = resp.get("timestamp")
        ts = round(float(ts_ms) / 1000.0, 3) if isinstance(ts_ms, (int, float)) else None
        return data, ts

    @staticmethod
    def _axis_values(axes: Any) -> List[float]:
        """把 [{index, value}, ...] 按 index 升序压成纯数值数组，非法项跳过。

        轴号不保证按顺序返回，且下标含义靠位置表达，所以必须显式按 index 排序。
        """
        picked: List[Tuple[int, float]] = []
        if isinstance(axes, list):
            for axis in axes:
                if not isinstance(axis, dict):
                    continue
                try:
                    picked.append((int(axis["index"]), float(axis["value"])))
                except (KeyError, TypeError, ValueError):
                    continue
        return [value for _, value in sorted(picked, key=lambda pair: pair[0])]

    def _fetch_robot_joints(self) -> Dict[str, Any]:
        """调 2.43 并整形为 {"items": [{frame, joints, extjoints}], "ts": 秒}"""
        data, ts = self._device_list_payload(self.robot_joint_list_info(), "2.43 robot-joint-list-info")
        items = [
            {
                "frame": item.get("deviceFrameCode"),
                "joints": self._axis_values(item.get("joints")),
                "extjoints": self._axis_values(item.get("extjoints")),
            }
            for item in data
            if isinstance(item, dict)
        ]
        return {"items": items, "ts": ts}

    def _fetch_gantry_pos(self) -> Dict[str, Any]:
        """调 2.44 并整形为 {"items": [{frame, x, y, z}], "ts": 秒}"""
        data, ts = self._device_list_payload(self.gantry_pos_list_info(), "2.44 gantry-pos-list-info")
        items = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                items.append({
                    "frame": item.get("deviceFrameCode"),
                    "x": float(item["x"]),
                    "y": float(item["y"]),
                    "z": float(item["z"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return {"items": items, "ts": ts}

    def _robot_pos_poll_loop(self) -> None:
        """后台守护线程：每 _ROBOT_POS_PERIOD_S 秒批量拉一次机器人 / 三轴坐标。

        两个接口各自计连续失败数，互不牵连——一个挂了不该把另一个的坐标也清空。
        任何异常只记 warning 并保留上一次缓存，绝不向外抛，以免影响建单 / 转运主流程；
        连续失败达到 _ROBOT_POS_MAX_FAIL 次后认为坐标已不可信，将该缓存置 None。
        """
        targets = (
            ("_robot_joint_cache", "2.43 robot-joint-list-info", self._fetch_robot_joints),
            ("_gantry_pos_cache", "2.44 gantry-pos-list-info", self._fetch_gantry_pos),
        )
        fail_counts = {attr: 0 for attr, _, _ in targets}
        while True:
            for attr, api_name, fetch in targets:
                try:
                    cache = fetch()
                    with self._robot_pos_lock:
                        setattr(self, attr, cache)
                    fail_counts[attr] = 0
                except Exception as e:
                    fail_counts[attr] += 1
                    if fail_counts[attr] >= self._ROBOT_POS_MAX_FAIL:
                        with self._robot_pos_lock:
                            setattr(self, attr, None)
                    logger.warning(
                        f"[机器人坐标] {api_name} 读取失败（连续第 {fail_counts[attr]} 次）: {e}"
                    )
            time.sleep(self._ROBOT_POS_PERIOD_S)

    # 返回类型必须是 str：Host 状态回调只接受标量，list/dict 会被框架强转为 String，语义不可靠。

    @property
    def robot_joint_positions(self) -> str:
        """2.43 各机器人关节坐标，JSON 字符串；无数据时返回 "{}"

        形如 {"robots": [{"frame": -1, "joints": [四轴], "extjoints": [扩增轴...]}], "ts": 秒}

        ⚠️ extjoints 长度按机器人实际配置变化，**不可**固定按下标取。实测现场
        （172.16.28.128:44389）frame -1/-16 回 2 个（夹爪 + 导轨），而 -12/-14 只回 1 个。
        下游要认某根轴，必须结合 frame 判断，而不是假定 extjoints[1] 就是导轨。
        """
        with self._robot_pos_lock:
            cache = self._robot_joint_cache
        if not cache:
            return "{}"
        return json.dumps({"robots": cache["items"], "ts": cache["ts"]})

    @property
    def gantry_positions(self) -> str:
        """2.44 各三轴机器人坐标，JSON 字符串；无数据时返回 "{}"

        形如 {"gantries": [{"frame": 3, "x": .., "y": .., "z": ..}], "ts": 秒}
        """
        with self._robot_pos_lock:
            cache = self._gantry_pos_cache
        if not cache:
            return "{}"
        return json.dumps({"gantries": cache["items"], "ts": cache["ts"]})

    # http报送服务，返回数据部分
    def process_step_finish_report(self, report_request):
        data = report_request.data
        stepId = data.get("stepId")
        stepName = data.get("stepName")
        orderCode = data.get("orderCode")
        orderId = self._normalize_order_id(data.get("orderId"))
        logger.info(f"步骤完成: stepId: {stepId}, stepName:{stepName}")

        # 仅统计本批订单；有 orderId 时与建单双字段校验，挡真机/仿真撞号
        try:
            if orderCode and orderCode in self._batch_order_codes:
                expected_oid = self._batch_order_ids.get(orderCode)
                if (
                    self._is_usable_order_id(expected_oid)
                    and self._is_usable_order_id(orderId)
                    and orderId != expected_oid
                ):
                    logger.warning(
                        f"[进度统计] 忽略异 orderId 步骤: orderCode={orderCode}, "
                        f"期望={expected_oid[:8]}..., 报文={orderId[:8]}..., step={stepName}"
                    )
                else:
                    with self._progress_lock:
                        if stepName == "开始混匀":
                            self._formulation_mixed.add(orderCode)
                        elif stepName == "三轴取":
                            # 混匀完成信号：同一订单先「开始混匀」再「三轴取」
                            if orderCode in self._formulation_mixed and orderCode not in self._formulation_completed:
                                self._formulation_completed.add(orderCode)
                                logger.info(
                                    f"[进度统计] 配液完成 {len(self._formulation_completed)}/{self._formulation_total} "
                                    f"({self.data_formulation_completion_percentage}%) orderCode={orderCode}"
                                )
                        elif stepName in self._DISPENSE_STEP_NAMES:
                            key = (orderCode, stepId)
                            if key not in self._dispense_done:
                                self._dispense_done.add(key)
                                logger.info(
                                    f"[进度统计] 分液完成 {self.data_dispense_completed_bottles}/{self._dispense_total} "
                                    f"({self.data_dispense_completion_percentage}%) orderCode={orderCode} step={stepName}"
                                )
        except Exception as e:
            logger.warning(f"[进度统计] 更新失败（不影响报送处理）: {e}")

        return data.get('executionStatus')

    def process_sample_finish_report(self, report_request):
        logger.info(f"通量完成: {report_request.data.get('sampleId')}")
        return {"status": "received"}

    def process_order_finish_report(self, report_request, used_materials=None):
        order_code = report_request.data.get("orderCode")
        order_id = report_request.data.get("orderId")
        status = report_request.data.get("status")
        
        # 🔍 详细调试日志
        logger.info(f"[DEBUG] ========== 收到 order_finish 报送 ==========")
        logger.info(f"[DEBUG] 报送的 orderCode: '{order_code}' (type: {type(order_code).__name__})")
        logger.info(f"[DEBUG] 报送的 orderId: '{order_id}'")
        logger.info(f"[DEBUG] 当前等待的 last_order_code: '{self.last_order_code}' (type: {type(self.last_order_code).__name__})")
        logger.info(f"[DEBUG] 报送状态: {status}")
        logger.info(f"[DEBUG] orderCode 是否匹配: {self.last_order_code == order_code}")
        logger.info(f"[DEBUG] Event 当前状态 (触发前): is_set={self.order_finish_event.is_set()}")
        logger.info(f"report_request: {report_request}")
        logger.info(f"任务完成: {order_code}, orderId={order_id}, status={status}")

        # 保存完整报文
        self.last_order_report = report_request.data
        
        # 如果是当前等待的订单，触发事件（最终是否接受由 wait 侧 orderCode+orderId 双校验决定）
        if self.last_order_code == order_code:
            logger.info(f"[DEBUG] ✅ orderCode 匹配！触发 order_finish_event")
            self.order_finish_event.set()
            logger.info(f"[DEBUG] Event 状态 (触发后): is_set={self.order_finish_event.is_set()}")
        else:
            logger.warning(f"[DEBUG] ❌ orderCode 不匹配，不触发 event")
            logger.warning(f"[DEBUG]    期望: '{self.last_order_code}'")
            logger.warning(f"[DEBUG]    实际: '{order_code}'")
        
        logger.info(f"[DEBUG] ========================================")

        # ========== finish 旁路缓存（配液 + 电导分库，互不 pop）==========
        # 配液 wait_for_order_finish 读 _order_finish_*；
        # 电导 _wait_conductivity_finish 读 _cond_finish_*。
        # 注意：真机/仿真机 orderCode 可能撞号，wait 侧会再用 orderId 过滤。
        if order_code:
            with self._order_finish_lock:
                self._order_finish_reports[order_code] = report_request.data
                ev = self._order_finish_events.get(order_code)
                if ev is not None:
                    ev.set()
                self._cond_finish_reports[order_code] = report_request.data
                cond_ev = self._cond_finish_events.get(order_code)
                if cond_ev is not None:
                    cond_ev.set()

        return {"status": "received"}

    def _classify_finish_report(
        self,
        order_code: str,
        report: Dict[str, Any],
        expected_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        把 LIMS finish 报文按 status 字段映射为 wait 函数的统一返回格式。
        - 30 → success，-11 → abnormal_stop，-12 → manual_stop，其他 → unknown_<status>
        - 报文 orderCode 必须与等待 orderCode 一致；expected_order_id 可用时 orderId 也必须一致
        """
        if not report:
            logger.warning(f"[wait_for_order_finish] 报文为空: orderCode={order_code}")
            return {"status": "mismatch", "report": {}}

        report_code = report.get("orderCode")
        status_raw = str(report.get("status", ""))

        if report_code != order_code:
            logger.warning(
                f"[wait_for_order_finish] 报文 orderCode 与请求不一致: "
                f"报文={report_code} ≠ 请求={order_code}"
            )
            return {"status": "mismatch", "report": report}

        if not self._report_matches_expected(order_code, expected_order_id, report):
            logger.error(
                f"[wait_for_order_finish] 报文 orderId 与期望不一致（疑似真机/仿真撞号）: "
                f"orderCode={order_code}, 期望={expected_order_id}, "
                f"报文={report.get('orderId')}, orderName={report.get('orderName')}"
            )
            return {"status": "mismatch", "report": report}

        if status_raw == "30":
            logger.info(
                f"[wait_for_order_finish] ✓ 任务成功 "
                f"(orderCode={order_code}, orderId={report.get('orderId')})"
            )
            return {"status": "success", "report": report}
        elif status_raw == "-11":
            logger.error(f"[wait_for_order_finish] ✗ 任务异常停止 (orderCode={order_code})")
            return {"status": "abnormal_stop", "report": report}
        elif status_raw == "-12":
            logger.warning(f"[wait_for_order_finish] 任务人工停止 (orderCode={order_code})")
            return {"status": "manual_stop", "report": report}
        else:
            logger.warning(
                f"[wait_for_order_finish] 任务未知状态 status={status_raw} (orderCode={order_code})"
            )
            return {"status": f"unknown_{status_raw}", "report": report}

    def wait_for_order_finish(
        self,
        order_code: str,
        timeout: int = 36000,
        expected_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        等待指定 orderCode 的 /report/order_finish 报送。

        接受条件：orderCode 匹配，且（若 expected_order_id 可用）orderId 也匹配。
        同 orderCode、异 orderId 的报文视为真机/仿真撞号，拒绝并继续等待，直到超时或命中正确单。

        Args:
            order_code: 任务编号
            timeout: 超时时间（秒）
            expected_order_id: 建单返回的 orderId；可用时与 finish 双字段校验
        Returns:
            完整的报送数据 + 状态判断结果
        """
        if not order_code:
            logger.error("wait_for_order_finish() 被调用，但 order_code 为空！")
            return {"status": "error", "message": "empty order_code"}

        # 兼容旧单值机制（调试日志 / wait_for_order_finish_polling）
        self.last_order_code = order_code
        self.last_order_report = None
        self.order_finish_event.clear()

        deadline = time.monotonic() + max(float(timeout), 0.0)
        exp_oid = self._normalize_order_id(expected_order_id)

        with self._order_finish_lock:
            ev = self._order_finish_events.get(order_code)
            if ev is None:
                ev = threading.Event()
                self._order_finish_events[order_code] = ev
            else:
                ev.clear()

        logger.info(
            f"等待任务完成报送: orderCode={order_code}, "
            f"expected_orderId={exp_oid or '<未校验>'} (timeout={timeout}s)"
        )

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error(f"等待任务超时: orderCode={order_code}")
                    return {"status": "timeout", "orderCode": order_code}

                with self._order_finish_lock:
                    report = self._order_finish_reports.pop(order_code, None)

                if report is None:
                    triggered = ev.wait(timeout=remaining)
                    with self._order_finish_lock:
                        report = self._order_finish_reports.pop(order_code, None)
                    if report is None:
                        if not triggered:
                            # 超时后再捞一次：报文可能刚好在 wait 返回与取锁之间到达
                            with self._order_finish_lock:
                                report = self._order_finish_reports.pop(order_code, None)
                            if report is None:
                                logger.error(f"等待任务超时: orderCode={order_code}")
                                return {"status": "timeout", "orderCode": order_code}
                        else:
                            # Event 触发但缓存已被其他路径取走，继续等
                            continue
                    ev.clear()

                if not self._report_matches_expected(order_code, exp_oid, report):
                    logger.error(
                        f"[配液wait] 拒绝异源/撞号 finish，继续等待: orderCode={order_code}, "
                        f"期望orderId={exp_oid or '<未校验>'}, "
                        f"报文orderId={report.get('orderId')}, "
                        f"orderName={report.get('orderName')}"
                    )
                    continue

                self.last_order_report = report
                return self._classify_finish_report(order_code, report, expected_order_id=exp_oid)
        finally:
            with self._order_finish_lock:
                cur = self._order_finish_events.get(order_code)
                if cur is ev:
                    self._order_finish_events.pop(order_code, None)

    def wait_for_order_finish_polling(
        self,
        order_code: str,
        timeout: int = 36000,
        poll_interval: float = 0.5,
        expected_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        等待指定 orderCode 的 /report/order_finish 报送（非阻塞轮询版本）。
        
        与 wait_for_order_finish 的区别：
        - 使用轮询而非阻塞等待，每隔 poll_interval 秒检查一次
        - 允许 ROS2 在等待期间处理 feedback 消息
        - 适用于长时间运行的 ROS2 Action
        - 同样要求 orderCode +（可用时）orderId 双字段匹配
        
        Args:
            order_code: 任务编号
            timeout: 超时时间（秒）
            poll_interval: 轮询间隔（秒），默认 0.5 秒
            expected_order_id: 建单 orderId，可用时双字段校验
        Returns:
            完整的报送数据 + 状态判断结果
        """
        if not order_code:
            logger.error("wait_for_order_finish_polling() 被调用，但 order_code 为空！")
            return {"status": "error", "message": "empty order_code"}

        self.last_order_code = order_code
        self.last_order_report = None
        self.order_finish_event.clear()
        exp_oid = self._normalize_order_id(expected_order_id)

        logger.info(
            f"[轮询模式] 等待任务完成报送: orderCode={order_code}, "
            f"expected_orderId={exp_oid or '<未校验>'} "
            f"(timeout={timeout}s, poll_interval={poll_interval}s)"
        )

        start_time = time.time()
        poll_count = 0
        while True:
            poll_count += 1
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.error(f"[轮询模式] 等待任务超时: orderCode={order_code}")
                return {"status": "timeout", "orderCode": order_code}

            report = None
            with self._order_finish_lock:
                report = self._order_finish_reports.pop(order_code, None)

            if report is None and self.order_finish_event.is_set():
                report = self.last_order_report
                self.order_finish_event.clear()

            if report is not None:
                if not self._report_matches_expected(order_code, exp_oid, report):
                    logger.error(
                        f"[轮询模式] 拒绝异源/撞号 finish，继续等待: orderCode={order_code}, "
                        f"期望orderId={exp_oid or '<未校验>'}, "
                        f"报文orderId={report.get('orderId')}, "
                        f"orderName={report.get('orderName')}"
                    )
                    self.last_order_report = None
                    time.sleep(poll_interval)
                    continue
                self.last_order_report = report
                return self._classify_finish_report(order_code, report, expected_order_id=exp_oid)

            if poll_count % 10 == 0:
                logger.info(f"[轮询模式] [DEBUG] 轮询中... 已等待 {elapsed:.1f}s (第{poll_count}次检查)")
            time.sleep(poll_interval)

    def _wait_conductivity_finish(
        self,
        order_code: str,
        timeout: int = 36000,
        expected_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        电导专用：等待指定 orderCode 的 /report/order_finish 报送（并发安全）。

        与 wait_for_order_finish（配液用）的关键区别：
        - 使用独立的 _cond_finish_events / _cond_finish_reports，不与配液共享缓存
        - 推送先于 wait 调用时报文已缓存，wait 进来可直接命中
        - expected_order_id 可用时与 finish 双字段校验；不可用时要求报文 orderId 非空非全 0
        - 配液的 last_order_code / order_finish_event / _order_finish_* 完全不动

        Args:
            order_code: LIMS 电导单号 (BSO...)
            timeout: 超时时间（秒）
            expected_order_id: 建单返回的 orderId（可能为全 0 占位）
        Returns:
            同 wait_for_order_finish 的返回格式
        """
        if not order_code:
            logger.error("_wait_conductivity_finish() 被调用，但 order_code 为空！")
            return {"status": "error", "message": "empty order_code"}

        exp_oid = self._normalize_order_id(expected_order_id)
        deadline = time.monotonic() + max(float(timeout), 0.0)

        with self._order_finish_lock:
            ev = self._cond_finish_events.get(order_code)
            if ev is None:
                ev = threading.Event()
                self._cond_finish_events[order_code] = ev
            else:
                ev.clear()

        logger.info(
            f"[电导wait] 等待电导单完成: orderCode={order_code}, "
            f"expected_orderId={exp_oid or '<未校验>'} (timeout={timeout}s)"
        )

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error(f"[电导wait] 等待电导单超时: orderCode={order_code}")
                    return {"status": "timeout", "orderCode": order_code}

                with self._order_finish_lock:
                    report = self._cond_finish_reports.pop(order_code, None)

                if report is None:
                    triggered = ev.wait(timeout=remaining)
                    with self._order_finish_lock:
                        report = self._cond_finish_reports.pop(order_code, None)
                    if report is None:
                        if not triggered:
                            with self._order_finish_lock:
                                report = self._cond_finish_reports.pop(order_code, None)
                            if report is None:
                                logger.error(f"[电导wait] 等待电导单超时: orderCode={order_code}")
                                return {"status": "timeout", "orderCode": order_code}
                        else:
                            continue
                    ev.clear()

                cached_oid = self._normalize_order_id(report.get("orderId"))
                if not self._is_usable_order_id(cached_oid):
                    logger.warning(
                        f"[电导wait] 报文 orderId 无效({cached_oid!r})，"
                        f"丢弃并继续等待: orderCode={order_code}"
                    )
                    continue

                if not self._report_matches_expected(order_code, exp_oid, report):
                    logger.error(
                        f"[电导wait] 拒绝异源/撞号 finish，继续等待: orderCode={order_code}, "
                        f"期望orderId={exp_oid or '<未校验>'}, "
                        f"报文orderId={cached_oid}, orderName={report.get('orderName')}"
                    )
                    continue

                return self._classify_finish_report(order_code, report, expected_order_id=exp_oid)
        finally:
            with self._order_finish_lock:
                cur = self._cond_finish_events.get(order_code)
                if cur is ev:
                    self._cond_finish_events.pop(order_code, None)

    def get_conductivity_order_result(self, order_id: str) -> Dict[str, Any]:
        """
        查询单条电导单的测试结果（电导率 / 温度等）。

        接口：POST /api/lims/order/conductivity-order-result
        请求体 data 直接是电导单的 orderId（GUID 字符串）。

        Args:
            order_id: 电导单 orderId（GUID）

        Returns:
            LIMS 返回的 data 对象，结构形如：
            {
                "orderId": "...", "bottleId": "...", "bottleBarCode": "",
                "boardId": "...", "boardBarCode": "CD2026062613",
                "bottleInnerX": 1, "bottleInnerY": 1, "bottleInnerZ": 1,
                "conductivity": 10, "conductivityUnit": "ms/cm",
                "temperature": 10, "targetTemperature": 40
            }
            失败时返回 {}（并打 warning）。
        """
        if not order_id:
            logger.warning("[电导结果] get_conductivity_order_result 被调用，但 order_id 为空")
            return {}
        resp = self._post_lims("/api/lims/order/conductivity-order-result", order_id)
        if not isinstance(resp, dict) or resp.get("code") != 1:
            logger.warning(
                f"[电导结果] 查询失败 orderId={order_id}: {resp}"
            )
            return {}
        return resp.get("data") or {}

    def _collect_conductivity_results(
        self, order_pairs: List[Tuple[str, str, str]]
    ) -> List[Dict[str, Any]]:
        """
        逐个 (orderCode, resolved_oid, creation_oid) 调电导结果接口，标准化成行字典列表。

        先用 resolved_oid 查询；若返回空且 creation_oid 与之不同，则用 creation_oid
        兜底重试（防止消费过期 finish 报文导致 resolved_oid 用错）。

        Args:
            order_pairs: [(orderCode, resolved_orderId, creation_orderId), ...]

        Returns:
            [{
                "orderCode": str, "orderId": str,
                "boardBarCode": str, "bottleBarCode": str,
                "bottleInnerX": int, "bottleInnerY": int, "bottleInnerZ": int,
                "conductivity": float, "conductivityUnit": str,
                "temperature": float, "targetTemperature": float,
                "report_time": "YYYY-MM-DD HH:MM:SS",
            }, ...]
            查询失败的订单仍保留一行（数值字段留空 / None），便于排查。
        """
        EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
        results: List[Dict[str, Any]] = []
        for order_code, resolved_oid, creation_oid in order_pairs:
            report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            order_id = (resolved_oid or "").strip()
            creation = (creation_oid or "").strip()

            if not order_id or order_id == EMPTY_GUID:
                # resolved 无效时直接试 creation
                if creation and creation != EMPTY_GUID:
                    logger.info(
                        f"[电导结果] orderCode={order_code} resolved_oid 无效，"
                        f"改用 creation_oid={creation[:8]}... 查询"
                    )
                    order_id = creation
                else:
                    logger.warning(
                        f"[电导结果] orderCode={order_code} 的 orderId 无效"
                        f"(resolved={resolved_oid}, creation={creation_oid})，跳过结果查询"
                    )
                    results.append({
                        "orderCode": order_code, "orderId": order_id or "",
                        "boardBarCode": "", "bottleBarCode": "",
                        "bottleInnerX": None, "bottleInnerY": None, "bottleInnerZ": None,
                        "conductivity": None, "conductivityUnit": None,
                        "temperature": None, "targetTemperature": None,
                        "report_time": report_time,
                    })
                    continue

            data = self.get_conductivity_order_result(order_id)
            # 兜底：resolved 查空且 creation 不同，再用 creation 重试
            if (
                not data
                and creation
                and creation != EMPTY_GUID
                and creation != order_id
            ):
                logger.info(
                    f"[电导结果] orderCode={order_code} resolved_oid={order_id[:8]}... "
                    f"查无数据，改用 creation_oid={creation[:8]}... 兜底重试"
                )
                data = self.get_conductivity_order_result(creation)
                if data:
                    order_id = creation

            results.append({
                "orderCode": order_code,
                "orderId": order_id,
                "boardBarCode": data.get("boardBarCode", "") or "",
                "bottleBarCode": data.get("bottleBarCode", "") or "",
                "bottleInnerX": data.get("bottleInnerX"),
                "bottleInnerY": data.get("bottleInnerY"),
                "bottleInnerZ": data.get("bottleInnerZ"),
                "conductivity": data.get("conductivity"),
                "conductivityUnit": data.get("conductivityUnit"),
                "temperature": data.get("temperature"),
                "targetTemperature": data.get("targetTemperature"),
                "report_time": report_time,
            })
            logger.info(
                f"[电导结果] orderCode={order_code} orderId={order_id[:8]}...: "
                f"board={data.get('boardBarCode')}, bottle={data.get('bottleBarCode')}, "
                f"X={data.get('bottleInnerX')}, Y={data.get('bottleInnerY')}, "
                f"cond={data.get('conductivity')}, T={data.get('temperature')}/"
                f"{data.get('targetTemperature')}"
            )
        return results

    def _wait_conductivity_and_resolve_ids(
        self,
        order_pairs: List[Tuple[str, str]],
        wait_timeout_seconds: int,
        tag: str = "conductivity",
    ) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
        """
        逐单阻塞等 /report/order_finish 推送，并回填真实 orderId。

        建单返回的 orderId 可能是全 0 GUID（执行时才填），finish 报文里带的是
        真实 orderId；这里优先用 finish 报文的 orderId，零 GUID 时回退创建时的值，
        供后续 /conductivity-order-result 按 orderId 查询。

        进入 wait 循环前会按本批 orderCode 清掉电导专用缓存中的历史残留，
        避免「报文已缓存立即返回」消费到过期 finish 推送。

        Args:
            order_pairs: [(orderCode, creation_orderId), ...]
            wait_timeout_seconds: 单个订单 wait 超时秒数
            tag: 日志标签

        Returns:
            (resolved_pairs, summary)
            resolved_pairs: [(orderCode, resolved_orderId, creation_orderId), ...]
            summary: {"success": int, "timeout": int, "abnormal_stop": int,
                      "manual_stop": int, "mismatch": int, "other": int}
        """
        EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
        summary = {
            "success": 0, "timeout": 0, "abnormal_stop": 0,
            "manual_stop": 0, "mismatch": 0, "other": 0,
        }
        resolved: List[Tuple[str, str, str]] = []
        total = len(order_pairs)

        # Fix①：进 wait 前清掉本批 orderCode 在电导缓存中的历史残留
        batch_codes = [oc for oc, _ in order_pairs if oc]
        if batch_codes:
            with self._order_finish_lock:
                cleared = 0
                for oc in batch_codes:
                    if self._cond_finish_reports.pop(oc, None) is not None:
                        cleared += 1
                    self._cond_finish_events.pop(oc, None)
            if cleared:
                logger.info(
                    f"[{tag}] 进 wait 前清理电导缓存残留 {cleared}/{len(batch_codes)} 条"
                )

        logger.info(
            f"[{tag}] 开始阻塞等待 {total} 个电导单完成 "
            f"(单订单 timeout={wait_timeout_seconds}s)..."
        )
        for idx, (order_code, creation_oid) in enumerate(order_pairs, 1):
            logger.info(f"[{tag}] 等待第 {idx}/{total} 个电导单: {order_code}")
            wait_result = self._wait_conductivity_finish(
                order_code,
                timeout=wait_timeout_seconds,
                expected_order_id=creation_oid,
            )
            wait_status = wait_result.get("status", "other")
            if wait_status == "success":
                summary["success"] += 1
                logger.info(f"[{tag}] ✓ 电导单 {order_code} 完成")
            elif wait_status in summary:
                summary[wait_status] += 1
                logger.warning(
                    f"[{tag}] ⚠ 电导单 {order_code} 非正常结束: status={wait_status}"
                )
            else:
                summary["other"] += 1
                logger.warning(
                    f"[{tag}] ⚠ 电导单 {order_code} 未知 wait status: {wait_status}"
                )

            report = wait_result.get("report") or {}
            report_oid = (report.get("orderId") or "").strip()
            if report_oid and report_oid != EMPTY_GUID:
                resolved_oid = report_oid
            else:
                resolved_oid = creation_oid or ""
            resolved.append((order_code, resolved_oid, creation_oid or ""))

        logger.info(f"[{tag}] 全部电导单等待结束: summary={summary}")
        return resolved, summary

    def get_material_info(self, material_id: str) -> Dict[str, Any]:
        """查询物料详细信息（物料详情接口）
        
        Args:
            material_id: 物料 ID (GUID)
            
        Returns:
            物料详情，包含 name, typeName, locations 等；失败返回空 dict
        """
        result = self._post_lims("/api/lims/storage/material-info", material_id)
        if result.get("error"):
            logger.error(
                f"[material-info] 请求失败: materialId={material_id}, error={result.get('error')}"
            )
            return {}
        if result.get("code") != 1:
            logger.error(
                f"[material-info] 业务失败: materialId={material_id}, "
                f"code={result.get('code')}, message={result.get('message')}"
            )
            return {}
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def _enrich_report_materials_from_create(
        self,
        report: Optional[Dict[str, Any]],
        create_entry: Optional[Dict[str, Any]],
    ) -> None:
        """用建单 usedMaterials 的 materialTypeName/materialName 补全 finish 报文（material-info 降级）。"""
        if not report or not create_entry:
            return
        by_id = {
            m.get("materialId"): m
            for m in (create_entry.get("usedMaterials") or [])
            if isinstance(m, dict) and m.get("materialId")
        }
        if not by_id:
            return
        for m in report.get("usedMaterials") or []:
            if not isinstance(m, dict):
                continue
            src = by_id.get(m.get("materialId"))
            if not src:
                continue
            if not m.get("materialTypeName") and src.get("materialTypeName"):
                m["materialTypeName"] = src["materialTypeName"]
            if not m.get("materialName") and src.get("materialName"):
                m["materialName"] = src["materialName"]

    def _resolve_material_type_info(self, material: Dict[str, Any], material_id: str) -> Dict[str, Any]:
        """解析物料类型与条码：typeName 可从报文快取，barCode 必须以真实条码为准。

        finish 推送 / 建单 usedMaterials 通常无 barCode；真实条码只在 material-info。
        禁止用 materialName（如占位名 999/888）冒充条码。
        """
        type_name = (material.get("materialTypeName") or material.get("typeName") or "").strip()
        bar_code = (material.get("barCode") or "").strip()  # 不再用 materialName 冒充条码
        need_barcode = (not type_name) or ("瓶" in type_name)  # 仅瓶/板需要真实条码
        if type_name and bar_code:
            # 报文已含真实条码，直接用
            return {
                "typeName": type_name,
                "barCode": bar_code,
                "associateId": material.get("associateId") or "",
                "locations": material.get("locations") or [],
                "name": material.get("materialName") or material.get("name") or "",
            }
        if not need_barcode and type_name:
            # 消耗品等：只要类型名，不查接口
            return {
                "typeName": type_name,
                "barCode": "",
                "associateId": material.get("associateId") or "",
                "locations": material.get("locations") or [],
                "name": material.get("materialName") or material.get("name") or "",
            }
        # 需要补 barCode（或连 typeName 都缺）→ 查一次 material-info
        try:
            info = self._query_material_info(material_id)
        except Exception as e:
            logger.warning(
                f"[物料类型] material-info 失败，降级: materialId={material_id}, 错误={e}"
            )
            info = {}
        return {
            "typeName": type_name or (info.get("typeName") or ""),
            "barCode": bar_code or (info.get("barCode") or ""),  # 真实条码来源
            "associateId": material.get("associateId") or info.get("associateId") or "",
            "locations": info.get("locations") or material.get("locations") or [],
            "name": material.get("materialName") or info.get("name") or material.get("name") or "",
        }

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
        
        # 2. 查询试剂名称（material-info 失败时回退 materialName / Unknown）
        reagent_data = []
        for reagent in reagents:
            material_id = reagent.get("materialId")
            if not material_id:
                continue
                
            try:
                info = self.get_material_info(material_id)
                name = (
                    (info.get("name") if info else None)
                    or reagent.get("materialName")
                    or f"Unknown_{material_id[:8]}"
                )
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

        # 4. 计算各试剂允差：(真实质量 - 目标质量) / 目标质量
        def calculate_mass_tolerance(items: List[Dict]) -> Dict[str, float]:
            result = {}
            for item in items:
                target = item["used_quantity"]
                real = item["real_quantity"]
                if target == 0:
                    result[item["name"]] = None
                else:
                    result[item["name"]] = round((real - target) / target, 6)
            return result

        # 5. 计算总质量允差：(Σ真实质量 - Σ目标质量) / Σ目标质量
        total_real = sum(item["real_quantity"] for item in reagent_data)
        total_used = sum(item["used_quantity"] for item in reagent_data)
        if total_used == 0:
            total_mass_tolerance = None
        else:
            total_mass_tolerance = round((total_real - total_used) / total_used, 6)

        real_mass_ratio = calculate_mass_ratio(reagent_data, "real_quantity")
        target_mass_ratio = calculate_mass_ratio(reagent_data, "used_quantity")
        mass_tolerance = calculate_mass_tolerance(reagent_data)

        logger.info(f"真实质量比: {real_mass_ratio}")
        logger.info(f"目标质量比: {target_mass_ratio}")
        logger.info(f"各试剂允差: {mass_tolerance}")
        logger.info(f"总质量允差: {total_mass_tolerance}")

        return {
            "real_mass_ratio": real_mass_ratio,
            "target_mass_ratio": target_mass_ratio,
            "mass_tolerance": mass_tolerance,
            "total_mass_tolerance": total_mass_tolerance,
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
        order_data = response.get("data") or {}
        order_code = order_data.get("orderCode")
        if not order_code:
            logger.error(f"上料任务未返回有效 orderCode！完整响应：{response}")
            return response
        result = self.wait_for_order_finish(
            order_code, expected_order_id=order_data.get("orderId")
        )
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
    def _sum_required_reagents(self, orders: List[Dict[str, Any]]) -> Dict[str, float]:
        """把整批订单的 materialInfos 按试剂名汇总成总需求量（g）。"""
        required: Dict[str, float] = {}
        for od in orders:
            for mat in od.get("materialInfos") or []:
                if not isinstance(mat, dict):
                    continue
                name = str(mat.get("name") or "").strip()
                if not name:
                    continue
                try:
                    mass = float(mat.get("mass") or 0)
                except (TypeError, ValueError):
                    continue
                if mass > 0:
                    required[name] = required.get(name, 0.0) + mass
        return required

    # 预检按克比对，非质量单位的试剂无法直接对比，只能跳过该项
    _MASS_UNITS = ("g", "gram", "grams", "克")

    def _query_reagent_stock(self) -> Optional[Dict[str, Any]]:
        """查 2.3 stock-material 取在库试剂可用量（g），按试剂名累加同名多瓶。

        typeMode=2 即「试剂」（1=样品、0=耗材），不会漏查其它 typeMode。

        Returns:
            {
              "stock":        {试剂名: 可用量(g)},
              "unit_skipped": {试剂名: 非质量单位},   # 这些项预检时跳过
              "names":        [在库全部试剂名],       # 供失配时列清单排查
            }
            查询失败返回 None，由调用方决定降级策略——预检是拦截手段，
            不能因为查询本身出问题就挡住正常建单。
        """
        resp = self._post_lims(
            "/api/lims/storage/stock-material",
            {"typeMode": 2, "includeDetail": False},
        )
        if not isinstance(resp, dict) or resp.get("code") != 1:
            return None
        data = resp.get("data")
        if not isinstance(data, list):
            return None

        stock: Dict[str, float] = {}
        unit_skipped: Dict[str, str] = {}
        names: List[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            names.append(name)

            # 单位校验：materialInfos.mass 是克，拿克跟 mL 比是静默错误，只能跳过该项
            unit = str(item.get("unit") or "g").strip()
            if unit.casefold() not in self._MASS_UNITS:
                unit_skipped[name] = unit
                continue

            try:
                quantity = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            # lockQuantity 常为负（LIMS 记的是余量修正），只有正值才是真占用，扣掉更保守
            try:
                locked = max(float(item.get("lockQuantity") or 0), 0.0)
            except (TypeError, ValueError):
                locked = 0.0
            stock[name] = stock.get(name, 0.0) + quantity - locked

        return {"stock": stock, "unit_skipped": unit_skipped, "names": sorted(set(names))}

    def _compare_reagents(
        self, required: Dict[str, float], snapshot: Dict[str, Any]
    ) -> Dict[str, Any]:
        """把需求量与在库快照逐项比对，返回结构化报告（不抛错）。

        `_preflight_check_reagents`（单批建单前）与 `precheck_reagents_total`
        （多轮提交前）共用同一套判据，避免两处口径漂移。

        判据取舍：
        - 单位非克 → 跳过该项（放行），单位对不上时无法比较，硬拦属于自伤
        - 在库查无此名 → **硬拦**。已决策宁可误拦不漏拦；但报错里附在库试剂名清单，
          便于一眼区分「名称失配」与「真的没有这个试剂」
        """
        stock: Dict[str, float] = snapshot.get("stock") or {}
        unit_skipped: Dict[str, str] = snapshot.get("unit_skipped") or {}
        stock_names: List[str] = snapshot.get("names") or []
        # LIMS 里试剂名大小写偶有出入，精确匹配不中时降级按大小写不敏感再找一次
        stock_ci = {k.casefold(): v for k, v in stock.items()}
        skipped_ci = {k.casefold(): v for k, v in unit_skipped.items()}

        items: List[Dict[str, Any]] = []
        shortages: List[str] = []
        skipped: List[str] = []
        for name in sorted(required):
            need = required[name]

            bad_unit = unit_skipped.get(name) or skipped_ci.get(name.casefold())
            if bad_unit is not None:
                skipped.append(f"{name}（单位 {bad_unit} 非质量单位）")
                items.append({
                    "name": name, "required": round(need, 4),
                    "available": None, "gap": None, "status": "skipped_unit",
                })
                continue

            have = stock.get(name)
            if have is None:
                have = stock_ci.get(name.casefold())

            if have is None:
                shortages.append(f"{name}: 需要 {need:.4f} g，在库无此试剂")
                items.append({
                    "name": name, "required": round(need, 4),
                    "available": None, "gap": None, "status": "not_found",
                })
            elif have < need:
                shortages.append(
                    f"{name}: 需要 {need:.4f} g，可用 {have:.4f} g，缺 {need - have:.4f} g"
                )
                items.append({
                    "name": name, "required": round(need, 4),
                    "available": round(have, 4), "gap": round(need - have, 4),
                    "status": "short",
                })
            else:
                items.append({
                    "name": name, "required": round(need, 4),
                    "available": round(have, 4), "gap": 0.0, "status": "ok",
                })

        return {
            "ok": not shortages,
            "items": items,
            "shortages": shortages,
            "skipped": skipped,
            "has_not_found": any(i["status"] == "not_found" for i in items),
            "stock_names": stock_names,
        }

    @staticmethod
    def _format_stock_name_hint(stock_names: List[str], limit: int = 30) -> str:
        """「在库无此试剂」时附上在库试剂名清单，用于区分名称失配与真缺料。"""
        if not stock_names:
            return "（在库试剂名清单为空，可能是库存查询口径有误）"
        shown = stock_names[:limit]
        more = f" ...等共 {len(stock_names)} 种" if len(stock_names) > limit else ""
        return f"在库试剂名清单: {', '.join(shown)}{more}"

    def _preflight_check_reagents(self, orders: List[Dict[str, Any]], tag: str) -> None:
        """建单前校验整批试剂需求是否够，不够就一单都不建。

        LIMS 的 /order/orders 是逐单校验逐单建：某单试剂不足只有它 errorMessage
        非空，其余单照样建出来并进调度队列，人工得去撤半批单。这里在提交前把整批
        需求汇总跟在库量比一次，缺料直接抛错，避免产生需要善后的半批订单。

        Raises:
            BioyondException: 有任一试剂不足时抛出，附每种试剂的需求/可用/缺口
        """
        required = self._sum_required_reagents(orders)
        if not required:
            return

        snapshot = self._query_reagent_stock()
        if snapshot is None:
            logger.warning(
                f"[{tag}] ⚠️ 试剂库存查询失败，跳过建单前预检，改由 LIMS 逐单校验兜底"
            )
            return

        report = self._compare_reagents(required, snapshot)

        for note in report["skipped"]:
            logger.warning(f"[{tag}] ⚠️ 预检跳过 {note}——无法与克数比较，该项交由 LIMS 校验")

        if not report["ok"]:
            shortage_text = "; ".join(report["shortages"])
            # 「在库无此试剂」既可能是真缺料，也可能是名称失配，附清单让人当场分辨
            hint = (
                f" {self._format_stock_name_hint(report['stock_names'])}"
                if report["has_not_found"] else ""
            )
            logger.error(
                f"[{tag}] ❌ 建单前预检未通过，{len(orders)} 单全部不下发: {shortage_text}{hint}"
            )
            raise BioyondException(
                f"[{tag}] 试剂余量不足，{len(orders)} 单全部未下发（避免产生半批待处置订单）。"
                f"缺口: {shortage_text}。请补齐试剂后重新下发。{hint}"
            )

        logger.info(
            f"[{tag}] ✅ 建单前预检通过，{len(required)} 种试剂余量充足: "
            + ", ".join(
                f"{i['name']} 需{i['required']:.4f}/余{i['available']:.4f}g"
                for i in report["items"] if i["status"] == "ok"
            )
        )

    @staticmethod
    def _sum_formulation_reagents(formulation: List[Dict[str, Any]]) -> Dict[str, float]:
        """把一轮 formulation（create_orders_formulation 入参形状）按试剂名汇总（g）。

        形状：[{"order_name": ..., "materials": [{"name": ..., "mass": ...}]}, ...]
        """
        required: Dict[str, float] = {}
        for entry in formulation or []:
            if not isinstance(entry, dict):
                continue
            for mat in entry.get("materials") or []:
                if not isinstance(mat, dict):
                    continue
                name = str(mat.get("name") or "").strip()
                if not name:
                    continue
                try:
                    mass = float(mat.get("mass") or 0)
                except (TypeError, ValueError):
                    continue
                if mass > 0:
                    required[name] = required.get(name, 0.0) + mass
        return required

    def _sum_xlsx_reagents(self, xlsx_path: str) -> Dict[str, float]:
        """把一轮 Excel（create_orders 入参）按试剂名汇总（g）。

        物料列口径与 create_orders 保持一致：以「(g)」结尾且不是总质量聚合列。
        """
        path = Path(xlsx_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到 Excel 文件：{path}")
        try:
            df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"读取 Excel 失败（{path}）：{e}")

        _NON_MATERIAL_NAMES = {"总质量", "totalMass", "TotalMass", "total_mass"}
        material_cols = [
            c for c in df.columns
            if isinstance(c, str) and c.endswith("(g)")
            and c.replace("(g)", "").strip() not in _NON_MATERIAL_NAMES
        ]
        if not material_cols:
            raise KeyError(f"{path} 中未发现任何以「(g)」结尾的物料列，请检查表头。")

        required: Dict[str, float] = {}
        for _, row in df.iterrows():
            for mcol in material_cols:
                val = row.get(mcol, None)
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    continue
                try:
                    mass = float(val)
                except (TypeError, ValueError):
                    continue
                if mass > 0:
                    name = mcol.replace("(g)", "").strip()
                    required[name] = required.get(name, 0.0) + mass
        return required

    def precheck_reagents_total(
        self,
        formulations: Optional[List[List[Dict[str, Any]]]] = None,
        xlsx_paths: Optional[List[str]] = None,
        raise_on_shortage: bool = False,
    ) -> Dict[str, Any]:
        """多批次总量预检：汇总所有待提交轮次的试剂需求，与在库余量比对一次。

        为什么放在提交之前：此刻**没有任何在飞批次**，库存不含未决占用，
        因此不需要扣减、也不用判断 LIMS 是建单即锁还是消耗才扣——一次
        `在库 >= 总需求` 就定论。这是最早、也是唯一无歧义的检查点。

        它是**建议性**的，不是保证：从本次检查到最后一轮跑完，中间可能有
        别人提交的任务、手工取用、其它工站消耗使结论失效。所以
        `create_orders` 内的建单前预检必须保留，那才是最终权威判据。

        Args:
            formulations: 多轮 formulation 列表，每个元素是一轮的 formulation
                （形状同 create_orders_formulation 的入参）
            xlsx_paths: 多轮 Excel 路径列表（形状同 create_orders 的入参）
            raise_on_shortage: 缺料时是否抛错。默认 False —— 它是提交侧的查询，
                由调用方决定要不要提交；置 True 可当工作流节点用

        Returns:
            {
              "ok": bool,                 # 总需求是否都够
              "rounds": int,              # 汇总了几轮
              "items": [{name, required, available, gap, status}],
              "shortages": [...],         # 缺口描述，缺料时即补料清单
              "skipped": [...],           # 因单位非克而跳过的项
              "stock_query_ok": bool,     # 库存查询是否成功
            }

        Raises:
            BioyondException: raise_on_shortage=True 且有缺口时
        """
        rounds = 0
        required: Dict[str, float] = {}

        for formulation in (formulations or []):
            part = self._sum_formulation_reagents(formulation)
            for name, mass in part.items():
                required[name] = required.get(name, 0.0) + mass
            rounds += 1

        for xlsx_path in (xlsx_paths or []):
            part = self._sum_xlsx_reagents(xlsx_path)
            for name, mass in part.items():
                required[name] = required.get(name, 0.0) + mass
            rounds += 1

        if not required:
            logger.warning("[总量预检] 未解析出任何试剂需求，请检查 formulations / xlsx_paths 入参")
            return {
                "ok": True, "rounds": rounds, "items": [], "shortages": [],
                "skipped": [], "stock_query_ok": True,
                "message": "未解析出任何试剂需求，跳过比对",
            }

        snapshot = self._query_reagent_stock()
        if snapshot is None:
            logger.warning("[总量预检] ⚠️ 试剂库存查询失败，无法给出结论，交由建单前预检兜底")
            return {
                "ok": True, "rounds": rounds, "items": [], "shortages": [],
                "skipped": [], "stock_query_ok": False,
                "message": "库存查询失败，未能比对；请依赖 create_orders 内的建单前预检",
            }

        report = self._compare_reagents(required, snapshot)
        result = {
            "ok": report["ok"],
            "rounds": rounds,
            "items": report["items"],
            "shortages": report["shortages"],
            "skipped": report["skipped"],
            "stock_query_ok": True,
        }

        for note in report["skipped"]:
            logger.warning(f"[总量预检] ⚠️ 跳过 {note}——无法与克数比较")

        if report["ok"]:
            logger.info(
                f"[总量预检] ✅ {rounds} 轮共 {len(required)} 种试剂余量充足: "
                + ", ".join(
                    f"{i['name']} 需{i['required']:.4f}/余{i['available']:.4f}g"
                    for i in report["items"] if i["status"] == "ok"
                )
            )
            return result

        shortage_text = "; ".join(report["shortages"])
        hint = (
            f" {self._format_stock_name_hint(report['stock_names'])}"
            if report["has_not_found"] else ""
        )
        result["message"] = f"{rounds} 轮总需求超出在库余量。缺口: {shortage_text}。{hint}"
        logger.error(f"[总量预检] ❌ {result['message']}")

        if raise_on_shortage:
            raise BioyondException(
                f"[总量预检] 试剂总量不足，请勿提交这 {rounds} 轮实验。"
                f"缺口: {shortage_text}。{hint}"
            )
        return result

    # -------------------- 多任务互锁：order-list 门控 --------------------

    @staticmethod
    def _lims_time_window(lookback_minutes: int) -> Tuple[str, str, datetime]:
        """构造 order-list 的时间窗，并返回**精确**的客户端截断时刻。

        实测口径（2026-08-04 对 172.16.28.127 实测，勿凭直觉改）：

        1. `timeType` 必须是**被过滤字段名** `"requestTime"`。填 `"beginTime"`
           （请求参数名）会被 LIMS 静默忽略 → 时间窗完全不生效、返回全部历史单。
           `createTime`/`startTime`/`endTime`/`finishTime` 同样是静默忽略。
        2. 过滤粒度是**按天**，起止两端都取日期闭区间，时分秒被丢弃：
           `[08-03T12:30, 08-03T23:59]` 与 `[08-03T00:00, 08-03T00:00]` 返回
           完全相同的结果（含当天 11:06 的单）。所以服务端窗口只能粗筛到「天」，
           分钟级的 lookback 必须由调用方用返回的 `requestTime` 再精确过滤一次，
           这就是第三个返回值 `cutoff` 的用途。
        3. 时间字面量用 ISO `T` 分隔即可，毫秒与 `Z` 后缀都可省略、结果一致；
           但换成空格分隔 `"YYYY-MM-DD HH:MM:SS"` 会让过滤静默失效（返回全量）。
        4. 按裸本地时间比较，Z 纯装饰——不要「顺手修正」成真 UTC，否则偏 8 小时。

        Returns:
            (beginTime 字面量, endTime 字面量, 精确截断时刻)
        """
        now = datetime.now()
        cutoff = now - timedelta(minutes=lookback_minutes)
        # 服务端按天取闭区间，故窗口起点必然放宽到 cutoff 当天 00:00，只作粗筛
        begin = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return begin, end, cutoff

    def _resolve_order_keywords(
        self, scenario: str = "all", workflow_name_keyword: str = ""
    ) -> List[str]:
        """解析门控过滤关键字。

        优先级：显式 workflow_name_keyword > scenario 查表 > 回落 all。
        未知 scenario 打 warning 后回落 ["配液"]，绝不静默放宽或收紧——
        静默收紧比报错危险，会让门控漏掉本该挡住的单。
        """
        kw = (workflow_name_keyword or "").strip()
        if kw:
            return [kw]
        key = (scenario or "all").strip() or "all"
        keywords = self.SCENARIO_ORDER_KEYWORDS.get(key)
        if keywords is None:
            logger.warning(
                f"[互锁门控] 未知 scenario={key!r}，回落到 all（关键字「配液」）。"
                f"可选: {list(self.SCENARIO_ORDER_KEYWORDS)}"
            )
            return list(self.SCENARIO_ORDER_KEYWORDS["all"])
        return list(keywords)

    def _query_scheduler_status(self) -> Optional[str]:
        """查调度状态（2.x scheduler-status），返回 data.schedulerStatus。

        取值：Init / Stop / Running / Pause / ErrorPause / ErrorStop。
        查询失败返回 None——门控只把它当参考，不因状态查询本身失败而阻断。
        """
        try:
            resp = self.scheduler_status()
        except Exception as e:
            logger.warning(f"[互锁门控] 调度状态查询异常: {e}")
            return None
        if not isinstance(resp, dict) or resp.get("code") != 1:
            logger.warning(f"[互锁门控] 调度状态查询返回异常: {resp}")
            return None
        status = ((resp.get("data") or {}).get("schedulerStatus") or "").strip()
        return status or None

    @staticmethod
    def _parse_lims_time(value: Any) -> Optional[datetime]:
        """解析 LIMS 返回的裸本地时间（如 2026-08-03T11:06:04.565674），失败返回 None。"""
        if not isinstance(value, str) or not value:
            return None
        text = value.strip().rstrip("Z")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _paginate_order_list(
        self,
        begin_time: str,
        end_time: str,
        keywords: List[str],
        page_size: int = 100,
        max_pages: int = 40,
        cutoff: Optional[datetime] = None,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """分页扫窗口内命中关键字的配液单，按**响应里的** status 字段分桶返回。

        ⚠️ 关键口径：**请求侧 `status` 与响应侧 `status` 不是同一套枚举**，
        所以这里请求侧传空串（=不按状态过滤），状态判定完全交给响应字段。
        2026-08-04 实测（近 60h 窗口）：

        | 请求 status | 命中数 | 这些单响应里的 status  |
        |-------------|--------|------------------------|
        | ""          | 33     | 80×31, 90×2            |
        | "60"        | 11     | **全是 80 Succeed**    |
        | "80"        | 31     | 80×31                  |
        | "90"        | 2      | 90×2                   |
        | "100"       | 22     | 80×20, 90×2            |

        请求 `"60"` 的 11 条**全部带 completeTime**（早已跑完），且
        `请求60 ∩ 请求100 = 0`、`11 + 20 = 31`（成功单被二分）——可见请求侧
        `60`/`100` 是「**未取出**/**已取出**」，而不是响应侧的「运行中/已取出」。
        照请求侧 `status=60` 当「正在运行」用，会把「配液已完成但板子还没取出」
        的单误判成在跑，门控就会一直空等到超时。

        请求侧 `""` 实测是所有分桶的超集（`"60"/"80"/"90"/"100"` 均 ⊆ `""`），
        所以单次扫描足够，不会漏掉真正在跑的单。窗口内总量也不大（近 60h 33 条、
        7 天 98 条、14 天 394 条），全量翻页成本可接受。

        `cutoff` 是精确的时间下界：服务端时间窗只有「天」粒度（见
        `_lims_time_window`），会把 begin 当天 00:00 起的单全都带回来，所以这里必须
        再按 `requestTime` 精确过滤一次，否则 lookback 会被静默放宽最多一天。
        `requestTime` 缺失或解析失败的条目保守保留（宁可多拦不可漏拦）。

        注意 pageCount 是「每页条数」而非「页数」。

        Returns:
            {响应status: [订单摘要]}，未出现的状态不建键
        """
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        skip = 0
        stale = 0
        scanned = 0
        for page in range(max_pages):
            try:
                resp = self.order_list_v2(
                    timeType="requestTime",
                    beginTime=begin_time,
                    endTime=end_time,
                    status="",  # 状态判定见 docstring，一律交给响应字段
                    skipCount=skip,
                    pageCount=page_size,
                )
            except Exception as e:
                logger.warning(f"[互锁门控] order-list 查询异常（page={page}）: {e}")
                return buckets

            data = (resp or {}).get("data") or {}
            items = data.get("items") or []
            total = int(data.get("totalCount") or 0)

            for it in items:
                if not isinstance(it, dict):
                    continue
                wf_name = it.get("workflowName") or ""
                if not any(kw in wf_name for kw in keywords):
                    continue
                req_dt = self._parse_lims_time(it.get("requestTime"))
                if cutoff is not None and req_dt is not None and req_dt < cutoff:
                    # 服务端只按天粗筛带回来的陈旧单，不计入门控
                    stale += 1
                    continue
                try:
                    st = int(it.get("status"))
                except (TypeError, ValueError):
                    # 状态读不出来时保守当「运行中」，宁可多拦不可漏拦
                    logger.warning(
                        f"[互锁门控] 订单 {it.get('name')} 的 status 无法解析"
                        f"（原值={it.get('status')!r}），保守按运行中处理"
                    )
                    st = self._ORDER_STATUS_RUNNING_CODE
                buckets.setdefault(st, []).append({
                    "orderName": it.get("name"),
                    "orderId": it.get("id") or it.get("orderId"),
                    "workflowName": wf_name,
                    "status": st,
                    "statusName": it.get("statusName"),
                    "requestTime": it.get("requestTime"),
                    "completeTime": it.get("completeTime"),
                })

            scanned += len(items)
            skip += len(items)
            if not items or skip >= total:
                break

            if page == max_pages - 1:
                logger.warning(
                    f"[互锁门控] 翻页达上限 {max_pages} 页（已扫 {scanned}/{total} 条），"
                    f"剩余未扫，门控判定可能不完整"
                )

        logger.info(
            f"[互锁门控] 扫描窗口内 {scanned} 条订单，命中配液单按状态分桶: "
            f"{ {k: len(v) for k, v in sorted(buckets.items())} }"
            + (f"，另有 {stale} 条超出精确回溯窗口已忽略" if stale else "")
        )
        return buckets

    @staticmethod
    def _count_by_workflow(items: List[Dict[str, Any]]) -> Dict[str, int]:
        """按 workflowName 分场景计数，供日志观测跨场景互相挡的频率。"""
        counts: Dict[str, int] = {}
        for it in items:
            name = it.get("workflowName") or "<未知>"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _scan_workflow_statuses(
        self,
        keywords: List[str],
        lookback_minutes: int,
        page_size: int = 100,
        max_pages: int = 40,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """扫一轮窗口内的执行中(60)与失败(90)配液单。

        单次分页扫描 + 按响应 status 分桶；不要改回「按请求 status 各查一次」，
        请求侧 60 是「未取出」而非「运行中」，详见 `_paginate_order_list`。
        """
        begin_time, end_time, cutoff = self._lims_time_window(lookback_minutes)
        buckets = self._paginate_order_list(
            begin_time, end_time, keywords, page_size, max_pages, cutoff
        )
        return {
            "running": buckets.get(self._ORDER_STATUS_RUNNING_CODE, []),
            "failed": buckets.get(self._ORDER_STATUS_FAILED_CODE, []),
            "begin_time": begin_time,
            "end_time": end_time,
        }

    def _pick_order_by_name(
        self,
        items: List[Dict[str, Any]],
        expect_name: str,
        registered_at: Optional[float],
        order_code: str,
    ) -> Optional[Dict[str, Any]]:
        """在同名多轮订单里挑出「本轮」那一个。

        orderName 不唯一（见 `_check_batch_all_finished`），单纯取第一条同名的会撞到
        上一轮。建单发生在 `register_interlock_batch` 之前几秒，所以本轮那一单的
        requestTime 应该是**不晚于 registered_at、且离它最近**的那个。
        """
        candidates = [it for it in items if it.get("name") == expect_name]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if registered_at is None:
            logger.warning(
                f"[互锁门控] {order_code}（{expect_name}）有 {len(candidates)} 个同名单，"
                f"但缓存里没有 registered_at 可供消歧，取 requestTime 最新的一条"
            )
            return max(candidates, key=lambda it: it.get("requestTime") or "")

        # 容忍少量时钟偏差：建单在注册之前，允许晚 60s
        limit = datetime.fromtimestamp(registered_at) + timedelta(seconds=60)
        in_window = [
            it for it in candidates
            if (dt := self._parse_lims_time(it.get("requestTime"))) is not None and dt <= limit
        ]
        pool = in_window or candidates
        picked = max(pool, key=lambda it: it.get("requestTime") or "")
        logger.info(
            f"[互锁门控] {order_code}（{expect_name}）命中 {len(candidates)} 个同名单，"
            f"按 registered_at={limit:%Y-%m-%d %H:%M:%S} 消歧后取 "
            f"requestTime={picked.get('requestTime')} / orderId={picked.get('id')}"
        )
        if not in_window:
            logger.warning(
                f"[互锁门控] {order_code}（{expect_name}）的同名单 requestTime 全都晚于"
                f" registered_at，消歧依据不可靠，请核对是否有并发建单"
            )
        return picked

    def _check_batch_all_finished(self) -> Tuple[bool, List[Dict[str, Any]]]:
        """核对互锁缓存里的上一批订单是否已全部到达终态（80/100）。

        用 orderCode 查 order-list，再用 orderId 双字段确认，防真机/仿真机撞号。

        ⚠️ orderName **不唯一**：实测同一批名字（如 DDCC1001~1008）会被多轮实验重复
        使用，2026-08-04 的 60h 窗口里 DDCC1008 就有 3 个不同 orderId（02:02/02:13/
        02:23 三轮）。所以 orderId 不可用时的 orderName 兜底必须再用 `registered_at`
        挑出「本轮那一个」，否则可能匹配到上一轮早已 Succeed 的同名单，
        把还在跑的本轮误判成已完成、提前放行下一批。
        """
        with self._interlock_lock:
            batch = dict(self._interlock_batch)
        if not batch:
            return True, []

        with self._interlock_lock:
            meta = self._interlock_batch_meta or {}
            name_by_code = dict(meta.get("order_name_by_code") or {})
            registered_at = meta.get("registered_at")

        pending: List[Dict[str, Any]] = []
        for order_code, expect_oid in batch.items():
            try:
                resp = self.order_list_v2(filter=order_code, pageCount=20)
            except Exception as e:
                logger.warning(f"[互锁门控] 核对批次单 {order_code} 查询异常: {e}")
                pending.append({"orderCode": order_code, "reason": f"查询异常: {e}"})
                continue

            items = ((resp or {}).get("data") or {}).get("items") or []
            matched = None
            # 优先按 orderId 匹配，避免真机/仿真机 orderCode 撞号时捞错单
            exp = self._normalize_order_id(expect_oid)
            if self._is_usable_order_id(exp):
                for it in items:
                    if self._normalize_order_id(it.get("id") or it.get("orderId")) == exp:
                        matched = it
                        break
            # orderId 不可用（如电导建单偶发的全 0 占位）时退化按 orderName 匹配，
            # 口径与 _recover_timeout_order_report 一致。没有这条兜底的话，
            # 多行命中会一直判为「未到终态」，把门控挂到 24h 超时
            expect_name = name_by_code.get(order_code)
            if matched is None and expect_name:
                matched = self._pick_order_by_name(items, expect_name, registered_at, order_code)
            if matched is None and len(items) == 1:
                matched = items[0]

            if matched is None:
                pending.append({"orderCode": order_code, "reason": "order-list 未查到该单"})
                continue

            st = matched.get("status")
            if st not in self._ORDER_TERMINAL_OK:
                pending.append({
                    "orderCode": order_code,
                    "status": st,
                    "statusName": matched.get("statusName"),
                    "reason": f"尚未到终态（status={st}）",
                })
        return (not pending), pending

    def _acquire_interlock_claim(
        self, scenario: str, task_hint: str, claim_timeout: float
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """尝试占据放行凭证。已有未交接凭证则占据失败（调用方继续轮询）。

        凭证语义 =「本轮门控已通过、尚未建单」。它填的是门控 return 到
        LIMS 把订单登记成 60 之间的空窗，否则第二个任务会同样看到「无 60 +
        缓存空」而放行，两批同时建单击穿 0016(1 位)/0018(4 位) 容量。

        claim_timeout 只兜进程崩溃这类未知情况；预检抛错、建单失败这两条已知路径
        由 _submit_and_wait_orders 的 finally 主动释放，不能靠超时干等。
        """
        now = time.time()
        with self._interlock_lock:
            claim = self._interlock_claim
            if claim is not None:
                # granted_at 缺失/非法时按「无限旧」处理，让它立刻作废。
                # 不能回落成 now——那样一张时间戳损坏的凭证会永远不过期，
                # 把整站锁死，正是 claim_timeout 要防的情况。
                granted_at = claim.get("granted_at")
                age = now - (float(granted_at) if isinstance(granted_at, (int, float)) else 0.0)
                if age <= claim_timeout:
                    return False, claim
                logger.error(
                    f"[互锁门控] 放行凭证超时作废（已持有 {age:.0f}s > {claim_timeout:.0f}s，"
                    f"token={claim.get('token')}, 持有者={claim.get('task_hint')}）。"
                    f"说明拿到放行权的任务在建单前就异常退出了，请检查上一个任务的日志。"
                )
            token = uuid.uuid4().hex
            self._interlock_claim = {
                "token": token,
                "granted_at": now,
                "scenario": scenario,
                "task_hint": task_hint,
            }
            return True, self._interlock_claim

    def _release_interlock_claim(self, reason: str = "", token: Optional[str] = None) -> None:
        """释放放行凭证（未交接给批次时的兜底路径）。

        传 token 时只释放匹配的那张：万一本任务的凭证已因超时作废、且被下一个
        任务重新占据，无差别释放会把别人的凭证抹掉。
        """
        with self._interlock_lock:
            claim = self._interlock_claim
            if claim is None:
                return
            if token is not None and claim.get("token") != token:
                logger.warning(
                    f"[互锁门控] 跳过释放：当前凭证 token={claim.get('token')} "
                    f"不属于本任务（token={token}），可能是本任务凭证已超时作废并被他人重新占据"
                )
                return
            self._interlock_claim = None
        logger.info(f"[互锁门控] 已释放放行凭证 token={claim.get('token')}（{reason}）")

    def has_interlock_claim(self) -> bool:
        """凭证是否仍在手。

        语义 =「本轮门控已通过但还没建单」。将来若把门控折进
        create_orders 的排程专用变体，内部调用可据此跳过重复门控——
        否则第二次门控会看到自己那张未交接的凭证而永久等待。
        """
        with self._interlock_lock:
            return self._interlock_claim is not None

    def register_interlock_batch(
        self,
        data_list: List[Dict[str, Any]],
        scenario: str = "all",
        tag: str = "",
    ) -> None:
        """建单成功后写入互锁缓存，并接管（清除）放行凭证，完成「凭证 → 批次」交接。"""
        batch: Dict[str, str] = {}
        name_by_code: Dict[str, str] = {}
        for item in data_list or []:
            if not isinstance(item, dict):
                continue
            code = (item.get("orderCode") or "").strip()
            if code:
                batch[code] = self._normalize_order_id(item.get("orderId"))
                # orderName 是 orderId 不可用时的兜底匹配依据，见 _check_batch_all_finished
                if item.get("orderName"):
                    name_by_code[code] = item["orderName"]
        if not batch:
            logger.warning(f"[{tag}] 建单返回里没有可用 orderCode，互锁缓存未写入")
            return

        with self._interlock_lock:
            self._interlock_batch = batch
            self._interlock_batch_meta = {
                "scenario": scenario,
                "tag": tag,
                "registered_at": time.time(),
                "order_name_by_code": name_by_code,
                "order_status": {},  # 逐单最终状态，由 _record_batch_order_status 补
            }
            claim = self._interlock_claim
            self._interlock_claim = None

        logger.info(
            f"[{tag}] 互锁缓存已写入 {len(batch)} 单（scenario={scenario}）: {list(batch)}"
            + (f"，并接管放行凭证 token={claim.get('token')}" if claim else "")
        )

    def _record_batch_order_status(self, order_code: str, status: str, detail: str = "") -> None:
        """记录本批逐单最终状态，供下一轮门控 blocked 时点名失败单。"""
        with self._interlock_lock:
            if not self._interlock_batch_meta:
                return
            self._interlock_batch_meta.setdefault("order_status", {})[order_code] = {
                "status": status,
                "detail": detail,
            }

    def clear_interlock_failure(self) -> Dict[str, Any]:
        """人工处置完建单失败后清除失败标记，让门控不再因 Pause 直接阻断。

        `scheduler_continue` / `scheduler_start` 内部会自动调它，
        这里单独暴露一个入口供调试或人工在别处恢复调度后手动清除。
        """
        with self._interlock_lock:
            cleared = self._interlock_last_failure
            self._interlock_last_failure = None
        if cleared:
            logger.info(f"[互锁门控] 已清除建单失败标记: {cleared.get('reason')}")
            return {"status": "cleared", "cleared": cleared}
        return {"status": "noop", "message": "当前没有建单失败标记"}

    def _describe_failed_orders(
        self, failed: List[Dict[str, Any]], lookback_minutes: int
    ) -> str:
        """给失败单生成可操作的说明：它什么时候滑出窗口、或该把 lookback 调到多少。

        没有失败确认（ack）机制，一次失败会把工站锁到该单滑出回溯窗口为止。
        与其让人对着「blocked」发懵，不如直接告诉他等到几点、或调成多少能立刻放行。
        """
        lines = []
        oldest_age_min: Optional[float] = None
        now = datetime.now()
        for f in failed:
            req = f.get("requestTime") or ""
            expire_hint = ""
            req_dt = self._parse_lims_time(req)
            if req_dt is not None:
                age_min = (now - req_dt).total_seconds() / 60.0
                out_at = req_dt + timedelta(minutes=lookback_minutes)
                expire_hint = f"，将于 {out_at.strftime('%Y-%m-%d %H:%M')} 滑出当前回溯窗口"
                oldest_age_min = age_min if oldest_age_min is None else min(oldest_age_min, age_min)
            lines.append(
                f"{f.get('orderName')}（orderId={f.get('orderId')}, "
                f"workflow={f.get('workflowName')}, requestTime={req}）{expire_hint}"
            )

        text = "; ".join(lines)
        if oldest_age_min is not None and oldest_age_min > 0:
            text += (
                f"。若确认这些失败单已人工处置完毕，可临时把 lookback_minutes 调到 "
                f"≤ {int(oldest_age_min)} 立即放行"
            )
        return text

    def wait_orderlist_idle(
        self,
        lookback_minutes: int = 3600,
        poll_interval: float = 10.0,
        timeout: int = 86400,
        scenario: str = "all",
        workflow_name_keyword: str = "",
        page_size: int = 100,
        max_pages: int = 40,
        check_scheduler: bool = True,
        claim_timeout: float = 7200.0,
        xlsx_path: str = "",
        formulation: Optional[List[Dict[str, Any]]] = None,
        task_hint: str = "",
    ) -> Dict[str, Any]:
        """多任务互锁门控：阻塞到「本工站可以安全发起下一批配液」为止。

        放在 `create_orders` **之前**，整条链路只调一次。建单之后的完成等待由
        `_submit_and_wait_orders` 内部负责，不需要二次门控。

        放行条件（全部满足）：

        1. 调度处于 Running（Init/Stop/Error* 直接阻断，Pause 视有无失败标记而定）
        2. 窗口内没有配液单处于失败(90)
        3. 窗口内没有配液单处于执行中(60)
        4. 上一批缓存为空，或缓存内订单已全部到达 80/100（到达则清空缓存）
        5. 能占据放行凭证（上一个放行者已完成建单交接）

        Args:
            lookback_minutes: 回溯窗口（分钟），默认 3600（60 小时），覆盖多批
                1–8 瓶的长周期与隔夜任务。现场遇到旧失败单锁站时可临时调小跨过去
            poll_interval: 未空闲时的轮询间隔（秒）
            timeout: 整次门控最长等待（秒），默认 24 小时
            scenario: 场景键（all/coin/pouch/cond），**当前只影响过滤范围与统计**，
                不收窄阻断作用域——配液共用同一台硬件，收窄会放进第二批
            workflow_name_keyword: 显式覆盖过滤关键字，非空时优先于 scenario
            page_size: order-list 单页条数（映射到接口的 pageCount）
            max_pages: 翻页上限，防异常数据把门控拖死
            check_scheduler: 是否检查调度状态，调试可关
            claim_timeout: 放行凭证的兜底作废时长（秒）。必须大于后续
                stack_inquiry_window_5_free 的等待时长，因为门控排在它前面
            xlsx_path / formulation: 可选的本轮配方来源。给了就顺带做一次
                「绝对不够」预检（不做在飞批次扣减，因此不可能误拦），
                把缺料从排队 40 分钟之后提前到排队第一分钟
            task_hint: 调用方标识，仅用于日志和凭证归属

        Returns:
            {"status": "idle", "reason": ..., "scheduler_status": ..., "scenario": ...,
             "matched_keywords": [...], "lookback_minutes": ..., "running": [...],
             "failed": [...], "batch": [...], "poll_count": int, "elapsed_seconds": float}

        Raises:
            BioyondException: 阻断（blocked）或超时（timeout）
        """
        keywords = self._resolve_order_keywords(scenario, workflow_name_keyword)
        hint = task_hint or "<未命名任务>"
        start = time.time()
        poll_count = 0
        scheduler_status: Optional[str] = None

        logger.info(
            f"[互锁门控] 进入（任务={hint}, scenario={scenario}, 关键字={keywords}, "
            f"回溯 {lookback_minutes} 分钟, 超时 {timeout}s）"
        )

        # 可选的「绝对不够」预检：只拦独占也不够的情况，不做扣减所以不可能误拦
        self._gate_precheck_reagents(xlsx_path, formulation)

        def _result(status: str, reason: str, scan: Dict[str, Any]) -> Dict[str, Any]:
            with self._interlock_lock:
                batch_snapshot = list(self._interlock_batch)
            return {
                "status": status,
                "reason": reason,
                "scheduler_status": scheduler_status,
                "scenario": scenario,
                "matched_keywords": keywords,
                "lookback_minutes": lookback_minutes,
                "running": scan.get("running", []),
                "failed": scan.get("failed", []),
                "batch": batch_snapshot,
                "poll_count": poll_count,
                "elapsed_seconds": round(time.time() - start, 1),
            }

        while True:
            elapsed = time.time() - start

            # ---------- 0. 调度状态 ----------
            if check_scheduler:
                scheduler_status = self._query_scheduler_status()
                if scheduler_status in ("Init", "Stop"):
                    raise BioyondException(
                        f"[互锁门控] 调度未启动（schedulerStatus={scheduler_status}），拒绝下发新任务。"
                        f"请先执行 scheduler_start_and_auto_feeding 起调度后重试。"
                        f"（不拦的话建单会成功但没人消费，最后表现成「配液超时失败」，极难定位）"
                    )
                if scheduler_status in ("ErrorPause", "ErrorStop"):
                    raise BioyondException(
                        f"[互锁门控] 调度处于错误状态（schedulerStatus={scheduler_status}），"
                        f"需人工在现场/LIMS 清错后再重试。"
                    )
                if scheduler_status == "Pause":
                    with self._interlock_lock:
                        last_failure = self._interlock_last_failure
                    if last_failure:
                        # 这不是人工临时暂停，是建单失败时自动暂停的，等下去不会好
                        raise BioyondException(
                            f"[互锁门控] 调度处于暂停状态，且是上一批"
                            f"{last_failure.get('reason')}后自动暂停的，不会自行恢复。"
                            f"详情: {last_failure.get('detail')}。"
                            f"已建成待处置的订单: {last_failure.get('created_order_codes')}。"
                            f"请补齐试剂后执行 scheduler_continue，或撤单后重新下发。"
                        )
                    logger.info(
                        f"[互锁门控] 调度处于人工暂停（Pause，无建单失败标记），"
                        f"等待恢复中…（已等待 {elapsed:.0f}s）"
                    )
                    if elapsed > timeout:
                        raise BioyondException(
                            f"[互锁门控] 等待调度从 Pause 恢复超时（{timeout}s）"
                        )
                    poll_count += 1
                    time.sleep(poll_interval)
                    continue

            # ---------- 1. 扫 60 / 90 ----------
            scan = self._scan_workflow_statuses(keywords, lookback_minutes, page_size, max_pages)
            failed = scan["failed"]
            running = scan["running"]

            if failed:
                counts = self._count_by_workflow(failed)
                detail = self._describe_failed_orders(failed, lookback_minutes)
                logger.error(f"[互锁门控] ❌ 窗口内存在失败配液单，分场景计数: {counts}")
                raise BioyondException(
                    f"[互锁门控] 窗口内存在 {len(failed)} 条失败配液单，禁止下发新任务。"
                    f"失败单: {detail}"
                )

            if running:
                counts = self._count_by_workflow(running)
                if elapsed > timeout:
                    raise BioyondException(
                        f"[互锁门控] 等待配液空闲超时（{timeout}s），"
                        f"仍有 {len(running)} 条执行中: {counts}"
                    )
                logger.info(
                    f"[互锁门控] 仍有 {len(running)} 条配液单执行中，分场景计数: {counts}，"
                    f"{poll_interval}s 后重试…（已等待 {elapsed:.0f}s）"
                )
                poll_count += 1
                time.sleep(poll_interval)
                continue

            # ---------- 2. 核对上一批缓存 ----------
            all_done, pending = self._check_batch_all_finished()
            if not all_done:
                if elapsed > timeout:
                    raise BioyondException(
                        f"[互锁门控] 等待上一批订单终态超时（{timeout}s），未完成: {pending}"
                    )
                logger.info(
                    f"[互锁门控] 上一批仍有 {len(pending)} 单未到终态: {pending}，"
                    f"{poll_interval}s 后重试…（已等待 {elapsed:.0f}s）"
                )
                poll_count += 1
                time.sleep(poll_interval)
                continue

            with self._interlock_lock:
                cleared_batch = list(self._interlock_batch)
                self._interlock_batch = {}
                self._interlock_batch_meta = {}
            if cleared_batch:
                logger.info(f"[互锁门控] 上一批 {len(cleared_batch)} 单已全部完成，清空缓存: {cleared_batch}")

            # ---------- 3. 占据放行凭证 ----------
            acquired, holder = self._acquire_interlock_claim(scenario, hint, claim_timeout)
            if not acquired:
                if elapsed > timeout:
                    raise BioyondException(
                        f"[互锁门控] 等待上一个放行者建单超时（{timeout}s），"
                        f"当前凭证持有者={holder.get('task_hint')}"
                    )
                logger.info(
                    f"[互锁门控] 上一个放行者（{holder.get('task_hint')}）尚未完成建单，"
                    f"{poll_interval}s 后重试…（已等待 {elapsed:.0f}s）"
                )
                poll_count += 1
                time.sleep(poll_interval)
                continue

            logger.info(
                f"[互锁门控] ✅ 放行（任务={hint}, 轮询 {poll_count} 次, "
                f"耗时 {time.time() - start:.1f}s, 凭证 token={holder.get('token')}）"
            )
            return _result("idle", "无执行中/失败配液单，上一批已完成，凭证已占据", scan)

    def _gate_precheck_reagents(
        self, xlsx_path: str = "", formulation: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """门控里的兜底预检：只拦「绝对不够」（独占也不够）。

        主机制是提交前的 `precheck_reagents_total`（那时没有在飞批次，结论无歧义）。
        这层只覆盖「有人绕过提交 skill 直接下发任务」的情况。

        **刻意不做在飞批次扣减**：入口看到的库存不含在飞批次的消耗，扣多少取决于
        LIMS 是建单即锁还是消耗才扣，猜错方向就会误拦——把本来能跑的批次挡住，
        比漏拦更难接受。这里只拦「就算独占也不够」，因此不可能误拦。
        """
        if not xlsx_path and not formulation:
            return
        try:
            report = self.precheck_reagents_total(
                formulations=[formulation] if formulation else None,
                xlsx_paths=[xlsx_path] if xlsx_path else None,
                raise_on_shortage=False,
            )
        except Exception as e:
            logger.warning(f"[互锁门控] 兜底预检执行异常，跳过（不阻断）: {e}")
            return

        if not report.get("ok"):
            raise BioyondException(
                f"[互锁门控] 本轮配方所需试剂在库余量绝对不足（就算独占整台设备也不够），"
                f"提前拦下、一单未建。缺口: {'; '.join(report.get('shortages') or [])}"
            )

    def _submit_and_wait_orders(
        self,
        orders: List[Dict[str, Any]],
        tag: str = "create_orders",
        batch_id: str = "",
    ) -> Dict[str, Any]:
        """公共流程的对外入口：只负责保证放行凭证一定会被释放。

        本体在 `_submit_and_wait_orders_impl`。这里包一层 try/finally 是因为
        本体有两条在 `register_interlock_batch` **之前**就抛出的路径——试剂预检
        不通过、建单未全部成功。这两条路径下凭证没人接管，只能干等
        claim_timeout（默认 2 小时），等于一次缺料把整站锁 2 小时，
        比它要解决的「半批订单」更难受。claim_timeout 只用来兜进程崩溃这类
        未知情况，已知的异常路径必须在这里主动释放。
        """
        with self._interlock_lock:
            claim_token = (self._interlock_claim or {}).get("token")

        try:
            return self._submit_and_wait_orders_impl(orders, tag=tag, batch_id=batch_id)
        finally:
            # 建单成功时 register_interlock_batch 已把凭证接管清除，这里查不到匹配的 token；
            # 只有异常路径才会真的释放。
            if claim_token:
                self._release_interlock_claim(
                    reason=f"{tag} 未完成建单交接，异常路径兜底释放", token=claim_token
                )

    def _submit_and_wait_orders_impl(
        self,
        orders: List[Dict[str, Any]],
        tag: str = "create_orders",
        batch_id: str = "",
    ) -> Dict[str, Any]:
        """
        公共流程：提交 orders → 等待完成 → 计算质量比 → 提取分液瓶板 → 返回结果。
        由 create_orders / create_orders_formulation 调用。

        batch_id 由调用方传入（约定 = Excel 文件名 / 入参 batch_id），随 final_result 透出，
        供下游通过 UniLab output handle 引用。
        """
        logger.info(f"[{tag}] 即将提交 {len(orders)} 个订单")
        self._preflight_check_reagents(orders, tag)
        response = self._post_lims("/api/lims/order/orders", orders)
        logger.info(f"[{tag}] 接口返回: {response}")

        # ========== 逐条判定建单结果 ==========
        # 判据与 conductivity_test_inline 一致：errorMessage 为空 + orderCode 非空才算建成。
        # LIMS 按订单全有或全无：某单试剂不够时它那条 orderCode=null、errorMessage 写明原因，
        # 其余单照常建出来。原先只按 orderCode 真值过滤，失败条目被列表推导静默丢掉，
        # 请求 8 单只建成 4 单也照样往下走，最后还报 all_completed。
        data_list = response.get("data") or []
        if not isinstance(data_list, list):
            data_list = []

        created_entries: List[Dict[str, Any]] = []
        failed_entries: List[Dict[str, Any]] = []
        for item in data_list:
            if not isinstance(item, dict):
                continue
            err_msg = (item.get("errorMessage") or "").strip()
            code = (item.get("orderCode") or "").strip()
            if code and not err_msg:
                created_entries.append(item)
            else:
                failed_entries.append(item)

        order_codes = [(item.get("orderCode") or "").strip() for item in created_entries]

        # 数量也要对账：LIMS 可能少返条目，此时既没有 orderCode 也没有 errorMessage
        if failed_entries or len(created_entries) != len(orders):
            # orderName 是入参带过来的，失败条目也有，用它定位是哪一单
            fail_desc = "; ".join(
                f"{e.get('orderName') or '<无名>'}: "
                f"{(e.get('errorMessage') or '').strip() or '未返回 orderCode'}"
                for e in failed_entries
            ) or f"LIMS 只返回 {len(data_list)} 条条目（请求 {len(orders)} 单）"

            logger.error(
                f"[{tag}] ❌ 建单未全部成功: 请求 {len(orders)} 单，建成 {len(created_entries)} 单，"
                f"失败 {len(failed_entries)} 单 → {fail_desc}"
            )

            # 先暂停调度：已建成的单还在 LIMS 队列里，暂停后它们不会继续执行，
            # 给人工留出补试剂或撤单的处置窗口。暂停失败不能盖掉建单失败本身，只记日志。
            try:
                pause_resp = self.scheduler_pause()
                logger.warning(f"[{tag}] 已暂停调度，等待人工处置: {pause_resp}")
                paused_hint = (
                    "调度已暂停，请补齐试剂后调 scheduler_continue 继续，或撤单后重新下发。"
                )
                # 记下「这次 Pause 是自动暂停的」。否则下一个任务的门控看到 Pause
                # 会当成人工临时暂停一路轮询到 24h 超时，只报个「超时」把真实原因埋掉。
                with self._interlock_lock:
                    self._interlock_last_failure = {
                        "reason": "建单未全部成功",
                        "detail": fail_desc,
                        "created_order_codes": order_codes,
                        "paused_at": time.time(),
                        "tag": tag,
                    }
            except Exception as e:
                logger.error(f"[{tag}] ⚠️ 暂停调度失败，请立即人工干预: {e}")
                paused_hint = f"调度暂停失败（{e}），已建成的订单可能仍在执行，请立即人工停机处置。"

            # 必须抛：一是调度已暂停，已建成的单永远不会 finish，继续等只会挂到超时；
            # 二是抛异常才会让 edge 把 job_status=failed + 本消息推到云端提醒人工。
            # 订单尚未执行，也就还没产出瓶板，跳过后处理不会让资源树漂移。
            raise BioyondException(
                f"[{tag}] 建单未全部成功（请求 {len(orders)} 单 / 建成 {len(created_entries)} 单）。"
                f"失败原因: {fail_desc}。"
                f"已建成待处置的订单: {order_codes or '无'}。{paused_hint}"
            )

        logger.info(f"[{tag}] 等待 {len(order_codes)} 个订单完成: {order_codes}")

        # 建单数量已对账，写入互锁缓存并接管放行凭证（完成「凭证 → 批次」交接）。
        # 必须在等待完成之前写：本批还在跑的这几十分钟里，其它任务的门控要靠
        # 这份缓存 + order-list 的 60 才知道工站正忙。
        try:
            self.register_interlock_batch(created_entries, scenario="all", tag=tag)
        except Exception as e:
            logger.warning(f"[{tag}] 写入互锁缓存失败（不影响本批执行）: {e}")

        # 重置配液/分液进度统计（供前端轮询 data_*_completion_percentage 属性）
        try:
            self._reset_progress_tracking(orders, order_codes, data_list)
        except Exception as e:
            logger.warning(f"[{tag}] 重置进度统计失败（不影响下单）: {e}")

        # orderCode → 建单条目（含 orderId / usedMaterials），供 wait 双字段校验与后处理降级
        # 键取自 created_entries 并同样 strip，保证和 order_codes 对得上
        create_entry_by_code = {
            (item.get("orderCode") or "").strip(): item
            for item in created_entries
        }

        # ========== 等待所有订单完成 ==========
        all_reports = []
        for idx, order_code in enumerate(order_codes, 1):
            create_entry = create_entry_by_code.get(order_code) or {}
            expected_oid = self._normalize_order_id(create_entry.get("orderId"))
            logger.info(
                f"[{tag}] 等待第 {idx}/{len(order_codes)} 个订单: {order_code} "
                f"(orderId={expected_oid or '<无>'})"
            )
            result = self.wait_for_order_finish(
                order_code, expected_order_id=expected_oid or None
            )
            if result.get("status") == "success":
                report = result.get("report", {})
                self._enrich_report_materials_from_create(report, create_entry)
                all_reports.append(report)
                self._record_batch_order_status(order_code, "success")
                logger.info(f"[{tag}] ✓ 订单 {order_code} 完成")
            else:
                logger.warning(f"订单 {order_code} 状态异常: {result.get('status')}")
                all_reports.append({
                    "orderCode": order_code,
                    "orderId": expected_oid,
                    "status": result.get("status"),
                    "error": result.get("message", "未知错误"),
                })
                # 记进批次 meta，下一轮门控 blocked 时能点名是哪一单挂的，
                # 把定位指回配液侧（否则报错只出现在下游转运节点上）
                self._record_batch_order_status(
                    order_code, result.get("status") or "failed",
                    result.get("message", "未知错误"),
                )

        # ========== timeout 兜底：隔夜暂停等场景下，迟到的 finish 报文可能已被缓存 ==========
        # wait 超时后报文才到达 `_order_finish_reports`，此处再捞一次，把 timeout 升级为成功。
        recovered = 0
        for i, report in enumerate(all_reports):
            # 仅兜底占位行（wait 失败时写入的 {orderCode, status, error}）
            if "error" not in report:
                continue
            order_code = report.get("orderCode") or ""
            if not order_code:
                continue
            expected_oid = self._normalize_order_id(
                (create_entry_by_code.get(order_code) or {}).get("orderId")
            )
            with self._order_finish_lock:
                cached = self._order_finish_reports.pop(order_code, None)
            if not cached:
                continue
            if not self._report_matches_expected(order_code, expected_oid, cached):
                logger.warning(
                    f"[{tag}] timeout 兜底拒绝撞号报文: orderCode={order_code}, "
                    f"期望orderId={expected_oid}, 报文orderId={cached.get('orderId')}"
                )
                continue
            classified = self._classify_finish_report(
                order_code, cached, expected_order_id=expected_oid or None
            )
            if classified.get("status") == "success":
                recovered_report = classified.get("report", cached)
                self._enrich_report_materials_from_create(
                    recovered_report, create_entry_by_code.get(order_code)
                )
                all_reports[i] = recovered_report
                recovered += 1
                logger.info(
                    f"[{tag}] ✓ timeout 兜底恢复订单 {order_code}（命中迟到缓存报文）"
                )
            else:
                logger.warning(
                    f"[{tag}] timeout 兜底命中但状态非 success: "
                    f"orderCode={order_code}, status={classified.get('status')}"
                )
        if recovered:
            logger.info(f"[{tag}] timeout 兜底共恢复 {recovered} 个订单")

        # ========== timeout 兜底（LIMS 核对）==========
        # 本地缓存里也没有迟到报文时（如隔夜暂停、进程重启导致推送彻底丢失），
        # 直接查 order-list 确认 LIMS 是否已完成；完成则用建单 usedMaterials 合成 report，
        # 让后续 prep/vial/plate 提取照常补全条码与类型。
        lims_recovered = 0
        for i, report in enumerate(all_reports):
            if "error" not in report:
                continue
            oc = report.get("orderCode") or ""
            synth = self._recover_timeout_order_report(oc, create_entry_by_code.get(oc))
            if synth:
                all_reports[i] = synth
                lims_recovered += 1
        if lims_recovered:
            logger.info(f"[{tag}] LIMS 核对兜底共恢复 {lims_recovered} 个订单")

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
                        "mass_tolerance": mass_ratios.get("mass_tolerance", {}),
                        "total_mass_tolerance": mass_ratios.get("total_mass_tolerance", None),
                    })
                    logger.info(f"✓ 已计算订单 {order_code} 的试剂质量比和允差")
                except Exception as e:
                    logger.error(f"计算订单 {order_code} 质量比失败: {e}")
                    all_mass_ratios.append({
                        "orderCode": order_code,
                        "orderName": report.get("orderName", "N/A"),
                        "real_mass_ratio": {},
                        "target_mass_ratio": {},
                        "mass_tolerance": {},
                        "total_mass_tolerance": None,
                        "error": str(e),
                    })
            else:
                all_mass_ratios.append({
                    "orderCode": order_code,
                    "orderName": report.get("orderName", "N/A"),
                    "real_mass_ratio": {},
                    "target_mass_ratio": {},
                    "mass_tolerance": {},
                    "total_mass_tolerance": None,
                    "error": "订单未成功完成",
                })

        logger.info(f"[{tag}] 质量比计算完成")

        # ========== 提取分液瓶板信息 + 创建资源树对象 ==========
        # 关键设计（2026-06-04 调整）：
        # 物理 plate 是按 materialId 唯一的；同一块物理 plate 可能在多个 order 的
        # usedMaterials 里都被列出（例如 2 个配液 order 把瓶子分别装到同一物理板的不同孔位）。
        # 因此 all_vial_plates 必须按 materialId 去重，每个 unique plate 用 order_refs 字段
        # 收集所有引用过它的 (orderId, orderCode) 对（保留供追溯）。瓶→单归属现由
        # vial_bottle_positions（detailMaterialId→orderCode 权威映射）承担，不再靠 associateId。
        plates_by_material: Dict[str, Dict[str, Any]] = {}
        for report in all_reports:
            plate_list = self._extract_vial_plate_from_report(report)
            for vial_plate_info in plate_list:
                material_id = vial_plate_info.get("materialId") or ""
                if not material_id:
                    logger.warning(
                        f"[资源树] ⚠️ 跳过 materialId 为空的 plate_info: {vial_plate_info}"
                    )
                    continue

                ref_entry = {
                    "orderId": vial_plate_info.get("orderId") or "",
                    "orderCode": vial_plate_info.get("orderCode") or "",
                }

                if material_id in plates_by_material:
                    existing = plates_by_material[material_id]
                    if ref_entry not in existing["order_refs"]:
                        existing["order_refs"].append(ref_entry)
                    logger.info(
                        f"[资源树] ℹ️ 瓶板已存在，合并 order_ref: materialId={material_id[:20]}..., "
                        f"+orderCode={ref_entry['orderCode']} (共用同一物理瓶板)"
                    )
                    continue

                # 首次出现，创建去重后的条目；保留 first 的 locationId/typeName/barCode
                merged = {
                    "materialId": material_id,
                    "locationId": vial_plate_info.get("locationId") or "",
                    "orderCode": ref_entry["orderCode"],  # = 第一次引用的 orderCode（向后兼容）
                    "orderId": ref_entry["orderId"],      # 同上
                    "typeName": vial_plate_info.get("typeName") or "",
                    "barCode": vial_plate_info.get("barCode") or "",
                    # 源坐标必须一起带过来：漏了会让下游 _find_plate_xyz / 321 转运
                    # 拿不到坐标，退化成按槽位标签硬算（C03→(3,3,1)），指到不存在的库位
                    "source_x": vial_plate_info.get("source_x", 1),
                    "source_y": vial_plate_info.get("source_y", 1),
                    "source_z": vial_plate_info.get("source_z", 1),
                    "source_found": bool(vial_plate_info.get("source_found")),
                    "batch_id": batch_id,
                    "order_refs": [ref_entry],            # 完整列表，保留供追溯（归属改由 vial_bottle_positions 承担）
                }
                plates_by_material[material_id] = merged

                try:
                    self._create_vial_plate_resource(merged)
                    logger.info(
                        f"[资源树] ✅ 瓶板资源创建成功: orderCode={ref_entry['orderCode']}, "
                        f"materialId={material_id[:20]}..."
                    )
                except Exception as e:
                    logger.error(
                        f"[资源树] 创建失败: orderCode={ref_entry['orderCode']}, 错误={e}"
                    )

        all_vial_plates: List[Dict[str, Any]] = list(plates_by_material.values())

        logger.info(
            f"[{tag}] 跨 {len(all_reports)} 个订单去重后得到 {len(all_vial_plates)} 块物理瓶板 "
            f"(每块板的 order_refs 长度: "
            f"{[len(p['order_refs']) for p in all_vial_plates]})"
        )

        # ========== 提取配液瓶 + 分液瓶信息（用于 CSV 导出）==========
        # 配液瓶：逐单按 typeName 提取（已去掉库位白名单，兼容多配液瓶板）
        all_prep_bottles = []
        for report in all_reports:
            try:
                prep_info = self._extract_prep_bottle_from_report(report)
                all_prep_bottles.append(prep_info)
            except Exception as e:
                logger.error(f"[提取配液瓶] 异常: orderCode={report.get('orderCode')}, 错误={e}")
                all_prep_bottles.append(None)

        # 分液瓶：主路径 = 逐单按 typeName 扫描单瓶物料（typeName=5ml/20ml分液瓶）。
        # 单瓶物料始终留在各单报文里，且每单报文天然只含自己的瓶，即便分液瓶板
        # 后续被电导站消耗、板 detail 清空也不受影响。
        # 板级路径（板 detail + associateId）作为兜底，用于报文里查不到单瓶、
        # 但板仍在库且 detail 完整的场景。
        order_codes_for_vial = [
            r.get("orderCode") for r in all_reports if r.get("orderCode")
        ]

        # 权威 materialId → orderCode（来自 create 响应 usedMaterials，不依赖 associateId）。
        # 供板级兜底提取 + 分液瓶孔位映射（vial_bottle_positions）复用。
        mat2order = self._build_vial_bottle_mat2order(data_list)

        all_vial_bottles: List[List[Dict]] = []
        need_plate_fallback = False
        for report in all_reports:
            oc = report.get("orderCode") or ""
            vial_list: List[Dict] = []
            if "error" not in report:
                try:
                    vial_list = self._extract_vial_bottles_from_report(report)
                except Exception as e:
                    logger.error(f"[提取分液瓶] 逐单异常: orderCode={oc}, 错误={e}")
                    vial_list = []
            if not vial_list and "error" not in report:
                need_plate_fallback = True
            all_vial_bottles.append(vial_list)

        # 兜底：仍有订单没提到分液瓶时，尝试板级路径补齐
        if need_plate_fallback and all_vial_plates:
            try:
                vials_by_order = self._extract_vial_bottles_from_plates(
                    all_vial_plates, order_codes_for_vial, mat2order=mat2order
                )
            except Exception as e:
                logger.error(f"[提取分液瓶-板级] 兜底异常: {e}")
                vials_by_order = {}
            for i, report in enumerate(all_reports):
                oc = report.get("orderCode") or ""
                if not all_vial_bottles[i] and oc and vials_by_order.get(oc):
                    all_vial_bottles[i] = vials_by_order[oc]
                    logger.info(
                        f"[提取分液瓶] 板级兜底补齐: orderCode={oc}, "
                        f"数量={len(all_vial_bottles[i])}"
                    )

        logger.info(
            f"[{tag}] 配液瓶提取完成: {sum(1 for p in all_prep_bottles if p)} 个, "
            f"分液瓶提取完成: {sum(len(v) for v in all_vial_bottles if isinstance(v, list))} 个"
        )

        # ========== 将条码 + 类型附加到 mass_ratios 中（给扣电组装站 / 电导 inline CSV 使用）==========
        # 2026-06-26：额外附加 prep_bottle_type / vial_bottle_types，使电导 inline 仅靠
        # 单个 mass_ratios handle 即可生成"配液+电导"合并 CSV（含配液瓶类型 / 分液瓶类型）。
        for idx in range(len(all_mass_ratios)):
            if idx < len(all_prep_bottles) and all_prep_bottles[idx]:
                all_mass_ratios[idx]["prep_bottle_barcode"] = all_prep_bottles[idx].get("barCode", "")
                all_mass_ratios[idx]["prep_bottle_type"] = all_prep_bottles[idx].get("typeName", "")
            else:
                all_mass_ratios[idx]["prep_bottle_barcode"] = ""
                all_mass_ratios[idx]["prep_bottle_type"] = ""
                
            if idx < len(all_vial_bottles):
                vials = all_vial_bottles[idx]
                if len(vials) == 0:
                    all_mass_ratios[idx]["vial_bottle_barcodes"] = ""
                    all_mass_ratios[idx]["vial_bottle_types"] = ""
                elif len(vials) == 1:
                    all_mass_ratios[idx]["vial_bottle_barcodes"] = vials[0].get("barCode", "")
                    all_mass_ratios[idx]["vial_bottle_types"] = vials[0].get("typeName", "")
                else:
                    all_mass_ratios[idx]["vial_bottle_barcodes"] = json.dumps([v.get("barCode", "") for v in vials], ensure_ascii=False)
                    all_mass_ratios[idx]["vial_bottle_types"] = json.dumps([v.get("typeName", "") for v in vials], ensure_ascii=False)
            else:
                all_mass_ratios[idx]["vial_bottle_barcodes"] = ""
                all_mass_ratios[idx]["vial_bottle_types"] = ""

        # ========== 构建分液瓶孔位映射（vial_bottle_positions）==========
        # 板状态干净时查每块板 detail，按 detailMaterialId 命中权威 mat2order 归属订单，
        # 过滤空占位孔/非本批瓶。供下游电导 conductivity_test_inline 直接组装 entry
        # （不再靠 associateId），并注入 mass_ratios 激活导出位置回退匹配。
        all_vial_bottle_positions: List[Dict[str, Any]] = []
        try:
            all_vial_bottle_positions = self._build_vial_bottle_positions(all_vial_plates, mat2order)
            # 按 orderCode 分组注入到对应 mass_ratios 项（激活 _match_formula_by_position 位置回退）
            pos_by_order: Dict[str, List[Dict[str, Any]]] = {}
            for pos in all_vial_bottle_positions:
                oc = pos.get("orderCode") or ""
                if oc:
                    pos_by_order.setdefault(oc, []).append(pos)
            for mr in all_mass_ratios:
                if isinstance(mr, dict):
                    mr["vial_bottle_positions"] = pos_by_order.get(mr.get("orderCode") or "", [])
        except Exception as e:
            logger.warning(f"[{tag}] 构建分液瓶孔位映射失败（不影响下单/导出）: {e}")

        # ========== 提取各类瓶板的源坐标（用于 321/32 任务 handles 传参）==========
        def _find_plate_xyz(plates, type_keyword):
            """取该板型在自动堆栈-左的源坐标；未命中则返回占位 (1,1,1) 并告警。

            占位值不可直接用于建单：A01 上可能是另一块板。手动调 321/32 时若发现
            日志里是占位值，说明配液刚结束板还没进自动堆栈-左，应改用 *_auto
            版本（转运前会实时查库定位）。
            """
            for p in plates:
                if p and type_keyword in p.get("typeName", ""):
                    if not p.get("source_found"):
                        logger.warning(
                            f"[{tag}] ⚠️ {type_keyword} 未在自动堆栈-左命中库位，"
                            f"源坐标输出为占位值 (1,1,1)，不可直接用于手动转运建单"
                        )
                    return p.get("source_x", 1), p.get("source_y", 1), p.get("source_z", 1)
            return 1, 1, 1

        vial_321_x, vial_321_y, vial_321_z = _find_plate_xyz(all_vial_plates, "5ml分液瓶板")
        vial_32_x, vial_32_y, vial_32_z = _find_plate_xyz(all_vial_plates, "20ml分液瓶板")
        logger.info(
            f"[{tag}] 3-2-1源坐标（5ml）: ({vial_321_x},{vial_321_y},{vial_321_z}), "
            f"3-2源坐标（20ml）: ({vial_32_x},{vial_32_y},{vial_32_z})"
        )

        # ========== 构造最终结果 ==========
        final_result = {
            # 走到这里说明建单数量已和请求对齐（不齐的在上面就暂停调度并抛了）。
            # total_orders 用请求数而非建成数：拿建成数当分母，4/8 会显示成 4/4。
            "status": "all_completed",
            "total_orders": len(orders),
            "bottle_count": len(order_codes),
            "reports": all_reports,
            "mass_ratios": all_mass_ratios,
            "vial_plates": all_vial_plates,
            "prep_bottles": all_prep_bottles,
            "vial_bottles": all_vial_bottles,
            "vial_bottle_positions": all_vial_bottle_positions,
            "original_response": response,
            "vial_321_source_pos": {"x": vial_321_x, "y": vial_321_y, "z": vial_321_z},
            "vial_32_source_pos": {"x": vial_32_x, "y": vial_32_y, "z": vial_32_z},
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
    def create_orders(self, xlsx_path: str, csv_export_path: str = "") -> Dict[str, Any]:
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

        # 物料列：所有以 (g) 结尾，但排除"总质量(g)"等聚合列
        # （totalMass 自动按物料质量求和，不能把"总质量"当物料发给 LIMS，否则 LIMS 报"物料总质量不可用"）
        _NON_MATERIAL_NAMES = {"总质量", "totalMass", "TotalMass", "total_mass"}
        material_cols = [
            c for c in df.columns
            if isinstance(c, str) and c.endswith("(g)")
            and c.replace("(g)", "").strip() not in _NON_MATERIAL_NAMES
        ]
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

        result = self._submit_and_wait_orders(orders, tag="create_orders", batch_id=batch_id)

        # ========== CSV 导出 ==========
        if csv_export_path:
            try:
                csv_file = self._export_order_csv(result, csv_export_path)
                result["csv_file"] = csv_file
            except Exception as e:
                logger.error(f"[create_orders] CSV 导出失败: {e}")

        return result

    def create_orders_formulation(
        self,
        formulation: List[Dict[str, Any]],
        batch_id: str = "",
        order_names: List[str] = [],
        bottle_type: str = "配液小瓶",
        mix_time: List[int] = [],
        coin_cell_volume: float = 0.0,
        pouch_cell_volume: float = 0.0,
        conductivity_volume: float = 0.0,
        conductivity_bottle_count: int = 0,
        csv_export_path: str = "",
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
            order_names: 配方ID/订单编号列表，与 formulation 一一对应。
                用于填写 DoE 撒点编号等自定义标识，便于后续扣电组装、测试环节追溯。
                优先级：order_names > formulation 内的 order_name > 自动生成({batch_id}_order_{序号})
            bottle_type: 配液瓶类型，默认 "配液小瓶"
            mix_time: 混匀时间列表(秒)，与 formulation 一一对应，不足则补 0
            coin_cell_volume: 纽扣电池组装分液体积
            pouch_cell_volume: 软包电池注液组装分液体积
            conductivity_volume: 电导率测试分液体积
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
            if idx < len(order_names) and order_names[idx]:
                order_name = str(order_names[idx])
            else:
                order_name = str(item.get("order_name", f"{batch_id}_order_{idx + 1}"))

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

            if isinstance(mix_time, (int, float)):
                raw_mix_time = mix_time
            else:
                raw_mix_time = mix_time[idx] if idx < len(mix_time) else None
            try:
                item_mix_time = int(raw_mix_time) if raw_mix_time not in (None, "", "null") else 0
            except (ValueError, TypeError):
                item_mix_time = 0
            logger.info(f"[create_orders_formulation] 第 {idx + 1} 个配方: orderName={order_name}, "
                        f"coinCellVolume={coin_cell_volume}, pouchCellVolume={pouch_cell_volume}, "
                        f"conductivityVolume={conductivity_volume}, totalMass={total_mass}, "
                        f"material_count={len(mats)}")

            orders.append({
                "batchId": batch_id,
                "orderName": order_name,
                "createTime": create_time,
                "bottleType": bottle_type,
                "mixTime": item_mix_time,
                "loadSheddingInfo": coin_cell_volume,
                "pouchCellInfo": pouch_cell_volume,
                "conductivityInfo": conductivity_volume,
                "conductivityBottleCount": conductivity_bottle_count,
                "materialInfos": mats,
                "totalMass": round(total_mass, 4),
            })

        if not orders:
            logger.error("[create_orders_formulation] 没有有效的订单可提交")
            return {"status": "error", "message": "没有有效配方数据"}

        result = self._submit_and_wait_orders(orders, tag="create_orders_formulation", batch_id=batch_id)

        # ========== CSV 导出 ==========
        if csv_export_path:
            try:
                csv_file = self._export_order_csv(result, csv_export_path)
                result["csv_file"] = csv_file
            except Exception as e:
                logger.error(f"[create_orders_formulation] CSV 导出失败: {e}")

        return result

    # -------------------- 2.37 5号站新建实验（手动 Excel 入口） --------------------
    def _validate_plate_barcode(self, barcode: str) -> Dict[str, Any]:
        """
        在 LIMS 物料系统中按 barCode 查找分液板物料，命中返回物料字典，未命中抛 BioyondException。

        - 先用 typeMode=1（样品）+ filter=barcode 查询；未命中再用 typeMode=2（试剂）兜底。
        - 实例级缓存：同一次 Excel 提交中多行复用同一板条码时只查 1 次 LIMS。

        Args:
            barcode: 分液板条码（必须与 LIMS 物料系统中的 barCode 严格相等）

        Returns:
            dict: 物料完整记录（含 id / name / typeName / barCode 等）

        Raises:
            BioyondException: 物料系统中不存在该 barCode
        """
        if not barcode:
            raise BioyondException("板条码不能为空")

        # 懒初始化缓存
        if not hasattr(self, "_validated_plate_barcode_cache"):
            self._validated_plate_barcode_cache: Dict[str, Dict[str, Any]] = {}

        if barcode in self._validated_plate_barcode_cache:
            logger.debug(f"[校验条码] 缓存命中: {barcode}")
            return self._validated_plate_barcode_cache[barcode]

        # typeMode: 1=样品, 2=试剂, 0=耗材；分液板通常在 1，兜底查 2
        for type_mode in (1, 2):
            query = {"typeMode": type_mode, "filter": barcode, "includeDetail": False}
            resp = self._post_lims("/api/lims/storage/stock-material", query)
            if not isinstance(resp, dict) or resp.get("code") != 1:
                logger.warning(
                    f"[校验条码] stock-material 查询返回异常: typeMode={type_mode}, "
                    f"barcode={barcode}, resp={resp}"
                )
                continue

            for mat in resp.get("data", []) or []:
                if mat.get("barCode") == barcode:
                    logger.info(
                        f"[校验条码] ✅ 命中: barCode={barcode}, typeName={mat.get('typeName')}, "
                        f"name={mat.get('name')}, typeMode={type_mode}"
                    )
                    self._validated_plate_barcode_cache[barcode] = mat
                    return mat

        raise BioyondException(
            f"板条码 {barcode} 未在物料系统中找到，请先在 LIMS 建立对应的分液板物料"
        )

    def conductivity_test_external(
        self,
        xlsx_path: str,
        validate_barcode: bool = True,
        start_scheduler: bool = True,
        wait_for_finish: bool = True,
        wait_timeout_seconds: int = 36000,
        csv_export_path: str = "",
    ) -> Dict[str, Any]:
        """
        2.37 5号电导工作站手动新建实验（外部进样 / Excel 入口）。

        外部进样：操作员手动填 Excel（板条码 + 瓶位 + 温控点）后提交，
        不接配液 handle。手动电导路径上没有"启动调度+上料"的前置流程
        （那是配液专用的 scheduler_start_and_auto_feeding），因此本函数会在
        提交订单前主动调用 scheduler_start，确保订单进入队列后立即被调度消费。

        2026-06-26 调整：POST 建单后不再立即 return，而是：
        - wait_for_finish=True（默认）：逐单阻塞等 /report/order_finish 推送，
          全部完成后用 /api/lims/order/conductivity-order-result 按 orderId 拉取
          电导率/温度结果，写入 External_conductivity_*.csv 并放进 return。

        Excel 列（中文 / 英文均支持）：
            算法批次ID / batchId
            配方ID / orderId / orderName
            创建日期 / createTime
            板BarCode / plateBarCode
            内部瓶位置X / bottleX
            内部瓶位置Y / bottleY
            温控点 / temperaturePoint

        Args:
            xlsx_path: Excel 模板路径
            validate_barcode: 是否在提交前对 plateBarCode 做物料系统强校验，默认 True
            start_scheduler: 提交订单前是否先启动调度，默认 True。
                调度若已 Running，再次启动是幂等的；置 False 可在外部控制时机。
            wait_for_finish: 是否阻塞等所有电导单完成并拉取结果，默认 True。
                False 时回退到 fire-and-forget，POST 完即返回。
            wait_timeout_seconds: 单个电导单 wait 超时秒数，默认 36000s = 10h。
            csv_export_path: 电导结果 CSV 导出目录，为空则不导出。

        Returns:
            {
                "status": "submitted" | "partial" | "error" | "all_completed" | "partial_completed",
                "total_entries": int,
                "validated_barcodes": List[str],
                "response": <LIMS 原始返回>,
                "scheduler_start_response": <可选，调度启动返回>,
                # 仅 wait_for_finish=True 时有：
                "conductivity_results": List[Dict],
                "completion_summary": {"success": int, "timeout": int, ...},
                "csv_file": <可选，External_conductivity_*.csv 路径>,
            }

        Raises:
            FileNotFoundError: Excel 文件不存在
            ValueError: Excel 缺少必要列或没有有效行
            BioyondException: validate_barcode=True 时板条码在物料系统中找不到
        """
        path = Path(xlsx_path) if xlsx_path else None
        if path is None or not path.exists():
            raise FileNotFoundError(f"未找到电导实验 Excel: {xlsx_path}")

        logger.info(f"[conductivity_test_external] 读取 Excel: {path}")
        try:
            df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"读取 Excel 失败: {e}")
        logger.info(
            f"[conductivity_test_external] 读取成功，行数={len(df)}, 列={list(df.columns)}"
        )

        def _pick(col_names: List[str]) -> Optional[str]:
            for c in col_names:
                if c in df.columns:
                    return c
            return None

        col_batch = _pick(["算法批次ID", "batchId"])
        col_order = _pick(["配方ID", "orderId", "orderName"])
        col_ctime = _pick(["创建日期", "createTime"])
        col_code = _pick(["板BarCode", "plateBarCode"])
        col_x = _pick(["内部瓶位置X", "bottleX"])
        col_y = _pick(["内部瓶位置Y", "bottleY"])
        col_temp = _pick(["温控点", "temperaturePoint"])

        required_map = {
            "batchId": col_batch,
            "orderId": col_order,
            "plateBarCode": col_code,
            "bottleX": col_x,
            "bottleY": col_y,
            "temperaturePoint": col_temp,
        }
        missing = [k for k, v in required_map.items() if not v]
        if missing:
            raise ValueError(
                f"Excel 缺少必要列: {missing}，请检查表头（支持中文或英文列名）"
            )

        def _to_ymd_slash(v) -> str:
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                ts = datetime.now()
            else:
                try:
                    ts = pd.to_datetime(v)
                except Exception:
                    ts = datetime.now()
            return (
                f"{ts.year}/{ts.month}/{ts.day} "
                f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}"
            )

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

        entries: List[Dict[str, Any]] = []
        for idx, row in df.iterrows():
            plate_barcode = _as_str(row[col_code])
            if not plate_barcode:
                logger.warning(
                    f"[conductivity_test_external] 第 {idx + 1} 行 plateBarCode 为空，跳过"
                )
                continue

            batch_id = _as_str(row[col_batch])
            order_id = _as_str(row[col_order])
            if not batch_id or not order_id:
                logger.warning(
                    f"[conductivity_test_external] 第 {idx + 1} 行 batchId/orderId 为空，跳过"
                )
                continue

            entry = {
                "batchId": batch_id,
                "orderId": order_id,
                "createTime": _to_ymd_slash(row[col_ctime]) if col_ctime else _to_ymd_slash(None),
                "plateBarCode": plate_barcode,
                "bottleX": _as_int(row[col_x]),
                "bottleY": _as_int(row[col_y]),
                "temperaturePoint": _as_float(row[col_temp]),
            }
            entries.append(entry)
            logger.info(
                f"[conductivity_test_external] 第 {idx + 1} 行: "
                f"orderId={entry['orderId']}, plateBarCode={entry['plateBarCode']}, "
                f"X={entry['bottleX']}, Y={entry['bottleY']}, T={entry['temperaturePoint']}"
            )

        if not entries:
            raise ValueError("Excel 没有有效行可提交，请检查模板内容")

        unique_barcodes = sorted({e["plateBarCode"] for e in entries})
        logger.info(
            f"[conductivity_test_external] 共组装 {len(entries)} 条 entry，"
            f"涉及 {len(unique_barcodes)} 个板条码: {unique_barcodes}"
        )

        if validate_barcode:
            logger.info(
                f"[conductivity_test_external] 开始批量校验 plateBarCode "
                f"({len(unique_barcodes)} 个)..."
            )
            for bc in unique_barcodes:
                self._validate_plate_barcode(bc)
            logger.info("[conductivity_test_external] ✅ 所有 plateBarCode 校验通过")
        else:
            logger.warning(
                "[conductivity_test_external] ⚠️ validate_barcode=False，跳过板条码校验"
            )

        scheduler_start_resp: Optional[Dict[str, Any]] = None
        if start_scheduler:
            logger.info(
                "[conductivity_test_external] 启动调度（确保订单进队列后立即消费）..."
            )
            try:
                scheduler_start_resp = self.scheduler_start()
                logger.info(
                    f"[conductivity_test_external] 调度启动返回: {scheduler_start_resp}"
                )
                if not (isinstance(scheduler_start_resp, dict) and scheduler_start_resp.get("code") == 1):
                    logger.warning(
                        "[conductivity_test_external] ⚠️ 调度启动未返回 code=1，"
                        "继续提交订单，但订单可能不会被立即消费"
                    )
            except Exception as e:
                logger.warning(
                    f"[conductivity_test_external] ⚠️ 启动调度异常（继续提交订单）: {e}"
                )
                scheduler_start_resp = {"error": str(e)}
        else:
            logger.info(
                "[conductivity_test_external] start_scheduler=False，跳过启动调度"
            )

        logger.info(
            f"[conductivity_test_external] 提交 {len(entries)} 条 entry 到 "
            f"/api/lims/order/conductivity-orders"
        )
        response = self._post_lims("/api/lims/order/conductivity-orders", entries)
        try:
            response_dump = json.dumps(response, ensure_ascii=False)
        except Exception:
            response_dump = str(response)
        logger.info(
            f"[conductivity_test_external] LIMS 完整返回: {response_dump}"
        )
        if isinstance(response, dict):
            data_field = response.get("data")
            if not data_field:
                logger.warning(
                    "[conductivity_test_external] ⚠️ LIMS 返回中 data 为空，"
                    "奔曜软件可能没有真正建任务，请检查 createTime 格式 / orderId 是否存在 / 板条码绑定情况"
                )
            else:
                logger.info(
                    f"[conductivity_test_external] LIMS data 含 {len(data_field) if isinstance(data_field, list) else 1} 条"
                )

        # 2026-06-04 修：成功判据从 orderId!=EMPTY_GUID 改为 errorMessage为空+orderCode非空。
        # LIMS 创建阶段总是返回 orderId=全 0 GUID，实际执行时才填。
        # 2026-06-26：同时收集 (orderCode, orderId) 对，结果接口要 orderId。
        status = "error"
        new_order_pairs: List[Tuple[str, str]] = []  # [(orderCode, orderId), ...]
        if isinstance(response, dict) and response.get("code") == 1:
            data_field = response.get("data") or []
            if isinstance(data_field, list) and data_field:
                valid_entries = []
                error_entries = []
                for d in data_field:
                    if not isinstance(d, dict):
                        continue
                    err_msg = (d.get("errorMessage") or "").strip()
                    new_code = (d.get("orderCode") or "").strip()
                    new_oid = (d.get("orderId") or "").strip()
                    if not err_msg and new_code:
                        valid_entries.append(d)
                        new_order_pairs.append((new_code, new_oid))
                    else:
                        error_entries.append(d)
                if len(valid_entries) == len(data_field):
                    status = "submitted"
                elif valid_entries:
                    status = "partial"
                else:
                    status = "error"
                if error_entries:
                    logger.warning(
                        f"[conductivity_test_external] ⚠️ {len(error_entries)} 条 entry 创建失败:"
                    )
                    for e in error_entries:
                        logger.warning(
                            f"  - errorMessage={e.get('errorMessage')!r}, orderCode={e.get('orderCode')!r}"
                        )
            else:
                status = "error"
        new_order_codes_excel = [oc for oc, _ in new_order_pairs]
        logger.info(
            f"[conductivity_test_external] 最终 status={status}, "
            f"LIMS 新建电导单号={new_order_codes_excel}"
        )

        result: Dict[str, Any] = {
            "status": status,
            "total_entries": len(entries),
            "validated_barcodes": unique_barcodes,
            "response": response,
            "scheduler_start_response": scheduler_start_resp,
        }

        # ========== 等待完成 + 拉取电导结果 + 导出 CSV ==========
        if wait_for_finish and new_order_pairs:
            resolved_pairs, summary = self._wait_conductivity_and_resolve_ids(
                new_order_pairs, wait_timeout_seconds, tag="conductivity_test_external"
            )
            if summary["success"] == len(new_order_pairs):
                result["status"] = "all_completed"
            elif summary["success"] > 0:
                result["status"] = "partial_completed"
            result["completion_summary"] = summary

            conductivity_results = self._collect_conductivity_results(resolved_pairs)
            result["conductivity_results"] = conductivity_results

            if csv_export_path:
                try:
                    csv_file = self._export_conductivity_csv(
                        conductivity_results, csv_export_path, "External_conductivity"
                    )
                    result["csv_file"] = csv_file
                except Exception as e:
                    logger.error(f"[conductivity_test_external] CSV 导出失败: {e}")
        elif wait_for_finish and not new_order_pairs:
            logger.warning(
                "[conductivity_test_external] wait_for_finish=True 但无成功 orderCode，跳过等待"
            )

        return result

    # -------------------- 2.37 5号站新建实验（自动入口，接配液 handle） --------------------
    def conductivity_test_inline(
        self,
        vial_plates: List[Dict[str, Any]],
        temperature_points: List[float],
        vial_bottle_positions: Optional[List[Dict[str, Any]]] = None,
        mass_ratios: Optional[List[Dict[str, Any]]] = None,
        validate_barcode: bool = True,
        wait_for_finish: bool = True,
        wait_timeout_seconds: int = 36000,
        csv_export_path: str = "",
        **_legacy_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        2.37 5 号电导工作站自动新建实验（配液站传过来 / 接配液 output handle）。

        相比外部进样入口 (conductivity_test_external)，本函数：
        - 上游 vial_plates 自带 batch_id / orderCode / barCode / materialId
        - 通过 LIMS material-info (2.4) 查每块板的 detail 拿真实瓶位 (X, Y)
        - 额外接收配液配方信息 mass_ratios（来自配液 handle），测试完成后把
          电导结果按"分液瓶二维码"与配方合并导出 Formulation_Conductivity_*.csv

        2026-06-04 调整（与配液 _submit_and_wait_orders 保持一致）：
        - 不再 scheduler_stop / scheduler_start 切换。
          实测配液 (`/api/lims/order/orders`) 在调度 Running 时也能成功创建订单；
          5 号站电导接口同理，stop/start 切换是冗余的过度设计。
        - 调度状态由上游 action 自行管理；本函数仅 POST 提交订单。
        - **_legacy_kwargs 兜住老 workflow JSON 残留的过期入参（如 stop_scheduler_timeout），
          静默丢弃避免 TypeError；新代码不应依赖此通道。

        2026-06-05 调整（与配液保持一致的"阻塞等完成"语义）：
        - 默认 wait_for_finish=True：POST 拿到 LIMS 新建电导单号后，
          逐个调 _wait_conductivity_finish 阻塞等 /report/order_finish 推送，
          所有电导单跑完才 return —— 节点会一直运行直到电导测试完成（数小时级）。
        - 电导用专属的 _wait_conductivity_finish（按 orderCode 字典 + 报文缓存），
          多订单乱序完成互不干扰；推送先于 wait 调用到达也不会丢失。
        - 配液仍走原 wait_for_order_finish 单值机制，路径完全不变。
        - wait_for_finish=False 时回退到 fire-and-forget 行为，仅创建不等。

        2026-06-10 调整（按 5 号自动传递窗过滤）：
        - 上游配液会把订单产出的**所有** 20ml 分液瓶板都传进 vial_plates，不区分用途。
          真正要测电导的板 = 已转运到「5 号自动传递窗」的板。
        - 提交前查 warehouse-info(5号自动传递窗) 按 materialId 取交集，只保留在 5 号站的板。
          这样即使配液传了扣电/软包等非电导板，也不会误建电导单。
        - 注意时序：要求板已转运到 5 号站后再调本 action；否则会被全部过滤而报错。
        - temperature_points 的长度校验在过滤之后进行（按过滤后的板数）。

        Args:
            vial_plates: 上游配液输出的分液瓶板列表，每项需含
                batch_id / materialId / barCode / orderCode（缺任一报错）
            temperature_points: 温度点列表（℃）。
                长度=1 → 广播到所有分液板；长度=N（=过滤后分液板数）→ 一一对应；其他长度报错。
                **同一块板上的所有分液瓶共享同一温度点（一块板一个温度）。**
            mass_ratios: 配液配方信息列表（来自配液 auto-create_orders /
                auto-create_orders_formulation 的 mass_ratios 输出 handle）。
                用于测试完成后按"分液瓶二维码"合并出配液+电导报告；为空则只出电导报告。
            validate_barcode: 提交前是否对 plateBarCode 做物料系统强校验，默认 True
            wait_for_finish: 是否阻塞等待所有 LIMS 新建电导单完成，默认 True
            wait_timeout_seconds: 单个订单 wait 超时秒数，默认 36000s = 10h（与配液一致）
            csv_export_path: CSV 导出目录，为空则不导出。非空时在同一目录生成两份：
                Inline_conductivity_*.csv（电导-only）+ Formulation_Conductivity_*.csv（配液+电导）

        Returns:
            {
                "status": "submitted" | "partial" | "error" | "all_completed" | "partial_completed",
                "total_entries": int,
                "validated_barcodes": List[str],
                "batch_id": str,
                "new_order_codes": List[str],
                "response": <LIMS POST 原始返回>,
                # 仅当 wait_for_finish=True 时有：
                "completion_summary": {"success": int, "timeout": int, ...},
                "conductivity_results": List[Dict],  # 每瓶电导结果
                "csv_file_conductivity": <可选，Inline_conductivity_*.csv 路径>,
                "csv_file_formulation": <可选，Formulation_Conductivity_*.csv 路径>,
            }

        Raises:
            ValueError: 入参不合法（vial_plates 空 / temperature_points 长度异常 / batch_id 为空 / plate 缺关键字段）
            BioyondException: barCode 校验失败 / detail 中无瓶位
        """
        if not vial_plates or not isinstance(vial_plates, list):
            raise ValueError("vial_plates 不能为空")
        if not temperature_points or not isinstance(temperature_points, list):
            raise ValueError("temperature_points 不能为空")
        # 一块板一个温度点，而 5 号自动传递窗只有 4 个库位 → 一轮最多 4 个温度点。
        # 超了必须拆轮，否则会建出永远排不进传递窗的单。
        if len(temperature_points) > self._WINDOW_5_CAPACITY:
            raise ValueError(
                f"单轮温度点数为 {len(temperature_points)}，超过 5 号自动传递窗的"
                f"{self._WINDOW_5_CAPACITY} 个库位上限（一块板对应一个温度点）。"
                f"请拆成多轮，每轮不超过 {self._WINDOW_5_CAPACITY} 个温度点。"
            )

        # ========== 阶段0: 只保留已转运到 5 号自动传递窗的板 ==========
        # 上游配液会把订单产出的全部 20ml 分液瓶板都传进来（不区分用途），
        # 真正要测电导的 = 已搬到「5 号自动传递窗」的板。查 warehouse-info(2.38) 按 materialId 取交集。
        wh_resp = self._post_lims(
            "/api/lims/storage/warehouse-info",
            {"whId": self._WH_ID_TRANSFER_WINDOW_5, "includeDetail": True},
        )
        if not isinstance(wh_resp, dict) or wh_resp.get("code") != 1:
            raise BioyondException(f"查询 5 号自动传递窗失败: {wh_resp}")
        station_mids = {
            (loc.get("holdMId") or "").strip()
            for loc in ((wh_resp.get("data") or {}).get("locations") or [])
            if isinstance(loc, dict) and loc.get("holdMId")
        }
        kept = [
            p for p in vial_plates
            if isinstance(p, dict) and (p.get("materialId") or "").strip() in station_mids
        ]
        logger.info(
            f"[conductivity_test_inline] 5 号自动传递窗过滤: "
            f"{len(vial_plates)} → {len(kept)} 块板（5 号站现有板 {len(station_mids)} 块）"
        )
        vial_plates = kept
        if not vial_plates:
            raise BioyondException(
                "过滤后没有任何板在 5 号自动传递窗，请确认要测电导的板已转运到 5 号站。"
            )

        # 提取 batch_id（同 batch 内所有 plate 共享同一值）
        batch_id_raw = vial_plates[0].get("batch_id") if isinstance(vial_plates[0], dict) else None
        if not batch_id_raw:
            raise ValueError("vial_plates[0] 缺少 batch_id 字段，请检查上游配液输出")
        batch_id = str(batch_id_raw).strip()
        if not batch_id:
            raise ValueError("vial_plates[0].batch_id 为空字符串")

        n_plates = len(vial_plates)
        n_temps = len(temperature_points)
        if n_temps not in (1, n_plates):
            raise ValueError(
                f"temperature_points 长度 {n_temps} 非法："
                f"应为 1（广播）或 {n_plates}（与分液板数一致）。"
                f"业务规则：一块板共享一个温度。"
            )

        logger.info(
            f"[conductivity_test_inline] 开始：batch_id={batch_id}, "
            f"分液板数={n_plates}, 温度点数={n_temps}"
        )

        def _to_ymd_slash_hms() -> str:
            ts = datetime.now()
            return (
                f"{ts.year}/{ts.month}/{ts.day} "
                f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}"
            )

        # ========== 阶段1: 校验 + 按 vial_bottle_positions 组装 entries ==========
        # 归属/瓶位来自上游配液在 create_orders 收尾（板状态干净）时构建的
        # vial_bottle_positions（detailMaterialId→orderCode 权威映射，已过滤空占位孔）。
        # 不再现场查 detail + associateId 反查，彻底避免串单 / 多算空孔。
        if not vial_bottle_positions or not isinstance(vial_bottle_positions, list):
            raise ValueError(
                "vial_bottle_positions 为空：本 action 已改为依赖上游配液输出的"
                "「分液瓶孔位映射」handle。请把 create_orders / create_orders_formulation 的"
                " vial_bottle_positions 输出接到本 action 的同名入参（旧 workflow 需同步更新接线）。"
            )

        EXPECTED_PLATE_TYPE = "20ml分液瓶板"
        # 校验 kept 板 + 记录每块板的温度/条码（温度仍按 kept 板序 idx）
        plate_temp_by_mid: Dict[str, float] = {}
        plate_barcode_by_mid: Dict[str, str] = {}
        for idx, plate in enumerate(vial_plates):
            if not isinstance(plate, dict):
                raise ValueError(f"vial_plates[{idx}] 不是 dict: {plate!r}")
            plate_barcode = plate.get("barCode")
            material_id = plate.get("materialId")
            type_name = plate.get("typeName", "")
            if not plate_barcode or not material_id:
                raise ValueError(
                    f"vial_plates[{idx}] 缺少关键字段: "
                    f"barCode={plate_barcode!r}, materialId={material_id!r}"
                )
            if type_name and type_name != EXPECTED_PLATE_TYPE:
                raise BioyondException(
                    f"vial_plates[{idx}] typeName={type_name!r}，电导测试要求 "
                    f"{EXPECTED_PLATE_TYPE!r}。请检查上游配液是否提取到了正确的板"
                    f"（参考 _extract_vial_plate_from_report 与 LIMS 工艺配置）。"
                )
            if validate_barcode:
                self._validate_plate_barcode(plate_barcode)
            plate_temp_by_mid[material_id] = float(
                temperature_points[0] if n_temps == 1 else temperature_points[idx]
            )
            plate_barcode_by_mid[material_id] = plate_barcode

        kept_mids = set(plate_temp_by_mid.keys())
        kept_barcodes = set(plate_barcode_by_mid.values())

        # 按 plateMaterialId（兜底 plateBarCode）把 positions 归到 kept 板
        positions_by_plate: Dict[str, List[Dict[str, Any]]] = {mid: [] for mid in kept_mids}
        for pos in vial_bottle_positions:
            if not isinstance(pos, dict):
                continue
            pmid = pos.get("plateMaterialId") or ""
            pbar = pos.get("plateBarCode") or ""
            if pmid in kept_mids:
                positions_by_plate[pmid].append(pos)
            elif pbar in kept_barcodes:
                # 兜底：materialId 对不上但条码在 kept 中，按条码找回对应 mid
                for mid, bc in plate_barcode_by_mid.items():
                    if bc == pbar:
                        positions_by_plate[mid].append(pos)
                        break

        entries: List[Dict[str, Any]] = []
        for mid in kept_mids:
            plate_barcode = plate_barcode_by_mid[mid]
            plate_temp = plate_temp_by_mid[mid]
            plate_positions = positions_by_plate.get(mid, [])
            if not plate_positions:
                logger.warning(
                    f"[conductivity_test_inline] ⚠️ kept 板 {plate_barcode} 在 "
                    f"vial_bottle_positions 中无对应分液瓶，跳过（请确认瓶在转运前已入板）。"
                )
                continue
            for pos in plate_positions:
                entry = {
                    "batchId": batch_id,
                    "orderId": pos.get("orderCode") or "",
                    "createTime": _to_ymd_slash_hms(),
                    "plateBarCode": plate_barcode,
                    "bottleX": pos.get("x"),
                    "bottleY": pos.get("y"),
                    "temperaturePoint": plate_temp,
                }
                entries.append(entry)
                logger.info(
                    f"[conductivity_test_inline] entry: orderId={entry['orderId']} "
                    f"plateBarCode={plate_barcode}, X={pos.get('x')}, Y={pos.get('y')}, "
                    f"bottle={pos.get('barCode')}, T={plate_temp}"
                )

        if not entries:
            raise ValueError("entries 组装为空，无法提交")

        unique_barcodes = sorted({e["plateBarCode"] for e in entries})
        logger.info(
            f"[conductivity_test_inline] 共组装 {len(entries)} 条 entry，"
            f"涉及 {len(unique_barcodes)} 个板条码"
        )

        # ========== 阶段2: 直接 POST（调度由上游/外部管理，与配液 _submit_and_wait_orders 一致）==========
        logger.info(
            f"[conductivity_test_inline] POST {len(entries)} 条 entry 到 "
            f"/api/lims/order/conductivity-orders ..."
        )
        response = self._post_lims("/api/lims/order/conductivity-orders", entries)
        try:
            response_dump = json.dumps(response, ensure_ascii=False)
        except Exception:
            response_dump = str(response)
        logger.info(f"[conductivity_test_inline] LIMS 完整返回: {response_dump}")

        # ========== 阶段3: 解析 response 算 status ==========
        # 2026-06-04 修：原先用 orderId != EMPTY_GUID 判断成功是错的——
        # LIMS 创建阶段总是返回 orderId="00000000-...-000000000000"（execution 时才填 GUID）。
        # 实际可用的成功判据是 errorMessage 为空 + orderCode 非空（=LIMS 已建出新电导单号）。
        # 失败示例：errorMessage="...没有在5号手套箱仓库..." & orderCode=null & usedMaterials=[]
        # 成功示例：errorMessage=null & orderCode="BSO20260604000XX" & usedMaterials=[plate, bottle]
        # 2026-06-26：同时收集 (orderCode, orderId) 对，结果接口要 orderId。
        status = "error"
        new_order_pairs: List[Tuple[str, str]] = []  # [(orderCode, orderId), ...]
        if isinstance(response, dict) and response.get("code") == 1:
            data_field = response.get("data") or []
            if isinstance(data_field, list) and data_field:
                valid_entries = []
                error_entries = []
                for d in data_field:
                    if not isinstance(d, dict):
                        continue
                    err_msg = (d.get("errorMessage") or "").strip()
                    new_code = (d.get("orderCode") or "").strip()
                    new_oid = (d.get("orderId") or "").strip()
                    if not err_msg and new_code:
                        valid_entries.append(d)
                        new_order_pairs.append((new_code, new_oid))
                    else:
                        error_entries.append(d)

                if len(valid_entries) == len(data_field):
                    status = "submitted"
                elif valid_entries:
                    status = "partial"
                else:
                    status = "error"

                if error_entries:
                    logger.warning(
                        f"[conductivity_test_inline] ⚠️ {len(error_entries)} 条 entry 创建失败:"
                    )
                    for e in error_entries:
                        logger.warning(
                            f"  - errorMessage={e.get('errorMessage')!r}, "
                            f"orderCode={e.get('orderCode')!r}"
                        )
        new_order_codes = [oc for oc, _ in new_order_pairs]
        logger.info(
            f"[conductivity_test_inline] 最终 status={status}, "
            f"LIMS 新建电导单号={new_order_codes}"
        )

        result: Dict[str, Any] = {
            "status": status,
            "total_entries": len(entries),
            "validated_barcodes": unique_barcodes,
            "batch_id": batch_id,
            "new_order_codes": new_order_codes,  # LIMS 新建的电导单号列表（成功 entry 对应的）
            "response": response,
        }

        # ========== 阶段4: （可选）阻塞等所有 LIMS 新建电导单跑完 + 拉结果 + 出两份 CSV ==========
        # 节点会一直运行到所有电导单子收到 /report/order_finish 推送（成功/异常/超时），
        # 然后按 orderId 调 /conductivity-order-result 拉电导率/温度，导出两份 CSV：
        # Inline_conductivity_*.csv（电导-only）+ Formulation_Conductivity_*.csv（配液+电导）。
        if wait_for_finish and new_order_pairs:
            resolved_pairs, summary = self._wait_conductivity_and_resolve_ids(
                new_order_pairs, wait_timeout_seconds, tag="conductivity_test_inline"
            )
            if summary["success"] == len(new_order_pairs):
                result["status"] = "all_completed"
            elif summary["success"] > 0:
                result["status"] = "partial_completed"
            result["completion_summary"] = summary

            conductivity_results = self._collect_conductivity_results(resolved_pairs)
            result["conductivity_results"] = conductivity_results

            if csv_export_path:
                try:
                    csv_file_cond = self._export_conductivity_csv(
                        conductivity_results, csv_export_path, "Inline_conductivity"
                    )
                    result["csv_file_conductivity"] = csv_file_cond
                except Exception as e:
                    logger.error(f"[conductivity_test_inline] 电导 CSV 导出失败: {e}")
                try:
                    csv_file_form = self._export_conductivity_formulation_csv(
                        conductivity_results, mass_ratios or [], csv_export_path
                    )
                    result["csv_file_formulation"] = csv_file_form
                    result["datacore_push_formulation"] = self._push_csv_to_datacore(
                        csv_file_form, tag="conductivity_test_inline"
                    )
                except Exception as e:
                    logger.error(f"[conductivity_test_inline] 配液+电导 CSV 导出失败: {e}")
        elif wait_for_finish and not new_order_pairs:
            logger.warning(
                "[conductivity_test_inline] wait_for_finish=True 但 LIMS 未返回任何成功 orderCode，跳过等待"
            )

        return result

    def _extract_vial_plate_from_report(self, report: Dict) -> List[Dict[str, Any]]:
        """
        从 order_finish 报文中提取该订单**所有**的分液瓶板。

        2026-06-04 重构：
        - 返回类型由 Optional[Dict] 改为 List[Dict]。
          按协议 3.37 示例，一个 orderId 下完全允许多块不同 plateBarCode 的板，
          配液工艺也确实会把同一 orderCode 拆到多块 20ml 分液瓶板上（电导多温度场景）。
        - 扫描所有 typemode=1 物料，逐个查 material-info（2.4 接口拿 typeName / barCode）；
          返回该单**全部** "*分液瓶板"（20ml 排在前，其它板型在后）。
        - 2026-07-26 修正：旧实现"找到 20ml 就只返回 20ml"，会在混合实验
          （20ml 测电导 + 5ml 供扣电）里把 5ml 板整块丢掉，导致 321 转运找不到板。
          现改为全部返回，按板型分流交由下游负责（321 只挑 5ml、电导只挑 20ml）。
        - 不再用 locationId 前缀（"3a19debc-84b5-" 自动堆栈-左）做硬过滤，
          实测 LIMS 把 20ml 板放在 3a1c68b5-… 等其它库位，老规则会误漏。

        Args:
            report: LIMS 订单完成推送报文（2.23 push 报文，usedMaterials 仅含
                materialId / locationId / typeMode / usedQuantity / realQuantity）

        Returns:
            List[Dict]：每块板一条字典，结构为
            {
                "materialId": "GUID",
                "locationId": "GUID",
                "orderCode": "BSO...",
                "orderId": "GUID",  # 配液订单 GUID，用于跨 order 共用板时按瓶子的 associateId 反查归属
                "typeName": "20ml分液瓶板",
                "barCode": "..."   # 可能为空字符串（LIMS 端没建条码时）
            }
            未找到任何分液瓶板时返回 []。
        """
        order_code = report.get("orderCode", "N/A")
        order_id_guid = report.get("orderId", "") or ""
        used_materials = report.get("usedMaterials", [])

        logger.info(
            f"[提取分液瓶板] 开始处理订单 orderCode={order_code}, "
            f"物料数量={len(used_materials)}"
        )

        PREFERRED_TYPE = "20ml分液瓶板"
        candidates_preferred: List[Dict[str, Any]] = []
        candidates_other_plate: List[Dict[str, Any]] = []
        seen_material_ids: set = set()

        for idx, material in enumerate(used_materials):
            typemode = material.get("typemode", "")
            material_id = material.get("materialId", "")
            location_id = material.get("locationId", "") or ""
            if str(typemode) != "1" or not material_id:
                continue
            # 同一 materialId 在 usedMaterials 里可能被列多次（出库/入库等场景），去重
            if material_id in seen_material_ids:
                continue
            seen_material_ids.add(material_id)

            logger.debug(
                f"[提取分液瓶板] 候选 typemode=1 物料 #{idx+1}: "
                f"materialId={material_id[:20]}..., locationId={location_id[:20]}..."
            )

            try:
                material_info = self._resolve_material_type_info(material, material_id)
            except Exception as e:
                logger.warning(
                    f"[提取分液瓶板] ⚠️ 查询物料详情失败: materialId={material_id}, 错误={e}"
                )
                continue

            if not material_info:
                logger.warning(
                    f"[提取分液瓶板] ⚠️ 无法解析物料类型: materialId={material_id}"
                )
                continue

            type_name = material_info.get("typeName", "") or ""
            if "分液瓶板" not in type_name:
                logger.debug(f"[提取分液瓶板] 跳过非分液瓶板: typeName={type_name}")
                continue

            # 从 locations 取"自动堆栈-左"仓库的 xyz 坐标（上游 851b923 引入），
            # 用于 transfer_3_to_2_to_1 / transfer_3_to_2 通过 handle 接收源坐标。
            # 多 plate 场景下每块板独立预存自己的坐标，下游 _find_plate_xyz 仍能正确按板型选取。
            # source_found 必须显式记录：未命中时的 (1,1,1) 只是占位默认值，
            # 与"真的在 A01"无法区分。下游 321/32 若拿占位值去建单，会去搬
            # A01 上那块**别的**板（转错板），或者坐标不存在直接建单失败。
            src_x, src_y, src_z = 1, 1, 1
            src_found = False
            for loc in (material_info.get("locations") or []):
                if loc.get("whid") == self._WH_ID_AUTO_STACK_LEFT:
                    src_x = loc.get("x", 1)
                    src_y = loc.get("y", 1)
                    src_z = loc.get("z", 1)
                    src_found = True
                    break

            plate_info = {
                "materialId": material_id,
                "locationId": location_id,
                "orderCode": order_code,
                "orderId": order_id_guid,
                "typeName": type_name,
                "barCode": material_info.get("barCode") or "",
                "source_x": src_x,
                "source_y": src_y,
                "source_z": src_z,
                "source_found": src_found,
            }
            if type_name == PREFERRED_TYPE:
                candidates_preferred.append(plate_info)
                logger.info(
                    f"[提取分液瓶板] ✅ 命中 {PREFERRED_TYPE}: "
                    f"materialId={material_id}, locationId={location_id}, "
                    f"barCode={plate_info['barCode']}, "
                    f"自动堆栈-左坐标=({src_x},{src_y},{src_z})"
                    f"{'' if src_found else ' [未在自动堆栈-左命中，为占位默认值]'}"
                )
            else:
                candidates_other_plate.append(plate_info)
                logger.info(
                    f"[提取分液瓶板] 命中其它分液瓶板: typeName={type_name}, "
                    f"materialId={material_id}, barCode={plate_info['barCode']}, "
                    f"自动堆栈-左坐标=({src_x},{src_y},{src_z})"
                    f"{'' if src_found else ' [未在自动堆栈-左命中，为占位默认值]'}"
                )

        # 返回全部分液瓶板（20ml + 5ml 等），不再"有 20ml 就丢掉 5ml"。
        # 混合实验（20ml 测电导 + 5ml 供扣电）下，旧的二选一返回会让 5ml 板
        # 永远进不了 vial_plates，导致 321 转运找不到板。按板型分流由下游各自负责：
        # 321 只挑 5ml、32/电导只挑 20ml（电导还会先按 5 号传递窗求交集过滤）。
        all_plates = candidates_preferred + candidates_other_plate

        if not all_plates:
            logger.warning(f"[提取分液瓶板] ❌ 未找到任何分液瓶板: orderCode={order_code}")
            return []

        if candidates_preferred:
            logger.info(
                f"[提取分液瓶板] ✅ orderCode={order_code} 共找到 "
                f"{len(candidates_preferred)} 块 {PREFERRED_TYPE}: "
                f"{[(p['barCode'] or p['materialId'][:8]) for p in candidates_preferred]}"
            )
        else:
            # 配液+扣电 / 配液+软包 等模式本来就没有 20ml 板，属正常情况，不告警。
            logger.info(
                f"[提取分液瓶板] orderCode={order_code} 无 {PREFERRED_TYPE}"
                f"（配液+扣电等模式属正常）；若本单需测电导，请检查 LIMS 工艺配置或上游入参。"
            )

        if candidates_other_plate:
            logger.info(
                f"[提取分液瓶板] 另含 {len(candidates_other_plate)} 块其它分液瓶板: "
                f"{[(p['typeName'], p['barCode'] or p['materialId'][:8]) for p in candidates_other_plate]}"
                f"（一并返回，由下游按板型分流）"
            )

        logger.info(
            f"[提取分液瓶板] orderCode={order_code} 合计返回 {len(all_plates)} 块分液瓶板"
        )
        return all_plates

    def _extract_prep_bottle_from_report(self, report: Dict) -> Optional[Dict]:
        """
        从 order_finish 报文中提取配液瓶信息

        2026-07-15 重构：去掉库位白名单硬过滤。多配液瓶板场景下，未完成的板仍停在
        「配液站内配液大板仓库」(3a1a21dc-…) 等位置，旧白名单会漏提。
        新规则与分液瓶板提取对齐：
        - 扫描所有 typemode == "1" 且 realQuantity == 1 且 usedQuantity == 1 的物料
        - 调 LIMS API 2.4，typeName 精确匹配 "配液瓶(小)" / "配液瓶(大)"
          （避免 "配液瓶(大)板" 子串误判）
        - 同单命中多个候选时，用 material-info.associateId 与 orderId 匹配去歧义

        Args:
            report: LIMS order_finish 报文

        Returns:
            {
                "materialId": "...",
                "locationId": "...",
                "orderCode": "...",
                "typeName": "配液瓶(小)" or "配液瓶(大)",
                "barCode": "..."
            }
            未找到时返回 None
        """
        order_code = report.get("orderCode", "N/A")
        order_id_guid = report.get("orderId", "") or ""
        used_materials = report.get("usedMaterials", [])

        logger.info(
            f"[提取配液瓶] 开始处理订单 orderCode={order_code}, "
            f"物料数量={len(used_materials)}"
        )

        PREP_TYPES = ("配液瓶(小)", "配液瓶(大)")
        candidates: List[Dict[str, Any]] = []
        seen_material_ids: set = set()

        for idx, material in enumerate(used_materials):
            typemode = material.get("typemode", "")
            material_id = material.get("materialId", "") or ""
            location_id = material.get("locationId", "") or ""
            real_qty = material.get("realQuantity")
            used_qty = material.get("usedQuantity")

            if str(typemode) != "1" or not material_id:
                continue
            if real_qty != 1 or used_qty != 1:
                continue
            if material_id in seen_material_ids:
                continue
            seen_material_ids.add(material_id)

            logger.debug(
                f"[提取配液瓶] 候选物料 #{idx+1}: materialId={material_id[:20]}..., "
                f"locationId={(location_id or '')[:20]}..."
            )

            try:
                material_info = self._resolve_material_type_info(material, material_id)
            except Exception as e:
                logger.warning(
                    f"[提取配液瓶] ⚠️ 查询物料详情失败: materialId={material_id}, 错误={e}"
                )
                continue

            if not material_info:
                logger.warning(
                    f"[提取配液瓶] ⚠️ 无法解析物料类型: materialId={material_id}"
                )
                continue

            type_name = material_info.get("typeName", "") or ""
            if type_name not in PREP_TYPES:
                logger.debug(
                    f"[提取配液瓶] 候选物料不是配液瓶: typeName={type_name}, 跳过"
                )
                continue

            candidates.append({
                "materialId": material_id,
                "locationId": location_id,
                "orderCode": order_code,
                "typeName": type_name,
                "barCode": material_info.get("barCode") or "",
                "associateId": material_info.get("associateId") or "",
            })
            logger.info(
                f"[提取配液瓶] ✅ 命中候选: orderCode={order_code}, "
                f"typeName={type_name}, barCode={material_info.get('barCode')}, "
                f"locationId={(location_id or '')[:20]}..., "
                f"associateId={(material_info.get('associateId') or '')[:20]}..."
            )

        if not candidates:
            logger.warning(f"[提取配液瓶] ❌ 未找到配液瓶: orderCode={order_code}")
            return None

        # 多候选去歧义：associateId == orderId 优先
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chosen = None
            if order_id_guid:
                for c in candidates:
                    if c.get("associateId") and c["associateId"] == order_id_guid:
                        chosen = c
                        break
            if chosen is None:
                chosen = candidates[0]
                logger.warning(
                    f"[提取配液瓶] ⚠️ orderCode={order_code} 命中 {len(candidates)} 个配液瓶候选，"
                    f"associateId 未能与 orderId={order_id_guid[:20] if order_id_guid else '<empty>'}... "
                    f"匹配，取第一个: barCode={chosen.get('barCode')}"
                )
            else:
                logger.info(
                    f"[提取配液瓶] 多候选按 associateId 选定: "
                    f"barCode={chosen.get('barCode')}"
                )

        # 不把 associateId 暴露到导出结构，保持向后兼容
        return {
            "materialId": chosen["materialId"],
            "locationId": chosen["locationId"],
            "orderCode": chosen["orderCode"],
            "typeName": chosen["typeName"],
            "barCode": chosen["barCode"],
        }

    def _extract_vial_bottles_from_report(self, report: Dict) -> List[Dict]:
        """
        从 order_finish 报文中提取分液瓶信息（注意不是分液瓶板）—— 单报文本地路径。

        保留为兼容回退：当板级 `_extract_vial_bottles_from_plates` 不可用时仍可调用。
        2026-07-15：去掉自动堆栈库位白名单；按 typeName 精确匹配 "5ml分液瓶"/"20ml分液瓶"。
        多板场景请优先走 `_extract_vial_bottles_from_plates`（板 detail + associateId）。

        Args:
            report: LIMS order_finish 报文

        Returns:
            分液瓶信息列表，每个元素：
            {
                "materialId": "...",
                "locationId": "...",
                "orderCode": "...",
                "typeName": "5ml分液瓶" or "20ml分液瓶",
                "barCode": "..."
            }
        """
        order_code = report.get("orderCode", "N/A")
        used_materials = report.get("usedMaterials", [])

        logger.info(
            f"[提取分液瓶] 开始处理订单 orderCode={order_code}, "
            f"物料数量={len(used_materials)}"
        )

        VIAL_TYPES = ("5ml分液瓶", "20ml分液瓶")
        vial_bottles: List[Dict] = []
        seen_material_ids: set = set()

        for idx, material in enumerate(used_materials):
            typemode = material.get("typemode", "")
            material_id = material.get("materialId", "") or ""
            location_id = material.get("locationId", "") or ""
            real_qty = material.get("realQuantity")
            used_qty = material.get("usedQuantity")

            if str(typemode) != "1" or not material_id:
                continue
            if real_qty != 1 or used_qty != 1:
                continue
            if material_id in seen_material_ids:
                continue
            seen_material_ids.add(material_id)

            logger.debug(
                f"[提取分液瓶] 候选物料 #{idx+1}: materialId={material_id[:20]}..."
            )

            try:
                material_info = self._resolve_material_type_info(material, material_id)
            except Exception as e:
                logger.warning(
                    f"[提取分液瓶] ⚠️ 查询物料详情失败: materialId={material_id}, 错误={e}"
                )
                continue

            if not material_info:
                logger.warning(
                    f"[提取分液瓶] ⚠️ 无法解析物料类型: materialId={material_id}"
                )
                continue

            type_name = material_info.get("typeName", "") or ""
            if type_name in VIAL_TYPES:
                bar_code = material_info.get("barCode") or ""
                logger.info(
                    f"[提取分液瓶] ✅ 确认为分液瓶: orderCode={order_code}, "
                    f"typeName={type_name}, barCode={bar_code}"
                )
                vial_bottles.append({
                    "materialId": material_id,
                    "locationId": location_id,
                    "orderCode": order_code,
                    "typeName": type_name,
                    "barCode": bar_code,
                })
            else:
                logger.debug(
                    f"[提取分液瓶] 候选物料不是分液瓶: typeName={type_name}, 跳过"
                )

        if vial_bottles:
            logger.info(
                f"[提取分液瓶] 订单 {order_code} 共找到 {len(vial_bottles)} 个分液瓶: "
                f"{[v['typeName'] for v in vial_bottles]}"
            )
        else:
            logger.warning(f"[提取分液瓶] ❌ 未找到分液瓶: orderCode={order_code}")

        return vial_bottles

    def _build_vial_bottle_mat2order(self, data_list: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        从 create_orders 返回的 data[*].usedMaterials 构建 分液瓶 materialId → orderCode 映射。

        每个分液瓶只在其归属订单的 usedMaterials 中出现一次（1:1），因此该映射权威可靠，
        用于取代旧的 associateId 反查（associateId 属订单侧独立 GUID，恒不等于 orderId，
        多单共板时会全部 fallback 到第一单，导致电导串单/多算空孔）。
        """
        VIAL_TYPES = ("5ml分液瓶", "20ml分液瓶")
        mat2order: Dict[str, str] = {}
        for od in data_list or []:
            if not isinstance(od, dict):
                continue
            order_code = od.get("orderCode") or ""
            if not order_code:
                continue
            for m in od.get("usedMaterials", []) or []:
                if not isinstance(m, dict):
                    continue
                if (m.get("materialTypeName") or "") not in VIAL_TYPES:
                    continue
                mid = m.get("materialId") or ""
                if mid:
                    mat2order[mid] = order_code
        logger.info(f"[分液瓶归属] materialId→orderCode 映射构建完成，共 {len(mat2order)} 个分液瓶")
        return mat2order

    def _build_vial_bottle_positions(
        self,
        vial_plates: List[Dict[str, Any]],
        mat2order: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        在 create_orders 收尾（板状态干净）时，查每块分液瓶板的孔位 detail，
        用 detailMaterialId 命中 mat2order 得到归属订单，产出“分液瓶孔位映射”。

        仅保留 detailMaterialId ∈ 本批 mat2order 的孔（自动过滤空占位孔/非本批瓶），
        供下游电导 conductivity_test_inline 直接组装 entry（不再靠 associateId 反查）。

        Returns:
            [{plateBarCode, plateMaterialId, bottleMaterialId, x, y, orderCode,
              typeName, barCode, batch_id}, ...]
        """
        positions: List[Dict[str, Any]] = []
        for plate in vial_plates or []:
            if not isinstance(plate, dict):
                continue
            plate_material_id = plate.get("materialId") or ""
            plate_barcode = plate.get("barCode") or ""
            batch_id = plate.get("batch_id") or ""
            plate_type = plate.get("typeName") or ""
            if not plate_material_id:
                continue
            vial_type = ""
            if "5ml" in plate_type.lower():
                vial_type = "5ml分液瓶"
            elif "20ml" in plate_type.lower():
                vial_type = "20ml分液瓶"
            try:
                bottles = self._query_plate_bottle_positions(plate_material_id)
            except Exception as e:
                logger.warning(
                    f"[分液瓶孔位] ⚠️ 查询板孔位失败，跳过: "
                    f"plate={plate_barcode or plate_material_id[:20]}, 错误={e}"
                )
                continue
            kept = 0
            for bottle in bottles:
                bottle_mid = bottle.get("detailMaterialId") or ""
                order_code = mat2order.get(bottle_mid)
                if not order_code:
                    # 空占位孔 / 非本批瓶 → 跳过（解决旧逻辑把全部孔都当瓶的多算问题）
                    continue
                positions.append({
                    "plateBarCode": plate_barcode,
                    "plateMaterialId": plate_material_id,
                    "bottleMaterialId": bottle_mid,
                    "x": bottle.get("x"),
                    "y": bottle.get("y"),
                    "orderCode": order_code,
                    "typeName": (bottle.get("typeName") or "").strip() or vial_type,
                    "barCode": bottle.get("code") or "",
                    "batch_id": batch_id,
                })
                kept += 1
            logger.info(
                f"[分液瓶孔位] plate={plate_barcode}: detail {len(bottles)} 孔 → 命中本批 {kept} 瓶"
            )
        logger.info(f"[分液瓶孔位] 汇总：共 {len(positions)} 个分液瓶孔位映射")
        return positions

    def _extract_vial_bottles_from_plates(
        self,
        vial_plates: List[Dict[str, Any]],
        order_codes: List[str],
        mat2order: Optional[Dict[str, str]] = None,
    ) -> Dict[str, List[Dict]]:
        """
        按物理分液瓶板提取分液瓶，用 detailMaterialId → mat2order 映射到归属订单。

        多配液瓶板 × 多分液瓶板场景下，单瓶报文常常只含「分液瓶板」、且库位停在
        「大分液瓶堆栈」(3a19da3d-…) 而非自动堆栈，旧的逐单库位白名单会整列漏提。
        本方法对去重后的每块板调 `_query_plate_bottle_positions`，再用权威的
        `mat2order`（materialId→orderCode，来自 create 响应 usedMaterials）按孔位
        detailMaterialId 归属；命中不到的孔（空占位/非本批）直接跳过。

        Args:
            vial_plates: `_submit_and_wait_orders` 去重后的 all_vial_plates
                （含 materialId / typeName / barCode）
            order_codes: 本批次全部 orderCode，用于保证返回 dict 覆盖所有订单
            mat2order: 分液瓶 materialId → orderCode 权威映射（`_build_vial_bottle_mat2order`）

        Returns:
            {orderCode: [{materialId, locationId, orderCode, typeName, barCode}, ...]}
        """
        result: Dict[str, List[Dict]] = {oc: [] for oc in order_codes if oc}
        mat2order = mat2order or {}

        for plate in vial_plates or []:
            if not isinstance(plate, dict):
                continue
            plate_material_id = plate.get("materialId") or ""
            plate_barcode = plate.get("barCode") or ""
            plate_location = plate.get("locationId") or ""
            if not plate_material_id:
                continue

            try:
                bottles = self._query_plate_bottle_positions(plate_material_id)
            except Exception as e:
                logger.warning(
                    f"[提取分液瓶-板级] ⚠️ 查询板孔位失败: "
                    f"plate={plate_barcode or plate_material_id[:20]}, 错误={e}"
                )
                continue

            for bottle in bottles:
                bottle_mid = bottle.get("detailMaterialId") or ""
                xy = (bottle.get("x", 0), bottle.get("y", 0))
                order_code = mat2order.get(bottle_mid)
                if not order_code:
                    # 空占位孔 / 非本批瓶（detailMaterialId 不在权威映射中）→ 跳过
                    continue

                # code 即板 detail 上的二维码；typeName 缺省时按板型推断
                bar_code = bottle.get("code") or ""
                type_name = (bottle.get("typeName") or "").strip()
                if not type_name:
                    plate_type = plate.get("typeName") or ""
                    if "5ml" in plate_type.lower() or "5mL" in plate_type:
                        type_name = "5ml分液瓶"
                    elif "20ml" in plate_type.lower() or "20mL" in plate_type:
                        type_name = "20ml分液瓶"

                entry = {
                    "materialId": bottle.get("detailMaterialId") or "",
                    "locationId": plate_location,
                    "orderCode": order_code,
                    "typeName": type_name,
                    "barCode": bar_code,
                    "plateBarCode": plate_barcode,
                    "x": bottle.get("x"),
                    "y": bottle.get("y"),
                }
                result.setdefault(order_code, []).append(entry)
                logger.info(
                    f"[提取分液瓶-板级] ✅ plate={plate_barcode}, "
                    f"orderCode={order_code}, typeName={type_name}, "
                    f"barCode={bar_code}, XY={xy}"
                )

        total = sum(len(v) for v in result.values())
        logger.info(
            f"[提取分液瓶-板级] 完成: 覆盖 {len(result)} 个订单, 共 {total} 个分液瓶"
        )
        return result

    def _export_order_csv(self, final_result: Dict, csv_export_path: str) -> str:
        """
        将配液分液结果导出为 CSV 文件

        CSV 表头:
        orderCode, orderName, 配液瓶类型, 配液瓶二维码, 分液瓶类型, 分液瓶二维码,
        目标配液质量比, 真实配液质量比, 时间

        Args:
            final_result: _submit_and_wait_orders 返回的完整结果
            csv_export_path: CSV 文件保存目录路径

        Returns:
            生成的 CSV 文件完整路径
        """
        # 确保目录存在
        os.makedirs(csv_export_path, exist_ok=True)

        # 生成文件名
        time_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = os.path.join(csv_export_path, f"electrolyte_orders_{time_date}.csv")

        # 从 final_result 提取数据
        reports = final_result.get("reports", [])
        mass_ratios = final_result.get("mass_ratios", [])
        prep_bottles = final_result.get("prep_bottles", [])
        vial_bottles_all = final_result.get("vial_bottles", [])

        # 建立 orderCode → mass_ratio 的索引
        ratio_map = {}
        for ratio_item in mass_ratios:
            oc = ratio_item.get("orderCode")
            if oc:
                ratio_map[oc] = ratio_item

        # 建立 orderCode → prep_bottle 的索引
        prep_map = {}
        for pb in prep_bottles:
            if pb:
                oc = pb.get("orderCode")
                if oc:
                    prep_map[oc] = pb

        # 建立 orderCode → vial_bottles 的索引
        vial_map: Dict[str, List[Dict]] = {}
        for vb_list in vial_bottles_all:
            if isinstance(vb_list, list):
                for vb in vb_list:
                    oc = vb.get("orderCode")
                    if oc:
                        vial_map.setdefault(oc, []).append(vb)
            elif isinstance(vb_list, dict):
                oc = vb_list.get("orderCode")
                if oc:
                    vial_map.setdefault(oc, []).append(vb_list)

        export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"[CSV导出] 开始导出, 订单数={len(reports)}, 路径={csv_file}")

        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            # 写表头
            writer.writerow([
                "orderCode", "orderName",
                "配液瓶类型", "配液瓶二维码",
                "分液瓶类型", "分液瓶二维码",
                "目标配液质量比", "真实配液质量比",
                "各试剂允差", "总质量允差",
                "时间",
            ])

            for report in reports:
                order_code = report.get("orderCode", "N/A")
                order_name = report.get("orderName", "N/A")

                # 配液瓶信息
                prep_info = prep_map.get(order_code, {})
                prep_type = prep_info.get("typeName", "")
                prep_barcode = prep_info.get("barCode", "")

                # 分液瓶信息（可能多个）
                vial_list = vial_map.get(order_code, [])
                if len(vial_list) == 0:
                    vial_type_str = ""
                    vial_barcode_str = ""
                elif len(vial_list) == 1:
                    vial_type_str = vial_list[0].get("typeName", "")
                    vial_barcode_str = vial_list[0].get("barCode", "")
                else:
                    # 多个分液瓶用JSON数组表示
                    vial_type_str = json.dumps(
                        [v.get("typeName", "") for v in vial_list],
                        ensure_ascii=False,
                    )
                    vial_barcode_str = json.dumps(
                        [v.get("barCode", "") for v in vial_list],
                        ensure_ascii=False,
                    )

                # 质量比信息
                ratio_info = ratio_map.get(order_code, {})
                target_ratio = ratio_info.get("target_mass_ratio", {})
                real_ratio = ratio_info.get("real_mass_ratio", {})
                mass_tolerance = ratio_info.get("mass_tolerance", {})
                total_mass_tolerance = ratio_info.get("total_mass_tolerance", None)
                target_ratio_str = json.dumps(target_ratio, ensure_ascii=False) if target_ratio else ""
                real_ratio_str = json.dumps(real_ratio, ensure_ascii=False) if real_ratio else ""
                mass_tolerance_str = json.dumps(mass_tolerance, ensure_ascii=False) if mass_tolerance else ""
                total_mass_tolerance_str = "" if total_mass_tolerance is None else str(total_mass_tolerance)

                writer.writerow([
                    order_code, order_name,
                    prep_type, prep_barcode,
                    vial_type_str, vial_barcode_str,
                    target_ratio_str, real_ratio_str,
                    mass_tolerance_str, total_mass_tolerance_str,
                    export_time,
                ])

                logger.info(
                    f"[CSV导出] 写入: orderCode={order_code}, "
                    f"配液瓶={prep_type}({prep_barcode}), "
                    f"分液瓶数={len(vial_list)}"
                )

            f.flush()

        logger.info(f"[CSV导出] ✅ 导出完成: {csv_file}")
        return csv_file

    # ==================== 按 orderCode 导出实验报告（不依赖 order_finish 推送）====================

    # 与 _export_order_csv 保持同一套表头
    _ORDER_REPORT_CSV_HEADER = (
        "orderCode", "orderName",
        "配液瓶类型", "配液瓶二维码",
        "分液瓶类型", "分液瓶二维码",
        "目标配液质量比", "真实配液质量比",
        "各试剂允差", "总质量允差",
        "时间",
    )

    # 报告 Excel 里三类分液小瓶二维码列，按此优先级取首个非空
    _REPORT_VIAL_QR_COLUMNS = (
        "电导测试任务瓶二维码",
        "扣电组装小瓶二维码",
        "软包组装小瓶二维码",
    )

    # orderCode 形如 BSO + 8 位日期 + 流水号
    _ORDER_CODE_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z]+)(?P<date>\d{8})(?P<serial>\d+)$")

    # 区间一次最多展开多少单，防止笔误刷接口
    _ORDER_CODE_EXPAND_LIMIT = 500

    def _expand_order_codes(self, spec: str) -> List[str]:
        """把打印机页码式的订单号写法展开成 orderCode 列表

        支持三种写法，可用逗号混合：
        - 单个：``BSO2026072800006``
        - 全码区间：``BSO2026072800006-BSO2026072800029``
        - 尾号简写区间：``BSO2026072800006-29``、``BSO2026072800006-00029``

        Args:
            spec: 订单号表达式，逗号（半角/全角）分隔

        Returns:
            去重保序后的 orderCode 列表

        Raises:
            BioyondException: 表达式为空、格式无法识别、区间倒置或展开数超限
        """
        if not spec or not str(spec).strip():
            raise BioyondException("[导出实验报告] order_codes 不能为空")

        text = str(spec).replace("，", ",").replace("－", "-").replace("~", "-")
        codes: List[str] = []
        for raw in text.split(","):
            part = raw.strip()
            if not part:
                continue
            if "-" in part:
                codes.extend(self._expand_order_code_range(part))
            else:
                codes.append(part)

        seen = set()
        result: List[str] = []
        for code in codes:
            if code not in seen:
                seen.add(code)
                result.append(code)

        if not result:
            raise BioyondException(f"[导出实验报告] 未能从 {spec!r} 解析出任何订单号")
        return result

    def _expand_order_code_range(self, part: str) -> List[str]:
        """展开单个区间段，如 BSO2026072800006-29"""
        lo_text, _, hi_text = part.partition("-")
        lo_text, hi_text = lo_text.strip(), hi_text.strip()

        match_lo = self._ORDER_CODE_PATTERN.match(lo_text)
        if not match_lo:
            raise BioyondException(
                f"[导出实验报告] 区间起始订单号格式无法识别: {lo_text!r}，期望形如 BSO2026072800006"
            )
        prefix = match_lo.group("prefix")
        date = match_lo.group("date")
        serial = match_lo.group("serial")
        width = len(serial)
        start = int(serial)

        match_hi = self._ORDER_CODE_PATTERN.match(hi_text)
        if match_hi:
            if match_hi.group("prefix") != prefix or match_hi.group("date") != date:
                raise BioyondException(
                    f"[导出实验报告] 区间两端批次不一致({lo_text} ~ {hi_text})，跨批次请用逗号分开写"
                )
            end = int(match_hi.group("serial"))
        elif hi_text.isdigit():
            # 尾号简写：按右对齐塞回流水号，BSO2026072800006-29 -> 00029
            if len(hi_text) > width:
                raise BioyondException(
                    f"[导出实验报告] 区间末尾 {hi_text!r} 位数超过流水号宽度 {width}"
                )
            end = int(serial[: width - len(hi_text)] + hi_text)
        else:
            raise BioyondException(f"[导出实验报告] 区间末尾格式无法识别: {hi_text!r}")

        if end < start:
            raise BioyondException(f"[导出实验报告] 区间末尾小于起始: {lo_text} ~ {hi_text}")
        count = end - start + 1
        if count > self._ORDER_CODE_EXPAND_LIMIT:
            raise BioyondException(
                f"[导出实验报告] 区间 {part} 将展开 {count} 个订单，"
                f"超过上限 {self._ORDER_CODE_EXPAND_LIMIT}，请确认写法"
            )
        return [f"{prefix}{date}{str(n).zfill(width)}" for n in range(start, end + 1)]

    def _resolve_order_by_code(self, order_code: str) -> Optional[Dict[str, Any]]:
        """按 orderCode 精确定位订单摘要

        2.5 order-list 的 filter 是模糊匹配，可能带回同前缀的其他单；用
        preIntakes[*].sampleCode（形如 BSO2026072800006-00001）确认归属，
        比多调一次 order-report 拿 code 更省。

        Returns:
            命中的 order-list item，未命中返回 None
        """
        try:
            resp = self.order_list_v2(filter=order_code, pageCount=20)
        except Exception as e:
            logger.warning(f"[导出实验报告] order-list 查询失败: orderCode={order_code}, 错误={e}")
            return None

        items = ((resp or {}).get("data") or {}).get("items") or []
        for item in items:
            for pre_intake in item.get("preIntakes") or []:
                sample_code = pre_intake.get("sampleCode") or ""
                if sample_code == order_code or sample_code.startswith(f"{order_code}-"):
                    return item
        return None

    def _report_bottles_by_kind(
        self, report: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """从 order-report 的 sampleMaterials 拆出配液瓶与分液瓶（排除板）

        Returns:
            (配液瓶列表, 分液瓶列表)，元素含 typeName / barCode / materialCode
        """
        preps: List[Dict[str, Any]] = []
        vials: List[Dict[str, Any]] = []
        for pre_intake in report.get("preIntakes") or []:
            for sample_material in pre_intake.get("sampleMaterials") or []:
                type_name = (sample_material.get("materialTypeName") or "").strip()
                if not type_name or "板" in type_name:
                    continue
                entry = {
                    "typeName": type_name,
                    "barCode": sample_material.get("materialBarCode") or "",
                    "materialCode": sample_material.get("materialCode") or "",
                }
                if "配液瓶" in type_name:
                    preps.append(entry)
                elif "分液瓶" in type_name:
                    vials.append(entry)
        return preps, vials

    def _fetch_lims_report_excel(self, rel_path: str, save_dir: str) -> Optional[str]:
        """下载 LIMS 报告 Excel

        Args:
            rel_path: order-list 里 preIntakes[].extraProperties.reportFile 的相对路径
            save_dir: 落盘目录（留档便于人工核对）

        Returns:
            本地文件路径，失败返回 None
        """
        if not rel_path:
            return None
        url = self._url(re.sub(r"/{2,}", "/", str(rel_path)))
        try:
            resp = requests.get(url, timeout=self.bioyond_config.get("timeout", 30))
        except Exception as e:
            logger.warning(f"[导出实验报告] 报告 Excel 下载失败: {url}, 错误={e}")
            return None
        if resp.status_code != 200:
            logger.warning(f"[导出实验报告] 报告 Excel 返回 HTTP {resp.status_code}: {url}")
            return None

        os.makedirs(save_dir, exist_ok=True)
        local_path = os.path.join(save_dir, str(rel_path).rsplit("/", 1)[-1])
        with open(local_path, "wb") as f:
            f.write(resp.content)
        logger.info(f"[导出实验报告] 报告 Excel 已存: {local_path}")
        return local_path

    def _parse_lims_report_excel(self, path: str) -> Dict[str, Any]:
        """解析 LIMS 报告 Excel

        表头两行合并在同一单元格（``中文\\n英文``），取中文首行定位列；第 2 行起是数据，
        前段字段只在首行有值，组分按行铺开在「组分ID/目标加样质量/真实加样质量/允差」。

        Returns:
            含 prep_bottle_type / prep_bottle_barcode / vial_barcode / components 的字典，
            解析失败返回空字典
        """
        try:
            df = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl")
        except Exception as e:
            logger.warning(f"[导出实验报告] 报告 Excel 解析失败: {path}, 错误={e}")
            return {}
        if df.empty or len(df) < 2:
            logger.warning(f"[导出实验报告] 报告 Excel 内容为空: {path}")
            return {}

        header = [
            "" if value is None or pd.isna(value) else str(value).split("\n")[0].strip()
            for value in df.iloc[0].tolist()
        ]

        def cell(row_idx: int, col_name: str) -> Any:
            if col_name not in header:
                return None
            value = df.iloc[row_idx, header.index(col_name)]
            return None if pd.isna(value) else value

        result: Dict[str, Any] = {
            "prep_bottle_type": cell(1, "配液瓶类型"),
            "prep_bottle_barcode": cell(1, "配液瓶二维码"),
            "tray_barcode": cell(1, "托盘二维码"),
            "target_total_mass": cell(1, "目标加样总质量(g)"),
            "actual_total_mass": cell(1, "真实加样总质量(g)"),
            "vial_barcode": "",
            "vial_barcode_column": "",
            "components": [],
        }

        for col_name in self._REPORT_VIAL_QR_COLUMNS:
            value = cell(1, col_name)
            if value not in (None, ""):
                result["vial_barcode"] = str(value).strip()
                result["vial_barcode_column"] = col_name
                break

        for row_idx in range(1, len(df)):
            name = cell(row_idx, "组分ID")
            if name in (None, ""):
                continue
            result["components"].append({
                "name": str(name).strip(),
                "target": cell(row_idx, "目标加样质量(g)"),
                "actual": cell(row_idx, "真实加样质量(g)"),
            })
        return result

    @staticmethod
    def _mass_ratio_from_components(components: List[Dict[str, Any]], key: str) -> Dict[str, float]:
        """按组分质量算质量比，口径与 _process_order_reagents 一致"""
        values: Dict[str, float] = {}
        for comp in components:
            value = comp.get(key)
            if isinstance(value, (int, float)):
                values[comp["name"]] = float(value)
        total = sum(values.values())
        if not total:
            return {}
        return {name: round(value / total, 4) for name, value in values.items()}

    @staticmethod
    def _format_lims_time(value: Any) -> str:
        """LIMS 的 ISO 时间转 CSV 展示格式"""
        if not value:
            return ""
        try:
            return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return str(value)

    def export_order_report(self, order_codes: str, csv_export_path: str) -> Dict[str, Any]:
        """按 BSO 订单号导出配液实验报告 CSV

        与 create_orders 结束时的导出格式完全一致，但数据全部现查 LIMS，不依赖
        order_finish 推送，因此可以事后补任意历史批次。取数链路：

        1. 2.5 ``order-list`` 按 orderCode 查订单摘要（orderName / completeTime /
           reportFile 相对路径）；
        2. 2.6 ``order-report`` 按 orderId 查 sampleMaterials，取配液瓶/分液瓶的
           规范类型名（CSV 要的是「配液瓶(小)」，报告 Excel 里写的是「15mL配液瓶」）；
        3. 下载并解析报告 Excel，取配液瓶二维码、分液小瓶二维码，以及逐组分的目标/
           真实加样质量，据此算质量比与允差。

        真实加样质量以报告 Excel 为准：已验证它与瓶身 material-info 的 feedingHistory
        逐位一致，比 order_finish 推送的 realQuantity 更可靠。

        分液瓶二维码只从报告 Excel 取，缺失时留空并告警——查接口拿到的是瓶位当前状态，
        会被后续批次扫码覆盖、串到别单。

        Args:
            order_codes: 订单号表达式，支持单个、区间（``BSO2026072800006-BSO2026072800029``
                或尾号简写 ``BSO2026072800006-29``）、逗号跳选，可混写
            csv_export_path: CSV 导出目录（报告 Excel 存到其下 reports/ 子目录）

        Returns:
            {
                "csv_file": CSV 路径,
                "total": 请求单数,
                "resolved": 成功取到数据的单数,
                "not_found": 未查到的 orderCode 列表,
                "missing_vial_qr": 分液瓶二维码缺失的 orderCode 列表,
                "rows": 每行数据
            }
        """
        codes = self._expand_order_codes(order_codes)
        if not csv_export_path or not str(csv_export_path).strip():
            raise BioyondException("[导出实验报告] csv_export_path 不能为空")

        csv_export_path = str(csv_export_path).strip()
        os.makedirs(csv_export_path, exist_ok=True)
        report_dir = os.path.join(csv_export_path, "reports")

        logger.info(f"[导出实验报告] 订单号展开为 {len(codes)} 个: {codes}")

        rows: List[Dict[str, str]] = []
        not_found: List[str] = []
        missing_vial_qr: List[str] = []

        for idx, order_code in enumerate(codes, 1):
            logger.info(f"[导出实验报告] ({idx}/{len(codes)}) 处理 orderCode={order_code}")
            row = {key: "" for key in self._ORDER_REPORT_CSV_HEADER}
            row["orderCode"] = order_code

            summary = self._resolve_order_by_code(order_code)
            if not summary:
                logger.warning(f"[导出实验报告] LIMS 未查到订单: orderCode={order_code}，该行留空")
                not_found.append(order_code)
                rows.append(row)
                continue

            row["orderName"] = summary.get("name") or ""
            row["时间"] = self._format_lims_time(summary.get("completeTime"))
            if str(summary.get("status")) != "80":
                logger.warning(
                    f"[导出实验报告] 订单未成功完成: orderCode={order_code}, "
                    f"status={summary.get('status')}({summary.get('statusName')})，数据可能不完整"
                )

            # 瓶子规范类型名（配液瓶条码也可从这里兜底）
            order_id = summary.get("id") or summary.get("orderId") or ""
            report: Dict[str, Any] = {}
            if order_id:
                try:
                    report = (self.order_report_v2(order_id) or {}).get("data") or {}
                except Exception as e:
                    logger.warning(
                        f"[导出实验报告] order-report 查询失败: orderCode={order_code}, 错误={e}"
                    )
            preps, vials = self._report_bottles_by_kind(report)

            # 报告 Excel
            excel: Dict[str, Any] = {}
            for pre_intake in summary.get("preIntakes") or []:
                rel_files = ((pre_intake.get("extraProperties") or {}).get("reportFile")) or []
                for rel_path in rel_files:
                    local_path = self._fetch_lims_report_excel(rel_path, report_dir)
                    if local_path:
                        excel = self._parse_lims_report_excel(local_path)
                        break
                if excel:
                    break
            if not excel:
                logger.warning(
                    f"[导出实验报告] 无报告 Excel: orderCode={order_code}，质量比相关列留空"
                )

            row["配液瓶类型"] = (
                (preps[0]["typeName"] if preps else "") or str(excel.get("prep_bottle_type") or "")
            )
            row["配液瓶二维码"] = (
                str(excel.get("prep_bottle_barcode") or "") or (preps[0]["barCode"] if preps else "")
            )
            row["分液瓶类型"] = vials[0]["typeName"] if vials else ""
            row["分液瓶二维码"] = excel.get("vial_barcode") or ""
            if not row["分液瓶二维码"]:
                missing_vial_qr.append(order_code)
                logger.warning(
                    f"[导出实验报告] 分液瓶二维码缺失: orderCode={order_code}"
                    f"（报告 Excel 未写入，多为工站扫码失败）；接口现值是瓶位当前状态、"
                    f"会串到别单，故留空"
                )

            components = excel.get("components") or []
            target_ratio = self._mass_ratio_from_components(components, "target")
            real_ratio = self._mass_ratio_from_components(components, "actual")
            mass_tolerance: Dict[str, Optional[float]] = {}
            target_sum = 0.0
            real_sum = 0.0
            for comp in components:
                target, actual = comp.get("target"), comp.get("actual")
                if not isinstance(target, (int, float)) or not isinstance(actual, (int, float)):
                    continue
                target_sum += float(target)
                real_sum += float(actual)
                mass_tolerance[comp["name"]] = (
                    round((float(actual) - float(target)) / float(target), 6) if target else None
                )
            total_mass_tolerance = (
                round((real_sum - target_sum) / target_sum, 6) if target_sum else None
            )

            row["目标配液质量比"] = json.dumps(target_ratio, ensure_ascii=False) if target_ratio else ""
            row["真实配液质量比"] = json.dumps(real_ratio, ensure_ascii=False) if real_ratio else ""
            row["各试剂允差"] = json.dumps(mass_tolerance, ensure_ascii=False) if mass_tolerance else ""
            row["总质量允差"] = "" if total_mass_tolerance is None else str(total_mass_tolerance)

            rows.append(row)
            logger.info(
                f"[导出实验报告] orderCode={order_code}, orderName={row['orderName']}, "
                f"配液瓶={row['配液瓶类型']}({row['配液瓶二维码']}), "
                f"分液瓶={row['分液瓶类型']}({row['分液瓶二维码'] or '缺失'}), "
                f"组分数={len(components)}"
            )

        time_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = os.path.join(csv_export_path, f"electrolyte_orders_bycode_{time_date}.csv")
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(list(self._ORDER_REPORT_CSV_HEADER))
            for row in rows:
                writer.writerow([row.get(key, "") for key in self._ORDER_REPORT_CSV_HEADER])
            f.flush()

        resolved = len(codes) - len(not_found)
        logger.info(
            f"[导出实验报告] ✅ 导出完成: {csv_file}，"
            f"共 {len(codes)} 单，成功 {resolved} 单，"
            f"未查到 {len(not_found)} 单，分液瓶二维码缺失 {len(missing_vial_qr)} 单"
        )
        if not_found:
            logger.warning(f"[导出实验报告] 未查到的订单: {not_found}")
        if missing_vial_qr:
            logger.warning(f"[导出实验报告] 分液瓶二维码缺失的订单: {missing_vial_qr}")

        return {
            "csv_file": csv_file,
            "total": len(codes),
            "resolved": resolved,
            "not_found": not_found,
            "missing_vial_qr": missing_vial_qr,
            "rows": rows,
        }

    def _export_conductivity_csv(
        self,
        results: List[Dict[str, Any]],
        csv_export_path: str,
        filename_prefix: str,
    ) -> str:
        """
        导出电导-only CSV（external 与 inline 共用）。

        Args:
            results: _collect_conductivity_results 的返回（每瓶一条）
            csv_export_path: 导出目录
            filename_prefix: 文件名前缀，如 "External_conductivity" / "Inline_conductivity"

        Returns:
            生成的 CSV 文件完整路径

        列：分液瓶板条码 | 分液瓶二维码 | 内部瓶位置X | 内部瓶位置Y |
            目标温度 | 实际温度 | 电导值 | 电导率单位 | 时间
        """
        os.makedirs(csv_export_path, exist_ok=True)
        time_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = os.path.join(csv_export_path, f"{filename_prefix}_{time_date}.csv")

        logger.info(
            f"[电导CSV导出] 开始导出, 行数={len(results)}, 路径={csv_file}"
        )
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "分液瓶板条码", "分液瓶二维码",
                "内部瓶位置X", "内部瓶位置Y",
                "目标温度", "实际温度", "电导值", "电导率单位", "时间",
            ])
            for r in results:
                writer.writerow([
                    r.get("boardBarCode", ""),
                    r.get("bottleBarCode", ""),
                    r.get("bottleInnerX") if r.get("bottleInnerX") is not None else "",
                    r.get("bottleInnerY") if r.get("bottleInnerY") is not None else "",
                    r.get("targetTemperature") if r.get("targetTemperature") is not None else "",
                    r.get("temperature") if r.get("temperature") is not None else "",
                    r.get("conductivity") if r.get("conductivity") is not None else "",
                    r.get("conductivityUnit") if r.get("conductivityUnit") is not None else "",
                    r.get("report_time", ""),
                ])
            f.flush()

        logger.info(f"[电导CSV导出] ✅ 导出完成: {csv_file}")
        return csv_file

    def _export_conductivity_formulation_csv(
        self,
        results: List[Dict[str, Any]],
        mass_ratios: List[Dict[str, Any]],
        csv_export_path: str,
    ) -> str:
        """
        导出配液+电导合并 CSV（inline 专用）。

        按"分液瓶二维码"把电导结果与配液配方信息合并，一瓶一行（不再把多瓶
        条码合并成数组）。bottleBarCode 为空时回退用 (boardBarCode + X + Y) 匹配。

        Args:
            results: _collect_conductivity_results 的返回（每瓶一条电导结果）
            mass_ratios: 配液配方信息列表（来自配液 handle），每项含
                orderCode/orderName/target_mass_ratio/real_mass_ratio/mass_tolerance/
                total_mass_tolerance/prep_bottle_barcode/prep_bottle_type/
                vial_bottle_barcodes/vial_bottle_types
            csv_export_path: 导出目录

        Returns:
            生成的 CSV 文件完整路径

        列：orderCode | orderName | 配液瓶类型 | 配液瓶二维码 | 分液瓶类型 |
            分液瓶二维码 | 目标配液质量比 | 真实配液质量比 | 各试剂允差 |
            总质量允差 | 电导值 | 电导率单位 | 目标温度 | 实际温度 | 时间
        """
        os.makedirs(csv_export_path, exist_ok=True)
        time_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = os.path.join(csv_export_path, f"Formulation_Conductivity_{time_date}.csv")

        mass_ratios = mass_ratios or []

        def _parse_barcode_list(raw: Any) -> List[str]:
            """vial_bottle_barcodes 可能是单串或 JSON 数组串，统一成 list[str]。"""
            if raw is None:
                return []
            if isinstance(raw, list):
                return [str(x) for x in raw if x]
            s = str(raw).strip()
            if not s:
                return []
            if s.startswith("["):
                try:
                    arr = json.loads(s)
                    if isinstance(arr, list):
                        return [str(x) for x in arr if x]
                except Exception:
                    pass
            return [s]

        # 建立 分液瓶二维码 → 配液配方项 的索引（按瓶条码逐个展开）
        barcode_to_formula: Dict[str, Dict[str, Any]] = {}
        for mr in mass_ratios:
            if not isinstance(mr, dict):
                continue
            for bc in _parse_barcode_list(mr.get("vial_bottle_barcodes")):
                barcode_to_formula[bc] = mr

        logger.info(
            f"[配液电导CSV导出] 开始导出, 电导结果={len(results)} 条, "
            f"配方索引瓶数={len(barcode_to_formula)}, 路径={csv_file}"
        )

        export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        matched_cnt = 0
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "orderCode", "orderName",
                "配液瓶类型", "配液瓶二维码",
                "分液瓶类型", "分液瓶二维码",
                "目标配液质量比", "真实配液质量比",
                "各试剂允差", "总质量允差",
                "电导值", "电导率单位", "目标温度", "实际温度", "时间",
            ])
            for r in results:
                bottle_barcode = r.get("bottleBarCode", "") or ""
                # 主匹配：分液瓶二维码
                mr = barcode_to_formula.get(bottle_barcode) if bottle_barcode else None
                if mr is None:
                    # 回退：bottleBarCode 为空/未命中时，按 (板码 + X + Y) 反查
                    mr = self._match_formula_by_position(r, mass_ratios)
                if mr is not None:
                    matched_cnt += 1
                mr = mr or {}

                target_ratio = mr.get("target_mass_ratio", {})
                real_ratio = mr.get("real_mass_ratio", {})
                mass_tol = mr.get("mass_tolerance", {})
                total_tol = mr.get("total_mass_tolerance", None)

                writer.writerow([
                    mr.get("orderCode", ""),
                    mr.get("orderName", ""),
                    mr.get("prep_bottle_type", ""),
                    mr.get("prep_bottle_barcode", ""),
                    self._pick_vial_type(mr),
                    bottle_barcode,
                    json.dumps(target_ratio, ensure_ascii=False) if target_ratio else "",
                    json.dumps(real_ratio, ensure_ascii=False) if real_ratio else "",
                    json.dumps(mass_tol, ensure_ascii=False) if mass_tol else "",
                    "" if total_tol is None else str(total_tol),
                    r.get("conductivity") if r.get("conductivity") is not None else "",
                    r.get("conductivityUnit") if r.get("conductivityUnit") is not None else "",
                    r.get("targetTemperature") if r.get("targetTemperature") is not None else "",
                    r.get("temperature") if r.get("temperature") is not None else "",
                    r.get("report_time", export_time),
                ])
            f.flush()

        logger.info(
            f"[配液电导CSV导出] ✅ 导出完成: {csv_file} "
            f"(电导 {len(results)} 条, 命中配方 {matched_cnt} 条)"
        )
        return csv_file

    def _push_csv_to_datacore(self, csv_file: str, tag: str) -> bool:
        """
        把 CSV 推送到 DataCore 大装置数据接入，凭据取自 bioyond_config.datacore_config。
        未配置或推送失败都只记日志，不影响主流程。
        """
        return push_csv_by_config(
            csv_file, self.bioyond_config.get("datacore_config"), tag=tag, log=logger.info
        )

    def upload_csv_to_datacore(self, file_path: str = "") -> Dict[str, Any]:
        """
        手动把指定 CSV 推送到 DataCore 大装置数据接入

        用于补传历史文件或重传失败的文件。接入凭据取自 bioyond_config.datacore_config，
        无需在此填写。文件筛选由调用方在外部完成，这里一次只认一个完整路径。

        Args:
            file_path: 待上传文件的完整路径（主控电脑上的路径）

        Returns:
            dict: 包含 success / file_path / filename / message
        """
        file_path = (file_path or "").strip()
        if not file_path:
            raise BioyondException("[upload_csv_to_datacore] file_path 不能为空")
        if not os.path.isfile(file_path):
            raise BioyondException(f"[upload_csv_to_datacore] 文件不存在: {file_path}")

        ok = self._push_csv_to_datacore(file_path, tag="upload_csv_to_datacore")
        if not ok:
            raise BioyondException(
                f"[upload_csv_to_datacore] DataCore 推送失败: {file_path}；"
                f"请检查 bioyond_config.datacore_config 与网络连通性"
            )

        filename = os.path.basename(file_path)
        return {
            "success": True,
            "file_path": file_path,
            "filename": filename,
            "message": f"已推送到 DataCore: {filename}",
        }

    @staticmethod
    def _pick_vial_type(mr: Dict[str, Any]) -> str:
        """从配方项里取分液瓶类型；vial_bottle_types 可能是单串或 JSON 数组串。"""
        raw = mr.get("vial_bottle_types") if isinstance(mr, dict) else None
        if raw is None:
            return ""
        if isinstance(raw, list):
            return str(raw[0]) if raw else ""
        s = str(raw).strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                if isinstance(arr, list) and arr:
                    return str(arr[0])
            except Exception:
                pass
        return s

    @staticmethod
    def _match_formula_by_position(
        result_row: Dict[str, Any], mass_ratios: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        回退匹配：当电导结果的 bottleBarCode 为空/未命中时，
        按 (boardBarCode + bottleInnerX + bottleInnerY) 反查所属配方项。

        依赖配方项里的 vial_bottle_positions（建单阶段注入：板码 + X + Y → 瓶）。
        没有该映射时返回 None。
        """
        board = result_row.get("boardBarCode", "") or ""
        x = result_row.get("bottleInnerX")
        y = result_row.get("bottleInnerY")
        if not board or x is None or y is None:
            return None
        for mr in mass_ratios:
            if not isinstance(mr, dict):
                continue
            for pos in (mr.get("vial_bottle_positions") or []):
                if (
                    str(pos.get("plateBarCode", "")) == str(board)
                    and pos.get("x") == x
                    and pos.get("y") == y
                ):
                    return mr
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

            if response.get("error"):
                error_msg = f"查询物料详情失败: {response.get('error')}"
                logger.warning(f"[查询物料详情] ❌ {error_msg}")
                raise ValueError(error_msg)
            
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

    def _query_plate_bottle_positions(
        self, plate_material_id: str
    ) -> List[Dict[str, Any]]:
        """
        查询分液瓶板上所有分液瓶的孔位 (X, Y) 坐标。

        通过调 LIMS API 2.4 (/api/lims/storage/material-info) 拿到板的 detail[*]，
        每条 detail 即为一个孔位，按 (x, y) 取出所有有效孔位返回。

        2026-06-04 修：原先按 typeName 过滤"含'分液瓶'"在实际数据上失效——
        LIMS 端 detail.typeName 经常为空字符串/None，导致过滤后变 0 块。
        现在与 `_populate_vial_bottles`（资源树创建用的同一份返回）保持一致，
        只要 detail 项有 x/y 即视为孔位；典型情况下板上每条 detail 就是一个瓶位，
        过滤 typeName 含"分液瓶板"的条目以防 detail 里掺了子板（极罕见）。

        Args:
            plate_material_id: 分液瓶板 materialId (GUID)

        Returns:
            [{"x": int, "y": int, "typeName": str, "name": str,
              "detailMaterialId": str, "code": str, "associateId": str}, ...]
            associateId 为该瓶子关联的配液订单 GUID（跨 order 共用板时用于反查归属）

        Raises:
            BioyondException: detail 为空 / 全部条目缺 x/y
        """
        data = self._query_material_info(plate_material_id)
        detail = data.get("detail") or []
        logger.info(
            f"[查询瓶位] 板 {plate_material_id[:20]}... 的 detail 共 {len(detail)} 条原始记录"
        )
        bottles: List[Dict[str, Any]] = []
        for idx, d in enumerate(detail):
            if not isinstance(d, dict):
                logger.debug(f"[查询瓶位] detail[{idx}] 非 dict，跳过: {d!r}")
                continue
            type_name = (d.get("typeName") or "").strip()
            x_raw = d.get("x")
            y_raw = d.get("y")
            if x_raw is None or y_raw is None:
                logger.debug(
                    f"[查询瓶位] detail[{idx}] 缺 x/y，跳过: typeName={type_name!r}, "
                    f"x={x_raw}, y={y_raw}, code={d.get('code')!r}"
                )
                continue
            # 几乎不可能，但保护一下：detail 里若混入子板，跳过
            if "分液瓶板" in type_name:
                logger.debug(
                    f"[查询瓶位] detail[{idx}] typeName 含'分液瓶板'，跳过（避免把子板当瓶）: "
                    f"typeName={type_name!r}"
                )
                continue
            try:
                x_int = int(x_raw)
                y_int = int(y_raw)
            except (TypeError, ValueError):
                logger.warning(
                    f"[查询瓶位] detail[{idx}] x/y 不能转 int，跳过: x={x_raw!r}, y={y_raw!r}"
                )
                continue
            bottles.append({
                "x": x_int,
                "y": y_int,
                "typeName": type_name,
                "name": d.get("name", "") or "",
                "code": d.get("code", "") or "",
                "detailMaterialId": d.get("detailMaterialId", "") or "",
                "associateId": d.get("associateId", "") or "",
            })

        if not bottles:
            raise BioyondException(
                f"分液瓶板 {plate_material_id} 的 detail 中未找到任何有效孔位 "
                f"(原始 detail 长度={len(detail)})"
            )
        logger.info(
            f"[查询瓶位] ✅ 板 {plate_material_id[:20]}... 共提取 {len(bottles)} 个孔位: "
            f"{[(b['x'], b['y'], b['typeName'] or '<no_type>', (b['associateId'] or '<no_assoc>')[:8]) for b in bottles]}"
        )
        return bottles

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

        # 位置先解析：库位刷新可能已经把同一块板放到盘面上，这时不再新建、不再占格
        wh_name, slot_name = self._get_warehouse_and_slot_from_location_id(
            location_id
        )
        if not wh_name or not slot_name:
            logger.warning(
                f"[资源树] ⚠️ 无法解析位置: locationId={location_id}, "
                f"wh_name={wh_name}, slot_name={slot_name}"
            )
            return

        warehouse = self.deck.get_resource(wh_name)
        if not warehouse:
            logger.error(f"[资源树] ❌ 未找到仓库: {wh_name}")
            return

        def _hold_id(resource) -> Optional[str]:
            extra = getattr(resource, "unilabos_extra", {}) or {}
            return extra.get("material_bioyond_id") or getattr(resource, "unilabos_uuid", None)

        occupant = self._slot_occupant(warehouse, slot_name)
        existing_wh, existing_slot, existing = self._find_resource_by_hold_id(material_id)
        if occupant is not None and _hold_id(occupant) == material_id:
            logger.info(
                f"[资源树] {wh_name}[{slot_name}] 已是同一物料 {occupant.name}，"
                f"库位刷新已对齐，跳过占格"
            )
            return
        if existing is not None:
            logger.info(
                f"[资源树] 同物料已在 {existing_wh.name}[{existing_slot}] "
                f"({existing.name})，位置交给库位刷新，跳过再占 {wh_name}[{slot_name}]"
            )
            return
        if occupant is not None:
            logger.warning(
                f"[资源树] {wh_name}[{slot_name}] 已有其他物料 {occupant.name}，"
                f"不覆盖，交给库位刷新"
            )
            return

        # 盘面还没有这块板：创建并放入
        # 命名必须含**完整** materialId：同订单多块物理板同前缀，重名会触发
        # deck 的 "already assigned to deck"（只挂第1块，其余槽位留空 holder）。
        plate_name = f"vial_plate_{order_code}_{material_id}"
        if "5ml" in type_name.lower() or "5mL" in type_name:
            vial_plate_obj = YB_Vial_5mL_Carrier(name=plate_name)
            logger.debug(f"[资源树] 创建 YB_Vial_5mL_Carrier: {vial_plate_obj.name}")
        elif "20ml" in type_name.lower() or "20mL" in type_name:
            vial_plate_obj = YB_Vial_20mL_Carrier(name=plate_name)
            logger.debug(f"[资源树] 创建 YB_Vial_20mL_Carrier: {vial_plate_obj.name}")
        else:
            logger.warning(
                f"[资源树] ⚠️ 未知的分液瓶板类型: {type_name}, 跳过创建"
            )
            return

        vial_plate_obj.unilabos_uuid = material_id
        extra = getattr(vial_plate_obj, "unilabos_extra", None)
        if not isinstance(extra, dict):
            extra = {}
        extra["material_bioyond_id"] = material_id
        extra["material_bioyond_type"] = type_name
        vial_plate_obj.unilabos_extra = extra
        logger.debug(f"[资源树] 分配 UUID: {material_id[:30]}...")

        try:
            self._populate_vial_bottles(vial_plate_obj, material_id, order_code)
        except Exception as e:
            logger.warning(
                f"[资源树] ⚠️ 创建瓶子资源失败（继续创建瓶板）: {e}"
            )

        logger.debug(
            f"[资源树] 解析位置: locationId={location_id[:20]}... → "
            f"{wh_name}[{slot_name}]"
        )

        try:
            warehouse[slot_name] = vial_plate_obj
            self._register_resource_in_tracker(vial_plate_obj)
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
        skipped_no_code = 0
        for idx, bottle_info in enumerate(bottles_detail, 1):
            try:
                bottle_material_id = bottle_info.get("detailMaterialId")
                # 用 or 而非 get 的默认值：LIMS 会把空瓶位的 code 显式返回成 null，
                # 键存在时 get 的默认值不生效，直接拿到 None
                bottle_code = bottle_info.get("code") or ""
                bottle_x = bottle_info.get("x", 0)
                bottle_y = bottle_info.get("y", 0)
                associate_id = bottle_info.get("associateId")  # 关联订单ID
                
                if not bottle_material_id:
                    logger.warning(f"  瓶子[{idx}]: 缺少materialId，跳过")
                    continue
                
                # 无编号的瓶位是本单没分液到的空位：LIMS 里仍有子物料记录，但没分配
                # 编号，而下游电导/扣电是靠 barCode 认瓶的，认不出这种瓶子。给它编个
                # 名字挂进资源树反而可能被下游取到空瓶，所以直接跳过。
                if not bottle_code:
                    skipped_no_code += 1
                    logger.debug(
                        f"  瓶子[{idx}]: 位置=({bottle_x},{bottle_y}) 无编号（空瓶位），跳过"
                    )
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
        
        logger.info(
            f"[资源树] ✅ 已创建 {created_count}/{len(bottles_detail)} 个瓶子资源"
            + (f"，{skipped_no_code} 个空瓶位（无编号）已跳过" if skipped_no_code else "")
        )
    
    def transfer_3_to_2_to_1_auto(
        self,
        vial_plates: List[Dict],
        target_device: str = "BatteryStation",
        target_location: str = "bottle_rack_6x2",
        mass_ratios: List[Dict] = None,
        source_pos: Optional[Dict] = None,  # 可选：统一 xyz 覆盖（当 vial_plate 内无预存坐标时使用）
        **kwargs  # 兼容性参数，捕获已废弃的 vial_plate_info 等参数
    ) -> Dict[str, Any]:
        """
        自动转运（从 create_orders 结果自动定位源位置）

        本方法只负责在退出时释放 `stack_inquiry_2to1` 占下的交接堆栈预约，转运主体见
        `_transfer_3_to_2_to_1_auto_impl`。

        释放必须放在 `finally`：本 action 有两个出口——成功的 `return summary` 和有板搬
        失败时的 `raise BioyondException`（2026-08-05 那次 8688 走的正是 raise）。只在
        return 前释放的话，失败路径上预约会一直挂到兜底作废时长（小时级）到期，
        期间每一轮的 `stack_inquiry_2to1` 都卡在开头等预约 —— 一次转运失败升级成全站停摆。

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
        try:
            return self._transfer_3_to_2_to_1_auto_impl(
                vial_plates=vial_plates,
                target_device=target_device,
                target_location=target_location,
                mass_ratios=mass_ratios,
                source_pos=source_pos,
                **kwargs,
            )
        finally:
            # 到这里 321 已尘埃落定：搬成了则 0016 的占用已由 LIMS 库存如实反映，
            # 搬失败则 0016 本就没被本轮占。两种情况都不该再挡着下一轮的门控。
            self._release_handoff_claim(reason="321 转运结束")

    def _transfer_3_to_2_to_1_auto_impl(
        self,
        vial_plates: List[Dict],
        target_device: str = "BatteryStation",
        target_location: str = "bottle_rack_6x2",
        mass_ratios: List[Dict] = None,
        source_pos: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """`transfer_3_to_2_to_1_auto` 的转运主体，参数含义见其 docstring。"""
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
                f"materialId={plate.get('materialId', 'N/A')[:20]}..., "
                f"typeName={plate.get('typeName', 'N/A')}"
            )
        logger.info("=" * 80)

        # ========== 321 仅转运 5ml 分液瓶板（20ml 走 32 转运）==========
        PLATE_TYPE_5ML = "5ml分液瓶板"
        filtered_plates: List[Dict] = []
        skip_results: List[Dict] = []
        for idx, plate in enumerate(vial_plates, 1):
            if not plate or not isinstance(plate, dict):
                skip_results.append({
                    "index": idx,
                    "orderCode": "N/A",
                    "materialId": "N/A",
                    "status": "failed",
                    "error": "分液瓶板信息无效或为空",
                })
                continue

            type_name = (plate.get("typeName") or "").strip()
            material_id = plate.get("materialId") or ""
            order_code = plate.get("orderCode", "N/A")

            # typeName 缺失时兜底查一次物料详情
            if not type_name and material_id:
                try:
                    info = self._query_material_info(material_id)
                    type_name = (info.get("typeName") or "").strip()
                    plate["typeName"] = type_name
                    logger.info(
                        f"[批量转运] typeName 缺失，已补查: materialId={material_id[:20]}..., "
                        f"typeName={type_name}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[批量转运] typeName 缺失且补查失败: materialId={material_id[:20]}..., "
                        f"错误={e}，将按非5ml跳过"
                    )

            if PLATE_TYPE_5ML not in type_name:
                logger.info(
                    f"[批量转运] ℹ️ [{idx}/{len(vial_plates)}] 非5ml板，跳过321转运 "
                    f"(orderCode={order_code}, typeName={type_name or '空'})"
                )
                skip_results.append({
                    "index": idx,
                    "orderCode": order_code,
                    "materialId": material_id,
                    "status": "skipped",
                    "message": f"非5ml分液瓶板（typeName={type_name or '空'}），321仅转运5ml板",
                })
                continue

            filtered_plates.append(plate)

        if not filtered_plates:
            logger.warning(
                f"[transfer_3_to_2_to_1_auto] 过滤后无5ml分液瓶板可转运 "
                f"（原始 {len(vial_plates)} 块，全部跳过）"
            )
            return {
                "total": len(vial_plates),
                "success": 0,
                "failed": sum(1 for r in skip_results if r.get("status") == "failed"),
                "results": skip_results,
            }

        logger.info(
            f"[批量转运] 5ml过滤后保留 {len(filtered_plates)}/{len(vial_plates)} 块板"
        )

        # ========== 一轮只能转 1 块板：0016 只有 1 个库位 ==========
        # 下面的 for 循环形式上支持多块，实际必然在第 2 块上失败——第 1 块放进 0016 后它
        # 就满了，LIMS 对「目标交接堆栈有物料」的 321 直接拒建（返回 data=None）。
        # 而 0016 要等整个扣电流程走完、transfer_1_to_2 把板送回 0017 才腾空，
        # 这个循环内根本等不到。与其让第 2 块板报一句看不懂的 data=None，不如在这里拦掉。
        # 历史日志里 321 也确实从来只搬 1 块板（一轮 1–8 瓶正好装在同一块 8 位板上）。
        if len(filtered_plates) > 1:
            raise BioyondException(
                f"3-2-1 转运一次只能搬 1 块 5ml 分液瓶板，本次收到 {len(filtered_plates)} 块"
                f"（orderCode={[p.get('orderCode') for p in filtered_plates]}）。\n"
                f"原因：1号2号手套箱交接堆栈只有 1 个库位，且要占用到本轮扣电结束、"
                f"transfer_1_to_2 送回 2号手套箱内堆栈才腾空，所以第 2 块板必被 LIMS 拒建。\n"
                f"处置：把这批拆成多轮实验提交，每轮 1 块板（≤8 瓶）。"
            )

        # ========== 步骤2：依次转运每个分液瓶板（去重，同一瓶板只转运一次）==========
        results = list(skip_results)
        success_count = 0
        failed_count = sum(1 for r in skip_results if r.get("status") == "failed")
        transferred_material_ids = set()  # ✅ 记录已转运的materialId
        
        logger.info(
            f"[批量转运] 开始转运 {len(filtered_plates)} 块5ml分液瓶板 → "
            f"{target_device}.{target_location}"
        )
        
        for idx, plate_info in enumerate(filtered_plates, 1):
            try:
                material_id = plate_info.get('materialId')
                order_code = plate_info.get('orderCode', 'N/A')
                
                logger.info(f"\n{'='*60}")
                logger.info(f"[批量转运] 处理 [{idx}/{len(filtered_plates)}]")
                logger.info(f"  orderCode: {order_code}")
                logger.info(f"  materialId: {material_id[:20] if material_id else 'N/A'}...")
                logger.info(f"  typeName: {plate_info.get('typeName', 'N/A')}")
                
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
                
                # 调用单个转运逻辑（source_pos 作为兜底坐标，低于 plate_info 内预存 xyz）
                result = self._transfer_single_vial_plate(
                    vial_plate_info=plate_info,
                    target_device=target_device,
                    target_location=target_location,
                    source_pos_fallback=source_pos
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
                logger.info(f"[批量转运] ✅ [{idx}/{len(filtered_plates)}] 转运成功")
                
            except Exception as e:
                logger.error(
                    f"[批量转运] ❌ [{idx}/{len(filtered_plates)}] 失败: {str(e)}"
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

        # 有板没搬成功必须让本 action 失败：否则工作流会继续往下跑
        # transfer_1_to_2 / 扣电组装，而料根本没到位。
        if failed_count > 0:
            fail_details = "; ".join(
                f"{r.get('orderCode')}/{(r.get('materialId') or '')[:8]}: {r.get('error')}"
                for r in results if r.get("status") == "failed"
            )
            raise BioyondException(
                f"3-2-1 批量转运有 {failed_count}/{len(filtered_plates)} 块板失败: {fail_details}"
            )

        return summary
    
    def _transfer_single_vial_plate(
        self,
        vial_plate_info: Dict,
        target_device: str,
        target_location: str,
        source_pos_fallback: Optional[Dict] = None
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
            f"[自动转运] 分液瓶板资源树位置: {wh_name}[{slot_name}], "
            f"materialId={material_id}"
        )

        # 步骤2：确定物理来源。321 的来源仓库只能是自动堆栈-左（WH3），
        # 与 locationId 解析出的 wh_name 无关 —— 后者常是配液站内的临时槽位
        # （如 小分液瓶堆栈[C03]），拿它的名字去查 uuid + 按槽位标签硬算坐标
        # 会得到一个不存在的库位（C03→(3,3,1)），LIMS 直接不建单。
        warehouse_id = self._WH_ID_AUTO_STACK_LEFT

        # 步骤3：坐标优先实时查库定位（权威），失败再退到下单时的快照
        located = self._locate_material_in_warehouse(warehouse_id, material_id)
        if located:
            x, y, z = located
            logger.info(f"[自动转运] 使用实时定位坐标: ({x}, {y}, {z})")
        elif vial_plate_info.get("source_found"):
            x = vial_plate_info["source_x"]
            y = vial_plate_info["source_y"]
            z = vial_plate_info["source_z"]
            logger.warning(
                f"[自动转运] 实时定位未命中，退用下单时快照坐标: ({x}, {y}, {z})"
            )
        elif source_pos_fallback:
            x = source_pos_fallback.get("x", 1)
            y = source_pos_fallback.get("y", 1)
            z = source_pos_fallback.get("z", 1)
            logger.warning(f"[自动转运] 使用调用方指定的兜底坐标: ({x}, {y}, {z})")
        else:
            # 没有任何可信坐标时必须放弃：默认 (1,1,1) 指向 A01，
            # 那里很可能是另一块板，搬过去就是转错板。
            raise ValueError(
                f"分液瓶板不在自动堆栈-左，且无可信源坐标，321 无法转运: "
                f"materialId={material_id}, 资源树位置={wh_name}[{slot_name}]。"
                f"请确认配液已结束且该板已转运到自动堆栈-左"
            )

        # 步骤4：调用物理转运。失败会抛异常，由调用方记为 failed；
        # 绝不能在物理没动的情况下往下走资源树同步。
        lims_result = self.transfer_3_to_2_to_1(
            source_wh_id=warehouse_id,
            source_x=x,
            source_y=y,
            source_z=z
        )
        logger.info(f"[LIMS转运] 完成: {lims_result}")
        
        # 步骤5：资源树数字转运
        # warehouse[slot] 语义：满槽时 sites[idx] 已被替换为瓶板对象；
        # 空槽时仍是原始 ResourceHolder。绝不能把 holder 当瓶板转运。
        try:
            warehouse = self.deck.get_resource(wh_name)
            if not warehouse:
                raise ValueError(f"资源树中未找到仓库: {wh_name}")

            site_obj = warehouse[slot_name]
            if isinstance(site_obj, ResourceHolder):
                vial_plate = getattr(site_obj, "resource", None)
            else:
                vial_plate = site_obj

            if vial_plate is None or getattr(vial_plate, "unilabos_uuid", None) is None:
                logger.warning(
                    f"[资源同步] 槽位 {wh_name}[{slot_name}] 无有效瓶板对象"
                    f"（site={getattr(site_obj, 'name', site_obj)}, "
                    f"plate={getattr(vial_plate, 'name', None)}），跳过资源树同步"
                )
            else:
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
            resource_name: 资源名称（如 "bottle_rack_6x2"）

        Returns:
            找到的 PLR Resource 对象，未找到则返回 None
        """
        # 优先：通过全局设备注册表直接访问目标设备的 deck
        # DeviceInfoType 是 TypedDict（即普通 dict），必须用 dict.get() 而非 getattr()
        try:
            from unilabos.ros.nodes.base_device_node import registered_devices
            device_info = registered_devices.get(device_id)
            if device_info is not None:
                driver = device_info.get("driver_instance")
                if driver is not None:
                    deck = getattr(driver, "deck", None)
                    if deck is not None and hasattr(deck, "get_resource"):
                        try:
                            res = deck.get_resource(resource_name)
                            if res is not None:
                                return res
                        except Exception:
                            pass
        except Exception:
            pass

        # 降级：遍历 workstation 已注册的 plr_resources 列表（仅当前设备）
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

        配置缺失时返回空串。这里**不做**"回退到自动堆栈-左"的降级：
        拿别的仓库 uuid 配上本仓库的坐标去建单，等于让机械臂去另一个仓库的
        同名坐标取板，要么建单失败，要么直接搬走那个坐标上的别的板。

        Args:
            warehouse_name: 仓库名称，例如 "自动堆栈-左"

        Returns:
            warehouse_id；未配置时返回 ""
        """
        warehouse_mapping = self.bioyond_config.get("warehouse_mapping", {})
        wh_data = warehouse_mapping.get(warehouse_name, {})
        warehouse_id = wh_data.get("uuid") or ""

        if not warehouse_id:
            logger.error(
                f"仓库 '{warehouse_name}' 的 uuid 未在 warehouse_mapping 中配置，"
                f"无法用它作为转运来源仓库"
            )

        return warehouse_id

    def _snapshot_warehouse(self, wh_id: str) -> Optional[Dict[str, Any]]:
        """
        查 warehouse-info(2.38)，返回 data 段（含 name / locations）。

        Args:
            wh_id: 仓库 uuid

        Returns:
            data 字典；查询失败返回 None
        """
        resp = self._post_lims(
            "/api/lims/storage/warehouse-info",
            {"whId": wh_id, "includeDetail": True},
        )
        if not isinstance(resp, dict) or resp.get("code") != 1:
            logger.warning(f"[库位定位] 查询仓库 {wh_id} 失败: {resp}")
            return None
        return resp.get("data") or {}

    def _locate_material_in_warehouse(
        self, wh_id: str, material_id: str
    ) -> Optional[Tuple[int, int, int]]:
        """
        按 holdMId 在指定仓库中定位物料，返回其 (x, y, z)。

        这是转运源坐标的权威来源：配液报告里的坐标是**下单时**的快照，板子在
        配液结束后才被搬进自动堆栈-左，快照往往对不上或压根没有该仓库的记录。

        Args:
            wh_id: 仓库 uuid
            material_id: 物料（瓶板）的 materialId

        Returns:
            命中则返回 (x, y, z)；仓库里没有这块板或查询失败则返回 None
        """
        data = self._snapshot_warehouse(wh_id)
        if data is None:
            return None

        wh_display = f"{data.get('name') or wh_id}"
        for loc in (data.get("locations") or []):
            if loc.get("holdMId") == material_id:
                x = int(loc.get("x") or 1)
                y = int(loc.get("y") or 1)
                z = int(loc.get("z") or 1)
                logger.info(
                    f"[库位定位] ✅ 在 {wh_display} 命中: 库位={loc.get('code')}, "
                    f"坐标=({x},{y},{z}), 物料={loc.get('holdMName')} "
                    f"({loc.get('holdMTypeName')})"
                )
                return (x, y, z)

        occupied = [
            f"{loc.get('code')}={loc.get('holdMTypeName') or '空'}"
            for loc in (data.get("locations") or [])
        ]
        logger.warning(
            f"[库位定位] ❌ {wh_display} 中未找到 materialId={material_id}；"
            f"当前占用情况: {occupied}"
        )
        return None
    
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


    def _list_plates_in_warehouse(
        self, wh_id: str, type_keyword: str = "分液瓶板"
    ) -> Optional[List[Dict[str, Any]]]:
        """列出仓库里被指定板型占用的库位。查询失败返回 None（供调用方容错重试）。

        判定口径与现有代码一致：holdMId 非空 + holdMTypeName 含 type_keyword。
        """
        data = self._snapshot_warehouse(wh_id)
        if data is None:
            return None
        return [
            loc for loc in (data.get("locations") or [])
            if isinstance(loc, dict)
            and (loc.get("holdMId") or "").strip()
            and type_keyword in (loc.get("holdMTypeName") or "")
        ]

    def _wait_stack_condition(
        self,
        wh_id: str,
        mode: str,
        type_keyword: str = "分液瓶板",
        material_ids: Optional[List[str]] = None,
        min_count: int = 1,
        poll_interval: float = 5.0,
        timeout: int = 3600,
        label: str = "库位门控",
    ) -> Dict[str, Any]:
        """轮询 warehouse-info(2.38)，直到指定仓库满足条件；超时抛 BioyondException。

        各库位门控的共用底座，避免每个场景重复写轮询/容错/日志。

        Args:
            wh_id: 目标仓库 uuid（只用 whId 定位，code 会与 materialTypeCode 撞）
            mode: "empty_of" 等到该板型清空；"occupied_by" 等到命中指定物料/数量
            type_keyword: 板型关键字
            material_ids: 给定则按 holdMId 精确匹配（occupied_by 时要求全部到位）
            min_count: occupied_by 且未给 material_ids 时，要求命中的最少库位数
            poll_interval: 轮询间隔（秒）
            timeout: 最长等待（秒）
            label: 日志前缀

        Returns:
            {"status": "ok", "matched": [...库位摘要], "poll_count": int, "elapsed_seconds": float}
        """
        want_ids = {mid for mid in (material_ids or []) if mid}
        start = time.time()
        poll_count = 0

        while True:
            plates = self._list_plates_in_warehouse(wh_id, type_keyword)
            elapsed = time.time() - start

            if plates is None:
                # 单次查询失败只告警重试，不中断轮询（对齐 monitor_manual_stack_3 的容错风格）
                if elapsed > timeout:
                    raise BioyondException(
                        f"[{label}] 查询仓库 {wh_id} 持续失败且已超时（{timeout}s）"
                    )
                logger.warning(f"[{label}] 查询仓库 {wh_id} 失败，{poll_interval}s 后重试…")
                poll_count += 1
                time.sleep(poll_interval)
                continue

            summary = [
                {
                    "code": loc.get("code"),
                    "holdMId": loc.get("holdMId"),
                    "holdMName": loc.get("holdMName"),
                    "holdMTypeName": loc.get("holdMTypeName"),
                }
                for loc in plates
            ]

            if mode == "empty_of":
                satisfied = not plates
            elif mode == "occupied_by":
                got_ids = {(loc.get("holdMId") or "").strip() for loc in plates}
                satisfied = want_ids <= got_ids if want_ids else len(plates) >= min_count
            else:
                raise ValueError(f"[{label}] 未知 mode: {mode!r}，只支持 empty_of / occupied_by")

            if satisfied:
                logger.info(
                    f"[{label}] ✅ 条件满足（mode={mode}，轮询 {poll_count} 次，耗时 {elapsed:.1f}s）"
                )
                return {
                    "status": "ok",
                    "matched": summary,
                    "poll_count": poll_count,
                    "elapsed_seconds": round(elapsed, 1),
                }

            if elapsed > timeout:
                raise BioyondException(
                    f"[{label}] 等待超时（{timeout}s，mode={mode}）。"
                    f"当前占用: {summary or '空'}"
                    + (f"；期望 materialId: {sorted(want_ids)}" if want_ids else "")
                )

            poll_count += 1
            logger.info(
                f"[{label}] 条件未满足（mode={mode}），当前占用 {len(plates)} 个库位: "
                f"{[loc.get('code') for loc in plates]}，"
                f"{poll_interval}s 后重试…（已等待 {elapsed:.1f}s）"
            )
            time.sleep(poll_interval)

    def _acquire_handoff_claim(
        self, task_hint: str, claim_timeout: float
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """尝试占据交接堆栈预约。已有未消费的预约则占据失败（调用方继续轮询）。

        预约语义 =「本轮已查到 1号2号交接堆栈空、但 321 还没把板放进去」。它填的是
        stack_inquiry_2to1 放行到 LIMS 库存显示占用之间的空窗（2026-08-05 实测 380ms：
        放行 17:09:40.868 → 321 开始放板 17:09:41.247），否则下一轮会同样看到「物理空」
        而放行，其 321 必被 LIMS 拒（交接堆栈有物料时 321 直接拒建，已与 LIMS 开发者确认）。

        claim_timeout 只兜进程崩溃这类未知情况；321 结束（成功或失败）由
        transfer_3_to_2_to_1_auto 的 finally 主动释放，不能靠超时干等。
        """
        now = time.time()
        with self._interlock_lock:
            claim = self._handoff_claim
            if claim is not None:
                # granted_at 缺失/非法时按「无限旧」处理，让它立刻作废。
                # 不能回落成 now——那样一张时间戳损坏的预约会永远不过期，把整站锁死。
                granted_at = claim.get("granted_at")
                age = now - (float(granted_at) if isinstance(granted_at, (int, float)) else 0.0)
                if age <= claim_timeout:
                    return False, claim
                logger.error(
                    f"[交接堆栈预约] 超时作废（已持有 {age:.0f}s > {claim_timeout:.0f}s，"
                    f"token={claim.get('token')}, 持有者={claim.get('task_hint')}）。"
                    f"说明拿到放行权的任务在 321 转运前就异常退出了，请检查上一个任务的日志。"
                )
            token = uuid.uuid4().hex
            self._handoff_claim = {
                "token": token,
                "granted_at": now,
                "task_hint": task_hint,
            }
            return True, self._handoff_claim

    def _release_handoff_claim(self, reason: str = "") -> None:
        """释放交接堆栈预约，由 transfer_3_to_2_to_1_auto 的 finally 调用。

        ⚠️ 与 `_release_interlock_claim` 不同，这里**不校验 token**：那边的占与放在同一个
        action 内、拿得到 token，而这里是 stack_inquiry_2to1 占、transfer_3_to_2_to_1_auto
        放，后者拿不到前者的 token（两个 action 参数分开，靠工作流传 token 太丑）。
        无条件释放可接受：321 紧跟门控执行（实测相隔数秒），而作废时长是小时级，
        中间不可能过期后被别人重新占据。持有者打进日志，对不上便于事后对账。
        """
        with self._interlock_lock:
            claim = self._handoff_claim
            if claim is None:
                logger.warning(
                    f"[交接堆栈预约] 释放时发现无预约在手（{reason}）——"
                    f"可能是工作流漏配 stack_inquiry_2to1，或预约已超时作废"
                )
                return
            self._handoff_claim = None
        logger.info(
            f"[交接堆栈预约] 已释放 token={claim.get('token')}, "
            f"持有者={claim.get('task_hint')}（{reason}）"
        )

    def _peek_handoff_claim(self, claim_timeout: float) -> Optional[Dict[str, Any]]:
        """只读查看当前是否有**未过期**的交接堆栈预约在手，不占据、不清除。

        与 `_acquire_handoff_claim` 的过期判定保持一致：granted_at 缺失/非法按「无限旧」
        处理，视为已过期。过期的预约在这里只当作 None，实际作废由 acquire 负责并记日志。
        """
        now = time.time()
        with self._interlock_lock:
            claim = self._handoff_claim
            if claim is None:
                return None
            granted_at = claim.get("granted_at")
            age = now - (float(granted_at) if isinstance(granted_at, (int, float)) else 0.0)
            return claim if age <= claim_timeout else None

    def _wait_handoff_claim_free(
        self, poll_interval: float, timeout: int, claim_timeout: float
    ) -> None:
        """等上一轮的交接堆栈预约被消费掉（即它的 321 已放完板）。

        放在 stack_inquiry_2to1 查库存**之前**。因为 stack_inquiry_2to1 是单个 action、
        单条 FIFO 队列，同时只有一个门控在跑（实测 8688 的门控 17:06:54 queued、
        等 faef 的门控完成后的 17:09:41 才 started），所以此处只需防「上一轮门控已放行、
        其 321 还没放板」这一种情况，不存在两个门控并发抢预约——也正因如此，这里
        「先查空再占」不构成 TOCTOU：中间没有第二个门控能插进来抢。

        预约只从门控放行持有到 321 放完板（实测约 7s），故这里通常只等数秒。
        """
        start = time.time()
        while True:
            holder = self._peek_handoff_claim(claim_timeout)
            if holder is None:
                return
            elapsed = time.time() - start
            if elapsed > timeout:
                granted_at = holder.get("granted_at")
                held = time.time() - (
                    float(granted_at) if isinstance(granted_at, (int, float)) else time.time()
                )
                raise BioyondException(
                    f"[stack_inquiry_2to1] 等上一轮交接堆栈预约释放超时（{timeout}s）："
                    f"持有者={holder.get('task_hint')}，已持有 {held:.0f}s。"
                    f"说明上一轮门控已放行但 321 迟迟未完成，"
                    f"请检查上一个任务的 transfer_3_to_2_to_1_auto 日志"
                )
            logger.info(
                f"[stack_inquiry_2to1] 上一轮（{holder.get('task_hint')}）已放行但 321 尚未放完板，"
                f"交接堆栈使用权未释放，{poll_interval}s 后重试…（已等待 {elapsed:.1f}s）"
            )
            time.sleep(poll_interval)

    def _take_handoff_claim(
        self, task_hint: str, poll_interval: float, timeout: int, claim_timeout: float
    ) -> str:
        """门控放行前占下交接堆栈预约，占不到就继续轮询。返回预约 token（仅供日志）。

        正常情况下开头的 `_wait_handoff_claim_free` 已确保无人持有、这里一次即中；
        循环只是防「查库存期间上一轮又抢到了」的理论情形，不能直接放行了事。
        """
        start = time.time()
        while True:
            ok, claim = self._acquire_handoff_claim(task_hint, claim_timeout)
            if ok:
                token = str((claim or {}).get("token"))
                logger.info(
                    f"[交接堆栈预约] 已占据 token={token}, 持有者={task_hint}"
                    f"（放行 321，将由 transfer_3_to_2_to_1_auto 释放）"
                )
                return token
            elapsed = time.time() - start
            if elapsed > timeout:
                raise BioyondException(
                    f"[stack_inquiry_2to1] 交接堆栈物理上已可用，但占据使用权超时（{timeout}s）："
                    f"被 {(claim or {}).get('task_hint')} 持有。"
                    f"请检查该任务的 transfer_3_to_2_to_1_auto 是否卡住"
                )
            logger.info(
                f"[stack_inquiry_2to1] 交接堆栈已空但使用权被 {(claim or {}).get('task_hint')} "
                f"抢先占据，{poll_interval}s 后重试…（已等待 {elapsed:.1f}s）"
            )
            time.sleep(poll_interval)

    def stack_inquiry_2to1(
        self,
        poll_interval: float = 5.0,
        timeout: int = 3600,
        type_keyword: str = "分液瓶板",
        match_by_material_id: bool = True,
        target_wh_id: str = "",
        task_hint: str = "",
    ) -> Dict[str, Any]:
        """321 转运的**前置**门控：确认 1号2号交接堆栈(0016) 可以放新板，并占下使用权。

        必须放在 `transfer_3_to_2_to_1_auto` **之前**——321 会把新板放进 0016，
        而 0016 只有 1 个库位，所以放之前必须确认它是空的。放到 321 之后就退化成
        一句无用查询，不会报错，只是保护静默失效。

        判定分两条路：

        1. **0016 本就空 → 立即放行**（fast_path，不查 0017）。首轮 0016/0017 都空，
           若强行要求「0017 必须有板」会永久阻塞
        2. **0016 有板** → 快照这些 holdMId（这就是上一轮待回流的板），等它们既
           离开 0016、又出现在 2号内部堆栈(0017)。「0016 腾空」与「0017 到位」是
           同一块板的两面，用 materialId 串起来才能确认板真的回流到位，
           而不是刚离开 0016 还在途中

        ⚠️ **光查物理占用不够，必须同时占下预约**（2026-08-05 实测踩过）：本函数放行到
        321 真把板放进 0016 之间，LIMS 库存显示的还是「空」（实测 380ms：放行
        17:09:40.868 → 321 开始放板 17:09:41.247 → 库存变占用 17:09:48.067）。那一瞬
        下一轮的门控查到的是**过期的真话**——使用权其实已被上一轮拿走。而 321 是同一条
        `(device, action)` FIFO 队列，下一轮的 321 必然排在上一轮之后执行（实测
        17:09:41.309 queued → 17:09:48.172 started），届时 0016 一定已被占，
        LIMS 直接拒建（交接堆栈有物料则拒绝 321，已与 LIMS 开发者确认）。
        所以失败是**确定的**而非概率性的。修法：开头先等上一轮预约被消费，
        放行时占下自己的预约，由 `transfer_3_to_2_to_1_auto` 的 finally 释放。

        Args:
            poll_interval: 轮询间隔（秒）
            timeout: 最长等待（秒），超时抛 BioyondException。注意它**不是**交接堆栈预约的
                作废时长，后者是固定的 `_HANDOFF_CLAIM_TTL_SECONDS`
            type_keyword: 板型关键字。默认 `分液瓶板` 会**同时匹配 5ml 和 20ml**；
                扣电路应显式传 `5ml分液瓶板`。否则 0016 里若停着一块 20ml 板
                （它走 3→2，不经 1→2 回 0017），「到位 0017」这个判据永远不成立，
                只能等到超时
            match_by_material_id: 是否按 materialId 精确追踪回流。置 False 退化为
                只要 0016 腾空即放行（旧行为）
            target_wh_id: 回流目标仓库，默认 2号内部堆栈(0017)
            task_hint: 调用方标识，仅用于日志和预约归属

        Returns:
            {"status": "clear", "fast_path": bool, "pending_material_ids": [...],
             "arrived_material_ids": [...], "poll_count": int, "elapsed_seconds": float}

        Raises:
            BioyondException: 查询失败 / 等待超时
        """
        target_wh_id = target_wh_id or self._WH_ID_STACK_2_INNER
        hint = task_hint or "<未命名任务>"
        start = time.time()

        # 先等上一轮的预约被消费（它的 321 已放完板），否则会查到过期的「物理空」
        self._wait_handoff_claim_free(poll_interval, timeout, self._HANDOFF_CLAIM_TTL_SECONDS)

        plates = self._list_plates_in_warehouse(self._WH_ID_TRANSFER_2TO1, type_keyword)
        if plates is None:
            raise BioyondException(
                f"查询 1号2号手套箱交接堆栈失败（whId={self._WH_ID_TRANSFER_2TO1}）"
            )

        # ---------- 快速路径：0016 本来就空 ----------
        if not plates:
            remain = max(int(timeout - (time.time() - start)), 1)
            claim_token = self._take_handoff_claim(
                hint, poll_interval, remain, self._HANDOFF_CLAIM_TTL_SECONDS
            )
            logger.info(
                f"[stack_inquiry_2to1] ✅ 1号2号交接堆栈已无「{type_keyword}」，直接放行"
                f"（快速路径，未查 0017）"
            )
            return {
                "status": "clear",
                "fast_path": True,
                "pending_material_ids": [],
                "arrived_material_ids": [],
                "poll_count": 0,
                "elapsed_seconds": round(time.time() - start, 1),
                "claim_token": claim_token,
            }

        pending_ids = sorted({
            (loc.get("holdMId") or "").strip()
            for loc in plates if (loc.get("holdMId") or "").strip()
        })
        logger.info(
            f"[stack_inquiry_2to1] 0016 仍有 {len(plates)} 块「{type_keyword}」"
            f"（库位 {[loc.get('code') for loc in plates]}），"
            f"快照待回流 materialId: {pending_ids}"
        )

        # ---------- 步骤 1：等 0016 腾空 ----------
        remain = max(int(timeout - (time.time() - start)), 1)
        try:
            self._wait_stack_condition(
                wh_id=self._WH_ID_TRANSFER_2TO1,
                mode="empty_of",
                type_keyword=type_keyword,
                poll_interval=poll_interval,
                timeout=remain,
                label="stack_inquiry_2to1/等 0016 腾空",
            )
        except BioyondException as exc:
            # 只有 1 个库位的交接堆栈占住不动 = 全站阻塞：后续每一轮的 321 都进不来。
            # 底层报错只说「等待超时 + 当前占用」，这里补上「谁占的、占了多久、怎么解」，
            # 否则值班的人拿到异常还得自己去翻 warehouse-info 才知道下一步做什么。
            stuck = [
                f"{loc.get('code')}(holdMId={loc.get('holdMId')}, "
                f"名称={loc.get('holdMName')}, 类型={loc.get('holdMTypeName')})"
                for loc in (
                    self._list_plates_in_warehouse(self._WH_ID_TRANSFER_2TO1, type_keyword) or []
                )
            ]
            raise BioyondException(
                f"{exc}\n"
                f"⚠️ 1号2号手套箱交接堆栈只有 1 个库位，被占住 {time.time() - start:.0f}s 未动，"
                f"后续所有轮次的 321 转运都会被 LIMS 拒建 → 全站阻塞。\n"
                f"当前占用: {stuck or '空（占用刚好在报错前被清掉，可直接重跑本节点）'}\n"
                f"处置：确认上一轮扣电已完成后，手工执行 transfer_1_to_2 把该板送回"
                f"2号手套箱内堆栈；若该板本轮无需回流（如走 3→2 的 20ml 电导板），"
                f"应改传 type_keyword='5ml分液瓶板' 把它排除在判据外。"
            ) from exc

        if not match_by_material_id or not pending_ids:
            remain = max(int(timeout - (time.time() - start)), 1)
            claim_token = self._take_handoff_claim(
                hint, poll_interval, remain, self._HANDOFF_CLAIM_TTL_SECONDS
            )
            logger.info("[stack_inquiry_2to1] ✅ 0016 已腾空，未启用 materialId 追踪，放行")
            return {
                "status": "clear",
                "fast_path": False,
                "pending_material_ids": pending_ids,
                "arrived_material_ids": [],
                "poll_count": 0,
                "elapsed_seconds": round(time.time() - start, 1),
                "claim_token": claim_token,
            }

        # ---------- 步骤 2：等快照的板到位 0017 ----------
        # 有的板可能被人工挪走/出库，既不在 0016 也不在 0017。这类视为已离场，
        # 不再要求它到位，否则会白等到超时。
        arrived_locs = self._list_plates_in_warehouse(target_wh_id, type_keyword) or []
        arrived_ids = {(loc.get("holdMId") or "").strip() for loc in arrived_locs}
        missing = [mid for mid in pending_ids if mid not in arrived_ids]

        if missing:
            remain = max(int(timeout - (time.time() - start)), 1)
            try:
                self._wait_stack_condition(
                    wh_id=target_wh_id,
                    mode="occupied_by",
                    type_keyword=type_keyword,
                    material_ids=pending_ids,
                    poll_interval=poll_interval,
                    timeout=remain,
                    label="stack_inquiry_2to1/等回流到位 0017",
                )
            except BioyondException:
                # 超时前再确认一次：只要 0016 已腾空、且失联的板确实哪儿都不在，
                # 就按「已离场」放行并告警，不把流程卡死在一块被人搬走的板上
                still_016 = self._list_plates_in_warehouse(
                    self._WH_ID_TRANSFER_2TO1, type_keyword
                ) or []
                final_017 = self._list_plates_in_warehouse(target_wh_id, type_keyword) or []
                final_ids = {(loc.get("holdMId") or "").strip() for loc in final_017}
                lost = [mid for mid in pending_ids if mid not in final_ids]
                if still_016 or not lost:
                    raise
                logger.warning(
                    f"[stack_inquiry_2to1] ⚠️ 0016 已腾空，但 materialId {lost} 既不在 0016 "
                    f"也不在 0017（疑似被人工挪走/出库），按已离场放行"
                )

        final_017 = self._list_plates_in_warehouse(target_wh_id, type_keyword) or []
        arrived = sorted({
            (loc.get("holdMId") or "").strip() for loc in final_017
        } & set(pending_ids))
        remain = max(int(timeout - (time.time() - start)), 1)
        claim_token = self._take_handoff_claim(
                hint, poll_interval, remain, self._HANDOFF_CLAIM_TTL_SECONDS
            )
        logger.info(
            f"[stack_inquiry_2to1] ✅ 0016 已腾空且 {len(arrived)}/{len(pending_ids)} 块板"
            f"已回流到 0017，放行（耗时 {time.time() - start:.1f}s）"
        )
        return {
            "status": "clear",
            "fast_path": False,
            "pending_material_ids": pending_ids,
            "arrived_material_ids": arrived,
            "poll_count": 0,
            "elapsed_seconds": round(time.time() - start, 1),
            "claim_token": claim_token,
        }

    def stack_inquiry_window_5_free(
        self,
        required_slots: int = 4,
        poll_interval: float = 5.0,
        timeout: int = 36000,
        type_keyword: str = "",
    ) -> Dict[str, Any]:
        """建单**前置**门控：等 5号自动传递窗(0018) 的空闲库位数 ≥ required_slots。

        必须放在 `create_orders` **之前**：配液流程结束时电导板已经直接堆在 0018 上，
        等「到位」没有意义；真正的瓶颈是 0018 只有 4 个库位，而一块板对应一个温度点。
        上一轮的板测完转去 0027、腾出空位后，下一轮才能建单。放到 create_orders
        之后不会报错，只是保护静默失效。

        Args:
            required_slots: 本轮需要的库位数 = 本轮温度点数（1–4），应与传给
                `conductivity_test_inline` 的 `temperature_points` 长度一致。
                默认 4 是最保守取值（等 0018 全空），按实际温度点数传可少等
            poll_interval: 轮询间隔（秒）
            timeout: 最长等待（秒），默认 10 小时，匹配电导测试时长
            type_keyword: 只统计含该关键字的板占用；留空则任何物料都算占位
                （0018 是限量通道，默认按最保守的口径算）

        Returns:
            {"status": "ok", "free_slots": int, "required_slots": int,
             "total_slots": int, "poll_count": int, "elapsed_seconds": float}

        Raises:
            ValueError: required_slots 超出 1–4
            BioyondException: 查询持续失败 / 等待超时
        """
        if not 1 <= required_slots <= self._WINDOW_5_CAPACITY:
            raise ValueError(
                f"required_slots 必须在 1–{self._WINDOW_5_CAPACITY} 之间（当前 {required_slots}）。"
                f"5号自动传递窗只有 {self._WINDOW_5_CAPACITY} 个库位，一块板一个温度点，"
                f"超过就得拆轮做，否则会建出永远排不进传递窗的单。"
            )

        start = time.time()
        poll_count = 0
        warned_misplaced = False

        while True:
            data = self._snapshot_warehouse(self._WH_ID_TRANSFER_WINDOW_5)
            elapsed = time.time() - start

            if data is None:
                if elapsed > timeout:
                    raise BioyondException(f"查询 5 号自动传递窗持续失败且已超时（{timeout}s）")
                logger.warning(f"[stack_inquiry_window_5_free] 查询失败，{poll_interval}s 后重试…")
                poll_count += 1
                time.sleep(poll_interval)
                continue

            locations = data.get("locations") or []
            occupied = [
                loc for loc in locations
                if isinstance(loc, dict) and (loc.get("holdMId") or "").strip()
                and (not type_keyword or type_keyword in (loc.get("holdMTypeName") or ""))
            ]
            total_slots = len(locations) or self._WINDOW_5_CAPACITY
            free_slots = total_slots - len(occupied)

            if free_slots >= required_slots:
                logger.info(
                    f"[stack_inquiry_window_5_free] ✅ 5 号自动传递窗空闲 {free_slots}/{total_slots} 个库位，"
                    f"满足本轮所需 {required_slots} 个，放行"
                    f"（轮询 {poll_count} 次，耗时 {elapsed:.1f}s）"
                )
                return {
                    "status": "ok",
                    "free_slots": free_slots,
                    "required_slots": required_slots,
                    "total_slots": total_slots,
                    "poll_count": poll_count,
                    "elapsed_seconds": round(elapsed, 1),
                }

            # 位置可疑告警：本门控是建单前置，进来时 0018 就满说明很可能被摆到了
            # create_orders 之后——那时板已经堆进去了，门控守了个寂寞
            if not warned_misplaced and poll_count == 0 and free_slots == 0:
                warned_misplaced = True
                logger.warning(
                    f"[stack_inquiry_window_5_free] ⚠️ 进入时 5 号自动传递窗已被占满"
                    f"（{len(occupied)}/{total_slots}）。若本轮的电导板已经在里面，说明本门控"
                    f"被放到了 create_orders **之后**，位置有误、保护已失效——它应当排在建单之前。"
                )

            if elapsed > timeout:
                raise BioyondException(
                    f"等待 5 号自动传递窗空位超时（{timeout}s）：需要 {required_slots} 个空位，"
                    f"当前只有 {free_slots} 个（占用 "
                    + ", ".join(
                        f"{loc.get('code')}={loc.get('holdMTypeName')}" for loc in occupied
                    )
                    + "）。请确认上一轮电导板已测完并转去 5 号右侧手动堆栈。"
                )

            poll_count += 1
            logger.info(
                f"[stack_inquiry_window_5_free] 5 号自动传递窗空闲 {free_slots}/{total_slots}，"
                f"不足本轮所需 {required_slots}，{poll_interval}s 后重试…（已等待 {elapsed:.1f}s）"
            )
            time.sleep(poll_interval)

    def monitor_manual_stack_3(
        self,
        poll_interval: float = 5.0,
        max_duration: int = 3600,
    ) -> Dict[str, Any]:
        """
        持续轮询监测 3 个堆栈的库位占用情况：
          - 3号箱手动堆栈      (手动堆栈,           code=0007)
          - 3号箱自动堆栈-左   (自动堆栈-左,        code=0008)
          - 1号2号手套箱交接堆栈 (1号2号手套箱交接堆栈, code=0016)

        每隔 poll_interval 秒分别查一次 warehouse-info(2.38)，打印各堆栈当前各库位的占用物料，
        累计运行达到 max_duration 秒后自动停止并返回最后一次快照。

        用途：人工盯这三个堆栈的物料进出（调试 / 观察）。
        说明：UniLab 的 action 不接受前端 cancel，故用 max_duration 到点自停；
              单次查询失败只告警重试，不中断监测。

        Args:
            poll_interval: 轮询间隔（秒），默认 5s（一轮内顺序查完 3 个堆栈后再 sleep）
            max_duration: 最长监测时长（秒），默认 3600s（1h），到点自动停止

        Returns:
            {
                "status": "stopped",
                "poll_count": int,
                "elapsed_seconds": float,
                "last_snapshot": {
                    "3号箱手动堆栈": [ {"code", "holdMName", "holdMTypeName"}, ... ],
                    "3号箱自动堆栈-左": [...],
                    "1号2号手套箱交接堆栈": [...],
                },
            }
        """
        # whId 来源：包含库位的仓库信息0610.json
        _STACKS = [
            ("3号箱手动堆栈", "3a19deae-2c79-05a3-9c76-8e6760424841"),       # 手动堆栈 code=0007
            ("3号箱自动堆栈-左", self._WH_ID_AUTO_STACK_LEFT),               # 自动堆栈-左 code=0008
            ("1号2号手套箱交接堆栈", self._WH_ID_TRANSFER_2TO1),              # 交接堆栈 code=0016
        ]

        start = time.time()
        poll_count = 0
        last_snapshot: Dict[str, Any] = {}
        logger.info(
            f"[monitor_3stacks] 开始监测 3 个堆栈（手动/自动堆栈-左/1-2交接），"
            f"poll_interval={poll_interval}s, max_duration={max_duration}s"
        )
        while True:
            poll_count += 1
            snapshot: Dict[str, Any] = {}
            for name, wh_id in _STACKS:
                resp = self._post_lims(
                    "/api/lims/storage/warehouse-info",
                    {"whId": wh_id, "includeDetail": True},
                )
                if not isinstance(resp, dict) or resp.get("code") != 1:
                    logger.warning(f"[monitor_3stacks] ⚠️ [{name}] 查询失败（将重试）: {resp}")
                    snapshot[name] = None
                    continue
                data = resp.get("data") or {}
                locations = data.get("locations") or []
                occupied = [
                    loc for loc in locations
                    if isinstance(loc, dict) and (loc.get("holdMId") or "").strip()
                ]
                snap = [
                    {
                        "code": loc.get("code"),
                        "holdMName": loc.get("holdMName"),
                        "holdMTypeName": loc.get("holdMTypeName"),
                    }
                    for loc in occupied
                ]
                snapshot[name] = snap
                logger.info(
                    f"[monitor_3stacks] 第{poll_count}次 [{name}] 占用 "
                    f"{len(occupied)}/{len(locations)} 库位: "
                    f"{[(s['code'], s['holdMTypeName'] or s['holdMName']) for s in snap]}"
                )
            last_snapshot = snapshot

            elapsed = time.time() - start
            if elapsed >= max_duration:
                logger.info(
                    f"[monitor_3stacks] 达到 max_duration={max_duration}s，停止监测"
                    f"（共轮询 {poll_count} 次，耗时 {elapsed:.1f}s）"
                )
                return {
                    "status": "stopped",
                    "poll_count": poll_count,
                    "elapsed_seconds": round(elapsed, 1),
                    "last_snapshot": last_snapshot,
                }
            time.sleep(poll_interval)

    def multitask_probe(self, task_no: int = 1, hold_ms: int = 1000) -> Dict[str, Any]:
        """
        多任务调度探测动作。

        对同一动作提交多个任务、分别传入不同的 task_no，根据返回的 start_time_ms
        判断 UniLab 多任务是从前往后还是从后往前启动执行。

        Args:
            task_no: 任务编号（由调用方自定义，用于区分多任务实例）
            hold_ms: 占用设备时长（毫秒），默认 1000，便于拉开各任务开始时间差

        Returns:
            包含 task_no、开始/结束时间（毫秒精度）等字段的字典
        """
        start_dt = datetime.now()
        start_time = (
            start_dt.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{int(start_dt.microsecond / 1000):03d}"
        )
        start_time_ms = int(start_dt.timestamp() * 1000)

        logger.info(
            f"[multitask_probe] 开始执行 task_no={task_no}, "
            f"start_time={start_time}, hold_ms={hold_ms}"
        )

        if hold_ms and hold_ms > 0:
            time.sleep(hold_ms / 1000.0)

        end_dt = datetime.now()
        end_time = (
            end_dt.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{int(end_dt.microsecond / 1000):03d}"
        )
        end_time_ms = int(end_dt.timestamp() * 1000)

        result = {
            "success": True,
            "task_no": task_no,
            "start_time": start_time,
            "start_time_ms": start_time_ms,
            "end_time": end_time,
            "end_time_ms": end_time_ms,
            "hold_ms": hold_ms,
            "message": f"task_no={task_no} 于 {start_time} 开始执行",
        }
        logger.info(f"[multitask_probe] 结束执行 {result}")
        return result

    # 2.7 启动调度
    def scheduler_start(self) -> Dict[str, Any]:
        # 人工重新起调度 = 已处置完上一批的建单失败，清掉标记，
        # 否则门控会一直把 Pause 当成「建单失败自动暂停」而直接阻断。
        self.clear_interlock_failure()
        return self._post_lims("/api/lims/scheduler/start")
    # 3.10 停止调度
    def scheduler_stop(self) -> Dict[str, Any]:

        """
        停止调度 (3.10)
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/stop")

    # 2.8 暂停调度
    def scheduler_pause(self) -> Dict[str, Any]:
        """
        暂停调度 (2.8)

        电解液站只有这一个暂停接口，语义等同智能暂停：执行完当前动作再停，
        不会把机械臂冻在半空。恢复用 scheduler_continue。
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/pause")

    # 2.9 继续调度
    def scheduler_continue(self) -> Dict[str, Any]:
        """
        继续调度 (2.9)
        请求体只包含 apiKey 和 requestTime
        """
        # 人工恢复调度 = 已处置完上一批的建单失败，清掉标记（同 scheduler_start）
        self.clear_interlock_failure()
        return self._post_lims("/api/lims/scheduler/continue")
    def scheduler_reset(self) -> Dict[str, Any]:
        """
        复位调度 (2.11)
        请求体只包含 apiKey 和 requestTime
        """
        return self._post_lims("/api/lims/scheduler/reset")

    # 查询调度状态
    def scheduler_status(self) -> Dict[str, Any]:
        """
        查询调度状态

        请求体只包含 apiKey 和 requestTime，返回原始响应，其中 data 为：
        schedulerStatus（Init / Stop / Running / Pause / ErrorPause / ErrorStop）、
        hasTask（是否有任务）、creationTime。
        互锁门控用的 `_query_scheduler_status` 只取 schedulerStatus 一个字段。
        """
        return self._post_lims("/api/lims/scheduler/scheduler-status")

    # 2.43 批量查询机器人坐标
    def robot_joint_list_info(self) -> Dict[str, Any]:
        """
        2.43 批量查询机器人坐标

        请求体只包含 apiKey 和 requestTime，返回原始响应，其中 data 为对象数组，每项：
        deviceFrameCode（设备 Framecode）、joints（四轴关节，index 轴号 / value 位置）、
        extjoints（扩增关节，如夹爪、导轨，同样 index / value）。
        extjoints 个数随机器人配置变化（实测有的回 2 个、有的只回 1 个），不可假定固定长度。
        后台轮询用的 `_fetch_robot_joints` 会把它整形成 robot_joint_positions 属性。
        """
        return self._post_lims("/api/lims/device/robot-joint-list-info")

    # 2.44 批量查询三轴机器人坐标
    def gantry_pos_list_info(self) -> Dict[str, Any]:
        """
        2.44 批量查询三轴机器人坐标

        请求体只包含 apiKey 和 requestTime，返回原始响应，其中 data 为对象数组，每项：
        deviceFrameCode（设备 Framecode）与 x / y / z 三个坐标。
        后台轮询用的 `_fetch_gantry_pos` 会把它整形成 gantry_positions 属性。
        """
        return self._post_lims("/api/lims/device/gantry-pos-list-info")

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
                             source_x: int = 1, source_y: int = 1, source_z: int = 1,
                             source_pos: Optional[Dict] = None) -> Dict[str, Any]:
        if source_pos:
            source_x = source_pos.get("x", source_x)
            source_y = source_pos.get("y", source_y)
            source_z = source_pos.get("z", source_z)
        payload: Dict[str, Any] = {
            "sourcePosX": source_x, "sourcePosY": source_y, "sourcePosZ": source_z
        }
        if source_wh_id:
            payload["sourceWHID"] = source_wh_id

        response = self._post_lims("/api/lims/order/transfer-task3To2To1", payload)
        # 等待任务报送成功
        order_data = response.get("data") or {}
        order_code = order_data.get("orderCode")
        if not order_code:
            # data=None 表示 LIMS 根本没建单，已知三种原因：
            #   1) 该坐标在来源仓库不存在；
            #   2) 该库位为空（没板可搬）；
            #   3) **目标的 1号2号手套箱交接堆栈已有物料**——它只有 1 个库位，占着时
            #      LIMS 直接拒建（已与 LIMS 开发者确认）。2026-08-05 那次失败就是这条，
            #      当时上一轮的板正等扣电结束后由 transfer_1_to_2 送回。
            # 绝不能当成功返回，否则调用方会继续做资源树同步 / 下一步 1→2 转运，
            # 而板子其实一步都没动。
            raise BioyondException(
                f"3-2-1 上料任务未创建（LIMS 未返回 orderCode）: "
                f"来源仓库={source_wh_id}, 坐标=({source_x},{source_y},{source_z}), "
                f"响应={response}。"
                f"可能原因：①该坐标在来源仓库不存在；②该库位为空；"
                f"③1号2号手套箱交接堆栈已有物料（只有 1 个库位，占着则拒建）——"
                f"请查 warehouse-info 确认，并检查是否漏配了前置的 stack_inquiry_2to1 门控"
            )
        result = self.wait_for_order_finish(
            order_code, expected_order_id=order_data.get("orderId")
        )
        if result.get("status") != "success":
            raise BioyondException(
                f"3-2-1 上料任务未成功完成: orderCode={order_code}, 结果={result}"
            )
        return result

    def transfer_3_to_2(self,
                        source_wh_id: Optional[str] = '3a19debc-84b4-0359-e2d4-b3beea49348b',
                        source_x: int = 1, 
                        source_y: int = 1, 
                        source_z: int = 1,
                        source_pos: Optional[Dict] = None) -> Dict[str, Any]:
        """
        2.34 3-2 物料转运接口
        
        新建从 3 -> 2 的搬运任务
        
        Args:
            source_wh_id: 来源仓库 Id (默认为3号仓库)
            source_x: 来源位置 X 坐标
            source_y: 来源位置 Y 坐标
            source_z: 来源位置 Z 坐标
            source_pos: 整合 xyz 的字典（优先级高于单独的 x/y/z 参数）
            
        Returns:
            dict: 包含任务 orderId 和 orderCode 的响应
        """
        if source_pos:
            source_x = source_pos.get("x", source_x)
            source_y = source_pos.get("y", source_y)
            source_z = source_pos.get("z", source_z)
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
        order_data = response.get("data") or {}
        order_code = order_data.get("orderCode") if isinstance(order_data, dict) else None
        if not order_code:
            raise BioyondException(
                f"[transfer_3_to_2] 转运任务未创建（LIMS 未返回 orderCode）: "
                f"来源仓库={source_wh_id}, 坐标=({source_x},{source_y},{source_z}), "
                f"响应={response}"
            )
        
        logger.info(f"[transfer_3_to_2] 转运任务已创建: {order_code}")
        result = self.wait_for_order_finish(
            order_code,
            expected_order_id=order_data.get("orderId") if isinstance(order_data, dict) else None,
        )
        if result.get("status") != "success":
            raise BioyondException(
                f"[transfer_3_to_2] 转运任务未成功完成: orderCode={order_code}, 结果={result}"
            )
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
            # 典型场景：上一步 321 其实没搬成功，1 号仓没有板可搬，LIMS 直接不建单。
            raise BioyondException(
                f"[transfer_1_to_2] 转运任务未创建（LIMS 未返回 orderCode）: 响应={response}。"
                f"请确认 1 号仓已有待转运物料（上一步 3→2→1 是否真的成功）"
            )
        
        expected_oid = data_field.get("orderId") if isinstance(data_field, dict) else None
        logger.info(f"[transfer_1_to_2] 转运任务已创建: {order_code}")
        result = self.wait_for_order_finish(order_code, expected_order_id=expected_oid)
        if result.get("status") != "success":
            raise BioyondException(
                f"[transfer_1_to_2] 转运任务未成功完成: orderCode={order_code}, 结果={result}"
            )
        logger.info(f"[transfer_1_to_2] 转运任务完成: {order_code}")
        return result

    def transfer_3_to_2_auto(
        self,
        vial_plates: List[Dict],
        plate_type: str = "20ml分液瓶板",
        **kwargs
    ) -> Dict[str, Any]:
        """
        批量 3→2 转运：把**当前在自动堆栈-左**的分液瓶板逐块搬到 2 号位置。

        为什么不能只按 typeName 挑板：电导+软包混合单里两类板 typeName 都是
        "20ml分液瓶板"，靠板型无法区分谁该走 32。真正的判据是**板在哪个仓库**：
          - 软包板：配液结束后停在自动堆栈-左，等 32 转运
          - 电导板：配液结束后已搬到 5 号自动传递窗，等 conductivity_test_inline
        因此这里先查一次自动堆栈-左的实时占用，只搬命中的板；不在的板直接跳过
        （而非报错），与电导侧"按 5 号传递窗求交集过滤"正好互补。

        与 transfer_3_to_2_to_1_auto 的另一个区别：3→2 的落点在 2 号手套箱，
        当前没有对应设备节点承接，故只做物理转运，不做资源树同步。

        Args:
            vial_plates: 分液瓶板列表（上游配液输出，含全部板型）
            plate_type: 参与 32 转运的板型关键字，默认 "20ml分液瓶板"

        Returns:
            {"total": 入参板数, "success": 成功数, "failed": 失败数, "results": [...]}
        """
        if kwargs:
            logger.warning(
                f"[transfer_3_to_2_auto] ⚠️ 检测到未识别的参数: {list(kwargs.keys())}，已忽略"
            )

        if not vial_plates:
            raise ValueError("vial_plates 参数不能为空")

        logger.info("=" * 80)
        logger.info(f"[transfer_3_to_2_auto] 接收到 {len(vial_plates)} 个分液瓶板")
        for idx, plate in enumerate(vial_plates, 1):
            logger.info(
                f"  [{idx}] orderCode={(plate or {}).get('orderCode', 'N/A')}, "
                f"materialId={((plate or {}).get('materialId') or 'N/A')[:20]}..., "
                f"typeName={(plate or {}).get('typeName', 'N/A')}"
            )
        logger.info("=" * 80)

        # ========== 步骤1：查一次自动堆栈-左的实时占用 ==========
        # 只查一次而非逐板查：固定堆栈的库位坐标不因取走某块板而变化，
        # 一次快照既省请求又保证同批板用的是一致的库存视图。
        wh_data = self._snapshot_warehouse(self._WH_ID_AUTO_STACK_LEFT)
        if wh_data is None:
            raise BioyondException(
                "查询自动堆栈-左失败，无法确定 32 转运的源坐标"
            )
        wh_display = wh_data.get("name") or self._WH_ID_AUTO_STACK_LEFT
        stack_coords: Dict[str, Tuple[int, int, int]] = {}
        occupied: List[str] = []
        for loc in (wh_data.get("locations") or []):
            hold_mid = (loc.get("holdMId") or "").strip()
            if not hold_mid:
                continue
            stack_coords[hold_mid] = (
                int(loc.get("x") or 1),
                int(loc.get("y") or 1),
                int(loc.get("z") or 1),
            )
            occupied.append(f"{loc.get('code')}={loc.get('holdMTypeName')}")
        logger.info(
            f"[32批量转运] {wh_display} 当前有 {len(stack_coords)} 块物料: {occupied}"
        )

        # ========== 步骤2：按板型 + 是否在自动堆栈-左双重过滤 ==========
        results: List[Dict] = []
        pending: List[Tuple[Dict, Tuple[int, int, int]]] = []
        seen_material_ids = set()

        for idx, plate in enumerate(vial_plates, 1):
            if not plate or not isinstance(plate, dict):
                results.append({
                    "index": idx, "orderCode": "N/A", "materialId": "N/A",
                    "status": "failed", "error": "分液瓶板信息无效或为空",
                })
                continue

            material_id = (plate.get("materialId") or "").strip()
            order_code = plate.get("orderCode", "N/A")
            type_name = (plate.get("typeName") or "").strip()

            if not material_id:
                results.append({
                    "index": idx, "orderCode": order_code, "materialId": "N/A",
                    "status": "failed", "error": "缺少 materialId",
                })
                continue

            if plate_type not in type_name:
                logger.info(
                    f"[32批量转运] ℹ️ [{idx}/{len(vial_plates)}] 板型不符，跳过 "
                    f"(orderCode={order_code}, typeName={type_name or '空'})"
                )
                results.append({
                    "index": idx, "orderCode": order_code, "materialId": material_id,
                    "status": "skipped",
                    "message": f"非{plate_type}（typeName={type_name or '空'}）",
                })
                continue

            if material_id in seen_material_ids:
                logger.info(
                    f"[32批量转运] ℹ️ [{idx}/{len(vial_plates)}] 该瓶板已排入本批，跳过"
                    f"（多订单共用同一物理瓶板）"
                )
                results.append({
                    "index": idx, "orderCode": order_code, "materialId": material_id,
                    "status": "skipped", "message": "该瓶板已排入本批（共用瓶板）",
                })
                continue

            coords = stack_coords.get(material_id)
            if coords is None:
                # 电导板停在 5 号自动传递窗，本就不该走 32；也可能是还没搬进来。
                logger.info(
                    f"[32批量转运] ℹ️ [{idx}/{len(vial_plates)}] 不在 {wh_display}，跳过 "
                    f"(orderCode={order_code}, materialId={material_id[:20]}...)"
                )
                results.append({
                    "index": idx, "orderCode": order_code, "materialId": material_id,
                    "status": "skipped",
                    "message": f"不在{wh_display}（电导板停 5 号自动传递窗，或尚未搬入）",
                })
                continue

            seen_material_ids.add(material_id)
            pending.append((plate, coords))

        if not pending:
            logger.warning(
                f"[transfer_3_to_2_auto] 过滤后无可转运的 {plate_type}"
                f"（入参 {len(vial_plates)} 块，全部跳过）"
            )
            return {
                "total": len(vial_plates),
                "success": 0,
                "failed": sum(1 for r in results if r.get("status") == "failed"),
                "results": results,
            }

        logger.info(
            f"[32批量转运] 过滤后保留 {len(pending)}/{len(vial_plates)} 块板待转运"
        )

        # ========== 步骤3：逐块物理转运 ==========
        success_count = 0
        failed_count = sum(1 for r in results if r.get("status") == "failed")

        for seq, (plate, (x, y, z)) in enumerate(pending, 1):
            material_id = (plate.get("materialId") or "").strip()
            order_code = plate.get("orderCode", "N/A")
            try:
                logger.info(f"\n{'='*60}")
                logger.info(f"[32批量转运] 处理 [{seq}/{len(pending)}]")
                logger.info(f"  orderCode: {order_code}")
                logger.info(f"  materialId: {material_id[:20]}...")
                logger.info(f"  源坐标: {wh_display}({x},{y},{z})")
                logger.info(f"{'='*60}")

                result = self.transfer_3_to_2(
                    source_wh_id=self._WH_ID_AUTO_STACK_LEFT,
                    source_x=x, source_y=y, source_z=z,
                )
                results.append({
                    "index": seq, "orderCode": order_code, "materialId": material_id,
                    "status": "success", "result": result,
                })
                success_count += 1
                logger.info(f"[32批量转运] ✅ [{seq}/{len(pending)}] 转运成功")
            except Exception as e:
                logger.error(f"[32批量转运] ❌ [{seq}/{len(pending)}] 失败: {e}")
                results.append({
                    "index": seq, "orderCode": order_code, "materialId": material_id,
                    "status": "failed", "error": str(e),
                })
                failed_count += 1

        summary = {
            "total": len(vial_plates),
            "success": success_count,
            "failed": failed_count,
            "results": results,
        }

        logger.info(f"\n{'='*60}")
        logger.info(f"[32批量转运] 完成汇总:")
        logger.info(f"  总数: {summary['total']}")
        logger.info(f"  成功: {summary['success']} ✅")
        logger.info(f"  失败: {summary['failed']} ❌")
        logger.info(f"{'='*60}\n")

        if failed_count > 0:
            fail_details = "; ".join(
                f"{r.get('orderCode')}/{(r.get('materialId') or '')[:8]}: {r.get('error')}"
                for r in results if r.get("status") == "failed"
            )
            raise BioyondException(
                f"3-2 批量转运有 {failed_count} 块板失败: {fail_details}"
            )

        return summary

    # 2.5 批量查询实验报告(post过滤关键字查询)
    def order_list_v2(self,
                      timeType: str = "",
                      beginTime: str = "",
                      endTime: str = "",
                      status: str = "", # 60表示正在运行,80表示完成，90表示失败
                      filter: str = "",
                      skipCount: int = 0,
                      pageCount: int = 1, # 每页条数（不是页数！实测 pageCount=10 配 totalCount=611 返回 10 条）
                      sorting: str = "") -> Dict[str, Any]:
        """
        批量查询实验报告的详细信息 (2.5)
        URL: /api/lims/order/order-list
        参数默认值和接口文档保持一致

        易错口径（写错**不报错**，只是静默查错范围，实测见 `_lims_time_window`）：
        - `timeType` 是「按哪个字段过滤」，要用时间窗只能传 `"requestTime"`。
          传 `"beginTime"`（参数名本身）或空串都等于**不做时间过滤**，返回全部历史单
        - 时间窗只有「天」粒度，时分秒被丢弃；分钟级精度要靠调用方按 requestTime 复筛
        - `beginTime`/`endTime` 是**本地时间 + 假 Z 后缀**，LIMS 按裸本地时间比较，
          改成真 UTC 会让窗口偏 8 小时。构造窗口请用 `_lims_time_window`
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

    # 2.6 按 orderId 查询单个实验报告
    def order_report_v2(self, order_id: str) -> Dict[str, Any]:
        """查询单个订单的实验报告详情

        URL: /api/lims/order/order-report

        Args:
            order_id: 订单 GUID。注意必须传 orderId，传 orderCode 接口会返回 400。

        Returns:
            接口原始响应（data 内含 code/name/preIntakes[].sampleMaterials/usedMaterials 等）
        """
        return self._post_lims("/api/lims/order/order-report", order_id)

    def _recover_timeout_order_report(
        self, order_code: str, create_entry: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """timeout 兜底（LIMS 侧核对）。

        工作站等 order_finish 推送超时（如隔夜暂停第二天才完成），但 LIMS 侧可能
        早已成功。此处用 2.5 `/api/lims/order/order-list` 按 orderCode 查真实状态；
        若 status==80(Succeed)，就用建单返回 `create_entry.usedMaterials`（自带
        materialTypeName）合成一份 push 口径的 report，交给既有 prep/vial/plate 提取
        逻辑补全条码与类型——不依赖迟到的 order_finish 推送。

        Args:
            order_code: 订单 orderCode（如 BSO2026071400001）
            create_entry: 建单返回 `/api/lims/order/orders` 的 data[*] 条目，
                含 orderId / orderName / usedMaterials(materialTypeName/materialTypeMode)

        Returns:
            合成 report（含 usedMaterials / orderName / orderId），
            核对失败或 LIMS 未完成时返回 None。
        """
        if not order_code:
            return None
        try:
            resp = self.order_list_v2(filter=order_code, pageCount=1)
        except Exception as e:
            logger.warning(f"[timeout兜底] order-list 查询失败: orderCode={order_code}, 错误={e}")
            return None

        items = ((resp or {}).get("data") or {}).get("items") or []
        matched = None
        expect_oid = self._normalize_order_id((create_entry or {}).get("orderId"))
        expect_name = (create_entry or {}).get("orderName")
        # 优先按 orderId 匹配，避免真机/仿真 orderCode 撞号时捞错单
        if self._is_usable_order_id(expect_oid):
            for it in items:
                if self._normalize_order_id(it.get("id") or it.get("orderId")) == expect_oid:
                    matched = it
                    break
        if matched is None and expect_name:
            for it in items:
                if it.get("name") == expect_name:
                    matched = it
                    break
        if matched is None and items and not self._is_usable_order_id(expect_oid):
            matched = items[0]
        if matched is None:
            logger.warning(f"[timeout兜底] order-list 未返回订单: orderCode={order_code}")
            return None

        status = matched.get("status")
        if status != 80:
            logger.warning(
                f"[timeout兜底] LIMS 未完成，不恢复: orderCode={order_code}, "
                f"status={status}({matched.get('statusName')})"
            )
            return None

        if not create_entry:
            logger.warning(
                f"[timeout兜底] LIMS 显示完成但缺建单 usedMaterials，无法合成 report: {order_code}"
            )
            return None

        # 建单返回 usedMaterials 字段口径与 push 报文不同（materialTypeMode 字符串、
        # 无 typemode/realQuantity/usedQuantity），这里转换成提取逻辑认得的字段。
        # 只取 Sample 模式物料（配液瓶/分液瓶/分液瓶板都是 Sample），排除耗材/试剂。
        synth_used: List[Dict[str, Any]] = []
        for m in create_entry.get("usedMaterials") or []:
            if (m.get("materialTypeMode") or "") != "Sample":
                continue
            mid = m.get("materialId") or ""
            if not mid:
                continue
            synth_used.append({
                "typemode": "1",
                "realQuantity": 1,
                "usedQuantity": 1,
                "materialId": mid,
                "locationId": m.get("locationId") or "",
                "materialName": m.get("materialName") or "",
                "materialTypeName": m.get("materialTypeName") or "",
            })

        logger.info(
            f"[timeout兜底] ✓ LIMS status=80(Succeed)，用建单 usedMaterials 合成 report: "
            f"orderCode={order_code}, Sample物料={len(synth_used)}"
        )
        return {
            "orderCode": order_code,
            "orderName": create_entry.get("orderName", "N/A"),
            "orderId": create_entry.get("orderId", ""),
            "usedMaterials": synth_used,
            "_recovered_via_order_list": True,
        }

    # 一直post执行bioyond接口查询任务状态
    def wait_for_transfer_task(self, timeout: int = 3000, interval: int = 5, filter_text: Optional[str] = None) -> bool:
        """
        轮询查询物料转移任务是否成功完成 (status=80)
        - timeout: 最大等待秒数 (默认600秒)
        - interval: 轮询间隔秒数 (默认3秒)
        返回 True 表示找到并成功完成，False 表示超时未找到
        """
        now = datetime.now()
        # 注意：下面 timeType="" 等于不做时间过滤，这里的 begin/endTime 是死参数，
        # 真正的筛选靠 filter=filter_text。不要「顺手」把 timeType 改成 requestTime——
        # 这个窗口是 [now, now+5min] 的未来区间，一旦生效会把所有单都过滤掉。
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
                # create_sample 会阻塞 1s+，刷新线程可能在 inbound 完成前看到空格并卸掉刚挂上的板
                self._touch_warehouse_refresh_skip()
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
                # 优先 unilabos_extra（由 collect_node_data 注入）；回退 PLR 对象上的 barcode
                barcode = plr_resource.unilabos_extra.get("barCode", "") or getattr(plr_resource, "barcode", "") or ""
                if isinstance(barcode, dict):
                    barcode = barcode.get("data", "") or ""
                logger.info(f"拖拽上料: {plr_resource.name} -> {warehouse_name} / {site}, barCode={barcode!r}")

                extra = getattr(plr_resource, "unilabos_extra", None)
                if not isinstance(extra, dict):
                    extra = {}
                    plr_resource.unilabos_extra = extra
                sample_uuid = self.create_sample(
                    plr_resource.name, board_type, bottle_type, site, warehouse_name, barcode
                )
                if isinstance(sample_uuid, str) and sample_uuid:
                    extra["material_bioyond_id"] = sample_uuid
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
    
    # 各板型对应的子位排列 (num_x, num_y)，用于构建 details
    # 同时支持中文板名与英文 model 名作为 key，避免不同调用方传入类型不一致时查表失败
    BOARD_GRID = {
        "配液瓶(大)板": (2, 2), "YB_PrepBottle_60mL_Carrier": (2, 2),
        "配液瓶(小)板": (2, 4), "YB_PrepBottle_15mL_Carrier": (2, 4),
        "5ml分液瓶板":  (2, 4), "YB_Vial_5mL_Carrier": (2, 4),
        "20ml分液瓶板": (2, 4), "YB_Vial_20mL_Carrier": (2, 4),
    }

    def create_sample(
        self,
        name: str,
        board_type: str,
        bottle_type: str,
        location_code: str,
        warehouse_name: str = "手动堆栈右",
        barcode: str = ""
    ) -> Optional[str]:
        """创建配液板物料并自动入库。
        Args:
            name: 物料名称（必填）
            board_type: 板类型，如 "5ml分液瓶板"、"配液瓶(小)板"
            bottle_type: 瓶类型，如 "5ml分液瓶"、"配液瓶(小)"
            location_code: 库位编号，例如 "A01"
            warehouse_name: 仓库名称，默认为 "手动堆栈右"，支持 "自动堆栈-左"、"自动堆栈-右" 等
            barcode: 物料条码（可选），填写后发给奔曜；不填则为空字符串
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

        # 根据板型获取子位网格尺寸；查不到时直接报错，避免静默按错误数量建料
        if board_type not in self.BOARD_GRID:
            raise ValueError(
                f"未找到板型 '{board_type}' 的子位网格配置，请在 BOARD_GRID 中补充该板型（中文板名或英文 model 名）"
            )
        num_x, num_y = self.BOARD_GRID[board_type]
        logger.debug(f"[create_sample] 板型 '{board_type}' 子位网格: {num_x}×{num_y}")

        # 新建小瓶
        details = []
        for y in range(1, num_y + 1):
            for x in range(1, num_x + 1):
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
                "barCode": barcode,
                "name": name,
                "unit": "块",
                "parameters": json.dumps({"unit": "块"}, ensure_ascii=False),
                "quantity": "1",
                "details": details,
            }
        # print("xxx:",data)
        create_result = self._post_lims("/api/lims/storage/material", data)
        raw = create_result.get("data") if isinstance(create_result, dict) else create_result
        if isinstance(raw, str):
            sample_uuid = raw
        elif isinstance(raw, dict):
            sample_uuid = raw.get("id") or raw.get("materialId")
        else:
            sample_uuid = None

        if not sample_uuid:
            logger.error(f"[create_sample] 建料未返回物料 ID: {create_result}")
            return None

        final_result = self._post_lims("/api/lims/storage/inbound", {
            "materialId": sample_uuid,
            "locationId": location_id,
        })
        if isinstance(final_result, dict) and final_result.get("error"):
            logger.error(f"[create_sample] 入库失败: {final_result}")
        return sample_uuid


def _install_stock_monitor_properties(cls) -> None:
    """ROS 只认类上的 property，用循环挂粉体/液体成对属性。"""

    def _make_name(kind: str, index: int):
        def _getter(self) -> str:
            return self._stock_slot_name(kind, index)

        _getter.__name__ = f"data_{kind}_{index:02d}_name"
        return property(_getter)

    def _make_qty(kind: str, index: int):
        def _getter(self) -> float:
            return self._stock_slot_qty(kind, index)

        _getter.__name__ = f"data_{kind}_{index:02d}_qty"
        return property(_getter)

    for i in range(1, 21):
        setattr(cls, f"data_powder_{i:02d}_name", _make_name("powder", i))
        setattr(cls, f"data_powder_{i:02d}_qty", _make_qty("powder", i))
    for i in range(1, 16):
        setattr(cls, f"data_liquid_{i:02d}_name", _make_name("liquid", i))
        setattr(cls, f"data_liquid_{i:02d}_qty", _make_qty("liquid", i))


_install_stock_monitor_properties(BioyondCellWorkstation)


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