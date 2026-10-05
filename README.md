# WorktreeSweep

**Find your stale Git worktrees and clean them up — safely.**

[![CI](https://github.com/MHK-ctrl/worktreesweep/actions/workflows/ci.yml/badge.svg)](https://github.com/MHK-ctrl/worktreesweep/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

`worktreesweep` is one Python file with no dependencies beyond the standard library
and `git`. It reviews every worktree attached to the repository you run it in, shows
you why each one was flagged, and removes only the worktrees you confirm one by one.

## The problem

Git worktrees are great: a branch per directory, no stashing, no juggling. Then they
accumulate. Some are weeks old, some have uncommitted work you forgot about, one has
a branch that was deleted, and a couple are just plain stale. Deleting the wrong one
costs you work, so the pile keeps growing.

`git worktree list` tells you what exists. It does not tell you what is *safe*.
WorktreeSweep does.

## Quick start

Nothing to install:

```sh
git clone https://github.com/MHK-ctrl/worktreesweep.git
cd worktreesweep
python3 src/worktreesweep.py --base main
```

Or put it on your `PATH`:

```sh
chmod +x src/worktreesweep.py
mkdir -p ~/.local/bin
ln -s "$PWD/src/worktreesweep.py" ~/.local/bin/worktreesweep
```

Example output (paths trimmed, one worktree of three shown in full):

```
$ worktreesweep --base main
WorktreeSweep - reviewing 3 worktree(s) in /home/you/project
Base branch: main | stale threshold: 30 day(s) | mode: dry run (nothing will be removed)

[1] /home/you/project
    Branch:  main
    HEAD:    40128004  committed 2020-01-01 (2469 days ago)
    Locked:  no
    Status:  clean (no tracked or untracked changes)
    Merged:  yes (HEAD is an ancestor of main)
    Verdict: KEEP - main worktree - never removed

[2] /home/you/project-wt-dirty-wip
    Branch:  dirty-wip
    HEAD:    40128004  committed 2020-01-01 (2469 days ago)
    Locked:  no
    Status:  dirty (1 tracked change(s), 0 untracked file(s))
    Merged:  yes (HEAD is an ancestor of main)
    Verdict: KEEP - dirty - 1 tracked change(s), 0 untracked file(s)

[3] /home/you/project-wt-old-feature
    Branch:  old-feature
    HEAD:    40128004  committed 2020-01-01 (2469 days ago)
    Locked:  no
    Status:  clean (no tracked or untracked changes)
    Merged:  yes (HEAD is an ancestor of main)
    Verdict: REMOVAL CANDIDATE - clean, merged into main, and committed 2020-01-01 (2469 days ago)

Summary: 1 removal candidate(s) | 0 need manual review | 2 to keep

Removal candidates (safe by the rules below, still your call):
  - /home/you/project-wt-old-feature  (old-feature, HEAD 40128004, committed 2020-01-01 (2469 days ago))

Dry run: nothing was removed. Re-run with --apply to remove candidates; you will be asked to confirm each one.
```

Then, when you agree with the report:

```
$ worktreesweep --base main --apply

Removal candidate: /home/you/project-wt-old-feature
  branch old-feature | HEAD 40128004 | committed 2020-01-01 (2469 days ago)
  Remove this worktree? [y/N] y
  REMOVED - /home/you/project-wt-old-feature

Applied: 1 removed | 0 skipped | 0 refused by git
```

The branch itself is kept. Only the worktree directory is removed.

## Safety

* **Dry run by default.** No `--apply`, nothing changes. Ever.
* **Never deletes without confirmation.** `--apply` still asks per worktree, and the
  default answer is no. Non-interactive stdin is treated as "no".
* **Never touches the main worktree.** It is reported, then excluded from removal.
* **Never uses `git worktree prune`, `rm -rf` or `--force`.** The single removal path
  is `git worktree remove <path>`.
* **Re-checks before removing.** Eligibility is recomputed immediately before the
  removal command; anything that changed is skipped.
* **Failed git command means refusal.** If git errors, the worktree stays where it is
  and the tool says so.

Every git call is an argument list (`subprocess.run([...], shell=False)`), so a path
containing spaces, quotes or newlines can never be reinterpreted by a shell.

## How it decides what is "stale"

A worktree is a **REMOVAL CANDIDATE** only when *all* of these hold:

| Condition | How it is checked |
| --- | --- |
| It is not the main worktree | `git worktree list --porcelain -z` + `git rev-parse --git-common-dir` |
| Its path exists | filesystem check (git also reports it `prunable` when missing) |
| It is not locked | `locked` attribute from the porcelain output |
| It is clean | `git status --porcelain=v1 --untracked-files=all` is empty — untracked files count as dirty |
| Its HEAD is merged | `git merge-base --is-ancestor <head> <base>` |
| Its HEAD commit is older than the threshold | `git show -s --format=%ct <commit>`, default 30 days |

Anything Git cannot prove safe is reported as **MANUAL REVIEW** and never offered for
removal: detached HEAD, a missing or deleted branch ref, a prunable or missing path, a
locked worktree, a symlinked path, and any git error. Everything else is **KEEP** with
the reason, including dirty worktrees and unmerged branches.

> "Stale" is a heuristic based on **commit age**, not on when you last touched the
> directory. Git does not record when a worktree was last *used*, and WorktreeSweep
> will not pretend otherwise. A worktree with an old HEAD that you actually use every
> day is still old: `--older-than` is the dial, and the confirmation prompt is the
> backstop.

## Flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--older-than DAYS` | `30` | Age threshold for calling a HEAD commit stale |
| `--base BRANCH` | `refs/remotes/origin/HEAD` | Branch to compare against for the merged check |
| `--apply` | off | Remove candidates, after a per-worktree confirmation |
| `--json` | off | Print a machine-readable dry-run report instead of the text report |
| `-h`, `--help` | | Usage and the safety rules |

There is deliberately **no `--force`** and **no blanket `--yes`** — argparse rejects
both. `--apply` cannot be combined with `--json`, so machine-readable output can never
delete anything.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Report produced (and, with `--apply`, every removal either succeeded or was skipped) |
| `1` | `--apply` ran but git refused at least one removal; nothing else was affected |
| `2` | Usage or configuration error, e.g. the base branch could not be determined |

## What it deliberately does NOT do

* **No automatic cleanup.** It never removes anything on its own, not even with
  `--apply` — each removal is a separate, explicit yes.
* **No `--force`.** If git refuses to remove a worktree, WorktreeSweep reports the
  refusal instead of forcing it.
* **No guessing the base branch.** If `refs/remotes/origin/HEAD` is unavailable and
  `--base` was not passed, it stops and tells you to pass `--base`.
* **No claim about when a worktree was last *used*.** Only commit age is knowable from
  git, so that is all it uses.
* **No branch deletion.** It removes worktrees, not branches.
* **No network access, no telemetry, no config file, no state.** It reads git and
  prints a report.

## Limitations

* The merged check is relative to one base branch. Work merged into a *different*
  long-lived branch reads as unmerged — that is the conservative direction, and you
  can point `--base` at the branch you care about.
* Squash merges and rebases rewrite history, so a branch merged that way may report as
  unmerged. Again: conservative, never the other way round.
* A worktree on a detached HEAD, or one whose branch ref was deleted, cannot be judged
  automatically. It is surfaced for manual review rather than guessed at.
* WorktreeSweep only knows about worktrees git still lists. Leftovers git no longer
  tracks are `git worktree prune`'s business, and it deliberately does not run it.
* Requires Python 3.9+ and a git with `worktree list --porcelain -z` (git 2.36+). On
  an older git it exits 2 with git's own error rather than parsing something
  ambiguous.

## Development

No virtual environment and no `pip install`:

```sh
python3 -m unittest discover -s tests -v
```

The suite builds a temporary Git repository per test and deletes it afterwards: no
network access, no committed fixtures, no shared state. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for the design, the decision table and the safety invariants.

## Roadmap

- [x] Read-only report with per-worktree verdicts and reasons
- [x] Confirmed `--apply` removal that re-checks before every deletion
- [x] `--json` output for scripting
- [ ] `--exit-code` for CI jobs that want to fail when stale worktrees exist
- [ ] Per-path ignore rules (for example, never offer worktrees under a demo directory)
- [ ] `--explain` to print the exact git commands behind each verdict
- [ ] Worktree age by last git activity as an optional second heuristic
- [ ] Packaging so `pipx install worktreesweep` works without changing the zero-dependency rule

## Contributing

Issues and pull requests are welcome — especially if you have found a case where a
worktree is flagged that should not be, or vice versa. Start with
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 Mohammad Hamza Khan
