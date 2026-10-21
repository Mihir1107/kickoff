"""RFC 6962 (Certificate Transparency) Merkle Tree Hash. Pure, stdlib only.

    MTH({})      = SHA-256()
    MTH({d0})    = SHA-256(0x00 || d0)                                   leaf hash
    MTH(D[n])    = SHA-256(0x01 || MTH(D[0:k]) || MTH(D[k:n]))            k = largest power of 2 < n

Domain separation (0x00 leaf / 0x01 node) prevents a leaf from being passed off as an internal node.
The split rule never duplicates a node, so ``[a, b, c]`` and ``[a, b, c, c]`` have different roots
(no CVE-2012-2459-style ambiguity, unlike Bitcoin's duplicate-last-node construction).

Custody batches (ADR 0003): leaf data for an item is ``bytes.fromhex(idempotency_key) ||
bytes.fromhex(content_hash)`` (32 + 32 bytes, fixed width, so unambiguous), and leaves are ordered by
``idempotency_key`` ascending (hex string order == byte order). Duplicate keys are an error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from itertools import pairwise

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _largest_power_of_two_below(n: int) -> int:
    return 1 << ((n - 1).bit_length() - 1)


def root_from_leaf_hashes(hashes: Sequence[bytes]) -> bytes:
    """Iterative RFC 6962 root. O(n) time; recursion depth O(log n)."""
    n = len(hashes)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return hashes[0]
    k = _largest_power_of_two_below(n)
    return node_hash(root_from_leaf_hashes(hashes[:k]), root_from_leaf_hashes(hashes[k:]))


def merkle_root(leaves: Iterable[bytes]) -> str:
    """Hex root over raw leaf data (already ordered by the caller)."""
    return root_from_leaf_hashes([leaf_hash(d) for d in leaves]).hex()


def item_leaf(idempotency_key: str, content_hash: str) -> bytes:
    key, content = bytes.fromhex(idempotency_key), bytes.fromhex(content_hash)
    if len(key) != 32 or len(content) != 32:
        raise ValueError("idempotency_key and content_hash must be 64 hex chars")
    return key + content


def batch_root(items: Iterable[tuple[str, str]]) -> str:
    """Merkle root for a custody batch: ``(idempotency_key, content_hash)`` pairs, any order in."""
    ordered = sorted(items)
    for (a, _), (b, _) in pairwise(ordered):
        if a == b:
            raise ValueError(f"duplicate idempotency_key in batch: {a}")
    return merkle_root(item_leaf(k, c) for k, c in ordered)
