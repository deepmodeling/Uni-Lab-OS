"""设备包桌面接入声明：只解析数据，不导入设备代码或创建服务。"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r'\$\{([A-Za-z0-9_.-]+)\}')


def package_file(package: Path, relative: str, suffix: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or path.suffix != suffix:
        raise ValueError(f'入口必须是包内相对 {suffix} 文件')
    resolved = (package / path).resolve(strict=True)
    if not resolved.is_relative_to(package.resolve()) or not resolved.is_file():
        raise ValueError('入口文件不得越出设备包')
    return resolved


def read_manifest(package: Path) -> dict:
    package = package.resolve(strict=True)
    entry = package / 'unilab-app.toml'
    if not entry.is_file():
        raise ValueError('所选设备包尚未配置桌面启动入口：根目录缺少 unilab-app.toml，请选择正确目录或按接入指南补齐启动声明')
    value = tomllib.loads(entry.read_text(encoding='utf-8'))
    version = value.get('schema_version')
    if type(version) is not int or version != 2:
        raise ValueError('unilab-app.toml 仅支持 schema_version=2 的设备包声明，旧 Python 适配器已退役')
    if not isinstance(value.get('package_id'), str) or not re.fullmatch(r'[a-z][a-z0-9_.-]*', value['package_id']):
        raise ValueError('package_id 必须是小写英文设备包标识')
    if not isinstance(value.get('display_name'), str) or not value['display_name'].strip():
        raise ValueError('display_name 必须是非空字符串')
    result = {'package_id': value['package_id'], 'display_name': value['display_name']}
    if 'adapter' in value:
        raise ValueError('设备包声明不支持 Python 适配器，请使用 runtime.file')
    entry = value.get('runtime')
    if not isinstance(entry, dict) or not isinstance(entry.get('file'), str):
        raise ValueError('声明式设备包缺少 runtime.file')
    return {**result, 'file': str(package_file(package, entry['file'], '.json'))}


def lookup(scope: dict, path: str) -> Any:
    value: Any = scope
    parts = path.split('.')
    while parts:
        if not isinstance(value, dict):
            raise ValueError(f'声明引用不存在：{path}')
        # 配置 key 可以含点或连字符，优先匹配完整字典键，不把它误作 Python 属性。
        for length in range(len(parts), 0, -1):
            key = '.'.join(parts[:length])
            if key in value:
                value = value[key]
                parts = parts[length:]
                break
        else:
            raise ValueError(f'声明引用不存在：{path}')
    return value


def render(value: Any, scope: dict) -> Any:
    """仅替换启动参数中的变量，不提供数据映射或对象构造语言。"""
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value)
        if match:
            return lookup(scope, match[1])
        def substitute(match):
            item = lookup(scope, match[1])
            if not isinstance(item, (str, int, float, bool)):
                raise ValueError('字符串模板仅允许标量插值')
            return str(item)
        return PLACEHOLDER.sub(substitute, value)
    if isinstance(value, list):
        return [render(item, scope) for item in value]
    if isinstance(value, dict):
        if any(key.startswith('$') for key in value):
            raise ValueError('不支持的声明表达式')
        return {key: render(item, scope) for key, item in value.items()}
    return value


def load_descriptor(path: Path) -> tuple[dict, list[str]]:
    spec = json.loads(path.read_text(encoding='utf-8'))
    allowed = {'schemaVersion', 'configuration', 'startup', 'services'}
    if (not isinstance(spec, dict) or type(spec.get('schemaVersion')) is not int
            or spec['schemaVersion'] != 2 or set(spec) - allowed):
        raise ValueError('运行声明需要 schemaVersion=2，仅支持配置和原生启动')
    if not isinstance(spec.get('configuration', []), list):
        raise ValueError('configuration 必须是数组')
    startup = spec.get('startup')
    if (not isinstance(startup, dict) or set(startup) -
            {'graph', 'config', 'registryPath', 'backend', 'actionMode', 'extraArgs'}
            or not isinstance(startup.get('graph'), str) or not startup['graph']):
        raise ValueError('startup 必须声明原生设备图 graph，不能以空壳后端替代设备加载')
    for key in ('registryPath', 'extraArgs'):
        if not isinstance(startup.get(key, []), list) or any(not isinstance(item, str) for item in startup.get(key, [])):
            raise ValueError(f'startup.{key} 必须是字符串数组')
    if startup.get('backend', 'ros') not in {'ros', 'simple', 'automancer'} or startup.get('actionMode', 'simulate') not in {'real', 'simulate'}:
        raise ValueError('startup 的后端或动作模式无效')
    if spec.get('services') not in (None, []):
        raise ValueError('外部服务需独立启动，请从桌面声明中移除 services')
    spec.pop('services', None)
    json.dumps(spec, allow_nan=False)
    return spec, []
