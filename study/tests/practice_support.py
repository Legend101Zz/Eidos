"""Isolated candidate receipts and explicitly indexed design scoring fixtures."""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from study.practice import guard, pipelines
from study.practice.build_cards import tree_digest, write_json
from study.practice.ledger import register


def registration(classes):
    classes = sorted(classes)
    identity = hashlib.sha256(json.dumps(classes).encode()).hexdigest()
    return register(evaluation_id=identity, candidate_digest='test-fixture', classes=classes,
                    primary_verdict='test-only inference', kind='candidate')


def evaluate(effects, roles, **options):
    return guard.evaluate(effects, roles, registration=registration(roles), **options)


def indexed_fixture(bundle, controller):
    root = Path(tempfile.mkdtemp(prefix='practice-test-index-', dir=Path(bundle).parent))
    target = root / 'bundles' / Path(bundle).name
    oracle = root / 'controller' / Path(bundle).name
    shutil.copytree(bundle, target)
    shutil.copytree(controller, oracle)
    write_json(root / 'index.json', dict(cards=[dict(card=target.name, generator='gen-001', role='design',
               bundle_digest=tree_digest(target), controller_digest=tree_digest(oracle))]))
    receipt = register(evaluation_id=str(root), candidate_digest='test-fixture', classes=['gen-001'],
                       primary_verdict='indexed design fixture', kind='construction', data_root=root)
    return target, oracle, receipt


def compute_table(bundle, test_inputs, test_labels, **options):
    # Tests supply tiny fixtures. Give those bytes an explicit design index;
    # production entry points never infer a role from an unindexed directory.
    temporary = Path(tempfile.mkdtemp(prefix='practice-test-oracle-', dir=Path(bundle).parent))
    shutil.copyfile(test_inputs, temporary / 'test_inputs.npz')
    shutil.copyfile(test_labels, temporary / 'test_labels.npz')
    target, oracle, receipt = indexed_fixture(bundle, temporary)
    return pipelines.compute_table(target, oracle / 'test_inputs.npz', oracle / 'test_labels.npz', registration=receipt, **options)


def guard_selftest():
    return guard.guard_selftest(registration=registration([f'g{i}' for i in range(12)]))
