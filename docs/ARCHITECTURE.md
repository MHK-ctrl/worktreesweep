# WorktreeSweep architecture

WorktreeSweep is deliberately one small file (`src/worktreesweep.py`) with no runtime
dependencies beyond the Python standard library and `git` on `PATH`. This document
explains how it works and which invariants future changes must preserve.

## Goal and non-goals

**Goal:** show a human which linked Git worktrees are provably safe to remove, and
remove only the ones that human explicitly confirms.

**Non-goals:** deciding policy for you, cleaning anything automatically, running as a
daemon, or talking to the network. WorktreeSweep is a report plus one narrow, guarded
action.

## Module layout

| Section | Contents | Purpose |
| --- | --- | --- |
| Git plumbing | `run_git`, `decode`, `path_key`, `GitError` | All process spawning and text handling live here |
| Model | `Worktree` | One worktree's raw facts, derived facts, verdict and reason |
| Parsing | `parse_worktree_list` | `git worktree list --porcelain -z` -> `list[Worktree]` |
| Safety | `removal_guard` | The last check before any mutating command |
| Engine | `Sweeper` | Discovery, inspection, decision, interactive apply |
| Output | `render_report`, `build_json`, `count_verdicts` | Text and machine-readable reports |
| CLI | `build_parser`, `main`, `non_negative_days` | Argument parsing, exit codes, wiring |

## Data flow

```
                git worktree list --porcelain -z
                             |
                    parse_worktree_list()
                             |
                 Sweeper._mark_main_worktree()
                             |
    +------------------------+-------------------------+
    |  for every worktree:                             |
    |    Sweeper.probe()      -> raw + measured facts  |
    |    Sweeper.decide()     -> verdict + reason      |
    +------------------------+-------------------------+
                             |
          +------------------+------------------+
          |                                     |
   render_report() (text)                 build_json()
          |
     --apply ? --> Sweeper.apply() --> confirm -> removal_guard -> git worktree remove
```

## The porcelain format we parse

`git worktree list --porcelain -z` is NUL terminated and looks like this
(`|` is a NUL here):

```
worktree /repo|HEAD <sha>|branch refs/heads/main||
worktree /repo-wt|HEAD <sha>|branch refs/heads/feat|locked paused work||
worktree /repo-gone|HEAD <sha>|branch refs/heads/x|prunable gitdir file points to non-existent location||
worktree /repo-detached|HEAD <sha>|detached||
worktree /repo-nobranch|HEAD 0000...|branch refs/heads/deleted||
```

Four properties matter, and `tests/test_worktreesweep.py` pins them:

1. An **empty field** ends a record, so splitting on NUL and treating `""` as a
   record terminator is correct.
2. Each field is `<keyword> [<value>]`; `locked` and `prunable` may carry a
   **free-form reason with spaces** (`locked paused work; do not touch`). The parser
   splits on the **first** space only.
3. A worktree whose branch ref was deleted still reports `branch refs/heads/...`
   but its **HEAD is the all-zero SHA**. `Worktree.branch_missing` and
   `Worktree.head_valid` cover that case.
4. Paths are reported absolute and are **never re-quoted**, which is why NUL
   delimiting is used: a path may contain spaces or newlines.

Unknown keywords are ignored on purpose so a newer git cannot crash the parser.

## The decision table

`Sweeper.decide()` returns the first matching row. This order *is* the policy:

| # | Condition | Verdict | Why |
| --- | --- | --- | --- |
| 1 | `bare` | `KEEP` | A bare repository has no worktree to remove |
| 2 | main worktree | `KEEP` | Hard rule: the main worktree is never removed |
| 3 | prunable, or path missing | `MANUAL REVIEW` | Git cannot inspect it; the human should `git worktree prune` |
| 4 | locked | `MANUAL REVIEW` | A lock is an explicit statement of intent |
| 5 | path is a symlink | `MANUAL REVIEW` | We refuse to follow symlinks, so we cannot verify the target |
| 6 | branch ref missing | `MANUAL REVIEW` | The worktree is orphaned, not stale |
| 7 | HEAD is not a valid commit | `MANUAL REVIEW` | Unborn or unreadable HEAD |
| 8 | any git error | `MANUAL REVIEW` | Failure is never treated as approval |
| 9 | detached HEAD | `MANUAL REVIEW` | Nothing ties it to a branch, so "merged" is meaningless |
| 10 | dirty (tracked or untracked) | `KEEP` | There is uncommitted work; never remove it |
| 11 | HEAD is not an ancestor of base | `KEEP` | Unmerged commits would be lost |
| 12 | merge status unknown | `MANUAL REVIEW` | Not provable |
| 13 | commit age unknown | `MANUAL REVIEW` | Not provable |
| 14 | commit age <= `--older-than` | `KEEP` | Too recent to call stale |
| 15 | everything above passed | `REMOVAL CANDIDATE` | Clean, merged and older than the threshold |

Rows 1-9 map onto the `MANUAL REVIEW` promise in the README: detached HEAD, missing
or deleted branch refs, prunable paths, locked worktrees and any git error are
surfaced for a human, never auto-flagged. "Dirty" and "unmerged" are `KEEP` because
they are *proofs* that the worktree is not safe to remove, not ambiguities.

## Facts, and the exact commands that produce them

| Fact | Command | Notes |
| --- | --- | --- |
| Worktree list | `git worktree list --porcelain -z` | NUL separated, see above |
| Main worktree | first record + `git rev-parse --git-common-dir` | Two independent signals, union of both |
| Dirtiness | `git status --porcelain=v1 --untracked-files=all` (cwd = the worktree) | Untracked files count as dirty |
| Merged | `git merge-base --is-ancestor <head> <base>` (cwd = the worktree) | Exit 0 = ancestor, 1 = not, anything else = error |
| Commit age | `git show -s --format=%ct <head>` (cwd = the worktree) | Epoch seconds, compared in local time |
| Branch ref exists | `git rev-parse --verify --quiet <ref>` (cwd = the worktree) | Non-zero means the ref is gone |
| Base branch | `--base`, else `git symbolic-ref --quiet refs/remotes/origin/HEAD` | Never guessed; refusal is exit code 2 |

Every call goes through `run_git`, which builds an **argument list** and passes
`shell=False`. No shell string is ever constructed, so a path can never be
reinterpreted as a command, flag or glob.

## Safety invariants

These are the rules the code must keep, and each is covered by a test:

1. **Dry run by default.** Without `--apply`, the only git subcommands executed are
   read-only. `test_dry_run_makes_no_changes` compares the full
   `git worktree list --porcelain` output before and after a run.
2. **Two keys to delete.** Removal needs `--apply` *and* a per-worktree "yes". The
   default answer is no, and `EOFError` (non-interactive stdin) is also no.
   `--apply --json` is rejected so machine output can never delete anything.
3. **No `--force`, no blanket `--yes`.** These flags are not defined, so argparse
   rejects them with exit code 2.
4. **Only `git worktree remove <path>`.** There is no recursive delete, no
   `rm -rf`, no `git worktree prune` and no `--force` in the removal path.
5. **Re-check before every removal.** `Sweeper.apply` calls `_fresh_candidate()`
   (a full re-enumeration, re-inspection and re-decision) before asking, and again
   immediately before the mutating command. If anything changed, the worktree is
   skipped.
6. **`removal_guard()` is authoritative.** Even if the verdict logic were wrong,
   the guard independently refuses main, bare, locked, prunable, symlinked, missing,
   detached, ref-less, unreadable, erroneous and dirty worktrees.
7. **Git failures refuse.** A failing `git worktree remove` is reported as `REFUSED`
   and the worktree is left alone; the process exits 1.

## Failure model

* `GitError` means "a git command I needed to succeed failed". It aborts discovery
  and maps to exit code 2 with a one-line `error:` message.
* Per-worktree failures are non-fatal: `Sweeper.probe` records them in
  `Worktree.error`, `decide` turns that into `MANUAL REVIEW`, and the report keeps
  going for every other worktree.

## Testability seams

`main(argv=None, git=None, confirm=None, out=None, err=None)` injects the git runner,
the confirmation function and both output streams. Tests therefore drive the real
CLI end to end (`TempRepoTestCase.run_cli`) while still being able to:

* simulate a git failure (`GitErrorTests.failing_runner`), and
* answer prompts deterministically without a TTY.

`Sweeper(base, older_than_days, git, confirm, cwd)` carries the same seams.

## Compatibility and performance

* Python 3.9+ (CI runs 3.9 through 3.13). No third-party runtime dependency, so
  nothing to install and nothing to pin.
* Requires a git that supports `worktree list --porcelain -z`; if the option is
  unsupported, git fails and the tool exits 2 with git's own message instead of
  silently parsing something ambiguous.
* Work is `O(worktrees)` git calls, each on a tiny amount of data; there is no
  history walk. A repository with a handful of worktrees finishes in milliseconds.
