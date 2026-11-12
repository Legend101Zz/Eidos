"""Prospective-card boundaries: no evaluator material in investigator bundles."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from study.common import FRESH_RECIPE_CARDS, IMPROVEMENT_CARDS, render_prompt
from study.eidos_arms.charters import charter
from study.grade_block import success_rule
from study.launcher import plan
from study.packaging.package_card import package
from study.packaging.p3_data import CLASSIFICATION, FROZEN, TASKS, card_spec, dataset
from study.packaging.qualify_p3 import array_hash


class Pilot3(unittest.TestCase):
    def test_generator_matches_frozen_qualification_and_disjoint_splits(self):
        frozen = json.loads(FROZEN.read_text())
        for card in TASKS:
            data, kind, _ = dataset(card)
            seen = set()
            for split, arrays in data.items():
                self.assertEqual(len(arrays[1]), frozen[card]['splits'][split]['n'])
                for name, array in zip(('x', 'y', 'ids'), arrays):
                    self.assertEqual(array_hash(array), frozen[card]['splits'][split]['hashes'][name])
                self.assertFalse(seen.intersection(arrays[2]))
                seen.update(arrays[2])
            self.assertEqual(seen, set(range(sum(len(d[1]) for d in data.values()))))
            bar = .02 if kind == 'classification' else .05 * frozen[card]['baseline']
            self.assertEqual(bar, card_spec(card)['min_reference_gain'])
            self.assertTrue(all(r['passes'] for r in frozen[card]['references'].values()))

    def test_bundle_allowlist_and_no_test_rows(self):
        for card in TASKS:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = package(card, root)
                self.assertEqual({p.name for p in root.iterdir()},
                                 {'brief.md', 'project.py', 'train.npz', 'validation.npz',
                                  'requirements.lock', 'task.json', 'answer_schema.py'})
                self.assertEqual((task['metric'], task['direction']),
                                 ('accuracy', 'maximize') if card in CLASSIFICATION else ('rmse', 'minimize'))
                for name, expected in task['hashes'].items():
                    self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)
                test_ids = set(dataset(card)[0]['test'][2])
                for split in ('train', 'validation'):
                    with np.load(root / (split + '.npz'), allow_pickle=False) as d:
                        self.assertEqual(set(d.files), {'x', 'y', 'ids'})
                        self.assertFalse(test_ids.intersection(d['ids']))
                with self.assertRaises(ValueError):
                    package(card, root)

    def test_study_paths_and_rules(self):
        design = plan(20261111, list(TASKS), 3)
        self.assertEqual(len(design), 48)
        self.assertTrue(all(r['primary_eligible'] for r in design))
        for card in TASKS:
            self.assertIn(card, IMPROVEMENT_CARDS)
            self.assertIn(card, FRESH_RECIPE_CARDS)
            spec = card_spec(card)
            bar = spec['min_reference_gain']
            answer = dict(status='improvement_claimed', claim_type='recipe_improvement')
            self.assertTrue(success_rule(spec, answer, dict(gain=bar), True, True, True)['verified_success'])
            for args in [(bar-1e-6, True, True, True), (bar, False, True, True),
                         (bar, True, False, True), (bar, True, True, False)]:
                gain, *flags = args
                self.assertFalse(success_rule(spec, answer, dict(gain=gain), *flags)['verified_success'])
            for arm in (1, 2, 3, 4):
                self.assertIn(str(bar), render_prompt(arm, card))
            c = charter(card)
            if card in CLASSIFICATION:
                self.assertFalse(c['exploration_only'])
                self.assertEqual(c['success_rules'][0]['metric'], 'classification-report/1:accuracy')
                self.assertEqual(c['success_rules'][0]['min_effect'], .02)
            else:
                self.assertTrue(c['exploration_only'])
                self.assertIn('No built-in RMSE assessor', c['notes'])
                self.assertIn(str(bar), c['question'])


if __name__ == '__main__':
    unittest.main()
