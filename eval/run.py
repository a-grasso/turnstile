#!/usr/bin/env python3
"""Score a turnstile AI module against the fixture corpus.

    ./eval/run.py                      # every fixture, 3 runs each
    ./eval/run.py --module secrets     # one module
    ./eval/run.py --runs 1 --only p01  # smoke test

The question this answers is narrow and operational: **for this diff, does the
gate block the push?** That is the only thing a module does to you. Findings
that do not cross the block threshold are recorded as noise, not as failures,
because they cost attention rather than a blocked push.

Every module call is non-deterministic, so one pass is not a measurement. Each
fixture runs `--runs` times and the score is a hit *rate*. A fixture that blocks
2 of 3 times is a different animal from one that blocks 3 of 3, and a scoreboard
that hides that will have you chasing noise as though it were regression.

The scoreboard records the sha256 of each module prompt. A score is only ever a
statement about one prompt version.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
RUNNER = os.path.join(ROOT, "bin", "turnstile-ai")

SEVERITIES = ("low", "medium", "high", "critical")
RANK = {s: i + 1 for i, s in enumerate(SEVERITIES)}

PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

C_RESET, C_DIM = "\033[0m", "\033[2m"
C_GREEN, C_RED, C_YELLOW = "\033[32m", "\033[31m", "\033[33m"


# ---------------------------------------------------------------- rendering

# Fixtures store credentials as `{{TOKEN}}` and the literal is synthesised at
# run time. Two reasons, both load-bearing:
#
#   1. A corpus of realistic secrets is itself a repo full of secret-shaped
#      strings. Committing them means every scanner pointed at this repo fires
#      forever - including turnstile's own `secrets` module, on the very commit
#      that adds the corpus.
#   2. It keeps the corpus honest about what it is. Nothing here was ever a
#      live credential, and nobody has to take that on trust.
#
# Derived from a fixed seed rather than random, so a fixture renders identically
# on every machine and two scoreboards are comparable.

ALPHABET_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
ALPHABET_MIXED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
ALPHABET_B64 = ALPHABET_MIXED + "+/"


def _chars(seed: str, n: int, alphabet: str) -> str:
    out, counter = [], 0
    while len(out) < n:
        digest = hashlib.sha256(f"turnstile-eval:{seed}:{counter}".encode()).digest()
        out.extend(alphabet[b % len(alphabet)] for b in digest)
        counter += 1
    return "".join(out[:n])


def token_table() -> dict[str, str]:
    return {
        "AWS_AKID": "AKIA" + _chars("aws-akid", 16, ALPHABET_UPPER),
        "AWS_SECRET": _chars("aws-secret", 40, ALPHABET_B64),
        "OPENAI_KEY": "sk-proj-" + _chars("openai", 48, ALPHABET_MIXED),
        "GH_PAT": "ghp_" + _chars("gh-pat", 36, ALPHABET_MIXED),
        "GH_PAT_REVOKED": "ghp_" + _chars("gh-pat-revoked", 36, ALPHABET_MIXED),
        "STRIPE_KEY": "sk_live_" + _chars("stripe", 32, ALPHABET_MIXED),
        "DJANGO_SECRET": _chars("django", 50, ALPHABET_MIXED),
        "PG_PASSWORD": _chars("pg", 22, ALPHABET_MIXED),
        "MQ_PASSWORD": _chars("mq", 22, ALPHABET_MIXED),
        "OPS_PASSWORD": _chars("ops", 14, ALPHABET_MIXED),
        "PEM_BODY_1": _chars("pem1", 68, ALPHABET_B64),
        "PEM_BODY_2": _chars("pem2", 68, ALPHABET_B64),
        "PEM_BODY_3": _chars("pem3", 40, ALPHABET_B64) + "=",
        "GCP_KEY_ID": _chars("gcp-key-id", 40, "0123456789abcdef"),
        "B64_CA": _chars("b64-ca", 120, ALPHABET_B64) + "=",
        "B64_CERT": _chars("b64-cert", 120, ALPHABET_B64) + "=",
        "B64_KEY": _chars("b64-key", 160, ALPHABET_B64) + "=",
        "SPLIT_A": _chars("split-a", 9, ALPHABET_MIXED),
        "SPLIT_B": _chars("split-b", 9, ALPHABET_MIXED),
    }


def render(patch: str, table: dict[str, str], where: str) -> str:
    unknown = sorted({m for m in PLACEHOLDER.findall(patch) if m not in table})
    if unknown:
        raise SystemExit(f"{where}: unknown placeholder(s): {', '.join(unknown)}")
    return PLACEHOLDER.sub(lambda m: table[m.group(1)], patch)


# ----------------------------------------------------------------- fixtures


def load_fixtures(module: str | None, only: list[str] | None) -> list[dict]:
    fixtures = []
    for name in sorted(os.listdir(FIXTURES)):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        patch_path = os.path.join(FIXTURES, stem + ".patch")
        if not os.path.isfile(patch_path):
            raise SystemExit(f"{stem}: expectation with no .patch beside it")
        with open(os.path.join(FIXTURES, name)) as fh:
            spec = json.load(fh)
        spec["name"] = stem
        spec["patch_path"] = patch_path
        if module and spec.get("module") != module:
            continue
        if only and not any(o in stem for o in only):
            continue
        fixtures.append(spec)

    stems = {f["name"] for f in fixtures}
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".patch"):
            stem = name[: -len(".patch")]
            if not os.path.isfile(os.path.join(FIXTURES, stem + ".json")) and not only:
                raise SystemExit(f"{stem}: .patch with no expectation beside it")
    return fixtures


def module_digest(module: str, home: str) -> str:
    path = os.path.join(home, "modules", f"{module}.md")
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


# ------------------------------------------------------------------ running


def run_once(spec: dict, table: dict, model: str | None, timeout: int,
             home: str) -> dict:
    with open(spec["patch_path"], encoding="utf-8") as fh:
        patch = render(fh.read(), table, spec["name"])

    fd, path = tempfile.mkstemp(prefix=f"turnstile-eval-{spec['name']}-", suffix=".patch")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(patch)

        cmd = [sys.executable, RUNNER, "--diff-file", path,
               "--module", spec["module"], "--json", "--no-cache"]
        if model:
            cmd += ["--model", model]

        env = {**os.environ, "TURNSTILE_HOME": home}
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        seconds = time.monotonic() - started
    finally:
        os.unlink(path)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stderr.strip() or proc.stdout.strip() or
                          f"exit {proc.returncode}, no JSON")[:400],
                "seconds": seconds, "findings": [], "blocking": []}

    findings = [f for r in payload["results"] for f in r["findings"]]
    errors = [r["error"] for r in payload["results"] if r.get("error")]
    return {"error": errors[0] if errors else None, "seconds": seconds,
            "findings": findings, "blocking": payload.get("blocking", [])}


def score(spec: dict, runs: list[dict]) -> dict:
    want_block = bool(spec.get("should_block"))
    ok_runs, notes = 0, []

    for run in runs:
        if run["error"]:
            continue
        blocked = bool(run["blocking"])
        if blocked != want_block:
            continue
        if want_block:
            # Blocking for the wrong reason is not a pass. The finding has to
            # land on the file the secret is actually in, or the author is
            # being sent to the wrong place.
            wanted = {e["file"] for e in spec.get("expect", [])}
            if wanted and not any(f["file"] in wanted for f in run["blocking"]):
                notes.append("blocked on the wrong file")
                continue
        else:
            cap = spec.get("allow_max_severity")
            over = [f for f in run["findings"]
                    if cap and RANK[f["severity"]] > RANK[cap]]
            if over:
                notes.append(f"reported above {cap}")
                continue
        ok_runs += 1

    errored = sum(1 for r in runs if r["error"])
    noise = sum(len(r["findings"]) - len(r["blocking"]) for r in runs if not r["error"])
    return {
        "name": spec["name"], "kind": spec.get("kind"), "guard": spec.get("guard"),
        "should_block": want_block, "runs": len(runs), "pass": ok_runs,
        "errored": errored, "noise": noise, "notes": sorted(set(notes)),
        "seconds": round(sum(r["seconds"] for r in runs) / max(1, len(runs)), 1),
        "findings": [
            [{"file": f["file"], "severity": f["severity"], "title": f["title"]}
             for f in r["findings"]] for r in runs
        ],
    }


# ----------------------------------------------------------------- reporting


def report(rows: list[dict], meta: dict) -> str:
    lines = [f"{C_DIM}{'fixture':<34} {'want':<6} {'pass':>6}  {'noise':>5}  {'sec':>5}{C_RESET}"]
    for r in rows:
        good = r["pass"] == r["runs"]
        colour = C_GREEN if good else (C_YELLOW if r["pass"] else C_RED)
        note = ""
        if r["errored"]:
            note = f"  {C_YELLOW}{r['errored']} errored{C_RESET}"
        elif r["notes"]:
            note = f"  {C_DIM}{'; '.join(r['notes'])}{C_RESET}"
        lines.append(
            f"{r['name']:<34} {'block' if r['should_block'] else 'pass':<6} "
            f"{colour}{r['pass']}/{r['runs']}{C_RESET:>6}  {r['noise']:>5}  "
            f"{r['seconds']:>5}{note}"
        )

    pos = [r for r in rows if r["should_block"]]
    neg = [r for r in rows if not r["should_block"]]
    lines.append("")
    if pos:
        lines.append(f"  caught      {sum(r['pass'] for r in pos)}/{sum(r['runs'] for r in pos)}"
                     f"   {C_DIM}blocked when it should{C_RESET}")
    if neg:
        held = sum(r["pass"] for r in neg)
        total = sum(r["runs"] for r in neg)
        lines.append(f"  held        {held}/{total}   {C_DIM}let through when it should{C_RESET}")
    lines.append(f"  noise       {sum(r['noise'] for r in rows)}     "
                 f"{C_DIM}non-blocking findings across all runs{C_RESET}")
    lines.append(f"  {C_DIM}model={meta['model']}  prompts={meta['prompts']}"
                 f"  home={meta['home']}{C_RESET}")
    return "\n".join(lines)


def scoreboard_md(rows: list[dict], meta: dict) -> str:
    out = ["# Corpus scoreboard", "",
           f"- model: `{meta['model']}`",
           f"- prompt digests: " + ", ".join(f"`{k}@{v}`" for k, v in meta["prompts"].items()),
           f"- runs per fixture: {meta['runs']}", "",
           "A score is a statement about the prompt digests above. Re-run after any",
           "prompt edit and diff this file; that diff is the only evidence a change",
           "was an improvement.", "",
           "| fixture | want | pass | noise | sec | notes |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        note = f"{r['errored']} errored" if r["errored"] else "; ".join(r["notes"])
        out.append(f"| `{r['name']}` | {'block' if r['should_block'] else 'pass'} | "
                   f"{r['pass']}/{r['runs']} | {r['noise']} | {r['seconds']} | {note} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="eval/run.py")
    p.add_argument("--module", default="secrets", help="module to score (default: secrets)")
    p.add_argument("--only", action="append", help="substring match on fixture name (repeatable)")
    p.add_argument("--runs", type=int, default=3, help="passes per fixture (default: 3)")
    p.add_argument("--model", default=None)
    p.add_argument("--jobs", type=int, default=4, help="concurrent module calls")
    p.add_argument("--timeout", type=int, default=300)
    # An A/B against a variant prompt is the whole point of having a corpus:
    # point this at a tree with a different `modules/<name>.md` and diff the
    # two scoreboards. `eval/probes/` holds deliberately degraded prompts used
    # to check the corpus can still tell good from bad.
    p.add_argument("--turnstile-home", default=ROOT, metavar="DIR",
                   help="tree whose modules/ holds the prompts to score (default: the repo)")
    p.add_argument("--write", action="store_true", help="write eval/scoreboard.md")
    args = p.parse_args(argv)

    fixtures = load_fixtures(args.module, args.only)
    if not fixtures:
        raise SystemExit("no fixtures matched")

    table = token_table()
    modules = sorted({f["module"] for f in fixtures})
    home = os.path.realpath(args.turnstile_home)
    meta = {"model": args.model or "module default", "runs": args.runs,
            "home": os.path.relpath(home, ROOT) if home != ROOT else ".",
            "prompts": {m: module_digest(m, home) for m in modules}}

    jobs = [(f, i) for f in fixtures for i in range(args.runs)]
    print(f"{len(fixtures)} fixtures x {args.runs} runs = {len(jobs)} module calls\n")

    results: dict[str, list] = {f["name"]: [] for f in fixtures}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_once, f, table, args.model, args.timeout, home): f
                   for f, _ in jobs}
        for fut in concurrent.futures.as_completed(futures):
            spec = futures[fut]
            try:
                results[spec["name"]].append(fut.result())
            except Exception as e:  # a dead call is data, not a crash
                results[spec["name"]].append(
                    {"error": f"{type(e).__name__}: {e}", "seconds": 0.0,
                     "findings": [], "blocking": []})
            done += 1
            print(f"\r  {done}/{len(jobs)}", end="", flush=True)
    print("\r" + " " * 20 + "\r", end="")

    rows = [score(f, results[f["name"]]) for f in fixtures]
    print(report(rows, meta))

    if args.write:
        with open(os.path.join(HERE, "scoreboard.md"), "w") as fh:
            fh.write(scoreboard_md(rows, meta))
        with open(os.path.join(HERE, "scoreboard.json"), "w") as fh:
            json.dump({"meta": meta, "rows": rows}, fh, indent=2)
        print(f"\n  wrote eval/scoreboard.md")

    failed = [r for r in rows if r["pass"] != r["runs"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
