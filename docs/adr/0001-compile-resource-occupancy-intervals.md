# Compile resource occupancy intervals before dispatch

Uni-Lab OS will compile Action defaults, Workflow-root resources, lexical resource scopes, and legacy device tenancy into one Resource Occupancy Interval model before dispatch. The runtime will reuse the existing all-or-none leases, claims, fences, and recovery machinery to execute a Static Acyclic Resource Plan; legacy `device_tenancy` remains a compatibility input rather than a second long-lived ownership model, so nested ownership and failure release have one set of rules.
