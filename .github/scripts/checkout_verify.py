#!/usr/bin/env python3
"""
checkout_verify.py - shared checkout + verification step for build
workflows. Resolves an upstream git tag to a commit, clones/checks it out
at that pinned commit, verifies the working tree is exact and unmodified,
creates a content-addressed source archive + SHA-256 digest, and writes
the upstream-source.json predicate consumed by
`actions/attest --predicate-type <UPSTREAM_PREDICATE>`.

Mirrors build-dbt-oss-win-arm64.yml's inline pwsh steps 1:1 (same
upstream-source.json field names/shapes) so both build workflows produce
attestation-compatible predicates. Pure Python + subprocess (git only, no
shell=True) so this runs unmodified via `python3` on Alpine/Ubuntu today
and via `python` on Windows later, without hitting pwsh's inline
-multiline-string escaping problems that caused the CRLF bug in the
win-arm64 workflow's rename/restore step.

Not platform- or package-specific: every upstream repo/tag/output path is
a CLI argument, so this same script is meant to be reused for other
platform/package combinations later, not just dbt-oss/musllinux.

Does NOT re-verify after a build step runs. Callers that build wheels
after invoking this script are still responsible for checking
`git diff --quiet HEAD` post-build and aborting before upload/attestation
if the tree became dirty - this script only guarantees (and the archive
only captures) a clean, pinned tree at the moment IT ran.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if check and result.returncode != 0:
        print(f"::error::command failed (exit {result.returncode}): {' '.join(cmd)}")
        sys.exit(result.returncode)
    return result


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_tag(repo_url: str, tag: str) -> str:
    # Prefer the dereferenced (annotated-tag) ref first, same as
    # build.sh/win-arm64 did inline; fall back to the lightweight ref.
    result = run(["git", "ls-remote", repo_url, f"refs/tags/{tag}^{{}}"], check=False)
    line = result.stdout.strip()
    if not line:
        result = run(["git", "ls-remote", repo_url, f"refs/tags/{tag}"], check=False)
        line = result.stdout.strip()
    if not line:
        print(f"::error::tag {tag} not found in {repo_url}")
        sys.exit(1)
    sha = line.splitlines()[0].split("\t")[0]
    print(f"Tag {tag} resolves to {sha}")
    return sha


def ensure_checkout(repo_url: str, sha: str, clone_dir: Path) -> None:
    if not clone_dir.exists():
        run(["git", "clone", repo_url, str(clone_dir)])
    elif run(["git", "-C", str(clone_dir), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode != 0:
        run(["git", "-C", str(clone_dir), "fetch", "origin", sha])
    else:
        print(f"Commit {sha} already present locally in {clone_dir} - skipping fetch")

    run(["git", "-C", str(clone_dir), "checkout", "--detach", sha])

    head = run(["git", "-C", str(clone_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != sha:
        print(f"::error::HEAD {head} != expected {sha}")
        sys.exit(1)
    if run(["git", "-C", str(clone_dir), "status", "--porcelain"]).stdout.strip():
        print("::error::working tree is not clean")
        sys.exit(1)
    print(f"Checkout verified: {head}")


def normalize_mtimes(clone_dir: Path, sha: str) -> None:
    """Set every tracked file's mtime to the commit's committer time.

    Cargo decides whether a path dependency is dirty by comparing source
    mtimes against the mtime of the cached dep-info/fingerprint. A fresh
    clone stamps every file with "now", so every workspace crate looks
    modified on every run even when the cache restored fine. Pinning
    mtimes to the (old, fixed) commit time makes them stable across
    clones. git status compares content, so the clean-tree check is
    unaffected."""
    ts = int(run(["git", "-C", str(clone_dir), "show", "-s", "--format=%ct", sha]).stdout.strip())
    out = subprocess.run(["git", "-C", str(clone_dir), "ls-files", "-z"],
                          text=True, capture_output=True, check=True).stdout
    files = [f for f in out.split("\0") if f]
    for f in files:
        try:
            os.utime(clone_dir / f, (ts, ts), follow_symlinks=False)
        except FileNotFoundError:
            pass
    print(f"Normalized mtimes of {len(files)} tracked files to {ts}")
    if run(["git", "-C", str(clone_dir), "status", "--porcelain"]).stdout.strip():
        print("::error::working tree became dirty after mtime normalization")
        sys.exit(1)


def create_archive(clone_dir: Path, archive_dir: Path, sha: str, name_prefix: str) -> tuple[str, str]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{name_prefix}-source-{sha}.tar.gz"
    archive_path = archive_dir / archive_name
    run(["git", "-C", str(clone_dir), "archive", "--format=tar.gz",
         f"--output={archive_path.resolve()}", "HEAD"])
    digest = sha256_file(archive_path)
    print(f"Source archive: {archive_name} sha256={digest}")
    return archive_name, digest


def write_predicate(predicate_path: Path, repo_url: str, tag: str, sha: str,
                     policy_version: str, archive_name: str, archive_sha256: str) -> None:
    upstream_repo = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    predicate = {
        "upstream_repo": upstream_repo,
        "upstream_tag": tag,
        "upstream_commit": sha,
        "tree_clean": True,
        "policy_version": policy_version,
        "snapshot_archive_name": archive_name,
        "snapshot_archive_sha256": archive_sha256,
    }
    predicate_path.write_text(json.dumps(predicate, indent=2) + "\n")
    print(predicate_path.read_text())


def write_outputs(outputs_file: Path, values: dict[str, str]) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    outputs_file.write_text(lines)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-url", required=True)
    p.add_argument("--tag", required=True, help="Upstream git tag to resolve and check out")
    p.add_argument("--clone-dir", required=True, type=Path)
    p.add_argument("--archive-dir", required=True, type=Path)
    p.add_argument("--predicate-path", required=True, type=Path)
    p.add_argument("--policy-version", required=True)
    p.add_argument("--archive-name-prefix", default=None,
                    help="Defaults to the repo's basename (e.g. 'dbt-oss' from .../dbt-oss.git)")
    p.add_argument("--outputs-file", type=Path, default=None,
                    help="If set, writes KEY=VALUE lines here (and to $GITHUB_OUTPUT if present)")
    args = p.parse_args()

    prefix = args.archive_name_prefix
    if prefix is None:
        base = args.repo_url.rstrip("/").rsplit("/", 1)[-1]
        prefix = base[:-4] if base.endswith(".git") else base

    sha = resolve_tag(args.repo_url, args.tag)
    ensure_checkout(args.repo_url, sha, args.clone_dir)
    normalize_mtimes(args.clone_dir, sha)
    archive_name, archive_sha256 = create_archive(args.clone_dir, args.archive_dir, sha, prefix)
    write_predicate(args.predicate_path, args.repo_url, args.tag, sha,
                     args.policy_version, archive_name, archive_sha256)

    if args.outputs_file:
        write_outputs(args.outputs_file, {
            "tag": args.tag,
            "sha": sha,
            "archive_name": archive_name,
            "archive_sha256": archive_sha256,
        })


if __name__ == "__main__":
    main()
