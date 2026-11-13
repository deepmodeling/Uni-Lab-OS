from pathlib import Path

from setuptools import setup, find_namespace_packages

package_name = 'unilabos'


def read_requirements(name='requirements.txt'):
    return [line.strip() for line in (Path(__file__).parent / 'unilabos/utils' / name)
            .read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


dependency_groups = {
    # ROS 原生运行时及消息由 Conda/RoboStack 提供；不要用 pip 的 OpenCV 覆盖 cv_bridge 所需 ABI。
    'ros2': ['transforms3d>=0.4'],
    'docs': read_requirements('requirements-docs.txt'),
    # 框架内通信适配器与工具用到的 SDK；厂商驱动的专用 SDK 由各外部驱动包自行声明。
    'drivers': [
        'opcua>=0.98.13', 'pandas>=2.2', 'matplotlib>=3.8',
        # utils/pywinauto_util 的 Windows GUI 自动化（模块级导入 pywinauto 与 psutil）；其他平台没有对应运行入口。
        'pywinauto>=0.6.8; sys_platform == "win32"',
        'psutil>=5.9; sys_platform == "win32"',
    ],
    'test': ['pytest>=8', 'pytest-asyncio>=0.23'],
}
extras = {
    'ros2': dependency_groups['ros2'],
    'full': list(dict.fromkeys([
        *(requirement for group in dependency_groups.values() for requirement in group),
        'build>=1.2', 'ipython>=8', 'jupyterlab>=4', 'ruff>=0.9',
    ])),
}

setup(
    name=package_name,
    python_requires='>=3.14,<3.15',
    version='0.13.0rc1',
    packages=[
        name for name in find_namespace_packages(
            include=['unilabos', 'unilabos.*'],
            exclude=['unilabos.test', 'unilabos.test.*'],
        ) if all(part.isidentifier() for part in name.split('.'))
    ],
    include_package_data=True,
    install_requires=read_requirements(),
    extras_require=extras,
    zip_safe=False,
    author="The unilabos developers",
    maintainer='Junhan Chang, Xuwznln',
    maintainer_email='Junhan Chang <changjh@pku.edu.cn>, Xuwznln <18435084+Xuwznln@users.noreply.github.com>',
    description='',
    license='GPL v3',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            "unilab = unilabos.app.main:main"
        ],
    },
)
