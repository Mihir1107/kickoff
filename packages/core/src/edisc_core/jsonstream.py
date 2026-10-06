"""Stream the elements of a top-level JSON array without loading the array (ADR 0014: metadata files
such as ``users.json`` can be large; "streaming, never loading").

``iter_array_elements`` yields each element's raw bytes (for ``json.loads``). Memory is bounded by the
largest element, which is capped by ``max_element_bytes``. It only finds element boundaries (strings,
escapes, nesting); each element's own JSON validity is checked by whoever parses it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterable, AsyncIterator

_SPECIAL = re.compile(rb'[\[\]{}",\\]')
_IN_STRING = re.compile(rb'["\\]')
_WS = b" \t\r\n"
_BOM = b"\xef\xbb\xbf"


class JsonStreamError(ValueError):
    """Not a JSON array, truncated, an empty element, or an element over the size cap."""


async def iter_array_elements(
    chunks: AsyncIterable[bytes], *, max_element_bytes: int
) -> AsyncIterator[bytes]:
    depth = 0  # 1 = inside the top-level array
    started = done = in_string = escape = False
    emitted = 0
    commas = 0
    cur = bytearray()

    def grow(piece: bytes | memoryview) -> None:
        cur.extend(piece)
        if len(cur) > max_element_bytes:
            raise JsonStreamError(f"array element larger than {max_element_bytes} bytes")

    async for chunk in _without_bom(chunks):
        # scanning is CPU work (about 20 MB/s here) and a coalescing source serves megabytes
        # without suspending: give the event loop (and the activity's heartbeats) a turn per chunk
        await asyncio.sleep(0)
        pos, n = 0, len(chunk)
        while pos < n:
            if done:
                if chunk[pos:].strip(_WS):
                    raise JsonStreamError("data after the end of the array")
                pos = n
                break
            if not started:
                rest = chunk[pos:].lstrip(_WS)
                if not rest:
                    pos = n
                    break
                if rest[:1] != b"[":
                    raise JsonStreamError("not a JSON array")
                started, depth = True, 1
                pos = n - len(rest) + 1
                continue
            if in_string:
                if escape:
                    grow(chunk[pos : pos + 1])
                    escape, pos = False, pos + 1
                    continue
                m = _IN_STRING.search(chunk, pos)
                if m is None:
                    grow(chunk[pos:])
                    pos = n
                    continue
                grow(chunk[pos : m.end()])
                pos = m.end()
                if m.group() == b'"':
                    in_string = False
                else:
                    escape = True
                continue
            m = _SPECIAL.search(chunk, pos)
            if m is None:
                grow(chunk[pos:])
                pos = n
                continue
            grow(chunk[pos : m.start()])
            c = m.group()
            pos = m.end()
            if depth == 1 and c in (b",", b"]"):
                element = bytes(cur).strip(_WS)
                cur.clear()
                if c == b",":
                    commas += 1
                    if not element:
                        raise JsonStreamError(f"empty array element after element {emitted}")
                    emitted += 1
                    yield element
                    continue
                if element:
                    emitted += 1
                    yield element
                elif commas:
                    raise JsonStreamError("trailing comma in array")
                depth, done = 0, True
                continue
            grow(c)
            if c == b'"':
                in_string = True
            elif c in (b"[", b"{"):
                depth += 1
            elif c in (b"]", b"}"):
                depth -= 1
            # a stray backslash outside a string stays in the element; json.loads rejects it
    if not done:
        raise JsonStreamError("truncated JSON array" if started else "empty input")


async def _without_bom(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Drop a leading UTF-8 byte order mark, even when it is split across chunks."""
    head = b""
    it = aiter(chunks)
    async for chunk in it:
        head += chunk
        if len(head) >= len(_BOM):
            break
    yield head.removeprefix(_BOM)
    async for chunk in it:
        yield chunk
