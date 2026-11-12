"""Independent cold score recomputation for at least one instance of every generator."""
import argparse
import json
from pathlib import Path

from study.practice.build_cards import build, write_json
from study.practice.run_gate import run


def score_projection(table):
    fields = ('spec', 'error', 'fold_scores', 'cv_score', 'validation_score', 'validation_predictions',
              'test_score', 'test_se', 'oof_score', 'oof_digest', 'paired_baseline')
    return dict(choices=table['choices_before_test'], paired=table['comparator_paired'],
                rows=[{key: row[key] for key in fields if key in row} for row in table['rows']])


def produce(family_path, first_gate, out):
    out = Path(out)
    if (out / 'gate/cache').exists():
        raise ValueError('cold evidence requires a new cache directory')
    family = json.loads(Path(family_path).read_text())
    build(out / 'data', instances=1)
    second = run(out / 'data', out / 'gate')
    return compare(family, first_gate, second, out)


def compare(family, first_gate, second, out):
    out = Path(out)
    instances = []
    for card in second['cards']:
        first = next(r for r in family['cards'] if r['card'] == card['card'])
        a = json.loads((Path(first_gate) / 'cache' / f"{first['cache_key']}.json").read_text())
        b = json.loads((out / 'gate/cache' / f"{card['cache_key']}.json").read_text())
        instances.append(dict(card=card['card'], generator=card['generator'], cache_hit=card['cache_hit'],
                              passed=score_projection(a) == score_projection(b),
                              recipes=len(a['rows']), source_digest_equal=a['source_digest'] == b['source_digest']))
    result = dict(schema='practice-cold-recompute/2', instances=instances,
                  all_met=all(r['passed'] and r['source_digest_equal'] and not r['cache_hit'] for r in instances))
    write_json(out / 'cold-recompute.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', required=True)
    parser.add_argument('--first-gate', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(json.dumps(produce(args.family, args.first_gate, args.out)))


if __name__ == '__main__':
    main()
