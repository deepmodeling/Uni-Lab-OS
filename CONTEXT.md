# Edge Resource and Workflow Context

This context defines the domain language shared by the frontend, Backend, and
Edge microbackend. Backend is the canonical public contract; Edge provides a
local adapter to the same concepts.

## Language

**Shared Interface**:
The public resource and workflow contract presented identically by Backend and
Edge, including route meaning, field spelling, enums, envelopes, and errors.
_Avoid_: Similar API, Edge-shaped response, frontend fallback

**Authority**:
The single selected owner allowed to accept changes for an aggregate during a
given operation; replicas and projections do not become additional owners.
_Avoid_: Last writer wins, dual truth, nearest database

**Backend Authority**:
The configured laboratory service that owns shared domain state. It may be a
central Docker deployment and remains external to Workspace Host lifecycle;
selecting it never implies that Workbench starts, stops, or restarts it.
_Avoid_: Managed Local process, workspace child, embedded Backend

**Material UUID**:
The sole stable identity of a Material, referred to as `material_uuid` by
relationships and workflow fields.
_Avoid_: Edge UUID, cloud UUID, instance UUID

**Material Composition**:
The structural parent-child relationship expressed by a Material's
`parent_uuid`.
_Avoid_: Site occupancy, laboratory layout, scheduler claim

**Site**:
A stable named position owned by one Material; `Site.uuid` identifies the
position itself, never its owner or occupant.
_Avoid_: Material UUID, slot name, PLR index

**Site Occupancy**:
The optional relationship from a Site to the Material currently placed there,
expressed by `occupied_material_uuid`.
_Avoid_: Material Composition, Site identity, execution lock

**Soft Deletion**:
The lifecycle state in which an aggregate retains its UUID and history while
normal reads exclude it after `deleted_at` becomes non-null.
_Avoid_: Physical row deletion, empty status, hidden alias

**Workflow**:
A reusable graph definition owned by the selected Authority. Backend Authority
may persist it; Local Authority rebuilds it into a process-local catalog from an
authorized domain-package Python source. JSON and Python imports are normalized
to that Python source before they become Local definitions.
_Avoid_: One execution, runtime snapshot, Run

**Workflow Revision（工作流修订）**:
The internal editable-definition revision carried by `workflow.revision` or
`workflow_revision`. It supports optimistic concurrency and binds a graph
snapshot. Users edit, validate, and publish the current Workflow definition;
there is no separately managed business release identity for that definition.
_Avoid_: Contract publication sequence, schema version, task execution identity

**Published Contract（发布合同）**:
An immutable contract produced by publishing a Workflow revision, identified by
`contract_uuid` and `contract_digest` (contract UUID/digest). A Workflow Task
retains the execution graph and source snapshot frozen at its creation;
subsequent definition edits or publications do not rewrite that task.
_Avoid_: Mutable draft, runtime lookup of a newer contract, task snapshot rewrite

**Contract Publication Sequence（合同发布序号）**:
The internal ordering value stored in `published_workflow_contract.version` and
retained in compatibility contract metadata. It orders published contracts for
storage and recovery; it is not a user-managed Workflow property and must not be
interpreted as `workflow_revision`. Existing compatibility expressions and text
remain unchanged during terminology-only cleanup.
_Avoid_: Workflow revision, public release selector, editable business identity

**Schema Version（schema version）**:
The value-format discriminator such as `version: 1` in Workflow input/output
contracts and Site Selector data. It identifies how a value is structured, not
the identity or revision of a Workflow definition or published contract.
_Avoid_: Workflow revision, contract publication sequence, task identity

**实验操作（ExperimentOperation）**:
中文定义：具有明确实验语义、可分类，并可在发布后供其他工作流复用的一类工作流定义。
中文避免：子工作流、可复用节点。
English: A classified Workflow definition with explicit experimental meaning
that other Workflows may reuse after publication.
_Avoid_: Subworkflow, reusable node

**Workspace Workflow Source**:
Authorized domain-package Python source that is AST-scanned, compiled, and
validated into the Local Workflow Catalog on every OS start; it has no implicit
write relationship to a Backend-owned Workflow.
_Avoid_: Backend definition, remote replica, implicit publication

**Local Workflow Catalog**:
The process-local, non-durable set of validated Workflow definitions used by
Local Authority. Domain-package workflows, including JSON/Python imports, are
rebuilt at startup; API edits to an imported Workflow are written back to the
same registered Python source before the in-memory revision advances.
_Avoid_: SQLite definition authority, temporary upload, Backend replica

**Local Workflow Publication Catalog**:
The domain-package `workflow_publications/<workflow_uuid>/` directory that stores
immutable published Workflow contracts and their source hashes. Each workflow has
a small `manifest.json` index and one complete contract file per immutable
contract. The legacy `workflow_publications.json` file is read only for migration
and backward compatibility; it is not the new-format authority. Local Authority
restores these contracts into the process-local catalog after Python sources are
activated, so published status and ExperimentOperation reuse survive restart. It
is definition-side file authority, not runtime SQLite state.
_Avoid_: SQLite publication authority, derived status without restart recovery

**组合工作流调用（CompositeWorkflowInvocation）**:
中文定义：父工作流中静态展开一个已发布实验操作的稳定调用节点。实验操作产生兼容的新应用
定义或发布合同时，本地权威（Local Authority）自动刷新没有待应用编辑的父工作流，
并保持调用 UUID、布局、参数值和外部连线；不兼容时保留父工作流上一个有效定义并
返回诊断，已经创建的工作流任务（WorkflowTask）始终不变。
中文避免：运行时动态读取实验操作、要求前端点击重新编译、静默重映射参数。
English: A stable parent node that statically expands one published child and is
automatically refreshed only when the new child contract is compatible.
_Avoid_: Runtime child lookup, frontend recompile button, silent parameter remap

**Workflow Task**:
One durable execution created from a frozen Workflow graph. Its snapshot,
execution plan, jobs, results, reservations, and recovery facts survive Local
Workflow Catalog replacement or process restart.
_Avoid_: Workflow definition, Run alias

**Runtime Restart Termination**:
A confirmed execution Runtime restart fails every unfinished station Workflow
Task, regardless of time scheduling or the Job identities in the notification.
Completed results remain; pending inventory settlement may delay cleanup but
never Task failure. Old actions are not replayed. See
[ADR 0005](docs/adr/0005-runtime-restart-fails-station-tasks.md).
_Avoid_: Connection loss, scheduler-only rebuild, physical stop confirmation

**Node Dispatch Boundary（节点派发边界）**:
A Workflow Task node crosses this boundary when its action is handed to the
Edge/device execution chain. Before the boundary, the task's public entry
values may be revised for the undispatched suffix; after it, the node's
execution contract and consumed values are immutable.
_Avoid_: Hardware start time, task completion, page refresh

**Undispatched Plan Revision（未下发计划修订）**:
An audited revision of a task's workflow entry values that revalidates and
replans only nodes before the Node Dispatch Boundary while retaining dispatched
predecessors and their results.
_Avoid_: Workflow definition revision, rerun, silent remap

**Task Plan Shape（任务计划形状）**:
The stable set and topology of node identities, control structure, and declared
inventory requirement identities for one Workflow Task. A task input edit may
change values in existing undispatched Jobs, but it cannot change this shape.
_Avoid_: Current parameter values, scheduler time plan, rendered graph layout

**Task Execution Input Edit（任务执行输入编辑）**:
An audited change to the public Workflow Entry Parameters of one task within an
Paused Edit Window. It preserves workflow topology and never rewrites a node
action, material contract, site binding, or device contract.
_Avoid_: Workflow authoring, node override, runtime remapping

**Paused Edit Window（暂停编辑窗口）**:
The interval in which a running task has first closed its dispatch gate and task
input editing is permitted. Already dispatched Jobs may finish, while only
undispatched nodes and unconsumed Workflow Entry Parameters remain eligible for
revision; editing does not require those in-flight Jobs to finish first, and
their original payloads remain unchanged. Claims and execution locks already
held by dispatched Jobs remain until normal settlement; the undispatched suffix
does not acquire new execution resources while the window is open.
_Avoid_: Device stop, station-wide pause, live mutation of dispatched work

**Workflow Entry Parameters（工作流入口参数）**:
The public values supplied against a published Workflow input contract for one
Workflow Task. They are the only execution values editable at task level; node
Action parameters remain part of the published workflow revision.
_Avoid_: Node Action parameters, intermediate values, device settings

**Consumed Entry Parameter（已消费入口参数）**:
A Workflow Entry Parameter captured by a node whose action has crossed the Node
Dispatch Boundary. Its value is immutable for that task even while later nodes
remain undispatched.
_Avoid_: Displayed parameter, completed output, mutable global setting

**Workflow-bound Device（工作流绑定设备）**:
The device identity or declared equivalent-device group carried by a workflow
node's execution contract. Task input editing cannot replace that contract;
device selection may vary only within the contract's declared equivalence.
_Avoid_: Task-level device override, arbitrary device swap

**Edge-local Task Pause（Edge 本地任务暂停）**:
A control decision applied to one Workflow Task that closes its dispatch gate,
leaves already dispatched Jobs and their held resources under the same task, and
does not stop a device action already handed to the execution chain. Pausing
multiple tasks requires separate task decisions.
_Avoid_: Station-wide pause, device stop, batch transaction

**CP-SAT Candidate Plan（CP-SAT 候选计划）**:
The validated ordering and timing candidate produced for one immutable planning
snapshot. It is usable only while its snapshot revision, time origin, node set,
and resource facts still match the current task state; it is not a dispatch
queue and does not itself create a device command or execution Claim.
_Avoid_: Durable dispatch order, device command, reserved future execution

**Paused Candidate Invalidation（暂停候选失效）**:
When a task closes its dispatch gate, every CP-SAT candidate node that has not
crossed the Node Dispatch Boundary becomes ineligible immediately. In-flight
nodes remain fixed facts with their held resources; resume creates a new
planning revision and re-solves the undispatched suffix. A previous candidate
may be used only as a soft stability hint after validation, never as execution
authority.
_Avoid_: Canceling an in-flight action, resuming an old queue, releasing claims

**Edge-only Inventory Interface**:
The operational lot, reservation, ledger, and diagnostic contract used inside
the local debug control plane; it is not started by the production Backend
control plane and is not part of the Shared Interface.
_Avoid_: Frontend fallback, Backend-compatible route

**Edge Task Presentation Interface**:
The local control-console read projections of the durable Workflow Task.
`/workflow-tasks?projection=summary` supplies ordinary tables with task fields,
aggregate progress and Job counts, without frozen graphs or individual Jobs.
`/workflow-tasks/count` returns only the filtered total. The distinct
`/workflow-task-presentations` route supplies the execution matrix with compact
Task and Job details. Detail refreshes use
`/workflow-tasks/{task_uuid}?projection=state` and `definition_version` to reuse
the full definition until its snapshot or execution plan changes; task status
changes alone do not advance that version. Default list and detail requests
retain their full responses for compatibility. These are read projections,
not additional authorities for task execution.
_Avoid_: Full task reads for counts, status timestamps as definition versions,
second task truth

**Station Event Stream**:
`/api/v1/stations/{station_id}/events/stream` carries runtime, inventory and
resource-usage invalidations over one SSE connection. Its opaque composite
cursor advances each source independently as frames are emitted; it does not
establish a global order across the underlying stores. A `stream.reset` names
the affected `scope` (`runtime`, `inventory` or `workflow`) and requires a REST
refresh of that scope. The original three SSE routes remain available, and
`/api/v1/stations/{station_id}/events` remains the JSON control-history route.
_Avoid_: Merged database sequence, event payloads as current-state authority,
control-history route as SSE

**Authoring Device Catalog**:
The Local Backend-only diagnostic projection that joins Driver registry and ROS
facts for installation, activation, and package acceptance. Normal product UI
uses the Shared Interface `DeviceOverview` plus `WorkflowNodeTemplate` instead.
_Avoid_: Public device fallback, Backend device list, production read model

**Control Plane**:
The single selected owner of workflow scheduling and material facts. `local`
starts the embedded debug Scheduler and Inventory modules; `backend` uses the
production HTTP/WebSocket Edge protocol and does not start those modules.
_Avoid_: Environment name, deployment label, simultaneous local and remote authority
