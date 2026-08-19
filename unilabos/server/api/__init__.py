"""微后端 HTTP API 安装入口。"""

from unilabos.server.api.materials import create_materials_router, install_materials_api

__all__ = ["create_materials_router", "install_materials_api"]
