"""The ordinary suite keeps no session files in the user's runtime / cache directory (kept_session): each test gets
its own directory. tests/hw keeps the real one (a run on a real probe is a host run like the CLI's)."""

import pytest


@pytest.fixture(autouse=True)
def _session_dir(request, tmp_path_factory, monkeypatch):
    path = request.node.path
    if path.parent.name != "hw" or path.name == "test_harness.py":
        monkeypatch.setenv("OEP_SESSION_DIR", str(tmp_path_factory.mktemp("sessions")))
