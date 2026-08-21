# eval - a corpus for the AI modules

Prompt tuning without an eval is taste. A change that removes a false positive
you noticed can just as easily remove a true positive you never re-ran, and
nothing tells you. This is the fixed set of diffs that makes a prompt change
measurable.

    ./eval/run.py                                          # score `secrets`, 3 runs each
    ./eval/run.py --only h03 --runs 5                       # chase one flaky fixture
    ./eval/run.py --turnstile-home eval/probes/no-negatives # A/B a variant prompt

`--write` records `scoreboard.md` and `scoreboard.json`. Edit a prompt, re-run,
diff the scoreboard: that diff is the only evidence a change was an improvement.

## What it measures

One question: **for this diff, does the gate block the push?** That is the only
thing a module does to you. A finding below the block threshold is recorded as
*noise* - it costs attention, not a blocked push - and does not fail a fixture.

A positive fixture also has to block *on the right file*. Blocking for the wrong
reason sends the author to the wrong place, which is a different failure wearing
a passing score.

Every call is non-deterministic, so one pass is not a measurement. Each fixture
runs `--runs` times and the score is a rate. `2/3` and `3/3` are different
animals; a scoreboard that collapses them has you chasing noise as regression.

## Fixtures

`fixtures/<name>.patch` is a unified diff; `fixtures/<name>.json` is what should
happen to it. Prefixes: `p` positive, `n` negative, `h` hard (either polarity,
built to be genuinely borderline).

`guard` records why a fixture exists:

- `prompt-anchored` - the module prompt names this case. A regression guard.
- `generalisation` - the prompt does not name it. Tests reasoning over matching.
- `hard` - built to be ambiguous, and the only kind that has ever moved.

### Secrets are placeholders, not literals

Credentials in fixtures are written `{{TOKEN}}` and synthesised at run time from
a fixed seed. Two reasons, both load-bearing: a corpus of realistic secrets is a
repo full of secret-shaped strings that every scanner - including turnstile's own
`secrets` module, on the commit that adds the corpus - will fire on forever; and
nothing here was ever a live credential, which nobody should have to take on
trust. The seed is fixed, so a fixture renders identically everywhere and two
scoreboards are comparable.

## The corpus is weaker than its score

The first thirteen fixtures scored 39/39, which looked like a result until the
control was run. `eval/probes/no-negatives` is the `secrets` prompt with its
entire "Do not report" section deleted - and it scores identically on `n01`-`n06`.
Those fixtures are not testing the prompt. They test cases the model gets right
regardless, and they would pass a prompt that had been gutted.

Only `h03` currently discriminates: 2/3 on the real prompt, 0/3 on the control.
So the corpus's actual resolving power is one fixture wide. Treat a green
scoreboard as "no regression detected by a weak instrument", not as "the prompt
is good".

The way to strengthen it is more `hard` fixtures, and re-running the control
after each batch. A fixture that both prompts pass is a fixture that measures
nothing.
