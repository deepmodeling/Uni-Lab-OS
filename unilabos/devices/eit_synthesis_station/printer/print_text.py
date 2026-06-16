"""
功能:
    TSPL兼容标签打印机交互式打印脚本.
    通过Windows打印机名称连接打印机, 用户输入文字后直接打印.
    支持TSC, 佳博(Gainscha)等TSPL兼容打印机.
    纸张/字体/位置参数保存在config.yaml中.

依赖:
    pyyaml (pip install pyyaml)

用法:
    python print_text.py
"""

import ctypes
import logging
import os
import subprocess
import sys

import yaml

# ──────────────────────────── 日志配置 ────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────── 常量 ────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "25x10x2.yaml")
DLL_PATH = os.path.join(SCRIPT_DIR, "libs", "TSCLIB.dll")

# 默认配置, 首次运行时写入config.yaml
DEFAULT_CONFIG = {
    "printer": {
        "port": "Gprinter GP-1134T",  # Windows打印机名称
        "ppi": 300,
    },
    "paper": {
        "width": 56,
        "height": 10,
        "unit": "mm",
        "columns": 2,
        "column_gap": 3,
        "margin": 1.8,
        "gap": 0,
        "gap_offset": 0,
        "direction": 1,
    },
    "font": {
        "name": "微软雅黑",
        "size": 60,  # 字号上限(dot), 仅溢出时缩小
        "bold": 0,
        "underline": 0,
        "rotation": 0,
    },
    "position": {
        "x": 10,
        "y": 30,
    },
}


def load_config(path):
    """
    功能:
        读取YAML配置文件, 若不存在则创建默认配置.

    参数:
        path: str, 配置文件路径

    返回:
        dict, 配置字典
    """
    if not os.path.exists(path):
        logger.info("配置文件不存在, 正在生成默认配置: %s", path)
        save_config(path, DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.debug("已加载配置: %s", path)
    return config


def save_config(path, config):
    """
    功能:
        将配置字典写入YAML文件.

    参数:
        path: str, 配置文件路径
        config: dict, 配置字典
    """
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_dll(dll_path):
    """
    功能:
        加载TSCLIB.dll动态链接库.

    参数:
        dll_path: str, DLL文件路径

    返回:
        ctypes.WinDLL 实例
    """
    if not os.path.exists(dll_path): 
        logger.error("找不到DLL文件: %s", dll_path)
        sys.exit(1)

    lib = ctypes.WinDLL(dll_path)
    logger.debug("已加载DLL: %s", dll_path)

    # 声明常用函数的参数类型, 确保ctypes正确传递参数
    wstr = ctypes.c_wchar_p
    lib.openportW.argtypes = [wstr]
    lib.openportW.restype = ctypes.c_int  # 0=失败, 非0=成功
    lib.closeport.argtypes = []
    lib.sendcommandW.argtypes = [wstr]
    lib.printlabelW.argtypes = [wstr, wstr]
    # windowsfontUnicode(x, y, fontheight, rotation, fontstyle, fontunderline, szFaceName, content)
    # 前6个int, 第7个char*(字体名), 第8个void*(UTF-16LE字节缓冲区, 不能用c_char_p会截断\x00)
    lib.windowsfontUnicode.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
    ]
    lib.windowsfontUnicode.restype = ctypes.c_int
    return lib


def list_windows_printers():
    """
    功能:
        列出系统中已安装的Windows打印机名称, 帮助用户查找正确的打印机端口名称.

    返回:
        list[str], 打印机名称列表. 获取失败时返回空列表.
    """
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-Printer | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except Exception as e:
        logger.warning("无法枚举系统打印机: %s", e)
    return []


def calc_label_width(paper):
    """
    功能:
        根据纸张总宽度/列数/边距/列间距计算单个标签的宽度.

    参数:
        paper: dict, 纸张配置

    返回:
        float, 单个标签宽度(与paper.unit同单位)
    """
    total_width = paper["width"]
    columns = paper.get("columns", 1)
    margin = paper.get("margin", 0)
    column_gap = paper.get("column_gap", 0)

    # 标签宽度 = (总宽 - 左右边距×2 - 列间距×(列数-1)) / 列数
    label_width = (total_width - margin * 2 - column_gap * (columns - 1)) / columns
    return round(label_width, 2)


def calc_dots_per_mm(config):
    """
    功能:
        根据配置中的PPI计算每毫米点数.

    参数:
        config: dict, 配置字典

    返回:
        float, 每毫米的点数(dots per mm)
    """
    ppi = config["printer"].get("ppi", 203)
    # 1英寸 = 25.4mm
    return ppi / 25.4


def calc_auto_layout(paper, font_cfg, text, dots_per_mm):
    """
    功能:
        根据标签尺寸和YAML字号上限计算布局.
        字号由标签物理尺寸决定, 不随文字长度逐字变化.
        仅当文字溢出一行时才尝试换行, 换行仍溢出才缩小字号.

    参数:
        paper: dict, 纸张配置
        font_cfg: dict, 字体配置 (font_cfg["size"]为字号上限, 单位dot)
        text: str, 要打印的文字
        dots_per_mm: float, 每毫米点数(由PPI计算得到)

    返回:
        list[tuple(x, y, font_height, line_text)], 每行布局信息.
        x/y 是相对于单个标签左上角的偏移(dot).
    """
    label_w_mm = calc_label_width(paper)
    label_h_mm = paper["height"]
    label_w = label_w_mm * dots_per_mm
    label_h = label_h_mm * dots_per_mm

    # 80%可用区域
    usable_w = label_w * 0.8
    usable_h = label_h * 0.8

    # YAML中的size字段作为字号上限(dot)
    max_font = font_cfg.get("size", 60)
    MIN_FONT = 8

    font_height = None
    final_lines = None

    # 依次尝试1~3行, 优先保持max_font不缩小
    for num_lines in range(1, 4):
        # 该行数下的高度上限: 可用高度/行数 * 0.9 (留行间距)
        h_cap = int(usable_h / num_lines * 0.9)
        fh = min(max_font, h_cap)

        if fh < MIN_FONT:
            continue

        # 拆分文字
        lines = [text] if num_lines == 1 else _split_text(text, num_lines)

        # 检查所有行在fh字号下是否都不超宽
        all_fit = True
        for line in lines:
            wf = _calc_width_factor(line)
            if fh * wf > usable_w:
                all_fit = False
                break

        if all_fit:
            font_height = fh
            final_lines = lines
            break
    else:
        # 3行仍溢出, 在3行基础上缩小字号直到适配
        final_lines = _split_text(text, 3)
        h_cap = int(usable_h / 3 * 0.9)
        font_height = min(max_font, h_cap)
        for line in final_lines:
            wf = _calc_width_factor(line)
            if wf > 0:
                font_height = min(font_height, int(usable_w / wf))
        if font_height < MIN_FONT:
            font_height = MIN_FONT

    # 计算每行居中坐标
    total_text_h = font_height * len(final_lines) + max(0, len(final_lines) - 1) * int(font_height * 0.15)
    y_start = int((label_h - total_text_h) / 2)
    if y_start < 0:
        y_start = 0

    result = []
    for i, line in enumerate(final_lines):
        wf = _calc_width_factor(line)
        text_w = font_height * wf
        x = int((label_w - text_w) / 2)
        if x < 0:
            x = 0
        y = y_start + int(i * font_height * 1.15)
        result.append((x, y, font_height, line))

    logger.debug("自动布局: 单标签%.1fx%.1fmm, %d行, 字号%d (上限%d)",
                 label_w_mm, label_h_mm, len(final_lines), font_height, max_font)
    return result


def _calc_width_factor(text):
    """
    功能:
        计算文字的宽度系数. CJK字符宽度约等于字高, ASCII约为字高的0.55倍.
    参数:
        text: str, 文字内容.
    返回:
        float, 宽度系数 (乘以字号即为文字像素宽度).
    """
    cjk_count = 0
    ascii_count = 0
    for ch in text:
        if ord(ch) > 0x7F:
            cjk_count += 1
        else:
            ascii_count += 1
    factor = cjk_count * 1.0 + ascii_count * 0.55
    return factor if factor > 0 else 1


def _split_text(text, max_lines):
    """
    功能:
        将文字按自然断点拆分为指定行数, 尽量在空格/括号/逗号处断开.
    参数:
        text: str, 原始文字.
        max_lines: int, 目标行数.
    返回:
        list[str], 拆分后的文字行列表.
    """
    if max_lines <= 1 or len(text) <= 1:
        return [text]

    # 按宽度系数找分割点, 使每段宽度尽量均匀
    total_wf = _calc_width_factor(text)
    target_wf = total_wf / max_lines

    lines = []
    remaining = text
    for line_idx in range(max_lines - 1):
        best_pos = -1
        accum_wf = 0
        # 遍历字符累加宽度, 在超过目标宽度附近找最佳断点
        for i, ch in enumerate(remaining):
            accum_wf += 1.0 if ord(ch) > 0x7F else 0.55
            # 在目标宽度的 70%~130% 范围内寻找自然断点
            if accum_wf >= target_wf * 0.7:
                if ch in " ,，;；()（）/\\-":
                    best_pos = i + 1  # 断点后面
                elif i + 1 < len(remaining) and remaining[i + 1] in " ,，;；()（）/\\-":
                    best_pos = i + 1
            if accum_wf >= target_wf * 1.3:
                break

        # 没找到自然断点, 按宽度均分强制切
        if best_pos <= 0:
            accum_wf = 0
            for i, ch in enumerate(remaining):
                accum_wf += 1.0 if ord(ch) > 0x7F else 0.55
                if accum_wf >= target_wf:
                    best_pos = i + 1
                    break
            if best_pos <= 0:
                best_pos = len(remaining) // 2

        lines.append(remaining[:best_pos].strip())
        remaining = remaining[best_pos:].strip()

    if remaining:
        lines.append(remaining)

    return [l for l in lines if l]


def init_printer(lib, config):
    """
    功能:
        打开打印机端口并发送纸张初始化命令(SIZE/GAP/DIRECTION).
        自动根据列数和边距计算单个标签尺寸.

    参数:
        lib: ctypes.WinDLL, TSC库实例
        config: dict, 配置字典
    """
    port = config["printer"]["port"]
    paper = config["paper"]
    unit = paper["unit"]

    # 连接打印机并检查返回值
    ret = lib.openportW(port)
    if ret == 0:
        available = list_windows_printers()
        hint = ""
        if available:
            hint = f" 系统中可用的打印机: {', '.join(available)}"
        raise RuntimeError(
            f"无法连接打印机, 端口/名称: '{port}'.{hint}"
            " 请检查配置中的打印机名称是否与系统中的名称一致"
        )
    logger.debug("已连接打印机, 端口: %s", port)

    # SIZE使用纸张总宽度, 打印机传感器自动识别多列布局
    size_cmd = f"SIZE {paper['width']} {unit}, {paper['height']} {unit}"
    lib.sendcommandW(size_cmd)
    logger.debug("纸张尺寸: %s", size_cmd)

    # 设置上下间隙
    gap_cmd = f"GAP {paper['gap']} {unit}, {paper['gap_offset']} {unit}"
    lib.sendcommandW(gap_cmd)

    # 设置打印方向
    lib.sendcommandW(f"DIRECTION {paper['direction']}")


def print_text(lib, config, texts):
    """
    功能:
        将文字列表发送到打印机并打印一张标签(支持多列, 每列不同内容).
        自动根据纸张尺寸和文字长度计算每列的字号及居中位置.

    参数:
        lib: ctypes.WinDLL, TSC库实例
        config: dict, 配置字典
        texts: list[str], 每列要打印的文字内容, 长度应等于列数
    """
    font = config["font"]
    paper = config["paper"]
    columns = paper.get("columns", 1)
    margin = paper.get("margin", 0)
    column_gap = paper.get("column_gap", 0)

    # 从配置计算DPI转换系数
    dots_per_mm = calc_dots_per_mm(config)

    # 计算单个标签宽度(mm)
    label_width_mm = calc_label_width(paper)

    # 读取位置偏移量 (用于物理打印机对齐微调, 单位mm, 转换为dot)
    pos_cfg = config.get("position", {})
    offset_x = int(float(pos_cfg.get("x", 0)) * dots_per_mm)
    offset_y = int(float(pos_cfg.get("y", 0)) * dots_per_mm)

    # 清除打印缓冲区
    lib.sendcommandW("CLS")

    font_name = font.get("name", "Arial").encode("ascii")

    # 为每一列绘制对应文字
    for col in range(columns):
        text = texts[col] if col < len(texts) else ""
        if text == "":
            continue

        # 每列独立计算字号和居中坐标, 支持自动换行
        layout_lines = calc_auto_layout(paper, font, text, dots_per_mm)

        # 列起始x偏移(mm) = 左边距 + 列序号 * (标签宽度 + 列间距)
        col_origin_mm = margin + col * (label_width_mm + column_gap)
        col_origin_dot = int(col_origin_mm * dots_per_mm)

        for center_x, center_y, font_height, line_text in layout_lines:
            # 绝对坐标 = 列起始偏移 + 标签内居中偏移 + 配置微调偏移
            abs_x = col_origin_dot + center_x + offset_x
            abs_y = center_y + offset_y

            # UTF-16LE编码后追加终止符, 用create_string_buffer避免c_char_p截断\x00
            raw = line_text.encode("utf-16-le") + b"\x00\x00"
            content_buf = ctypes.create_string_buffer(raw)

            lib.windowsfontUnicode(
                abs_x, abs_y, font_height,
                int(font.get("rotation", 0)),
                int(font.get("bold", 0)),
                int(font.get("underline", 0)),
                font_name,
                ctypes.cast(content_buf, ctypes.c_void_p),
            )
        logger.debug("列%d: 文字'%s' (%d行), 列起始%.1fmm(%ddot)",
                     col + 1, text, len(layout_lines), col_origin_mm, col_origin_dot)

    # 所有列绘制完毕后统一打印
    lib.printlabelW("1", "1")
    logger.debug("已发送打印(%d列): %s", columns, " | ".join(texts))


def check_printer_ready(lib, config):
    """
    功能:
        预检打印机连接. 启动时短暂打开并关闭一次打印端口, 用于确认配置可用,
        同时避免长时间占用同一个打印会话导致作业延迟提交.

    参数:
        lib: ctypes.WinDLL, TSC库实例.
        config: dict, 配置字典.
    """
    session_open = False
    try:
        init_printer(lib, config)
        session_open = True
    finally:
        if session_open:
            close_printer(lib)


def execute_print_job(lib, config, texts):
    """
    功能:
        执行一次完整打印作业. 每次打印都重新打开和关闭端口, 确保Windows打印队列
        在单次作业结束后立即提交, 不会等到脚本退出时才真正出纸.

    参数:
        lib: ctypes.WinDLL, TSC库实例.
        config: dict, 配置字典.
        texts: list[str], 每列要打印的文字内容.
    """
    session_open = False
    try:
        init_printer(lib, config)
        session_open = True
        print_text(lib, config, texts)
    finally:
        if session_open:
            close_printer(lib)


def close_printer(lib):
    """
    功能:
        关闭打印机端口连接.

    参数:
        lib: ctypes.WinDLL, TSC库实例
    """
    lib.closeport()
    logger.debug("打印机连接已关闭")


def main():
    """
    功能:
        交互式打印主循环.
        加载配置和DLL, 连接打印机, 循环等待用户输入文字并打印.
    """
    logger.info("=== TSC交互式打印脚本启动 ===")

    # 加载配置和DLL
    config = load_config(CONFIG_PATH)
    lib = load_dll(DLL_PATH)

    # 启动时预检一次, 但不长期占用端口.
    try:
        check_printer_ready(lib, config)
    except Exception as e:
        logger.error("打印机初始化失败: %s", e)
        sys.exit(1)

    columns = config["paper"].get("columns", 1)

    logger.info("打印机就绪, 等待输入...")
    print("-" * 40)
    if columns > 1:
        print(f"当前配置: {columns}列标签, 每次需依次输入{columns}列内容")
    print("输入要打印的文字后按回车即可打印")
    print("输入 quit 退出程序")
    print("-" * 40)

    try:
        while True:
            texts = []
            quit_flag = False

            # 收集每列的输入内容
            for col in range(columns):
                try:
                    if columns > 1:
                        prompt = f"\n请输入第{col + 1}列内容: "
                    else:
                        prompt = "\n请输入打印内容: "
                    text = input(prompt).strip()
                except EOFError:
                    quit_flag = True
                    break

                if text.lower() == "quit":
                    quit_flag = True
                    break

                texts.append(text)

            if quit_flag:
                break

            # 检查是否所有列都为空
            if all(t == "" for t in texts):
                print("输入为空, 请重新输入")
                continue

            try:
                execute_print_job(lib, config, texts)
                print(f"已打印: {' | '.join(texts)}")
            except Exception as e:
                logger.error("打印失败: %s", e)
                print(f"打印出错: {e}, 请检查打印机连接")

    except KeyboardInterrupt:
        print("\n检测到中断信号")

    finally:
        logger.info("=== 脚本已退出 ===")


if __name__ == "__main__":
    main()
