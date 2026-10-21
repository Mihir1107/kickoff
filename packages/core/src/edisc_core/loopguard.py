"""TEST ONLY: detect an event loop blocked longer than a threshold and record the blocked stack.

Activities heartbeat from the event loop (ADR 0015 §23): synchronous CPU-bound or blocking work on the
loop stops the heartbeats, and a loop blocked longer than the heartbeat timeout makes Temporal time out
a LIVE attempt. Tests run on small inputs, so such work is short there and passes by luck; this guard
makes it fail the test instead (`tests/conftest.py`, and every worker process a test spawns).

How: a callback on the loop re-arms itself every ``tick`` seconds. A watchdog thread notices when it is
late by more than the threshold, samples the loop thread's stack while it is still blocked, and
reports a `Block`. From the moment the loop is overdue until it runs again, the watchdog also
samples the loop thread's stack every `SAMPLE_INTERVAL` (10 ms) and the block carries the
aggregate (`Block.profile`: the hottest innermost frames, functions and frames of ours): one
sample is one instant, and twice it pointed at innocent code that merely ran next (a BEGIN's
send, ADR 0015 §24.13; a Redis send, §24.24). The block is attributed to the innermost COROUTINE frame on that stack (the async
function that made the blocking synchronous call): product code (`packages/`, `apps/`, `workers/`
sources) or anything unattributable counts; a test's own coroutine (test setup that builds data
synchronously) and asyncpg's SCRAM handshake for a new connection are recorded but do not count
(new connections are churn, failed by `loopblock.report_product`, ADR 0015 §24.13); a loop
sampled inside its selector or a non-blocking socket call counts only if its thread was busy for
the stall (measured, not assumed): a sample is one instant of a stall. A
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
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Any, Literal

ENV_MS = "EDISC_TEST_LOOP_BLOCK_MS"
ENV_DIR = "EDISC_TEST_LOOP_BLOCK_DIR"

Origin = Literal["product", "test", "unattributed", "idle", "library"]
# while the loop is overdue the watchdog samples its thread this often (the profile of a stall)
SAMPLE_INTERVAL = 0.010
MAX_SAMPLES = 6_000  # one minute of a stall; beyond, samples are counted, not kept
PROFILE_TOP = 10  # rows per table in the profile
# a stall counts only if the loop's thread was on the CPU for at least this share of it: CPU-bound
# work keeps it near 1 (still above 0.25 with every core contended), a descheduled process near 0
BUSY_SHARE = 0.25

_REPO = Path(__file__).resolve().parents[4]
PRODUCT_ROOTS: tuple[str, ...] = tuple(
    str(_REPO / d) + os.sep for d in ("packages", "apps", "workers")
)
TEST_ROOTS: tuple[str, ...] = (str(_REPO / "tests") + os.sep, str(_REPO / "scripts") + os.sep)
_ASYNC = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
_LOOPBLOCK = str(Path(__file__).with_name("loopblock.py"))


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
    profile: dict[str, Any] | None = None  # `profile()` of the samples taken during the stall

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
        """An ``idle`` sample (the loop in its selector or a non-blocking socket call) is excused
        only when the loop's thread did NOT run for the stall: a sample is one instant, and a loop
        that burnt the CPU for the whole stall was working, wherever the sample landed (a burst of
        new connections' handshakes sampled on a BEGIN's send, ADR 0015 §24.13)."""
        if self.origin == "idle":
            return self.cpu_ms is not None and self.busy  # measured, never assumed
        return self.origin in ("product", "unattributed") and self.busy

    def describe(self) -> str:
        total = f"{self.total_ms:.0f} ms" if self.total_ms is not None else "still blocked"
        cpu = (
            f", {self.cpu_ms:.0f} ms of CPU on the loop's thread" if self.cpu_ms is not None else ""
        )
        return (
            f"event loop blocked (pid {self.pid}, {self.origin}, lag {self.lag_ms:.0f} ms when "
            f"sampled ({self.gc_ms:.0f} ms of it garbage collection), total {total}{cpu}) in "
            f"{self.blamed}; task {self.task}\n{self.stack}{describe_profile(self.profile)}"
        )


# (monotonic time, waiting for I/O, frames outermost first as (path, line, qualified name))
Sample = tuple[float, bool, tuple[tuple[str, int, str], ...]]


def _short(path: str) -> str:
    if path.startswith(str(_REPO) + os.sep):
        return path[len(str(_REPO)) + 1 :]
    _, sep, rest = path.rpartition(f"site-packages{os.sep}")
    if sep:
        return rest
    _, sep, rest = path.rpartition(f"{os.sep}lib{os.sep}python")
    return f"<stdlib>{rest[rest.find(os.sep) :]}" if sep else path


def take_sample(frame: FrameType | None) -> Sample:
    """The loop thread's stack at one instant: cheap (no source lines read), for aggregation."""
    frames: list[tuple[str, int, str]] = []
    waiting = frame is not None and _waiting(_unwrapped(frame))
    while frame is not None and len(frames) < 200:
        frames.append((frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_qualname))
        frame = frame.f_back
    frames.reverse()
    return time.monotonic(), waiting, tuple(frames)


def profile(
    samples: Sequence[Sample], *, dropped: int = 0,
    product_roots: Sequence[str] = PRODUCT_ROOTS, test_roots: Sequence[str] = TEST_ROOTS,
) -> dict[str, Any]:  # fmt: skip
    """Aggregate the samples of one stall: where the loop's thread WAS, sample after sample.
    ``leaf``: the innermost Python frame (self time, by line; a C call such as JSON encoding is
    charged to the Python line that called it); ``functions``: every function on the stack, once
    per sample (inclusive time), from the deepest frame that every running sample shares inward
    (the frames the whole stall ran under, the event loop's and the test runner's, would
    otherwise fill the table at 100 %); equal counts deepest first; ``ours``: the innermost frame
    under the product or test roots. ``waiting``: samples taken in the selector or a non-blocking socket call (the loop was not
    running code then). ``interval_ms``: the measured mean spacing of the samples (the watchdog
    needs the GIL, so a loop thread busy in Python stretches the 10 ms). Counts, most first, at
    most `PROFILE_TOP` rows each (`functions`: twice that)."""
    roots = (*product_roots, *test_roots)
    leaf: Counter[str] = Counter()
    functions: Counter[str] = Counter()
    depth: dict[str, int] = {}
    ours: Counter[str] = Counter()
    waiting = 0
    running = [frames for _, idle, frames in samples if frames and not idle]
    shared = 0  # how many outermost frames every running sample has in common
    if running:
        first = running[0]
        while shared < len(first) and all(
            len(f) > shared and f[shared] == first[shared] for f in running
        ):
            shared += 1
    for _, idle, frames in samples:
        waiting += idle
        if not frames:
            continue
        path, line, name = frames[-1]
        leaf[f"{_short(path)}:{line} in {name}"] += 1
        inner = frames[max(shared - 1, 0) :] if not idle else frames[-1:]
        names = {f"{_short(p)} in {n}": i for i, (p, _, n) in enumerate(inner)}
        functions.update(names.keys())
        for name, i in names.items():
            depth[name] = max(depth.get(name, 0), i)
        mine = next((f for f in reversed(frames) if f[0].startswith(roots)), None)
        if mine is not None:
            ours[f"{_short(mine[0])}:{mine[1]} in {mine[2]}"] += 1
    span = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0
    ranked = sorted(functions.items(), key=lambda kv: (-kv[1], -depth[kv[0]], kv[0]))
    return {
        "interval_ms": round(1000 * span / (len(samples) - 1), 1) if span else None,
        "samples": len(samples), "dropped": dropped, "waiting": waiting,
        "leaf": leaf.most_common(PROFILE_TOP), "functions": ranked[: 2 * PROFILE_TOP],
        "ours": ours.most_common(PROFILE_TOP),
    }  # fmt: skip


def describe_profile(p: dict[str, Any] | None) -> str:
    if not p or not p.get("samples"):
        return "\n(no profile: the stall ended before a sample was taken)\n" if p else ""
    n = int(p["samples"])
    out = [
        f"\nprofile of the stall: {n} samples (one every {p['interval_ms'] or 0:.0f} ms) while "
        f"the loop was overdue ({p['waiting']} waiting for I/O"
        + (f", {p['dropped']} more not kept" if p.get("dropped") else "")
        + ")"
    ]
    for title, key in (
        ("hottest innermost frames (self)", "leaf"),
        ("hottest functions (on the stack)", "functions"),
        ("innermost frames of ours", "ours"),
    ):
        out.append(f"  {title}:")
        out += [f"    {c:5d} {100 * c / n:5.1f}%  {where}" for where, c in p[key]] or ["    (none)"]
    return "\n".join(out) + "\n"


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


def _unwrapped(frame: FrameType) -> FrameType:
    if frame.f_code.co_filename == _LOOPBLOCK and frame.f_back is not None:
        return frame.f_back  # the blocking-I/O guard's wrapper around the transport's call
    return frame


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
    if frame is not None and _waiting(inner := _unwrapped(frame)):
        return "idle", f"{inner.f_code.co_filename}:{inner.f_lineno} (waiting for I/O)"
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
    if _driver_read_callback(stack):
        return "library", "a driver's wire protocol in a transport read callback (ADR 0015 §24.6)"
    return "unattributed", "no coroutine frame of ours on the stack"


def _driver_read_callback(stack: Sequence[FrameType]) -> bool:
    """The loop running a driver's wire protocol synchronously inside a transport read callback
    (`data_received`), between tasks and with no coroutine of ours responsible: asyncpg's SCRAM
    handshake (4,096 PBKDF2 iterations per NEW connection) and the sends it makes from
    data_received, driver result parsing. A cost of the driver on the loop, not code of ours
    (ADR 0015 §24; BACKLOG: pre-warm pools). The earlier check matched only SCRAM's PBKDF2 frame
    (`hmac.py`), so a sample landing in the handshake's own send counted as unattributed and failed
    a test flakily (a burst of new connections stalled ~300 ms there, ADR 0015 §24.6). asyncpg's
    protocol is Cython, so no frame of ours sits between `data_received` and that send: the read
    callback itself is the reliable signal."""
    return any(f.f_code.co_name == "_read_ready__data_received" for f in stack)


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
    _samples: list[Sample] = field(default_factory=list, init=False)  # since the loop was overdue
    _dropped: int = field(default=0, init=False)

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
            samples, dropped = self._take_samples()
        if ended is not None:
            self.on_block(replace(ended, total_ms=late * 1000, cpu_ms=used * 1000,
                                  profile=self._profile(samples, dropped)))  # fmt: skip
        if not self._stop.is_set():
            self._handle = self.loop.call_later(self.tick, self._beat)

    def _take_samples(self) -> tuple[list[Sample], int]:
        """The stall's samples so far, and a fresh start (call under the lock)."""
        taken = self._samples, self._dropped
        self._samples, self._dropped = [], 0
        return taken

    def _profile(self, samples: Sequence[Sample], dropped: int) -> dict[str, Any]:
        return profile(samples, dropped=dropped, product_roots=self.product_roots,
                       test_roots=self.test_roots)  # fmt: skip

    def _watch(self) -> None:
        poll = max(self.threshold_ms / 10000, 0.002)
        wait = poll
        while not self._stop.wait(wait):
            now = time.monotonic()
            wait = poll
            if not self.loop.is_running():  # a stopped loop is idle, not blocked
                with self._lock:
                    ended, self._open = self._open, None
                    late, self._due = now - self._due, now + self.tick
                    samples, dropped = self._take_samples()
                if ended is not None:  # it stopped right after the stall: ends here (CPU unknown)
                    self.on_block(replace(ended, total_ms=late * 1000,
                                          profile=self._profile(samples, dropped)))  # fmt: skip
                continue
            with self._lock:
                late = now - self._due
                if late > 0:  # overdue: whatever the loop's thread runs now is the stall
                    wait = min(poll, SAMPLE_INTERVAL)
                    if len(self._samples) < MAX_SAMPLES:
                        frame = sys._current_frames().get(self._thread_id or -1)
                        self._samples.append(take_sample(frame))
                    else:
                        self._dropped += 1
                if self._open is not None or late * 1000 <= self.threshold_ms:
                    continue
                paused = _GC.within(self._due, now) if _GC is not None else 0.0
                if (late - paused) * 1000 <= self.threshold_ms:
                    continue  # late because of garbage collection, not because of code
                block = replace(
                    self._sample(late, paused), profile=self._profile(self._samples, self._dropped)
                )  # so far
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
        if directory is not None and block.origin in ("product", "unattributed", "idle"):
            write_block(directory, block)  # at detection, then again with its end

    guard = LoopGuard(loop, float(raw), report)
    guard.start()
    return guard
