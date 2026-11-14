"""Practice tests isolate trusted controller state from the programme ledger."""
import pytest


@pytest.fixture(autouse=True)
def practice_trusted_state(request, tmp_path, monkeypatch):
    if 'test_practice' not in request.node.path.name:
        return
    from study.practice import generators, ledger
    monkeypatch.setattr(ledger, 'LEDGER_ROOT', tmp_path / 'ledger')
    ledger.initialize()
    salt = tmp_path / 'row-token-salt'
    salt.write_bytes(bytes(range(32)))
    monkeypatch.setattr(generators, 'ROW_SALT_PATH', salt)

    # Pre-epoch suites exercise legacy ledger behavior with isolated epoch-1
    # indices. The r5 suite exercises the real epoch-2 controller transition.
    if request.node.path.name != 'test_practice_r5.py':
        from study.practice import build_cards
        frozen = generators.registry()
        monkeypatch.setattr(build_cards, 'registry', lambda: [
            {k: v for k, v in entry.items() if k != 'holdout_epoch'} for entry in frozen
        ])
