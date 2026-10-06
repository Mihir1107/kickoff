"""The event-loop block guard (`edisc_core.loopguard`) behind `tests/conftest.py`."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from edisc_core import loopguard

HERE = str(Path(__file__).parent) + os.sep


def _guard(blocks: list[loopguard.Block], *, as_product: bool) -> loopguard.LoopGuard:
    roots = {"product_roots": (HERE,), "test_roots": ()} if as_product else {}
    return loopguard.LoopGuard(asyncio.get_running_loop(), 100, blocks.append, **roots)  # type: ignore[arg-type]


async def _blocking_coroutine(seconds: float) -> None:
    time.sleep(seconds)  # noqa: ASYNC251  (blocking the loop is what the guard catches)


async def test_a_blocked_loop_is_reported_with_the_coroutine_that_blocked_it() -> None:
    blocks: list[loopguard.Block] = []
    guard = _guard(blocks, as_product=True)
    guard.start()
    try:
        await _blocking_coroutine(0.4)
        await asyncio.sleep(0.05)  # let the loop run again so the stall is closed
    finally:
        guard.stop()
    detected = [b for b in blocks if b.total_ms is None]
    ended = [b for b in blocks if b.total_ms is not None]
    assert len(detected) == 1 and len(ended) == 1, blocks
    (b,) = detected
    assert b.origin == "product" and b.counts
    assert "_blocking_coroutine" in b.blamed and "time.sleep(seconds)" in b.stack
    assert b.lag_ms > 100 and ended[0].total_ms is not None and ended[0].total_ms >= 350


async def test_a_test_coroutine_blocking_the_loop_is_recorded_but_does_not_count() -> None:
    blocks: list[loopguard.Block] = []
    guard = _guard(blocks, as_product=False)
    guard.start()
    try:
        await _blocking_coroutine(0.3)
        await asyncio.sleep(0.05)
    finally:
        guard.stop()
    assert blocks and all(b.origin == "test" and not b.counts for b in blocks)


async def test_awaiting_and_threads_do_not_block_the_loop() -> None:
    blocks: list[loopguard.Block] = []
    guard = _guard(blocks, as_product=True)
    guard.start()
    try:
        await asyncio.sleep(0.4)
        await asyncio.to_thread(time.sleep, 0.4)
    finally:
        guard.stop()
    assert blocks == []


def test_a_stopped_loop_is_idle_not_blocked() -> None:
    loop = asyncio.new_event_loop()
    blocks: list[loopguard.Block] = []
    guard = loopguard.LoopGuard(loop, 100, blocks.append, product_roots=(HERE,), test_roots=())
    try:
        guard.start()
        loop.run_until_complete(asyncio.sleep(0.01))
        time.sleep(0.4)  # the loop is not running: nothing is late
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        guard.stop()
        loop.close()
    assert blocks == []


def test_a_worker_process_reports_to_the_directory_and_refuses_outside_test(
    tmp_path: Path,
) -> None:
    code = (
        "import asyncio, time\n"
        "from edisc_core import loopguard\n"
        "async def main():\n"
        "    loopguard.install_from_env(asyncio.get_running_loop(), permitted=True, log=print)\n"
        "    await asyncio.sleep(0.05)\n"
        "    time.sleep(0.5)\n"  # unattributed: this coroutine is in neither tree
        "    await asyncio.sleep(0.05)\n"
        "asyncio.run(main())\n"
    )
    env = {**os.environ, loopguard.ENV_MS: "100", loopguard.ENV_DIR: str(tmp_path)}
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)
    seen: set[str] = set()
    (block,) = loopguard.read_blocks(tmp_path, seen)
    assert block.counts and block.total_ms is not None and block.total_ms >= 400
    assert json.loads((tmp_path / next(iter(seen))).read_text())["pid"] == block.pid

    refused = subprocess.run(
        [sys.executable, "-c", code.replace("permitted=True", "permitted=False")],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert refused.returncode != 0 and loopguard.ENV_MS in refused.stderr


def test_classify_walks_to_the_innermost_coroutine_of_ours() -> None:
    frames: dict[str, object] = {}

    async def outer() -> None:
        def sync_leaf() -> None:
            frames["f"] = sys._getframe()

        sync_leaf()

    asyncio.run(outer())
    origin, blamed = loopguard.classify(frames["f"], product_roots=(HERE,), test_roots=())  # type: ignore[arg-type]
    assert origin == "product" and "outer" in blamed
    origin, _ = loopguard.classify(frames["f"], product_roots=(), test_roots=(HERE,))  # type: ignore[arg-type]
    assert origin == "test"
    assert threading.current_thread() is threading.main_thread()


def test_a_coroutine_driven_synchronously_is_the_work_of_its_sync_caller() -> None:
    """The offline verifier is synchronous and drives its async readers itself: called from a
    coroutine, the CALLING coroutine blocks the loop, not the readers."""
    frames: dict[str, object] = {}

    async def reader() -> None:
        frames["f"] = sys._getframe()

    def sync_verifier() -> None:
        with contextlib.suppress(StopIteration):
            reader().send(None)

    async def caller() -> None:
        sync_verifier()

    asyncio.run(caller())
    origin, blamed = loopguard.classify(frames["f"], product_roots=(HERE,), test_roots=())  # type: ignore[arg-type]
    assert origin == "product" and "caller" in blamed and "reader" not in blamed


async def test_garbage_collection_inside_a_stall_is_excused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection holds the GIL, so the watchdog samples AFTER it, wherever the loop is then:
    that lateness is excused, not blamed on the next frame."""

    class EverythingWasGc:
        def within(self, start: float, end: float) -> float:
            return end - start

    monkeypatch.setattr(loopguard, "_GC", EverythingWasGc())
    blocks: list[loopguard.Block] = []
    guard = _guard(blocks, as_product=True)
    guard.start()
    try:
        await _blocking_coroutine(0.3)
        await asyncio.sleep(0.05)
    finally:
        guard.stop()
    assert blocks == []


def test_the_gc_clock_records_collections() -> None:
    clock = loopguard._GcClock()
    try:
        start = time.monotonic()
        gc.collect()
        assert clock.within(start, time.monotonic()) > 0
        assert clock.within(start - 10, start - 5) == 0
    finally:
        gc.callbacks.remove(clock._on)


def test_a_loop_waiting_in_its_selector_is_idle() -> None:
    """Late but inside select(): the host did not schedule the thread, no code of ours ran."""
    selector = selectors.DefaultSelector()
    waiting = threading.Event()

    def wait() -> None:
        waiting.set()
        selector.select(timeout=0.5)

    thread = threading.Thread(target=wait)
    thread.start()
    waiting.wait()
    try:
        deadline = time.monotonic() + 0.4
        origin = None
        while time.monotonic() < deadline and origin != "idle":
            frame = sys._current_frames()[thread.ident or 0]
            origin, _ = loopguard.classify(frame)
    finally:
        thread.join()
        selector.close()
    assert origin == "idle"


def test_a_loop_inside_a_non_blocking_socket_call_is_idle() -> None:
    import asyncio.selector_events as se

    path = se.__file__
    lines = open(path).read().splitlines()  # noqa: SIM115
    lineno = next(i for i, line in enumerate(lines, 1) if "self._sock.recv(" in line)

    class Code:
        co_filename = path
        co_flags = 0
        co_qualname = co_name = "_read_ready__data_received"

    class Frame:
        f_code = Code()
        f_lineno = lineno
        f_back = None

    origin, _ = loopguard.classify(Frame())  # type: ignore[arg-type]
    assert origin == "idle"
    Frame.f_lineno = 1
    assert loopguard.classify(Frame())[0] == "unattributed"  # type: ignore[arg-type]


def test_a_new_connections_scram_handshake_is_library_time() -> None:
    import asyncio.selector_events as se
    import hmac

    def frame(path: str, name: str, back: object) -> object:
        code = type("Code", (), {"co_filename": path, "co_flags": 0, "co_qualname": name,
                                 "co_name": name})()  # fmt: skip
        return type("Frame", (), {"f_code": code, "f_lineno": 1, "f_back": back})()

    received = frame(se.__file__, "_read_ready__data_received", None)
    leaf = frame(hmac.__file__, "_init_hmac", frame(hmac.__file__, "new", received))
    assert loopguard.classify(leaf)[0] == "library"  # type: ignore[arg-type]
    assert loopguard.classify(frame(hmac.__file__, "new", None))[0] == "unattributed"  # type: ignore[arg-type]
