# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-05

First public release.

### Added

- Read-only report of every worktree attached to the current repository, with
  path, branch, locked state, dirtiness (including untracked files), merged status
  against the base branch, HEAD commit age and a verdict per worktree.
- Verdicts: `REMOVAL CANDIDATE`, `MANUAL REVIEW` and `KEEP`, each with the reason
  it was given.
- `--older-than <days>` (default 30) for the stale threshold.
- `--base <branch>` for the merged check, with discovery through
  `refs/remotes/origin/HEAD` when it is not passed. The base branch is never guessed.
- `--apply` for removal, asking for confirmation per worktree and re-checking
  eligibility immediately before each removal.
- `--json` for a machine-readable dry-run report.
- Parcel-safe parsing of `git worktree list --porcelain -z`, so paths containing
  spaces or newlines are handled correctly.
- Test suite of 28 tests, each using its own temporary Git repository.
- GitHub Actions workflow running the suite on Python 3.9 through 3.13 plus a
  stdlib-only import check.

### Safety

- Dry run by default; no mutating command runs without `--apply`.
- Removal requires `--apply` **and** an explicit per-worktree "yes"; the default
  answer is no and non-interactive stdin is treated as no.
- The main worktree, locked worktrees, dirty worktrees, symlinked paths, prunable
  paths, detached HEADs, missing branch refs and any git error are never removed.
- Removal happens only through `git worktree remove <path>`: no `--force`, no
  recursive delete, no `git worktree prune`.
- A failing git command refuses removal and leaves the worktree in place.
