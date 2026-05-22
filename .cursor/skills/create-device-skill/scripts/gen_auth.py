#!/usr/bin/env python3
"""
从 ak/sk 生成 UniLab API Authorization header。

算法: base64(ak:sk) → "Authorization: Lab <token>"

用法:
  python gen_auth.py <ak> <sk>
  python gen_auth.py --config <config.py>

示例:
  python gen_auth.py myak mysk
  python gen_auth.py --config experiments/config.py
"""
import base64
import re
import sys


def gen_auth(ak: str, sk: str) -> str:
    token = base64.b64encode(f"{ak}:{sk}".encode("utf-8")).decode("utf-8")
    return token


def extract_from_config(config_path: str) -> tuple:
    """从 config.py 中提取 ak 和 sk"""
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    ak_match = re.search(r'''ak\s*=\s*["']([^"']+)["']''', content)
    sk_match = re.search(r'''sk\s*=\s*["']([^"']+)["']''', content)
    if not ak_match or not sk_match:
        return None, None
    return ak_match.group(1), sk_match.group(1)


def main():
    args = sys.argv[1:]

    if len(args) == 2 and args[0] == "--config":
        ak, sk = extract_from_config(args[1])
        if not ak or not sk:
            print(f"错误: 在 {args[1]} 中未找到 ak/sk 配置")
            print("期望格式: ak = \"xxx\"  sk = \"xxx\"")
            sys.exit(1)
        print(f"配置文件: {args[1]}")
    elif len(args) == 2:
        ak, sk = args
    else:
        print("用法:")
        print("  python gen_auth.py <ak> <sk>")
        print("  python gen_auth.py --config <config.py>")
        sys.exit(1)

    token = gen_auth(ak, sk)
    print(f"ak: {ak}")
    print(f"sk: {sk}")
    print()
    print(f"Authorization header:")
    print(f"  Authorization: Lab {token}")
    print()
    print(f"curl 用法:")
    print(f'  curl -H "Authorization: Lab {token}" ...')
    print()
    print(f"Shell 变量:")
    print(f'  AUTH="Authorization: Lab {token}"')


if __name__ == "__main__":
    main()
