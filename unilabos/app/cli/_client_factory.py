"""已认证 HTTP 客户端工厂

从 args + SessionManager 构造一个已注入 ak/sk 鉴权的 client.HTTPClient。
凭据按 resolve_effective_auth 的优先级解析。

需要在 SessionManager 上下文管理器内调用。
"""

import sys
from typing import Any, Tuple

from unilabos.client import HTTPClient, HTTPClientConfig, SessionManager, print_error

from .auth_resolver import resolve_effective_auth


def make_authenticated_client(
    args: Any, session_manager: SessionManager
) -> Tuple[HTTPClient, str]:
    """构造已认证的 HTTPClient

    Returns:
        (client, base_url)。如果凭据缺失，打印错误并 sys.exit(1)。
    """
    effective = resolve_effective_auth(args, session_manager)
    if not effective["ak"] or not effective["sk"]:
        print_error(
            "未找到 ak/sk。请通过以下方式之一配置：\n"
            "  1. unilab login --ak <ak> --sk <sk>\n"
            "  2. 命令行传入 --ak <ak> --sk <sk>\n"
            "  3. 在 local_config.py 中设置 BasicConfig.ak/sk"
        )
        sys.exit(1)

    import base64
    secret = base64.b64encode(f"{effective['ak']}:{effective['sk']}".encode("utf-8")).decode("utf-8")

    config = HTTPClientConfig(base_url=effective["base_url"])
    client = HTTPClient(config, get_auth_secret=lambda: secret)
    return client, effective["base_url"]
