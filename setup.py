import shutil
from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py

package_name = 'unilabos'
# 设备驱动由独立设备包安装，不进开源包；只保留工站基类旧导入路径的兼容转发。
PUBLIC_DEVICE_PACKAGES = ["unilabos.devices", "unilabos.devices.workstation"]
PUBLIC_DEVICE_FILES = (
    Path("__init__.py"),
    Path("workstation", "__init__.py"),
    Path("workstation", "workstation_base.py"),
)


def keep_public_device_files(devices_dir: Path) -> None:
    """构建目录只保留兼容转发文件，其余设备驱动和旧构建残留一并删除。"""

    kept = {
        relative: (devices_dir / relative).read_bytes()
        for relative in PUBLIC_DEVICE_FILES
        if (devices_dir / relative).is_file()
    }
    shutil.rmtree(devices_dir, ignore_errors=True)
    for relative, content in kept.items():
        target = devices_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class BuildPyWithSkills(build_py):
    def run(self):
        super().run()
        # 复用旧构建目录时，也要清除曾复制的私有实现。
        built_package = Path(self.build_lib) / package_name
        keep_public_device_files(built_package / "devices")
        shutil.rmtree(built_package / "scheduling", ignore_errors=True)
        for module in ("time_planning.py", "time_scheduling.py"):
            (built_package / "app" / "scheduler" / module).unlink(missing_ok=True)
        source_dir = Path(__file__).resolve().parent / "skills"
        resource_dir = Path(self.build_lib) / package_name / "_bundled_skills"
        for skill_file in sorted(source_dir.glob("*/SKILL.md")):
            target = resource_dir / skill_file.relative_to(source_dir)
            self.mkpath(str(target.parent))
            self.copy_file(str(skill_file), str(target))


setup(
    name=package_name,
    version='0.11.4',
    packages=find_packages(exclude=[
        "unilabos.devices", "unilabos.devices.*", "unilabos.scheduling", "unilabos.scheduling.*",
    ]) + PUBLIC_DEVICE_PACKAGES,
    include_package_data=True,
    cmdclass={"build_py": BuildPyWithSkills},
    install_requires=[
        'setuptools',
        'requests>=2.32,<3',
        'cryptography>=43',
        'jsonschema>=4.18',
        'watchfiles>=1.1,<2',
        'rfc8785>=0.1.4,<0.2',
        'msgcenterpy>=0.1.8,<0.2',
        'pylabrobot==0.2.1',
        'python-multipart>=0.0.9,<1',
        'openpyxl>=3.1,<4',
        'sqlglot==27.14.0',
        'usd-core>=26.11',
    ],
    extras_require={
        'center': ['requests>=2.32,<3', 'cryptography>=43'],
        'mcp': ['mcp>=1.10,<2'],
        'assets': ['oss2>=2.19,<3'],
        'recording': ['mcap>=1.1,<2'],
        'postgresql': ['psycopg[binary]==3.2.10'],
        'twin-simulation': ['unilab-plc-sim==0.2.11', 'numpy>=1.24', 'scipy>=1.10'],
        'twin-kinematics': ['numpy>=1.24'],
    },
    zip_safe=False,
    author="The unilabos developers",
    maintainer='Junhan Chang, Xuwznln',
    maintainer_email='Junhan Chang <changjh@pku.edu.cn>, Xuwznln <18435084+Xuwznln@users.noreply.github.com>',
    description='',
    license='GPL v3',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            "unilab = unilabos.app.main:main",
            "unilab-launcher = unilabos.workspace_host.launcher.cli:main",
            "unilab-supervisor = unilabos.managed_runtime.supervisor:main",
            "unilab-mcp = unilabos.agent_tools.workflow:main",
        ],
    },
)
