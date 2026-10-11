#!/bin/sh
# Local clean build: same image ref, same mounts, same build.sh as CI.
# Usage: ./run-build.sh [arch]    env: DBT_OSS_TAG, PACKAGE_TARGETS
# - verifies the pinned builder first, pulls by digest only
# - builds from COMMITTED files (git archive HEAD), never your working tree
# - starts from empty caches in a fresh temp dir (cold run = the proof run)
set -eu

ARCH="${1:-x86_64}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
if command -v podman >/dev/null 2>&1; then ENGINE=podman; else ENGINE=docker; fi

OUT=$("$ROOT/verify-builder.sh" "$ARCH")
REF=$(printf '%s\n' "$OUT" | tail -n1)
[ -n "$REF" ] || { echo "no verified image ref" >&2; exit 1; }
echo "== pulling $REF" >&2
$ENGINE pull "$REF" >&2

W=$(mktemp -d)
echo "workdir: $W" >&2
git -C "$ROOT" archive HEAD recipes/dbt-oss .github/scripts | tar -x -C "$W"
for d in cargo-registry cargo-git cargo-target sccache; do mkdir -p "$W/cache/$d"; done

$ENGINE run --rm \
    -e RUSTFLAGS="--cap-lints=warn" \
    -e CARGO_LOG="cargo::core::compiler::fingerprint=info" \
    -e DBT_OSS_TAG="${DBT_OSS_TAG:-v2.0.5}" \
    -e PACKAGE_TARGETS="${PACKAGE_TARGETS:-both}" \
    -e COLD_CACHE=1 \
    -e RUSTUP_AUTO_INSTALL=0 \
    ${RUSTUP_TOOLCHAIN:+-e RUSTUP_TOOLCHAIN=$RUSTUP_TOOLCHAIN} \
    -e POLICY_VERSION="${POLICY_VERSION:-local-unverified}" \
    -v "$W/cache/cargo-registry:/opt/cargo/registry" \
    -v "$W/cache/cargo-git:/opt/cargo/git" \
    -v "$W/cache/cargo-target:/cargo-target" \
    -v "$W/cache/sccache:/sccache" \
    -v "$W/recipes/dbt-oss:/build" \
    -v "$W/.github/scripts:/scripts:ro" \
    "$REF" sh /build/build.sh

echo "builder: $REF" >&2
echo "wheels:  $W/recipes/dbt-oss/dist" >&2
echo "delete $W when done" >&2