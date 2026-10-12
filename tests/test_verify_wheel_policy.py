import hashlib
import io
import json
import subprocess
import sys
import tarfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import verify_wheel_policy as v  # noqa: E402

SHA = "a" * 40
DIG = "sha256:" + "b" * 64
IMG = "ghcr.io/patrickryankenneth/python-wheels-builder-alpine"
WHEEL = "dbt_oss-2.0.5-cp311-abi3-musllinux_1_2_x86_64.whl"
ARCH = "dbt-oss-source-" + SHA + ".tar.gz"


def make_ctx(tmp_path, preds=None, lock_text=None, **over):
    wd = tmp_path / "wheels"
    wd.mkdir(exist_ok=True)
    (wd / WHEEL).write_bytes(b"w")
    lock = tmp_path / "builder.lock"
    lock.write_text(lock_text or f"release x\nx86_64 {DIG}\n")
    a = types.SimpleNamespace(root=str(tmp_path), wheel_dir=str(wd), assets_dir=str(tmp_path / "assets"),
                              lock=str(lock), repo="o/r", signer_workflow="o/r/.github/workflows/b.yml",
                              source_ref="refs/heads/main", build_sha=SHA, upstream_tag="v2.0.5",
                              release_tag="dbt-oss-v2.0.5.post2")
    for k, val in over.items():
        setattr(a, k, val)
    ctx = v.Ctx(a, b"policy-bytes")
    if preds is not None:
        ctx.predicate = lambda wheel, ptype: (preds.get(ptype), None)
    return ctx


def make_archive(assets, channel):
    assets.mkdir(exist_ok=True)
    data = f'[toolchain]\nchannel = "{channel}"\n'.encode()
    with tarfile.open(assets / ARCH, "w:gz") as t:
        ti = tarfile.TarInfo("rust-toolchain.toml")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))


def run_stub(responses):
    def run(argv):
        joined = " ".join(argv)
        for key, res in responses.items():
            if key in joined:
                return res
        return 1, ""
    return run


def test_satisfies():
    assert v._satisfies("1.96", "rustc 1.96.1 (x)")
    assert not v._satisfies("1.96", "rustc 1.97.0 (x)")
    assert not v._satisfies("1.96.1", "rustc 1.96.2 (x)")
    assert not v._satisfies("stable", "rustc 1.96.1 (x)")
    assert not v._satisfies("1.96", None)


def test_builder_pinned(tmp_path):
    ok = make_ctx(tmp_path, preds={v.UPSTREAM: {"builder_ref": f"{IMG}@{DIG}"}})
    assert v.check_builder_pinned(ok, {"image": IMG}) == []
    missing = make_ctx(tmp_path, preds={v.UPSTREAM: {}})
    assert v.check_builder_pinned(missing, {"image": IMG})
    wrong = make_ctx(tmp_path, preds={v.UPSTREAM: {"builder_ref": f"{IMG}@{DIG}"}},
                     lock_text=f"aarch64 {DIG}\n")
    assert v.check_builder_pinned(wrong, {"image": IMG})
    other = make_ctx(tmp_path, preds={v.UPSTREAM: {"builder_ref": f"{IMG}@sha256:{'c' * 64}"}})
    assert v.check_builder_pinned(other, {"image": IMG})


def test_toolchain(tmp_path):
    assets = tmp_path / "assets"
    make_archive(assets, "1.96")
    pred = {"snapshot_archive_name": ARCH, "rustc_version": "rustc 1.96.1 (31fca3adb 2026-06-26)"}
    params = {"toolchain_file": "rust-toolchain.toml"}
    assert v.check_toolchain(make_ctx(tmp_path, preds={v.UPSTREAM: pred}), params) == []
    bad = dict(pred, rustc_version="rustc 1.98.1 (x)")
    assert v.check_toolchain(make_ctx(tmp_path, preds={v.UPSTREAM: bad}), params)
    gone = dict(pred, rustc_version=None)
    assert v.check_toolchain(make_ctx(tmp_path, preds={v.UPSTREAM: gone}), params)


def test_release_assets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    for n in (WHEEL, WHEEL + ".attestations.jsonl", ARCH, "build-log-v2.0.5.post2.txt.gz"):
        (assets / n).write_bytes(b"x")
    params = {"build_log_pattern": r"build-log-.+\.txt\.gz"}
    ctx = make_ctx(tmp_path, preds={v.UPSTREAM: {"snapshot_archive_name": ARCH}})
    assert v.check_release_assets(ctx, params) == []
    (assets / "extra.txt").write_bytes(b"x")
    assert v.check_release_assets(ctx, params)


def test_predicate_complete(tmp_path):
    params = {"required_fields": ["a", "b"]}
    assert v.check_predicate_complete(make_ctx(tmp_path, preds={v.UPSTREAM: {"a": 1, "b": 2}}), params) == []
    assert v.check_predicate_complete(make_ctx(tmp_path, preds={v.UPSTREAM: {"a": 1}}), params)
    assert v.check_predicate_complete(make_ctx(tmp_path, preds={v.UPSTREAM: {"a": 1, "b": 2, "c": 3}}), params)


def test_scripts_at_commit(tmp_path):
    data = b"print('x')\n"
    good = hashlib.sha256(data).hexdigest()
    params = {"files": {"script_sha256": "s.py"}}
    ctx = make_ctx(tmp_path, preds={v.UPSTREAM: {"script_sha256": good}})
    ctx.git_show = lambda path: data
    assert v.check_scripts_at_commit(ctx, params) == []
    bad = make_ctx(tmp_path, preds={v.UPSTREAM: {"script_sha256": "0" * 64}})
    bad.git_show = lambda path: data
    assert v.check_scripts_at_commit(bad, params)
    absent = make_ctx(tmp_path, preds={v.UPSTREAM: {}})
    absent.git_show = lambda path: data
    assert v.check_scripts_at_commit(absent, params)


def test_lock_committed(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.git_show = lambda path: ctx.lock.read_bytes()
    assert v.check_lock_committed(ctx, {"lock_path": "builder.lock"}) == []
    ctx.git_show = lambda path: b"different"
    assert v.check_lock_committed(ctx, {"lock_path": "builder.lock"})


def test_release_target(tmp_path):
    draft = json.dumps({"isDraft": True, "targetCommitish": SHA})
    pub = json.dumps({"isDraft": False, "targetCommitish": SHA})
    ref_ok = json.dumps({"object": {"type": "commit", "sha": SHA}})
    ref_bad = json.dumps({"object": {"type": "commit", "sha": "d" * 40}})
    ctx = make_ctx(tmp_path)
    ctx.run = run_stub({"release view": (0, draft)})
    assert v.check_release_target(ctx, {}) == []
    ctx.run = run_stub({"release view": (0, pub), "git/ref/tags": (0, ref_ok)})
    assert v.check_release_target(ctx, {}) == []
    ctx.run = run_stub({"release view": (0, pub), "git/ref/tags": (0, ref_bad)})
    assert v.check_release_target(ctx, {})
    ctx.run = run_stub({"release view": (0, json.dumps({"isDraft": True, "targetCommitish": "main"}))})
    assert v.check_release_target(ctx, {})
    assert v.check_release_target(make_ctx(tmp_path, release_tag=None), {})


def test_actions_pinned(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yml").write_text(f"steps:\n  - uses: actions/checkout@{'c' * 40} # v4\n")
    ctx = make_ctx(tmp_path)
    assert v.check_actions_pinned(ctx, {"workflows": ["a.yml"]}) == []
    (wf / "a.yml").write_text(f"steps:\n  - uses: actions/checkout@{'c' * 40}\n  - uses: actions/cache@v4\n")
    errs = v.check_actions_pinned(ctx, {"workflows": ["a.yml"]})
    assert len(errs) == 1 and "cache" in errs[0]


def test_fails_closed(tmp_path):
    ctx = make_ctx(tmp_path)
    assert v.run_rule({"check": "nope"}, ctx)
    assert v.run_rule({"check": "check_builder_pinned"}, ctx)


def test_policy_lints():
    base = [sys.executable, str(ROOT / ".github/scripts/policy_tool_v2.py"),
            "--policy", str(ROOT / "policy/wheel-build/policy.json"),
            "--md", str(ROOT / "policy/wheel-build/POLICY.md"),
            "--verifier", str(ROOT / ".github/scripts/verify_wheel_policy.py")]
    for cmd in ("lint", "check-md"):
        r = subprocess.run(base + [cmd], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
