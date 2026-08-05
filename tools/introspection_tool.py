"""Agent self-introspection tools.

Registers tools that let the agent query its own run context:
- ``session_context`` — current session info, files modified, tools used
- ``run_summary`` — a structured summary of what happened in this session

These tools help the agent write better demo summaries and decide what to
capture, making the demo-workflow more autonomous.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SESSION_CONTEXT_SCHEMA: Dict[str, Any] = {
    "name": "session_context",
    "description": (
        "Get the current agent session context: session ID, working "
        "directory, files modified in this session (via git diff), tools "
        "available, and loaded skills. Use to write better demo summaries "
        "or understand what work was done in this session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_diff": {
                "type": "boolean",
                "description": "Include git diff of changes in this session (default true).",
            },
            "include_skills": {
                "type": "boolean",
                "description": "List loaded skills (default true).",
            },
        },
    },
}

RUN_SUMMARY_SCHEMA: Dict[str, Any] = {
    "name": "run_summary",
    "description": (
        "Generate a structured summary of the current agent run: what was "
        "requested, what was done, files created/modified, tools used, "
        "and suggested next steps. Useful for demo narration and PR "
        "descriptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["markdown", "json"],
                "description": "Output format (default: markdown).",
            },
            "focus": {
                "type": "string",
                "description": (
                    "Optional focus area to emphasize in the summary "
                    "(e.g. 'frontend changes', 'API endpoints')."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _get_git_diff() -> Dict[str, Any]:
    """Get git diff information for the current working directory."""
    result = {
        "modified_files": [],
        "added_files": [],
        "deleted_files": [],
        "diff_stat": "",
        "has_changes": False,
    }

    try:
        # Get diff stat
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_stat.returncode == 0 and diff_stat.stdout.strip():
            result["diff_stat"] = diff_stat.stdout.strip()
            result["has_changes"] = True

        # Get modified files
        diff_names = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_names.returncode == 0:
            result["modified_files"] = [
                f for f in diff_names.stdout.strip().split("\n") if f
            ]

        # Get untracked (added) files
        untracked = subprocess.run(
            ["git", "status", "--porcelain", "-u"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if untracked.returncode == 0:
            for line in untracked.stdout.strip().split("\n"):
                if line.startswith("??"):
                    result["added_files"].append(line[3:].strip())
                elif line.startswith(" D"):
                    result["deleted_files"].append(line[3:].strip())

        result["has_changes"] = bool(
            result["modified_files"] or result["added_files"] or result["deleted_files"]
        )

    except FileNotFoundError:
        result["error"] = "Git not available"
    except Exception as e:
        result["error"] = str(e)

    return result


def _get_loaded_skills() -> List[Dict[str, str]]:
    """List skills available in the Hermes home directory."""
    skills: List[Dict[str, str]] = []
    skills_base = Path.home() / ".hermes" / "skills"

    if not skills_base.exists():
        return skills

    # Walk the skills directory and find SKILL.md files
    for root, dirs, files in os.walk(skills_base):
        if "SKILL.md" in files:
            skill_path = os.path.join(root, "SKILL.md")
            try:
                with open(skill_path, encoding="utf-8") as f:
                    # Read just the frontmatter
                    content = f.read(2000)
                    name = ""
                    desc = ""
                    in_frontmatter = False
                    for line in content.split("\n"):
                        if line.strip() == "---":
                            in_frontmatter = not in_frontmatter
                            continue
                        if not in_frontmatter:
                            break
                        if line.strip().startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.strip().startswith("description:"):
                            desc = line.split(":", 1)[1].strip()[:120]
                    if name:
                        skills.append({
                            "name": name,
                            "description": desc,
                            "path": skill_path,
                        })
            except Exception:
                pass

    return skills


def _get_session_info() -> Dict[str, Any]:
    """Get current session metadata."""
    import hermes_constants

    info: Dict[str, Any] = {}

    # Session working directory
    info["cwd"] = os.getcwd()

    # Hermes home
    info["hermes_home"] = str(hermes_constants.get_hermes_dir("", "hermes"))

    # Environment context
    info["platform"] = os.name
    info["pid"] = os.getpid()

    # Check if running in gateway/desktop mode
    info["mode"] = os.environ.get("HERMES_MODE", "cli")

    return info


def session_context(
    include_diff: bool = True,
    include_skills: bool = True,
    **kwargs: Any,
) -> str:
    """Get the current agent session context."""
    result: Dict[str, Any] = {"success": True}

    # Session info
    result["session"] = _get_session_info()

    # Git diff
    if include_diff:
        result["git"] = _get_git_diff()

    # Skills
    if include_skills:
        result["skills"] = _get_loaded_skills()

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def run_summary(
    format: str = "markdown",
    focus: str = "",
    **kwargs: Any,
) -> str:
    """Generate a structured summary of the current agent run."""
    git_info = _get_git_diff()
    session = _get_session_info()
    all_files = git_info.get("modified_files", []) + git_info.get("added_files", [])

    if format == "json":
        summary = {
            "session": session,
            "changes": git_info,
            "file_count": len(all_files),
            "has_uncommitted_changes": git_info.get("has_changes", False),
        }
        return json.dumps(summary, ensure_ascii=False, indent=2, default=str)

    # Markdown format
    lines = ["# Run Summary\n"]

    lines.append(f"**Working directory:** {session.get('cwd', 'unknown')}\n")

    if all_files:
        lines.append("## Files Changed\n")
        for f in sorted(all_files):
            lines.append(f"- `{f}`")
        lines.append("")
    else:
        lines.append("_No uncommitted changes._\n")

    if git_info.get("diff_stat"):
        lines.append("## Diff Summary\n")
        lines.append("```")
        lines.append(git_info["diff_stat"])
        lines.append("```\n")

    if focus:
        lines.append(f"## Focus: {focus}\n")
        lines.append(f"The following changes are most relevant to **{focus}**:")
        lines.append("")

    lines.append("## Suggested Demo Targets\n")
    if all_files:
        # Look for HTML, JSX, TSX, Vue files
        ui_files = [
            f
            for f in all_files
            if any(
                f.endswith(ext)
                for ext in [".html", ".jsx", ".tsx", ".vue", ".css", ".scss"]
            )
        ]
        if ui_files:
            lines.append("UI files changed — consider capturing a screenshot:")
            for f in ui_files[:5]:
                lines.append(f"- `{f}`")
        else:
            lines.append("No UI files detected. Consider a terminal-based demo.")
    else:
        lines.append("_No files to demo._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------


def _always_available(**kwargs: Any) -> bool:
    return True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="session_context",
    toolset="skills",
    schema=SESSION_CONTEXT_SCHEMA,
    handler=lambda args, **kw: session_context(
        include_diff=args.get("include_diff", True),
        include_skills=args.get("include_skills", True),
    ),
    check_fn=_always_available,
    emoji="🔍",
    description="Get current agent session context: cwd, git diff, loaded skills.",
)

registry.register(
    name="run_summary",
    toolset="skills",
    schema=RUN_SUMMARY_SCHEMA,
    handler=lambda args, **kw: run_summary(
        format=args.get("format", "markdown"),
        focus=args.get("focus", ""),
    ),
    check_fn=_always_available,
    emoji="📋",
    description="Generate a structured summary of the current agent run for demo narration.",
)
