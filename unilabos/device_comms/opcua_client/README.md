# OPC UA 通用客户端

本模块提供了一个通用的 OPC UA 客户端实现，可以通过外部配置（CSV文件）来定义节点，并通过JSON配置来执行工作流。

## 特点

- 支持通过 CSV 文件配置 OPC UA 节点（只需提供名称、类型和数据类型,支持节点为中文名，需指定NodeLanguage）
- 自动查找服务器中的节点，无需知道确切的节点ID
- 提供工作流机制
- 支持通过 JSON 配置创建工作流

## 使用方法

step1: 准备opcua_nodes.csv文件
step2: 编写opcua_workflow_example.json,以定义工作流。指定opcua_nodes.csv
step3: 编写工作流对应action
step4: 编写opcua_example.yaml注册表
step5: 编写opcua_example.json组态图。指定opcua_workflow_example.json定义工作流文件

