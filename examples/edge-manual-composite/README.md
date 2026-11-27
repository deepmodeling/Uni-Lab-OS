# Edge 网页手测工作区

当前网页：http://127.0.0.1:60542/?page=workflows

正式 Workspace Host 管理调度后端和 ROS Edge 两个进程；runtime-mode 为 normal。3个设备来自本目录的验收驱动，S06/S07 通过 OPC UA 连接本地 Kubernetes edge PLC Sim；不是脚本 CallbackDispatcher。共同范围使用独立库存中的 region 身份。

网页已加载“综合并行验收 A”“综合并行验收 B”，两者 Preflight 均 runnable_now。启动校验任务 `18892cbd-8020-46e9-adf2-59bc54d55aa3` 已经由正式 HTTP API 创建，通过 ROS Edge 及验收驱动执行到 succeeded，无错误。

手动操作：
1. 选择 A → 进入运行准备 → 运行 Preflight → 提交任务。
2. 返回工作流选择 B，同样提交，然后到“任务”观察两个任务。
3. 保持 hold_seconds=2，便于看清在途和等待；可设为5延长观察，支持0～10秒。
4. choose_first=true/false 用来选择条件分支。A先条件后循环，B先循环后条件，循环为3轮。

这份网页版本保留正常的共享范围、设备竞争、并行join、条件和循环。原先运行器自动批准的人工节点和注入失败/超时/未知回执的测试控制未搬入网页版本，不能将其视为原11配置的完全等价迁移。源代码工作流在工作区能力允许时可直接运行，未发布到运行目录。虚拟液体补料测试也不在此网页工作区。

两个流程的额外等待只用于观察调度；PLC完成位已确认后仍在动作内等待 hold_seconds，再复位并返回。此时间不是PLC实际加工时长。循环退出信息按输入轮次生成，不是质量传感器。

源码：parallel_lab/workflows/composite_a.py、composite_b.py。
驱动：parallel_lab/devices/channel.py。

继续运行需要保留到模拟器的端口转发：

```bash
kubectl -n edge port-forward svc/plc-sim 24855:4855
```

从 Uni-Lab-OS 根目录重新启动：

```bash
/Users/xiongyanfei/.local/share/mamba/envs/unilab/bin/python -m unilabos.app.main workspace start --workspace examples/edge-manual-composite --graph deployment/graph.json --runtime-mode normal --json
```

重启后端口可能变化，用同一命令入口的 `workspace status --workspace examples/edge-manual-composite --json` 查看 components.backend.address。停止请使用 `workspace stop` 排空，不要删除运行数据库。

`.unilabos/` 保存本地进程鉴权、数据库和日志，已忽略提交。
