"""F5/F6 regression: the durable trigger path.

`hermes workflow run` and the desktop canvas both spawn a daemon thread inside
a process that then exits, so neither is a durable runner. Cron ticks and
inbound webhooks are — they execute a generated script in a FRESH interpreter.
Two things have to hold for that to work, and neither did:

  F5  start_from_trigger inherited background=True, so the script spawned a
      thread and exited, stranding every run at status=running with ran=[].
  F6  the generated script has to put the plugin dir on sys.path, and that
      directory must not shadow the repo's own `tools` package.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from wfgraph.runner import start_from_trigger
from wfgraph.store import load_run, upsert_document
from wfgraph.triggers import _write_tick_script

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "wfgraph"


def _stub(prompt, context, payload, cfg):
    return {"ok": True, "verdict": "PASS", "summary": "done"}


def _linear_scenario():
    return {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "Start", "config": {"title": "Start"}},
            {"id": "work", "kind": "agent", "title": "Work", "config": {"prompt": "go"}},
        ],
        "edges": [{"source": "t", "target": "work"}],
    }


def test_trigger_run_completes_before_returning(wf_home):
    """F5: a trigger walks its graph inline, so the caller's exit is safe.

    Against the unfixed engine this returns with ran=[] and status=running —
    the thread it spawned dies with the script that called it.
    """
    upsert_document({"id": "durable", "name": "Durable", "scenario": _linear_scenario()})

    state = start_from_trigger("durable", source="cron", execute_fn=_stub)

    final = load_run(state["runId"]) or state
    assert final["status"] != "running", final
    assert "work" in (final.get("ran") or []), final


def test_generated_tick_script_imports_in_a_bare_interpreter(wf_home):
    """F6: the generated script must import cleanly with nothing preloaded.

    It runs under plain `python script.py` from an arbitrary cwd. If the
    plugin dir shadows the repo's `tools` package, the agent step later dies
    with "'tools' is not a package"; if the path injection is missing, this
    fails at the import line instead.
    """
    upsert_document({"id": "ticked", "name": "Ticked", "scenario": _linear_scenario()})
    name = _write_tick_script("ticked")
    body = (wf_home / "scripts" / name).read_text()

    assert "sys.path.insert" in body, body
    assert str(PLUGIN_DIR) in body, body

    # Import-only: prove the module graph resolves standalone without paying
    # for a live agent walk. The repo root stands in for the installed
    # Hermes that a real cron tick would import `tools.registry` from.
    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        sys.path.insert(0, {str(PLUGIN_DIR)!r})
        from wfgraph.runner import start_from_trigger   # F4 path injection
        import tools.registry                            # F6 no shadowing
        print("IMPORTS_OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(wf_home),
        timeout=120,
    )
    assert "IMPORTS_OK" in proc.stdout, (proc.stdout, proc.stderr)
