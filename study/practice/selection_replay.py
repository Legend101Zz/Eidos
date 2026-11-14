"""Selection-only ablations over immutable cached scores; no fitting or data access."""
import json

import numpy as np

from study.practice.build_cards import ROOT
from study.practice.pipelines import replay as base_replay


def replay(table):
    result = base_replay(table)
    constants = json.loads((ROOT / 'docs/experiments/practice-family/MANIFEST.json').read_text())['constants']
    order = constants['complexity_order']
    ranks = {name: i for i, name in enumerate(order)}
    aliases = dict(knn='neighbours', svm='kernel', gbm='trees', boosting='trees', mlp='neural')
    ranks.update({alias: ranks[family] for alias, family in aliases.items()})
    rng = np.random.default_rng(constants['partition_seed'])
    permuted = dict(zip(order, map(int, rng.permutation(len(order))), strict=True))
    permuted.update({alias: permuted[family] for alias, family in aliases.items()})
    ablations = {}
    for pipeline in ('A1', 'A2', 'A3'):
        rows = [r for r in table['rows'] if pipeline in r['spec']['pipelines'] and not r.get('error')]
        best = max(rows, key=lambda row: (row['cv_score'], -row['spec']['complexity']))
        for band in (0, 0.5, 1, 1.5, 2):
            eligible = []
            for row in rows:
                difference = np.asarray(best['fold_scores']) - row['fold_scores']
                se = float(difference.std(ddof=1) / np.sqrt(len(difference)))
                if float(difference.mean()) <= band * se + 1e-12:
                    eligible.append(row)
            for prior, prior_order in [('fixed', ranks), ('permuted', permuted), ('none', {})]:
                choice = min(eligible, key=lambda r: (prior_order.get(r['spec']['family'], -r['cv_score']), r['spec']['complexity']))
                ablations[f'{pipeline}/band={band}/prior={prior}'] = dict(
                    recipe=choice['spec']['id'], test_score=choice['test_score'],
                    selection_regret=max(r['test_score'] for r in rows) - choice['test_score'],
                    optimism=choice['cv_score'] - choice['test_score'])
    result['ablations'] = ablations
    result['ablation_status'] = 'descriptive replay only; constants frozen before scoring; no holdout tuning'
    return result
