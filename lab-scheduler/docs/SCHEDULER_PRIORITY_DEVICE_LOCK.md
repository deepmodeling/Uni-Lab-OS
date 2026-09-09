# Device-lock admission and priority ordering

This scheduler treats device locks as a hard admission constraint.

For every dispatch round it:

1. collects DAG nodes that are ready and whose submission time has arrived;
2. checks `DevicePool.get_available(machine_type, current_time)`;
3. removes only the currently locked nodes from that round's dispatch
   candidate list;
4. applies the algorithm's priority/heuristic ordering to the remaining
   candidates; and
5. re-checks the device immediately before acquiring it in `_do_allocate`.

The important distinction is that a locked node is **not** deleted, cancelled,
or permanently moved to the end of a global queue. It stays `ready` in its
`TaskDAG` and is reconsidered after a completion event advances the clock and
releases the device. This means an urgent action cannot preempt an in-flight
action, but it can become the next action as soon as its device is available.

Devices of different types remain independent. For example, an urgent action
waiting for device `A` does not prevent a lower-priority action using free
device `B` from starting at the current time.

The behavior is covered by
`tests/test_step_algorithms.py::TestHeuristicAlgorithms::test_device_lock_is_checked_before_priority`.
