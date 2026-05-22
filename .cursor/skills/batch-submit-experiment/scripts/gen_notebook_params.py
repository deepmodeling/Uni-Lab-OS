#!/usr/bin/env python3
"""
从 workflow 模板详情 + 本地设备注册表生成 notebook 提交用的 node_params 模板。

用法:
  python gen_notebook_params.py --auth <token> --base <url> --workflow-uuid <uuid> [选项]

选项:
  --auth <token>          Lab token（base64(ak:sk) 的结果，不含 "Lab " 前缀）
  --base <url>            API 基础 URL（如 https://leap-lab.test.bohrium.com）
  --workflow-uuid <uuid>  目标 workflow 的 UUID
  --registry <path>       本地注册表文件路径（默认自动搜索）
  --rounds <n>            实验轮次数（默认 1）
  --output <path>         输出模板文件路径（默认 notebook_template.json）
  --dump-response         打印 workflow detail API 的原始响应（调试用）

示例:
  python gen_notebook_params.py \\
    --auth YTFmZDlkNGUtxxxx \\
    --base https://leap-lab.test.bohrium.com \\
    --workflow-uuid abc-123-def \\
    --rounds 2
"""
import copy
import json
import os
import sys
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REGISTRY_FILENAME = "req_device_registry_upload.json"


def find_registry(explicit_path=None):
    """查找本地注册表文件，逻辑同 extract_device_actions.py"""
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        if os.path.isdir(explicit_path):
            fp = os.path.join(explicit_path, REGISTRY_FILENAME)
            if os.path.isfile(fp):
                return fp
        print(f"警告: 指定的注册表路径不存在: {explicit_path}")
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
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_registry_index(registry_data):
    """构建 device_id → action_value_mappings 的索引"""
    index = {}
    for res in registry_data.get("resources", []):
        rid = res.get("id", "")
        avm = res.get("class", {}).get("action_value_mappings", {})
        if rid and avm:
            index[rid] = avm
    return index


def flatten_goal_schema(action_data):
    """从 action_value_mappings 条目中提取 goal 层的 schema"""
    schema = action_data.get("schema", {})
    goal_schema = schema.get("properties", {}).get("goal", {})
    return goal_schema if goal_schema else schema


def build_param_template(goal_schema):
    """根据 goal schema 生成 param 模板，含类型标注"""
    properties = goal_schema.get("properties", {})
    required = set(goal_schema.get("required", []))
    template = {}
    for field_name, field_def in properties.items():
        if field_name == "unilabos_device_id":
            continue
        ftype = field_def.get("type", "any")
        default = field_def.get("default")
        if default is not None:
            template[field_name] = default
        elif ftype == "string":
            template[field_name] = f"$TODO ({ftype}, {'required' if field_name in required else 'optional'})"
        elif ftype == "number" or ftype == "integer":
            template[field_name] = 0
        elif ftype == "boolean":
            template[field_name] = False
        elif ftype == "array":
            template[field_name] = []
        elif ftype == "object":
            template[field_name] = {}
        else:
            template[field_name] = f"$TODO ({ftype})"
    return template


def fetch_workflow_detail(base_url, auth_token, workflow_uuid):
    """调用 workflow detail API"""
    url = f"{base_url}/api/v1/lab/workflow/template/detail/{workflow_uuid}"
    req = Request(url, method="GET")
    req.add_header("Authorization", f"Lab {auth_token}")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"API 错误 {e.code}: {body}")
        return None
    except URLError as e:
        print(f"网络错误: {e.reason}")
        return None


def extract_nodes_from_response(response):
    """
    从 workflow detail 响应中提取 action 节点列表。
    适配多种可能的响应格式。

    返回: [(node_uuid, resource_template_name, node_template_name, existing_param), ...]
    """
    data = response.get("data", response)

    search_keys = ["nodes", "workflow_nodes", "node_list", "steps"]
    nodes_raw = None
    for key in search_keys:
        if key in data and isinstance(data[key], list):
            nodes_raw = data[key]
            break

    if nodes_raw is None:
        if isinstance(data, list):
            nodes_raw = data
        else:
            for v in data.values():
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    nodes_raw = v
                    break

    if not nodes_raw:
        print("警告: 未能从响应中提取节点列表")
        print("响应顶层 keys:", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        return []

    result = []
    for node in nodes_raw:
        if not isinstance(node, dict):
            continue

        node_uuid = (
            node.get("uuid")
            or node.get("node_uuid")
            or node.get("id")
            or ""
        )
        resource_name = (
            node.get("resource_template_name")
            or node.get("device_id")
            or node.get("resource_name")
            or node.get("device_name")
            or ""
        )
        template_name = (
            node.get("node_template_name")
            or node.get("action_name")
            or node.get("template_name")
            or node.get("action")
            or node.get("name")
            or ""
        )
        existing_param = node.get("param", {}) or {}

        if node_uuid:
            result.append((node_uuid, resource_name, template_name, existing_param))

    return result


def generate_template(nodes, registry_index, rounds):
    """生成 notebook 提交模板"""
    node_params = []
    schema_info = {}

    datas_template = []
    for node_uuid, resource_name, template_name, existing_param in nodes:
        param_template = {}
        matched = False

        if resource_name and template_name and resource_name in registry_index:
            avm = registry_index[resource_name]
            if template_name in avm:
                goal_schema = flatten_goal_schema(avm[template_name])
                param_template = build_param_template(goal_schema)
                goal_default = avm[template_name].get("goal_default", {})
                if goal_default:
                    for k, v in goal_default.items():
                        if k in param_template and v is not None:
                            param_template[k] = v
                matched = True

                schema_info[node_uuid] = {
                    "device_id": resource_name,
                    "action_name": template_name,
                    "action_type": avm[template_name].get("type", ""),
                    "schema_properties": list(goal_schema.get("properties", {}).keys()),
                    "required": goal_schema.get("required", []),
                }

        if not matched and existing_param:
            param_template = existing_param

        if not matched and not existing_param:
            schema_info[node_uuid] = {
                "device_id": resource_name,
                "action_name": template_name,
                "warning": "未在本地注册表中找到匹配的 action schema",
            }

        datas_template.append({
            "node_uuid": node_uuid,
            "param": param_template,
            "sample_params": [
                {
                    "container_uuid": "$TODO_CONTAINER_UUID",
                    "sample_value": {
                        "liquid_names": "$TODO_LIQUID_NAME",
                        "volumes": 0,
                    },
                }
            ],
        })

    for i in range(rounds):
        node_params.append({
            "sample_uuids": f"$TODO_SAMPLE_UUID_ROUND_{i + 1}",
            "datas": copy.deepcopy(datas_template),
        })

    return {
        "lab_uuid": "$TODO_LAB_UUID",
        "project_uuid": "$TODO_PROJECT_UUID",
        "workflow_uuid": "$TODO_WORKFLOW_UUID",
        "name": "$TODO_EXPERIMENT_NAME",
        "node_params": node_params,
        "_schema_info（仅参考，提交时删除）": schema_info,
    }


def parse_args(argv):
    """简单的参数解析"""
    opts = {
        "auth": None,
        "base": None,
        "workflow_uuid": None,
        "registry": None,
        "rounds": 1,
        "output": "notebook_template.json",
        "dump_response": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--auth" and i + 1 < len(argv):
            opts["auth"] = argv[i + 1]
            i += 2
        elif arg == "--base" and i + 1 < len(argv):
            opts["base"] = argv[i + 1].rstrip("/")
            i += 2
        elif arg == "--workflow-uuid" and i + 1 < len(argv):
            opts["workflow_uuid"] = argv[i + 1]
            i += 2
        elif arg == "--registry" and i + 1 < len(argv):
            opts["registry"] = argv[i + 1]
            i += 2
        elif arg == "--rounds" and i + 1 < len(argv):
            opts["rounds"] = int(argv[i + 1])
            i += 2
        elif arg == "--output" and i + 1 < len(argv):
            opts["output"] = argv[i + 1]
            i += 2
        elif arg == "--dump-response":
            opts["dump_response"] = True
            i += 1
        else:
            print(f"未知参数: {arg}")
            i += 1
    return opts


def main():
    opts = parse_args(sys.argv[1:])

    if not opts["auth"] or not opts["base"] or not opts["workflow_uuid"]:
        print("用法:")
        print("  python gen_notebook_params.py --auth <token> --base <url> --workflow-uuid <uuid> [选项]")
        print()
        print("必需参数:")
        print("  --auth <token>          Lab token（base64(ak:sk)）")
        print("  --base <url>            API 基础 URL")
        print("  --workflow-uuid <uuid>  目标 workflow UUID")
        print()
        print("可选参数:")
        print("  --registry <path>       注册表文件路径（默认自动搜索）")
        print("  --rounds <n>            实验轮次数（默认 1）")
        print("  --output <path>         输出文件路径（默认 notebook_template.json）")
        print("  --dump-response         打印 API 原始响应")
        sys.exit(1)

    # 1. 查找并加载本地注册表
    registry_path = find_registry(opts["registry"])
    registry_index = {}
    if registry_path:
        mtime = os.path.getmtime(registry_path)
        gen_time = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"注册表: {registry_path}  (生成时间: {gen_time})")
        registry_data = load_registry(registry_path)
        registry_index = build_registry_index(registry_data)
        print(f"已索引 {len(registry_index)} 个设备的 action schemas")
    else:
        print("警告: 未找到本地注册表，将跳过 param 模板生成")
        print("  提交时需要手动填写各节点的 param 字段")

    # 2. 获取 workflow 详情
    print(f"\n正在获取 workflow 详情: {opts['workflow_uuid']}")
    response = fetch_workflow_detail(opts["base"], opts["auth"], opts["workflow_uuid"])
    if not response:
        print("错误: 无法获取 workflow 详情")
        sys.exit(1)

    if opts["dump_response"]:
        print("\n=== API 原始响应 ===")
        print(json.dumps(response, indent=2, ensure_ascii=False)[:5000])
        print("=== 响应结束（截断至 5000 字符） ===\n")

    # 3. 提取节点
    nodes = extract_nodes_from_response(response)
    if not nodes:
        print("错误: 未能从 workflow 中提取任何 action 节点")
        print("请使用 --dump-response 查看原始响应结构")
        sys.exit(1)

    print(f"\n找到 {len(nodes)} 个 action 节点:")
    print(f"  {'节点 UUID':<40} {'设备 ID':<30} {'动作名':<25} {'Schema'}")
    print("  " + "-" * 110)
    for node_uuid, resource_name, template_name, _ in nodes:
        matched = "✓" if (resource_name in registry_index and
                          template_name in registry_index.get(resource_name, {})) else "✗"
        print(f"  {node_uuid:<40} {resource_name:<30} {template_name:<25} {matched}")

    # 4. 生成模板
    template = generate_template(nodes, registry_index, opts["rounds"])
    template["workflow_uuid"] = opts["workflow_uuid"]

    output_path = opts["output"]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    print(f"\n模板已写入: {output_path}")
    print(f"  轮次数: {opts['rounds']}")
    print(f"  节点数/轮: {len(nodes)}")
    print()
    print("下一步:")
    print("  1. 打开模板文件，将 $TODO 占位符替换为实际值")
    print("  2. 删除 _schema_info 字段（仅供参考）")
    print("  3. 使用 POST /api/v1/lab/notebook 提交")


if __name__ == "__main__":
    main()
