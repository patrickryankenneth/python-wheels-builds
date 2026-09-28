#!/usr/bin/env python3
"""
release_manager.py - shared verification + release transaction, called by
the _verify-and-release.yml reusable workflow. Runs on ubuntu-latest
regardless of what OS/arch built the wheels being released - this only
ever operates on already-built artifact files, so there's nothing here
that needs Windows, Alpine, or any exotic arch.

Kept as pure Python (subprocess calls to `git`/`gh` only, no shell=True)
rather than bash/pwsh so this exact logic could later run unmodified on a
Windows runner too - e.g. for a build-side checkout-verification script -
without hitting pwsh's inline-multiline-string escaping problems that made
the CRLF rename/restore bug possible in the first place.

Dry-run mode runs the identical draft -> upload -> verify sequence as a
real release and only diverges at the very last step: publish vs. delete.
A draft is deletable regardless of the repo's immutable-release policy, so
this is a real end-to-end test of the release transaction, not a stand-in
for it.
"""
import argparse
import email.parser
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
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


def run_cmd_json(cmd: list[str]):
    return json.loads(run_cmd(cmd).stdout)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Chunked read - avoids loading a whole wheel into memory, which
    matters once this is running across a 1000+-package matrix."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_attestations(wheel_dir: Path, assets_dir: Path, repo: str,
                         signer_workflow: str, upstream_predicate: str) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    wheels = sorted(wheel_dir.rglob("*.whl"))
    if not wheels:
        print(f"::error::no wheels found under {wheel_dir}")
        sys.exit(1)

    for wheel in wheels:
        base = wheel.name
        print(f"== {base} ==")
        shutil.copy2(wheel, assets_dir / base)

        # Two separate checks, not one: build provenance (GitHub attests
        # this came from a run of this exact workflow) AND upstream-source
        # (this wheel's contents match an unmodified upstream commit).
        # Dropping either one leaves half the verification story unproven.
        run_cmd(["gh", "attestation", "verify", str(wheel),
                  "--repo", repo, "--signer-workflow", signer_workflow])
        run_cmd(["gh", "attestation", "verify", str(wheel),
                  "--repo", repo, "--predicate-type", upstream_predicate,
                  "--signer-workflow", signer_workflow])

        # gh attestation download always names its output after the
        # artifact's digest (sha256:<hex>.jsonl), no --output flag exists.
        # Computing the digest ourselves - rather than parsing it back out
        # of the CLI's human-readable text - means nothing can grab the
        # wrong file if the CLI's formatting ever changes.
        digest = sha256_file(wheel)
        run_cmd(["gh", "attestation", "download", str(wheel), "--repo", repo])
        # NOTE: the colon in "sha256:<hex>.jsonl" is fine on Linux/macOS
        # but NTFS disallows ':' in filenames - if this ever needs to run
        # on a Windows runner directly, this bundle name would need
        # remapping (e.g. sha256_<hex>.jsonl) before that works there.
        bundle = Path(f"sha256:{digest}.jsonl")
        if not bundle.exists():
            print(f"::error::expected attestation bundle {bundle} not found for {base}")
            sys.exit(1)
        bundle.rename(assets_dir / f"{base}.attestations.jsonl")


def wheel_version(wheel: Path) -> str:
    """Version: field from the wheel's own *.dist-info/METADATA - exactly
    what pip will see, and the only authoritative source (pyproject.toml
    may not carry it at all, e.g. maturin reading it from Cargo.toml)."""
    with zipfile.ZipFile(wheel) as z:
        meta = next((n for n in z.namelist()
                     if n.count("/") == 1 and n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            print(f"::error::no .dist-info/METADATA in {wheel.name}")
            sys.exit(1)
        msg = email.parser.Parser().parsestr(z.read(meta).decode("utf-8"), headersonly=True)
    version = msg["Version"]
    if not version:
        print(f"::error::no Version field in METADATA of {wheel.name}")
        sys.exit(1)
    return version.strip()


def derive_version(wheel_dir: Path) -> str:
    versions = {wheel_version(w) for w in sorted(wheel_dir.rglob("*.whl"))}
    if len(versions) != 1:
        print(f"::error::wheels in {wheel_dir} do not agree on one version: {sorted(versions)}")
        sys.exit(1)
    return versions.pop()


def choose_tag(repo: str, prefix: str, version: str, wheel_names: set[str],
               force_new: bool = False) -> tuple[str, bool]:
    """
    First release for an upstream version gets `v<version>`; any later
    release for it gets `.post1`, `.post2`, ... regardless of why (more
    wheels, security fix, anything) - a published release is immutable, so
    a new tag is the only way to change what's released.

    The version text is taken verbatim from the wheel and our suffix is
    appended to it as a plain label. It is never parsed back out of a tag,
    and if the label is already taken by a release with a different wheel
    set, we simply move to the next one.

    force_new: skip the "already released with these exact wheels" no-op
    and take the next free revision instead. Used when the wheels' names
    are unchanged but what is attested about them is not (e.g. a newer
    POLICY_VERSION / attestation schema), which name comparison can't see.

    Returns (tag, already_released). already_released means a published
    release under that tag already has exactly these wheels: nothing to do.
    """
    n = 0
    while True:
        tag = f"v{version}" + (f".post{n}" if n else "")
        rel = f"{prefix}-{tag}"
        view = run_cmd(["gh", "release", "view", rel, "--repo", repo,
                        "--json", "isDraft,assets"], check=False)
        if view.returncode != 0:
            if "not found" not in view.stderr.lower():
                print(f"::error::could not look up release {rel}")
                sys.exit(view.returncode)
            return tag, False
        info = json.loads(view.stdout)
        if info["isDraft"]:
            return tag, False
        existing = {a["name"] for a in info["assets"] if a["name"].endswith(".whl")}
        if existing == wheel_names and not force_new:
            return tag, True
        print(f"{rel} is published with a different wheel set - trying next post revision")
        n += 1


def check_bundle_uniqueness(assets_dir: Path) -> None:
    """Catches a wheel accidentally ending up with another wheel's
    attestation bundle via a globbing mistake - this bit the win-arm64
    workflow once already before it was made per-wheel."""
    wheels = sorted(assets_dir.glob("*.whl"))
    bundles = sorted(assets_dir.glob("*.attestations.jsonl"))
    unique_hashes = {sha256_file(b) for b in bundles}
    print(f"wheels: {len(wheels)}, unique bundle hashes: {len(unique_hashes)}")
    if len(wheels) > 1 and len(unique_hashes) < len(wheels):
        print("::error::attestation bundles are not unique per wheel - refusing to proceed")
        sys.exit(1)


def fetch_build_job_log(repo: str, run_id: str, job_name: str, tag: str, assets_dir: Path) -> None:
    """GitHub Actions logs are only retained ~90 days by default - this
    lands a durable copy in the release assets themselves. Works because
    this job only runs after `needs: build` (or equivalent) finishes, so
    the log is already complete and fetchable, not read mid-run."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    data = run_cmd_json(["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs"])
    job_id = next((j["id"] for j in data.get("jobs", []) if j["name"] == job_name), None)
    if job_id is None:
        print(f"::error::could not find a job named '{job_name}' in this run")
        sys.exit(1)

    result = run_cmd(["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs", "--allow-escape-sequences"])
    txt_path = assets_dir / f"build-log-{tag}.txt"
    txt_path.write_text(result.stdout)
    gz_path = Path(f"{txt_path}.gz")
    with txt_path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    txt_path.unlink()
    print(f"Saved build log for job {job_id} -> {gz_path.name}")


def release_transaction(repo: str, rel: str, sha: str, tag: str, title_prefix: str,
                         assets_dir: Path, signer_workflow: str, dry_run: bool,
                         upstream_tag: str | None = None) -> str | None:
    """
    Draft -> upload -> verify asset set -> (publish | delete). Same code
    path for real releases and dry runs; they only diverge at the final
    step, so a passing dry run actually proves the release transaction
    works, not just that attestations parse.

    Idempotent: a rerun of an already-published release with the same
    exact asset set is a no-op; a different asset set is refused rather
    than silently overwriting something someone may have already verified
    and pinned. An incomplete draft (died mid-upload) is safely resumed.

    Returns the published release URL, or None if nothing was published
    (dry run, or an already-published release with matching assets).
    """
    expected = sorted(p.name for p in assets_dir.iterdir())

    view = run_cmd(["gh", "release", "view", rel, "--repo", repo,
                     "--json", "isDraft,assets"], check=False)

    if view.returncode == 0:
        existing_view = json.loads(view.stdout)
        if not existing_view["isDraft"]:
            existing = sorted(a["name"] for a in existing_view["assets"])
            if existing == expected:
                print(f"Release {rel} is already published with these exact assets - nothing to do")
                return None
            print(f"::error::Published release {rel} already exists with a different asset set - refusing to overwrite")
            print(f"existing: {existing}")
            print(f"expected: {expected}")
            sys.exit(1)
        print(f"Resuming incomplete draft release {rel} (not yet public)")
    else:
        title = f"{'DRY RUN - ' if dry_run else ''}{title_prefix} {tag}"
        # `tag`/`rel` name this python-wheels distribution release; the upstream
        # tag + commit are a separate fact and must not be conflated with it.
        upstream = (f"upstream tag {upstream_tag} (commit {sha})" if upstream_tag
                    else f"upstream commit {sha}")
        notes = (f"python-wheels distribution release {rel}, built from {upstream}. "
                 f"Verify with: gh attestation verify <wheel> --repo {repo} "
                 f"--signer-workflow {signer_workflow}")
        create_cmd = ["gh", "release", "create", rel, "--repo", repo, "--draft",
                       "--title", title, "--notes", notes]
        if dry_run:
            create_cmd.insert(create_cmd.index("--draft") + 1, "--prerelease")
        run_cmd(create_cmd)

    # --clobber is safe here: the release is still a draft, so nothing
    # public is being overwritten - only ever resuming or completing an
    # upload that hasn't been shown to anyone yet.
    run_cmd(["gh", "release", "upload", rel, "--repo", repo, "--clobber",
              *[str(p) for p in assets_dir.iterdir()]])

    final = json.loads(run_cmd(["gh", "release", "view", rel, "--repo", repo,
                                  "--json", "assets"]).stdout)
    final_names = sorted(a["name"] for a in final["assets"])
    if final_names != expected:
        print("::error::Uploaded asset set does not match expected set - not publishing")
        print(f"uploaded: {final_names}")
        print(f"expected: {expected}")
        sys.exit(1)

    if dry_run:
        print(f"Dry run verified end-to-end - deleting draft {rel}, nothing published")
        run_cmd(["gh", "release", "delete", rel, "--repo", repo, "--yes"], check=False)
        return None

    run_cmd(["gh", "release", "edit", rel, "--repo", repo, "--draft=false"])
    url = json.loads(run_cmd(["gh", "release", "view", rel, "--repo", repo, "--json", "url"]).stdout)["url"]
    print(f"Published {rel} -> {url}")
    return url


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", default=None,
                    help="Explicit release tag override. Normally omitted: derived from the wheels' version")
    p.add_argument("--sha", required=True, help="Resolved upstream commit")
    p.add_argument("--upstream-tag", default=None, help="Upstream git tag that resolved to --sha, e.g. v2.0.5")
    p.add_argument("--signer-workflow", required=True)
    p.add_argument("--upstream-predicate",
                    default="https://patrickryankenneth.github.io/attestations/upstream-source/v1")
    p.add_argument("--release-title-prefix", default="dbt-oss")
    p.add_argument("--wheel-dir", default="dist")
    p.add_argument("--assets-dir", default="release-assets")
    p.add_argument("--build-job-name", default="build")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-new-post", action="store_true",
                    help="Publish under the next free .postN even if a published release already has these exact wheel names")
    args = p.parse_args()

    run_id = os.environ["GITHUB_RUN_ID"]
    wheel_dir = Path(args.wheel_dir)
    assets_dir = Path(args.assets_dir)

    verify_attestations(wheel_dir, assets_dir, args.repo, args.signer_workflow, args.upstream_predicate)
    check_bundle_uniqueness(assets_dir)

    tag = args.tag
    if tag is None:
        version = derive_version(wheel_dir)
        wheel_names = {w.name for w in wheel_dir.rglob("*.whl")}
        tag, already = choose_tag(args.repo, args.release_title_prefix, version, wheel_names,
                                force_new=args.force_new_post)
        print(f"Wheel version {version} -> release tag {tag}")
        if already:
            print(f"{args.release_title_prefix}-{tag} is already published with these exact wheels - nothing to do")
            write_outputs(tag, None)
            return
    rel = f"{args.release_title_prefix}-{tag}"

    fetch_build_job_log(args.repo, run_id, args.build_job_name, tag, assets_dir)

    url = release_transaction(args.repo, rel, args.sha, tag, args.release_title_prefix,
                                assets_dir, args.signer_workflow, args.dry_run,
                                upstream_tag=args.upstream_tag)
    write_outputs(tag, url)


def write_outputs(tag: str, url: str | None) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        return
    with open(gh_output, "a") as f:
        f.write(f"release_tag={tag}\n")
        if url:
            f.write(f"release_url={url}\n")


if __name__ == "__main__":
    main()
