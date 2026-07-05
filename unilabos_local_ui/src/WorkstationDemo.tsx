import { useState } from "react";

type ActionDemo = {
  label: string;
  subtitle: string;
  status: "ready" | "selected" | "locked";
};

type ActionGroupDemo = {
  title: string;
  device: string;
  hint: string;
  actions: ActionDemo[];
};

type StackResourceDemo = {
  id: string;
  title: string;
  role: string;
  used: number;
  total: number;
  nextSlot: string;
  status: "ok" | "busy" | "warn";
};

type CanvasNode = {
  id: string;
  label: string;
  device: string;
  status: "done" | "running" | "queued" | "blocked";
  x: number;
  y: number;
};

type StationDemo = {
  label: string;
  value: string;
  status: "ok" | "busy" | "empty" | "warn";
};

type StackSlotDemo = {
  id: string;
  material?: string;
  status: "occupied" | "reserved" | "empty" | "blocked";
};

type StackDemo = {
  id: string;
  title: string;
  role: string;
  sensor: string;
  rule: string;
  slots: StackSlotDemo[];
};

const actionGroups: ActionGroupDemo[] = [
  {
    title: "机械臂转运",
    device: "szlab_mixer_robot",
    hint: "负责堆栈、S04、S05 之间的 pick/place",
    actions: [
      { label: "从上料堆栈抓取孔板", subtitle: "pick_from_loading_stack(position)", status: "ready" },
      { label: "放置到 S04 磁搅位", subtitle: "submit_place_to_s04(position)", status: "selected" },
      { label: "从 S04 磁搅位取走", subtitle: "submit_pick_from_s04(position)", status: "ready" },
      { label: "转运到 S05 拍照站", subtitle: "submit_place_to_s05()", status: "ready" },
    ],
  },
  {
    title: "szlab_mixer_stirrer",
    device: "szlab_mixer_stirrer",
    hint: "只表达设备动作，不负责物料位置变更",
    actions: [
      { label: "执行 S04 磁搅加工", subtitle: "run_stirring(position, temp, speed, time)", status: "ready" },
    ],
  },
  {
    title: "szlab_mixer_photoshotting",
    device: "szlab_mixer_photoshotting",
    hint: "只表达设备动作，不负责物料位置变更",
    actions: [
      { label: "S05 拍照并保存结果", subtitle: "take_photo()", status: "locked" },
    ],
  },
];

const stackResources: StackResourceDemo[] = [
  { id: "loading_a", title: "上料堆栈 A", role: "输入", used: 3, total: 8, nextSlot: "A01", status: "busy" },
  { id: "unload_b", title: "下料堆栈 B", role: "输出", used: 0, total: 8, nextSlot: "B01", status: "ok" },
  { id: "buffer_c", title: "缓存堆栈 C", role: "缓存", used: 2, total: 4, nextSlot: "C02", status: "warn" },
];

const canvasNodes: CanvasNode[] = [
  { id: "node_001", label: "抓取 plate-001", device: "szlab_mixer_robot", status: "done", x: 12, y: 34 },
  { id: "node_002", label: "放入 S04[1]", device: "szlab_mixer_robot", status: "running", x: 36, y: 34 },
  { id: "node_003", label: "S04 磁搅加工", device: "szlab_mixer_stirrer", status: "queued", x: 60, y: 34 },
  { id: "node_004", label: "转运到 S05", device: "szlab_mixer_robot", status: "queued", x: 60, y: 62 },
  { id: "node_005", label: "S05 拍照归档", device: "szlab_mixer_photoshotting", status: "blocked", x: 84, y: 62 },
];

const stationCards: StationDemo[] = [
  { label: "上料堆栈 A", value: "8 槽 / 2 已占用", status: "ok" },
  { label: "机械臂夹爪", value: "plate-001 转运中", status: "busy" },
  { label: "S04 磁搅位 1", value: "已预约，允许加工", status: "ok" },
  { label: "S05 拍照站", value: "等待 S04 完成", status: "empty" },
  { label: "下料堆栈 B", value: "8 槽空闲", status: "ok" },
  { label: "废液/异常位", value: "无异常物料", status: "warn" },
];

const stacks: StackDemo[] = [
  {
    id: "loading_a",
    title: "上料堆栈 A",
    role: "输入孔板",
    sensor: "A01到位=true, A满料=false",
    rule: "从低号槽出栈；机械臂抓取前必须确认槽位有料。",
    slots: [
      { id: "A01", material: "plate-001", status: "reserved" },
      { id: "A02", material: "plate-002", status: "occupied" },
      { id: "A03", material: "plate-003", status: "occupied" },
      { id: "A04", status: "empty" },
      { id: "A05", status: "empty" },
      { id: "A06", status: "empty" },
      { id: "A07", status: "empty" },
      { id: "A08", status: "empty" },
    ],
  },
  {
    id: "unload_b",
    title: "下料堆栈 B",
    role: "完成品回收",
    sensor: "B01满料=false, B门禁=true",
    rule: "按低号空槽入栈；满料时禁止 S05 后续出料。",
    slots: [
      { id: "B01", status: "empty" },
      { id: "B02", status: "empty" },
      { id: "B03", status: "empty" },
      { id: "B04", status: "empty" },
      { id: "B05", status: "empty" },
      { id: "B06", status: "empty" },
      { id: "B07", status: "empty" },
      { id: "B08", status: "empty" },
    ],
  },
  {
    id: "buffer_c",
    title: "缓存堆栈 C",
    role: "异常/等待缓存",
    sensor: "C占用=1, C异常=false",
    rule: "用于设备忙碌或人工复核，不自动进入主流程。",
    slots: [
      { id: "C01", material: "tube-rack-01", status: "occupied" },
      { id: "C02", status: "empty" },
      { id: "C03", status: "empty" },
      { id: "C04", status: "blocked" },
    ],
  },
];

const materialRows = [
  { id: "plate-001", stack: "A01", current: "robot_gripper", next: "S04[1]", state: "转运中" },
  { id: "plate-002", stack: "A02", current: "loading_stack", next: "等待 S04", state: "等待" },
  { id: "plate-003", stack: "A03", current: "loading_stack", next: "等待队列", state: "未开始" },
];

const sensorRows = [
  { index: 1, node: "放入 S04[1]", nodeId: "ns=4;s=上位机通讯|S04取放料编号", name: "S04取放料编号", begin: "0", goal: "1", end: "1" },
  { index: 2, node: "放入 S04[1]", nodeId: "ns=4;s=上位机通讯|PLC_R任务号", name: "PLC_R任务号", begin: "1288", goal: "1289", end: "1289" },
  { index: 3, node: "S04 磁搅加工", nodeId: "ns=4;s=上位机通讯|S041允许加工", name: "S041允许加工", begin: "true", goal: "true", end: "true" },
  { index: 4, node: "S04 磁搅加工", nodeId: "ns=4;s=上位机通讯|S041加工完成", name: "S041加工完成", begin: "false", goal: "true", end: "pending" },
  { index: 5, node: "S05 拍照归档", nodeId: "ns=4;s=上位机通讯|S05拍照结果", name: "S05拍照结果", begin: "-", goal: "ok", end: "pending" },
];

const eventRows = [
  { id: "#1", text: "载入工作站布局：S04/S05/机械臂/堆栈", tone: "ok" },
  { id: "#2", text: "物料 plate-001 从 A01 出栈，夹爪占用", tone: "ok" },
  { id: "#3", text: "正在写入 S04 取放料编号与 PLC_R 任务号", tone: "running" },
  { id: "#4", text: "S05 节点等待 S04 完成信号，暂不执行", tone: "wait" },
];

export function WorkstationDemo() {
  const [selectedStackId, setSelectedStackId] = useState(stacks[0].id);
  const [modalStackId, setModalStackId] = useState<string | null>(null);
  const [leftTab, setLeftTab] = useState<"devices" | "stacks">("devices");
  const [mainTab, setMainTab] = useState<"workflow" | "sensors">("workflow");
  const [sideTab, setSideTab] = useState<"control" | "materials" | "logs">("control");
  const selectedStack = stacks.find((stack) => stack.id === selectedStackId) || stacks[0];
  const modalStack = stacks.find((stack) => stack.id === modalStackId) || null;

  return (
    <div className="demo-shell demo-tool-shell">
      <header className="demo-tool-header">
        <div>
          <p>SZLab Poly Studio</p>
          <h1>S04/S05 工作站联调 Demo</h1>
        </div>
        <dl className="demo-header-metrics" aria-label="联调状态摘要">
          <div>
            <dt>状态</dt>
            <dd>运行中</dd>
          </div>
          <div>
            <dt>节点</dt>
            <dd>5</dd>
          </div>
          <div>
            <dt>物料</dt>
            <dd>3</dd>
          </div>
          <div>
            <dt>OPC</dt>
            <dd>500ms</dd>
          </div>
        </dl>
      </header>

      <main className="demo-workbench">
        <aside className="demo-card demo-action-panel">
          <div className="demo-panel-title">
            <h2>联调入口</h2>
            <span>Device / Stack</span>
          </div>
          <div className="demo-tabbar left" role="tablist" aria-label="联调入口切换">
            <button className={leftTab === "devices" ? "active" : ""} onClick={() => setLeftTab("devices")} type="button">设备动作</button>
            <button className={leftTab === "stacks" ? "active" : ""} onClick={() => setLeftTab("stacks")} type="button">堆栈资源</button>
          </div>
          <div className="demo-left-sections">
            {leftTab === "devices" && (
            <section className="demo-left-section">
              <div className="demo-left-section-head">
                <strong>设备动作</strong>
                <span>Device actions</span>
              </div>
              <div className="demo-action-tree">
                {actionGroups.map((group) => (
                  <section className="demo-action-tree-group" key={group.title}>
                    <div className="demo-action-tree-parent">
                      <strong>{group.title}</strong>
                      <code>{group.device}</code>
                      <span>{group.actions.length} 项</span>
                    </div>
                    <div className="demo-action-tree-children">
                      {group.actions.map((action) => (
                        <button className={`demo-action-row ${action.status}`} key={action.label} type="button">
                          <span>{action.label}</span>
                          <code title={action.subtitle}>{action.subtitle}</code>
                          <em>{actionStatusText(action.status)}</em>
                        </button>
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            </section>
            )}

            {leftTab === "stacks" && (
            <section className="demo-left-section stack-entry">
              <div className="demo-left-section-head">
                <strong>堆栈资源</strong>
                <span>Stack resources</span>
              </div>
              <table className="demo-stack-resource-table">
                <thead>
                  <tr>
                    <th>堆栈</th>
                    <th>占用</th>
                    <th>下一槽</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {stackResources.map((stack) => (
                    <tr
                      className={selectedStack.id === stack.id ? "active" : ""}
                      key={stack.id}
                      onClick={() => setSelectedStackId(stack.id)}
                      onDoubleClick={() => setModalStackId(stack.id)}
                    >
                      <td>
                        <strong>{stack.title}</strong>
                        <span>{stack.role}</span>
                      </td>
                      <td>{stack.used}/{stack.total}</td>
                      <td>{stack.nextSlot}</td>
                      <td>
                        <button className="demo-table-action" onClick={() => setModalStackId(stack.id)} type="button">
                          详情
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
            )}
          </div>
        </aside>

        <section className="demo-card demo-main-panel">
          <div className="demo-canvas-toolbar">
            <div>
              <strong>szlab_mixer_integrated_flow</strong>
              <span>物料状态校验 + 传感器快照保存 + 机械臂搬运</span>
            </div>
            <div className="demo-toolbar-actions">
              <button type="button">校验流程</button>
              <button type="button">保存模板</button>
              <button className="primary" type="button">Dry-run</button>
            </div>
          </div>

          <div className="demo-tabbar" role="tablist" aria-label="主工作区切换">
            <button className={mainTab === "workflow" ? "active" : ""} onClick={() => setMainTab("workflow")} type="button">流程画布</button>
            <button className={mainTab === "sensors" ? "active" : ""} onClick={() => setMainTab("sensors")} type="button">传感器快照</button>
          </div>

          {mainTab === "workflow" && (
            <div className="demo-canvas">
              <div className="demo-flow-line line-1" />
              <div className="demo-flow-line line-2" />
              {canvasNodes.map((node) => (
                <article
                  className={`demo-canvas-node ${node.status}`}
                  key={node.id}
                  style={{ left: `${node.x}%`, top: `${node.y}%` }}
                >
                  <small>{nodeStatusText(node.status)}</small>
                  <strong>{node.label}</strong>
                  <code>{node.device}</code>
                </article>
              ))}
            </div>
          )}

          {mainTab === "sensors" && (
            <div className="demo-opc-dock tabbed">
              <div className="demo-panel-title compact">
                <h2>OPC / 传感器快照</h2>
                <span>动作前后自动保存</span>
              </div>
              <table className="demo-opc-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Workflow Node</th>
                    <th>NodeID</th>
                    <th>Name</th>
                    <th>Value Begin</th>
                    <th>Value Goal</th>
                    <th>Value End</th>
                  </tr>
                </thead>
                <tbody>
                  {sensorRows.map((row) => (
                    <tr key={row.index}>
                      <td>{row.index}</td>
                      <td>{row.node}</td>
                      <td><code>{row.nodeId}</code></td>
                      <td><strong>{row.name}</strong></td>
                      <td>{row.begin}</td>
                      <td>{row.goal}</td>
                      <td>{row.end}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <aside className="demo-card demo-right-panel">
          <div className="demo-tabbar side" role="tablist" aria-label="右侧信息切换">
            <button className={sideTab === "control" ? "active" : ""} onClick={() => setSideTab("control")} type="button">控制</button>
            <button className={sideTab === "materials" ? "active" : ""} onClick={() => setSideTab("materials")} type="button">物料</button>
            <button className={sideTab === "logs" ? "active" : ""} onClick={() => setSideTab("logs")} type="button">日志</button>
          </div>

          {sideTab === "control" && (
          <section className="demo-side-tab-panel">
            <div className="demo-panel-title">
              <h2>流程控制</h2>
              <span>Run manager</span>
            </div>
            <div className="demo-run-buttons">
              <button type="button">运行配置</button>
              <button type="button">校验流程</button>
              <button className="primary" type="button">运行</button>
              <button className="danger" type="button">停止</button>
            </div>
            <div className="demo-control-summary">
              <div className="demo-panel-title compact">
                <h2>站位摘要</h2>
                <span>Station state</span>
              </div>
              <div className="demo-station-list">
                {stationCards.map((station) => (
                  <article className={`demo-station-mini ${station.status}`} key={station.label}>
                    <span>{station.label}</span>
                    <strong>{station.value}</strong>
                  </article>
                ))}
              </div>
            </div>
          </section>
          )}

          {sideTab === "materials" && (
          <section className="demo-material-section demo-side-tab-panel">
            <div className="demo-panel-title">
              <h2>物料 / 堆栈</h2>
              <span>Compact ledger</span>
            </div>
            <table className="demo-material-table">
              <thead>
                <tr>
                  <th>物料</th>
                  <th>当前位置</th>
                  <th>下一步</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {materialRows.map((material) => (
                  <tr key={material.id}>
                    <td>
                      <strong>{material.id}</strong>
                      <span>{material.stack}</span>
                    </td>
                    <td>{material.current}</td>
                    <td>{material.next}</td>
                    <td>{material.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          )}

          {sideTab === "logs" && (
          <section className="demo-log-section demo-side-tab-panel">
            <div className="demo-panel-title">
              <h2>运行日志</h2>
              <span>Timeline</span>
            </div>
            <div className="demo-event-list">
              {eventRows.map((event) => (
                <article className={`demo-event-row ${event.tone}`} key={event.id}>
                  <span>{event.id}</span>
                  <p>{event.text}</p>
                </article>
              ))}
            </div>
          </section>
          )}
        </aside>
      </main>
      {modalStack && (
        <div className="demo-modal-backdrop" onMouseDown={() => setModalStackId(null)}>
          <section
            aria-label={`${modalStack.title} 详情`}
            className="demo-stack-modal"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="demo-modal-head">
              <div>
                <p>Stack detail</p>
                <h2>{modalStack.title}</h2>
                <span>{modalStack.role}</span>
              </div>
              <button onClick={() => setModalStackId(null)} type="button">关闭</button>
            </div>
            <div className="demo-stack-modal-grid">
              {modalStack.slots.map((slot) => (
                <article className={`demo-stack-modal-slot ${slot.status}`} key={slot.id}>
                  <strong>{slot.id}</strong>
                  <small>{slotStatusText(slot.status)}</small>
                  <p>{slot.material || "空槽"}</p>
                </article>
              ))}
            </div>
            <div className="demo-stack-modal-meta">
              <article>
                <strong>传感器</strong>
                <code>{modalStack.sensor}</code>
              </article>
              <article>
                <strong>出入栈规则</strong>
                <p>{modalStack.rule}</p>
              </article>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function nodeStatusText(status: CanvasNode["status"]) {
  if (status === "done") return "成功";
  if (status === "running") return "运行中";
  if (status === "blocked") return "等待信号";
  return "排队";
}

function slotStatusText(status: StackSlotDemo["status"]) {
  if (status === "occupied") return "有料";
  if (status === "reserved") return "已预约";
  if (status === "blocked") return "禁用";
  return "空闲";
}

function actionStatusText(status: ActionDemo["status"]) {
  if (status === "selected") return "选中";
  if (status === "locked") return "锁定";
  return "可用";
}
