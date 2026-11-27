#!/usr/bin/env bash
# 在 GitLab macOS ARM64 Runner 上构建、验证并发布 Uni-Lab-OS 安装器。
set -euo pipefail

usage() {
  echo "Usage: $0 --repo PATH --expected-commit SHA --release-tag vX.Y.Z --build-root PATH --artifact-dir PATH --uploader PATH --version-preparer PATH --oss-precheck PATH --notifier PATH --oss-public-url PATH" >&2
  exit 2
}

repo=""
expected_commit=""
release_tag=""
build_root=""
artifact_dir=""
uploader=""
version_preparer=""
oss_precheck=""
notifier=""
oss_public_url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --expected-commit) expected_commit="$2"; shift 2 ;;
    --release-tag) release_tag="$2"; shift 2 ;;
    --build-root) build_root="$2"; shift 2 ;;
    --artifact-dir) artifact_dir="$2"; shift 2 ;;
    --uploader) uploader="$2"; shift 2 ;;
    --version-preparer) version_preparer="$2"; shift 2 ;;
    --oss-precheck) oss_precheck="$2"; shift 2 ;;
    --notifier) notifier="$2"; shift 2 ;;
    --oss-public-url) oss_public_url="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$repo" && -n "$expected_commit" && -n "$release_tag" && -n "$build_root" && -n "$artifact_dir" ]] || usage
[[ -n "$uploader" && -n "$version_preparer" && -n "$oss_precheck" && -n "$notifier" && -n "$oss_public_url" ]] || usage

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || {
  echo "This job requires a native macOS ARM64 Runner." >&2
  exit 1
}
repo="$(cd "$repo" && pwd -P)"
for file in "$uploader" "$version_preparer" "$oss_precheck" "$notifier" "$oss_public_url"; do
  [[ -f "$file" ]] || { echo "Missing CI helper: $file" >&2; exit 1; }
done
commit="$(git -C "$repo" rev-parse --verify 'HEAD^{commit}')"
[[ "$commit" == "$expected_commit" ]] || {
  echo "CI checkout is not at the requested commit: expected $expected_commit, got $commit" >&2
  exit 1
}
if [[ ! "$release_tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.-]+)?)$ ]]; then
  echo "Release tag must use vMAJOR.MINOR.PATCH format, for example v0.3.0." >&2
  exit 1
fi
version="${BASH_REMATCH[1]}"
for name in OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET OSS_ENDPOINT OSS_PREFIX; do
  [[ -n "${!name:-}" ]] || { echo "Missing CI variable: $name" >&2; exit 1; }
done
[[ "$OSS_PREFIX" == oss://* ]] || { echo "OSS_PREFIX must start with oss://" >&2; exit 1; }

command -v conda >/dev/null || { echo "Missing conda command." >&2; exit 1; }
conda_base="$(conda info --base)"
# shellcheck source=/dev/null
source "$conda_base/etc/profile.d/conda.sh"
conda activate constructor-build
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export PYTHONNOUSERSITE=1
export CONDA_SOLVER=libmamba
for tool in npm node python constructor micromamba rattler-build xcrun; do
  command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(process.arch!=="arm64" || a<22 || (a===22 && b<13)) throw Error("macOS ARM64 Node.js >=22.13 is required")'
xcrun --show-sdk-path >/dev/null

build_root="$(python -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$build_root")"
artifact_dir="$(python -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$artifact_dir")"
[[ ! -e "$build_root" ]] || { echo "Build root already exists: $build_root" >&2; exit 1; }
mkdir -p "$build_root" "$artifact_dir"
channel_dir="$build_root/channel"
installer_dir="$build_root/installer"
mkdir -p "$channel_dir" "$installer_dir"
exec > >(tee "$build_root/build.log") 2>&1
trap 'status=$?; if [[ "$status" -ne 0 ]]; then echo "macOS build failed; log retained at $build_root/build.log" >&2; fi' EXIT

echo "Release tag: $release_tag"
echo "Source commit: $commit"
echo "Build directory: $build_root"
cd "$repo"
python "$version_preparer" --repo "$repo" --tag "$release_tag"
python -m pip install --no-cache-dir oss2
oss_release_prefix="${OSS_PREFIX%/}/installer/${version}/osx-arm64"
python "$oss_precheck" --prefix "$oss_release_prefix"

export UNILABOS_INSTALLER_VERSION="$version"
export UNILABOS_INSTALLER_PACKAGE=unilabos
export UNILABOS_INSTALLER_CHANNEL="$(python -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().as_uri())' "$channel_dir")"
export CONSTRUCTOR_CACHE="$build_root/constructor-cache"
export SOURCE_COMMIT="$commit"
export SOURCE_TAG="$release_tag"
export BUILD_DIRECTORY="$build_root"
export SOURCE_DIRECTORY="$repo"
unset UNILABOS_INSTALLER_BUILD
conda_exe="$(command -v micromamba)"

# 两个 recipe 的远程限定会绕过本地 channel。仅改 CI 临时 checkout，
# 使当前 tag 生成的消息包和环境包能够参与安装器求解。
python - <<'PY'
import difflib
import os
from pathlib import Path

changes = []
for name, old, new in (
    (".conda/base/recipe.yaml", "uni-lab::unilabos-env", "unilabos-env"),
    (".conda/environment/recipe.yaml", "uni-lab::ros-humble-unilabos-msgs", "ros-humble-unilabos-msgs"),
):
    path = Path(name)
    before = path.read_text()
    if before.count(old) == 0 and before.count(new) == 1:
        continue
    if before.count(old) != 1:
        raise SystemExit(f"Recipe does not match expected content: {name}")
    after = before.replace(old, new)
    path.write_text(after)
    changes.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=name, tofile=name))
Path(os.environ["BUILD_DIRECTORY"], "packaging-overrides.diff").write_text("".join(changes))
PY

pushd "$repo" >/dev/null
npm --prefix frontend ci
npm --prefix frontend run build
test -s unilabos/app/web/static/console/index.html

recipes=(
  recipes/msgs/recipe.yaml
  .conda/environment/recipe.yaml
  .conda/vendor/rfc8785/recipe.yaml
  .conda/vendor/opcua/recipe.yaml
  .conda/vendor/msgcenterpy/recipe.yaml
  .conda/vendor/pylabrobot/recipe.yaml
  .conda/base/recipe.yaml
)
for recipe in "${recipes[@]}"; do
  echo "Building Conda package: $recipe"
  rattler-build build -r "$recipe" --target-platform osx-arm64 \
    --channel "$channel_dir" -c uni-lab -c robostack -c robostack-staging -c conda-forge \
    --output-dir "$channel_dir" --build-string-prefix "g${commit:0:8}" \
    --test native --log-style plain
done

package_build="$(python - "$channel_dir" <<'PY'
import os
import sys
from pathlib import Path

prefix = f"unilabos-{os.environ['UNILABOS_INSTALLER_VERSION']}-"
packages = list(Path(sys.argv[1], "osx-arm64").glob(prefix + "*.conda"))
if len(packages) != 1:
    raise SystemExit(f"Expected exactly one runtime package, found: {packages}")
print(packages[0].name[len(prefix):-len('.conda')])
PY
)"
export UNILABOS_INSTALLER_BUILD="$package_build"
echo "Building installer with runtime package: unilabos-$version-$package_build"
constructor .conda/constructor --platform osx-arm64 --conda-exe "$conda_exe" --output-dir "$installer_dir"
installer="$installer_dir/Uni-Lab-OS-$version-osx-arm64.sh"
test -s "$installer"

# 在隔离前缀验证，避免错误地从当前 checkout 导入 unilabos。
test_prefix="$build_root/smoke-env"
bash "$installer" -b -p "$test_prefix"
popd >/dev/null
smoke_script="$build_root/smoke-test.py"
cat > "$smoke_script" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

import rclpy
import unilabos

expected_version = os.environ["UNILABOS_INSTALLER_VERSION"]
runtime_root = Path(unilabos.__file__).resolve().parent
environment_root = Path(sys.prefix).resolve()
source_console = Path(os.environ["SOURCE_DIRECTORY"]) / "unilabos/app/web/static/console/index.html"
runtime_console = runtime_root / "app/web/static/console/index.html"

print(f"Smoke expected version: {expected_version}")
print(f"Smoke installed version: {unilabos.__version__}")
print(f"Smoke Python prefix: {environment_root}")
print(f"Smoke imported runtime: {runtime_root}")
if unilabos.__version__ != expected_version:
    raise SystemExit("Smoke failure: installed runtime version does not match the release tag.")
if not runtime_root.is_relative_to(environment_root):
    raise SystemExit("Smoke failure: unilabos was not imported from the isolated installer environment.")
if not runtime_console.is_file() or not source_console.is_file():
    raise SystemExit("Smoke failure: embedded console index.html is missing.")
runtime_hash = hashlib.sha256(runtime_console.read_bytes()).hexdigest()
source_hash = hashlib.sha256(source_console.read_bytes()).hexdigest()
print(f"Smoke installed console SHA256: {runtime_hash}")
print(f"Smoke source console SHA256: {source_hash}")
if runtime_hash != source_hash:
    raise SystemExit("Smoke failure: installer console files differ from this source revision.")
print("Runtime and embedded console smoke test passed")
PY
conda run --no-capture-output -p "$test_prefix" python "$smoke_script"
conda run --no-capture-output -p "$test_prefix" unilab --help
conda run --no-capture-output -p "$test_prefix" unilab-supervisor --help

metadata="$installer_dir/source.json"
python - "$metadata" <<'PY'
import json
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "tag": os.environ["SOURCE_TAG"],
    "commit": os.environ["SOURCE_COMMIT"],
    "platform": "osx-arm64",
    "version": os.environ["UNILABOS_INSTALLER_VERSION"],
    "build": os.environ["UNILABOS_INSTALLER_BUILD"],
    "packaging_overrides": "../packaging-overrides.diff",
    "smoke_test_passed": True,
}, indent=2) + "\n")
PY
(cd "$installer_dir" && shasum -a 256 "$(basename "$installer")" > SHA256SUMS)
cp "$installer_dir/SHA256SUMS" "$installer.sha256"

export OSS_UPLOAD_PREFIX="$oss_release_prefix"
export OSS_UPLOAD_FILES="$installer;$installer.sha256;$metadata"
python "$uploader"
installer_url="$(python "$oss_public_url" --prefix "$OSS_UPLOAD_PREFIX" --object-name "${installer##*/}")"
echo "Installer download URL: $installer_url"
python "$notifier" --system "macOS ARM64" --version "$version" --oss-url "$installer_url"

# 安装器由 OSS 分发；GitLab artifact 只保留可审计的小型产物与完整日志。
printf 'COMPLETE\n' > "$build_root/build-status.txt"
cp "$installer.sha256" "$metadata" "$build_root/packaging-overrides.diff" "$build_root/build-status.txt" "$build_root/build.log" "$artifact_dir/"
echo "macOS installer build and upload complete: $build_root"
