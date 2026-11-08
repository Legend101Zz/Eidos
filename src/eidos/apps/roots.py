"""Composition roots: each process role wires only the adapters and credentials it needs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sys

from eidos.persistence.roles import roles_from_config
from eidos.settings import LabConfig

log = logging.getLogger("eidos")


def setup_logging(role: str) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=os.environ.get("EIDOS_LOG_LEVEL", "INFO"),
                        format=f"%(asctime)s {role} %(levelname)s %(name)s %(message)s", stream=sys.stderr)


def client(cfg: LabConfig, principal: str, name: str):
    from eidos.client import LabClient
    return LabClient(cfg.api_url, cfg.token(principal), client_name=name)


async def temporal_client(cfg: LabConfig):
    from temporalio.client import Client
    return await Client.connect(cfg.temporal_target, namespace=cfg.temporal_namespace)


def run_api(cfg: LabConfig) -> None:
    """Command service + MCP-independent HTTP API + outbox relay. Holds the ledger writer role."""
    import uvicorn

    from eidos.api.app import create_app
    from eidos.api.relay import OutboxRelay
    from eidos.application.service import CommandService, load_handlers
    from eidos.persistence.ledger import SqlLedger
    from eidos.runtime.artifacts import LocalArtifactStore

    load_handlers()
    ledger = SqlLedger(roles_from_config(cfg).url("writer"), pool_size=8)
    store = LocalArtifactStore(cfg.p("artifacts"), min_free_bytes=cfg.min_free_bytes)

    class Notifier:
        relay = None

        def notify(self):
            if self.relay is not None and loop is not None:
                loop.call_soon_threadsafe(self.relay.notify)

    notifier = Notifier()
    service = CommandService(ledger, store, settings={"max_upload_bytes": 4 << 30, "lab_root": str(cfg.root),
                                                      "sealed_available": cfg.sealed_available,
                                                      "sealed_profile": cfg.sealed_profile}, notifier=notifier)
    loop = None

    async def main():
        nonlocal loop
        loop = asyncio.get_running_loop()
        temporal = await temporal_client(cfg)
        relay = OutboxRelay(ledger, temporal, investigation_args={"history_limit": cfg.history_limit,
                            "turns_per_run": cfg.turns_per_run, "idle_poll_seconds": cfg.idle_poll_seconds})
        notifier.relay = relay
        relay.maintenance_flag = cfg.p("run", "maintenance")
        app = create_app(service, relay=relay, maintenance_flag=cfg.p("run", "maintenance"))
        server = uvicorn.Server(uvicorn.Config(app, host=cfg.api_host, port=cfg.api_port, log_level="warning"))
        relay_task = asyncio.create_task(relay.run())
        try:
            await server.serve()
        finally:
            relay_task.cancel()

    asyncio.run(main())


def _worker(cfg: LabConfig, queue: str, *, workflows=(), activities=(), max_concurrent: int = 4) -> None:
    from temporalio.worker import Worker

    async def main():
        tc = await temporal_client(cfg)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent * 2) as pool:
            w = Worker(tc, task_queue=queue, workflows=list(workflows), activities=list(activities),
                       activity_executor=pool, max_concurrent_activities=max_concurrent)
            await w.run()

    asyncio.run(main())


def run_workflow_worker(cfg: LabConfig) -> None:
    from eidos.orchestration.names import WORKFLOW_QUEUE
    from eidos.orchestration.workflows import ALL_WORKFLOWS
    _worker(cfg, WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS)


def run_investigator_worker(cfg: LabConfig) -> None:
    from eidos.investigator.activities import InvestigatorActivities
    from eidos.orchestration.names import INVESTIGATOR_QUEUE
    acts = InvestigatorActivities(client(cfg, "svc-investigator", "investigator-worker"),
                                  client(cfg, "svc-workflow", "investigator-worker"))
    _worker(cfg, INVESTIGATOR_QUEUE, activities=[acts.begin_turn, acts.build_context, acts.decide,
                                                 acts.execute_decision, acts.complete_turn, acts.check_news])


def _runner(cfg: LabConfig, principal: str, profiles: tuple[str, ...]):
    from eidos.runtime.artifacts import LocalArtifactStore
    from eidos.runtime.runner import AttemptRunner
    return AttemptRunner(client(cfg, principal, principal), LocalArtifactStore(cfg.p("artifacts"), min_free_bytes=cfg.min_free_bytes),
                         lab_root=cfg.root, launcher_id=principal, api_port=cfg.api_port,
                         credential_path=str(cfg.secret_path("admin.token")), allowed_profiles=profiles)


def run_training_runner(cfg: LabConfig) -> None:
    from eidos.orchestration.activities import RunnerActivities
    from eidos.orchestration.names import RUNNER_QUEUE
    acts = RunnerActivities(_runner(cfg, "svc-launcher", ("dev:same-user",)))
    _worker(cfg, RUNNER_QUEUE, activities=[acts.run_attempt, acts.fail_job], max_concurrent=int(cfg.extra.get("runner_slots", 2)))


def run_sealed_runner(cfg: LabConfig) -> None:
    from eidos.orchestration.activities import RunnerActivities
    from eidos.orchestration.names import SEALED_QUEUE
    acts = RunnerActivities(_runner(cfg, "svc-sealed-launcher", (cfg.sealed_profile or "linux:bwrap-sealed",)))
    # Sealed is the default profile, so it needs the same parallelism as the dev runner.
    _worker(cfg, SEALED_QUEUE, activities=[acts.run_attempt, acts.fail_job],
            max_concurrent=int(cfg.extra.get("sealed_runner_slots", cfg.extra.get("runner_slots", 2))))


def run_assessor_worker(cfg: LabConfig) -> None:
    from eidos.orchestration.activities import AssessorActivities
    from eidos.orchestration.names import ASSESSOR_QUEUE
    acts = AssessorActivities(client(cfg, "svc-assessor", "assessor-worker"), client(cfg, "svc-qualifier", "assessor-worker"),
                              None, cfg.lab_python or sys.executable,
                              credential_path=str(cfg.secret_path("svc-assessor.token")), api_port=cfg.api_port,
                              lab_root=str(cfg.root))
    _worker(cfg, ASSESSOR_QUEUE, activities=[acts.assess_plan, acts.submit_qualification_results])


ROLES = {"api": run_api, "workflow-worker": run_workflow_worker, "investigator-worker": run_investigator_worker,
         "training-runner": run_training_runner, "sealed-runner": run_sealed_runner, "assessor-worker": run_assessor_worker}


def main_role(role: str) -> None:
    setup_logging(role)
    ROLES[role](LabConfig.load())
