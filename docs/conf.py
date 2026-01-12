# Configuration file for the Sphinx documentation builder.
# Sphinx 文档生成器的配置文件
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys

# 将项目的根目录添加到 sys.path 中，以便 Sphinx 能够找到 unilabos 包
sys.path.insert(0, os.path.abspath(".."))

project = "Uni-Lab"
copyright = "2025, Uni-Lab Community, DP Technology & Peking University"
author = "Uni-Lab Community, DP Technology & Peking University"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",  # 如果您使用 Google 或 NumPy 风格的 docstrings
    "sphinx_rtd_theme",
    "sphinxcontrib.mermaid",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".txt": "markdown",
    ".md": "markdown",
}

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "dollarmath",
    "html_image",
    "replacements",
    "smartquotes",
    "substitution",
]

myst_fence_as_directive = ["mermaid"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

language = "zh"

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

# 设置 HTML 主题为 sphinx-book-theme
html_theme = "sphinx_rtd_theme"

# sphinx-book-theme 主题选项
html_theme_options = {
    "repository_url": "https://github.com/deepmodeling/Uni-Lab-OS",
    "use_repository_button": True,
    "use_issues_button": True,
    "use_edit_page_button": True,
    "use_download_button": True,
    "path_to_docs": "docs",
    "show_navbar_depth": 2,
    "show_toc_level": 2,
    "home_page_in_toc": True,
    "logo_only": False,
}

# 设置 HTML 文档的静态文件路径
html_static_path = ["_static"]  # 如果有自定义 CSS，可以放在 _static 目录中

section_titles = {
    "Simple": "## 简单单变量动作函数",
    "Organic": """## 常量有机化学操作

Uni-Lab 常量有机化学指令集多数来自 [XDL](https://croningroup.gitlab.io/chemputer/xdl/standard/full_steps_specification.html#)，包含有机合成实验中常见的操作，如加热、搅拌、冷却等。
""",
    "Bio": """## 移液工作站及相关生物自动化设备操作

Uni-Lab 生物操作指令集多数来自 [PyLabRobot](https://docs.pylabrobot.org/user_guide/index.html)，包含生物实验中常见的操作，如移液、混匀、离心等。
""",
    "MobileRobot": "## 多工作站及小车运行、物料转移",
    "Robot": """## 机械臂、夹爪等机器人设备

Uni-Lab 机械臂、机器人、夹爪和导航指令集沿用 ROS2 的 `control_msgs` 和 `nav2_msgs`：
""",
}

import os
from pathlib import Path


def get_conda_share_dir(package_name=None):
    """获取 Conda 环境的 share 目录路径

    :param package_name: 可选参数，指定具体包的 share 子目录
    :return: Path 对象或 None
    """
    # 获取当前 Conda 环境根目录
    conda_prefix = os.getenv("CONDA_PREFIX")
    if not conda_prefix:
        raise EnvironmentError("未检测到激活的 Conda 环境")

    # 构建基础 share 目录路径
    share_dir = Path(conda_prefix) / "share"

    # 如果指定了包名，追加包子目录
    if package_name:
        share_dir = share_dir / package_name

    # 验证路径是否存在
    if not share_dir.exists():
        print(f"警告: 路径 {share_dir} 不存在")
        return None

    return share_dir


def generate_action_includes(app):
    src_dir = Path(app.srcdir)
    print(f"Generating action includes for {src_dir}")
    action_dir = src_dir.parent / "unilabos_msgs" / "action"  # 修改为你的实际路径
    output_file = src_dir / "developer_guide" / "action_includes.md"

    # 确保输出目录存在
    output_file.parent.mkdir(exist_ok=True)

    # 初始化各部分内容
    sections = {}

    # 仅处理本地消息文件
    if action_dir.exists():
        for action_file in sorted(action_dir.glob("*.action")):
            # 获取相对路径
            rel_path = f"../../unilabos_msgs/action/{action_file.name}"
            # 读取首行注释
            try:
                with open(action_file, "r", encoding="utf-8") as af:
                    first_line = af.readline().strip()
                    # 提取注释内容（去除#和空格）
                    section = first_line.lstrip("#").strip()

                    text = f"""
### `{action_file.stem}`

```{{literalinclude}} {rel_path}
:language: yaml
```

----
"""

                    if sections.get(section) is None:
                        sections[section] = text
                    else:
                        sections[section] += text
            except Exception as e:
                print(f"处理文件 {action_file} 时出错: {e}")
    else:
        print(f"警告: 动作消息目录 {action_dir} 不存在")

    ros_action_dirs = []
    control_msgs_dir = get_conda_share_dir("control_msgs")
    nav2_msgs_dir = get_conda_share_dir("nav2_msgs")

    if control_msgs_dir is not None:
        ros_action_dirs.append(control_msgs_dir / "action")
    if nav2_msgs_dir is not None:
        ros_action_dirs.append(nav2_msgs_dir / "action")

    for action_dir in ros_action_dirs:
        for action_file in sorted(action_dir.glob("*.action")):
            # 获取相对路径
            rel_path = f"{action_file.absolute()}"
            # 读取首行注释
            with open(action_file, "r", encoding="utf-8") as af:
                # 提取注释内容（去除#和空格）
                section = "Robot"

                text = f"""### `{action_file.stem}`

```yaml
{open(rel_path, 'r').read()}
```

----
"""
                if sections.get(section) is None:
                    sections[section] = text
                else:
                    sections[section] += text

    # 写入内容到输出文件
    with open(output_file, "w", encoding="utf-8") as f:
        # 按 Section 生成总文档
        for section, title in section_titles.items():
            content = sections.get(section, "")
            if content:  # 只有有内容时才写入标题和内容
                f.write(f"{title}\n\n")
                f.write(content)


def setup(app):
    app.connect("builder-inited", generate_action_includes)
    app.add_js_file("https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js")
    app.add_js_file(None, body="mermaid.initialize({startOnLoad:true});")
