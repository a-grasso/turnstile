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

  • ai review (2 findings, 18s)

    high     Shell injection via unescaped filename in os.system
    report.py:59  [review, blocks]
      os.system("cp -r " + name + " " + dest) concatenates an
      unsanitized filename derived from sys.argv[1] into a shell
      command with no quoting.

  push refused - 2 check(s) failed
    fix them, or bypass deliberately:  git push --no-verify
```

Not a skill. Not an agent. A `pre-push` hook that runs the repo's own checks
and refuses the push if they fail — because the code path was taken, not
because a model decided the moment was relevant.

**Status: prototype.** Two evenings old, unproven in daily use. The
deterministic half is solid; the [AI modules](#ai-modules) are new and have no
eval behind them yet.

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

The AI modules additionally need `python3` and the `claude` CLI on PATH;
`doctor` checks for both, but only in a repo that actually configures a module.
Symlink `bin/turnstile-ai` too if you want to call the runner directly (for a
pre-commit hook, say) rather than through `turnstile ai`.

Then, per repo you want gated, a `.turnstile` file in the root:

```
lint: make lint
test: go test ./...

ai review:  block=high
ai secrets: block=medium
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
turnstile ai [args]    run only the ai modules
turnstile doctor       diagnose the installation
```

## AI modules

A line of the form `ai <name>: block=<level>` adds a model-backed check.
Modules run **in parallel with each other and with the deterministic checks**,
so the slow half costs wall-clock once rather than once per module.

```
ai review:  block=high         # refuse the push on high or critical findings
ai secrets: block=medium       # ...and on medium for this one
ai review:  block=never        # report findings, never refuse
```

`block` names the lowest severity that refuses a push. Findings below it still
print, marked `advisory`. Severities are `low`, `medium`, `high`, `critical`.

Two built-ins ship: **`review`** (correctness defects provable from the diff)
and **`secrets`** (credentials and private data about to be published).

### Writing one

A module is markdown with frontmatter. The body *is* the system prompt; the
runner supplies the diff and the finding schema.

```markdown
---
name: migrations
description: Schema changes that cannot be rolled back.
block: critical
model: sonnet
---

You are reviewing a diff for irreversible database migrations.
Report a finding when a migration drops a column or table with no
down-path...
```

`block` and `model` in frontmatter are the module's *defaults*. The
`.turnstile` line overrides both (`ai review: block=critical model=opus`), so a
module can declare what it needs without dictating it to every repo.

Module files resolve **repo → user → built-in**, first hit wins:

```
<repo>/.turnstile.d/modules/<name>.md    # this project's house rules
~/.turnstile/modules/<name>.md           # your own, everywhere
<turnstile>/modules/<name>.md            # shipped
```

So a repo can override `review` for its own conventions without forking, and
`turnstile status` prints which file each name actually resolved to.

### Findings are structured, not prose

Modules do not return text that gets grepped. The runner passes a JSON schema
to the model and reads back validated objects with `file`, `line`, `severity`,
`title`, and `detail`. A module that returns nothing schema-conforming is an
error, not a silently-empty pass — parsing prose for verdicts is the failure
mode this exists to avoid.

### Cost, and the cache

Each module call is a real API call (~$0.15 on `sonnet` for a small diff), and
a gate you pay for on every retry is a gate you turn off. Results are cached
under `~/.turnstile/cache/ai/` keyed on a hash of **the diff, the module's own
source, and the model**. So:

- Re-pushing the same commits is free and instant (~0.1s vs ~30s).
- Editing a module's prompt invalidates its cache, because tuning a prompt and
  silently replaying the old answer reads as the edit having done nothing.

### Failure is open, on purpose

A module that cannot run - network down, no `claude` on PATH, a timeout -
prints `?` and **does not refuse the push**. A gate that fails closed on a
network blip is a gate people learn to `--no-verify` past, which costs more
than the review it was protecting. Pass `--strict` to invert that where you
would rather stop.

Diffs are capped at 4000 lines before they reach the model, and the truncation
is stated in the report rather than silently narrowing what was reviewed. A
module that overruns `TURNSTILE_AI_TIMEOUT` (default 300s) is killed as a
process group, because a hook that hangs is worse than one that fails.

When a module is skipped this way, the summary says so — `all checks passed
(1 ai module(s) could not run)` — rather than reporting a clean pass for a
review that never happened.

```
TURNSTILE_MODEL         default model for all modules (default: sonnet)
TURNSTILE_AI_TIMEOUT    per-module timeout in seconds (default: 300)
TURNSTILE_CACHE         cache directory (default: ~/.turnstile/cache/ai)
```

## Use it as a pre-commit hook

The runner is a standalone entry point with no dependency on the bash gate or
on `core.hooksPath`, so it works in repos that already have a hook manager:

```sh
turnstile-ai --staged            # review the staged diff
turnstile-ai --range main..HEAD  # review a range
turnstile-ai --only secrets --json
```

For the [pre-commit](https://pre-commit.com) framework, this repo ships a
[`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml):

```yaml
repos:
  - repo: https://github.com/<you>/turnstile
    rev: v0.2.0
    hooks:
      - id: turnstile-ai
```

The module list still comes from the repo's `.turnstile` either way — the hook
manager decides *when* the runner fires, never *what* it runs.

Pre-commit is the more aggressive placement: it fires on every commit rather
than once per push, which multiplies both the latency and the bill. `secrets`
is the module that earns it, since a secret is unrecoverable the moment it is
pushed, and `review` is usually better left on pre-push.

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

**Deterministic checks stay the floor.** `make lint`, `go test` — fast, free,
never wrong, never annoying. They cover the boring majority of what a gate
should catch: broken build, failing test, lint drift. The AI modules are added
on top of that floor, never in place of it. A model is the wrong tool for
anything a compiler can already decide.

**The model in the push path had one real objection**, and it was not cost or
latency: *a git hook knows the diff but not the intent*. A reviewer without
intent flags every deliberate decision as a mistake, and after two of those you
stop reading the findings — at which point the gate costs you time and trains
you to ignore it.

That objection was probed three times before the stage was built, each an arm
of `claude -p` given the diff and nothing else:

| diff | findings | false positives |
|---|---|---|
| turnstile's own 852-line initial commit (new code) | 5 | 0 — two were real bugs (committed `.pyc`, dead `.gitignore` entry) |
| `reflock@8f884e6` (modifies existing code; its whole point is a deliberate "do **not** auto-repair" decision that should bait a false positive) | 1 | 0 — real string drift between two copies meant to stay in sync |
| a 61-line script with **no tests and no structure**: module-level globals, no argv validation, bare `except`, env-var token | 5 | 0 — shell injection via `os.system`, a `cp` reading the wrong path, an unclosed file, a division by zero on empty input, one internal hostname |

The third was chosen because the first two shared a weakness: both were code
where tests and structure already encoded the *why*, which is the easy case.
The hypothesis worth falsifying was that intent matters most exactly where
nothing else records it. It did not degrade. The reviewer reported the four
real defects, and flagged none of the missing tests, the globals, or the
unvalidated `argv` — the noise the objection predicted.

**Then it was run on someone's real work, and the objection finally landed.**
Five commits from a mature Rust compiler (`beacon`), 300–700 diff lines each:

| | result |
|---|---|
| 2 commits | no findings — correctly silent |
| `critical` | bounds-check elision scan walks only top-level `body.stmts`, so a reassignment inside any nested block never invalidates. Elision removes the check entirely → silent out-of-bounds write. **Verified true.** |
| `medium` | one `type_map.push(.., fty.name())` hover site missed by a diff whose whole point was routing type renders through `display_ty`. **Verified true**, three lines from its own fix. |
| `high` | a use-after-free shape in `to_cstring`. Real — **and documented in the diff's own comment** as a known gap, "not patched here", tracked as issue #206. **False positive**, and a blocking one. |
| `secrets`, all 5 | 0 findings |

The false positive is the interesting one, because it inverts the original
objection. Intent was not missing — it was written in a comment in the diff,
the reviewer *read* it (the finding cites #206), and reported it as a defect
anyway. The failure was not blindness to intent but refusal to defer to it.

That is a prompt bug, not a design flaw, and it is fixed: `review` now treats a
documented limitation as the author saying they already know, outranking its
own reading of the code. Re-running the same three commits kept the `critical`
and the `medium` and dropped the `high`.

So the hypothesis, once more refined: a hook reviewer's noise does not come
from lacking intent. It comes from overriding intent that is already on the
page. Absent structure was harmless; a `TODO` it disagreed with was not.

Still small-n and single-model. What is missing before any of this is
trustworthy is an eval corpus — see below.

**Prompts do most of the work.** The built-in modules spend more words on what
*not* to report than on what to find, and the runner prepends a contract to
every module saying that a deliberate decision it cannot distinguish from a
mistake is not a finding. That is the tuning surface: a false positive here is
not a wasted minute, it is the author learning to ignore the gate.

**Blocking is a per-module decision, not a global one.** `secrets` blocks at
`medium` and `review` at `high` in the same file, because the two failure modes
are not comparable — a pushed secret is unrecoverable, a missed correctness
finding is a bug report. Anything below the line still prints, so lowering the
bar costs nothing but reading.

**Frontmatter is not YAML** — flat `key: value`, same reasoning as the
line-oriented `.turnstile` format. No dependency, and a format that can only
express flat strings cannot grow into a second configuration language.

**The runner does not use the bash gate's config parser.** It re-implements it
in Python. That duplication is deliberate: `--staged` has to work in a repo
with no `core.hooksPath` and no turnstile install at all, and a format this
small is cheaper to parse twice than to factor into a third component both
sides shell out to.

**Modules get no tools.** `--tools ""`. The reviewer's job is to read a diff it
has already been given, and a hook is the wrong place to hand a model the
ability to edit the tree it is gating. A module can opt back in via
`tools:` in its frontmatter, which is a decision to make deliberately.

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

**Every pushed ref is gated, in its own pass.** An earlier cut checked only the
first content-bearing ref and warned about the rest, which was the wrong
trade — a gate that reads as having checked three refs while checking one is
worse than no gate. Multi-ref pushes are rare enough that repeating the
deterministic checks costs nothing in practice, and the AI cache means an
identical diff across two refs is not paid for twice.

**Unsolved: the intent channel.** The `PreToolUse` hook fires inside a session
that *does* know why a change was made, and could hand that to the review
stage. The probe above is the reason that is not urgent any more, not a reason
it is worthless — the case it would help is precisely the one the probe could
not construct, a deliberate decision that reads as a defect.

**Unsolved: no eval.** Module prompts are tuned by reading their output on a
handful of diffs. The one prompt fix so far *was* checked against the three
commits it had to keep getting right — but that is a regression test with n=3,
run by hand, not an eval. A change that trades a true positive for a false
negative on code nobody re-ran is still invisible.

That is the honest ceiling on everything above. Until there is a corpus of
diffs with known defects, every claim here is "it looked right on the diffs we
tried", and prompt tuning stays a matter of taste. It is also the single most
expensive thing left to build, which is why the recommendation is to run the
cheap module everywhere and the expensive one advisory-only until it has
earned more than n=8.

## Prior art

The gate-on-the-delivery-path idea is lifted from
[no-mistakes](https://github.com/kunchenguid/no-mistakes), which does far more
(disposable worktrees, a daemon, a nine-step agent pipeline, PR authoring, CI
repair) and asks you to adopt its working mode to get it. This takes the
enforcement point and leaves the pipeline. Evaluation:
`ai-dev/explorations/no-mistakes.md`.
