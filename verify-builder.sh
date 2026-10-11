#!/bin/sh
# Verify the pinned builder release BEFORE anything is pulled or run.
# Usage: ./verify-builder.sh [arch]        (default: x86_64)
# Needs: gh (authenticated), cosign, sha256sum.
# Progress goes to stderr; the LAST stdout line is the verified image ref
# (repo@sha256:...), so callers can do: REF=$(./verify-builder.sh | tail -n1)
set -eu

ARCH="${1:-x86_64}"
cd "$(dirname "$0")"

BUILDER_REPO="${BUILDER_REPO:-patrickryankenneth/python-wheels-builder}"
IMAGE="${IMAGE:-ghcr.io/patrickryankenneth/python-wheels-builder-alpine}"
SIGNER_IDENTITY="${SIGNER_IDENTITY:-patrickryankenneth@gmail.com}"
SIGNER_ISSUER="${SIGNER_ISSUER:-https://github.com/login/oauth}"
SIGNER_WORKFLOW="$BUILDER_REPO/.github/workflows/build-environment-image.yml"

die() { echo "VERIFY FAILED: $*" >&2; exit 1; }
log() { echo "== $*" >&2; }
lock() { sed -n "s/^$1=//p" builder.lock | head -n1; }

RELEASE=$(lock release); COMMIT=$(lock commit); PINNED=$(lock "$ARCH")
[ -n "$RELEASE" ] && [ -n "$COMMIT" ] && [ -n "$PINNED" ] || die "builder.lock incomplete for $ARCH"

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT

log "download $RELEASE from $BUILDER_REPO"
gh release download "$RELEASE" --repo "$BUILDER_REPO" -D "$W" >&2

log "signature on SHA256SUMS"
( cd "$W" && cosign verify-blob \
    --bundle SHA256SUMS.sigstore.json \
    --certificate-identity "$SIGNER_IDENTITY" \
    --certificate-oidc-issuer "$SIGNER_ISSUER" \
    SHA256SUMS ) >&2 || die "SHA256SUMS signature"

log "asset hashes"
( cd "$W" && sha256sum -c SHA256SUMS ) >&2 || die "asset hash mismatch"

log "released digest equals builder.lock"
RELEASED=$(grep -o 'sha256:[0-9a-f]\{64\}' "$W/image.digest.$ARCH" | head -n1)
[ "$RELEASED" = "$PINNED" ] || die "image.digest.$ARCH is $RELEASED, builder.lock pins $PINNED"

log "build attestation on the image digest"
gh attestation verify "oci://$IMAGE@$PINNED" \
    --repo "$BUILDER_REPO" \
    --signer-workflow "$SIGNER_WORKFLOW" \
    --source-ref refs/heads/main \
    --source-digest "$COMMIT" >&2 || die "image attestation"

log "OK: $RELEASE ($ARCH) verified"
echo "$IMAGE@$PINNED"