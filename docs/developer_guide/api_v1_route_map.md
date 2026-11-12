# V1.0 → unified dev HTTP API route map

Generated 2026-11-12 by statically reading dev (#360 + 98f8c0d1) and V1.0 (tag v1.0, 690491a3), with router prefixes resolved. Handler locations are relative to each tree. The status of each route is updated here as the migration lands; conventions are in api_v1_migration.md.

**Classes**

- SAME: path and DTO are compatible.
- PATH-DIFF: DEV has the capability under a different path or method. The DTO may differ too; see the note.
- DTO-DIFF: the path is the same but the request or response is incompatible.
- MISSING: DEV has no such capability. A "(partial)" counterpart is only the nearest analog.
- V1-DEPRECATED: V1 returns 410, marks the route legacy or hidden, or does not mount it in the product.
- n/a: UI hosting. Not relevant because the UI is the external uni-lab-fe.

"Used by the V1 console" means V1's in-tree `frontend/src` (not uni-lab-fe) calls the route. That is evidence the official FE does too, not proof. UNVERIFIED marks a guess.

**Counts** (244 V1 routes, including 4 Workspace Host routes, the console mount and one dead route): MISSING 115, DTO-DIFF 24, PATH-DIFF 79, SAME 7, V1-DEPRECATED 17, n/a 2

| Domain | MISSING | DTO-DIFF | PATH-DIFF | SAME | V1-DEPRECATED | n/a |
|---|---|---|---|---|---|---|
| Workflow authoring | 21 | 10 | 1 | 3 | 1 | 0 |
| Task lifecycle and observability | 21 | 7 | 9 | 0 | 7 | 0 |
| Inventory and materials | 41 | 1 | 35 | 0 | 1 | 0 |
| Devices and registry | 4 | 0 | 12 | 0 | 0 | 0 |
| Error handling | 17 | 2 | 7 | 0 | 0 | 0 |
| Workspace and lifecycle | 11 | 4 | 4 | 4 | 1 | 0 |
| Misc (edge transport, legacy pages) | 0 | 0 | 11 | 0 | 7 | 2 |


## Cross-cutting conventions

These matter more than any single route. Each item gives evidence from both trees.

**1. Response envelope.** Both sides mix conventions, and the split falls in different places.
- V1 has four styles:
  - **Backend envelope**, HTTP 200 even on error: success `{code:0,data}`, error `{code:N,error:{msg, code?, details?}}` (`app/workflow_api.py:915`, `:976`; `app/scheduler/inventory/backend_response.py:12-27`). It covers workflows, tasks, jobs, node templates, operation categories, authoring, resource-templates, materials, reagents, samples and package-mounts.
  - **Legacy `Resp`** `{code,data,message}` with codes 2001/2002 (`app/model.py:6-11,47`). It covers `/online-devices`, `/actions`, `/job/*`, `/resources`, `/file-browser` and `/authoring/device-catalog`.
  - **Plain DTO with HTTP status errors.** It covers `/inventory/*`, scheduler ops, `/device-state`, `/error-decisions`, `/edge/*` and `/lab/*`.
  - **`{code:0,data}` on success but FastAPI `{detail}` with 404/409/422/503 on error.** It covers `/stations/{id}/*` (`app/error_handling_api.py:160-178`).
- DEV:
  - Uses the same Backend envelope for workflow, graphs, chemicals and registry (`server/api/envelope.py:1-40`, `runtime/workflow.py:347-386`).
  - Returns **plain DTOs with HTTP 404/409/410/422/503** for `/materials/*`, `/runtime/*`, `/telemetry`, `/history`, `/lab`, diagnostics and driver-packages (`server/api/materials/core.py:84-99`).
- The V1 console's `unwrapEnvelope` requires `code===0 && data!==undefined` (`frontend/src/lib/edgeClient.ts:135`). So every DEV materials response fails in a V1 client even where the path matches.
- **Error body detail.** V1 adds a symbolic `error.code` for workflow_identity_mismatch, debug_not_allowed, debug_task_conflict, device_action_not_allowed and preflight_failed, plus `error.details` (`workflow_api.py:976-1000`). DEV sends only `error.msg` (`runtime/workflow.py:356-386`).

**2. Business code table.**
- V1:
  - Workflow: 0, 1, 1000 invalid, 1001 read_only_mode/debug_not_allowed/device_action_not_allowed, 3002 not_found, 3003 conflict, 5001 unavailable (`workflow_api.py:942`).
  - Resource contract: 1000, 2005, 4000 REAGENT_INFO_IN_USE, **4001 RESOURCE_NOT_FOUND**, 4002, 5000/5003/5004 template, 6000-6011 material/site (`scheduler/inventory/backend_contract.py:49-67`).
  - 4100 retired debug (410). Legacy 2001/2002.
- DEV: 0, 1, 1000, 3002, 3003, 5001, plus **4001 = unauthorized**, sent as HTTP 401 (`envelope.py:29-37`, `api/app.py:151`).
- **4001 means two different things.** DEV also never emits 1001, so V1 clients can't tell "read-only or debug disabled" apart from other errors.

**3. Auth, capabilities and CORS.**
- **Control token on every write.** DEV's `ControlTokenMiddleware` rejects every POST/PUT/PATCH/DELETE that lacks `X-UniLab-Token` or `Authorization: Bearer <control token>`, with HTTP 401 and code 4001 (`api/app.py:121-152`). V1 has no API token, only a host allow-list and a same-origin check (`app/web/console.py:101-126`). A V1-written FE therefore gets 401 on every write until it learns the token.
- **Two meanings for Bearer.** V1 `/station/workflow-invocations` expects `Bearer <EdgeControlConfig.api_key>` (`workflow_api.py:1740`), which collides with DEV's Bearer control token.
- **Capability gating.** V1 gates writes and debugging with `RuntimeCapabilities` (`can_edit_definitions`, `can_view_drafts`, `can_run_unpublished`, `can_debug`, `can_execute_device_actions`; `app/runtime_capabilities.py:18-27`, `workflow_api.py:278-310`) and publishes them in `/readiness.capabilities`. DEV has no such gate or projection.
- **CORS.** DEV's `allow_headers` is `Authorization, Content-Type, Accept, Last-Event-ID, X-UniLab-Token` (`api/app.py:110`). V1 also allows `Idempotency-Key`, `X-Workflow-Filename`, `trace_id`, `traceparent` and `tracestate` (`app/web/server.py:283-293`). A cross-origin V1 FE that sends `Idempotency-Key` (interventions, station decisions) or `X-Workflow-Filename` (Python import) fails the CORS preflight on DEV.

**4. Pagination.**
- Workflow lists are the same on both sides: `page`/`page_size`, returning `{items, has_more, page, page_size(, total)}` (V1 `workflow_api.py:1317`, DEV `runtime/workflow.py:579`).
- V1 elsewhere:
  - The resource contract and node templates also use page/page_size. `page=0`/`page_size=0` means "all", and the max is 100.
  - Edge `/inventory/*` uses `limit≤500` with named arrays (`{lots:[]}`, `{instances:[]}`).
  - Job feedback uses page/page_size.
- DEV elsewhere:
  - Runtime sub-collections (manual-confirmations, interventions, results, feedback-history) use `limit`/`offset` (`services/runtime/workflow/service.py:1453`).
  - `/materials/*` has **no pagination** and returns full lists.
  - Chemicals use page/page_size up to 500.
- The V1 console's `requestAllPages` loops with `page`/`page_size=100` until `has_more` is false (`edgeClient.ts:161-184`).

**5. ID field naming.**
- The workflow domain uses `uuid` as the primary key on both sides, with `<entity>_uuid` foreign keys. This is shared lineage.
- The materials domain diverges:
  - V1 Backend DTOs use `uuid`, `parent_uuid` and `resource_template_uuid` (`backend_contract.py:384-430`).
  - DEV uses prefixed primary keys: `material_uuid`, `template_uuid`, `lot_uuid`, `chemical_uuid`, `site_uuid` and `parent_material_uuid` (`protocol/materials.py:145-490`).
- V1 edge inventory uses `lot_id`, `template_id` and `edge_uuid`; V1 legacy uses `id`/`jobId`.
- DEV registry entries are keyed by `name`, driver packages by `name`, and processes by `process_id`.

**6. Timestamps.**
- Workflow: both sides use ISO-8601 `...Z` strings in `create_time`/`update_time` (V1 `workflow/store.py:92`, DEV `services/runtime/workflow/store.py:34`).
- V1 resource contract: ISO with milliseconds `.sssZ` (`backend_contract.py:71`).
- V1 edge inventory and layout: integer ms (`scheduler/inventory/layout.py:43`, `inventory/service.py:189`).
- DEV materials, runtime, telemetry, history, lab and chemicals: integer `*_at_ms` (`protocol/materials.py:164`, `protocol/runtime/lab.py:108`).
- DEV graphs: ISO (`services/materials/graph.py:483`).

**7. SSE.**
- `/api/v1/events` uses the same cursor protocol on both sides: `Last-Event-ID`, `stream.reset` with `cursor_ahead`/`cursor_expired`, and replay from 0 (V1 `workflow_api.py:2237`, DEV `runtime/events.py:98`).
- **The event names differ.** V1 emits `workflow.runtime.changed` (18 call sites) and `workflow.authoring.changed`. DEV emits `workflow.task.changed`, `workflow.node_run.changed`, `workflow.node_job.changed`, `workflow.manual_confirmation.changed`, `workflow.resource.changed` and `workflow.authoring.changed`, plus unnumbered log-notice events.
- A V1 client never sees DEV runtime invalidations.
- V1's `/monitor/events` (4 channels) has no DEV counterpart. DEV adds `/api/v1/materials/events`.

**8. HTTP status for create and commands.**
- V1 returns 201 for commands; DEV returns 200.
- For composite-invocations V1 returns 200 and DEV returns 201.
- V1 device-action-runs and intervention decisions return 201 when new and 200 on replay.
- V1 resolve-uncertain returns 202 while waiting for edge confirmation.

**9. Strictness.** Several DEV request models are `extra="forbid"` where V1's were `extra="ignore"`: task create, preflight and commands. Where DEV is lenient, V1-only fields are **silently dropped**: workflow create/update loses `workflow_type` and `operation_category_uuid`, and graph nodes lose `manual_confirmation`. Both failure modes are hard to diagnose from the FE.

## Conflicts: same path, different meaning

Adopting V1's contract on any of these breaks something DEV already relies on.

1. **`/api/v1/history/*`.** DEV owns it as the event and payload store (`api/history.py:42`). DEV's own clients use it (`client/history.py:70`, `backend/edge_control.py:103`), and the edge proxy forwards the whole prefix to the Host (`api/edge_proxy.py:23-31`). V1's `/history/workflows|jobs` would be proxied to the Host and 404 there. Adopting them needs a carve-out, or better, new paths.
2. **`GET /api/v1/lab/layout`.** In DEV it's a revisioned grid document `{layout_key, revision, cell_size, zones, walls}` with PUT/DELETE (`runtime/lab.py:21`). In V1 it's a read view `{zones, placements, storage_summary}` over placed instances, and zones and placements are separate resources (`scheduler/inventory/layout.py:447`).
3. **`POST /api/v1/resource-templates`.** In DEV this is the Registry Authority intake the Host calls at startup (`runtime/registry.py:52`). It versions entries and returns a registry report, with HTTP 400/422 on error. V1 returns an id→uuid identity receipt in the envelope. The request shape `{resources:[..]}` is compatible, so only the response needs care. DEV's `legacy_adaptor/sync/instances.py:186` already speaks V1's form as an upstream client.
4. **`/api/v1/error-decisions`.**
   - List: V1 `{decisions}` vs DEV `{items}`.
   - Resolve: V1 `delivered`/404 vs DEV `resolved`/409, and DEV defaults the action to `abort`.
   - DEV adds the physical-settlement restriction (`runtime/diagnostics.py:305-371` vs V1 `scheduler/api.py:492-520`).
5. **`POST /workflow-tasks/{uuid}/commands`.**
   - DEV requires `expected_revision` as a cross-tab guard, and its `pause` means "switch to step".
   - V1 has both `pause` and `switch_to_step`, plus `unlock_resources`, `target_node_uuid` and a 201 response.
   - Accepting V1 bodies means making `expected_revision` optional, which weakens DEV's guard, or having the FE send it.
6. **`POST /workflows/{uuid}/composite-invocations`.** V1 inserts a *contract* (`contract_uuid`, `device_bindings`, `param`). DEV inlines a published child (`child_workflow_uuid`, `child_revision`, `arguments`). DEV's MCP catalog and tests target DEV's shape.
7. **`POST /workflows/{uuid}/authoring/apply`.** V1 takes `{candidate_hash}`; DEV takes a triple CAS. Adopting V1 drops DEV's draft/revision guard unless the server derives those values.
8. **`PUT /workflows/{uuid}/graph`.** DEV defaults `site_binding_mode=resolve` for programmatic importers. A V1 FE omits the field, so it gets resolve, and can fail where it expects a plain save. Flipping the default breaks DEV's import scripts (UNVERIFIED which scripts).
9. **`GET /api/v1/events` names.** Renaming DEV events to V1's breaks DEV's own CLI `workflow watch` (`tests/app/test_workflow_watch_sse.py:33-144`). The safe fix is additive: also emit `workflow.runtime.changed`.
10. **`GET /api/v1/readiness` and `/health`.** DEV's readiness is a Kubernetes-style `ready|not_ready` contract. V1's status is `ready|starting|failed` with `capabilities` and `workflowProgress`. Adding V1's fields is safe; changing the `status` enum is not. The `/health` `scheduler` enums differ too (`ready|disabled` vs `local|remote`).
11. **Code 4001 and the Bearer header.** Both mean different things on each side (see conventions 2 and 3).
12. **The `/api/v1/materials` namespace.** V1's `GET /materials/{material_uuid}` takes a UUID-typed path parameter (`backend_api.py:316`). If it is registered before DEV's materials router, it captures `/materials/lots`, `/templates`, `/instances`, `/links`, `/changes`, `/events`, `/reservations`, `/executions` and `/registry-classes`. FastAPI does not fall through on a validation failure. So V1 material routes must be mounted after DEV's.
    - DEV's own `RemoteMaterialsClient` depends on DEV's plain-DTO shapes (`client/materials/core.py:404`).
    - DEV's `InventoryMutation` writes are their own convention (`protocol/materials.py:82`).
    - Therefore V1's shapes must be added alongside DEV's, not in place of them.

## DEV-only routes (brief)

- **Workflow:**
  - `/workflows/from-template`, `/workflows/template-catalog`, `/workflows/template-instances`.
  - `PUT /workflows/{uuid}/io`, `PUT /workflows/{uuid}/resource-scopes`.
  - `GET /workflows/{uuid}/publications`, `/workflow-publications`.
  - `PATCH /workflow-tasks/{uuid}/input`, `/workflow-tasks/{uuid}/node-runs`, `/workflow-node-runs/{uuid}`.
  - `/workflow-resource-intervals|loans|device-tenancies` (+ release, lend, return).
  - Manual confirmations: list, get and decide, by confirmation and per task.
  - `/workflow-tasks/{uuid}/interventions`, `/workflow-node-jobs/{uuid}/results|feedback-history|begin|started`.
- **Registry:** `/api/v1/registry/*` (12 routes: digest, entries, versions, apply/dismiss/restore, reports, pending-impacts, workflow-templates).
- **Materials:**
  - `/api/v1/materials/*` (42 routes: templates, lots, stocktake, reservations, executions, trees, instantiate, instances, links, snapshots, notify-device, changes, events, registry-classes, inventory/preview).
  - `/api/v1/chemicals` (5) and `/api/v1/graphs` (6).
- **Host data plane:** `/api/v1/runtime/*` (27), `/api/v1/telemetry/*` (6), `/api/v1/history/*` (7). All served on the Host and proxied by the authority.
- **Operations:**
  - `/api/v1/driver-packages/*` (14), `/api/v1/device-processes/*` (9), `/api/v1/hostlink/{logs,log-sources,material-sync,notify-device}`.
  - `/api/v1/restart` (3), `/api/v1/reset` (2), `/ping`, `/scheduler/resources`, `/scheduler/inventory-recovery` (2), `/scheduler/physical-recovery` (3), `/status-incidents` (2), `/api/v1/debug/databases` (2).
  - `PUT|DELETE /api/v1/lab/layout`.
- **Transport:** `/mcp`, `/edge/commands/{uuid}` (no `/api/v1`), `/api/v1/edge/http-responses/{uuid}`, `WS /api/v1/ws/schedule`, and the edge-proxy catch-alls.


## Workflow authoring

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `POST /api/v1/authoring/compile` | unilabos/app/workflow_authoring_transform.py:391 | - |  |
| MISSING | `POST /api/v1/authoring/generate-python` | unilabos/app/workflow_authoring_transform.py:412 | - |  |
| MISSING | `POST /api/v1/authoring/validate` | unilabos/app/workflow_authoring_transform.py:433 | - |  |
| MISSING | `GET /api/v1/experiment-operation-categories` | unilabos/app/operation_category_api.py:92 | - | Used by the V1 console |
| MISSING | `POST /api/v1/experiment-operation-categories` | unilabos/app/operation_category_api.py:109 | - |  |
| MISSING | `DELETE /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:176 | - |  |
| MISSING | `GET /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:135 | - |  |
| MISSING | `PUT /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:154 | - |  |
| MISSING | `POST /api/v1/local/workflows/import-python` | unilabos/app/workflow_api.py:1203 | (partial) POST /workflows + PUT /authoring/draft + POST /authoring/apply | Raw .py body with X-Workflow-Filename header. Used by the V1 console |
| MISSING | `DELETE /api/v1/workflow-edges/{edge_uuid}` | unilabos/app/workflow_api.py:1703 | - |  |
| MISSING | `DELETE /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1688 | - |  |
| MISSING | `GET /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1664 | - |  |
| MISSING | `PATCH /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1676 | - |  |
| MISSING | `POST /api/v1/workflow-nodes/{node_uuid}/duplicate` | unilabos/app/workflow_api.py:1693 | - |  |
| MISSING | `POST /api/v1/workflows/import` | unilabos/app/workflow_api.py:1183 | (different) POST /api/v1/workflows/from-template | Legacy JSON import. Used by the V1 console |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/batch-delete` | unilabos/app/workflow_api.py:1532 | - |  |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/duplicate` | unilabos/app/workflow_api.py:1549 | - |  |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/edges` | unilabos/app/workflow_api.py:1515 | (whole-graph PUT only) | Used by the V1 console |
| MISSING | `GET /api/v1/workflows/{workflow_uuid}/nodes` | unilabos/app/workflow_api.py:1494 | - |  |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/nodes` | unilabos/app/workflow_api.py:1477 | (whole-graph PUT only) | Used by the V1 console |
| MISSING | `GET /api/v1/workflows/{workflow_uuid}/referenced-by` | unilabos/app/workflow_api.py:1351 | - |  |
| DTO-DIFF | `GET /api/v1/workflows` | unilabos/app/workflow_api.py:1237 | GET /api/v1/workflows | F01. DEV has no workflow_type, status, operation_category_uuid or keyword filter and always returns workflow_type=workflow. V1 uses normal\|experiment_operation. The V1 console queries ?workflow_type=experiment_operation&status=published |
| DTO-DIFF | `POST /api/v1/workflows` | unilabos/app/workflow_api.py:1159 | POST /api/v1/workflows | DEV silently drops workflow_type and operation_category_uuid (extra=ignore). DEV accepts workflow_uuid |
| DTO-DIFF | `GET /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1405 | GET /api/v1/workflows/{workflow_uuid} | workflow_type value domain; DEV has no operation_category_uuid |
| DTO-DIFF | `PUT /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1420 | PUT /api/v1/workflows/{workflow_uuid} | Same as POST /workflows |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/authoring/apply` | unilabos/app/workflow_api.py:2218 | POST /api/v1/workflows/{workflow_uuid}/authoring/apply | V1 {candidate_hash} vs DEV {expected_draft_hash, expected_workflow_revision, expected_candidate_hash}. Both strict |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/composite-invocations` | unilabos/app/workflow_api.py:1597 | POST /api/v1/workflows/{workflow_uuid}/composite-invocations | V1 {revision, contract_uuid, invocation_uuid, device_bindings, pose, param} vs DEV {revision, child_workflow_uuid, child_revision, invocation_uuid, pose, arguments}. Both strict, so each rejects the other's body. V1 returns 200, DEV 201 |
| DTO-DIFF | `GET /api/v1/workflows/{workflow_uuid}/graph` | unilabos/app/workflow_api.py:1449 | GET /api/v1/workflows/{workflow_uuid}/graph | V1 returns {workflow(+status, category), nodes, edges, inventory_requirements} for the current layer only. DEV returns {workflow, nodes, edges, node_templates, handle_templates} |
| DTO-DIFF | `PUT /api/v1/workflows/{workflow_uuid}/graph` | unilabos/app/workflow_api.py:1461 | PUT /api/v1/workflows/{workflow_uuid}/graph | DEV defaults site_binding_mode to 'resolve' (its own doc says browsers must send 'preserve'), so a V1 client's save can fail with site_binding_invalid. DEV also drops node.manual_confirmation |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/publications` | unilabos/app/workflow_api.py:1563 | POST /api/v1/workflows/{workflow_uuid}/publications | Request is the same ({revision}). V1 returns a public contract whose contract_uuid feeds composite-invocations; DEV returns a publication record. Field-level diff UNVERIFIED |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/run-preflight` | unilabos/app/workflow_api.py:1639 | POST /api/v1/workflows/{workflow_uuid}/run-preflight | F02. DEV is strict, forbids inventory_bindings and restricts run_mode to a Literal |
| PATH-DIFF | `GET /api/v1/workflows/{workflow_uuid}/run-preflight` | unilabos/app/workflow_api.py:1618 | POST /api/v1/workflows/{workflow_uuid}/run-preflight | DEV has POST only |
| SAME | `DELETE /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1440 | DELETE /api/v1/workflows/{workflow_uuid} | Reference-integrity checks UNVERIFIED |
| SAME | `GET /api/v1/workflows/{workflow_uuid}/authoring` | unilabos/app/workflow_api.py:2199 | GET /api/v1/workflows/{workflow_uuid}/authoring | Payload equality UNVERIFIED |
| SAME | `PUT /api/v1/workflows/{workflow_uuid}/authoring/draft` | unilabos/app/workflow_api.py:2204 | PUT /api/v1/workflows/{workflow_uuid}/authoring/draft | Same strict DraftWriteRequest |
| V1-DEPRECATED | `GET /api/v1/published-workflow-contracts` | unilabos/app/workflow_api.py:1323 | GET /api/v1/workflow-publications | Hidden. V1 says new frontends must not use it, but the V1 console still calls it |

## Task lifecycle and observability

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/monitor/events` | unilabos/app/scheduler/api.py:265 | (partial) SSE /api/v1/events log notices; SSE /api/v1/materials/events | V1 has 4 channels (material, device, action, scheduler) |
| MISSING | `GET /api/v1/monitor/snapshot` | unilabos/app/scheduler/api.py:313 | (partial) GET /api/v1/scheduler/resources |  |
| MISSING | `GET /api/v1/reagents/{reagent_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1799 | - |  |
| MISSING | `POST /api/v1/reschedule` | unilabos/app/scheduler/api.py:231 | - | Manual re-dispatch |
| MISSING | `GET /api/v1/scheduler/time-plan` | unilabos/app/scheduler/api.py:258 | - | Used by the V1 console (timeScheduleClient) |
| MISSING | `POST /api/v1/station/workflow-invocations` | unilabos/app/workflow_api.py:1740 | (partial) POST /api/v1/workflow-tasks idempotency_key | Bearer EdgeControlConfig.api_key, which clashes with DEV's Bearer control token |
| MISSING | `GET /api/v1/timeline` | unilabos/app/scheduler/api.py:253 | - | Swimlane timeline |
| MISSING | `POST /api/v1/workflow-human-interactions/{interaction_uuid}/reply` | unilabos/app/workflow_api.py:2164 | - | Reply shapes: choice, text, values, x/y, corner_x/y. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-interventions/{intervention_uuid}` | unilabos/app/workflow_api.py:2180 | - |  |
| MISSING | `POST /api/v1/workflow-interventions/{intervention_uuid}/decisions` | unilabos/app/workflow_api.py:2184 | - | Idempotency-Key header; returns 201 or 200. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-runtime/wait-graph` | unilabos/app/workflow_api.py:2068 | - |  |
| MISSING | `GET /api/v1/workflow-task-presentations` | unilabos/app/workflow_api.py:1890 | - | F07. view=matrix. Used by the V1 console |
| MISSING | `DELETE /api/v1/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1940 | - | Soft delete. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/delete-eligibility` | unilabos/app/workflow_api.py:1926 | - | Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/events` | unilabos/app/workflow_api.py:2037 | (partial) GET /workflow-tasks/{uuid}/node-runs | after_sequence cursor |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/execution-locks` | unilabos/app/workflow_api.py:2010 | - | F07. DEV has an execution_lock_lease table but no API. Used by the V1 console |
| MISSING | `POST /api/v1/workflow-tasks/{task_uuid}/execution-locks/{lease_uuid}/force-release` | unilabos/app/workflow_api.py:2016 | (analog) /workflow-resource-intervals/{owner}/release; /workflow-device-tenancies/{uuid}/release | Fencing-token CAS and physical_settlement_confirmed. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/human-interactions` | unilabos/app/workflow_api.py:2160 | (partial) GET /workflow-tasks/{uuid}/manual-confirmations | Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1781 | (partial) GET /api/v1/materials/reservations?task_uuid= |  |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/material-events` | unilabos/app/workflow_api.py:1787 | - | Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/step-state` | unilabos/app/workflow_api.py:1954 | (partial) GET /workflow-tasks/{uuid}/node-runs | Used by the V1 console |
| DTO-DIFF | `GET /api/v1/events` | unilabos/app/workflow_api.py:2237 | GET /api/v1/events | Same cursor protocol (Last-Event-ID, stream.reset). Event names differ: V1 workflow.runtime.changed / workflow.authoring.changed; DEV workflow.task\|node_run\|node_job\|manual_confirmation\|resource.changed plus log-notice events. V1 clients miss every DEV runtime change |
| DTO-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}` | unilabos/app/workflow_api.py:2064 | GET /api/v1/workflow-node-jobs/{job_uuid} | F04 |
| DTO-DIFF | `GET /api/v1/workflow-tasks` | unilabos/app/workflow_api.py:1864 | GET /api/v1/workflow-tasks | DEV has no execution_kind filter and strips output from list items. Task DTO diff UNVERIFIED |
| DTO-DIFF | `POST /api/v1/workflow-tasks` | unilabos/app/workflow_api.py:1708 | POST /api/v1/workflow-tasks | F03. DEV is strict, so priority and inventory_bindings fail with 1000 (envelope, HTTP 200), not 422. V1 also checks capabilities (debug for step) |
| DTO-DIFF | `GET /api/v1/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1922 | GET /api/v1/workflow-tasks/{task_uuid} | DEV task has no priority or inventory_bindings (UNVERIFIED full diff) |
| DTO-DIFF | `POST /api/v1/workflow-tasks/{task_uuid}/commands` | unilabos/app/workflow_api.py:1961 | POST /api/v1/workflow-tasks/{task_uuid}/commands | DEV requires expected_revision, which V1 clients don't send, and forbids the target_node_uuid, description and meta_data that the V1 console sends. DEV has no switch_to_step or unlock_resources. V1 returns 201, DEV 200 |
| DTO-DIFF | `GET /api/v1/workflow-tasks/{task_uuid}/jobs` | unilabos/app/workflow_api.py:1982 | GET /api/v1/workflow-tasks/{task_uuid}/jobs | F04: no topological_index or executor_kind. DEV returns a flat attempt view |
| PATH-DIFF | `GET /api/v1/history/jobs` | unilabos/app/scheduler/api.py:410 | GET /api/v1/workflow-tasks/{uuid}/jobs; GET /api/v1/runtime/jobs | CONFLICT: same as the row above |
| PATH-DIFF | `GET /api/v1/history/workflows` | unilabos/app/scheduler/api.py:377 | GET /api/v1/workflow-tasks?status= | CONFLICT: DEV owns /api/v1/history/* (event and payload store) and the edge proxy forwards the whole prefix to the Host |
| PATH-DIFF | `GET /api/v1/history/workflows/{workflow_id}` | unilabos/app/scheduler/api.py:396 | GET /api/v1/workflow-tasks/{task_uuid} | CONFLICT: same as the row above |
| PATH-DIFF | `POST /api/v1/jobs/{job_id}/finish` | unilabos/app/scheduler/api.py:225 | POST /api/v1/runtime/jobs/{job_uuid}/transitions | Internal |
| PATH-DIFF | `GET /api/v1/workflow-interventions` | unilabos/app/workflow_api.py:2171 | GET /api/v1/workflow-tasks/{task_uuid}/interventions | DEV lists per task only; V1 is a global open list. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}/feedback` | unilabos/app/workflow_api.py:2074 | GET /api/v1/workflow-node-jobs/{job_uuid}/feedback-history | V1 paginates with page/page_size; DEV uses limit/offset |
| PATH-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1793 | GET /api/v1/materials/reservations/by-job/{job_uuid}; /materials/executions/{job_uuid} | UNVERIFIED |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/manual-confirmation` | unilabos/app/workflow_api.py:2134 | POST /api/v1/workflow-manual-confirmations/{confirmation_uuid}/decision | V1 keys on job_uuid, DEV on confirmation_uuid. DEV body adds confirmed_by, comment and decision_idempotency_key. V1 answers here with real HTTP 400/404/409. Used by the V1 console |
| PATH-DIFF | `PATCH /api/v1/workflow-tasks/{task_uuid}/jobs/{job_uuid}/param` | unilabos/app/workflow_api.py:1986 | PATCH /api/v1/workflow-tasks/{task_uuid}/input | Granularity differs: per-job param in V1, task entry input in DEV |
| V1-DEPRECATED | `POST /api/v1/debug/workflow-tasks` | unilabos/app/workflow_api.py:1805 | - | Returns 410 code 4100 |
| V1-DEPRECATED | `GET /api/v1/debug/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1822 | - | Returns 410 |
| V1-DEPRECATED | `POST /api/v1/debug/workflow-tasks/{task_uuid}/commands` | unilabos/app/workflow_api.py:1835 | - | Returns 410 |
| V1-DEPRECATED | `GET /api/v1/workflows` | unilabos/app/scheduler/api.py:204 | - | Not mounted in the product |
| V1-DEPRECATED | `POST /api/v1/workflows` | unilabos/app/scheduler/api.py:194 | (definition route at workflow.py:461) | Execution-shaped; not mounted in the product (include_execution_shaped_workflow_routes=False) |
| V1-DEPRECATED | `GET /api/v1/workflows/{workflow_id}` | unilabos/app/scheduler/api.py:208 | - | Not mounted in the product |
| V1-DEPRECATED | `POST /api/v1/workflows/{workflow_id}/cancel` | unilabos/app/scheduler/api.py:217 | POST /api/v1/workflow-tasks/{uuid}/commands type=cancel | Not mounted in the product |

## Inventory and materials

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/compounds/{cas}` | unilabos/app/scheduler/inventory/reagent_api.py:529 | (partial) GET /api/v1/chemicals?cas= (local data only) | V1 uses an external compound source (PubChem). Used by the V1 console |
| MISSING | `GET /api/v1/current-substances` | unilabos/app/scheduler/inventory/content_api.py:116 | - |  |
| MISSING | `POST /api/v1/current-substances` | unilabos/app/scheduler/inventory/content_api.py:106 | (partial) PUT /api/v1/materials/instances/{uuid}/data substances |  |
| MISSING | `DELETE /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:143 | - |  |
| MISSING | `GET /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:129 | - |  |
| MISSING | `PUT /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:133 | - |  |
| MISSING | `GET /api/v1/inventory/commands/processed` | unilabos/app/scheduler/inventory/api.py:200 | - | DEV has idempotency by command_uuid but no listing |
| MISSING | `GET /api/v1/inventory/contents` | unilabos/app/scheduler/inventory/api.py:169 | (partial) substances inside material data | Container contents |
| MISSING | `GET /api/v1/inventory/health` | unilabos/app/scheduler/inventory/api.py:55 | (trivial) GET /api/v1/health |  |
| MISSING | `POST /api/v1/inventory/ingress-reservations` | unilabos/app/scheduler/inventory/ingress_api.py:116 | - | Station ingress |
| MISSING | `GET /api/v1/inventory/ingress-reservations/{reservation_uuid}` | unilabos/app/scheduler/inventory/ingress_api.py:126 | - |  |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/cancel` | unilabos/app/scheduler/inventory/ingress_api.py:162 | - |  |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/in-transit` | unilabos/app/scheduler/inventory/ingress_api.py:136 | - |  |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/receive` | unilabos/app/scheduler/inventory/ingress_api.py:149 | - |  |
| MISSING | `GET /api/v1/inventory/sync/cursors` | unilabos/app/scheduler/inventory/api.py:210 | (partial) POST /api/v1/materials/changes/ack |  |
| MISSING | `GET /api/v1/material-models/{asset_path:path}` | unilabos/app/scheduler/inventory/backend_api.py:444 | - |  |
| MISSING | `GET /api/v1/material-shapes` | unilabos/app/scheduler/inventory/backend_api.py:435 | - | Material 3D or 2D shapes |
| MISSING | `GET /api/v1/material-states/{state_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:408 | - |  |
| MISSING | `POST /api/v1/materials/sites/resolve-identities` | unilabos/app/scheduler/inventory/backend_api.py:309 | - |  |
| MISSING | `GET /api/v1/materials/{material_uuid}/current-substance` | unilabos/app/scheduler/inventory/content_api.py:151 | (partial) GET /api/v1/materials/instances/{uuid} data |  |
| MISSING | `GET /api/v1/materials/{material_uuid}/reagent-history` | unilabos/app/scheduler/inventory/reagent_api.py:709 | - | Used by the V1 console |
| MISSING | `GET /api/v1/materials/{material_uuid}/states` | unilabos/app/scheduler/inventory/backend_api.py:389 | - | No state history |
| MISSING | `GET /api/v1/materials/{material_uuid}/substance-history` | unilabos/app/scheduler/inventory/content_api.py:155 | - |  |
| MISSING | `GET /api/v1/reagent-history/{history_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:705 | - |  |
| MISSING | `POST /api/v1/reagent-infos/batch` | unilabos/app/scheduler/inventory/reagent_api.py:560 | - |  |
| MISSING | `POST /api/v1/reagent-infos/import` | unilabos/app/scheduler/inventory/reagent_api.py:573 | - |  |
| MISSING | `GET /api/v1/reagent-infos/{reagent_info_uuid}/structure-3d` | unilabos/app/scheduler/inventory/reagent_api.py:600 | - |  |
| MISSING | `GET /api/v1/reagents` | unilabos/app/scheduler/inventory/reagent_api.py:664 | - | Used by the V1 console |
| MISSING | `POST /api/v1/reagents` | unilabos/app/scheduler/inventory/reagent_api.py:618 | (partial) lots or material data substances | F05. Used by the V1 console |
| MISSING | `POST /api/v1/reagents/batch` | unilabos/app/scheduler/inventory/reagent_api.py:628 | - |  |
| MISSING | `POST /api/v1/reagents/import` | unilabos/app/scheduler/inventory/reagent_api.py:641 | - |  |
| MISSING | `DELETE /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:701 | - |  |
| MISSING | `GET /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:687 | - |  |
| MISSING | `PUT /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:691 | - |  |
| MISSING | `GET /api/v1/samples` | unilabos/app/scheduler/inventory/content_api.py:69 | - |  |
| MISSING | `POST /api/v1/samples` | unilabos/app/scheduler/inventory/content_api.py:61 | - |  |
| MISSING | `DELETE /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:102 | - |  |
| MISSING | `GET /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:90 | - |  |
| MISSING | `PUT /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:94 | - |  |
| MISSING | `GET /api/v1/sites/{site_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:412 | - |  |
| MISSING | `GET /api/v1/substance-history/{history_uuid}` | unilabos/app/scheduler/inventory/content_api.py:147 | - |  |
| DTO-DIFF | `POST /api/v1/resource-templates` | unilabos/app/scheduler/inventory/backend_api.py:215 | POST /api/v1/resource-templates (registry.py:52) | CONFLICT. Both take {resources:[..]}. V1 returns an id→uuid identity receipt (envelope, code 1000 on error); DEV returns a Registry Authority report with HTTP 400/422 errors. The DEV Host depends on this route |
| PATH-DIFF | `POST /api/v1/inventory/commands` | unilabos/app/scheduler/inventory/api.py:59 | M<..> on /api/v1/materials/{templates,lots/inbound,lots/{uuid}/stocktake,reservations,reservations/{uuid}/consume\|release\|quarantine} | V1 command types: template.upsert/delete, inbound, reserve, release, consume, quarantine, reconcile_count. DEV covers them but as one route per operation. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/inventory/instances` | unilabos/app/scheduler/inventory/api.py:92 | GET /api/v1/materials/instances | No status filter in DEV. F05 says 'path missing'; the capability exists |
| PATH-DIFF | `GET /api/v1/inventory/instances/{edge_uuid}` | unilabos/app/scheduler/inventory/api.py:127 | GET /api/v1/materials/instances/{material_uuid} |  |
| PATH-DIFF | `GET /api/v1/inventory/ledger` | unilabos/app/scheduler/inventory/api.py:178 | GET /api/v1/materials/changes |  |
| PATH-DIFF | `GET /api/v1/inventory/lots` | unilabos/app/scheduler/inventory/api.py:75 | GET /api/v1/materials/lots | V1 returns {lots:[..]}; DEV returns a plain list |
| PATH-DIFF | `GET /api/v1/inventory/lots/{lot_id}` | unilabos/app/scheduler/inventory/api.py:81 | GET /api/v1/materials/lots/{lot_uuid} |  |
| PATH-DIFF | `GET /api/v1/inventory/outbox/backlog` | unilabos/app/scheduler/inventory/api.py:185 | GET /api/v1/runtime/backend-events | UNVERIFIED equivalence (upstream outbox) |
| PATH-DIFF | `GET /api/v1/inventory/outbox/events` | unilabos/app/scheduler/inventory/api.py:192 | GET /api/v1/runtime/backend-events | UNVERIFIED |
| PATH-DIFF | `GET /api/v1/inventory/relations` | unilabos/app/scheduler/inventory/api.py:165 | GET /api/v1/materials/links |  |
| PATH-DIFF | `GET /api/v1/inventory/reservations` | unilabos/app/scheduler/inventory/api.py:100 | GET /api/v1/materials/reservations |  |
| PATH-DIFF | `GET /api/v1/inventory/reservations/{reservation_id}` | unilabos/app/scheduler/inventory/api.py:142 | GET /api/v1/materials/reservations/{reservation_uuid} |  |
| PATH-DIFF | `GET /api/v1/inventory/snapshot` | unilabos/app/scheduler/inventory/api.py:173 | GET /api/v1/graphs/live/payload | UNVERIFIED shape |
| PATH-DIFF | `GET /api/v1/inventory/templates` | unilabos/app/scheduler/inventory/api.py:108 | GET /api/v1/materials/templates |  |
| PATH-DIFF | `GET /api/v1/inventory/templates/{template_id}` | unilabos/app/scheduler/inventory/api.py:114 | GET /api/v1/materials/templates/{template_uuid} |  |
| PATH-DIFF | `GET /api/v1/inventory/workflows/{workflow_id}/reservations` | unilabos/app/scheduler/inventory/api.py:155 | GET /api/v1/materials/reservations?task_uuid= |  |
| PATH-DIFF | `GET /api/v1/materials` | unilabos/app/scheduler/inventory/backend_api.py:286 | GET /api/v1/materials/instances | DEV has no pagination, no barcode filter and exact name match only. Used by the V1 console |
| PATH-DIFF | `POST /api/v1/materials` | unilabos/app/scheduler/inventory/backend_api.py:278 | POST /api/v1/materials/trees \| /instantiate (M<..>) | Direct body vs mutation envelope. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/materials/graph` | unilabos/app/scheduler/inventory/backend_api.py:305 | GET /api/v1/graphs/live/payload | Node-link shape is incompatible. F05 calls it 'missing'. Used by the V1 console |
| PATH-DIFF | `DELETE /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:343 | DELETE /api/v1/materials/instances/{material_uuid} (M<MaterialDelete>) |  |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:315 | GET /api/v1/materials/instances/{material_uuid} | Used by the V1 console |
| PATH-DIFF | `PUT /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:319 | PATCH /api/v1/materials/instances/{uuid}; PUT .../position; PUT .../data |  |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/ledger` | unilabos/app/scheduler/inventory/backend_api.py:354 | GET /api/v1/materials/changes (global, no per-material filter) | Used by the V1 console |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/sites` | unilabos/app/scheduler/inventory/backend_api.py:369 | GET /api/v1/materials/instances/{uuid} (aggregate includes sites, UNVERIFIED) |  |
| PATH-DIFF | `POST /api/v1/materials/{material_uuid}/states` | unilabos/app/scheduler/inventory/backend_api.py:378 | PUT /api/v1/materials/instances/{uuid}/data | Append in V1, overwrite in DEV |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/states/latest` | unilabos/app/scheduler/inventory/backend_api.py:404 | GET /api/v1/materials/instances/{uuid} (data) |  |
| PATH-DIFF | `GET /api/v1/reagent-infos` | unilabos/app/scheduler/inventory/reagent_api.py:541 | GET /api/v1/chemicals | Used by the V1 console |
| PATH-DIFF | `POST /api/v1/reagent-infos` | unilabos/app/scheduler/inventory/reagent_api.py:533 | POST /api/v1/chemicals | Fields are near-identical (cas, name, aliases, formula, smiles, inchi_key, mw, density, physical_state). DEV names the key chemical_uuid |
| PATH-DIFF | `DELETE /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:614 | DELETE /api/v1/chemicals/{chemical_uuid}?expected_version= | DEV requires expected_version |
| PATH-DIFF | `GET /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:596 | GET /api/v1/chemicals/{chemical_uuid} |  |
| PATH-DIFF | `PUT /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:604 | PATCH /api/v1/chemicals/{chemical_uuid} | Method differs; DEV requires expected_version |
| PATH-DIFF | `GET /api/v1/resource-templates` | unilabos/app/scheduler/inventory/backend_api.py:219 | GET /api/v1/materials/templates | DEV has no page, keyword or resource_type params, returns a plain list, and names the key template_uuid where V1 uses uuid. Used by the V1 console |
| PATH-DIFF | `DELETE /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:274 | DELETE /api/v1/materials/templates/{template_uuid} (InventoryMutation body) |  |
| PATH-DIFF | `GET /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:234 | GET /api/v1/materials/templates/{template_uuid} | Plain DTO vs V1 envelope |
| PATH-DIFF | `PUT /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:238 | PUT /api/v1/materials/templates/{template_uuid} (M<ResourceTemplateWrite>) | V1 edits only display fields |
| PATH-DIFF | `GET /api/v1/resources` | unilabos/app/web/api.py:1256 | GET /api/v1/materials/instances | Uses the legacy Resp envelope |
| V1-DEPRECATED | `POST /api/v1/edge/material/query` | unilabos/app/scheduler/inventory/api.py:234 | GET /api/v1/materials/instances/{uuid}/tree; /instances/by-resource-id/{id} | Compat route 'already used by HostNode' |

## Devices and registry

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/authoring/device-catalog` | unilabos/app/web/api.py:1278 | (partial) GET /api/v1/workflows/template-catalog devices; /registry/entries; /runtime/endpoints | F06. Used by the V1 console |
| MISSING | `GET /api/v1/devices` | unilabos/app/web/api.py:1301 | (partial) GET /api/v1/runtime/endpoints + /materials/instances | F05/F06. V1 joins the edge registration with device materials. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-node-templates` | unilabos/app/workflow_template_api.py:299 | (partial) GET /api/v1/workflow-publications; /registry/entries; graph.node_templates | F06. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-node-templates/{template_uuid}` | unilabos/app/workflow_template_api.py:326 | - | Used by the V1 console |
| PATH-DIFF | `GET /api/v1/actions` | unilabos/app/web/api.py:1383 | GET /api/v1/runtime/endpoints |  |
| PATH-DIFF | `POST /api/v1/device-action-runs` | unilabos/app/workflow_api.py:1851 | POST /api/v1/workflow-tasks execution_kind=ad_hoc_device_action | V1 identifies the action by material_uuid + workflow_node_template_uuid; DEV by device_id + action_name. V1 returns {created, ...} with 201 or 200. F05/F06. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/device-state` | unilabos/app/scheduler/api.py:442 | GET /api/v1/telemetry/states | Plain DTO on both sides |
| PATH-DIFF | `GET /api/v1/device-state/history` | unilabos/app/scheduler/api.py:448 | GET /api/v1/telemetry/events |  |
| PATH-DIFF | `POST /api/v1/device-state/report` | unilabos/app/scheduler/api.py:473 | POST /api/v1/telemetry/events |  |
| PATH-DIFF | `GET /api/v1/device-state/{device_id:path}` | unilabos/app/scheduler/api.py:464 | GET /api/v1/telemetry/states/{endpoint_uuid}/{device_uuid} | DEV also needs endpoint_uuid |
| PATH-DIFF | `GET /api/v1/device-state/{device_id:path}/history` | unilabos/app/scheduler/api.py:453 | GET /api/v1/telemetry/events (device filter UNVERIFIED) |  |
| PATH-DIFF | `GET /api/v1/devices/{device_id}/actions` | unilabos/app/web/api.py:1350 | GET /api/v1/runtime/endpoints (action_capabilities) |  |
| PATH-DIFF | `GET /api/v1/devices/{device_id}/actions/{action_name}/schema` | unilabos/app/web/api.py:1366 | GET /api/v1/registry/entries/{name} | Schema location UNVERIFIED |
| PATH-DIFF | `POST /api/v1/job/add` | unilabos/app/web/api.py:1403 | POST /api/v1/workflow-tasks {execution_kind:'ad_hoc_device_action', device_id, action_name, param} | Legacy Resp; still documented in V1 developer_guide/http_api.md |
| PATH-DIFF | `GET /api/v1/job/{id}/status` | unilabos/app/web/api.py:1396 | GET /api/v1/workflow-tasks/{task_uuid}; GET /api/v1/workflow-node-jobs/{uuid} | Legacy Resp; still documented in V1 developer_guide/http_api.md |
| PATH-DIFF | `GET /api/v1/online-devices` | unilabos/app/web/api.py:1337 | GET /api/v1/runtime/endpoints?state=online | Legacy Resp envelope in V1; Host data, proxied in DEV |

## Error handling

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/stations/{station_id}/control-commands/{command_uuid}` | unilabos/app/error_handling_api.py:335 | - | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/control-commands/{command_uuid}/apply` | unilabos/app/error_handling_api.py:341 | - | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/error-handling` | unilabos/app/error_handling_api.py:180 | (partial) GET /api/v1/error-decisions, /status-incidents, /scheduler/physical-recovery | No station snapshot with versions and error epoch. Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/events` | unilabos/app/error_handling_api.py:407 | (partial) SSE GET /api/v1/events | Station control-operation log |
| MISSING | `GET /api/v1/stations/{station_id}/manual-action-jobs/{job_uuid}/review` | unilabos/app/error_handling_api.py:316 | - | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-action-jobs/{job_uuid}/review` | unilabos/app/error_handling_api.py:322 | - | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/manual-actions/{command_id}` | unilabos/app/error_handling_api.py:275 | - | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-actions/{command_id}/review` | unilabos/app/error_handling_api.py:301 | - | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-actions/{command_id}/stop` | unilabos/app/error_handling_api.py:281 | (partial) POST /api/v1/workflow-tasks/{uuid}/commands type=cancel | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/resource-usage` | unilabos/app/error_handling_api.py:380 | (partial) GET /api/v1/scheduler/resources, /workflow-resource-intervals | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}` | unilabos/app/error_handling_api.py:204 | - | DEV has no manual-handling sessions |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/complete` | unilabos/app/error_handling_api.py:210 | - | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}/devices/{device_id}/actions` | unilabos/app/error_handling_api.py:244 | (partial) GET /api/v1/runtime/endpoints action_capabilities | Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}/inventory` | unilabos/app/error_handling_api.py:351 | - | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/inventory-corrections` | unilabos/app/error_handling_api.py:360 | (partial) POST /api/v1/scheduler/inventory-recovery/reconcile | Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/manual-actions` | unilabos/app/error_handling_api.py:255 | (partial) POST /api/v1/workflow-tasks execution_kind=ad_hoc_device_action | Not bound to a session or error. Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/return` | unilabos/app/error_handling_api.py:228 | - | Used by the V1 console |
| DTO-DIFF | `GET /api/v1/error-decisions` | unilabos/app/scheduler/api.py:492 | GET /api/v1/error-decisions | V1 returns {decisions:[..]}; DEV returns {items:[..]} plus category and material_execution |
| DTO-DIFF | `POST /api/v1/error-decisions/{decision_id}` | unilabos/app/scheduler/api.py:498 | POST /api/v1/error-decisions/{decision_id} | V1 returns status=delivered and 404 when not pending. DEV returns status=resolved and 409, and the action defaults to abort. physical_settlement allows only operator_intervention or abort |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/errors/{decision_id}/decision` | unilabos/app/error_handling_api.py:184 | POST /api/v1/error-decisions/{decision_id} | DEV has no Idempotency-Key header, no station/decision version or error-epoch CAS, and no enter_manual_handling action. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/stations/{station_id}/resource-dispositions` | unilabos/app/error_handling_api.py:384 | GET /api/v1/workflow-resource-intervals + /workflow-device-tenancies | UNVERIFIED equivalence. Used by the V1 console |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/resource-dispositions` | unilabos/app/error_handling_api.py:390 | POST /api/v1/workflow-resource-intervals/{owner_uuid}/release; /workflow-device-tenancies/{uuid}/release | V1 uses Idempotency-Key and returns 202. UNVERIFIED equivalence |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/resume` | unilabos/app/error_handling_api.py:413 | POST /api/v1/workflow-tasks/{uuid}/commands type=resume; POST /api/v1/scheduler/drain/resume | Station-wide in V1, per-task in DEV. Used by the V1 console |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity` | unilabos/app/workflow_api.py:2106 | POST /api/v1/scheduler/inventory-recovery/reconcile | UNVERIFIED |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/resolve-uncertain` | unilabos/app/workflow_api.py:2090 | POST /api/v1/scheduler/physical-recovery/{job_uuid}/settle\|release; /error-decisions/{id} | UNVERIFIED equivalence |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/settle-material-transfer` | unilabos/app/workflow_api.py:2119 | POST /api/v1/scheduler/physical-recovery/{job_uuid}/settle; /materials/executions/settle | UNVERIFIED. Used by the V1 console |

## Workspace and lifecycle

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/file-browser` | unilabos/app/web/api.py:1185 | - | Listed under Workspace in the V1 manual. Uses the legacy Resp envelope |
| MISSING | `POST /api/v1/lab/demo` | unilabos/app/scheduler/inventory/layout.py:485 | - | Demo seeding |
| MISSING | `POST /api/v1/lab/placements` | unilabos/app/scheduler/inventory/layout.py:467 | (partial) PUT /api/v1/materials/instances/{uuid}/position |  |
| MISSING | `DELETE /api/v1/lab/placements/{subject_id}` | unilabos/app/scheduler/inventory/layout.py:474 | - |  |
| MISSING | `GET /api/v1/lab/profile` | unilabos/app/scheduler/inventory/layout.py:439 | - |  |
| MISSING | `PUT /api/v1/lab/profile` | unilabos/app/scheduler/inventory/layout.py:443 | - |  |
| MISSING | `GET /api/v1/lab/warehouse` | unilabos/app/scheduler/inventory/layout.py:451 | - |  |
| MISSING | `GET /api/v1/workspace/package-mounts` | unilabos/app/workspace_authoring_api.py:25 | (analog) GET /api/v1/driver-packages | The Workspace Host probes it for readiness |
| MISSING | `POST /v1/operations` | unilabos/workspace_host/host.py:2201 | (partial) POST /api/v1/restart, /api/v1/reset, /device-processes/{id}/start\|stop\|restart | Material layout preview/apply has no DEV analog |
| MISSING | `GET /v1/operations/{operation_id}` | unilabos/workspace_host/host.py:2176 | - |  |
| MISSING | `GET /v1/snapshot` | unilabos/workspace_host/host.py:2173 | (partial) /api/v1/health, /readiness, /restart | Workspace Host supervisor state |
| DTO-DIFF | `GET /api/v1/health` | unilabos/app/scheduler/api.py:550 | GET /api/v1/health | Standalone scheduler app only, not mounted in the product. Same difference as the web /health row |
| DTO-DIFF | `GET /api/v1/health` | unilabos/app/web/api.py:1266 | GET /api/v1/health | V1 scheduler is ready\|disabled; DEV scheduler is local\|remote and DEV adds execution |
| DTO-DIFF | `GET /api/v1/lab/layout` | unilabos/app/scheduler/inventory/layout.py:447 | GET /api/v1/lab/layout | CONFLICT. V1 returns {zones, placements, storage_summary}, a view of placed instances. DEV returns {layout_key, revision, cell_size, zones, walls, *_at_ms}, a grid document |
| DTO-DIFF | `GET /api/v1/readiness` | unilabos/app/web/server.py:243 | GET /api/v1/readiness | V1 returns {status: ready\|starting\|failed, capabilities{can_edit_definitions, can_view_drafts, can_run_unpublished, can_debug, can_execute_device_actions}, phase, workflowRuntime, workflowProgress, observability?, error?}. DEV returns {status: ready\|not_ready, scheduler, execution, reasons}. Both use 200/503. The V1 console gates the UI on capabilities |
| PATH-DIFF | `GET /api/v1/lab/assembly/{subject_id}` | unilabos/app/scheduler/inventory/layout.py:478 | GET /api/v1/materials/instances/{uuid}/tree |  |
| PATH-DIFF | `POST /api/v1/lab/zones` | unilabos/app/scheduler/inventory/layout.py:456 | PUT /api/v1/lab/layout (whole document with revision) | Zone models differ |
| PATH-DIFF | `DELETE /api/v1/lab/zones/{zone_id}` | unilabos/app/scheduler/inventory/layout.py:463 | PUT /api/v1/lab/layout |  |
| PATH-DIFF | `GET /v1/logs/{component}?maxBytes=` | unilabos/workspace_host/host.py:2180 | GET /api/v1/hostlink/log-sources + /api/v1/hostlink/logs?source_id=&cursor=&limit= |  |
| SAME | `GET /api/v1/hostlink/peers` | unilabos/app/scheduler/api.py:155 | GET /api/v1/hostlink/peers | DEV adds host_node_id. On the authority the edge proxy forwards it to the Host child (503 while the Host is down) |
| SAME | `GET /api/v1/scheduler/drain` | unilabos/app/scheduler/api.py:235 | GET /api/v1/scheduler/drain | DEV SchedulerDrainStatus is a superset of V1's fields |
| SAME | `POST /api/v1/scheduler/drain` | unilabos/app/scheduler/api.py:241 | POST /api/v1/scheduler/drain | Superset |
| SAME | `POST /api/v1/scheduler/drain/resume` | unilabos/app/scheduler/api.py:247 | POST /api/v1/scheduler/drain/resume | Superset |
| V1-DEPRECATED | `GET /api/v1/health` | unilabos/app/web/server.py:500 | GET /api/v1/health | Dead code for backend mode |

## Misc (edge transport, legacy pages)

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| PATH-DIFF | `POST /api/v1/edge/error-decisions` | unilabos/app/edge_control/local_authority.py:2137 | POST /api/v1/runtime/jobs/{job_uuid}/error-gate/open | Internal edge protocol |
| PATH-DIFF | `GET /api/v1/edge/jobs/{job_uuid}` | unilabos/app/edge_control/local_authority.py:2149 | GET /edge/commands/{command_uuid} (no /api/v1 prefix); GET /api/v1/runtime/jobs/{job_uuid} | Internal edge protocol |
| PATH-DIFF | `POST /api/v1/edge/jobs/{job_uuid}/begin` | unilabos/app/edge_control/local_authority.py:2179 | POST /api/v1/workflow-node-jobs/{job_uuid}/begin | Internal edge protocol |
| PATH-DIFF | `POST /api/v1/edge/jobs/{job_uuid}/execution-started` | unilabos/app/edge_control/local_authority.py:2197 | POST /api/v1/workflow-node-jobs/{job_uuid}/started | Internal edge protocol |
| PATH-DIFF | `POST /api/v1/edge/jobs/{job_uuid}/feedback` | unilabos/app/edge_control/local_authority.py:2216 | POST /api/v1/runtime/jobs/{job_uuid}/feedback | Internal edge protocol |
| PATH-DIFF | `PUT /api/v1/edge/jobs/{job_uuid}/outcome` | unilabos/app/edge_control/local_authority.py:2236 | POST /api/v1/runtime/jobs/{job_uuid}/transitions | Internal edge protocol; method also differs |
| PATH-DIFF | `POST /api/v1/edge/jobs/{job_uuid}/resolve-unknown` | unilabos/app/edge_control/local_authority.py:2273 | POST /api/v1/scheduler/physical-recovery/{job_uuid}/settle\|release; POST /api/v1/error-decisions/{id} | UNVERIFIED equivalence |
| PATH-DIFF | `GET /api/v1/edge/readiness` | unilabos/app/edge_control/local_authority.py:2091 | GET /api/v1/readiness (execution field) | Listed as a health probe in the V1 manual; DEV has no edge-scoped readiness |
| PATH-DIFF | `POST /api/v1/edge/sessions` | unilabos/app/edge_control/local_authority.py:2105 | WS /api/v1/ws/schedule hello + PUT /api/v1/runtime/sessions/{session_uuid} | Internal edge protocol; you already decided DEV transport wins |
| PATH-DIFF | `PUT /api/v1/edge/sessions/{session_uuid}/devices/{local_device_id}/status` | unilabos/app/edge_control/local_authority.py:2117 | PUT /api/v1/runtime/endpoints/{endpoint_uuid}/snapshot; POST /api/v1/telemetry/events | Internal edge protocol |
| PATH-DIFF | `WEBSOCKET /api/v1/edge/ws` | unilabos/app/edge_control/local_authority.py:2290 | WS /api/v1/ws/schedule (runtime.v1) | Internal edge protocol |
| V1-DEPRECATED | `GET /` | unilabos/app/web/pages.py:42 | - | Legacy local home page (shadowed by the console redirect) |
| V1-DEPRECATED | `WEBSOCKET /api/v1/ws/device_status` | unilabos/app/web/api.py:254 | - | Serves the legacy local status page (web/api.py is the 'legacy local API') |
| V1-DEPRECATED | `WEBSOCKET /api/v1/ws/registry_editor` | unilabos/app/web/api.py:286 | - | Legacy local page |
| V1-DEPRECATED | `WEBSOCKET /api/v1/ws/status_page` | unilabos/app/web/api.py:317 | - | Legacy local page |
| V1-DEPRECATED | `GET /open-folder` | unilabos/app/web/pages.py:133 | - | Legacy local page |
| V1-DEPRECATED | `GET /registry-editor` | unilabos/app/web/pages.py:177 | - | Legacy local page |
| V1-DEPRECATED | `GET /status` | unilabos/app/web/pages.py:72 | - | Legacy local page |
| n/a | `GET /` | unilabos/app/web/console.py:160 | GET / (HTML catalog page) | V1 redirects to /console/. Not relevant because the UI is the external uni-lab-fe |
| n/a | `MOUNT /console` | unilabos/app/web/console.py:168 | - | Not relevant because the UI is the external uni-lab-fe |
