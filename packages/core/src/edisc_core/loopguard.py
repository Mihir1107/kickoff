"""TEST ONLY: detect an event loop blocked longer than a threshold and record the blocked stack.

Activities heartbeat from the event loop (ADR 0015 §23): synchronous CPU-bound or blocking work on the
loop stops the heartbeats, and a loop blocked longer than the heartbeat timeout makes Temporal time out
a LIVE attempt. Tests run on small inputs, so such work is short there and passes by luck; this guard
makes it fail the test instead (`tests/conftest.py`, and every worker process a test spawns).

How: a callback on the loop re-arms itself every ``tick`` seconds. A watchdog thread notices when it is
late by more than the threshold, samples the loop thread's stack while it is still blocked, and
reports a `Block`. The block is attributed to the innermost COROUTINE frame on that stack (the async
function that made the blocking synchronous call): product code (`packages/`, `apps/`, `workers/`
sources) or anything unattributable counts; a test's own coroutine (test setup that builds data
synchronously), a loop sampled inside its selector (waiting for I/O on a host that did not
schedule it) and asyncpg's SCRAM handshake for a new connection are recorded but do not count. A
stall also counts only if the loop's thread was on the CPU for at least `BUSY_SHARE` of it (its
`time.thread_time()` across the stall): CPU-bound work on the loop burns that time, a process the OS
did not schedule does not. Blocking system calls on the loop burn none either: ruff's ASYNC rules
find those statically, and the `OFF_LOOP` check in `tests/conftest.py` is not affected.

Enabled only through `EDISC_TEST_LOOP_BLOCK_MS` (pytest sets it for the processes it spawns; a worker
refuses it unless EDISC_ENV is test or ci). Reports go to `EDISC_TEST_LOOP_BLOCK_DIR` as one JSON file
per block, so the test that spawned the worker can fail on them.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import linecache
import os
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Literal

ENV_MS = "EDISC_TEST_LOOP_BLOCK_MS"
ENV_DIR = "EDISC_TEST_LOOP_BLOCK_DIR"

Origin = Literal["product", "test", "unattributed", "idle", "library"]
# a stall counts only if the loop's thread was on the CPU for at least this share of it: CPU-bound
# work keeps it near 1 (still above 0.25 with every core contended), a descheduled process near 0
BUSY_SHARE = 0.25

_REPO = Path(__file__).resolve().parents[4]
PRODUCT_ROOTS: tuple[str, ...] = tuple(
    str(_REPO / d) + os.sep for d in ("packages", "apps", "workers")
)
TEST_ROOTS: tuple[str, ...] = (str(_REPO / "tests") + os.sep, str(_REPO / "scripts") + os.sep)
_ASYNC = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR


@dataclass(frozen=True)
class Block:
    """One stall of the loop. ``lag_ms`` is how late the loop was when its stack was sampled;
    ``total_ms`` the whole stall once the loop ran again (None while it is still blocked)."""

    id: str
    pid: int
    lag_ms: float
    origin: Origin
    blamed: str  # innermost coroutine frame: "path:line in function"
    task: str | None
    stack: str
    total_ms: float | None = None
    gc_ms: float = 0.0  # garbage collection inside the stall (excused, not counted in the lag)
    cpu_ms: float | None = None  # CPU time of the loop's thread over the whole stall (at its end)

    @property
    def busy(self) -> bool:
        """Did the loop's thread RUN for the stall? CPU-bound work on the loop burns its thread's
        CPU time; a loop whose process the OS did not schedule (a loaded or swapping host) burns
        none. Unknown while the stall lasts: then it is assumed busy."""
        if self.total_ms is None or self.cpu_ms is None:
            return True
        return (self.cpu_ms - self.gc_ms) >= BUSY_SHARE * (self.total_ms - self.gc_ms)

    @property
    def counts(self) -> bool:
        return self.origin in ("product", "unattributed") and self.busy

    def describe(self) -> str:
        total = f"{self.total_ms:.0f} ms" if self.total_ms is not None else "still blocked"
        cpu = (
            f", {self.cpu_ms:.0f} ms of CPU on the loop's thread" if self.cpu_ms is not None else ""
        )
        return (
            f"event loop blocked (pid {self.pid}, {self.origin}, lag {self.lag_ms:.0f} ms when "
            f"sampled ({self.gc_ms:.0f} ms of it garbage collection), total {total}{cpu}) in "
            f"{self.blamed}; task {self.task}\n{self.stack}"
        )


def _ours(
    frame: FrameType, product_roots: Sequence[str], test_roots: Sequence[str]
) -> tuple[Origin, str] | None:
    path = frame.f_code.co_filename
    where = f"{path}:{frame.f_lineno} in {frame.f_code.co_qualname}"
    if path.startswith(tuple(test_roots)):
        return "test", where
    if path.startswith(tuple(product_roots)):
        return "product", where
    return None


def _awaited_frames(task: object) -> list[FrameType]:
    """The coroutine frames of a task, outermost first, following what each one awaits."""
    frames: list[FrameType] = []
    coro = task.get_coro() if isinstance(task, asyncio.Task) else None
    while coro is not None and len(frames) < 200:
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
        frame = frame or getattr(coro, "gi_frame", None)
        if frame is not None:
            frames.append(frame)
        coro = (
            getattr(coro, "cr_await", None)
            or getattr(coro, "ag_await", None)
            or getattr(coro, "gi_yieldfrom", None)
        )
    return frames


def _waiting(frame: FrameType) -> bool:
    path = frame.f_code.co_filename
    if path.endswith(f"{os.sep}selectors.py"):
        return True
    if path.endswith(f"asyncio{os.sep}selector_events.py"):
        line = linecache.getline(path, frame.f_lineno)
        return "self._sock.recv" in line or "self._sock.send" in line
    return False


def classify(
    frame: FrameType | None,
    *,
    task: object = None,
    product_roots: Sequence[str] = PRODUCT_ROOTS,
    test_roots: Sequence[str] = TEST_ROOTS,
) -> tuple[Origin, str]:
    """Who blocked the loop. The loop runs a task's coroutine, which awaits coroutines (and async
    generators), until one of them makes a SYNCHRONOUS call: that coroutine is to blame. So the
    chain is followed from the outermost coroutine frame inward while frames are coroutines or
    generators, and the innermost frame of ours in it decides (product or test). A coroutine driven
    synchronously from a sync function (the offline verifier runs its async readers that way) is
    the sync caller's work, not the loop's: the chain stops before it.
    When the thread's stack has no coroutine at all (SQLAlchemy runs its sync core in a greenlet,
    whose frames do not link back to the awaiting coroutine), the running task's await chain
    decides. A loop sampled inside its selector, or inside a NON-BLOCKING socket call of the
    transport, is waiting for I/O, not blocked: it was late because the OS did not schedule the
    process in time (a loaded or swapping host)."""
    if frame is not None and _waiting(frame):
        return "idle", f"{frame.f_code.co_filename}:{frame.f_lineno} (waiting for I/O)"
    stack: list[FrameType] = []
    while frame is not None:
        stack.append(frame)
        frame = frame.f_back
    stack.reverse()  # outermost first
    first = next((i for i, f in enumerate(stack) if f.f_code.co_flags & _ASYNC), None)
    chain: list[FrameType] = []
    for f in stack[first:] if first is not None else ():
        if not f.f_code.co_flags & (_ASYNC | inspect.CO_GENERATOR):
            break
        chain.append(f)
    if not chain:
        chain = _awaited_frames(task)
    for f in reversed(chain):
        if f.f_code.co_flags & _ASYNC:
            found = _ours(f, product_roots, test_roots)
            if found is not None:
                return found
    if _connection_auth(stack):
        return "library", "asyncpg SCRAM-SHA-256 authentication of a new connection (PBKDF2)"
    return "unattributed", "no coroutine frame of ours on the stack"


def _connection_auth(stack: Sequence[FrameType]) -> bool:
    """A transport callback (`data_received`) computing HMACs: asyncpg's SCRAM handshake, 4,096
    PBKDF2 iterations per NEW connection, run by the driver on the loop. A cost of opening a pooled
    connection, not code of ours (ADR 0015 §24; BACKLOG: pre-warm pools)."""
    names = [(f.f_code.co_filename, f.f_code.co_name) for f in stack]
    received = any(n == "_read_ready__data_received" for _, n in names)
    return received and any(p.endswith(f"{os.sep}hmac.py") for p, _ in names[-6:])


class _GcClock:
    """When this process's garbage collections ran (``gc.callbacks``). A collection holds the GIL,
    so the watchdog only runs after it, and the loop is late by the pause: that time is excused,
    it is not code on the loop (the stack sampled afterwards would blame whatever ran next)."""

    def __init__(self) -> None:
        self.started: float | None = None
        self.done: deque[tuple[float, float]] = deque(maxlen=64)
        gc.callbacks.append(self._on)

    def _on(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self.started = time.monotonic()
        elif self.started is not None:
            self.done.append((self.started, time.monotonic()))
            self.started = None

    def within(self, start: float, end: float) -> float:
        spans = list(self.done)
        if self.started is not None:
            spans.append((self.started, end))
        return sum(max(0.0, min(b, end) - max(a, start)) for a, b in spans)


_GC: _GcClock | None = None


@dataclass
class LoopGuard:
    """Watch one loop. ``on_block`` is called from the watchdog thread when a stall is detected and
    again (with ``total_ms``) when it ends; it must be quick and thread-safe."""

    loop: asyncio.AbstractEventLoop
    threshold_ms: float
    on_block: Callable[[Block], None]
    product_roots: Sequence[str] = PRODUCT_ROOTS
    test_roots: Sequence[str] = TEST_ROOTS
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _due: float = field(default=0.0, init=False)
    _open: Block | None = field(default=None, init=False)
    _thread_id: int | None = field(default=None, init=False)
    _handle: asyncio.TimerHandle | None = field(default=None, init=False)
    _watchdog: threading.Thread | None = field(default=None, init=False)
    _cpu: float = field(default=0.0, init=False)  # the loop thread's CPU time at its last beat

    @property
    def tick(self) -> float:
        return max(self.threshold_ms / 4000, 0.005)

    def start(self) -> None:
        """Call from the loop's thread (inside the loop or before it runs)."""
        global _GC
        _GC = _GC or _GcClock()
        self._thread_id = threading.get_ident()
        self._cpu = time.thread_time()
        with self._lock:
            self._due = time.monotonic() + self.tick
        self._handle = self.loop.call_later(self.tick, self._beat)
        self._watchdog = threading.Thread(target=self._watch, name="loop-guard", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        if self._handle is not None:
            self._handle.cancel()
        if self._watchdog is not None:
            self._watchdog.join(timeout=1)

    def _beat(self) -> None:
        now, cpu = time.monotonic(), time.thread_time()  # on the loop's own thread
        with self._lock:
            ended, self._open = self._open, None
            late = now - self._due
            self._due = now + self.tick
            used, self._cpu = cpu - self._cpu, cpu
        if ended is not None:
            self.on_block(replace(ended, total_ms=late * 1000, cpu_ms=used * 1000))
        if not self._stop.is_set():
            self._handle = self.loop.call_later(self.tick, self._beat)

    def _watch(self) -> None:
        poll = max(self.threshold_ms / 10000, 0.002)
        while not self._stop.wait(poll):
            now = time.monotonic()
            if not self.loop.is_running():  # a stopped loop is idle, not blocked
                with self._lock:
                    ended, self._open = self._open, None
                    late, self._due = now - self._due, now + self.tick
                if ended is not None:  # it stopped right after the stall: ends here (CPU unknown)
                    self.on_block(replace(ended, total_ms=late * 1000))
                continue
            with self._lock:
                late = now - self._due
                if self._open is not None or late * 1000 <= self.threshold_ms:
                    continue
                paused = _GC.within(self._due, now) if _GC is not None else 0.0
                if (late - paused) * 1000 <= self.threshold_ms:
                    continue  # late because of garbage collection, not because of code
                block = self._sample(late, paused)
                self._open = block
            self.on_block(block)

    def _sample(self, late: float, paused: float) -> Block:
        frame = sys._current_frames().get(self._thread_id or -1)
        task = getattr(asyncio.tasks, "_current_tasks", {}).get(self.loop)
        origin, blamed = classify(
            frame, task=task, product_roots=self.product_roots, test_roots=self.test_roots
        )
        stack = "".join(traceback.format_stack(frame)) if frame is not None else "(no frame)"
        return Block(
            id=uuid.uuid4().hex, pid=os.getpid(), lag_ms=late * 1000, origin=origin,
            blamed=blamed, task=None if task is None else repr(task)[:300], stack=stack,
            gc_ms=paused * 1000,
        )  # fmt: skip


def write_block(directory: Path, block: Block) -> None:
    """One file per block, rewritten (atomically) when the stall ends."""
    target = directory / f"{block.pid}-{block.id}.json"
    tmp = directory / f".{block.pid}-{block.id}-{uuid.uuid4().hex}.tmp"  # two writers: own temp
    tmp.write_text(json.dumps(asdict(block)))
    tmp.replace(target)


def read_blocks(directory: Path, seen: set[str], *, unfinished: bool = True) -> list[Block]:
    """Blocks reported by other processes since ``seen`` (updated). A block whose stall has not
    ended yet is returned (and marked seen) only if ``unfinished``; otherwise it is left for a later
    read, when its end (and so whether the loop's thread was busy) is known."""
    found = []
    for path in sorted(directory.glob("*.json")):
        if path.name in seen:
            continue
        block = Block(**json.loads(path.read_text()))
        if block.total_ms is None and not unfinished:
            continue
        seen.add(path.name)
        found.append(block)
    return found


def install_from_env(
    loop: asyncio.AbstractEventLoop, *, permitted: bool, log: Callable[[str], None]
) -> LoopGuard | None:
    """In a process a test spawned: watch ``loop`` if `EDISC_TEST_LOOP_BLOCK_MS` is set. Refused
    (SystemExit) unless ``permitted`` (EDISC_ENV is test or ci)."""
    raw = os.environ.get(ENV_MS)
    if not raw:
        return None
    if not permitted:
        raise SystemExit(f"{ENV_MS} is only permitted when EDISC_ENV is test or ci")
    directory = Path(os.environ[ENV_DIR]) if os.environ.get(ENV_DIR) else None

    def report(block: Block) -> None:
        if block.total_ms is None:
            log(block.describe())
        if directory is not None and block.origin in ("product", "unattributed"):
            write_block(directory, block)  # at detection, then again with its end

    guard = LoopGuard(loop, float(raw), report)
    guard.start()
    return guard
