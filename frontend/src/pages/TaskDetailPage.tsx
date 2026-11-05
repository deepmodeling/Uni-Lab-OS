/**
 * 任务详情：控制命令（step / resume / pause / cancel，带 control_revision 与幂等键）、
 * 暂停窗口内的入口参数修改、冻结图 DAG（叠加节点状态）、节点运行与 attempt、
 * 人工确认、干预与错误决策。
 */
import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { CirclePause, CirclePlay, CircleStop, StepForward } from 'lucide-react'
import { api } from '../api'
import type { ErrorDecisionReport, JsonObject, ManualConfirmation, TaskCommandType, WorkflowNodeRun } from '../api/types'
import { WorkflowDag } from '../components/WorkflowDag'
import {
  Button,
  Degraded,
  EmptyState,
  JsonView,
  PageHeader,
  Panel,
  PanelHeader,
  PropertyList,
  RefreshButton,
  StatusBadge,
  Uuid,
  describeError,
} from '../components/ui'
import { formatDuration, formatTime, isEmptyJson, jsonPreview, newIdempotencyKey } from '../lib/format'
import {
  countByStatus,
  decisionsForTask,
  flattenAttempts,
  nodeIndex,
  sortNodeRuns,
  statusByNode,
} from '../lib/taskDetail'
import { TASK_COMMANDS, canEditInput, commandAvailability, isTerminalTask } from '../lib/taskStatus'
import { toDagModel } from '../lib/workflowGraph'

const COMMAND_META: Record<TaskCommandType, { label: string; icon: ReactNode; tone: 'primary' | 'secondary' | 'danger' }> = {
  step: { label: '单步放行', icon: <StepForward size={15} />, tone: 'primary' },
  resume: { label: '转自动', icon: <CirclePlay size={15} />, tone: 'secondary' },
  pause: { label: '暂停', icon: <CirclePause size={15} />, tone: 'secondary' },
  cancel: { label: '取消任务', icon: <CircleStop size={15} />, tone: 'danger' },
}

export function TaskDetailPage() {
  const { taskUuid = '' } = useParams()
  const queryClient = useQueryClient()
  const task = useQuery({
    queryKey: ['task', taskUuid],
    queryFn: () => api.tasks.get(taskUuid),
    refetchInterval: (query) => (query.state.data && isTerminalTask(query.state.data) ? 10000 : 2000),
  })
  const active = task.data ? !isTerminalTask(task.data) : true
  const interval = active ? 2000 : 10000
  const nodeRuns = useQuery({ queryKey: ['task', taskUuid, 'node-runs'], queryFn: () => api.tasks.nodeRuns(taskUuid), refetchInterval: interval })
  const confirmations = useQuery({ queryKey: ['task', taskUuid, 'manual-confirmations'], queryFn: () => api.tasks.manualConfirmations(taskUuid), refetchInterval: interval })
  const interventions = useQuery({ queryKey: ['task', taskUuid, 'interventions'], queryFn: () => api.tasks.interventions(taskUuid), refetchInterval: interval })
  const decisions = useQuery({ queryKey: ['error-decisions'], queryFn: api.errorDecisions.list, refetchInterval: interval, retry: false })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['task', taskUuid] })
  const [commandResult, setCommandResult] = useState<{ ok: boolean; text: string }>()
  const command = useMutation({
    mutationFn: (type: TaskCommandType) =>
      api.tasks.command(taskUuid, {
        type,
        expected_revision: task.data?.control_revision ?? 0,
        idempotency_key: newIdempotencyKey(type),
      }),
    onSuccess: (result, type) => {
      setCommandResult({ ok: true, text: `${type} 已受理 → status=${result.status} control=${result.control_status} run_mode=${result.run_mode} revision=${result.control_revision}` })
      queryClient.setQueryData(['task', taskUuid], result)
      void invalidate()
    },
    onError: (error, type) => {
      setCommandResult({ ok: false, text: `${type} 被拒绝：${describeError(error)}` })
      void invalidate()
    },
  })

  const [selectedNode, setSelectedNode] = useState<string>()
  const runs = useMemo(() => sortNodeRuns(nodeRuns.data ?? []), [nodeRuns.data])
  const nodes = useMemo(() => nodeIndex(task.data), [task.data])
  const dag = useMemo(() => toDagModel(task.data?.workflow_snapshot ?? {}, statusByNode(runs)), [task.data?.workflow_snapshot, runs])
  const attempts = useMemo(() => flattenAttempts(runs), [runs])
  const taskDecisions = useMemo(
    () => decisionsForTask(decisions.data?.items ?? [], taskUuid, runs),
    [decisions.data, taskUuid, runs],
  )

  if (task.error && !task.data) {
    return (
      <>
        <PageHeader eyebrow="TASK" title="任务详情" description={taskUuid} />
        <Degraded error={task.error} action={<Link className="link-button" to="/tasks">返回列表</Link>} />
      </>
    )
  }
  const data = task.data
  if (!data) return <EmptyState title="正在读取任务" />
  const availability = commandAvailability(data, runs)
  const selectedRun = runs.find((run) => run.workflow_node_uuid === selectedNode)
  const workflowName = data.workflow_snapshot?.workflow?.name

  return (
    <>
      <PageHeader
        eyebrow="TASK"
        title={workflowName ?? (data.execution_kind === 'workflow' ? '工作流任务' : '单点设备动作')}
        description={<><code>{data.uuid}</code>{data.description ? ` · ${data.description}` : ''}</>}
        actions={<><Link className="button button-ghost" to="/tasks">返回列表</Link><RefreshButton onClick={() => void invalidate()} busy={task.isFetching} /></>}
      />
      {task.error ? <Degraded error={task.error} title="刷新失败，显示的是上一次读取的数据" /> : null}

      <div className="grid-2">
        <Panel>
          <PanelHeader title="任务" description="GET /api/v1/workflow-tasks/{uuid}" />
          <div className="panel-body">
            <PropertyList
              items={[
                ['status', <StatusBadge status={data.status} />],
                ['control_status', <StatusBadge status={data.control_status} />],
                ['run_mode', data.run_mode],
                ['control_revision', data.control_revision ?? '—'],
                ['execution_kind', data.execution_kind],
                ['cleanup_status', data.cleanup_status],
                ['workflow', data.workflow_uuid ? <Link className="mono-link" to={`/workflows/${data.workflow_uuid}`}>{data.workflow_uuid}</Link> : '—'],
                ['创建 / 开始 / 结束', `${formatTime(data.create_time)} / ${formatTime(data.started_at)} / ${formatTime(data.finished_at)}`],
                ['耗时', formatDuration(data.started_at, data.finished_at)],
                ...(data.attention_reason ? [['attention_reason', data.attention_reason] as [string, string]] : []),
                ...(data.target_node_uuid ? [['target_node_uuid', <Uuid value={data.target_node_uuid} length={36} />] as [string, ReactNode]] : []),
              ]}
            />
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="控制" description="POST …/commands：expected_revision = control_revision，每次点击一个新幂等键" />
          <div className="panel-body">
            <div className="command-bar">
              {TASK_COMMANDS.map((type) => {
                const state = availability[type]
                const meta = COMMAND_META[type]
                return (
                  <Button
                    key={type}
                    tone={meta.tone}
                    icon={meta.icon}
                    disabled={!state.enabled || command.isPending}
                    title={state.reason}
                    onClick={() => {
                      if (type === 'cancel' && !window.confirm('取消这个任务？已在执行的动作会收到取消请求，设备返回后任务落 canceled。')) return
                      command.mutate(type)
                    }}
                  >
                    {meta.label}
                  </Button>
                )
              })}
            </div>
            <ul className="command-hints">
              {TASK_COMMANDS.map((type) => (
                <li key={type} className={availability[type].enabled ? '' : 'muted'}>
                  <code>{type}</code> {availability[type].reason}
                </li>
              ))}
            </ul>
            {commandResult ? <p className={`command-result ${commandResult.ok ? 'ok' : 'bad'}`}>{commandResult.text}</p> : null}
          </div>
        </Panel>
      </div>

      <div className="grid-2">
        <InputPanel taskUuid={data.uuid} input={data.input ?? {}} editable={canEditInput(data)} revision={data.control_revision} onSaved={() => void invalidate()} />
        <Panel>
          <PanelHeader title="输出与错误" description="output 只在详情返回；error_info 为任务级错误" />
          <div className="panel-body stack">
            <JsonView title="output" value={data.output ?? {}} />
            {!isEmptyJson(data.error_info) ? <JsonView title="error_info" value={data.error_info} /> : <p className="muted">error_info 为空</p>}
          </div>
        </Panel>
      </div>

      {dag.nodes.length ? (
        <Panel className="section">
          <PanelHeader title="冻结执行图" description="workflow_snapshot · 节点颜色 = 当前 attempt 状态；单击节点查看运行" />
          <WorkflowDag model={dag} selectedNodeUuid={selectedNode} onSelectNode={setSelectedNode} />
        </Panel>
      ) : null}

      <Panel className="section">
        <PanelHeader
          title="节点运行"
          description={`GET …/node-runs · ${countByStatus(runs).map(([status, count]) => `${status} ${count}`).join(' · ') || '无'}`}
        />
        {nodeRuns.error ? <Degraded error={nodeRuns.error} /> : null}
        {runs.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>#</th><th>节点</th><th>executor</th><th>状态</th><th>attempts</th><th>开始</th><th>耗时</th><th>return_info</th></tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const info = nodes.get(run.workflow_node_uuid)
                  return (
                    <tr
                      key={run.uuid}
                      className={selectedNode === run.workflow_node_uuid ? 'selected' : ''}
                      onClick={() => setSelectedNode(run.workflow_node_uuid)}
                    >
                      <td>{run.topological_index}</td>
                      <td>
                        <span className="cell-title">{info?.name ?? run.workflow_node_uuid}</span>
                        <small className="cell-sub">{[info?.deviceId, info?.action].filter(Boolean).join('.') || info?.type}</small>
                      </td>
                      <td>{run.executor_kind}</td>
                      <td><StatusBadge status={run.status} /></td>
                      <td>{run.attempt_count}</td>
                      <td>{formatTime(run.started_at)}</td>
                      <td>{formatDuration(run.started_at, run.finished_at)}</td>
                      <td className="cell-json">{jsonPreview(run.return_info, 90)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : !nodeRuns.error ? <EmptyState title="还没有节点运行" /> : null}
      </Panel>

      {selectedRun ? <NodeRunDetail run={selectedRun} name={nodes.get(selectedRun.workflow_node_uuid)?.name} onClose={() => setSelectedNode(undefined)} /> : null}

      <Panel className="section">
        <PanelHeader title="全部 attempt（job）" description="node-runs 内嵌的 attempts 平铺，最新在前；job uuid 与执行器 / 错误决策的 job_id 一致" />
        {attempts.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>job</th><th>节点</th><th>attempt</th><th>trigger</th><th>状态</th><th>开始</th><th>耗时</th><th>error_info</th></tr>
              </thead>
              <tbody>
                {attempts.map(({ job, run, current }) => (
                  <tr key={job.uuid} onClick={() => setSelectedNode(run.workflow_node_uuid)}>
                    <td><Uuid value={job.uuid} />{current ? <small className="cell-sub">当前</small> : null}</td>
                    <td>{nodes.get(run.workflow_node_uuid)?.name ?? run.workflow_node_uuid}</td>
                    <td>{job.attempt_no}</td>
                    <td>{job.trigger}</td>
                    <td><StatusBadge status={job.status} />{job.uncertainty_reason ? <small className="cell-sub">{job.uncertainty_reason}</small> : null}</td>
                    <td>{formatTime(job.started_at)}</td>
                    <td>{formatDuration(job.started_at, job.finished_at)}</td>
                    <td className="cell-json">{isEmptyJson(job.error_info) ? '' : jsonPreview(job.error_info, 90)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title="还没有 attempt" />}
      </Panel>

      <ErrorDecisionsPanel decisions={taskDecisions} error={decisions.error} onResolved={() => void queryClient.invalidateQueries({ queryKey: ['error-decisions'] })} />
      <ManualConfirmationsPanel items={confirmations.data ?? []} error={confirmations.error} onDecided={() => void invalidate()} />

      <Panel className="section">
        <PanelHeader title="干预" description="GET …/interventions（只读）" />
        {interventions.error ? <Degraded error={interventions.error} /> : interventions.data?.length ? (
          <div className="table-scroll">
            <table className="data-table static">
              <thead><tr><th>uuid</th><th>job</th><th>revision</th><th>状态</th><th>打开</th><th>选项</th><th>已选</th></tr></thead>
              <tbody>
                {interventions.data.map((item) => (
                  <tr key={item.uuid}>
                    <td><Uuid value={item.uuid} /></td>
                    <td><Uuid value={item.workflow_node_job_uuid} /></td>
                    <td>{item.revision}</td>
                    <td><StatusBadge status={item.status} /></td>
                    <td>{formatTime(item.opened_at)}</td>
                    <td className="cell-json">{jsonPreview(item.options, 120)}</td>
                    <td className="cell-json">{item.selected_option_id ?? jsonPreview(item.selected_option, 60)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title="没有干预记录" />}
      </Panel>
    </>
  )
}

function InputPanel({
  taskUuid,
  input,
  editable,
  revision,
  onSaved,
}: {
  taskUuid: string
  input: JsonObject
  editable: boolean
  revision?: number
  onSaved: () => void
}) {
  const [draft, setDraft] = useState<string>()
  const [message, setMessage] = useState<{ ok: boolean; text: string }>()
  const save = useMutation({
    mutationFn: (value: JsonObject) =>
      api.tasks.editInput(taskUuid, { expected_revision: revision ?? 0, input: value, idempotency_key: newIdempotencyKey('input') }),
    onSuccess: (task) => {
      setMessage({ ok: true, text: `已保存 → control_revision=${task.control_revision}` })
      setDraft(undefined)
      onSaved()
    },
    onError: (error) => setMessage({ ok: false, text: describeError(error) }),
  })
  const submit = () => {
    let value: unknown
    try {
      value = JSON.parse(draft ?? '{}')
    } catch (error) {
      setMessage({ ok: false, text: `JSON 解析失败：${error instanceof Error ? error.message : String(error)}` })
      return
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      setMessage({ ok: false, text: 'input 必须是 JSON 对象' })
      return
    }
    save.mutate(value as JsonObject)
  }
  return (
    <Panel>
      <PanelHeader
        title="入口参数"
        description={editable ? 'PATCH …/input：暂停窗口内部分更新；已被下发节点消费的参数会被拒绝（3003）' : '任务的有效 input；暂停（control_status=paused）时可修改'}
        action={editable && draft === undefined ? <Button onClick={() => { setDraft(JSON.stringify(input, null, 2)); setMessage(undefined) }}>修改</Button> : null}
      />
      <div className="panel-body stack">
        {draft === undefined ? <JsonView value={input} /> : (
          <>
            <textarea className="json-editor" value={draft} onChange={(event) => setDraft(event.target.value)} rows={10} spellCheck={false} />
            <div className="row-actions">
              <Button tone="primary" disabled={save.isPending} onClick={submit}>提交（只发改动的键也可以）</Button>
              <Button tone="ghost" onClick={() => setDraft(undefined)}>放弃</Button>
            </div>
          </>
        )}
        {message ? <p className={`command-result ${message.ok ? 'ok' : 'bad'}`}>{message.text}</p> : null}
      </div>
    </Panel>
  )
}

function NodeRunDetail({ run, name, onClose }: { run: WorkflowNodeRun; name?: string; onClose: () => void }) {
  const [attemptUuid, setAttemptUuid] = useState<string>()
  const attempts = [...(run.attempts ?? [])].sort((left, right) => right.attempt_no - left.attempt_no)
  const attempt = attempts.find((job) => job.uuid === attemptUuid) ?? attempts[0]
  return (
    <Panel className="section">
      <PanelHeader
        title={<>节点运行：{name ?? run.workflow_node_uuid} <StatusBadge status={run.status} /></>}
        description={<>run <code>{run.uuid}</code> · node <code>{run.workflow_node_uuid}</code> · executor {run.executor_kind} · 超时 {run.execution_timeout_seconds || '—'} s</>}
        action={<Button tone="ghost" onClick={onClose}>收起</Button>}
      />
      <div className="node-run-detail">
        <div className="attempt-list">
          {attempts.length ? attempts.map((job) => (
            <button type="button" key={job.uuid} className={job.uuid === attempt?.uuid ? 'active' : ''} onClick={() => setAttemptUuid(job.uuid)}>
              <strong>attempt {job.attempt_no}</strong>
              <StatusBadge status={job.status} />
              <small>{job.trigger} · {formatDuration(job.started_at, job.finished_at)}</small>
            </button>
          )) : <p className="muted">还没有 attempt</p>}
        </div>
        <div className="stack">
          <div className="grid-2 tight">
            <JsonView title="node param" value={run.param} defaultOpen={false} />
            <JsonView title="execution_policy" value={run.execution_policy} defaultOpen={false} />
          </div>
          {attempt ? (
            <>
              <PropertyList
                items={[
                  ['job', <code>{attempt.uuid}</code>],
                  ['status', <StatusBadge status={attempt.status} />],
                  ['started / finished', `${formatTime(attempt.started_at)} / ${formatTime(attempt.finished_at)}`],
                  ['edge_command_uuid', attempt.edge_command_uuid ?? '—'],
                  ['retry_of', attempt.retry_of_job_uuid ?? '—'],
                  ['uncertainty_reason', attempt.uncertainty_reason ?? '—'],
                ]}
              />
              <div className="grid-2 tight">
                <JsonView title="return_info" value={attempt.return_info} />
                <JsonView title="error_info" value={attempt.error_info} />
                <JsonView title="param" value={attempt.param} defaultOpen={false} />
                <JsonView title="feedback_data" value={attempt.feedback_data} defaultOpen={false} />
                <JsonView title="control_data" value={attempt.control_data} defaultOpen={false} />
                <JsonView title="error_resolution" value={attempt.error_resolution} defaultOpen={false} />
              </div>
            </>
          ) : null}
        </div>
      </div>
    </Panel>
  )
}

function ErrorDecisionsPanel({ decisions, error, onResolved }: { decisions: ErrorDecisionReport[]; error: unknown; onResolved: () => void }) {
  const [message, setMessage] = useState<{ ok: boolean; text: string }>()
  const resolve = useMutation({
    mutationFn: ({ decision, option }: { decision: ErrorDecisionReport; option: ErrorDecisionReport['options'][number] }) =>
      api.errorDecisions.resolve(decision.decision_id, {
        action: option.action,
        option: option as Record<string, unknown>,
        job_id: decision.job_id,
        device_id: decision.device_id ?? '',
        reason: '调试控制台',
      }),
    onSuccess: (result) => {
      setMessage({ ok: true, text: `${result.decision_id} → ${result.status}` })
      onResolved()
    },
    onError: (failure) => setMessage({ ok: false, text: describeError(failure) }),
  })
  return (
    <Panel className="section">
      <PanelHeader title="错误决策" description="GET /api/v1/error-decisions 中属于本任务的项；只提交服务端给出的 options[].action" />
      {error ? <Degraded error={error} title="错误决策不可用（Host 专属路由，Host 不在线时 503）" /> : null}
      {decisions.length ? decisions.map((decision) => (
        <div className="decision-card" key={decision.decision_id}>
          <div>
            <strong>{decision.device_id}.{decision.action_name}</strong>
            <span className="muted"> · job <Uuid value={decision.job_id} /> · {decision.category ?? ''} · 重试 {decision.retry_count ?? 0}/{decision.max_retries ?? '?'}</span>
            <p>{decision.exception_type ? `${decision.exception_type}: ` : ''}{decision.error_message}</p>
          </div>
          <div className="row-actions">
            {decision.options.map((option) => (
              <Button key={option.action} disabled={resolve.isPending} title={option.description} onClick={() => resolve.mutate({ decision, option })}>
                {option.label ?? option.action}
              </Button>
            ))}
          </div>
        </div>
      )) : !error ? <EmptyState title="没有待决策的失败" /> : null}
      {message ? <p className={`command-result pad ${message.ok ? 'ok' : 'bad'}`}>{message.text}</p> : null}
    </Panel>
  )
}

function ManualConfirmationsPanel({ items, error, onDecided }: { items: ManualConfirmation[]; error: unknown; onDecided: () => void }) {
  const [message, setMessage] = useState<{ ok: boolean; text: string }>()
  const decide = useMutation({
    mutationFn: ({ uuid, action }: { uuid: string; action: string }) =>
      api.tasks.decideManualConfirmation(uuid, { action, comment: '调试控制台', decision_idempotency_key: newIdempotencyKey('confirm') }),
    onSuccess: (result) => {
      setMessage({ ok: true, text: `${result.uuid} → ${result.status}` })
      onDecided()
    },
    onError: (failure) => setMessage({ ok: false, text: describeError(failure) }),
  })
  return (
    <Panel className="section">
      <PanelHeader title="人工确认" description="GET …/manual-confirmations；待确认的可以批准或拒绝" />
      {error ? <Degraded error={error} /> : items.length ? (
        <div className="table-scroll">
          <table className="data-table static">
            <thead><tr><th>uuid</th><th>job</th><th>状态</th><th>指派</th><th>打开</th><th>截止</th><th>param</th><th /></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.uuid}>
                  <td><Uuid value={item.uuid} /></td>
                  <td><Uuid value={item.workflow_node_job_uuid} /></td>
                  <td><StatusBadge status={item.status} /></td>
                  <td>{item.assignee_user_ids.join('、') || '不限'}</td>
                  <td>{formatTime(item.opened_at)}</td>
                  <td>{formatTime(item.deadline_at)}</td>
                  <td className="cell-json">{jsonPreview(item.param, 80)}</td>
                  <td>
                    {item.status === 'pending' ? (
                      <div className="row-actions">
                        <Button tone="primary" disabled={decide.isPending} onClick={() => decide.mutate({ uuid: item.uuid, action: 'approve' })}>批准</Button>
                        <Button tone="danger" disabled={decide.isPending} onClick={() => decide.mutate({ uuid: item.uuid, action: 'reject' })}>拒绝</Button>
                      </div>
                    ) : item.confirmed_by ?? ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EmptyState title="没有人工确认" />}
      {message ? <p className={`command-result pad ${message.ok ? 'ok' : 'bad'}`}>{message.text}</p> : null}
    </Panel>
  )
}
