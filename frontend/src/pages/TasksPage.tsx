/** 任务列表：按状态过滤、分页，每 3 秒刷新。 */
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { api } from '../api'
import { Button, Degraded, EmptyState, FilterTabs, PageHeader, Panel, RefreshButton, StatusBadge, Uuid } from '../components/ui'
import { formatDuration, formatTime } from '../lib/format'
import { TASK_STATUSES, statusLabel } from '../lib/taskStatus'

const PAGE_SIZE = 20

export function TasksPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const status = params.get('status') ?? ''
  const workflowUuid = params.get('workflow') ?? ''
  const page = Math.max(1, Number(params.get('page')) || 1)
  const query = { status, page, page_size: PAGE_SIZE, workflow_uuid: workflowUuid || undefined }
  const tasks = useQuery({
    queryKey: ['tasks', query],
    queryFn: () => api.tasks.list(query),
    refetchInterval: 3000,
    placeholderData: keepPreviousData,
  })

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setParams(next, { replace: true })
  }
  const total = tasks.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader
        eyebrow="TASKS"
        title="任务"
        description="GET /api/v1/workflow-tasks：列表投影不含 output 与 control_revision，进入详情读取完整任务。"
        actions={<RefreshButton onClick={() => void tasks.refetch()} busy={tasks.isFetching} />}
      />
      <Panel>
        <div className="table-toolbar">
          <FilterTabs
            value={status}
            onChange={(value) => update({ status: value, page: '' })}
            options={[{ value: '', label: '全部' }, ...TASK_STATUSES.map((value) => ({ value, label: statusLabel(value) }))]}
          />
          <label className="inline-field">
            <span>workflow_uuid</span>
            <input
              value={workflowUuid}
              placeholder="可选，精确匹配"
              onChange={(event) => update({ workflow: event.target.value.trim(), page: '' })}
            />
          </label>
        </div>
        {tasks.error ? <Degraded error={tasks.error} /> : null}
        {tasks.data && !tasks.data.items.length ? <EmptyState title="没有任务" description={status ? `没有状态为 ${status} 的任务` : '还没有提交过任务'} /> : null}
        {tasks.data?.items.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>工作流</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>控制</th>
                  <th>模式</th>
                  <th>清理</th>
                  <th>创建</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                {tasks.data.items.map((task) => (
                  <tr key={task.uuid} onClick={() => navigate(`/tasks/${task.uuid}`)}>
                    <td><Uuid value={task.uuid} /></td>
                    <td>
                      <span className="cell-title">{task.workflow_snapshot?.workflow?.name ?? (task.execution_kind === 'workflow' ? '—' : '单点动作')}</span>
                      {task.description ? <small className="cell-sub">{task.description}</small> : null}
                    </td>
                    <td>{task.execution_kind}</td>
                    <td><StatusBadge status={task.status} /></td>
                    <td><StatusBadge status={task.control_status} /></td>
                    <td>{task.run_mode}</td>
                    <td>{task.cleanup_status}</td>
                    <td>{formatTime(task.create_time)}</td>
                    <td>{formatDuration(task.started_at, task.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <div className="pager">
          <span>共 {total} 条 · 第 {page} / {pages} 页</span>
          <Button icon={<ChevronLeft size={15} />} disabled={page <= 1} onClick={() => update({ page: String(page - 1) })}>上一页</Button>
          <Button icon={<ChevronRight size={15} />} disabled={page >= pages} onClick={() => update({ page: String(page + 1) })}>下一页</Button>
        </div>
      </Panel>
    </>
  )
}
