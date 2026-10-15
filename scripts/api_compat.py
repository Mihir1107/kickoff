"""Is the API change compatible? (M17 step 4, plan §5, threat T26.)

    uv run python scripts/api_compat.py --base-ref <git ref>           # CI: spec at the merge base
    uv run python scripts/api_compat.py --base old.json --new new.json  # two files

Compares ``docs/api/openapi.json`` (or ``--new``) with the spec at ``--base-ref`` (or ``--base``):

1. **oasdiff** ``breaking`` (release pinned below, each archive by SHA-256, downloaded once into
   ``~/.cache/edisc``): every ERR-level finding breaks. WARN-level findings (a new enum value in a
   response: allowed by decision D9, the UI renders unknown values as "unknown") are listed, not
   failed.
2. **Our rules**, which oasdiff does not know or does not rate ERR (each has a fixture pair in
   ``tests/unit/api/test_api_compat.py``):
   - ``edisc-x-permission-changed``: an existing operation's ``x-permission`` changed;
   - ``edisc-operation-id-changed``: generated clients are named by it;
   - ``edisc-error-code-removed``: a code left an operation's ``x-error-codes`` list;
   - ``edisc-sse-event-changed``: a schema an operation lists in ``x-sse-events`` changed or left
     the list;
   - where oasdiff 1.33 is silent or rates it below ERR (found by the fixture pairs): a removed
     response status (``edisc-response-status-removed``), a removed media type
     (``edisc-media-type-removed``), a property removed from any schema
     (``edisc-property-removed``: optional response fields included; a client sending a removed
     request field gets a 422, our request models forbid extras), an enum value removed anywhere
     (``edisc-enum-value-removed``), a changed default (``edisc-default-changed``).
   oasdiff rates a NEW response enum value ERR (``response-property-enum-value-added``); decision
   D9 makes it not breaking, so it is reported as a warning.
3. **The allow-list** ``docs/api/breaking-allowlist.yaml``: an intended break needs an entry with
   ``rule`` (the finding's id), ``operation`` (the operationId, or ``"METHOD /path"``), ``reason``
   and ``frontend_commit`` (the frontend commit that absorbs it). An entry missing any of the four,
   or one that matches nothing, FAILS: the list holds only live, explained exceptions.

Exit 0 when nothing breaks; 1 otherwise. With ``GITHUB_STEP_SUMMARY`` set, a summary is appended.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

OASDIFF_VERSION = "1.33.0"
OASDIFF_SHA256 = {  # from the release's checksums.txt, verified when pinned
    "darwin_all": "2a479337c15afdcbf0b1e89c0b4d0cf0176472dbc11483358fed1f831d1e46c5",
    "linux_amd64": "43a4e328e2d13ba1552d760aa68d2485c75c5621f309f6ff64ae895188345247",
}
SPEC = Path("docs/api/openapi.json")
ALLOWLIST = Path("docs/api/breaking-allowlist.yaml")
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
ERR, WARN = 3, 2
NOT_BREAKING = frozenset({"response-property-enum-value-added"})  # decision D9
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


class CompatError(RuntimeError):
    pass


@dataclass(frozen=True)
class Finding:
    id: str
    level: int
    operation: str  # "METHOD /path"
    operation_id: str | None
    text: str

    def matches(self, operation: str) -> bool:
        return operation in (self.operation, self.operation_id)


# ------------------------------------------------------------------ oasdiff
def _platform() -> str:
    system, machine = platform.system().lower(), platform.machine().lower()
    if system == "darwin":
        return "darwin_all"
    if system == "linux" and machine in ("x86_64", "amd64"):
        return "linux_amd64"
    raise CompatError(f"no pinned oasdiff build for {system}/{machine}")


def oasdiff_binary() -> Path:
    """The pinned oasdiff, downloaded and checked once (never a binary from PATH)."""
    plat = _platform()
    cache = Path(os.environ.get("EDISC_TOOL_CACHE", Path.home() / ".cache" / "edisc"))
    target = cache / f"oasdiff-{OASDIFF_VERSION}-{plat}" / "oasdiff"
    if target.exists():
        return target
    name = f"oasdiff_{OASDIFF_VERSION}_{plat}.tar.gz"
    url = f"https://github.com/oasdiff/oasdiff/releases/download/v{OASDIFF_VERSION}/{name}"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != OASDIFF_SHA256[plat]:
        raise CompatError(f"{name}: SHA-256 {digest} is not the pinned one")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember("oasdiff")
        source = archive.extractfile(member)
        if source is None:
            raise CompatError(f"{name}: no oasdiff binary")
        binary = source.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    partial.write_bytes(binary)
    partial.chmod(0o755)
    partial.replace(target)
    return target


def oasdiff_findings(base: Path, new: Path) -> list[Finding]:
    result = subprocess.run(  # noqa: S603 (the pinned binary, our own files)
        [str(oasdiff_binary()), "breaking", str(base), str(new), "--format", "json"],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    if result.returncode != 0:
        raise CompatError(f"oasdiff failed ({result.returncode}): {result.stderr[-2000:]}")
    raw = json.loads(result.stdout or "[]") or []
    return [
        Finding(
            f["id"],
            WARN if f["id"] in NOT_BREAKING else int(f["level"]),
            f"{f.get('operation', '')} {f.get('path', '')}".strip(),
            f.get("operationId"),
            f.get("text", ""),
        )
        for f in raw
    ]


# ------------------------------------------------------------------ our rules
def _ops(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{m.upper()} {p}": op
        for p, item in doc.get("paths", {}).items()
        for m, op in item.items()
        if m in METHODS
    }


def _nodes(schema: Any, at: str) -> dict[str, dict[str, Any]]:
    """Every schema node under ``schema`` by its location (properties, items, compositions)."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(schema, dict):
        return out
    out[at] = schema
    for name, sub in schema.get("properties", {}).items():
        out.update(_nodes(sub, f"{at}.{name}"))
    if "items" in schema:
        out.update(_nodes(schema["items"], f"{at}[]"))
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key, [])):
            out.update(_nodes(sub, f"{at}<{key}{i}>"))
    return out


def _schema_findings(base: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    """Properties, enum values and defaults, over every component and parameter schema."""

    def collect(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        nodes: dict[str, dict[str, Any]] = {}
        for name, schema in doc.get("components", {}).get("schemas", {}).items():
            nodes.update(_nodes(schema, f"#/components/schemas/{name}"))
        for key, op in _ops(doc).items():
            for param in op.get("parameters", []):
                nodes.update(
                    _nodes(param.get("schema"), f"{key} {param.get('in')}:{param.get('name')}")
                )
        return nodes

    found: list[Finding] = []
    old_nodes, new_nodes = collect(base), collect(new)
    for at, old in old_nodes.items():
        cur = new_nodes.get(at)
        if cur is None:
            continue  # the parent's property removal (or the operation's) is the finding
        for name in sorted(set(old.get("properties", {})) - set(cur.get("properties", {}))):
            found.append(
                Finding("edisc-property-removed", ERR, at, None, f"property {name!r} removed")
            )
        for value in [v for v in old.get("enum", []) if v not in cur.get("enum", [])]:
            found.append(
                Finding("edisc-enum-value-removed", ERR, at, None, f"enum value {value!r} removed")
            )
        if "default" in old and old.get("default") != cur.get("default"):
            found.append(Finding("edisc-default-changed", ERR, at, None,
                                 f"default {old['default']!r} -> {cur.get('default')!r}"))  # fmt: skip
    return found


def our_findings(base: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = _schema_findings(base, new)
    new_ops = _ops(new)
    schemas = (base.get("components", {}).get("schemas", {}),
               new.get("components", {}).get("schemas", {}))  # fmt: skip
    for key, old in _ops(base).items():
        cur = new_ops.get(key)
        if cur is None:
            continue  # a removed operation: oasdiff's own finding
        oid = old.get("operationId")

        def add(rule: str, text: str, oid: Any = oid, key: str = key) -> None:
            found.append(Finding(rule, ERR, key, oid, text))

        if old.get("x-permission") != cur.get("x-permission"):
            add("edisc-x-permission-changed",
                f"x-permission {old.get('x-permission')!r} -> {cur.get('x-permission')!r}")  # fmt: skip
        if oid != cur.get("operationId"):
            add("edisc-operation-id-changed", f"operationId {oid!r} -> {cur.get('operationId')!r}")
        for code in sorted(set(old.get("x-error-codes", [])) - set(cur.get("x-error-codes", []))):
            add("edisc-error-code-removed", f"error code {code!r} removed")
        for status, response in old.get("responses", {}).items():
            now = cur.get("responses", {}).get(status)
            if now is None:
                add("edisc-response-status-removed", f"response {status} removed")
                continue
            for media in sorted(set(response.get("content", {})) - set(now.get("content", {}))):
                add("edisc-media-type-removed", f"response {status} media type {media!r} removed")
        old_body = old.get("requestBody", {}).get("content", {})
        new_body = cur.get("requestBody", {}).get("content", {})
        for media in sorted(set(old_body) - set(new_body)) if old_body else []:
            add("edisc-media-type-removed", f"request media type {media!r} removed")
        for name in old.get("x-sse-events", []):
            if name not in cur.get("x-sse-events", []):
                add("edisc-sse-event-changed", f"SSE event {name!r} removed")
            elif schemas[0].get(name) != schemas[1].get(name):
                add("edisc-sse-event-changed", f"SSE event schema {name!r} changed")
    return found


# ------------------------------------------------------------------ the allow-list
def load_allowlist(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text()) or []
    if not isinstance(entries, list):
        raise CompatError(f"{path}: a list of entries")
    for i, e in enumerate(entries):
        missing = [k for k in ("rule", "operation", "reason", "frontend_commit")
                   if not (isinstance(e, dict) and isinstance(e.get(k), str) and e[k].strip())]  # fmt: skip
        if missing:
            raise CompatError(f"{path} entry {i}: missing {', '.join(missing)}")
        if not COMMIT.match(e["frontend_commit"]):
            raise CompatError(f"{path} entry {i}: frontend_commit must be a commit id")
    return entries


@dataclass
class Verdict:
    breaking: list[Finding]
    allowed: list[Finding]
    warnings: list[Finding]
    unused: list[dict[str, str]]

    @property
    def ok(self) -> bool:
        return not self.breaking and not self.unused


def judge(findings: list[Finding], allowlist: list[dict[str, str]]) -> Verdict:
    breaking, allowed, used = [], [], set()
    for f in (f for f in findings if f.level >= ERR):
        hit = [
            i for i, e in enumerate(allowlist) if e["rule"] == f.id and f.matches(e["operation"])
        ]
        if hit:
            allowed.append(f)
            used.update(hit)
        else:
            breaking.append(f)
    warnings = [f for f in findings if f.level == WARN]
    unused = [e for i, e in enumerate(allowlist) if i not in used]
    return Verdict(breaking, allowed, warnings, unused)


def compare(base: dict[str, Any], new: dict[str, Any], allowlist: list[dict[str, str]]) -> Verdict:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "base.json"), Path(tmp, "new.json")
        a.write_text(json.dumps(base))
        b.write_text(json.dumps(new))
        findings = oasdiff_findings(a, b)
    return judge(findings + our_findings(base, new), allowlist)


def summary(v: Verdict) -> str:
    lines = ["## API compatibility", ""]
    for title, items in (("Breaking", v.breaking), ("Allowed by the allow-list", v.allowed),
                         ("Warnings (not failing; D9: new response enum values)", v.warnings)):  # fmt: skip
        lines.append(f"**{title}:** {len(items)}")
        lines += [f"- `{f.id}` {f.operation} ({f.operation_id}): {f.text}" for f in items]
        lines.append("")
    if v.unused:
        lines.append(f"**Allow-list entries matching nothing (fail):** {len(v.unused)}")
        lines += [f"- `{e['rule']}` {e['operation']}" for e in v.unused]
    return "\n".join(lines) + "\n"


def _spec_at(ref: str) -> dict[str, Any] | None:
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{ref}:{SPEC}"],  # noqa: S607 (git from PATH, like every make target)
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(result.stdout) if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API compatibility check (oasdiff + our rules)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base-ref")
    group.add_argument("--base", type=Path)
    parser.add_argument("--new", type=Path, default=SPEC)
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST)
    args = parser.parse_args(argv)
    if args.base_ref:
        base = _spec_at(args.base_ref)
        if base is None:
            print(f"no {SPEC} at {args.base_ref}: nothing to compare (the first published spec)")
            return 0
    else:
        base = json.loads(args.base.read_text())
    verdict = compare(base, json.loads(args.new.read_text()), load_allowlist(args.allowlist))
    text = summary(verdict)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(text)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
