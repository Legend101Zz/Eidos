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
