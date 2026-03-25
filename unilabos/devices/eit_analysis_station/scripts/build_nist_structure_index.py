#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    该脚本已废弃.
    结构映射改为运行时从 MSP/MOL 自动构建, 不再支持提前生成 index.json.
参数:
    无.
返回:
    无.
"""

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    """
    功能:
        输出废弃提示并退出.
    参数:
        无.
    返回:
        无.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.error("该脚本已废弃, 请直接运行分析流程触发运行时结构映射构建.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
