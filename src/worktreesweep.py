#!/usr/bin/env python3
"""WorktreeSweep - find stale Git worktrees and clean them up, safely.

WorktreeSweep inspects every Git worktree attached to the repository you run it
in and labels each one:

* ``REMOVAL CANDIDATE`` - every safety condition holds (see ``Sweeper.decide``).
* ``MANUAL REVIEW``     - Git cannot prove the worktree is safe to remove.
* ``KEEP``              - provably not stale, or the main worktree.

The default mode is a read-only report. ``--apply`` removes candidates, but only
after re-checking each one and asking for a per-worktree confirmation.

Design rules this module must preserve (see docs/ARCHITECTURE.md):

* Standard library only. No third-party runtime dependencies, ever.
* Every Git invocation is an argument list (``shell=False``); never a shell
  string, never a ``sh -c``.
* Removal happens only through ``git worktree remove <path>``. Never
  ``git worktree prune``, never a recursive delete, never ``--force``.
* The base branch is never guessed: it comes from ``--base`` or from
  ``refs/remotes/origin/HEAD``, and the tool refuses to run without one.
* Dry run by default. Removal needs ``--apply`` *and* an explicit per-worktree
  "yes". There is no blanket ``--yes`` and no ``--force`` flag.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

PROG = "worktreesweep"
DEFAULT_OLDER_THAN_DAYS = 30

# Verdicts.
CANDIDATE = "REMOVAL CANDIDATE"
MANUAL = "MANUAL REVIEW"
KEEP = "KEEP"

ZERO_SHA = "0" * 40


class GitError(Exception):
    """A Git command failed where WorktreeSweep needed it to succeed."""


def run_git(args, cwd=None):
    """Run Git with an explicit argument list and return the CompletedProcess.

    ``shell=False`` plus one list element per argument means paths containing
    spaces, quotes, globs or newlines can never be reinterpreted by a shell.
    """
    return subprocess.run(
        ["git"] + [str(arg) for arg in args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )


def decode(raw):
    """Decode Git output without ever raising on odd bytes."""
    return raw.decode("utf-8", "replace")


def path_key(path):
    """Canonical key for comparing paths (symlinks resolved, trailing slash gone)."""
    try:
        return os.path.realpath(path)
    except OSError:  # pragma: no cover - realpath is defensive here
        return os.path.normpath(path)


def short_sha(sha, length=8):
    return (sha or "")[:length]


def default_confirm(prompt):
    """Ask a yes/no question on stdin. Anything but an explicit yes is a no."""
    try:
        answer = input(prompt)
    except EOFError:
        # Non-interactive stdin must never be read as consent.
        return False
    return answer.strip().lower() in ("y", "yes")


class Worktree:
    """One entry of ``git worktree list --porcelain -z`` plus its inspection."""

    def __init__(self, path=""):
        # Facts reported by git porcelain output.
        self.path = path
        self.head = None
        self.branch = None
        self.detached = False
        self.bare = False
        self.locked = False
        self.lock_reason = None
        self.prunable = False
        self.prunable_reason = None
        self.is_main = False
        # Facts gathered by inspecting the filesystem and the worktree itself.
        self.path_exists = False
        self.path_is_symlink = False
        self.head_valid = False
        self.branch_missing = False
        self.dirty = None
        self.tracked_changes = 0
        self.untracked_files = 0
        self.merged = None
        self.head_timestamp = None
        self.age_days = None
        self.error = None
        # Outcome.
        self.verdict = None
        self.reason = None

    # -- presentation helpers ------------------------------------------
    @property
    def branch_label(self):
        if self.bare:
            return "(bare repository)"
        if self.detached:
            return "DETACHED HEAD"
        if self.branch:
            if self.branch.startswith("refs/heads/"):
                return self.branch[len("refs/heads/"):]
            return self.branch
        return "(unknown)"

    def age_text(self):
        if self.head_timestamp is None:
            return "age unknown"
        when = datetime.datetime.fromtimestamp(self.head_timestamp).strftime("%Y-%m-%d")
        if self.age_days is None:
            return "committed %s" % when
        if self.age_days < 1:
            return "committed %s (less than a day ago)" % when
        return "committed %s (%d days ago)" % (when, int(self.age_days))

    def status_text(self):
        if self.dirty is None:
            return "not inspected"
        if self.dirty:
            return "dirty (%d tracked change(s), %d untracked file(s))" % (
                self.tracked_changes,
                self.untracked_files,
            )
        return "clean (no tracked or untracked changes)"

    def merged_text(self, base):
        if self.merged is True:
            return "yes (HEAD is an ancestor of %s)" % base
        if self.merged is False:
            return "NO (HEAD is not an ancestor of %s)" % base
        return "unknown"

    def to_dict(self, base):
        return {
            "path": self.path,
            "branch": self.branch,
            "branch_name": self.branch_label,
            "head": self.head,
            "head_short": short_sha(self.head),
            "detached": self.detached,
            "bare": self.bare,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
            "prunable": self.prunable,
            "prunable_reason": self.prunable_reason,
            "is_main": self.is_main,
            "path_exists": self.path_exists,
            "path_is_symlink": self.path_is_symlink,
            "branch_missing": self.branch_missing,
            "dirty": self.dirty,
            "tracked_changes": self.tracked_changes,
            "untracked_files": self.untracked_files,
            "merged_into_base": self.merged,
            "base": base,
            "head_commit_timestamp": self.head_timestamp,
            "head_commit_age_days": None if self.age_days is None else round(self.age_days, 2),
            "error": self.error,
            "verdict": self.verdict,
            "reason": self.reason,
        }


def parse_worktree_list(raw):
    """Parse ``git worktree list --porcelain -z`` output into Worktree objects.

    The NUL delimited format terminates every field with ``\\0`` and separates
    records with an empty field, so paths containing spaces or newlines survive
    intact. Fields are ``<keyword> [<value>]``; unknown keywords are ignored so a
    newer Git cannot break the parser.
    """
    worktrees = []
    current = None

    def finish():
        if current is not None:
            worktrees.append(current)

    for field in raw.split("\0"):
        if field == "":
            finish()
            current = None
            continue
        if current is None:
            current = Worktree()
        keyword, _, value = field.partition(" ")
        if keyword == "worktree":
            current.path = value
        elif keyword == "HEAD":
            current.head = value
        elif keyword == "branch":
            current.branch = value
        elif keyword == "detached":
            current.detached = True
        elif keyword == "bare":
            current.bare = True
        elif keyword == "locked":
            current.locked = True
            current.lock_reason = value or None
        elif keyword == "prunable":
            current.prunable = True
            current.prunable_reason = value or None
        # Anything else is ignored on purpose.

    finish()
    return worktrees


def removal_guard(worktree):
    """Last line of defence. Returns a refusal reason, or None if removal is allowed.

    This is checked immediately before ``git worktree remove``, independently of
    the verdict that produced the candidate list, so an unknown or unsafe state
    can never reach the mutating command.
    """
    if worktree is None:
        return "unknown worktree state"
    if worktree.bare:
        return "a bare repository has no worktree to remove"
    if worktree.is_main:
        return "the main worktree is never removed"
    if worktree.locked:
        return "locked worktrees are never removed"
    if worktree.prunable:
        return "git reports this worktree as prunable"
    if worktree.path_is_symlink:
        return "the path is a symlink; refusing to follow it"
    if not worktree.path_exists:
        return "the path does not exist"
    if worktree.detached:
        return "HEAD is detached; Git cannot prove what this worktree is for"
    if worktree.branch_missing:
        return "the branch ref no longer exists"
    if not worktree.head_valid:
        return "HEAD does not point at a commit"
    if worktree.error:
        return "git reported an error: %s" % worktree.error
    if worktree.dirty:
        return "the worktree has uncommitted or untracked changes"
    return None


class Sweeper:
    """Collects worktree facts and decides what, if anything, may be removed."""

    def __init__(self, base=None, older_than_days=DEFAULT_OLDER_THAN_DAYS, git=None,
                 confirm=None, cwd=None):
        self.base_arg = base
        self.older_than_days = older_than_days
        self.git = git or run_git
        self.confirm = confirm or default_confirm
        self.cwd = cwd or os.getcwd()
        self.base = None
        self.repo_root = None

    # -- git plumbing ---------------------------------------------------
    def _git_ok(self, args, cwd=None, what=None):
        proc = self.git(list(args), cwd=cwd or self.cwd)
        if proc.returncode != 0:
            message = decode(proc.stderr).strip() or "exit code %d" % proc.returncode
            raise GitError("%s: %s" % (what or ("git " + " ".join(args)), message))
        return proc

    # -- discovery ------------------------------------------------------
    def list_worktrees(self):
        """Enumerate worktrees via the NUL delimited porcelain format."""
        proc = self._git_ok(
            ["worktree", "list", "--porcelain", "-z"],
            what="git worktree list",
        )
        worktrees = parse_worktree_list(decode(proc.stdout))
        self._mark_main_worktree(worktrees)
        return worktrees

    def _mark_main_worktree(self, worktrees):
        """Flag the main worktree conservatively, from two independent signals."""
        main_keys = set()
        # git documents the main worktree first in `worktree list` output.
        if worktrees:
            main_keys.add(path_key(worktrees[0].path))
        # ... and `.git` (the common dir) belongs to the main worktree.
        proc = self.git(["rev-parse", "--git-common-dir"], cwd=self.cwd)
        if proc.returncode == 0:
            common = decode(proc.stdout).strip()
            if common:
                if not os.path.isabs(common):
                    common = os.path.join(self.cwd, common)
                common = os.path.normpath(common)
                if os.path.basename(common) == ".git":
                    main_keys.add(path_key(os.path.dirname(common)))
                else:  # bare repository
                    main_keys.add(path_key(common))
        for index, worktree in enumerate(worktrees):
            if index == 0 or path_key(worktree.path) in main_keys:
                worktree.is_main = True

    def resolve_base(self):
        """Return the rev to compare against, or refuse to guess."""
        if self.base_arg:
            proc = self.git(
                ["rev-parse", "--verify", "--quiet", "%s^{commit}" % self.base_arg],
                cwd=self.cwd,
            )
            if proc.returncode != 0:
                raise GitError(
                    "base branch %r could not be resolved; pass a valid --base"
                    % self.base_arg
                )
            return self.base_arg

        proc = self.git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=self.cwd)
        if proc.returncode == 0:
            ref = decode(proc.stdout).strip()
            if ref.startswith("refs/remotes/"):
                return ref[len("refs/remotes/"):]
        raise GitError(
            "could not determine the base branch: refs/remotes/origin/HEAD is not set.\n"
            "WorktreeSweep will not guess which branch is the base. Pass it explicitly,"
            " for example: %s --base main" % PROG
        )

    # -- inspection -----------------------------------------------------
    def inspect_all(self):
        """Enumerate, inspect and judge every worktree. Returns a list of Worktree."""
        # A bare repository has no top level; fall back to the working directory.
        proc = self.git(["rev-parse", "--show-toplevel"], cwd=self.cwd)
        self.repo_root = decode(proc.stdout).strip() if proc.returncode == 0 else self.cwd
        if not self.repo_root:
            self.repo_root = self.cwd
        self.base = self.resolve_base()
        worktrees = self.list_worktrees()
        for worktree in worktrees:
            self.probe(worktree)
            verdict, reason = self.decide(worktree)
            worktree.verdict = verdict
            worktree.reason = reason
        return worktrees

    def probe(self, worktree):
        """Gather facts about one worktree. Never raises: failures land in .error."""
        # Filesystem facts first, and never follow a symlinked path.
        worktree.path_is_symlink = os.path.islink(worktree.path)
        worktree.path_exists = os.path.exists(worktree.path)
        worktree.head_valid = bool(worktree.head) and worktree.head != ZERO_SHA

        if worktree.bare or worktree.prunable or worktree.path_is_symlink:
            return
        if not worktree.path_exists:
            return

        try:
            self._probe_status(worktree)
            if worktree.branch:
                proc = self.git(
                    ["rev-parse", "--verify", "--quiet", worktree.branch],
                    cwd=worktree.path,
                )
                worktree.branch_missing = proc.returncode != 0
            if worktree.head_valid:
                self._probe_merge_state(worktree)
        except GitError as exc:
            worktree.error = str(exc)

    def _probe_status(self, worktree):
        """Untracked files count as dirty: a worktree is clean only if Git says so."""
        proc = self._git_ok(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree.path,
            what="git status in %s" % worktree.path,
        )
        tracked = 0
        untracked = 0
        for line in decode(proc.stdout).splitlines():
            if not line.strip():
                continue
            if line.startswith("??"):
                untracked += 1
            else:
                tracked += 1
        worktree.tracked_changes = tracked
        worktree.untracked_files = untracked
        worktree.dirty = bool(tracked or untracked)

    def _probe_merge_state(self, worktree):
        proc = self.git(
            ["merge-base", "--is-ancestor", worktree.head, self.base],
            cwd=worktree.path,
        )
        if proc.returncode == 0:
            worktree.merged = True
        elif proc.returncode == 1:
            worktree.merged = False
        else:
            raise GitError(
                "git merge-base --is-ancestor %s %s: %s"
                % (
                    short_sha(worktree.head),
                    self.base,
                    decode(proc.stderr).strip() or "exit code %d" % proc.returncode,
                )
            )

        proc = self._git_ok(
            ["show", "-s", "--format=%ct", worktree.head],
            cwd=worktree.path,
            what="git show %s" % short_sha(worktree.head),
        )
        try:
            worktree.head_timestamp = int(decode(proc.stdout).strip())
        except ValueError:
            raise GitError("could not read a commit timestamp for %s" % short_sha(worktree.head))
        now = datetime.datetime.now()
        committed = datetime.datetime.fromtimestamp(worktree.head_timestamp)
        worktree.age_days = (now - committed).total_seconds() / 86400.0

    # -- decision table -------------------------------------------------
    def decide(self, worktree):
        """Return ``(verdict, reason)``. A candidate must satisfy every condition."""
        if worktree.bare:
            return KEEP, "bare repository; there is no worktree to remove"
        if worktree.is_main:
            return KEEP, "main worktree - never removed"
        if worktree.prunable or not worktree.path_exists:
            return MANUAL, "path is missing (git reports it as prunable)"
        if worktree.locked:
            reason = "locked"
            if worktree.lock_reason:
                reason += " (%s)" % worktree.lock_reason
            return MANUAL, reason + " - locked worktrees are never removed"
        if worktree.path_is_symlink:
            return MANUAL, "path is a symlink - refusing to follow it"
        if worktree.branch_missing:
            return MANUAL, "branch ref %s no longer exists" % worktree.branch
        if not worktree.head_valid:
            return MANUAL, "HEAD does not point at a commit (unborn or missing ref)"
        if worktree.error:
            return MANUAL, "git error: %s" % worktree.error
        if worktree.detached:
            return MANUAL, "detached HEAD - nothing ties this worktree to a branch"
        if worktree.dirty:
            return KEEP, "dirty - %d tracked change(s), %d untracked file(s)" % (
                worktree.tracked_changes,
                worktree.untracked_files,
            )
        if worktree.merged is False:
            return KEEP, "HEAD is not merged into %s" % self.base
        if worktree.merged is None:
            return MANUAL, "could not determine whether HEAD is merged into %s" % self.base
        if worktree.age_days is None:
            return MANUAL, "could not determine the HEAD commit age"
        if worktree.age_days <= self.older_than_days:
            return KEEP, "HEAD commit is newer than the %d day threshold (%s)" % (
                self.older_than_days,
                worktree.age_text(),
            )
        return CANDIDATE, "clean, merged into %s, and %s" % (self.base, worktree.age_text())

    # -- the only mutating code path ------------------------------------
    def _fresh_candidate(self, path):
        """Re-inspect the repository and return ``(worktree, None)`` if *path* is
        still a removal candidate, else ``(None, refusal reason)``."""
        try:
            worktrees = self.inspect_all()
        except GitError as exc:
            return None, "git failed while re-checking: %s" % exc
        for worktree in worktrees:
            if path_key(worktree.path) == path_key(path):
                if worktree.verdict == CANDIDATE:
                    return worktree, None
                return None, "it is no longer a removal candidate (%s)" % worktree.reason
        return None, "git no longer lists this worktree"

    def apply(self, worktrees, out):
        """Interactively remove candidates. Returns a process exit code."""
        candidates = [wt for wt in worktrees if wt.verdict == CANDIDATE]
        if not candidates:
            out.write("\nNo removal candidates. Nothing was removed.\n")
            return 0

        removed, skipped, refused = [], [], []
        for worktree in candidates:
            out.write("\nRemoval candidate: %s\n" % worktree.path)
            out.write(
                "  branch %s | HEAD %s | %s\n"
                % (worktree.branch_label, short_sha(worktree.head), worktree.age_text())
            )

            fresh, refusal = self._fresh_candidate(worktree.path)
            if fresh is None:
                out.write("  SKIPPED - %s\n" % refusal)
                skipped.append(worktree.path)
                continue

            refusal = removal_guard(fresh)
            if refusal:
                out.write("  SKIPPED - %s\n" % refusal)
                skipped.append(worktree.path)
                continue

            if not self.confirm("  Remove this worktree? [y/N] "):
                out.write("  SKIPPED - not confirmed. Nothing was removed.\n")
                skipped.append(worktree.path)
                continue

            # Re-check one more time, immediately before the mutating command.
            fresh, refusal = self._fresh_candidate(worktree.path)
            if fresh is None:
                out.write("  SKIPPED - %s\n" % refusal)
                skipped.append(worktree.path)
                continue
            refusal = removal_guard(fresh)
            if refusal:
                out.write("  SKIPPED - %s\n" % refusal)
                skipped.append(worktree.path)
                continue

            proc = self.git(["worktree", "remove", fresh.path], cwd=self.repo_root)
            if proc.returncode != 0:
                message = decode(proc.stderr).strip() or "git worktree remove exited %d" % proc.returncode
                out.write("  REFUSED - git did not remove it: %s\n" % message)
                refused.append((fresh.path, message))
                continue

            out.write("  REMOVED - %s\n" % fresh.path)
            removed.append(fresh.path)

        out.write(
            "\nApplied: %d removed | %d skipped | %d refused by git\n"
            % (len(removed), len(skipped), len(refused))
        )
        if refused:
            for path, message in refused:
                out.write("  git refused %s: %s\n" % (path, message))
            return 1
        return 0


# -- rendering ----------------------------------------------------------
def count_verdicts(worktrees, verdict):
    return sum(1 for wt in worktrees if wt.verdict == verdict)


def render_report(out, worktrees, repo_root, base, older_than_days, dry_run):
    mode = "dry run (nothing will be removed)" if dry_run else "apply (each removal asks for confirmation)"
    out.write("WorktreeSweep - reviewing %d worktree(s) in %s\n" % (len(worktrees), repo_root))
    out.write(
        "Base branch: %s | stale threshold: %d day(s) | mode: %s\n\n"
        % (base, older_than_days, mode)
    )

    for index, worktree in enumerate(worktrees, 1):
        out.write("[%d] %s\n" % (index, worktree.path or "(unknown path)"))
        out.write("    Branch:  %s\n" % worktree.branch_label)
        out.write("    HEAD:    %s  %s\n" % (short_sha(worktree.head), worktree.age_text()))
        locked = "yes" if worktree.locked else "no"
        if worktree.locked and worktree.lock_reason:
            locked += " (%s)" % worktree.lock_reason
        out.write("    Locked:  %s\n" % locked)
        out.write("    Status:  %s\n" % worktree.status_text())
        out.write("    Merged:  %s\n" % worktree.merged_text(base))
        out.write("    Verdict: %s - %s\n\n" % (worktree.verdict, worktree.reason))

    out.write(
        "Summary: %d removal candidate(s) | %d need manual review | %d to keep\n"
        % (
            count_verdicts(worktrees, CANDIDATE),
            count_verdicts(worktrees, MANUAL),
            count_verdicts(worktrees, KEEP),
        )
    )

    candidates = [wt for wt in worktrees if wt.verdict == CANDIDATE]
    if candidates:
        out.write("\nRemoval candidates (safe by the rules below, still your call):\n")
        for worktree in candidates:
            out.write(
                "  - %s  (%s, HEAD %s, %s)\n"
                % (worktree.path, worktree.branch_label, short_sha(worktree.head), worktree.age_text())
            )
    manual = [wt for wt in worktrees if wt.verdict == MANUAL]
    if manual:
        out.write("\nNeeds manual review:\n")
        for worktree in manual:
            out.write("  - %s  (%s)\n" % (worktree.path, worktree.reason))

    if dry_run:
        out.write(
            "\nDry run: nothing was removed. Re-run with --apply to remove candidates;"
            " you will be asked to confirm each one.\n"
        )


def build_json(worktrees, repo_root, base, older_than_days, dry_run):
    return {
        "schema": "worktreesweep/v1",
        "repo_root": repo_root,
        "base": base,
        "older_than_days": older_than_days,
        "dry_run": dry_run,
        "worktrees": [wt.to_dict(base) for wt in worktrees],
        "summary": {
            "total": len(worktrees),
            "removal_candidates": count_verdicts(worktrees, CANDIDATE),
            "manual_review": count_verdicts(worktrees, MANUAL),
            "keep": count_verdicts(worktrees, KEEP),
        },
    }


# -- CLI ----------------------------------------------------------------
def non_negative_days(text):
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("expected a whole number of days, got %r" % text)
    if value < 0:
        raise argparse.ArgumentTypeError("days must be zero or greater, got %d" % value)
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Find stale Git worktrees and clean them up - safely. Reports only by"
            " default; --apply removes candidates after a per-worktree confirmation."
        ),
        epilog=(
            "A worktree is only ever a removal candidate when it is a linked (non-main)"
            " worktree, its path exists, it is not locked, it is clean including"
            " untracked files, its HEAD is an ancestor of the base branch, and its HEAD"
            " commit is older than --older-than.\n"
            "\n"
            "There is deliberately no --force and no blanket --yes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--older-than",
        metavar="DAYS",
        type=non_negative_days,
        default=DEFAULT_OLDER_THAN_DAYS,
        help="age threshold in days for calling a worktree stale (default: %(default)s)",
    )
    parser.add_argument(
        "--base",
        metavar="BRANCH",
        default=None,
        help=(
            "base branch used for the merged check; defaults to the branch"
            " refs/remotes/origin/HEAD points at, and is never guessed"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="remove candidates that you confirm one by one (default is a dry run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print a machine-readable dry-run report instead of the text report",
    )
    return parser


def main(argv=None, git=None, confirm=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)

    if args.apply and args.as_json:
        err.write("error: --apply cannot be combined with --json. --json is a dry-run report.\n")
        return 2

    sweeper = Sweeper(base=args.base, older_than_days=args.older_than, git=git, confirm=confirm)
    try:
        worktrees = sweeper.inspect_all()
    except GitError as exc:
        err.write("error: %s\n" % exc)
        return 2

    if args.as_json:
        json.dump(
            build_json(worktrees, sweeper.repo_root, sweeper.base, args.older_than, dry_run=True),
            out,
            indent=2,
        )
        out.write("\n")
        return 0

    render_report(
        out,
        worktrees,
        sweeper.repo_root,
        sweeper.base,
        args.older_than,
        dry_run=not args.apply,
    )
    if args.apply:
        return sweeper.apply(worktrees, out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write(
            "\nInterrupted. Any removal that already finished stays removed;"
            " re-run to see the current state.\n"
        )
        sys.exit(130)
