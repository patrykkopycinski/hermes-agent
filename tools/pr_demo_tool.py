"""GitHub PR demo attachment tool.

Registers the ``attach_demo_to_pr`` tool — uploads screenshots/videos to an
image host and posts them as a formatted comment on a GitHub pull request.
Supports multiple upload backends: local file paths, catbox.moe (anonymous,
no auth), and a pluggable interface for custom backends (S3, imgur, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.request import Request, urlopen

from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

ATTACH_DEMO_TO_PR_SCHEMA: Dict[str, Any] = {
    "name": "attach_demo_to_pr",
    "description": (
        "Attach demo screenshots or video recordings to a GitHub pull "
        "request as a formatted comment with embedded media. Uploads media "
        "to an image host (catbox.moe by default) and posts a comment with "
        "embedded images/video and a summary. Use after capture_demo or "
        "record_screen to share visual evidence on a PR — 'demos not diffs'. "
        "Requires the gh CLI to be authenticated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pr_url": {
                "type": "string",
                "description": (
                    "Full GitHub PR URL (e.g. "
                    "'https://github.com/owner/repo/pull/123')."
                ),
            },
            "screenshots": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of screenshot file paths to attach. Can be paths "
                    "from capture_demo output."
                ),
            },
            "video_path": {
                "type": "string",
                "description": "Optional video file path to attach.",
            },
            "summary": {
                "type": "string",
                "description": "Human-readable summary of what was built/demoed.",
            },
            "console_errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of console errors to include in the comment.",
            },
            "title": {
                "type": "string",
                "description": "Optional title for the comment (default: '📸 Demo').",
            },
        },
        "required": ["pr_url", "screenshots"],
    },
}


# ---------------------------------------------------------------------------
# Upload backends
# ---------------------------------------------------------------------------


def _upload_to_catbox(file_path: str) -> Optional[str]:
    """Upload a file to catbox.moe (anonymous, no auth required).

    Returns the public URL or None on failure.
    """
    if not os.path.exists(file_path):
        logger.warning("File not found: %s", file_path)
        return None

    file_size = os.path.getsize(file_path)
    if file_size > 200 * 1024 * 1024:  # 200MB catbox limit
        logger.warning("File too large for catbox: %s (%d bytes)", file_path, file_size)
        return None

    try:
        import requests

        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload", "userhash": ""},
                files={"fileToUpload": f},
                timeout=60,
            )
        if resp.status_code == 200 and resp.text.startswith("https://"):
            url = resp.text.strip()
            logger.info("Uploaded %s to %s", file_path, url)
            return url
        else:
            logger.warning(
                "Catbox upload failed: %s %s", resp.status_code, resp.text[:200]
            )
            return None
    except Exception as e:
        logger.warning("Catbox upload error: %s", e)
        return None


def _upload_to_freeimage(file_path: str) -> Optional[str]:
    """Upload to freeimage.host (no auth, returns direct image URL)."""
    # Public demo key — override via FREEIMAGE_API_KEY env var for production use
    api_key = os.environ.get("FREEIMAGE_API_KEY", "6d207e02198a847aa98d0a2a901485a5")
    try:
        import base64

        import requests

        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        resp = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": api_key,
                "action": "upload",
                "source": b64,
                "type": "base64",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "image" in data and "url" in data["image"]:
                return data["image"]["url"]
        logger.warning(
            "freeimage upload failed: %s %s", resp.status_code, resp.text[:200]
        )
    except Exception as e:
        logger.warning("freeimage upload error: %s", e)
    return None


def _upload_to_tmpfiles(file_path: str) -> Optional[str]:
    """Upload to tmpfiles.org (no auth, returns direct download URL)."""
    try:
        import requests

        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": f},
                timeout=30,
            )
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data and "url" in data["data"]:
                # Convert viewer URL to direct download
                return data["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception as e:
        logger.warning("tmpfiles upload error: %s", e)
    return None


def _upload_media(file_path: str) -> Optional[str]:
    """Upload a media file and return a public URL.

    Tries multiple backends: freeimage.host, tmpfiles.org, catbox.moe.
    """
    if not os.path.exists(file_path):
        logger.warning("File not found: %s", file_path)
        return None

    # Try freeimage.host first (reliable, returns direct image URL)
    url = _upload_to_freeimage(file_path)
    if url:
        return url

    # Try tmpfiles.org
    url = _upload_to_tmpfiles(file_path)
    if url:
        return url

    # Try catbox as last resort
    url = _upload_to_catbox(file_path)
    if url:
        return url

    logger.warning("All upload backends failed for %s", file_path)
    return None


# ---------------------------------------------------------------------------
# GitHub PR comment
# ---------------------------------------------------------------------------


def _parse_pr_url(pr_url: str) -> tuple[str, str, str]:
    """Parse a GitHub PR URL into (owner, repo, pr_number).

    Handles: https://github.com/owner/repo/pull/123
    Also handles URLs with /files suffix.
    """
    # Strip query params and fragments
    clean = pr_url.split("?")[0].split("#")[0].rstrip("/")
    parts = clean.split("/")
    # Expected: ['https:', '', 'github.com', 'owner', 'repo', 'pull', '123']
    # After 'github.com', next three are: owner, repo, 'pull', pr_number
    if "github.com" not in pr_url:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
    # Find 'github.com' index
    try:
        gh_idx = parts.index("github.com")
    except ValueError:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")

    if len(parts) < gh_idx + 4:
        raise ValueError(f"PR URL too short: {pr_url}")

    owner = parts[gh_idx + 1]
    repo = parts[gh_idx + 2]
    # parts[gh_idx + 3] should be 'pull'
    pr_number = parts[gh_idx + 4] if len(parts) > gh_idx + 4 else parts[-1]

    if not owner or not repo or not pr_number.isdigit():
        raise ValueError(f"Could not parse owner/repo/pr_number from: {pr_url}")

    return owner, repo, pr_number


def _post_pr_comment(owner: str, repo: str, pr_number: str, body: str) -> Optional[str]:
    """Post a comment on a GitHub PR using the gh CLI."""
    import shutil

    gh_path = shutil.which("gh")
    if not gh_path:
        # Try common paths
        for candidate in ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"]:
            if os.path.exists(candidate):
                gh_path = candidate
                break
    if not gh_path:
        logger.error("gh CLI not found in PATH or common locations")
        return None

    try:
        result = subprocess.run(
            [
                gh_path,
                "pr",
                "comment",
                pr_number,
                "--repo",
                f"{owner}/{repo}",
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "github.com" in line and "issuecomment" in line:
                    return line.strip()
            return result.stdout.strip()
        else:
            logger.error("gh pr comment failed: %s", result.stderr)
            return None
    except Exception as e:
        logger.error("Failed to post PR comment: %s", e)
        return None


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


def _format_pr_comment(
    title: str,
    summary: str,
    media_urls: List[Dict[str, str]],
    console_errors: Optional[List[str]] = None,
) -> str:
    """Format a Markdown comment body with embedded media."""
    lines = [f"## {title}\n"]

    if summary:
        lines.append(summary)
        lines.append("")

    for media in media_urls:
        url = media["url"]
        label = media.get("label", "")
        if media.get("type") == "video":
            lines.append(f"**{label}**" if label else "")
            lines.append(f"[🎬 Recording]({url})")
        else:
            if label:
                lines.append(f"**{label}**")
            lines.append(f"![{label or 'Screenshot'}]({url})")
        lines.append("")

    if console_errors:
        lines.append("<details>")
        lines.append("<summary>⚠️ Console Errors Detected</summary>\n")
        for err in console_errors:
            lines.append(f"- `{err}`")
        lines.append("\n</details>\n")

    lines.append("---")
    lines.append("*Generated by Hermes Agent demo-workflow*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def attach_demo_to_pr(
    pr_url: str,
    screenshots: List[str],
    video_path: Optional[str] = None,
    summary: str = "",
    console_errors: Optional[List[str]] = None,
    title: str = "📸 Demo",
    **kwargs: Any,
) -> str:
    """Upload demo media and post a formatted PR comment."""
    try:
        owner, repo, pr_number = _parse_pr_url(pr_url)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})

    media_urls: List[Dict[str, str]] = []
    failed_uploads: List[str] = []

    # Upload screenshots
    for i, path in enumerate(screenshots):
        label = f"Screenshot {i + 1}" if len(screenshots) > 1 else "Screenshot"
        url = _upload_media(path)
        if url:
            media_urls.append({"url": url, "label": label, "type": "image"})
        else:
            failed_uploads.append(path)

    # Upload video
    if video_path:
        url = _upload_media(video_path)
        if url:
            media_urls.append({"url": url, "label": "Recording", "type": "video"})
        else:
            failed_uploads.append(video_path)

    if not media_urls:
        return json.dumps({
            "success": False,
            "error": "All media uploads failed",
            "failed_uploads": failed_uploads,
        })

    # Format and post comment
    body = _format_pr_comment(title, summary, media_urls, console_errors)
    comment_url = _post_pr_comment(owner, repo, pr_number, body)

    if comment_url:
        return json.dumps(
            {
                "success": True,
                "pr_url": pr_url,
                "comment_url": comment_url,
                "media_count": len(media_urls),
                "failed_uploads": failed_uploads,
            },
            ensure_ascii=False,
        )
    else:
        return json.dumps({
            "success": False,
            "error": "Failed to post PR comment (gh CLI not available or not authenticated)",
            "media_urls": [m["url"] for m in media_urls],
        })


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def _check_gh_available(**kwargs: Any) -> bool:
    """Check if gh CLI is available."""
    import shutil

    return shutil.which("gh") is not None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="attach_demo_to_pr",
    toolset="github",
    schema=ATTACH_DEMO_TO_PR_SCHEMA,
    handler=lambda args, **kw: attach_demo_to_pr(
        pr_url=args.get("pr_url", ""),
        screenshots=args.get("screenshots", []),
        video_path=args.get("video_path"),
        summary=args.get("summary", ""),
        console_errors=args.get("console_errors"),
        title=args.get("title", "📸 Demo"),
    ),
    check_fn=_check_gh_available,
    emoji="📎",
    description=(
        "Upload demo screenshots/video to an image host and post them as a "
        "formatted comment on a GitHub pull request."
    ),
)
