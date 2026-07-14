import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.sync_cloud_mirror import (
    STATE_RELATIVE_PATH,
    SyncError,
    _repository_name_from_remote,
    run_sync,
)


class CloudMirrorSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        root = Path(self._temporary_directory.name)
        self.source = root / "canonical"
        self.target = root / "cloud"
        self.source.mkdir()
        self.target.mkdir()
        (self.target / ".git").mkdir()
        (self.source / "deploy").mkdir()
        self.manifest = self.source / "deploy" / "cloud_manifest.txt"
        (self.source / "VERSION").write_text("test-version\n", encoding="utf-8")

    def _write_source(self, relative: str, content: str) -> None:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_manifest(self, *paths: str) -> None:
        self.manifest.write_text("\n".join(paths) + "\n", encoding="utf-8")

    def _run(self, mode: str):
        return run_sync(
            source_root=self.source,
            target_root=self.target,
            manifest_path=self.manifest,
            mode=mode,
            source_commit="a" * 40,
            canonical_repository="example/canonical",
            allow_non_git_source=True,
        )

    def test_repository_provenance_accepts_github_urls_and_rejects_local_paths(self) -> None:
        expected = "davidbenizri25-wq/trading-elite-system"
        self.assertEqual(
            _repository_name_from_remote(
                "https://github.com/davidbenizri25-wq/trading-elite-system.git"
            ),
            expected,
        )
        self.assertEqual(
            _repository_name_from_remote(
                "git@github-trading-elite:davidbenizri25-wq/trading-elite-system.git"
            ),
            expected,
        )
        with self.assertRaisesRegex(SyncError, "GitHub repository URL"):
            _repository_name_from_remote(
                "/Users/davidbenizri/Documents/GitHub/trading-elite-system"
            )

    def test_apply_copies_allowlist_and_only_deletes_previously_managed_files(self) -> None:
        self._write_source("alpha.txt", "alpha v1\n")
        self._write_source("retire.txt", "managed then retired\n")
        self._write_source("private-note.txt", "never allowlisted\n")
        self._write_manifest("alpha.txt", "retire.txt")
        (self.target / "unmanaged.txt").write_text("preserve me\n", encoding="utf-8")

        first_plan = self._run("apply")

        self.assertEqual(first_plan.copy_paths, ("alpha.txt", "retire.txt"))
        self.assertEqual((self.target / "alpha.txt").read_text(), "alpha v1\n")
        self.assertFalse((self.target / "private-note.txt").exists())

        self._write_source("alpha.txt", "alpha v2\n")
        self._write_manifest("alpha.txt")
        second_plan = self._run("apply")

        self.assertEqual(second_plan.copy_paths, ("alpha.txt",))
        self.assertEqual(second_plan.delete_paths, ("retire.txt",))
        self.assertFalse((self.target / "retire.txt").exists())
        self.assertEqual((self.target / "unmanaged.txt").read_text(), "preserve me\n")
        state = json.loads(
            (self.target / Path(STATE_RELATIVE_PATH.as_posix())).read_text(encoding="utf-8")
        )
        self.assertEqual(state["managed_paths"], ["alpha.txt"])
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["canonical_repository"], "example/canonical")
        self.assertEqual(state["canonical_commit"], "a" * 40)
        self.assertEqual(state["version"], "test-version")

    def test_dry_run_reports_drift_without_writing(self) -> None:
        self._write_source("alpha.txt", "source\n")
        self._write_manifest("alpha.txt")

        plan = self._run("dry-run")

        self.assertTrue(plan.has_drift)
        self.assertEqual(plan.copy_paths, ("alpha.txt",))
        self.assertFalse((self.target / "alpha.txt").exists())
        self.assertFalse((self.target / Path(STATE_RELATIVE_PATH.as_posix())).exists())

    def test_check_is_clean_after_apply_and_detects_content_drift(self) -> None:
        self._write_source("alpha.txt", "canonical\n")
        self._write_manifest("alpha.txt")
        self._run("apply")

        self.assertFalse(self._run("check").has_drift)

        (self.target / "alpha.txt").write_text("manual cloud edit\n", encoding="utf-8")
        drift = self._run("check")
        self.assertTrue(drift.has_drift)
        self.assertEqual(drift.copy_paths, ("alpha.txt",))

    def test_schema_one_state_migrates_without_losing_managed_ownership(self) -> None:
        self._write_source("alpha.txt", "canonical\n")
        self._write_manifest("alpha.txt")
        self._run("apply")
        state_path = self.target / Path(STATE_RELATIVE_PATH.as_posix())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for key in ("canonical_repository", "canonical_commit", "version"):
            state.pop(key)
        state["schema_version"] = 1
        state_path.write_text(json.dumps(state), encoding="utf-8")

        plan = self._run("apply")

        self.assertTrue(plan.state_needs_update)
        migrated = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["managed_paths"], ["alpha.txt"])

    def test_rejects_same_tree_and_nested_tree_targets(self) -> None:
        self._write_source("alpha.txt", "canonical\n")
        self._write_manifest("alpha.txt")
        (self.source / ".git").mkdir()

        with self.assertRaisesRegex(SyncError, "separate trees"):
            run_sync(
                source_root=self.source,
                target_root=self.source,
                manifest_path=self.manifest,
                mode="dry-run",
            )

        nested_target = self.source / "nested-cloud"
        nested_target.mkdir()
        (nested_target / ".git").mkdir()
        with self.assertRaisesRegex(SyncError, "separate trees"):
            run_sync(
                source_root=self.source,
                target_root=nested_target,
                manifest_path=self.manifest,
                mode="dry-run",
            )

    def test_rejects_sensitive_and_traversal_manifest_paths(self) -> None:
        self._write_manifest(".streamlit/secrets.toml")
        with self.assertRaisesRegex(SyncError, "Sensitive path|secrets"):
            self._run("dry-run")

        self._write_manifest("../outside.txt")
        with self.assertRaisesRegex(SyncError, "traversal"):
            self._run("dry-run")

    def test_refuses_target_parent_symlink_that_escapes_repo(self) -> None:
        self._write_source("redirected/file.txt", "canonical\n")
        self._write_manifest("redirected/file.txt")
        external = Path(self._temporary_directory.name) / "external"
        external.mkdir()
        try:
            (self.target / "redirected").symlink_to(external, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - platform capability guard
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaisesRegex(SyncError, "escapes through a symlink"):
            self._run("apply")
        self.assertFalse((external / "file.txt").exists())

    def test_corrupt_state_refuses_to_guess_prior_ownership(self) -> None:
        self._write_source("alpha.txt", "canonical\n")
        self._write_manifest("alpha.txt")
        state = self.target / Path(STATE_RELATIVE_PATH.as_posix())
        state.parent.mkdir(parents=True)
        state.write_text("not-json\n", encoding="utf-8")

        with self.assertRaisesRegex(SyncError, "refusing deletions"):
            self._run("apply")

    def test_modified_stale_managed_file_is_preserved(self) -> None:
        self._write_source("keep.txt", "keep\n")
        self._write_source("retire.txt", "generated\n")
        self._write_manifest("keep.txt", "retire.txt")
        self._run("apply")
        (self.target / "retire.txt").write_text("manual cloud change\n", encoding="utf-8")
        self._write_manifest("keep.txt")

        with self.assertRaisesRegex(SyncError, "changed since the last sync"):
            self._run("apply")
        self.assertEqual(
            (self.target / "retire.txt").read_text(encoding="utf-8"),
            "manual cloud change\n",
        )

    def test_non_git_sources_require_explicit_test_opt_in(self) -> None:
        self._write_source("alpha.txt", "canonical\n")
        self._write_manifest("alpha.txt")

        with self.assertRaisesRegex(SyncError, "Git checkout"):
            run_sync(
                source_root=self.source,
                target_root=self.target,
                manifest_path=self.manifest,
                mode="apply",
                source_commit="a" * 40,
                canonical_repository="example/canonical",
            )

    def test_apply_and_check_require_clean_matching_git_source(self) -> None:
        self._write_source("alpha.txt", "canonical v1\n")
        self._write_manifest("alpha.txt")
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Mirror Test",
                "-c",
                "user.email=mirror-test@example.invalid",
                "commit",
                "-qm",
                "initial",
            ],
            check=True,
        )
        first_commit = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with self.assertRaisesRegex(SyncError, "source-commit is required"):
            run_sync(
                source_root=self.source,
                target_root=self.target,
                manifest_path=self.manifest,
                mode="apply",
                canonical_repository="example/canonical",
            )

        run_sync(
            source_root=self.source,
            target_root=self.target,
            manifest_path=self.manifest,
            mode="apply",
            source_commit=first_commit,
            canonical_repository="example/canonical",
        )
        self._write_source("alpha.txt", "dirty change\n")
        with self.assertRaisesRegex(SyncError, "dirty"):
            run_sync(
                source_root=self.source,
                target_root=self.target,
                manifest_path=self.manifest,
                mode="check",
                source_commit=first_commit,
                canonical_repository="example/canonical",
            )

        self._write_source("alpha.txt", "canonical v2\n")
        subprocess.run(["git", "-C", str(self.source), "add", "alpha.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Mirror Test",
                "-c",
                "user.email=mirror-test@example.invalid",
                "commit",
                "-qm",
                "second",
            ],
            check=True,
        )
        with self.assertRaisesRegex(SyncError, "does not match"):
            run_sync(
                source_root=self.source,
                target_root=self.target,
                manifest_path=self.manifest,
                mode="check",
                source_commit=first_commit,
                canonical_repository="example/canonical",
            )


if __name__ == "__main__":
    unittest.main()
