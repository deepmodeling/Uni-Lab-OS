# SZLab Robot PLC Contract

## Confirmed Robot Handshake

Every robot pick/place action uses the updated upper-computer PLC handshake:

1. Run the station sensor gate. Pick requires the source sensor to be `True`; place requires the target sensor to be `False`.
2. Read `Robot_Home` and require `True` before writing any robot task parameters.
3. Read `Robot_任务允许写入` and require `True` before writing any robot task parameters.
4. Write all station task parameters as integers. The updated PLC contract exposes these Robot PC->PLC parameters as `DINT`.
5. Write `任务号` with the action task number.
6. Write `Robot_任务写入完成=False`, then `Robot_任务写入完成=True`.
7. Wait for `Robot_任务完成` to become non-zero. Although the CSV type is `DINT`, non-zero is the confirmed completion/true condition.
8. Write `Robot_任务写入完成=False`.
9. Reset every written station parameter and `任务号` to `0`.

`Robot_Home`, `Robot_任务允许写入`, and `Robot_任务完成` are PLC->PC variables and must not be written by Uni-LabOS. `Robot_任务写入完成`, `任务号`, and the Sxx task parameters are PC->PLC variables.

## PC->PLC Reset Rule

Normal robot actions reset PC->PLC variables to `0` after task completion or after a failed/timeout task submission. PLC->PC variables are read-only from Uni-LabOS and must be reset by PLC logic.

Field CLI debugging uses a different observation-friendly rule:

- Before each real CLI case, `CLEAR_PC_TO_PLC_BEFORE_RUN=1` clears known PC->PLC variables.
- After a successful CLI case, `SKIP_RESET_AFTER_RUN=1` keeps written task parameters visible, but still resets `Robot_任务写入完成=False`.
- If writing fails partway through a task submission, Uni-LabOS rolls back `Robot_任务写入完成=False` and the variables that were already written.

## OPC UA NodeId Mapping

The real upper-computer OPC UA server exposes variables with string NodeIds in this format:

`ns=4;s=上位机通讯|<变量名>`

Examples:

- `传感器状态_上位机[2].NO[10]` -> `ns=4;s=上位机通讯|传感器状态_上位机[2].NO[10]`
- `S03取放料产品` -> `ns=4;s=上位机通讯|S03取放料产品`
- `任务号` -> `ns=4;s=上位机通讯|任务号`
- `Robot_任务写入完成` -> `ns=4;s=上位机通讯|Robot_任务写入完成`

`cli_robot_test.sh` generates `opcua_node_id_map` from the PLC CSV `变量名` column and passes it to `SZLabPolyPLCDevice`, avoiding recursive BrowseName matching issues for array-like sensor names.

## Robot Task Variables

| Station | Action | Task Number | PC->PLC Variables | Reset Values |
| --- | ---: | ---: | --- | --- |
| S01 | pick product | 1 | `S01出入料产品`, `S01取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S01 | pick position | 2 | `S01取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S02 | place | 3 | `S02取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S02 | pick | 4 | `S02取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S03 | place | 5 | `S03取放料产品`, `S03取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S03 | pick | 6 | `S03取放料产品`, `S03取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S04 | place | 7 | `S04取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S04 | pick | 8 | `S04取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S05 | place | 9 | `任务号` | `任务号=0` |
| S05 | pick | 10 | `任务号` | `任务号=0` |
| S06 | place | 11 | `任务号` | `任务号=0` |
| S06 | pick | 12 | `任务号` | `任务号=0` |
| S071 | place | 13 | `S071取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S071 | pick | 14 | `S071取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S072 | place | 15 | `S072取放料产品`, `任务号` | all written PC->PLC variables reset to `0` |
| S072 | pick | 16 | `S072取放料产品`, `任务号` | all written PC->PLC variables reset to `0` |
| S08 | place | 17 | `S08取放料产品`, `S08取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S08 | pick | 18 | `S08取放料产品`, `S08取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S08 | pour | 25 | `S08倒料产品选择`, `任务号` | all written PC->PLC variables reset to `0` |
| S09 | place | 19 | `S09取放料产品`, `S09取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S09 | pick | 20 | `S09取放料产品`, `S09取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S10 | place | 21 | `S10取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S10 | pick | 22 | `S10取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S11 | place | 23 | `S11取放料产品`, `S11取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |
| S11 | pick | 24 | `S11取放料产品`, `S11取放料编号`, `任务号` | all written PC->PLC variables reset to `0` |

## Sensor Gate Rules

Sensor values are PLC->PC. `True` means occupied; `False` means empty. Every pick action requires the source sensor to be `True`; every place action requires the target sensor to be `False`. If the gate fails, Uni-LabOS does not write action variables or `任务号`.

For PLC connectivity-only debugging, `SKIP_SENSOR_PRECHECK=1` skips these pick/place sensor gates. This is only for confirming read/write behavior with the upper computer; it can allow task writes even when source/target physical state is not valid.

Confirmed built-in mappings:

- S02 TIP positions 1-6: `传感器状态_上位机[0].NO[0]` to `[0].NO[5]`.
- S03 unused beakers/sample vials: mappings from `plc.py` `S3_UNUSED_BEAKER_SENSORS` and `S3_UNUSED_SAMPLE_VIAL_SENSORS`.
- S04 mixer positions 1-6: `传感器状态_上位机[2].NO[10]` to `[2].NO[15]`.
- S05 photo material sensor: `传感器状态_上位机[3].NO[0]`.
- S06 material sensor: `传感器状态_上位机[3].NO[1]`.
- S071 powder container sensors: `传感器状态_上位机[3].NO[8]` to `[3].NO[13]`.
- S09 TIP positions 1-2: `传感器状态_上位机[4].NO[5]` and `[4].NO[6]`.
- S09 beaker position 1: `传感器状态_上位机[4].NO[7]`.
- S10 liquid reagent sensors: mappings from `plc.py` `S10_LIQUID_REAGENT_SENSORS`.
- S11 used beakers/sample vials: mappings from `plc.py` `S11_USED_BEAKER_SENSORS` and `S11_USED_SAMPLE_VIAL_SENSORS`.

## Field CLI Debug Notes

`cli_robot_test.sh` supports scoped station tests:

- `MODE=pick|place|both` selects action direction.
- `PRODUCT_TYPES="1"` limits product type enumeration.
- `SKIP_SENSOR_PRECHECK=1` skips source/target sensor gates.
- `SKIP_ROBOT_HANDSHAKE_CHECK=1` skips `Robot_Home`, `Robot_任务允许写入`, and `Robot_任务完成` checks for PLC connectivity-only write tests.
- `SKIP_RESET_AFTER_RUN=1` keeps successful task parameter values visible after completion while still clearing `Robot_任务写入完成`.
- `CLEAR_PC_TO_PLC_BEFORE_RUN=1` still clears PC->PLC values before each case.

## Field Confirmation Items

- Confirm OPC UA Browser can read `Robot_Home`, `Robot_任务允许写入`, and `Robot_任务完成` from the updated CSV.
- Confirm OPC UA Browser can safely write `Robot_任务写入完成`, `任务号`, and one DINT station parameter.
- Confirm `Robot_任务完成` returns to `0` or otherwise starts a fresh cycle before the next task, so stale non-zero completion does not mask a failed task.
- Confirm exact source/target sensor mapping for S01, S072, S08, and S09 liquid reagent full/not-full selection.
- Confirm whether all PC->PLC task variables use `0` as the final reset value.
- S07固体加样准备 is not ready. The Feishu flowchart text is incomplete: "再从固体粉末容器瓶平台上与固体粉末容器瓶". Do not implement this preparation workflow until the missing PLC variables and complete motion text are confirmed.
