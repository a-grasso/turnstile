# Probes - controls for the corpus

A corpus that every prompt passes measures nothing. These are deliberately
degraded prompts, scored the same way as the real ones, to check that the
fixtures can still tell a good prompt from a bad one.

Run one with:

    ./eval/run.py --turnstile-home eval/probes/<name> --write

and compare against the baseline scoreboard.

## `no-negatives`

The built-in `secrets` prompt with its "Do not report" section deleted, and
nothing else changed. If the negative fixtures still pass, they are not testing
the negative guidance - they are testing something the model would have got
right anyway, and the corpus is weaker than its score suggests.
