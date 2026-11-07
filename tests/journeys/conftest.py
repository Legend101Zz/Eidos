def pytest_configure(config):
    config.addinivalue_line("markers", "journey: end-to-end journey against a running lab (EIDOS_JOURNEY=1)")
