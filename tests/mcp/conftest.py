import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs a running lab (EIDOS_HOME); skipped otherwise")


@pytest.fixture
def live_lab():
    home = os.environ.get("EIDOS_HOME")
    if not home or not os.path.exists(os.path.join(home, "config", "lab.json")):
        pytest.skip("EIDOS_HOME not set to a lab")
    return home
