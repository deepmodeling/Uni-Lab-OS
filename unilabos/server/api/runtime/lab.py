"""实验室布局路由已按 V1 合同迁到 :mod:`unilabos.server.api.materials.lab`（档案 / 分区 / 摆放在 materials.db，
dev 网格文档并入档案的 ``grid``）；这里保留旧导入位置。"""

from unilabos.server.api.materials.lab import create_lab_router, install_lab_api

__all__ = ["create_lab_router", "install_lab_api"]
