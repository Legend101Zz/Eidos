"""Step-1 evidence: references, cold recomputation, real controls and measured power."""
import argparse
import json
import statistics
from pathlib import Path

from study.practice.build_cards import ROOT, write_json


def reference_evidence(family, references):
    family_generators = {r['generator'] for r in family['cards']}
    named = {r['generator'] for r in family['cards'] if r['role'] not in ('control', 'calibration-only')}
    holdouts = {r['generator'] for r in family['cards'] if r['role'] in ('holdout', 'holdout-class')}
    near, headroom, comparisons = [], [], []
    for g in sorted(named):
        rows = [r for r in family['cards'] if r['generator'] == g]
        ref = references['generators'].get(g)
        if not ref or ref.get('headroom') is None:
            continue
        tolerance = max(2 * ref['reference_uncertainty_se'],
                        2 * statistics.mean(r['pipelines']['A1']['test_se'] for r in rows))
        close_margin = .02 if ref['reference_score'] >= 0 else .05 * max(abs(ref['reference_score']), .1)
        if abs(ref['headroom']) <= min(tolerance, close_margin):
            near.append(g)
        if ref['headroom'] > tolerance:
            headroom.append(g)
        if statistics.mean(r['pipelines']['A1']['test_score'] - r['pipelines']['A3']['test_score'] for r in rows) >= 0:
            comparisons.append(g)
    return dict(passed=bool(family_generators <= set(references['generators']) and len(near) >= 3
                            and len(set(headroom) & holdouts) >= 2 and len(comparisons) >= 3
                            and len(set(comparisons) & holdouts) >= 2),
                near_reference_generators=near, measurable_headroom_generators=headroom,
                holdout_headroom=sorted(set(headroom) & holdouts), A1_at_least_A3_generators=comparisons,
                rule='All generators referenced; >=3 named near attainable reference (within 2 SE and an absolute .02 accuracy / 5% RMSE margin); >=2 holdout generators with >2 SE reference headroom; A1 mean >= A3 on >=3 named including >=2 holdouts. No single easy-card ceiling is sufficient.')


def determinism_evidence(family, rerun, cold, rebuilt_data_digest):
    generators = {r['generator'] for r in family['cards']}
    covered = {r['generator'] for r in cold['instances'] if r['passed'] and not r['cache_hit']}
    return dict(passed=bool(family['semantic_digest'] == rerun['semantic_digest']
                            and family['data_digest'] == rebuilt_data_digest
                            and generators <= covered and cold.get('all_met') is True
                            and all(r['passed'] and r.get('source_digest_equal') for r in cold['instances'])),
                first=family['semantic_digest'], rerun=rerun['semantic_digest'],
                first_data=family['data_digest'], rebuilt_data=rebuilt_data_digest,
                cold_instances=len(cold['instances']), cold_generators=sorted(covered),
                comparison='Every recipe: fold scores, CV, validation, test, OOF digest; frozen choices and paired test effect. At least one cold card per generator, in a separate process and cache directory.')


def produce(family, rerun, development, out, *, rebuilt_data_digest, references, cold, operating, controls):
    constants = json.loads((ROOT / 'docs/experiments/practice-family/MANIFEST.json').read_text())['constants']
    calibration = {}
    for card, target in constants['calibration'].items():
        row = next(r for r in development['cards'] if r['card'] == card)
        delta = row['pipelines']['A3']['test_score'] - row['pipelines']['A1']['test_score']
        if card == 'N01':
            delta *= 114
        calibration[card] = dict(measured=delta, **target, passed=abs(delta - target['target']) <= target['tolerance'])
    wall = [dict(card=r['card'], seconds=r['measurement_seconds']) for r in family['cards']]
    criteria = {
        'a_calibration': dict(passed=all(v['passed'] for v in calibration.values()), measurements=calibration),
        'b_references_and_headroom': reference_evidence(family, references),
        'c_cold_digest_stability': determinism_evidence(family, rerun, cold, rebuilt_data_digest),
        'd_real_controls_and_power': dict(passed=controls['all_met'] and operating['targets_met'],
                                          controls={k: v['verdict'] for k, v in controls['controls'].items()},
                                          leak=controls['leak'], monte_carlo_targets_met=operating['targets_met']),
        'e_instance_wall_within_twice_estimate': dict(passed=all(r['seconds'] <= constants['wall_limit_seconds'] for r in wall),
                                                     estimate_seconds=constants['wall_estimate_seconds'], limit_seconds=constants['wall_limit_seconds'], instances=wall),
    }
    result = dict(schema='v2-step1-acceptance/2', criteria=criteria, all_met=all(v['passed'] for v in criteria.values()),
                  family_instances=len(family['cards']), development_instances=len(development['cards']),
                  scoring_source_digest=family['source_digest'], model_calls=0, provider_calls=0,
                  limitation='Offline harness evidence. A3 is an EDA proxy, not a coding-agent ceiling. Pipeline C and the investigator remain unevaluated. Positive fault-repair controls are distinct from the +0.8-SE Monte Carlo power target.')
    out = Path(out)
    write_json(out / 'acceptance.json', result)
    lines = ['# V2 step 1 r2 acceptance', '', f"Family: {len(family['cards'])} cards; development: {len(development['cards'])}.", '']
    lines += [f"- {'PASS' if v['passed'] else 'NOT MET'} — {k}" for k, v in criteria.items()]
    lines += ['', '| Variance profile | Primaries | Neutral accept | Harmful accept | Card-specific accept | Helpful accept |',
              '|---|---:|---:|---:|---:|---:|']
    profiles = list(dict.fromkeys((r['profile'], r['primaries']) for r in operating['rows']))
    for profile, primaries in profiles:
        rates = {r['scenario']: r['acceptance_rate'] for r in operating['rows'] if r['profile'] == profile and r['primaries'] == primaries}
        lines.append(f"| {profile} | {primaries} | {rates['neutral']:.2%} | {rates['harmful']:.2%} | {rates['card-specific']:.2%} | {rates['helpful']:.2%} |")
    lines += ['', operating['effect_justification'], '', operating['scope'], '', result['limitation']]
    (out / 'SUMMARY.md').write_text('\n'.join(lines) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('family', 'rerun', 'development', 'out', 'rebuilt-data-digest', 'references', 'cold', 'operating', 'controls'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    def read(p):
        return json.loads(Path(p).read_text())
    result = produce(read(args.family), read(args.rerun), read(args.development), args.out,
                     rebuilt_data_digest=args.rebuilt_data_digest, references=read(args.references), cold=read(args.cold),
                     operating=read(args.operating), controls=read(args.controls))
    print(json.dumps(dict(all_met=result['all_met'])))
    raise SystemExit(0 if result['all_met'] else 1)


if __name__ == '__main__':
    main()
