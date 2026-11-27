"""将纯数据桌面声明转换为主线工作区启动参数，不创建设备或进程。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .manifest import load_descriptor, package_file, read_manifest, render
from .options import validate_configuration, validate_options


class DesktopPackage:
    """固定设备包路径、描述和主线可支持的启动配置。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.manifest = read_manifest(self.workspace)
        self.spec, _ = load_descriptor(Path(self.manifest['file']))
        validate_configuration(self.spec.get('configuration', []))

    def describe(self) -> dict[str, Any]:
        return {'schemaVersion': 1, 'id': self.manifest['package_id'],
                'name': self.manifest['display_name'],
                'configuration': self.spec.get('configuration', [])}

    def parameters(self, options: dict[str, Any]) -> dict[str, object]:
        values = validate_options(self.describe(), options)
        startup = render(self.spec['startup'], {'options': values, 'package_dir': str(self.workspace)})
        graph_value = Path(startup['graph'])
        if graph_value.is_absolute():
            try:
                graph_value = graph_value.resolve(strict=True).relative_to(self.workspace)
            except ValueError as error:
                raise ValueError('设备图必须位于设备包内') from error
        graph = package_file(self.workspace, graph_value.as_posix(), '.json')
        # 主线工作区入口固定 ROS、配置及桥接器，不另建 CLI 命令生成器。
        if startup.get('backend', 'ros') != 'ros' or startup.get('registryPath'):
            raise ValueError('桌面工作区使用主线 ROS 后端和工作区注册表')
        config = startup.get('config', 'deployment/local_config.py')
        if package_file(self.workspace, config, '.py') != self.workspace / 'deployment/local_config.py':
            raise ValueError('桌面工作区使用 deployment/local_config.py')
        extras = startup.get('extraArgs', ['--external_devices_only', '--ros_discovery_server', 'off'])
        if extras != ['--external_devices_only', '--ros_discovery_server', 'off']:
            raise ValueError('桌面工作区启动参数须与主线默认参数一致')
        return {'graphPath': str(graph),
                'runtimeMode': 'normal' if startup.get('actionMode', 'simulate') == 'real' else 'dry-run'}
