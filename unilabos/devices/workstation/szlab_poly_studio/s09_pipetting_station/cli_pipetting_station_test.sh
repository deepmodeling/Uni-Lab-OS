#!/usr/bin/env bash
set -euo pipefail

# SZLab S09 移液站 CLI 调试脚本。
# 默认会连接真实 OPC UA/PLC 并下发 S09 工艺参数，运行前请确认现场安全、移液站处于可调试状态。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/../../../../../.." && pwd))"

PYTHON="${PYTHON:-/opt/mamba/envs/unilab/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python"
fi

RUNNER="$REPO_ROOT/scripts/run_workflow_local.py"
DEFAULT_CSV="$REPO_ROOT/unilabos/devices/workstation/szlab_poly_studio/s09_pipetting_station/pipetting_station_nodes.csv"
CSV_PATH="${CSV_PATH:-$DEFAULT_CSV}"
OPCUA_URL="${OPCUA_URL:-opc.tcp://192.168.1.10:4840}"
if [[ "$OPCUA_URL" != *"://"* ]]; then
  OPCUA_URL="opc.tcp://$OPCUA_URL"
fi
LOG_DIR="${LOG_DIR:-$REPO_ROOT/unilabos_data/szlab_poly_studio/s09_pipetting_cli_logs}"

# 调试控制:
#   DRY_RUN=1                         只打印用例和临时 workflow，不连接 PLC
#   CONFIRM=YES                       跳过真实执行前确认
#   CONTINUE_ON_ERROR=1               单个用例失败后继续，默认继续
#   IGNORE_OPCUA_TOKEN_TIME_DRIFT=1   忽略 OPC UA token 时间漂移，仅用于现场调试
#   CLEAR_PC_TO_PLC_BEFORE_RUN=1      执行前清空机器人 PC->PLC 变量；S09 工艺调试默认不启用
DRY_RUN="${DRY_RUN:-0}"
CONFIRM="${CONFIRM:-}"
CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-1}"
IGNORE_OPCUA_TOKEN_TIME_DRIFT="${IGNORE_OPCUA_TOKEN_TIME_DRIFT:-1}"
CLEAR_PC_TO_PLC_BEFORE_RUN="${CLEAR_PC_TO_PLC_BEFORE_RUN:-0}"

# S09 动作参数覆盖:
HOME_POSITIONS="${HOME_POSITIONS:-1}"
PROCESSES="${PROCESSES:-5 7 8 6 9 10}"
TIP_BOX_INDEX="${TIP_BOX_INDEX:-1}"
RELEASE_TIP_BOX_INDEX="${RELEASE_TIP_BOX_INDEX:-2}"
TIP_INDEX="${TIP_INDEX:-1}"
LIQUID_BOTTLE_INDEX="${LIQUID_BOTTLE_INDEX:-1}"
STATION="${STATION:-1}"
ASPIRATE_VOLUME="${ASPIRATE_VOLUME:-1}"
DISPENSE_VOLUME="${DISPENSE_VOLUME:-1}"
VOLUME_UNIT="${VOLUME_UNIT:-raw}"
REMAINING_VOLUME="${REMAINING_VOLUME:-100.0}"
REQUIRE_ALLOW="${REQUIRE_ALLOW:-false}"
RESET_DELAY="${RESET_DELAY:-0.1}"
SAMPLE_ID="${SAMPLE_ID:-cli-debug-s09}"

usage() {
  cat <<'EOF'
用法:
  cli_pipetting_station_test.sh list
  cli_pipetting_station_test.sh run <action>
  cli_pipetting_station_test.sh run-all

支持 action:
  status              读取 S09 状态
  prepare             确认 S09 唯一加液工位空闲
  allow               读取 S09允许加工（允许参数写入）信号
  home                一次读取四个原点信号（只读，不动作）
  safe                执行 HOME_POSITIONS 中的去安全位工艺，默认只测 1 个
  go-home             safe 的别名，执行“到原点/安全位”
  init-volume         初始化 1-5 号液体瓶剩余液量
  set-volume          写入 LIQUID_BOTTLE_INDEX 的剩余液量
  balance             读取天平读数
  process             执行 PROCESSES 中的单个 PLC 工艺
  take-tip            取 TIP（工艺 5）
  take-liquid         液体瓶取液（工艺 7）
  dispense            烧杯放液（工艺 8）
  release-tip         放 TIP（工艺 6）
  liquid-steps        按顺序执行 take-tip -> take-liquid -> dispense -> release-tip
  add-liquid          执行业务单次加液: 5 -> 7 -> 8 -> 6
  workflow            执行包含一个液体步骤的 run_liquid_workflow

示例:
  DRY_RUN=1 ./cli_pipetting_station_test.sh run process
  CONFIRM=YES PROCESSES="5" ./cli_pipetting_station_test.sh run process
  CONFIRM=YES ./cli_pipetting_station_test.sh run allow
  CONFIRM=YES HOME_POSITIONS="1" ./cli_pipetting_station_test.sh run go-home
  CONFIRM=YES HOME_POSITIONS="2" ./cli_pipetting_station_test.sh run go-home
  CONFIRM=YES HOME_POSITIONS="3" ./cli_pipetting_station_test.sh run go-home
  CONFIRM=YES HOME_POSITIONS="4" ./cli_pipetting_station_test.sh run go-home
  CONFIRM=YES ./cli_pipetting_station_test.sh run take-tip
  CONFIRM=YES VOLUME_UNIT=mL ASPIRATE_VOLUME=2 ./cli_pipetting_station_test.sh run take-liquid
  CONFIRM=YES VOLUME_UNIT=mL DISPENSE_VOLUME=2 ./cli_pipetting_station_test.sh run dispense
  CONFIRM=YES ./cli_pipetting_station_test.sh run release-tip
  CONFIRM=YES VOLUME_UNIT=mL ASPIRATE_VOLUME=2 DISPENSE_VOLUME=2 ./cli_pipetting_station_test.sh run liquid-steps
  CONFIRM=YES VOLUME_UNIT=mL ASPIRATE_VOLUME=6 DISPENSE_VOLUME=6 ./cli_pipetting_station_test.sh run add-liquid
  CONFIRM=YES TIP_INDEX=3 LIQUID_BOTTLE_INDEX=2 ASPIRATE_VOLUME=50 DISPENSE_VOLUME=50 ./cli_pipetting_station_test.sh run add-liquid
  CONFIRM=YES ./cli_pipetting_station_test.sh run balance

常用环境变量:
  OPCUA_URL=opc.tcp://192.168.1.10:4840
  CSV_PATH=/path/to/pipetting_station_nodes.csv
  PYTHON=/opt/mamba/envs/unilab/bin/python
  VOLUME_UNIT=raw|uL|mL
EOF
}

ensure_files() {
  if [[ ! -f "$RUNNER" ]]; then
    echo "找不到本地 workflow runner: $RUNNER" >&2
    exit 1
  fi
  if [[ ! -f "$CSV_PATH" ]]; then
    echo "找不到 PLC CSV: $CSV_PATH" >&2
    exit 1
  fi
}

list_actions() {
  cat <<'EOF'
S09 移液站调试动作:
  status        get_pipetting_status
  prepare       prepare_liquid_station
  allow         read_allow_process，读取 S09允许加工（允许参数写入）信号
  home          read_home_positions，一次返回四个原点信号，只读
  safe          go_to_safe_position(home_position=1..4)，默认只测 HOME_POSITIONS=1
  go-home       safe 的别名，执行 go_to_safe_position(home_position=1..4)
  init-volume   initialize_liquid_bottle_remaining_volumes
  set-volume    set_liquid_bottle_remaining_volume
  balance       read_balance
  process       run_process(process=5/7/8/6/9/10 by default)
  take-tip      run_process(process=5)
  take-liquid   run_process(process=7)
  dispense      run_process(process=8)
  release-tip   run_process(process=6)
  liquid-steps  依次执行 5 -> 7 -> 8 -> 6
  add-liquid    add_liquid
  workflow      run_liquid_workflow

S09 工艺号:
  1 去安全位1（机器人 TIP 盒取放）
  2 去安全位2（机器人液体试剂 1/2/3 取放）
  3 去安全位3（机器人液体试剂 4/5 取放）
  4 去安全位4（机器人烧杯取放）
  5 取 TIP
  6 放 TIP
  7 液体瓶取液（润洗一次后取液）
  8 烧杯放液
  9 测密度抽液并读取天平
  10 测密度排液并读取天平
EOF
}

write_runtime_config() {
  local runtime_file="$1"
  "$PYTHON" - "$runtime_file" <<'PY'
import json
import sys

runtime_file = sys.argv[1]
runtime_config = {
    "device_factory": {
        "plc_device_id": "szlab_poly_plc",
        "devices": {
            "szlab_poly_plc": "unilabos.devices.workstation.szlab_poly_studio.plc.SZLabPolyPLCDevice",
            "szlab_mixer_pipetting_station": (
                "unilabos.devices.workstation.szlab_poly_studio."
                "s09_pipetting_station.pipetting_station.SzlabMixerPipettingStationDevice"
            ),
        },
    },
    "opc_snapshot": {
        "common_variables": [],
        "action_variables": {
            "prepare_liquid_station": ["工站状态[8]"],
            "read_allow_process": ["S09允许加工"],
            "read_home_positions": ["S09原点信号_1", "S09原点信号_2", "S09原点信号_3", "S09原点信号_4"],
            "check_home_position": ["S09原点信号_1", "S09原点信号_2", "S09原点信号_3", "S09原点信号_4"],
            "go_to_safe_position": [
                "S09允许加工",
                "S09工艺选择",
                "S09参数写入完成",
                "S09工艺完成",
                "S09TIP盒工位编号",
                "S09TIP编号",
                "S09液体瓶编号",
                "S09抽液量",
                "S09放液量",
            ],
            "run_process": [
                "S09工艺选择",
                "S09允许加工",
                "S09参数写入完成",
                "S09工艺完成",
                "S09TIP盒工位编号",
                "S09TIP编号",
                "S09液体瓶编号",
                "S09抽液量",
                "S09放液量",
                "S09天平读数",
            ],
            "add_liquid": [
                "S09工艺选择",
                "S09参数写入完成",
                "S09工艺完成",
                "S09TIP盒工位编号",
                "S09TIP编号",
                "S09液体瓶编号",
                "S09抽液量",
                "S09放液量",
            ],
            "read_balance": ["S09天平读数稳定", "S09天平读数"],
            "initialize_liquid_bottle_remaining_volumes": [
                "S09液体瓶1剩余液量",
                "S09液体瓶2剩余液量",
                "S09液体瓶3剩余液量",
                "S09液体瓶4剩余液量",
                "S09液体瓶5剩余液量",
            ],
            "set_liquid_bottle_remaining_volume": [
                "S09液体瓶1剩余液量",
                "S09液体瓶2剩余液量",
                "S09液体瓶3剩余液量",
                "S09液体瓶4剩余液量",
                "S09液体瓶5剩余液量",
            ],
            "get_pipetting_status": [
                "S09工艺完成",
                "工站状态[8]",
                "S09天平读数稳定",
                "S09天平读数",
                "S09液体瓶1剩余液量",
                "S09液体瓶2剩余液量",
                "S09液体瓶3剩余液量",
                "S09液体瓶4剩余液量",
                "S09液体瓶5剩余液量",
            ],
        },
    },
}
with open(runtime_file, "w", encoding="utf-8") as f:
    json.dump(runtime_config, f, ensure_ascii=False, indent=2)
PY
}

write_graph() {
  local graph_file="$1"
  "$PYTHON" - "$graph_file" "$OPCUA_URL" "$CSV_PATH" <<'PY'
import csv
import json
import sys

graph_file, opcua_url, csv_path = sys.argv[1:4]


def load_opcua_node_id_map(path):
    node_id_map = {}
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "gb18030", "gbk"):
        for delimiter in (",", "\t"):
            try:
                with open(path, newline="", encoding=encoding) as csv_file:
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
                "opcua_node_id_map": opcua_node_id_map,
            },
            "data": {},
        },
        {
            "id": "szlab_mixer_pipetting_station",
            "name": "szlab_mixer_pipetting_station",
            "children": [],
            "parent": None,
            "type": "device",
            "class": "szlab_mixer_pipetting_station",
            "position": {"x": 420, "y": 0, "z": 0},
            "config": {
                "plc_device_id": "szlab_poly_plc",
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
    "name": f"s09_pipetting_cli_{action}",
    "nodes": [
        {
            "uuid": f"s09-pipetting-cli-{action}",
            "name": f"auto-{action}",
            "device_name": "szlab_mixer_pipetting_station",
            "param": json.loads(params_json),
        }
    ],
    "edges": [],
}
with open(workflow_file, "w", encoding="utf-8") as f:
    json.dump(workflow, f, ensure_ascii=False, indent=2)
PY
}

cases_for_action() {
  local action_group="$1"
  "$PYTHON" - "$action_group" "$HOME_POSITIONS" "$PROCESSES" "$TIP_BOX_INDEX" "$RELEASE_TIP_BOX_INDEX" "$TIP_INDEX" \
    "$LIQUID_BOTTLE_INDEX" "$STATION" "$ASPIRATE_VOLUME" "$DISPENSE_VOLUME" "$REMAINING_VOLUME" \
    "$VOLUME_UNIT" "$REQUIRE_ALLOW" "$RESET_DELAY" "$SAMPLE_ID" <<'PY'
import json
import sys

(
    action_group,
    home_positions_text,
    processes_text,
    tip_box_index,
    release_tip_box_index,
    tip_index,
    liquid_bottle_index,
    station,
    aspirate_volume,
    dispense_volume,
    remaining_volume,
    volume_unit,
    require_allow,
    reset_delay,
    sample_id,
) = sys.argv[1:16]


def bool_value(text):
    return str(text).lower() in {"1", "true", "yes", "y"}


def emit(action, params, label):
    print("\t".join([action, json.dumps(params, ensure_ascii=False, separators=(",", ":")), label]))


def emit_run_process(process, label, process_tip_box_index=None):
    params = {
        **common,
        "tip_box_index": int(process_tip_box_index or tip_box_index),
        "process": process,
        "aspirate_volume": float(aspirate_volume) if process in {7, 9} else 0,
        "dispense_volume": float(dispense_volume) if process in {8, 10} else 0,
        "volume_unit": volume_unit,
        "require_allow": bool_value(require_allow),
        "reset_delay": float(reset_delay),
    }
    emit("run_process", params, label)


common = {
    "tip_box_index": int(tip_box_index),
    "tip_index": int(tip_index),
    "liquid_bottle_index": int(liquid_bottle_index),
    "station": int(station),
}
add_liquid_common = {
    "take_tip_box_index": int(tip_box_index),
    "release_tip_box_index": int(release_tip_box_index),
    "tip_index": int(tip_index),
    "liquid_bottle_index": int(liquid_bottle_index),
    "station": int(station),
}
volume_params = {
    "aspirate_volume": float(aspirate_volume),
    "dispense_volume": float(dispense_volume),
    "volume_unit": volume_unit,
}

if action_group == "go-home":
    action_group = "safe"

if action_group == "status":
    emit("get_pipetting_status", {}, "读取 S09 状态")
elif action_group == "prepare":
    emit("prepare_liquid_station", {}, "确认 S09 唯一加液工位空闲")
elif action_group == "allow":
    emit("read_allow_process", {}, "读取 S09允许加工（允许参数写入）信号")
elif action_group == "home":
    emit("read_home_positions", {}, "读取 S09 四个安全位原点信号")
elif action_group == "safe":
    for home in [int(item) for item in home_positions_text.replace(",", " ").split()]:
        emit("go_to_safe_position", {"home_position": home}, f"执行去安全位 {home}")
elif action_group == "init-volume":
    emit("initialize_liquid_bottle_remaining_volumes", {"remaining_volume": float(remaining_volume)}, "初始化 1-5 号液体瓶剩余液量")
elif action_group == "set-volume":
    emit(
        "set_liquid_bottle_remaining_volume",
        {"bottle": int(liquid_bottle_index), "remaining_volume": float(remaining_volume)},
        f"写入液体瓶 {liquid_bottle_index} 剩余液量",
    )
elif action_group == "balance":
    emit("read_balance", {"require_stable": False}, "读取 S09 天平读数")
elif action_group == "process":
    for process in [int(item) for item in processes_text.replace(",", " ").split()]:
        emit_run_process(process, f"执行 S09 工艺 {process}")
elif action_group == "take-tip":
    emit_run_process(5, "执行 S09 工艺 5：取 TIP")
elif action_group == "take-liquid":
    emit_run_process(7, "执行 S09 工艺 7：液体瓶取液")
elif action_group == "dispense":
    emit_run_process(8, "执行 S09 工艺 8：烧杯放液")
elif action_group == "release-tip":
    emit_run_process(6, "执行 S09 工艺 6：放 TIP", release_tip_box_index)
elif action_group == "liquid-steps":
    emit_run_process(5, "执行 S09 工艺 5：取 TIP")
    emit_run_process(7, "执行 S09 工艺 7：液体瓶取液")
    emit_run_process(8, "执行 S09 工艺 8：烧杯放液")
    emit_run_process(6, "执行 S09 工艺 6：放 TIP", release_tip_box_index)
elif action_group == "add-liquid":
    emit("add_liquid", {**add_liquid_common, **volume_params}, "执行 S09 单次业务加液 5->7->8->6")
elif action_group == "workflow":
    emit(
        "run_liquid_workflow",
        {
            "sample_id": sample_id,
            "liquid_steps": [{**add_liquid_common, **volume_params}],
            "release_after": True,
        },
        "执行 S09 run_liquid_workflow 单步样例",
    )
else:
    print(f"未知 action: {action_group}", file=sys.stderr)
    raise SystemExit(2)
PY
}

run_case() {
  local tmp_dir="$1"
  local index="$2"
  local action="$3"
  local params_json="$4"
  local label="$5"

  local workflow_file="$tmp_dir/${index}_${action}.json"
  local log_file="$LOG_DIR/${index}_${action}_$(date +%Y%m%d_%H%M%S).log"

  write_workflow "$workflow_file" "$action" "$params_json"
  echo "[$index] $label"
  echo "    action: $action"
  echo "    params: $params_json"
  echo "    workflow: $workflow_file"
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
    --runtime-config "$tmp_dir/s09_runtime_config.json" \
    --graph "$tmp_dir/s09_pipetting_graph.json" \
    --workflow "$workflow_file" \
    --url "$OPCUA_URL" \
    --csv "$CSV_PATH" \
    --no-subscription \
    "${ignore_token_time_drift_args[@]}" \
    "${clear_pc_to_plc_args[@]}" \
    --log-file "$log_file"
}

confirm_real_run() {
  local action_group="$1"
  local count="$2"
  if [[ "$DRY_RUN" == "1" || "$CONFIRM" == "YES" ]]; then
    return 0
  fi
  cat <<EOF
将真实执行 S09 移液站 $action_group 的 $count 个测试用例。
OPC UA: $OPCUA_URL
CSV: $CSV_PATH
IGNORE_OPCUA_TOKEN_TIME_DRIFT: $IGNORE_OPCUA_TOKEN_TIME_DRIFT
CLEAR_PC_TO_PLC_BEFORE_RUN: $CLEAR_PC_TO_PLC_BEFORE_RUN

请确认现场安全、移液站路径无障碍、TIP/液体瓶/烧杯状态与本次动作匹配。
EOF
  read -r -p "确认继续? 输入 YES: " answer
  if [[ "$answer" != "YES" ]]; then
    echo "已取消"
    exit 0
  fi
}

run_action_group() {
  local action_group="$1"
  ensure_files
  mkdir -p "$LOG_DIR"

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  local cases_file="$tmp_dir/${action_group}_cases.tsv"
  write_runtime_config "$tmp_dir/s09_runtime_config.json"
  write_graph "$tmp_dir/s09_pipetting_graph.json"
  cases_for_action "$action_group" > "$cases_file"

  local count
  count="$(wc -l < "$cases_file" | tr -d ' ')"
  if [[ "$count" == "0" ]]; then
    echo "$action_group 没有生成任何测试用例" >&2
    exit 1
  fi

  echo "S09 action group: $action_group"
  echo "测试用例数: $count"
  echo "DRY_RUN: $DRY_RUN"
  confirm_real_run "$action_group" "$count"

  local index=0
  local failures=0
  local action params_json label
  while IFS=$'\t' read -r action params_json label; do
    index=$((index + 1))
    if ! run_case "$tmp_dir" "$index" "$action" "$params_json" "$label"; then
      failures=$((failures + 1))
      echo "用例失败: $label" >&2
      if [[ "$CONTINUE_ON_ERROR" != "1" ]]; then
        exit 1
      fi
    fi
  done < "$cases_file"

  if [[ "$failures" != "0" ]]; then
    echo "$action_group 完成，但有 $failures/$count 个用例失败" >&2
    rm -rf "$tmp_dir"
    exit 1
  fi
  rm -rf "$tmp_dir"
  echo "$action_group 完成，$count 个用例全部通过"
}

run_all() {
  for action_group in status prepare home init-volume set-volume balance process add-liquid workflow; do
    run_action_group "$action_group"
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
      run_action_group "$2"
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
