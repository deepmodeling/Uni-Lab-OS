# Limit task edits to workflow entry parameters

Status: accepted

Task-level editing is limited to public Workflow Entry Parameters, including
quantities derived from those values, and is permitted only inside a Paused Edit
Window. Node Action parameters, material and site bindings, device contracts,
and workflow topology remain owned by the published Workflow revision; once a
node crosses the Node Dispatch Boundary, its payload and consumed entry values
are immutable. This preserves physical evidence and execution-lock continuity
while allowing corrections to the undispatched suffix after the dispatch gate
has been closed. For an active task, the user must pause it before opening this
edit window; in-flight Jobs need not finish before the edit, and resuming the
task revalidates and continues the revised suffix.

If an entry parameter has been captured by any dispatched node, that parameter
is frozen for the whole task. A pending Job that also uses it cannot receive a
new value, because one task must not silently contain two values for the same
consumed input.

An edit may change values in existing undispatched Jobs, including an allowed
quantity value, but it must preserve the Task Plan Shape: node identities,
topology, control structure, and inventory requirement identities cannot be
added, removed, or remapped during a running Task.

Claims and execution locks already held by dispatched Jobs remain until their
normal settlement. The paused undispatched suffix acquires no new execution
resources; continuing the Task performs fresh admission for that suffix.

This scope assumes one operator edits a Task at a time; multi-writer conflict
detection is deferred until that use case exists.
