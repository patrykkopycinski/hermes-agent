"""Tests for tools.hook_output_spill."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import hook_output_spill as hos


class GetSpillConfigTests(unittest.TestCase):
    def test_defaults_when_no_config(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            cfg = hos.get_spill_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["max_chars"], hos.DEFAULT_MAX_CHARS)
        self.assertEqual(cfg["preview_head"], hos.DEFAULT_PREVIEW_HEAD)
        self.assertEqual(cfg["preview_tail"], hos.DEFAULT_PREVIEW_TAIL)
        self.assertIsNone(cfg["directory"])


    def test_load_config_exception_is_swallowed(self):
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("bad")):
            cfg = hos.get_spill_config()
        self.assertEqual(cfg["max_chars"], hos.DEFAULT_MAX_CHARS)
        self.assertTrue(cfg["enabled"])


class SpillIfOversizedTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hermes-spill-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cfg(self, **overrides):
        base = {
            "enabled": True,
            "max_chars": 100,
            "preview_head": 20,
            "preview_tail": 20,
            "directory": self.tmpdir,
        }
        base.update(overrides)
        return base

    def test_empty_and_none_are_noops(self):
        self.assertEqual(hos.spill_if_oversized("", config=self._cfg()), "")
        self.assertEqual(hos.spill_if_oversized(None, config=self._cfg()), "")

    def test_text_under_cap_is_unchanged(self):
        small = "x" * 50
        self.assertEqual(hos.spill_if_oversized(small, config=self._cfg()), small)

    def test_line_boundary_slices_never_cut_entries_mid_string(self):
        """Head/tail previews must contain only WHOLE lines from the input.

        Regression: raw char slices produced dangling fragments (e.g. `It'`,
        `Verified:`) from recalled-memory prefetches, which the model then read
        as stray user text.
        """
        lines = [f"- Memory entry {i} - some content here" for i in range(300)]
        lines.append("- DESKTOP CLIP BUG (It'/I' messages): composer detail line")
        text = "\n".join(lines)
        cfg = self._cfg(max_chars=1000, preview_head=200, preview_tail=200)
        out = hos.spill_if_oversized(
            text, session_id="line-boundary", source="probe", config=cfg
        )
        section = None
        for line in out.split("\n"):
            if line in ("--- head ---", "--- tail ---"):
                section = line
                continue
            if line.startswith("[") or section is None or not line.strip():
                continue
            self.assertIn(
                line,
                set(lines),
                f"{section} leaked a mid-string fragment: {line[:60]!r}",
            )


    def test_newline_free_payload_never_previews_a_partial_line(self):
        """A window holding no newline is one partial line — drop it, don't ship it raw.

        Regression: the line-boundary helpers returned the slice unchanged when
        it contained no newline, so a single-line payload still leaked a
        mid-word fragment into the prompt — the exact shape the line-boundary
        rule exists to prevent.
        """
        text = "entry-without-any-newlines " * 400
        cfg = self._cfg(max_chars=1000, preview_head=200, preview_tail=200)
        out = hos.spill_if_oversized(
            text, session_id="no-newline", source="probe", config=cfg
        )

        self.assertNotIn("--- head ---", out)
        self.assertNotIn("--- tail ---", out)
        self.assertIn("output truncated", out)

    def test_tail_window_starting_mid_word_never_leaks_a_partial_line(self):
        """The tail slice (``text[-tail:]``) can start mid-word even in a genuinely
        multi-line payload; the fragment before its first newline must be dropped,
        not shipped raw.

        Regression: ``_tail_from_newline`` correctly drops everything up to the
        first '\\n' when the tail window IS a real cut, but this must hold even
        when that first "line" inside the window begins mid-word (not just when
        the window holds no newline at all — that narrower case was 467ef51's fix).
        """
        lines = [f"filler-line-{i}-padding-content-xyz" for i in range(50)]
        text = "\n".join(lines)
        # Choose a tail window whose start lands inside a line's characters, not
        # exactly on a line boundary, and whose window is a genuine cut (tail < total).
        tail_window = len(lines[-1]) + len(lines[-2]) // 2
        cfg = self._cfg(max_chars=100, preview_head=0, preview_tail=tail_window)
        out = hos.spill_if_oversized(
            text, session_id="mid-word-tail", source="probe", config=cfg
        )
        lineset = set(lines)
        section = None
        for line in out.split("\n"):
            if line in ("--- head ---", "--- tail ---"):
                section = line
                continue
            if line.startswith("[") or section is None or not line.strip():
                continue
            self.assertIn(
                line, lineset,
                f"{section} leaked a mid-word fragment: {line[:60]!r}",
            )

    def test_single_line_payload_never_previews_a_raw_fragment(self):
        """A single-line (no-newline) payload over the cap must never ship a raw
        char-boundary fragment for either window — it is dropped, per 467ef51."""
        text = "x" * 5000
        cfg = self._cfg(max_chars=100, preview_head=30, preview_tail=30)
        out = hos.spill_if_oversized(
            text, session_id="single-line", source="probe", config=cfg
        )
        self.assertNotIn("--- head ---", out)
        self.assertNotIn("--- tail ---", out)
        self.assertIn("output truncated", out)

    def test_head_plus_tail_equal_total_keeps_whole_boundary_lines(self):
        """When ``preview_head + preview_tail`` exactly equals the payload length,
        the head window ends exactly where the tail window begins — both windows
        must still resolve to whole lines with no overlap-induced fragment."""
        text = "AAAA-first-line\nBBBB-second-line\nCCCC-third-line-final"
        total = len(text)
        head = 20
        tail = total - head
        self.assertEqual(head + tail, total)
        cfg = self._cfg(max_chars=5, preview_head=head, preview_tail=tail)
        out = hos.spill_if_oversized(
            text, session_id="boundary-exact", source="probe", config=cfg
        )
        lineset = set(text.split("\n"))
        section = None
        for line in out.split("\n"):
            if line in ("--- head ---", "--- tail ---"):
                section = line
                continue
            if line.startswith("[") or section is None or not line.strip():
                continue
            self.assertIn(line, lineset)
        # Both windows are populated (head resolves to the whole first line;
        # tail resolves to the whole final line).
        self.assertIn("--- head ---", out)
        self.assertIn("--- tail ---", out)
        self.assertIn("AAAA-first-line", out)
        self.assertIn("CCCC-third-line-final", out)

    def test_tail_window_covering_entire_text_keeps_the_genuine_first_line(self):
        """When ``preview_tail`` >= total length, ``text[-tail:]`` is the WHOLE
        document, not an artificial cut — the genuine first line must survive,
        even though it has no preceding newline to anchor on.

        Regression: line-boundary snapping was applied unconditionally, so a tail
        window that reached the true start of the document was still treated as if
        it might start mid-line, silently dropping the real first line.
        """
        text = "genuine-first-line-not-a-cut\nsecond-line\nthird-line-final"
        cfg = self._cfg(max_chars=5, preview_head=0, preview_tail=len(text) + 100)
        out = hos.spill_if_oversized(
            text, session_id="full-tail-window", source="probe", config=cfg
        )
        self.assertIn("--- tail ---", out)
        self.assertIn("genuine-first-line-not-a-cut", out)

    def test_head_window_covering_entire_text_keeps_the_genuine_last_line(self):
        """Symmetric case: ``preview_head`` >= total length means ``text[:head]``
        is the WHOLE document. The genuine last line (even with no trailing
        newline, the normal shape for most text) must survive."""
        text = "line-one\nline-two\nline-three-no-trailing-newline"
        cfg = self._cfg(max_chars=5, preview_head=len(text) + 50, preview_tail=0)
        out = hos.spill_if_oversized(
            text, session_id="full-head-window", source="probe", config=cfg
        )
        self.assertIn("--- head ---", out)
        self.assertIn("line-three-no-trailing-newline", out)

    def test_default_directory_uses_hermes_home(self):
        """When no directory override, spill under HERMES_HOME/hook_outputs."""
        test_home = tempfile.mkdtemp(prefix="hermes-home-")
        try:
            with patch.dict(os.environ, {"HERMES_HOME": test_home}):
                # Also patch get_hermes_home to the env var to mirror production.
                cfg = self._cfg(directory=None, max_chars=5)
                hos.spill_if_oversized("x" * 200, session_id="sess", config=cfg)
            # Spill directory exists somewhere under test_home OR default
            # ~/.hermes/hook_outputs depending on get_hermes_home behaviour.
            candidates = [Path(test_home) / "hook_outputs" / "sess"]
            # At least one of the candidate dirs now exists and has a file.
            existing = [c for c in candidates if c.is_dir() and list(c.iterdir())]
            self.assertTrue(existing, f"No spill dir found in {candidates}")
        finally:
            import shutil
            shutil.rmtree(test_home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
