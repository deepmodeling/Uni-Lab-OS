#!/usr/bin/env python3
"""
从 req_device_registry_upload.json 中提取指定设备的 action schema。

用法:
  # 列出所有设备及 action 数量（自动搜索注册表文件）
  python extract_device_actions.py

  # 指定注册表文件路径
  python extract_device_actions.py --registry <path/to/req_device_registry_upload.json>

  # 提取指定设备的 action 到目录
  python extract_device_actions.py <device_id> <output_dir>
  python extract_device_actions.py --registry <path> <device_id> <output_dir>

示例:
  python extract_device_actions.py --registry unilabos_data/req_device_registry_upload.json
  python extract_device_actions.py liquid_handler.prcxi .cursor/skills/unilab-device-api/actions/
"""
import json
import os
import sys
from datetime import datetime

REGISTRY_FILENAME = "req_device_registry_upload.json"

def find_registry(explicit_path=None):
    """
    查找 req_device_registry_upload.json 文件。

    搜索优先级：
    1. 用户通过 --registry 显式指定的路径
    2. <cwd>/unilabos_data/req_device_registry_upload.json
    3. <cwd>/req_device_registry_upload.json
    4. <script所在目录>/../../.. (workspace根) 下的 unilabos_data/
    5. 向上逐级搜索父目录（最多 5 层）
    """
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        if os.path.isdir(explicit_path):
            fp = os.path.join(explicit_path, REGISTRY_FILENAME)
            if os.path.isfile(fp):
                return fp
        print(f"警告: 指定的路径不存在: {explicit_path}")
        return None

    candidates = [
        os.path.join("unilabos_data", REGISTRY_FILENAME),
        REGISTRY_FILENAME,
    ]

    for c in candidates:
        if os.path.isfile(c):
            return c

    script_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_root = os.path.normpath(os.path.join(script_dir, "..", "..", ".."))
    for c in candidates:
        path = os.path.join(workspace_root, c)
        if os.path.isfile(path):
            return path

    cwd = os.getcwd()
    for _ in range(5):
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
        for c in candidates:
            path = os.path.join(cwd, c)
            if os.path.isfile(path):
                return path

    return None

def load_registry(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def list_devices(data):
    """列出所有包含 action_value_mappings 的设备，同时返回 module 路径"""
    resources = data.get('resources', [])
    devices = []
    for res in resources:
        rid = res.get('id', '')
        cls = res.get('class', {})
        avm = cls.get('action_value_mappings', {})
        module = cls.get('module', '')
        if avm:
            devices.append((rid, len(avm), module))
    return devices

def flatten_schema_to_goal(action_data):
    """将 schema 中嵌套的 goal 内容提升为顶层 schema，去掉 feedback/result 包装"""
    schema = action_data.get('schema', {})
    goal_schema = schema.get('properties', {}).get('goal', {})
    if goal_schema:
        action_data = dict(action_data)
        action_data['schema'] = goal_schema
    return action_data


def extract_actions(data, device_id, output_dir):
    """提取指定设备的 action schema 到独立 JSON 文件"""
    resources = data.get('resources', [])
    for res in resources:
        if res.get('id') == device_id:
            cls = res.get('class', {})
            module = cls.get('module', '')
            avm = cls.get('action_value_mappings', {})
            if not avm:
                print(f"设备 {device_id} 没有 action_value_mappings")
                return []

            if module:
                py_path = module.split(":")[0].replace(".", "/") + ".py"
                class_name = module.split(":")[-1] if ":" in module else ""
                print(f"Python 源码: {py_path}")
                if class_name:
                    print(f"设备类: {class_name}")

            os.makedirs(output_dir, exist_ok=True)
            written = []
            for action_name in sorted(avm.keys()):
                action_data = flatten_schema_to_goal(avm[action_name])
                filename = action_name.replace('-', '_') + '.json'
                filepath = os.path.join(output_dir, filename)
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(action_data, f, indent=2, ensure_ascii=False)
                written.append(filename)
                print(f"  {filepath}")
            return written

    print(f"设备 {device_id} 未找到")
    return []

def main():
    args = sys.argv[1:]
    explicit_registry = None

    if "--registry" in args:
        idx = args.index("--registry")
        if idx + 1 < len(args):
            explicit_registry = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("错误: --registry 需要指定路径")
            sys.exit(1)

    registry_path = find_registry(explicit_registry)
    if not registry_path:
        print(f"错误: 找不到 {REGISTRY_FILENAME}")
        print()
        print("解决方法:")
        print("  1. 先运行 unilab 启动命令，等待注册表生成")
        print("  2. 用 --registry 指定文件路径:")
        print(f"     python {sys.argv[0]} --registry <path/to/{REGISTRY_FILENAME}>")
        print()
        print("搜索过的路径:")
        for p in [
            os.path.join("unilabos_data", REGISTRY_FILENAME),
            REGISTRY_FILENAME,
            os.path.join("<workspace_root>", "unilabos_data", REGISTRY_FILENAME),
        ]:
            print(f"  - {p}")
        sys.exit(1)

    print(f"注册表: {registry_path}")
    mtime = os.path.getmtime(registry_path)
    gen_time = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    size_mb = os.path.getsize(registry_path) / (1024 * 1024)
    print(f"生成时间: {gen_time}  (文件大小: {size_mb:.1f} MB)")
    data = load_registry(registry_path)

    if len(args) == 0:
        devices = list_devices(data)
        print(f"\n找到 {len(devices)} 个设备:")
        print(f"{'设备 ID':<50} {'Actions':>7}  {'Python 模块'}")
        print("-" * 120)
        for did, count, module in sorted(devices, key=lambda x: x[0]):
            py_path = module.split(":")[0].replace(".", "/") + ".py" if module else ""
            print(f"{did:<50} {count:>7}  {py_path}")

    elif len(args) == 2:
        device_id = args[0]
        output_dir = args[1]
        print(f"\n提取 {device_id} 的 actions 到 {output_dir}/")
        written = extract_actions(data, device_id, output_dir)
        if written:
            print(f"\n共写入 {len(written)} 个 action 文件")

    else:
        print("用法:")
        print("  python extract_device_actions.py [--registry <path>]                # 列出设备")
        print("  python extract_device_actions.py [--registry <path>] <device_id> <dir>  # 提取 actions")
        sys.exit(1)

if __name__ == '__main__':
    main()
