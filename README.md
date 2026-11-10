<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

<!-- Language switcher -->

**English** | [中文](README_zh.md)

[![GitHub Stars](https://img.shields.io/github/stars/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS is a platform for laboratory automation, designed to connect and control various experimental equipment, enabling automation and standardization of experimental workflows.

## Key Features

- Multi-device integration management
- Automated experimental workflows
- Cloud connectivity capabilities
- Flexible configuration system
- Support for multiple experimental protocols

## Documentation

Detailed documentation can be found at:

- [Online Documentation](https://deepmodeling.github.io/Uni-Lab-OS/)

## Supported Runtime

The default installation and runtime are **Python 3.14 + NumPy 2 + HostLink,
without ROS**. ROS 2 Jazzy/Humble remain explicit optional backends. Ordinary
Python drivers, Host/Slave, Workstation/sub-devices, materials, workflows,
HTTP/WS APIs and MCP do not require ROS. MoveIt, RViz and ROS-native drivers or
image pipelines still require an appropriate ROS environment.

See the [installation guide](docs/user_guide/installation.md) and
[runtime baseline](docs/user_guide/runtime_baseline.md) for the exact boundaries.

## Quick Start

### 1. Install without ROS (default)

From source (works before the new Conda release is published):

```bash
git clone -b dev https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv/Scripts/Activate.ps1
python scripts/dev_install.py
unilab --disable-browser
```

Alternatively, extract the platform-specific companion wheel bundle and run
`python install_wheel_release.py` with Python 3.14. It creates a new environment,
installs all default dependencies offline and verifies an Opentrons 96-well plate.
The patched Opentrons and pinned PLR wheels are release assets, not PyPI uploads.
Bare `pip install unilabos` against PyPI cannot discover these companion wheels;
use the installer or `--find-links <bundle>/wheelhouse`. Source development uses
the helper above to build them first. See the [installation guide](docs/user_guide/installation.md).

For the **0.12.3+ default Conda package**, once published:

```bash
mamba create -n unilab --override-channels -c uni-lab -c conda-forge "unilabos>=0.12.3"
mamba activate unilab
unilab --disable-browser
```

Older Conda releases still include ROS. Do not add a RoboStack channel for the
default installation.

| Package | Purpose |
| --- | --- |
| `unilabos` | Default application and ROS-free dependencies |
| `unilabos-ros2` | Explicit Jazzy/Humble runtime and message extension |
| `unilabos-full` | Complete runtime, ROS desktop, documentation, tests and development tools |

ROS users install the matching `unilabos-ros2` variant in a separate environment
and select `--backend ros2`; see the installation guide for commands. Individual
hardware SDKs belong to their device packages. For source development, use
`python scripts/dev_install.py --extras full`. The only pip extras are `ros2`
and `full`; native ROS is installed separately via Conda/RoboStack.
`unilabos-env` is retired; default dependencies are declared by `unilabos` itself.

### 2. Clone Repository (Optional, for developers)

```bash
# Clone the repository (only needed for development or examples)
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

### 3. Start Uni-Lab

The default launcher starts a microbackend authority and a supervised Host child.
The authority serves the management/HTTP API (default `8002`); the Host serves
HostLink TCP (default `7302`). Neither requires ROS with the default backend.

```bash
# HostLink runtime (the default)
unilab -g path/to/graph.json --backend hostlink --port 8002 --hostlink-port 7302

# ROS 2 Jazzy runtime (activate the Conda environment containing Jazzy first)
mamba activate unilab
unilab -g path/to/graph.json --backend ros2 --port 8002

# Validate/import the registry without starting devices
unilab --check-mode --complete-registry --skip-env-check
```

For a split deployment, start the scheduler/workflow authority separately and
point one or more Edge processes at it:

```bash
# Terminal 1: Backend authority (no device graph or device executor)
unilab --role backend --port 8081

# Terminal 2: Edge execution process
unilab -g path/to/graph.json --backend hostlink \
  --address http://127.0.0.1:8081 --port 8002
```

The Edge can also start with an empty graph (omit `-g`) when devices will be
added through the driver-package or managed-device APIs. A Slave still needs
its own graph and connects with `--is-slave --host-node-ip <host>`.

### 4. Connect to a remote Backend

Use one explicit `--address` for a remote Backend. The value may be the service
root or its `/api/v1` root; `--ak` and `--sk` provide the laboratory
credentials:

```bash
unilab -g path/to/graph.json --backend hostlink \
  --address https://backend.example.com/api/v1 \
  --ak "$AK" --sk "$SK"
```

An Edge speaks only `runtime.v1` to that address: HTTP `/api/v1/*` and the
`/api/v1/ws/schedule` control WebSocket live on the same host and port, and the
Backend owns scheduling, workflows, materials and the registry. The WebSocket
carries short notifications; the complete authoritative content is fetched over
HTTP. `--role backend` starts exactly such an authority.

Compatibility with the old cloud Backend (the `job_start` / `host_node_ready`
message family, `/ws/schedule` on HTTP port `+1`) is not selected on the Edge
any more. It lives in `unilabos.server.backend.legacy_adaptor.legacy` and is
wired explicitly on the Backend side (`BackendSessionFactory.create_legacy_client()`,
`build_legacy_backend_websocket_url()`), so neither address derivation nor the
Edge connection factory branches on a legacy probe.

### 5. Use a custom UI

Uni-Lab is backend-only: an Edge process does not host a SPA. Build or deploy
your UI separately ([OpenLab](https://xuwznln.github.io/OpenLabSite/) is one example), and configure its API base URL to
the management endpoint:

| Deployment | UI API base URL |
|------------|-----------------|
| Local single-process Edge | `http://127.0.0.1:8002` |
| Split deployment | `http://127.0.0.1:8081` (the Backend authority) |
| Remote Backend | the address passed to `--address` |

For debugging, this repository also ships a React debug console in [`frontend/`](frontend/README.md)
(`cd frontend && npm ci && npm run dev`, proxied to `http://127.0.0.1:8002`); OpenLab remains the recommended UI.

Use the generated contract at `/api/openapi.json` and the interactive docs at
`/api/docs`. A Vite-style static UI can be developed and served independently:

```bash
pnpm install
pnpm dev --host 0.0.0.0       # development server
pnpm build
python -m http.server 4173 --directory dist  # simple local preview
```

The exact environment-variable name for the API base is framework-specific;
set it to one of the URLs above and use `/api/v1/...` for typed HTTP calls.
For live invalidation, a browser UI may use SSE/EventSource and then refetch
the authoritative record over HTTP. That browser notification channel is
separate from the Backend↔Edge `control.v1` WebSocket. Do not point a browser
UI at the HostLink TCP port `7302`.

External device packages can be installed from an exact Git revision and are
mounted on the next process restart:

```bash
unilab package install \
  "git+https://github.com/<org>/<device-package>.git@<commit-sha>"
```

### 6. Best Practice

See [Best Practice Guide](https://deepmodeling.github.io/Uni-Lab-OS/user_guide/best_practice.html)

For the current microbackend architecture, HTTP API catalog, workflow and material contracts,
real-time channels, Python/CLI and MCP integration, see the
[Complete Interface Guide](docs/developer_guide/interfaces/index.md).

## Reference Driver Implementations

Seven runnable example device packages are maintained as standalone GitHub repositories (generated
from [LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate)). Clone any of them, load it
with `--devices <pkg> --external_devices_only`, and read it when writing your own drivers:

| Example repository | Demonstrates |
|--------------------|--------------|
| [LabDeviceLanDemo](https://github.com/Xuwznln/LabDeviceLanDemo) | Cross-device `@subscribe` + remote `call_device_action` LAN closed loop (hub/sub as two processes) |
| [LabDeviceWorkstationDemo](https://github.com/Xuwznln/LabDeviceWorkstationDemo) | `hardware_interface` proxy — multiple sub-devices share one communication endpoint: shared serial (default IO method names) and Modbus `extra_info` (per-device `slave_id` injection) |
| [LabDeviceExceptionDemo](https://github.com/Xuwznln/LabDeviceExceptionDemo) | Exception propagation driven entirely through web-style workflow submission (`/api/v1/workflow-tasks` + `/api/v1/error-decisions`): an exception escaping the action boundary is held for an `abort` / `operator_intervention` decision, a point-to-point `call_device_action` error is caught on the caller side as a workflow node, business-level guarded returns, and an operator-replaced result letting the task finish `succeeded` |
| [LabDeviceMaterialsDemo](https://github.com/Xuwznln/LabDeviceMaterialsDemo) | Host/slave dual process — `@device(available_sites=...)` fixed sites (declaration → registry template → authoritative site instances → occupancy), `@resource` labware with the `materials.*` CRUD facade across HostLink, and `SiteSlot` action parameters (frontend Site picker uuid or label shorthand); outbound plate + fill entirely over the web-style HTTP API — `POST /materials/instantiate` two plates per item, `POST /materials/lots/inbound` (deliberately too little) water by quantity, `POST /workflows` + `PUT graph` a three-node graph (`host_node/apply_deduct_resource` with a `material` requirement and `mount_resource={"name": ...}` referencing the deck by name only → device `fill_well` with a `lot` requirement → report); the first submission fails whole-task reservation with `plan_not_executable` (neither plate nor water left reserved, device never called), restock and resubmit succeeds — plate `active → in_use` mounted cross-process on the slave deck, lot deducted, well contents in the authority |
| [LabDeviceLockDemo](https://github.com/Xuwznln/LabDeviceLockDemo) | Scheduler lock semantics made observable through concurrently submitted workflows: the `(device, action)` action lock serializes two `occupy` calls in submission order (the second shows up `waiting` with `blockers` in `/api/v1/scheduler/resources`), `@action(always_free=True)` lets two `peek` calls of the same action overlap, and `materials_need_lock=["plate"]` locks per authoritative plate uuid (two devices on one plate serialize, one device on two plates runs in parallel); a `lock_auditor` node reads the probes' ledgers and fails the task if any conclusion does not hold |
| [LabDeviceInventoryDemo](https://github.com/Xuwznln/LabDeviceInventoryDemo) | Quantity-based inventory through workflows: a registry `@resource` reagent template, `restock` as the web's `POST /api/v1/materials/lots/inbound` (100 ml into a fixed lot), a `dispense` step whose `inventory=[...]` requirement is reserved all-or-nothing at task start and deducted right before the action (device reports the lot after deduction, `60 / 60 / 0`), and a 500 ml requirement refused at reservation time — task `failed` / `plan_not_executable` ("short by 440 ml"), node `canceled`, device never called, lot unchanged |
| [LabDeviceComplexWorkflowDemo](https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo) | **Runtime control flow** in workflows — loop containers executed round by round by the scheduler (each round is a new attempt of the body nodes, `trigger=loop_iteration`): `with ctx.loop_for(3)` with `{{loop.iteration}}` generating sample ids, `ctx.loop_while(ctx.device_state("reactor", "temperature_c", "<", 80))` polling a device state field with an empty body while the device heats in the background ("wait until a state"), `ctx.loop_while(ctx.step_output("取样检测", "ready", "==", False))` referencing a probe step inside the body ("repeat until ready"), nested `for` over plates × wells, and a final template chaining all three into one procedure verified by a single report |

Every example starts its devices with `unilab -g` and then runs workflows through the management
HTTP API: a package's `@workflow` functions are **workflow templates** (reported with the registry,
`GET /api/v1/registry/workflow-templates`), instantiated with role bindings via
`POST /api/v1/workflows/from-template` and then run with `POST /api/v1/workflow-tasks`; each
repository README ships a step-by-step launch
tutorial with verified output, and every package carries a terminating dual-runtime smoke
(`python -m <pkg>.smoke --backend hostlink|ros2`). All seven are also verified end to end in this
repository's CI. For the underlying communication-sharing mechanism see
[Best Practice Guide §11.5](https://deepmodeling.github.io/Uni-Lab-OS/user_guide/best_practice.html);
to write a new driver from scratch see [Add Device](https://deepmodeling.github.io/Uni-Lab-OS/developer_guide/add_device.html).

## Message Format

Uni-Lab-OS uses pre-built `unilabos_msgs` for system communication. You can find the built versions on the [GitHub Releases](https://github.com/deepmodeling/Uni-Lab-OS/releases) page.

## Citation

If you use [Uni-Lab-OS](https://arxiv.org/abs/2512.21766) in academic research, please cite:

```bibtex
@article{gao2025unilabos,
    title = {UniLabOS: An AI-Native Operating System for Autonomous Laboratories},
    doi = {10.48550/arXiv.2512.21766},
    publisher = {arXiv},
    author = {Gao, Jing and Chang, Junhan and Que, Haohui and Xiong, Yanfei and
              Zhang, Shixiang and Qi, Xianwei and Liu, Zhen and Wang, Jun-Jie and
              Ding, Qianjun and Li, Xinyu and Pan, Ziwei and Xie, Qiming and
              Yan, Zhuang and Yan, Junchi and Zhang, Linfeng},
    year = {2025}
}
```

## License

This project uses a dual licensing structure:

- **Main Framework**: GPL-3.0 - see [LICENSE](LICENSE)
- **Device Drivers** (`unilabos/devices/`): DP Technology Proprietary License

See [NOTICE](NOTICE) for complete licensing details.

## Project Statistics

### Stars Trend

<a href="https://star-history.com/#deepmodeling/Uni-Lab-OS&Date">
  <img src="https://api.star-history.com/svg?repos=deepmodeling/Uni-Lab-OS&type=Date" alt="Star History Chart" width="600">
</a>

## Contact Us

- GitHub Issues: [https://github.com/deepmodeling/Uni-Lab-OS/issues](https://github.com/deepmodeling/Uni-Lab-OS/issues)
