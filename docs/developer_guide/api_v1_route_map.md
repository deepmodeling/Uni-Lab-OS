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

> **Status (branch `fix/p7-foundation`, 2026-11-12):** the foundation resolves conventions 1, 2 and 3 and the SSE
> naming in 7. Every DEV `/api/v1` JSON route now goes through `server/api/envelope.py` (`envelope_router` /
> `EnvelopeRoute`): `{code:0,data}` on success, HTTP 200 + `{code,error:{msg[,code,details]}}` on failure, V1's
> code table with `unauthorized` moved to 1002, validation errors as 1000. The control token is read only from
> `X-UniLab-Token`; CORS allows `Idempotency-Key` and `X-Workflow-Filename`. `/api/v1/events` emits
> `workflow.runtime.changed` with the DEV kind in `data.kind`. The findings below describe the state *before* that
> branch and remain the input for the per-domain ports (paths, DTO field names, V1-only routes).

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

1. **`/api/v1/history/*`.** DEV owns it as the event and payload store (`api/history.py:42`). DEV's own clients use it (`client/history.py:70`, `backend/edge_control.py:103`), and the edge proxy forwards the whole prefix to the Host (`api/edge_proxy.py:23-31`). V1's `/history/workflows|jobs` would be proxied to the Host and 404 there. Adopting them needs a carve-out, or better, new paths. **Resolved (p7-b):** DEV's store moved to `/api/v1/edge/history/*` (edge proxy prefix `edge/history`, `HTTPHistoryClient`, `EdgeControlService` payload pull, MCP catalog); V1's three routes are served by the authority.
2. **`GET /api/v1/lab/layout`.** *(Resolved in p7-c: V1 view; dev grid is `grid` in `/lab/profile`.)* In DEV it's a revisioned grid document `{layout_key, revision, cell_size, zones, walls}` with PUT/DELETE (`runtime/lab.py:21`). In V1 it's a read view `{zones, placements, storage_summary}` over placed instances, and zones and placements are separate resources (`scheduler/inventory/layout.py:447`).
3. **`POST /api/v1/resource-templates`.** *(Resolved in p7-c: V1 identity receipt; Host registry report moved to `POST /api/v1/registry/reports`.)* In DEV this is the Registry Authority intake the Host calls at startup (`runtime/registry.py:52`). It versions entries and returns a registry report, with HTTP 400/422 on error. V1 returns an id→uuid identity receipt in the envelope. The request shape `{resources:[..]}` is compatible, so only the response needs care. DEV's `legacy_adaptor/sync/instances.py:186` already speaks V1's form as an upstream client.
4. **`/api/v1/error-decisions`.**
   - List: V1 `{decisions}` vs DEV `{items}`.
   - Resolve: V1 `delivered`/404 vs DEV `resolved`/409, and DEV defaults the action to `abort`.
   - DEV adds the physical-settlement restriction (`runtime/diagnostics.py:305-371` vs V1 `scheduler/api.py:492-520`).
   - **Resolved (p7-b):** V1's actual contract wins (`decisions`, `delivered`, 404 → not-found envelope); DEV's physical-settlement restriction stays as an additive guard.
5. **`POST /workflow-tasks/{uuid}/commands`.**
   - DEV requires `expected_revision` as a cross-tab guard, and its `pause` means "switch to step".
   - V1 has both `pause` and `switch_to_step`, plus `unlock_resources`, `target_node_uuid` and a 201 response.
   - Accepting V1 bodies means making `expected_revision` optional, which weakens DEV's guard, or having the FE send it.
6. **`POST /workflows/{uuid}/composite-invocations`.** V1 inserts a *contract* (`contract_uuid`, `device_bindings`, `param`). DEV inlines a published child (`child_workflow_uuid`, `child_revision`, `arguments`). DEV's MCP catalog and tests target DEV's shape.
7. **`POST /workflows/{uuid}/authoring/apply`.** V1 takes `{candidate_hash}`; DEV takes a triple CAS. Adopting V1 drops DEV's draft/revision guard unless the server derives those values.
8. **`PUT /workflows/{uuid}/graph`.** DEV defaults `site_binding_mode=resolve` for programmatic importers. A V1 FE omits the field, so it gets resolve, and can fail where it expects a plain save. Flipping the default breaks DEV's import scripts (UNVERIFIED which scripts).
9. **`GET /api/v1/events` names.** Renaming DEV events to V1's breaks DEV's own CLI `workflow watch` (`tests/app/test_workflow_watch_sse.py:33-144`). The safe fix is additive: also emit `workflow.runtime.changed`.
10. **`GET /api/v1/readiness` and `/health`.** *(Resolved in p7-c: V1 fields and enums, dev probe summary kept as extension fields; both raw.)* DEV's readiness is a Kubernetes-style `ready|not_ready` contract. V1's status is `ready|starting|failed` with `capabilities` and `workflowProgress`. Adding V1's fields is safe; changing the `status` enum is not. The `/health` `scheduler` enums differ too (`ready|disabled` vs `local|remote`).
11. **Code 4001 and the Bearer header.** Both mean different things on each side (see conventions 2 and 3).
12. **The `/api/v1/materials` namespace.** *(Resolved in p7-c: V1 routes mounted after dev literals; dev duplicates removed, dev writes moved to `/inventory/commands`.)* V1's `GET /materials/{material_uuid}` takes a UUID-typed path parameter (`backend_api.py:316`). If it is registered before DEV's materials router, it captures `/materials/lots`, `/templates`, `/instances`, `/links`, `/changes`, `/events`, `/reservations`, `/executions` and `/registry-classes`. FastAPI does not fall through on a validation failure. So V1 material routes must be mounted after DEV's.
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
- **Host data plane:** `/api/v1/runtime/*` (27), `/api/v1/telemetry/*` (6), `/api/v1/edge/history/*` (7, moved from `/api/v1/history/*`). All served on the Host and proxied by the authority.
- **Operations:**
  - `/api/v1/driver-packages/*` (14), `/api/v1/device-processes/*` (9), `/api/v1/hostlink/{logs,log-sources,material-sync,notify-device}`.
  - `/api/v1/restart` (3), `/api/v1/reset` (2), `/ping`, `/scheduler/resources`, `/scheduler/inventory-recovery` (2), `/scheduler/physical-recovery` (3), `/status-incidents` (2), `/api/v1/debug/databases` (2).
  - `PUT|DELETE /api/v1/lab/layout`.
- **Transport:** `/mcp`, `/edge/commands/{uuid}` (no `/api/v1`), `/api/v1/edge/http-responses/{uuid}`, `WS /api/v1/ws/schedule`, and the edge-proxy catch-alls.


## Workflow authoring

> **Status (branch `fix/p7-a-authoring`, 2026-11-12):** every MISSING / DTO-DIFF / PATH-DIFF row below is ported
> (rows marked **Done (p7-a)**). Storage: runtime.db **v9** (`workflow.workflow_type`, `workflow.operation_category_uuid`,
> `workflow_node.manual_confirmation`, table `workflow_operation_category`). Dev bodies removed: composite-invocations
> `{child_workflow_uuid, child_revision, arguments}` and authoring/apply `{expected_draft_hash, expected_workflow_revision,
> expected_candidate_hash}`. Dev-only routes kept: `/workflows/from-template`, `/template-catalog`, `/template-instances`,
> `PUT /io`, `PUT /resource-scopes`, `GET /workflows/{uuid}/publications`, `/workflow-publications`. Authoring compile is
> `WorkflowAuthoringCompiler` over `registry/workflow_ast` (one compiler for device-package templates and authoring).
> Not done here: the scheduler does not yet read `node.manual_confirmation.timeout_seconds` (task-lifecycle branch);
> structural node templates (condition / repeat_until / material_io) are not projected.

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `POST /api/v1/authoring/compile` | unilabos/app/workflow_authoring_transform.py:391 | - | **Done (p7-a)**: `services/runtime/workflow/authoring_compiler.py` (`WorkflowAuthoringCompiler`, the `AuthoringCompiler` implementation) on top of `registry/workflow_ast.compile_workflow_source` — the same static compiler the device-package `@workflow(compiler="ast")` templates use, now also accepting source text. V1 request DTO (strict, identity/revision checked); response is the closed transform DTO. The dev authoring syntax is the `@workflow` / `ctx.run` DSL, not V1's `device()` engine. Tests: `tests/server/test_v1_workflow_authoring_compiler.py`. |
| MISSING | `POST /api/v1/authoring/generate-python` | unilabos/app/workflow_authoring_transform.py:412 | - | **Done (p7-a)**: Renders `@workflow` source with `# unilab:node_uuid` anchors (V1 format) and re-compiles; structures the DSL cannot express (conditions, scripts, manual confirmation, composites, named outputs, handle edges) return a `graph_not_representable` diagnostic. |
| MISSING | `POST /api/v1/authoring/validate` | unilabos/app/workflow_authoring_transform.py:433 | - | **Done (p7-a)**: Fixed-point check: compiling the source against the graph must yield a source-only changeset. |
| MISSING | `GET /api/v1/experiment-operation-categories` | unilabos/app/operation_category_api.py:92 | - | **Done (p7-a)**: `api/runtime/workflow_definition.py`; runtime.db v9 `workflow_operation_category` (V1 default categories seeded once, soft delete). Tests: `tests/server/test_v1_workflow_authoring_api.py`. Was: Used by the V1 console |
| MISSING | `POST /api/v1/experiment-operation-categories` | unilabos/app/operation_category_api.py:109 | - | **Done (p7-a)**: Same router. Name unique (casefold), 409-class conflict → 3003. |
| MISSING | `DELETE /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:176 | - | **Done (p7-a)**: 3003 while an experiment operation references it. |
| MISSING | `GET /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:135 | - | **Done (p7-a)**: Unknown → 3002, malformed UUID → 1000. |
| MISSING | `PUT /api/v1/experiment-operation-categories/{category_uuid}` | unilabos/app/operation_category_api.py:154 | - | **Done (p7-a)**: Partial update (name / sort_order). |
| MISSING | `POST /api/v1/local/workflows/import-python` | unilabos/app/workflow_api.py:1203 | (partial) POST /workflows + PUT /authoring/draft + POST /authoring/apply | **Done (p7-a)**: Raw `.py` body + `X-Workflow-Filename`; source is only parsed (never imported); creates the workflow, registers a managed source under `<db root>/workflow_sources/workflows/`, saves the draft and applies the candidate atomically (cleans up on failure). Returns the graph (201). Was: Raw .py body with X-Workflow-Filename header. Used by the V1 console |
| MISSING | `DELETE /api/v1/workflow-edges/{edge_uuid}` | unilabos/app/workflow_api.py:1703 | - | **Done (p7-a)**: `services/runtime/workflow/definition_service.py` (full-graph CAS save). |
| MISSING | `DELETE /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1688 | - | **Done (p7-a)**: 3003 while the node still has children. |
| MISSING | `GET /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1664 | - | **Done (p7-a)**:  |
| MISSING | `PATCH /api/v1/workflow-nodes/{node_uuid}` | unilabos/app/workflow_api.py:1676 | - | **Done (p7-a)**: Only explicitly sent fields are applied. |
| MISSING | `POST /api/v1/workflow-nodes/{node_uuid}/duplicate` | unilabos/app/workflow_api.py:1693 | - | **Done (p7-a)**:  |
| MISSING | `POST /api/v1/workflows/import` | unilabos/app/workflow_api.py:1183 | (different) POST /api/v1/workflows/from-template | **Done (p7-a)**: Legacy JSON (direct or `data`-wrapped) with node/edge identities rebuilt (ported V1 `duplicate_graph`); `workflow_type` honoured; `inventory_requirements` rejected as in V1. Was: Legacy JSON import. Used by the V1 console |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/batch-delete` | unilabos/app/workflow_api.py:1532 | - | **Done (p7-a)**:  |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/duplicate` | unilabos/app/workflow_api.py:1549 | - | **Done (p7-a)**: Copies kind/category too. |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/edges` | unilabos/app/workflow_api.py:1515 | (whole-graph PUT only) | **Done (p7-a)**: Returns the saved graph (201). Was: Used by the V1 console |
| MISSING | `GET /api/v1/workflows/{workflow_uuid}/nodes` | unilabos/app/workflow_api.py:1494 | - | **Done (p7-a)**: Current layer only (composite private sub-graphs hidden). |
| MISSING | `POST /api/v1/workflows/{workflow_uuid}/nodes` | unilabos/app/workflow_api.py:1477 | (whole-graph PUT only) | **Done (p7-a)**: Template nodes derive type/action/defaults from `/workflow-node-templates`; a fixed material also writes `meta_data.target_device_id` (dev dispatch key). Returns the saved graph (201). Was: Used by the V1 console |
| MISSING | `GET /api/v1/workflows/{workflow_uuid}/referenced-by` | unilabos/app/workflow_api.py:1351 | - | **Done (p7-a)**: Parents found via `meta_data.unilab.composite.child_workflow_uuid`. |
| DTO-DIFF | `GET /api/v1/workflows` | unilabos/app/workflow_api.py:1237 | GET /api/v1/workflows | **Done (p7-a)**: V1 filters `keyword`/`name`/`workflow_type`/`status`/`operation_category_uuid`; response `{items, has_more, page, page_size}`; items carry `workflow_type` (normal\|experiment_operation), `operation_category_uuid`, `status`. Was: F01. DEV has no workflow_type, status, operation_category_uuid or keyword filter and always returns workflow_type=workflow. V1 uses normal\|experiment_operation. The V1 console queries ?workflow_type=experiment_operation&status=published |
| DTO-DIFF | `POST /api/v1/workflows` | unilabos/app/workflow_api.py:1159 | POST /api/v1/workflows | **Done (p7-a)**: `workflow_type` + `operation_category_uuid` (omitted → legacy tag, explicit null → none); dev keeps the optional `workflow_uuid` for Host upserts. Was: DEV silently drops workflow_type and operation_category_uuid (extra=ignore). DEV accepts workflow_uuid |
| DTO-DIFF | `GET /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1405 | GET /api/v1/workflows/{workflow_uuid} | **Done (p7-a)**: V1 read model. Was: workflow_type value domain; DEV has no operation_category_uuid |
| DTO-DIFF | `PUT /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1420 | PUT /api/v1/workflows/{workflow_uuid} | **Done (p7-a)**: Type cannot change (1000); category omitted keeps, null clears. Was: Same as POST /workflows |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/authoring/apply` | unilabos/app/workflow_api.py:2218 | POST /api/v1/workflows/{workflow_uuid}/authoring/apply | **Done (p7-a)**: Body `{candidate_hash}` only; draft hash and base revision are derived from the stored candidate (`WorkflowService.apply_authoring_by_candidate`). The triple-CAS body is rejected (1000). Was: V1 {candidate_hash} vs DEV {expected_draft_hash, expected_workflow_revision, expected_candidate_hash}. Both strict |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/composite-invocations` | unilabos/app/workflow_api.py:1597 | POST /api/v1/workflows/{workflow_uuid}/composite-invocations | **Done (p7-a)**: V1 body `{revision, contract_uuid, invocation_uuid, device_bindings, pose, param}`; child = the contract's workflow, child revision = the contract's revision; child must be an experiment operation (1000) whose current revision is published (3003); dev publications have no executor requirements, so `device_bindings` must be `{}`. Returns the parent graph (current layer), HTTP 200. Dev body removed. Was: V1 {revision, contract_uuid, invocation_uuid, device_bindings, pose, param} vs DEV {revision, child_workflow_uuid, child_revision, invocation_uuid, pose, arguments}. Both strict, so each rejects the other's body. V1 returns 200, DEV 201 |
| DTO-DIFF | `GET /api/v1/workflows/{workflow_uuid}/graph` | unilabos/app/workflow_api.py:1449 | GET /api/v1/workflows/{workflow_uuid}/graph | **Done (p7-a)**: `workflow` gains `status`/`workflow_type`/`operation_category_uuid`; current layer only (V1 `definition_graph`); `inventory_requirements` present (dev keeps node-level requirements in `meta_data`, so the list is empty). Was: V1 returns {workflow(+status, category), nodes, edges, inventory_requirements} for the current layer only. DEV returns {workflow, nodes, edges, node_templates, handle_templates} |
| DTO-DIFF | `PUT /api/v1/workflows/{workflow_uuid}/graph` | unilabos/app/workflow_api.py:1461 | PUT /api/v1/workflows/{workflow_uuid}/graph | **Done (p7-a)**: `site_binding_mode` defaults to `preserve` (V1 plain save); programmatic import (`client/runtime/workflow.py`, from-template) sends `resolve` explicitly. `node.manual_confirmation` persisted (runtime.db v9 column). Composite private sub-graphs are restored when the client sends the current layer only. Was: DEV defaults site_binding_mode to 'resolve' (its own doc says browsers must send 'preserve'), so a V1 client's save can fail with site_binding_invalid. DEV also drops node.manual_confirmation |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/publications` | unilabos/app/workflow_api.py:1563 | POST /api/v1/workflows/{workflow_uuid}/publications | **Done (p7-a)**: Returns the V1 contract (`uuid` = contract_uuid, `workflow_revision`, `version`, `input_contract`, `output_contract`, `executor_requirements: []`, `node_template_uuid`, `node_count`, `edge_count`, `revision_fingerprint`) plus dev's palette fields (`revision`, `inputs`, `outputs`, `node_template`, `handle_templates`). Was: Request is the same ({revision}). V1 returns a public contract whose contract_uuid feeds composite-invocations; DEV returns a publication record. Field-level diff UNVERIFIED |
| DTO-DIFF | `POST /api/v1/workflows/{workflow_uuid}/run-preflight` | unilabos/app/workflow_api.py:1639 | POST /api/v1/workflows/{workflow_uuid}/run-preflight | **Done (p7-a)**: V1 body (`run_mode` string, `inventory_bindings` V1 DTO, extra fields ignored); dev preflight does not consume inventory bindings yet and echoes them in the report. `Cache-Control: no-store`. Was: F02. DEV is strict, forbids inventory_bindings and restricts run_mode to a Literal |
| PATH-DIFF | `GET /api/v1/workflows/{workflow_uuid}/run-preflight` | unilabos/app/workflow_api.py:1618 | POST /api/v1/workflows/{workflow_uuid}/run-preflight | **Done (p7-a)**: Added (query `run_mode`, `target_node_uuid`). Was: DEV has POST only |
| SAME | `DELETE /api/v1/workflows/{workflow_uuid}` | unilabos/app/workflow_api.py:1440 | DELETE /api/v1/workflows/{workflow_uuid} | Reference-integrity checks UNVERIFIED |
| SAME | `GET /api/v1/workflows/{workflow_uuid}/authoring` | unilabos/app/workflow_api.py:2199 | GET /api/v1/workflows/{workflow_uuid}/authoring | **Done (p7-a)**: Now backed by a compiler: `backend/composition.setup_local_scheduler` calls `WorkflowService.configure_authoring(compiler=WorkflowAuthoringCompiler(), source_root=<db root>/workflow_sources)`; any workflow gets a managed source `workflows/wf_<uuid>.py` on first authoring access (state `draft_missing` until a draft is saved). Before this the endpoints always answered 3002/5001. Was: Payload equality UNVERIFIED |
| SAME | `PUT /api/v1/workflows/{workflow_uuid}/authoring/draft` | unilabos/app/workflow_api.py:2204 | PUT /api/v1/workflows/{workflow_uuid}/authoring/draft | **Done (p7-a)**: Compiles with `WorkflowAuthoringCompiler`; diagnostics carry UTF-8 byte source ranges. Was: Same strict DraftWriteRequest |
| V1-DEPRECATED | `GET /api/v1/published-workflow-contracts` | unilabos/app/workflow_api.py:1323 | GET /api/v1/workflow-publications | **Done (p7-a)**: Kept because the V1 console still calls it: latest contract per workflow, V1 page shape, hidden from OpenAPI. Was: Hidden. V1 says new frontends must not use it, but the V1 console still calls it |

## Task lifecycle and observability

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/monitor/events` | unilabos/app/scheduler/api.py:265 | (partial) SSE /api/v1/events log notices; SSE /api/v1/materials/events | **Ported (p7-b): SSE {seq, ts, channel, type, data} projected from frontend_event (action / scheduler / device); material channel empty (materials events stay on /materials/events); follow=false for one-shot backlog.** V1 has 4 channels (material, device, action, scheduler) |
| MISSING | `GET /api/v1/monitor/snapshot` | unilabos/app/scheduler/api.py:313 | (partial) GET /api/v1/scheduler/resources | **Ported (p7-b): devices (busy/idle from jobs), scheduler {workflow_states, inflight, reschedule_count}, recent per channel.** |
| MISSING | `GET /api/v1/reagents/{reagent_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1799 | - |  |
| MISSING | `POST /api/v1/reschedule` | unilabos/app/scheduler/api.py:231 | - | **Ported (p7-b): re-runs resource reconcile + wakes active tasks; {dispatched: [job ids observed]}; no local scheduler → 5001.** Manual re-dispatch |
| MISSING | `GET /api/v1/scheduler/time-plan` | unilabos/app/scheduler/api.py:258 | - | **Ported (p7-b): no time-planning engine in the open tree → V1 no-engine shape {enabled: false, algorithm: stable-ready-order}; DEV scheduler dispatches in DAG ready order and is not exposed as a candidate plan.** Used by the V1 console (timeScheduleClient) |
| MISSING | `POST /api/v1/station/workflow-invocations` | unilabos/app/workflow_api.py:1740 | (partial) POST /api/v1/workflow-tasks idempotency_key | Bearer EdgeControlConfig.api_key, which clashes with DEV's Bearer control token |
| MISSING | `GET /api/v1/timeline` | unilabos/app/scheduler/api.py:253 | - | **Ported (p7-b): running = persisted in-flight jobs, completed = job_runs view in window; estimated_s = observed mean per device:action (estimate_source observed|unknown); estimator.mode observed.** Swimlane timeline |
| MISSING | `POST /api/v1/workflow-human-interactions/{interaction_uuid}/reply` | unilabos/app/workflow_api.py:2164 | - | **Ported (p7-b): V1 reply validation per kind; replay of the same answer is idempotent; ended/timed-out → 3003.** Reply shapes: choice, text, values, x/y, corner_x/y. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-interventions/{intervention_uuid}` | unilabos/app/workflow_api.py:2180 | - | **Ported (p7-b).** |
| MISSING | `POST /api/v1/workflow-interventions/{intervention_uuid}/decisions` | unilabos/app/workflow_api.py:2184 | - | **Ported (p7-b): revision + option_id + Idempotency-Key; 201 new / 200 replay; delivered through the same release channel as /error-decisions; undelivered → 3003 (retry with the same key).** Idempotency-Key header; returns 201 or 200. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-runtime/wait-graph` | unilabos/app/workflow_api.py:2068 | - | **Ported (p7-b): from workflow_resource_waiter + active leases / resource uses; Tarjan cycles.** |
| MISSING | `GET /api/v1/workflow-task-presentations` | unilabos/app/workflow_api.py:1890 | - | **Ported (p7-b): compact task + embedded compact jobs; view=matrix returns active/attention tasks + terminal_limit recent terminal ones.** F07. view=matrix. Used by the V1 console |
| MISSING | `DELETE /api/v1/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1940 | - | **Ported (p7-b): soft delete with in-transaction re-check; already_deleted on replay; blockers → 3003.** Soft delete. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/delete-eligibility` | unilabos/app/workflow_api.py:1926 | - | **Ported (p7-b): blockers from DEV facts (jobs, claims, intervals, waiters, tenancies, pending commands, interventions, confirmations, human interactions).** Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/events` | unilabos/app/workflow_api.py:2037 | (partial) GET /workflow-tasks/{uuid}/node-runs | **Ported (p7-b): after_sequence cursor over the persisted frontend_event log, grouped per task (kind task_transition / job_transition / ...); not a separate runtime journal.** after_sequence cursor |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/execution-locks` | unilabos/app/workflow_api.py:2010 | - | **Ported (p7-b): DEV workflow_execution_claim + lease rows; lease uuid is uuid5(claim, lock_key); can_release / release_block_reason per V1 rules.** F07. DEV has an execution_lock_lease table but no API. Used by the V1 console |
| MISSING | `POST /api/v1/workflow-tasks/{task_uuid}/execution-locks/{lease_uuid}/force-release` | unilabos/app/workflow_api.py:2016 | (analog) /workflow-resource-intervals/{owner}/release; /workflow-device-tenancies/{uuid}/release | **Ported (p7-b): Claim/Fence CAS + physical_settlement_confirmed; releases the job claim in runtime.db and the scheduler ledger; already_released on replay.** Fencing-token CAS and physical_settlement_confirmed. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/human-interactions` | unilabos/app/workflow_api.py:2160 | (partial) GET /workflow-tasks/{uuid}/manual-confirmations | **Ported (p7-b): runtime.db workflow_human_interaction (v9); pending requests of terminal tasks are canceled on read. DEV has no native command that opens one yet (producer API HumanInteractions.open).** Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1781 | (partial) GET /api/v1/materials/reservations?task_uuid= |  |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/material-events` | unilabos/app/workflow_api.py:1787 | - | **Ported (p7-b): MaterialsService ledger changes whose job_uuid belongs to the task, annotated with node name and step path; empty when no materials authority.** Used by the V1 console |
| MISSING | `GET /api/v1/workflow-tasks/{task_uuid}/step-state` | unilabos/app/workflow_api.py:1954 | (partial) GET /workflow-tasks/{uuid}/node-runs | **Ported (p7-b): candidates = pending node runs whose upstreams finished, only while paused with nothing in flight. No capability gate (DEV has no debug capability).** Used by the V1 console |
| DTO-DIFF | `GET /api/v1/events` | unilabos/app/workflow_api.py:2237 | GET /api/v1/events | **Names done by the foundation branch; p7-b adds station.error_handling.changed (data.station_id) from the station model.** Same cursor protocol (Last-Event-ID, stream.reset). Event names differ: V1 workflow.runtime.changed / workflow.authoring.changed; DEV workflow.task\|node_run\|node_job\|manual_confirmation\|resource.changed plus log-notice events. V1 clients miss every DEV runtime change |
| DTO-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}` | unilabos/app/workflow_api.py:2064 | GET /api/v1/workflow-node-jobs/{job_uuid} | **Ported (p7-b): same V1 job fields.** F04 |
| DTO-DIFF | `GET /api/v1/workflow-tasks` | unilabos/app/workflow_api.py:1864 | GET /api/v1/workflow-tasks | **Ported (p7-b): execution_kind filter, has_more; items carry priority, inventory_bindings, execution_mode, dispatch_gate, wait_reason.** DEV has no execution_kind filter and strips output from list items. Task DTO diff UNVERIFIED |
| DTO-DIFF | `POST /api/v1/workflow-tasks` | unilabos/app/workflow_api.py:1708 | POST /api/v1/workflow-tasks | **Ported (p7-b): priority (normal|high) and inventory_bindings (V1 shape) accepted and persisted on workflow_task (runtime v9); bindings are frozen with the task, reservation stays with the inventory domain; ad-hoc tasks reject non-default values.** F03. DEV is strict, so priority and inventory_bindings fail with 1000 (envelope, HTTP 200), not 422. V1 also checks capabilities (debug for step) |
| DTO-DIFF | `GET /api/v1/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1922 | GET /api/v1/workflow-tasks/{task_uuid} | **Ported (p7-b): same task projection as the list.** DEV task has no priority or inventory_bindings (UNVERIFIED full diff) |
| DTO-DIFF | `POST /api/v1/workflow-tasks/{task_uuid}/commands` | unilabos/app/workflow_api.py:1961 | POST /api/v1/workflow-tasks/{task_uuid}/commands | **Ported (p7-b): V1 body (type, target_node_uuid, idempotency_key, description, meta_data) + optional expected_revision CAS; returns the command projection with HTTP 201, business refusals as status=rejected + result.reason; pause / switch_to_step / unlock_resources added.** DEV requires expected_revision, which V1 clients don't send, and forbids the target_node_uuid, description and meta_data that the V1 console sends. DEV has no switch_to_step or unlock_resources. V1 returns 201, DEV 200 |
| DTO-DIFF | `GET /api/v1/workflow-tasks/{task_uuid}/jobs` | unilabos/app/workflow_api.py:1982 | GET /api/v1/workflow-tasks/{task_uuid}/jobs | **Ported (p7-b): topological_index, executor_kind, execution_policy, execution_timeout_seconds, attempt, current_attempt, start_state, execution_source, manual_confirmation.** F04: no topological_index or executor_kind. DEV returns a flat attempt view |
| PATH-DIFF | `GET /api/v1/history/jobs` | unilabos/app/scheduler/api.py:410 | GET /api/v1/workflow-tasks/{uuid}/jobs; GET /api/v1/runtime/jobs | **Ported (p7-b): served by the authority from runtime.db view job_runs; dev event store moved to /api/v1/edge/history/*.** CONFLICT: same as the row above |
| PATH-DIFF | `GET /api/v1/history/workflows` | unilabos/app/scheduler/api.py:377 | GET /api/v1/workflow-tasks?status= | **Ported (p7-b): runtime.db view workflow_runs, {runs, stats}.** CONFLICT: DEV owns /api/v1/history/* (event and payload store) and the edge proxy forwards the whole prefix to the Host |
| PATH-DIFF | `GET /api/v1/history/workflows/{workflow_id}` | unilabos/app/scheduler/api.py:396 | GET /api/v1/workflow-tasks/{task_uuid} | **Ported (p7-b): run + spec + jobs; unknown id → 3002.** CONFLICT: same as the row above |
| PATH-DIFF | `POST /api/v1/jobs/{job_id}/finish` | unilabos/app/scheduler/api.py:225 | POST /api/v1/runtime/jobs/{job_uuid}/transitions | Internal |
| PATH-DIFF | `GET /api/v1/workflow-interventions` | unilabos/app/workflow_api.py:2171 | GET /api/v1/workflow-tasks/{task_uuid}/interventions | **Ported (p7-b): global list by status (open|selected|superseded), limit 1..500. Interventions are persisted when the scheduler marks an attempt decision-pending and superseded on resume/terminal. DEV per-task /workflow-tasks/{uuid}/interventions removed.** DEV lists per task only; V1 is a global open list. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}/feedback` | unilabos/app/workflow_api.py:2074 | GET /api/v1/workflow-node-jobs/{job_uuid}/feedback-history | **Ported (p7-b): page/page_size; DEV /feedback-history removed (MCP catalog updated).** V1 paginates with page/page_size; DEV uses limit/offset |
| PATH-DIFF | `GET /api/v1/workflow-node-jobs/{job_uuid}/inventory-consumptions` | unilabos/app/workflow_api.py:1793 | GET /api/v1/materials/reservations/by-job/{job_uuid}; /materials/executions/{job_uuid} | UNVERIFIED |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/manual-confirmation` | unilabos/app/workflow_api.py:2134 | POST /api/v1/workflow-manual-confirmations/{confirmation_uuid}/decision | **Ported (p7-b): {action: approve|reject} (+ optional DEV confirmed_by / comment / decision_idempotency_key); enveloped (HTTP 200 + code) instead of V1 raw 400/404/409. DEV POST /workflow-manual-confirmations/{uuid}/decision and /workflow-tasks/{uuid}/manual-confirmations/{uuid}/decision removed; MCP catalog updated.** V1 keys on job_uuid, DEV on confirmation_uuid. DEV body adds confirmed_by, comment and decision_idempotency_key. V1 answers here with real HTTP 400/404/409. Used by the V1 console |
| PATH-DIFF | `PATCH /api/v1/workflow-tasks/{task_uuid}/jobs/{job_uuid}/param` | unilabos/app/workflow_api.py:1986 | PATCH /api/v1/workflow-tasks/{task_uuid}/input | **Ported (p7-b): top-level merge into the pending current attempt, its node run and plan node while paused; DEV PATCH /workflow-tasks/{uuid}/input (entry params) stays as a DEV-only capability.** Granularity differs: per-job param in V1, task entry input in DEV |
| V1-DEPRECATED | `POST /api/v1/debug/workflow-tasks` | unilabos/app/workflow_api.py:1805 | - | Returns 410 code 4100 |
| V1-DEPRECATED | `GET /api/v1/debug/workflow-tasks/{task_uuid}` | unilabos/app/workflow_api.py:1822 | - | Returns 410 |
| V1-DEPRECATED | `POST /api/v1/debug/workflow-tasks/{task_uuid}/commands` | unilabos/app/workflow_api.py:1835 | - | Returns 410 |
| V1-DEPRECATED | `GET /api/v1/workflows` | unilabos/app/scheduler/api.py:204 | - | **Done (p7-a)**: V1 filters `keyword`/`name`/`workflow_type`/`status`/`operation_category_uuid`; response `{items, has_more, page, page_size}`; items carry `workflow_type` (normal\|experiment_operation), `operation_category_uuid`, `status`. Was: Not mounted in the product |
| V1-DEPRECATED | `POST /api/v1/workflows` | unilabos/app/scheduler/api.py:194 | (definition route at workflow.py:461) | **Done (p7-a)**: `workflow_type` + `operation_category_uuid` (omitted → legacy tag, explicit null → none); dev keeps the optional `workflow_uuid` for Host upserts. Was: Execution-shaped; not mounted in the product (include_execution_shaped_workflow_routes=False) |
| V1-DEPRECATED | `GET /api/v1/workflows/{workflow_id}` | unilabos/app/scheduler/api.py:208 | - | Not mounted in the product |
| V1-DEPRECATED | `POST /api/v1/workflows/{workflow_id}/cancel` | unilabos/app/scheduler/api.py:217 | POST /api/v1/workflow-tasks/{uuid}/commands type=cancel | Not mounted in the product |

## Inventory and materials

> **Status (branch `fix/p7-c-inventory`, 2026-11-12):** materials.db is now at schema **v7** (v6: V1 inventory
> concepts as tables; v7: lot location columns). All rows below except `GET /api/v1/resources` are ported (see the
> status column). Writes: every dev `materials.v1` mutation and the V1 typed commands go through
> `POST /api/v1/inventory/commands` (raw V1 result, not enveloped); dev's per-operation write routes and the dev reads
> that V1 covers (`/materials/{templates,lots,reservations,instances,links,changes}`) are removed. V1's
> `/materials/{material_uuid}` routes are registered after the remaining dev literal routes. Reagents, samples and
> current substances are roles (`material_content`) over `material_substance`, which stays the only quantity authority.

| class | V1 route | V1 handler | DEV counterpart | note | status (p7-c) |
|---|---|---|---|---|---|
| MISSING | `GET /api/v1/compounds/{cas}` | unilabos/app/scheduler/inventory/reagent_api.py:529 | (partial) GET /api/v1/chemicals?cas= (local data only) | V1 uses an external compound source (PubChem). Used by the V1 console | PORTED: local CAS registry, then optional PubChem (`UNILABOS_PUBCHEM_ADDR`, `off` disables) |
| MISSING | `GET /api/v1/current-substances` | unilabos/app/scheduler/inventory/content_api.py:116 | - |  | PORTED |
| MISSING | `POST /api/v1/current-substances` | unilabos/app/scheduler/inventory/content_api.py:106 | (partial) PUT /api/v1/materials/instances/{uuid}/data substances |  | PORTED |
| MISSING | `DELETE /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:143 | - |  | PORTED |
| MISSING | `GET /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:129 | - |  | PORTED |
| MISSING | `PUT /api/v1/current-substances/{substance_uuid}` | unilabos/app/scheduler/inventory/content_api.py:133 | - |  | PORTED |
| MISSING | `GET /api/v1/inventory/commands/processed` | unilabos/app/scheduler/inventory/api.py:200 | - | DEV has idempotency by command_uuid but no listing | PORTED: V1 typed commands + recent materials.v1 effects |
| MISSING | `GET /api/v1/inventory/contents` | unilabos/app/scheduler/inventory/api.py:169 | (partial) substances inside material data | Container contents | PORTED: substances per container from material_substance |
| MISSING | `GET /api/v1/inventory/health` | unilabos/app/scheduler/inventory/api.py:55 | (trivial) GET /api/v1/health |  | PORTED |
| MISSING | `POST /api/v1/inventory/ingress-reservations` | unilabos/app/scheduler/inventory/ingress_api.py:116 | - | Station ingress | PORTED: materials.db `station_ingress_reservation` |
| MISSING | `GET /api/v1/inventory/ingress-reservations/{reservation_uuid}` | unilabos/app/scheduler/inventory/ingress_api.py:126 | - |  | PORTED |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/cancel` | unilabos/app/scheduler/inventory/ingress_api.py:162 | - |  | PORTED |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/in-transit` | unilabos/app/scheduler/inventory/ingress_api.py:136 | - |  | PORTED |
| MISSING | `POST /api/v1/inventory/ingress-reservations/{reservation_uuid}/receive` | unilabos/app/scheduler/inventory/ingress_api.py:149 | - |  | PORTED: receive = dev move_material into the reserved Site |
| MISSING | `GET /api/v1/inventory/sync/cursors` | unilabos/app/scheduler/inventory/api.py:210 | (partial) POST /api/v1/materials/changes/ack |  | PORTED: read view over the materials ledger acknowledgement |
| MISSING | `GET /api/v1/material-models/{asset_path:path}` | unilabos/app/scheduler/inventory/backend_api.py:444 | - |  | PORTED: raw file route; 404 until a workspace model catalog is wired |
| MISSING | `GET /api/v1/material-shapes` | unilabos/app/scheduler/inventory/backend_api.py:435 | - | Material 3D or 2D shapes | PORTED: empty list until workspace shapes are wired |
| MISSING | `GET /api/v1/material-states/{state_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:408 | - |  | PORTED: `material_state_history` |
| MISSING | `POST /api/v1/materials/sites/resolve-identities` | unilabos/app/scheduler/inventory/backend_api.py:309 | - |  | PORTED |
| MISSING | `GET /api/v1/materials/{material_uuid}/current-substance` | unilabos/app/scheduler/inventory/content_api.py:151 | (partial) GET /api/v1/materials/instances/{uuid} data |  | PORTED |
| MISSING | `GET /api/v1/materials/{material_uuid}/reagent-history` | unilabos/app/scheduler/inventory/reagent_api.py:709 | - | Used by the V1 console | PORTED: `material_content_history` |
| MISSING | `GET /api/v1/materials/{material_uuid}/states` | unilabos/app/scheduler/inventory/backend_api.py:389 | - | No state history | PORTED |
| MISSING | `GET /api/v1/materials/{material_uuid}/substance-history` | unilabos/app/scheduler/inventory/content_api.py:155 | - |  | PORTED |
| MISSING | `GET /api/v1/reagent-history/{history_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:705 | - |  | PORTED |
| MISSING | `POST /api/v1/reagent-infos/batch` | unilabos/app/scheduler/inventory/reagent_api.py:560 | - |  | PORTED |
| MISSING | `POST /api/v1/reagent-infos/import` | unilabos/app/scheduler/inventory/reagent_api.py:573 | - |  | PORTED: JSON / CSV / TSV |
| MISSING | `GET /api/v1/reagent-infos/{reagent_info_uuid}/structure-3d` | unilabos/app/scheduler/inventory/reagent_api.py:600 | - |  | PORTED: returns `pending` (no local 3D generator) |
| MISSING | `GET /api/v1/reagents` | unilabos/app/scheduler/inventory/reagent_api.py:664 | - | Used by the V1 console | PORTED |
| MISSING | `POST /api/v1/reagents` | unilabos/app/scheduler/inventory/reagent_api.py:618 | (partial) lots or material data substances | F05. Used by the V1 console | PORTED: quantity lives in material_substance (role row in material_content) |
| MISSING | `POST /api/v1/reagents/batch` | unilabos/app/scheduler/inventory/reagent_api.py:628 | - |  | PORTED |
| MISSING | `POST /api/v1/reagents/import` | unilabos/app/scheduler/inventory/reagent_api.py:641 | - |  | PORTED |
| MISSING | `DELETE /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:701 | - |  | PORTED |
| MISSING | `GET /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:687 | - |  | PORTED |
| MISSING | `PUT /api/v1/reagents/{reagent_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:691 | - |  | PORTED: cannot go below active content reservations |
| MISSING | `GET /api/v1/samples` | unilabos/app/scheduler/inventory/content_api.py:69 | - |  | PORTED |
| MISSING | `POST /api/v1/samples` | unilabos/app/scheduler/inventory/content_api.py:61 | - |  | PORTED |
| MISSING | `DELETE /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:102 | - |  | PORTED |
| MISSING | `GET /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:90 | - |  | PORTED |
| MISSING | `PUT /api/v1/samples/{sample_uuid}` | unilabos/app/scheduler/inventory/content_api.py:94 | - |  | PORTED |
| MISSING | `GET /api/v1/sites/{site_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:412 | - |  | PORTED |
| MISSING | `GET /api/v1/substance-history/{history_uuid}` | unilabos/app/scheduler/inventory/content_api.py:147 | - |  | PORTED |
| DTO-DIFF | `POST /api/v1/resource-templates` | unilabos/app/scheduler/inventory/backend_api.py:215 | POST /api/v1/resource-templates (registry.py:52) | CONFLICT. Both take {resources:[..]}. V1 returns an id→uuid identity receipt (envelope, code 1000 on error); DEV returns a Registry Authority report with HTTP 400/422 errors. The DEV Host depends on this route | PORTED: identity receipt; Host registry report moved to `POST /api/v1/registry/reports` |
| PATH-DIFF | `POST /api/v1/inventory/commands` | unilabos/app/scheduler/inventory/api.py:59 | M<..> on /api/v1/materials/{templates,lots/inbound,lots/{uuid}/stocktake,reservations,reservations/{uuid}/consume\|release\|quarantine} | V1 command types: template.upsert/delete, inbound, reserve, release, consume, quarantine, reconcile_count. DEV covers them but as one route per operation. Used by the V1 console | PORTED: raw V1 result (not enveloped). Carries every dev `materials.v1` mutation (dev write routes removed) and V1 types reagent.dispense, inventory.template.*, inventory.inbound (lot), inventory.reserve/release/consume/quarantine, inventory.reconcile_count, material.move/detach/set_parent/discard; new inventory.lot.correct, content.*, source.*. Not ported: material.deploy, material.content.set/clear, material.consume, material.mark_used, material.adjust, instance inbound |
| PATH-DIFF | `GET /api/v1/inventory/instances` | unilabos/app/scheduler/inventory/api.py:92 | GET /api/v1/materials/instances | No status filter in DEV. F05 says 'path missing'; the capability exists | PORTED: dev `GET /materials/instances` removed |
| PATH-DIFF | `GET /api/v1/inventory/instances/{edge_uuid}` | unilabos/app/scheduler/inventory/api.py:127 | GET /api/v1/materials/instances/{material_uuid} |  | PORTED: dev `GET /materials/instances/{uuid}` removed |
| PATH-DIFF | `GET /api/v1/inventory/ledger` | unilabos/app/scheduler/inventory/api.py:178 | GET /api/v1/materials/changes |  | PORTED: dev `GET /materials/changes` removed |
| PATH-DIFF | `GET /api/v1/inventory/lots` | unilabos/app/scheduler/inventory/api.py:75 | GET /api/v1/materials/lots | V1 returns {lots:[..]}; DEV returns a plain list | PORTED: dev `GET /materials/lots` removed |
| PATH-DIFF | `GET /api/v1/inventory/lots/{lot_id}` | unilabos/app/scheduler/inventory/api.py:81 | GET /api/v1/materials/lots/{lot_uuid} |  | PORTED: dev route removed |
| PATH-DIFF | `GET /api/v1/inventory/outbox/backlog` | unilabos/app/scheduler/inventory/api.py:185 | GET /api/v1/runtime/backend-events | UNVERIFIED equivalence (upstream outbox) | PORTED: materials ledger delivery status |
| PATH-DIFF | `GET /api/v1/inventory/outbox/events` | unilabos/app/scheduler/inventory/api.py:192 | GET /api/v1/runtime/backend-events | UNVERIFIED | PORTED |
| PATH-DIFF | `GET /api/v1/inventory/relations` | unilabos/app/scheduler/inventory/api.py:165 | GET /api/v1/materials/links |  | PORTED: parent/Site relations + topology links; dev `GET /materials/links` removed |
| PATH-DIFF | `GET /api/v1/inventory/reservations` | unilabos/app/scheduler/inventory/api.py:100 | GET /api/v1/materials/reservations |  | PORTED: lot/material + content reservations + source bindings; dev route removed |
| PATH-DIFF | `GET /api/v1/inventory/reservations/{reservation_id}` | unilabos/app/scheduler/inventory/api.py:142 | GET /api/v1/materials/reservations/{reservation_uuid} |  | PORTED: dev route removed |
| PATH-DIFF | `GET /api/v1/inventory/snapshot` | unilabos/app/scheduler/inventory/api.py:173 | GET /api/v1/graphs/live/payload | UNVERIFIED shape | PORTED |
| PATH-DIFF | `GET /api/v1/inventory/templates` | unilabos/app/scheduler/inventory/api.py:108 | GET /api/v1/materials/templates |  | PORTED: dev `GET /materials/templates` removed |
| PATH-DIFF | `GET /api/v1/inventory/templates/{template_id}` | unilabos/app/scheduler/inventory/api.py:114 | GET /api/v1/materials/templates/{template_uuid} |  | PORTED: dev route removed |
| PATH-DIFF | `GET /api/v1/inventory/workflows/{workflow_id}/reservations` | unilabos/app/scheduler/inventory/api.py:155 | GET /api/v1/materials/reservations?task_uuid= |  | PORTED |
| PATH-DIFF | `GET /api/v1/materials` | unilabos/app/scheduler/inventory/backend_api.py:286 | GET /api/v1/materials/instances | DEV has no pagination, no barcode filter and exact name match only. Used by the V1 console | PORTED |
| PATH-DIFF | `POST /api/v1/materials` | unilabos/app/scheduler/inventory/backend_api.py:278 | POST /api/v1/materials/trees \| /instantiate (M<..>) | Direct body vs mutation envelope. Used by the V1 console | PORTED: dev `/materials/trees` and `/instantiate` moved into `/inventory/commands` |
| PATH-DIFF | `GET /api/v1/materials/graph` | unilabos/app/scheduler/inventory/backend_api.py:305 | GET /api/v1/graphs/live/payload | Node-link shape is incompatible. F05 calls it 'missing'. Used by the V1 console | PORTED: dev `/graphs` (Graph Authority) kept |
| PATH-DIFF | `DELETE /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:343 | DELETE /api/v1/materials/instances/{material_uuid} (M<MaterialDelete>) |  | PORTED |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:315 | GET /api/v1/materials/instances/{material_uuid} | Used by the V1 console | PORTED: registered after dev literal routes |
| PATH-DIFF | `PUT /api/v1/materials/{material_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:319 | PATCH /api/v1/materials/instances/{uuid}; PUT .../position; PUT .../data |  | PORTED |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/ledger` | unilabos/app/scheduler/inventory/backend_api.py:354 | GET /api/v1/materials/changes (global, no per-material filter) | Used by the V1 console | PORTED |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/sites` | unilabos/app/scheduler/inventory/backend_api.py:369 | GET /api/v1/materials/instances/{uuid} (aggregate includes sites, UNVERIFIED) |  | PORTED |
| PATH-DIFF | `POST /api/v1/materials/{material_uuid}/states` | unilabos/app/scheduler/inventory/backend_api.py:378 | PUT /api/v1/materials/instances/{uuid}/data | Append in V1, overwrite in DEV | PORTED |
| PATH-DIFF | `GET /api/v1/materials/{material_uuid}/states/latest` | unilabos/app/scheduler/inventory/backend_api.py:404 | GET /api/v1/materials/instances/{uuid} (data) |  | PORTED |
| PATH-DIFF | `GET /api/v1/reagent-infos` | unilabos/app/scheduler/inventory/reagent_api.py:541 | GET /api/v1/chemicals | Used by the V1 console | PORTED: over the `chemical` table; dev `/chemicals` kept (dev-only expected_version writes) |
| PATH-DIFF | `POST /api/v1/reagent-infos` | unilabos/app/scheduler/inventory/reagent_api.py:533 | POST /api/v1/chemicals | Fields are near-identical (cas, name, aliases, formula, smiles, inchi_key, mw, density, physical_state). DEV names the key chemical_uuid | PORTED |
| PATH-DIFF | `DELETE /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:614 | DELETE /api/v1/chemicals/{chemical_uuid}?expected_version= | DEV requires expected_version | PORTED: 4000 while referenced |
| PATH-DIFF | `GET /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:596 | GET /api/v1/chemicals/{chemical_uuid} |  | PORTED |
| PATH-DIFF | `PUT /api/v1/reagent-infos/{reagent_info_uuid}` | unilabos/app/scheduler/inventory/reagent_api.py:604 | PATCH /api/v1/chemicals/{chemical_uuid} | Method differs; DEV requires expected_version | PORTED |
| PATH-DIFF | `GET /api/v1/resource-templates` | unilabos/app/scheduler/inventory/backend_api.py:219 | GET /api/v1/materials/templates | DEV has no page, keyword or resource_type params, returns a plain list, and names the key template_uuid where V1 uses uuid. Used by the V1 console | PORTED |
| PATH-DIFF | `DELETE /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:274 | DELETE /api/v1/materials/templates/{template_uuid} (InventoryMutation body) |  | PORTED |
| PATH-DIFF | `GET /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:234 | GET /api/v1/materials/templates/{template_uuid} | Plain DTO vs V1 envelope | PORTED |
| PATH-DIFF | `PUT /api/v1/resource-templates/{template_uuid}` | unilabos/app/scheduler/inventory/backend_api.py:238 | PUT /api/v1/materials/templates/{template_uuid} (M<ResourceTemplateWrite>) | V1 edits only display fields | PORTED |
| PATH-DIFF | `GET /api/v1/resources` | unilabos/app/web/api.py:1256 | GET /api/v1/materials/instances | Uses the legacy Resp envelope | NOT PORTED: legacy `Resp` page API, not called by the V1 console; use `/materials` or `/inventory/instances` |
| V1-DEPRECATED | `POST /api/v1/edge/material/query` | unilabos/app/scheduler/inventory/api.py:234 | GET /api/v1/materials/instances/{uuid}/tree; /instances/by-resource-id/{id} | Compat route 'already used by HostNode' | SKIPPED (V1-DEPRECATED) |

## Devices and registry

> **Status (branch `fix/p7-a-authoring`, 2026-11-12):** all rows ported. The device views are composed on the authority
> from device root materials + active registry + Host endpoint capabilities (`services/runtime/devices.py`); the
> `/device-state*` views run on the Host (edge-proxy prefix `device-state`). Dev's broader routes stay as dev-only
> capabilities: `/runtime/endpoints`, `/telemetry/*`, `/registry/entries/*`. `POST /workflow-tasks
> execution_kind=ad_hoc_device_action` is left to the task-lifecycle branch, which owns `/workflow-tasks`.

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/authoring/device-catalog` | unilabos/app/web/api.py:1278 | (partial) GET /api/v1/workflows/template-catalog devices; /registry/entries; /runtime/endpoints | **Done (p7-a)**: `services/runtime/devices.py` composes device root materials + active registry + Host endpoint capabilities; envelope `{code:0,data}` instead of legacy `Resp`. Tests: `tests/server/test_v1_devices_api.py`. Was: F06. Used by the V1 console |
| MISSING | `GET /api/v1/devices` | unilabos/app/web/api.py:1301 | (partial) GET /api/v1/runtime/endpoints + /materials/instances | **Done (p7-a)**: Same composition; Backend `DeviceOverview` shape, action `workflow_node_template_uuid` uses V1 `template_identity` namespaces. Was: F05/F06. V1 joins the edge registration with device materials. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-node-templates` | unilabos/app/workflow_template_api.py:299 | (partial) GET /api/v1/workflow-publications; /registry/entries; graph.node_templates | **Done (p7-a)**: Registry device actions projected into runtime.db `workflow_node_template`/`workflow_handle_template` (`authority_id=registry-template-projection`, V1 deterministic UUIDs, typed v2 contracts via `compile_action_template_handles`); structural templates (condition/repeat_until/material_io) are not projected because dev's control-node params differ. Was: F06. Used by the V1 console |
| MISSING | `GET /api/v1/workflow-node-templates/{template_uuid}` | unilabos/app/workflow_template_api.py:326 | - | **Done (p7-a)**: `{template, handles}`; unknown → 3002. Was: Used by the V1 console |
| PATH-DIFF | `GET /api/v1/actions` | unilabos/app/web/api.py:1383 | GET /api/v1/runtime/endpoints | **Done (p7-a)**: Authority-side view; `/runtime/endpoints` stays as the dev-only full endpoint snapshot. |
| PATH-DIFF | `POST /api/v1/device-action-runs` | unilabos/app/workflow_api.py:1851 | POST /api/v1/workflow-tasks execution_kind=ad_hoc_device_action | **Done (p7-a)**: material_uuid → device id via materials, template → action name/type; runs dev's ad-hoc device action task; `{task, job, created}` 201/200. `POST /workflow-tasks execution_kind=ad_hoc_device_action` left to the task-lifecycle branch. Was: V1 identifies the action by material_uuid + workflow_node_template_uuid; DEV by device_id + action_name. V1 returns {created, ...} with 201 or 200. F05/F06. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/device-state` | unilabos/app/scheduler/api.py:442 | GET /api/v1/telemetry/states | **Done (p7-a)**: `api/runtime/device_state.py` over the Host's `TelemetryDeviceStateProjection`; `device-state` added to the edge-proxy prefixes. `/telemetry/*` stays as dev-only telemetry query surface. Was: Plain DTO on both sides |
| PATH-DIFF | `GET /api/v1/device-state/history` | unilabos/app/scheduler/api.py:448 | GET /api/v1/telemetry/events | **Done (p7-a)**:  |
| PATH-DIFF | `POST /api/v1/device-state/report` | unilabos/app/scheduler/api.py:473 | POST /api/v1/telemetry/events | **Done (p7-a)**: Scalar values only (1000 otherwise). |
| PATH-DIFF | `GET /api/v1/device-state/{device_id:path}` | unilabos/app/scheduler/api.py:464 | GET /api/v1/telemetry/states/{endpoint_uuid}/{device_uuid} | **Done (p7-a)**: Unknown → 4001. Was: DEV also needs endpoint_uuid |
| PATH-DIFF | `GET /api/v1/device-state/{device_id:path}/history` | unilabos/app/scheduler/api.py:453 | GET /api/v1/telemetry/events (device filter UNVERIFIED) | **Done (p7-a)**:  |
| PATH-DIFF | `GET /api/v1/devices/{device_id}/actions` | unilabos/app/web/api.py:1350 | GET /api/v1/runtime/endpoints (action_capabilities) | **Done (p7-a)**: Accepts the device id or the device material UUID. |
| PATH-DIFF | `GET /api/v1/devices/{device_id}/actions/{action_name}/schema` | unilabos/app/web/api.py:1366 | GET /api/v1/registry/entries/{name} | **Done (p7-a)**: Registry declaration first, Host capability descriptor as fallback; unknown → 4001. Was: Schema location UNVERIFIED |
| PATH-DIFF | `POST /api/v1/job/add` | unilabos/app/web/api.py:1403 | POST /api/v1/workflow-tasks {execution_kind:'ad_hoc_device_action', device_id, action_name, param} | **Done (p7-a)**: Envelope `{jobId, status}` (1 = accepted), runs as an ad-hoc device action task. Was: Legacy Resp; still documented in V1 developer_guide/http_api.md |
| PATH-DIFF | `GET /api/v1/job/{id}/status` | unilabos/app/web/api.py:1396 | GET /api/v1/workflow-tasks/{task_uuid}; GET /api/v1/workflow-node-jobs/{uuid} | **Done (p7-a)**: Maps dev job status to V1 `JobData.status`; unknown job → status 0. Was: Legacy Resp; still documented in V1 developer_guide/http_api.md |
| PATH-DIFF | `GET /api/v1/online-devices` | unilabos/app/web/api.py:1337 | GET /api/v1/runtime/endpoints?state=online | **Done (p7-a)**: Online = Host endpoint online with a route/capability for the device. Was: Legacy Resp envelope in V1; Host data, proxied in DEV |

## Error handling

| class | V1 route | V1 handler | DEV counterpart | note |
|---|---|---|---|---|
| MISSING | `GET /api/v1/stations/{station_id}/control-commands/{command_uuid}` | unilabos/app/error_handling_api.py:335 | - | **Ported (p7-b): runtime.db v10 station model (workflow_station_control / _control_command / workflow_error_session / workflow_station_error); Idempotency-Key + request hash + operator; station/session/decision version CAS; enveloped (4001/3003/1000/5001 instead of HTTP 404/409/422/503).** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/control-commands/{command_uuid}/apply` | unilabos/app/error_handling_api.py:341 | - | **Ported (p7-b): replays a PENDING cancel/retry with its saved identity.** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/error-handling` | unilabos/app/error_handling_api.py:180 | (partial) GET /api/v1/error-decisions, /status-incidents, /scheduler/physical-recovery | **Ported (p7-b): snapshot (mode, station_version, error_epoch, snapshot_id, active_session, errors, manual_actions, control_commands, event_cursor). A station error opens with the persisted intervention when the scheduler marks an attempt decision-pending.** No station snapshot with versions and error epoch. Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/events` | unilabos/app/error_handling_api.py:407 | (partial) SSE GET /api/v1/events | **Ported (p7-b): station.error_handling.changed window over frontend_event, after cursor + reset_required.** Station control-operation log |
| MISSING | `GET /api/v1/stations/{station_id}/manual-action-jobs/{job_uuid}/review` | unilabos/app/error_handling_api.py:316 | - | **Ported (p7-b).** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-action-jobs/{job_uuid}/review` | unilabos/app/error_handling_api.py:322 | - | **Ported (p7-b): review_version is a hash of the job outcome; stored in job control_data.manual_review.** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/manual-actions/{command_id}` | unilabos/app/error_handling_api.py:275 | - | **Ported (p7-b): {execution_job, execution_task, source_task_uuid}.** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-actions/{command_id}/review` | unilabos/app/error_handling_api.py:301 | - | **Ported (p7-b).** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/manual-actions/{command_id}/stop` | unilabos/app/error_handling_api.py:281 | (partial) POST /api/v1/workflow-tasks/{uuid}/commands type=cancel | **Ported (p7-b): cancels the manual ad-hoc task through the V1 cancel command.** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/resource-usage` | unilabos/app/error_handling_api.py:380 | (partial) GET /api/v1/scheduler/resources, /workflow-resource-intervals | **Ported (p7-b): {captured_at, occupancies, material_bindings: [], jobs}.** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}` | unilabos/app/error_handling_api.py:204 | - | **Ported (p7-b): session + decision + manual actions.** DEV has no manual-handling sessions |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/complete` | unilabos/app/error_handling_api.py:210 | - | **Ported (p7-b).** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}/devices/{device_id}/actions` | unilabos/app/error_handling_api.py:244 | (partial) GET /api/v1/runtime/endpoints action_capabilities | **Ported (p7-b): from the Host endpoint action_capabilities (action_version = descriptor_hash).** Used by the V1 console |
| MISSING | `GET /api/v1/stations/{station_id}/sessions/{session_id}/inventory` | unilabos/app/error_handling_api.py:351 | - | **Ported (p7-b): instances/sites read from MaterialsService (inventory domain owns the model).** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/inventory-corrections` | unilabos/app/error_handling_api.py:360 | (partial) POST /api/v1/scheduler/inventory-recovery/reconcile | **Ported (p7-b): applied as MaterialsService.move_material per change (actor human), position_version CAS.** Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/manual-actions` | unilabos/app/error_handling_api.py:255 | (partial) POST /api/v1/workflow-tasks execution_kind=ad_hoc_device_action | **Ported (p7-b): an error_manual ad-hoc device task (always_free); the only work dispatched while the station is PAUSED.** Not bound to a session or error. Used by the V1 console |
| MISSING | `POST /api/v1/stations/{station_id}/sessions/{session_id}/return` | unilabos/app/error_handling_api.py:228 | - | **Ported (p7-b).** Used by the V1 console |
| DTO-DIFF | `GET /api/v1/error-decisions` | unilabos/app/scheduler/api.py:492 | GET /api/v1/error-decisions | **Ported (p7-b): V1 list key decisions; DEV category / material_execution kept on physical-settlement items.** V1 returns {decisions:[..]}; DEV returns {items:[..]} plus category and material_execution |
| DTO-DIFF | `POST /api/v1/error-decisions/{decision_id}` | unilabos/app/scheduler/api.py:498 | POST /api/v1/error-decisions/{decision_id} | **Ported (p7-b): body exactly V1 ErrorDecisionIn (payload assembled as V1; no action → device side defaults abort, as V1); success {decision_id, status: delivered}; not pending → V1 404 → 4001 (GENERIC profile). Optional DEV extensions: Idempotency-Key replay, operator_id, physical-settlement guard (3003).** V1 returns status=delivered and 404 when not pending. DEV returns status=resolved and 409, and the action defaults to abort. physical_settlement allows only operator_intervention or abort |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/errors/{decision_id}/decision` | unilabos/app/error_handling_api.py:184 | POST /api/v1/error-decisions/{decision_id} | **Ported (p7-b): cancel_task / retry_current_node delivered as DEV abort / retry through the /error-decisions channel (+ task cancel); enter_manual_handling opens a session and pauses the station; 202.** DEV has no Idempotency-Key header, no station/decision version or error-epoch CAS, and no enter_manual_handling action. Used by the V1 console |
| PATH-DIFF | `GET /api/v1/stations/{station_id}/resource-dispositions` | unilabos/app/error_handling_api.py:384 | GET /api/v1/workflow-resource-intervals + /workflow-device-tenancies | **Ported (p7-b): execution locks (claims), resource intervals, device tenancies with versions.** UNVERIFIED equivalence. Used by the V1 console |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/resource-dispositions` | unilabos/app/error_handling_api.py:390 | POST /api/v1/workflow-resource-intervals/{owner_uuid}/release; /workflow-device-tenancies/{uuid}/release | **Ported (p7-b): releases the selected occupancies with version CAS; 202.** V1 uses Idempotency-Key and returns 202. UNVERIFIED equivalence |
| PATH-DIFF | `POST /api/v1/stations/{station_id}/resume` | unilabos/app/error_handling_api.py:413 | POST /api/v1/workflow-tasks/{uuid}/commands type=resume; POST /api/v1/scheduler/drain/resume | **Ported (p7-b): operator_confirmed + confirm_snapshot_id; wakes every active task.** Station-wide in V1, per-task in DEV. Used by the V1 console |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity` | unilabos/app/workflow_api.py:2106 | POST /api/v1/scheduler/inventory-recovery/reconcile | UNVERIFIED |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/resolve-uncertain` | unilabos/app/workflow_api.py:2090 | POST /api/v1/scheduler/physical-recovery/{job_uuid}/settle\|release; /error-decisions/{id} | UNVERIFIED equivalence |
| PATH-DIFF | `POST /api/v1/workflow-node-jobs/{job_uuid}/settle-material-transfer` | unilabos/app/workflow_api.py:2119 | POST /api/v1/scheduler/physical-recovery/{job_uuid}/settle; /materials/executions/settle | **Ported (p7-b): actual_change_set {kind: material_transfer, material_uuid, target_owner_material_uuid, target_site_uuid} → one PhysicalMove receipt on DEV physical recovery (version = current frozen record). DEV /scheduler/physical-recovery/{job}/settle|release stay (DEV-only, explicit CAS + aliquots).** UNVERIFIED. Used by the V1 console |

## Workspace and lifecycle

> **Status (branch `fix/p7-c-inventory`, 2026-11-12):** lab, package mounts, file browser, `/health` and `/readiness`
> are ported. The four Workspace Host routes (`/v1/snapshot`, `/v1/operations`, `/v1/operations/{id}`,
> `/v1/logs/{component}`) remain: dev has no separate workspace supervisor process.

| class | V1 route | V1 handler | DEV counterpart | note | status (p7-c) |
|---|---|---|---|---|---|
| MISSING | `GET /api/v1/file-browser` | unilabos/app/web/api.py:1185 | - | Listed under Workspace in the V1 manual. Uses the legacy Resp envelope | PORTED: Host route behind the edge proxy (`file-browser` prefix); V1 envelope instead of legacy Resp; confined to the working dir |
| MISSING | `POST /api/v1/lab/demo` | unilabos/app/scheduler/inventory/layout.py:485 | - | Demo seeding | PORTED: seeds zones, device placements, demo templates and zone-assigned lots |
| MISSING | `POST /api/v1/lab/placements` | unilabos/app/scheduler/inventory/layout.py:467 | (partial) PUT /api/v1/materials/instances/{uuid}/position |  | PORTED: materials.db `lab_placement` |
| MISSING | `DELETE /api/v1/lab/placements/{subject_id}` | unilabos/app/scheduler/inventory/layout.py:474 | - |  | PORTED |
| MISSING | `GET /api/v1/lab/profile` | unilabos/app/scheduler/inventory/layout.py:439 | - |  | PORTED: includes dev grid document as `grid` |
| MISSING | `PUT /api/v1/lab/profile` | unilabos/app/scheduler/inventory/layout.py:443 | - |  | PORTED: `grid` writes dev grid (revision CAS), `grid: null` resets |
| MISSING | `GET /api/v1/lab/warehouse` | unilabos/app/scheduler/inventory/layout.py:451 | - |  | PORTED |
| MISSING | `GET /api/v1/workspace/package-mounts` | unilabos/app/workspace_authoring_api.py:25 | (analog) GET /api/v1/driver-packages | The Workspace Host probes it for readiness | PORTED: `workspace-package-mounts/v1` projection over the dev driver-package ledger (`editable` is always false); Host route behind the edge proxy |
| MISSING | `POST /v1/operations` | unilabos/workspace_host/host.py:2201 | (partial) POST /api/v1/restart, /api/v1/reset, /device-processes/{id}/start\|stop\|restart | Material layout preview/apply has no DEV analog | NOT PORTED (remaining): Workspace Host supervisor. Dev analogs: `/restart`, `/reset`, `/device-processes/*`; material layout preview/apply maps to dev `/materials/snapshots/compare` + `apply_material_snapshot` command |
| MISSING | `GET /v1/operations/{operation_id}` | unilabos/workspace_host/host.py:2176 | - |  | NOT PORTED (remaining): see `/v1/operations` |
| MISSING | `GET /v1/snapshot` | unilabos/workspace_host/host.py:2173 | (partial) /api/v1/health, /readiness, /restart | Workspace Host supervisor state | NOT PORTED (remaining): dev analogs `/health`, `/readiness`, `/restart` |
| DTO-DIFF | `GET /api/v1/health` | unilabos/app/scheduler/api.py:550 | GET /api/v1/health | Standalone scheduler app only, not mounted in the product. Same difference as the web /health row | PORTED: same route as the web `/health` row |
| DTO-DIFF | `GET /api/v1/health` | unilabos/app/web/api.py:1266 | GET /api/v1/health | V1 scheduler is ready\|disabled; DEV scheduler is local\|remote and DEV adds execution | PORTED: V1 `scheduler` = ready\|disabled; dev local/remote moved to `scheduler_mode`; `execution` kept; raw (probe) |
| DTO-DIFF | `GET /api/v1/lab/layout` | unilabos/app/scheduler/inventory/layout.py:447 | GET /api/v1/lab/layout | CONFLICT. V1 returns {zones, placements, storage_summary}, a view of placed instances. DEV returns {layout_key, revision, cell_size, zones, walls, *_at_ms}, a grid document | PORTED: V1 placed-instances view (+ `grid`); dev `PUT\|DELETE /lab/layout` removed |
| DTO-DIFF | `GET /api/v1/readiness` | unilabos/app/web/server.py:243 | GET /api/v1/readiness | V1 returns {status: ready\|starting\|failed, capabilities{can_edit_definitions, can_view_drafts, can_run_unpublished, can_debug, can_execute_device_actions}, phase, workflowRuntime, workflowProgress, observability?, error?}. DEV returns {status: ready\|not_ready, scheduler, execution, reasons}. Both use 200/503. The V1 console gates the UI on capabilities | PORTED: V1 `status` ready\|starting\|failed + `capabilities`, `phase`, `workflowRuntime`; dev `scheduler`/`execution`/`reasons` kept as extensions; raw (probe), 200/503 |
| PATH-DIFF | `GET /api/v1/lab/assembly/{subject_id}` | unilabos/app/scheduler/inventory/layout.py:478 | GET /api/v1/materials/instances/{uuid}/tree |  | PORTED: dev `/materials/instances/{uuid}/tree` kept (consistency snapshot) |
| PATH-DIFF | `POST /api/v1/lab/zones` | unilabos/app/scheduler/inventory/layout.py:456 | PUT /api/v1/lab/layout (whole document with revision) | Zone models differ | PORTED |
| PATH-DIFF | `DELETE /api/v1/lab/zones/{zone_id}` | unilabos/app/scheduler/inventory/layout.py:463 | PUT /api/v1/lab/layout |  | PORTED |
| PATH-DIFF | `GET /v1/logs/{component}?maxBytes=` | unilabos/workspace_host/host.py:2180 | GET /api/v1/hostlink/log-sources + /api/v1/hostlink/logs?source_id=&cursor=&limit= |  | NOT PORTED (remaining): dev `/hostlink/log-sources` + `/hostlink/logs` kept |
| SAME | `GET /api/v1/hostlink/peers` | unilabos/app/scheduler/api.py:155 | GET /api/v1/hostlink/peers | DEV adds host_node_id. On the authority the edge proxy forwards it to the Host child (503 while the Host is down) | unchanged (SAME) |
| SAME | `GET /api/v1/scheduler/drain` | unilabos/app/scheduler/api.py:235 | GET /api/v1/scheduler/drain | DEV SchedulerDrainStatus is a superset of V1's fields | unchanged (SAME) |
| SAME | `POST /api/v1/scheduler/drain` | unilabos/app/scheduler/api.py:241 | POST /api/v1/scheduler/drain | Superset | unchanged (SAME) |
| SAME | `POST /api/v1/scheduler/drain/resume` | unilabos/app/scheduler/api.py:247 | POST /api/v1/scheduler/drain/resume | Superset | unchanged (SAME) |
| V1-DEPRECATED | `GET /api/v1/health` | unilabos/app/web/server.py:500 | GET /api/v1/health | Dead code for backend mode | SKIPPED (V1-DEPRECATED) |

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
