# Action Index — liquid_handler.prcxi

24 个动作，按功能分类。每个动作的完整 JSON Schema 在 `actions/<name>.json`。

---

## 移液操作

### `transfer_liquid`

完整移液操作：从源孔吸液并分配到目标孔，自动管理枪头

- **action_type**: `LiquidHandlerTransfer`
- **Schema**: [`actions/transfer_liquid.json`](actions/transfer_liquid.json)
- **可选参数**: `unilabos_device_id`, `asp_vols`, `dis_vols`, `sources`, `targets`, `tip_racks`, `use_channels`, `asp_flow_rates`, `dis_flow_rates`, `offsets`, `touch_tip`, `liquid_height`, `blow_out_air_volume`, `spread`, `is_96_well`, `mix_stage`, `mix_times`, `mix_vol`, `mix_rate`, `mix_liquid_height`, `delays`, `none_keys`

### `add_liquid`

向目标孔中添加液体（从试剂源到指定孔位）

- **action_type**: `LiquidHandlerAdd`
- **Schema**: [`actions/add_liquid.json`](actions/add_liquid.json)
- **可选参数**: `unilabos_device_id`, `asp_vols`, `dis_vols`, `reagent_sources`, `targets`, `use_channels`, `flow_rates`, `offsets`, `liquid_height`, `blow_out_air_volume`, `spread`, `is_96_well`, `mix_time`, `mix_vol`, `mix_rate`, `mix_liquid_height`, `none_keys`

### `remove_liquid`

从孔中移除液体到废液池

- **action_type**: `LiquidHandlerRemove`
- **Schema**: [`actions/remove_liquid.json`](actions/remove_liquid.json)
- **可选参数**: `unilabos_device_id`, `vols`, `sources`, `waste_liquid`, `use_channels`, `flow_rates`, `offsets`, `liquid_height`, `blow_out_air_volume`, `spread`, `delays`, `is_96_well`, `top`, `none_keys`

### `transfer`

简单的容器间转移操作（vessel-to-vessel）

- **action_type**: `Transfer`
- **Schema**: [`actions/transfer.json`](actions/transfer.json)
- **可选参数**: `unilabos_device_id`, `from_vessel`, `to_vessel`, `volume`, `amount`, `time`, `viscous`, `rinsing_solvent`, `rinsing_volume`, `rinsing_repeats`, `solid`

---

## 移液基础

### `aspirate`

从指定孔位吸取液体

- **action_type**: `LiquidHandlerAspirate`
- **Schema**: [`actions/aspirate.json`](actions/aspirate.json)
- **可选参数**: `unilabos_device_id`, `resources`, `vols`, `use_channels`, `flow_rates`, `offsets`, `liquid_height`, `blow_out_air_volume`, `spread`

### `dispense`

向指定孔位分配液体

- **action_type**: `LiquidHandlerDispense`
- **Schema**: [`actions/dispense.json`](actions/dispense.json)
- **可选参数**: `unilabos_device_id`, `resources`, `vols`, `use_channels`, `flow_rates`, `offsets`, `blow_out_air_volume`, `spread`

### `mix`

在孔内混合液体（反复吸吐）

- **action_type**: `LiquidHandlerMix`
- **Schema**: [`actions/mix.json`](actions/mix.json)
- **可选参数**: `unilabos_device_id`, `targets`, `mix_time`, `mix_vol`, `height_to_bottom`, `offsets`, `mix_rate`, `none_keys`

---

## 枪头管理

### `pick_up_tips`

从枪头架上拾取枪头

- **action_type**: `LiquidHandlerPickUpTips`
- **Schema**: [`actions/pick_up_tips.json`](actions/pick_up_tips.json)
- **可选参数**: `unilabos_device_id`, `tip_spots`, `use_channels`, `offsets`

### `drop_tips`

将枪头放回指定位置

- **action_type**: `LiquidHandlerDropTips`
- **Schema**: [`actions/drop_tips.json`](actions/drop_tips.json)
- **可选参数**: `unilabos_device_id`, `tip_spots`, `use_channels`, `offsets`, `allow_nonzero_volume`

### `discard_tips`

将枪头丢弃到废物槽

- **action_type**: `LiquidHandlerDiscardTips`
- **Schema**: [`actions/discard_tips.json`](actions/discard_tips.json)
- **可选参数**: `unilabos_device_id`, `use_channels`

### `set_tiprack`

配置枪头架参数

- **action_type**: `LiquidHandlerSetTipRack`
- **Schema**: [`actions/set_tiprack.json`](actions/set_tiprack.json)
- **可选参数**: `unilabos_device_id`, `tip_racks`

---

## 液体/资源设置

### `set_liquid`

更新孔内液体信息（类型、体积）

- **action_type**: `LiquidHandlerSetLiquid`
- **Schema**: [`actions/set_liquid.json`](actions/set_liquid.json)
- **可选参数**: `unilabos_device_id`, `wells`, `liquid_names`, `volumes`

### `set_liquid_from_plate`

从板定义文件中读取并设置液体信息

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/set_liquid_from_plate.json`](actions/set_liquid_from_plate.json)
- **核心参数**: `plate`, `well_names`, `liquid_names`, `volumes`
- **可选参数**: `unilabos_device_id`

### `move_plate`

移动板到新的位置

- **action_type**: `LiquidHandlerMovePlate`
- **Schema**: [`actions/move_plate.json`](actions/move_plate.json)
- **可选参数**: `unilabos_device_id`, `plate`, `to`, `intermediate_locations`, `resource_offset`, `pickup_offset`, `destination_offset`, `pickup_direction`, `drop_direction`, `get_direction`, `put_direction`, `pickup_distance_from_top`

---

## 协议自动化

### `auto-create_protocol`

自动创建实验协议

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_create_protocol.json`](actions/auto_create_protocol.json)
- **可选参数**: `unilabos_device_id`, `protocol_name`, `protocol_description`, `protocol_version`, `protocol_author`, `protocol_date`, `protocol_type`, `none_keys`

### `auto-run_protocol`

自动执行已创建的协议

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_run_protocol.json`](actions/auto_run_protocol.json)
- **可选参数**: `unilabos_device_id`

### `auto-custom_delay`

暂停执行指定时间

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_custom_delay.json`](actions/auto_custom_delay.json)
- **可选参数**: `unilabos_device_id`, `seconds`, `msg`

---

## 外设控制

### `auto-heater_action`

控制加热器温度设置

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_heater_action.json`](actions/auto_heater_action.json)
- **核心参数**: `temperature`, `time`
- **可选参数**: `unilabos_device_id`

### `auto-shaker_action`

控制振荡器设备

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_shaker_action.json`](actions/auto_shaker_action.json)
- **核心参数**: `time`, `module_no`, `amplitude`, `is_wait`
- **可选参数**: `unilabos_device_id`

---

## 移动定位

### `auto-move_to`

将移液器移动到指定孔位

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_move_to.json`](actions/auto_move_to.json)
- **核心参数**: `well`
- **可选参数**: `unilabos_device_id`, `dis_to_top`, `channel`

### `auto-touch_tip`

触碰枪头到孔壁

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_touch_tip.json`](actions/auto_touch_tip.json)
- **核心参数**: `targets`
- **可选参数**: `unilabos_device_id`

---

## 孔组操作

### `auto-set_group`

定义孔组（well group）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/auto_set_group.json`](actions/auto_set_group.json)
- **核心参数**: `group_name`, `wells`, `volumes`
- **可选参数**: `unilabos_device_id`

### `auto-transfer_group`

在孔组之间转移液体

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/auto_transfer_group.json`](actions/auto_transfer_group.json)
- **核心参数**: `source_group_name`, `target_group_name`, `unit_volume`
- **可选参数**: `unilabos_device_id`

---

## 其他

### `auto-iter_tips`

迭代枪头操作

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/auto_iter_tips.json`](actions/auto_iter_tips.json)
- **核心参数**: `tip_racks`
- **可选参数**: `unilabos_device_id`

---

## 使用示例

构建 Run Single Device Action 请求的 `param` 字段：

```json
{
  "goal": {
    "source": [["A1"]],
    "destination": [["B1"]],
    "volume": [[100]]
  }
}
```

> **WARNING: `action_type` 必须正确，传错会导致任务永远卡住无法完成。** 每个 action 条目中的 `action_type` 即为 API 调用时的必填值，也可从 `actions/<name>.json` 的 `type` 字段获取。