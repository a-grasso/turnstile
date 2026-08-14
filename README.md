# turnstile

A mechanical gate on the push path.

```
  turnstile pre-push → origin

  ✓ lint (2s)
  ✗ test (exit 1, 6s)

    $ go test ./...
    --- FAIL: TestRangeComputation (0.00s)
        range_test.go:41: want abc..def, got abc..HEAD
    FAIL

  push refused - 1 check(s) failed
    fix them, or bypass deliberately:  git push --no-verify
```

Not a skill. Not an agent. A `pre-push` hook that runs the repo's own checks
and refuses the push if they fail — because the code path was taken, not
because a model decided the moment was relevant.

**Status: prototype.** One evening old, unproven in daily use.

## Why

A skill fires when the model judges it relevant, which means it does not fire
exactly when you most need it: hour three of a long session, context thinning,
the model persuaded that it is done. That is the moment a check is worth the
most and the moment it is least likely to be invoked.

A hook has no judgment. It is on the road out or it isn't.

Two enforcement points, covering different traffic:

| | catches | can be bypassed by |
|---|---|---|
| `pre-push` hook | you, agents, the IDE, anything calling git | a human typing `--no-verify` |
| `PreToolUse` hook | Claude Code's Bash tool | you, by editing settings.json |

The pair matters. `--no-verify` is the right escape hatch for a person — they
have decided to take responsibility. It is the wrong one for an agent, which
has decided nothing and is routing around a failing check because that makes
the task look finished. So the second hook closes it, for the agent only.

**How firmly it closes it: not very.** The blocker is regex over the command
string, so `sh -c` wrapping, quote splitting, an alias, or a helper script that
calls `git push --no-verify` itself all walk straight past it. It raises the
cost of an accidental bypass from zero to deliberate; it is not a security
boundary and cannot become one at this layer. The `pre-push` hook is the actual
gate — this only stops the model from reflexively reaching for the hatch.

## Install

Nothing is installed until you run this. `install` writes a **global**
`core.hooksPath`.

```sh
git clone <this> ~/Projects/.../turnstile
ln -s ~/Projects/.../turnstile/bin/turnstile /usr/local/bin/turnstile

turnstile install
turnstile doctor
```

Then, per repo you want gated, a `.turnstile` file in the root:

```
lint: make lint
test: go test ./...
```

Repos without one pass straight through. See [examples/.turnstile](examples/.turnstile).

For the agent-side half, point a Claude Code `PreToolUse` hook at
[claude/no-bypass.py](claude/no-bypass.py) — the docstring has the settings.json
block.

Undo everything with `turnstile uninstall`.

## Commands

```
turnstile install      install the global hook dispatcher
turnstile uninstall    remove it
turnstile status       show gate state + this repo's checks
turnstile run          run this repo's checks now, without pushing
turnstile doctor       diagnose the installation
```

## The global-hooksPath problem

Git has no hook-chaining and no per-repo layering: a global `core.hooksPath`
**replaces** `.git/hooks` everywhere. Installing naively would silently disable
every existing hook on the machine.

So `install` does not drop in one `pre-push`. It symlinks
[hooks/dispatch](hooks/dispatch) as *every* client-side hook name, and the
dispatcher's second job — before anything else — is to delegate to the repo's
own `.git/hooks/<name>`, replaying `pre-push`'s stdin so the delegate sees the
same ref updates. Adding a hook name to the list in `bin/turnstile` is the
contract, not a convenience: a name missing from it is a hook that stops firing
for every repo you own.

Two cases it deliberately does not fight:

- **Repos that set their own `core.hooksPath`** (husky, lefthook) win over the
  global one, so turnstile simply is not in the path there. `turnstile status`
  says so rather than implying coverage it does not have.
- **A pre-existing global `core.hooksPath`** makes `install` refuse rather than
  overwrite. Quietly taking over another tool's hooks is the kind of trapdoor
  this is supposed to not be.

## Design notes

**Deterministic only, on purpose.** No model in the push path. Checks are
whatever the repo already has — `make lint`, `go test`. Fast, free, never
wrong, never annoying. That covers the boring majority of what a gate should
catch: broken build, failing test, lint drift.

**The interesting half is deliberately absent.** Adversarial review of the diff
in a fresh context is where the real value is (see
`ai-dev/explorations/no-mistakes.md`), and it is not here yet, for one specific
reason: *a git hook knows the diff but not the intent*. A reviewer without
intent flags every deliberate decision as a mistake, and after two of those you
stop reading the findings — at which point the gate is costing you time and
training you to ignore it.

The `PreToolUse` hook is the way out, and the reason it is worth having beyond
bypass-blocking: it fires inside a session that *does* know why the change was
made. Capturing intent there and handing it to a review stage is the next step.

**Except the noise did not show up when probed.** Two trials of `claude -p`
given the diff and nothing else:

| diff | findings | false positives |
|---|---|---|
| turnstile's own 852-line initial commit (new code) | 5 | 0 — two were real bugs (committed `.pyc`, dead `.gitignore` entry) |
| `reflock@8f884e6` (modifies existing code; its whole point is a deliberate "do **not** auto-repair" decision that should bait a false positive) | 1 | 0 — real string drift between two copies meant to stay in sync |

On the second, an arm run *with* the intent supplied produced the **same single
finding**. Intent changed nothing, and the no-intent arm did not flag the
deliberate decision as a bug.

n=2, both on code with tests and clear structure, so this is suggestive rather
than settled. The live hypothesis is narrower than "review needs intent":
intent matters when it is *not recoverable from the diff*. Where tests and
structure already encode the why, a hook-supplied diff may be enough.

**Output is tail-only.** Failures print the last 40 lines, not the whole log. A
5000-line test dump inside a hook is how people learn to reach for
`--no-verify`.

**Config is line-oriented, not YAML.** No parser dependency, and a format that
cannot express anything but a list of commands cannot grow into a second
configuration language.

**Checks run in-tree, not in a worktree.** They see your working directory as
it is. That is wrong for validation of a *pushed* range and right for a
prototype that has to stay fast; `TURNSTILE_RANGE` and
`TURNSTILE_CHANGED_FILES` are exported so a check can scope itself. Worktree
isolation belongs with the review stage, where it actually buys something.

**Unsolved: the first-ref-only shortcut.** `compute_range` gates the first
content-bearing ref of a push and ignores the rest. Fine for `git push`, wrong
for `--all`.

## Prior art

The gate-on-the-delivery-path idea is lifted from
[no-mistakes](https://github.com/kunchenguid/no-mistakes), which does far more
(disposable worktrees, a daemon, a nine-step agent pipeline, PR authoring, CI
repair) and asks you to adopt its working mode to get it. This takes the
enforcement point and leaves the pipeline. Evaluation:
`ai-dev/explorations/no-mistakes.md`.
