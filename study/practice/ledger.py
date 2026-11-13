"""Programme-wide append-only authenticated ledger, independent of checkout and --out.

The controller owns this directory. Hash links and a separately pinned head detect
edits, deletions and truncation; they do not defend against a controller account
that can rewrite both the secret and the checkpoint. Never copy these to bundles.
"""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path

LEDGER_ROOT = Path('/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-ledger')
GENESIS = '0' * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def initialize():
    """Explicit controller provisioning only; scoring never recreates a missing ledger."""
    LEDGER_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(LEDGER_ROOT.iterdir()):
        raise ValueError('ledger already provisioned; never reset it from a scoring run')
    for name, content in [('key', os.urandom(32)), ('entries.jsonl', b''), ('head', GENESIS.encode()), ('lock', b'')]:
        fd = os.open(LEDGER_ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())


@contextlib.contextmanager
def locked():
    try:
        with (LEDGER_ROOT / 'lock').open('r+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
    except FileNotFoundError as exc:
        raise ValueError('fixed programme ledger is missing; controller provisioning required') from exc


def _read():
    key = (LEDGER_ROOT / 'key').read_bytes()
    if len(key) != 32:
        raise ValueError('invalid ledger authentication key')
    previous, entries = GENESIS, []
    try:
        for line in (LEDGER_ROOT / 'entries.jsonl').read_bytes().splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise ValueError('truncated ledger entry')
            entry = json.loads(line)
            digest = entry.pop('hash')
            if entry['previous_hash'] != previous or entry['sequence'] != len(entries):
                raise ValueError('ledger chain order mismatch')
            expected = hmac.new(key, canonical(entry), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, digest):
                raise ValueError('ledger chain authentication failed')
            entry['hash'] = digest
            previous = digest
            entries.append(entry)
        if (LEDGER_ROOT / 'head').read_text() != previous:
            raise ValueError('ledger head mismatch: deletion, truncation or incomplete append')
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError('invalid ledger chain') from exc
    return entries


def verify_chain():
    with locked():
        return _read()


def _append(entries, event):
    entry = dict(event, sequence=len(entries), previous_hash=entries[-1]['hash'] if entries else GENESIS)
    entry['hash'] = hmac.new((LEDGER_ROOT / 'key').read_bytes(), canonical(entry), hashlib.sha256).hexdigest()
    # Never rewrite or truncate prior entries. The head is a separate rollback checkpoint.
    with (LEDGER_ROOT / 'entries.jsonl').open('ab') as stream:
        stream.write(canonical(entry) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary = LEDGER_ROOT / 'head.next'
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(entry['hash'])
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(LEDGER_ROOT / 'head')
    return entry


def register(*, evaluation_id, candidate_digest, classes, primary_verdict, kind='candidate'):
    classes = sorted(set(classes))
    if not evaluation_id or not candidate_digest or not primary_verdict or not classes:
        raise ValueError('evaluation ID, frozen candidate digest, generators and primary are required')
    if kind not in ('candidate', 'construction'):
        raise ValueError('unknown registration kind')
    identity = dict(event='registration', evaluation_id=evaluation_id, candidate_digest=candidate_digest,
                    classes=classes, primary_verdict=primary_verdict, kind=kind)
    with locked():
        entries = _read()
        registrations = [e for e in entries if e['event'] == 'registration']
        for entry in registrations:
            if entry['evaluation_id'] == evaluation_id:
                if any(entry[k] != v for k, v in identity.items()):
                    raise ValueError('a registered verdict cannot be changed after scoring')
                return entry
        candidates = [e for e in registrations if e['kind'] == 'candidate']
        if kind == 'candidate':
            # All scored generators are receipt-bound, but exposure burns apply
            # to holdouts. Replacing exhausted holdouts must not be blocked by
            # reusing the unchanged design/calibration set.
            roles = {g['id']: g['role'] for g in json.loads(Path(__file__).with_name('registry.json').read_text())}
            protected = [name for name in classes if roles.get(name, 'holdout') in ('holdout', 'holdout-class')]
            for name in protected:
                if sum(name in e['classes'] for e in candidates) >= 2:
                    raise ValueError('two-burn limit reached; independent replacement and review required')
            if len(candidates) >= 8:
                raise ValueError('at most eight primary verdicts per programme')
        return _append(entries, identity)


def _require(entries, registration, classes=(), *, exact=False):
    if not isinstance(registration, dict) or not registration.get('evaluation_id'):
        raise ValueError('scoring and verdicts require a ledger registration receipt')
    match = next((e for e in entries if e['event'] == 'registration' and e['hash'] == registration.get('hash')), None)
    classes_match = match is not None and (set(classes) == set(match['classes']) if exact else set(classes) <= set(match['classes']))
    if match != registration or not classes_match:
        raise ValueError('registration does not match the ledger candidate and generator set')
    return match


def require_registration(registration, *, classes=(), exact=False):
    with locked():
        return _require(_read(), registration, classes, exact=exact)


def record_verdict(registration, result, *, classes):
    """The sole public verdict emission path: verify, append durably, then return."""
    with locked():
        entries = _read()
        receipt = _require(entries, registration, classes, exact=True)
        event = _append(entries, dict(event='verdict', evaluation_id=receipt['evaluation_id'],
            registration_hash=receipt['hash'], candidate_digest=receipt['candidate_digest'], result=result))
        return dict(result, ledger_event_hash=event['hash'], evaluation_id=receipt['evaluation_id'])
