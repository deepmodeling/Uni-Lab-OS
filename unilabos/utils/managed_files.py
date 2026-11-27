"""工作区运行文件共用的大小上限和原子写入。"""

import os
import tempfile
from pathlib import Path

MAX_FILE_BYTES = 20 * 1024 * 1024


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
