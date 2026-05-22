"""
读取实验结果文件（JSON / CSV），整合为 agent_result 请求体并可选提交。

用法:
    python prepare_agent_result.py \
        --notebook-uuid <uuid> \
        --files data1.json data2.csv \
        [--auth <Lab token>] \
        [--base <BASE_URL>] \
        [--submit] \
        [--output <output.json>]

支持的输入文件格式:
    - .json  → 直接作为 dict 合并
    - .csv   → 转为 {"filename": [row_dict, ...]} 格式
"""

import argparse
import base64
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def read_json_file(filepath: str) -> Dict[str, Any]:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def read_csv_file(filepath: str) -> List[Dict[str, Any]]:
    rows = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            converted = {}
            for k, v in row.items():
                try:
                    converted[k] = int(v)
                except (ValueError, TypeError):
                    try:
                        converted[k] = float(v)
                    except (ValueError, TypeError):
                        converted[k] = v
            rows.append(converted)
    return rows


def merge_files(filepaths: List[str]) -> Dict[str, Any]:
    """将多个文件合并为一个 agent_result dict"""
    merged: Dict[str, Any] = {}
    for fp in filepaths:
        path = Path(fp)
        ext = path.suffix.lower()
        key = path.stem

        if ext == ".json":
            data = read_json_file(fp)
            if isinstance(data, dict):
                merged.update(data)
            else:
                merged[key] = data
        elif ext == ".csv":
            merged[key] = read_csv_file(fp)
        else:
            print(f"[警告] 不支持的文件格式: {fp}，跳过", file=sys.stderr)

    return merged


def build_request_body(notebook_uuid: str, agent_result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "notebook_uuid": notebook_uuid,
        "agent_result": agent_result,
    }


def submit(base: str, auth: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import requests
    except ImportError:
        print("[错误] 提交需要 requests 库: pip install requests", file=sys.stderr)
        sys.exit(1)

    url = f"{base}/api/v1/lab/notebook/agent-result"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Lab {auth}",
    }
    resp = requests.put(url, json=body, headers=headers, timeout=30)
    return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}


def main():
    parser = argparse.ArgumentParser(description="整合实验结果文件并构建 agent_result 请求体")
    parser.add_argument("--notebook-uuid", required=True, help="目标 notebook UUID")
    parser.add_argument("--files", nargs="+", required=True, help="输入文件路径（JSON / CSV）")
    parser.add_argument("--auth", help="Lab token（base64(ak:sk)）")
    parser.add_argument("--base", help="API base URL")
    parser.add_argument("--submit", action="store_true", help="直接提交到云端")
    parser.add_argument("--output", default="agent_result_body.json", help="输出 JSON 文件路径")

    args = parser.parse_args()

    for fp in args.files:
        if not os.path.exists(fp):
            print(f"[错误] 文件不存在: {fp}", file=sys.stderr)
            sys.exit(1)

    agent_result = merge_files(args.files)
    body = build_request_body(args.notebook_uuid, agent_result)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    print(f"[完成] 请求体已保存: {args.output}")
    print(f"  notebook_uuid: {args.notebook_uuid}")
    print(f"  agent_result 字段数: {len(agent_result)}")
    print(f"  合并文件数: {len(args.files)}")

    if args.submit:
        if not args.auth or not args.base:
            print("[错误] 提交需要 --auth 和 --base 参数", file=sys.stderr)
            sys.exit(1)
        print(f"\n[提交] PUT {args.base}/api/v1/lab/notebook/agent-result ...")
        result = submit(args.base, args.auth, body)
        print(f"  HTTP {result['status_code']}")
        print(f"  响应: {json.dumps(result['body'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
