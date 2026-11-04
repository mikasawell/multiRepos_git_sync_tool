import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import git_sync as sync


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="git-sync-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.remote = self.base / "remote.git"
        self.producer = self.base / "computer 1"
        self.consumer = self.base / "computer 2"
        self.git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.base, "clone", str(self.remote), str(self.producer))
        self.commit(self.producer, "app.txt", "initial\n")
        self.git(self.producer, "push", "origin", "main")
        self.git(self.base, "clone", str(self.remote), str(self.consumer))
        self.repo = sync.Repository("demo", self.consumer, "origin", "main", "main")
        self.initial = self.head()

    def git(self, path, *args, check=True):
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                                text=True, encoding="utf-8", env=sync.git_env())
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
        return result.stdout.strip()

    def commit(self, path, filename, text):
        target = path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.git(path, "add", "--", filename)
        self.git(path, "commit", "-m", f"Update {filename}")
        return self.git(path, "rev-parse", "HEAD")

    def push_change(self, filename="app.txt", text="upstream\n"):
        head = self.commit(self.producer, filename, text)
        self.git(self.producer, "push", "origin", "main")
        return head

    def head(self):
        return self.git(self.consumer, "rev-parse", "HEAD")

    def assert_clean(self):
        self.assertEqual("", self.git(self.consumer, "status", "--porcelain"))

    def backups(self):
        return self.git(self.consumer, "for-each-ref", "--format=%(objectname)",
                        self.repo.ref_prefix + "/backup").splitlines()

    def make_conflict(self):
        self.commit(self.consumer, "app.txt", "internal\n")
        self.push_change()
        return replace(self.repo, strategy="merge")


class GitSyncTests(GitFixture):
    def test_up_to_date(self):
        result = sync.sync_one(self.repo)
        self.assertEqual("UP_TO_DATE", result.status, result.detail)
        self.assertEqual([], self.backups())

    def test_fast_forward_creates_backup_without_pushing(self):
        upstream = self.push_change()
        result = sync.sync_one(self.repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual(upstream, self.head())
        self.assertEqual([self.initial], self.backups())
        self.assertEqual(upstream, self.git(self.remote, "rev-parse", "refs/heads/main"))
        self.assert_clean()

    def test_different_source_and_target_branch_names(self):
        self.git(self.consumer, "switch", "-c", "internal")
        upstream = self.push_change()
        result = sync.sync_one(replace(self.repo, target_branch="internal"))
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual(upstream, self.head())
        self.assertEqual(self.initial, self.git(self.consumer, "rev-parse", "main"))

    def test_source_url_does_not_change_origin(self):
        upstream = self.push_change()
        before = self.git(self.consumer, "remote", "get-url", "origin")
        result = sync.sync_one(replace(self.repo, source=str(self.remote)))
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual(upstream, self.head())
        self.assertEqual(before, self.git(self.consumer, "remote", "get-url", "origin"))

    def test_linked_worktree_merges_without_touching_main_worktree(self):
        linked = self.base / "dedicated sync workspace"
        self.git(self.consumer, "worktree", "add", "-b", "internal", str(linked), "main")
        own = self.commit(linked, "internal.ini", "private=yes\n")
        upstream = self.push_change()
        repo = replace(self.repo, path=linked, target_branch="internal", strategy="merge")
        with sync.repository_lock(self.consumer):
            self.assertEqual("BLOCKED", sync.sync_one(repo).status)
        result = sync.sync_one(repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual(f"{own} {upstream}", self.git(linked, "show", "-s", "--format=%P", "HEAD"))
        self.assertEqual(self.initial, self.head())
        self.assertEqual("initial\n", (self.consumer / "app.txt").read_text())
        self.assert_clean()
        self.assertEqual("", self.git(linked, "status", "--porcelain"))
        self.assertEqual(2, self.git(self.consumer, "worktree", "list", "--porcelain").count("worktree "))

    def test_dirty_worktree_and_index_and_untracked_files_block(self):
        self.push_change()
        for kind in ("modified", "staged", "untracked"):
            with self.subTest(kind=kind):
                path = self.consumer / ("notes.txt" if kind == "untracked" else "app.txt")
                path.write_text("keep me\n")
                if kind == "staged":
                    self.git(self.consumer, "add", "app.txt")
                result = sync.sync_one(self.repo)
                self.assertEqual("BLOCKED", result.status, result.detail)
                self.assertEqual(self.initial, self.head())
                self.assertEqual("keep me\n", path.read_text())
                if kind == "untracked":
                    path.unlink()
                else:
                    self.git(self.consumer, "restore", "--source=HEAD", "--staged", "--worktree", "app.txt")
        self.assertEqual([], self.backups())

    def test_wrong_branch_and_detached_head_block(self):
        self.push_change()
        self.git(self.consumer, "switch", "-c", "work")
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status)
        self.assertIn("work", result.detail)
        self.git(self.consumer, "checkout", "--detach")
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status)
        self.assertIn("detached", result.detail)
        self.assertEqual(self.initial, self.head())

    def test_internal_commit_ahead_is_preserved(self):
        own = self.commit(self.consumer, "internal.ini", "private=yes\n")
        result = sync.sync_one(self.repo)
        self.assertEqual("AHEAD", result.status, result.detail)
        self.assertEqual(own, self.head())
        self.assertEqual([], self.backups())

    def test_divergence_blocks_by_default(self):
        own = self.commit(self.consumer, "internal.ini", "private=yes\n")
        self.push_change()
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual(own, self.head())
        self.assert_clean()

    def test_merge_preserves_both_histories_and_internal_settings(self):
        own = self.commit(self.consumer, "internal.ini", "private=yes\n")
        upstream = self.push_change()
        result = sync.sync_one(replace(self.repo, strategy="merge"))
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual("private=yes\n", (self.consumer / "internal.ini").read_text())
        self.assertEqual("upstream\n", (self.consumer / "app.txt").read_text())
        self.assertEqual(f"{own} {upstream}", self.git(self.consumer, "show", "-s", "--format=%P", "HEAD"))
        self.assertEqual([own], self.backups())
        self.assertEqual(upstream, self.git(self.remote, "rev-parse", "main"))
        self.assert_clean()

    def test_conflict_preview_never_touches_original_worktree(self):
        repo = self.make_conflict()
        before = self.head()
        result = sync.sync_one(repo)
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assertIn("app.txt", result.detail)
        self.assertEqual(before, self.head())
        self.assertEqual("internal\n", (self.consumer / "app.txt").read_text())
        self.assert_clean()
        self.assertEqual([], self.backups())
        self.assertEqual(1, self.git(self.consumer, "worktree", "list", "--porcelain").count("worktree "))
        # The same conflict remains safe on subsequent polling cycles.
        self.assertEqual("CONFLICT", sync.sync_one(repo).status)
        self.assert_clean()

    def test_branch_merge_options_cannot_silently_choose_theirs(self):
        repo = self.make_conflict()
        self.git(self.consumer, "config", "branch.main.mergeOptions", "-X theirs --autostash")
        result = sync.sync_one(repo)
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assertEqual("internal\n", (self.consumer / "app.txt").read_text())
        self.assert_clean()

    def test_interactive_decline_keeps_clean_worktree(self):
        repo = self.make_conflict()
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            result = sync.sync_one(repo, interactive=True)
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assertEqual([], self.backups())
        self.assert_clean()

    def test_interactive_accept_starts_manual_merge_and_blocks_next_run(self):
        repo = self.make_conflict()
        before = self.head()
        with mock.patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(io.StringIO()):
            result = sync.sync_one(repo, interactive=True)
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assertIn("git merge --abort", result.detail)
        self.assertEqual(before, self.head())
        self.assertTrue((self.consumer / ".git" / "MERGE_HEAD").exists())
        self.assertEqual([before], self.backups())
        self.assertEqual("BLOCKED", sync.sync_one(repo).status)
        self.git(self.consumer, "merge", "--abort")
        self.assert_clean()

    def test_interactive_ff_only_can_approve_clean_merge(self):
        self.commit(self.consumer, "internal.ini", "private=yes\n")
        self.push_change()
        with mock.patch("builtins.input", return_value="y"):
            result = sync.sync_one(self.repo, interactive=True)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assert_clean()

    def test_dry_run_fetches_without_branch_or_accepted_checkpoint_change(self):
        self.push_change()
        result = sync.sync_one(self.repo, dry_run=True)
        self.assertEqual("DRY_RUN", result.status, result.detail)
        self.assertEqual(self.initial, self.head())
        self.assertEqual([], self.backups())
        self.assertIsNone(sync.oid(self.consumer, self.repo.ref_prefix + "/accepted", 120))
        self.assert_clean()

    def test_dry_run_reports_merge_conflict(self):
        repo = self.make_conflict()
        result = sync.sync_one(repo, dry_run=True)
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assert_clean()

    def test_missing_branch_is_error_without_target_changes(self):
        result = sync.sync_one(replace(self.repo, source_branch="does-not-exist"))
        self.assertEqual("ERROR", result.status, result.detail)
        self.assertEqual(self.initial, self.head())
        self.assertEqual([], self.backups())
        self.assert_clean()

    def test_force_push_stays_blocked_across_retries_until_accepted(self):
        self.assertEqual("UP_TO_DATE", sync.sync_one(self.repo).status)
        first = self.push_change("version-one.txt", "one\n")
        self.assertEqual("UPDATED", sync.sync_one(self.repo).status)
        self.git(self.producer, "reset", "--hard", self.initial)
        second = self.commit(self.producer, "version-two.txt", "two\n")
        self.git(self.producer, "push", "--force", "origin", "main")
        for _ in range(2):
            result = sync.sync_one(replace(self.repo, strategy="merge"))
            self.assertEqual("BLOCKED", result.status, result.detail)
            self.assertIn("force push", result.detail)
            self.assertEqual(first, self.head())
        accepted = sync.sync_one(replace(self.repo, strategy="merge"), accept_rewritten=True)
        self.assertEqual("UPDATED", accepted.status, accepted.detail)
        self.assertTrue(sync.ancestor(self.consumer, first, self.head(), 120))
        self.assertTrue(sync.ancestor(self.consumer, second, self.head(), 120))
        self.assert_clean()

    def test_ignored_local_file_is_not_overwritten(self):
        self.commit(self.consumer, ".gitignore", "secret.ini\n")
        (self.consumer / "secret.ini").write_text("private-secret\n")
        self.push_change("secret.ini", "upstream-default\n")
        before = self.head()
        result = sync.sync_one(replace(self.repo, strategy="merge"))
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual("private-secret\n", (self.consumer / "secret.ini").read_text())
        self.assertEqual(before, self.head())

    def test_fast_forward_does_not_overwrite_ignored_file(self):
        (self.consumer / ".git" / "info" / "exclude").write_text("secret.ini\n")
        (self.consumer / "secret.ini").write_text("private\n")
        self.push_change("secret.ini", "public\n")
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual("private\n", (self.consumer / "secret.ini").read_text())
        self.assertEqual(self.initial, self.head())
        self.assertEqual([], self.backups())

    def test_source_file_cannot_overwrite_ignored_directory(self):
        (self.consumer / ".git" / "info" / "exclude").write_text("private/\n")
        (self.consumer / "private").mkdir()
        (self.consumer / "private" / "secret.ini").write_text("private\n")
        self.push_change("private", "source-file\n")
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual("private\n", (self.consumer / "private" / "secret.ini").read_text())
        self.assertEqual(self.initial, self.head())

    def test_ignored_file_collision_respects_case_insensitive_filesystems(self):
        self.git(self.consumer, "config", "core.ignorecase", "true")
        (self.consumer / ".git" / "info" / "exclude").write_text("secret.ini\n")
        (self.consumer / "secret.ini").write_text("private\n")
        self.push_change("Secret.ini", "public\n")
        result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual("private\n", (self.consumer / "secret.ini").read_text())
        self.assertEqual(self.initial, self.head())

    def test_index_flags_cannot_hide_internal_config_modifications(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                self.git(self.consumer, "update-index", "--" + flag, "app.txt")
                (self.consumer / "app.txt").write_text("private configuration\n")
                self.assert_clean()
                result = sync.sync_one(self.repo)
                self.assertEqual("BLOCKED", result.status, result.detail)
                self.assertEqual("private configuration\n", (self.consumer / "app.txt").read_text())
                self.git(self.consumer, "update-index", "--no-" + flag, "app.txt")
                self.git(self.consumer, "restore", "app.txt")

    def test_fetch_failure_does_not_modify_target(self):
        result = sync.sync_one(replace(self.repo, source=str(self.base / "unavailable.git")))
        self.assertEqual("ERROR", result.status, result.detail)
        self.assertEqual(self.initial, self.head())
        self.assert_clean()

    def test_merge_failure_keeps_backup_and_pauses_with_recovery_instructions(self):
        self.commit(self.consumer, "internal.ini", "private=yes\n")
        self.push_change()
        hook = self.consumer / ".git" / "hooks" / "pre-merge-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        before = self.head()
        result = sync.sync_one(replace(self.repo, strategy="merge"))
        self.assertEqual("CONFLICT", result.status, result.detail)
        self.assertIn("git merge --abort", result.detail)
        self.assertEqual(before, self.head())
        self.assertEqual([before], self.backups())
        self.assertEqual("BLOCKED", sync.sync_one(self.repo).status)
        self.git(self.consumer, "merge", "--abort")
        self.assert_clean()

    def test_concurrent_sync_is_blocked_and_os_lock_releases(self):
        with sync.repository_lock(self.consumer):
            result = sync.sync_one(self.repo)
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual("UP_TO_DATE", sync.sync_one(self.repo).status)

    def test_one_bad_repository_does_not_stop_other_repositories(self):
        bad = replace(self.repo, name="unavailable", path=self.base / "missing")
        with contextlib.redirect_stdout(io.StringIO()):
            outcomes = sync.run_sync(sync.Configuration([bad, self.repo]))
        self.assertEqual(["BLOCKED", "UP_TO_DATE"], [outcome.status for outcome in outcomes])

    def test_unrelated_history_never_merges(self):
        self.git(self.producer, "checkout", "--orphan", "separate")
        self.commit(self.producer, "separate.txt", "different project\n")
        self.git(self.producer, "push", "origin", "separate")
        result = sync.sync_one(replace(self.repo, source_branch="separate", strategy="merge"))
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertIn("共同祖先", result.detail)
        self.assert_clean()

    def test_head_change_during_preview_blocks_mutation(self):
        self.commit(self.consumer, "internal.ini", "private=yes\n")
        self.push_change()
        original_preview = sync.preview_merge

        def changing_preview(*args):
            result = original_preview(*args)
            self.commit(self.consumer, "parallel-edit.txt", "user changed HEAD\n")
            return result

        with mock.patch.object(sync, "preview_merge", side_effect=changing_preview):
            result = sync.sync_one(replace(self.repo, strategy="merge"))
        self.assertEqual("BLOCKED", result.status, result.detail)
        self.assertEqual([], self.backups())
        self.assertEqual("user changed HEAD\n", (self.consumer / "parallel-edit.txt").read_text())
        self.assert_clean()

    def test_config_round_trip_relative_paths_and_validation(self):
        path = self.base / "sync-config.json"
        data = sync.Configuration([self.repo]).as_dict()
        data["repositories"][0]["path"] = self.consumer.name
        path.write_text(json.dumps(data))
        loaded = sync.load_config(path)
        self.assertEqual(self.consumer, loaded.repositories[0].path)
        sync.write_config(path, loaded)
        self.assertEqual(loaded, sync.load_config(path))
        for change in ({"interval_seconds": 0}, {"version": True}, {"repositories": []}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**data, **change}))
                with self.assertRaises(sync.ConfigError):
                    sync.load_config(path)

    def test_init_and_cli_sync_are_usable_without_dependencies(self):
        script = Path(sync.__file__).resolve()
        config = self.base / "settings" / "sync-config.json"
        result = subprocess.run([
            sys_executable(), str(script), "--config", str(config), "init",
            "--repo", str(self.consumer), "--name", "demo", "--source", "origin",
            "--source-branch", "main", "--target-branch", "main", "--strategy", "ff-only",
        ], capture_output=True, text=True, env=sync.git_env())
        self.assertEqual(0, result.returncode, result.stderr)
        upstream = self.push_change()
        result = subprocess.run([sys_executable(), str(script), "--config", str(config), "sync"],
                                capture_output=True, text=True, env=sync.git_env())
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual(upstream, self.head())
        self.assertTrue((config.parent / ".git-sync" / "sync-config.log").is_file())

    def test_redaction_hides_http_credentials(self):
        self.assertEqual("https://***@github.com/a/b?token=***",
                         sync.redact("https://name:password@github.com/a/b?token=secret"))


def sys_executable():
    import sys
    return sys.executable


class WatchAndProcessTests(unittest.TestCase):
    def test_init_with_missing_repository_returns_readable_error(self):
        with tempfile.TemporaryDirectory(prefix="git-sync-invalid-init-") as directory:
            config = str(Path(directory) / "config.json")
            missing = str(Path(directory) / "missing")
            error = io.StringIO()
            with mock.patch.object(sync, "check_git"), contextlib.redirect_stderr(error):
                code = sync.main(["--config", config, "init", "--repo", missing])
            self.assertEqual(2, code)
            self.assertIn("仓库目录不存在", error.getvalue())
            self.assertFalse(Path(config).exists())

    def test_watch_reloads_config_and_only_prints_changed_results(self):
        repo = sync.Repository("demo", Path("/unused"), "origin", "main", "main")
        first = sync.Configuration([repo], interval_seconds=60)
        second = sync.Configuration([repo], interval_seconds=30)
        output = io.StringIO()
        with mock.patch.object(sync, "check_git"), \
                mock.patch.object(sync, "load_config", side_effect=[first, first, second]) as load, \
                mock.patch.object(sync, "setup_logging", return_value=Path("/unused.log")), \
                mock.patch.object(sync, "sync_one", return_value=sync.Outcome("demo", "UP_TO_DATE", "same")), \
                mock.patch.object(sync.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = sync.main(["watch"])
        self.assertEqual(0, code)
        self.assertEqual(3, load.call_count)
        self.assertEqual([mock.call(60), mock.call(30)], sleep.call_args_list)
        self.assertEqual(1, output.getvalue().count("[demo]"))

    def test_git_timeout_is_a_controlled_error(self):
        with mock.patch.object(sync.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaisesRegex(sync.GitError, "超时"):
                sync.git(Path.cwd(), "fetch", timeout=1)

    def test_git_hook_routing_environment_cannot_redirect_repository(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/wrong", "GIT_WORK_TREE": "/wrong",
                                          "GIT_INDEX_FILE": "/wrong/index"}):
            cleaned = sync.git_env()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.assertNotIn(key, cleaned)


if __name__ == "__main__":
    unittest.main()
