"""End-to-end tests: real temp repos, the real scripts, a fake `claude` on PATH."""

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURNSTILE = os.path.join(HOME, "bin", "turnstile")
SETTINGS = os.path.join(HOME, "claude", "settings.json")

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
system = args[args.index("--system-prompt") + 1]
log = os.environ["FAKE_CLAUDE_LOG"]
os.makedirs(log, exist_ok=True)
with open(os.path.join(log, f"{time.time_ns()}-{os.getpid()}.json"), "w") as fh:
    json.dump({"prompt": prompt, "system": system}, fh)
findings = []
if "BLOCKME" in system and os.path.exists(os.environ.get("FAKE_CLAUDE_BLOCK_FLAG", "/nonexistent")):
    findings = [{"file": "a.txt", "line": 1, "severity": "high",
                 "title": "blocked", "detail": "fake finding"}]
print(json.dumps({"is_error": False, "result": "", "structured_output": {"findings": findings}}))
"""


class Repo:
    def __init__(self, test: unittest.TestCase):
        self.tmp = tempfile.mkdtemp(prefix="turnstile-test-")
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "repo")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.counter = os.path.join(self.tmp, "counter")
        self.claude_log = os.path.join(self.tmp, "claude-log")
        self.block_flag = os.path.join(self.tmp, "block-flag")

        self.fakebin = fakebin = os.path.join(self.tmp, "bin")
        os.makedirs(fakebin)
        self.fake("claude", FAKE_CLAUDE)

        gitconfig = os.path.join(self.tmp, "gitconfig")
        with open(gitconfig, "w") as fh:
            fh.write("[user]\n\tname = t\n\temail = turnstile-test\n[init]\n\tdefaultBranch = main\n")

        # Under a git hook (this suite runs in turnstile's own pre-push gate)
        # GIT_DIR and friends point at the real repo, and every git call below
        # would land there.
        inherited = {k: v for k, v in os.environ.items()
                     if not k.startswith(("GIT_", "TURNSTILE_"))
                     and k not in ("IN_NIX_SHELL", "DIRENV_DIR")}
        self.env = {
            **inherited,
            "PATH": f"{fakebin}:{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": gitconfig,
            "GIT_CONFIG_NOSYSTEM": "1",
            "TURNSTILE_CACHE": os.path.join(self.tmp, "cache-ai"),
            "TURNSTILE_CHECK_CACHE": os.path.join(self.tmp, "cache-checks"),
            "TURNSTILE_STATE": os.path.join(self.tmp, "state"),
            "TURNSTILE_ALLOWED": os.path.join(self.tmp, "allowed"),
            "FAKE_CLAUDE_LOG": self.claude_log,
            "FAKE_CLAUDE_BLOCK_FLAG": self.block_flag,
            "NO_COLOR": "1",
        }

        subprocess.run(["git", "init", "-q", "--bare", self.remote], env=self.env, check=True)
        subprocess.run(["git", "init", "-q", self.root], env=self.env, check=True)
        self.write("a.txt", "a\n")
        self.write("lib/b.txt", "b\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.git("remote", "add", "origin", self.remote)
        self.git("push", "-q", "-u", "origin", "main", "--no-verify")

    def fake(self, name: str, script: str) -> None:
        path = os.path.join(self.fakebin, name)
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)

    def write(self, rel: str, content: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def config(self, text: str, allow: bool = True) -> None:
        self.write(".turnstile", textwrap.dedent(text))
        if allow:
            self.turnstile("allow")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def commit_all(self, msg: str = "change") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def turnstile(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([TURNSTILE, *args], cwd=self.root, env=self.env, input=stdin,
                              capture_output=True, text=True)

    def pre_push(self) -> subprocess.CompletedProcess:
        head = self.git("rev-parse", "HEAD").strip()
        remote = self.git("rev-parse", "origin/main").strip()
        return self.turnstile("__pre-push", "origin",
                              stdin=f"refs/heads/main {head} refs/heads/main {remote}\n")

    def counted(self) -> str:
        """A check command that records each time it actually executes."""
        return f"echo x >> {self.counter}"

    def runs(self) -> int:
        try:
            with open(self.counter) as fh:
                return len(fh.readlines())
        except FileNotFoundError:
            return 0

    def prompts(self) -> list[dict]:
        if not os.path.isdir(self.claude_log):
            return []
        out = []
        for name in sorted(os.listdir(self.claude_log)):
            with open(os.path.join(self.claude_log, name)) as fh:
                out.append(json.load(fh))
        return out


class ScopedChecks(unittest.TestCase):
    def test_check_is_skipped_when_nothing_in_its_scope_changed(self):
        r = Repo(self)
        r.config(f"lib [lib/**]: {r.counted()}\n")
        r.write("a.txt", "changed\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)
        self.assertIn("lib", res.stderr)
        self.assertIn("not touched", res.stderr)

    def test_untracked_file_in_scope_triggers_the_check(self):
        r = Repo(self)
        r.config(f"lib [lib/**]: {r.counted()}\n")
        r.write("lib/new.txt", "new\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_unscoped_check_always_runs(self):
        r = Repo(self)
        r.config(f"all: {r.counted()}\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_changed_files_include_uncommitted_and_untracked_work(self):
        r = Repo(self)
        out = os.path.join(r.tmp, "changed")
        r.config(f'all: printf "%s" "$TURNSTILE_CHANGED_FILES" > {out}\n')
        r.write("a.txt", "edited\n")
        r.write("lib/new.txt", "new\n")

        r.turnstile("run", "--no-ai")

        with open(out) as fh:
            changed = set(fh.read().split())
        self.assertTrue({"a.txt", "lib/new.txt"} <= changed, changed)

    def test_changed_files_name_non_ascii_paths_as_they_are_on_disk(self):
        r = Repo(self)
        out = os.path.join(r.tmp, "changed")
        r.config(f'all: printf "%s" "$TURNSTILE_CHANGED_FILES" > {out}\n')
        r.write("data/Kundenliste_M\u00fcller.csv", "x\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(out) as fh:
            changed = fh.read()
        self.assertIn("data/Kundenliste_M\u00fcller.csv", changed.split("\n"))
        self.assertNotIn("\\", changed)

    def test_pushed_changed_files_name_non_ascii_paths_as_they_are_on_disk(self):
        r = Repo(self)
        out = os.path.join(r.tmp, "changed")
        r.config(f'all: printf "%s" "$TURNSTILE_CHANGED_FILES" > {out}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.write("data/Kundenliste_M\u00fcller.csv", "x\n")
        r.commit_all("umlaut")

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(out) as fh:
            changed = fh.read()
        self.assertEqual(changed.split("\n"), ["data/Kundenliste_M\u00fcller.csv"])

    def test_diff_file_carries_the_change_under_review(self):
        r = Repo(self)
        r.config('no-skip: test -f "$TURNSTILE_DIFF_FILE" && ! grep -q "^+.*SKIPPED" "$TURNSTILE_DIFF_FILE"\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.write("lib/new.txt", "fine\n")
        clean = r.turnstile("run", "--no-ai")
        r.write("lib/new.txt", "SKIPPED\n")
        dirty = r.turnstile("run", "--no-ai")

        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(dirty.returncode, 1, dirty.stderr)


class HookEnvironment(unittest.TestCase):
    def test_checks_do_not_see_the_hooks_git_environment(self):
        r = Repo(self)
        decoy = os.path.join(r.tmp, "decoy.git")
        subprocess.run(["git", "init", "-q", "--bare", decoy], env=r.env, check=True)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "${{GIT_DIR:-unset}}" > {seen}\n')
        r.commit_all("config")

        head = r.git("rev-parse", "HEAD").strip()
        remote = r.git("rev-parse", "origin/main").strip()
        env = {**r.env, "GIT_DIR": os.path.join(r.root, ".git"), "GIT_INDEX_FILE": os.path.join(r.root, ".git", "index")}
        res = subprocess.run([TURNSTILE, "__pre-push", "origin"], cwd=r.root, env=env,
                             input=f"refs/heads/main {head} refs/heads/main {remote}\n",
                             capture_output=True, text=True)

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read(), "unset")


class NewBranchBase(unittest.TestCase):
    def test_new_branch_is_diffed_against_the_remote_branch_it_was_cut_from(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "develop")
        r.write("unreleased.txt", "on develop, not on main\n")
        r.commit_all("develop work")
        r.git("push", "-q", "-u", "origin", "develop", "--no-verify")
        r.git("switch", "-q", "-c", "feature")
        r.write("feature.txt", "the change being pushed\n")
        r.commit_all("feature")

        head = r.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/feature {head} refs/heads/feature {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read().split(), ["feature.txt"])

    def test_rebased_branch_is_diffed_against_its_new_base_not_its_old_tip(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "feature")
        r.write("feature.txt", "the change being pushed\n")
        r.commit_all("feature")
        r.git("push", "-q", "-u", "origin", "feature", "--no-verify")
        old_tip = r.git("rev-parse", "HEAD").strip()
        r.git("switch", "-q", "main")
        r.write("upstream.txt", "landed on main meanwhile\n")
        r.commit_all("upstream work")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "feature")
        r.git("rebase", "-q", "main")

        head = r.git("rev-parse", "HEAD").strip()
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/feature {head} refs/heads/feature {old_tip}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read().split(), ["feature.txt"])

    def test_pushing_a_commit_the_remote_already_has_to_a_new_name_checks_an_empty_change(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")

        head = r.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/copy {head} refs/heads/copy {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read(), "")

    def test_a_commit_only_a_non_default_remote_branch_holds_is_still_checked(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "HEAD:refs/heads/wip", "--no-verify")
        r.git("update-ref", "-d", "refs/remotes/origin/main")

        head = r.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/copy {head} refs/heads/copy {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertTrue({"a.txt", "lib/b.txt"} <= set(fh.read().split()))

    def test_the_first_push_into_an_empty_remote_is_diffed_against_the_empty_tree(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("update-ref", "-d", "refs/remotes/origin/main")

        head = r.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/main {head} refs/heads/main {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertTrue({"a.txt", "lib/b.txt", ".turnstile"} <= set(fh.read().split()))

    def test_a_branch_unrelated_to_the_remote_is_diffed_against_the_empty_tree(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "--orphan", "fresh")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.write("fresh.txt", "unrelated history\n")
        r.commit_all("fresh")

        head = r.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/fresh {head} refs/heads/fresh {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(set(fh.read().split()), {".turnstile", "fresh.txt"})


class PassCache(unittest.TestCase):
    def test_a_pass_on_an_unchanged_tree_is_not_rerun(self):
        r = Repo(self)
        r.config(f"all: {r.counted()}\n")

        r.turnstile("run", "--no-ai")
        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)
        self.assertIn("cached", res.stderr)

    def test_change_outside_scope_keeps_the_cached_pass(self):
        r = Repo(self)
        r.config(f"lib [lib/**]: {r.counted()}\n")
        r.write("lib/b.txt", "changed\n")
        r.turnstile("run", "--no-ai")

        r.write("a.txt", "elsewhere\n")
        r.turnstile("run", "--no-ai")

        self.assertEqual(r.runs(), 1)

    def test_change_inside_scope_reruns_the_check(self):
        r = Repo(self)
        r.config(f"lib [lib/**]: {r.counted()}\n")
        r.write("lib/b.txt", "changed\n")
        r.turnstile("run", "--no-ai")

        r.write("lib/b.txt", "changed again\n")
        r.turnstile("run", "--no-ai")

        self.assertEqual(r.runs(), 2)

    def test_editing_the_command_reruns_the_check(self):
        r = Repo(self)
        r.config(f"all: {r.counted()}\n")
        r.turnstile("run", "--no-ai")

        r.config(f"all: {r.counted()} && true\n")
        r.turnstile("run", "--no-ai")

        self.assertEqual(r.runs(), 2)

    def test_a_failure_is_never_cached(self):
        r = Repo(self)
        r.config(f"all: {r.counted()} && false\n")

        r.turnstile("run", "--no-ai")
        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 1)
        self.assertEqual(r.runs(), 2)

    def test_no_cache_flag_forces_a_rerun(self):
        r = Repo(self)
        r.config(f"all: {r.counted()}\n")

        r.turnstile("run", "--no-ai")
        r.turnstile("run", "--no-ai", "--no-cache")

        self.assertEqual(r.runs(), 2)

    def test_push_of_a_tree_that_already_passed_is_free(self):
        r = Repo(self)
        r.config(f"lib [lib/**]: {r.counted()}\n")
        r.write("lib/b.txt", "changed\n")
        r.turnstile("run", "--no-ai")

        r.commit_all()
        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)
        self.assertIn("cached", res.stderr)


class TreeMutation(unittest.TestCase):
    def test_a_check_that_rewrites_files_fails(self):
        r = Repo(self)
        r.config("fmt: echo formatted > lib/b.txt\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("lib/b.txt", res.stderr)
        self.assertIn("modified", res.stderr)

    def test_the_second_run_after_a_rewrite_passes(self):
        r = Repo(self)
        r.config("fmt: echo formatted > lib/b.txt\n")

        r.turnstile("run", "--no-ai")
        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)


class Parallel(unittest.TestCase):
    def test_checks_run_concurrently(self):
        r = Repo(self)
        a, b = os.path.join(r.tmp, "a-started"), os.path.join(r.tmp, "b-started")
        wait_for = "for i in $(seq 50); do [ -f {} ] && exit 0; sleep 0.1; done; exit 1"
        r.config(f"a: touch {a}; {wait_for.format(b)}\n"
                 f"b: touch {b}; {wait_for.format(a)}\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)

    def test_results_are_reported_in_config_order(self):
        r = Repo(self)
        r.config("slow: sleep 1\nfast: true\n")

        res = r.turnstile("run", "--no-ai")

        self.assertLess(res.stderr.index("slow"), res.stderr.index("fast"))


class PushOnly(unittest.TestCase):
    def test_stop_mode_skips_a_push_only_check_and_says_so(self):
        r = Repo(self)
        r.config(f"push e2e: {r.counted()}\n")

        res = r.turnstile("run", "--stop")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)
        self.assertIn("e2e (at push)", res.stderr)

    def test_a_full_run_includes_push_only_checks(self):
        r = Repo(self)
        r.config(f"push e2e: {r.counted()}\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_push_only_check_keeps_its_scope(self):
        r = Repo(self)
        r.config(f"push e2e [lib/**]: {r.counted()}\n")
        r.write("a.txt", "changed\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(r.runs(), 0)
        self.assertIn("e2e (not touched)", res.stderr)

    def test_stop_mode_never_calls_the_model(self):
        r = Repo(self)
        r.write(".turnstile.d/modules/m.md", "---\nname: m\n---\nReview it.\n")
        r.config("unit: true\nai m: block=high\n")
        r.write("a.txt", "x\n")
        r.commit_all()

        r.turnstile("run", "--stop")

        self.assertEqual(r.prompts(), [])


class Intent(unittest.TestCase):
    def test_intent_line_is_not_executed_as_a_check(self):
        r = Repo(self)
        r.config(f"intent: {r.counted()}\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)

    def test_intent_output_reaches_the_module_prompt(self):
        r = Repo(self)
        r.write(".turnstile.d/modules/m.md", "---\nname: m\n---\nReview it.\n")
        r.config('intent: echo "ISSUE-$((40 + 2)) make the widget blue"\nai m: block=high\n')
        r.write("a.txt", "blue\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ISSUE-42 make the widget blue", r.prompts()[0]["prompt"])

    def test_failing_intent_command_does_not_stop_the_review(self):
        r = Repo(self)
        r.write(".turnstile.d/modules/m.md", "---\nname: m\n---\nReview it.\n")
        r.config("intent: exit 3\nai m: block=high\n")
        r.write("a.txt", "blue\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(r.prompts()), 1)


class AiOnlyAtTheEnd(unittest.TestCase):
    def test_no_ai_flag_never_calls_the_model(self):
        r = Repo(self)
        r.write(".turnstile.d/modules/m.md", "---\nname: m\n---\nReview it.\n")
        r.config("ai m: block=high\n")
        r.write("a.txt", "x\n")
        r.commit_all()

        r.turnstile("run", "--no-ai")

        self.assertEqual(r.prompts(), [])


class DeltaReview(unittest.TestCase):
    def setUp(self):
        r = self.r = Repo(self)
        r.write(".turnstile.d/modules/docs.md", "---\nname: docs\n---\nDocs reviewer.\n")
        r.write(".turnstile.d/modules/tests.md", "---\nname: tests\n---\nBLOCKME tests reviewer.\n")
        r.config("ai docs: block=high\nai tests: block=high\n")
        r.write("a.txt", "FIRST-CHANGE\n")
        r.commit_all("first")

    def review(self) -> subprocess.CompletedProcess:
        return self.r.turnstile("ai", "--range", "origin/main..HEAD")

    def prompt_for(self, marker: str) -> str:
        return [p["prompt"] for p in self.r.prompts() if marker in p["system"]][-1]

    def test_a_module_that_passed_reviews_only_the_follow_up(self):
        r = self.r
        open(r.block_flag, "w").close()
        self.assertEqual(self.review().returncode, 1)

        os.unlink(r.block_flag)
        r.write("lib/b.txt", "SECOND-CHANGE\n")
        r.commit_all("fix")
        res = self.review()

        self.assertEqual(res.returncode, 0, res.stderr)
        docs = self.prompt_for("Docs reviewer")
        self.assertIn("SECOND-CHANGE", docs)
        self.assertNotIn("FIRST-CHANGE", docs)

    def test_a_module_that_blocked_reviews_the_whole_change_again(self):
        r = self.r
        open(r.block_flag, "w").close()
        self.review()

        os.unlink(r.block_flag)
        r.write("lib/b.txt", "SECOND-CHANGE\n")
        r.commit_all("fix")
        self.review()

        tests = self.prompt_for("BLOCKME")
        self.assertIn("FIRST-CHANGE", tests)
        self.assertIn("SECOND-CHANGE", tests)

    def test_a_pass_on_a_sibling_branch_is_not_a_pass_on_this_one(self):
        r = self.r
        self.assertEqual(self.review().returncode, 0)

        r.git("switch", "-q", "-c", "sibling", "origin/main")
        r.git("checkout", "main", "--", ".turnstile", ".turnstile.d")
        r.write("lib/b.txt", "SIBLING-CHANGE\n")
        r.commit_all("sibling")
        res = self.review()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("follow-up", res.stderr + res.stdout)
        docs = self.prompt_for("Docs reviewer")
        self.assertIn("SIBLING-CHANGE", docs)
        self.assertNotIn("FIRST-CHANGE", docs)

    def test_an_unchanged_rerun_costs_no_model_call(self):
        self.review()
        calls = len(self.r.prompts())

        self.review()

        self.assertEqual(len(self.r.prompts()), calls)


class CannotRunHere(unittest.TestCase):
    def test_exit_77_is_reported_with_the_last_line_and_is_not_a_failure(self):
        r = Repo(self)
        r.config("e2e: echo noise; echo 'docker is not running'; exit 77\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("? e2e (could not run: docker is not running)", res.stderr)
        self.assertNotIn("check(s) failed", res.stderr)

    def test_the_summary_counts_the_checks_that_could_not_run(self):
        r = Repo(self)
        r.config("a: exit 77\nb: exit 77\nc: true\n")

        res = r.turnstile("run", "--no-ai")

        self.assertIn("all checks passed (2 check(s) could not run)", res.stderr)

    def test_a_check_that_could_not_run_is_never_cached(self):
        r = Repo(self)
        r.config(f"e2e: {r.counted()}; exit 77\n")

        r.turnstile("run", "--no-ai")
        res = r.turnstile("run", "--no-ai")

        self.assertEqual(r.runs(), 2)
        self.assertNotIn("cached", res.stderr)

    def test_it_does_not_hide_a_real_failure_beside_it(self):
        r = Repo(self)
        r.config("e2e: exit 77\nlint: exit 1\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 1)
        self.assertIn("1 check(s) failed", res.stderr)

    def test_a_push_goes_through_but_says_it_was_not_fully_verified(self):
        r = Repo(self)
        r.config("e2e: exit 77\n")
        r.commit_all("config")

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("1 check(s) could not run", res.stderr)

    def test_the_stop_hook_lets_the_agent_stop_and_says_what_did_not_run(self):
        r = Repo(self)
        r.config("e2e: echo no docker; exit 77\n")
        payload = {"session_id": "s1", "cwd": r.root, "hook_event_name": "Stop"}

        res = subprocess.run([TURNSTILE, "hook", "claude-stop"], input=json.dumps(payload),
                             env=r.env, capture_output=True, text=True, cwd=r.root)

        out = json.loads(res.stdout)
        self.assertNotIn("decision", out)
        self.assertIn("1 check(s) could not run", out["systemMessage"])
        self.assertIn("e2e (could not run: no docker)", out["systemMessage"])


SLEEPY_CLAUDE = "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n"


def ai_repo(test: unittest.TestCase, config: str = "ai m: block=high\n",
            frontmatter: str = "") -> Repo:
    """A repo whose gate (config and module) is already on the remote, so a range
    review sees only what the test commits afterwards."""
    r = Repo(test)
    r.write(".turnstile.d/modules/m.md", f"---\nname: m\n{frontmatter}---\nReview it.\n")
    r.config(config)
    r.commit_all("gate")
    r.git("push", "-q", "origin", "main", "--no-verify")
    return r


class AiTimeout(unittest.TestCase):
    def test_a_timed_out_module_is_a_question_mark_not_a_failure(self):
        r = ai_repo(self)
        r.fake("claude", SLEEPY_CLAUDE)
        r.env["TURNSTILE_AI_TIMEOUT"] = "1"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertRegex(res.stderr, r"\? ai m .*timed out after 1s")
        self.assertIn("ai module(s) could not run - this push was NOT reviewed by them", res.stderr)

    def test_config_timeout_overrides_the_environment(self):
        r = ai_repo(self, "ai m: timeout=1\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.env["TURNSTILE_AI_TIMEOUT"] = "600"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("timed out after 1s", res.stderr)

    def test_config_timeout_wins_over_frontmatter(self):
        r = ai_repo(self, "ai m: timeout=1\n", "timeout: 600\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertIn("timed out after 1s", res.stderr)

    def test_frontmatter_timeout_wins_over_the_environment(self):
        r = ai_repo(self, "ai m: block=high\n", "timeout: 1\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.env["TURNSTILE_AI_TIMEOUT"] = "600"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertIn("timed out after 1s", res.stderr)

    def test_an_explicit_timeout_flag_wins_over_the_frontmatter(self):
        r = ai_repo(self, "ai m: block=high\n", "timeout: 600\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD", "--timeout", "1")

        self.assertIn("timed out after 1s", res.stderr)

    def test_config_timeout_wins_over_an_explicit_timeout_flag(self):
        r = ai_repo(self, "ai m: timeout=1\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD", "--timeout", "600")

        self.assertIn("timed out after 1s", res.stderr)

    def test_an_unparseable_timeout_in_the_environment_does_not_stop_the_review(self):
        r = ai_repo(self)
        r.env["TURNSTILE_AI_TIMEOUT"] = "soon"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TURNSTILE_AI_TIMEOUT='soon' is not a positive integer, using 600", res.stderr)
        self.assertEqual(len(r.prompts()), 1)

    def test_bad_timeout_value_is_a_config_error(self):
        r = ai_repo(self, "ai m: timeout=abc\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")
        strict = r.turnstile("ai", "--range", "origin/main..HEAD", "--strict")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(".turnstile:1: timeout='abc' is not a positive integer", res.stderr)
        self.assertEqual(r.prompts(), [])
        self.assertEqual(strict.returncode, 2, strict.stderr)

    def test_a_zero_timeout_is_a_config_error(self):
        r = ai_repo(self, "ai m: timeout=0\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertIn("timeout='0' is not a positive integer", res.stderr)
        self.assertEqual(r.prompts(), [])

    def test_bad_frontmatter_timeout_is_a_config_error(self):
        r = ai_repo(self, "ai m: block=high\n", "timeout: soon\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")
        strict = r.turnstile("ai", "--range", "origin/main..HEAD", "--strict")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("timeout: 'soon' is not a positive integer", res.stderr)
        self.assertEqual(r.prompts(), [])
        self.assertEqual(strict.returncode, 2, strict.stderr)


class AiFailClosed(unittest.TestCase):
    def cannot_run(self, config: str = "ai m: fail=closed\n") -> Repo:
        r = ai_repo(self, config)
        r.fake("claude", SLEEPY_CLAUDE)
        r.env["TURNSTILE_AI_TIMEOUT"] = "1"
        r.write("a.txt", "changed\n")
        return r

    def test_fail_closed_module_that_cannot_run_refuses_the_push(self):
        r = self.cannot_run()
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ai m (could not run: timed out after 1s) [fail=closed]", res.stderr)
        self.assertIn("push refused", res.stderr)
        self.assertNotIn("? ai m", res.stderr)

    def test_fail_closed_applies_to_a_staged_review_too(self):
        r = self.cannot_run()
        r.git("add", "-A")

        res = r.turnstile("ai", "--staged")

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("[fail=closed]", res.stderr)

    def test_fail_open_module_that_cannot_run_keeps_the_question_mark(self):
        r = self.cannot_run("ai m: fail=open\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("? ai m (timed out after 1s)", res.stderr)
        self.assertNotIn("fail=closed", res.stderr)

    def test_fail_closed_module_that_runs_green_passes(self):
        r = ai_repo(self, "ai m: fail=closed\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("fail=closed", res.stderr)

    def test_fail_closed_module_that_cannot_be_loaded_refuses_the_push(self):
        r = ai_repo(self, "ai ghost: fail=closed\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ai ghost (could not run: no module 'ghost') [fail=closed]", res.stderr)

    def test_fail_open_module_that_cannot_be_loaded_does_not_refuse(self):
        r = ai_repo(self, "ai ghost: block=high\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 0, res.stderr)

    def test_fail_closed_line_with_a_bad_timeout_refuses_the_push(self):
        r = ai_repo(self, "ai m: timeout=abc fail=closed\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.pre_push()

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ai m (could not run: timeout='abc' is not a positive integer) [fail=closed]",
                      res.stderr)
        self.assertEqual(r.prompts(), [])

    def test_fail_closed_refuses_when_the_diff_cannot_be_read(self):
        r = ai_repo(self, "ai m: fail=closed\n")

        res = r.turnstile("ai", "--range", "nosuchref..HEAD")

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("fail=closed", res.stderr)

    def test_fail_open_module_lets_an_unreadable_diff_through(self):
        r = ai_repo(self, "ai m: block=high\n")

        res = r.turnstile("ai", "--range", "nosuchref..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)

    def test_bad_fail_value_is_a_config_error(self):
        r = ai_repo(self, "ai m: fail=sideways\n")
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")
        strict = r.turnstile("ai", "--range", "origin/main..HEAD", "--strict")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(".turnstile:1: fail='sideways' is not one of open, closed", res.stderr)
        self.assertEqual(r.prompts(), [])
        self.assertEqual(strict.returncode, 2, strict.stderr)

    def test_frontmatter_cannot_opt_a_module_into_failing_closed(self):
        r = ai_repo(self, "ai m: block=high\n", "fail: closed\n")
        r.fake("claude", SLEEPY_CLAUDE)
        r.env["TURNSTILE_AI_TIMEOUT"] = "1"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = r.turnstile("ai", "--range", "origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("? ai m", res.stderr)


class AiDiffSelection(unittest.TestCase):
    def repo(self, max_lines: int = 40) -> Repo:
        r = ai_repo(self)
        r.env["TURNSTILE_AI_MAX_DIFF_LINES"] = str(max_lines)
        return r

    def big_and_tail(self, r: Repo) -> None:
        r.write("aa_big.txt", "".join(f"filler {i}\n" for i in range(300)))
        r.write("zz_tail.txt", "TAIL-MARKER\n")
        r.commit_all()

    def review(self, r: Repo, *args: str) -> subprocess.CompletedProcess:
        return r.turnstile("ai", "--range", "origin/main..HEAD", *args)

    def test_every_changed_file_contributes_when_the_diff_is_truncated(self):
        r = self.repo()
        self.big_and_tail(r)

        res = self.review(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TAIL-MARKER", r.prompts()[0]["prompt"])

    def test_a_file_too_big_to_fit_whole_is_cut_with_a_marker(self):
        r = self.repo(100)
        self.big_and_tail(r)

        self.review(r)

        prompt = r.prompts()[0]["prompt"]
        self.assertIn("TAIL-MARKER", prompt)
        self.assertIn("filler 0", prompt)
        self.assertNotIn("filler 299", prompt)
        self.assertRegex(prompt, r"\.\.\. \[aa_big\.txt: showing \d+ of 306 diff lines\]")
        self.assertNotIn("omitted:", prompt)

    def test_truncation_names_the_omitted_files(self):
        r = self.repo()
        self.big_and_tail(r)

        res = self.review(r)

        prompt = r.prompts()[0]["prompt"]
        self.assertRegex(prompt, r"NOTE: diff truncated: showing 1 of 2 changed files "
                                 r"\(\d+ of 3\d\d lines\); omitted: aa_big\.txt\. "
                                 r"Do not report on what you cannot see\.")
        self.assertRegex(res.stderr, r"! turnstile-ai: diff truncated:.*omitted: aa_big\.txt")

    def test_the_stderr_warning_lists_at_most_ten_omitted_paths(self):
        r = self.repo(10)
        for i in range(15):
            r.write(f"f{i:02}.txt", "".join(f"row {j}\n" for j in range(20)))
        r.commit_all()

        res = self.review(r)

        warning = next(l for l in res.stderr.splitlines() if "diff truncated:" in l)
        self.assertIn("(+5 more)", warning)
        self.assertEqual(warning.count(".txt"), 10)
        self.assertIn("f14.txt", r.prompts()[0]["prompt"])

    def test_generated_files_are_dropped_before_source(self):
        r = self.repo(100)
        r.write("package-lock.json", "".join(f"LOCK-CONTENT-{i}\n" for i in range(30)))
        r.write("src.py", "".join(f"SRC-LINE-{i}\n" for i in range(70)))
        r.commit_all()

        self.review(r)

        prompt = r.prompts()[0]["prompt"]
        self.assertIn("SRC-LINE-69", prompt)
        self.assertNotIn("LOCK-CONTENT", prompt)
        self.assertIn("omitted: package-lock.json", prompt)

    def test_a_partial_review_writes_no_pass_record(self):
        r = self.repo()
        r.write("a.txt", "FIRST-CHANGE\n")
        self.big_and_tail(r)
        self.assertEqual(self.review(r).returncode, 0)

        r.write("lib/b.txt", "SECOND-CHANGE\n")
        r.commit_all("second")
        res = self.review(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("follow-up since", res.stderr)
        prompt = r.prompts()[-1]["prompt"]
        self.assertIn("FIRST-CHANGE", prompt)
        self.assertIn("SECOND-CHANGE", prompt)

    def test_a_partial_review_says_so_in_the_report(self):
        r = self.repo()
        self.big_and_tail(r)

        res = self.review(r)

        self.assertRegex(res.stderr, r"✓ ai m \(no findings, partial review, ")

    def test_json_reports_truncation(self):
        r = self.repo()
        self.big_and_tail(r)

        res = self.review(r, "--json")

        out = json.loads(res.stdout)
        self.assertEqual(out["truncation"]["omitted_files"], ["aa_big.txt"])
        self.assertEqual(out["truncation"]["shown_files"], 1)
        self.assertEqual(out["truncation"]["total_files"], 2)
        self.assertIn("diff truncated", out["truncation"]["note"])
        self.assertTrue(all(res["partial"] for res in out["results"]))

    def test_json_truncation_is_null_when_nothing_was_cut(self):
        r = self.repo(4000)
        r.write("a.txt", "small\n")
        r.commit_all()

        out = json.loads(self.review(r, "--json").stdout)

        self.assertIsNone(out["truncation"])
        self.assertFalse(any(res["partial"] for res in out["results"]))

    def test_small_diff_is_not_truncated(self):
        r = self.repo(4000)
        r.write("a.txt", "FIRST-CHANGE\n")
        r.commit_all("first")
        first = self.review(r)

        r.write("lib/b.txt", "SECOND-CHANGE\n")
        r.commit_all("second")
        second = self.review(r)

        for res in (first, second):
            self.assertNotIn("truncated", res.stderr)
            self.assertNotIn("partial", res.stderr)
        self.assertNotIn("truncated", r.prompts()[0]["prompt"])
        self.assertIn("follow-up since", second.stderr)

    def test_a_byte_heavy_file_does_not_crowd_out_the_small_ones_after_it(self):
        r = self.repo(4000)
        r.env["TURNSTILE_AI_MAX_DIFF_BYTES"] = "2000"
        r.write("a_wide.txt", "w" * 5000 + "\n")
        for name in ("b", "c", "d"):
            r.write(f"{name}.txt", f"SMALL-{name}\nrow\nrow\n")
        r.commit_all()

        self.review(r)

        prompt = r.prompts()[0]["prompt"]
        for name in ("b", "c", "d"):
            self.assertIn(f"SMALL-{name}", prompt)

    def test_a_form_feed_in_a_line_does_not_count_as_a_line_break(self):
        r = self.repo(40)
        r.write("ff.txt", "".join("x\x0cy\n" for _ in range(30)))
        r.commit_all()

        res = self.review(r)

        self.assertNotIn("truncated", res.stderr)

    def test_lockfiles_and_generated_code_of_other_ecosystems_go_last(self):
        r = self.repo(100)
        for name in ("go.sum", "pnpm-lock.yaml", "x_pb2.py", "x_pb2_grpc.py"):
            r.write(name, "".join(f"GEN-{i}\n" for i in range(30)))
        r.write("src.py", "".join(f"SRC-LINE-{i}\n" for i in range(70)))
        r.commit_all()

        self.review(r)

        prompt = r.prompts()[0]["prompt"]
        self.assertIn("SRC-LINE-69", prompt)
        self.assertNotIn("GEN-", prompt)

    def test_an_unparseable_budget_in_the_environment_does_not_stop_the_review(self):
        r = ai_repo(self)
        r.env["TURNSTILE_AI_MAX_DIFF_LINES"] = "lots"
        r.env["TURNSTILE_AI_MAX_DIFF_BYTES"] = "0"
        r.write("a.txt", "changed\n")
        r.commit_all()

        res = self.review(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TURNSTILE_AI_MAX_DIFF_LINES='lots' is not a positive integer, using 4000",
                      res.stderr)
        self.assertIn("TURNSTILE_AI_MAX_DIFF_BYTES='0' is not a positive integer, using 400000",
                      res.stderr)
        self.assertEqual(len(r.prompts()), 1)

    def test_a_follow_up_diff_over_budget_is_partial_too(self):
        r = self.repo(40)
        r.write("a.txt", "FIRST-CHANGE\n")
        r.commit_all("first")
        self.assertEqual(self.review(r).returncode, 0)

        self.big_and_tail(r)
        res = self.review(r)

        self.assertIn("follow-up since", res.stderr)
        self.assertIn("partial review", res.stderr)
        self.assertIn("omitted: aa_big.txt", r.prompts()[-1]["prompt"])

    def test_a_hand_made_diff_is_cut_by_position(self):
        r = self.repo(5)
        patch = os.path.join(r.tmp, "x.patch")
        with open(patch, "w") as fh:
            fh.write("--- a/x\n+++ b/x\n@@ -0,0 +1,9 @@\n" + "".join(f"+row {i}\n" for i in range(9)))

        res = r.turnstile("ai", "--diff-file", patch, "--module", "m")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("diff truncated to 5 of 12 lines", r.prompts()[0]["prompt"])

    def test_non_ascii_paths_are_named_as_they_are(self):
        r = self.repo(10)
        r.write("zzz.txt", "".join(f"row {j}\n" for j in range(20)))
        r.write("über.txt", "tiny\n")
        r.commit_all()

        res = self.review(r)

        self.assertIn("omitted: zzz.txt", res.stderr)
        self.assertIn("diff --git a/über.txt b/über.txt", r.prompts()[0]["prompt"])


class Ci(unittest.TestCase):
    def pull_request(self, base_config: str, pr_config: str | None = None) -> Repo:
        r = Repo(self)
        r.config(base_config.replace("{counted}", r.counted()))
        r.commit_all("gate")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "pr")
        if pr_config is not None:
            r.config(pr_config.replace("{counted}", r.counted()))
        r.write("lib/change.txt", "the change\n")
        r.commit_all("the pr")
        return r

    def test_a_pr_that_deletes_a_check_still_gets_it_run(self):
        r = self.pull_request("lint: {counted}\n", "# no checks left\n")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_a_pr_cannot_turn_a_failing_check_green(self):
        r = self.pull_request("lint: false\n", "lint: true\n")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("lint", res.stderr)

    def test_a_check_the_pr_adds_is_not_run(self):
        r = Repo(self)
        r.config("lint: true\n")
        r.commit_all("gate")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "pr")
        r.config(f"lint: true\nextra: {r.counted()}\n")
        r.commit_all("the pr")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)

    def test_checks_see_exactly_the_files_of_base_to_head(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_CHANGED_FILES" > {seen}\n')
        r.commit_all("gate")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "pr")
        r.write("lib/change.txt", "the change\n")
        r.commit_all("the pr")
        r.write("uncommitted.txt", "not part of the pr\n")

        r.turnstile("ci", "--base", "main")

        with open(seen) as fh:
            self.assertEqual(fh.read().split(), ["lib/change.txt"])

    def test_ci_never_reads_or_writes_the_pass_cache(self):
        r = Repo(self)
        r.config(f"lint: {r.counted()}\n")
        r.commit_all("gate")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "pr")
        r.write("lib/change.txt", "x\n")
        r.commit_all("the pr")
        r.turnstile("run", "--no-ai")

        r.turnstile("ci", "--base", "main")
        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(r.runs(), 3)
        self.assertNotIn("cached", res.stderr)

    def test_ci_never_calls_the_model(self):
        r = self.pull_request("ai review: block=high\nlint: true\n")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.prompts(), [])
        self.assertIn("ai modules do not run in ci", res.stderr)

    def test_a_base_without_a_config_has_nothing_to_run(self):
        r = Repo(self)
        r.git("switch", "-q", "-c", "pr")
        r.config(f"lint: {r.counted()}\n")
        r.commit_all("adds the gate")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)
        self.assertIn("no .turnstile at main", res.stderr)

    def test_ci_requires_a_resolvable_base(self):
        r = Repo(self)

        self.assertNotEqual(r.turnstile("ci").returncode, 0)
        self.assertNotEqual(r.turnstile("ci", "--base", "no-such-ref").returncode, 0)


class CiAction(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HOME, "action.yml")) as fh:
            self.action = fh.read()

    def test_it_installs_nix_and_runs_ci_against_the_pull_requests_base(self):
        self.assertIn("cachix/install-nix-action", self.action)
        self.assertIn("github.event.pull_request.base.sha", self.action)
        self.assertIn('ci --base "$BASE"', self.action)

    def test_it_runs_inside_the_projects_dev_shell_when_there_is_a_flake(self):
        self.assertIn("[ -f flake.nix ]", self.action)
        self.assertIn('nix develop --no-write-lock-file -c "$turnstile" ci', self.action)

    def test_the_readme_shows_how_to_use_it(self):
        with open(os.path.join(HOME, "README.md")) as fh:
            readme = fh.read()
        self.assertIn("- uses: a-grasso/turnstile@main", readme)
        self.assertIn("fetch-depth: 0", readme)


UNAPPROVED = "turnstile: .turnstile changed since you approved it, run `turnstile allow` after reviewing it"


class Allow(unittest.TestCase):
    def unapproved(self) -> Repo:
        r = Repo(self)
        r.config(f"lint: {r.counted()}\n", allow=False)
        return r

    def test_an_unapproved_config_is_not_executed(self):
        r = self.unapproved()

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 1)
        self.assertIn(UNAPPROVED, res.stderr)
        self.assertEqual(r.runs(), 0)

    def test_allow_approves_the_current_content(self):
        r = self.unapproved()

        allowed = r.turnstile("allow")
        res = r.turnstile("run", "--no-ai")

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_editing_the_config_withdraws_the_approval(self):
        r = self.unapproved()
        r.turnstile("allow")
        r.write(".turnstile", f"lint: {r.counted()} && true\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 1)
        self.assertIn(UNAPPROVED, res.stderr)
        self.assertEqual(r.runs(), 0)

    def test_an_approval_does_not_carry_over_to_another_clone(self):
        r = self.unapproved()
        r.turnstile("allow")
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        clone = os.path.join(r.tmp, "clone")
        subprocess.run(["git", "clone", "-q", r.remote, clone], env=r.env, check=True)

        res = subprocess.run([TURNSTILE, "run", "--no-ai"], cwd=clone, env=r.env,
                             capture_output=True, text=True)

        self.assertEqual(res.returncode, 1)
        self.assertIn(UNAPPROVED, res.stderr)

    def test_a_push_with_an_unapproved_config_is_refused(self):
        r = self.unapproved()
        r.commit_all("config")

        refused = r.pre_push()
        r.turnstile("allow")
        allowed = r.pre_push()

        self.assertEqual(refused.returncode, 1)
        self.assertIn(UNAPPROVED, refused.stderr)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(r.runs(), 1)

    def test_ci_reads_the_base_branch_and_needs_no_approval(self):
        r = self.unapproved()
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "-c", "pr")
        r.write("lib/change.txt", "x\n")
        r.commit_all("the pr")

        res = r.turnstile("ci", "--base", "main")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 1)

    def test_the_stop_hook_says_so_without_blocking_or_running_anything(self):
        r = self.unapproved()
        payload = {"session_id": "s1", "cwd": r.root, "hook_event_name": "Stop"}

        res = subprocess.run([TURNSTILE, "hook", "claude-stop"], input=json.dumps(payload),
                             env=r.env, capture_output=True, text=True, cwd=r.root)

        out = json.loads(res.stdout)
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"], UNAPPROVED)
        self.assertEqual(r.runs(), 0)

    def test_status_shows_whether_the_config_is_approved(self):
        r = self.unapproved()

        before = r.turnstile("status").stderr
        r.turnstile("allow")
        after = r.turnstile("status").stderr

        self.assertIn("not approved", before)
        self.assertNotIn("not approved", after)
        self.assertIn("approved", after)

    def test_allow_without_a_config_says_there_is_nothing_to_approve(self):
        r = Repo(self)

        res = r.turnstile("allow")

        self.assertNotEqual(res.returncode, 0)


class PushedCommits(unittest.TestCase):
    def push_of(self, r: Repo, ref: str, sha: str, env: dict | None = None) -> subprocess.CompletedProcess:
        remote = r.git("rev-parse", "origin/main").strip()
        zero = "0" * 40
        known = r.git("ls-remote", "origin", ref).split()
        return subprocess.run([TURNSTILE, "__pre-push", "origin"], cwd=r.root,
                              env={**r.env, **(env or {})}, capture_output=True, text=True,
                              input=f"{ref} {sha} {ref} {known[0] if known else zero}\n")

    def head(self, r: Repo) -> str:
        return r.git("rev-parse", "HEAD").strip()

    def test_a_dirty_tree_does_not_stand_in_for_the_commits_being_pushed(self):
        r = Repo(self)
        r.config("clean: test ! -e junk.txt\n")
        r.commit_all("config")
        r.write("junk.txt", "untracked, not part of the push\n")

        res = self.push_of(r, "refs/heads/main", self.head(r))

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("temporary worktree", res.stderr)

    def test_uncommitted_edits_do_not_hide_a_bad_commit(self):
        r = Repo(self)
        r.config("content: grep -q good a.txt\n")
        r.write("a.txt", "bad\n")
        r.commit_all("bad commit")
        r.write("a.txt", "good\n")

        res = self.push_of(r, "refs/heads/main", self.head(r))

        self.assertEqual(res.returncode, 1, res.stderr)

    def test_a_branch_that_is_not_checked_out_is_checked_as_pushed(self):
        r = Repo(self)
        r.config("content: grep -q good a.txt\n")
        r.commit_all("config")
        r.git("switch", "-q", "-c", "feature")
        r.write("a.txt", "bad\n")
        r.commit_all("bad on feature")
        feature = self.head(r)
        r.git("switch", "-q", "main")
        r.write("a.txt", "good\n")
        r.commit_all("good on main")

        res = self.push_of(r, "refs/heads/feature", feature)

        self.assertEqual(res.returncode, 1, res.stderr)

    def test_a_clean_tree_that_matches_the_push_runs_in_place(self):
        r = Repo(self)
        where = os.path.join(r.tmp, "where")
        r.config(f'where: pwd -P > {where}\n')
        r.commit_all("config")

        res = self.push_of(r, "refs/heads/main", self.head(r))

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("temporary worktree", res.stderr)
        with open(where) as fh:
            self.assertEqual(fh.read().strip(), os.path.realpath(r.root))

    def test_the_temporary_worktree_is_removed_afterwards(self):
        r = Repo(self)
        where = os.path.join(r.tmp, "where")
        r.config(f'where: pwd -P > {where}\n')
        r.commit_all("config")
        r.write("junk.txt", "dirty\n")

        self.push_of(r, "refs/heads/main", self.head(r))

        with open(where) as fh:
            checked_out_at = fh.read().strip()
        self.assertNotEqual(checked_out_at, os.path.realpath(r.root))
        self.assertFalse(os.path.exists(checked_out_at))
        self.assertEqual(len(r.git("worktree", "list").strip().splitlines()), 1)

    def test_checks_that_cannot_run_in_the_checkout_are_not_red(self):
        r = Repo(self)
        r.config("e2e: echo 'node_modules missing'; exit 77\n")
        r.commit_all("config")
        r.write("junk.txt", "dirty\n")
        sha = self.head(r)

        res = self.push_of(r, "refs/heads/main", sha)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"could not verify {sha[:7]}", res.stderr)

    def test_it_works_with_the_git_environment_a_hook_inherits(self):
        r = Repo(self)
        r.config("clean: test ! -e junk.txt\n")
        r.commit_all("config")
        r.write("junk.txt", "dirty\n")

        res = self.push_of(r, "refs/heads/main", self.head(r),
                           env={"GIT_DIR": os.path.join(r.root, ".git")})

        self.assertEqual(res.returncode, 0, res.stderr)

    def test_the_pass_cache_is_shared_between_the_checkout_and_the_tree(self):
        r = Repo(self)
        r.config(f"lint: {r.counted()}\n")
        r.commit_all("config")
        r.write("junk.txt", "dirty\n")
        self.push_of(r, "refs/heads/main", self.head(r))
        r.git("clean", "-fdq")

        res = self.push_of(r, "refs/heads/main", self.head(r))

        self.assertEqual(r.runs(), 1)
        self.assertIn("cached", res.stderr)


class RepoHook(unittest.TestCase):
    def setUp(self):
        r = self.r = Repo(self)
        os.symlink(TURNSTILE, os.path.join(r.fakebin, "turnstile"))
        self.hook = os.path.join(r.root, ".git", "hooks", "pre-push")

    def push(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "push", *extra, "origin", "HEAD:refs/heads/topic"],
                              cwd=self.r.root, env=self.r.env, capture_output=True, text=True)

    def test_install_repo_writes_a_pre_push_hook_that_gates_a_real_push(self):
        r = self.r
        r.config("lint: false\n")
        r.commit_all("config")

        installed = r.turnstile("install", "--repo")
        refused = self.push()
        bypassed = self.push("--no-verify")

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertTrue(os.access(self.hook, os.X_OK))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("push refused", refused.stderr)
        self.assertEqual(bypassed.returncode, 0, bypassed.stderr)

    def test_a_passing_push_goes_through(self):
        r = self.r
        r.config("lint: true\n")
        r.commit_all("config")
        r.turnstile("install", "--repo")

        self.assertEqual(self.push().returncode, 0)

    def test_install_repo_touches_nothing_global(self):
        self.r.turnstile("install", "--repo")

        res = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"],
                             env=self.r.env, capture_output=True, text=True)

        self.assertEqual(res.stdout.strip(), "")

    def test_install_repo_is_idempotent_and_leaves_a_foreign_hook_alone(self):
        r = self.r
        self.assertEqual(r.turnstile("install", "--repo").returncode, 0)
        self.assertEqual(r.turnstile("install", "--repo").returncode, 0)
        os.unlink(self.hook)
        with open(self.hook, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")

        res = r.turnstile("install", "--repo")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("turnstile pre-push", res.stderr)
        with open(self.hook) as fh:
            self.assertIn("echo mine", fh.read())

    def test_uninstall_repo_removes_only_its_own_hook(self):
        r = self.r
        r.turnstile("install", "--repo")

        r.turnstile("uninstall", "--repo")

        self.assertFalse(os.path.exists(self.hook))

    def test_the_hook_says_so_instead_of_blocking_when_turnstile_is_not_installed(self):
        r = self.r
        r.config("lint: false\n")
        r.commit_all("config")
        r.turnstile("install", "--repo")
        bare = os.path.join(r.tmp, "bare-bin")
        os.makedirs(bare)
        os.symlink(shutil.which("git"), os.path.join(bare, "git"))
        # Some git builds resolve receive-pack via PATH rather than their
        # exec path; the bare PATH must only be missing turnstile, not git.
        receive = shutil.which("git-receive-pack")
        if receive:
            os.symlink(receive, os.path.join(bare, "git-receive-pack"))

        res = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/topic"], cwd=r.root,
                             env={**r.env, "PATH": bare}, capture_output=True, text=True)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("turnstile is not installed", res.stderr)

    def test_a_hook_manager_can_call_pre_push_with_git_stdin(self):
        r = self.r
        r.config("lint: false\n")
        r.commit_all("config")
        head = r.git("rev-parse", "HEAD").strip()
        remote = r.git("rev-parse", "origin/main").strip()

        res = r.turnstile("pre-push", "origin", stdin=f"refs/heads/main {head} refs/heads/main {remote}\n")

        self.assertEqual(res.returncode, 1)
        self.assertIn("push refused", res.stderr)

    def test_the_global_dispatcher_and_a_repo_hook_do_not_gate_twice(self):
        r = self.r
        r.config(f"lint: {r.counted()} && false\n")
        r.commit_all("config")
        head = r.git("rev-parse", "HEAD").strip()
        remote = r.git("rev-parse", "origin/main").strip()

        res = subprocess.run([TURNSTILE, "pre-push", "origin"], cwd=r.root, capture_output=True,
                             text=True, env={**r.env, "TURNSTILE_IN_pre_push": "1"},
                             input=f"refs/heads/main {head} refs/heads/main {remote}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(r.runs(), 0)


class OptionHandling(unittest.TestCase):
    def setUp(self):
        r = self.r = Repo(self)
        r.env["TURNSTILE_HOOKS_DIR"] = os.path.join(r.tmp, "global-hooks")

    def global_hooks_path(self) -> str:
        res = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"],
                             env=self.r.env, capture_output=True, text=True)
        return res.stdout.strip()

    def test_install_help_prints_usage_and_leaves_the_global_config_alone(self):
        res = self.r.turnstile("install", "--help")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage", res.stderr)
        self.assertEqual(self.global_hooks_path(), "")
        self.assertFalse(os.path.exists(self.r.env["TURNSTILE_HOOKS_DIR"]))

    def test_install_rejects_an_unknown_option_without_installing(self):
        res = self.r.turnstile("install", "--bogus")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unknown option for install: --bogus", res.stderr)
        self.assertEqual(self.global_hooks_path(), "")
        self.assertFalse(os.path.exists(self.r.env["TURNSTILE_HOOKS_DIR"]))

    def test_uninstall_help_prints_usage_and_leaves_the_global_config_alone(self):
        installed = self.r.turnstile("install")
        res = self.r.turnstile("uninstall", "-h")

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage", res.stderr)
        self.assertEqual(self.global_hooks_path(), self.r.env["TURNSTILE_HOOKS_DIR"])
        self.assertTrue(os.path.isdir(self.r.env["TURNSTILE_HOOKS_DIR"]))

    def test_uninstall_rejects_an_unknown_option_without_uninstalling(self):
        self.r.turnstile("install")
        res = self.r.turnstile("uninstall", "--bogus")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unknown option for uninstall: --bogus", res.stderr)
        self.assertEqual(self.global_hooks_path(), self.r.env["TURNSTILE_HOOKS_DIR"])
        self.assertTrue(os.path.isdir(self.r.env["TURNSTILE_HOOKS_DIR"]))

    def test_run_help_works_outside_a_repository(self):
        outside = os.path.join(self.r.tmp, "outside")
        os.makedirs(outside)

        res = subprocess.run([TURNSTILE, "run", "--help"], cwd=outside, env=self.r.env,
                             capture_output=True, text=True)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage", res.stderr)

    def test_run_help_works_in_a_repository_with_an_unapproved_config(self):
        self.r.config("lint: true\n", allow=False)

        res = self.r.turnstile("run", "--help")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage", res.stderr)

    def test_run_and_ci_print_usage_for_help(self):
        for command in ("run", "ci"):
            with self.subTest(command=command):
                res = self.r.turnstile(command, "--help")

                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("Usage", res.stderr)


class PushScope(unittest.TestCase):
    RANGE = {"TURNSTILE_RANGE": "origin/main..HEAD"}

    def files(self, r: Repo, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([TURNSTILE, "files", *args], cwd=r.root, env={**r.env, **(env or {})},
                              capture_output=True, text=True)

    def listed(self, r: Repo, *args: str, env: dict | None = None) -> list[str]:
        res = self.files(r, *args, env=self.RANGE if env is None else env)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.splitlines()

    def feature(self, r: Repo) -> None:
        r.git("switch", "-q", "-c", "feature")

    def land_on_main(self, r: Repo, rel: str) -> None:
        r.git("switch", "-q", "main")
        r.write(rel, "landed on main\n")
        r.commit_all("upstream work")
        r.git("push", "-q", "origin", "main", "--no-verify")
        r.git("switch", "-q", "feature")

    def test_files_of_every_pushed_commit_are_listed_once(self):
        r = Repo(self)
        self.feature(r)
        r.write("src/A.kt", "one\n")
        r.write("README.md", "one\n")
        r.commit_all("first")
        r.write("src/A.kt", "two\n")
        r.write("docs/b.md", "two\n")
        r.commit_all("second")

        names = self.listed(r)

        self.assertEqual(sorted(names), ["README.md", "docs/b.md", "src/A.kt"])

    def test_a_file_added_and_deleted_within_the_push_is_not_listed(self):
        r = Repo(self)
        self.feature(r)
        r.write("scratch.txt", "temporary\n")
        r.write("keep.txt", "kept\n")
        r.commit_all("first")
        os.remove(os.path.join(r.root, "scratch.txt"))
        r.commit_all("second")

        self.assertEqual(self.listed(r), ["keep.txt"])

    def test_a_non_ascii_path_is_printed_raw(self):
        r = Repo(self)
        self.feature(r)
        r.write("Kundenliste_Müller.csv", "name\n")
        r.commit_all("customers")

        self.assertEqual(self.listed(r), ["Kundenliste_Müller.csv"])

    def test_dash_zero_separates_names_with_nul(self):
        r = Repo(self)
        self.feature(r)
        r.write("Kundenliste_Müller.csv", "name\n")
        r.write("with space.txt", "x\n")
        r.commit_all("odd names")

        res = self.files(r, "-0", env=self.RANGE)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.endswith("\0"))
        self.assertEqual(sorted(res.stdout.split("\0")[:-1]),
                         ["Kundenliste_Müller.csv", "with space.txt"])

    def test_glob_filters_to_matching_files_at_any_depth(self):
        r = Repo(self)
        self.feature(r)
        r.write("src/main/A.kt", "x\n")
        r.write("B.kt", "x\n")
        r.write("README.md", "x\n")
        r.commit_all("kotlin")

        names = self.listed(r, "--glob", "**/*.kt")

        self.assertEqual(sorted(names), ["B.kt", "src/main/A.kt"])

    def test_glob_is_repeatable(self):
        r = Repo(self)
        self.feature(r)
        r.write("src/A.kt", "x\n")
        r.write("docs/a.md", "x\n")
        r.write("other.txt", "x\n")
        r.commit_all("mixed")

        names = self.listed(r, "--glob", "**/*.kt", "--glob", "**/*.md")

        self.assertEqual(sorted(names), ["docs/a.md", "src/A.kt"])

    def test_a_new_ref_whose_tip_is_already_on_the_default_branch_lists_nothing(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f"probe: {TURNSTILE} files > {seen}\n")
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        head = r.git("rev-parse", "HEAD").strip()

        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/copy {head} refs/heads/copy {'0' * 40}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read(), "")

    def test_a_range_from_the_empty_tree_lists_only_what_the_default_branch_lacks(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        names = self.listed(r, env={"TURNSTILE_RANGE": f"{empty_tree}..HEAD"})

        self.assertEqual(names, ["feature.txt"])

    def test_a_clean_merge_of_the_default_branch_contributes_no_files(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        self.land_on_main(r, "upstream.txt")
        r.git("merge", "-q", "origin/main", "-m", "merge main")

        self.assertEqual(self.listed(r), ["feature.txt"])

    def test_a_clean_merge_contributes_no_files_without_remerge_diff_too(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        self.land_on_main(r, "upstream.txt")
        r.git("merge", "-q", "origin/main", "-m", "merge main")

        names = self.listed(r, env={**self.RANGE, "TURNSTILE_FORCE_CC": "1"})

        self.assertEqual(names, ["feature.txt"])

    def test_a_file_only_the_merge_commit_adds_is_listed(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        self.land_on_main(r, "upstream.txt")
        r.git("merge", "-q", "--no-commit", "--no-ff", "origin/main")
        r.write("evil.txt", "slipped into the merge\n")
        r.commit_all("merge main")

        self.assertEqual(sorted(self.listed(r)), ["evil.txt", "feature.txt"])

    def test_worktree_mode_does_not_list_what_a_merged_in_default_branch_brought(self):
        r = Repo(self)
        fork = r.git("rev-parse", "origin/main").strip()
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        self.land_on_main(r, "upstream.txt")
        r.git("merge", "-q", "origin/main", "-m", "merge main")
        r.write("untracked.txt", "x\n")

        names = self.listed(r, env={"TURNSTILE_MODE": "worktree", "TURNSTILE_RANGE": f"{fork}..HEAD"})

        self.assertEqual(sorted(names), ["feature.txt", "untracked.txt"])

    def test_without_perl_it_exits_two_naming_it(self):
        r = Repo(self)
        bare = os.path.join(r.tmp, "bare-bin")
        os.makedirs(bare)
        os.symlink(shutil.which("dirname"), os.path.join(bare, "dirname"))

        res = subprocess.run([shutil.which("bash"), TURNSTILE, "files"], cwd=r.root, capture_output=True,
                             text=True, env={**r.env, **self.RANGE, "PATH": bare})

        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertIn("perl", res.stderr)

    def second_remote(self, r: Repo) -> None:
        upstream = os.path.join(r.tmp, "upstream.git")
        subprocess.run(["git", "init", "-q", "--bare", upstream], env=r.env, check=True)
        r.git("remote", "add", "upstream", upstream)

    def test_the_default_branch_of_the_pushed_to_remote_is_excluded(self):
        r = Repo(self)
        self.second_remote(r)
        r.write("only-upstream.txt", "x\n")
        r.commit_all("held by upstream only")
        r.git("push", "-q", "upstream", "main", "--no-verify")
        r.git("reset", "-q", "--keep", "origin/main")
        r.git("switch", "-q", "-c", "feature", "upstream/main")
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        env = {"TURNSTILE_RANGE": "origin/main..HEAD"}

        to_origin = self.listed(r, env=env)
        to_upstream = self.listed(r, env={**env, "TURNSTILE_REMOTE": "upstream"})

        self.assertEqual(sorted(to_origin), ["feature.txt", "only-upstream.txt"])
        self.assertEqual(to_upstream, ["feature.txt"])

    def test_pre_push_tells_checks_which_remote_it_is_pushing_to(self):
        r = Repo(self)
        self.second_remote(r)
        seen = os.path.join(r.tmp, "seen")
        r.config(f'probe: printf "%s" "$TURNSTILE_REMOTE" > {seen}\n')
        r.commit_all("config")
        head = r.git("rev-parse", "HEAD").strip()

        res = r.turnstile("__pre-push", "upstream",
                          stdin=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(fh.read(), "upstream")

    def test_a_local_default_branch_excludes_its_commits_when_the_remote_has_none(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        r.git("update-ref", "-d", "refs/remotes/origin/main")
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        names = self.listed(r, env={"TURNSTILE_RANGE": f"{empty_tree}..HEAD"})

        self.assertEqual(names, ["feature.txt"])

    def test_a_three_dot_range_starts_at_the_merge_base(self):
        r = Repo(self)
        self.feature(r)
        r.write("feature.txt", "x\n")
        r.commit_all("feature")
        self.land_on_main(r, "upstream.txt")
        for mode in ("push", "worktree"):
            with self.subTest(mode=mode):
                names = self.listed(r, env={"TURNSTILE_MODE": mode, "TURNSTILE_RANGE": "main...HEAD"})

                self.assertEqual(names, ["feature.txt"])

    def test_a_bare_range_means_push_mode_and_ignores_the_working_tree(self):
        r = Repo(self)
        self.feature(r)
        r.write("committed.txt", "x\n")
        r.commit_all("committed")
        r.write("committed.txt", "edited\n")
        r.write("untracked.txt", "x\n")

        self.assertEqual(self.listed(r, env=self.RANGE), ["committed.txt"])

    def test_worktree_mode_lists_committed_uncommitted_and_untracked_files(self):
        r = Repo(self)
        seen = os.path.join(r.tmp, "seen")
        r.config(f"probe: {TURNSTILE} files > {seen}\n")
        r.write("committed.txt", "x\n")
        r.commit_all("committed")
        r.write("a.txt", "edited\n")
        r.write("untracked.txt", "x\n")

        res = r.turnstile("run", "--no-ai")

        self.assertEqual(res.returncode, 0, res.stderr)
        with open(seen) as fh:
            self.assertEqual(sorted(fh.read().split()),
                             [".turnstile", "a.txt", "committed.txt", "untracked.txt"])

    def test_worktree_is_the_default_mode_outside_a_run(self):
        r = Repo(self)
        r.write("committed.txt", "x\n")
        r.commit_all("committed")
        r.write("untracked.txt", "x\n")

        res = self.files(r, env={})

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(res.stdout.split()), ["committed.txt", "untracked.txt"])

    def test_worktree_mode_does_not_list_a_file_deleted_from_disk(self):
        r = Repo(self)
        os.remove(os.path.join(r.root, "a.txt"))
        r.write("b.txt", "x\n")

        res = self.files(r, env={"TURNSTILE_MODE": "worktree"})

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["b.txt"])

    def test_a_range_naming_an_unknown_revision_exits_two(self):
        r = Repo(self)
        for rng in ("bogus..HEAD", "origin/main..bogus"):
            with self.subTest(range=rng):
                res = self.files(r, env={"TURNSTILE_RANGE": rng})

                self.assertEqual(res.returncode, 2, res.stderr)
                self.assertEqual(res.stdout, "")

    def test_an_unknown_option_is_refused(self):
        res = self.files(Repo(self), "--bogus")

        self.assertEqual(res.returncode, 1)
        self.assertIn("unknown option for files: --bogus", res.stderr)

    def test_help_prints_usage(self):
        res = self.files(Repo(self), "--help")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("turnstile files", res.stderr)


class ScanMatch(unittest.TestCase):
    PATTERN = "forbid\x1fFORBIDDEN_[A-Z]+"
    LEAK = "FORBIDDEN_TOKEN"

    def scan(self, diff: str, patterns: str | None = None, **env: str) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "TURNSTILE_", "TS_"))}
        return subprocess.run([TURNSTILE, "__scan-match"], input=diff, capture_output=True, text=True,
                              env={**base, "TS_PATTERNS": patterns or self.PATTERN,
                                   "TS_CONTEXT": "abc1234", **env})

    def assertReports(self, res: subprocess.CompletedProcess, *lines: str) -> None:
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(res.stdout.splitlines(), [f"  x forbid: {line} (abc1234)" for line in lines])
        self.assertNotIn(self.LEAK, res.stdout + res.stderr)

    def test_an_added_line_is_reported_with_its_new_file_line_number(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/src/app.py b/src/app.py
            index 111..222 100644
            --- a/src/app.py
            +++ b/src/app.py
            @@ -10,2 +10,3 @@ def f():
             keep
            +safe = 1
            +leak = "{self.LEAK}"
            """))

        self.assertReports(res, "src/app.py:12")

    def test_removed_and_context_lines_are_not_reported(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/a.txt b/a.txt
            --- a/a.txt
            +++ b/a.txt
            @@ -1,3 +1,2 @@
             {self.LEAK}
            -{self.LEAK}
             tail
            """))

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_the_path_follows_each_file_and_the_line_number_each_hunk(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/one.txt b/one.txt
            --- a/one.txt
            +++ b/one.txt
            @@ -0,0 +1 @@
            +{self.LEAK}
            diff --git a/two.txt b/two.txt
            --- a/two.txt
            +++ b/two.txt
            @@ -4,0 +5,2 @@
            +fine
            +{self.LEAK}
            @@ -20 +22 @@
            -old
            +{self.LEAK}
            """))

        self.assertReports(res, "one.txt:1", "two.txt:6", "two.txt:22")

    def test_an_added_line_that_looks_like_a_file_header_stays_an_added_line(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/real.txt b/real.txt
            --- a/real.txt
            +++ b/real.txt
            @@ -0,0 +1,2 @@
            +first
            +++ b/fake.txt {self.LEAK}
            """))

        self.assertReports(res, "real.txt:2")

    def test_a_renamed_file_is_reported_under_its_new_path(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/old.txt b/new.txt
            similarity index 90%
            rename from old.txt
            rename to new.txt
            --- a/old.txt
            +++ b/new.txt
            @@ -1 +1,2 @@
             x
            +{self.LEAK}
            """))

        self.assertReports(res, "new.txt:2")

    def test_a_path_with_a_trailing_tab_and_spaces_is_reported_whole(self):
        res = self.scan(f"diff --git a/we ird.txt b/we ird.txt\n--- a/we ird.txt\t\n"
                        f"+++ b/we ird.txt\t\n@@ -0,0 +1 @@\n+{self.LEAK}\n")

        self.assertReports(res, "we ird.txt:1")

    def test_every_matching_pattern_is_reported_for_a_line(self):
        res = self.scan(f"+++ b/a.txt\n@@ -0,0 +1 @@\n+{self.LEAK} and more\n",
                        patterns="first\x1fFORBIDDEN\x1esecond\x1fmore$")

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(res.stdout.splitlines(),
                         ["  x first: a.txt:1 (abc1234)", "  x second: a.txt:1 (abc1234)"])

    def test_a_diff_with_no_match_exits_zero_silently(self):
        res = self.scan("+++ b/a.txt\n@@ -0,0 +1 @@\n+fine\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_a_pattern_that_does_not_compile_exits_two_naming_it(self):
        res = self.scan("+++ b/a.txt\n@@ -0,0 +1 @@\n+fine\n", patterns="broken\x1f(")

        self.assertEqual(res.returncode, 2)
        self.assertIn("bad pattern broken", res.stderr)

    def test_plain_input_is_every_line_of_the_named_file(self):
        res = self.scan(f"fine\n{self.LEAK}\n", TS_PLAIN="1", TS_PATH="notes.txt")

        self.assertReports(res, "notes.txt:2")

    def test_only_lines_every_parent_lacks_count_as_added_by_a_merge(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --cc app.py
            index 111,222..333
            --- a/app.py
            +++ b/app.py
            @@@ -1,2 -1,2 +1,4 @@@
              ctx
             +from one parent {self.LEAK}
            + from the other parent {self.LEAK}
            ++added by the merge {self.LEAK}
             -gone
            """))

        self.assertReports(res, "app.py:4")

    def test_a_combined_diff_without_file_headers_takes_the_path_from_diff_cc(self):
        res = self.scan(f"diff --cc dir/app.py\nindex 111,222..333\n@@@ -1 -1 +1 @@@\n++{self.LEAK}\n")

        self.assertReports(res, "dir/app.py:1")

    def test_three_parent_combined_lines_need_all_three_columns_added(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --combined app.py
            --- a/app.py
            +++ b/app.py
            @@@@ -1 -1 -1 +1,2 @@@@
            ++ two of three {self.LEAK}
            +++all three {self.LEAK}
            """))

        self.assertReports(res, "app.py:2")


    def test_a_quoted_path_with_an_escaped_tab_is_reported_unescaped(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git "a/tab\\tname.txt" "b/tab\\tname.txt"
            --- "a/tab\\tname.txt"
            +++ "b/tab\\tname.txt"
            @@ -0,0 +1 @@
            +{self.LEAK}
            """))

        self.assertReports(res, "tab\tname.txt:1")

    def test_a_quoted_header_is_never_attributed_to_the_previous_file(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/one.txt b/one.txt
            --- a/one.txt
            +++ b/one.txt
            @@ -0,0 +1 @@
            +fine
            diff --git "a/q\\"x.txt" "b/q\\"x.txt"
            new file mode 100644
            @@ -0,0 +1 @@
            +{self.LEAK}
            """))

        self.assertReports(res, 'q"x.txt:1')

    def test_a_file_whose_path_is_unknown_is_not_given_the_previous_files_name(self):
        res = self.scan(textwrap.dedent(f"""\
            diff --git a/one.txt b/one.txt
            --- a/one.txt
            +++ b/one.txt
            @@ -0,0 +1 @@
            +fine
            diff --git a/old name.txt b/new name.txt
            @@ -0,0 +1 @@
            +{self.LEAK}
            """))

        self.assertReports(res, "(unknown):1")


class ScanWorktree(unittest.TestCase):
    def scan(self, r: Repo, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([TURNSTILE, "__scan-worktree"], cwd=r.root, capture_output=True, text=True,
                              env={**r.env, "TS_PATTERNS": "forbid\x1fFORBIDDEN_[A-Z]+", **env})

    def dirty(self, r: Repo) -> None:
        r.write("a.txt", "a\nFORBIDDEN_TOKEN\n")
        r.write("fresh.txt", "fine\nFORBIDDEN_TOKEN\n")

    def test_uncommitted_and_untracked_additions_are_scanned_in_worktree_mode(self):
        r = Repo(self)
        self.dirty(r)

        res = self.scan(r)

        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(sorted(res.stdout.splitlines()),
                         ["  x forbid: a.txt:2 (uncommitted)", "  x forbid: fresh.txt:2 (untracked)"])
        self.assertNotIn("FORBIDDEN_TOKEN", res.stdout + res.stderr)

    def test_the_working_tree_is_not_scanned_in_push_mode(self):
        r = Repo(self)
        self.dirty(r)

        res = self.scan(r, TURNSTILE_RANGE="origin/main..HEAD")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_ignored_files_are_not_scanned(self):
        r = Repo(self)
        r.write(".gitignore", "ignored.txt\n")
        r.commit_all("ignore")
        r.write("ignored.txt", "FORBIDDEN_TOKEN\n")

        res = self.scan(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")


class MissingConfigWarning(unittest.TestCase):
    WARNING = "tracks a .turnstile this push does not carry - its checks did not run"

    def tracked_config(self) -> Repo:
        r = Repo(self)
        r.config("lint: false\n")
        r.commit_all("config")
        r.git("push", "-q", "origin", "main", "--no-verify")
        return r

    def orphan(self, r: Repo, name: str) -> str:
        r.git("switch", "-q", "--orphan", name)
        r.write(f"{name}.txt", "no gate here\n")
        r.commit_all(name)
        return r.git("rev-parse", "HEAD").strip()

    def test_warns_when_the_default_branch_tracks_a_config_the_pushed_commit_lacks(self):
        r = self.tracked_config()
        head = self.orphan(r, "bare")

        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/bare {head} refs/heads/bare {'0' * 40}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"origin/main {self.WARNING}", res.stderr)

    def test_stays_quiet_when_no_default_branch_tracks_a_config(self):
        r = Repo(self)
        head = self.orphan(r, "bare")

        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/bare {head} refs/heads/bare {'0' * 40}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(self.WARNING, res.stderr)

    def test_consults_the_remote_being_pushed_to(self):
        r = self.tracked_config()
        other = os.path.join(r.tmp, "other.git")
        subprocess.run(["git", "init", "-q", "--bare", other], env=r.env, check=True)
        r.git("remote", "add", "mirror", other)
        r.git("push", "-q", "mirror", "main", "--no-verify")
        r.git("update-ref", "-d", "refs/remotes/origin/main")
        head = self.orphan(r, "bare")

        to_origin = r.turnstile("__pre-push", "origin",
                                stdin=f"refs/heads/bare {head} refs/heads/bare {'0' * 40}\n")
        to_mirror = r.turnstile("__pre-push", "mirror",
                                stdin=f"refs/heads/bare {head} refs/heads/bare {'0' * 40}\n")

        self.assertNotIn(self.WARNING, to_origin.stderr)
        self.assertIn(f"mirror/main {self.WARNING}", to_mirror.stderr)

    def test_warns_once_for_a_push_of_several_refs(self):
        r = self.tracked_config()
        first = self.orphan(r, "bare-one")
        second = self.orphan(r, "bare-two")
        zero = "0" * 40

        res = r.turnstile("__pre-push", "origin",
                          stdin=f"refs/heads/bare-one {first} refs/heads/bare-one {zero}\n"
                                f"refs/heads/bare-two {second} refs/heads/bare-two {zero}\n")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr.count(self.WARNING), 1, res.stderr)


class FailureSummary(unittest.TestCase):
    CONFIG = """
        lint: echo 'src/a.go:3: undefined: x'; echo 'make: *** [lint] Error 1'; exit 2
        ok: true
        unit: echo 'FAIL TestWidget'; exit 1
        e2e: echo 'no docker'; exit 77
    """

    def test_run_opens_its_failure_report_with_one_line_per_failing_check(self):
        r = Repo(self)
        r.config(self.CONFIG)

        res = r.turnstile("run", "--no-ai")

        lines = res.stderr.splitlines()
        first_detail = next(i for i, line in enumerate(lines) if line.strip().startswith("$ "))
        head = "\n".join(lines[:first_detail])
        self.assertIn("lint: exit 2, src/a.go:3: undefined: x", head)
        self.assertIn("unit: exit 1, FAIL TestWidget", head)
        self.assertNotIn("e2e: exit", res.stderr)
        self.assertNotIn("ok: exit", res.stderr)

    def test_a_green_run_has_no_failure_report(self):
        r = Repo(self)
        r.config("ok: true\n")

        res = r.turnstile("run", "--no-ai")

        self.assertNotIn("failing", res.stderr)

    def test_a_check_that_rewrites_the_tree_is_named_in_the_summary(self):
        r = Repo(self)
        r.config("fmt: echo formatted > lib/b.txt\n")

        res = r.turnstile("run", "--no-ai")

        self.assertIn("tree: modified while fmt ran", res.stderr.split("$ ")[0])

    def test_the_summary_names_rewritten_non_ascii_files_as_they_are_on_disk(self):
        r = Repo(self)
        r.write("lib/Kundenliste_M\u00fcller.csv", "x\n")
        r.commit_all("umlaut")
        r.config("fmt: echo formatted > lib/Kundenliste_M\u00fcller.csv\n")

        res = r.turnstile("run", "--no-ai")

        self.assertIn("lib/Kundenliste_M\u00fcller.csv", res.stderr.split("$ ")[0])

    def test_the_stop_hook_reason_starts_with_the_summary_even_when_the_report_is_cut(self):
        r = Repo(self)
        r.config("lint: seq 1 3000; echo 'the real problem'; exit 2\nunit: echo boom; exit 1\n")
        payload = {"session_id": "s1", "cwd": r.root, "hook_event_name": "Stop"}

        res = subprocess.run([TURNSTILE, "hook", "claude-stop"], input=json.dumps(payload),
                             env=r.env, capture_output=True, text=True, cwd=r.root)

        reason = json.loads(res.stdout)["reason"]
        self.assertTrue(reason.startswith("lint: exit 2, the real problem\nunit: exit 1, boom\n"), reason[:200])


class StopHook(unittest.TestCase):
    def stop(self, r: Repo, active: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
        payload = {"session_id": "s1", "cwd": r.root, "hook_event_name": "Stop",
                   "stop_hook_active": active}
        # A loaded machine pushes real check time past the 10s default notice;
        # only tests that opt in via env should see the slow-run message.
        return subprocess.run([TURNSTILE, "hook", "claude-stop"], input=json.dumps(payload),
                              env={**r.env, "TURNSTILE_SLOW_NOTICE": "600", **(env or {})},
                              capture_output=True, text=True, cwd=r.root)

    def test_repo_without_turnstile_config_is_left_alone(self):
        r = Repo(self)

        res = self.stop(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")

    def test_failing_check_blocks_the_stop_with_the_reason(self):
        r = Repo(self)
        r.config("unit: echo 'TestWidget failed' && false\n")

        res = self.stop(r)

        decision = json.loads(res.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("TestWidget failed", decision["reason"])

    def test_passing_checks_let_the_agent_stop(self):
        r = Repo(self)
        r.config("unit: true\n")

        res = self.stop(r)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")

    def test_no_progress_since_the_last_block_lets_the_agent_stop(self):
        r = Repo(self)
        r.config("unit: false\n")
        self.stop(r)

        res = self.stop(r, active=True)

        self.assertNotIn('"block"', res.stdout)

    def test_progress_since_the_last_block_is_checked_again(self):
        r = Repo(self)
        r.config("unit: false\n")
        self.stop(r)

        r.write("a.txt", "attempted fix\n")
        res = self.stop(r, active=True)

        self.assertEqual(json.loads(res.stdout)["decision"], "block")

    def test_a_failing_push_only_check_does_not_block_the_stop(self):
        r = Repo(self)
        r.config("unit: true\npush e2e: false\n")

        res = self.stop(r)

        self.assertEqual(res.stdout.strip(), "")

    def test_ai_modules_do_not_run_on_stop(self):
        r = Repo(self)
        r.write(".turnstile.d/modules/m.md", "---\nname: m\n---\nReview it.\n")
        r.config("unit: true\nai m: block=high\n")
        r.write("a.txt", "x\n")
        r.commit_all()

        self.stop(r)

        self.assertEqual(r.prompts(), [])

    def test_malformed_payload_never_breaks_the_session(self):
        r = Repo(self)
        r.config("unit: false\n")

        res = r.turnstile("hook", "claude-stop", stdin="not json")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")

    def test_a_slow_uncached_run_says_why_the_stop_took_long(self):
        r = Repo(self)
        r.config("slow: sleep 1.2\n")

        res = self.stop(r, env={"TURNSTILE_SLOW_NOTICE": "1"})

        message = json.loads(res.stdout)["systemMessage"]
        self.assertIn("checks took", message)
        self.assertIn("later stops reuse", message)

    def test_a_fast_run_stays_silent(self):
        r = Repo(self)
        r.config("fast: true\n")

        res = self.stop(r, env={"TURNSTILE_SLOW_NOTICE": "30"})

        self.assertEqual(res.stdout.strip(), "")

    def test_unknown_hook_is_refused(self):
        r = Repo(self)

        res = r.turnstile("hook", "nope")

        self.assertNotEqual(res.returncode, 0)


FAKE_NIX = """#!/bin/sh
echo "$*" >> "$NIX_LOG"
[ -z "${NIX_FAILS:-}" ] || { echo "error: flake has no devShell" >&2; exit 1; }
[ "$1" = print-dev-env ] || exit 1
echo 'export DEVENV_TOOL=from-devenv'
[ -z "${FAKE_BASH:-}" ] || echo "BASH='$FAKE_BASH'"
"""


class StopInDevEnvironment(unittest.TestCase):
    def setUp(self):
        r = self.r = Repo(self)
        self.nix_log = os.path.join(r.tmp, "nix-log")
        self.seen = os.path.join(r.tmp, "seen")
        r.fake("nix", FAKE_NIX)
        os.symlink(TURNSTILE, os.path.join(r.fakebin, "turnstile"))
        r.write("flake.nix", "{}\n")
        r.write("flake.lock", "{}\n")
        r.config(f'probe: printf "%s" "${{DEVENV_TOOL:-bare}}" > {self.seen}\n')
        r.commit_all("flake")
        self.env = {**r.env, "NIX_LOG": self.nix_log, "CLAUDE_PROJECT_DIR": r.root}

    def stop(self, **extra: str) -> subprocess.CompletedProcess:
        with open(SETTINGS) as fh:
            command = json.load(fh)["hooks"]["Stop"][0]["hooks"][0]["command"]
        payload = {"session_id": "s1", "cwd": self.r.root, "hook_event_name": "Stop"}
        return subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload),
                              env={**self.env, "TURNSTILE_SLOW_NOTICE": "600", **extra},
                              capture_output=True, text=True, cwd=self.r.tmp)

    def nix_calls(self) -> list[str]:
        try:
            with open(self.nix_log) as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []

    def probed(self) -> str:
        with open(self.seen) as fh:
            return fh.read()

    def test_checks_run_inside_the_projects_dev_environment(self):
        res = self.stop()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.probed(), "from-devenv")
        self.assertEqual(len(self.nix_calls()), 1)
        self.assertTrue(self.nix_calls()[0].startswith("print-dev-env"))

    def test_the_dev_environment_is_cached_under_the_git_dir(self):
        self.stop()

        cached = os.listdir(os.path.join(self.r.root, ".git", "turnstile"))
        self.assertEqual(len(cached), 1, cached)

    def test_a_second_stop_does_not_ask_nix_again(self):
        self.stop()
        self.r.write("a.txt", "edited\n")

        res = self.stop()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.nix_calls()), 1)
        self.assertEqual(self.probed(), "from-devenv")

    def test_editing_flake_lock_rebuilds_the_dev_environment(self):
        self.stop()
        self.r.write("flake.lock", '{"version": 7}\n')

        self.stop()

        self.assertEqual(len(self.nix_calls()), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.r.root, ".git", "turnstile"))), 1)

    def test_already_inside_a_nix_shell_nix_is_not_asked(self):
        res = self.stop(IN_NIX_SHELL="impure")

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.nix_calls(), [])
        self.assertEqual(self.probed(), "bare")

    def test_already_inside_direnv_nix_is_not_asked(self):
        self.stop(DIRENV_DIR="-/some/project")

        self.assertEqual(self.nix_calls(), [])
        self.assertEqual(self.probed(), "bare")

    def test_a_project_without_a_flake_runs_bare(self):
        self.r.git("rm", "-q", "flake.nix", "flake.lock")
        self.r.commit_all("no flake")

        self.stop()

        self.assertEqual(self.nix_calls(), [])
        self.assertEqual(self.probed(), "bare")

    def test_a_repo_without_turnstile_config_never_builds_an_environment(self):
        self.r.git("rm", "-q", ".turnstile")
        self.r.commit_all("ungated")

        res = self.stop()

        self.assertEqual(self.nix_calls(), [])
        self.assertEqual(res.stdout.strip(), "")

    def test_without_nix_the_checks_run_bare(self):
        os.unlink(os.path.join(self.r.fakebin, "nix"))

        res = self.stop()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.probed(), "bare")

    def test_a_failed_environment_build_is_reported_and_the_checks_still_run(self):
        res = self.stop(NIX_FAILS="1")

        message = json.loads(res.stdout)["systemMessage"]
        self.assertIn("could not build the dev environment", message)
        self.assertIn("flake has no devShell", message)
        self.assertEqual(self.probed(), "bare")
        self.assertEqual(os.listdir(os.path.join(self.r.root, ".git", "turnstile")), [])

    def test_the_profile_is_sourced_with_the_bash_it_names(self):
        bash_log = os.path.join(self.r.tmp, "bash-log")
        self.r.fake("devenv-bash", f'#!/bin/sh\necho used >> {bash_log}\nexec /bin/bash "$@"\n')

        self.stop(FAKE_BASH=os.path.join(self.r.fakebin, "devenv-bash"))

        self.assertTrue(os.path.exists(bash_log))
        self.assertEqual(self.probed(), "from-devenv")

    def test_building_the_environment_is_announced_once(self):
        first = self.stop()
        self.r.write("a.txt", "edited\n")
        second = self.stop()

        self.assertIn("built the dev environment (first run", json.loads(first.stdout)["systemMessage"])
        self.assertEqual(second.stdout.strip(), "")

    def test_the_announcement_rides_along_with_a_block(self):
        self.r.config("unit: false\n")

        out = json.loads(self.stop().stdout)

        self.assertEqual(out["decision"], "block")
        self.assertIn("built the dev environment", out["systemMessage"])

    def test_a_red_check_still_blocks_with_the_payload_read_through_the_environment(self):
        self.r.config("unit: false\n")

        res = self.stop()

        self.assertEqual(json.loads(res.stdout)["decision"], "block")


class ClaudeSettingsBlock(unittest.TestCase):
    def setUp(self):
        with open(SETTINGS) as fh:
            self.command = json.load(fh)["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.tmp = tempfile.mkdtemp(prefix="turnstile-shim-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)

    def fake(self, name: str) -> None:
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(f'#!/bin/sh\necho "{name} $*"\n')
        os.chmod(path, 0o755)

    def run_hook(self) -> subprocess.CompletedProcess:
        env = {"PATH": self.bin, "CLAUDE_PROJECT_DIR": self.project}
        return subprocess.run(["/bin/sh", "-c", self.command], input="{}", env=env,
                              capture_output=True, text=True, cwd=self.tmp)

    def test_print_claude_settings_prints_the_committed_block(self):
        res = subprocess.run([TURNSTILE, "print-claude-settings"], capture_output=True, text=True)

        with open(SETTINGS) as fh:
            self.assertEqual(res.stdout, fh.read())

    def test_readme_shows_the_committed_block(self):
        with open(SETTINGS) as fh, open(os.path.join(HOME, "README.md")) as readme:
            self.assertIn(fh.read(), readme.read())

    def test_the_spinner_says_what_a_slow_first_run_is_doing(self):
        with open(SETTINGS) as fh:
            hook = json.load(fh)["hooks"]["Stop"][0]["hooks"][0]

        self.assertIn("dev environment", hook["statusMessage"])

    def test_installed_turnstile_is_used_first(self):
        self.fake("turnstile")
        self.fake("nix")
        open(os.path.join(self.project, "flake.nix"), "w").close()

        res = self.run_hook()

        self.assertEqual(res.stdout.strip(), "turnstile hook claude-stop")

    def test_a_flake_without_turnstile_on_path_goes_through_nix_develop(self):
        self.fake("nix")
        open(os.path.join(self.project, "flake.nix"), "w").close()

        res = self.run_hook()

        self.assertEqual(res.stdout.strip(), "nix develop -c turnstile hook claude-stop")

    def test_without_turnstile_or_a_flake_it_says_so_and_lets_the_agent_stop(self):
        self.fake("nix")

        res = self.run_hook()

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("turnstile is not installed", json.loads(res.stdout)["systemMessage"])


def all_test_names(suite: unittest.TestSuite) -> list[str]:
    names = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            names.extend(all_test_names(item))
        else:
            names.append(item.id().removeprefix("__main__."))
    return names


def run_in_parallel() -> int:
    names = all_test_names(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    jobs = int(os.environ.get("TURNSTILE_TEST_JOBS") or min(8, os.cpu_count() or 1))

    def run(name: str) -> tuple[str, subprocess.CompletedProcess]:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), name],
                              capture_output=True, text=True)
        return name, proc

    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, proc in pool.map(run, names):
            if proc.returncode != 0:
                failed.append(name)
                print(f"FAILED {name}\n{proc.stderr}", file=sys.stderr)
    print(f"Ran {len(names)} tests, {len(failed)} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        unittest.main()
    sys.exit(run_in_parallel())
