/** 总览：就绪探针、存活检查、HostLink 对端与任务计数。 */
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Activity, Cpu, Network, ShieldCheck } from 'lucide-react'
import { api } from '../api'
import { Degraded, EmptyState, PageHeader, Panel, PanelHeader, PropertyList, RefreshButton, StatusBadge } from '../components/ui'
import { secondsAgo } from '../lib/format'
import { TASK_STATUSES, statusLabel } from '../lib/taskStatus'

const REFRESH_MS = 5000

export function OverviewPage() {
  const readiness = useQuery({ queryKey: ['readiness'], queryFn: api.system.readiness, refetchInterval: REFRESH_MS })
  const health = useQuery({ queryKey: ['health'], queryFn: api.system.health, refetchInterval: REFRESH_MS })
  const peers = useQuery({ queryKey: ['hostlink-peers'], queryFn: api.system.hostlinkPeers, refetchInterval: REFRESH_MS })
  const counts = useQuery({
    queryKey: ['task-counts'],
    queryFn: async () => {
      const totals = await Promise.all(
        TASK_STATUSES.map(async (status) => [status, (await api.tasks.list({ status, page_size: 1 })).total] as const),
      )
      return new Map<string, number>(totals)
    },
    refetchInterval: REFRESH_MS,
  })

  const refresh = () => {
    void readiness.refetch()
    void health.refetch()
    void peers.refetch()
    void counts.refetch()
  }
  const ready = readiness.data
  const onlinePeers = peers.data?.peers.filter((peer) => peer.online).length ?? 0

  return (
    <>
      <PageHeader
        eyebrow="OVERVIEW"
        title="总览"
        description="/readiness（能否接收并执行任务）、/health（进程存活）与 HostLink 对端；每 5 秒刷新。"
        actions={<RefreshButton onClick={refresh} busy={readiness.isFetching} />}
      />
      <div className="kpi-grid">
        <Kpi icon={<ShieldCheck size={20} />} label="就绪" value={ready ? statusLabel(ready.status) : '—'} hint={ready?.reasons.join('；') || ready?.execution} tone={ready?.status === 'ready' ? 'green' : 'amber'} />
        <Kpi icon={<Activity size={20} />} label="调度权威" value={health.data?.scheduler === 'local' ? '本进程' : health.data?.scheduler === 'remote' ? '远端' : '—'} hint={`scheduler=${health.data?.scheduler ?? '?'}`} tone="violet" />
        <Kpi icon={<Cpu size={20} />} label="执行面" value={health.data?.execution ?? '—'} hint="ready / restarting / disabled" tone="teal" />
        <Kpi icon={<Network size={20} />} label="HostLink" value={peers.data ? `${peers.data.role}` : '—'} hint={peers.data ? `${onlinePeers} / ${peers.data.peers.length} 个对端在线` : undefined} tone="blue" />
      </div>

      <div className="grid-2">
        <Panel>
          <PanelHeader title="就绪探针" description="GET /api/v1/readiness（503 = not_ready，正文同形）" />
          <div className="panel-body">
            {readiness.error ? <Degraded error={readiness.error} /> : ready ? (
              <PropertyList
                items={[
                  ['status', <StatusBadge status={ready.status} />],
                  ['scheduler', ready.scheduler],
                  ['execution', ready.execution],
                  ['reasons', ready.reasons.length ? ready.reasons.join('；') : '无'],
                ]}
              />
            ) : <EmptyState title="正在读取" />}
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="存活检查" description="GET /api/v1/health（进程在线即 200）" />
          <div className="panel-body">
            {health.error ? <Degraded error={health.error} /> : health.data ? (
              <PropertyList items={[['status', health.data.status], ['scheduler', health.data.scheduler], ['execution', health.data.execution]]} />
            ) : <EmptyState title="正在读取" />}
          </div>
        </Panel>
      </div>

      <Panel className="section">
        <PanelHeader title="任务计数" description="GET /api/v1/workflow-tasks?status=…（取 total）" />
        <div className="panel-body status-counts">
          {counts.error ? <Degraded error={counts.error} /> : TASK_STATUSES.map((status) => (
            <Link key={status} className="status-count" to={`/tasks?status=${status}`}>
              <StatusBadge status={status} />
              <strong>{counts.data?.get(status) ?? '—'}</strong>
            </Link>
          ))}
        </div>
      </Panel>

      <Panel className="section">
        <PanelHeader
          title="HostLink 对端"
          description={peers.data ? `role=${peers.data.role} · host_id=${peers.data.host_id ?? '—'} · protocol=${peers.data.protocol_version ?? '—'}` : 'GET /api/v1/hostlink/peers'}
        />
        {peers.error ? <Degraded error={peers.error} /> : peers.data ? (
          <>
            {peers.data.client ? (
              <div className="panel-body">
                <PropertyList
                  items={[
                    ['作为 Slave 连接', `${peers.data.client.host}:${peers.data.client.port}`],
                    ['online', String(peers.data.client.online)],
                    ['node_id', peers.data.client.node_id],
                    ['设备', peers.data.client.device_ids.join('、') || '—'],
                  ]}
                />
              </div>
            ) : null}
            {peers.data.peers.length ? (
              <div className="table-scroll">
                <table className="data-table static">
                  <thead>
                    <tr><th>node_id</th><th>在线</th><th>设备</th><th>最近心跳</th><th>其他</th></tr>
                  </thead>
                  <tbody>
                    {peers.data.peers.map((peer, index) => {
                      const { node_id, online, device_ids, last_seen, ...rest } = peer
                      return (
                        <tr key={node_id ?? index}>
                          <td><code>{node_id ?? '—'}</code></td>
                          <td><StatusBadge status={online ? 'ready' : 'not_ready'} label={online ? '在线' : '离线'} /></td>
                          <td>{device_ids?.join('、') || '—'}</td>
                          <td>{secondsAgo(last_seen)}</td>
                          <td className="cell-json">{JSON.stringify(rest)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="没有 HostLink 对端" description={peers.data.role === 'host' ? '本 Host 还没有 Slave 接入。' : `HostLink 角色：${peers.data.role}`} />
            )}
          </>
        ) : <EmptyState title="正在读取" />}
      </Panel>
    </>
  )
}

function Kpi({ icon, label, value, hint, tone }: { icon: ReactNode; label: string; value: string; hint?: string; tone: 'green' | 'amber' | 'violet' | 'teal' | 'blue' }) {
  return (
    <div className={`kpi-card kpi-${tone}`}>
      <span className="kpi-icon">{icon}</span>
      <div className="kpi-copy">
        <span>{label}</span>
        <strong>{value}</strong>
        {hint ? <small>{hint}</small> : null}
      </div>
    </div>
  )
}
