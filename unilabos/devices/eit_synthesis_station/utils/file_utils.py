# -*- coding: utf-8 -*-
"""
功能:
    提供 Excel 文件安全写入与保存工具.
    当目标文件被 Excel 占用时, 优先通过 COM 精准定位目标工作簿,
    先保存再关闭目标工作簿, 然后重试写入.
    本模块不会关闭其他工作簿, 也不会终止 Excel 进程.
参数:
    无.
返回:
    无.
"""

import ctypes
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple, Union

import psutil

logger = logging.getLogger("FileUtils")

_EXCEL_PROC_NAMES = {"excel.exe", "microsoft excel"}
_SCAN_TIMEOUT_SECONDS = 10.0
_RELEASE_WAIT_SECONDS = 0.5


def _normalize_windows_path(path: Union[str, Path]) -> str:
    """
    功能:
        将路径转换为适合 Windows 比较的规范字符串.
    参数:
        path: str | Path, 待规范化的路径.
    返回:
        str, 统一大小写与分隔符后的绝对路径字符串.
    """
    if path is None:
        return ""

    path_str = str(path).strip()
    if path_str == "":
        return ""

    return os.path.normcase(os.path.normpath(str(Path(path_str).resolve())))


def _load_com_modules() -> Tuple[Any, Any]:
    """
    功能:
        延迟加载 pywin32 相关模块, 避免无 COM 环境下导入失败.
    参数:
        无.
    返回:
        Tuple[Any, Any], (pythoncom, win32com.client).
    """
    import pythoncom
    import win32com.client

    return pythoncom, win32com.client


def _iter_excel_workbooks_from_rot() -> Iterator[Any]:
    """
    功能:
        从 Running Object Table 中枚举已注册的 Excel 工作簿对象.
    参数:
        无.
    返回:
        Iterator[Any], 逐个返回可访问 FullName 的 Excel 工作簿对象.
    """
    pythoncom, win32_client = _load_com_modules()
    bind_context = pythoncom.CreateBindCtx(0)
    running_table = pythoncom.GetRunningObjectTable()
    enum_moniker = running_table.EnumRunning()

    while True:
        monikers = enum_moniker.Next(1)
        if not monikers:
            break

        moniker = monikers[0]
        try:
            display_name = str(moniker.GetDisplayName(bind_context, None)).lower()
        except Exception:
            display_name = ""

        # 先用显示名过滤, 避免访问无关 COM 对象.
        if display_name and ".xls" not in display_name and "excel" not in display_name:
            continue

        try:
            workbook = win32_client.Dispatch(running_table.GetObject(moniker))
            full_name = getattr(workbook, "FullName", "")
        except Exception:
            continue

        if isinstance(full_name, str) and full_name:
            yield workbook


def _iter_excel_workbooks_from_active_instance() -> Iterator[Any]:
    """
    功能:
        兼容只暴露活动实例的场景, 枚举当前活动 Excel 实例中的工作簿.
    参数:
        无.
    返回:
        Iterator[Any], 当前活动实例中的工作簿对象序列.
    """
    _, win32_client = _load_com_modules()
    excel = win32_client.GetActiveObject("Excel.Application")

    for workbook in excel.Workbooks:
        yield workbook


class _GUID(ctypes.Structure):
    """COM GUID 结构体, 用于 ctypes 调用 AccessibleObjectFromWindow."""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# IDispatch GUID: {00020400-0000-0000-C000-000000000046}
_IID_IDISPATCH = _GUID(
    0x00020400,
    0x0000,
    0x0000,
    (ctypes.c_ubyte * 8)(0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46),
)


def _iter_excel_workbooks_from_all_instances() -> Iterator[Any]:
    """
    功能:
        通过窗口句柄枚举所有 Excel 实例中的工作簿.
        使用 EnumWindows + AccessibleObjectFromWindow 技术,
        能覆盖多实例场景, 不依赖 ROT 或 GetActiveObject.
    参数:
        无.
    返回:
        Iterator[Any], 所有 Excel 实例中的工作簿对象序列.
    """
    import win32gui  # noqa: 延迟导入, 避免非 Windows 环境报错

    pythoncom, win32_client = _load_com_modules()

    excel_hwnds: list = []

    # 收集所有 XLMAIN 窗口句柄.
    def _collect_excel_windows(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) == "XLMAIN":
                excel_hwnds.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(_collect_excel_windows, None)

    seen_app_hwnds: set = set()

    for main_hwnd in excel_hwnds:
        # 查找 XLMAIN -> XLDESK -> EXCEL7 子窗口.
        try:
            desk_hwnd = win32gui.FindWindowEx(main_hwnd, 0, "XLDESK", None)
            if desk_hwnd == 0:
                continue
            excel7_hwnd = win32gui.FindWindowEx(desk_hwnd, 0, "EXCEL7", None)
            if excel7_hwnd == 0:
                continue
        except Exception:
            continue

        # 通过 AccessibleObjectFromWindow 获取 COM 对象.
        try:
            ptr = ctypes.c_void_p()
            hr = ctypes.windll.oleacc.AccessibleObjectFromWindow(
                excel7_hwnd,
                ctypes.c_long(-16),  # OBJID_NATIVEOM = 0xFFFFFFF0
                ctypes.byref(_IID_IDISPATCH),
                ctypes.byref(ptr),
            )
            if hr != 0 or not ptr.value:
                continue

            # 将原始 IDispatch 指针包装为 pywin32 COM 对象.
            window_obj = win32_client.Dispatch(
                pythoncom.ObjectFromAddress(ptr.value)
            )
            excel_app = window_obj.Application
        except Exception:
            continue

        # 用 Application.Hwnd 去重, 避免同一实例的多个窗口重复枚举.
        try:
            app_hwnd = excel_app.Hwnd
        except Exception:
            app_hwnd = main_hwnd

        if app_hwnd in seen_app_hwnds:
            continue
        seen_app_hwnds.add(app_hwnd)

        try:
            for workbook in excel_app.Workbooks:
                yield workbook
        except Exception:
            continue


def _close_target_workbook_from_iterable(workbooks: Iterator[Any], target_path: str) -> bool:
    """
    功能:
        在工作簿序列中查找目标文件, 命中后先保存再关闭.
    参数:
        workbooks  : Iterator[Any], Excel 工作簿对象序列.
        target_path: str, 规范化后的目标绝对路径.
    返回:
        bool, True 表示已保存并关闭目标工作簿, False 表示未命中.
    """
    seen_paths = set()

    for workbook in workbooks:
        try:
            workbook_path = _normalize_windows_path(getattr(workbook, "FullName", ""))
        except Exception:
            continue

        if workbook_path in seen_paths:
            continue
        seen_paths.add(workbook_path)

        logger.debug("COM 枚举到工作簿: %s | 目标: %s", workbook_path, target_path)

        if workbook_path != target_path:
            continue

        # 先保存 Excel 侧改动, 再关闭目标工作簿.
        workbook.Save()
        workbook.Close(SaveChanges=False)
        return True

    return False


def _cleanup_lock_file(lock_file: Path) -> None:
    """
    功能:
        清理 Excel 残留的锁文件.
    参数:
        lock_file: Path, 锁文件路径.
    返回:
        无.
    """
    if not lock_file.exists():
        return

    try:
        lock_file.unlink()
        logger.info("已删除残留 Excel 锁文件: %s", lock_file.name)
    except OSError as exc:
        try:
            os.chmod(lock_file, 0o666)  # 先放宽权限, 再重试删除残留锁文件.
            lock_file.unlink()
            logger.info("已删除残留 Excel 锁文件: %s", lock_file.name)
        except OSError as retry_exc:
            logger.warning("删除 Excel 锁文件失败 | 文件: %s | 错误: %s", lock_file.name, retry_exc)


def _try_close_excel_workbook(path: Path) -> bool:
    """
    功能:
        通过 Windows COM 精准定位目标 Excel 工作簿.
        命中后先保存, 再关闭目标工作簿, 不影响其他已打开文件.
    参数:
        path: Path, 目标工作簿路径.
    返回:
        bool, True 表示已通过 COM 保存并关闭目标工作簿, False 表示未成功关闭.
    """
    path = Path(path).resolve()
    target_path = _normalize_windows_path(path)
    pythoncom_module: Optional[Any] = None
    com_initialized = False

    try:
        pythoncom_module, _ = _load_com_modules()
        pythoncom_module.CoInitialize()
        com_initialized = True

        if _close_target_workbook_from_iterable(_iter_excel_workbooks_from_rot(), target_path):
            logger.info("已通过 COM 保存并关闭 Excel 工作簿: %s", path.name)
            return True

        if _close_target_workbook_from_iterable(_iter_excel_workbooks_from_active_instance(), target_path):
            logger.info("已通过活动 Excel 实例保存并关闭工作簿: %s", path.name)
            return True

        if _close_target_workbook_from_iterable(_iter_excel_workbooks_from_all_instances(), target_path):
            logger.info("已通过窗口句柄枚举保存并关闭工作簿: %s", path.name)
            return True

        logger.warning("未在运行中的 Excel 中找到目标工作簿, 无法自动关闭 | 文件: %s", path.name)
        return False
    except ImportError as exc:
        logger.warning("缺少 pywin32, 无法通过 COM 自动关闭 Excel 工作簿 | 文件: %s | 错误: %s", path.name, exc)
        return False
    except Exception as exc:
        logger.warning("通过 COM 保存并关闭 Excel 工作簿失败 | 文件: %s | 错误: %s", path.name, exc)
        return False
    finally:
        if com_initialized and pythoncom_module is not None:
            try:
                pythoncom_module.CoUninitialize()
            except Exception:
                logger.debug("COM 反初始化失败, 已忽略 | 文件: %s", path.name)


def _iter_excel_processes() -> Iterator[psutil.Process]:
    """
    功能:
        迭代当前系统中的 Excel 进程.
    参数:
        无.
    返回:
        Iterator[psutil.Process], Excel 进程对象序列.
    """
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc_name = (proc.info.get("name") or "").lower()
            if proc_name in _EXCEL_PROC_NAMES:
                yield proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _find_excel_lock_holders(path: Path, lock_file: Path) -> List[int]:
    """
    功能:
        扫描哪些 Excel 进程仍持有目标文件或其锁文件句柄.
    参数:
        path     : Path, 目标文件路径.
        lock_file: Path, Excel 锁文件路径.
    返回:
        List[int], 持有目标句柄的 Excel 进程 PID 列表.
    """
    holder_pids: List[int] = []
    scan_start = time.monotonic()

    for proc in _iter_excel_processes():
        if time.monotonic() - scan_start > _SCAN_TIMEOUT_SECONDS:
            logger.warning("Excel 文件句柄扫描超时, 已停止继续扫描")
            break

        try:
            open_files = proc.open_files() or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        held_paths = set()
        for open_file in open_files:
            try:
                held_paths.add(Path(open_file.path).resolve())
            except OSError:
                continue

        if path in held_paths or lock_file in held_paths:
            holder_pids.append(proc.info["pid"])

    return holder_pids


def release_file_lock(path: Union[str, Path]) -> bool:
    """
    功能:
        尝试以非破坏方式释放 Excel 对目标文件的占用.
        优先通过 COM 对目标工作簿执行保存并关闭.
        若目标文件已无 Excel 占用, 则仅清理残留锁文件.
    参数:
        path: str | Path, 目标 Excel 文件路径.
    返回:
        bool, True 表示已释放占用或清理残留锁文件, False 表示目标文件仍被 Excel 占用.
    """
    path = Path(path).resolve()
    lock_file = path.parent / f"~${path.name}"

    if _try_close_excel_workbook(path):
        time.sleep(_RELEASE_WAIT_SECONDS)  # 等待 Excel 释放文件句柄.
        _cleanup_lock_file(lock_file)
        return True

    holder_pids = _find_excel_lock_holders(path, lock_file)
    if holder_pids:
        logger.warning(
            "Excel 仍占用目标文件, 未执行任何强制关闭动作 | 文件: %s | PID=%s",
            path.name,
            holder_pids,
        )
        return False

    if lock_file.exists():
        logger.info("未检测到 Excel 占用, 锁文件视为残留文件并清理 | 文件: %s", lock_file.name)
        _cleanup_lock_file(lock_file)
        return True

    return False


def _build_permission_error_message(action_text: str, path: Path, retries: int) -> str:
    """
    功能:
        生成统一的 Excel 写权限失败提示.
    参数:
        action_text: str, 当前动作描述, 如写入或保存.
        path       : Path, 目标文件路径.
        retries    : int, 已重试次数.
    返回:
        str, 中文错误提示.
    """
    return (
        f"{action_text} Excel 文件失败, 已重试 {retries} 次仍无法获得写权限: {path}\n"
        "已尝试通过 COM 保存并关闭目标工作簿.\n"
        "已取消强制关闭 Excel 进程.\n"
        "如果当前环境未安装 pywin32, 系统将无法自动关闭 Excel 工作簿.\n"
        "请先在 Excel 中保存并关闭目标文件后重试."
    )


def safe_excel_write(df, path: Union[str, Path], retries: int = 3, delay: float = 2.0, **kwargs) -> None:
    """
    功能:
        将 DataFrame 安全写入 Excel 文件.
        遇到 PermissionError 时, 尝试先保存并关闭目标工作簿, 再重试写入.
    参数:
        df      : Any, 具有 to_excel 方法的对象.
        path    : str | Path, 目标 Excel 文件路径.
        retries : int, 最大重试次数.
        delay   : float, 每次重试前等待秒数.
        **kwargs: 透传给 to_excel 的额外参数.
    返回:
        无.
    """
    path = Path(path)

    for attempt in range(1, retries + 1):
        try:
            df.to_excel(path, **kwargs)
            if attempt > 1:
                logger.info("Excel 写入成功 | 文件: %s | 第 %d 次尝试", path.name, attempt)
            return
        except PermissionError as exc:
            logger.warning(
                "写入 Excel 权限不足, 准备尝试保存并关闭目标工作簿 | 文件: %s | 第 %d/%d 次 | 错误: %s",
                path.name,
                attempt,
                retries,
                exc,
            )

            released = release_file_lock(path)
            if released:
                logger.info("目标工作簿已释放, 等待文件句柄回收 | 文件: %s", path.name)
            else:
                logger.warning("未能自动释放目标工作簿, 将继续按重试策略处理 | 文件: %s", path.name)

            time.sleep(delay)
            if attempt == retries:
                raise PermissionError(_build_permission_error_message("写入", path, retries)) from exc


def safe_workbook_save(wb, path: Union[str, Path], retries: int = 3, delay: float = 2.0) -> None:
    """
    功能:
        将 openpyxl Workbook 安全保存到文件.
        遇到 PermissionError 时, 尝试先保存并关闭目标工作簿, 再重试保存.
    参数:
        wb      : Any, 具有 save 方法的 Workbook 对象.
        path    : str | Path, 目标文件路径.
        retries : int, 最大重试次数.
        delay   : float, 每次重试前等待秒数.
    返回:
        无.
    """
    path = Path(path)

    for attempt in range(1, retries + 1):
        try:
            wb.save(path)
            if attempt > 1:
                logger.info("Excel 保存成功 | 文件: %s | 第 %d 次尝试", path.name, attempt)
            return
        except PermissionError as exc:
            logger.warning(
                "保存 Excel 权限不足, 准备尝试保存并关闭目标工作簿 | 文件: %s | 第 %d/%d 次 | 错误: %s",
                path.name,
                attempt,
                retries,
                exc,
            )

            released = release_file_lock(path)
            if released:
                logger.info("目标工作簿已释放, 等待文件句柄回收 | 文件: %s", path.name)
            else:
                logger.warning("未能自动释放目标工作簿, 将继续按重试策略处理 | 文件: %s", path.name)

            time.sleep(delay)
            if attempt == retries:
                raise PermissionError(_build_permission_error_message("保存", path, retries)) from exc
