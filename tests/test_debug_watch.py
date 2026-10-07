import json
import os
import pathlib
import sys
import time
import unittest

from tests.helpers import PLANKA_DIR, RULES, Env, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import debug_watch  # noqa: E402

# Корпус поля error события PostToolUseFailure на Bash; источник — поле source каждой строки.
CORPUS = pathlib.Path(__file__).parent / "fixtures" / "bash-failure-errors.jsonl"


def corpus():
    with CORPUS.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

DEBUGGING = "# Лестница отладки\n\nЧитай, когда фикс не удался дважды.\n\n- Каталог: {RULES}.\n"


class DebugWatchTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(rules=dict(RULES, debugging=DEBUGGING))

    def tearDown(self):
        self.env.close()

    def failure(self, command="make test", prompt_id="p-1", error="Exit code 1\nboom", **extra):
        fields = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "t",
                  "error": error, "is_interrupt": False, "prompt_id": prompt_id}
        fields.update(extra)
        return self.env.run("debug_watch.py", self.env.hook_input("PostToolUseFailure", **fields))

    def succeed(self, command="make test", prompt_id="p-1"):
        fields = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "t",
                  "tool_response": {"stdout": "", "stderr": "", "interrupted": False, "isImage": False},
                  "prompt_id": prompt_id}
        return self.env.run("debug_watch.py", self.env.hook_input("PostToolUse", **fields))

    def context(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = output(r)
        if out is None:
            return None
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        return out["hookSpecificOutput"]["additionalContext"]

    def state_file(self):
        return self.env.data / "state" / "sess-1.debug.json"

    def test_first_failure_silent(self):
        r = self.failure()
        self.assertIsNone(self.context(r))
        self.assertEqual(messages(r), [])

    def test_second_failure_emits_module(self):
        self.failure()
        ctx = self.context(self.failure())
        rules = self.env.root / "rules"
        self.assertTrue(ctx.startswith(
            f"Команда `make test` упала второй раз подряд — дальше по модулю {rules}/debugging.md.\n\n"))
        self.assertIn("# Лестница отладки", ctx)
        self.assertIn(f"Каталог: {rules}.", ctx)
        self.assertNotIn("{RULES}", ctx)

    def test_whitespace_normalized(self):
        self.failure("  make   test ")
        ctx = self.context(self.failure("make\ttest"))
        self.assertIn("`make test`", ctx)

    def test_success_between_failures_resets(self):
        self.failure()
        self.assertIsNone(self.context(self.succeed()))
        self.assertIsNone(self.context(self.failure()))

    def test_other_command_success_does_not_reset(self):
        self.failure()
        self.succeed("ls")
        self.assertIsNotNone(self.context(self.failure()))

    def test_distinct_commands_distinct_counters(self):
        self.failure("make test")
        self.assertIsNone(self.context(self.failure("make lint")))
        self.assertIsNotNone(self.context(self.failure("make lint")))

    def test_once_per_prompt(self):
        self.failure()
        self.assertIsNotNone(self.context(self.failure()))
        self.assertIsNone(self.context(self.failure()))
        self.failure("make lint")
        self.assertIsNone(self.context(self.failure("make lint")))
        ctx = self.context(self.failure(prompt_id="p-2"))
        self.assertIn("упала 4-й раз подряд", ctx)

    def test_interrupt_not_counted(self):
        self.failure()
        self.assertIsNone(self.context(self.failure(is_interrupt=True)))
        self.assertIsNotNone(self.context(self.failure()))

    def test_barrier(self):
        self.failure()
        hook_input = self.env.hook_input("PostToolUseFailure", tool_name="Bash",
                                         tool_input={"command": "make test"}, is_interrupt=False)
        r = self.env.run("debug_watch.py", hook_input, PLANKA_JUDGE="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_not_bash_silent(self):
        for _ in range(2):
            r = self.env.run("debug_watch.py", self.env.hook_input(
                "PostToolUseFailure", tool_name="Edit", tool_input={"file_path": "/x"}, error="e"))
            self.assertEqual(r.stdout, "")
        self.assertFalse(self.state_file().exists())

    def test_corrupt_state_starts_over(self):
        self.failure()
        self.state_file().write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.context(self.failure()))
        self.assertIsNotNone(self.context(self.failure()))

    def test_wrong_shape_state_starts_over(self):
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"counts": {"x": "y"}, "shown_prompt": 5}), encoding="utf-8")
        self.assertIsNone(self.context(self.failure()))
        self.assertIsNotNone(self.context(self.failure()))

    def test_missing_module_warns_and_retries(self):
        (self.env.root / "rules" / "debugging.md").unlink()
        self.failure()
        r = self.failure()
        self.assertIsNone(self.context(r))
        self.assertIn("planka: нет модуля правил", "\n".join(messages(r)))
        (self.env.root / "rules" / "debugging.md").write_text(DEBUGGING, encoding="utf-8")
        self.assertIsNotNone(self.context(self.failure()))

    def test_unwritable_state_warns(self):
        (self.env.data / "state").write_text("", encoding="utf-8")
        r = self.failure()
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))

    def test_garbage_stdin(self):
        r = self.env.run("debug_watch.py", "not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_missing_command_silent(self):
        r = self.env.run("debug_watch.py", self.env.hook_input(
            "PostToolUseFailure", tool_name="Bash", tool_input={}, is_interrupt=False))
        self.assertEqual(r.stdout, "")

    def test_corpus(self):
        for sample in corpus():
            with self.subTest(command=sample["command"]):
                self.assertIsNone(self.context(self.failure(sample["command"], error=sample["error"])))
                ctx = self.context(self.failure(sample["command"], prompt_id=sample["command"],
                                                error=sample["error"]))
                if sample["code1_is_answer"]:
                    self.assertIsNone(ctx)
                else:
                    self.assertIsNotNone(ctx)

    def test_grep_no_match_across_prompts_silent(self):
        for prompt_id in ("p-1", "p-2"):
            self.assertIsNone(self.context(self.failure("sudo grep -rn old_name src/", prompt_id=prompt_id,
                                                        error="Exit code 1")))

    def test_answer_code_resets_counter(self):
        self.failure("grep -rn x src/", error="Exit code 2\ngrep: src/: No such file or directory")
        self.assertIsNone(self.context(self.failure("grep -rn x src/", error="Exit code 1")))
        self.assertIsNone(self.context(self.failure(
            "grep -rn x src/", error="Exit code 2\ngrep: src/: No such file or directory")))

    def test_stale_state_pruned(self):
        state = self.env.data / "state"
        state.mkdir()
        stale = state / "old.debug.json"
        stale.write_text("{}", encoding="utf-8")
        old = time.time() - 8 * 86400
        os.utime(stale, (old, old))
        self.failure()
        self.assertFalse(stale.exists())
        self.assertTrue(self.state_file().exists())


class ExitCodeTest(unittest.TestCase):
    def test_corpus(self):
        expected = {"spawn /bin/bash ENOENT": None}
        for sample in corpus():
            with self.subTest(error=sample["error"]):
                first = sample["error"].split("\n", 1)[0]
                want = expected.get(sample["error"], int(first.removeprefix("Exit code ")) if
                                    first.startswith("Exit code ") else None)
                self.assertEqual(debug_watch.exit_code(sample["error"]), want)

    def test_shapes(self):
        self.assertEqual(debug_watch.exit_code("Exit code 1"), 1)
        self.assertEqual(debug_watch.exit_code("Exit code 127\nbash: x: command not found"), 127)
        self.assertIsNone(debug_watch.exit_code("boom\nExit code 1"))
        self.assertIsNone(debug_watch.exit_code("Exit code 1x"))
        self.assertIsNone(debug_watch.exit_code(None))


class Code1IsAnswerTest(unittest.TestCase):
    def test_corpus(self):
        for sample in corpus():
            with self.subTest(command=sample["command"]):
                if sample["error"].startswith("Exit code 1\n") or sample["error"] == "Exit code 1":
                    self.assertEqual(debug_watch.code1_is_answer(sample["command"]), sample["code1_is_answer"])

    def test_answers(self):
        for command in ("grep -E 'a|b' f.txt", "make test | grep FAIL", "cd src && grep -q x f",
                        "make; rg x", "git -C sub diff --quiet", "git --no-pager diff --exit-code -- a",
                        "git diff --no-index a b", "git grep -n x", "sudo -u root grep x f",
                        "env -u FOO LC_ALL=C grep x f", "/usr/bin/grep x f", "nice -n 5 diff a b",
                        "time test -f x", "[ -f x ]", "[[ -f x ]]", "\"grep\" x f", "! make test",
                        "command -V x", "nohup pgrep -f x", "grep x f  # поиск", "timeout -s KILL 5 cmp a b",
                        "x=1 y=2 which x", "grep x f\n", "grep 'a;b' f", "echo \"a|b\" | grep x",
                        "grep x f 2>&1", "grep a\\|b f", "grep x f # it's",
                        "grep -q x f && echo found", "test -f x && printf ok && true && :",
                        "x=$(grep a b)", "x=\"$(git diff --quiet)\"", "x=$(make; grep a b)",
                        "grep x f && echo \"a > b\"", "grep x f && echo 'a<b'", "grep x f && echo a\\>b",
                        "grep x f;#c", "make;grep x f;#make",
                        "(cd d && grep x f)", "(make; grep x f)"):
            with self.subTest(command=command):
                self.assertTrue(debug_watch.code1_is_answer(command))

    def test_failures(self):
        for command in ("make test", "grep x f && make test", "grep x f | sort -c", "git diff",
                        "git diff-tree HEAD", "find . -name x", "make test # grep", "grep 'x",
                        "command make test", "xargs grep x", "bash -c 'grep x f'", "(grep x f)",
                        "git", "env", "timeout 5", "sudo -u", "", "grep x f; make test",
                        "make test;", "make & grep x", "make test && echo ok", "grep x f || echo no",
                        "grep x f; echo ok", "grep x f | echo ok", "x=$(make test)", "x=$(grep a b) make",
                        "x=$(grep a b)$(make)", "echo ok", "x=$(grep a b) && echo ok && make",
                        "grep x f && echo ok > /ro/f", "grep x f && echo ok>/ro/f", "grep x f && echo ok 2>&1",
                        "grep x f && echo ok < in", "make test;#grep", "grep a#b; make", "{ grep x f; }"):
            with self.subTest(command=command):
                self.assertFalse(debug_watch.code1_is_answer(command))


if __name__ == "__main__":
    unittest.main()
