"""独立 Bullet 后端（B 方案）：RB_* 兼容层 + ctypes 加载器。

与 ``unilabos.simulation.bullet_native`` 不同，本包不导入 bpy，也不链接 Blender
进程内符号；``compat`` 子模块可直接替代 ``bullet_native`` 的模块面。
"""

from .loader import (
    BulletABI,
    BulletBuildError,
    LoaderError,
    availability,
    build_available,
    library_path,
    load,
)

__all__ = [
    "BulletABI",
    "BulletBuildError",
    "LoaderError",
    "availability",
    "build_available",
    "library_path",
    "load",
]
