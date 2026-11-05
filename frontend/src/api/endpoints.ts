/**
 * 控制台用到的全部路由。路径类型受生成的 `paths` 约束：快照里没有的路径编译不过；
 * 方法由 endpoints.test.ts 对着快照逐条核对。
 */
import type { paths } from './generated'
import { fillPath, type HttpMethod, type Transport } from './http'
import type {
  ErrorDecisionBody,
  ErrorDecisionReport,
  HealthStatus,
  HostlinkSnapshot,
  Intervention,
  ManualConfirmation,
  ManualConfirmationDecisionBody,
  MaterialAggregate,
  Page,
  ReadinessSummary,
  SchedulerResourceSnapshot,
  TaskCommandBody,
  TaskCreateBody,
  TaskInputEditBody,
  Workflow,
  WorkflowGraph,
  WorkflowNodeJob,
  WorkflowNodeRun,
  WorkflowPublication,
  WorkflowTask,
} from './types'

type Endpoint = readonly [HttpMethod, keyof paths]

export const ENDPOINTS = {
  health: ['GET', '/api/v1/health'],
  readiness: ['GET', '/api/v1/readiness'],
  hostlinkPeers: ['GET', '/api/v1/hostlink/peers'],
  schedulerResources: ['GET', '/api/v1/scheduler/resources'],
  errorDecisions: ['GET', '/api/v1/error-decisions'],
  resolveErrorDecision: ['POST', '/api/v1/error-decisions/{decision_id}'],
  events: ['GET', '/api/v1/events'],
  listWorkflows: ['GET', '/api/v1/workflows'],
  getWorkflow: ['GET', '/api/v1/workflows/{workflow_uuid}'],
  workflowGraph: ['GET', '/api/v1/workflows/{workflow_uuid}/graph'],
  workflowPublications: ['GET', '/api/v1/workflows/{workflow_uuid}/publications'],
  publishedWorkflows: ['GET', '/api/v1/workflow-publications'],
  listTasks: ['GET', '/api/v1/workflow-tasks'],
  createTask: ['POST', '/api/v1/workflow-tasks'],
  getTask: ['GET', '/api/v1/workflow-tasks/{task_uuid}'],
  commandTask: ['POST', '/api/v1/workflow-tasks/{task_uuid}/commands'],
  editTaskInput: ['PATCH', '/api/v1/workflow-tasks/{task_uuid}/input'],
  taskNodeRuns: ['GET', '/api/v1/workflow-tasks/{task_uuid}/node-runs'],
  taskJobs: ['GET', '/api/v1/workflow-tasks/{task_uuid}/jobs'],
  taskManualConfirmations: ['GET', '/api/v1/workflow-tasks/{task_uuid}/manual-confirmations'],
  taskInterventions: ['GET', '/api/v1/workflow-tasks/{task_uuid}/interventions'],
  decideManualConfirmation: ['POST', '/api/v1/workflow-manual-confirmations/{confirmation_uuid}/decision'],
  listMaterials: ['GET', '/api/v1/materials/instances'],
  getMaterial: ['GET', '/api/v1/materials/instances/{material_uuid}'],
  materialTree: ['GET', '/api/v1/materials/instances/{material_uuid}/tree'],
} as const satisfies Record<string, Endpoint>

export type EndpointName = keyof typeof ENDPOINTS

/** 生成类型里某个 GET 路由的查询参数。 */
type GetQuery<P extends keyof paths> = paths[P] extends { get: { parameters: { query?: infer Q } } } ? NonNullable<Q> : never

export type TaskListQuery = GetQuery<'/api/v1/workflow-tasks'>
export type WorkflowListQuery = GetQuery<'/api/v1/workflows'>
export type MaterialListQuery = GetQuery<'/api/v1/materials/instances'>

export function createApi(transport: Transport) {
  const call = <T>(
    name: EndpointName,
    options: { params?: Record<string, string>; query?: Record<string, string | number | boolean | null | undefined>; body?: unknown; acceptStatus?: number[] } = {},
  ) => {
    const [method, template] = ENDPOINTS[name]
    return transport.request<T>(fillPath(template, options.params), {
      method,
      query: options.query,
      body: options.body,
      acceptStatus: options.acceptStatus,
    })
  }

  return {
    transport,
    system: {
      health: () => call<HealthStatus>('health'),
      /** 503 = not_ready，正文同形，不当作失败。 */
      readiness: () => call<ReadinessSummary>('readiness', { acceptStatus: [503] }),
      hostlinkPeers: () => call<HostlinkSnapshot>('hostlinkPeers'),
    },
    scheduler: {
      /** 调度权威在远端时 503。 */
      resources: () => call<SchedulerResourceSnapshot>('schedulerResources'),
    },
    errorDecisions: {
      list: () => call<{ items: ErrorDecisionReport[] }>('errorDecisions'),
      resolve: (decisionId: string, body: ErrorDecisionBody) =>
        call<{ decision_id: string; status: string }>('resolveErrorDecision', { params: { decision_id: decisionId }, body }),
    },
    events: {
      url: () => transport.url(ENDPOINTS.events[1]),
    },
    workflows: {
      list: (query: WorkflowListQuery = {}) => call<Page<Workflow>>('listWorkflows', { query }),
      get: (uuid: string) => call<Workflow>('getWorkflow', { params: { workflow_uuid: uuid } }),
      graph: (uuid: string) => call<WorkflowGraph>('workflowGraph', { params: { workflow_uuid: uuid } }),
      publications: (uuid: string) =>
        call<WorkflowPublication[]>('workflowPublications', { params: { workflow_uuid: uuid } }),
      published: () => call<WorkflowPublication[]>('publishedWorkflows'),
    },
    tasks: {
      list: (query: TaskListQuery = {}) => call<Page<WorkflowTask>>('listTasks', { query }),
      create: (body: TaskCreateBody) => call<WorkflowTask>('createTask', { body }),
      get: (uuid: string) => call<WorkflowTask>('getTask', { params: { task_uuid: uuid } }),
      command: (uuid: string, body: TaskCommandBody) =>
        call<WorkflowTask>('commandTask', { params: { task_uuid: uuid }, body }),
      editInput: (uuid: string, body: TaskInputEditBody) =>
        call<WorkflowTask>('editTaskInput', { params: { task_uuid: uuid }, body }),
      nodeRuns: (uuid: string) => call<WorkflowNodeRun[]>('taskNodeRuns', { params: { task_uuid: uuid } }),
      jobs: (uuid: string) => call<WorkflowNodeJob[]>('taskJobs', { params: { task_uuid: uuid } }),
      manualConfirmations: (uuid: string) =>
        call<ManualConfirmation[]>('taskManualConfirmations', { params: { task_uuid: uuid } }),
      interventions: (uuid: string) => call<Intervention[]>('taskInterventions', { params: { task_uuid: uuid } }),
      decideManualConfirmation: (confirmationUuid: string, body: ManualConfirmationDecisionBody) =>
        call<ManualConfirmation>('decideManualConfirmation', { params: { confirmation_uuid: confirmationUuid }, body }),
    },
    materials: {
      list: (query: MaterialListQuery = {}) => call<MaterialAggregate[]>('listMaterials', { query }),
      get: (uuid: string) => call<MaterialAggregate>('getMaterial', { params: { material_uuid: uuid } }),
      tree: (uuid: string) => call<unknown>('materialTree', { params: { material_uuid: uuid } }),
    },
  }
}

export type Api = ReturnType<typeof createApi>
