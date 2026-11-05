/** 调度锁：持有的锁、等待请求（含 blockers）、持有中的请求与交接，自动刷新。 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api'
import type { LockIdentifier } from '../api/types'
import { Degraded, EmptyState, PageHeader, Panel, PanelHeader, RefreshButton, StatusBadge, TaskLink, Uuid } from '../components/ui'
import { materialLabel, materialUuid } from '../lib/materialTree'
import { lockKey, lockLabel, shapeScheduler } from '../lib/scheduler'

export function SchedulerPage() {
  const [auto, setAuto] = useState(true)
  const [intervalMs, setIntervalMs] = useState(1500)
  const snapshot = useQuery({
    queryKey: ['scheduler-resources'],
    queryFn: api.scheduler.resources,
    refetchInterval: auto ? intervalMs : false,
    retry: false,
  })
  // 物料锁只给 uuid；用物料列表补上名字（低频，失败不影响主视图）
  const materials = useQuery({ queryKey: ['materials'], queryFn: () => api.materials.list(), refetchInterval: 30000 })
  const materialNames = useMemo(
    () => new Map((materials.data ?? []).map((material) => [materialUuid(material), materialLabel(material).primary])),
    [materials.data],
  )
  const view = useMemo(() => (snapshot.data ? shapeScheduler(snapshot.data) : undefined), [snapshot.data])
  const remote = snapshot.error instanceof ApiError && snapshot.error.status === 503
  const label = (identifier: LockIdentifier) => lockLabel(identifier, materialNames)

  return (
    <>
      <PageHeader
        eyebrow="SCHEDULER"
        title="调度锁"
        description="GET /api/v1/scheduler/resources：动作锁 = (device_id, action_name)，物料锁 = material_uuid；blockers 是持有冲突锁的 owner。"
        actions={
          <>
            <label className="inline-field">
              <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
              <span>自动刷新</span>
            </label>
            <select value={intervalMs} onChange={(event) => setIntervalMs(Number(event.target.value))} disabled={!auto}>
              {[500, 1500, 3000, 10000].map((value) => <option key={value} value={value}>{value / 1000} s</option>)}
            </select>
            <RefreshButton onClick={() => void snapshot.refetch()} busy={snapshot.isFetching} />
          </>
        }
      />
      {snapshot.error ? (
        <Degraded
          error={snapshot.error}
          title={remote ? '调度权威不在本进程（scheduler=remote），这里没有锁快照' : '读取调度快照失败'}
        />
      ) : null}
      {view ? (
        <>
          <div className="summary-strip">
            <span>sequence <strong>{view.sequence}</strong></span>
            <span>持有的锁 <strong>{view.held.length}</strong></span>
            <span>等待请求 <strong>{view.waiting.length}</strong></span>
            <span>持有中的请求 <strong>{view.holding.length}</strong></span>
            <span>待完成交接 <strong>{view.pendingHandoffs.length}</strong></span>
            <span>已结束请求 <strong>{view.settledRequests}</strong></span>
          </div>

          <Panel className="section">
            <PanelHeader title="持有的锁" description="ownerships：按获得顺序" />
            {view.held.length ? (
              <div className="table-scroll">
                <table className="data-table static">
                  <thead><tr><th>锁</th><th>类型</th><th>owner</th><th>任务</th><th>获得序号</th><th>排队</th></tr></thead>
                  <tbody>
                    {view.held.map((row) => (
                      <tr key={row.key}>
                        <td><span className="cell-title">{label(row.ownership.identifier)}</span></td>
                        <td>{row.ownership.identifier.kind}</td>
                        <td><Uuid value={row.ownership.owner_uuid} /></td>
                        <td><TaskLink uuid={row.taskUuid} /></td>
                        <td>{row.ownership.acquired_sequence}</td>
                        <td>{row.waiters ? <span className="count-badge">{row.waiters}</span> : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <EmptyState title="没有锁被持有" />}
          </Panel>

          <Panel className="section">
            <PanelHeader title="等待中的请求" description="按提交顺序（FIFO）；blockers 展开为持有者与它挡住的锁" />
            {view.waiting.length ? (
              <div className="table-scroll">
                <table className="data-table static">
                  <thead><tr><th>请求</th><th>任务</th><th>要的锁</th><th>被谁挡住</th><th>序号</th></tr></thead>
                  <tbody>
                    {view.waiting.map(({ request, blockers, freeLocks }) => (
                      <tr key={request.request_uuid}>
                        <td><Uuid value={request.owner_uuid} />{request.release_requested ? <small className="cell-sub">已请求释放</small> : null}</td>
                        <td><TaskLink uuid={request.task_uuid} /></td>
                        <td>
                          {request.identifiers.map((identifier) => (
                            <div key={lockKey(identifier)} className={freeLocks.some((free) => lockKey(free) === lockKey(identifier)) ? 'muted' : ''}>
                              {label(identifier)}
                            </div>
                          ))}
                        </td>
                        <td>
                          {blockers.length ? blockers.map((blocker) => (
                            <div key={blocker.ownerUuid} className="blocker">
                              <Uuid value={blocker.ownerUuid} /> · 任务 <TaskLink uuid={blocker.taskUuid} />
                              {blocker.locks.length ? <small className="cell-sub">{blocker.locks.map(label).join('、')}</small> : null}
                            </div>
                          )) : <span className="muted">无（按队列顺序等待）</span>}
                        </td>
                        <td>{request.created_sequence}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <EmptyState title="没有等待中的请求" />}
          </Panel>

          <Panel className="section">
            <PanelHeader title="持有中的请求" description="status=held" />
            {view.holding.length ? (
              <div className="table-scroll">
                <table className="data-table static">
                  <thead><tr><th>owner</th><th>任务</th><th>锁</th><th>状态</th><th>序号</th></tr></thead>
                  <tbody>
                    {view.holding.map((request) => (
                      <tr key={request.request_uuid}>
                        <td><Uuid value={request.owner_uuid} /></td>
                        <td><TaskLink uuid={request.task_uuid} /></td>
                        <td>{request.identifiers.length ? request.identifiers.map(label).join('、') : <span className="muted">always_free（无锁）</span>}</td>
                        <td><StatusBadge status={request.status} />{request.release_requested ? <small className="cell-sub">已请求释放</small> : null}</td>
                        <td>{request.created_sequence} → {request.updated_sequence}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <EmptyState title="没有持有中的请求" />}
          </Panel>

          <Panel className="section">
            <PanelHeader title="交接" description="handoffs：pending 在前" />
            {view.pendingHandoffs.length + view.finishedHandoffs.length ? (
              <div className="table-scroll">
                <table className="data-table static">
                  <thead><tr><th>handoff</th><th>from → to</th><th>锁</th><th>状态</th><th>原因</th><th>Handle 证明</th></tr></thead>
                  <tbody>
                    {[...view.pendingHandoffs, ...view.finishedHandoffs].map((handoff) => (
                      <tr key={handoff.handoff_uuid}>
                        <td><Uuid value={handoff.handoff_uuid} /></td>
                        <td><Uuid value={handoff.from_owner_uuid} /> → <Uuid value={handoff.to_owner_uuid} /></td>
                        <td>{handoff.identifiers.map(label).join('、')}</td>
                        <td><StatusBadge status={handoff.status} /></td>
                        <td>{handoff.reason || '—'}</td>
                        <td>{handoff.material_handle_proofs?.length ?? 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <EmptyState title="没有交接" />}
          </Panel>
        </>
      ) : !snapshot.error ? <EmptyState title="正在读取调度快照" /> : null}
    </>
  )
}
