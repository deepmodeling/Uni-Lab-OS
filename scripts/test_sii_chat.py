#!/usr/bin/env python3
"""通过本地 HTTP 代理调用 SII OpenAPI chat completions。"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE_URL = (
    "https://o8kjqm58o8ogcm5ek8aggddkb5ggk8dp.openapi-sj.sii.edu.cn"
)
API_URL = f"{BASE_URL}/v1/chat/completions"
MODELS_URL = f"{BASE_URL}/v1/models"
DEFAULT_API_KEY = "MajUa5noC1OtfZ3RxznY23AZYWYisTPGc4MKZJyXB9Q="
PROXY = "http://bj:bj20260107@127.0.0.1:7899"


def _opener():
    proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    https_handler = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    return urllib.request.build_opener(proxy_handler, https_handler)


def list_models(api_key: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        MODELS_URL,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        print(f"HTTP {resp.status} GET /v1/models")
        return json.loads(raw)


def post_chat(payload: dict, api_key: str, timeout: int = 180) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        print(f"HTTP {resp.status}")
        return json.loads(raw)


def dump_result(title: str, data: dict) -> None:
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:8000])
    print()
    choices = data.get("choices") or []
    if not choices:
        return
    msg = choices[0].get("message") or {}
    for key in ("reasoning", "reasoning_content", "content"):
        text = msg.get(key)
        if text:
            print(f"--- {key} ---")
            print(text)
            print()


def main() -> int:
    api_key = os.environ.get("INF_API_KEY", DEFAULT_API_KEY)
    ok = True
    print("\n>>> GET /v1/models")
    try:
        models = list_models(api_key)
        print(json.dumps(models, ensure_ascii=False, indent=2)[:12000])
    except urllib.error.HTTPError as exc:
        ok = False
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTPError {exc.code}: {err_body}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"失败: {type(exc).__name__}: {exc}")

    version_q = (
        "请如实回答：你的具体模型全名/checkpoint 是什么？"
        "是 DeepSeek-V4-Pro-0813 正式版、V4-Pro Preview，"
        "还是 DeepSeek-V4-Flash-0731？不要编造。"
    )
    cases = [
        (
            "ds-v4-pro 版本自报",
            {
                "model": "ds-v4-pro",
                "messages": [{"role": "user", "content": version_q}],
            },
        ),
        (
            "dsv4-flash-0731 简要介绍",
            {
                "model": "dsv4-flash-0731",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "请简要介绍一下你自己"},
                ],
            },
        ),
        (
            "dsv4-flash-0731 thinking: sqrt(2)",
            {
                "model": "dsv4-flash-0731",
                "messages": [
                    {"role": "user", "content": "证明 sqrt(2) 是无理数"},
                ],
                "chat_template_kwargs": {"thinking": True},
                "temperature": 0.6,
                "top_p": 0.95,
                "max_tokens": 16384,
            },
        ),
        (
            "dsv4-flash-0731 版本自报",
            {
                "model": "dsv4-flash-0731",
                "messages": [{"role": "user", "content": version_q}],
            },
        ),
    ]

    for title, payload in cases:
        print(f"\n>>> 请求: {title}")
        try:
            data = post_chat(payload, api_key)
            dump_result(title, data)
        except urllib.error.HTTPError as exc:
            ok = False
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"HTTPError {exc.code}: {err_body}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"失败: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
