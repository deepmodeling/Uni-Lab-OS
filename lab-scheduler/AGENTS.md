# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Laboratory experiment scheduling microservice. Optimally schedules experiment steps (DAGs) across shared lab devices, minimizing weighted completion time while respecting precedence, device capacity, time-window, and sample-transfer constraints.

Part of the LabOS platform. See parent `../AGENTS.md` for monorepo-wide conventions.

## Commands

```bash
# Install
pip install -e ".[dev]"          # editable install with test deps
pip install ortools              # needed by CP-SAT solver (not in pyproject.toml)

# Run
uvicorn scheduler.main:app --host 0.0.0.0 --port 8090 --reload

# Test
pytest                           # all tests
pytest tests/test_step_algorithms.py  # single file
pytest tests/test_cp_solver.py -k "test_chain"  # single test by name
pytest tests/test_biolab_benchmark.py  # benchmarks with Gantt chart output

# Docker
docker build -t uni-lab-scheduler .
docker run -p 8090:8090 uni-lab-scheduler
```

No lint/format tooling is configured yet. pytest config is in `pyproject.toml` (`asyncio_mode = "auto"`).

## Architecture

```
API (FastAPI)  ->  Service (SchedulerService)  ->  Core (Algorithms + Solvers)  ->  Models (dataclasses)
```

- **Entry point**: `src/scheduler/main.py` — FastAPI app, CORS, lifespan
- **API routes** (`src/scheduler/api/router.py`): `POST /api/v1/schedule`, `POST /api/v1/reschedule`, `GET /api/v1/algorithms`, `GET /api/v1/health`
- **API schemas** (`src/scheduler/api/schemas.py`): Pydantic v2 request/response models (Step, Task, Machine, Robot, Resources, ScheduleRequest, etc.)
- **Service** (`src/scheduler/service/scheduler_service.py`): orchestrates DAG building, device pool construction, algorithm execution, sample flow analysis, transfer scheduling
- **Config** (`src/scheduler/config.py`): `pydantic-settings` with `SCHEDULER_` env prefix (port, default_algorithm, default_transfer_time, cors_origins)

### Core scheduling (`src/scheduler/core/`)

**SchedulerBase** (`base.py`): abstract base using event-driven discrete simulation with a min-heap event queue. Subclasses implement `_schedule_ready_tasks()`.

**Algorithm registry** (`step_algorithms.py`): `@register_algorithm("name")` decorator populates `ALGORITHM_REGISTRY`. Seven heuristic algorithms: Greedy, CriticalPath, WeightedCriticalPath (default), DynamicPriority, MultiObjective, Realtime, HybridCriticality.

**Exact solvers**:
- `cp_solver.py` — OR-Tools CP-SAT with interval variables, NoOverlap constraints, time windows, alternative device routing. Supports batch processing via `batch_capacity`.
- `dp_solver.py` — bitmask DP for small instances (N <= 20), used as optimality benchmark
- `ga_solver.py` — Genetic Algorithm with permutation encoding, Order Crossover (OX), and event-driven simulation decoder. Registered as "GA".

**Batch scheduling** (`batch_factory.py`): `make_batch_scheduler()` wraps any heuristic algorithm to handle batch processing. Auto-registers "Batch_*" variants for all heuristics.

**Validators** (`validators.py`): `validate_time_constraints()` checks min_gap/max_gap feasibility in DAGs.

**Transfer scheduling** (`coupled_solver.py`, `transfer_scheduler.py`, `sample_flow.py`): CoupledSolver iteratively couples step scheduling with robot transfer scheduling. `analyze_sample_flow()` detects cross-device transfers. `TransferScheduler` does greedy batched robot assignment.

### Models (`src/scheduler/models/`)

Pure dataclasses: `StepNode`, `Edge`, `TimeConstraint`, `TaskDAG` (dag.py); `DeviceInstance`, `DevicePool` (resources.py); `ScheduledStep`, `ScheduledTransfer`, `ScheduleObjective` (schedule_result.py).

**TaskDAG fields**: `task_id`, `priority` (enum: LOW/NORMAL/HIGH/URGENT), `weight` (float, for weighted completion time), `submitted_at` (timestamp).

**DeviceInstance fields**: `device_type`, `batch_capacity` (int, slots for parallel batch processing).

### Legacy code

`src/mix/` and `src/mix_real/` are earlier prototypes. The current `SchedulerBase` was refactored from `mix_real.py`. These are not part of the deployed service.

## Key Patterns

- **Strategy + Registry**: algorithms register via decorator, selected by name at runtime
- **Template Method**: `SchedulerBase.schedule()` is the concrete loop; subclasses override `_schedule_ready_tasks()`
- **Pydantic at API boundary, dataclasses internally**: API schemas in `api/schemas.py`, internal models in `models/`
- **Auto-import for registry**: `core/__init__.py` imports all algorithm modules so decorators run at import time
- **Public API exports**: `core/__init__.py` exports 14 symbols via `__all__` for library usage: `SchedulerBase`, `ALGORITHM_REGISTRY`, `get_algorithm`, `list_algorithms`, `register_algorithm`, `make_batch_scheduler`, `CPSATScheduler`, `ExactDPScheduler`, `GeneticAlgorithmScheduler`, `decode_permutation`, `validate_time_constraints`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULER_PORT` | 8090 | Server port |
| `SCHEDULER_DEFAULT_ALGORITHM` | WeightedCriticalPath | Algorithm when none specified |
| `SCHEDULER_DEFAULT_TRANSFER_TIME` | 5 | Default transfer time (minutes) |
| `SCHEDULER_CORS_ORIGINS` | ["*"] | CORS allowed origins |

## Notes

- Python >= 3.11 required (uses `X | None` syntax)
- `ortools` is an implicit dependency used by `cp_solver.py` but not listed in `pyproject.toml`
- Comments are in simplified Chinese
- `test/` (singular) contains legacy stubs; actual tests are in `tests/` (plural)
