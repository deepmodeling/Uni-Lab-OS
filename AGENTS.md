# AGENTS.md

This file provides guidance for coding agents working in this repository.

## Build & Development

```bash
# Default installation needs only Python 3.14; ROS 2 is explicitly optional
python scripts/dev_install.py  # builds companion PLR / Opentrons wheels; no PyPI upload
python scripts/dev_install.py --extras full  # docs, tests, driver SDKs and development tools

# Run with a device graph
unilab --graph <graph.json> --config <config.py>  # HostLink, no ROS runtime or messages
unilab --graph <graph.json> --config <config.py> --backend ros2  # requires unilabos-ros2

# Common CLI flags
unilab --test_mode                        # simulate hardware, no real execution
unilab --check_mode                       # CI validation of registry imports
unilab --skip_env_check                   # skip auto-install of dependencies
unilab --visual rviz|web|disable          # visualization mode
unilab --is_slave                         # run as slave node
unilab --role backend                     # scheduler authority and runtime.v1 control plane, without devices or ROS

# Workflow upload subcommand
unilab workflow upload -f <workflow.json> -n <name> --tags tag1 tag2

# Tests
pytest tests/                              # all tests
pytest tests/resources/test_resourcetreeset_groups.py  # single test file
pytest tests/resources/test_resourcetreeset_groups.py::test_name  # single test
```

## Architecture

### Startup Flow

`unilab` CLI → `unilabos/app/cli/parser.py:build_parser()` → `app/cli/router.py` handles lightweight subcommands (`package` included) → `unilabos/app/main.py:main()` loads config and starts device runtime only when no CLI subcommand handled the request. Default `unilab` is a two-process tree (`app/backend_main.py`, `app/supervisor.py`): the `unilab` process itself is the scheduling authority (Scheduler / Workflow / Registry / Materials, owns the `--port` management API the browser connects to) and directly supervises the Host child (devices, HostLink, telemetry, driver packages; started with `--address` pointing back at the authority, databases under `<root>/edge`). The Host child binds no HTTP port: it only makes outbound HTTP calls (materials / registry / workflow reports, response delivery) and keeps the runtime.v1 control WebSocket. Host-only routes are served by `server/api/edge_proxy.py` sending a `backend_http` notice over that WebSocket; the Host runs the request against its own ASGI app in-process and posts the result to `POST /api/v1/edge/http-responses/{request_uuid}`. Payload pulls, restart notification and the device relay use the same channel, so the browser sees one address. `POST /api/v1/restart` defaults to `scope=edge`: only the Host child exits (code 75) and is relaunched; the authority and its port stay up, and it rejects `scope=process` for itself. `--role backend` runs the authority alone; `--address` runs a top-level Host against a remote authority (only then a thin supervisor from `app/supervisor.py` wraps it for whole-process restarts); `--no-safe-restart` collapses everything into one process (debugging); `--is_slave` and `--check_mode` never get any wrapper. One process owns one database root: before opening any database, `app/main.py:_claim_process_database_root` takes an exclusive OS lock on `<root>/unilab.lock` (`server/database/root_lock.py`; the authority holds `<root>`, its Host child `<root>/edge`, `--role backend` `<root>/backend`, each managed Slave its own `device_processes/<id>/database`; `--check_mode` takes none), re-entrant within the process and released by the OS on exit; a second instance exits with code 73. A `hostlink` Host opens its HostLink port right after config load, before the registry, graph and devices (`backend/hostlink/startup.py:prebind_host_server`, called from `app/main.py`); `HostLinkBackend.start()` adopts that server and gates Slave requests until `materials` (authority attached) and then `devices` (local devices and host services started) are ready, answering "Host 启动中" (`not_ready`) that Slaves retry within `HostLinkConfig.host_wait_timeout`. Runtime then builds the registry, loads the device graph (node-link JSON, registered with the Graph Authority), and starts `hostlink` or `ros2`. HostLink's local driver executor is `unilabos.backend.hostlink.local_runtime.HostLinkLocalRuntime`.

### Core Layers

**Registry** (`unilabos/registry/`): Singleton `Registry` class discovers and catalogs all device types, resource types, and communication devices from YAML definitions. Device types live in `registry/devices/*.yaml`, resources in `registry/resources/`, comms in `registry/device_comms/`. The registry resolves class paths to actual Python classes via `utils/import_manager.py`.

**Resource Tracking** (`unilabos/resources/resource_tracker.py`): Pydantic-based `ResourceDict` → `ResourceDictInstance` → `ResourceTreeSet` hierarchy. `ResourceTreeSet` is the canonical in-memory representation of all devices and resources, used throughout the system, and it owns every resource conversion: flat dicts (`from_raw_dict_list`, `dump`/`load`) and PLR (`from_plr_resources`, `to_plr_resources`). PLR drafts from a registry resource class come from `resources/materials.py:instantiate_registry_resource`. There is no separate graph I/O module: device graphs are node-link JSON only (GraphML is not supported) and are turned into runtime structures by the Graph Authority (`server/services/materials/graph.py:load_graph_payload`).

**Device Drivers**: vendor drivers (PRCXI, LaiYu, Bioyond, HPLC, balances, arms, …) are not in this repository. They live in external driver packages, installed with `unilab package install` (`app/cli/package.py`, `server/services/driver_packages.py`) or mounted with `--devices`; create new ones with `.cursor/skills/create-device-package`. A driver is a Python class with `@device` / `@action` decorators that the runtime wraps as a HostLink or ROS 2 device node. Driver packages that define PLR resource or backend classes outside their registry entries register them with `register_plr_module()` / `@register_plr_class` from `resources/plr_additional_res_reg.py`, so `ResourceTreeSet.to_plr_resources()` can deserialize them. `unilabos/devices/` keeps only the built-ins the framework depends on: `workstation/workstation_base.py` (`WorkstationBase`, `WorkStationContainer`), `virtual/*` (used by the protocol compilers, `--test_mode`, example graphs and tests), `liquid_handling/liquid_handler_abstract.py` with `rviz_backend.py`, and `ros_dev/liquid_handler_joint_publisher*.py`.

**Device Runtime** (`unilabos/backend/runtime/`): Transport-neutral device execution kernel shared by HostLink and ROS 2 — `DeviceNode`, action routing, resource service, and runtime exceptions (`DeviceActionError`, `DeviceClassInvalid`, `ActionResultError`).

**Transport Layers**: `unilabos/backend/ros2/` wraps device classes as `ROS2DeviceNode` instances; its presets include `host_node`, `workstation`, controller, serial, and camera nodes. `unilabos/backend/hostlink/` provides the HostLink transport runtime plus its own `host_node.py:HostNode` and `workstation.py:WorkstationNode` (the ROS2 counterpart keeps its original name `ROS2WorkstationNode`); `unilabos/backend/dora/` is experimental. The per-backend host/workstation orchestrators share transport-neutral logic from `backend/runtime/` (`host_adapter.py:HostAdapterBase` for bookkeeping/bridge notification/ping-pong/test-mode, `workstation_protocol.py` for protocol name/model resolution and resource expand/write-back) and carry no `@device` decorator. `backend/host_services.py:HostServices` uniquely defines the host service actions; the file is excluded from the default registry AST scan and scanned separately by `Registry._setup_host_node`. Host-to-device material and management requests use the module-level functions in `backend/hostlink/downlink.py` (cross-machine channel is HostLink RPC; ros2 reuses it).

**Experiment Protocols** (`unilabos/experiments/`): `models.py` holds Pydantic parameter models for XDL-style experiment actions (mirroring `unilabos_msgs` ROS actions); `compile/` holds 20+ protocol compilers (add, centrifuge, dissolve, filter, heatchill, stir, pump, etc.) that expand protocol steps into device action sequences at execution time (consumed by the `workstation` preset node). One-shot workflow import converters (`from_xdl.py`, `from_python_script.py`, legacy JSON) live in `scripts/workflow/`, not in the runtime packages.

**Communication** (`unilabos/device_comms/`): Hardware communication adapters that external driver packages build on — OPC-UA client, Modbus PLC, RPC, and a universal driver. `app/communication.py` provides a factory pattern for WebSocket client connections to the cloud.

**Microbackend HTTP API** (`unilabos/server/api/`): FastAPI routers and the Host management application. It exposes runtime, materials, telemetry, history, and scheduler observation APIs on port 8002 by default.

### Configuration System

- **Config classes** in `unilabos/config/config.py`: `BasicConfig`, `WSConfig`, `HTTPConfig`, `ROSConfig` — all class-level attributes, loaded from Python config files
- Config files are `.py` files with matching class names (see `config/example_config.py`)
- Environment variables override with prefix `UNILABOS_` (e.g., `UNILABOS_BASICCONFIG_PORT=9000`)
- Device topology defined in graph files (node-link JSON; GraphML is rejected)

### Key Data Flow

1. `-g <file>.json` → old-format graphs (legacy cloud exports, dev-era examples with `class`/root `position`/`children`) are upgraded once at the `-g` / `unilab graph upload` read boundary by `server/backend/legacy_adaptor/legacy/graph.py` → the Graph Authority (`GraphService.upsert_graph`, `on_existing="adopt"`) assigns identities and stores the snapshot → `app/main.py:_load_startup_graph` feeds the authority payload to `load_graph_payload()`, which returns `LoadedGraph(resource_tree_set, links, physical_setup_graph)`. `-g <name|uuid>` skips the file step and loads the stored snapshot. The Graph Authority accepts the current node-link contract only. `physical_setup_graph` (`nx.Graph`, edges carry `port`) is only for the protocol compilers and is published through `backend/runtime/workstation_protocol.py:physical_setup_graph()`
2. `ResourceTreeSet` + `Registry` → `initialize_device.initialize_device_from_dict()` → `ROS2DeviceNode` instances
3. Device nodes communicate via ROS2 topics/actions or HostLink
4. Backend control notices use `server/backend/legacy_adaptor/websocket.py`; complete state and commands use the microbackend HTTP APIs in `server/api/`

### Test Data

Example device graphs and experiment configs are in `unilabos/test/experiments/` (not `tests/`). Registry test fixtures in `unilabos/test/registry/`.

## Code Conventions

- Code comments and log messages in simplified Chinese
- Python 3.14 only (CPython cp314, not free-threaded), type hints expected
- Pydantic models for data validation (`resource_tracker.py`)
- Singleton pattern via `@singleton` decorator (`utils/decorator.py`)
- Dynamic class loading via `utils/import_manager.py` — device classes resolved at runtime from registry YAML paths
- CLI argument dashes auto-converted to underscores for consistency

## Licensing

- The whole repository, including the built-ins in `unilabos/devices/`, is GPL-3.0.
- Do not add vendor drivers to `unilabos/devices/`: they belong in external driver packages, which carry their own license.
