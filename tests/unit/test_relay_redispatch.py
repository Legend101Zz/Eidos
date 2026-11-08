"""G5 live run: a coordinator that stopped on its own (budget exhausted) left the investigation active; the
reconciler restarted it at turn index 0, which replayed turn 1's recorded begin_turn receipt and failed with
COMMAND_KEY_CONFLICT on complete_turn, in a loop. Now a re-dispatched coordinator continues after the ledger's
last turn, and an investigation that needs the operator is not re-dispatched."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from eidos.api.relay import OutboxRelay


class FakeTx:
    project_id = "_system"

    def __init__(self, invs, turns):
        self._invs, self._turns = invs, turns

    def list_turns(self, inv):
        return self._turns.get(inv, [])

    def all_unfinished_jobs(self):
        return []

    def list_projects(self):
        return [{"id": "p"}]

    def list_records(self, pid, kind, status=None, limit=500):
        return self._invs


class FakeLedger:
    def __init__(self, tx):
        self.tx = tx

    @contextlib.contextmanager
    def transaction(self):
        yield self.tx


class FakeTemporal:
    def __init__(self):
        self.started = []

    async def start_workflow(self, wf, arg, **kw):
        self.started.append((wf, arg))

    def get_workflow_handle(self, wid):
        from temporalio.service import RPCError, RPCStatusCode

        async def describe():
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return SimpleNamespace(describe=describe)


def test_reconcile_resumes_after_the_last_turn_and_skips_needs_input():
    invs = [SimpleNamespace(id="inv-a", labels={}), SimpleNamespace(id="inv-b", labels={"needs_input": {"reason": "x"}}),
            SimpleNamespace(id="inv-c", labels=None)]
    turns = {"inv-a": [{"seq": 1}, {"seq": 2}, {"seq": 11}]}
    t = FakeTemporal()
    relay = OutboxRelay(FakeLedger(FakeTx(invs, turns)), t)
    out = asyncio.run(relay.reconcile())
    assert out["redispatched"] == ["inv-a", "inv-c"]  # inv-b waits for the operator
    args = {a["investigation_id"]: a for _, a in t.started}
    assert args["inv-a"]["turn_counter"] == 11
    assert "turn_counter" not in args["inv-c"]  # no turns yet: starts at 0


def test_decision_usd_envelope_is_a_tight_upper_bound():
    from eidos.investigator.activities import decision_usd_envelope
    price = {"input": 2.0, "output": 10.0}
    cfg = {"max_requests_per_decision": 4, "max_output_tokens": 4096}
    usd = decision_usd_envelope(120_000, price, cfg)
    out = 4 * 4096
    assert abs(usd - ((120_000 - out) * 2 + out * 10) / 1e6) < 1e-12
    assert usd < 120_000 * 10 / 1e6  # the old bound priced everything as output
    assert usd >= 120_000 * 2 / 1e6  # never below all-input
    assert decision_usd_envelope(1000, price, cfg) == 1000 * 10 / 1e6  # tiny envelope: may all be output
    assert decision_usd_envelope(1000, {"flat": 3.0}, cfg) == 1000 * 3.0 / 1e6
