# -*- coding: utf-8 -*-
"""
功能:
    TSC标签打印服务类. 封装print_text.py的核心功能为可复用接口.
    支持中文, 自动缩放字号, 自动居中对齐, 兼容单列和多列标签纸.

用法:
    from eit_synthesis_station.printer import LabelPrintService

    svc = LabelPrintService("path/to/25x10x2.yaml")
    svc.connect()
    svc.print_batch(["试剂A", "试剂B", "试剂C"])
    svc.disconnect()
"""

import logging
import os
from typing import List, Optional

from .print_text import (
    load_config,
    load_dll,
    check_printer_ready,
    execute_print_job,
)

logger = logging.getLogger(__name__)

# 默认配置文件: 与本文件同目录下的 25x10x2.yaml
_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "25x10x2.yaml")
# DLL 默认路径
_DEFAULT_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs", "TSCLIB.dll")


class LabelPrintService:
    """
    功能:
        TSC标签打印服务. 基于print_text.py的windowsfontUnicode方案,
        支持中文, 自动缩放字号, 自动居中对齐.
        兼容单列和多列标签纸, 按列数自动分组打印.
    参数:
        config_path: str, 标签规格YAML配置文件路径. 默认使用 25x10x2.yaml.
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = _DEFAULT_CONFIG
        self._config_path = config_path
        self._config = load_config(config_path)
        self._dll_path = _DEFAULT_DLL
        self._lib = None
        self._connected = False

    # ──────────────────── 连接管理 ────────────────────

    def connect(self) -> bool:
        """
        功能:
            加载DLL并预检打印机可用性.
            预检结束后立即关闭端口, 避免长期占用同一个打印会话.
        返回:
            bool, 连接成功返回True.
        """
        if self._connected:
            return True
        try:
            if self._lib is None:
                self._lib = load_dll(self._dll_path)
            check_printer_ready(self._lib, self._config)
            self._connected = True
            logger.debug("标签打印服务已就绪")
            return True
        except Exception as exc:
            logger.error("标签打印服务连接失败: %s", exc)
            self._lib = None
            self._connected = False
            return False

    def disconnect(self) -> None:
        """
        功能:
            释放打印服务资源.
        """
        self._lib = None
        self._connected = False
        logger.debug("标签打印服务已断开")

    @property
    def connected(self) -> bool:
        """返回当前连接状态."""
        return self._connected

    @property
    def columns(self) -> int:
        """返回配置中的标签列数."""
        return self._config.get("paper", {}).get("columns", 1)

    # ──────────────────── 打印方法 ────────────────────

    def print_label(self, texts: List[str], copies: int = 1) -> bool:
        """
        功能:
            打印一张标签纸(一行), 每列分别渲染对应文字.
            自动计算字号和居中位置.
        参数:
            texts: List[str], 每列的文字内容. 长度应 <= 列数, 不足的列留空.
            copies: int, 打印份数, 默认1.
        返回:
            bool, 打印指令发送成功返回True.
        """
        if not self._connected:
            if not self.connect():
                return False
        try:
            for _ in range(copies):
                execute_print_job(self._lib, self._config, texts)
            return True
        except Exception as exc:
            logger.error("标签打印失败: %s", exc)
            return False

    def print_batch(self, text_list: List[str], copies_each: int = 1) -> bool:
        """
        功能:
            批量打印多张标签. 将text_list按列数自动分组,
            每组占一行标签纸, 最后不足一组的也打印.
        参数:
            text_list: List[str], 所有要打印的文字内容(扁平列表).
            copies_each: int, 每张标签的打印份数.
        返回:
            bool, 全部打印成功返回True.
        """
        if not text_list:
            logger.warning("没有需要打印的内容")
            return True

        if not self._connected:
            if not self.connect():
                return False

        cols = self.columns
        total = len(text_list)
        success = True

        try:
            # 按列数分组
            for batch_start in range(0, total, cols):
                batch = text_list[batch_start:batch_start + cols]
                if not self.print_label(batch, copies=copies_each):
                    success = False
                    break

            if success:
                logger.debug("批量打印完成: 共 %d 张标签", total)
        except Exception as exc:
            logger.error("批量打印失败: %s", exc)
            success = False

        return success
