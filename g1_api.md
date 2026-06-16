# G1 前端接口整理

来源：`http://192.168.43.237:8099/` 当前前端静态包。

## 全局请求约定

- 前端统一 baseURL：`/api`
- 请求封装：`$post`、`$get`、`$put`、`$delete`
- 默认超时：`10000 ms`
- 默认 `Content-Type`：`application/json; charset=UTF-8`
- 默认响应类型：`json`；导出接口使用 `responseType: blob`
- 页面业务判断多数使用 `code === "0"`；封装层对 `401` 会清理 `localStorage.token` 并跳转 `/#/login`

## 前端路由

| 路由 | 说明 |
| --- | --- |
| `/` | 物料/板位配置页，与 `/pointMaterial` 共用组件 |
| `/experimentSetting` | 实验设置/板位配置 |
| `/pointSetting` | 参数设置、蜂鸣器、实验启停 |
| `/pointMaterial` | 物料/板位配置 |
| `/realTimeData` | 实时数据、历史实验数据、导出 |
| `/pointMove` | 点动控制 |

## 实验数据接口

页面：`/realTimeData`

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/experiment/getRealTimeSummaryData` | 无 | 获取全部通道实时汇总数据 |
| POST | `/api/experiment/getRealTimeExperimentData` | `params.channel` | 获取单通道实时实验数据 |
| POST | `/api/experiment/exportInfoByChannel` | `params.channel`，`responseType: blob` | 按通道导出实时数据 Excel |
| POST | `/api/experiment/exportExperimentDataByCode` | `params.experimentCode`，`responseType: blob` | 按实验编号导出历史实验 Excel |
| POST | `/api/experiment/getExperimentListByTime` | `data.startTime`、`data.endTime`、`data.keyword`、`data.pageNo`、`data.pageSize` | 按时间范围分页查询实验列表 |
| DELETE | `/api/experiment/deleteExperimentByCodeList` | `data: string[]`，实验 `code` 列表 | 删除一个或多个历史实验 |
| POST | `/api/experiment/exportZipExperimentDataByCodeList` | `data: string[]`，实验 `code` 列表，`responseType: blob` | 批量导出实验数据 ZIP |
| POST | `/api/experiment/getExperimentDataByCode` | `params.experimentCode` | 查询单个实验详情数据 |

## 物料与板位配置接口

页面：`/`、`/pointMaterial`、`/experimentSetting`、`/pointSetting`

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/materia/config/queryAllPlateConfig` | 无 | 查询全部板位/物料配置 |
| POST | `/api/materia/config/saveAll` | `data: PlateConfig[]` | 保存全部板位/物料配置 |
| GET | `/api/materia/config/import/last` | `params.plateCode` | 按板位导入上一次物料配置 |

`PlateConfig` 前端构造字段：

| 字段 | 说明 |
| --- | --- |
| `plateCode` | 板位编号，例如 `1#`、`2#` |
| `materialType` | 物料类型，例如 `bad_solvent`、`dilution_solvent`、`drop_down_material`、`mix_empty_bottle`、`tip_head`、`cuvette` |
| `configDetail.tubeNum` | 管位数量 |
| `configDetail.tubeList` | 管位列表，元素含 `number`、`name`、`enabled` |
| `configDetail.startNum` | 起始编号 |
| `configDetail.tipType` | TIP 类型 |
| `configDetail.enabled` | 是否启用，主要用于比色皿 |

## 参数设置接口

页面：`/pointSetting`，部分接口也在 `/pointMove`、`/pointMaterial` 复用。

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/params/config/getHoneyStatus` | 无 | 获取蜂鸣器屏蔽状态 |
| POST | `/api/params/config/buzzerShield` | 无 | 切换蜂鸣器屏蔽 |
| POST | `/api/params/config/mute` | 无 | 消音 |
| POST | `/api/params/config/getCurrentParamsConfig` | 无 | 获取当前实验参数配置 |
| POST | `/api/params/config/getTubeStatus` | 无 | 获取当前管位/TIP 状态 |
| POST | `/api/params/config/getLastParamsConfig` | 无 | 获取上一次实验参数配置 |
| POST | `/api/params/config/oneClickReset` | 无 | 一键复位 |
| POST | `/api/params/config/primingExp` | 无 | 开始/停止 priming 操作 |
| POST | `/api/params/config/startOrPauseExp` | 无 | 开始/暂停实验 |
| POST | `/api/params/config/save` | `data: ParamsConfig` | 保存实验参数配置 |

`ParamsConfig` 前端字段：

| 字段 | 说明 |
| --- | --- |
| `poorSolventVolume` | 单次使用不良溶剂体积，前端限制不大于 `3 ml` |
| `titrantVolume` | 单次滴定物体积 |
| `dilutionVolume` | 单次稀释剂体积 |
| `mixedTimes` | 混合次数 |
| `singleDropTimes` | 单皿滴定次数 |
| `intervalTime` | 间隔时间 |
| `dropQuantity` | 滴定数量/滴数 |

## 点动控制接口

页面：`/pointMove`

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/jogControl/arm/getState` | 无 | 获取机械臂当前状态，前端显示手动/自动 |
| POST | `/api/jogControl/arm/switchState` | 无 | 切换机械臂手动/自动状态 |
| POST | `/api/jogControl/arm/moveTo` | `params.position` | 四轴机械臂移动到指定位置 |
| POST | `/api/jogControl/arm/faultReset` | 无 | 四轴机械臂故障复位 |
| POST | `/api/jogControl/arm/start` | 无 | 四轴机械臂继续程序 |
| POST | `/api/jogControl/installTipHead` | 无 | 装 TIP 头 |
| POST | `/api/jogControl/uninstallTipHead` | 无 | 退 TIP 头 |
| POST | `/api/jogControl/electricClaw/init` | 无 | 电动夹爪初始化 |
| POST | `/api/jogControl/electricClaw/close` | 无 | 电动夹爪夹紧/关闭 |
| POST | `/api/jogControl/electricClaw/open` | 无 | 电动夹爪松开/打开 |
| GET | `/api/jogControl/magneticStirring/getSpeed` | 无 | 获取磁力搅拌实时转速 |
| POST | `/api/jogControl/magneticStirring/start` | `data.channel_1_speed` 到 `data.channel_5_speed` | 启动 1-5 通道磁力搅拌 |
| POST | `/api/jogControl/magneticStirring/stop` | 无 | 停止磁力搅拌 |
| POST | `/api/jogControl/pipette/suck` | `params.suckVolume` | 移液枪吸液 |
| POST | `/api/jogControl/pipette/dispense` | `params.dispenseVolume` | 移液枪排液 |
| POST | `/api/jogControl/pipette/empty` | 无 | 移液枪排空 |
| POST | `/api/jogControl/pipette/init` | 无 | 移液枪初始化 |
| POST | `/api/jogControl/rotateClaw/init` | 无 | 旋转夹爪初始化 |
| POST | `/api/jogControl/rotateClaw/close` | 无 | 旋转夹爪关闭 |
| POST | `/api/jogControl/rotateClaw/open` | `params.openRatio` | 旋转夹爪按张开比例打开 |
| POST | `/api/jogControl/rotateClaw/closeLid` | 无 | 旋转夹爪关盖 |
| POST | `/api/jogControl/rotateClaw/openLid` | 无 | 旋转夹爪开盖 |
| POST | `/api/jogControl/getSensorRealData` | 无 | 获取传感器实时数据 |
| POST | `/api/jogControl/titrationGroup/moveBack` | 无 | 滴定组后退 |
| POST | `/api/jogControl/titrationGroup/moveForward` | 无 | 滴定组前进 |
| POST | `/api/jogControl/titrationGroup/restore` | 无 | 滴定组复位 |

## 接口分类汇总

| 前缀 | 去重接口数 | 前端调用处数 | 模块 |
| --- | ---: | ---: | --- |
| `/api/experiment/*` | 8 | 9 | 实时/历史实验数据 |
| `/api/materia/config/*` | 3 | 5 | 物料与板位配置 |
| `/api/params/config/*` | 10 | 13 | 参数设置、复位、实验启停 |
| `/api/jogControl/*` | 26 | 26 | 点动控制 |

合计：去重接口 `47` 个，前端调用处 `53` 处。

## 流程分析

G1 前端围绕“物料/板位配置、实验参数设置、点动调试、实时数据监控、历史数据导出”展开。它不像 S1 那样包含登录、用户角色和完整实验队列管理，当前前端更像一套设备本地控制和数据查看界面。

### 设备配置流程

1. 进入首页或物料页。
   - 路由：`/`、`/pointMaterial`
   - 接口：`GET /api/materia/config/queryAllPlateConfig`
   - 前端读取全部板位配置，展示溶剂、滴加物、空瓶、TIP、比色皿等物料布局。

2. 复用上一次板位配置。
   - TIP 相关配置：`POST /api/params/config/getTubeStatus`
   - 普通板位配置：`GET /api/materia/config/import/last?plateCode={plateCode}`
   - 用于快速填充当前选中板位的物料或起始编号。

3. 保存全部物料/板位配置。
   - 接口：`POST /api/materia/config/saveAll`
   - 前端将 `PlateConfig[]` 一次性提交，包含 `plateCode`、`materialType`、`configDetail` 等字段。

### 实验参数设置流程

1. 打开参数设置页。
   - 路由：`/pointSetting`
   - 接口：`POST /api/params/config/getHoneyStatus`、`GET /api/materia/config/queryAllPlateConfig`
   - 前端展示蜂鸣器屏蔽状态和当前板位配置。

2. 查询当前实验参数和管位状态。
   - 当前参数：`POST /api/params/config/getCurrentParamsConfig`
   - 管位/TIP 状态：`POST /api/params/config/getTubeStatus`
   - 上次参数：`POST /api/params/config/getLastParamsConfig`

3. 保存实验参数。
   - 接口：`POST /api/params/config/save`
   - 前端提交 `poorSolventVolume`、`titrantVolume`、`dilutionVolume`、`mixedTimes`、`singleDropTimes`、`intervalTime`、`dropQuantity`。

4. 复位、priming、开始/暂停实验。
   - 一键复位：`POST /api/params/config/oneClickReset`
   - priming：`POST /api/params/config/primingExp`
   - 开始/暂停：`POST /api/params/config/startOrPauseExp`
   - 这些接口会影响真实设备，mock 中默认拒绝执行。

### 点动调试流程

1. 进入点动控制页。
   - 路由：`/pointMove`
   - 接口：`POST /api/jogControl/arm/getState`
   - 前端先读取机械臂自动/手动状态。

2. 切换手动状态。
   - 接口：`POST /api/jogControl/arm/switchState`
   - 多数点动动作要求处于手动模式。

3. 查询实时传感器和搅拌速度。
   - 传感器：`POST /api/jogControl/getSensorRealData`
   - 搅拌速度：`GET /api/jogControl/magneticStirring/getSpeed`
   - 前端定时刷新显示。

4. 执行点动动作。
   - 机械臂：`arm/moveTo`、`arm/faultReset`、`arm/start`
   - TIP：`installTipHead`、`uninstallTipHead`
   - 移液枪：`pipette/suck`、`pipette/dispense`、`pipette/empty`、`pipette/init`
   - 夹爪：`electricClaw/*`、`rotateClaw/*`
   - 搅拌：`magneticStirring/start`、`magneticStirring/stop`
   - 滴定组：`titrationGroup/moveBack`、`moveForward`、`restore`
   - 这些接口均属于真实设备动作，mock 中默认拒绝执行。

### 实时数据和历史数据流程

1. 进入实时数据页。
   - 路由：`/realTimeData`
   - 全部通道：`POST /api/experiment/getRealTimeSummaryData`
   - 单通道：`POST /api/experiment/getRealTimeExperimentData?channel={channel}`
   - 前端每 5 秒刷新一次数据。

2. 按时间查询历史实验。
   - 接口：`POST /api/experiment/getExperimentListByTime`
   - 参数：`startTime`、`endTime`、`keyword`、`pageNo`、`pageSize`

3. 查看历史实验详情。
   - 接口：`POST /api/experiment/getExperimentDataByCode?experimentCode={code}`
   - 前端读取单个实验曲线数据并绘图。

4. 导出或删除历史数据。
   - 导出单通道：`POST /api/experiment/exportInfoByChannel`
   - 导出单实验：`POST /api/experiment/exportExperimentDataByCode`
   - 批量导出：`POST /api/experiment/exportZipExperimentDataByCodeList`
   - 删除：`DELETE /api/experiment/deleteExperimentByCodeList`
   - 删除会改变历史数据，mock 中建议返回拒绝或仅返回演示成功。

## Mock Response 示例

说明：以下响应只用于联调和文档示例。G1 前端业务成功通常判断 `code === "0"`。涉及复位、实验启停、点动、搅拌、移液、夹爪、滴定组、删除历史数据等可能操作真实设备或改变数据的接口，mock 默认返回拒绝执行。

### 通用响应格式

成功响应：

```json
{
  "code": "0",
  "message": "成功",
  "result": {}
}
```

参数错误：

```json
{
  "code": "400",
  "message": "Invalid request parameter.",
  "result": null
}
```

安全拒绝执行，适用于动作类接口：

```json
{
  "code": "403",
  "message": "Mock mode: command rejected to avoid operating real hardware.",
  "result": null
}
```

### Mock - 物料与板位配置

`GET /api/materia/config/queryAllPlateConfig`

```json
{
  "code": "0",
  "message": "成功",
  "result": [
    {
      "plateCode": "1#",
      "materialType": "bad_solvent",
      "configDetail": {
        "tubeNum": 6,
        "tubeList": [
          {
            "number": "1",
            "name": "Mock Bad Solvent",
            "enabled": true
          }
        ]
      }
    },
    {
      "plateCode": "6#",
      "materialType": "tip_head",
      "configDetail": {
        "tipType": "big",
        "startNum": 1
      }
    }
  ]
}
```

`GET /api/materia/config/import/last?plateCode={plateCode}`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "plateCode": "1#",
    "materialType": "bad_solvent",
    "configDetail": {
      "tubeNum": 6,
      "tubeList": [
        {
          "number": "1",
          "name": "Mock Last Material",
          "enabled": true
        }
      ]
    }
  }
}
```

`POST /api/materia/config/saveAll`

```json
{
  "code": "0",
  "message": "配置成功！",
  "result": {
    "saved": true,
    "affected": 2
  }
}
```

### Mock - 参数设置

查询类接口：

- `POST /api/params/config/getHoneyStatus`
- `POST /api/params/config/getCurrentParamsConfig`
- `POST /api/params/config/getTubeStatus`
- `POST /api/params/config/getLastParamsConfig`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "poorSolventVolume": 1.0,
    "titrantVolume": 0.1,
    "dilutionVolume": 0.5,
    "mixedTimes": 3,
    "singleDropTimes": 5,
    "intervalTime": 10,
    "dropQuantity": 8,
    "currentCavityNum": 1,
    "bigTipHeadPlateCode": "6#",
    "bigTipHeadStartNum": 1,
    "smallTipHeadPlateCode": "11#",
    "smallTipHeadStartNum": 1
  }
}
```

保存和蜂鸣器类接口：

- `POST /api/params/config/save`
- `POST /api/params/config/buzzerShield`
- `POST /api/params/config/mute`

```json
{
  "code": "0",
  "message": "设置成功！",
  "result": {
    "saved": true
  }
}
```

动作类接口，mock 统一拒绝执行：

- `POST /api/params/config/oneClickReset`
- `POST /api/params/config/primingExp`
- `POST /api/params/config/startOrPauseExp`

```json
{
  "code": "403",
  "message": "Mock mode: params command rejected to avoid operating real hardware.",
  "result": null
}
```

### Mock - 点动控制

状态查询接口：

- `POST /api/jogControl/arm/getState`
- `GET /api/jogControl/magneticStirring/getSpeed`
- `POST /api/jogControl/getSensorRealData`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "armState": 1,
    "speed": [
      300,
      300,
      300,
      300,
      300
    ],
    "surface": [
      {
        "channel": 1,
        "value": 0.82
      },
      {
        "channel": 2,
        "value": 0.79
      }
    ]
  }
}
```

动作类接口，mock 统一拒绝执行：

- `POST /api/jogControl/arm/switchState`
- `POST /api/jogControl/arm/moveTo`
- `POST /api/jogControl/arm/faultReset`
- `POST /api/jogControl/arm/start`
- `POST /api/jogControl/installTipHead`
- `POST /api/jogControl/uninstallTipHead`
- `POST /api/jogControl/electricClaw/init`
- `POST /api/jogControl/electricClaw/close`
- `POST /api/jogControl/electricClaw/open`
- `POST /api/jogControl/magneticStirring/start`
- `POST /api/jogControl/magneticStirring/stop`
- `POST /api/jogControl/pipette/suck`
- `POST /api/jogControl/pipette/dispense`
- `POST /api/jogControl/pipette/empty`
- `POST /api/jogControl/pipette/init`
- `POST /api/jogControl/rotateClaw/init`
- `POST /api/jogControl/rotateClaw/close`
- `POST /api/jogControl/rotateClaw/open`
- `POST /api/jogControl/rotateClaw/closeLid`
- `POST /api/jogControl/rotateClaw/openLid`
- `POST /api/jogControl/titrationGroup/moveBack`
- `POST /api/jogControl/titrationGroup/moveForward`
- `POST /api/jogControl/titrationGroup/restore`

```json
{
  "code": "403",
  "message": "Mock mode: jog control command rejected to avoid operating real hardware.",
  "result": {
    "status": "REJECTED"
  }
}
```

### Mock - 实时与历史实验数据

`POST /api/experiment/getRealTimeSummaryData`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "xAxis": [
      "17:30:00",
      "17:30:05",
      "17:30:10"
    ],
    "channelData": [
      {
        "channel": 1,
        "yAxis": [
          0.82,
          0.84,
          0.85
        ]
      },
      {
        "channel": 2,
        "yAxis": [
          0.77,
          0.78,
          0.8
        ]
      }
    ]
  }
}
```

`POST /api/experiment/getRealTimeExperimentData?channel={channel}`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "experiment": {
      "name": "Mock Channel 1",
      "title": "通道一实时数据"
    },
    "surface_avg": {
      "xAxis": [
        "17:30:00",
        "17:30:05"
      ],
      "yAxis": [
        0.82,
        0.84
      ]
    }
  }
}
```

`POST /api/experiment/getExperimentListByTime`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "content": [
      {
        "code": "mock-exp-001",
        "name": "Mock Historical Experiment",
        "title": "Mock Historical Experiment",
        "createdAt": "2026-06-11 17:30:00"
      }
    ],
    "totalElements": 1
  }
}
```

`POST /api/experiment/getExperimentDataByCode?experimentCode={code}`

```json
{
  "code": "0",
  "message": "成功",
  "result": {
    "experiment": {
      "code": "mock-exp-001",
      "name": "Mock Historical Experiment",
      "title": "历史实验详情"
    },
    "surface_avg": {
      "xAxis": [
        "0",
        "1",
        "2"
      ],
      "yAxis": [
        0.8,
        0.83,
        0.86
      ]
    }
  }
}
```

导出类接口：

- `POST /api/experiment/exportInfoByChannel`
- `POST /api/experiment/exportExperimentDataByCode`
- `POST /api/experiment/exportZipExperimentDataByCodeList`

导出接口真实响应是 `blob`。mock 可以返回空文件或如下 JSON 错误，避免前端误下载真实数据：

```json
{
  "code": "403",
  "message": "Mock mode: export rejected; use a generated fixture file instead.",
  "result": null
}
```

删除接口：

- `DELETE /api/experiment/deleteExperimentByCodeList`

```json
{
  "code": "403",
  "message": "Mock mode: delete rejected to avoid modifying historical data.",
  "result": null
}
```
