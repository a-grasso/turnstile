#!/usr/bin/env python3
"""turnstile - Claude Code PreToolUse hook: close the agent's bypass routes.

The git pre-push hook is the gate. `--no-verify` is its deliberate escape
hatch, and that is correct *for a human*: a person typing it has decided to
take responsibility for the push.

An agent typing it has decided nothing. It is routing around a failing check
because routing around the check makes the task look finished. So the escape
hatch stays open at the terminal and closes for the agent - which is the whole
point of putting the gate somewhere the model does not control.

Wire up in settings.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {
                "type": "command",
                "command": "/absolute/path/to/turnstile/claude/no-bypass.py"
              }
            ]
          }
        ]
      }
    }
"""

import json
import re
import sys

# `git push -n` is --dry-run, NOT --no-verify. Blocking it would be wrong.
PUSH = re.compile(r"\bgit\b(?:\s+-\S+|\s+--\S+)*\s+push\b")

# (pattern, why, always) - `always` rules are denied even without a `git push`
# in the command, because they disable the gate for every future push too.
BYPASSES = [
    (
        re.compile(r"--no-verify\b"),
        "`--no-verify` disables the pre-push gate.",
        False,
    ),
    (
        re.compile(r"\bTURNSTILE_SKIP\b"),
        "`TURNSTILE_SKIP` is the human bypass and is not yours to set.",
        False,
    ),
    (
        re.compile(r"\bcore\.hooksPath\b"),
        "Touching `core.hooksPath` points git at a different hook directory, "
        "which disables the gate for every push from here on.",
        True,
    ),
]

REASON = """turnstile: refusing to bypass the pre-push gate.

{why}

The gate runs this repo's own checks (see `.turnstile`). If they fail, the
change is not ready to push - fix what they reported. Run `turnstile run` to
see the failures without pushing.

If the checks themselves are wrong, say so and let the user decide. Do not
route around them."""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the session over a malformed payload

    if payload.get("tool_name") != "Bash":
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    is_push = bool(PUSH.search(command))
    for pattern, why, always in BYPASSES:
        if not pattern.search(command):
            continue
        if not (always or is_push):
            continue
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": REASON.format(why=why),
                    }
                }
            )
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
