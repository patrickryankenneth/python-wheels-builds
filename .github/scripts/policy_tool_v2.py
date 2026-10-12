#!/usr/bin/env python3
"""
policy_tool.py - lint, render, hash and stage a policy.json.

policy.json is the source of truth. Its sha256 is taken over the exact file
bytes, so it equals the digest GitHub shows for the release asset. Lint
requires the file to already be in canonical form (indent=2, sorted keys,
trailing newline) so the same policy always hashes the same.

POLICY.md is generated from policy.json; `check-md` fails on drift.

Every rule is either
  enforced  - names a `check_*` function in the verifier script; lint fails if
              the function does not exist, and fails if a check_* function
              exists that no rule uses.
  declared  - a trust root: evidence is recorded, nothing is verified. It must
              say what evidence in `evidence`, and may not carry a `check`.

Subcommands: lint | fmt | render-md | check-md | hash | version | mode |
             tag | stage <dir>
Pure stdlib. Prints ::error:: lines so failures show up in Actions logs.
"""
import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ENFORCED_VERSION_RE = re.compile(r"^v[1-9][0-9]*(\.[0-9]+)?$")
RULE_ID_RE = re.compile(r"^[A-Z]{3,4}-[0-9]+$")
CHECK_RE = re.compile(r"^check_[a-z0-9_]+$")
SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
STATUSES_V2 = ("enforced", "declared")
RULE_FIELDS_V2 = {"id", "title", "statement", "status", "implemented_by",
                  "check", "parameters", "evidence"}
TOP_FIELDS_V2 = {"schema", "scope", "policy_version", "enforcement_mode",
                 "summary", "rules", "not_guaranteed"}
DEFAULT_VERIFIER = ".github/scripts/verify_builder_policy.py"
V2_COMMENT = "<!-- Generated from policy.json by policy_tool.py render-md. Do not edit. -->"


def die(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def canonical(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path):
    if not path.exists():
        die(f"{path} not found")
    raw = path.read_bytes()
    try:
        return raw, json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}")


def verifier_checks(path: Path) -> set[str] | None:
    """Top-level check_* function names defined in the verifier, or None if absent."""
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(), filename=str(path))
    return {n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("check_")}


def _nonempty(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def validate_v2(obj, verifier: Path | None) -> list[str]:
    errs: list[str] = []
    extra, missing = set(obj) - TOP_FIELDS_V2, TOP_FIELDS_V2 - set(obj)
    if extra:
        errs.append(f"unknown top-level fields: {sorted(extra)}")
    if missing:
        errs.append(f"missing top-level fields: {sorted(missing)}")
        return errs
    if not (isinstance(obj["scope"], str) and SCOPE_RE.match(obj["scope"])):
        errs.append("scope must be lowercase words joined by '-', e.g. builder-image")
    if obj["enforcement_mode"] != "enforced":
        errs.append("schema 2 policies must have enforcement_mode 'enforced'")
    ver = obj["policy_version"]
    if not (isinstance(ver, str) and ENFORCED_VERSION_RE.match(ver)):
        errs.append("policy_version must look like v1, v2, v2.1, ...")
    if not _nonempty(obj["summary"]):
        errs.append("summary must be a non-empty string")
    if not (isinstance(obj["not_guaranteed"], list) and obj["not_guaranteed"]
            and all(_nonempty(s) for s in obj["not_guaranteed"])):
        errs.append("not_guaranteed must be a non-empty list of non-empty strings")
    rules = obj["rules"]
    if not (isinstance(rules, list) and rules):
        errs.append("rules must be a non-empty list")
        return errs
    seen: set[str] = set()
    used: set[str] = set()
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{where} must be an object")
            continue
        unknown = set(r) - RULE_FIELDS_V2
        if unknown:
            errs.append(f"{where} unknown fields: {sorted(unknown)}")
        for f in ("id", "title", "statement", "status", "implemented_by"):
            if not _nonempty(r.get(f)):
                errs.append(f"{where}.{f} must be a non-empty string")
        rid = r.get("id", "")
        if not RULE_ID_RE.match(rid or ""):
            errs.append(f"{where}.id {rid!r} must look like IMG-1")
        if rid in seen:
            errs.append(f"duplicate rule id {rid}")
        seen.add(rid)
        status = r.get("status")
        if status not in STATUSES_V2:
            errs.append(f"{where}.status must be one of {STATUSES_V2}")
        if "parameters" in r and not isinstance(r["parameters"], dict):
            errs.append(f"{where}.parameters must be an object")
        if status == "enforced":
            chk = r.get("check")
            if not (isinstance(chk, str) and CHECK_RE.match(chk)):
                errs.append(f"{where}: enforced rule {rid} needs a `check` like check_something")
            else:
                used.add(chk)
            if "evidence" in r:
                errs.append(f"{where}: enforced rule {rid} must not carry `evidence`")
        elif status == "declared":
            for f in ("check", "parameters"):
                if f in r:
                    errs.append(f"{where}: declared rule {rid} must not carry `{f}` (a declared rule verifies nothing)")
            if not _nonempty(r.get("evidence")):
                errs.append(f"{where}: declared rule {rid} must say what evidence is recorded")
    if not used:
        errs.append("a schema 2 policy needs at least one enforced rule")
    if verifier is not None:
        funcs = verifier_checks(verifier)
        if funcs is None:
            errs.append(f"verifier {verifier} not found")
        else:
            for c in sorted(used - funcs):
                errs.append(f"rule check {c} is not defined in {verifier}")
            for c in sorted(funcs - used):
                errs.append(f"{verifier} defines {c} but no enforced rule uses it")
    return errs


def validate(obj, verifier: Path | None = None) -> list[str]:
    if not isinstance(obj, dict):
        return ["top level must be an object"]
    if obj.get("schema") != 2:
        return ["schema must be 2"]
    return validate_v2(obj, verifier)


def _render_v2(obj) -> str:
    out = [
        f"# Policy {obj['scope']} {obj['policy_version']}",
        "",
        V2_COMMENT,
        "",
        f"- Scope: `{obj['scope']}`",
        f"- Enforcement mode: **{obj['enforcement_mode']}**",
        "- Hash: sha256 of the exact `policy.json` bytes, published as `policy.json.sha256` and as the release asset digest. "
        "This file cannot contain its own hash.",
        "",
        obj["summary"],
        "",
        "## Rules",
        "",
    ]
    for r in obj["rules"]:
        out += [f"### {r['id']} - {r['title']}", "", f"- Status: `{r['status']}`"]
        if r["status"] == "enforced":
            out.append(f"- Check: `{r['check']}`")
        out.append(f"- Implemented by: {r['implemented_by']}")
        if r["status"] == "declared":
            out.append(f"- Evidence recorded, not verified: {r['evidence']}")
        out += ["", r["statement"], ""]
        if r.get("parameters"):
            out += ["Parameters:", "", "```json",
                    json.dumps(r["parameters"], indent=2, sort_keys=True), "```", ""]
    out += ["## Not guaranteed", ""]
    out += [f"- {s}" for s in obj["not_guaranteed"]]
    return "\n".join(out) + "\n"


def render(obj) -> str:
    return _render_v2(obj)


def cmd_lint(a) -> None:
    raw, obj = load(a.policy)
    errs = validate(obj, a.verifier)
    if not errs and raw != canonical(obj):
        errs.append(f"{a.policy} is not canonical - run: python3 .github/scripts/policy_tool.py --policy {a.policy} fmt")
    for e in errs:
        print(f"::error::{e}")
    if errs:
        sys.exit(1)
    print(f"{a.policy} OK sha256={sha256_bytes(raw)}")


def cmd_fmt(a) -> None:
    _, obj = load(a.policy)
    a.policy.write_bytes(canonical(obj))
    print(f"rewrote {a.policy}")


def cmd_render_md(a) -> None:
    _, obj = load(a.policy)
    a.md.write_text(render(obj))
    print(f"wrote {a.md}")


def cmd_check_md(a) -> None:
    _, obj = load(a.policy)
    if not a.md.exists() or a.md.read_text() != render(obj):
        die(f"{a.md} is out of date - run: python3 .github/scripts/policy_tool.py --policy {a.policy} --md {a.md} render-md")
    print(f"{a.md} matches {a.policy}")


def cmd_hash(a) -> None:
    raw, _ = load(a.policy)
    print(sha256_bytes(raw))


def cmd_version(a) -> None:
    _, obj = load(a.policy)
    print(obj["policy_version"])


def cmd_mode(a) -> None:
    _, obj = load(a.policy)
    print(obj["enforcement_mode"])


def cmd_tag(a) -> None:
    """Git tag / release name: policy-<scope>-<version>."""
    _, obj = load(a.policy)
    print(f"policy-{obj['scope']}-{obj['policy_version']}")


def cmd_stage(a) -> None:
    cmd_lint(a)
    cmd_check_md(a)
    raw, _ = load(a.policy)
    a.outdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(a.policy, a.outdir / "policy.json")
    shutil.copy2(a.md, a.outdir / "POLICY.md")
    (a.outdir / "policy.json.sha256").write_text(f"{sha256_bytes(raw)}  policy.json\n")
    print(f"staged policy.json, POLICY.md, policy.json.sha256 in {a.outdir}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy", type=Path, default=Path("policy/policy.json"))
    p.add_argument("--md", type=Path, default=Path("policy/POLICY.md"))
    p.add_argument("--verifier", type=Path, default=Path(DEFAULT_VERIFIER),
                   help="verifier script whose check_* functions schema 2 rules must match exactly")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("lint", cmd_lint), ("fmt", cmd_fmt), ("render-md", cmd_render_md),
                     ("check-md", cmd_check_md), ("hash", cmd_hash), ("version", cmd_version),
                     ("mode", cmd_mode), ("tag", cmd_tag)):
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("stage")
    s.add_argument("outdir", type=Path)
    s.set_defaults(fn=cmd_stage)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()