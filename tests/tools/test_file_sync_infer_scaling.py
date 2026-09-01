"""Performance regression test for ``_infer_host_path``.

``_infer_host_path`` is called once per synced file and used to walk the whole
mapping, re-running ``_is_upload_only_host_path`` (a filesystem
``Path.resolve()``) for every candidate.  With ~6.6k files x ~6.6k mappings that
was 3.4M resolve calls and 27M ``lstat`` syscalls -- 111s of a 146s profiled
``sync_back`` against a real remote.

The directory index makes the filter run once per mapping entry per sync
instead of once per (file, mapping) pair.  These tests pin the call count,
which is the property that actually decays, rather than wall-clock timing.
"""

from unittest.mock import patch

from tools.environments.file_sync import FileSyncManager


def _manager() -> FileSyncManager:
    return FileSyncManager(
        get_files_fn=lambda: [],
        upload_fn=lambda *a, **k: None,
        delete_fn=lambda *a, **k: None,
        bulk_upload_fn=lambda *a, **k: None,
        bulk_download_fn=lambda *a, **k: None,
    )


def _mapping(n: int) -> list[tuple[str, str]]:
    return [(f"/host/dir{i}/f.txt", f"/remote/dir{i}/f.txt") for i in range(n)]


class TestInferHostPathScaling:
    def test_filter_runs_once_per_mapping_entry_not_per_file(self):
        """The upload-only filter must not rerun for every file."""
        mgr = _manager()
        mapping = _mapping(50)

        with patch.object(
            FileSyncManager,
            "_is_upload_only_host_path",
            return_value=False,
        ) as filt:
            for i in range(20):
                mgr._infer_host_path(
                    f"/remote/dir{i}/new.txt",
                    mapping,
                    upload_only_host_paths=set(),
                )

        # Pre-fix: 20 files x 50 mappings = up to 1000 calls.
        # With the index: 50 (one pass), reused for every subsequent file.
        assert filt.call_count <= len(mapping), (
            f"filter ran {filt.call_count} times for {len(mapping)} mappings "
            "-- the per-file rescan is back"
        )

    def test_inference_result_is_still_correct(self):
        """Caching must not change which host path is returned."""
        mgr = _manager()
        mapping = _mapping(5)
        got = mgr._infer_host_path(
            "/remote/dir3/brand_new.txt",
            mapping,
            upload_only_host_paths=set(),
        )
        assert got == "/host/dir3/brand_new.txt"

    def test_first_match_wins_is_preserved(self):
        """Two mappings sharing a remote dir: the first must win."""
        mgr = _manager()
        mapping = [
            ("/host/first/f.txt", "/remote/shared/f.txt"),
            ("/host/second/g.txt", "/remote/shared/g.txt"),
        ]
        got = mgr._infer_host_path(
            "/remote/shared/new.txt",
            mapping,
            upload_only_host_paths=set(),
        )
        assert got == "/host/first/new.txt"

    def test_upload_only_mappings_are_still_skipped(self):
        """An upload-only host dir must never be inferred as a download target."""
        mgr = _manager()
        mapping = [
            ("/host/secret/creds", "/remote/secret/creds"),
            ("/host/ok/f.txt", "/remote/secret/f.txt"),
        ]

        def only_secret(host_path, upload_only):
            return host_path == "/host/secret/creds"

        with patch.object(
            FileSyncManager,
            "_is_upload_only_host_path",
            side_effect=only_secret,
        ):
            got = mgr._infer_host_path(
                "/remote/secret/new.txt",
                mapping,
                upload_only_host_paths={"/host/secret/creds"},
            )

        assert got == "/host/ok/new.txt"

    def test_cache_refreshes_when_filter_set_changes(self):
        """A different upload-only set must not reuse a stale index."""
        mgr = _manager()
        mapping = [("/host/a/f.txt", "/remote/a/f.txt")]

        first = mgr._infer_host_path(
            "/remote/a/new.txt", mapping, upload_only_host_paths=set()
        )
        assert first == "/host/a/new.txt"

        with patch.object(
            FileSyncManager, "_is_upload_only_host_path", return_value=True
        ):
            second = mgr._infer_host_path(
                "/remote/a/new.txt",
                mapping,
                upload_only_host_paths={"/host/a/f.txt"},
            )

        assert second is None, "stale index reused across a different filter set"
