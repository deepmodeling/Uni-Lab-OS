"""工作目录（working_dir）的统一解析：配置文件、日志、数据库、驱动包等运行数据都放在这里。

规则（主入口与轻量子命令共用）：

1. ``--working_dir`` 指定时使用它；
2. 否则 ``--config`` 指向的文件存在时，使用其所在目录；
3. 否则使用当前目录；
4. 以上目录本身名为 ``.unilabos`` 或 ``unilabos_data`` 时直接使用；其下有这样的子目录时使用该子目录；
5. 第 3 步的当前目录不满足第 4 步时，使用默认的 ``~/.unilabos``。显式指定的目录（第 1、2 步）原样使用。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: 未指定时的默认工作目录；其下放 local_config.py、logs/、database/ 等。
DEFAULT_WORKING_DIR = "~/.unilabos"
#: 可以直接作为工作目录的目录名；``unilabos_data`` 是旧版本在当前目录下生成的名字。
WORKING_DIR_NAMES = ("unilabos_data", ".unilabos")


def _adopt(path: Path) -> Optional[Path]:
    if path.name in WORKING_DIR_NAMES:
        return path
    for name in WORKING_DIR_NAMES:
        candidate = path / name
        if candidate.is_dir():
            return candidate
    return None


def resolve_working_dir(
    raw_working_dir: Optional[str] = None,
    config_path: Optional[str] = None,
    *,
    cwd: Optional[str] = None,
) -> str:
    """按模块说明的规则返回工作目录的绝对路径（不创建目录）。"""

    if raw_working_dir:
        path = Path(os.path.abspath(os.path.expanduser(raw_working_dir)))
        return str(_adopt(path) or path)
    if config_path and os.path.exists(config_path):
        path = Path(os.path.dirname(os.path.abspath(os.path.expanduser(config_path))))
        return str(_adopt(path) or path)
    current = Path(os.path.abspath(cwd or os.getcwd()))
    adopted = _adopt(current)
    if adopted is not None:
        return str(adopted)
    return os.path.abspath(os.path.expanduser(DEFAULT_WORKING_DIR))


__all__ = ["DEFAULT_WORKING_DIR", "WORKING_DIR_NAMES", "resolve_working_dir"]
