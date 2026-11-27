#!/usr/bin/env bash
# 对齐 GitHub CI Check 的 scheduler-core-test。由 unit_tests_scheduler_core 调用。
set -euo pipefail
# 镜像拉取由 Runner 完成；这里配置容器启动后的依赖下载源，允许 CI 变量覆盖。
# pip 和 APT 默认使用共用脚本中的阿里源，Conda 与 micromamba 默认使用中科大源。
export CONDA_FORGE_CHANNEL="${CONDA_FORGE_CHANNEL:-https://mirrors.ustc.edu.cn/anaconda/cloud/conda-forge}"
source "$(dirname "$0")/unit-test-cache.sh"

workers="$(nproc 2>/dev/null || echo 1)"
echo "pytest workers: $workers"

requirements=tests/scheduler_core/requirements.txt
image_python=/opt/unilab-ci/bin/python
if [[ -x "$image_python" ]] \
  && "$image_python" ci/python-suite-image-key.py --verify-requirements "$requirements" pytest-xdist; then
  py="$image_python"
  echo "python env: prebuilt scheduler dependencies"
else
  echo "python env: provisioning scheduler dependencies from the current checkout"
  cache_root="$(unit_test_cache_root)"
  mkdir -p "$cache_root"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$cache_root/.lock"
    flock 9
  fi
  echo "unit-test cache: $cache_root"

  req_key="$(unit_test_sha256 "$requirements")"
  env_key="${req_key}-py311-xdist1"
  env_line="$(unit_test_prepare_env scheduler-core "$env_key")"
  env_state="${env_line%% *}"
  prefix="${env_line#* }"
  py="$prefix/bin/python"
  export PATH="$prefix/bin:$PATH"
  echo "python env ${env_state}: $prefix"
  if [[ "$env_state" == miss ]]; then
    "$py" -m pip install --no-cache-dir -r "$requirements" pytest-xdist
    unit_test_commit_env "$prefix" "$env_key"
  fi
fi

"$py" -m pytest -q -n "$workers" --dist loadfile tests/scheduler_core
"$py" -m pytest -q -n "$workers" --dist loadfile tests/scheduler_core \
  -k "not fifty_tasks_with_eight_jobs" \
  --cov=unilabos.app.scheduler.ordering \
  --cov=unilabos.app.scheduler.resource_wait_policy \
  --cov=unilabos.workflow.execution_wait_graph \
  --cov-branch \
  --cov-report=term \
  --cov-report=json:scheduler-core-coverage.json
"$py" scripts/check_scheduler_core_coverage.py \
  scheduler-core-coverage.json \
  --statements 85 \
  --branches 75
