# Configuration file for the Sphinx documentation builder.
# Sphinx 文档生成器的配置文件
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys
from pathlib import Path

# 将项目的根目录添加到 sys.path 中，以便 Sphinx 能够找到 unilabos 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

project = "Uni-Lab-OS"
copyright = "2026, Uni-Lab-OS Community"
author = "Uni-Lab-OS Community"

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

# Read the Docs 主题；不要混用其他主题的配置键。
html_theme = "sphinx_rtd_theme"

html_theme_options = {
    "navigation_depth": 3,
    "logo_only": False,
}
html_context = {
    "display_github": True,
    "github_user": "deepmodeling",
    "github_repo": "Uni-Lab-OS",
    "github_version": "dev",
    "conf_py_path": "/docs/",
}

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

def get_conda_share_dir(package_name=None):
    """获取 Conda 环境的 share 目录路径

    :param package_name: 可选参数，指定具体包的 share 子目录
    :return: Path 对象或 None
    """
    # 可显式复用 ROS 的消息定义目录；读取文档数据，不把另一环境加入 Python 导入路径。
    conda_prefix = os.getenv("UNILABOS_DOCS_ROS_PREFIX") or os.getenv("CONDA_PREFIX")
    if not conda_prefix:
        conda_prefix = sys.prefix

    # Windows Conda 把 ROS 数据放在 Library/share；Unix 位于 share。
    candidates = [Path(conda_prefix) / "share", Path(conda_prefix) / "Library/share"]
    if package_name:
        candidates = [path / package_name for path in candidates]
    for share_dir in candidates:
        if share_dir.is_dir():
            return share_dir
    if os.getenv("UNILABOS_DOCS_REQUIRE_ROS") == "1":
        raise EnvironmentError(f"完整文档缺少 ROS 消息定义目录：{candidates}")
    print(f"警告: 未找到 ROS 消息定义目录：{candidates}")
    return None


def generate_action_includes(app):
    src_dir = Path(app.srcdir)
    print(f"Generating action includes for {src_dir}")
    action_dir = src_dir.parent / "unilabos_msgs" / "action"  # 修改为你的实际路径
    output_file = Path(app.doctreedir) / "generated" / "action_includes.md"

    # 确保输出目录存在
    output_file.parent.mkdir(parents=True, exist_ok=True)

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
    control_msgs_dir = get_conda_share_dir("control_msgs/action")
    nav2_msgs_dir = get_conda_share_dir("nav2_msgs/action")

    if control_msgs_dir is not None:
        ros_action_dirs.append(control_msgs_dir)
    if nav2_msgs_dir is not None:
        ros_action_dirs.append(nav2_msgs_dir)

    for action_dir in ros_action_dirs:
        action_files = sorted(action_dir.glob("*.action"))
        if not action_files and os.getenv("UNILABOS_DOCS_REQUIRE_ROS") == "1":
            raise EnvironmentError(f"完整文档缺少 ROS 动作定义：{action_dir}")
        for action_file in action_files:
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

    # 生成物留在构建目录；不改写仓库里已跟踪的 Markdown。
    rendered = "# 动作接口参考\n\n" + "".join(f"{title}\n\n{sections[section]}" for section, title in section_titles.items()
                       if sections.get(section))
    output_file.write_text(rendered, encoding="utf-8")
    app._unilab_action_reference = rendered


def inject_action_includes(app, docname, source):
    if docname == "developer_guide/action_includes":
        source[0] = app._unilab_action_reference


def refresh_action_reference(app, env, added, changed, removed):
    # ROS 环境可能与上次构建不同；增量构建也必须更新这一页，不能复用缺项内容。
    return ["developer_guide/action_includes"]


def setup(app):
    app.connect("builder-inited", generate_action_includes)
    app.connect("source-read", inject_action_includes)
    app.connect("env-get-outdated", refresh_action_reference)
    app.add_js_file("https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js")
    app.add_js_file(None, body="mermaid.initialize({startOnLoad:true});")
