#!/usr/bin/env bash
set -euo pipefail

python -m autopep8 \
  --in-place \
  --recursive \
  --max-line-length=120 \
  scripts \
  tests/szlab \
  unilabos \
  unilabos_local_ui \
  unilabos_msgs

