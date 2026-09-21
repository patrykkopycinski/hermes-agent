"""`phaseTimeout`: a recipe-level per-phase budget in the verify manifest.

Regression origin: the `~/.hermes` recipe carries a mutation harness needing
~23 minutes, against a hard-coded 600s cap in `run_verify`. Every bare
`hermes verify` killed it mid-mutation -- the exact corruption mode those
harnesses warn about -- and no phase after it ever ran, so the recipe could
never go green. `--timeout` existed but only helps the human who remembers to
type it; the stop-nudge tells everyone to run `hermes verify --json` bare.

Precedence under test: explicit caller value > manifest `phaseTimeout` > default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.verify.environment import load_manifest, save_manifest  # noqa: E402
from agent.verify.recipes import Recipe  # noqa: E402
from agent.verify.runner import DEFAULT_PHASE_TIMEOUT, run_verify  # noqa: E402


def _recipe(**kw) -> Recipe:
    return Recipe(name="t", kind="custom", test=["true"], **kw)


def _parsed(raw: dict) -> Recipe:
    """`from_dict` is a tolerant loader returning None on junk; these cases all
    supply a valid `name`, so a None here is itself a failure worth surfacing."""
    recipe = Recipe.from_dict(raw)
    assert recipe is not None
    return recipe


# --- schema: parse, serialize, round-trip -----------------------------------

def test_from_dict_reads_camel_and_snake():
    assert _parsed({"name": "t", "phaseTimeout": 1800}).phase_timeout == 1800.0
    assert _parsed({"name": "t", "phase_timeout": 1800}).phase_timeout == 1800.0


def test_from_dict_accepts_numeric_string():
    assert _parsed({"name": "t", "phaseTimeout": "1800"}).phase_timeout == 1800.0


def test_absent_stays_none_so_the_default_applies():
    assert _parsed({"name": "t"}).phase_timeout is None


@pytest.mark.parametrize("bad", [0, -1, "abc", "", None, [], {}, True, False])
def test_garbage_degrades_to_none_never_crashes(bad):
    """A corrupt budget must fall back to the default, not raise and not become
    a tiny cap that kills every phase. `True` is the sharp one: bool is an int
    subclass, so a stray `true` would otherwise mean a 1-second timeout."""
    assert _parsed({"name": "t", "phaseTimeout": bad}).phase_timeout is None


def test_to_dict_omits_when_unset_keeping_manifests_clean():
    assert "phaseTimeout" not in _recipe().to_dict()


def test_to_dict_emits_when_set():
    assert _recipe(phase_timeout=1800.0).to_dict()["phaseTimeout"] == 1800.0


def test_survives_a_manifest_round_trip(tmp_path):
    save_manifest(tmp_path, _recipe(phase_timeout=1800.0))
    reloaded = load_manifest(tmp_path)
    assert reloaded is not None
    assert reloaded.phase_timeout == 1800.0


def test_hermes_verify_save_does_not_silently_drop_it(tmp_path):
    """`--save` rewrites the manifest; a budget lost there is a silent regression."""
    save_manifest(tmp_path, _recipe(phase_timeout=1800.0))
    reloaded = load_manifest(tmp_path)
    assert reloaded is not None
    save_manifest(tmp_path, reloaded)
    raw = json.loads((tmp_path / ".hermes" / "environment.json").read_text())
    assert raw["recipe"]["phaseTimeout"] == 1800.0


# --- precedence: what actually reaches subprocess.run -----------------------

@pytest.fixture
def spy(monkeypatch):
    """Capture the timeout `run_verify` hands each phase command."""
    seen: list[float] = []
    import agent.verify.runner as runner

    def fake(phase, command, root, timeout, on_output=None):
        seen.append(timeout)
        return runner.PhaseResult(
            phase=phase, command=command, exit_code=0, duration=0.0, output_tail="")

    monkeypatch.setattr(runner, "_run_phase_command", fake)
    return seen


def _run(root, recipe, spy, **kw):
    run_verify(root, recipe, phases=("test",), skip_start=True, **kw)
    return spy[0]


def test_manifest_budget_is_used_by_a_bare_run(tmp_path, spy):
    assert _run(tmp_path, _recipe(phase_timeout=1800.0), spy) == 1800.0


def test_default_applies_when_manifest_is_silent(tmp_path, spy):
    assert _run(tmp_path, _recipe(), spy) == DEFAULT_PHASE_TIMEOUT


def test_explicit_caller_value_overrides_the_manifest(tmp_path, spy):
    assert _run(tmp_path, _recipe(phase_timeout=1800.0), spy, phase_timeout=30.0) == 30.0


def test_explicit_value_still_works_without_a_manifest_budget(tmp_path, spy):
    assert _run(tmp_path, _recipe(), spy, phase_timeout=30.0) == 30.0


# --- CLI wiring -------------------------------------------------------------

def _parse(argv):
    import argparse

    from hermes_cli.subcommands.verify import build_verify_parser

    parser = argparse.ArgumentParser()
    build_verify_parser(parser.add_subparsers(dest="cmd"), cmd_verify=lambda a: None)
    return parser.parse_args(["verify", *argv])


def test_bare_cli_forwards_none_so_the_manifest_survives():
    """The bug this guards: argparse defaulting to 600 made every bare
    `hermes verify` override the manifest with the very cap we are escaping."""
    assert _parse([]).timeout is None
    assert _parse([]).ready_timeout is None


def test_typed_flag_is_forwarded():
    assert _parse(["--timeout", "1800"]).timeout == 1800.0
