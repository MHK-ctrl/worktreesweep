#!/usr/bin/env python3
"""Tests for WorktreeSweep.

Every test builds its own tiny temporary Git repository and tears it down again.
There is no network access, no committed fixture and no third-party dependency.
Run them with::

    python3 -m unittest discover -s tests -v
"""

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import worktreesweep as wts  # noqa: E402  (imported after the sys.path tweak)

# A fixed, old timestamp so "stale" tests never depend on the wall clock.
OLD_DATE = "2020-01-01T00:00:00+0000"


class TempRepoTestCase(unittest.TestCase):
    """Scaffolding: a tiny repo with one old commit on ``main``."""

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="worktreesweep-test-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._original_cwd)

        # resolve() keeps macOS /var -> /private/var out of path comparisons.
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()

        self.git("init", "-q", "-b", "main")  # cwd defaults to self.repo
        self.git("config", "user.name", "WorktreeSweep Tests")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self.git("add", "README.md")
        self.commit("initial commit", date=OLD_DATE)

        os.chdir(str(self.repo))

    # -- git helpers ----------------------------------------------------
    def git(self, *args, cwd=None, date=None):
        env = dict(os.environ)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        proc = subprocess.run(
            ["git"] + [str(arg) for arg in args],
            cwd=str(cwd or self.repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "git %s failed (rc=%d): %s"
            % (" ".join(str(a) for a in args), proc.returncode, proc.stderr.decode("utf-8", "replace")),
        )
        return proc

    def commit(self, message, date=None, cwd=None):
        self.git("commit", "-q", "--allow-empty", "-m", message, date=date, cwd=cwd)

    def add_worktree(self, name, branch=None, at="HEAD", detach=False, path=None):
        """Add a linked worktree at *at* (default: HEAD, which is the old commit)."""
        path = Path(path) if path else (self.root / name)
        if detach:
            self.git("worktree", "add", "-q", "--detach", str(path), at)
        else:
            self.git("worktree", "add", "-q", "-b", branch or name, str(path), at)
        return path

    # -- CLI helpers ----------------------------------------------------
    def run_cli(self, argv, confirm=None, git=None):
        out, err = io.StringIO(), io.StringIO()
        code = wts.main(list(argv), git=git, confirm=confirm, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def report(self, extra=(), confirm=None, git=None):
        code, out, err = self.run_cli(["--base", "main", "--json", *extra], confirm=confirm, git=git)
        self.assertEqual(code, 0, "cli exited %d\nstdout:\n%s\nstderr:\n%s" % (code, out, err))
        return json.loads(out)

    def worktree_entry(self, report, path):
        wanted = wts.path_key(str(path))
        for entry in report["worktrees"]:
            if wts.path_key(entry["path"]) == wanted:
                return entry
        self.fail("worktree %s is missing from the report" % path)

    @staticmethod
    def no_prompt(prompt):
        raise AssertionError("the CLI must not prompt here: %r" % prompt)


class CandidateTests(TempRepoTestCase):
    """The decision table: what may be removed and what must be protected."""

    def test_main_worktree_is_never_flagged(self):
        entry = self.worktree_entry(self.report(), self.repo)
        self.assertTrue(entry["is_main"])
        self.assertEqual(entry["verdict"], wts.KEEP)
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_clean_merged_old_linked_worktree_is_flagged(self):
        worktree = self.add_worktree("wt-stale")
        report = self.report()
        entry = self.worktree_entry(report, worktree)
        self.assertEqual(entry["verdict"], wts.CANDIDATE)
        self.assertFalse(entry["dirty"])
        self.assertTrue(entry["merged_into_base"])
        self.assertGreater(entry["head_commit_age_days"], 30)
        self.assertEqual(report["summary"]["removal_candidates"], 1)

    def test_recent_commit_is_not_flagged(self):
        self.commit("recent commit")
        worktree = self.add_worktree("wt-fresh")
        entry = self.worktree_entry(self.report(), worktree)
        self.assertLess(entry["head_commit_age_days"], 1)
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_dirty_worktree_is_blocked(self):
        worktree = self.add_worktree("wt-dirty")
        (worktree / "README.md").write_text("changed but uncommitted\n", encoding="utf-8")
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["dirty"])
        self.assertGreater(entry["tracked_changes"], 0)
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_untracked_file_blocks_candidate(self):
        worktree = self.add_worktree("wt-untracked")
        (worktree / "scratch.txt").write_text("notes\n", encoding="utf-8")
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["dirty"])
        self.assertGreater(entry["untracked_files"], 0)
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_unmerged_branch_is_blocked(self):
        worktree = self.add_worktree("wt-unmerged")
        (worktree / "work.txt").write_text("work\n", encoding="utf-8")
        self.git("add", "work.txt", cwd=worktree)
        self.commit("unmerged work", date=OLD_DATE, cwd=worktree)
        entry = self.worktree_entry(self.report(), worktree)
        self.assertFalse(entry["merged_into_base"])
        self.assertIn("not merged", entry["reason"])
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_locked_worktree_is_blocked(self):
        worktree = self.add_worktree("wt-locked")
        self.git("worktree", "lock", "--reason", "paused work", str(worktree))
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["locked"])
        self.assertEqual(entry["lock_reason"], "paused work")
        self.assertEqual(entry["verdict"], wts.MANUAL)

    def test_detached_head_is_manual_review(self):
        worktree = self.add_worktree("wt-detached", detach=True)
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["detached"])
        self.assertEqual(entry["verdict"], wts.MANUAL)

    def test_missing_path_is_manual_review(self):
        worktree = self.add_worktree("wt-pruned")
        shutil.rmtree(str(worktree))
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["prunable"])
        self.assertEqual(entry["verdict"], wts.MANUAL)

    def test_missing_branch_ref_is_manual_review(self):
        worktree = self.add_worktree("wt-nobranch")
        self.git("update-ref", "-d", "refs/heads/wt-nobranch")
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["branch_missing"])
        self.assertEqual(entry["verdict"], wts.MANUAL)
        self.assertIn("no longer exists", entry["reason"])

    def test_symlinked_worktree_path_is_manual_review(self):
        worktree = self.add_worktree("wt-link")
        moved = self.root / "wt-link-target"
        os.rename(str(worktree), str(moved))
        os.symlink(str(moved), str(worktree))
        entry = self.worktree_entry(self.report(), worktree)
        self.assertTrue(entry["path_is_symlink"])
        self.assertEqual(entry["verdict"], wts.MANUAL)

    def test_path_with_spaces_is_handled(self):
        worktree = self.root / "wt with spaces"
        self.add_worktree("spaces-branch", path=worktree)
        entry = self.worktree_entry(self.report(), worktree)
        self.assertEqual(entry["path"], str(worktree))
        self.assertEqual(entry["verdict"], wts.CANDIDATE)

    def test_older_than_threshold_is_configurable(self):
        ten_days_ago = (
            datetime.datetime.now().astimezone() - datetime.timedelta(days=10)
        ).strftime("%Y-%m-%dT%H:%M:%S%z")
        self.commit("ten days old", date=ten_days_ago)
        worktree = self.add_worktree("wt-ten-days")

        entry = self.worktree_entry(self.report(), worktree)
        self.assertEqual(entry["verdict"], wts.KEEP)

        report = self.report(extra=["--older-than", "5"])
        self.assertEqual(report["older_than_days"], 5)
        self.assertEqual(self.worktree_entry(report, worktree)["verdict"], wts.CANDIDATE)


class SafetyTests(TempRepoTestCase):
    """Nothing is removed unless --apply and an explicit per-worktree yes."""

    def test_dry_run_makes_no_changes(self):
        candidate = self.add_worktree("wt-candidate")
        dirty = self.add_worktree("wt-dirty-keep")
        (dirty / "README.md").write_text("keep me\n", encoding="utf-8")
        before = self.git("worktree", "list", "--porcelain").stdout

        code, out, err = self.run_cli(["--base", "main"], confirm=self.no_prompt)
        self.assertEqual(code, 0)
        self.assertIn(wts.CANDIDATE, out)
        self.assertIn("Dry run: nothing was removed", out)
        self.assertTrue(candidate.exists())
        self.assertTrue(dirty.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)

    def test_declining_confirmation_makes_no_changes(self):
        candidate = self.add_worktree("wt-candidate")
        before = self.git("worktree", "list", "--porcelain").stdout

        code, out, err = self.run_cli(
            ["--base", "main", "--apply"], confirm=lambda prompt: False
        )
        self.assertEqual(code, 0)
        self.assertIn("not confirmed", out)
        self.assertTrue(candidate.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)

    def test_confirming_removes_only_the_selected_worktree(self):
        first = self.add_worktree("wt-first")
        second = self.add_worktree("wt-second")
        dirty = self.add_worktree("wt-dirty")
        (dirty / "README.md").write_text("uncommitted\n", encoding="utf-8")

        asked = []

        def confirm(prompt):
            asked.append(prompt)
            return len(asked) == 1  # yes to the first, no to the second

        code, out, err = self.run_cli(["--base", "main", "--apply"], confirm=confirm)
        self.assertEqual(code, 0)
        self.assertEqual(len(asked), 2, "every candidate must be asked about separately")
        self.assertFalse(first.exists(), "the confirmed worktree should be removed")
        self.assertTrue(second.exists(), "the declined worktree must survive")
        self.assertTrue(dirty.exists(), "a dirty worktree must never be touched")
        self.assertTrue(self.repo.exists(), "the main worktree must survive")

        remaining = self.git("worktree", "list", "--porcelain").stdout.decode("utf-8")
        self.assertNotIn(str(first), remaining)
        self.assertIn(str(second), remaining)
        # the repo still works after the removal
        self.git("status", "--porcelain")

    def test_apply_with_no_candidates_removes_nothing(self):
        code, out, err = self.run_cli(
            ["--base", "main", "--apply"], confirm=self.no_prompt
        )
        self.assertEqual(code, 0)
        self.assertIn("No removal candidates", out)


class GitErrorTests(TempRepoTestCase):
    """A Git failure always means refusal, never removal."""

    def failing_runner(self, fail_prefix):
        real = wts.run_git

        def runner(args, cwd=None):
            args = list(args)
            if args[: len(fail_prefix)] == list(fail_prefix):
                return subprocess.CompletedProcess(["git"] + args, 1, b"", b"simulated git failure")
            return real(args, cwd=cwd)

        return runner

    def test_git_error_during_inspection_prevents_removal(self):
        worktree = self.add_worktree("wt-status-fails")
        entry = self.worktree_entry(self.report(git=self.failing_runner(["status"])), worktree)
        self.assertEqual(entry["verdict"], wts.MANUAL)
        self.assertIn("simulated git failure", entry["error"])
        self.assertNotEqual(entry["verdict"], wts.CANDIDATE)

    def test_git_error_during_removal_refuses_to_remove(self):
        worktree = self.add_worktree("wt-remove-fails")
        code, out, err = self.run_cli(
            ["--base", "main", "--apply"],
            confirm=lambda prompt: True,
            git=self.failing_runner(["worktree", "remove"]),
        )
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        self.assertIn("simulated git failure", out)
        self.assertTrue(worktree.exists(), "a refused removal must leave the worktree alone")


class BaseBranchTests(TempRepoTestCase):
    """The base branch is discovered or demanded, never guessed."""

    def test_base_is_not_guessed_without_origin_head(self):
        code, out, err = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("--base", err)
        self.assertIn("will not guess", err)

    def test_explicit_base_that_does_not_resolve_fails(self):
        code, out, err = self.run_cli(["--base", "no-such-branch"])
        self.assertEqual(code, 2)
        self.assertIn("could not be resolved", err)

    def test_base_is_discovered_from_origin_head(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        worktree = self.add_worktree("wt-stale")
        code, out, err = self.run_cli(["--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["base"], "origin/main")
        self.assertEqual(self.worktree_entry(report, worktree)["verdict"], wts.CANDIDATE)


class CliTests(TempRepoTestCase):
    def test_json_report_shape_and_dry_run_flag(self):
        self.add_worktree("wt-stale")
        report = self.report()
        self.assertEqual(report["schema"], "worktreesweep/v1")
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["older_than_days"], 30)
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(
            set(report["summary"]),
            {"total", "removal_candidates", "manual_review", "keep"},
        )
        entry = report["worktrees"][0]
        for key in (
            "path",
            "branch",
            "head",
            "locked",
            "dirty",
            "merged_into_base",
            "head_commit_age_days",
            "verdict",
            "reason",
        ):
            self.assertIn(key, entry)

    def test_apply_cannot_be_combined_with_json(self):
        code, out, err = self.run_cli(["--base", "main", "--apply", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("--json", err)

    def test_force_and_blanket_yes_do_not_exist(self):
        for flag in ("--force", "--yes"):
            with self.assertRaises(SystemExit) as caught:
                wts.build_parser().parse_args([flag])
            self.assertEqual(caught.exception.code, 2)

    def test_older_than_rejects_negative_values(self):
        with self.assertRaises(SystemExit) as caught:
            wts.build_parser().parse_args(["--older-than", "-1"])
        self.assertEqual(caught.exception.code, 2)


class ParserTests(unittest.TestCase):
    """Unit tests for the pure functions, no repository needed."""

    def test_porcelain_parser_handles_spaces_newlines_and_reasons(self):
        raw = (
            "worktree /tmp/repo\0HEAD " + "a" * 40 + "\0branch refs/heads/main\0\0"
            "worktree /tmp/space dir\0HEAD " + "b" * 40 + "\0branch refs/heads/feat\0\0"
            "worktree /tmp/line\nbreak\0HEAD " + "c" * 40 + "\0detached\0\0"
            "worktree /tmp/locked\0HEAD " + "d" * 40
            + "\0branch refs/heads/x\0locked paused work; do not touch\0\0"
            "worktree /tmp/gone\0HEAD " + "e" * 40
            + "\0branch refs/heads/y\0prunable gitdir file points to non-existent location\0\0"
        )
        worktrees = wts.parse_worktree_list(raw)
        self.assertEqual(len(worktrees), 5)
        self.assertEqual(worktrees[0].path, "/tmp/repo")
        self.assertEqual(worktrees[0].branch, "refs/heads/main")
        self.assertEqual(worktrees[1].path, "/tmp/space dir")
        self.assertEqual(worktrees[2].path, "/tmp/line\nbreak")
        self.assertTrue(worktrees[2].detached)
        self.assertTrue(worktrees[3].locked)
        self.assertEqual(worktrees[3].lock_reason, "paused work; do not touch")
        self.assertTrue(worktrees[4].prunable)
        self.assertIn("non-existent", worktrees[4].prunable_reason)

    def test_removal_guard_refuses_every_unsafe_state(self):
        def guarded(**attributes):
            worktree = wts.Worktree("/tmp/example")
            worktree.path_exists = True
            worktree.head_valid = True
            for name, value in attributes.items():
                setattr(worktree, name, value)
            return wts.removal_guard(worktree)

        self.assertIsNone(guarded())
        self.assertIsNotNone(wts.removal_guard(None))
        for unsafe in (
            {"is_main": True},
            {"bare": True},
            {"locked": True},
            {"prunable": True},
            {"path_is_symlink": True},
            {"path_exists": False},
            {"detached": True},
            {"branch_missing": True},
            {"head_valid": False},
            {"error": "boom"},
            {"dirty": True},
        ):
            self.assertIsNotNone(guarded(**unsafe), "guard must refuse %s" % unsafe)


if __name__ == "__main__":
    unittest.main()
