"""RFC 6962 Merkle conformance: CT reference vectors, independent implementation, no duplication collisions."""

import hashlib
import itertools
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_custody.merkle import (
    EMPTY_ROOT,
    batch_root,
    item_leaf,
    leaf_hash,
    merkle_root,
    node_hash,
)

# Certificate Transparency reference test data (certificate-transparency / merkle_tree_test):
# leaf inputs and the root hash of the tree over the first n of them.
CT_LEAVES = [
    b"",
    b"\x00",
    b"\x10",
    b"\x20\x21",
    b"\x30\x31",
    b"\x40\x41\x42\x43",
    b"\x50\x51\x52\x53\x54\x55\x56\x57",
    b"\x60\x61\x62\x63\x64\x65\x66\x67\x68\x69\x6a\x6b\x6c\x6d\x6e\x6f",
]
CT_ROOTS = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]


def test_empty_tree() -> None:
    assert merkle_root([]) == EMPTY_ROOT == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("n", range(1, 9))
def test_certificate_transparency_vectors(n: int) -> None:
    assert merkle_root(CT_LEAVES[:n]) == CT_ROOTS[n - 1]


def test_small_trees_by_hand() -> None:
    a, b, c = (leaf_hash(x) for x in (b"a", b"b", b"c"))
    assert merkle_root([b"a"]) == a.hex()
    assert merkle_root([b"a", b"b"]) == node_hash(a, b).hex()
    # n=3: k=2 -> node(node(a,b), c): the odd leaf is promoted, never duplicated
    assert merkle_root([b"a", b"b", b"c"]) == node_hash(node_hash(a, b), c).hex()


def _reference_bottom_up(data: list[bytes]) -> str:
    """Independent construction: pair left-to-right per level, promote an unpaired last node unchanged.
    Equivalent to RFC 6962's split rule; written separately to cross-check the production code."""
    if not data:
        return hashlib.sha256(b"").hexdigest()
    level = [hashlib.sha256(b"\x00" + d).digest() for d in data]
    while len(level) > 1:
        nxt = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 255, 1001, 4097])
def test_matches_independent_implementation(n: int) -> None:
    data = [os.urandom(64) for _ in range(n)]
    assert merkle_root(data) == _reference_bottom_up(data)


@settings(max_examples=300)
@given(st.lists(st.binary(max_size=40), max_size=70))
def test_property_matches_independent_implementation(data: list[bytes]) -> None:
    assert merkle_root(data) == _reference_bottom_up(data)


@pytest.mark.parametrize("n", [1, 2, 3, 5, 6, 7, 9, 1001])
def test_duplicating_leaves_changes_the_root(n: int) -> None:
    """CVE-2012-2459 class: Bitcoin-style trees give [a,b,c] and [a,b,c,c] the same root."""
    data = [os.urandom(16) for _ in range(n)]
    base = merkle_root(data)
    assert merkle_root([*data, data[-1]]) != base
    assert merkle_root([*data, *data[-2:]]) != base
    assert merkle_root(data + data) != base


def test_no_collisions_among_duplication_variants() -> None:
    leaves = [bytes([i]) for i in range(6)]
    roots: dict[str, tuple[bytes, ...]] = {}
    for n in range(1, 7):
        base = leaves[:n]
        variants = [tuple(base)]
        variants += [tuple(base) + (base[-1],) * r for r in (1, 2, 3)]
        variants += [tuple(base) + tuple(base[i:]) for i in range(n)]
        for v in variants:
            r = merkle_root(v)
            assert roots.setdefault(r, v) == v, f"collision between {roots[r]} and {v}"


def test_leaf_is_not_confusable_with_node() -> None:
    a, b = leaf_hash(b"a"), leaf_hash(b"b")
    # A leaf whose data is the concatenation of two child hashes must not equal their parent node.
    assert merkle_root([a + b]) != merkle_root([b"a", b"b"])


def test_batch_root_orders_by_key_and_rejects_duplicates() -> None:
    items = [(os.urandom(32).hex(), os.urandom(32).hex()) for _ in range(11)]
    shuffled = list(reversed(items))
    assert batch_root(items) == batch_root(shuffled)
    expected = merkle_root(item_leaf(k, c) for k, c in sorted(items))
    assert batch_root(items) == expected
    with pytest.raises(ValueError, match="duplicate"):
        batch_root([*items, (items[0][0], os.urandom(32).hex())])
    # changing any content hash changes the root
    k, _ = items[3]
    altered = [(kk, os.urandom(32).hex() if kk == k else c) for kk, c in items]
    assert batch_root(altered) != batch_root(items)


def test_item_leaf_rejects_bad_lengths() -> None:
    with pytest.raises(ValueError, match="64 hex"):
        item_leaf("ab", "cd" * 32)


def test_all_permutations_of_small_set_are_order_sensitive_in_raw_api() -> None:
    data = [b"x", b"y", b"z"]
    roots = {merkle_root(p) for p in itertools.permutations(data)}
    assert len(roots) == 6  # raw merkle_root is order-sensitive; batch_root fixes the order
