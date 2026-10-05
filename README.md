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
and refuses the push if they fail, because the code path was taken, not
because a model decided the moment was relevant.

**Status: prototype.** Unproven in daily use. The deterministic half is solid
and has a test suite. Of the [AI modules](#ai-modules), only `secrets` has an
[eval corpus](#measuring-a-module) behind it.

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
| `Stop` hook | a Claude Code agent ending its turn on a red tree | editing `.claude/settings.json` |

The `Stop` hook is the early one; see [the agent's loop](#the-agents-loop).
`--no-verify` is the right escape hatch for a person: they have decided to take
responsibility. turnstile does not try to close it for an agent. The `pre-push`
hook is the actual gate, and the `Stop` hook makes reaching for the hatch
unnecessary by catching a red tree a turn earlier.

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
cli [cli/** go.work]: make -C cli test
push e2e [web/**]: make e2e

ai review:  block=high
ai secrets: block=medium
```

Repos without one pass straight through. A check with a bracketed scope runs
only when the change touches a file its globs match; see
[scoped checks](#scoped-checks-and-the-pass-cache). See [examples/.turnstile](examples/.turnstile)<!--@13caced8-->.

For the agent-side half, commit a Claude Code `Stop` hook to the project's
`.claude/settings.json`, so contributors install nothing for it.
`turnstile print-claude-settings` prints the block, which is
[claude/settings.json](claude/settings.json)<!--@049f7c8c-->:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"${CLAUDE_PROJECT_DIR:-.}\" || exit 0; command -v turnstile >/dev/null && exec turnstile hook claude-stop; if [ -f flake.nix ]; then for n in nix /nix/var/nix/profiles/default/bin/nix; do command -v $n >/dev/null && exec $n develop -c turnstile hook claude-stop; done; fi; echo '{\"systemMessage\": \"turnstile is not installed, so the checks of this repo did not run at agent stop. See https://github.com/a-grasso/turnstile#install\"}'",
            "timeout": 900,
            "statusMessage": "turnstile: running checks (a first run builds the dev environment)"
          }
        ]
      }
    ]
  }
}
```

The command runs in the project directory and picks the first of three:

1. `turnstile hook claude-stop`, when `turnstile` is on PATH. In a project with
   a `flake.nix`, outside its dev shell (neither `IN_NIX_SHELL` nor
   `DIRENV_DIR` set), the hook runs the checks in the project's toolchain
   ([claude/stop-hook.sh](claude/stop-hook.sh)<!--@201741b3-->): it caches
   `nix print-dev-env` under `.git/turnstile/`, keyed on the hash of
   `flake.nix` and `flake.lock`, and sources it. That costs one evaluation per
   change to those two files instead of the 25 to 44s `nix develop` takes on
   every stop of a dirty tree. A file the flake imports is not part of the
   key; delete `.git/turnstile/` after editing one. Without `nix`, or when the
   build fails, the checks run bare and the hook says so.
2. `nix develop -c turnstile hook claude-stop`, when turnstile is not on PATH,
   the project has a `flake.nix` that puts it in its devShell (see
   [Install with Nix](#install-with-nix)) and `nix` is on PATH or at
   `/nix/var/nix/profiles/default/bin/nix`. This path pays the evaluation on
   every stop; put turnstile on PATH to get the cache.
3. A `systemMessage` saying turnstile is not installed, exit 0.

The third is fail-open but visible, on purpose: a teammate who has not adopted
turnstile is never blocked, and sees that the checks did not run. The fallback
lives in the settings command and not in turnstile because it has to work when
turnstile is absent. Repos without a `.turnstile` are left alone either way.

Claude Code shows a hook's output only once it has finished, so a slow stop is
explained twice: the spinner reads the block's `statusMessage` while the hook
runs, and a `systemMessage` afterwards says what was slow, either that the dev
environment was built or that the checks took ten seconds or more uncached
(`TURNSTILE_SLOW_NOTICE` sets that threshold, in seconds).

To leave global git config alone, `turnstile install --repo` writes only this
repo's `.git/hooks/pre-push`, which calls `turnstile pre-push` (and says so,
without blocking, when turnstile is missing). A hook manager can call
`turnstile pre-push "$@"` itself, with git's stdin passed through: husky does
that by default and lefthook needs `use_stdin: true`. The pre-commit framework
does not forward stdin, so use `install --repo` there.

Undo everything with `turnstile uninstall`.

## Approving a config

`.turnstile` is executed, and a clone or a pull can change it into code you have
not read. So `turnstile run`, the pre-push gate and the Stop hook refuse to
execute one whose content you have not approved:

```
turnstile: .turnstile changed since you approved it, run `turnstile allow` after reviewing it
```

`turnstile allow` approves the current content. Approvals live in
`~/.turnstile/allowed/`, one per repo and content, the way direnv does it. The
Stop hook shows the line as a message and does not block, because an agent that
approves its own gate has not been gated. `turnstile ci` is exempt, since it
reads the base branch's config and review has already approved that. Pre-push
requires approval too, because it is the first moment a freshly cloned or pulled
`.turnstile` would run on your machine, and refusing costs one command.

## CI

`turnstile ci --base <ref>` is the check CI can trust. It runs the deterministic
checks only (no ai modules, no pass cache) over `base..HEAD`, and reads
`.turnstile` from `<ref>` with `git show`, so a pull request that deletes or
weakens a check is still judged by it. What a check's command does is still the
checked-out code's business: a PR can edit the `Makefile` that `make lint`
runs, so protect those files with CODEOWNERS.

[action.yml](action.yml)<!--@c6445728-->, a composite GitHub Action, installs nix, builds
turnstile from the action's own checkout and runs `turnstile ci` inside the
project's devShell when it has a `flake.nix`:

```yaml
on: pull_request
jobs:
  turnstile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: a-grasso/turnstile@main
```

## Install with Nix

Pin it per project with a flake input, and the version lives in `flake.lock`:

```nix
{
  inputs.turnstile.url = "github:a-grasso/turnstile";
  inputs.turnstile.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { nixpkgs, turnstile, ... }: let
    system = "aarch64-darwin";
    pkgs = nixpkgs.legacyPackages.${system};
  in {
    devShells.${system}.default = pkgs.mkShell {
      packages = [ turnstile.packages.${system}.default ];
    };
  };
}
```

Or run it once: `nix run github:a-grasso/turnstile -- doctor`.

The package puts `turnstile` and `turnstile-ai` on PATH with `bash`, `git`,
`python3` and the coreutils they need. `turnstile install` still writes the
global `core.hooksPath`, pointing at the store path of the pinned version.

## Commands

```
turnstile install      install the global hook dispatcher
turnstile install --repo   write only this repo's .git/hooks/pre-push
turnstile uninstall    remove it (--repo: this repo's hook)
turnstile pre-push     the gate for hook managers, git's pre-push stdin on stdin
turnstile allow        approve this repo's current .turnstile
turnstile status       show gate state + this repo's checks
turnstile run          run this repo's checks on the working tree, without pushing
  --no-ai              skip the ai modules (they run once, at push)
  --no-cache           rerun checks that already passed on this tree
  --stop               what an agent's turn end runs: no ai, no push-only checks
turnstile ci --base <ref>   CI: deterministic checks only, no cache, config read from <ref>
turnstile ai [args]    run only the ai modules
turnstile hook claude-stop        Claude Code Stop hook, payload on stdin
turnstile print-claude-settings   the Stop hook block for .claude/settings.json
turnstile doctor       diagnose the installation
```

## Scoped checks and the pass cache

A repo with several modules rarely wants every check on every change, and an
agent that already ran the tests should not wait for them again at push. Two
mechanisms handle that, and neither makes turnstile a task runner. `.turnstile`
says *what* gates and *when*; the commands it names (`make`, `just`, a script)
keep saying *how*.

**Scope.** `name [glob ...]: command` runs only when the change touches a file
one of the globs matches. Globs are git pathspec globs: `**` crosses
directories, `*` does not. A scoped check that is skipped prints as
`· name (not touched)`, so a quiet gate never reads as a thorough one.

**Push-only.** `push name [glob ...]: command` is a check too slow for every
agent turn: an e2e suite, a full frontend build. The Stop hook skips it and
prints `· name (at push)`; pre-push, CI and a plain `turnstile run` still run
it. The budget for what stays is about ten seconds warm, because the agent
waits for it on every turn.

**Parallel.** Checks that have to run start together, so the gate costs the
slowest check rather than the sum. Results print in config order.

**Pass cache.** A check that passes is recorded against what it could see:
the files in its scope (the whole tree when unscoped), the command, and the
base of the change. The same check on the same content is then
`✓ name (cached)` and costs nothing. An edit outside a check's scope keeps its
pass. A failure is never cached. Entries live under `~/.turnstile/cache/checks/`
and expire after 30 days; `--no-cache` ignores them.

The tree is read as it stands, uncommitted and untracked files included,
through a scratch index that never touches yours. That is what lets a pass from
`turnstile run` on uncommitted work be a cache hit at push, once that work is
committed unchanged.

**A check that rewrites files fails.** A formatter in fix mode passes against
files that no longer exist, so turnstile compares the tree before and after
the checks and refuses a run that changed it, listing the files. Checks run
concurrently, so the report names the batch rather than one check. Commit what
is right and run again; the second run passes.

**Cannot run here.** A check that exits 77 (the autotools skip code) could not
run: no docker, a missing credential, a dependency a clean checkout lacks. It
prints `? name (could not run: <last line of output>)`, is not a failure and is
never cached. The summary counts it, as in `all checks passed (1 check(s) could
not run)`, the way it counts ai modules that could not run, so a quiet gate
never reads as a thorough one. The Stop hook lets the agent stop and says what
did not run.

Checks get `TURNSTILE_RANGE`, `TURNSTILE_BASE`, `TURNSTILE_CHANGED_FILES` and
`TURNSTILE_DIFF_FILE` (the change as a patch) so they can scope themselves
further.

## The agent's loop

```
agent writes code
  → runs a single test, as often as it likes        (its own business)
  → ends its turn  → Stop hook: turnstile run --stop (cached, refuses red)
  → pushes         → pre-push: cached checks + ai modules, once
```

The pre-push gate alone catches a failing check after the session that caused
it has moved on. `turnstile hook claude-stop`
([claude/stop-hook.py](claude/stop-hook.py)<!--@7a50b851-->) reads Claude Code's Stop
payload, runs the deterministic checks that are not push-only whenever the
agent ends a turn and, on failure, blocks the stop with the report, so the
agent fixes it while it still has the context.
Nothing depends on the agent remembering to verify, which is the argument
against skills this tool started from.

The loop is bounded. When the checks still fail and the tree has not changed
since the last refusal, the agent is let go and the failure is left for you.
The ai modules never run here: they cost money per call and run once, at push.
Because passes are cached per tree, a turn that changed nothing costs nothing,
and the push after a green turn mostly reads the cache.

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

Four built-ins ship:

| module | finds | default block |
|---|---|---|
| `review` | correctness defects provable from the diff | `high` |
| `secrets` | credentials and private data about to be published | `medium` |
| `docs` | docs the change makes false, and doc obligations it skipped | `medium` |
| `tests` | tests weakened, bent or removed to make a change pass | `high` |

`docs` is the one built-in with tools (`Read`, `Grep`, `Glob`): the doc that
went stale is usually a file the diff does not touch.

### Intent

```
intent: git log --format=%B "$TURNSTILE_RANGE" | grep -oE '#[0-9]+' | sort -u | tr -d '#' | xargs -n1 gh issue view
```

An `intent:` line names a command whose output tells the modules what the
change is for, usually the linked issue. It runs once per review with
`TURNSTILE_RANGE` set, and its output reaches every module inside an
`<intent>` block. A reviewer that knows the intent stops flagging deliberate
decisions, and `docs` can hold the change against the issue's acceptance
criteria. A failing or slow intent command costs the context, not the push.

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
error, not a silently-empty pass. Parsing prose for verdicts is the failure
mode this exists to avoid.

### Cost, and the cache

Each module call is a real API call (~$0.15 on `sonnet` for a small diff), and
a gate you pay for on every retry is a gate you turn off. Results are cached
under `~/.turnstile/cache/ai/` keyed on a hash of **the diff, the module's own
source, and the model**. So:

- Re-pushing the same commits is free and instant (~0.1s vs ~30s).
- Editing a module's prompt invalidates its cache, because tuning a prompt and
  silently replaying the old answer reads as the edit having done nothing.

A module that passed a change is not asked about it twice. When `tests`
refuses a push and the fix is pushed, `docs` reviews only the follow-up since
its pass, and says so as `follow-up since abc1234`. A module that refused
reviews the whole change again, because it has to see whether its finding is
gone.

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

When a module is skipped this way, the summary says so, printing `all checks
passed (1 ai module(s) could not run)` rather than reporting a clean pass for
a review that never happened.

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
turnstile-ai --diff-file bug.patch --module secrets   # a diff from no repo at all
```

For the [pre-commit](https://pre-commit.com) framework, this repo ships a
[`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml)<!--@d2bb34b9-->:

```yaml
repos:
  - repo: https://github.com/<you>/turnstile
    rev: v0.2.0
    hooks:
      - id: turnstile-ai
```

The module list still comes from the repo's `.turnstile` either way: the hook
manager decides *when* the runner fires, never *what* it runs.

Pre-commit is the more aggressive placement: it fires on every commit rather
than once per push, which multiplies both the latency and the bill. `secrets`
is the module that earns it, since a secret is unrecoverable the moment it is
pushed, and `review` is usually better left on pre-push.

## Measuring a module

A module is a prompt, and a prompt change is unfalsifiable without a corpus:
removing a false positive you noticed can just as easily remove a true positive
you never re-ran. [`eval/`](eval/) is the fixed set of diffs that makes the
change measurable.

```sh
./eval/run.py --write                                   # score `secrets`, 3 runs each
./eval/run.py --turnstile-home eval/probes/no-negatives # A/B against a variant prompt
```

It scores one thing - does the gate block this diff - because that is the only
thing a module does to you. Findings below the block threshold are counted as
noise rather than failure. Each fixture runs three times, since one pass of a
non-deterministic call is not a measurement.

The corpus is honest about its own weakness. `eval/probes/no-negatives` is the
`secrets` prompt with its entire "Do not report" section deleted; it scores the
same as the real prompt on all but one fixture. So a green scoreboard means "no
regression detected by a weak instrument", not "the prompt is good". See
[eval/README.md](eval/README.md)<!--@3e92c5a5-->.

## Gating the prose

This repo runs [vale](https://vale.sh) over its own docs, as an ordinary
deterministic check rather than an ai module. The house rule is "no em dashes",
which is a rule, so a linter enforces it perfectly and a model would only
approximate it.

```sh
vale sync          # fetch the pinned style package into .vale/styles
vale README.md     # everything, including the advisory suggestions
turnstile run      # only what gates, only on the files the push carries
```

[.vale.ini](.vale.ini)<!--@31ec1305--> gates two rules out of [ai-tells](https://github.com/tbhb/vale-ai-tells)'
78 and demotes the other 76 to suggestions, because gating all of them means a
stray "comprehensive" refuses a push. On top of that sits a small house style in
[.vale/styles/Turnstile](.vale/styles/Turnstile): no machine-specific paths, no
real addresses or private ranges, no unresolved `TODO:`, and no claiming
`production-ready` when the second line of this file says prototype. One rule,
`PromptContract`, watches [modules/](modules/) rather than the docs: it fails if
a module prompt has lost its "not a finding" guidance entirely, which is the
one prompt regression [eval/](eval/) has already admitted it cannot see.

Three things about the placement are worth knowing, because none of them are
obvious from vale's own docs:

- **The check reads `TURNSTILE_CHANGED_FILES`**, which is the hook-side
  equivalent of vale's `filter_mode: file`. A page you never touch stays as it
  is; a page you edit you leave clean. There is no equivalent of `added`, since
  a check reports one exit code and not a set of lines.
- **A failing check is shown as `tail -n 40`.** Left at the config's
  `MinAlertLevel`, forty advisory suggestions push the violation that actually
  blocked the push out of the window, so the check runs at
  `--minAlertLevel=error` and the suggestions stay a manual `vale` away.
- **Missing vale fails the check** rather than skipping it. Fail-open is for the
  ai modules, where the alternative teaches people to `--no-verify` past a
  network blip. A linter that is simply not installed is a broken gate, and a
  gate that disappears when its tool does is worse than not having it.

## Pinning the cross-references

The prose gate catches how a sentence is written. It cannot catch a sentence
that was true when it was written and is now wrong because the thing it
describes moved. That is [reflock](https://github.com/a-grasso/reflock), and it
is the second deterministic check on this repo's own docs.

A reference opts in by carrying an empty pin, `<!--@-->` after the link, and
`reflock stamp` fills it with a fingerprint of the target as it stands. `reflock
check` recomputes and compares: a target that changed is `DRIFTED`, a target
that no longer exists is `DANGLING`. Each pinned reference in this file is a place where the prose makes a claim
about a file rather than merely linking to it.

```sh
reflock check                       # what the `refs` gate runs
reflock explain README.md:271       # why one reference is unhappy
reflock stamp --rebless --reviewed  # accept a drift, after re-reading the prose
```

The placement inverts the prose gate, and the inversion is the whole point.
`prose` is scoped to the files the push carries, because a page you never touch
is not your problem. `refs` is scoped to the whole tree, because the file that
went stale is precisely the one the push does not contain: you edited A, and the
paragraph describing A lives in B. Scoping this to changed files would leave it
checking everything except the case it exists for.

Accepting a drift takes `--rebless --reviewed`, and `--rebless` alone writes
nothing. Re-reading the paragraph is the work; the fingerprint is only the
receipt that you did.

## The global-hooksPath problem

Git has no hook-chaining and no per-repo layering: a global `core.hooksPath`
**replaces** `.git/hooks` everywhere. Installing naively would silently disable
every existing hook on the machine.

So `install` does not drop in one `pre-push`. It symlinks
[hooks/dispatch](hooks/dispatch)<!--@b9da807d--> as *every* client-side hook name, and the
dispatcher's second job, before anything else, is to delegate to the repo's
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

**Deterministic checks stay the floor.** `make lint`, `go test`: fast, free,
never wrong, never annoying. They cover the boring majority of what a gate
should catch: broken build, failing test, lint drift. The AI modules are added
on top of that floor, never in place of it. A model is the wrong tool for
anything a compiler can already decide.

**The model in the push path had one real objection**, and it was not cost or
latency: *a git hook knows the diff but not the intent*. A reviewer without
intent flags every deliberate decision as a mistake, and after two of those you
stop reading the findings, at which point the gate costs you time and trains
you to ignore it.

That objection was probed three times before the stage was built, each an arm
of `claude -p` given the diff and nothing else:

| diff | findings | false positives |
|---|---|---|
| turnstile's own 852-line initial commit (new code) | 5 | 0. Two were real bugs (committed `.pyc`, dead `.gitignore` entry) |
| `reflock@8f884e6` (modifies existing code; its whole point is a deliberate "do **not** auto-repair" decision that should bait a false positive) | 1 | 0. Real string drift between two copies meant to stay in sync |
| a 61-line script with **no tests and no structure**: module-level globals, no argv validation, bare `except`, env-var token | 5 | 0. Shell injection via `os.system`, a `cp` reading the wrong path, an unclosed file, a division by zero on empty input, one internal hostname |

The third was chosen because the first two shared a weakness: both were code
where tests and structure already encoded the *why*, which is the easy case.
The hypothesis worth falsifying was that intent matters most exactly where
nothing else records it. It did not degrade. The reviewer reported the four
real defects, and flagged none of the missing tests, the globals, or the
unvalidated `argv`, the noise the objection predicted.

**Then it was run on someone's real work, and the objection finally landed.**
Five commits from a mature Rust compiler (`beacon`), 300-700 diff lines each:

| | result |
|---|---|
| 2 commits | no findings, correctly silent |
| `critical` | bounds-check elision scan walks only top-level `body.stmts`, so a reassignment inside any nested block never invalidates. Elision removes the check entirely → silent out-of-bounds write. **Verified true.** |
| `medium` | one `type_map.push(.., fty.name())` hover site missed by a diff whose whole point was routing type renders through `display_ty`. **Verified true**, three lines from its own fix. |
| `high` | a use-after-free shape in `to_cstring`. Real, **and documented in the diff's own comment** as a known gap, "not patched here", tracked as issue #206. **False positive**, and a blocking one. |
| `secrets`, all 5 | 0 findings |

The false positive is the interesting one, because it inverts the original
objection. Intent was not missing. It was written in a comment in the diff,
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
trustworthy is an eval corpus (see below).

**Prompts do most of the work.** The built-in modules spend more words on what
*not* to report than on what to find, and the runner prepends a contract to
every module saying that a deliberate decision it cannot distinguish from a
mistake is not a finding. That is the tuning surface: a false positive here is
not a wasted minute, it is the author learning to ignore the gate.

**Blocking is a per-module decision, not a global one.** `secrets` blocks at
`medium` and `review` at `high` in the same file, because the two failure modes
are not comparable: a pushed secret is unrecoverable, a missed correctness
finding is a bug report. Anything below the line still prints, so lowering the
bar costs nothing but reading.

**Frontmatter is not YAML.** Flat `key: value`, same reasoning as the
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

**Push checks the commits, not the tree.** When the working tree is exactly
the pushed commit, the checks run in place. Otherwise (uncommitted work, another
branch checked out, a branch pushed that is not checked out) they run in a
temporary `git worktree` of the pushed commit, with hooks off, removed
afterwards. A fresh checkout lacks what is untracked (`node_modules`, `.venv`),
so a check that cannot run there should exit 77: the push then reports `could
not verify <sha>` instead of going red. The pass cache is keyed on tree content,
so both modes share it. `turnstile run` still checks the working tree, since
checking uncommitted work is its job.

**Every pushed ref is gated, in its own pass.** An earlier cut checked only the
first content-bearing ref and warned about the rest, which was the wrong
trade: a gate that reads as having checked three refs while checking one is
worse than no gate. Multi-ref pushes are rare enough that repeating the
deterministic checks costs nothing in practice, and the AI cache means an
identical diff across two refs is not paid for twice.

**Partly solved: the intent channel.** The [`intent:` line](#intent) carries
what the tracker knows. What it does not carry is the session: the agent knows
why it made each change, and nothing hands that to the review stage yet. The
case it would help is precisely the one the probe could not construct, a
deliberate decision that reads as a defect.

**Unsolved: an eval for three of four modules.** `secrets` has a corpus. The
`review`, `docs` and `tests` prompts are tuned by reading their output on a
handful of diffs. The one `review` fix so far *was* checked against the three
commits it had to keep getting right, but that is a regression test with n=3,
run by hand, not an eval. A change that trades a true positive for a false
negative on code nobody re-ran is still invisible.

That is the honest ceiling on everything above. Until those modules have a
corpus of diffs with known defects, every claim about them is "it looked right
on the diffs we tried", and prompt tuning stays a matter of taste. It is also the single most
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
