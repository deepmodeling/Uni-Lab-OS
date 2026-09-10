"""工作流创作可信静态子集使用的 Python 源码身份合同。"""

from __future__ import annotations

import keyword
import unicodedata


class PythonSourceIdentityError(ValueError):
    """源码身份不能安全生成静态 Python import。"""


def validate_python_source_identity(raw_identity: object) -> tuple[str, str]:
    """校验 ``module.path:symbol`` 形式的 Python 源码身份。

    参数说明：``raw_identity`` 是注册表或持久投影提供的不可信值。返回：模块名
    与符号名二元组；空白改写、控制字符、非单一冒号、非法点分标识符或 Python
    关键字均抛出 ``PythonSourceIdentityError``。
    """

    if (
        not isinstance(raw_identity, str)
        or not raw_identity
        or raw_identity != raw_identity.strip()
        or raw_identity.count(":") != 1
        or any(unicodedata.category(character).startswith("C") for character in raw_identity)
    ):
        raise PythonSourceIdentityError("源码身份必须是无控制字符的 module:symbol")
    module, symbol = raw_identity.split(":", 1)
    module_parts = module.split(".")
    if not module_parts or any(
        not _is_safe_identifier(part) for part in module_parts
    ):
        raise PythonSourceIdentityError("源码身份模块必须由合法点分 Python 标识符组成")
    if not _is_safe_identifier(symbol):
        raise PythonSourceIdentityError("源码身份符号必须是非关键字 Python 标识符")
    return module, symbol


def canonical_python_source_identity(raw_identity: object) -> str:
    """返回已经过可信静态子集校验的规范源码身份。

    参数说明：``raw_identity`` 是任意可疑值。返回：保持大小写的
    ``module.path:symbol`` 字符串；非法输入透传 ``PythonSourceIdentityError``。
    """

    module, symbol = validate_python_source_identity(raw_identity)
    return f"{module}:{symbol}"


def _is_safe_identifier(value: str) -> bool:
    """判断一个名称是否为可安全生成的 Python 标识符。

    参数说明：``value`` 是单个模块段或导入符号。返回：合法、非关键字且不含
    控制字符时为 ``True``，否则为 ``False``。
    """

    return (
        bool(value)
        and value.isidentifier()
        and not keyword.iskeyword(value)
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


__all__ = [
    "PythonSourceIdentityError",
    "canonical_python_source_identity",
    "validate_python_source_identity",
]
