"""What every Redis client of ours tells the server about itself, resolved ONCE.

redis-py resolves its own version with ``importlib.metadata.version("redis")`` for EVERY new
connection unless given one: a scan of the installed distributions' metadata (file reads) on the
event loop. A pool opening connections under a burst blocked the API's loop for 301 ms on a CI
runner (CI run 39132450451, ``test_stalled_clients``). Resolved here at import, outside any loop,
and passed to every client (``driver_info=DRIVER_INFO``)."""

from __future__ import annotations

from importlib import metadata

from redis.driver_info import DriverInfo

DRIVER_INFO = DriverInfo(name="redis-py", lib_version=metadata.version("redis"))
