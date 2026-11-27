"""App 与 CLI 共用的设备包配置声明和取值校验。"""
from __future__ import annotations

import json
import re
from typing import Any


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} 必须是非空字符串')
    return value


def identifier(value: Any, label: str) -> str:
    value = text(value, label)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', value):
        raise ValueError(f'{label} 格式无效')
    return value


def validate_configuration(fields: Any) -> None:
    """校验声明字段及其默认值，不重复验证框架自己生成的 IPC 返回结构。"""
    if not isinstance(fields, list):
        raise ValueError('configuration 必须是配置字段数组')
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError('配置字段必须是对象')
        key = identifier(field.get('key'), '配置字段 key')
        if key in seen:
            raise ValueError(f'配置字段重复：{key}')
        seen.add(key)
        text(field.get('label'), '配置字段 label')
        if field.get('type') not in {'string', 'integer', 'boolean', 'select', 'file'}:
            raise ValueError(f'不支持的配置字段类型：{key}')
        if field.get('type') == 'file':
            extensions = field.get('extensions', [])
            if (not isinstance(extensions, list) or not extensions
                    or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9]+', item)
                           for item in extensions)):
                raise ValueError(f'文件字段 extensions 必须是非空扩展名数组：{key}')
        if 'required' in field and type(field['required']) is not bool:
            raise ValueError(f'配置字段 required 必须是布尔值：{key}')
        if field.get('type') == 'select':
            choices = field.get('choices')
            if not isinstance(choices, list) or not choices:
                raise ValueError(f'配置字段缺少 choices：{key}')
            if any(not isinstance(choice, dict) for choice in choices):
                raise ValueError(f'配置选项必须是对象：{key}')
            for choice in choices:
                text(choice.get('value'), '选项 value')
                text(choice.get('label'), '选项 label')
            if len({choice['value'] for choice in choices}) != len(choices):
                raise ValueError(f'配置选项重复：{key}')
        for bound in ('min', 'max'):
            if bound in field and type(field[bound]) is not int:
                raise ValueError(f'配置字段 {bound} 必须是整数：{key}')
        if 'min' in field and 'max' in field and field['min'] > field['max']:
            raise ValueError(f'配置字段范围无效：{key}')
    json.dumps(fields, allow_nan=False)
    # 配置默认值也必须符合其自身声明。
    validate_options({'configuration': fields}, {}, require_all=False)


def validate_options(spec: dict, values: Any, *, require_all: bool = True) -> dict:
    if not isinstance(values, dict):
        raise ValueError('设备包配置必须是对象')
    fields = spec.get('configuration', [])
    known = {field['key'] for field in fields}
    if set(values) - known:
        raise ValueError('包含未声明的设备包配置字段')
    result = {}
    for field in fields:
        key = field['key']
        if key not in values and 'default' not in field:
            if require_all and field.get('required'):
                raise ValueError(f'请填写 {field["label"]}')
            continue
        value = values.get(key, field.get('default'))
        kind = field['type']
        valid = ((kind in {'string', 'select', 'file'} and isinstance(value, str))
                 or (kind == 'integer' and type(value) is int)
                 or (kind == 'boolean' and type(value) is bool))
        if not valid:
            raise ValueError(f'{field["label"]} 类型无效')
        if field.get('required') and isinstance(value, str) and not value.strip():
            raise ValueError(f'请填写 {field["label"]}')
        if kind == 'select' and value not in [choice['value'] for choice in field['choices']]:
            raise ValueError(f'{field["label"]} 不在允许的选项中')
        if kind == 'file' and value and ('.' not in value or value.rsplit('.', 1)[-1].lower() not in [
            extension.lower() for extension in field['extensions']
        ]):
            raise ValueError(f'{field["label"]} 文件扩展名无效')
        if kind == 'integer' and (value < field.get('min', value) or value > field.get('max', value)):
            raise ValueError(f'{field["label"]} 超出允许范围')
        result[key] = value
    return result
