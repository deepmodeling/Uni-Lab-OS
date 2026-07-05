#!/usr/bin/env bash
set -euo pipefail

# SZLab mixer 机械臂按工位 CLI 调试脚本。
# run Sxx 会生成该工位下 position/sensor 的一一对应测试用例，并覆盖 pick/place。
# 默认会连接真实 OPC UA/PLC 并下发机器人任务号，运行前请确认现场安全、Robot_Home/允许写入状态、急停可用、目标工位状态正确。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/../../../../../.." && pwd))"

PYTHON="${PYTHON:-/opt/homebrew/Caskroom/miniforge/base/envs/unilab/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python"
fi

RUNNER="$REPO_ROOT/scripts/run_workflow_local.py"
RUNTIME_CONFIG="$REPO_ROOT/tests/szlab_poly_studio/runtime_configs/szlab_mixer_runtime.json"
CSV_PATH="${CSV_PATH:-$REPO_ROOT/unilabos/devices/workstation/szlab_poly_studio/s12_robot/上位机通讯_new(3).csv}"
OPCUA_URL="${OPCUA_URL:-opc.tcp://192.168.1.10:4840}"
if [[ "$OPCUA_URL" != *"://"* ]]; then
  OPCUA_URL="opc.tcp://$OPCUA_URL"
fi
TIMEOUT="${TIMEOUT:-300}"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/unilabos_data/szlab_poly_studio/robot_cli_logs}"

# 调试控制:
#   MODE=both|pick|place    选择取/放动作，默认 both
#   DRY_RUN=1               只打印用例，不连接 PLC
#   CONFIRM=YES             跳过真实执行前确认
#   CONTINUE_ON_ERROR=1     单个用例失败后继续，默认继续
#   IGNORE_OPCUA_TOKEN_TIME_DRIFT=1  忽略 OPC UA token 时间漂移，仅用于现场调试
#   CLEAR_PC_TO_PLC_BEFORE_RUN=1     每个真实用例执行前先清空 PC->PLC 写入变量
#   SKIP_SENSOR_PRECHECK=1           跳过取/放传感器门禁，仅用于 PLC 连通性写入测试
#   SKIP_ROBOT_HANDSHAKE_CHECK=1     跳过 Robot_Home/允许写入/完成等待，仅用于 PLC 连通性写入测试
#   SKIP_RESET_AFTER_RUN=1           任务完成后保留任务号/Sxx参数；默认完成后全部清零
MODE="${MODE:-both}"
DRY_RUN="${DRY_RUN:-0}"
CONFIRM="${CONFIRM:-}"
CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-1}"
IGNORE_OPCUA_TOKEN_TIME_DRIFT="${IGNORE_OPCUA_TOKEN_TIME_DRIFT:-1}"
CLEAR_PC_TO_PLC_BEFORE_RUN="${CLEAR_PC_TO_PLC_BEFORE_RUN:-1}"
SKIP_SENSOR_PRECHECK="${SKIP_SENSOR_PRECHECK:-0}"
SKIP_ROBOT_HANDSHAKE_CHECK="${SKIP_ROBOT_HANDSHAKE_CHECK:-0}"
SKIP_RESET_AFTER_RUN="${SKIP_RESET_AFTER_RUN:-0}"
export SKIP_SENSOR_PRECHECK SKIP_ROBOT_HANDSHAKE_CHECK SKIP_RESET_AFTER_RUN

# 参数覆盖:
#   PRODUCT_TYPES="1 2 3"   产品类型。1=烧杯/TIP，2=250ml样品瓶，3=500ml样品瓶/烧杯(按动作定义)
#   SAMPLE_ID=cli-debug     S04/S05 记录样品 ID
PRODUCT_TYPES="${PRODUCT_TYPES:-1 2 3}"
SAMPLE_ID="${SAMPLE_ID:-cli-debug}"

usage() {
  cat <<'EOF'
用法:
  cli_robot_test.sh list
  cli_robot_test.sh run Sxx
  cli_robot_test.sh run-all

示例:
  DRY_RUN=1 ./cli_robot_test.sh run S04
  MODE=pick ./cli_robot_test.sh run S02
  PRODUCT_TYPES="1" ./cli_robot_test.sh run S03
  CONFIRM=YES CONTINUE_ON_ERROR=1 ./cli_robot_test.sh run S09

支持工位:
  S01 S02 S03 S04 S05 S06 S07 S071 S072 S08 S09 S10 S11

说明:
  - run Sxx 会自动枚举该工位下所有已知 position/sensor 对应关系。
  - MODE=both 默认同时生成 place 和 pick；S01 只有 pick。
  - S07 会同时覆盖 S071 和 S072；也可以单独 run S071 或 run S072。
  - 正常运行要求 Robot_Home=True 且 Robot_任务允许写入=True，并等待 Robot_任务完成 == 任务号。
  - DRY_RUN=1 只打印将执行的用例，不连接真实 PLC。
EOF
}

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

station_cases() {
  local station="$1"
  "$PYTHON" - "$station" "$MODE" "$PRODUCT_TYPES" "$SAMPLE_ID" <<'PY'
import json
import sys

station, mode, product_types_text, sample_id = sys.argv[1:5]


def product_types():
    return [int(item) for item in product_types_text.replace(",", " ").split()]


def slot_number(position):
    row, col = [int(part) for part in str(position).split("-", maxsplit=1)]
    return (row - 1) * 6 + col


def emit(action, params, station_name, task, position="", sensor="", product_type=""):
    if mode == "pick" and task == "place":
        return
    if mode == "place" and task == "pick":
        return
    label_parts = [station_name, task]
    if product_type != "":
        label_parts.append(f"product={product_type}")
    if position != "":
        label_parts.append(f"position={position}")
    if sensor:
        label_parts.append(f"sensor={sensor}")
    print(
        "\t".join(
            [
                action,
                json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                " ".join(label_parts),
                sensor,
                str(position),
            ]
        )
    )


S02_TIP_SENSORS = {str(index): f"传感器状态_上位机[0].NO[{index - 1}]" for index in range(1, 7)}
S03_UNUSED_BEAKER_SENSORS = {
    "1-1": "传感器状态_上位机[0].NO[6]",
    "1-2": "传感器状态_上位机[0].NO[7]",
    "1-3": "传感器状态_上位机[0].NO[8]",
    "1-4": "传感器状态_上位机[0].NO[9]",
    "1-5": "传感器状态_上位机[0].NO[10]",
    "1-6": "传感器状态_上位机[0].NO[11]",
    "2-1": "传感器状态_上位机[0].NO[12]",
    "2-2": "传感器状态_上位机[0].NO[13]",
    "2-3": "传感器状态_上位机[0].NO[14]",
    "2-4": "传感器状态_上位机[0].NO[15]",
    "2-5": "传感器状态_上位机[1].NO[0]",
    "2-6": "传感器状态_上位机[1].NO[1]",
    "3-1": "传感器状态_上位机[1].NO[2]",
    "3-2": "传感器状态_上位机[1].NO[3]",
    "3-3": "传感器状态_上位机[1].NO[4]",
    "3-4": "传感器状态_上位机[1].NO[5]",
    "3-5": "传感器状态_上位机[1].NO[6]",
    "3-6": "传感器状态_上位机[1].NO[7]",
}
S03_UNUSED_SAMPLE_VIAL_SENSORS = {
    "1-1": "传感器状态_上位机[1].NO[8]",
    "1-2": "传感器状态_上位机[1].NO[9]",
    "1-3": "传感器状态_上位机[1].NO[10]",
    "1-4": "传感器状态_上位机[1].NO[11]",
    "1-5": "传感器状态_上位机[1].NO[12]",
    "1-6": "传感器状态_上位机[1].NO[13]",
    "2-1": "传感器状态_上位机[1].NO[14]",
    "2-2": "传感器状态_上位机[1].NO[15]",
    "2-3": "传感器状态_上位机[2].NO[0]",
    "2-4": "传感器状态_上位机[2].NO[1]",
    "2-5": "传感器状态_上位机[2].NO[2]",
    "2-6": "传感器状态_上位机[2].NO[3]",
    "3-1": "传感器状态_上位机[2].NO[4]",
    "3-2": "传感器状态_上位机[2].NO[5]",
    "3-3": "传感器状态_上位机[2].NO[6]",
    "3-4": "传感器状态_上位机[2].NO[7]",
    "3-5": "传感器状态_上位机[2].NO[8]",
    "3-6": "传感器状态_上位机[2].NO[9]",
}
S04_SENSORS = {
    1: "传感器状态_上位机[2].NO[10]",
    2: "传感器状态_上位机[2].NO[11]",
    3: "传感器状态_上位机[2].NO[12]",
    4: "传感器状态_上位机[2].NO[13]",
    5: "传感器状态_上位机[2].NO[14]",
    6: "传感器状态_上位机[2].NO[15]",
}
S01_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[3].NO[6]",
}
POWDER_CONTAINER_SENSORS = {
    "1-1": "传感器状态_上位机[3].NO[8]",
    "1-2": "传感器状态_上位机[3].NO[9]",
    "1-3": "传感器状态_上位机[3].NO[10]",
    "2-1": "传感器状态_上位机[3].NO[11]",
    "2-2": "传感器状态_上位机[3].NO[12]",
    "2-3": "传感器状态_上位机[3].NO[13]",
}
S072_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[3].NO[14]",
    2: "传感器状态_上位机[3].NO[15]",
}
S08_PICK_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[3].NO[14]",
    2: "传感器状态_上位机[3].NO[15]",
}
S08_PLACE_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[4].NO[0]",
    2: "传感器状态_上位机[4].NO[1]",
    3: "传感器状态_上位机[4].NO[2]",
    4: "传感器状态_上位机[4].NO[3]",
    5: "传感器状态_上位机[4].NO[4]",
}
S09_SENSORS = [
    (1, 1, "传感器状态_上位机[4].NO[5]"),
    (1, 2, "传感器状态_上位机[4].NO[6]"),
    (3, 1, "传感器状态_上位机[4].NO[7]"),
]
S10_SENSORS = {
    "1-1": "传感器状态_上位机[4].NO[12]",
    "1-2": "传感器状态_上位机[4].NO[13]",
    "1-3": "传感器状态_上位机[4].NO[14]",
    "1-4": "传感器状态_上位机[4].NO[15]",
    "1-5": "传感器状态_上位机[5].NO[0]",
    "2-1": "传感器状态_上位机[5].NO[1]",
    "2-2": "传感器状态_上位机[5].NO[2]",
    "2-3": "传感器状态_上位机[5].NO[3]",
    "2-4": "传感器状态_上位机[5].NO[4]",
    "2-5": "传感器状态_上位机[5].NO[5]",
    "3-1": "传感器状态_上位机[5].NO[6]",
    "3-2": "传感器状态_上位机[5].NO[7]",
    "3-3": "传感器状态_上位机[5].NO[8]",
    "3-4": "传感器状态_上位机[5].NO[9]",
}
S11_USED_BEAKER_SENSORS = {
    "1-1": "传感器状态_上位机[6].NO[0]",
    "1-2": "传感器状态_上位机[6].NO[1]",
    "1-3": "传感器状态_上位机[6].NO[2]",
    "1-4": "传感器状态_上位机[6].NO[3]",
    "1-5": "传感器状态_上位机[6].NO[4]",
    "1-6": "传感器状态_上位机[6].NO[5]",
    "2-1": "传感器状态_上位机[6].NO[6]",
    "2-2": "传感器状态_上位机[6].NO[7]",
    "2-3": "传感器状态_上位机[6].NO[8]",
    "2-4": "传感器状态_上位机[6].NO[9]",
    "2-5": "传感器状态_上位机[6].NO[10]",
    "2-6": "传感器状态_上位机[6].NO[11]",
    "3-1": "传感器状态_上位机[6].NO[12]",
    "3-2": "传感器状态_上位机[6].NO[13]",
    "3-3": "传感器状态_上位机[6].NO[14]",
    "3-4": "传感器状态_上位机[6].NO[15]",
    "3-5": "传感器状态_上位机[7].NO[0]",
    "3-6": "传感器状态_上位机[7].NO[1]",
}
S11_USED_SAMPLE_VIAL_SENSORS = {
    "1-1": "传感器状态_上位机[7].NO[2]",
    "1-2": "传感器状态_上位机[7].NO[3]",
    "1-3": "传感器状态_上位机[7].NO[4]",
    "1-4": "传感器状态_上位机[7].NO[5]",
    "1-5": "传感器状态_上位机[7].NO[6]",
    "1-6": "传感器状态_上位机[7].NO[7]",
    "2-1": "传感器状态_上位机[7].NO[8]",
    "2-2": "传感器状态_上位机[7].NO[9]",
    "2-3": "传感器状态_上位机[7].NO[10]",
    "2-4": "传感器状态_上位机[7].NO[11]",
    "2-5": "传感器状态_上位机[7].NO[12]",
    "2-6": "传感器状态_上位机[7].NO[13]",
    "3-1": "传感器状态_上位机[7].NO[14]",
    "3-2": "传感器状态_上位机[7].NO[15]",
    "3-3": "传感器状态_上位机[8].NO[0]",
    "3-4": "传感器状态_上位机[8].NO[1]",
    "3-5": "传感器状态_上位机[8].NO[2]",
    "3-6": "传感器状态_上位机[8].NO[3]",
}

if station == "S01":
    for product_type in product_types():
        for position, sensor in S01_SENSOR_BY_POSITION.items():
            emit(
                "submit_pick_from_s01",
                {"product_type": product_type, "position": position},
                "S01",
                "pick",
                position,
                sensor,
                product_type,
            )
elif station == "S02":
    for position, sensor in S02_TIP_SENSORS.items():
        position_int = int(position)
        emit("submit_place_to_s02", {"position": position_int}, "S02", "place", position, sensor)
        emit("submit_pick_from_s02", {"position": position_int}, "S02", "pick", position, sensor)
elif station == "S03":
    for product_type in product_types():
        sensors = S03_UNUSED_BEAKER_SENSORS if product_type == 1 else S03_UNUSED_SAMPLE_VIAL_SENSORS
        for position, sensor in sensors.items():
            params = {"product_type": product_type, "position": position}
            emit("submit_place_to_s03", params, "S03", "place", position, sensor, product_type)
            emit("submit_pick_from_s03", params, "S03", "pick", position, sensor, product_type)
elif station == "S04":
    for position, sensor in S04_SENSORS.items():
        emit("submit_place_to_s04", {"position": position, "sample_id": sample_id}, "S04", "place", position, sensor)
        emit("submit_pick_from_s04", {"position": position}, "S04", "pick", position, sensor)
elif station == "S05":
    sensor = "传感器状态_上位机[3].NO[0]"
    emit("submit_place_to_s05", {"sample_id": sample_id}, "S05", "place", "1", sensor)
    emit("submit_pick_from_s05", {"sample_id": sample_id}, "S05", "pick", "1", sensor)
elif station == "S06":
    sensor = "传感器状态_上位机[3].NO[1]"
    emit("submit_place_to_s06", {}, "S06", "place", "1", sensor)
    emit("submit_pick_from_s06", {}, "S06", "pick", "1", sensor)
elif station in {"S07", "S071"}:
    for position, sensor in POWDER_CONTAINER_SENSORS.items():
        emit("submit_place_to_s071", {"position": position}, "S071", "place", position, sensor)
        emit("submit_pick_from_s071", {"position": position}, "S071", "pick", position, sensor)
    if station == "S071":
        raise SystemExit
    # S07 continues into S072 below.
    station = "S072"

if station == "S072":
    for product_type in product_types():
        for position, sensor in S072_SENSOR_BY_POSITION.items():
            params = {"product_type": product_type, "position": position}
            emit("submit_place_to_s072", params, "S072", "place", position, sensor, product_type)
            emit("submit_pick_from_s072", params, "S072", "pick", position, sensor, product_type)
elif station == "S08":
    for product_type in product_types():
        for position, sensor in S08_PLACE_SENSOR_BY_POSITION.items():
            emit("submit_place_to_s08", {"product_type": product_type, "position": position}, "S08", "place", position, sensor, product_type)
        for position, sensor in S08_PICK_SENSOR_BY_POSITION.items():
            emit("submit_pick_from_s08", {"product_type": product_type, "position": position}, "S08", "pick", position, sensor, product_type)
elif station == "S09":
    for product_type, position, sensor in S09_SENSORS:
        params = {"product_type": product_type, "position": position}
        emit("submit_place_to_s09", params, "S09", "place", position, sensor, product_type)
        emit("submit_pick_from_s09", params, "S09", "pick", position, sensor, product_type)
elif station == "S10":
    for index, (position_label, sensor) in enumerate(S10_SENSORS.items(), start=1):
        emit("submit_place_to_s10", {"position": index}, "S10", "place", position_label, sensor)
        emit("submit_pick_from_s10", {"position": index}, "S10", "pick", position_label, sensor)
elif station == "S11":
    for product_type in product_types():
        sensors = S11_USED_BEAKER_SENSORS if product_type == 1 else S11_USED_SAMPLE_VIAL_SENSORS
        for position, sensor in sensors.items():
            params = {"product_type": product_type, "position": position}
            emit("submit_place_to_s11", params, "S11", "place", position, sensor, product_type)
            emit("submit_pick_from_s11", params, "S11", "pick", position, sensor, product_type)
elif station not in {"S01", "S02", "S03", "S04", "S05", "S06", "S071"}:
    print(f"未知工位: {station}", file=sys.stderr)
    raise SystemExit(2)
PY
}

list_actions() {
  cat <<'EOF'
按工位调试:
  S01  进料取料: product_type/source_sensor 与 position/source_sensor
  S02  TIP: position 1..6 对应 TIP 传感器，覆盖 place/pick
  S03  未使用容器: product_type + 行列 position 对应传感器，覆盖 place/pick
  S04  磁搅: position 1..6 对应磁搅传感器，覆盖 place/pick
  S05  拍照: 单工位传感器，覆盖 place/pick
  S06  加溶剂: 单工位传感器，覆盖 place/pick
  S07  同时覆盖 S071 粉末容器和 S072 产品位
  S071 粉末容器: 行列 position 对应传感器，覆盖 place/pick
  S072 产品位: PRODUCT_TYPES x 内置 position/sensor 映射，覆盖 place/pick
  S08  开盖: PRODUCT_TYPES x 内置 position/sensor 映射，覆盖 place/pick
  S09  加液体/TIP: TIP 1..2 和烧杯 1，覆盖 place/pick
  S10  液体试剂瓶: position 1..14 对应传感器，覆盖 place/pick
  S11  已使用/成品: product_type + 行列 position 对应传感器，覆盖 place/pick

运行:
  ./cli_robot_test.sh run S04
  MODE=pick ./cli_robot_test.sh run S02
  DRY_RUN=1 ./cli_robot_test.sh run S11
EOF
}

write_graph() {
  local graph_file="$1"
  "$PYTHON" - "$graph_file" "$OPCUA_URL" "$CSV_PATH" "$TIMEOUT" <<'PY'
import csv
import json
import sys

graph_file, opcua_url, csv_path, timeout = sys.argv[1:5]


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


opcua_node_id_map = load_opcua_node_id_map(csv_path)
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
                "opcua_node_id_map": opcua_node_id_map,
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
    "name": f"robot_cli_{action}",
    "nodes": [
        {
            "uuid": f"robot-cli-{action}",
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

run_case() {
  local tmp_dir="$1"
  local station="$2"
  local index="$3"
  local action="$4"
  local params_json="$5"
  local label="$6"

  local graph_file="$tmp_dir/szlab_mixer_robot_graph.json"
  local workflow_file="$tmp_dir/${station}_${index}_${action}.json"
  local log_file="$LOG_DIR/${station}_${index}_${action}_$(date +%Y%m%d_%H%M%S).log"

  write_workflow "$workflow_file" "$action" "$params_json"
  echo "[$index] $label"
  echo "    action: $action"
  echo "    params: $params_json"
  echo "    log: $log_file"

  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi

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
}

confirm_real_run() {
  local station="$1"
  local count="$2"
  if [[ "$DRY_RUN" == "1" || "$CONFIRM" == "YES" ]]; then
    return 0
  fi
  cat <<EOF
将真实执行 $station 的 $count 个机械臂测试用例。
OPC UA: $OPCUA_URL
CSV: $CSV_PATH
MODE: $MODE
IGNORE_OPCUA_TOKEN_TIME_DRIFT: $IGNORE_OPCUA_TOKEN_TIME_DRIFT
CLEAR_PC_TO_PLC_BEFORE_RUN: $CLEAR_PC_TO_PLC_BEFORE_RUN
SKIP_SENSOR_PRECHECK: $SKIP_SENSOR_PRECHECK
SKIP_ROBOT_HANDSHAKE_CHECK: $SKIP_ROBOT_HANDSHAKE_CHECK
SKIP_RESET_AFTER_RUN: $SKIP_RESET_AFTER_RUN

请确认现场安全、机械臂路径无障碍、目标工位状态与 pick/place 前置传感器条件匹配。
EOF
  read -r -p "确认继续? 输入 YES: " answer
  if [[ "$answer" != "YES" ]]; then
    echo "已取消"
    exit 0
  fi
}

run_station() {
  local station
  station="$(normalize_station "$1")"
  ensure_files
  mkdir -p "$LOG_DIR"

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  local graph_file="$tmp_dir/szlab_mixer_robot_graph.json"
  local cases_file="$tmp_dir/${station}_cases.tsv"

  write_graph "$graph_file"
  station_cases "$station" > "$cases_file"

  local count
  count="$(wc -l < "$cases_file" | tr -d ' ')"
  if [[ "$count" == "0" ]]; then
    echo "$station 没有生成任何测试用例，请检查 MODE/PRODUCT_TYPES/SENSOR 配置" >&2
    exit 1
  fi

  echo "工位: $station"
  echo "测试用例数: $count"
  echo "DRY_RUN: $DRY_RUN"
  echo "Robot握手: Robot_Home -> Robot_任务允许写入 -> Robot_任务写入完成 -> Robot_任务完成==任务号"
  confirm_real_run "$station" "$count"

  local index=0
  local failures=0
  local action params_json label sensor position
  while IFS=$'\t' read -r action params_json label sensor position; do
    index=$((index + 1))
    if ! run_case "$tmp_dir" "$station" "$index" "$action" "$params_json" "$label"; then
      failures=$((failures + 1))
      echo "用例失败: $label" >&2
      if [[ "$CONTINUE_ON_ERROR" != "1" ]]; then
        exit 1
      fi
    fi
  done < "$cases_file"

  if [[ "$failures" != "0" ]]; then
    echo "$station 完成，但有 $failures/$count 个用例失败" >&2
    rm -rf "$tmp_dir"
    exit 1
  fi
  rm -rf "$tmp_dir"
  echo "$station 完成，$count 个用例全部通过"
}

run_all() {
  for station in S01 S02 S03 S04 S05 S06 S07 S08 S09 S10 S11; do
    run_station "$station"
  done
}

main() {
  local command="${1:-}"
  case "$command" in
    list)
      list_actions
      ;;
    run)
      if [[ $# -ne 2 ]]; then
        usage >&2
        exit 1
      fi
      run_station "$2"
      ;;
    run-all)
      run_all
      ;;
    -h|--help|help|"")
      usage
      ;;
    *)
      echo "未知命令: $command" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
