/** 工作流：列表 / 已发布目录、DAG、节点、发布记录与「提交任务」。 */
import { useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Search } from 'lucide-react'
import { api } from '../api'
import type { JsonObject, RunMode, RunPreflight, WorkflowGraph } from '../api/types'
import { WorkflowDag } from '../components/WorkflowDag'
import {
  Button,
  Degraded,
  EmptyState,
  FilterTabs,
  JsonView,
  PageHeader,
  Panel,
  PanelHeader,
  PropertyList,
  RefreshButton,
  Uuid,
  describeError,
} from '../components/ui'
import { formatTime, jsonPreview, newIdempotencyKey } from '../lib/format'
import { toDagModel } from '../lib/workflowGraph'

type ListTab = 'all' | 'published'
type DetailTab = 'graph' | 'nodes' | 'publications' | 'run'

export function WorkflowsPage() {
  const { workflowUuid } = useParams()
  const navigate = useNavigate()
  const [tab, setTab] = useState<ListTab>('all')
  const [name, setName] = useState('')
  const workflows = useQuery({
    queryKey: ['workflows', name],
    queryFn: () => api.workflows.list({ page: 1, page_size: 200, name }),
    placeholderData: keepPreviousData,
  })
  const published = useQuery({ queryKey: ['workflow-publications'], queryFn: api.workflows.published, enabled: tab === 'published' })

  return (
    <>
      <PageHeader
        eyebrow="WORKFLOWS"
        title="工作流"
        description="GET /api/v1/workflows 与 /workflows/{uuid}/graph；依赖来自 handle 连线与 execution_policy.depends_on。"
        actions={<RefreshButton onClick={() => void workflows.refetch()} busy={workflows.isFetching} />}
      />
      <div className="workflow-layout">
        <Panel className="workflow-list-panel">
          <div className="table-toolbar">
            <FilterTabs value={tab} onChange={setTab} options={[{ value: 'all', label: '全部', count: workflows.data?.total }, { value: 'published', label: '已发布' }]} />
          </div>
          {tab === 'all' ? (
            <>
              <label className="search-field">
                <Search size={15} />
                <input value={name} onChange={(event) => setName(event.target.value)} placeholder="按名称搜索" />
              </label>
              {workflows.error ? <Degraded error={workflows.error} /> : null}
              <div className="workflow-list">
                {workflows.data?.items.map((workflow) => (
                  <button
                    type="button"
                    key={workflow.uuid}
                    className={`workflow-list-item ${workflow.uuid === workflowUuid ? 'active' : ''}`}
                    onClick={() => navigate(`/workflows/${workflow.uuid}`)}
                  >
                    <strong>{workflow.name}</strong>
                    <small>rev {workflow.revision} · {formatTime(workflow.update_time)}</small>
                  </button>
                ))}
                {workflows.data && !workflows.data.items.length ? <EmptyState title="没有工作流" /> : null}
              </div>
            </>
          ) : (
            <div className="workflow-list">
              {published.error ? <Degraded error={published.error} /> : null}
              {published.data?.map((publication) => (
                <button
                  type="button"
                  key={publication.uuid}
                  className={`workflow-list-item ${publication.workflow_uuid === workflowUuid ? 'active' : ''}`}
                  onClick={() => navigate(`/workflows/${publication.workflow_uuid}`)}
                >
                  <strong>{publication.name}</strong>
                  <small>rev {publication.revision} · {publication.inputs.length} 输入 / {publication.outputs.length} 输出</small>
                </button>
              ))}
              {published.data && !published.data.length ? <EmptyState title="还没有发布" /> : null}
            </div>
          )}
        </Panel>
        {workflowUuid ? <WorkflowDetail key={workflowUuid} uuid={workflowUuid} /> : <Panel><EmptyState title="选择一个工作流" /></Panel>}
      </div>
    </>
  )
}

function WorkflowDetail({ uuid }: { uuid: string }) {
  const [tab, setTab] = useState<DetailTab>('graph')
  const graph = useQuery({ queryKey: ['workflow-graph', uuid], queryFn: () => api.workflows.graph(uuid) })
  const publications = useQuery({ queryKey: ['workflow-publications', uuid], queryFn: () => api.workflows.publications(uuid), enabled: tab === 'publications' })
  const dag = useMemo(() => toDagModel(graph.data ?? {}), [graph.data])
  const [selectedNode, setSelectedNode] = useState<string>()

  if (graph.error) return <Panel><Degraded error={graph.error} /></Panel>
  if (!graph.data) return <Panel><EmptyState title="正在读取工作流图" /></Panel>
  const { workflow } = graph.data
  const io = (workflow.meta_data?.unilab ?? {}) as JsonObject
  const node = graph.data.nodes.find((item) => item.uuid === selectedNode)

  return (
    <div className="stack">
      <Panel>
        <PanelHeader
          title={workflow.name}
          description={<><code>{workflow.uuid}</code> · revision {workflow.revision} · {workflow.description ?? ''}</>}
          action={<RefreshButton onClick={() => void graph.refetch()} busy={graph.isFetching} />}
        />
        <div className="table-toolbar">
          <FilterTabs
            value={tab}
            onChange={setTab}
            options={[
              { value: 'graph', label: '图' },
              { value: 'nodes', label: '节点', count: graph.data.nodes.length },
              { value: 'publications', label: '发布' },
              { value: 'run', label: '提交任务' },
            ]}
          />
          <span className="muted">tags: {jsonPreview(workflow.tags, 80) || '[]'}</span>
        </div>
        {tab === 'graph' ? (
          <>
            <WorkflowDag model={dag} selectedNodeUuid={selectedNode} onSelectNode={setSelectedNode} />
            {node ? (
              <div className="panel-body stack">
                <PropertyList
                  items={[
                    ['节点', `${node.name} (${node.type})`],
                    ['uuid', <code>{node.uuid}</code>],
                    ['action', node.action_name ?? '—'],
                    ['parent', <Uuid value={node.parent_uuid} length={36} />],
                  ]}
                />
                <div className="grid-2 tight">
                  <JsonView title="param" value={node.param} />
                  <JsonView title="execution_policy" value={node.execution_policy} />
                </div>
              </div>
            ) : null}
            {Object.keys(io).length ? <div className="panel-body"><JsonView title="meta_data.unilab（入口参数 / 输出合同）" value={io} defaultOpen={false} /></div> : null}
          </>
        ) : null}
        {tab === 'nodes' ? <NodesTable graph={graph.data} /> : null}
        {tab === 'publications' ? (
          publications.error ? <Degraded error={publications.error} /> : publications.data?.length ? (
            <div className="table-scroll">
              <table className="data-table static">
                <thead><tr><th>revision</th><th>名称</th><th>contract_digest</th><th>source_hash</th><th>输入</th><th>输出</th><th>时间</th></tr></thead>
                <tbody>
                  {publications.data.map((publication) => (
                    <tr key={publication.uuid}>
                      <td>{publication.revision}</td>
                      <td>{publication.name}</td>
                      <td><code title={publication.contract_digest}>{publication.contract_digest.slice(0, 19)}…</code></td>
                      <td><code title={publication.source_hash}>{publication.source_hash.slice(0, 19)}…</code></td>
                      <td>{publication.inputs.map((input) => input.name).join('、') || '—'}</td>
                      <td>{publication.outputs.map((output) => output.name).join('、') || '—'}</td>
                      <td>{formatTime(publication.create_time)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <EmptyState title="这个工作流还没有发布" description="发布 = POST /workflows/{uuid}/publications（当前 revision）；这里只读。" />
        ) : null}
        {tab === 'run' ? <RunForm workflowUuid={workflow.uuid} revision={workflow.revision} /> : null}
      </Panel>
    </div>
  )
}

function NodesTable({ graph }: { graph: WorkflowGraph }) {
  const names = new Map(graph.nodes.map((node) => [node.uuid, node.name]))
  return (
    <div className="table-scroll">
      <table className="data-table static">
        <thead><tr><th>#</th><th>名称</th><th>type</th><th>action</th><th>parent</th><th>depends_on</th><th>param</th></tr></thead>
        <tbody>
          {graph.nodes.map((node, index) => {
            const depends = (node.execution_policy?.depends_on ?? []) as string[]
            return (
              <tr key={node.uuid}>
                <td>{index}</td>
                <td><span className="cell-title">{node.name}</span><small className="cell-sub"><Uuid value={node.uuid} /></small></td>
                <td>{node.type}</td>
                <td>{node.action_name ?? '—'}</td>
                <td>{node.parent_uuid ? names.get(node.parent_uuid) ?? node.parent_uuid : '—'}</td>
                <td>{Array.isArray(depends) && depends.length ? depends.map((uuid) => names.get(uuid) ?? uuid).join('、') : '—'}</td>
                <td className="cell-json">{jsonPreview(node.param, 100)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function RunForm({ workflowUuid, revision }: { workflowUuid: string; revision: number }) {
  const navigate = useNavigate()
  const [runMode, setRunMode] = useState<RunMode>('normal')
  const [input, setInput] = useState('{}')
  const [error, setError] = useState<string>()
  const [report, setReport] = useState<RunPreflight>()
  const check = async (value: JsonObject) => {
    const result = await api.workflows.preflight(workflowUuid, { run_mode: runMode, input: value, expected_revision: revision })
    setReport(result)
    return result
  }
  const preview = useMutation({ mutationFn: check, onError: (failure) => setError(describeError(failure)) })
  const create = useMutation({
    mutationFn: async (value: JsonObject) => {
      const result = await check(value)
      if (!result.can_run) throw new Error('预检发现阻塞项，请处理后重试。')
      return api.tasks.create({
        workflow_uuid: workflowUuid,
        expected_revision: result.workflow_revision,
        run_mode: runMode,
        input: value,
        idempotency_key: newIdempotencyKey('run'),
        description: '调试控制台提交',
      })
    },
    onSuccess: (task) => navigate(`/tasks/${task.uuid}`),
    onError: (failure) => setError(describeError(failure)),
  })
  const submit = (previewOnly = false) => {
    let value: unknown
    try {
      value = JSON.parse(input || '{}')
    } catch (failure) {
      setError(`input 不是合法 JSON：${failure instanceof Error ? failure.message : String(failure)}`)
      return
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      setError('input 必须是 JSON 对象')
      return
    }
    setError(undefined)
    if (previewOnly) preview.mutate(value as JsonObject)
    else create.mutate(value as JsonObject)
  }
  return (
    <div className="panel-body stack">
      <p className="muted">POST /api/v1/workflow-tasks（execution_kind=workflow）。step 模式提交后在任务页逐个放行；没有入口参数合同的工作流只接受 {'{}'}。</p>
      <label className="inline-field">
        <span>run_mode</span>
        <select disabled={create.isPending || preview.isPending} value={runMode} onChange={(event) => { setRunMode(event.target.value as RunMode); setReport(undefined) }}>
          <option value="normal">normal（自动）</option>
          <option value="step">step（单步）</option>
        </select>
      </label>
      <label className="field-block">
        <span>input（JSON 对象）</span>
        <textarea disabled={create.isPending || preview.isPending} className="json-editor" rows={6} value={input} onChange={(event) => { setInput(event.target.value); setReport(undefined) }} spellCheck={false} />
      </label>
      <div className="row-actions">
        <Button disabled={create.isPending || preview.isPending} onClick={() => submit(true)}>检查运行条件</Button>
        <Button tone="primary" disabled={create.isPending || preview.isPending} onClick={() => submit()}>检查并提交任务</Button>
        <Link className="link-button" to={`/tasks?workflow=${workflowUuid}`}>查看这个工作流的任务</Link>
      </div>
      {report ? <div className="stack" aria-live="polite">
        <strong>{({ invalid: '配置有误', temporarily_unavailable: '当前暂不可运行', runnable_now: '当前检查通过' })[report.status]}</strong>
        <p className="muted">检查不预留库存或设备，派发时还会重新校验。</p>
        <ul>{report.checks.map((item, index) => <li key={index} className={item.blocking ? 'command-result bad' : ''}>
          {item.status === 'deferred' ? '运行时检查：' : ''}{item.message}
        </li>)}</ul>
        <JsonView title="预检详情" value={report} defaultOpen={false} />
      </div> : null}
      {error ? <p className="command-result bad">{error}</p> : null}
    </div>
  )
}
