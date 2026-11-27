#!/bin/bash
set -e

echo "================================================"
echo "UniLabOS Environment Installation Script"
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
ENV_NAME="unilab"
ENV_PATH="$CONDA_BASE/envs/$ENV_NAME"

# Check if environment already exists
if [ -d "$ENV_PATH" ]; then
    echo "WARNING: Environment '$ENV_NAME' already exists at $ENV_PATH"
    read -p "Do you want to overwrite it? (y/n): " OVERWRITE
    if [ "$OVERWRITE" != "y" ] && [ "$OVERWRITE" != "Y" ]; then
        echo "Installation cancelled."
        exit 0
    fi
    echo "Removing existing environment..."
    rm -rf "$ENV_PATH"
fi

# Find the packed environment file
PACK_FILE=$(ls unilab-env*.tar.gz 2>/dev/null | head -n 1)

if [ -z "$PACK_FILE" ]; then
    echo "ERROR: Could not find unilab-env*.tar.gz file!"
    echo "Please make sure the packed environment file is in the same directory as this script."
    exit 1
fi

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
echo "Checking UniLabOS entry point..."
# Check if unilab script exists in bin directory
UNILAB_SCRIPT="$ENV_PATH/bin/unilab"
if [ ! -f "$UNILAB_SCRIPT" ]; then
    echo "WARNING: unilab script not found, creating it..."
    cat > "$UNILAB_SCRIPT" << 'EOF'
#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re
import sys

from unilabos.app.main import main

if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\.pyw?|\.exe)?$', '', sys.argv[0])
    sys.exit(main())
EOF
    chmod +x "$UNILAB_SCRIPT"
    echo "Created: $UNILAB_SCRIPT"
else
    echo "Found: $UNILAB_SCRIPT"
fi

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
echo "  python verify_installation.py"
echo ""

