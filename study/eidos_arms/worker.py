"""Installed-interpreter entry point used by the controller's two dispatcher hooks."""
import argparse
import json
import time
import traceback
from datetime import datetime
from pathlib import Path

from .common import utc, write_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--arm', type=int, choices=(3, 4), required=True)
    p.add_argument('--run-dir', type=Path, required=True)
    a = p.parse_args()
    started, t0 = utc(), time.monotonic()
    a.run_dir.mkdir(parents=True, exist_ok=True)
    meta = json.loads((a.run_dir / 'arm.json').read_text())
    if int(meta['arm']) != a.arm:
        raise ValueError('arm.json disagrees with dispatcher')
    record = {k: meta.get(k) for k in ('arm', 'card', 'run_index', 'block_id')}
    record.update(started_at=started, incident_reason=None, billed_usd=None,
                  tokens={k: 0 for k in ('input', 'cache_read', 'cache_write', 'output', 'total')},
                  model_ids_seen=[], agent_claim='', transcript='transcript.jsonl')
    try:
        from . import lab
        record.update(eidos_commit_sha=lab.build_record()['eidos_commit_sha'],
                      eidos_build=lab.build_record(), lab_home=str(lab.home()), llm_broker_enabled=False)
        if a.arm == 4:
            from .arm4 import run
        else:
            from .arm3 import run
        record.update(run(a.run_dir, meta))
    except Exception as exc:
        # Tracebacks contain no credential values: credentials are never passed as args.
        (a.run_dir / 'worker_error.txt').write_text(traceback.format_exc())
        record.update(status='incident', incident_reason=f'{type(exc).__name__}: {exc}')
    finally:
        if a.arm == 4:
            setup = a.run_dir / 'setup.json'
            record.update(json.loads(setup.read_text()) if setup.exists() else {
                'setup_started_at': started, 'setup_ended_at': utc(),
                'setup_wall_s': round(time.monotonic() - t0, 3), 'setup_complete': False})
        (a.run_dir / 'transcript.jsonl').touch(exist_ok=True)
        if (a.run_dir / 'lab_policy.json').exists():
            record['llm_broker_policy'] = json.loads((a.run_dir / 'lab_policy.json').read_text())
            try:
                c = lab.client()
                try:
                    policy = c.query('get_profile', {}, project=record['llm_broker_policy']['project'])['body']['policy']
                finally:
                    c.close()
                record['llm_broker_final_policy'] = policy
                record['llm_broker_enabled'] = policy.get('llm_broker', {}).get('enabled') is not False
                if policy.get('llm_broker', {}).get('enabled') is not False:
                    raise RuntimeError('component LLM broker became enabled during the run')
            except Exception as exc:
                record.update(status='incident', incident_reason=f'final policy check: {exc}')
        if (a.run_dir / 'charter.json').exists():
            record['charter'] = json.loads((a.run_dir / 'charter.json').read_text())
        ended = utc()
        wall = (datetime.fromisoformat(ended) - datetime.fromisoformat(record['started_at'])).total_seconds()
        record.update(ended_at=ended, wall_s=round(wall, 3), adapter_wall_s=round(time.monotonic() - t0, 3))
        write_json(a.run_dir / 'run_record.json', record)
    print(json.dumps(record))
    return 0 if record['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
