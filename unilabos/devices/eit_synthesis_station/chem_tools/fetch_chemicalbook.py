# -*- coding: utf-8 -*-
"""
功能:
    提供 ChemicalBook 抓取命令行入口, 支持按 CAS 输出结构化 JSON 文件.
参数:
    无.
返回:
    无.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from ..config.setting import configure_logging
from .chemicalbook_scraper import fetch_chemicalbook_by_cas


logger = logging.getLogger("FetchChemicalBookCLI")


def main(argv: Optional[list] = None) -> int:
    """
    功能:
        解析命令行参数并执行 ChemicalBook 抓取.
    参数:
        argv: Optional[list], 可选命令行参数列表, None 表示使用系统参数.
    返回:
        int, 进程退出码. 成功返回 0, 失败返回 1.
    """
    parser = argparse.ArgumentParser(description="按 CAS 抓取 ChemicalBook 化学品信息")
    parser.add_argument("--cas", required=True, help="CAS 号, 例如 64-17-5")
    parser.add_argument("--out", help="输出 JSON 文件路径")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")
    parser.add_argument("--save-html", action="store_true", help="同时保存原始 HTML 文件")
    parser.add_argument("--log-level", default="INFO", help="日志级别, 例如 INFO 或 DEBUG")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    logger.info("开始抓取 ChemicalBook, CAS=%s", args.cas)

    record = fetch_chemicalbook_by_cas(
        cas=args.cas,
        timeout=args.timeout,
        save_raw_html=args.save_html,
    )

    output_text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.out is not None:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")
        logger.info("ChemicalBook 抓取结果已写入: %s", output_path)
    else:
        print(output_text)

    if record["status"] in ("ok", "parse_partial"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
