"""直接访问 Edge 物料权威的命令。"""

import sys

from unilabos.client import SessionManager, print_error, print_output
from unilabos.config.config import BasicConfig
from unilabos.client.materials import HTTPMaterialsClient


def cmd_material_list(args, session_manager: SessionManager):
    """列出微后端中的物料实例。"""

    try:
        address = str(getattr(args, "address_resolved", "") or "").strip()
        if not address:
            address = str(
                getattr(args, "material_microbackend_addr", "") or ""
            ).strip()
        if not address:
            from unilabos.app.cli.auth_resolver import resolve_effective_auth

            with session_manager:
                effective = resolve_effective_auth(args, session_manager)
            if effective["base_url_source"] != "default":
                address = effective["base_url"]
        if not address:
            port = getattr(args, "port_management", None) or BasicConfig.port
            address = f"http://127.0.0.1:{port}"
        materials = HTTPMaterialsClient(address).list_materials(
            roots_only=bool(args.roots_only)
        )
        print_output(
            [item.model_dump(mode="json", exclude_none=False) for item in materials]
        )
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"获取物料列表失败: {e}")
        sys.exit(1)
