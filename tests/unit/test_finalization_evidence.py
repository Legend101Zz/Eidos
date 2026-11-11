"""Held-out provenance closes over lineage, producers, digest aliases and project-wide jobs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eidos.application.handlers.finalization_evidence import check_read, excluded, safe_view
from eidos.domain.errors import Forbidden


def evidence_ctx():
    artifacts = [
        {'id': 'held', 'digest': 'root', 'access_class': 'project'},
        {'id': 'direct', 'digest': 'd1', 'producer_job': 'j1', 'access_class': 'project'},
        {'id': 'alias', 'digest': 'd1', 'access_class': 'project'},
        {'id': 'derived', 'digest': 'd2', 'producer_job': 'j2', 'access_class': 'project'},
        {'id': 'source', 'digest': 'd3', 'access_class': 'project'},
        {'id': 'safe', 'digest': 's', 'access_class': 'project'},
    ]
    jobs = [{'id': 'j1', 'lock': {'inputs': {'x': {'artifact_id': 'held', 'digest': 'root'}}}},
            {'id': 'j2', 'lock': {'inputs': {'x': {'artifact_id': 'alias', 'digest': 'd1'}}}}]
    inv = SimpleNamespace(id='inv', body={'charter_ref': {'id': 'charter'}})
    tx = Mock()
    tx.get_record.return_value = SimpleNamespace(body={'held_out': {'labels': ['sha256:root']}})
    tx.require.return_value = inv
    tx.list_artifacts.return_value = artifacts
    tx.list_jobs.return_value = jobs
    tx.lineage.return_value = [{'src': 'derived', 'dst': 'source'}, {'src': 'source', 'dst': 'component'}]
    return SimpleNamespace(tx=tx, project_id='p'), inv


def test_exclusion_follows_transitive_jobs_aliases_and_lineage():
    ctx, inv = evidence_ctx()
    assert excluded(ctx, inv) == {'held', 'j1', 'direct', 'alias', 'j2', 'derived', 'source', 'component'}


def test_read_boundary_checks_allowlist_and_rechecks_provenance_before_disclosure():
    ctx, _ = evidence_ctx()
    ctx.tx.get_turn.return_value = {'project_id': 'p', 'investigation_id': 'inv', 'spec': {
        'evidence_allowlist': {'artifacts': ['safe', 'derived']}}}
    check_read(ctx, {'finalization_turn_id': 't'}, 'artifacts', 'safe')
    for artifact in ['derived', 'unknown']:
        with pytest.raises(Forbidden):
            check_read(ctx, {'finalization_turn_id': 't'}, 'artifacts', artifact)


def test_packed_projection_removes_echoes_of_held_out_results_and_pins():
    view = {'investigation': {'id': 'inv', 'body': {'question': 'q', 'inputs': {'held': 'held', 'safe': 'safe'}}},
            'jobs': [{'id': 'j1', 'receipt': {'secret': 123}}, {'id': 'good', 'receipt': {'accuracy': .9}}],
            'call_results': [{'secret': 123}], 'turns': [{'result': {'secret': 123}}],
            'pinned_notes': [{'text': 'derived result held'}, {'text': 'locked good'}],
            'created_artifacts': [{'artifact_id': 'held'}, {'artifact_id': 'safe'}],
            'baseline_record': {'job_id': 'j1', 'value': 123}}
    clean = safe_view(view, {'jobs': ['good'], 'artifacts': ['safe']}, blocked={'held', 'j1'})
    assert clean['jobs'] == [view['jobs'][1]] and clean['baseline_record'] is None
    assert clean['turns'] == clean['call_results'] == []
    assert clean['pinned_notes'] == [{'text': 'locked good'}]
    assert clean['created_artifacts'] == [{'artifact_id': 'safe'}]
    assert clean['investigation']['body']['inputs'] == {'safe': 'safe'}
