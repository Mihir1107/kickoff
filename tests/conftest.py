"""Session-wide test guard: a test fails when the event loop was blocked longer than
`EDISC_TEST_LOOP_BLOCK_MS` (`edisc_core.loopguard`), in this process or in a worker it spawned.

Why 250 ms (the default): the tightest heartbeat timeout in the suite is 3 s (the real-SIGKILL tests),
with a ticker every second (`_ticking`: timeout / 3), so a stall of about 2 s already risks a timed-out
LIVE attempt; CI hosts run 2-5x slower than this laptop under load (ADR 0015 §23: a 1.5 s slice here
was "several times that" in CI), so 250 ms here is 0.5-1.25 s there, still inside the budget, and a
test that blocks 250 ms on test-sized data blocks for minutes on production-sized data (a 60 s
heartbeat timeout). The noise floor measured over the whole suite (asyncio and driver work between
awaits, first-use imports) is recorded in ADR 0015 §24; it stays well below the threshold.

`EDISC_TEST_LOOP_BLOCK_LOG=<path>`: also append every block (including test-origin ones) as JSON
lines, for measuring. `EDISC_TEST_LOOP_BLOCK_MODE=report`: record only, never fail (measurement runs).

Timing alone only catches CPU work that is slow on test-sized inputs. So every pure function that the
product runs in a worker thread (`asyncio.to_thread`) is also listed in `OFF_LOOP`: for the whole
session it is wrapped, and a call made on a thread that is running an event loop fails the test,
however fast it was (deterministic; ADR 0015 §24). A new `to_thread` site adds its function here and
a mutation entry (`scripts/mutation/catalog.py`, round `s24-loop`).

Timing is also blind to BLOCKING I/O on the loop: it burns no CPU, so the CPU-share rule (§24.7) drops
it. `edisc_core.loopblock` closes that: installed for the session (and in any worker a test spawns), it
fails a test when product code on the loop thread makes a blocking socket call, a `time.sleep`, a
synchronous database driver call (a blocking socket) or a synchronous file read/write at or above its
threshold. Its breaks are in the `s24-loop` mutation round.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import os
import shutil
import tempfile
import threading
import traceback
from collections.abc import AsyncIterator, Callable, Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest

from edisc_core import loopblock, loopguard

DEFAULT_BLOCK_MS = 250

# "module:attribute" as the CALL SITE looks it up (so tests that call the function directly from a
# coroutine, e.g. normalizer unit tests through `edisc_normalizer.slack`, are unaffected)
OFF_LOOP = (
    "edisc_worker.render_store:_render_slice_blocking",
    "edisc_renderers.rsmf.reconcile:Reconciler.add_slice",
    "edisc_worker.pipeline:normalize_messages_page",
    "edisc_worker.pipeline:normalize_directory_page",
    "edisc_worker.pipeline:message_page_subjects",
    "edisc_worker.pipeline:directory_page_subjects",
    "edisc_worker.pipeline:messages_fragment_hash",
    "edisc_worker.pipeline:access_restored",
    "edisc_worker.pipeline:finalize_unit",
    "edisc_worker.pipeline:file_refs",
    "edisc_normalizer.store:_encode_derivations",
    "edisc_normalizer.store:_order_items",
    "edisc_normalizer.store:_assign_ids",
    "edisc_normalizer.store:_item_rows",
    "edisc_worker.pipeline:_by_key",
    "edisc_worker.render_loader:_build_messages",
    "edisc_custody.log:_verify_page",
    "edisc_worker.render_loader:_check_page",
    "edisc_worker.render_loader:_check_derivations",
    "edisc_worker.render_loader:_index_rows",
    "edisc_connector_dummy.connector:DummyConnector.plan",
    "edisc_connector_dummy.dialects.slack:history_page",
    "edisc_connector_dummy.dialects.slack:replies_page",
    "edisc_connector_dummy.dialects.slack:users_page",
    "edisc_connector_dummy.dialects.slack:conversations_page",
)


class LoopBlockedError(AssertionError):
    pass


@dataclass
class _State:
    threshold_ms: float
    directory: Path
    owns_directory: bool
    log_path: Path | None
    fail: bool
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, loopguard.Block] = field(default_factory=dict)  # by id: latest report
    seen: set[str] = field(default_factory=set)
    viol_seen: set[str] = field(default_factory=set)  # worker violation files already read
    on_loop: list[str] = field(default_factory=list)  # OFF_LOOP functions called on a loop thread
    violations: list[str] = field(default_factory=list)  # blocking I/O on a loop thread (loopblock)
    loopblock: loopblock._Config | None = None  # the installed blocking-I/O guard, to uninstall

    def _log(self, where: str, blocks: list[loopguard.Block]) -> None:
        if self.log_path is None or not blocks:
            return
        test = os.environ.get("PYTEST_CURRENT_TEST")
        with self.lock, self.log_path.open("a") as fh:
            for b in blocks:
                fh.write(json.dumps({"where": where, "test": test, **asdict(b)}) + "\n")

    def record(self, block: loopguard.Block) -> None:
        """Called at a stall's detection and again at its end (with its CPU time)."""
        if block.total_ms is not None:
            self._log("pytest", [block])
        if block.origin in ("product", "unattributed"):
            with self.lock:
                self.pending[block.id] = block

    def record_violation(self, v: loopblock.Violation) -> None:
        """Called from a wrapper on the loop thread (in this process)."""
        with self.lock:
            self.violations.append(v.describe())

    def take_on_loop(self) -> list[str]:
        with self.lock:
            found, self.on_loop = self.on_loop, []
        return found

    def take_violations(self) -> list[str]:
        """Blocking-I/O violations in this process and in any worker a test spawned."""
        with self.lock:
            found, self.violations = self.violations, []
        found += [v.describe() for v in loopblock.read_violations(self.directory, self.viol_seen)]
        return found

    def take(self, phase: str) -> list[loopguard.Block]:
        """The blocks that count, decided at their end (a stall still open counts). Worker blocks
        whose end is not known yet wait for a later phase, except at teardown (the worker is gone)."""
        with self.lock:
            found, self.pending = list(self.pending.values()), {}
        others = loopguard.read_blocks(self.directory, self.seen, unfinished=phase == "teardown")
        self._log("worker", others)
        return [b for b in (*found, *others) if b.counts]


_KEY = pytest.StashKey[_State]()


def pytest_configure(config: pytest.Config) -> None:
    ms = os.environ.setdefault(loopguard.ENV_MS, str(DEFAULT_BLOCK_MS))
    owns = not os.environ.get(loopguard.ENV_DIR)
    if owns:  # spawned workers inherit the environment and report here
        os.environ[loopguard.ENV_DIR] = tempfile.mkdtemp(prefix="edisc-loop-blocks-")
    log = os.environ.get("EDISC_TEST_LOOP_BLOCK_LOG")
    config.stash[_KEY] = _State(
        threshold_ms=float(ms), directory=Path(os.environ[loopguard.ENV_DIR]),
        owns_directory=owns, log_path=Path(log) if log else None,
        fail=os.environ.get("EDISC_TEST_LOOP_BLOCK_MODE", "fail") != "report",
    )  # fmt: skip
    state = config.stash[_KEY]
    _wrap_off_loop(state)
    state.loopblock = loopblock.install(state.record_violation)


def _off_loop(name: str, fn: Callable[..., Any], state: _State) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if asyncio.events._get_running_loop() is not None:
            where = "".join(traceback.format_stack(limit=12)[:-1])
            with state.lock:
                state.on_loop.append(f"{name} ran ON the event loop thread:\n{where}")
        return fn(*args, **kwargs)

    return wrapper


def _wrap_off_loop(state: _State) -> None:
    for target in OFF_LOOP:
        module_name, _, attr = target.partition(":")
        owner: Any = importlib.import_module(module_name)
        *path, leaf = attr.split(".")
        for part in path:
            owner = getattr(owner, part)
        setattr(owner, leaf, _off_loop(target, getattr(owner, leaf), state))


def pytest_unconfigure(config: pytest.Config) -> None:
    state = config.stash.get(_KEY, None)
    if state is None:
        return
    if state.loopblock is not None:
        loopblock.uninstall(state.loopblock)
    if state.owns_directory:
        shutil.rmtree(state.directory, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
async def _event_loop_guard(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    state = request.config.stash[_KEY]
    guard = loopguard.LoopGuard(asyncio.get_running_loop(), state.threshold_ms, state.record)
    guard.start()
    yield
    guard.stop()


def _check(item: pytest.Item, phase: str) -> Generator[None, Any, None]:
    outcome = yield
    state = item.config.stash[_KEY]
    blocks, on_loop, violations = state.take(phase), state.take_on_loop(), state.take_violations()
    if not blocks and not on_loop and not violations:
        return
    text = "\n\n".join([*on_loop, *violations, *(b.describe() for b in blocks)])
    msg = (
        f"{len(on_loop)} call(s) of thread-only functions on the event loop, {len(violations)} "
        f"blocking I/O call(s) on the event loop, {len(blocks)} event loop block(s) longer than "
        f"{loopguard.ENV_MS}={state.threshold_ms:.0f} during {phase}: synchronous CPU-bound or "
        f"blocking work ran on the event loop; move it to a thread (CLAUDE.md, ADR 0015 §23, §24)"
        f"\n\n{text}"
    )
    if state.fail and outcome.excinfo is None:
        outcome.force_exception(LoopBlockedError(msg))
    else:
        item.add_report_section(phase, "loop blocks", msg)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, Any, None]:
    yield from _check(item, "setup")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, None]:
    yield from _check(item, "call")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, Any, None]:
    yield from _check(item, "teardown")
