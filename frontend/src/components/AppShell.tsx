/** 控制台外壳（移植自 v1.0 components/AppShell.tsx）：侧栏导航、连接卡片、顶栏。 */
import type { PropsWithChildren } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import {
  Boxes,
  BookOpen,
  FileJson,
  FlaskConical,
  LayoutDashboard,
  ListChecks,
  LockKeyhole,
  Radio,
  RadioTower,
  Workflow,
} from 'lucide-react'

export type ConnectionMode = 'loading' | 'connected' | 'error'

export const NAVIGATION = [
  { to: '/', label: '总览', icon: LayoutDashboard },
  { to: '/tasks', label: '任务', icon: ListChecks },
  { to: '/scheduler', label: '调度锁', icon: LockKeyhole },
  { to: '/events', label: '事件流', icon: RadioTower },
  { to: '/materials', label: '物料', icon: Boxes },
  { to: '/workflows', label: '工作流', icon: Workflow },
] as const

const connectionLabels: Record<ConnectionMode, string> = {
  loading: '正在连接',
  connected: '后端在线',
  error: '后端不可达',
}

export function pageLabel(pathname: string): string {
  const match = [...NAVIGATION]
    .sort((left, right) => right.to.length - left.to.length)
    .find((item) => (item.to === '/' ? pathname === '/' : pathname === item.to || pathname.startsWith(`${item.to}/`)))
  return match?.label ?? '总览'
}

export function AppShell({
  connection,
  activeTaskCount,
  children,
}: PropsWithChildren<{ connection: ConnectionMode; activeTaskCount: number }>) {
  const location = useLocation()
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <span className="brand-mark"><FlaskConical size={22} /></span>
          <div>
            <strong>Uni-Lab OS</strong>
            <span>调试控制台</span>
          </div>
        </div>

        <nav aria-label="主导航">
          <span className="nav-label">调试</span>
          {NAVIGATION.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
              <Icon size={19} />
              <span>{label}</span>
              {to === '/tasks' && activeTaskCount > 0 ? <em>{activeTaskCount}</em> : null}
            </NavLink>
          ))}
          <span className="nav-label nav-label-gap">接口</span>
          <a className="nav-item" href="/api/docs" target="_blank" rel="noreferrer">
            <BookOpen size={19} />
            <span>Swagger</span>
          </a>
          <a className="nav-item" href="/api/openapi.json" target="_blank" rel="noreferrer">
            <FileJson size={19} />
            <span>OpenAPI JSON</span>
          </a>
        </nav>

        <div className={`edge-card edge-${connection}`}>
          <div className="edge-card-head">
            <Radio size={17} />
            <span>{connectionLabels[connection]}</span>
          </div>
          <strong>开发代理 /api →</strong>
          <small title={__UNILAB_BACKEND_URL__}>{__UNILAB_BACKEND_URL__}</small>
          <div className="edge-meter"><span /></div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="breadcrumb">
            <span>调试控制台</span>
            <i>/</i>
            <strong>{pageLabel(location.pathname)}</strong>
          </div>
          <div className="topbar-actions">
            <span className="topbar-note">推荐界面是 OpenLab；这里只做调试</span>
            <span className={`live-chip live-chip-${connection}`}>
              <i />
              {connectionLabels[connection]}
            </span>
          </div>
        </header>
        <main className="page-content">
          <div className="page">{children}</div>
        </main>
      </div>
    </div>
  )
}
