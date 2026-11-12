# Keep pause task-scoped

Status: accepted

Pause and resume apply to one Workflow Task at a time. A pause closes only that
Task's dispatch gate, preserves in-flight execution and its resource ownership,
and does not provide a batch transaction or a device-level stop; this keeps
control and audit facts independent for each execution.

The CP-SAT behavior for this gate is defined in
[ADR 0004](0004-cp-sat-pause-invalidation.md): pending candidates become
invalid, while already admitted Jobs remain fixed and continue to settle.
