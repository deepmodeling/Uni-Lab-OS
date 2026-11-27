#!/usr/bin/env bash
set -euo pipefail

checkout=$(mktemp -d)
container=''
cleanup() {
  if [[ -n "$container" ]]; then
    docker rm -f "$container" >/dev/null || true
  fi
  rm -rf -- "$checkout"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker pull "$PYTHON_TEST_IMAGE"

# 远端 Docker 服务无法挂载 Runner 的工作目录。传入浅克隆以保留 git ls-files。
git -C "$checkout" init --quiet
git -C "$checkout" fetch --quiet --depth=1 "file://$CI_PROJECT_DIR" "$CI_COMMIT_SHA"
git -C "$checkout" checkout --quiet --detach FETCH_HEAD

container=$(docker create --interactive --workdir /workspace \
  --env CI=true \
  --env "PYTEST_WORKERS=$PYTEST_WORKERS" \
  --env MAMBA_ROOT_PREFIX=/workspace/.mamba \
  --env CONDA_PKGS_DIRS=/workspace/.conda-pkgs \
  --env MPLBACKEND=Agg --env PYTHONUTF8=1 --env PYTHONIOENCODING=utf-8 \
  "$PYTHON_TEST_IMAGE" bash -c 'set -euo pipefail; tar -xf -; exec bash ci/unit-tests-python-suite.sh')
tar -C "$checkout" -cf - . | docker start --attach --interactive "$container"
test "$(docker inspect --format '{{.State.ExitCode}}' "$container")" -eq 0
