"""TEST ONLY: fail a test when the event-loop thread makes a BLOCKING I/O call.

The timing guard (`edisc_core.loopguard`) counts a stall only when the loop's thread was on the CPU
for at least `BUSY_SHARE` of it (ADR 0015 §24.7): CPU-bound work on the loop burns that time. Blocking
I/O burns almost none of it -- the thread is parked in a system call -- so a blocking socket read, a
`time.sleep`, a synchronous database driver call or a synchronous file read/write on the loop slips
past the timing guard entirely, even though it stops the heartbeats exactly as a timed-out LIVE attempt
would (ADR 0015 §23). ruff's ASYNC rules catch the ones that are statically obvious; this guard is the
deterministic runtime net for the rest, the counterpart of `OFF_LOOP` in `tests/conftest.py`.

How: the four stdlib entry points below are wrapped once, for the whole process. A wrapper acts only
when it runs on a thread that is driving an event loop (`asyncio.events._get_running_loop()` is not
None) -- so the same call made in a worker thread (`asyncio.to_thread`) or in a synchronous CLI (the
offline verifier) is ignored -- and only when the call actually blocks:

- **blocking socket call** -- a method of `socket.socket` / `ssl.SSLSocket` whose socket is NOT in
  non-blocking mode (`gettimeout() != 0`). asyncio's own sockets, and every async driver built on them
  (asyncpg, aiobotocore/aiohttp, redis.asyncio), are non-blocking (`gettimeout() == 0`) and never fire;
  a synchronous driver's socket blocks (`gettimeout()` is None or a positive timeout) and does. This is
  also how a **synchronous database driver call** is caught: it blocks on its socket.
- **`time.sleep`** with a positive duration.
- **`os.read` / `os.write` / `os.pread` / `os.pwrite` / `os.readv` / `os.writev`** moving at least
  `threshold_bytes`, and a **buffered file read/write** of at least `threshold_bytes` through a handle
  that product code opened while on the loop (`builtins.open` / `io.open`). Why 64 KiB: CPython's
  buffered-IO block is 8 KiB and `shutil` copies in 64 KiB, the usual pipe/socket buffer; a single
  synchronous transfer of 64 KiB or more is therefore a deliberate bulk move of payload bytes on the
  loop, not the few-KiB reads the product does at import (the RSMF schema, the dev-IdP PEM, a local-KMS
  key file) or asyncio's one-byte self-pipe wakeups. It is independent of host speed, so it is
  deterministic where the timing guard is not, and it sits below the 1 MiB chunk the package/verifier
  readers stream in, so a bulk read that ever landed on the loop is caught on its first chunk.

Like the timing guard, a blocking call is attributed with `loopguard.classify`: to the innermost
COROUTINE frame of ours, because a synchronous subtree is the work of the coroutine that entered it.
So product code counts; a synchronous tool a TEST runs on the loop -- the offline verifier reading a
package in an async test, or the loopguard tests' `time.sleep` standing in for a descheduled thread --
is the test's work, recorded but never failing the test (the verifier runs off any loop in its real
CLI). A handle is wrapped only when OUR code opened it on the loop (stdlib trampolines like
`pathlib.Path.open` are skipped, but a library opening its own resource file is not), so a library or
test open is never proxied, and whether a read on it counts is then decided the same way.

Enabled in-process by `tests/conftest.py`; in a worker a test spawns by `install_from_env`, which drops
one JSON file per violation into `EDISC_TEST_LOOP_BLOCK_DIR/violations` for the spawning test to fail
on (the same plumbing as `loopguard`).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import json
import os
import socket
import ssl
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from edisc_core.loopguard import ENV_DIR, ENV_MS, PRODUCT_ROOTS, TEST_ROOTS, classify

THRESHOLD_BYTES = 1 << 16  # 64 KiB (see the module docstring for the justification)
VIOL_SUBDIR = "violations"
_OURS = __file__
# the stdlib directory, to skip pathlib/gzip/... trampolines when deciding who opened a file
# (site-packages lives under it, so it is excluded explicitly)
_STDLIB = os.path.dirname(os.__file__) + os.sep


@dataclass(frozen=True)
class Violation:
    """One blocking I/O call made on an event-loop thread by code of ours."""

    id: str
    pid: int
    kind: str  # "blocking socket", "time.sleep", "os.read", "file read", ...
    detail: str
    origin: str  # "product" or "unattributed" (a "test"-origin call is dropped, never a Violation)
    where: str  # innermost frame of ours: "path:line in function"
    stack: str

    def describe(self) -> str:
        return (
            f"blocking I/O on the event loop (pid {self.pid}, {self.origin}): {self.kind} "
            f"-- {self.detail}; move it to a thread (CLAUDE.md, ADR 0015 §23, §24.7) in "
            f"{self.where}\n{self.stack}"
        )


@dataclass
class _Config:
    report: Callable[[Violation], None]
    product_roots: Sequence[str] = PRODUCT_ROOTS
    test_roots: Sequence[str] = TEST_ROOTS
    threshold_bytes: int = THRESHOLD_BYTES


_STATES: list[_Config] = []  # a stack: the top decides (a test pushes its own roots, then pops)
_PATCHED = False


def _current() -> _Config | None:
    return _STATES[-1] if _STATES else None


def _on_loop() -> bool:
    """Is the current thread driving an event loop? (False in a worker thread or a sync CLI.)"""
    return asyncio.events._get_running_loop() is not None


def _flag(cfg: _Config, kind: str, detail: str) -> None:
    """Attribute the blocking call the way the timing guard does (`loopguard.classify`: the innermost
    coroutine frame of ours) and report it only when it counts -- product code or unattributable. A
    test driving a synchronous tool on the loop (origin ``test``), a loop waiting for I/O (``idle``)
    or a driver's own cost (``library``) is dropped."""
    caller = sys._getframe(1)
    origin, where = classify(caller, product_roots=cfg.product_roots, test_roots=cfg.test_roots)
    if origin not in ("product", "unattributed"):
        return
    stack = "".join(traceback.format_stack(caller))
    cfg.report(Violation(uuid.uuid4().hex, os.getpid(), kind, detail, origin, where, stack))


# -- socket -----------------------------------------------------------------------------------------

# methods of socket.socket / ssl.SSLSocket that block until bytes move or a peer connects
_SOCK_METHODS = (
    "recv", "recvfrom", "recv_into", "recvfrom_into", "recvmsg", "recvmsg_into",
    "send", "sendall", "sendto", "sendmsg", "sendfile", "connect", "connect_ex", "accept",
    "read", "write",  # ssl.SSLSocket's own byte path
)  # fmt: skip


def _blocking(sock: object) -> bool:
    """A socket NOT in non-blocking mode. asyncio and every async driver use non-blocking sockets
    (`gettimeout() == 0`); a synchronous socket blocks (`None`, or a positive timeout)."""
    try:
        return bool(sock.gettimeout() != 0)  # type: ignore[attr-defined]
    except OSError:
        return False


def _wrap_socket_method(cls: type, name: str) -> None:
    orig = getattr(cls, name)  # may be inherited from _socket.socket (a C method not in __dict__)

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        cfg = _current()
        if cfg is not None and _on_loop() and _blocking(self):
            _flag(cfg, "blocking socket", f"{cls.__name__}.{name}() on a blocking socket")
        return orig(self, *args, **kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = f"{cls.__name__}.{name}"
    setattr(cls, name, wrapper)


# -- time.sleep -------------------------------------------------------------------------------------


def _wrap_time_sleep() -> None:
    orig = time.sleep

    def sleep(seconds: float) -> None:
        cfg = _current()
        if cfg is not None and seconds and seconds > 0 and _on_loop():
            _flag(cfg, "time.sleep", f"time.sleep({seconds!r})")
        orig(seconds)

    time.sleep = sleep  # type: ignore[assignment]


# -- os raw-fd read/write ---------------------------------------------------------------------------


def _wrap_os_fd() -> None:
    def size_of(name: str, args: tuple[Any, ...]) -> int:
        if name in ("read", "pread"):  # read(fd, n[, ...]) / pread(fd, n, offset)
            return int(args[1]) if len(args) > 1 else 0
        if name in ("write", "pwrite"):  # write(fd, data) / pwrite(fd, data, offset)
            return len(args[1]) if len(args) > 1 else 0
        if name in ("readv", "writev"):  # (fd, buffers)
            return sum(len(b) for b in args[1]) if len(args) > 1 else 0
        return 0

    def blocks(args: tuple[Any, ...]) -> bool:
        """Only a BLOCKING descriptor stalls the loop. Regular files are blocking; the pipes and
        sockets asyncio reads with `os.read` on the loop are non-blocking and return at once."""
        try:
            return bool(args) and os.get_blocking(int(args[0]))
        except (OSError, ValueError, TypeError):
            return False

    def make(name: str, orig: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cfg = _current()
            n = size_of(name, args)
            if cfg is not None and _on_loop() and n >= cfg.threshold_bytes and blocks(args):
                _flag(cfg, f"os.{name}", f"os.{name}() of {n} bytes")
            return orig(*args, **kwargs)

        return wrapper

    for name in ("read", "write", "pread", "pwrite", "readv", "writev"):
        orig = getattr(os, name, None)
        if orig is not None:
            setattr(os, name, make(name, orig))


# -- buffered file read/write (handles product opened while on the loop) ----------------------------


class _GuardedFile:
    """A transparent proxy over a file object product code opened on the loop: it forwards
    everything and flags a read/write of at least the threshold made on the loop thread."""

    __slots__ = ("_cfg", "_f")

    def __init__(self, fileobj: Any, cfg: _Config) -> None:
        self._f = fileobj
        self._cfg = cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._f, name)

    def _check(self, nbytes: int) -> None:
        cfg = self._cfg
        if nbytes >= cfg.threshold_bytes and _on_loop() and _current() is not None:
            _flag(cfg, "file read/write", f"a {nbytes}-byte read/write on an open file")

    def read(self, *args: Any) -> Any:
        data = self._f.read(*args)
        self._check(len(data))
        return data

    def read1(self, *args: Any) -> Any:
        data = self._f.read1(*args)
        self._check(len(data))
        return data

    def readall(self) -> Any:
        data = self._f.readall()
        self._check(len(data))
        return data

    def readinto(self, b: Any) -> Any:
        n = self._f.readinto(b)
        self._check(n or 0)
        return n

    def write(self, b: Any) -> Any:
        self._check(len(b))
        return self._f.write(b)

    def __enter__(self) -> _GuardedFile:
        self._f.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._f.__exit__(*exc)

    def __iter__(self) -> Any:
        return iter(self._f)


def _opener_is_product(cfg: _Config) -> bool:
    """Who called ``open``? Skip this module and stdlib trampolines (`pathlib.Path.open`,
    `gzip`, ...), then the first real frame decides. A library that opens its OWN resource file
    (botocore's gzipped service data) is that first frame and is NOT product, so it is not proxied,
    even though product code is higher up the stack that triggered it."""
    frame: FrameType | None = sys._getframe(2)  # skip _opener_is_product and guarded_open
    while frame is not None:
        path = frame.f_code.co_filename
        if path == _OURS or (path.startswith(_STDLIB) and "site-packages" not in path):
            frame = frame.f_back
            continue
        return path.startswith(tuple(cfg.product_roots))
    return False


def _wrap_open() -> None:
    orig = io.open

    def guarded_open(*args: Any, **kwargs: Any) -> Any:
        fileobj = orig(*args, **kwargs)
        cfg = _current()
        if cfg is None or not _on_loop() or not _opener_is_product(cfg):
            return fileobj
        return _GuardedFile(fileobj, cfg)

    io.open = guarded_open
    builtins.open = guarded_open


def _patch_all() -> None:
    global _PATCHED
    if _PATCHED:
        return
    for name in _SOCK_METHODS:
        # socket.socket inherits its byte methods from the C _socket.socket (not in its __dict__):
        # override them on socket.socket. On ssl.SSLSocket wrap only its OWN overrides, so a method
        # it inherits from socket.socket (already wrapped) is not wrapped twice.
        if hasattr(socket.socket, name):
            _wrap_socket_method(socket.socket, name)
        if name in ssl.SSLSocket.__dict__:
            _wrap_socket_method(ssl.SSLSocket, name)
    _wrap_time_sleep()
    _wrap_os_fd()
    _wrap_open()
    _PATCHED = True


def install(
    report: Callable[[Violation], None],
    *,
    product_roots: Sequence[str] = PRODUCT_ROOTS,
    test_roots: Sequence[str] = TEST_ROOTS,
    threshold_bytes: int = THRESHOLD_BYTES,
) -> _Config:
    """Patch the entry points (once) and make ``report`` the active sink. A later install stacks on
    top (so a test can push its own roots); ``uninstall`` pops it. Returns the config to uninstall."""
    _patch_all()
    cfg = _Config(report, product_roots, test_roots, threshold_bytes)
    _STATES.append(cfg)
    return cfg


def uninstall(cfg: _Config) -> None:
    with contextlib.suppress(ValueError):
        _STATES.remove(cfg)


@contextlib.contextmanager
def installed(report: Callable[[Violation], None], **kwargs: Any) -> Iterator[_Config]:
    cfg = install(report, **kwargs)
    try:
        yield cfg
    finally:
        uninstall(cfg)


def write_violation(directory: Path, v: Violation) -> None:
    sub = directory / VIOL_SUBDIR
    sub.mkdir(parents=True, exist_ok=True)
    tmp = sub / f".{v.pid}-{v.id}-{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(asdict(v)))
    tmp.replace(sub / f"{v.pid}-{v.id}.json")


def read_violations(directory: Path, seen: set[str]) -> list[Violation]:
    sub = directory / VIOL_SUBDIR
    found = []
    for path in sorted(sub.glob("*.json")) if sub.is_dir() else ():
        if path.name in seen:
            continue
        seen.add(path.name)
        found.append(Violation(**json.loads(path.read_text())))
    return found


def install_from_env(*, permitted: bool, log: Callable[[str], None]) -> _Config | None:
    """In a process a test spawned: wrap and report to ``EDISC_TEST_LOOP_BLOCK_DIR/violations`` if
    `EDISC_TEST_LOOP_BLOCK_MS` is set. Refused (SystemExit) unless ``permitted`` (EDISC_ENV test/ci)."""
    if not os.environ.get(ENV_MS):
        return None
    if not permitted:
        raise SystemExit(f"{ENV_MS} is only permitted when EDISC_ENV is test or ci")
    directory = Path(os.environ[ENV_DIR]) if os.environ.get(ENV_DIR) else None

    def report(v: Violation) -> None:
        log(v.describe())
        if directory is not None:
            write_violation(directory, v)

    return install(report)
