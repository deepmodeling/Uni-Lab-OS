"""有效凭据解析

按优先级聚合来自不同来源的 ak/sk 与 base_url：
  1. CLI 参数 (--ak / --sk / --address)
  2. 会话文件 (working_dir/session.json)
  3. 本地配置 (working_dir/local_config.py 的 BasicConfig.ak/sk + HTTPConfig.remote_addr)

"""

import os
from typing import Any, Dict, Optional

from unilabos.client import SessionManager


def _try_load_local_config(working_dir: str) -> Optional[Dict[str, str]]:
    """尝试从 working_dir/local_config.py 读取 BasicConfig.ak/sk + HTTPConfig.remote_addr

    返回 None 表示文件不存在或加载失败；返回 dict 时只包含真实存在的字段。
    """
    config_path = os.path.join(working_dir, "local_config.py")
    if not os.path.isfile(config_path):
        return None

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_unilab_local_config", config_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None

    out: Dict[str, str] = {}
    basic = getattr(module, "BasicConfig", None)
    if basic is not None:
        ak = getattr(basic, "ak", "")
        sk = getattr(basic, "sk", "")
        if ak:
            out["ak"] = ak
        if sk:
            out["sk"] = sk
    http = getattr(module, "HTTPConfig", None)
    if http is not None:
        addr = getattr(http, "remote_addr", "")
        if addr:
            out["base_url"] = addr
    return out


def resolve_effective_auth(args: Any, session_manager: SessionManager) -> Dict[str, str]:
    """解析当前有效的 ak/sk + base_url，并标注每个值的来源

    必须在 SessionManager 上下文管理器内调用（已加载 state）。

    Returns:
        {
          "ak": str, "ak_source": "cli|session|config|none",
          "sk": str, "sk_source": "cli|session|config|none",
          "base_url": str, "base_url_source": "cli|session|config|none",
        }

    base_url 不做任何云端兜底；三个来源都缺失时返回空串，
    由调用方决定回退行为（通常是本机微后端）。
    """
    state = session_manager.get_state()

    cli_ak = getattr(args, "ak", "") or ""
    cli_sk = getattr(args, "sk", "") or ""
    cli_addr = getattr(args, "address_resolved", None)
    if cli_addr is None:
        cli_addr = getattr(args, "addr_resolved", None)

    config_data = _try_load_local_config(str(session_manager.working_dir))
    cfg_ak = (config_data or {}).get("ak", "")
    cfg_sk = (config_data or {}).get("sk", "")
    cfg_base_url = (config_data or {}).get("base_url", "")

    # ak: CLI > session > config
    if cli_ak:
        ak, ak_source = cli_ak, "cli"
    elif state.auth.ak:
        ak, ak_source = state.auth.ak, "session"
    elif cfg_ak:
        ak, ak_source = cfg_ak, "config"
    else:
        ak, ak_source = "", "none"

    # sk: 同上
    if cli_sk:
        sk, sk_source = cli_sk, "cli"
    elif state.auth.sk:
        sk, sk_source = state.auth.sk, "session"
    elif cfg_sk:
        sk, sk_source = cfg_sk, "config"
    else:
        sk, sk_source = "", "none"

    # base_url: CLI > session > config；无兜底默认
    if cli_addr:
        base_url, base_url_source = cli_addr, "cli"
    elif state.base_url:
        base_url, base_url_source = state.base_url, "session"
    elif cfg_base_url:
        base_url, base_url_source = cfg_base_url, "config"
    else:
        base_url, base_url_source = "", "none"

    return {
        "ak": ak,
        "ak_source": ak_source,
        "sk": sk,
        "sk_source": sk_source,
        "base_url": base_url,
        "base_url_source": base_url_source,
    }
