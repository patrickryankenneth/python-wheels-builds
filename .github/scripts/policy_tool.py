#!/usr/bin/env python3
"""
policy_tool.py - lint, render, hash and stage policy/policy.json.

policy.json is the source of truth. Its sha256 is taken over the exact file
bytes, so it equals the digest GitHub shows for the release asset. Lint
requires the file to already be in canonical form (indent=2, sorted keys,
trailing newline) so the same policy always hashes the same.

POLICY.md is generated from policy.json; `check-md` fails on drift.

Subcommands: lint | fmt | render-md | check-md | hash | version | mode | stage <dir>
Pure stdlib. Prints ::error:: lines so failures show up in Actions logs.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ENFORCED_VERSION_RE = re.compile(r"^v[1-9][0-9]*$")
LEGACY_VERSION_RE = re.compile(r"^legacy-[0-9]{4}-(0[1-9]|1[0-2])$")
RULE_ID_RE = re.compile(r"^[A-Z]{3,4}-[0-9]+$")
MODES = ("descriptive", "enforced")
STATUSES = ("performed-at-build", "verified-at-release", "declared", "enforced")
RULE_FIELDS = {"id", "title", "statement", "status", "implemented_by"}
TOP_FIELDS = {"schema", "policy_version", "enforcement_mode", "legacy_aliases",
              "summary", "rules", "not_guaranteed"}


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


def validate(obj) -> list[str]:
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["top level must be an object"]
    extra, missing = set(obj) - TOP_FIELDS, TOP_FIELDS - set(obj)
    if extra:
        errs.append(f"unknown top-level fields: {sorted(extra)}")
    if missing:
        errs.append(f"missing top-level fields: {sorted(missing)}")
        return errs
    if obj["schema"] != 1:
        errs.append("schema must be 1")
    if obj["enforcement_mode"] not in MODES:
        errs.append(f"enforcement_mode must be one of {MODES}")
    ver = obj["policy_version"]
    if not isinstance(ver, str):
        errs.append("policy_version must be a string")
    elif obj["enforcement_mode"] == "enforced" and not ENFORCED_VERSION_RE.match(ver):
        errs.append("enforced policies must be versioned v1, v2, ...")
    elif obj["enforcement_mode"] == "descriptive" and not LEGACY_VERSION_RE.match(ver):
        errs.append("descriptive policies must be versioned legacy-YYYY-MM (v1, v2, ... are reserved for enforced policies)")
    if not (isinstance(obj["legacy_aliases"], list) and all(isinstance(a, str) for a in obj["legacy_aliases"])):
        errs.append("legacy_aliases must be a list of strings")
    if not (isinstance(obj["summary"], str) and obj["summary"].strip()):
        errs.append("summary must be a non-empty string")
    if not (isinstance(obj["not_guaranteed"], list) and all(isinstance(s, str) and s.strip() for s in obj["not_guaranteed"])):
        errs.append("not_guaranteed must be a list of non-empty strings")
    rules = obj["rules"]
    if not (isinstance(rules, list) and rules):
        errs.append("rules must be a non-empty list")
        return errs
    seen: set[str] = set()
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{where} must be an object")
            continue
        unknown = set(r) - RULE_FIELDS
        if unknown:
            errs.append(f"{where} unknown fields: {sorted(unknown)}")
        for f in ("id", "title", "statement", "status"):
            if not (isinstance(r.get(f), str) and r[f].strip()):
                errs.append(f"{where}.{f} must be a non-empty string")
        rid = r.get("id", "")
        if not RULE_ID_RE.match(rid or ""):
            errs.append(f"{where}.id {rid!r} must look like SRC-1")
        if rid in seen:
            errs.append(f"duplicate rule id {rid}")
        seen.add(rid)
        if r.get("status") not in STATUSES:
            errs.append(f"{where}.status must be one of {STATUSES}")
        if obj["enforcement_mode"] == "descriptive" and r.get("status") == "enforced":
            errs.append(f"{where}: status 'enforced' not allowed when enforcement_mode is 'descriptive'")
    return errs


def render(obj) -> str:
    out = [
        f"# Policy {obj['policy_version']}",
        "",
        "<!-- Generated from policy/policy.json by policy_tool.py render-md. Do not edit. -->",
        "",
        f"- Enforcement mode: **{obj['enforcement_mode']}**",
        f"- Legacy aliases: {', '.join(f'`{a}`' for a in obj['legacy_aliases']) or 'none'}",
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
        if r.get("implemented_by"):
            out.append(f"- Implemented by: {r['implemented_by']}")
        out += ["", r["statement"], ""]
    out += ["## Not guaranteed", ""]
    out += [f"- {s}" for s in obj["not_guaranteed"]]
    return "\n".join(out) + "\n"


def cmd_lint(a) -> None:
    raw, obj = load(a.policy)
    errs = validate(obj)
    if not errs and raw != canonical(obj):
        errs.append(f"{a.policy} is not canonical - run: python3 .github/scripts/policy_tool.py fmt")
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
        die(f"{a.md} is out of date - run: python3 .github/scripts/policy_tool.py render-md")
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
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("lint", cmd_lint), ("fmt", cmd_fmt), ("render-md", cmd_render_md),
                     ("check-md", cmd_check_md), ("hash", cmd_hash), ("version", cmd_version),
                     ("mode", cmd_mode)):
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("stage")
    s.add_argument("outdir", type=Path)
    s.set_defaults(fn=cmd_stage)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
