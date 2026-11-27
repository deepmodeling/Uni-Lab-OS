"""包内相对路径与摘要的公共校验。"""
import hashlib
from pathlib import Path


def inside(root, relative):
    """把 POSIX 相对路径解析到 root 之内；拒绝反斜杠、空段、. 与 .. 以及越界。"""
    root = Path(root).resolve()
    if not isinstance(relative, str) or '\\' in relative or any(x in ('', '.', '..') for x in relative.split('/')):
        raise ValueError('Invalid asset code path')
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or result == root:
        raise ValueError('Asset code escapes package')
    return result


def sha(path):
    """文件内容的 sha256 十六进制摘要。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_sha(path):
    """项目文本文件的行尾无关摘要：CRLF 统一为 LF 后取 sha256。

    同一提交在 Windows（autocrlf 检出 CRLF）和 macOS/Linux（LF）上字节不同；绑定依据要求的是
    同一份内容，不是同一种检出方式。
    """
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def text_sha_matches(path, expected):
    """依据摘要核对：接受行尾无关摘要，也接受旧锁里按某一平台检出字节（LF 或 CRLF）写的摘要。"""
    lf = Path(path).read_bytes().replace(b'\r\n', b'\n')
    forms = (lf, lf.replace(b'\n', b'\r\n'))
    return expected in {hashlib.sha256(form).hexdigest() for form in forms}
