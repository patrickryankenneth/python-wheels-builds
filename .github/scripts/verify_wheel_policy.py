#!/usr/bin/env python3
"""
verify_wheel_policy.py - checks built wheels against policy/wheel-build/policy.json.

Same contract as the builder's verify_builder_policy.py: every enforced rule
names one check_* function here (policy_tool_v2.py lint keeps both in sync).
A check takes (ctx, params) and returns a list of failure strings; empty
means pass. Fail closed: a raise, a missing input or an unknown check is a
failure, never a skip.

Inputs:
  --policy        the published policy.json (its sha256 must equal the attested policy_sha256)
  --root          repo checkout at the policy tag (workflows are read from here)
  --wheel-dir     directory tree holding the wheels
  --assets-dir    the release assets directory (source archive, bundles, build log)
  --lock          builder.lock
  --repo / --signer-workflow / --source-ref / --build-sha / --upstream-tag
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ARCHES = ("x86_64", "aarch64")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)")
PROVENANCE = "https://slsa.dev/provenance/v1"
UPSTREAM = "https://patrickryankenneth.github.io/attestations/upstream-source/v1"


def _sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _arch(wheel):
    for a in ARCHES:
        if wheel.name.endswith(f"{a}.whl"):
            return a
    return None


class Ctx:
    def __init__(self, a, policy_bytes):
        self.root = Path(a.root)
        self.wheels = sorted(Path(a.wheel_dir).rglob("*.whl"))
        self.assets = Path(a.assets_dir) if a.assets_dir else None
        self.lock = Path(a.lock)
        self.repo = a.repo
        self.signer = a.signer_workflow
        self.source_ref = a.source_ref
        self.build_sha = a.build_sha
        self.upstream_tag = a.upstream_tag
        self.release_tag = getattr(a, "release_tag", None)
        self.policy_sha = hashlib.sha256(policy_bytes).hexdigest()
        self._cache = {}

    def run(self, argv):
        r = subprocess.run(argv, text=True, capture_output=True)
        return r.returncode, r.stdout

    def git_show(self, path):
        r = subprocess.run(["git", "-C", str(self.root), "show", f"{self.build_sha}:{path}"], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"git show {self.build_sha}:{path} failed")
        return r.stdout

    def predicate(self, wheel, ptype):
        """(predicate, error) of the one verified attestation of this type on this wheel,
        pinned to the signer workflow, the source ref and the build commit."""
        key = (wheel.name, ptype)
        if key in self._cache:
            return self._cache[key]
        rc, out = self.run(["gh", "attestation", "verify", str(wheel),
                            "--repo", self.repo,
                            "--signer-workflow", self.signer,
                            "--source-ref", self.source_ref,
                            "--source-digest", self.build_sha,
                            "--predicate-type", ptype,
                            "--format", "json"])
        res = (None, f"{wheel.name}: gh attestation verify failed for {ptype}")
        if rc == 0:
            try:
                rows = json.loads(out)
                preds = [(((r or {}).get("verificationResult") or {}).get("statement") or {}).get("predicate")
                         for r in (rows if isinstance(rows, list) else [])]
                preds = [p for p in preds if isinstance(p, dict)]
                res = (preds[0], None) if len(preds) == 1 else \
                    (None, f"{wheel.name}: expected exactly one {ptype} attestation, got {len(preds)}")
            except ValueError:
                res = (None, f"{wheel.name}: attestation output is not JSON ({ptype})")
        self._cache[key] = res
        return res


def _preds(ctx, ptype, errs):
    if not ctx.wheels:
        errs.append("no wheels found")
    out = []
    for w in ctx.wheels:
        p, e = ctx.predicate(w, ptype)
        if e:
            errs.append(e)
        else:
            out.append((w, p))
    return out


def _archive(ctx, pred):
    if ctx.assets is None:
        return None
    return ctx.assets / str(pred.get("snapshot_archive_name", ""))


def _channel(archive, toolchain_file):
    with tarfile.open(archive, "r:gz") as t:
        for m in t.getmembers():
            if m.name.lstrip("./") == toolchain_file and m.isfile():
                text = t.extractfile(m).read().decode("utf-8", "replace")
                mm = re.search(r'^\s*channel\s*=\s*"([^"]+)"', text, re.M)
                return mm.group(1) if mm else None
    return None


def _satisfies(channel, version):
    c = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", channel or "")
    v = re.match(r"rustc (\d+)\.(\d+)\.(\d+)", version or "")
    if not c or not v:
        return False
    if c.group(3) is None:
        return c.group(1, 2) == v.group(1, 2)
    return c.group(1, 2, 3) == v.group(1, 2, 3)


# ---------- checks (one per enforced rule) ----------

def check_builder_pinned(ctx, params):
    errs = []
    lock = ctx.lock.read_text()
    for w, p in _preds(ctx, UPSTREAM, errs):
        ref = p.get("builder_ref")
        m = re.fullmatch(re.escape(params["image"]) + r"@(sha256:[0-9a-f]{64})", ref or "")
        if not m:
            errs.append(f"{w.name}: builder_ref {ref!r} is not {params['image']}@sha256:<digest>")
            continue
        d = m.group(1)
        lines = [l for l in lock.splitlines() if d in l]
        if not lines:
            errs.append(f"{w.name}: builder digest {d} is not in {ctx.lock.name}")
            continue
        arch = _arch(w)
        if arch is None:
            errs.append(f"{w.name}: cannot tell the architecture from the wheel name")
        elif any(o in l for l in lines for o in ARCHES if o != arch):
            errs.append(f"{w.name}: digest {d} is locked for a different architecture than {arch}")
    return errs


def check_source_pinned(ctx, params):
    errs = []
    for w, p in _preds(ctx, UPSTREAM, errs):
        if p.get("upstream_repo") != params["upstream_repo"]:
            errs.append(f"{w.name}: upstream_repo {p.get('upstream_repo')!r} != {params['upstream_repo']}")
        if p.get("upstream_tag") != ctx.upstream_tag:
            errs.append(f"{w.name}: upstream_tag {p.get('upstream_tag')!r} != {ctx.upstream_tag!r}")
        c = p.get("upstream_commit")
        if not (isinstance(c, str) and SHA40_RE.match(c)):
            errs.append(f"{w.name}: upstream_commit missing or malformed: {c!r}")
        if p.get("tree_clean") is not True:
            errs.append(f"{w.name}: tree_clean is not true")
        name = str(p.get("snapshot_archive_name", ""))
        if not (isinstance(c, str) and c in name and name.endswith(".tar.gz")):
            errs.append(f"{w.name}: snapshot archive name {name!r} does not carry the commit")
        arch = _archive(ctx, p)
        if arch is not None:
            if not arch.exists():
                errs.append(f"{w.name}: source archive {name} not in assets")
            elif _sha256_file(arch) != p.get("snapshot_archive_sha256"):
                errs.append(f"{w.name}: source archive sha256 differs from the attested one")
    return errs


def check_toolchain(ctx, params):
    errs = []
    for w, p in _preds(ctx, UPSTREAM, errs):
        arch = _archive(ctx, p)
        if arch is None or not arch.exists():
            errs.append(f"{w.name}: source archive needed to read {params['toolchain_file']}")
            continue
        channel = _channel(arch, params["toolchain_file"])
        version = p.get("rustc_version")
        if not _satisfies(channel, version):
            errs.append(f"{w.name}: attested {version!r} does not satisfy upstream channel {channel!r}")
    return errs


def check_provenance(ctx, params):
    errs = []
    if ctx.source_ref not in params["allowed_source_refs"]:
        errs.append(f"source ref {ctx.source_ref} not in {params['allowed_source_refs']}")
    for w, p in _preds(ctx, PROVENANCE, errs):
        bd = p.get("buildDefinition") or {}
        wf = (bd.get("externalParameters") or {}).get("workflow") or {}
        if wf.get("path") != params["workflow_path"]:
            errs.append(f"{w.name}: workflow path {wf.get('path')!r} != {params['workflow_path']}")
        if wf.get("repository") != f"https://github.com/{ctx.repo}":
            errs.append(f"{w.name}: workflow repository {wf.get('repository')!r}")
        if wf.get("ref") != ctx.source_ref:
            errs.append(f"{w.name}: workflow ref {wf.get('ref')!r} != {ctx.source_ref}")
        commits = [(d.get("digest") or {}).get("gitCommit") for d in bd.get("resolvedDependencies") or []]
        if ctx.build_sha not in commits:
            errs.append(f"{w.name}: build commit {ctx.build_sha} not in provenance {commits}")
    return errs


def check_policy_pinned(ctx, params):
    errs = []
    for w, p in _preds(ctx, UPSTREAM, errs):
        if p.get("policy_sha256") != ctx.policy_sha:
            errs.append(f"{w.name}: attested policy_sha256 {p.get('policy_sha256')!r} != published {ctx.policy_sha}")
        if p.get("policy_version") != params["policy_version"]:
            errs.append(f"{w.name}: attested policy_version {p.get('policy_version')!r} != {params['policy_version']}")
    return errs


def check_release_assets(ctx, params):
    if ctx.assets is None:
        return ["assets dir not provided"]
    errs, expected, archives = [], set(), set()
    for w, p in _preds(ctx, UPSTREAM, errs):
        expected |= {w.name, f"{w.name}.attestations.jsonl"}
        archives.add(str(p.get("snapshot_archive_name", "")))
    if len(archives) != 1:
        errs.append(f"wheels disagree on the source archive: {sorted(archives)}")
    expected |= archives
    names = {p.name for p in ctx.assets.iterdir()} - {"SHA256SUMS", "SHA256SUMS.sigstore.json"}
    logs = {n for n in names if re.fullmatch(params["build_log_pattern"], n)}
    if len(logs) != 1:
        errs.append(f"expected exactly one build log, found {sorted(logs)}")
    got = names - logs
    if got != expected:
        errs.append(f"asset set mismatch: missing {sorted(expected - got)}, unexpected {sorted(got - expected)}")
    return errs


MANIFEST = "SHA256SUMS"
MANIFEST_BUNDLE = "SHA256SUMS.sigstore.json"


def _release_is_draft(ctx):
    rc, out = ctx.run(["gh", "api", f"repos/{ctx.repo}/releases?per_page=100", "--paginate",
                       "--jq", f'.[] | select(.tag_name=="{ctx.release_tag}") | .draft'])
    vals = out.split()
    if rc != 0 or not vals:
        return None
    return vals[0] == "true"


def check_release_manifest(ctx, params):
    if ctx.assets is None:
        return ["assets dir not provided"]
    if not ctx.release_tag:
        return ["--release-tag is required"]
    draft = _release_is_draft(ctx)
    if draft is None:
        return [f"cannot determine whether release {ctx.release_tag} is a draft"]
    if draft:
        return []  # signing happens after draft verification; enforced once published
    man, bun = ctx.assets / MANIFEST, ctx.assets / MANIFEST_BUNDLE
    errs = [f"missing {p.name}" for p in (man, bun) if not p.is_file()]
    if errs:
        return errs
    r = subprocess.run(["cosign", "verify-blob", "--bundle", str(bun),
                        "--certificate-identity", params["signer_identity"],
                        "--certificate-oidc-issuer", params["signer_issuer"], str(man)],
                       text=True, capture_output=True)
    if r.returncode != 0:
        return [f"cosign verify-blob failed: {r.stderr.strip()[-300:]}"]
    listed = {}
    for ln in man.read_text().splitlines():
        if not ln.strip():
            continue
        h, _, n = ln.partition(" ")
        n = n.lstrip(" *")
        if n in listed:
            errs.append(f"{n} listed twice in {MANIFEST}")
        listed[n] = h
    actual = {p.name for p in ctx.assets.iterdir() if p.is_file()} - {MANIFEST, MANIFEST_BUNDLE}
    if set(listed) != actual:
        errs.append(f"manifest/asset mismatch: not in manifest {sorted(actual - set(listed))}, "
                    f"not in release {sorted(set(listed) - actual)}")
    for n in sorted(set(listed) & actual):
        h = hashlib.sha256()
        with open(ctx.assets / n, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != listed[n]:
            errs.append(f"{n}: sha256 does not match {MANIFEST}")
    return errs


def check_actions_pinned(ctx, params):
    errs, seen = [], 0
    for name in params["workflows"]:
        f = ctx.root / ".github" / "workflows" / name
        if not f.exists():
            errs.append(f"{name}: workflow not found")
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            m = USES_RE.match(line)
            if not m or m.group(1).startswith("./"):
                continue
            seen += 1
            ref = m.group(1)
            ok = ("@sha256:" in ref) if ref.startswith("docker://") else \
                ("@" in ref and bool(SHA40_RE.match(ref.rpartition("@")[2])))
            if not ok:
                errs.append(f"{name}:{n}: not pinned to a full SHA: {ref}")
    if seen == 0:
        errs.append("found no `uses:` lines at all (parser broken?)")
    return errs


def check_predicate_complete(ctx, params):
    errs, want = [], set(params["required_fields"])
    for w, p in _preds(ctx, UPSTREAM, errs):
        got = set(p)
        if want - got:
            errs.append(f"{w.name}: predicate missing {sorted(want - got)}")
        if got - want:
            errs.append(f"{w.name}: predicate has unexpected fields {sorted(got - want)}")
    return errs


def check_scripts_at_commit(ctx, params):
    errs = []
    for w, p in _preds(ctx, UPSTREAM, errs):
        for field, path in sorted(params["files"].items()):
            try:
                want = hashlib.sha256(ctx.git_show(path)).hexdigest()
            except Exception as e:
                errs.append(f"{w.name}: {e}")
                continue
            if p.get(field) != want:
                errs.append(f"{w.name}: attested {field} {p.get(field)!r} != sha256 of {path} at {ctx.build_sha}")
    return errs


def check_lock_committed(ctx, params):
    try:
        committed = ctx.git_show(params["lock_path"])
    except Exception as e:
        return [str(e)]
    if committed != ctx.lock.read_bytes():
        return [f"{ctx.lock} differs from {params['lock_path']} at build commit {ctx.build_sha}"]
    return []


def check_release_target(ctx, params):
    if not ctx.release_tag:
        return ["release tag not provided"]
    rc, out = ctx.run(["gh", "release", "view", ctx.release_tag, "--repo", ctx.repo,
                       "--json", "isDraft,targetCommitish"])
    if rc != 0:
        return [f"cannot view release {ctx.release_tag}"]
    try:
        info = json.loads(out)
    except ValueError:
        return ["release view output is not JSON"]
    errs = []
    if info.get("targetCommitish") != ctx.build_sha:
        errs.append(f"release target {info.get('targetCommitish')!r} != build commit {ctx.build_sha}")
    if info.get("isDraft") is False:
        rc, out = ctx.run(["gh", "api", f"repos/{ctx.repo}/git/ref/tags/{ctx.release_tag}"])
        if rc != 0:
            errs.append(f"cannot read tag ref {ctx.release_tag}")
        else:
            try:
                obj = json.loads(out)["object"]
            except (ValueError, KeyError):
                errs.append("tag ref output malformed")
            else:
                if obj.get("type") != "commit" or obj.get("sha") != ctx.build_sha:
                    errs.append(f"tag {ctx.release_tag} points at {obj.get('sha')!r}, not {ctx.build_sha}")
    return errs


# ---------- driver ----------

def run_rule(rule, ctx):
    fn = globals().get(rule["check"])
    if not callable(fn):
        return [f"check {rule['check']} not found"]
    try:
        return fn(ctx, rule.get("parameters", {}))
    except Exception as e:  # fail closed
        return [f"{rule['check']} raised {type(e).__name__}: {e}"]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--wheel-dir", type=Path, required=True)
    p.add_argument("--assets-dir", type=Path)
    p.add_argument("--lock", type=Path, default=Path("builder.lock"))
    p.add_argument("--repo", default="patrickryankenneth/python-wheels-builds")
    p.add_argument("--signer-workflow",
                   default="patrickryankenneth/python-wheels-builds/.github/workflows/build-dbt-oss-musllinux.yml")
    p.add_argument("--source-ref", required=True)
    p.add_argument("--build-sha", required=True)
    p.add_argument("--upstream-tag", required=True)
    p.add_argument("--release-tag", help="release name, e.g. dbt-oss-v2.0.5.post2 (BLD-13)")
    p.add_argument("--only", help="run a single rule id")
    a = p.parse_args()
    raw = a.policy.read_bytes()
    policy = json.loads(raw)
    ctx = Ctx(a, raw)
    failed = 0
    for rule in policy["rules"]:
        if a.only and rule["id"] != a.only:
            continue
        if rule["status"] == "declared":
            print(f"DECLARED {rule['id']}  {rule['title']} (trust root, not verified)")
            continue
        errs = run_rule(rule, ctx)
        if errs:
            failed += 1
            print(f"FAIL     {rule['id']}  {rule['title']}")
            for e in errs:
                print(f"::error::{rule['id']}: {e}")
        else:
            print(f"PASS     {rule['id']}  {rule['title']}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
