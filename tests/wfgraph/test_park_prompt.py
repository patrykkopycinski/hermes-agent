"""Human park prompts can reference earlier nodes' summaries via {node_id}."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "wfgraph"))

from wfgraph.waits import park_human  # noqa: E402


def test_park_prompt_substitutes_node_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scenario = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "diag", "kind": "agent", "config": {
                "goal": "diagnose; end with verdict PASS or FAIL on its own last line"}},
            {"id": "ask", "kind": "human", "config": {
                "goal": "Gateway diagnosis: {diag}. Approve remediation?"}},
        ],
        "edges": [
            {"from": "t", "to": "diag"}, {"from": "diag", "to": "ask"},
        ],
    }
    state = {
        "runId": "test-prompt-run",
        "summaries": {"diag": "429 storm on cursor hop; bench it"},
        "scenario": scenario,
        "status": "running",
    }
    step = scenario["steps"][2]
    park_human(state, step, 0)
    prompt = state["park"]["prompt"]
    assert "429 storm on cursor hop" in prompt
    assert "{diag}" not in prompt
    assert state["status"] == "waiting_human"


def test_park_prompt_leaves_unknown_tokens_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scenario = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "ask", "kind": "human", "config": {"goal": "approve {nope}?"}},
        ],
        "edges": [{"from": "t", "to": "ask"}],
    }
    state = {"runId": "test-prompt-run2", "summaries": {}, "scenario": scenario, "status": "running"}
    park_human(state, scenario["steps"][1], 0)
    assert state["park"]["prompt"] == "approve {nope}?"
