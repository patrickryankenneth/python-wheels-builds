# Policy legacy-2026-09

<!-- Generated from policy/policy.json by policy_tool.py render-md. Do not edit. -->

- Enforcement mode: **descriptive**
- Legacy aliases: `2026-09-sha-pinned-v1`
- Hash: sha256 of the exact `policy.json` bytes, published as `policy.json.sha256` and as the release asset digest. This file cannot contain its own hash.

Describes what releases built under the legacy label 2026-09-sha-pinned-v1 actually did. Historical and descriptive only: no verifier loaded this file or failed a release against it, builds do not consume it, and it is not an enforcement contract or evidence that any policy was enforced. Written after those releases; they reference the label, not this file's hash. The first enforcing policy is v1.

## Rules

### SRC-1 - Pinned upstream commit, clean tree

- Status: `performed-at-build`
- Implemented by: checkout_verify.py (musllinux); inline pwsh steps (win-arm64)

The upstream git tag is resolved to a commit, checked out detached at that exact commit, and the working tree is verified clean before building. The build fails if the check fails. The result is recorded as upstream_commit and tree_clean in the signed upstream-source attestation.

### SRC-2 - Source snapshot archive

- Status: `performed-at-build`
- Implemented by: checkout_verify.py (musllinux); inline pwsh steps (win-arm64)

A git archive of the pinned commit is created, its sha256 is recorded in the upstream-source attestation as snapshot_archive_sha256, and the archive is attached to the release.

### POL-1 - Policy label recorded

- Status: `performed-at-build`
- Implemented by: POLICY_VERSION env in both build workflows; checkout_verify.py (musllinux); inline pwsh (win-arm64)

The build writes the label 2026-09-sha-pinned-v1 into the upstream-source attestation as policy_version. It is a label only: no hash and no rule list were attached to it.

### ATT-1 - Two attestations per wheel, verified with gh before publish

- Status: `verified-at-release`
- Implemented by: release_manager.py (musllinux); inline release job (win-arm64)

Each wheel has a build-provenance attestation and an upstream-source attestation. Before publishing, each was verified with GitHub's attestation verification against the expected repository and calling workflow. The historical release path did not additionally constrain the attestation's source ref, workflow commit, runner type, or predicate contents.

### ATT-2 - Attestation bundles attached

- Status: `verified-at-release`
- Implemented by: release_manager.py (musllinux); inline release job (win-arm64)

Each wheel's attestation bundle is attached to the release as <wheel>.attestations.jsonl, and the bundles are checked to be distinct per wheel.

### REL-1 - Immutable release and exact verified asset set

- Status: `verified-at-release`
- Implemented by: release_manager.py release_transaction, choose_tag (musllinux); inline release job (win-arm64); GitHub immutable releases

The release job publishes only the verified asset set and does not overwrite an existing release with a different asset set (musllinux releases a changed wheel set under a new .postN tag; win-arm64 refuses the rerun). Published releases are immutable.

### ACT-1 - Actions pinned to commit SHAs

- Status: `declared`
- Implemented by: workflow authoring

When this policy was written, every GitHub Action `uses:` in both build workflows and the shared release workflow was pinned to a full commit SHA. This is a convention: no automated check enforced it, and it was not re-verified for the workflow commit that built each earlier release.

## Not guaranteed

- No policy file was loaded or enforced. The label 2026-09-sha-pinned-v1 carries no hash and no rule list; this v1 text was written afterwards to describe the behaviour above.
- The release job did not read attestation predicate contents: upstream_commit, tree_clean and policy_version were not compared to release inputs.
- Attestations were verified by repository and signer workflow only. Source ref, workflow commit and runner type were not constrained.
- The source archive's sha256 was not compared to the digest in the attestation at release time.
- Release tags were not required to point at the exact build commit. win-arm64 created its tag without a target, so it lands on the default-branch tip at publish time; dbt-oss-v2.0.5.post1's tag and build commits differ.
- Release tags are lightweight and unsigned. A verified-signature badge on a tagged commit can be GitHub's own signature for a commit created on GitHub.com; it does not sign the tag and does not identify the author.
- Dependencies of a wheel are not verified; only the exact wheel is covered.
- Wheels are not claimed to be bit-for-bit reproducible.
- Commit signing and signer-identity checks protect promotion into main and are not part of this policy.