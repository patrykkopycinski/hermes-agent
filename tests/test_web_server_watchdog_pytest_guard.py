"""The parent-death watchdog must never arm inside a test process.

``_start_parent_death_watchdog`` spawns a thread whose terminal action is
``os._exit(0)`` -- an unconditional, unwindable process kill.  Inside pytest
that kills the *runner*: the session stops mid-file, ``pytest_sessionfinish``
never fires, no summary line and no ``--junit-xml`` are written, and the
shell still observes exit status 0.  A truncated run is indistinguishable
from a green one unless you notice the missing summary.

The trigger is ordinary inheritance: a test process launched from the Hermes
desktop app inherits ``HERMES_PARENT_PID`` (plus marker/nonce) pointing at
the desktop.  Any test that reaches ``start_server()`` then arms a watchdog
aimed at a parent that has nothing to do with the test run.
"""

import os
import threading
from unittest.mock import patch

from hermes_cli import web_server


def _watchdog_threads():
    return [t for t in threading.enumerate() if t.name == "serve-parent-watchdog"]


class TestWatchdogNeverArmsUnderPytest:
    def test_no_thread_started_with_full_parent_identity(self):
        """Marker+nonce present and valid -- still must not arm under pytest."""
        before = len(_watchdog_threads())
        env = {
            "HERMES_PARENT_PID": str(os.getpid()),
            "HERMES_PARENT_START_MARKER": "123456",
            "HERMES_PARENT_NONCE": "abc123",
            "PYTEST_CURRENT_TEST": "test_x (call)",
        }
        with patch.dict(os.environ, env, clear=False):
            web_server._start_parent_death_watchdog()
        assert len(_watchdog_threads()) == before

    def test_no_thread_started_with_legacy_pid_only(self):
        """The PID-only legacy path is guarded too."""
        before = len(_watchdog_threads())
        env = {
            "HERMES_PARENT_PID": str(os.getpid()),
            "PYTEST_CURRENT_TEST": "test_x (call)",
        }
        with patch.dict(os.environ, env, clear=False):
            for stale in ("HERMES_PARENT_START_MARKER", "HERMES_PARENT_NONCE"):
                os.environ.pop(stale, None)
            web_server._start_parent_death_watchdog()
        assert len(_watchdog_threads()) == before

    def test_os_exit_is_never_reached(self):
        """The guard returns before any thread that could _exit is created."""
        env = {
            "HERMES_PARENT_PID": str(os.getpid()),
            "PYTEST_CURRENT_TEST": "test_x (call)",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(os, "_exit") as fake_exit, \
             patch.object(threading, "Thread") as fake_thread:
            web_server._start_parent_death_watchdog()
            assert not fake_thread.called, "watchdog thread must not be constructed"
            assert not fake_exit.called
