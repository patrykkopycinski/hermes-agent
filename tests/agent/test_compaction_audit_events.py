"""Durable compaction audit events (#104099): start/end brackets + orphan detection."""

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="compaction-audit-test-"))

from agent.compaction_audit import find_orphaned_compaction_starts, record_compaction_event
from hermes_state import SessionDB


def _store() -> SessionDB:
    fd, path = tempfile.mkstemp(prefix="audit-", suffix=".db")
    os.close(fd)
    os.unlink(path)
    db = SessionDB(Path(path))
    db.create_session("sess-a", source="cli")
    db.create_session("sess-b", source="cli")
    return db


class TestCompactionEventsStore(unittest.TestCase):
    def setUp(self):
        self.db = _store()

    def tearDown(self):
        self.db.close()

    def test_start_end_pair_is_not_orphaned(self):
        self.assertTrue(record_compaction_event(self.db, "sess-a", "att-1", "start", {"message_count": 40}))
        self.assertTrue(record_compaction_event(self.db, "sess-a", "att-1", "end", {"commit_status": "committed"}))
        self.assertEqual(find_orphaned_compaction_starts(self.db), [])

    def test_orphaned_start_detected_per_attempt(self):
        record_compaction_event(self.db, "sess-a", "att-1", "start", {})
        record_compaction_event(self.db, "sess-a", "att-1", "end", {"commit_status": "aborted"})
        record_compaction_event(self.db, "sess-b", "att-2", "start", {"approx_tokens": 190000})
        orphans = find_orphaned_compaction_starts(self.db)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["attempt_id"], "att-2")
        self.assertEqual(orphans[0]["session_id"], "sess-b")

    def test_orphan_query_scopes_to_session(self):
        record_compaction_event(self.db, "sess-a", "att-1", "start", {})
        record_compaction_event(self.db, "sess-b", "att-2", "start", {})
        self.assertEqual(len(find_orphaned_compaction_starts(self.db, "sess-a")), 1)
        self.assertEqual(find_orphaned_compaction_starts(self.db, "sess-a")[0]["session_id"], "sess-a")

    def test_end_event_survives_without_start(self):
        # Abort paths before lease acquisition emit telemetry (an ``end``) with no start; the
        # orphan query must stay silent — it only flags start-without-end.
        record_compaction_event(self.db, "sess-a", "att-pre", "end", {"failure_class": "cooldown"})
        self.assertEqual(find_orphaned_compaction_starts(self.db), [])

    def test_payload_is_content_free_and_bounded(self):
        record_compaction_event(self.db, "sess-a", "att-1", "start", {"blob": "x" * 50_000})
        rows = self.db._read_all("SELECT payload_json FROM compaction_events WHERE session_id = 'sess-a'")
        self.assertLessEqual(len(rows[0][0]), 8_192)
        self.assertIn("truncated", json.loads(rows[0][0]))

    def test_failed_write_never_raises(self):
        class Exploding:
            def append_compaction_event(self, *a, **k):
                raise RuntimeError("store is gone")

        self.assertFalse(record_compaction_event(Exploding(), "sess-a", "att-1", "start", {}))

    def test_missing_session_or_attempt_is_noop(self):
        self.assertFalse(record_compaction_event(self.db, "", "att-1", "start"))
        self.assertFalse(record_compaction_event(self.db, "sess-a", "", "start"))

    def test_store_without_append_method_is_noop(self):
        self.assertFalse(record_compaction_event(SimpleNamespace(), "sess-a", "att-1", "start"))

    def test_telemetry_emitter_persists_end_event(self):
        # Bites the wiring in _emit_compression_attempt_telemetry (#104099): reverting the
        # recorder call there makes this fail, so the audit path can't silently detach.
        from agent.conversation_compression import _emit_compression_attempt_telemetry

        agent = SimpleNamespace(_session_db=self.db, session_id="sess-a", model="m", provider="p",
                                _compression_attempt_id="att-emit")
        agent.context_compressor = SimpleNamespace(_last_compression_telemetry={"trigger_source": "auto"})
        _emit_compression_attempt_telemetry(
            agent, started_at=0.0, commit_status="committed", split_status="committed",
        )
        rows = self.db._read_all(
            "SELECT event, payload_json FROM compaction_events WHERE attempt_id = 'att-emit'"
        )
        self.assertEqual([r[0] for r in rows], ["end"])
        self.assertEqual(json.loads(rows[0][1])["commit_status"], "committed")


class TestEveryExitPathClosesTheBracket(unittest.TestCase):
    """An unpaired `start` is the crash signal, so every NON-crash exit of
    compress_context between the `start` write and the telemetry emitter must
    write an `end`. Any uncovered early return makes a clean run look like a
    crash and turns the orphan query into noise (#104099)."""

    def test_no_unbracketed_early_return_between_start_and_emitter(self):
        import ast
        import inspect

        from agent import conversation_compression as cc

        src = inspect.getsource(cc.compress_context)
        tree = ast.parse(textwrap.dedent(src)).body[0]

        def _audits(node) -> bool:
            """True when this statement records an audit `end` itself."""
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_audit":
                    if sub.args and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == "end":
                        return True
            return False

        # Helpers that own an emitter call, so returns fed by them are already bracketed.
        emitting_helpers = {
            "_run_summary_phase", "_candidate_rejected", "_emit_aborted_attempt_telemetry",
            "_emit_compression_attempt_telemetry",
        }
        # Values produced by an emitting helper: returning one means that helper already
        # wrote the `end` (verified for _run_summary_phase's abort_prompt path). NOTE:
        # `commit` is deliberately NOT here — _commit_compaction owns no emitter, so its
        # refusal return needs its own `end` and must stay biteable by this test.
        emitting_values = {"phase"}
        start_line = next(
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_audit"
            and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "start"
        )

        # Walk statement lists so a return can be checked against its own block's guard.
        unbracketed = []
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            for block in (body, getattr(parent, "orelse", None), getattr(parent, "finalbody", None)):
                if not isinstance(block, list):
                    continue
                for idx, stmt in enumerate(block):
                    if not isinstance(stmt, ast.Return) or stmt.lineno <= start_line:
                        continue
                    preceding = block[:idx]
                    if any(_audits(s) for s in preceding):
                        continue
                    guard = getattr(parent, "test", None)
                    names = {
                        n.func.id for n in ast.walk(guard or ast.Pass())
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    }
                    if names & emitting_helpers:
                        continue
                    if any(
                        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in emitting_helpers
                        for s in preceding for n in ast.walk(s)
                    ):
                        continue
                    # `return phase.<x>` / `return commit.<x>`: the helper that built the
                    # value emitted on that path, so the bracket is already closed.
                    if {
                        n.value.id for n in ast.walk(stmt)
                        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    } & emitting_values:
                        continue
                    unbracketed.append(stmt.lineno)

        self.assertEqual(
            unbracketed, [],
            f"compress_context returns at source lines {unbracketed} exit after the `start` audit row "
            "without writing an `end` — a clean run there is indistinguishable from a crash.",
        )

    def test_parent_rotated_adoption_writes_an_end(self):
        # Bites the adoption path specifically: reverting its _audit("end", ...) call
        # reproduces the false orphan a clean rotated-parent run used to leave behind.
        import ast
        import inspect

        from agent import conversation_compression as cc

        tree = ast.parse(textwrap.dedent(inspect.getsource(cc.compress_context))).body[0]
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            assigns = [
                n for n in ast.walk(node.test)
                if isinstance(n, ast.Name) and n.id == "_adopted"
            ]
            if not assigns:
                continue
            audited = any(
                isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_audit"
                and sub.args and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == "end"
                for stmt in node.body for sub in ast.walk(stmt)
            )
            self.assertTrue(
                audited,
                "the parent-rotated adoption return must write an audit `end`; without it a normal "
                "concurrent-compression outcome is recorded as an orphaned (crash) start.",
            )
            return
        self.fail("could not locate the `_adopted` early-return branch in compress_context")


if __name__ == "__main__":
    unittest.main()
