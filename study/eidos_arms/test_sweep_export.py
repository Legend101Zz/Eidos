"""Offline evidence-export policy checks using fabricated ledger receipts and blobs."""
import hashlib
import json
from copy import deepcopy

import pytest

from .arm4 import export_answer, export_sweep_evidence, validate

AID = 'art_0123456789abcdef_evidence_table'
JOB = 'job_0123456789abcdef'
COMPONENT = 'eidos.search.recipe-sweep@1.1.0'
SCHEMA = 'recipe-sweep-table/1'
BUNDLE_SHA = '22068c870b4463189e8dfde0266a6b695b29858304a71f456d842a870e023e07'
INPUTS = {'train': 'art_task_train', 'validation': 'art_task_validation', 'task': 'art_task_manifest'}
TABLE = {'schema': SCHEMA, 'selection_split': 'visible-validation', 'exploratory': True,
         'claim': None, 'recipes': [{'name': 'linear'}]}
MEASUREMENT = {'metric': 'RMSE', 'direction': 'lower_is_better', 'split': 'visible-validation',
               'denominator': 10, 'units': 'score', 'baseline': 2, 'candidate': 1,
               'uncertainty_method': 'No independent confirmation', 'evidence_receipt': AID}


class SweepClient:
    def __init__(self, table=TABLE):
        self.payload = json.dumps(table).encode()
        sha = hashlib.sha256(self.payload).hexdigest()
        expected = {'schema': SCHEMA, 'role': 'output', 'size': len(self.payload), 'digest': sha,
                    'media_type': 'application/json'}
        self.art = {**expected, 'id': AID, 'project_id': 'p', 'producer_job': JOB, 'producer_attempt': 'a1',
                    'origin': 'job-output', 'access_class': 'project'}
        self.job = {'id': JOB, 'project_id': 'p', 'investigation_id': 'inv', 'status': 'succeeded',
                    'current_attempt': 'a1', 'terminal': {'attempt_id': 'a1'},
                    'outputs': [{**expected, 'artifact_id': AID, 'slot': 'evidence_table', 'attempt_id': 'a1'}],
                    'lock': {'project_id': 'p', 'grants': [], 'output_access_class': 'project', 'config': {'template': {'name': 'quick'}},
                             'component': {'ref': COMPONENT, 'digest': 'release-sha', 'bundle': {'digest': BUNDLE_SHA}},
                             'outputs': {'evidence_table': SCHEMA},
                             'inputs': {split: {'artifact_id': INPUTS[split], 'schema': 'task-array/1'}
                                        for split in ('train', 'validation')}}}
        self.release = {'id': COMPONENT, 'status': 'active',
                        'body': {'digest': 'release-sha', 'bundle_digest': BUNDLE_SHA}}
        self.downloads = []

    def query(self, name, params, *, project):
        assert project == 'p'
        if name == 'get_job':
            assert params == {'job_id': JOB}
            return self.job
        if name == 'get_artifact':
            assert params == {'artifact_id': AID}
            return self.art
        assert name == 'describe_component' and params == {'component_ref': COMPONENT}
        return self.release

    def download(self, project, aid, path):
        assert (project, aid) == ('p', AID)
        self.downloads.append(aid)
        path.write_bytes(self.payload)


def test_export_preserves_an_authored_abstention_and_maps_only_the_verified_bytes(tmp_path):
    client = SweepClient()
    abstention = {'abstained': True, 'reason': 'Independent evidence is incomplete.',
                  'unresolved': ['Held-out comparison'], 'next_discriminating_test': 'Run independent confirmation.'}
    cited = {'path': AID, 'sha256': client.art['digest'], 'role': 'search table',
             'supports': 'The investigator observed a validation difference.', 'limitations': 'Visible data only.'}
    metadata = {'evidence': [cited], 'status': 'abstained', 'claim_type': 'no_positive_claim', 'claim': 'No verified improvement.',
                'measurements': [MEASUREMENT, MEASUREMENT], 'abstention': abstention}
    authored = deepcopy(metadata)
    export_answer(client, 'p', 'inv', tmp_path, {'card': 'N02'}, None, 'No locked export',
                  'Investigator abstained', [], dict.fromkeys(('input', 'cache_read', 'cache_write', 'output'), 0), [],
                  answer_metadata=metadata, task_artifacts=INPUTS)
    assert metadata == authored
    path = tmp_path / 'out/answer.json'
    assert validate(path)
    answer = json.loads(path.read_bytes())
    assert (answer['status'], answer['claim_type'], answer['claim'], answer['abstention']) == (
        'abstained', 'no_positive_claim', authored['claim'], abstention)
    assert answer['candidate'] is None
    relative = f'evidence/{AID}.json'
    assert answer['measurements'] == [{**MEASUREMENT, 'evidence_receipt': relative}] * 2
    assert client.downloads == [AID]
    assert (path.parent / relative).read_bytes() == client.payload
    mapping, = json.loads((tmp_path / 'measurement_receipt_mappings.json').read_bytes())['mappings']
    assert (mapping['author'], mapping['artifact_id'], mapping['path'], mapping['sha256']) == (
        'study-adapter', AID, relative, client.art['digest'])
    assert mapping['authored_evidence'] == [cited] and mapping['investigation_id'] == 'inv'


@pytest.mark.parametrize(('record', 'path', 'value'), [
    ('art', ['access_class'], 'sealed-labels'),
    ('art', ['origin'], 'upload'),
    ('art', ['producer_attempt'], 'wrong-attempt'),
    ('job', ['investigation_id'], 'foreign-investigation'),
    ('job', ['status'], 'failed'),
    ('job', ['lock', 'inputs', 'validation', 'artifact_id'], 'foreign-split'),
    ('job', ['lock', 'config'], {'command': {'argv': ['python']}}),
    ('job', ['lock', 'component', 'ref'], 'unapproved@1.0.0'),
    ('release', ['body', 'digest'], 'unmatched-release'),
])
def test_unverified_and_restricted_provenance_never_downloads(tmp_path, record, path, value):
    client = SweepClient()
    target = getattr(client, record)
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match='unverified or restricted'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, AID, INPUTS)
    assert not client.downloads and not (tmp_path / 'evidence').exists()


def test_unknown_artifact_and_linked_evidence_path_never_download(tmp_path):
    client = SweepClient()
    with pytest.raises(ValueError, match='outside the recipe-sweep'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, 'art_unknown', INPUTS)
    (tmp_path / 'evidence').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='unsafe evidence'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, AID, INPUTS)
    assert not client.downloads


@pytest.mark.parametrize('bad', ['digest', 'split'])
def test_bytes_and_split_mismatch_are_never_exported(tmp_path, bad):
    client = SweepClient(TABLE if bad == 'digest' else {**TABLE, 'selection_split': 'held-out'})
    if bad == 'digest':
        client.payload = client.payload.replace(b'linear', b'random')
    with pytest.raises(ValueError, match='digest or size|visible exploratory'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, AID, INPUTS)
    assert not (tmp_path / 'evidence' / (AID + '.json')).exists()



def test_authored_digest_mismatch_never_downloads(tmp_path):
    client = SweepClient()
    with pytest.raises(ValueError, match='unverified or restricted'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, AID, INPUTS, [{'path': AID, 'sha256': '0' * 64}])
    assert not client.downloads



def test_matching_job_and_release_with_replaced_bundle_never_download(tmp_path):
    client = SweepClient()
    client.release['body']['bundle_digest'] = '1' * 64
    client.job['lock']['component']['bundle']['digest'] = '1' * 64
    with pytest.raises(ValueError, match='unverified or restricted'):
        export_sweep_evidence(client, 'p', 'inv', tmp_path, AID, INPUTS)
    assert not client.downloads


def test_linked_answer_directory_is_refused_before_any_export(tmp_path):
    destination = tmp_path / 'destination'
    destination.mkdir()
    (tmp_path / 'out').symlink_to(destination)
    with pytest.raises(ValueError, match='unsafe answer export directory'):
        export_answer(SweepClient(), 'p', 'inv', tmp_path, {'card': 'N02'}, None, 'No export', '', [], {}, [])
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize('tamper', [None, 'pin', 'slot', 'scheme', 'input', 'task', 'task-mismatch'])
def test_portfolio_table_has_explicit_pinned_export_policy(tmp_path, tamper):
    from .arm4 import _SWEEP_BUNDLE_SHA
    aid = AID.replace('_evidence_table', '_portfolio_table')
    component = 'eidos.search.recipe-portfolio@1.0.0'
    schema = 'recipe-portfolio-table/1'
    pin = '4b02e49fd939d0856c16b2adee42ad51249eca19eb27cad4c3c523202dfdc3a0'
    client = SweepClient()
    table = dict(schema=schema, scheme='cv-train', exploratory=True, claim=None, recipes=[])
    if tamper == 'scheme':
        table['scheme'] = 'held-out'
    client.payload = json.dumps(table).encode()
    sha = hashlib.sha256(client.payload).hexdigest()
    for row in (client.art, client.job['outputs'][0]):
        row.update(schema=schema, digest=sha, size=len(client.payload))
    client.art['id'] = aid
    client.job['outputs'][0].update(artifact_id=aid, slot='portfolio_table')
    client.job['lock'].update(config=dict(mode='portfolio'), outputs=dict(portfolio_table=schema))
    client.job['lock']['component'].update(ref=component, bundle=dict(digest=pin))
    client.release.update(id=component, body=dict(digest='release-sha', bundle_digest=pin))
    if tamper == 'pin':
        client.release['body']['bundle_digest'] = _SWEEP_BUNDLE_SHA
    if tamper == 'slot':
        client.job['outputs'][0]['slot'] = 'evidence_table'
    if tamper == 'input':
        client.job['lock']['inputs']['validation']['artifact_id'] = 'foreign'
    if tamper in ('task', 'task-mismatch'):
        client.job['lock']['inputs']['task'] = dict(artifact_id=INPUTS['task'] if tamper == 'task' else 'foreign', schema='task-manifest/1')
    client.query = lambda name, params, **kw: {'get_job': client.job, 'get_artifact': client.art, 'describe_component': client.release}[name]
    client.download = lambda project, reference, path: path.write_bytes(client.payload)
    if tamper not in (None, 'task'):
        with pytest.raises(ValueError):
            export_sweep_evidence(client, 'p', 'inv', tmp_path, aid, INPUTS)
    else:
        entry, mapping = export_sweep_evidence(client, 'p', 'inv', tmp_path, aid, INPUTS)
        assert mapping['component_ref'] == component
        assert (tmp_path / entry['path']).read_bytes() == client.payload


def test_candidate_provenance_uses_accepted_executed_call_stamp():
    from .arm4 import candidate_provenance
    stamp = dict(tool='prepare_candidate', proposed_by='dec', basis='explicit-export')
    decision = dict(id='dec', accepted=True, body=dict(calls=[dict(tool='run_component', macro=stamp)]))
    record = dict(body=dict(receipts=[dict(index=0, ok=True, result=dict(job_id='selected'))]))
    class Client:
        def query(self, name, params, **kwargs):
            return dict(decisions=[decision]) if name == 'get_turn' else record
    provenance = candidate_provenance(Client(), 'p', dict(turns=[dict(id='turn')]), 'selected')
    assert provenance['macro'] == stamp and provenance['basis'] == 'explicit-export'
    decision['revoked'] = True
    assert candidate_provenance(Client(), 'p', dict(turns=[dict(id='turn')]), 'selected')['preparation'] == 'baseline-or-direct'
