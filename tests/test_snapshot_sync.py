from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import git_sync as core
import snapshot_sync as snapshot
from test_git_sync import GitFixture


class SnapshotTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.repo = replace(self.repo, strategy="snapshot")

    def entries(self, plan):
        return {entry["path"]: entry for entry in plan["entries"]}

    def align(self):
        plan = snapshot.prepare(self.repo)
        for entry in plan["entries"]:
            if entry["choice"] == "pending":
                snapshot.resolve(self.repo, entry["path"], "source")
        snapshot.apply(self.repo)

    def unrelated(self):
        self.git(self.consumer, "checkout", "--orphan", "internal")
        self.git(self.consumer, "rm", "-rf", ".")
        self.commit(self.consumer, "app.txt", "internal initial\n")
        self.repo = replace(self.repo, target_branch="internal")

    def test_unrelated_histories_require_first_review_and_apply_tree_only(self):
        self.unrelated()
        before = self.head()
        result = core.sync_one(self.repo)
        self.assertEqual("REVIEW_REQUIRED", result.status, result.detail)
        self.assertEqual(before, self.head())
        plan = snapshot.prepare(self.repo)
        self.assertTrue(plan["first_sync"])
        self.assertEqual("pending", self.entries(plan)["app.txt"]["choice"])
        snapshot.resolve(self.repo, "app.txt", "source")
        result = snapshot.apply(self.repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual("initial\n", (self.consumer / "app.txt").read_text())
        self.assertEqual(before, self.git(self.consumer, "show", "-s", "--format=%P", "HEAD"))
        self.assertFalse(snapshot.plan_directory(self.repo, 120).exists())
        self.assertEqual("", self.git(self.consumer, "branch", "--list", "sync-backup/*"))
        self.assertEqual(before, core.oid(self.consumer, self.repo.ref_prefix + "/backup", 120))
        self.assert_clean()

    def test_preserved_paths_and_local_only_files_remain(self):
        self.unrelated()
        self.commit(self.consumer, "config/internal.ini", "private=yes\n")
        self.commit(self.consumer, "internal-only.py", "private code\n")
        self.push_change("config/internal.ini", "public-default\n")
        self.repo = replace(self.repo, preserve_paths=("config/**",))
        plan = snapshot.prepare(self.repo)
        self.assertEqual("preserve", self.entries(plan)["config/internal.ini"]["choice"])
        snapshot.resolve(self.repo, "app.txt", "source")
        snapshot.resolve(self.repo, "internal-only.py", "local")
        snapshot.apply(self.repo)
        self.push_change("other.txt", "new public code\n")
        result = core.sync_one(self.repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual("private=yes\n", (self.consumer / "config/internal.ini").read_text())
        self.assertEqual("private code\n", (self.consumer / "internal-only.py").read_text())
        self.assertEqual("new public code\n", (self.consumer / "other.txt").read_text())
        self.assert_clean()

    def test_later_upstream_update_automatically_applies_without_new_branches(self):
        self.align()
        before = self.head()
        self.push_change()
        result = core.sync_one(self.repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual("upstream\n", (self.consumer / "app.txt").read_text())
        self.assertEqual(before, self.git(self.consumer, "show", "-s", "--format=%P", "HEAD"))
        self.assertEqual(["main"], self.git(self.consumer, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())
        self.assertFalse(snapshot.plan_directory(self.repo, 120).exists())

    def test_clean_three_way_merge_preserves_internal_changes(self):
        self.push_change(text="a\nb\nc\nd\ne\n")
        self.align()
        self.commit(self.consumer, "app.txt", "internal a\nb\nc\nd\ne\n")
        self.push_change(text="a\nb\nc\nd\nsource e\n")
        result = core.sync_one(self.repo)
        self.assertEqual("UPDATED", result.status, result.detail)
        self.assertEqual("internal a\nb\nc\nd\nsource e\n", (self.consumer / "app.txt").read_text())
        self.assert_clean()

    def test_conflicts_never_modify_worktree_and_review_is_not_auto_applied(self):
        self.align()
        self.commit(self.consumer, "app.txt", "private\n")
        self.push_change(text="public\n")
        before = self.head()
        self.assertEqual("REVIEW_REQUIRED", core.sync_one(self.repo).status)
        self.assertEqual("private\n", (self.consumer / "app.txt").read_text())
        snapshot.resolve(self.repo, "app.txt", "merged", merged_content=b"combined\n")
        self.assertEqual("REVIEW_REQUIRED", core.sync_one(self.repo).status)
        self.assertEqual(before, self.head())
        snapshot.apply(self.repo)
        self.assertEqual("combined\n", (self.consumer / "app.txt").read_text())
        self.assert_clean()

    def test_prepare_keeps_a_clean_plan_until_explicit_apply(self):
        self.align()
        self.push_change()
        snapshot.prepare(self.repo)
        before = self.head()
        result = core.sync_one(self.repo)
        self.assertEqual("REVIEW_REQUIRED", result.status, result.detail)
        self.assertEqual(before, self.head())
        snapshot.apply(self.repo)

    def test_source_delete_respects_locally_modified_file(self):
        self.align()
        self.commit(self.consumer, "app.txt", "private\n")
        self.git(self.producer, "rm", "app.txt")
        self.git(self.producer, "commit", "-m", "Delete app")
        self.git(self.producer, "push", "origin", "main")
        result = core.sync_one(self.repo)
        self.assertEqual("REVIEW_REQUIRED", result.status, result.detail)
        snapshot.resolve(self.repo, "app.txt", "local")
        snapshot.apply(self.repo)
        self.assertEqual("private\n", (self.consumer / "app.txt").read_text())

    def test_unmodified_file_deleted_upstream_is_deleted_after_baseline(self):
        self.align()
        self.git(self.producer, "rm", "app.txt")
        self.git(self.producer, "commit", "-m", "Delete app")
        self.git(self.producer, "push", "origin", "main")
        self.assertEqual("UPDATED", core.sync_one(self.repo).status)
        self.assertFalse((self.consumer / "app.txt").exists())

    def test_worktree_change_invalidates_old_plan_without_losing_resolution(self):
        self.push_change()
        plan = snapshot.prepare(self.repo)
        snapshot.resolve(self.repo, "app.txt", "source")
        self.commit(self.consumer, "local.txt", "my work\n")
        with self.assertRaisesRegex(core.SyncError, "变化"):
            snapshot.apply(self.repo)
        self.assertTrue(snapshot.plan_directory(self.repo, 120).exists())
        snapshot.discard(self.repo)
        self.assertFalse(snapshot.plan_directory(self.repo, 120).exists())
        self.assertEqual("my work\n", (self.consumer / "local.txt").read_text())

    def test_ignored_file_and_file_directory_collisions_block_apply(self):
        self.align()
        (self.consumer / ".git" / "info" / "exclude").write_text("secret.ini\n")
        (self.consumer / "secret.ini").write_text("private\n")
        self.push_change("secret.ini", "public\n")
        snapshot.prepare(self.repo)
        with self.assertRaisesRegex(core.SyncError, "忽略"):
            snapshot.apply(self.repo)
        self.assertEqual("private\n", (self.consumer / "secret.ini").read_text())

    def test_binary_choice_is_preserved_byte_for_byte(self):
        data = b"\x00\xff\r\nbinary\x00"
        (self.producer / "asset.bin").write_bytes(data)
        self.git(self.producer, "add", "asset.bin")
        self.git(self.producer, "commit", "-m", "Binary")
        self.git(self.producer, "push", "origin", "main")
        plan = snapshot.prepare(self.repo)
        self.assertTrue(self.entries(plan)["asset.bin"]["binary"])
        snapshot.apply(self.repo)
        self.assertEqual(data, (self.consumer / "asset.bin").read_bytes())

    def test_rewritten_source_requires_confirmation_even_without_text_conflict(self):
        self.align()
        self.push_change("one.txt", "one\n")
        self.assertEqual("UPDATED", core.sync_one(self.repo).status)
        self.git(self.producer, "reset", "--hard", self.initial)
        self.commit(self.producer, "two.txt", "two\n")
        self.git(self.producer, "push", "--force", "origin", "main")
        self.assertEqual("REVIEW_REQUIRED", core.sync_one(self.repo).status)
        self.assertTrue(snapshot.prepare(self.repo)["source_rewritten"])

    def test_push_all_does_not_publish_temporary_or_backup_refs(self):
        self.push_change()
        self.align()
        publication = self.base / "internal-publication.git"
        self.git(self.base, "init", "--bare", str(publication))
        self.git(self.consumer, "push", "--all", str(publication))
        refs = self.git(publication, "for-each-ref", "--format=%(refname)").splitlines()
        self.assertEqual(["refs/heads/main"], refs)

    def test_bridge_configurable_logs_and_json_protocol(self):
        config = core.Configuration([self.repo], log_directory=self.base / "chosen logs")
        config_path = self.base / "settings.json"
        core.write_config(config_path, config)
        result = subprocess.run([sys.executable, str(Path(core.__file__).with_name("ide_bridge.py")),
                                 "--config", str(config_path), "prepare", "--repo", self.repo.name],
                                capture_output=True, text=True, env=core.git_env())
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertTrue((self.base / "chosen logs" / "settings.log").is_file())
        self.assertEqual(config, core.load_config(config_path))

    def test_completed_ref_transaction_recovers_before_plan_cleanup(self):
        self.align()
        self.push_change()
        snapshot.prepare(self.repo)
        original = snapshot.shutil.rmtree

        def failed_cleanup(path, *args, **kwargs):
            if Path(path) == snapshot.plan_directory(self.repo, 120):
                raise OSError("simulated cleanup interruption")
            return original(path, *args, **kwargs)

        with mock.patch.object(snapshot.shutil, "rmtree", side_effect=failed_cleanup):
            with self.assertRaises(OSError):
                snapshot.apply(self.repo)
        self.assertEqual("upstream\n", (self.consumer / "app.txt").read_text())
        self.assertTrue(snapshot.plan_directory(self.repo, 120).exists())
        result = snapshot.apply(self.repo)
        self.assertEqual("UP_TO_DATE", result.status, result.detail)
        self.assertFalse(snapshot.plan_directory(self.repo, 120).exists())

    def test_bridge_refuses_to_overwrite_unloaded_config(self):
        config_path = self.base / "settings.json"
        config = core.Configuration([self.repo])
        core.write_config(config_path, config)
        before = config_path.read_bytes()
        result = subprocess.run([sys.executable, str(Path(core.__file__).with_name("ide_bridge.py")),
                                 "--config", str(config_path), "save"], input=json.dumps(config.as_dict()),
                                capture_output=True, text=True, env=core.git_env())
        self.assertEqual(1, result.returncode)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(before, config_path.read_bytes())

    def test_bridge_initialization_revision_and_multi_repository_monitoring(self):
        config_path = self.base / "ui-settings.json"
        bad = replace(self.repo, name="unavailable", path=self.base / "missing")
        config = core.Configuration([bad, self.repo], log_directory=self.base / "ui logs")

        def call(operation, request=None):
            result = subprocess.run([sys.executable, str(Path(core.__file__).with_name("ide_bridge.py")),
                                     "--config", str(config_path), operation],
                                    input=json.dumps(request) if request is not None else None,
                                    capture_output=True, text=True, env=core.git_env())
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)

        saved = call("save", config.as_dict())
        loaded = call("get")
        self.assertEqual(saved["revision"], loaded["revision"])
        request = loaded["config"]
        request["_expected_revision"] = loaded["revision"]
        request["interval_seconds"] = 30
        updated = call("save", request)
        self.assertNotEqual(saved["revision"], updated["revision"])
        results = call("auto-all")["results"]
        self.assertEqual(["BLOCKED", "REVIEW_REQUIRED"], [item["outcome"]["status"] for item in results])
        self.assertTrue(results[1]["plan"]["manual_review"])


if __name__ == "__main__":
    unittest.main()
