import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import { AppShell, type ConnectionMode } from './components/AppShell'
import { Degraded } from './components/ui'
import { EventsPage } from './pages/EventsPage'
import { MaterialsPage } from './pages/MaterialsPage'
import { OverviewPage } from './pages/OverviewPage'
import { SchedulerPage } from './pages/SchedulerPage'
import { TaskDetailPage } from './pages/TaskDetailPage'
import { TasksPage } from './pages/TasksPage'
import { WorkflowsPage } from './pages/WorkflowsPage'

export default function App() {
  // 连接状态：/health 恒 200，能读到即后端在线
  const health = useQuery({ queryKey: ['health'], queryFn: api.system.health, refetchInterval: 5000, retry: false })
  const running = useQuery({
    queryKey: ['task-count', 'running'],
    queryFn: () => api.tasks.list({ status: 'running', page_size: 1 }),
    refetchInterval: 5000,
    retry: false,
  })
  const connection: ConnectionMode = health.data ? 'connected' : health.error ? 'error' : 'loading'

  return (
    <AppShell connection={connection} activeTaskCount={running.data?.total ?? 0}>
      {health.error ? (
        <div className="connection-banner">
          <Degraded error={health.error} title="后端不可达：检查 unilab 是否在运行、UNILAB_BACKEND_URL 是否正确" />
        </div>
      ) : null}
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/tasks/:taskUuid" element={<TaskDetailPage />} />
        <Route path="/scheduler" element={<SchedulerPage />} />
        <Route path="/events" element={<EventsPage />} />
        <Route path="/materials" element={<MaterialsPage />} />
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/:workflowUuid" element={<WorkflowsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}
