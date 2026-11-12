"""Authored hosted/local grader controls; no model calls or comparative pilot runs."""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from study.common import read_json, research_python, write_json
from study.grade_block import digest, grade_run
from study.launcher import prepare
from study.stub_agent import stub


def main(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for card in ('N01', 'N03'):
        for case in ('reference', 'baseline', 'invalid'):
            run = root / card / case
            prepare(dict(arm=1, task=card, run=1, block_id=f'control-{card}-{case}'), run)
            stub(run)
            out = run / 'out'
            answer = read_json(out / 'answer.json')
            with np.load(run / 'task/validation.npz', allow_pickle=False) as d:
                truth = d['y']
            def measure():
                with np.load(out / 'validation_predictions.npz', allow_pickle=False) as p:
                    predicted = p['predictions']
                return float(np.mean(truth == predicted)) if card == 'N01' else float(np.sqrt(np.mean((truth.astype(np.float64) - predicted.astype(np.float64))**2)))
            baseline_validation = measure()
            if case == 'reference':
                shutil.copyfile(Path(__file__).with_name('reference_p3.py'), out / 'project.py')
                subprocess.run(['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny network*)',
                                research_python(), str(out / 'project.py'), 'train',
                                '--inputs', str(run / 'task'), '--out', str(out), '--seed', '11'],
                               check=True, timeout=120)
            answer.update(status='improvement_claimed', claim_type='recipe_improvement',
                          claim=f'Authored {case} grader qualification control; not a pilot outcome.',
                          exposures=['Reference-assisted authored grader qualification control'])
            for path_key, sha_key in (('code_path', 'code_sha256'), ('model_path', 'model_sha256'),
                                      ('predictions_path', 'predictions_sha256')):
                answer['candidate'][sha_key] = digest(out / answer['candidate'][path_key])
            answer['evidence'][0]['sha256'] = digest(out / 'receipt.json')
            candidate_validation = measure()
            write_json(out / 'control-validation.json', dict(baseline=baseline_validation,
                        candidate=candidate_validation, n=len(truth), split='validation'))
            answer['evidence'].append(dict(path='control-validation.json',
                         sha256=digest(out / 'control-validation.json'), role='validation',
                         supports='Measured control validation metric', limitations='Development validation only'))
            answer['measurements'] = [dict(metric='accuracy' if card == 'N01' else 'rmse',
                        direction='higher_is_better' if card == 'N01' else 'lower_is_better',
                        split='validation', denominator=len(truth), units='fraction' if card == 'N01' else 'target units',
                        baseline=baseline_validation, candidate=candidate_validation,
                        uncertainty_method='Authored control; independent test scoring follows.',
                        evidence_receipt='control-validation.json')]
            if case == 'invalid':
                answer['candidate']['model_sha256'] = '0' * 64
            write_json(out / 'answer.json', answer)
            result = grade_run(run, root / 'local-grading' / card / case)
            results.append(dict(card=card, case=case, **result))
            assert result['status'] == ('invalid_output' if case == 'invalid' else 'valid'), result
            assert result['verified_success'] == (case == 'reference'), result
            if case == 'baseline':
                assert result['numeric']['gain'] == 0 and result['false_positive_claim'], result
            write_json(root / 'local-results.json', results)
    print('PASS: N01/N03 reference passes, claimed baseline fails at zero gain, invalid output is a candidate failure')


if __name__ == '__main__':
    main(sys.argv[1])
