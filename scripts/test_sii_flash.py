#!/usr/bin/env python3
"""通过本地 HTTP 代理调用 SII Flash OpenAPI。"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request

BASE_URL = "https://d8cj8amkhbkccckmh5g9cpdh5ogm8cpk.openapi-sj.sii.edu.cn"
API_KEY = "EnlfIIG26Oo7LPZTmjSqdvx8gf57VSsaUnpXT8CuYRc="
PROXY = "http://bj:bj20260107@127.0.0.1:7899"


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def request(url: str, *, data: bytes | None = None, timeout: int = 180) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers=headers,
    )
    with opener().open(req, timeout=timeout) as resp:
        print(f"HTTP {resp.status} {req.get_method()} {url}")
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ok = True
    print("=== GET /v1/models ===")
    try:
        models = request(f"{BASE_URL}/v1/models", timeout=30)
        print(json.dumps(models, ensure_ascii=False, indent=2)[:12000])
    except urllib.error.HTTPError as exc:
        ok = False
        print(f"HTTPError {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"{type(exc).__name__}: {exc}")

    print("\n=== POST chat: 9.8 vs 9.11 ===")
    payload = {
        "model": "dsv4-flash-0731",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "9.8和9.11哪一个大?"},
        ],
    }
    try:
        data = request(
            f"{BASE_URL}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        print(json.dumps(data, ensure_ascii=False, indent=2)[:8000])
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        for key in ("reasoning", "reasoning_content", "content"):
            text = msg.get(key)
            if text:
                print(f"--- {key} ---")
                print(text)
    except urllib.error.HTTPError as exc:
        ok = False
        print(f"HTTPError {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"{type(exc).__name__}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
