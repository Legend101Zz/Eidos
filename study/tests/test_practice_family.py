"""Practice packaging tests use only newly generated data, never private registries."""
import hashlib
import json
from pathlib import Path

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('sklearn')

from study.practice.build_cards import audit_bundles, build, tree_digest  # noqa: E402
from study.practice.generators import generate, registry  # noqa: E402


def test_registry_is_frozen_and_neutral():
    entries = registry()
    assert len(entries) == 15
    manifest = json.loads((Path(__file__).parents[2] /
                           'docs/experiments/practice-family/MANIFEST.json').read_text())
    assert entries == manifest['generators']
    assert len({g['id'] for g in entries}) == 15
    assert sum(g['role'] == 'holdout-class' for g in entries) == 2


@pytest.mark.parametrize('entry', registry(), ids=lambda g: g['id'])
def test_all_generators_have_deterministic_disjoint_splits(entry):
    a, b = generate(entry, 0), generate(entry, 0)
    assert a.task_type == b.task_type
    all_ids = []
    for split in ('train', 'validation', 'test'):
        for key in ('x', 'y', 'ids'):
            assert np.array_equal(a.splits[split][key], b.splits[split][key])
        assert len(a.splits[split]['x']) == entry['sizes'][split]
        assert len(a.splits[split]['y']) == entry['sizes'][split]
        assert np.isfinite(a.splits[split]['x']).all()
        all_ids.extend(a.splits[split]['ids'])
    assert len(all_ids) == len(set(all_ids))
    if entry['id'] == 'gen-010':
        groups = [set(a.splits[s]['groups']) for s in a.splits]
        assert not groups[0] & groups[1] and not groups[0] & groups[2]


def test_bundle_bytes_and_boundary(tmp_path):
    first = build(tmp_path / 'first', generator_ids=['gen-001'], instances=1)
    second = build(tmp_path / 'second', generator_ids=['gen-001'], instances=1)
    assert first['data_digest'] == second['data_digest']
    assert audit_bundles(tmp_path / 'first')['ok']
    assert tree_digest(tmp_path / 'first') == tree_digest(tmp_path / 'second')
    bundle = tmp_path / 'first/bundles/P0001'
    assert {p.name for p in bundle.iterdir()} == {'train.npz', 'validation.npz', 'task.json', 'project.py', 'requirements.lock'}
    task = json.loads((bundle / 'task.json').read_text())
    assert task['task_id'] == 'P0001'
    assert 'gen-' not in (bundle / 'task.json').read_text()
    for path in bundle.iterdir():
        assert b'oracle_' not in path.read_bytes()
    test = tmp_path / 'first/controller/P0001/test_inputs.npz'
    with np.load(test) as values:
        assert set(values.files) == {'x', 'ids'}
    assert hashlib.sha256(test.read_bytes()).hexdigest()


def test_build_rejects_repository_output():
    with pytest.raises(ValueError, match='outside'):
        build(Path(__file__).parents[2] / '.pytest_cache/practice', generator_ids=[], instances=0)


@pytest.mark.parametrize('task_type', ['classification', 'regression'])
def test_score_table_fits_screens_in_fold_and_selection_cannot_read_test(tmp_path, monkeypatch, task_type):
    from study.practice.build_cards import write_json, write_npz
    from study.practice.guard import check_in_fold
    from study.practice.models import CorrelationScreen
    from study.practice.pipelines import compute_table, replay

    rng = np.random.default_rng(61)
    bundle = tmp_path / 'bundle'
    write_json(bundle / 'task.json', dict(task_type=task_type))
    for i, split in enumerate(('train', 'validation', 'test')):
        x = rng.normal(size=(60, 5))
        y = (x[:, 0] > 0).astype(int) if task_type == 'classification' else x[:, 0] + x[:, 1] ** 2
        write_npz(bundle / f'{split}.npz', x=x, y=y, ids=np.arange(60) + i * 60)
    specs = [dict(id='base', family='linear', complexity=0, pipelines=['A0', 'A1', 'A2']),
             dict(id='screen', family='kernel', screen=True, complexity=1, pipelines=['A3'])]
    fitted_rows = []
    original = CorrelationScreen.fit

    def recording(self, x, y=None):
        fitted_rows.append(len(x))
        return original(self, x, y)

    monkeypatch.setattr(CorrelationScreen, 'fit', recording)
    first = compute_table(bundle, bundle / 'test.npz', bundle / 'test.npz', specs=specs)
    assert fitted_rows == [48] * 5 + [60]
    assert check_in_fold(first['in_fold_trace'])
    assert all(len(row['fold_scores']) == 5 for row in first['rows'])
    with np.load(bundle / 'test.npz') as data:
        write_npz(bundle / 'changed.npz', y=rng.permutation(data['y']), ids=data['ids'])
    second = compute_table(bundle, bundle / 'test.npz', bundle / 'changed.npz', specs=specs)
    assert first['choices_before_test'] == second['choices_before_test']
    # Replay must be possible without fitting any estimator or reading any data file.
    from study.practice.models import Recipe
    monkeypatch.setattr(Recipe, 'fit', lambda *args, **kwargs: pytest.fail('replay refitted'))
    assert replay(first) == replay(first)


def test_different_seed_changes_data_and_unknown_generator_is_refused(tmp_path):
    entry = registry()[0]
    assert not np.array_equal(generate(entry, 0).splits['train']['x'], generate(entry, 1).splits['train']['x'])
    with pytest.raises(ValueError, match='unknown generator'):
        build(tmp_path / 'bad', generator_ids=['absent'], instances=1)


def test_selection_replay_uses_frozen_complexity_order_without_refitting():
    from study.practice.selection_replay import replay
    rows = []
    # The neural recipe has the earlier index. A paired near-tie must still prefer linear.
    for i, family in enumerate(('neural', 'linear')):
        rows.append(dict(spec=dict(id=str(i), family=family, complexity=i, pipelines=['A0', 'A1', 'A2', 'A3']),
                         fold_scores=[0.8, 0.9, 0.7, 0.9, 0.8], cv_score=0.82, validation_score=0.8,
                         test_score=0.8, paired_baseline=dict(lower95=0)))
    result = replay(dict(rows=rows))
    assert result['ablations']['A3/band=1/prior=fixed']['recipe'] == '1'
    assert result['pipeline_C'] == 'not implemented in step 1'


def test_bundle_audit_rejects_planted_heldout_array(tmp_path):
    from study.practice.build_cards import write_npz
    build(tmp_path / 'cards', generator_ids=['gen-001'], instances=1)
    path = tmp_path / 'cards/bundles/P0001/train.npz'
    with np.load(path) as values:
        arrays = dict(values)
    write_npz(path, **arrays, test_labels=np.array([1]))
    with pytest.raises(ValueError, match='non-visible arrays'):
        audit_bundles(tmp_path / 'cards')


def test_gate_refuses_an_interpreter_that_disagrees_with_the_bundle_lock(tmp_path, monkeypatch):
    from study.practice.run_gate import run
    build(tmp_path / 'cards', generator_ids=['gen-001'], instances=1)
    monkeypatch.setattr('importlib.metadata.version', lambda name: 'wrong-version')
    with pytest.raises(ValueError, match='practice gate requires numpy=='):
        run(tmp_path / 'cards', tmp_path / 'evidence')


def test_fresh_supplied_baseline_need_not_emit_a_recipe_receipt(tmp_path, monkeypatch):
    from study.practice.build_cards import write_npz
    from study.practice.run_gate import fresh_recipe

    bundle, controller = tmp_path / 'bundle', tmp_path / 'controller'
    for split in ('train', 'validation'):
        write_npz(bundle / f'{split}.npz', x=np.array([[-1], [1]]), y=np.array([0, 1]), ids=np.array([1, 2]))
    (bundle / 'project.py').write_text('# supplied fixture baseline\n')
    write_npz(controller / 'test_inputs.npz', x=np.array([[-2], [2]]), ids=np.array([3, 4]))
    write_npz(controller / 'test_labels.npz', y=np.array([0, 1]), ids=np.array([3, 4]))

    def candidate_command(workspace, command, timeout):
        assert timeout == 120
        output = Path(command[command.index('--out') + 1])
        if command[2] == 'train':
            output.mkdir(parents=True)
            (output / 'model.pt').write_bytes(b'fixture')
            write_npz(output / 'validation_predictions.npz', ids=np.array([1, 2]), predictions=np.array([0, 1]))
        else:
            with np.load(command[command.index('--inputs') + 1]) as values:
                write_npz(output, ids=values['ids'], predictions=(values['x'][:, 0] > 0).astype(int))

    monkeypatch.setattr('study.common.research_python', lambda: 'fixture-python')
    monkeypatch.setattr('study.fresh_check.candidate_command', candidate_command)
    result = fresh_recipe(tmp_path / 'workspace', bundle, controller,
                          dict(spec={'supplied': True}, validation_predictions=[0, 1]), 'classification', 0.5, 0.02)
    assert result['fresh_process_equal'] and result['export_eligible']
    assert len(result['receipts']) == 3
    assert all(receipt['fit_seconds'] is None for receipt in result['receipts'])
