"""End-to-end tests: real temp repos, the real scripts, a fake `claude` on PATH."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURNSTILE = os.path.join(HOME, "bin", "turnstile")
STOP_HOOK = os.path.join(HOME, "claude", "verify-on-stop.py")

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

        fakebin = os.path.join(self.tmp, "bin")
        os.makedirs(fakebin)
        claude = os.path.join(fakebin, "claude")
        with open(claude, "w") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(claude, 0o755)

        gitconfig = os.path.join(self.tmp, "gitconfig")
        with open(gitconfig, "w") as fh:
            fh.write("[user]\n\tname = t\n\temail = turnstile-test\n[init]\n\tdefaultBranch = main\n")

        # Under a git hook (this suite runs in turnstile's own pre-push gate)
        # GIT_DIR and friends point at the real repo, and every git call below
        # would land there.
        inherited = {k: v for k, v in os.environ.items()
                     if not k.startswith(("GIT_", "TURNSTILE_"))}
        self.env = {
            **inherited,
            "PATH": f"{fakebin}:{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": gitconfig,
            "GIT_CONFIG_NOSYSTEM": "1",
            "TURNSTILE_CACHE": os.path.join(self.tmp, "cache-ai"),
            "TURNSTILE_CHECK_CACHE": os.path.join(self.tmp, "cache-checks"),
            "TURNSTILE_STATE": os.path.join(self.tmp, "state"),
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

    def write(self, rel: str, content: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def config(self, text: str) -> None:
        self.write(".turnstile", textwrap.dedent(text))

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


class StopHook(unittest.TestCase):
    def stop(self, r: Repo, active: bool = False) -> subprocess.CompletedProcess:
        payload = {"session_id": "s1", "cwd": r.root, "hook_event_name": "Stop",
                   "stop_hook_active": active}
        return subprocess.run([STOP_HOOK], input=json.dumps(payload), env=r.env,
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


if __name__ == "__main__":
    unittest.main()
