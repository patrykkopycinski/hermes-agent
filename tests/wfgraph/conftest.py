import sys
from pathlib import Path

import pytest

# The engine ships inside the plugin directory; make it importable as a
# top-level package the same way the plugin loader and the generated
# cron/webhook scripts do.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'wfgraph'))


@pytest.fixture()
def wf_home(tmp_path, monkeypatch):
    """Point the engine's store at a scratch HERMES_HOME.

    Runs and documents are written under get_hermes_home(); without this a
    test would scribble into the developer's real profile.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants

    if hasattr(hermes_constants.get_hermes_home, "cache_clear"):
        hermes_constants.get_hermes_home.cache_clear()
    yield tmp_path
    if hasattr(hermes_constants.get_hermes_home, "cache_clear"):
        hermes_constants.get_hermes_home.cache_clear()
