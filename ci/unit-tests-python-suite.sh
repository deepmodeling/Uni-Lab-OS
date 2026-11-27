#!/usr/bin/env bash
# 运行无 ROS 的 Python 测试并检查覆盖率。
set -euo pipefail
source "$(dirname "$0")/unit-test-cache.sh"

workers="${PYTEST_WORKERS:-$(nproc 2>/dev/null || echo 1)}"
echo "pytest workers: $workers"

image_prefix=/opt/unilab-ci
if [[ -x "$image_prefix/bin/python" && -r "$image_prefix/dependencies.sha256" ]] \
  && image_key="$("$image_prefix/bin/python" ci/python-suite-image-key.py)" \
  && [[ "$image_key" == "$(<"$image_prefix/dependencies.sha256")" ]]; then
  py="$image_prefix/bin/python"
  echo "python env: prebuilt dependencies ($image_key)"
  phase_start=$SECONDS
  "$py" -m pip install --disable-pip-version-check --no-index --no-deps --no-build-isolation .
  "$py" -m pip check
else
  echo "python env: provisioning from the current checkout"
  cache_root="$(unit_test_cache_root)"
  mkdir -p "$cache_root"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$cache_root/.lock"
    flock 9
  fi
  echo "unit-test cache: $cache_root"

  req_key="$(unit_test_sha256 unilabos/utils/requirements.txt)"
  setup_key="$(unit_test_sha256 setup.py)"
  env_key="${req_key}-${setup_key}-py311-xdist1-numpy1"
  phase_start=$SECONDS
  env_line="$(unit_test_prepare_env python-suite "$env_key")"
  env_state="${env_line%% *}"
  prefix="${env_line#* }"
  py="$prefix/bin/python"
  export PATH="$prefix/bin:$PATH"
  echo "python env ${env_state}: $prefix ($((SECONDS - phase_start))s)"
  phase_start=$SECONDS
  if [[ "$env_state" == miss ]]; then
    # 驱动需要 NumPy 1.x，pandas 3.1 需要 NumPy 2.x。提前限定兼容范围，避免回溯下载。
    "$py" -m pip install --no-cache-dir -r unilabos/utils/requirements.txt \
      'numpy<2' 'pandas<3.1' pytest pytest-cov pytest-xdist .
    unit_test_commit_env "$prefix" "$env_key"
  else
    "$py" -m pip install --disable-pip-version-check --no-deps .
  fi
fi
echo "pip install: $((SECONDS - phase_start))s"

mapfile -t paths < <(grep -vE '^(#|[[:space:]]*$)' tests/ci_python_suite.txt)
mapfile -t ignores < <(grep -vE '^(#|[[:space:]]*$)' tests/ci_ros_required.txt)
ignore_args=()
for item in "${ignores[@]}"; do
  ignore_args+=(--ignore="$item")
done
"$py" -m pytest -q -n "$workers" --dist loadfile "${paths[@]}" "${ignore_args[@]}" \
  --cov=unilabos \
  --cov-branch \
  --cov-report=term \
  --cov-report=json:repository-coverage.json
"$py" scripts/check_python_suite_coverage.py \
  repository-coverage.json \
  --statements 67 \
  --branches 54
