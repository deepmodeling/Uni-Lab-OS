"""Send one release notification after an installer is verified in OSS."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--oss-url", required=True)
    args = parser.parse_args()

    webhook = os.environ.get("FEISHU_WEBHOOK_URL")
    if not webhook:
        print("未配置 FEISHU_WEBHOOK_URL，跳过发布通知", flush=True)
        return
    text = "\n".join(
        (
            "Uni-Lab-OS installer published",
            "System: " + args.system,
            "Version: " + args.version,
            "OSS: " + args.oss_url,
        )
    )
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    request = Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as error:
        raise SystemExit("Feishu webhook request failed: " + str(error)) from error
    try:
        body = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise SystemExit("Feishu webhook returned invalid JSON") from error
    if body.get("code", 0) != 0:
        raise SystemExit("Feishu webhook rejected the notification: " + str(body.get("msg", body)))
    print("Feishu release notification sent", flush=True)


if __name__ == "__main__":
    main()
