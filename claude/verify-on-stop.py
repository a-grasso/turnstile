#!/usr/bin/env python3
"""turnstile - Claude Code Stop hook: the agent does not finish red.

The pre-push gate catches a failing check after the session that caused it has
moved on. This runs the same deterministic checks when the agent ends its turn,
and on failure hands the reasons back so it fixes them while it still has the
context. It fires because the turn ended, not because the agent remembered to
verify.

Only the deterministic checks run here. The ai modules cost money per call and
run once, at push. Passes are cached per tree, so a turn that changed nothing
costs nothing.

The corrective loop is bounded: if the checks still fail and the tree has not
changed since the last refusal, the agent is allowed to stop and the failure is
left for the user.

Repos without a `.turnstile` are left alone. Wire up in settings.json (the
timeout is in seconds and must cover the slowest test suite):

    {
      "hooks": {
        "Stop": [
          {
            "hooks": [
              {
                "type": "command",
                "command": "/absolute/path/to/turnstile/claude/verify-on-stop.py",
                "timeout": 900
              }
            ]
          }
        ]
      }
    }
"""

import json
import os
import subprocess
import sys

TURNSTILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin", "turnstile"
)
MAX_REASON_CHARS = 6000

REASON = """turnstile: this repo's checks fail on the working tree, so the work is not done.

{report}

Fix what they report, then end your turn again; the checks rerun automatically,
and the ones that already passed on this tree are not rerun. `turnstile run
--stop` shows the same report. If a check itself is wrong, say so to the user
instead of working around it."""


def git(*args: str, cwd: str) -> str | None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    return proc.stdout.strip() if proc.returncode == 0 else None


def state_path(session: str) -> str:
    base = os.environ.get("TURNSTILE_STATE") or os.path.expanduser("~/.turnstile/state")
    os.makedirs(base, exist_ok=True)
    safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_") or "unknown"
    return os.path.join(base, f"stop-{safe}")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the session over a malformed payload

    root = git("rev-parse", "--show-toplevel", cwd=payload.get("cwd") or os.getcwd())
    if not root or not os.path.isfile(os.path.join(root, ".turnstile")):
        return 0

    env = {**os.environ, "NO_COLOR": "1"}
    run = subprocess.run([TURNSTILE, "run", "--stop"], cwd=root, env=env,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    state = state_path(str(payload.get("session_id") or ""))

    if run.returncode == 0:
        try:
            os.unlink(state)
        except OSError:
            pass
        return 0

    tree = subprocess.run([TURNSTILE, "__tree"], cwd=root, env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()
    try:
        with open(state, encoding="utf-8") as fh:
            last_refused = fh.read().strip()
    except OSError:
        last_refused = ""

    if payload.get("stop_hook_active") and tree and tree == last_refused:
        print(json.dumps({
            "systemMessage": "turnstile: checks still fail and nothing changed since the "
                             "last attempt, so the agent stopped. Run `turnstile run`."
        }))
        return 0

    with open(state, "w", encoding="utf-8") as fh:
        fh.write(tree)

    report = (run.stderr + run.stdout).strip()
    if len(report) > MAX_REASON_CHARS:
        report = "...\n" + report[-MAX_REASON_CHARS:]
    print(json.dumps({"decision": "block", "reason": REASON.format(report=report)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
