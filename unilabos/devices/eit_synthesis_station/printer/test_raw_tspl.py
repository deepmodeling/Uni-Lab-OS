"""
功能:
    诊断脚本: 绕过TSCLIB.dll, 通过Windows打印API直接发送TSPL原始命令到打印机.
    用于验证佳博1134T是否支持TSPL指令集.

用法:
    python test_raw_tspl.py
"""

import ctypes
import ctypes.wintypes as wt

# ──────────────── Windows Spooler API 声明 ────────────────
winspool = ctypes.WinDLL("winspool.drv")

# OpenPrinterW
winspool.OpenPrinterW.argtypes = [
    ctypes.c_wchar_p,                # pPrinterName
    ctypes.POINTER(wt.HANDLE),       # phPrinter
    ctypes.c_void_p,                 # pDefault (NULL)
]
winspool.OpenPrinterW.restype = wt.BOOL


class DOC_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("pDocName", ctypes.c_wchar_p),
        ("pOutputFile", ctypes.c_wchar_p),
        ("pDatatype", ctypes.c_wchar_p),
    ]


# StartDocPrinterW
winspool.StartDocPrinterW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(DOC_INFO_1W)]
winspool.StartDocPrinterW.restype = wt.DWORD

# StartPagePrinter
winspool.StartPagePrinter.argtypes = [wt.HANDLE]
winspool.StartPagePrinter.restype = wt.BOOL

# WritePrinter
winspool.WritePrinter.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
winspool.WritePrinter.restype = wt.BOOL

# EndPagePrinter
winspool.EndPagePrinter.argtypes = [wt.HANDLE]
winspool.EndPagePrinter.restype = wt.BOOL

# EndDocPrinter
winspool.EndDocPrinter.argtypes = [wt.HANDLE]
winspool.EndDocPrinter.restype = wt.BOOL

# ClosePrinter
winspool.ClosePrinter.argtypes = [wt.HANDLE]
winspool.ClosePrinter.restype = wt.BOOL


def send_raw_tspl(printer_name, tspl_commands):
    """
    功能:
        通过Windows Spooler API以RAW模式向打印机发送TSPL原始命令.

    参数:
        printer_name: str, Windows中的打印机名称
        tspl_commands: str, TSPL指令(多行, 用\\n分隔)

    返回:
        bool, 发送成功返回True
    """
    h_printer = wt.HANDLE()

    # 打开打印机
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(h_printer), None):
        print(f"[错误] 无法打开打印机: '{printer_name}'")
        return False
    print(f"[成功] 已打开打印机: '{printer_name}'")

    # 设置RAW数据模式
    doc_info = DOC_INFO_1W()
    doc_info.pDocName = "TSPL Test"
    doc_info.pOutputFile = None
    doc_info.pDatatype = "RAW"

    job_id = winspool.StartDocPrinterW(h_printer, 1, ctypes.byref(doc_info))
    if job_id == 0:
        print("[错误] StartDocPrinter失败")
        winspool.ClosePrinter(h_printer)
        return False
    print(f"[成功] 创建打印任务, JobID={job_id}")

    winspool.StartPagePrinter(h_printer)

    # 发送TSPL命令(以字节形式)
    data = tspl_commands.encode("ascii", errors="replace")
    bytes_written = wt.DWORD(0)
    ok = winspool.WritePrinter(
        h_printer,
        ctypes.c_char_p(data),
        len(data),
        ctypes.byref(bytes_written),
    )
    print(f"[信息] WritePrinter: ok={ok}, 写入字节={bytes_written.value}/{len(data)}")

    winspool.EndPagePrinter(h_printer)
    winspool.EndDocPrinter(h_printer)
    winspool.ClosePrinter(h_printer)

    if ok and bytes_written.value == len(data):
        print("[成功] TSPL命令已发送到打印机")
        return True
    else:
        print("[错误] 写入数据不完整")
        return False


if __name__ == "__main__":
    PRINTER_NAME = "Gprinter GP-1134T"

    # 简单的TSPL测试命令: 设置纸张 -> 清除缓冲 -> 打印文字 -> 出纸
    tspl_test = (
        "SIZE 56 mm, 10 mm\n"
        "GAP 2 mm, 0 mm\n"
        "DIRECTION 1\n"
        "CLS\n"
        'TEXT 10,10,"3",0,1,1,"TSPL TEST"\n'
        "PRINT 1,1\n"
    )

    print(f"目标打印机: {PRINTER_NAME}")
    print(f"发送TSPL命令:\n{tspl_test}")
    print("-" * 40)

    send_raw_tspl(PRINTER_NAME, tspl_test)
