/** 事件流：`GET /api/v1/events` 实时查看，可过滤、暂停、从 0 回放。 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Pause, Play, Trash2 } from 'lucide-react'
import { api } from '../api'
import { openEventStream, type StreamStatus } from '../api/sse'
import { Button, EmptyState, PageHeader, Panel, StatusBadge, Uuid } from '../components/ui'
import {
  LOG_NOTICE_EVENT,
  STREAM_RESET_EVENT,
  countByType,
  matchesFilter,
  prependBounded,
  toEntry,
  type EventEntry,
} from '../lib/eventLog'
import { formatTime } from '../lib/format'

const LIMIT = 1000

const STATUS_TEXT: Record<StreamStatus, string> = {
  connecting: '正在连接',
  open: '已连接',
  reconnecting: '重连中',
  error: '连接失败，稍后重试',
  closed: '已关闭',
}

export function EventsPage() {
  const [entries, setEntries] = useState<EventEntry[]>([])
  const [held, setHeld] = useState<EventEntry[]>([])
  const [paused, setPaused] = useState(false)
  const [replay, setReplay] = useState(false)
  const [status, setStatus] = useState<{ value: StreamStatus; detail?: string }>({ value: 'connecting' })
  const [cursor, setCursor] = useState<string>()
  const [text, setText] = useState('')
  const [types, setTypes] = useState<Set<string>>(new Set())
  const [hideLogs, setHideLogs] = useState(true)
  const [expanded, setExpanded] = useState<number>()
  const pausedRef = useRef(paused)
  useEffect(() => {
    pausedRef.current = paused
  }, [paused])
  const seqRef = useRef(0)

  // replay 切换 = 以新的起点重连
  useEffect(() => {
    setEntries([])
    setHeld([])
    const close = openEventStream({
      url: api.events.url(),
      lastEventId: replay ? '0' : undefined,
      onStatus: (value, detail) => setStatus({ value, detail }),
      onCursor: setCursor,
      onMessage: (message) => {
        seqRef.current += 1
        const entry = toEntry(message, seqRef.current, Date.now())
        if (pausedRef.current) setHeld((current) => prependBounded(current, [entry], LIMIT))
        else setEntries((current) => prependBounded(current, [entry], LIMIT))
      },
    })
    return close
  }, [replay])

  const resume = () => {
    setEntries((current) => [...held, ...current].slice(0, LIMIT))
    setHeld([])
    setPaused(false)
  }
  const filter = useMemo(() => ({ types, text, hideLogNotices: hideLogs }), [types, text, hideLogs])
  const visible = useMemo(() => entries.filter((entry) => matchesFilter(entry, filter)), [entries, filter])
  const typeCounts = useMemo(() => countByType(entries), [entries])
  const toggleType = (type: string) =>
    setTypes((current) => {
      const next = new Set(current)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })

  return (
    <>
      <PageHeader
        eyebrow="EVENTS"
        title="事件流"
        description="SSE /api/v1/events：通知只表示「某个聚合变了」，正文要回 REST 读取。持久通知带 id（序号），runtime.logs.changed 是瞬时通知。"
        actions={
          <>
            {paused ? (
              <Button tone="primary" icon={<Play size={15} />} onClick={resume}>继续（{held.length} 条待显示）</Button>
            ) : (
              <Button icon={<Pause size={15} />} onClick={() => setPaused(true)}>暂停</Button>
            )}
            <Button tone="ghost" icon={<Trash2 size={15} />} onClick={() => { setEntries([]); setHeld([]) }}>清空</Button>
          </>
        }
      />
      <div className="summary-strip">
        <span>连接 <StatusBadge status={status.value === 'open' ? 'ready' : status.value === 'error' ? 'failed' : 'pending'} label={STATUS_TEXT[status.value]} /></span>
        <span>游标 Last-Event-ID <strong>{cursor ?? '—'}</strong></span>
        <span>缓冲 <strong>{entries.length}</strong> / {LIMIT}</span>
        <span>显示 <strong>{visible.length}</strong></span>
        {status.detail ? <span className="muted">{status.detail}</span> : null}
      </div>
      <Panel>
        <div className="table-toolbar wrap">
          <div className="event-filters">
            <label className="inline-field grow">
              <span>过滤</span>
              <input value={text} onChange={(event) => setText(event.target.value)} placeholder="事件类型、uuid 或正文子串" />
            </label>
            <label className="inline-field">
              <input type="checkbox" checked={hideLogs} onChange={(event) => setHideLogs(event.target.checked)} />
              <span>隐藏 {LOG_NOTICE_EVENT}</span>
            </label>
            <label className="inline-field" title="以 Last-Event-ID: 0 重连，回放服务端保留的全部持久通知">
              <input type="checkbox" checked={replay} onChange={(event) => setReplay(event.target.checked)} />
              <span>从 0 回放</span>
            </label>
          </div>
          <div className="type-chips">
            {typeCounts.map(([type, count]) => (
              <button type="button" key={type} className={types.has(type) ? 'active' : ''} onClick={() => toggleType(type)}>
                {type} <span>{count}</span>
              </button>
            ))}
            {types.size ? <button type="button" className="link-button" onClick={() => setTypes(new Set())}>全部类型</button> : null}
          </div>
        </div>
        {visible.length ? (
          <div className="table-scroll events-scroll">
            <table className="data-table">
              <thead><tr><th>id</th><th>接收时间</th><th>事件</th><th>主体</th><th>正文</th></tr></thead>
              <tbody>
                {visible.map((entry) => (
                  <tr
                    key={entry.seq}
                    className={`${entry.event === STREAM_RESET_EVENT ? 'row-warn' : ''} ${expanded === entry.seq ? 'selected' : ''}`}
                    onClick={() => setExpanded((current) => (current === entry.seq ? undefined : entry.seq))}
                  >
                    <td><code>{entry.id ?? '·'}</code></td>
                    <td>{formatTime(entry.receivedAt)}</td>
                    <td><code className="event-type">{entry.event}</code></td>
                    <td>
                      {entry.subject?.kind === 'task' ? (
                        <Link className="mono-link" to={`/tasks/${entry.subject.uuid}`} onClick={(event) => event.stopPropagation()}>任务 {entry.subject.uuid.slice(0, 8)}</Link>
                      ) : entry.subject?.kind === 'workflow' ? (
                        <Link className="mono-link" to={`/workflows/${entry.subject.uuid}`} onClick={(event) => event.stopPropagation()}>工作流 {entry.subject.uuid.slice(0, 8)}</Link>
                      ) : entry.subject ? <>{entry.subject.kind} <Uuid value={entry.subject.uuid} /></> : '—'}
                    </td>
                    <td className="cell-json">
                      {expanded === entry.seq ? <pre>{JSON.stringify(entry.data, null, 2)}</pre> : entry.raw}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={entries.length ? '没有匹配的事件' : '还没有收到事件'}
            description={replay ? '已请求从 0 回放。' : '首连只推新通知；勾选「从 0 回放」可看保留的历史。提交一个任务就会有 workflow.task.changed。'}
          />
        )}
      </Panel>
    </>
  )
}
