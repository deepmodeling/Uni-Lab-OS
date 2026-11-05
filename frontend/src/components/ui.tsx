/** 基础 UI 部件（Panel / PageHeader / Button / StatusBadge / EmptyState 移植自 v1.0 components/ui.tsx）。 */
import { useState, type ButtonHTMLAttributes, type PropsWithChildren, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, Copy, RefreshCw, TriangleAlert } from 'lucide-react'
import { ApiError } from '../api'
import { shortId } from '../lib/format'
import { statusLabel, statusTone } from '../lib/taskStatus'

export function Panel({ className = '', children }: PropsWithChildren<{ className?: string }>) {
  return <section className={`panel ${className}`}>{children}</section>
}

export function PanelHeader({ title, description, action }: { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="panel-header">
      <div>
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {action ? <div className="panel-actions">{action}</div> : null}
    </div>
  )
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  )
}

export function Button({
  tone = 'secondary',
  icon,
  children,
  className = '',
  type = 'button',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'primary' | 'secondary' | 'ghost' | 'danger'; icon?: ReactNode }) {
  return (
    <button type={type} className={`button button-${tone} ${className}`} {...props}>
      {icon}
      {children !== undefined ? <span>{children}</span> : null}
    </button>
  )
}

export function RefreshButton({ onClick, busy }: { onClick: () => void; busy?: boolean }) {
  return (
    <Button icon={<RefreshCw size={15} className={busy ? 'spin' : ''} />} onClick={onClick}>
      刷新
    </Button>
  )
}

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  return <span className={`status-badge tone-${statusTone(status)}`} title={status ?? undefined}>{label ?? statusLabel(status)}</span>
}

export function EmptyState({ title, description }: { title: string; description?: ReactNode }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
    </div>
  )
}

/** 能力不可用 / 请求失败的说明条（不静默转圈）。 */
export function Degraded({ error, title, action }: { error: unknown; title?: string; action?: ReactNode }) {
  const message = describeError(error)
  return (
    <div className="degraded" role="status">
      <TriangleAlert size={16} />
      <div>
        <strong>{title ?? '读取失败'}</strong>
        <span>{message}</span>
      </div>
      {action}
    </div>
  )
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const where = `${error.method} ${error.path}`
    if (error.kind === 'business') return `${error.message}（业务码 ${error.code} · ${error.category}）· ${where}`
    if (error.kind === 'http') return `${error.message}（HTTP ${error.status}）· ${where}`
    return `${error.message} · ${where}`
  }
  return error instanceof Error ? error.message : String(error)
}

/** 可复制的短 uuid。 */
export function Uuid({ value, length = 8 }: { value: string | null | undefined; length?: number }) {
  if (!value) return <span className="muted">—</span>
  return (
    <code
      className="uuid"
      title={`${value}（点击复制）`}
      onClick={(event) => {
        event.stopPropagation()
        void navigator.clipboard?.writeText(value).catch(() => undefined)
      }}
    >
      {shortId(value, length)}
    </code>
  )
}

export function TaskLink({ uuid, length = 8 }: { uuid: string | null | undefined; length?: number }) {
  if (!uuid) return <span className="muted">—</span>
  return (
    <Link className="mono-link" to={`/tasks/${uuid}`} title={uuid} onClick={(event) => event.stopPropagation()}>
      {shortId(uuid, length)}
    </Link>
  )
}

/** 折叠 / 展开的 JSON 正文。 */
export function JsonView({
  value,
  title,
  defaultOpen = true,
  maxHeight = 360,
}: {
  value: unknown
  title?: ReactNode
  defaultOpen?: boolean
  maxHeight?: number
}) {
  const [open, setOpen] = useState(defaultOpen)
  const text = value === undefined ? 'undefined' : JSON.stringify(value, null, 2)
  return (
    <div className="json-view">
      {title !== undefined ? (
        <div className="json-view-head">
          <button type="button" className="json-toggle" onClick={() => setOpen((current) => !current)} aria-expanded={open}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {title}
          </button>
          <button
            type="button"
            className="json-copy"
            title="复制 JSON"
            onClick={() => void navigator.clipboard?.writeText(text).catch(() => undefined)}
          >
            <Copy size={13} />
          </button>
        </div>
      ) : null}
      {open ? <pre style={{ maxHeight }}>{text}</pre> : null}
    </div>
  )
}

export function PropertyList({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="property-list">
      {items.map(([label, value], index) => (
        <div key={index}>
          <dt>{label}</dt>
          <dd>{value ?? '—'}</dd>
        </div>
      ))}
    </dl>
  )
}

export function FilterTabs<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string; count?: number }[]
  onChange: (value: T) => void
}) {
  return (
    <div className="filter-tabs" role="tablist">
      {options.map((option) => (
        <button
          type="button"
          role="tab"
          key={option.value}
          aria-selected={value === option.value}
          className={value === option.value ? 'active' : ''}
          onClick={() => onChange(option.value)}
        >
          {option.label}
          {option.count !== undefined ? <span>{option.count}</span> : null}
        </button>
      ))}
    </div>
  )
}
