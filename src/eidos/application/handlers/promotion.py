"""Ledger inputs for pure preparation; source inclusions use installed release bytes."""
from __future__ import annotations

import hashlib
from typing import Any

from eidos.application.handlers import exploration
from eidos.application.handlers.components import read_bundle
from eidos.application.service import Ctx
from eidos.domain import exploration as rules
from eidos.domain.errors import Invalid, NotFound
from eidos.domain.promotion import PORTFOLIO_REF


def stamps(ctx: Ctx, inv_id: str) -> dict[str, Any]:
    out = {}
    for turn in ctx.tx.list_turns(inv_id):
        for decision in ctx.tx.decisions(turn['id']):
            if not decision['accepted']:
                continue
            calls = decision['body'].get('calls') or []
            receipts = ctx.tx.get_record('decision_receipts', decision['id'])
            for receipt in (receipts.body.get('receipts') if receipts else []) or []:
                idx = receipt.get('index')
                if isinstance(idx, int) and idx < len(calls) and calls[idx].get('macro') and receipt.get('ok'):
                    jid = (receipt.get('result') or {}).get('job_id')
                    if jid:
                        out[jid] = calls[idx]['macro']
    return out


def expansion_view(ctx: Ctx, inv: Any, calls: list[dict]) -> dict:
    if [i for i, c in enumerate(calls) if c['tool'] == 'prepare_candidate'] != [len(calls) - 1]:
        raise Invalid('prepare_candidate: must be the last call, once per decision')
    jobs = exploration._jobs(ctx, inv.id)
    baseline = ctx.tx.get_record(exploration.BASELINE_KIND, inv.id)
    baseline_job = (baseline.body if baseline else {}).get('job_id')
    base = next((j for j in jobs if j['id'] == baseline_job), {})
    predictions = next((o['artifact_id'] for o in base.get('outputs', []) if o['slot'] == 'validation_predictions'), None)
    releases = [r.id for r in ctx.tx.list_records(ctx.project_id, 'component_release', status='active', limit=100000)]
    view = dict(investigation=dict(id=inv.id, body=inv.body), jobs=jobs, policy=rules.policy(inv.body.get('investigator')),
                releases=releases, baseline_predictions=predictions, exploration=exploration.exploration_state(ctx, inv))
    source = calls[-1]['args'].get('source')
    if isinstance(source, str):
        try:
            art, bundle = read_bundle(ctx, source)
        except NotFound:
            raise Invalid('prepare_candidate: source artifact not found in this project') from None
        producer = next((j for j in jobs if j['id'] == art.get('producer_job') and j['status'] == 'succeeded'), None)
        if producer is None or producer['component'] != PORTFOLIO_REF or not any(
                o['slot'] == 'promotion_source' and o['artifact_id'] == source for o in producer['outputs']):
            raise Invalid('prepare_candidate: source must be a finished pinned portfolio promotion_source')
        view.update(source=bundle, source_metrics=(producer.get('receipt') or {}).get('metrics') or {})
    return view


def inclusion(ctx: Ctx, args: dict) -> None:
    """Validate at acceptance, then pin inclusion identity for execution and retry."""
    if not args.get('include'):
        return
    if args['include'] != ['eidos_recipes']:
        raise Invalid('author_source: include accepts only ["eidos_recipes"]')
    release = ctx.tx.get_record('component_release', PORTFOLIO_REF)
    if release is None or release.status != 'active':
        raise Invalid('author_source: pinned portfolio release is not installed and active')
    _, bundle = read_bundle(ctx, release.body['bundle_artifact_id'])
    files, digest = library_files(bundle)
    if collisions(files, args.get('files') or {}):
        raise Invalid('author_source: included library files collide with authored files')
    args['include_bundle'] = dict(artifact_id=release.body['bundle_artifact_id'], digest=release.body['bundle_digest'], library_digest=digest)


def collisions(library: dict, authored: dict) -> bool:
    """Refuse file/directory and case-folded collisions on supported filesystems."""
    left, right = {p.casefold() for p in library}, {p.casefold() for p in authored}
    return any(a == b or a.startswith(b + '/') or b.startswith(a + '/') for a in left for b in right)


def library_files(bundle: dict) -> tuple[dict[str, str], str]:
    files = {name: value for name, value in bundle['files'].items() if name.startswith('eidos_recipes/') and name.endswith('.py')}
    if not files or any(not isinstance(value, str) or name.count('/') != 1 for name, value in files.items()):
        raise Invalid('author_source: invalid pinned library bundle')
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.split('/')[-1].encode() + b'\0' + value.encode())
    return files, digest.hexdigest()
