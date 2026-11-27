"""Windows 交付按传统路径上限校验，不依赖宿主机的长路径开关。"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import zipfile

MAX_PATH_CHARS = 259
MAX_DIRECTORY_CHARS = 247
MAX_APP_DIRECTORY_CHARS = 26
RUNTIME_RELATIVE = 'os/runtime/'


def path_chars(value: str | Path) -> int:
    return len(str(value).encode('utf-16-le')) // 2


def check_relative(relative: str, origin: str, *, is_directory: bool = False) -> int:
    path = PurePosixPath(relative.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
        raise ValueError(f'交付路径无效：{origin} -> {relative}')
    size = path_chars(path.as_posix())
    maximum = MAX_DIRECTORY_CHARS if is_directory else MAX_PATH_CHARS
    parent_size = 0 if path.parent == PurePosixPath('.') else path_chars(path.parent.as_posix())
    if (MAX_APP_DIRECTORY_CHARS + 1 + size > maximum
            or MAX_APP_DIRECTORY_CHARS + 1 + parent_size > MAX_DIRECTORY_CHARS):
        raise ValueError(f'Windows 交付路径过长：{origin} -> {relative}；'
                         f'文件/目录预算 {MAX_PATH_CHARS}/{MAX_DIRECTORY_CHARS}，请缩短包内目录或文件名')
    return size


def sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def constructor_policy(
    info_file: Path, installer: Path, lock: Path, *, installer_digest: str | None = None,
) -> dict:
    data = json.loads(info_file.read_text(encoding='utf-8-sig'))
    if installer_digest is None:
        installer_digest = sha256(installer)
    if (not isinstance(data, dict) or data.get('_platform') != 'win-64'
            or (data.get('_installer_hashes') or {}).get('sha256') != installer_digest):
        raise ValueError('Constructor info.json 与所选 Windows 安装器摘要不匹配，请重新生成安装器及元数据')
    expected = {line.strip() for line in lock.read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.strip().startswith(('#', '@'))}
    actual = {url + '#' + md5 for url, md5 in data.get('_urls', [])}
    if not expected or expected != actual:
        raise ValueError('Constructor info.json 与 Conda 精确锁不匹配')
    maximum = data.get('_max_relative_path_length')
    if type(maximum) is not int or maximum <= 0:
        raise ValueError('Constructor info.json 缺少有效的最长相对路径统计，请重新构建 Constructor')
    allowed = MAX_DIRECTORY_CHARS - MAX_APP_DIRECTORY_CHARS - 1 - path_chars(RUNTIME_RELATIVE)
    if maximum > allowed:
        raise ValueError(f'Constructor 包路径过长（{maximum} > {allowed}），请缩短包内路径或精简依赖后重新生成')
    return {'schemaVersion': 1, 'maxPathChars': MAX_PATH_CHARS,
            'maxDirectoryChars': MAX_DIRECTORY_CHARS,
            'maxAppDirectoryChars': MAX_APP_DIRECTORY_CHARS, 'constructorRelativeMax': maximum}


def audit_tree(root: Path, relative_prefix: str = '') -> dict:
    """按最终安装目录预算校验；开发机绝对路径可使用长路径。"""
    if not root.is_dir():
        raise ValueError(f'待校验目录不存在：{root}')
    longest = (0, '')
    count = 0
    for folder, directories, files in os.walk(root):
        for name in directories + files:
            file = Path(folder) / name
            relative = relative_prefix + file.relative_to(root).as_posix()
            size = check_relative(relative, str(file), is_directory=name in directories)
            count += 1
            if size > longest[0]:
                longest = (size, relative)
    return {'entries': count, 'longestRelativeChars': longest[0], 'longestRelativePath': longest[1]}


def audit_wheels(wheels: list[Path]) -> dict:
    longest = (0, '')
    count = 0
    for wheel in wheels:
        try:
            with zipfile.ZipFile(wheel) as archive:
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    check_relative(item.filename, str(wheel))
                    parts = PurePosixPath(item.filename).parts
                    target = RUNTIME_RELATIVE + 'Lib/site-packages/' + item.filename
                    if len(parts) >= 3 and parts[0].endswith('.data'):
                        locations = {'purelib': 'Lib/site-packages/', 'platlib': 'Lib/site-packages/',
                                     'scripts': 'Scripts/', 'data': '',
                                     'headers': 'Include/site/python3.11/' + wheel.name.split('-')[0] + '/'}
                        if parts[1] not in locations:
                            raise ValueError(f'不支持的 wheel 安装布局：{wheel.name} -> {item.filename}')
                        target = RUNTIME_RELATIVE + locations[parts[1]] + '/'.join(parts[2:])
                    targets = [target]
                    if target.endswith('.py'):
                        file = PurePosixPath(target)
                        targets.append(str(file.parent / '__pycache__' / (file.stem + '.cpython-311.opt-2.pyc')))
                    if item.filename.endswith('.dist-info/entry_points.txt'):
                        entrypoints = configparser.ConfigParser(interpolation=None)
                        entrypoints.optionxform = str
                        entrypoints.read_string(archive.read(item).decode('utf-8'))
                        for group in ('console_scripts', 'gui_scripts'):
                            if entrypoints.has_section(group):
                                for name in entrypoints[group]:
                                    targets.append(RUNTIME_RELATIVE + 'Scripts/' + name + '.exe')
                    for target in targets:
                        size = check_relative(target, wheel.name)
                        count += 1
                        if size > longest[0]:
                            longest = (size, target)
        except zipfile.BadZipFile as error:
            raise ValueError(f'无法校验 wheel 路径，文件无效：{wheel}') from error
    return {'entries': count, 'longestRelativeChars': longest[0], 'longestRelativePath': longest[1]}
