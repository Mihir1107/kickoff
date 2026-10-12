"""Every text ORDER BY in the product orders by code point (ADR 0018 §28, §30).

Static half of the collation check (the dynamic half runs the corpus against a database with a
tailored ICU collation: `tests/integration/corpus/test_collation.py`). Every ``ORDER BY`` term in
the product's SQL (every string literal under packages/, apps/ and workers/, migrations included),
against a database migrated from scratch here, is
either explicitly ``COLLATE "C"``, a column whose every collatable definition in the schema is
COLLATE "C", a column that is not text anywhere, or an expression listed in ``NOT_TEXT`` with the
reason. A new ORDER BY on a text column with the database's default collation fails here, naming
the file and the term.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade

from .conftest import Connect

ROOT = Path(__file__).resolve().parents[3]
SCRATCH_DB = "edisc_collation_static"  # migrated here, so a change to a migration counts too
SOURCES = ("packages", "apps", "workers")
ORDER_BY = re.compile(r"\bORDER BY\b(.*?)(?=\bLIMIT\b|\bOFFSET\b|\bFOR\b|;|$)", re.S | re.I)
# ordered expressions that are not text (each with the reason)
NOT_TEXT = {
    "string_to_array(normalizer_version, '.')::int[]": "an integer array",
}


def _strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _terms(clause: str) -> list[str]:
    """Top-level comma-separated terms of an ORDER BY clause, cut at the closing parenthesis of an
    enclosing call (`array_agg(x ORDER BY y)`, `OVER (ORDER BY ...)`)."""
    out, depth, cur = [], 0, ""
    for ch in clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    cleaned = []
    for term in out:
        t = re.sub(r"\s+(ASC|DESC)\b.*$", "", term.strip(), flags=re.I | re.S)
        t = re.sub(r"\s+NULLS\s+(FIRST|LAST)\b.*$", "", t, flags=re.I)
        if t:
            cleaned.append(t)
    return cleaned


def ordered_terms() -> list[tuple[str, str]]:
    found = []
    for top in SOURCES:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "/tests/" in str(path) or "apps/web" in str(path):
                continue
            for s in _strings(path):
                for m in ORDER_BY.finditer(s):
                    found += [(str(path.relative_to(ROOT)), t) for t in _terms(m.group(1))]
    return found


async def test_ordered_text_columns_order_by_code_point(
    connect: Connect, settings: Settings
) -> None:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()
    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB)
    conn = await connect("owner", db=SCRATCH_DB)
    try:
        rows = await conn.fetch(
            "SELECT a.attname AS name, c.relname AS rel, coalesce(co.collname, 'default') AS coll"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " LEFT JOIN pg_collation co ON co.oid = a.attcollation"
            " WHERE n.nspname = 'edisc' AND c.relkind IN ('r', 'p', 'v') AND a.attnum > 0"
            " AND NOT a.attisdropped AND a.attcollation <> 0"
        )
        columns = {
            r["name"]
            for r in await conn.fetch(
                "SELECT DISTINCT a.attname AS name FROM pg_attribute a"
                " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'edisc' AND a.attnum > 0 AND NOT a.attisdropped"
            )
        }
    finally:
        await conn.close()
    collatable: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        collatable.setdefault(r["name"], []).append((r["rel"], r["coll"]))
    terms = await asyncio.to_thread(ordered_terms)  # parses every source file
    assert len(terms) > 50, "the scan found the product's ORDER BY clauses"
    problems = []
    for where, term in terms:
        if re.search(r'COLLATE\s+"C"\s*$', term) or term in NOT_TEXT:
            continue
        name = term.rsplit(".", 1)[-1].strip('"')
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            problems.append(f"{where}: ORDER BY {term!r} is an expression: COLLATE it or list it")
            continue
        if name not in columns:
            problems.append(f"{where}: ORDER BY {term!r} names no column: COLLATE it")
            continue
        loose = [f"{rel}.{name} ({coll})" for rel, coll in collatable.get(name, []) if coll != "C"]
        if loose:
            problems.append(f"{where}: ORDER BY {term!r}: text with another collation: {loose}")
    assert problems == [], "\n".join(problems)
