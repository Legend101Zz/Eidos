"""Audit structured visible claims without writing claims on an investigator's behalf."""
import argparse
import json
import re
from pathlib import Path

BANNED = re.compile(r'\b(?:confirmed|proven|guaranteed|held.out improvement)\b', re.IGNORECASE)


def audit(claim, evidence):
    problems = []
    if BANNED.search(claim.get('text', '')):
        problems.append('unsupported confirmation wording')
    refs = claim.get('measurements', [])
    stated_values = {float(m['value']) for m in refs if isinstance(m.get('value'), (int, float))}
    for token in re.findall(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', claim.get('text', '')):
        if float(token) not in stated_values:
            problems.append('untraceable number in claim text')
    for measurement in refs:
        source = evidence.get(measurement.get('evidence_ref'))
        if source is None or source.get(measurement.get('metric')) != measurement.get('value'):
            problems.append('untraceable measurement')
    if claim.get('status') == 'improvement_claimed':
        paired = evidence.get(claim.get('paired_evidence_ref'), {})
        if paired.get('lower95', 0) <= 0 or paired.get('split') != 'validation':
            problems.append('improvement requires a paired visible validation interval excluding zero')
        if not refs:
            problems.append('improvement requires traceable measurements')
    return dict(ok=not problems, problems=problems)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claim', required=True)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    result = audit(json.loads(Path(args.claim).read_text()), json.loads(Path(args.evidence).read_text()))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
