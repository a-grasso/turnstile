#!/usr/bin/env python3
"""Claude Code Stop hook behind `turnstile hook claude-stop`.

Reads the Stop payload on stdin and runs `turnstile run --stop`. On red it
blocks the stop with the report, so the agent fixes it while it still has the
context. The loop is bounded: when the checks still fail and the tree has not
changed since the last refusal, the agent is let go with a systemMessage.

Repos without a `.turnstile` are left alone.
"""

import json
import os
import re
import subprocess
import sys
import time

TURNSTILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin", "turnstile"
)
MAX_REASON_CHARS = 6000
SLOW_NOTICE_SECONDS = float(os.environ.get("TURNSTILE_SLOW_NOTICE") or 10)

REASON = """turnstile: this repo's checks fail on the working tree, so the work is not done.

{report}

Fix what they report, then end your turn again; the checks rerun automatically,
and the ones that already passed on this tree are not rerun. `turnstile run
--stop` shows the same report. If a check itself is wrong, say so to the user
instead of working around it."""


def emit(decision: dict, notices: list[str]) -> None:
    messages = [*filter(None, [os.environ.get("TURNSTILE_NOTICE")]), *notices,
                *filter(None, [decision.get("systemMessage")])]
    if messages:
        decision["systemMessage"] = "\n".join(messages)
    if decision:
        print(json.dumps(decision))


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

    notices: list[str] = []
    env = {**os.environ, "NO_COLOR": "1"}
    approval = subprocess.run([TURNSTILE, "__approved"], cwd=root, env=env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
    if approval.returncode != 0:
        emit({"systemMessage": approval.stdout.strip()}, notices)
        return 0
    started = time.monotonic()
    run = subprocess.run([TURNSTILE, "run", "--stop"], cwd=root, env=env,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    elapsed = time.monotonic() - started
    if elapsed >= SLOW_NOTICE_SECONDS:
        notices.append(f"turnstile: the checks took {elapsed:.0f}s (not cached); "
                       "later stops reuse what passed on an unchanged tree")
    unran = re.findall(r"^\s*\? (?!ai )(.+\(could not run: .*\))$", run.stderr, re.MULTILINE)
    if unran:
        notices.append(f"turnstile: {len(unran)} check(s) could not run, so they did not verify "
                       "this work:\n" + "\n".join(unran))
    state = state_path(str(payload.get("session_id") or ""))

    if run.returncode == 0:
        try:
            os.unlink(state)
        except OSError:
            pass
        emit({}, notices)
        return 0

    tree = subprocess.run([TURNSTILE, "__tree"], cwd=root, env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()
    try:
        with open(state, encoding="utf-8") as fh:
            last_refused = fh.read().strip()
    except OSError:
        last_refused = ""

    if payload.get("stop_hook_active") and tree and tree == last_refused:
        emit({"systemMessage": "turnstile: checks still fail and nothing changed since the "
                               "last attempt, so the agent stopped. Run `turnstile run`."}, notices)
        return 0

    with open(state, "w", encoding="utf-8") as fh:
        fh.write(tree)

    report = (run.stderr + run.stdout).strip()
    if len(report) > MAX_REASON_CHARS:
        report = "...\n" + report[-MAX_REASON_CHARS:]
    emit({"decision": "block", "reason": REASON.format(report=report)}, notices)
    return 0


if __name__ == "__main__":
    sys.exit(main())
