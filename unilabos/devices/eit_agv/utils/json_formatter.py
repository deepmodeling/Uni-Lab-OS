"""
JSON格式化工具模块

功能:
    将压缩的JSON文件(如.jspf格式)转换为易读的格式化JSON
"""

import json
import logging
from pathlib import Path
from typing import Union
import re


logger = logging.getLogger(__name__)

def _inline_val_lists(json_text: str) -> str:
    """
    功能:
        将格式化结果中的val列表压缩为单行, 不影响其他字段
    参数:
        json_text: str, 已格式化的JSON文本
    返回:
        str, 已调整val列表展示方式的JSON文本
    """
    pattern = re.compile(r'(^\s*"val"\s*:\s*)\[\s*\n([\s\S]*?)\n\s*\](,?)', re.MULTILINE)

    def _repl(match: re.Match) -> str:
        prefix = match.group(1)
        body = match.group(2)
        suffix = match.group(3)
        items = []
        for line in body.splitlines():
            value = line.strip().rstrip(',')
            if value:
                items.append(value)
        inline = ', '.join(items)
        return f"{prefix}[{inline}]{suffix}"

    return pattern.sub(_repl, json_text)

def format_json_file(
    input_path: Union[str, Path],
    output_path: Union[str, Path] = None,
    indent: int = 2,
    ensure_ascii: bool = False
) -> str:
    """
    功能:
        读取原始JSON或JSPF文件并格式化, 其中val字段的坐标列表保持单行
    参数:
        input_path: str | Path, 输入文件路径
        output_path: str | Path | None, 输出文件路径, None时自动生成 *_formatted.json
        indent: int, 缩进空格数
        ensure_ascii: bool, 是否转义非ASCII字符
    返回:
        str, 格式化后的JSON字符串
    """
    input_path = Path(input_path)

    if not input_path.exists():
        error_msg = f"输入文件不存在: {input_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    try:
        # 读取原始文件内容
        with open(input_path, 'r', encoding='utf-8') as file_obj:
            data = json.load(file_obj)

        logger.info(f"成功读取文件: {input_path}")

        # 按缩进格式化JSON文本
        formatted_json = json.dumps(
            data,
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=False
        )

        # 保持val列表为单行便于坐标阅读
        formatted_json = _inline_val_lists(formatted_json)

        if output_path is None:
            output_path = input_path.parent / f"{input_path.stem}_formatted.json"
        else:
            output_path = Path(output_path)

        # 写回格式化结果
        with open(output_path, 'w', encoding='utf-8') as file_obj:
            file_obj.write(formatted_json)

        logger.info(f"格式化完成, 已保存: {output_path}")

        return formatted_json

    except json.JSONDecodeError as exc:
        error_msg = f"JSON解析失败: {exc}"
        logger.error(error_msg)
        raise ValueError(error_msg)

    except Exception as exc:
        error_msg = f"处理文件时出现异常: {exc}"
        logger.error(error_msg)
        raise

def format_json_string(
    json_str: str,
    indent: int = 2,
    ensure_ascii: bool = False
) -> str:
    """
    功能:
        将压缩的JSON字符串转换为易读的格式化JSON字符串

    参数:
        json_str: 输入的JSON字符串
        indent: 缩进空格数, 默认2
        ensure_ascii: 是否将非ASCII字符转义, 默认False(保留中文等字符)

    返回:
        str, 格式化后的JSON字符串
    """
    try:
        data = json.loads(json_str)
        formatted_json = json.dumps(
            data,
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=False
        )
        return formatted_json

    except json.JSONDecodeError as e:
        error_msg = f"JSON解析失败: {e}"
        logger.error(error_msg)
        raise ValueError(error_msg)

def format_all_jspf_in_directory(
    directory: Union[str, Path] = None,
    indent: int = 2,
    ensure_ascii: bool = False
) -> dict:
    """
    功能:
        批量格式化指定目录下的所有.jspf文件, 保留原文件并另存为.json
    参数:
        directory: 目标目录路径, 如果为None则使用默认的program_file目录
        indent: 缩进空格数, 默认2
        ensure_ascii: 是否将非ASCII字符转义, 默认False(保留中文等字符)
    返回:
        Dict[str, str], 键为文件名, 值为处理状态("成功"或错误信息)
    """
    # 如果未指定目录, 使用默认的program_file目录
    if directory is None:
        current_file = Path(__file__)
        project_root = current_file.parent.parent
        directory = project_root / "program_file"
    else:
        directory = Path(directory)

    if not directory.exists():
        error_msg = f"目录不存在: {directory}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    if not directory.is_dir():
        error_msg = f"路径不是目录: {directory}"
        logger.error(error_msg)
        raise NotADirectoryError(error_msg)

    # 查找所有.jspf文件
    jspf_files = list(directory.glob("*.jspf"))

    if not jspf_files:
        logger.warning(f"在目录 {directory} 中未找到.jspf文件")
        return {}

    logger.info(f"找到 {len(jspf_files)} 个.jspf文件")

    results = {}

    for jspf_file in jspf_files:
        try:
            # 生成与原文件同名的.json输出, 保留原始.jspf
            output_path = jspf_file.with_suffix(".json")

            format_json_file(
                input_path=jspf_file,
                output_path=output_path,
                indent=indent,
                ensure_ascii=ensure_ascii
            )

            results[jspf_file.name] = "成功"
            logger.info(f"✓ {jspf_file.name} 已格式化并另存为 {output_path.name}")

        except Exception as e:
            error_msg = f"失败: {str(e)}"
            results[jspf_file.name] = error_msg
            logger.error(f"✗ {jspf_file.name}: {error_msg}")

    return results


if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    import sys

    # 默认处理program_file目录下的所有jspf文件
    if len(sys.argv) == 1:
        print("=" * 60)
        print("批量格式化 program_file 目录下的所有 .jspf 文件")
        print("=" * 60)

        try:
            results = format_all_jspf_in_directory()

            # 统计结果
            success_count = sum(1 for status in results.values() if status == "成功")
            fail_count = len(results) - success_count

            print("\n" + "=" * 60)
            print(f"处理完成! 成功: {success_count}, 失败: {fail_count}")
            print("=" * 60)

            if fail_count > 0:
                print("\n失败的文件:")
                for filename, status in results.items():
                    if status != "成功":
                        print(f"  - {filename}: {status}")

        except Exception as e:
            print(f"错误: {e}")

    # 处理单个文件
    elif len(sys.argv) >= 2:
        input_file = sys.argv[1]
        output_file = sys.argv[2] if len(sys.argv) > 2 else None

        try:
            result = format_json_file(input_file, output_file)
            print("格式化成功!")
            print(f"预览前100个字符:\n{result[:100]}...")
        except Exception as e:
            print(f"错误: {e}")

    else:
        print("用法:")
        print("  1. 批量处理program_file目录: python json_formatter.py")
        print("  2. 处理单个文件: python json_formatter.py <输入文件路径> [输出文件路径]")
        print("\n示例:")
        print("  python json_formatter.py")
        print("  python json_formatter.py agv_get_bg4.jspf")
