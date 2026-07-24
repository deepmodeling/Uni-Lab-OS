#!/usr/bin/env bash
set -euxo pipefail

# make sure pip is available
"$PREFIX/bin/python" -m pip install --upgrade pip

# install extra deps
"$PREFIX/bin/python" -m pip install paho-mqtt opentrons_shared_data
"$PREFIX/bin/python" -m pip install git+https://github.com/Xuwznln/pylabrobot.git
