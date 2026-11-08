# Install the public v1.0 source

This guide installs the open-source serial Task edition from the `feat/schedule` branch. It does not use the older Uni-Lab Conda package workflow.

## Requirements

- Python 3.11 or newer.
- Stable Rust toolchain and Cargo, to build the native serial scheduler.
- For the full ROS 2 runtime, a supported ROS 2 environment and matching `unilabos_msgs` package. Mamba/Conda may be used for those ROS dependencies; the Uni-Lab source itself is installed with pip.

The scheduler-only build and CLI help do not require ROS 2. Starting the full device runtime, including the no-hardware mock Task in the package guide, requires the ROS 2 runtime dependencies supported by the selected platform.

## Get the source

```bash
git clone --branch feat/schedule https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

## Choose an environment

For scheduler development and CLI checks without a ROS 2 runtime, create and activate a virtual environment:

```bash
python -m venv .venv
```

For the full ROS 2 runtime, use one Mamba/Conda environment that contains Python 3.11 and the ROS 2 distribution plus matching `unilabos_msgs` supported by your OS. Keep that environment active for the pip commands below; do not create a separate venv inside it. The source checkout is still installed with pip, not as a Uni-Lab Conda package.

Activate the virtual environment for the scheduler/CLI-only path.

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

## Install the source and build the scheduler

```bash
python -m pip install --upgrade pip
python -m pip install -e .
python native/scheduler-queue/build.py --destination unilabos/scheduling/queue_native
unilab --help
```

The editable install provides `unilab`, `unilab-supervisor`, and `unilab-mcp`. The scheduler build creates the platform-specific native library used by the default `queue` engine. Runtime startup does not compile it.

For the full Python runtime and device integrations, install the repository's additional Python requirements in the same environment:

```bash
python -m pip install -r unilabos/utils/requirements.txt
```

If using the ROS 2 backend, install a ROS 2 distribution and matching `unilabos_msgs` package for your OS before starting the runtime. Use Mamba/Conda only for these ROS dependencies if that is your platform's supported installation route. Do not install a separate Uni-Lab Conda package over this source checkout.

To enable the optional MCP command, install the extra:

```bash
python -m pip install -e ".[mcp]"
```

## Verify the scheduler build

Rust tests:

```bash
cargo test --locked --manifest-path native/scheduler_shared/Cargo.toml
cargo test --locked --manifest-path native/scheduler-queue/Cargo.toml
```

Python acceptance suite on Linux/macOS:

```bash
UNILAB_TEST_CLEAN_INSTALL=1 python -m pytest -q tests/scheduling
```

PowerShell:

```powershell
$env:UNILAB_TEST_CLEAN_INSTALL = "1"
python -m pytest -q tests/scheduling
```

The [external device package guide](../developer_guide/external_device_packages.md) shows how to create a no-hardware mock package and run its first Task after installing the full runtime dependencies.

For fixed queue behavior and old scheduling-variable migration, see the [serial Task release note](../scheduler/release.md).
