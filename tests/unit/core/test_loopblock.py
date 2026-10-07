"""The deterministic blocking-I/O guard (`edisc_core.loopblock`) behind `tests/conftest.py`.

Each test installs its OWN config on top of the session one (so this file counts as "product" and its
blocking calls are observed) and pops it again; the session guard treats this file as a test and drops
everything, so nothing here fails the suite.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from edisc_core import loopblock, loopguard

HERE = str(Path(__file__).parent) + os.sep


def _as_product() -> tuple[list[loopblock.Violation], loopblock._Config]:
    got: list[loopblock.Violation] = []
    cfg = loopblock.install(got.append, product_roots=(HERE,), test_roots=())
    return got, cfg


async def test_a_blocking_socket_call_on_the_loop_is_flagged() -> None:
    got, cfg = _as_product()
    try:
        a, b = socket.socketpair()
        a.setblocking(True)
        b.setblocking(True)
        b.sendall(b"ping")
        a.recv(4)
        a.close()
        b.close()
    finally:
        loopblock.uninstall(cfg)
    kinds = {v.kind for v in got}
    assert kinds == {"blocking socket"}, [v.describe() for v in got]
    assert all(v.origin == "product" and "test_loopblock" in v.where for v in got)


async def test_a_non_blocking_socket_is_not_flagged() -> None:
    got, cfg = _as_product()
    try:
        a, b = socket.socketpair()
        a.setblocking(False)  # as asyncio and every async driver use their sockets
        with contextlib.suppress(BlockingIOError):
            a.recv(4)
        a.close()
        b.close()
    finally:
        loopblock.uninstall(cfg)
    assert got == []


async def test_time_sleep_on_the_loop_is_flagged_but_sleep_zero_is_not() -> None:
    got, cfg = _as_product()
    try:
        time.sleep(0)  # noqa: ASYNC251  (a cooperative no-op: not blocking)
        assert got == []
        time.sleep(0.01)  # noqa: ASYNC251
    finally:
        loopblock.uninstall(cfg)
    assert [v.kind for v in got] == ["time.sleep"] and got[0].origin == "product"


async def test_a_large_raw_read_or_write_on_the_loop_is_flagged_small_is_not(
    tmp_path: Path,
) -> None:
    got, cfg = _as_product()
    path = tmp_path / "raw.bin"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            os.write(fd, b"x" * (loopblock.THRESHOLD_BYTES + 1))
            os.lseek(fd, 0, os.SEEK_SET)
            os.read(fd, loopblock.THRESHOLD_BYTES + 1)
            os.lseek(fd, 0, os.SEEK_SET)
            os.read(fd, 8)  # small: below the threshold
        finally:
            os.close(fd)
    finally:
        loopblock.uninstall(cfg)
    assert sorted(v.kind for v in got) == ["os.read", "os.write"]
    assert all(v.origin == "product" for v in got)


async def test_a_large_buffered_file_read_on_the_loop_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "buffered.bin"
    path.write_bytes(b"y" * (loopblock.THRESHOLD_BYTES + 1))  # before the guard is installed
    got, cfg = _as_product()
    try:
        with open(path, "rb") as fh:  # noqa: ASYNC230  (product-opened on the loop -> proxied)
            fh.read()
        with open(path, "rb") as fh:  # noqa: ASYNC230
            fh.read(8)  # small read: not flagged
    finally:
        loopblock.uninstall(cfg)
    assert [v.kind for v in got] == ["file read/write"] and got[0].origin == "product"


async def test_a_test_origin_blocking_call_is_dropped() -> None:
    got: list[loopblock.Violation] = []
    cfg = loopblock.install(got.append, product_roots=(), test_roots=(HERE,))
    try:
        time.sleep(0.01)  # noqa: ASYNC251  ("test" for this config: the timing guard records it)
    finally:
        loopblock.uninstall(cfg)
    assert got == []


def test_off_the_loop_nothing_is_flagged() -> None:
    got, cfg = _as_product()
    try:
        time.sleep(0.01)  # a plain sync function: no running loop on this thread
        a, b = socket.socketpair()
        a.setblocking(True)
        b.sendall(b"x")
        a.recv(1)
        a.close()
        b.close()
    finally:
        loopblock.uninstall(cfg)
    assert got == []


def test_violations_round_trip_through_the_directory(tmp_path: Path) -> None:
    v = loopblock.Violation(
        id="abc", pid=1, kind="time.sleep", detail="time.sleep(1)", origin="product",
        where="x.py:1 in f", stack="",
    )  # fmt: skip
    loopblock.write_violation(tmp_path, v)
    seen: set[str] = set()
    (read,) = loopblock.read_violations(tmp_path, seen)
    assert read == v and loopblock.read_violations(tmp_path, seen) == []  # seen is honoured


def test_a_worker_process_reports_blocking_io_and_refuses_outside_test(tmp_path: Path) -> None:
    code = (
        "import asyncio, time\n"
        "from edisc_core import loopblock\n"
        "async def main():\n"
        "    loopblock.install_from_env(permitted=True, log=print)\n"
        "    time.sleep(0.02)\n"  # unattributed (in neither tree): still written
        "asyncio.run(main())\n"
    )
    env = {**os.environ, loopguard.ENV_MS: "100", loopguard.ENV_DIR: str(tmp_path)}
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)
    (v,) = loopblock.read_violations(tmp_path, set())
    assert v.kind == "time.sleep" and v.origin == "unattributed"
    assert (
        json.loads(next((tmp_path / loopblock.VIOL_SUBDIR).glob("*.json")).read_text())["pid"]
        == v.pid
    )

    refused = subprocess.run(
        [sys.executable, "-c", code.replace("permitted=True", "permitted=False")],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert refused.returncode != 0 and loopguard.ENV_MS in refused.stderr


async def test_a_sync_database_driver_is_caught_at_its_blocking_socket() -> None:
    """A synchronous DB driver has no special hook: it blocks on its socket, which is what the socket
    check sees. Stand in for one with a blocking socket carrying a query-shaped payload."""
    got, cfg = _as_product()
    try:
        a, b = socket.socketpair()
        a.setblocking(True)
        b.setblocking(True)
        b.sendall(b"SELECT 1")
        a.recv(8)  # the driver would block here waiting for the server
        a.close()
        b.close()
    finally:
        loopblock.uninstall(cfg)
    assert got and all(v.kind == "blocking socket" for v in got)
