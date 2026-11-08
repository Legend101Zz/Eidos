"""Real wall-clock Temporal behaviour (no time skipping): heartbeat timeout, start-to-close timeout,
retry policy, and a worker that disappears (SIGKILL) mid-activity with another worker taking over.

Runs against EIDOS_TEMPORAL_TARGET (e.g. the lab's regular Temporal server, 127.0.0.1:7633) when set,
otherwise against a local Temporal server binary started for the module (same server code paths and
real timers; time-skipping is NOT used)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError as TemporalTimeoutError, TimeoutType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

pytestmark = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")


@pytest.fixture(scope="module")
async def target():
    if os.environ.get("EIDOS_TEMPORAL_TARGET"):
        client = await Client.connect(os.environ["EIDOS_TEMPORAL_TARGET"], namespace=os.environ.get("EIDOS_TEMPORAL_NS", "default"))
        yield client, os.environ["EIDOS_TEMPORAL_TARGET"]
        return
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield env.client, env.client.service_client.config.target_host
    await env.shutdown()


ATTEMPTS: dict[str, list[int]] = {}


@activity.defn(name="rt_heartbeat_once_then_stall")
async def heartbeat_once_then_stall(key: str) -> int:
    ATTEMPTS.setdefault(key, []).append(activity.info().attempt)
    if activity.info().attempt == 1:
        activity.heartbeat()
        await asyncio.sleep(6)  # no further heartbeats -> heartbeat timeout fires
    return activity.info().attempt


@activity.defn(name="rt_slow")
async def slow(key: str) -> str:
    ATTEMPTS.setdefault(key, []).append(activity.info().attempt)
    await asyncio.sleep(4)
    return "late"


@activity.defn(name="rt_flaky")
async def flaky(arg: dict) -> int:
    ATTEMPTS.setdefault(arg["key"], []).append(activity.info().attempt)
    if arg.get("fatal"):
        raise ApplicationError("bad input", type="INVALID", non_retryable=True)
    if activity.info().attempt < 3:
        raise ApplicationError("transient", type="Transient")
    return activity.info().attempt


@workflow.defn(name="RealtimeWorkflow", sandboxed=False)
class RealtimeWorkflow:
    @workflow.run
    async def run(self, a: dict) -> dict:
        kind = a["kind"]
        if kind == "heartbeat":
            r = await workflow.execute_activity("rt_heartbeat_once_then_stall", a["key"], start_to_close_timeout=timedelta(seconds=30),
                                                heartbeat_timeout=timedelta(seconds=1),
                                                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200), maximum_attempts=3))
            return {"attempt": r}
        if kind == "start_to_close":
            try:
                await workflow.execute_activity("rt_slow", a["key"], start_to_close_timeout=timedelta(seconds=1),
                                                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200), maximum_attempts=2))
            except ActivityError as exc:
                c = exc.cause
                return {"timeout_type": c.type.name if isinstance(c, TemporalTimeoutError) else type(c).__name__}
            return {"timeout_type": None}
        if kind in ("retry", "fatal"):
            try:
                r = await workflow.execute_activity("rt_flaky", {"key": a["key"], "fatal": kind == "fatal"},
                                                    start_to_close_timeout=timedelta(seconds=10),
                                                    retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200),
                                                                             backoff_coefficient=1.5, maximum_attempts=5,
                                                                             non_retryable_error_types=["INVALID"]))
                return {"attempt": r}
            except ActivityError as exc:
                return {"failed": type(exc.cause).__name__}
        if kind == "takeover":
            r = await workflow.execute_activity("rt_takeover", a["marker"], start_to_close_timeout=timedelta(seconds=60),
                                                heartbeat_timeout=timedelta(seconds=2),
                                                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200), maximum_attempts=3))
            return {"result": r}
        raise ValueError(kind)


async def _run(client, queue, arg, activities):
    async with Worker(client, task_queue=queue, workflows=[RealtimeWorkflow], activities=activities):
        return await client.execute_workflow(RealtimeWorkflow.run, arg, id=f"rt-{uuid.uuid4().hex[:8]}", task_queue=queue)


async def test_heartbeat_timeout_triggers_a_retry(target):
    client, _ = target
    key = uuid.uuid4().hex
    t0 = time.monotonic()
    r = await _run(client, f"rt-{key}", {"kind": "heartbeat", "key": key}, [heartbeat_once_then_stall])
    assert r["attempt"] == 2 and ATTEMPTS[key] == [1, 2]
    assert time.monotonic() - t0 < 6  # retried after ~1 s heartbeat timeout, not after the 6 s stall


async def test_start_to_close_timeout_is_enforced_with_finite_attempts(target):
    client, _ = target
    key = uuid.uuid4().hex
    t0 = time.monotonic()
    r = await _run(client, f"rt-{key}", {"kind": "start_to_close", "key": key}, [slow])
    assert r["timeout_type"] in ("START_TO_CLOSE", TimeoutType.START_TO_CLOSE.name) and len(ATTEMPTS[key]) == 2
    assert time.monotonic() - t0 < 4


async def test_retry_policy_and_non_retryable_errors(target):
    client, _ = target
    k1, k2 = uuid.uuid4().hex, uuid.uuid4().hex
    assert (await _run(client, f"rt-{k1}", {"kind": "retry", "key": k1}, [flaky]))["attempt"] == 3
    assert ATTEMPTS[k1] == [1, 2, 3]
    r = await _run(client, f"rt-{k2}", {"kind": "fatal", "key": k2}, [flaky])
    assert r["failed"] == "ApplicationError" and ATTEMPTS[k2] == [1]


WORKER_A = textwrap.dedent('''
    import asyncio, sys
    from pathlib import Path
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Worker

    @activity.defn(name="rt_takeover")
    async def takeover(marker: str) -> str:
        Path(marker + ".a-started").write_text(str(activity.info().attempt))
        while True:  # heartbeats until this process is killed
            activity.heartbeat()
            await asyncio.sleep(0.2)

    async def main():
        c = await Client.connect(sys.argv[1], namespace=sys.argv[3])
        async with Worker(c, task_queue=sys.argv[2], activities=[takeover]):
            await asyncio.Event().wait()
    asyncio.run(main())
''')


async def test_worker_killed_mid_activity_is_taken_over_after_heartbeat_timeout(target, tmp_path):
    client, host = target
    queue = f"rt-takeover-{uuid.uuid4().hex[:6]}"
    marker = str(tmp_path / "m")
    script = tmp_path / "worker_a.py"
    script.write_text(WORKER_A)
    proc = subprocess.Popen([sys.executable, str(script), host, queue, client.namespace])
    try:
        async with Worker(client, task_queue=queue, workflows=[RealtimeWorkflow]):
            handle = await client.start_workflow(RealtimeWorkflow.run, {"kind": "takeover", "marker": marker},
                                                 id=f"rt-{uuid.uuid4().hex[:8]}", task_queue=queue)
            for _ in range(100):
                if Path(marker + ".a-started").exists():
                    break
                await asyncio.sleep(0.1)
            assert Path(marker + ".a-started").exists(), "worker A never started the activity"
            proc.kill()  # the worker disappears mid-activity (SIGKILL, no graceful shutdown)
            proc.wait(10)

            @activity.defn(name="rt_takeover")
            async def takeover_b(m: str) -> str:
                return f"worker-b attempt {activity.info().attempt}"

            t0 = time.monotonic()
            async with Worker(client, task_queue=queue, activities=[takeover_b]):
                result = await handle.result()
            assert result["result"] == "worker-b attempt 2"
            assert time.monotonic() - t0 < 15  # recovered via the 2 s heartbeat timeout
    finally:
        if proc.poll() is None:
            proc.kill()
    _ = WorkflowFailureError
