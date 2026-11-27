"""OS 合同对照：用 Uni-Lab-OS 自己的静态扫描器读原设备类与生成的转发类，逐项比较 OS 登记的内容。

用法：python conformance_os.py --os-src <Uni-Lab-OS 源码根> --package-root <领域包源码根> --package <导入包名> --out <报告.json>
需要能导入 unilabos（含 package_manager.package_distribution.asset_behavior 与 registry.ast_registry_scanner）；
--os-src 指定的 OS 源码根放到 sys.path 最前，环境里有其依赖即可。
静态对照允许实现类已注册（allow_registered_implementation），运行时不能这样用。
每个 @device 类用默认选项生成转发，与原类对照。
"""
import argparse
import ast
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from unilabos.package_manager.package_distribution.asset_behavior import ForwarderOptions, generate_forwarder

COMPARED = ('device_id', 'model', 'available_sites', 'metadata', 'category', 'handles', 'hardware_interface')


def norm_action(record):
    keep = {k: record.get(k) for k in ('params', 'return_type', 'is_async', 'docstring', 'contract_kind', 'action_args', 'goal_default')}
    keep['schema'] = record.get('schema')
    keep['has_diagnostic'] = 'contract_diagnostic' in record
    return keep


def view(meta):
    return {
        **{k: meta.get(k) for k in COMPARED},
        'actions': {k: norm_action(v) for k, v in meta.get('actions', {}).items()},
        'status_properties': meta.get('status_properties', {}),
        'init_params': meta.get('init_params', []),
        'auto_methods': meta.get('auto_methods', {}),
    }


def diff(a, b, path=''):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f'{path}/{k}: only in forwarder')
            elif k not in b:
                out.append(f'{path}/{k}: missing in forwarder')
            else:
                out += diff(a[k], b[k], f'{path}/{k}')
    elif a != b:
        out.append(f'{path}: {str(a)[:160]} != {str(b)[:160]}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--os-src', required=True)
    ap.add_argument('--package-root', required=True)
    ap.add_argument('--package', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    sys.path.insert(0, a.os_src)
    from unilabos.registry.ast_registry_scanner import _parse_file
    root = Path(a.package_root)
    report = {'devices': 0, 'new_identical': 0, 'new_diffs': {}, 'generate_errors': {}, 'new_diff_kinds': Counter(),
              'original_actions': 0}
    with tempfile.TemporaryDirectory() as tmp:
        out_root = Path(tmp)
        for file in sorted((root / a.package).rglob('*.py')):
            if '__pycache__' in file.parts:
                continue
            try:
                devices, _ = _parse_file(file, root)
            except Exception as e:
                report['generate_errors'][str(file)] = 'scan original: ' + repr(e)
                continue
            module = '.'.join(file.relative_to(root).with_suffix('').parts)
            for meta in devices:
                cls = meta['class_name']
                report['devices'] += 1
                original = view(meta)
                report['original_actions'] += len(original['actions'])
                variants = [('new', {'options': ForwarderOptions(allow_registered_implementation=True)})]
                for label, kw in variants:
                    target = out_root / label / (a.package + '_fwd') / file.relative_to(root / a.package)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        target.write_text(generate_forwarder(file, module, cls, **kw), 'utf8')
                        scanned = [d for d in _parse_file(target, out_root / label)[0] if d['device_id'] == meta['device_id']]
                    except Exception as e:
                        report['generate_errors'][f'{label}:{meta["device_id"]}'] = repr(e)
                        continue
                    if not scanned:
                        report['generate_errors'][f'{label}:{meta["device_id"]}'] = 'forwarder not registered'
                        continue
                    d = diff(original, view(scanned[0]))
                    if not d:
                        report[label + '_identical'] += 1
                    else:
                        report[label + '_diffs'][meta['device_id']] = d[:20]
                        if label == 'new':
                            report['new_diff_kinds'].update({x.split(':')[0].split('/')[1] if '/' in x else x for x in d})
    Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), 'utf8')
    print({k: report[k] for k in ('devices', 'new_identical', 'original_actions')},
          'errors', len(report['generate_errors']), dict(report['new_diff_kinds']))


if __name__ == '__main__':
    main()
