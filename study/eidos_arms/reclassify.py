"""Re-apply the current arm-4 run-status rule to a finished pilot block (pilot-3 addendum 2).

Recomputes arm 4's status from its saved records with ``classify``, keeps the previous values under
``*_before_reclassification``, and rebuilds the block status with the launcher's rule. Arms 1-3 are not
re-classified; only the block-level incident override is undone or kept accordingly.
Usage: python -m study.eidos_arms.reclassify <block_dir> [--write]
"""
import json, sys
from pathlib import Path

from study.eidos_arms.common import classify


def main(block_dir, write=False):
    block_dir = Path(block_dir)
    run4 = block_dir / 'arm4'
    rec4 = json.loads((run4 / 'run_record.json').read_text())
    shown = json.loads((run4 / 'investigation_latest.json').read_text())
    usage = json.loads((run4 / 'usage_receipts.json').read_text())
    timed_out = rec4.get('pre_block_status', rec4['status']) == 'timeout'
    valid = (run4 / 'out' / 'answer.json').exists() and not (run4 / 'validation_error.json').exists()
    status, incident = classify(shown, usage['receipts'], usage['model_ids_seen'], timed_out, valid)
    records = {a: json.loads((block_dir / f'arm{a}' / 'run_record.json').read_text()) for a in (1, 2, 3, 4)}
    own = {a: r.get('pre_block_status', r['status']) for a, r in records.items()}
    own[4] = status
    reasons = {a: records[a].get('incident_reason') for a in (1, 2, 3)}
    block_incident = next((reasons[a] for a in (1, 2, 3) if own[a] == 'incident'), None) or (incident if status == 'incident' else None)
    print(json.dumps({'block': block_dir.name, 'arm4_before': rec4.get('pre_block_status', rec4['status']), 'arm4_after': status,
                      'own_status': own, 'block_status': 'incident' if block_incident else 'ok'}))
    if not write:
        return
    for a, r in records.items():
        r['status_before_reclassification'] = r['status']
        r['incident_reason_before_reclassification'] = r.get('incident_reason')
        r['reclassified_by'] = 'pilot-3 addendum 2'
        if block_incident:
            r['pre_block_status'] = own[a]; r['status'] = 'incident'; r['incident_reason'] = block_incident
        else:
            r['status'] = own[a]; r['pre_block_status'] = own[a]
            r['incident_reason'] = incident if (a == 4 and status == 'incident') else (
                ('investigator ended without a conclusion or requires operator input' if a == 4 and status == 'crash' else None))
        (block_dir / f'arm{a}' / 'run_record.json').write_text(json.dumps(r, indent=2, sort_keys=True) + '\n')
    br_path = block_dir / 'block_record.json'
    br = json.loads(br_path.read_text())
    br['status_before_reclassification'] = br['status']; br['incident_reason_before_reclassification'] = br.get('incident_reason')
    br['status'] = 'incident' if block_incident else 'ok'; br['incident_reason'] = block_incident
    br['reclassified_by'] = 'pilot-3 addendum 2'
    br_path.write_text(json.dumps(br, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main(sys.argv[1], '--write' in sys.argv[2:])
