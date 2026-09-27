#!/bin/sh
# Run INSIDE the pywheels-alpine-builder container, cwd /build.
# Mirrors build-dbt-oss-win-arm64.yml's build steps (resolve tag -> clone
# at pinned commit -> verify clean -> per-target maturin build), minus
# attestation/release since this is the local pre-CI validation pass.
set -eu

DBT_OSS_TAG="${DBT_OSS_TAG:-v2.0.5}"
PACKAGE_TARGETS="${PACKAGE_TARGETS:-both}"
REPO_URL="https://github.com/dbt-labs/dbt-oss.git"

if ! command -v protoc >/dev/null 2>&1; then
    echo "protoc not found on PATH - rebuild the image (Containerfile installs it via apk)" >&2
    exit 1
fi
echo "protoc: $(protoc --version)"

echo "== Resolving $DBT_OSS_TAG on $REPO_URL =="
line=$(git ls-remote "$REPO_URL" "refs/tags/${DBT_OSS_TAG}^{}") || true
if [ -z "$line" ]; then
    line=$(git ls-remote "$REPO_URL" "refs/tags/${DBT_OSS_TAG}")
fi
if [ -z "$line" ]; then
    echo "Tag $DBT_OSS_TAG not found in $REPO_URL" >&2
    exit 1
fi
SHA=$(printf '%s' "$line" | head -n1 | cut -f1)
echo "Tag $DBT_OSS_TAG resolves to $SHA"

if [ ! -d dbt-oss ]; then
    git clone "$REPO_URL" dbt-oss
fi

if git -C dbt-oss cat-file -e "${SHA}^{commit}" 2>/dev/null; then
    echo "Commit $SHA already present locally - skipping fetch"
else
    git -C dbt-oss fetch origin "$SHA"
fi
git -C dbt-oss checkout --detach "$SHA"

head=$(git -C dbt-oss rev-parse HEAD)
if [ "$head" != "$SHA" ]; then
    echo "HEAD $head != expected $SHA" >&2
    exit 1
fi
if [ -n "$(git -C dbt-oss status --porcelain)" ]; then
    echo "Working tree is not clean" >&2
    exit 1
fi
echo "Checkout verified: $head"

cd dbt-oss/crates/dbt-sa-python
PYPROJECT="$(pwd)/pyproject.toml"
trap 'sed -i "s/^name = \"dbt-oss\"\$/name = \"dbt-core\"/" "$PYPROJECT" 2>/dev/null || true' EXIT
original_hash=$(sha256sum "$PYPROJECT" | cut -d' ' -f1)

if [ -n "${CARGO_TARGET_DIR:-}" ]; then
    WHEELS_DIR="$CARGO_TARGET_DIR/wheels"
else
    WHEELS_DIR="$(cd ../.. && pwd)/target/wheels"
fi
echo "Wheel output dir: $WHEELS_DIR"

if [ "$PACKAGE_TARGETS" = "both" ]; then
    targets="dbt-core dbt-oss"
else
    targets="$PACKAGE_TARGETS"
fi
echo "Building: $targets"

for name in $targets; do
    if [ "$name" = "dbt-oss" ]; then
        sed -i 's/^name = "dbt-core"$/name = "dbt-oss"/' "$PYPROJECT"
        if ! grep -q '^name = "dbt-oss"$' "$PYPROJECT"; then
            echo "rename to dbt-oss did not match any line" >&2
            exit 1
        fi
    fi

    TARGET_DIR="${CARGO_TARGET_DIR:-$(cd ../.. && pwd)/target}"
    find "$TARGET_DIR/maturin" -maxdepth 3 -iname '*.so' -delete 2>/dev/null || true
    find "$TARGET_DIR/maturin" -maxdepth 3 -type d -iname '*.libs' -exec rm -rf {} + 2>/dev/null || true
    find "$TARGET_DIR/release" -maxdepth 2 -iname 'libdbt_core_pyo3*.so' -delete 2>/dev/null || true
    find "$TARGET_DIR/release/deps" -maxdepth 1 -iname 'libdbt_core_pyo3*.so' -delete 2>/dev/null || true
    find "$TARGET_DIR/release/.fingerprint" -maxdepth 1 -iname 'dbt_core_pyo3-*' -exec rm -rf {} + 2>/dev/null || true
    find "$TARGET_DIR/release/.fingerprint" -maxdepth 1 -iname 'dbt-sa-python-*' -exec rm -rf {} + 2>/dev/null || true
    find "$TARGET_DIR/release/.fingerprint" -maxdepth 1 -iname 'dbt_python_core-*' -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$WHEELS_DIR"

    # COLD_CACHE=1 forces --locked (allows Cargo.lock/registry updates on a
    # deliberate cold-cache run); default is --frozen (fail instead of
    # silently touching the network/lockfile - what you want on every
    # normal cached run).
    FROZEN_FLAG="--frozen"
    if [ "${COLD_CACHE:-0}" = "1" ]; then
        FROZEN_FLAG="--locked"
    fi
    # --timings writes per-crate compile duration + concurrency data
    # to $CARGO_TARGET_DIR/cargo-timings/ - use this to see how long each
    # crate actually took instead of guessing from scrollback.
    maturin build --release "$FROZEN_FLAG" --timings

    wheel_count=$(find "$WHEELS_DIR" -maxdepth 1 -name '*.whl' | wc -l)
    if [ "$wheel_count" -ne 1 ]; then
        echo "expected exactly 1 wheel for $name, found $wheel_count" >&2
        exit 1
    fi
    wheel=$(find "$WHEELS_DIR" -maxdepth 1 -name '*.whl')

    dest_dir="../../../dist/$name"
    mkdir -p "$dest_dir"
    mv "$wheel" "$dest_dir/"
    echo "Built $name -> $dest_dir/$(basename "$wheel")"

    if [ "$name" = "dbt-oss" ]; then
        sed -i 's/^name = "dbt-oss"$/name = "dbt-core"/' "$PYPROJECT"
        if ! grep -q '^name = "dbt-core"$' "$PYPROJECT"; then
            echo "restore to dbt-core did not match any line" >&2
            exit 1
        fi
        restored_hash=$(sha256sum "$PYPROJECT" | cut -d' ' -f1)
        if [ "$restored_hash" != "$original_hash" ]; then
            echo "pyproject.toml restore hash mismatch: got $restored_hash, expected $original_hash" >&2
            exit 1
        fi
    fi
done

echo "== dist =="
find ../../../dist -name '*.whl'

if ! git -C ../.. diff --quiet HEAD; then
    echo "Build modified tracked source files" >&2
    exit 1
fi
echo "Source tree unmodified: $head"