"""Event sequence must never let a reader skip a committed event (review item: sequence gaps under
concurrent writers). Reproduces the hazard: T1 allocates a lower seq, T2 a higher one and commits
first; a reader advancing its cursor past T2 must still see T1's event afterwards."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.pg


def test_reader_cursor_never_skips_late_committing_lower_sequence(lab):
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid})
    t1_inserted, release_t1 = threading.Event(), threading.Event()

    def slow_writer():
        with lab.ledger.transaction() as tx:
            tx.project_id = pid
            tx.append_event(pid, "test.slow", "slow", {})
            t1_inserted.set()
            release_t1.wait(5)

    th = threading.Thread(target=slow_writer)
    th.start()
    assert t1_inserted.wait(5)
    # A second writer either waits for T1 (gap-free allocation) or commits a higher seq first.
    done = threading.Event()

    def fast_writer():
        with lab.ledger.transaction() as tx:
            tx.project_id = pid
            tx.append_event(pid, "test.fast", "fast", {})
        done.set()

    th2 = threading.Thread(target=fast_writer)
    th2.start()
    time.sleep(0.5)
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        first_read = tx.events_after(pid, 0, types=["test.slow", "test.fast"])
    cursor = max((e["seq"] for e in first_read), default=0)
    release_t1.set()
    th.join()
    th2.join()
    assert done.is_set()
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        later = tx.events_after(pid, cursor, types=["test.slow", "test.fast"])
    seen = {e["subject"] for e in first_read} | {e["subject"] for e in later}
    assert seen == {"slow", "fast"}, (first_read, later)
