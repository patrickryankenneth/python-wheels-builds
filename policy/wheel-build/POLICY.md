# Policy wheel-build v1

<!-- Generated from policy.json by policy_tool.py render-md. Do not edit. -->

- Scope: `wheel-build`
- Enforcement mode: **enforced**
- Hash: sha256 of the exact `policy.json` bytes, published as `policy.json.sha256` and as the release asset digest. This file cannot contain its own hash.

Wheels are built in a builder image whose digest matches builder.lock, from an unmodified upstream tag, by a pinned workflow on main. Each rule below is verified at release by verify_wheel_policy.py against the attestations on the wheels. Declared rules are trust roots: recorded, not verified.

## Rules

### BLD-1 - Builder image pinned by digest

- Status: `enforced`
- Check: `check_builder_pinned`
- Implemented by: verify_wheel_policy.py check_builder_pinned

The upstream-source attestation records the builder image as <image>@sha256:<digest>, and that digest appears in builder.lock for the wheel's architecture.

Parameters:

```json
{
  "image": "ghcr.io/patrickryankenneth/python-wheels-builder-alpine"
}
```

### BLD-2 - Unmodified upstream source

- Status: `enforced`
- Check: `check_source_pinned`
- Implemented by: verify_wheel_policy.py check_source_pinned

The attested upstream repo, tag and 40-hex commit match the release inputs, tree_clean is true (checked before the build; the post-build tree check is performed by build.sh and logged, not attested), and the released source archive has the attested sha256.

Parameters:

```json
{
  "upstream_repo": "https://github.com/dbt-labs/dbt-oss"
}
```

### BLD-3 - Compiler satisfies upstream channel

- Status: `enforced`
- Check: `check_toolchain`
- Implemented by: verify_wheel_policy.py check_toolchain

The attested rustc version satisfies the channel in rust-toolchain.toml of the archived upstream source. A minor channel such as 1.96 matches any 1.96.x, a full version must match exactly, and stable/beta/nightly fail.

Parameters:

```json
{
  "toolchain_file": "rust-toolchain.toml"
}
```

### BLD-4 - Build provenance from the pinned workflow

- Status: `enforced`
- Check: `check_provenance`
- Implemented by: verify_wheel_policy.py check_provenance

SLSA provenance verifies with the signer workflow, an allowed source ref and the build commit, and records the same workflow path, repository, ref and commit.

Parameters:

```json
{
  "allowed_source_refs": [
    "refs/heads/main"
  ],
  "workflow_path": ".github/workflows/build-dbt-oss-musllinux.yml"
}
```

### BLD-5 - Built under this exact policy

- Status: `enforced`
- Check: `check_policy_pinned`
- Implemented by: verify_wheel_policy.py check_policy_pinned

The attested policy_sha256 equals the sha256 of the published policy.json, and the attested policy_version equals this policy's version.

Parameters:

```json
{
  "policy_version": "v1"
}
```

### BLD-6 - Release asset set

- Status: `enforced`
- Check: `check_release_assets`
- Implemented by: verify_wheel_policy.py check_release_assets

The release holds exactly each wheel, its attestation bundle, the attested source archive and one build log.

Parameters:

```json
{
  "build_log_pattern": "build-log-.+\\.txt\\.gz"
}
```

### BLD-7 - Workflow actions pinned

- Status: `enforced`
- Check: `check_actions_pinned`
- Implemented by: verify_wheel_policy.py check_actions_pinned

Every external action used by the build and release workflows is pinned to a full commit SHA.

Parameters:

```json
{
  "workflows": [
    "build-dbt-oss-musllinux.yml",
    "verify-and-release.yml"
  ]
}
```

### BLD-10 - Predicate is complete

- Status: `enforced`
- Check: `check_predicate_complete`
- Implemented by: verify_wheel_policy.py check_predicate_complete

The upstream-source attestation carries exactly the required fields: none missing, none unexpected.

Parameters:

```json
{
  "required_fields": [
    "build_script_sha256",
    "builder_ref",
    "checkout_verify_sha256",
    "policy_sha256",
    "policy_version",
    "rustc_version",
    "snapshot_archive_name",
    "snapshot_archive_sha256",
    "tree_clean",
    "upstream_commit",
    "upstream_repo",
    "upstream_tag"
  ]
}
```

### BLD-11 - Build scripts are the committed ones

- Status: `enforced`
- Check: `check_scripts_at_commit`
- Implemented by: verify_wheel_policy.py check_scripts_at_commit

The attested sha256 of checkout_verify.py and build.sh equals the sha256 of those files at the build commit.

Parameters:

```json
{
  "files": {
    "build_script_sha256": "recipes/dbt-oss/build.sh",
    "checkout_verify_sha256": ".github/scripts/checkout_verify.py"
  }
}
```

### BLD-12 - Builder lock is the committed one

- Status: `enforced`
- Check: `check_lock_committed`
- Implemented by: verify_wheel_policy.py check_lock_committed

The builder.lock used for verification is byte-identical to builder.lock at the build commit.

Parameters:

```json
{
  "lock_path": "builder.lock"
}
```

### BLD-13 - Release tag pinned to the build commit

- Status: `enforced`
- Check: `check_release_target`
- Implemented by: verify_wheel_policy.py check_release_target

The release target is the build commit, and once published the tag ref resolves to that commit.

### BLD-8 - Trust root: runner and signing

- Status: `declared`
- Implemented by: none, trust root
- Evidence recorded, not verified: GitHub-hosted ubuntu runner, GitHub Actions OIDC, Sigstore public-good instance, the cosign binary installed by the pinned installer action.

The build runs on a GitHub-hosted runner and signs through GitHub and Sigstore. These are trusted, not verified here.

### BLD-9 - Trust root: packages and sources

- Status: `declared`
- Implemented by: none, trust root
- Evidence recorded, not verified: Alpine APKs, rustup and Rust toolchain archives, crates.io tarballs, and the upstream git dependencies sdf-labs/arrow-rs, sdf-labs/datafusion, sdf-labs/ring, sdf-labs/datafusion-sqlparser-rs, dbt-labs/arrow-adbc, astral-sh/ruff. libgcc_s.so.1 from the builder image is grafted into the wheels.

Inputs fetched at build time are trusted, not verified here. The grafted libgcc_s.so.1 is covered by the builder image SBOM.

## Not guaranteed

- Reproducibility or bit-identity with upstream's own CI.
- Absence of vulnerabilities in the wheels or their dependencies.
- Offline compilation: the build fetches crates and git dependencies.
- Any audit of wheel contents beyond the checks above.
- That the recorded rustc is the one that compiled: it is recorded by the checkout step in the same image before the compile.
- That the build log is attested: it is released as an asset, not as an attested subject.
- File modification times: they are normalized to the commit time, contents are unmodified.
- The release tag is pinned to the build commit but is not signed.
- The post-build tree check is logged by build.sh, not attested.
- SBOM and VEX for the wheels are not part of this policy version.
