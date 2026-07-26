# unmanaged
"""Shared fixtures for the botcore test suite."""
import pytest


@pytest.fixture(autouse=True)
def _unpin_booted_config_set():
    """Reset the config-set boot pin between tests.

    The app-startup path (event_handlers) calls pin_booted_set(), which
    stores the active set name in a module global for the life of the
    process. Any test that drives startup (e.g. the brain files API
    tests) would otherwise leak its pin into every later test —
    active_set_name() then ignores the environment and the env-driven
    expectations in test_config_sets break.
    """
    yield
    from botcore.runtime import config_sets
    config_sets._BOOTED_SET = None
