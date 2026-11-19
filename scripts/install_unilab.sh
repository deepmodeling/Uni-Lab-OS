#!/bin/bash
set -e

echo "================================================"
echo "Uni-Lab-OS Environment Installation Script"
echo "================================================"
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Find conda installation
echo "Searching for conda installation..."
CONDA_BASE=""

# Try to find conda in PATH
if command -v conda &> /dev/null; then
    CONDA_BASE=$(conda info --base)
    echo "Found conda at: $CONDA_BASE"
elif [ -d "$HOME/miniforge3" ]; then
    CONDA_BASE="$HOME/miniforge3"
    echo "Found conda at: $CONDA_BASE"
elif [ -d "$HOME/miniconda3" ]; then
    CONDA_BASE="$HOME/miniconda3"
    echo "Found conda at: $CONDA_BASE"
elif [ -d "$HOME/anaconda3" ]; then
    CONDA_BASE="$HOME/anaconda3"
    echo "Found conda at: $CONDA_BASE"
elif [ -d "/opt/conda" ]; then
    CONDA_BASE="/opt/conda"
    echo "Found conda at: $CONDA_BASE"
else
    echo "ERROR: Could not find conda installation!"
    echo "Please make sure conda/mamba is installed."
    exit 1
fi

echo ""

# Initialize conda for this shell
if [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    source "$CONDA_BASE/etc/profile.d/conda.sh"
fi

# Set target environment path
ENV_NAME="${1:-unilab}"
if [[ ! "$ENV_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "ERROR: Use only letters, numbers, '-' or '_' in the environment name."
    exit 1
fi
ENV_PATH="$CONDA_BASE/envs/$ENV_NAME"

# Check if environment already exists
if [ -e "$ENV_PATH" ]; then
    echo "ERROR: Environment already exists at $ENV_PATH. Nothing was removed."
    echo "Use a new name: bash install_unilab.sh unilab-hostlink"
    exit 1
fi

# Find the packed environment file
shopt -s nullglob
PACK_FILES=(unilab-env*.tar.gz)
if [ "${#PACK_FILES[@]}" -ne 1 ]; then
    echo "ERROR: Expected exactly one unilab-env*.tar.gz file!"
    echo "Please make sure the packed environment file is in the same directory as this script."
    exit 1
fi
PACK_FILE="${PACK_FILES[0]}"

echo "Found packed environment: $PACK_FILE"
echo ""

# Extract the packed environment
echo "Extracting environment to $ENV_PATH..."
mkdir -p "$ENV_PATH"
tar -xzf "$PACK_FILE" -C "$ENV_PATH"

echo ""
echo "Unpacking conda environment..."
echo "Changing to environment directory: $ENV_PATH"
cd "$ENV_PATH"

# Run conda-unpack from the environment directory
if [ -f "bin/conda-unpack" ]; then
    echo "Running: ./bin/conda-unpack"
    ./bin/conda-unpack
elif [ -f "bin/activate" ]; then
    echo "Running: source bin/activate followed by conda-unpack"
    source bin/activate
    conda-unpack
else
    echo "ERROR: Could not find bin/conda-unpack or bin/activate!"
    echo "Current directory: $(pwd)"
    echo "Expected location: $ENV_PATH/bin/"
    exit 1
fi

echo ""
echo "Checking Uni-Lab-OS entry point..."
# Check if unilab script exists in bin directory
UNILAB_SCRIPT="$ENV_PATH/bin/unilab"
if [ ! -f "$UNILAB_SCRIPT" ]; then
    echo "ERROR: Entry point is missing. The archive is incomplete."
    exit 1
else
    echo "Found: $UNILAB_SCRIPT"
fi

conda activate "$ENV_PATH"
"$ENV_PATH/bin/python" "$SCRIPT_DIR/verify_installation.py" --assert-no-ros
"$UNILAB_SCRIPT" --help

echo ""
echo "================================================"
echo "Installation completed successfully!"
echo "================================================"
echo ""
echo "To activate the environment, run:"
echo "  conda activate $ENV_NAME"
echo ""
echo "or"
echo ""
echo "  source $ENV_PATH/bin/activate"
echo ""
echo "You can verify the installation by running:"
echo "  cd $SCRIPT_DIR"
echo "  python verify_installation.py --assert-no-ros"
echo ""

