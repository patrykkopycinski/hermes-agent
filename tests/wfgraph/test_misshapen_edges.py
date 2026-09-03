"""Misshapen edges (from/to, dangling refs) are rejected at start, not ignored."""
import pytest

from wfgraph.validate import WorkflowGraphError, reject_misshapen_edges


def _steps(*ids):
    return [{"id": i, "kind": "agent", "config": {}} for i in ids]


def test_from_to_edges_are_rejected():
    scenario = {"steps": _steps("t", "diag", "act"), "edges": [
        {"from": "t", "to": "diag"},
        {"from": "diag", "to": "act"},
    ]}
    with pytest.raises(WorkflowGraphError, match="from/to"):
        reject_misshapen_edges(scenario, _steps("t", "diag", "act"))


def test_edgeless_dict_is_rejected():
    scenario = {"steps": _steps("a", "b"), "edges": [{"id": "e1"}]}
    with pytest.raises(WorkflowGraphError, match="missing source/target"):
        reject_misshapen_edges(scenario, _steps("a", "b"))


def test_dangling_edge_is_rejected():
    scenario = {"steps": _steps("a"), "edges": [{"source": "a", "target": "nope"}]}
    with pytest.raises(WorkflowGraphError, match="does not exist"):
        reject_misshapen_edges(scenario, _steps("a"))


def test_well_formed_edges_pass():
    scenario = {"steps": _steps("t", "g", "ask", "act"), "edges": [
        {"id": "e1", "source": "t", "target": "g"},
        {"id": "e2", "source": "g", "target": "ask", "sourceHandle": "pass"},
        {"id": "e3", "source": "ask", "target": "act"},
    ]}
    reject_misshapen_edges(scenario, _steps("t", "g", "ask", "act"))  # no raise
