"""Split a SQL script into statements (asyncpg cannot run multi-statement prepared queries)."""

from __future__ import annotations


def split_sql(script: str) -> list[str]:
    """Split on top-level ``;``, respecting ``'...'`` literals, ``"..."`` identifiers, ``$$`` bodies
    and ``--`` comments. Sufficient for our hand-written migrations; not a general SQL parser."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    in_single = in_double = in_dollar = False
    while i < n:
        ch = script[i]
        if not (in_single or in_double or in_dollar) and script.startswith("--", i):
            end = script.find("\n", i)
            i = n if end == -1 else end
            continue
        if not (in_single or in_double) and script.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if not (in_double or in_dollar) and ch == "'":
            in_single = not in_single
        elif not (in_single or in_dollar) and ch == '"':
            in_double = not in_double
        if ch == ";" and not (in_single or in_double or in_dollar):
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    if in_single or in_double or in_dollar:
        raise ValueError("unterminated quote or dollar-quoted body in SQL script")
    return statements
