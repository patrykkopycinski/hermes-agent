"""wfgraph — agent graphs as an opt-in Hermes plugin.

The engine lives in the ``wfgraph`` package next to this file rather than
inside it. Two callers need it importable by that bare name: the plugin
loader (which imports this directory as ``hermes_plugins.wfgraph``) and the
cron/webhook scripts the trigger sync generates, which run in a *fresh*
process with no plugin loader at all. A path entry pointing at this
directory is the one arrangement that satisfies both.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the ``wfgraph`` tool.

    Called once by the plugin loader when the plugin is enabled via
    ``plugins.enabled`` in config.yaml. Declaring ``provides_tools`` in
    plugin.yaml is not sufficient on its own — that list is the manifest's
    promise, this function is what keeps it.
    """
    from .tool import SCHEMA, wfgraph_tool

    ctx.register_tool(
        name="wfgraph",
        toolset="workflow",
        schema=SCHEMA,
        handler=wfgraph_tool,
        description="Run and inspect agent graphs (fan-out, gates, rework loops).",
        emoji="🕸️",
    )
