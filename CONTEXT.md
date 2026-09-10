# Edge Resource and Workflow Context

This context defines the domain language shared by the frontend, Backend, and
Edge microbackend. Backend is the canonical public contract; Edge provides a
local adapter to the same concepts.

## Language

**Shared Interface**:
The public resource and workflow contract presented identically by Backend and
Edge, including route meaning, field spelling, enums, envelopes, and errors.
_Avoid_: Similar API, Edge-shaped response, frontend fallback

**Authority**:
The single selected owner allowed to accept changes for an aggregate during a
given operation; replicas and projections do not become additional owners.
_Avoid_: Last writer wins, dual truth, nearest database

**Material UUID**:
The sole stable identity of a Material, referred to as `material_uuid` by
relationships and workflow fields.
_Avoid_: Edge UUID, cloud UUID, instance UUID

**Material Composition**:
The structural parent-child relationship expressed by a Material's
`parent_uuid`.
_Avoid_: Site occupancy, laboratory layout, scheduler claim

**Site**:
A stable named position owned by one Material; `Site.uuid` identifies the
position itself, never its owner or occupant.
_Avoid_: Material UUID, slot name, PLR index

**Site Occupancy**:
The optional relationship from a Site to the Material currently placed there,
expressed by `occupied_material_uuid`.
_Avoid_: Material Composition, Site identity, execution lock

**Soft Deletion**:
The lifecycle state in which an aggregate retains its UUID and history while
normal reads exclude it after `deleted_at` becomes non-null.
_Avoid_: Physical row deletion, empty status, hidden alias

**Workflow**:
A reusable persisted graph definition.
_Avoid_: One execution, runtime snapshot, Run

**Workflow Task**:
One execution created from a frozen Workflow graph.
_Avoid_: Workflow definition, Run alias

**Edge-only Inventory Interface**:
The operational lot, reservation, ledger, and diagnostic contract used inside
Edge; it is not part of the Shared Interface.
_Avoid_: Frontend fallback, Backend-compatible route
