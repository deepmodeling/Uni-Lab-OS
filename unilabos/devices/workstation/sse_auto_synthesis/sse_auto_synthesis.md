# SSE auto synthesis 用户指南（UniLab 接入）

## 概述
针对“SSE auto synthesis”设备的完整流程：
参数生成与下发：将两个配方（Recipe_1、Recipe_2）与烧结参数打包为 params.json，并通过 TCP 发送到设备。
启动配方并轮询：触发配方执行，按协议长连接轮询配方状态；完成后拉取结果。
状态查询：获取马弗炉（furnace）工作状态。
启动烧结与球磨：发送相应启动命令。
接入形式：
前端以 UniLabJsonCommand 的 auto-* 动作进行交互（定义见 [sse_auto_synthesis.yaml](Uni-Lab-OS/unilabos/registry/devices/sse_auto_synthesis.yaml)）。
后端设备实现由 YAML 的 module 字段指定（类名 SSEAutoSynthesisStation）。

## 设备配置
注册文件：
[sse_auto_synthesis.yaml](Uni-Lab-OS/unilabos/registry/devices/sse_auto_synthesis.yaml)
关键字段：
  category/class：sse_auto_synthesis
  module：指向后端实现模块与类 SSEAutoSynthesisStation
  type：python
设备图（如使用可视化拓扑）：
class 需为 sse_auto_synthesis
name 建议为 SSE auto synthesis
config 含 ip/port（默认 127.0.0.1:8091）
## 初始化
默认使用设备图或配置中的 ip/port 与设备进行 TCP 通信（长连接、CRLF 结尾）。
后端提供连接检查接口（内部使用）：check_connection，可用于验证目标端口可达。
## 任务 ID 对照
| ID | 动作 | 说明 | 返回 |
| --- | --- | --- | --- |
| 1 | load_params | 下发参数 | {"request_id":1,"result":0} |
| 2 | start_recipt | 启动配方 | {"request_id":2,"result":0} 或失败码 |
| 4 | get_furnace_status | 查询四台马弗炉状态 | {"request_id":4,"result":0,"data":{...}} |
| 3 | start_sintering | 启动烧结 | {"request_id":3,"result":0} |
| 8 | start_milling | 启动球磨 | {"request_id":8,"result":0} |
| 12 | get_ball_bead | 获取球磨珠数量 | {"request_id":12,"result":0,"data":{"n_ball_bead":<int>}} |
| 13 | set_ball_bead | 设置球磨珠数量 | {"request_id":13,"result":0} |
| 14 | unload_sintering | 下坩埚 | {"request_id":14,"result":0} |
| 9 | loading_material | 上料 | {"request_id":9,"result":0} |
| 10 | unloading_material | 下料 | {"request_id":10,"result":0} |
| 11 | start_material | 启动上下料任务 | {"request_id":11,"result":0} |
## 动作一览
### auto-update_params
功能：生成并下发 params.json（同时支持写文件）。
输入（goal）：A（Recipe_1、Recipe_2 的材料与球珠数），B（烧结参数，可附加温度段），out（输出文件路径，可为空）。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":1,"action":"load_params","data":params}
返回格式：{"request_id":1,"result":0}
### auto-start_recipt
功能：启动配方，长连接轮询状态，完成后拉取结果，可选导出 JSON。
输入（goal）：out（输出文件路径；为空不写入；为目录写入 recipt_result.json）。
参考：见 YAML module 指向的实现模块
协议封装：启动 {"request_id":2,"action":"start_recipt"}；轮询 {"request_id":6,"action":"get_recipt_status"}；拉取 {"request_id":7,"action":"get_recipt_result","param":{"recipt":"<key>"}}。
### auto-get_furnace_status
功能：查询四台马弗炉的状态信息。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":4,"action":"get_furnace_status"}
### auto-start_sintering
功能：启动烧结流程。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":3,"action":"start_sintering"}
返回格式：{"request_id":3,"result":0}
### auto-get_ball_bead
功能：获取球磨珠数量。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":12,"action":"get_ball_bead"}
返回格式：{"request_id":12,"result":0,"data":{"n_ball_bead":<int>}}
### auto-set_ball_bead
功能：设置球磨珠数量（前端输入）。
输入（goal）：n_ball_bead（整数）。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":13,"action":"set_ball_bead","param":{"n_ball_bead":<int>}}
返回格式：{"request_id":13,"result":0}
### auto-unload_sintering
功能：下坩埚（炉号列表可变）。
输入（goal）：furnace（数组，元素为 1–4，可传 1–4 个）。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":14,"action":"unload_sintering","param":{"furnace":[...]}}
返回格式：{"request_id":14,"result":0}
### auto-loading_material
功能：上料（物料列表可变，键名固定）。
输入（goal）：materials=[{material,weight,manual_rack,auto_rack},...]
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":9,"action":"loading_material","param":[...]}
返回格式：{"request_id":9,"result":0}
### auto-unloading_material
功能：下料（列表可变，键名固定）。
输入（goal）：materials=[{manual_rack,auto_rack},...]
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":10,"action":"unloading_material","param":[...]}
返回格式：{"request_id":10,"result":0}
### auto-start_material
功能：启动上下料任务。
参考：见 YAML module 指向的实现模块
协议封装：{"request_id":11,"action":"start_material"}
返回格式：{"request_id":11,"result":0}

## 流程
下发参数
```json
{"device_id":"<设备ID>","action":"auto-update_params","action_args":{
  "goal": {
    "A": {
      "Recipe_1": {
        "Materials": [
          {"Material":"A1","Quality (g)":1.23,"Precision (g)":0.01},
          {"Material":"A2","Quality (g)":2.34,"Precision (g)":0.02}
        ],
        "n_ball_bead": 100
      },
      "Recipe_2": {
        "Materials": [
          {"Material":"B1","Quality (g)":3.21,"Precision (g)":0.01}
        ],
        "n_ball_bead": 100
      }
    },
    "B": {
      "Status": true,
      "furnace_01": {"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":480,"time_temp2":360,"end_temp2":480},
      "furnace_02": {"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":490,"time_temp2":360,"end_temp2":490}
    },
    "out": ""
  }
}}
```
启动配方并轮询（可指定结果写入位置）
```json
{"device_id":"<设备ID>","action":"auto-start_recipt","action_args":{
  "goal": {
    "out": "d:/data/solid/results"
  }
}}
```
查看马弗炉状态（可在任意时刻执行）
```json
{"device_id":"<设备ID>","action":"auto-get_furnace_status","action_args":{"goal":{}}}
```
启动烧结

```json
{"device_id":"<设备ID>","action":"auto-start_sintering","action_args":{"goal":{}}}
```
获取球磨珠数量
```json
{"device_id":"<设备ID>","action":"auto-get_ball_bead","action_args":{"goal":{}}}
```
设置球磨珠数量
```json
{"device_id":"<设备ID>","action":"auto-set_ball_bead","action_args":{"goal":{"n_ball_bead":200}}}
```
上料（物料列表可变）
```json
{"device_id":"<设备ID>","action":"auto-loading_material","action_args":{
  "goal": {
    "materials": [
      {"material":"Li2S","weight":10.0,"manual_rack":1,"auto_rack":1},
      {"material":"P2S5","weight":10.0,"manual_rack":2,"auto_rack":2}
    ]
  }
}} 
```
下料（列表可变）
```json
{"device_id":"<设备ID>","action":"auto-unloading_material","action_args":{
  "goal": {
    "materials": [
      {"auto_rack":1,"manual_rack":1},
      {"auto_rack":2,"manual_rack":2}
    ]
  }
}}
```
启动上下料任务
```json
{"device_id":"<设备ID>","action":"auto-start_material","action_args":{"goal":{}}}
```

## 基础启动与组合
参数更新：仅执行参数生成与下发，便于后续分步操作。
独立启动：在已下发参数后，单独执行 auto-start_recipt；过程中自动轮询状态与拉取结果。
独立状态查询：在执行中或结束后，使用 auto-get_furnace_status 查询状态。
独立烧结：在需要时执行 auto-start_sintering。

## 重要说明
长连接说明：
  所有接口均使用长连接复用同一 TCP 套接字（update_params、start_recipt、start_sintering、start_milling），异常时自动重连（实现细节见后端模块 _send_recv/_ensure_socket）。
  发送按行结束符 CRLF，接收按 b"\\n"/b"\\r\\n" 聚合。
写文件策略：
  auto-update_params：out 为空则写到脚本同目录的 params.json；为目录则写入该目录；为具体文件路径则按指定路径写入。
  auto-start_recipt：out 为空则不写入；为目录则写入 recipt_result.json；为具体文件路径则按指定路径写入。
协议解析一致性：
  返回存在 return_value 时优先解析其字段，否则解析顶层字段。
  get_recipt_status 的 data 值支持 int/float/string（去空白可转 int），仅接受 0/1/2；所有为 2 判定完成。
  update_params/start_sintering/start_milling/set_ball_bead/unload_sintering/loading_material/unloading_material/start_material 的返回仅包含 request_id 与 result（不含 data）；get_ball_bead 返回包含 data。
## 数据字段
参数结构（片段）：
```json
{
  "Solid_loading_module": {
    "status": true,
    "n_Recipes": 2,
    "Recipe_1": {"name": "YYYYMMDD-R1", "n_ball_bead": 100, "A1": [1.23,0.01], "A2": [2.34,0.02]},
    "Recipe_2": {"name": "YYYYMMDD-R2", "n_ball_bead": 100, "B1": [3.21,0.01]}
  },
  "Sintering_module": {
    "Status": true,
    "sintering_recipe": ["YYYYMMDD-R1", "YYYYMMDD-R2"],
    "furnace_01": {"status": true, "begin_temp1": 25, "time_temp1": 180, "end_temp1": 480}
  }
}
```

状态查询返回（示例）：

```json
{"request_id":4,"result":0,"data":{
  "furnace_01":{"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":480},
  "furnace_02":{"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":490}
}}
```

配方状态（服务端约定示例）：

```json
{"request_id":6,"result":0,"data":{
  "#1-20251205-R1":2,
  "#2-20251205-R2":1
}}
```

更新参数返回（示例）：

```json
{"request_id":1,"result":0}
```

烧结返回（示例）：

```json
{"request_id":3,"result":0}
```

球磨返回（示例）：

```json
{"request_id":8,"result":0}
```

## 协议与返回
发送协议：每次发送 JSON 文本并追加 CRLF。
返回解析：
  若包含 return_value 且为对象，则解析其 request_id/result/data。
  否则解析顶层对象的 request_id/result/data。
相关实现：见 YAML module 指向的后端模块

## 参考路径
设备注册配置：[sse_auto_synthesis.yaml](Uni-Lab-OS/unilabos/registry/devices/sse_auto_synthesis.yaml)
设备图配置：[sse_auto_synthesis.json](Uni-Lab-OS/unilabos/devices/workstation/sse_auto_synthesis/sse_auto_synthesis.json)
设备实现代码：[sse_auto_synthesis.py](Uni-Lab-OS/unilabos/devices/workstation/sse_auto_synthesis/sse_auto_synthesis.py)
