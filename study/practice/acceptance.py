"""Produce the five step-1 acceptance verdicts from actual gate receipts."""
import argparse
import json
from pathlib import Path

from study.practice.build_cards import ROOT, write_json
from study.practice.guard import guard_selftest


def produce(family, rerun, development, out, *, rebuilt_data_digest):
    manifest = json.loads((ROOT / 'docs/experiments/practice-family/MANIFEST.json').read_text())
    constants = manifest['constants']
    calibration = {}
    for card, target in constants['calibration'].items():
        result = next(row for row in development['cards'] if row['card'] == card)
        delta = result['pipelines']['A3']['test_score'] - result['pipelines']['A1']['test_score']
        if card == 'N01':
            delta *= 114
        calibration[card] = dict(measured=delta, **target, passed=abs(delta - target['target']) <= target['tolerance'])
    near_reference = [row['card'] for row in family['cards']
                      if row['generator'] == 'gen-002' and row['pipelines']['A1']['test_score'] >= 0.99
                      and row['pipelines']['A1']['test_score'] >= row['pipelines']['A3']['test_score']]
    comparisons = [row['card'] for row in family['cards']
                   if row['pipelines']['A1']['test_score'] >= row['pipelines']['A3']['test_score']]
    controls = guard_selftest()
    expected = {'card_specific_patch': 'design-only', 'harmful_operator': 'reject-no-harm', 'screen_before_cv': 'reject-in-fold'}
    wall = [dict(card=row['card'], seconds=row['measurement_seconds']) for row in family['cards']]
    stable = family['semantic_digest'] == rerun['semantic_digest'] and family['data_digest'] == rebuilt_data_digest
    criteria = {
        'a_calibration': dict(passed=all(item['passed'] for item in calibration.values()), measurements=calibration),
        'b_near_reference_and_not_uniformly_below': dict(passed=bool(near_reference and comparisons),
                                                         near_reference_cards=near_reference, A1_at_least_A3=comparisons,
                                                         reference='accuracy ceiling 1.0 on easy radial generator; A1 >= .99 and >= A3'),
        'c_digest_stable_rerun': dict(passed=stable, first=family['semantic_digest'], rerun=rerun['semantic_digest'],
                                     first_data=family['data_digest'], rebuilt_data=rebuilt_data_digest,
                                     all_cache_hits=all(row['cache_hit'] for row in rerun['cards'])),
        'd_selftest_controls_say_no': dict(passed=controls == expected, controls=controls),
        'e_instance_wall_within_twice_estimate': dict(passed=all(item['seconds'] <= constants['wall_limit_seconds'] for item in wall),
                                                     estimate_seconds=constants['wall_estimate_seconds'],
                                                     limit_seconds=constants['wall_limit_seconds'], instances=wall),
    }
    result = dict(schema='v2-step1-acceptance/1', criteria=criteria, all_met=all(x['passed'] for x in criteria.values()),
                  family_instances=len(family['cards']), development_instances=len(development['cards']),
                  scoring_source_digest=family['source_digest'], model_calls=0, provider_calls=0,
                  limitation='Offline development evidence only; pipeline C, the investigator and a prospective pilot are not evaluated.')
    write_json(Path(out) / 'acceptance.json', result)
    lines = ['# V2 step 1 acceptance', '', f"Family: {len(family['cards'])} instances; development calibration: {len(development['cards'])} cards.", '']
    lines += [f"- {'PASS' if value['passed'] else 'NOT MET'} — {key}" for key, value in criteria.items()]
    lines += ['', f"N04 A3−A1: {calibration['N04']['measured']:.6f} accuracy; N01: {calibration['N01']['measured']:.3f} test examples.",
              f"Maximum first-run instance wall: {max(item['seconds'] for item in wall):.3f} s (limit 840 s).", '', result['limitation']]
    (Path(out) / 'SUMMARY.md').write_text('\n'.join(lines) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('family', 'rerun', 'development', 'out', 'rebuilt-data-digest'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    result = produce(*(json.loads(Path(path).read_text()) for path in (args.family, args.rerun, args.development)),
                     args.out, rebuilt_data_digest=args.rebuilt_data_digest)
    print(json.dumps(dict(all_met=result['all_met'])))
    raise SystemExit(0 if result['all_met'] else 1)


if __name__ == '__main__':
    main()
