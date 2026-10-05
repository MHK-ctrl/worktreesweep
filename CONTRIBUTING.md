# Contributing to WorktreeSweep

Thanks for helping. WorktreeSweep deletes things, so the bar for safety is high and
the bar for new surface area is intentionally low.

## Ground rules

1. **Standard library only.** No third-party runtime dependency, ever. Not for
   colours, not for prompts, not for packaging. Adding an import to
   `src/worktreesweep.py` means updating the allow-list in `.github/workflows/ci.yml`
   and explaining why in the pull request.
2. **No `--force` and no blanket `--yes`.** Removal must always stay a two-key
   action: `--apply` plus a per-worktree confirmation.
3. **Never weaken a safety check** to make a test pass. If a check is genuinely
   wrong, change the check and add a test that proves the new behaviour is safe.
4. **No shell strings.** Use `subprocess.run([...], shell=False)` with one list
   element per argument. `run_git` exists for this.
5. **One mutating command.** `git worktree remove <path>` is the only way this tool
   may change the filesystem.
6. **Keep the CLI small.** Four flags (`--older-than`, `--base`, `--apply`,
   `--json`) are the whole surface. Propose new ones in an issue first.

## Development setup

Nothing to install:

```sh
git clone https://github.com/MHK-ctrl/worktreesweep.git
cd worktreesweep
python3 --version        # 3.9 or newer
git --version            # needs `worktree list --porcelain -z`
python3 src/worktreesweep.py --help
```

## Running the tests

```sh
python3 -m unittest discover -s tests -v
```

The suite builds a tiny temporary Git repository per test and removes it afterwards.
There is no network access, no committed fixture and no shared state, so tests are
safe to run in a loop while you work.

Manual check (also the fastest way to see real output):

```sh
tmp=$(mktemp -d)
cd "$tmp" && mkdir repo && cd repo
git init -q -b main
git config user.name demo && git config user.email demo@example.invalid
echo hi > README.md && git add README.md && git commit -qm initial
git worktree add -q -b old-feature ../wt-old-feature HEAD
echo "wip" >> ../wt-old-feature/README.md   # make it dirty
python3 /path/to/worktreesweep/src/worktreesweep.py --base main
```

## How the tests are organised

| Class | Covers |
| --- | --- |
| `CandidateTests` | The decision table: what is flagged, what is protected |
| `SafetyTests` | Dry run, declining, confirming, no-candidate runs |
| `GitErrorTests` | Simulated git failures must refuse, not remove |
| `BaseBranchTests` | Base discovery and refusal to guess |
| `CliTests` | JSON shape, rejected flag combinations, flag validation |
| `ParserTests` | Pure functions: porcelain parsing and `removal_guard` |

To add a case, subclass `TempRepoTestCase`. Helpers available: `git()`,
`commit()`, `add_worktree()`, `run_cli()`, `report()` and `worktree_entry()`.
Prefer `report()` (JSON) when you are asserting on verdicts, and `run_cli()` when
you are asserting on human-facing text.

## Style

* Comments explain *why*, not *what*. The interesting comments are about safety.
* Keep functions small and named after what they decide.
* Avoid new global state; `Sweeper` is the only stateful object.
* Machine-readable output (JSON) uses ASCII only so it survives any terminal.

## Commit messages

Conventional Commits, imperative mood, for example:

```
feat(worktreesweep): add --older-than threshold

fix(worktreesweep): refuse removal when the branch ref disappeared

test: cover a worktree path containing a newline
```

## Pull request checklist

- [ ] `python3 -m unittest discover -s tests -v` passes.
- [ ] `src/worktreesweep.py` still imports the standard library only.
- [ ] New behaviour has a test in `tests/test_worktreesweep.py`.
- [ ] README/docs updated when flags or verdicts change.
- [ ] `CHANGELOG.md` updated under `Unreleased`.
- [ ] No new runtime dependency, no new mutating command.

## Reporting bugs

Please include:

* operating system and `python3 --version`,
* `git --version`,
* the command you ran,
* the tool's output, and
* `git worktree list --porcelain` if the layout matters.

Remove anything private from paths before pasting. Never paste tokens or credentials.

Be kind and assume good faith in issues and reviews.
