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
    # Separate append-only checkpoint history, also witnessed by the orchestrator.
    fd = os.open(LEDGER_ROOT / 'HEADS.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'ab') as stream:
        stream.write(f"{entry['sequence']} {entry['hash']}\n".encode())
        stream.flush()
        os.fsync(stream.fileno())
    return entry


def register(*, evaluation_id, candidate_digest, classes, primary_verdict, kind='candidate', data_root=None):
    classes = sorted(set(classes))
    if not evaluation_id or not candidate_digest or not primary_verdict or not classes:
        raise ValueError('evaluation ID, frozen candidate digest, generators and primary are required')
    if kind not in ('candidate', 'construction'):
        raise ValueError('unknown registration kind')
    roles = {g['id']: g['role'] for g in json.loads(Path(__file__).with_name('registry.json').read_text())}
    roles.update({g: 'calibration-only' for g in ('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04')})
    if kind == 'construction' and any(roles.get(g) not in ('design', 'calibration-only', 'control') for g in classes):
        raise ValueError('construction registration may cover only design/calibration/control generators')
    binding = None
    if data_root is not None:
        data_root = Path(data_root).resolve()
        index_path = data_root / 'index.json'
        index = json.loads(index_path.read_text())['cards']
        if set(classes) != {r['generator'] for r in index}:
            raise ValueError('registered generator set differs from index')
        if any(roles.get(r['generator']) != r['role'] for r in index):
            raise ValueError('index roles differ from trusted registry')
        binding = dict(data_root=str(data_root), index_digest=hashlib.sha256(index_path.read_bytes()).hexdigest())
    identity = dict(event='registration', evaluation_id=evaluation_id, candidate_digest=candidate_digest,
                    classes=classes, primary_verdict=primary_verdict, kind=kind, index_binding=binding)
    with locked():
        entries = _read()
        registrations = [e for e in entries if e['event'] == 'registration']
        for entry in registrations:
            if entry['evaluation_id'] == evaluation_id:
                active = _active_binding(entries, entry)
                if any((active if k == 'index_binding' else entry.get(k)) != v for k, v in identity.items()):
                    raise ValueError('a registered verdict cannot be changed after scoring')
                return entry
        candidates = [e for e in registrations if e['kind'] == 'candidate']
        if kind == 'candidate':
            # All scored generators are receipt-bound, but exposure burns apply
            # to holdouts. Replacing exhausted holdouts must not be blocked by
            # reusing the unchanged design/calibration set.
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


def require_registration(registration, *, classes=(), exact=False, candidate=False):
    with locked():
        receipt = _require(_read(), registration, classes, exact=exact)
        if candidate and receipt['kind'] != 'candidate':
            raise ValueError('accept/reject inference requires a candidate registration with burn accounting')
        return receipt


def _active_binding(entries, receipt):
    binding = receipt.get('index_binding')
    for event in entries:
        if event['event'] == 'design-index-revision' and event['registration_hash'] == receipt['hash']:
            if binding is None or binding['index_digest'] != event['previous_index_digest']:
                raise ValueError('invalid design index revision chain')
            binding = dict(binding, index_digest=event['index_digest'])
    return binding


def revise_design_index(registration, *, previous_index):
    """Controller-only design resizing; protected entries and candidate identity stay frozen."""
    with locked():
        entries = _read()
        receipt = _require(entries, registration)
        binding = _active_binding(entries, receipt)
        if not binding or hashlib.sha256(Path(previous_index).read_bytes()).hexdigest() != binding['index_digest']:
            raise ValueError('previous index does not match registered index binding')
        old = json.loads(Path(previous_index).read_text())['cards']
        path = Path(binding['data_root']) / 'index.json'
        new = json.loads(path.read_text())['cards']
        roles = {g['id']: g['role'] for g in json.loads(Path(__file__).with_name('registry.json').read_text())}
        roles.update({g: 'calibration-only' for g in ('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04')})
        if {r['generator'] for r in new} != set(receipt['classes']) or any(roles.get(r['generator']) != r['role'] for r in new):
            raise ValueError('design revision cannot change registered generator sets or trusted roles')
        def protected(cards):
            return [r for r in cards if roles.get(r['generator']) not in ('design', 'control', 'calibration-only')]
        if canonical(protected(old)) != canonical(protected(new)):
            raise ValueError('design revision cannot change any protected holdout index entry')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest == binding['index_digest']:
            raise ValueError('design index revision must change the index')
        return _append(entries, dict(event='design-index-revision', registration_hash=receipt['hash'],
            evaluation_id=receipt['evaluation_id'], candidate_digest=receipt['candidate_digest'],
            previous_index_digest=binding['index_digest'], index_digest=digest,
            reason='design/control/calibration resizing only; protected holdouts unchanged'))


def require_bundle_registration(registration, bundle, *, test_inputs=None, test_labels=None):
    """Authorize real indexed bytes before any scoring or held-label access."""
    from study.practice.build_cards import tree_digest
    with locked():
        entries = _read()
        receipt = _require(entries, registration)
        binding = _active_binding(entries, receipt)
    if not binding:
        raise ValueError('scoring requires a registered generator index binding')
    root = Path(binding['data_root'])
    index_path = root / 'index.json'
    if not index_path.exists() or hashlib.sha256(index_path.read_bytes()).hexdigest() != binding['index_digest']:
        raise ValueError('registered generator index binding differs or is absent')
    bundle = Path(bundle).resolve()
    item = next((r for r in json.loads(index_path.read_text())['cards'] if root / 'bundles' / r['card'] == bundle), None)
    if item is None:
        raise ValueError('bundle missing from registered generator index binding')
    require_registration(receipt, classes=[item['generator']])
    roles = {g['id']: g['role'] for g in json.loads(Path(__file__).with_name('registry.json').read_text())}
    role = roles.get(item['generator'], 'calibration-only' if item['generator'] in ('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04') else None)
    if role != item['role'] or (receipt['kind'] == 'construction' and role not in ('design', 'control', 'calibration-only')):
        raise ValueError('holdout scoring requires a candidate registration')
    controller = root / 'controller' / item['card']
    if item.get('bundle_digest') != tree_digest(bundle) or item.get('controller_digest') != tree_digest(controller):
        raise ValueError('indexed bundle/controller content differs')
    for value, name in ((test_inputs, 'test_inputs.npz'), (test_labels, 'test_labels.npz')):
        if value is not None and Path(value).resolve() != controller / name:
            raise ValueError('test data must match the registered controller index binding')
    return receipt


def record_verdict(registration, result, *, classes):
    """The sole public verdict emission path: verify, append durably, then return."""
    with locked():
        entries = _read()
        receipt = _require(entries, registration, classes, exact=True)
        if receipt['kind'] != 'candidate':
            raise ValueError('accept/reject inference requires a candidate registration with burn accounting')
        event = _append(entries, dict(event='verdict', evaluation_id=receipt['evaluation_id'],
            registration_hash=receipt['hash'], candidate_digest=receipt['candidate_digest'], result=result))
        return dict(result, ledger_event_hash=event['hash'], evaluation_id=receipt['evaluation_id'])
