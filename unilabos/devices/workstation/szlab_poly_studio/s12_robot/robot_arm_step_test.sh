#!/usr/bin/env bash
set -euo pipefail

# SZLab robot 单步调试脚本。
# 默认只跑一个动作、一个 position、一个 sensor；跳过 Robot_Home，但保留 sensor/允许写入/完成检查。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/../../../../../.." && pwd))"

PYTHON="${PYTHON:-/opt/homebrew/Caskroom/miniforge/base/envs/unilab/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python"
fi

RUNNER="$REPO_ROOT/scripts/run_workflow_local.py"
RUNTIME_CONFIG="$REPO_ROOT/tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json"
CSV_PATH="${CSV_PATH:-$SCRIPT_DIR/上位机通讯_new(3).csv}"
OPCUA_URL="${OPCUA_URL:-opc.tcp://192.168.1.10:4840}"
if [[ "$OPCUA_URL" != *"://"* ]]; then
  OPCUA_URL="opc.tcp://$OPCUA_URL"
fi
TIMEOUT="${TIMEOUT:-300}"
WRITE_DONE_HOLD_SECONDS="${WRITE_DONE_HOLD_SECONDS:-2.0}"
WRITE_READBACK_TIMEOUT="${WRITE_READBACK_TIMEOUT:-5.0}"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/unilabos_data/szlab_poly_studio/robot_step_logs}"

# 默认跑 S03 的第一个位置；行列工位中 position=1 会映射为 1-1。
STATION="${STATION:-S03}"
TASK="${TASK:-pick}"
PRODUCT_TYPE="${PRODUCT_TYPE:-1}"
POSITION="${POSITION:-1}"
SAMPLE_ID="${SAMPLE_ID:-step-debug}"

# 单步调试默认跳过 Robot_Home，但不跳过 sensor 检查。
SKIP_ROBOT_PRECHECK_VARIABLES="${SKIP_ROBOT_PRECHECK_VARIABLES:-Robot_Home}"
SKIP_SENSOR_PRECHECK="${SKIP_SENSOR_PRECHECK:-0}"
SKIP_ROBOT_HANDSHAKE_CHECK="${SKIP_ROBOT_HANDSHAKE_CHECK:-0}"
SKIP_RESET_AFTER_RUN="${SKIP_RESET_AFTER_RUN:-0}"
CLEAR_PC_TO_PLC_BEFORE_RUN="${CLEAR_PC_TO_PLC_BEFORE_RUN:-1}"
IGNORE_OPCUA_TOKEN_TIME_DRIFT="${IGNORE_OPCUA_TOKEN_TIME_DRIFT:-1}"
DRY_RUN="${DRY_RUN:-0}"
CONFIRM="${CONFIRM:-}"
export SKIP_ROBOT_PRECHECK_VARIABLES SKIP_SENSOR_PRECHECK SKIP_ROBOT_HANDSHAKE_CHECK SKIP_RESET_AFTER_RUN

usage() {
  cat <<'EOF'
用法:
  robot_arm_step_test.sh
  robot_arm_step_test.sh S03 pick 1

环境变量:
  STATION=S03                 工位，默认 S03
  TASK=pick|place             动作，默认 pick
  POSITION=1                  位置，默认 1；S03/S071/S11 中表示第一个 1-1
  PRODUCT_TYPE=1              产品类型，默认 1
  DRY_RUN=1                   只打印 workflow，不连接 PLC
  SKIP_ROBOT_PRECHECK_VARIABLES=Robot_Home
                               跳过指定 Robot 前置变量检查，默认只跳过 Robot_Home
  SKIP_SENSOR_PRECHECK=1      跳过 sensor 检查，默认关闭
  SKIP_RESET_AFTER_RUN=1      完成后保留任务号/Sxx参数；默认完成后全部清零
  WRITE_DONE_HOLD_SECONDS=2   Robot_任务写入完成=True 的保持秒数，默认 2
  WRITE_READBACK_TIMEOUT=5    等待任务号/Sxx参数读回非 0 的秒数，默认 5

示例:
  DRY_RUN=1 ./robot_arm_step_test.sh
  CONFIRM=YES ./robot_arm_step_test.sh S03 pick 1
  CONFIRM=YES STATION=S04 TASK=place POSITION=1 ./robot_arm_step_test.sh
EOF
}

normalize_station() {
  local station
  station="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  case "$station" in
    S1) echo "S01" ;;
    S2) echo "S02" ;;
    S3) echo "S03" ;;
    S4) echo "S04" ;;
    S5) echo "S05" ;;
    S6) echo "S06" ;;
    S7) echo "S07" ;;
    S8) echo "S08" ;;
    S9) echo "S09" ;;
    *) echo "$station" ;;
  esac
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  usage
  exit 0
fi
if [[ $# -ge 1 ]]; then
  STATION="$1"
fi
if [[ $# -ge 2 ]]; then
  TASK="$2"
fi
if [[ $# -ge 3 ]]; then
  POSITION="$3"
fi
STATION="$(normalize_station "$STATION")"
TASK="$(printf '%s' "$TASK" | tr '[:upper:]' '[:lower:]')"

ensure_files() {
  if [[ ! -f "$RUNNER" ]]; then
    echo "找不到本地 workflow runner: $RUNNER" >&2
    exit 1
  fi
  if [[ ! -f "$RUNTIME_CONFIG" ]]; then
    echo "找不到 runtime config: $RUNTIME_CONFIG" >&2
    exit 1
  fi
  if [[ ! -f "$CSV_PATH" ]]; then
    echo "找不到 PLC CSV: $CSV_PATH" >&2
    exit 1
  fi
}

build_case() {
  "$PYTHON" - "$STATION" "$TASK" "$POSITION" "$PRODUCT_TYPE" "$SAMPLE_ID" "$SKIP_SENSOR_PRECHECK" <<'PY'
import json
import sys

station, task, position, product_type, sample_id, skip_sensor_precheck = sys.argv[1:7]
product_type = int(product_type)
skip_sensor_precheck = skip_sensor_precheck == "1"

ROW_COL_STATIONS = {"S03", "S071", "S11"}
NUMBERED_STATIONS = {"S02", "S04", "S08", "S09", "S10", "S072"}
position_label = position
if station in ROW_COL_STATIONS and "-" not in position:
    position = f"1-{int(position)}"
elif station in NUMBERED_STATIONS and "-" in position:
    position = position.split("-")[-1]

SENSORS = {
    "S01": {"1": "传感器状态_上位机[3].NO[6]"},
    "S02": {str(i): f"传感器状态_上位机[0].NO[{i - 1}]" for i in range(1, 7)},
    "S03": {
        "1-1": "传感器状态_上位机[0].NO[6]",
        "1-2": "传感器状态_上位机[0].NO[7]",
        "1-3": "传感器状态_上位机[0].NO[8]",
        "1-4": "传感器状态_上位机[0].NO[9]",
        "1-5": "传感器状态_上位机[0].NO[10]",
        "1-6": "传感器状态_上位机[0].NO[11]",
    },
    "S04": {str(i): f"传感器状态_上位机[2].NO[{9 + i}]" for i in range(1, 7)},
    "S05": {"1": "传感器状态_上位机[3].NO[0]"},
    "S06": {"1": "传感器状态_上位机[3].NO[1]"},
    "S071": {
        "1-1": "传感器状态_上位机[3].NO[8]",
        "1-2": "传感器状态_上位机[3].NO[9]",
        "1-3": "传感器状态_上位机[3].NO[10]",
        "2-1": "传感器状态_上位机[3].NO[11]",
        "2-2": "传感器状态_上位机[3].NO[12]",
        "2-3": "传感器状态_上位机[3].NO[13]",
    },
    "S072": {"1": "传感器状态_上位机[3].NO[14]", "2": "传感器状态_上位机[3].NO[15]"},
    "S08": {"1": "传感器状态_上位机[3].NO[14]" if task == "pick" else "传感器状态_上位机[4].NO[0]"},
    "S09": {"1": "传感器状态_上位机[4].NO[5]", "2": "传感器状态_上位机[4].NO[6]"},
    "S10": {"1": "传感器状态_上位机[4].NO[12]"},
    "S11": {"1-1": "传感器状态_上位机[6].NO[0]"},
}

def params_for(station_name: str) -> tuple[str, dict]:
    prefix = "submit_pick_from" if task == "pick" else "submit_place_to"
    if station_name == "S01":
        if task != "pick":
            raise ValueError("S01 只支持 pick")
        return "submit_pick_from_s01", {"product_type": product_type, "position": int(position)}
    if station_name == "S03":
        return f"{prefix}_s03", {"product_type": product_type, "position": position}
    if station_name == "S04":
        params = {"position": int(position)}
        if task == "place":
            params["sample_id"] = sample_id
        return f"{prefix}_s04", params
    if station_name in {"S05", "S06"}:
        return f"{prefix}_{station_name.lower()}", ({"sample_id": sample_id} if station_name == "S05" else {})
    if station_name == "S071":
        return f"{prefix}_s071", {"position": position}
    if station_name in {"S072", "S08", "S09"}:
        return f"{prefix}_{station_name.lower()}", {"product_type": product_type, "position": int(position)}
    if station_name in {"S02", "S10"}:
        return f"{prefix}_{station_name.lower()}", {"position": int(position)}
    if station_name == "S11":
        return f"{prefix}_s11", {"product_type": product_type, "position": position}
    raise ValueError(f"暂不支持工位: {station_name}")

sensor = SENSORS.get(station, {}).get(str(position))
if not sensor and not skip_sensor_precheck:
    raise ValueError(f"找不到默认 sensor: station={station}, position={position_label} mapped={position}")
action, params = params_for(station)
print(json.dumps({"action": action, "params": params, "sensor": sensor or "SKIPPED", "position": position}, ensure_ascii=False))
PY
}

write_graph() {
  local graph_file="$1"
  "$PYTHON" - "$graph_file" "$OPCUA_URL" "$CSV_PATH" "$TIMEOUT" "$WRITE_DONE_HOLD_SECONDS" "$WRITE_READBACK_TIMEOUT" <<'PY'
import csv
import json
import sys

graph_file, opcua_url, csv_path, timeout, write_done_hold_seconds, write_readback_timeout = sys.argv[1:7]

def load_opcua_node_id_map(csv_path):
    node_id_map = {}
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "gb18030", "gbk"):
        for delimiter in (",", "\t"):
            try:
                with open(csv_path, newline="", encoding=encoding) as csv_file:
                    reader = csv.DictReader(csv_file, delimiter=delimiter)
                    if "变量名" not in (reader.fieldnames or []):
                        continue
                    for row in reader:
                        name = (row.get("变量名") or "").strip()
                        if name:
                            node_id_map[name] = f"ns=4;s=上位机通讯|{name}"
                return node_id_map
            except UnicodeDecodeError:
                node_id_map.clear()
                break
    return node_id_map

graph = {
    "nodes": [
        {
            "id": "szlab_poly_plc",
            "name": "szlab_poly_plc",
            "children": [],
            "parent": None,
            "type": "device",
            "class": "szlab_poly_plc",
            "position": {"x": 0, "y": 0, "z": 0},
            "config": {
                "url": opcua_url,
                "csv_path": csv_path,
                "timeout": float(timeout),
                "opcua_node_id_map": load_opcua_node_id_map(csv_path),
            },
            "data": {},
        },
        {
            "id": "szlab_mixer_robot",
            "name": "szlab_mixer_robot",
            "children": [],
            "parent": None,
            "type": "device",
            "class": "szlab_mixer_robot",
            "position": {"x": 420, "y": 0, "z": 0},
            "config": {
                "plc_device_id": "szlab_poly_plc",
                "timeout": float(timeout),
                "write_done_hold_seconds": float(write_done_hold_seconds),
                "write_readback_timeout": float(write_readback_timeout),
            },
            "data": {},
        },
    ],
    "links": [],
}
with open(graph_file, "w", encoding="utf-8") as f:
    json.dump(graph, f, ensure_ascii=False, indent=2)
PY
}

write_workflow() {
  local workflow_file="$1"
  local action="$2"
  local params_json="$3"
  "$PYTHON" - "$workflow_file" "$action" "$params_json" <<'PY'
import json
import sys

workflow_file, action, params_json = sys.argv[1:4]
workflow = {
    "name": f"robot_arm_step_{action}",
    "nodes": [
        {
            "uuid": f"robot-arm-step-{action}",
            "name": f"auto-{action}",
            "device_name": "szlab_mixer_robot",
            "param": json.loads(params_json),
        }
    ],
    "edges": [],
}
with open(workflow_file, "w", encoding="utf-8") as f:
    json.dump(workflow, f, ensure_ascii=False, indent=2)
PY
}

confirm_real_run() {
  if [[ "$DRY_RUN" == "1" || "$CONFIRM" == "YES" ]]; then
    return 0
  fi
  cat <<EOF
将真实执行一个机械臂单步动作。
OPC UA: $OPCUA_URL
CSV: $CSV_PATH
STATION: $STATION
TASK: $TASK
POSITION: $POSITION
PRODUCT_TYPE: $PRODUCT_TYPE
SKIP_ROBOT_PRECHECK_VARIABLES: $SKIP_ROBOT_PRECHECK_VARIABLES
SKIP_SENSOR_PRECHECK: $SKIP_SENSOR_PRECHECK

请确认现场安全、路径无障碍、目标工位 sensor 状态与 $TASK 条件匹配。
EOF
  read -r -p "确认继续? 输入 YES: " answer
  if [[ "$answer" != "YES" ]]; then
    echo "已取消"
    exit 0
  fi
}

run_step() {
  ensure_files
  mkdir -p "$LOG_DIR"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  local graph_file="$tmp_dir/robot_arm_step_graph.json"
  local workflow_file="$tmp_dir/robot_arm_step_workflow.json"
  local case_json
  case_json="$(build_case)"
  local action params_json sensor mapped_position
  action="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["action"])' "$case_json")"
  params_json="$("$PYTHON" -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["params"], ensure_ascii=False, separators=(",", ":")))' "$case_json")"
  sensor="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["sensor"])' "$case_json")"
  mapped_position="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["position"])' "$case_json")"
  local log_file="$LOG_DIR/${STATION}_${TASK}_${POSITION}_${action}_$(date +%Y%m%d_%H%M%S).log"

  write_graph "$graph_file"
  write_workflow "$workflow_file" "$action" "$params_json"

  echo "单步动作: $STATION $TASK position=$POSITION mapped_position=$mapped_position sensor=$sensor"
  echo "action: $action"
  echo "params: $params_json"
  echo "跳过 Robot前置变量: $SKIP_ROBOT_PRECHECK_VARIABLES"
  echo "Robot_任务写入完成保持秒数: $WRITE_DONE_HOLD_SECONDS"
  echo "任务参数读回等待秒数: $WRITE_READBACK_TIMEOUT"
  echo "完成后清除任务参数: $([[ "$SKIP_RESET_AFTER_RUN" == "1" ]] && echo no || echo yes)"
  echo "sensor 检查: $([[ "$SKIP_SENSOR_PRECHECK" == "1" ]] && echo skipped || echo enabled)"
  echo "log: $log_file"

  if [[ "$DRY_RUN" == "1" ]]; then
    rm -rf "$tmp_dir"
    return 0
  fi
  confirm_real_run

  local ignore_token_time_drift_args=()
  if [[ "$IGNORE_OPCUA_TOKEN_TIME_DRIFT" == "1" ]]; then
    ignore_token_time_drift_args=(--ignore-opcua-token-time-drift)
  fi
  local clear_pc_to_plc_args=()
  if [[ "$CLEAR_PC_TO_PLC_BEFORE_RUN" == "1" ]]; then
    clear_pc_to_plc_args=(--clear-pc-to-plc-before-run)
  fi

  "$PYTHON" "$RUNNER" \
    --runtime-config "$RUNTIME_CONFIG" \
    --graph "$graph_file" \
    --workflow "$workflow_file" \
    --url "$OPCUA_URL" \
    --csv "$CSV_PATH" \
    --timeout "$TIMEOUT" \
    --no-subscription \
    "${ignore_token_time_drift_args[@]}" \
    "${clear_pc_to_plc_args[@]}" \
    --log-file "$log_file"
  rm -rf "$tmp_dir"
}

run_step
