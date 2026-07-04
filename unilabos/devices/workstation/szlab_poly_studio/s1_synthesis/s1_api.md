# S1 前端接口整理

来源：`http://192.168.43.141:8055/#/homepage` 当前前端静态包。

## 全局请求约定

- 前端打包方式：Vite 单 bundle，入口 JS 为 `/assets/index-25f0a184.js`
- axios 默认 baseURL：`/`
- 默认超时：`300000 ms`
- 默认 `Content-Type`：`application/json`
- 鉴权：从 `localStorage.Authorization` 读取 token，并以 `Authorization: Bearer <token>` 发送
- token 刷新：响应头 `new-authorization` 存在时会写回 `localStorage.Authorization`
- 业务成功判断：HTTP `200` 且 `data.code === "200"`
- 下载类路径包含 `/download` 时，封装只按 HTTP 状态判断
- WebSocket：`/api/v1/log`

## 前端路由

| 路由 | 说明 |
| --- | --- |
| `/` | 重定向到 `/login` |
| `/login` | 登录页 |
| `/homepage` | 首页/设备状态大屏 |
| `/prepare` | 实验准备 |
| `/prepare/materialManagement` | 物料管理 |
| `/experiment` | 实验列表/实验编辑 |
| `/equipment` | 设备相关页面 |
| `/materialCollect` | 物料收集 |
| `/pointControl` | 点动控制 |
| `/clean` | 清洗 |
| `/bigScreen` | 大屏统计 |
| `/userInfo` | 个人信息 |
| `/system/usersManagement` | 用户管理 |
| `/navigation` | 导航页 |
| `/system/rolesManagement` | 角色管理 |
| `/system/menusManagement` | 菜单管理 |

## 认证与日志

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/login` | `data.username`、`data.password` | 登录，返回 token |
| WS | `/api/v1/log` | 无 | 实时日志推送；`status === "DIALOG"` 时弹窗 |
| POST | `/api/v1/logHistory/find` | `data` 查询条件 | 查询日志历史 |

## 首页与实验实时信息

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/experimentInformation/channel?channel={channel}` | `channel`，默认 `1` | 查询单通道实验信息 |
| GET | `/api/v1/experimentInformation/Allchannel` | 无 | 查询全部通道实验信息 |
| GET | `/api/v1/bigScreen/getExperimentNum` | 无 | 查询大屏实验数量统计 |
| GET | `/api/v1/experiment/monthly-stats` | 无 | 查询月度实验统计 |

## 实验管理

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/v1/experiment/add` | `data` 实验表单 | 新增实验 |
| POST | `/api/v1/experiment/edit` | `data` 实验表单 | 编辑实验 |
| POST | `/api/v1/experiment/delete` | `data` 实验 id/列表 | 删除实验 |
| POST | `/api/v1/experiment/start` | `data` 启动参数 | 启动实验 |
| GET | `/api/v1/experiment/listReady?pageNum={pageNum}&pageSize={pageSize}` | 分页参数 | 查询待准备实验 |
| GET | `/api/v1/experiment/listQueue?pageNum={pageNum}&pageSize={pageSize}` | 分页参数 | 查询队列实验 |
| GET | `/api/v1/experiment/listDone?pageNum={pageNum}&pageSize={pageSize}&name={name}&startTime={startTime}&endTime={endTime}` | 分页、名称、时间范围 | 查询已完成实验 |
| GET | `/api/v1/experiment/changeOrder?id={id}&type={type}` | `id`、`type` | 调整实验队列顺序 |
| GET | `/api/v1/experiment/cancel?id={id}` | `id` | 取消实验 |
| GET | `/api/v1/experiment/getDEPhase?id={id}` | `id` | 查询实验 DE 阶段信息 |

## 实验准备与物料管理

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/preparation/getCurrentInfo` | 无 | 获取当前准备信息 |
| POST | `/api/v1/preparation/setInfo` | `data` 准备信息 | 设置准备信息 |
| GET | `/api/v1/material/search?nameKey={nameKey}&pageNum={pageNum}&pageSize={pageSize}` | 名称关键字、分页参数 | 查询物料列表 |
| POST | `/api/v1/material/add` | `data` 物料信息 | 新增物料 |
| POST | `/api/v1/material/edit` | `data` 物料信息 | 编辑物料 |
| POST | `/api/v1/material/delete` | `data: [id]` | 删除物料 |
| POST | `/api/v1/material/upload` | `multipart/form-data` | 上传物料文件，超时 `600000 ms` |

## 点动与手动控制

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/manualControl/findSensorList` | 无 | 查询传感器列表 |
| POST | `/api/v1/manualControl/common` | `data` 通用控制命令 | 通用手动控制 |
| POST | `/api/v1/manualControl/peeling` | `data` | 剥离/相关手动动作 |
| POST | `/api/v1/manualControl/setLimitConf` | `data` | 设置限位配置 |
| POST | `/api/v1/manualControl/gasQualityControl` | `data` | 气体质量控制 |
| GET | `/api/v1/manualControl/reset` | 无 | 手动控制复位 |
| GET | `/api/v1/manualControl/alarmReset` | 无 | 报警复位 |
| GET | `/api/v1/manualControl/stop?channel={channel}` | `channel` | 停止指定通道 |
| GET | `/api/v1/manualControl/alarm` | 无 | 查询报警状态 |
| GET | `/api/v1/manualControl/alarm?status={status}` | `status` | 按状态查询/过滤报警 |

## 清洗与补液

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/wash/get` | 无 | 获取清洗配置 |
| POST | `/api/v1/wash/set` | `data` 清洗配置 | 保存清洗配置 |
| POST | `/api/v1/wash/oneClickWash` | `data` | 一键清洗 |
| POST | `/api/v1/wash/manual` | `data` | 手动清洗 |
| GET | `/api/v1/wash/washStatus` | 无 | 查询清洗状态 |
| GET | `/api/v1/wash/getInject` | 无 | 获取注入/进样相关清洗参数 |
| GET | `/api/v1/wash/getPump` | 无 | 获取泵相关清洗参数 |
| POST | `/api/v1/fill/start` | `data` | 开始补液 |
| GET | `/api/v1/fill/status` | 无 | 查询补液状态 |

## 收集与打印

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/collection/find?channel={channel}` | `channel`，默认 `4` | 查询通道收集信息 |
| GET | `/api/v1/collection/findTrayStatus` | 无 | 查询托盘状态 |
| POST | `/api/v1/collection/print` | `data` | 打印收集标签/信息 |
| POST | `/api/v1/collection/clear` | `data` | 清空收集信息 |

## 系统配置

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/sysConf/get` | 无 | 获取系统配置 |
| POST | `/api/v1/sysConf/set` | `data` 系统配置 | 保存系统配置 |
| GET | `/api/v1/coefficient/findAll` | 无 | 查询全部系数配置 |
| POST | `/api/v1/coefficient/update` | `data` 系数配置 | 更新系数配置 |
| GET | `/api/v1/autoConf/findAll` | 无 | 查询自动配置 |
| POST | `/api/v1/autoConf/update` | `data` 自动配置 | 更新自动配置 |
| GET | `/api/v1/channelConf/get` | 无 | 获取通道配置 |
| POST | `/api/v1/channelConf/set` | `data` 通道配置 | 保存通道配置 |

## 用户、角色与菜单

| 方法 | 接口 | 参数 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/user/info?userName={userName}` | `userName` | 查询用户信息 |
| GET | `/api/v1/user/getUsersList?nickname={nickname}&pageNum={pageNum}&pageSize={pageSize}` | 昵称、分页参数 | 查询用户列表 |
| POST | `/api/v1/user/addUser` | `data` 用户信息 | 新增用户 |
| POST | `/api/v1/user/editUser` | `data` 用户信息 | 编辑用户 |
| POST | `/api/v1/user/deleteUser` | `data` 用户 id/列表 | 删除用户 |
| POST | `/api/v1/user/resetPassword` | `data.userId`、`data.password` | 重置/修改密码 |
| GET | `/api/v1/user/getRoleIdByUid?userId={userId}` | `userId` | 查询用户角色 |
| GET | `/api/v1/user/getSysRoles` | 无 | 查询系统角色 |
| GET | `/api/v1/user/getSysMenuByRoleId?roleId={roleId}` | `roleId` | 查询角色菜单 |
| GET | `/api/v1/user/getAllSysMenu` | 无 | 查询全部系统菜单 |
| POST | `/api/v1/user/saveRoleMenus` | `data` 角色菜单配置 | 保存角色菜单 |

## 接口分类汇总

| 前缀 | 去重接口数 | 模块 |
| --- | ---: | --- |
| `/api/v1/auth/*` | 1 | 认证 |
| `/api/v1/log*` | 2 | 实时/历史日志 |
| `/api/v1/experimentInformation/*` | 2 | 实时实验信息 |
| `/api/v1/bigScreen/*` | 1 | 大屏 |
| `/api/v1/experiment/*` | 11 | 实验管理 |
| `/api/v1/preparation/*` | 2 | 实验准备 |
| `/api/v1/material/*` | 5 | 物料管理 |
| `/api/v1/manualControl/*` | 10 | 点动与手动控制 |
| `/api/v1/wash/*` | 7 | 清洗 |
| `/api/v1/fill/*` | 2 | 补液 |
| `/api/v1/collection/*` | 4 | 收集与打印 |
| `/api/v1/sysConf/*` | 2 | 系统配置 |
| `/api/v1/coefficient/*` | 2 | 系数配置 |
| `/api/v1/autoConf/*` | 2 | 自动配置 |
| `/api/v1/channelConf/*` | 2 | 通道配置 |
| `/api/v1/user/*` | 11 | 用户、角色、菜单 |

合计：文档列出接口 `66` 个，其中 HTTP 接口 `65` 个，WebSocket 接口 `1` 个。

## 流程分析

S1 可以按“PAUL-S1 外部对接流程”和“当前前端完整业务流程”两层理解。Apipost 旧文档里的 PAUL-S1 流程更精简，适合外部系统按步骤对接；当前前端流程更完整，覆盖物料管理、实验队列、清洗、收集、系统配置、用户角色等后台能力。

### PAUL-S1 外部对接流程

1. 登录获取 token。
   - `POST /api/v1/auth/login`
   - 返回 token 后，后续请求通过 `Authorization: Bearer <token>` 访问。

2. 准备物料。
   - `GET /api/v1/material/search?nameKey={nameKey}&pageNum={pageNum}&pageSize={pageSize}`
   - `POST /api/v1/material/add`
   - 先查询物料库，缺少物料时再新增。

3. 设置设备物料。
   - `POST /api/v1/preparation/setInfo`
   - 将物料绑定到设备准备信息中，供后续实验使用。

4. 新建实验。
   - Apipost 旧文档记录：`POST /api/v1/experiment/autoAdd`
   - 当前前端实际调用：`POST /api/v1/experiment/add`
   - 实测空 body 下两个路径都能进入后端但返回 `500`，说明需要合法实验 payload；当前对接优先按前端的 `experiment/add` 处理。

5. 执行实验。
   - `POST /api/v1/experiment/start` (TODO: 有继续， check一下)
   - 这是会触发真实设备动作的接口，联调时应先使用 mock 或测试环境。

6. 查询实验信息和阶段。
   - Apipost 旧文档记录：`GET /api/v1/experiment/{id}`
   - 当前设备实测该路径返回 `404`
   - 当前前端可用：`GET /api/v1/experiment/getDEPhase?id={id}`
   - 首页状态也可通过 `GET /api/v1/experimentInformation/channel?channel={channel}` 和 `GET /api/v1/experimentInformation/Allchannel` 获取。

7. 停止实验或通道。
   - Apipost 旧文档记录：`POST /api/v1/manualControl/stop?channel={channel}`
   - 当前设备实测 POST 返回 `405 Method Not Allowed`
   - 当前前端实际调用：`GET /api/v1/manualControl/stop?channel={channel}`
   - 这是动作类接口，mock 文档中默认拒绝执行。

### 当前前端完整业务流程

1. 登录进入系统。
   - 路由：`/login` -> `/homepage`
   - 接口：`POST /api/v1/auth/login`
   - 前端保存 token，并初始化日志 WebSocket：`WS /api/v1/log`。

2. 首页监控设备状态。
   - 路由：`/homepage`
   - 接口：`GET /api/v1/experimentInformation/channel?channel={channel}`、`GET /api/v1/experimentInformation/Allchannel`
   - 展示当前实验、阶段状态、反应时间、预计剩余时间、排队数量、累计完成数量和实时日志。

3. 物料准备。
   - 路由：`/prepare`、`/prepare/materialManagement`
   - 接口：`material/search`、`material/add`、`material/edit`、`material/delete`、`material/upload`
   - 先维护物料库，再通过 `preparation/setInfo` 设置本次设备物料。

4. 实验创建和排队。
   - 路由：`/experiment`
   - 接口：`experiment/add`、`experiment/edit`、`experiment/listReady`、`experiment/listQueue`、`experiment/listDone`
   - 实验创建后进入待准备或队列，可调整顺序、取消或删除。

5. 实验启动和运行监控。
   - 启动接口：`POST /api/v1/experiment/start`
   - 阶段查询：`GET /api/v1/experiment/getDEPhase?id={id}`
   - 日志：`WS /api/v1/log`、`POST /api/v1/logHistory/find`
   - 启动属于真实设备动作，应在确认物料、队列和设备状态后执行。

6. 运行中的辅助操作。
   - 点动控制：`/pointControl` 下的 `manualControl/*`
   - 清洗补液：`/clean` 下的 `wash/*`、`fill/*`
   - 收集打印：`collection/*`
   - 这些接口中多数会影响设备或现场数据，mock 示例中按安全策略默认拒绝执行。

7. 系统和权限维护。
   - 系统配置：`sysConf/*`、`coefficient/*`、`autoConf/*`、`channelConf/*`
   - 用户角色菜单：`user/*`
   - 这部分主要服务后台管理，不是 PAUL-S1 外部实验执行链路的必需步骤。

## Mock Response 示例

说明：以下响应只用于联调和文档示例。涉及启动、停止、清洗、点动、报警复位、补液等可能操作真实设备的接口，mock 默认返回拒绝执行，避免误触发硬件动作。

### 通用响应格式

成功响应：

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {}
}
```

参数错误：

```json
{
  "code": "400",
  "desc": "Invalid request parameter.",
  "data": null
}
```

未授权：

```json
{
  "code": "401",
  "desc": "Unauthorized.",
  "data": null
}
```

安全拒绝执行，适用于动作类接口：

```json
{
  "code": "403",
  "desc": "Mock mode: command rejected to avoid operating real hardware.",
  "data": null
}
```

### Mock - 认证与日志

`POST /api/v1/auth/login`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": "mock.jwt.token"
}
```

`POST /api/v1/logHistory/find`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "records": [
      {
        "date": "2026-06-11 17:30:00",
        "status": "INFO",
        "message": "Mock log: system entered standby mode."
      },
      {
        "date": "2026-06-11 17:31:00",
        "status": "WARN",
        "message": "Mock log: reagent level is below warning threshold."
      }
    ],
    "total": 2
  }
}
```

`WS /api/v1/log`

```json
{
  "date": "2026-06-11 17:30:00",
  "status": "INFO",
  "message": "Mock websocket log message.",
  "data": {}
}
```

弹窗类 WebSocket 消息：

```json
{
  "date": "2026-06-11 17:30:00",
  "status": "DIALOG",
  "message": "Mock dialog message: please confirm the next step.",
  "data": {
    "type": "manual_confirm",
    "confirmId": "mock-confirm-001"
  }
}
```

### Mock - 首页与实验实时信息

适用接口：

- `GET /api/v1/experimentInformation/channel?channel={channel}`
- `GET /api/v1/experimentInformation/Allchannel`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "experimentName": "Mock Experiment",
    "channel": 1,
    "process": "WAITING",
    "reactionTime": 12,
    "completeTimeLeft": 48,
    "queueNum": 3,
    "completeNum": 18,
    "reactionTemperature": 30.0,
    "backPressure": 3.0,
    "reactionSpeed": 0.5,
    "reactionClean": "OFF"
  }
}
```

`GET /api/v1/bigScreen/getExperimentNum`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "total": 128,
    "running": 1,
    "waiting": 3,
    "completed": 124,
    "failed": 0
  }
}
```

`GET /api/v1/experiment/monthly-stats`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": [
    {
      "month": "2026-06",
      "count": 24
    },
    {
      "month": "2026-05",
      "count": 31
    }
  ]
}
```

### Mock - 实验管理

实验列表接口：

- `GET /api/v1/experiment/listReady?pageNum={pageNum}&pageSize={pageSize}`
- `GET /api/v1/experiment/listQueue?pageNum={pageNum}&pageSize={pageSize}`
- `GET /api/v1/experiment/listDone?pageNum={pageNum}&pageSize={pageSize}&name={name}&startTime={startTime}&endTime={endTime}`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "records": [
      {
        "id": 2804265,
        "name": "Mock Experiment 001",
        "experimenter": "SSAgent",
        "status": "READY",
        "createdAt": "2026-06-11 17:30:00"
      }
    ],
    "total": 1,
    "pageNum": 1,
    "pageSize": 10
  }
}
```

新增、编辑、删除、调整顺序、取消实验接口：

- `POST /api/v1/experiment/add`
- `POST /api/v1/experiment/edit`
- `POST /api/v1/experiment/delete`
- `GET /api/v1/experiment/changeOrder?id={id}&type={type}`
- `GET /api/v1/experiment/cancel?id={id}`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "id": 2804265,
    "affected": 1
  }
}
```

`GET /api/v1/experiment/getDEPhase?id={id}`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "id": 2804265,
    "currentPhase": "REACTION_COLLECTION",
    "phases": [
      {
        "name": "MIXED_CLEANING",
        "status": "DONE"
      },
      {
        "name": "MIXED_INGREDIENTS",
        "status": "DONE"
      },
      {
        "name": "REACTION_COLLECTION",
        "status": "RUNNING"
      }
    ]
  }
}
```

`POST /api/v1/experiment/start`

```json
{
  "code": "403",
  "desc": "Mock mode: experiment start rejected to avoid operating real hardware.",
  "data": {
    "requestedExperimentIds": [
      2804265
    ]
  }
}
```

### Mock - 实验准备与物料管理

`GET /api/v1/preparation/getCurrentInfo`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "materials": [
      {
        "id": 1,
        "materialId": 54331,
        "name": "Mock Material A",
        "note": "Mock reagent for integration test"
      }
    ],
    "updatedAt": "2026-06-11 17:30:00"
  }
}
```

`POST /api/v1/preparation/setInfo`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "saved": true,
    "materialCount": 1
  }
}
```

`GET /api/v1/material/search?nameKey={nameKey}&pageNum={pageNum}&pageSize={pageSize}`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "records": [
      {
        "id": 54331,
        "name": "Mock Material A",
        "casNumber": "000-00-0",
        "chemicalFormula": "C1H1",
        "appearance": "clear liquid",
        "density": 1.0,
        "molarity": 1.0
      }
    ],
    "total": 1,
    "pageNum": 1,
    "pageSize": 10
  }
}
```

物料新增、编辑、删除、上传接口：

- `POST /api/v1/material/add`
- `POST /api/v1/material/edit`
- `POST /api/v1/material/delete`
- `POST /api/v1/material/upload`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "id": 54331,
    "affected": 1,
    "fileName": "mock-materials.xlsx"
  }
}
```

### Mock - 点动与手动控制

只读状态类接口：

- `GET /api/v1/manualControl/findSensorList`
- `GET /api/v1/manualControl/alarm`
- `GET /api/v1/manualControl/alarm?status={status}`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": [
    {
      "name": "reactorTemperature",
      "value": 30.1,
      "unit": "°C",
      "status": "NORMAL"
    },
    {
      "name": "backPressure",
      "value": 3.0,
      "unit": "bar",
      "status": "NORMAL"
    }
  ]
}
```

动作类接口，mock 统一拒绝执行：

- `POST /api/v1/manualControl/common`
- `POST /api/v1/manualControl/peeling`
- `POST /api/v1/manualControl/setLimitConf`
- `POST /api/v1/manualControl/gasQualityControl`
- `GET /api/v1/manualControl/reset`
- `GET /api/v1/manualControl/alarmReset`
- `GET /api/v1/manualControl/stop?channel={channel}`

```json
{
  "code": "403",
  "desc": "Mock mode: manual control command rejected to avoid operating real hardware.",
  "data": {
    "channel": 1,
    "command": "mock-command"
  }
}
```

### Mock - 清洗与补液

查询类接口：

- `GET /api/v1/wash/get`
- `GET /api/v1/wash/washStatus`
- `GET /api/v1/wash/getInject`
- `GET /api/v1/wash/getPump`
- `GET /api/v1/fill/status`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "status": "IDLE",
    "injectVolume": 1.0,
    "pumpSpeed": 0.5,
    "lastUpdatedAt": "2026-06-11 17:30:00"
  }
}
```

动作类接口，mock 统一拒绝执行：

- `POST /api/v1/wash/set`
- `POST /api/v1/wash/oneClickWash`
- `POST /api/v1/wash/manual`
- `POST /api/v1/fill/start`

```json
{
  "code": "403",
  "desc": "Mock mode: wash or fill command rejected to avoid operating real hardware.",
  "data": {
    "status": "REJECTED"
  }
}
```

### Mock - 收集与打印

查询类接口：

- `GET /api/v1/collection/find?channel={channel}`
- `GET /api/v1/collection/findTrayStatus`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "channel": 4,
    "trayStatus": "READY",
    "positions": [
      {
        "position": "A1",
        "status": "EMPTY"
      },
      {
        "position": "A2",
        "status": "OCCUPIED"
      }
    ]
  }
}
```

打印和清空接口，mock 拒绝执行：

- `POST /api/v1/collection/print`
- `POST /api/v1/collection/clear`

```json
{
  "code": "403",
  "desc": "Mock mode: collection print or clear rejected to avoid side effects.",
  "data": null
}
```

### Mock - 系统配置

配置查询接口：

- `GET /api/v1/sysConf/get`
- `GET /api/v1/coefficient/findAll`
- `GET /api/v1/autoConf/findAll`
- `GET /api/v1/channelConf/get`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "temperatureLimit": 80,
    "pressureLimit": 10,
    "channels": [
      {
        "channel": 1,
        "enabled": true
      },
      {
        "channel": 2,
        "enabled": true
      }
    ]
  }
}
```

配置保存接口：

- `POST /api/v1/sysConf/set`
- `POST /api/v1/coefficient/update`
- `POST /api/v1/autoConf/update`
- `POST /api/v1/channelConf/set`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "saved": true,
    "affected": 1
  }
}
```

### Mock - 用户、角色与菜单

用户与角色查询接口：

- `GET /api/v1/user/info?userName={userName}`
- `GET /api/v1/user/getUsersList?nickname={nickname}&pageNum={pageNum}&pageSize={pageSize}`
- `GET /api/v1/user/getRoleIdByUid?userId={userId}`
- `GET /api/v1/user/getSysRoles`
- `GET /api/v1/user/getSysMenuByRoleId?roleId={roleId}`
- `GET /api/v1/user/getAllSysMenu`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "userId": 1,
    "username": "mock-user",
    "nickname": "Mock User",
    "roleId": 1,
    "roles": [
      {
        "roleId": 1,
        "roleName": "管理员"
      }
    ],
    "menus": [
      {
        "menuId": 1,
        "menuName": "首页",
        "path": "/homepage",
        "parentId": 0
      }
    ]
  }
}
```

用户、密码、角色菜单写入接口：

- `POST /api/v1/user/addUser`
- `POST /api/v1/user/editUser`
- `POST /api/v1/user/deleteUser`
- `POST /api/v1/user/resetPassword`
- `POST /api/v1/user/saveRoleMenus`

```json
{
  "code": "200",
  "desc": "Succeed!",
  "data": {
    "affected": 1,
    "saved": true
  }
}
```



1. POST：tip位置
2. POST：17个板的数据(1, 不良溶剂，2。稀释剂， 3. 滴定物，4. 配液瓶子)
   1. 前四个板，配液
   2. 第6,7,8个板，大tip头(96)
   3. 第11， 12， 13， 小tip头(200ul)， 96个
   4. 比色皿：1-10
   5. 
3. POST：保存参数
4. POST: 启动复位
5. POST: 开始/停止
6. POST：暂停/继续
7. 