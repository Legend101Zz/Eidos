"""Pipeline C controls cannot select by test outcomes or bypass the frozen guard."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from study.practice import pipeline_c


class PipelineCContracts(unittest.TestCase):
    def test_replay_ignores_test_scores_and_permutation_is_deterministic(self):
        rows = [dict(id='best', scores=[.9, .7, .9, .7], mean=.8, complexity=9,
                     export_eligible=True, status='ok', test_score=0),
                dict(id='simple', scores=[.8, .8, .8, .8], mean=.8, complexity=1,
                     export_eligible=True, status='ok', test_score=1)]
        first = pipeline_c.replay_choices(rows)
        rows[0]['test_score'], rows[1]['test_score'] = 1, 0
        self.assertEqual(first, pipeline_c.replay_choices(rows))
        self.assertEqual(first['paired-1.0/fixed'], 'simple')
        fixed = pipeline_c.replay_choices(rows, multiples=(1.,))
        self.assertEqual(set(fixed), {'argmax-cv', 'paired-1.0/fixed', 'paired-1.0/permuted'})

    def test_controller_scores_use_the_export_executor_float_precision(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundle, controller = root / 'bundle', root / 'controller'
            bundle.mkdir()
            controller.mkdir()
            rng = np.random.default_rng(31)
            x = rng.normal(size=(90, 4)).astype(np.float32)
            y = (x[:, 0] * 2 + x[:, 1]).astype(np.float32)
            for name, sl in [('train', slice(0, 50)), ('validation', slice(50, 70))]:
                np.savez(bundle / f'{name}.npz', x=x[sl], y=y[sl], ids=np.arange(90)[sl], groups=np.arange(90)[sl] // 5)
            np.savez(controller / 'test_inputs.npz', x=x[70:], ids=np.arange(90)[70:])
            np.savez(controller / 'test_labels.npz', y=y[70:], ids=np.arange(90)[70:])
            spec = dict(schema='recipe-spec/1', task_type='regression', steps=[dict(op='standardize')],
                        model=dict(family='ridge', params={}))
            expected = pipeline_c.models.build(spec, 11).fit(x[:50].astype(float), y[:50])
            table = dict(task_type='regression', scheme='cv-train', model_seed=11, partition_seed=81723,
                         selection=dict(chosen_id='r'), validation_check=dict(predictions=expected.predict(x[50:70].astype(float)).tolist()),
                         recipes=[dict(id='r', spec=spec, scores=[-1., -1.], mean=-1., complexity=0,
                                       status='ok', export_eligible=True)])
            build = pipeline_c.models.build
            seen = []
            def recording_build(*args):
                model = build(*args)
                fit = model.fit
                def recording_fit(features, target):
                    seen.append(features.dtype)
                    return fit(features, target)
                model.fit = recording_fit
                return model
            with patch.object(pipeline_c.models, 'build', side_effect=recording_build), patch.object(pipeline_c.cv, 'partitions', wraps=pipeline_c.cv.partitions) as partitions:
                _, (_, prediction), _ = pipeline_c.controller_scores(bundle, controller, table)
            self.assertTrue(seen and all(dtype == np.dtype('float64') for dtype in seen))
            np.testing.assert_array_equal(partitions.call_args.kwargs['groups'], np.arange(50) // 5)
            np.testing.assert_array_equal(prediction, expected.predict(x[70:].astype(float)))

    def test_control_claims_distinguish_null_from_weak_signal_grader_rules(self):
        fresh = dict(fresh_gains=[.03, .03, .03], fresh_process_equal=True, export_eligible=True)
        rows = [dict(generator=g, pipeline_C=dict(visible_claim=True, **fresh),
                     comparators=dict(A2=dict(paired_baseline=dict(lower95=.01))), A2_fresh=fresh)
                for g in ('null', 'weak')]
        result = pipeline_c.control_claims(rows, {'null'})
        for name in ('C', 'A2'):
            self.assertEqual(result[name]['groups']['null']['false_positive_claim_rate'], 1.)
            self.assertEqual(result[name]['groups']['weak_signal']['false_positive_claim_rate'], 0.)
            self.assertEqual(result[name]['false_positive_claim_rate'], .5)

    def test_freeze_refuses_candidate_changes_without_replacing_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(pipeline_c, 'candidate_digest', return_value='first'):
                first = pipeline_c.freeze(root)
            with patch.object(pipeline_c, 'candidate_digest', return_value='second'), self.assertRaisesRegex(ValueError, 'frozen'):
                pipeline_c.freeze(root)
            self.assertEqual(first, json.loads((Path(root) / 'frozen-candidate.json').read_text()))

    def test_holdout_cannot_score_or_burn_before_design_is_finished(self):
        with tempfile.TemporaryDirectory() as root:
            data, out = Path(root) / 'data', Path(root) / 'out'
            data.mkdir()
            (data / 'index.json').write_text(json.dumps(dict(cards=[
                dict(card='P0001', generator='g1', role='design'),
                dict(card='P0002', generator='g2', role='holdout')
            ])))
            with patch.object(pipeline_c, 'register_burn', side_effect=AssertionError('burned early')), self.assertRaisesRegex(ValueError, 'finish design'):
                pipeline_c.run(data, out, stage='holdout')


if __name__ == '__main__':
    unittest.main()
