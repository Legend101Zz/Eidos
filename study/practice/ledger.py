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



def comparator_digest():
    """Pin the entire trusted harness, including references and exported comparators."""
    root = Path(__file__).resolve().parents[2]
    paths = [*Path(__file__).parent.glob('*.py'), Path(__file__).with_name('registry.json'),
             root / 'docs/experiments/practice-family/MANIFEST.json',
             root / 'components/eidos.search.recipe-sweep/recipe_sweep.py',
             root / 'components/eidos.search.recipe-sweep/templates/fit_recipe.py',
             root / 'study/fresh_check.py']
    return hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in sorted(paths))).hexdigest()


def open_epoch(epoch, *, data_roots, reason):
    """Explicit controller transition: append retirement and fresh data/source bindings."""
    from study.practice.build_cards import tree_digest
    from study.practice.generators import registry
    protected = {g['id']: g for g in registry() if g['role'] in ('holdout', 'holdout-class')}
    if not isinstance(epoch, int) or epoch < 2 or not reason or not data_roots:
        raise ValueError('a new epoch, data roots and controller reason are required')
    bindings = []
    for root in data_roots:
        root = Path(root).resolve()
        path = root / 'index.json'
        index = json.loads(path.read_text())
        rows = [r for r in index['cards'] if r['generator'] in protected]
        if index.get('holdout_epoch') != epoch or {r['generator'] for r in rows} != set(protected):
            raise ValueError('new epoch must cover every protected generator')
        for row in rows:
            entry = protected[row['generator']]
            if row['bundle_digest'] != tree_digest(root / 'bundles' / row['card']) or row['controller_digest'] != tree_digest(root / 'controller' / row['card']):
                raise ValueError('epoch index differs from actual protected data')
            if (row.get('holdout_epoch'), row.get('seed_stream')) != (epoch, entry['seed_stream']):
                raise ValueError('epoch seeds differ from trusted registry')
        bindings.append(dict(data_root=str(root), index_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
                             protected_cards=rows))
    with locked():
        entries = _read()
        prior = [e for e in entries if e['event'] == 'epoch-open']
        current = prior[-1]['epoch'] if prior else 1
        if epoch != current + 1:
            raise ValueError('epochs must advance once, without resetting history')
        old_streams = ({g['id']: g['seed_stream'] for g in prior[-1]['generators']} if prior else
                       {g: entry['seed_stream'] - 1000000000 for g, entry in protected.items()})
        if any(entry['seed_stream'] == old_streams.get(g) for g, entry in protected.items()):
            raise ValueError('protected epoch requires fresh seed streams')
        old_cards = []
        for receipt in entries:
            if receipt['event'] != 'registration' or receipt.get('holdout_epoch', 1) != current:
                continue
            binding = _active_binding(entries, receipt)
            if binding:
                path = Path(binding['data_root']) / 'index.json'
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == binding['index_digest']:
                    old_cards.extend(r for r in json.loads(path.read_text())['cards'] if r['generator'] in protected)
        old_digests = {r['bundle_digest'] for r in old_cards}
        if any(r['bundle_digest'] in old_digests for b in bindings for r in b['protected_cards']):
            raise ValueError('new epoch cannot reuse protected data from the previous epoch')
        _append(entries, dict(event='epoch-retired', epoch=current, reason=reason))
        entries = _read()
        return _append(entries, dict(event='epoch-open', epoch=epoch, reason=reason,
            generators=list(protected.values()), index_bindings=bindings,
            candidate_budget_per_generator=2, harness_validation_budget_per_generator=2,
            comparator_digest=comparator_digest()))


def _check_epoch(entries, receipt):
    protected = receipt.get('protected_generators', [])
    # Legacy registrations are epoch 1. Construction contains no protected data.
    if not protected and receipt['kind'] == 'construction':
        return
    epoch = receipt.get('holdout_epoch', 1)
    if any(e['event'] == 'epoch-retired' and e['epoch'] == epoch for e in entries):
        raise ValueError('holdout epoch is retired; no new scoring or candidate verdicts')
    if receipt['kind'] == 'harness-validation':
        event = next((e for e in entries if e['event'] == 'epoch-open' and e['epoch'] == epoch), None)
        if not event or receipt.get('comparator_digest') != event['comparator_digest'] or comparator_digest() != event['comparator_digest']:
            raise ValueError('harness-validation requires the epoch-pinned comparator source digest')


def register(*, evaluation_id, candidate_digest, classes, primary_verdict, kind='candidate', data_root=None, epoch=None):
    classes = sorted(set(classes))
    if not evaluation_id or not candidate_digest or not primary_verdict or not classes:
        raise ValueError('evaluation ID, frozen candidate digest, generators and primary are required')
    if kind not in ('candidate', 'construction', 'harness-validation'):
        raise ValueError('unknown registration kind')
    roles = {g['id']: g['role'] for g in json.loads(Path(__file__).with_name('registry.json').read_text())}
    roles.update({g: 'calibration-only' for g in ('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04')})
    if kind == 'construction' and any(roles.get(g) not in ('design', 'calibration-only', 'control') for g in classes):
        raise ValueError('construction registration may cover only design/calibration/control generators')
    binding = None
    index_epoch = 1
    if data_root is not None:
        data_root = Path(data_root).resolve()
        index_path = data_root / 'index.json'
        document = json.loads(index_path.read_text())
        index_epoch = document.get('holdout_epoch', 1)
        index = document['cards']
        if set(classes) != {r['generator'] for r in index}:
            raise ValueError('registered generator set differs from index')
        if any(roles.get(r['generator']) != r['role'] for r in index):
            raise ValueError('index roles differ from trusted registry')
        binding = dict(data_root=str(data_root), index_digest=hashlib.sha256(index_path.read_bytes()).hexdigest())
    epoch = index_epoch if epoch is None else epoch
    if binding and epoch != index_epoch:
        raise ValueError('registration epoch differs from index')
    protected = [g for g in classes if roles.get(g, 'holdout') in ('holdout', 'holdout-class')]
    identity = dict(event='registration', evaluation_id=evaluation_id, candidate_digest=candidate_digest,
                    classes=classes, primary_verdict=primary_verdict, kind=kind, index_binding=binding,
                    holdout_epoch=epoch, protected_generators=protected,
                    comparator_digest=comparator_digest() if kind == 'harness-validation' else None)
    with locked():
        entries = _read()
        _check_epoch(entries, identity)
        epoch_event = next((e for e in entries if e['event'] == 'epoch-open' and e['epoch'] == epoch), None)
        if protected and (epoch_event or epoch != 1) and (not epoch_event or not binding or not any(
                binding == {k: b[k] for k in ('data_root', 'index_digest')} for b in epoch_event['index_bindings'])):
            raise ValueError('protected registration must bind an authorized epoch index')
        if kind == 'harness-validation' and candidate_digest != identity['comparator_digest']:
            raise ValueError('harness-validation digest must identify frozen comparators')
        registrations = [e for e in entries if e['event'] == 'registration']
        for entry in registrations:
            if entry['evaluation_id'] == evaluation_id:
                active = _active_binding(entries, entry)
                if any((active if k == 'index_binding' else entry.get(k)) != v for k, v in identity.items()):
                    raise ValueError('a registered verdict cannot be changed after scoring')
                return entry
        candidates = [e for e in registrations if e['kind'] == 'candidate']
        if kind in ('candidate', 'harness-validation'):
            # All scored generators are receipt-bound, but exposure burns apply
            # to holdouts. Replacing exhausted holdouts must not be blocked by
            # reusing the unchanged design/calibration set.
            protected = [name for name in classes if roles.get(name, 'holdout') in ('holdout', 'holdout-class')]
            for name in protected:
                if sum(name in e['classes'] and e.get('holdout_epoch', 1) == epoch and e['kind'] == kind for e in registrations) >= 2:
                    raise ValueError('two-burn limit reached; independent replacement and review required')
            if kind == 'candidate' and len(candidates) >= 8:
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
        entries = _read()
        receipt = _require(entries, registration, classes, exact=exact)
        _check_epoch(entries, receipt)
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


def require_bundle_registration(registration, bundle, *, test_inputs=None, test_labels=None, purpose=None):
    """Authorize real indexed bytes before any scoring or held-label access."""
    from study.practice.build_cards import tree_digest
    with locked():
        entries = _read()
        receipt = _require(entries, registration)
        _check_epoch(entries, receipt)
        if receipt['kind'] == 'harness-validation' and purpose not in ('comparators', 'references', 'fault-controls', 'fresh-export'):
            raise ValueError('harness-validation is restricted to explicit comparator entry points')
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
        _check_epoch(entries, receipt)
        if receipt['kind'] != 'candidate':
            raise ValueError('accept/reject inference requires a candidate registration with burn accounting')
        event = _append(entries, dict(event='verdict', evaluation_id=receipt['evaluation_id'],
            registration_hash=receipt['hash'], candidate_digest=receipt['candidate_digest'], result=result))
        return dict(result, ledger_event_hash=event['hash'], evaluation_id=receipt['evaluation_id'])


def record_validation(registration, result, *, classes):
    """A harness diagnostic can never become a candidate acceptance event."""
    with locked():
        entries = _read()
        receipt = _require(entries, registration, classes, exact=True)
        _check_epoch(entries, receipt)
        if receipt['kind'] != 'harness-validation':
            raise ValueError('harness diagnostics require harness-validation registration')
        event = _append(entries, dict(event='harness-validation-result', evaluation_id=receipt['evaluation_id'],
            registration_hash=receipt['hash'], holdout_epoch=receipt['holdout_epoch'], result=result))
        return dict(result, harness_validation_only=True, candidate_verdict=False,
                    ledger_event_hash=event['hash'], evaluation_id=receipt['evaluation_id'])
