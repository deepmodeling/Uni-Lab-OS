# SZLab Poly Studio PLC/OPC-UA 统一规范

面向 `unilabos/devices/workstation/szlab_poly_studio` 后续开发，所有涉及 PLC 的设备 action 必须遵守以下规则。

## 核心抽象

- 设备 action 只读写“PLC 变量名”，不要在 action 逻辑里硬编码 OPC-UA `NodeId` 或 PLC 软元件地址。
- 变量名到 OPC-UA 节点的解析统一放在底层通信层完成，来源可以是 OPC-UA server 浏览结果、CSV 的 `node_id/nodeid` 列、`opcua_node_id_map` 或 `opcua_node_id_prefix`。
- CSV 里的 `软元件地址` 只作为 PLC 工程/人工排查信息；当前设备代码不得直接依赖它读写。

## 推荐接入方式

- 新增 SZLab 设备优先通过 `plc_device_id="szlab_poly_plc"` 使用统一 PLC 网关，并实现 `set_plc_gateway(plc_gateway)`。
- 设备内部读写统一封装为 `_read_variable(name, use_cache=False)` 和 `_write_variable(name, value)`，action 不直接调用 OPC-UA client。
- 需要独立 OPC-UA client 的设备也必须保持同一抽象：对上层暴露变量名读写，对下层用 URL/CSV/映射解析 NodeId。
- ROS 设备需要转发 PLC 操作时，统一调用 `szlab_poly_plc` 的 `read_variable` / `write_variable`，不要新增一套 PLC 命令协议。

## 配置约定

- 真实 PLC、外部虚拟 OPC-UA server、CI 虚拟 PLC 都通过 `url` 连接；设备 action 不区分环境。
- 真机或外部虚拟 server 的 NodeId 已知时，优先在 CSV `node_id/nodeid` 列或 `opcua_node_id_map` 中声明，避免依赖大范围递归浏览。
- CI 虚拟 PLC 使用 CSV 创建临时 OPC-UA server，并用 flow daemon 模拟 PLC 状态机；测试应验证同一套设备 action 可以直接连这个 endpoint。
- runtime/preset 中的 `action_variables` 只用于 UI/日志采样展示，不作为 action 执行的唯一事实来源。

## 开发禁忌

- 不要在 action 方法里写 `ns=...`、`R10000.0` 这类地址。
- 不要为真机、外部虚拟 server、CI server 分叉三套 action 逻辑。
- 不要让上层 workflow 直接依赖 NodeId；workflow 参数应保持业务语义。
- 不要在设备间复制粘贴 PLC 连接逻辑；能走 `szlab_poly_plc` 网关时必须复用网关。

## 新设备检查清单

- action 是否只使用变量名。
- 是否提供或复用了变量名常量/生成函数，例如 `s04_process_var(position)`。
- 是否支持通过 `url` 指向真实或虚拟 OPC-UA server。
- 是否能通过 CSV `node_id/nodeid` 或 `opcua_node_id_map` 精确定位 NodeId。
- 是否有 CI/本地虚拟 OPC-UA flow 覆盖关键握手：写入参数、触发完成、复位状态。
