import threading
import time
import uuid

from edisc_core.ids import new_id, uuid7, uuid7_timestamp_ms


def test_uuid7_layout() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert abs(uuid7_timestamp_ms(value) - time.time_ns() // 1_000_000) < 5_000


def test_monotonic_and_unique_in_a_tight_loop() -> None:
    ids = [new_id() for _ in range(50_000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_unique_across_threads() -> None:
    out: list[uuid.UUID] = []
    lock = threading.Lock()

    def work() -> None:
        local = [uuid7() for _ in range(5_000)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 40_000
