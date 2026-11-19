#!/usr/bin/env python3
"""从原生授予策略的 C 头文件生成 Python ctypes 绑定（unilabos/server/backend/scheduler/grant/_abi.py）。

头文件 ``native/grant-policy/include/unilab_grant_policy.h`` 由 cargo build 经 cbindgen 从 Rust 源码生成；
本脚本再把它翻译成 ctypes 声明：``#define`` 整数常量、``typedef struct`` 结构体、函数原型。
只支持 cbindgen 为这个库输出的 C 子集（定宽整数、size_t、指针、结构体），遇到不认识的写法直接报错。

    python scripts/gen_grant_bindings.py            # 重新生成
    python scripts/gen_grant_bindings.py --check    # 与已提交的绑定不一致时退出码 1
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native" / "grant-policy" / "include" / "unilab_grant_policy.h"
OUTPUT = ROOT / "unilabos" / "server" / "backend" / "scheduler" / "grant" / "_abi.py"

SCALARS = {
    "void": "None",
    "uint8_t": "ctypes.c_uint8",
    "int8_t": "ctypes.c_int8",
    "uint16_t": "ctypes.c_uint16",
    "int16_t": "ctypes.c_int16",
    "uint32_t": "ctypes.c_uint32",
    "int32_t": "ctypes.c_int32",
    "uint64_t": "ctypes.c_uint64",
    "int64_t": "ctypes.c_int64",
    "size_t": "ctypes.c_size_t",
    "uintptr_t": "ctypes.c_size_t",
    "intptr_t": "ctypes.c_ssize_t",
}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _ctype(declaration: str, structs: set[str]) -> str:
    """把 ``const struct UniGrantWaiting *`` / ``uint32_t`` 之类翻成 ctypes 表达式。"""

    text = declaration.replace("const", " ").replace("struct", " ")
    pointers = text.count("*")
    base = text.replace("*", " ").split()
    if len(base) != 1:
        raise SystemExit(f"不支持的类型写法：{declaration!r}")
    name = base[0]
    if name in SCALARS:
        ctype = SCALARS[name]
    elif name in structs:
        ctype = name
    else:
        raise SystemExit(f"未知类型：{declaration!r}")
    for _ in range(pointers):
        ctype = "ctypes.c_void_p" if ctype == "None" else f"ctypes.POINTER({ctype})"
    return ctype


def _split_declaration(text: str) -> tuple[str, str]:
    """``const uint32_t *keys`` → (``const uint32_t *``, ``keys``)。"""

    match = re.fullmatch(r"(.*?[\s*])([A-Za-z_]\w*)", text.strip())
    if match is None:
        raise SystemExit(f"无法解析声明：{text!r}")
    return match.group(1).strip(), match.group(2)


def generate(header_text: str) -> str:
    source = _strip_comments(header_text)
    constants = re.findall(r"^\s*#define\s+([A-Z][A-Z0-9_]*)\s+(-?\d+)\s*$", source, flags=re.M)
    struct_blocks = re.findall(r"typedef\s+struct\s+(\w+)\s*\{(.*?)\}\s*(\w+)\s*;", source, flags=re.S)
    structs = {name for name, _body, _alias in struct_blocks}
    functions = re.findall(r"^\s*([A-Za-z_][\w\s*]*?[\s*])([a-z_]\w*)\s*\(([^)]*)\)\s*;", source, flags=re.M | re.S)

    digest = hashlib.sha256(header_text.encode("utf-8")).hexdigest()
    lines = [
        '"""原生锁授予策略的 ctypes 声明。',
        "",
        "由 scripts/gen_grant_bindings.py 从 native/grant-policy/include/unilab_grant_policy.h 生成，不要手改；",
        "头文件本身由 cargo build 经 cbindgen 从 Rust 源码生成。",
        "",
        f"头文件 sha256: {digest}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import ctypes",
        "",
    ]
    for name, value in constants:
        lines.append(f"{name} = {int(value)}")
    for name, body, alias in struct_blocks:
        if alias != name:
            raise SystemExit(f"结构体别名必须与名称一致：{name} / {alias}")
        lines += ["", "", f"class {name}(ctypes.Structure):", "    _fields_ = ["]
        for field in (item.strip() for item in body.split(";")):
            if field:
                type_text, field_name = _split_declaration(field)
                lines.append(f'        ("{field_name}", {_ctype(type_text, structs)}),')
        lines.append("    ]")
    lines += ["", "", "# 函数名 → (返回类型, 参数类型)", "FUNCTIONS = {"]
    for restype, name, arguments in functions:
        arguments = " ".join(arguments.split())
        argtypes = [] if arguments in {"", "void"} else [
            _ctype(_split_declaration(argument)[0], structs) for argument in arguments.split(",")
        ]
        if len(argtypes) <= 2:
            lines.append(f'    "{name}": ({_ctype(restype, structs)}, [{", ".join(argtypes)}]),')
        else:
            lines.append(f'    "{name}": ({_ctype(restype, structs)}, [')
            lines.extend(f"        {argtype}," for argtype in argtypes)
            lines.append("    ]),")
    lines += [
        "}",
        "",
        "",
        "def bind(library: ctypes.CDLL) -> ctypes.CDLL:",
        '    """给已加载的库设置全部函数原型。"""',
        "",
        "    for name, (restype, argtypes) in FUNCTIONS.items():",
        "        function = getattr(library, name)",
        "        function.restype = restype",
        "        function.argtypes = argtypes",
        "    return library",
        "",
    ]
    return "\n".join(lines)


def _current(output: Path) -> str:
    return output.read_text(encoding="utf-8") if output.is_file() else ""


def write(header: Path = HEADER, output: Path = OUTPUT) -> bool:
    """重新生成绑定；有变化时写盘并返回 True。"""

    generated = generate(header.read_text(encoding="utf-8"))
    if _current(output) == generated:
        return False
    output.write_text(generated, encoding="utf-8")
    print(f"已生成 {output}")
    return True


def main_check(header: Path = HEADER, output: Path = OUTPUT) -> int:
    if _current(output) != generate(header.read_text(encoding="utf-8")):
        print(f"{output} 与 {header} 不一致：运行 python scripts/gen_grant_bindings.py", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查已提交的绑定是否最新")
    parser.add_argument("--header", type=Path, default=HEADER)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.check:
        return main_check(args.header, args.output)
    write(args.header, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
