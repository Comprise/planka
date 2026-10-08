import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

from tests.helpers import PLANKA_DIR, REPO, RULES, Env, assert_linear, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import debug_watch  # noqa: E402

# Корпус поля error события PostToolUseFailure на Bash; источник — поле source каждой строки.
CORPUS = pathlib.Path(__file__).parent / "fixtures" / "bash-failure-errors.jsonl"


def corpus():
    with CORPUS.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

DEBUGGING = "# Лестница отладки\n\nЧитайте, мой дорогой друг, когда фикс не удался дважды.\n\n- Каталог: {RULES}.\n"


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
            f"{common.ADDRESS},\n\nКоманда `make test` упала второй раз подряд — дальше, пожалуйста, по модулю "
            f"{rules}/debugging.md.\n\n"))
        self.assertIn("# Лестница отладки", ctx)
        self.assertIn(f"Каталог: {rules}.", ctx)
        self.assertNotIn("{RULES}", ctx)

    def test_real_module_address_once(self):
        # Настоящий debugging.md: обращение строки условия снято, остаётся одно — от context_output.
        real = (REPO / "plugin" / "rules" / "debugging.md").read_text(encoding="utf-8")
        self.env.close()
        self.env = Env(rules=dict(RULES, debugging=real))
        self.failure()
        ctx = self.context(self.failure())
        self.assertIn("# Лестница отладки", ctx)
        self.assertIn("Читайте, пожалуйста,", ctx)
        self.assertEqual(ctx.casefold().count(common.ADDRESS.casefold()), 1)

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

    def test_command_of_other_tool_ignored(self):
        # Вход не Bash с полем command: не неудача и не успех команды — счётчик Bash не меняется.
        self.failure()
        for event in ("PostToolUse", "PostToolUseFailure", "PostToolUseFailure"):
            r = self.env.run("debug_watch.py", self.env.hook_input(
                event, tool_name="Task", tool_input={"command": "make test"}, tool_use_id="t",
                error="Exit code 1\nboom", is_interrupt=False))
            self.assertEqual(r.stdout, "", event)
        self.assertIn("упала второй раз подряд", self.context(self.failure()))

    def test_other_event_ignored(self):
        # Событие вне PostToolUse и PostToolUseFailure (PreToolUse на Bash) — не исход команды: счётчик не
        # сбрасывается.
        self.failure()
        r = self.env.run("debug_watch.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "make test"}, tool_use_id="t"))
        self.assertEqual(r.stdout, "")
        self.assertIn("упала второй раз подряд", self.context(self.failure()))

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

    def test_subagents_count_separately(self):
        self.failure(agent_id="a1", agent_type="Explore")
        self.assertIsNone(self.context(self.failure(agent_id="a2", agent_type="Explore")))
        self.assertIsNotNone(self.context(self.failure(agent_id="a1", agent_type="Explore")))

    def test_main_agent_and_subagent_count_separately(self):
        self.failure()
        self.assertIsNone(self.context(self.failure(agent_id="a1")))
        self.assertIsNotNone(self.context(self.failure()))
        self.assertIsNotNone(self.context(self.failure(agent_id="a1", prompt_id="p-2")))

    def test_subagent_success_does_not_reset_other_agent(self):
        self.failure()
        self.env.run("debug_watch.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "make test"}, tool_use_id="t",
            tool_response={}, prompt_id="p-1", agent_id="a1"))
        self.assertIsNotNone(self.context(self.failure()))

    def test_shown_mark_per_agent(self):
        for agent in ("a1", "a2"):
            self.failure(agent_id=agent)
        self.assertIsNotNone(self.context(self.failure(agent_id="a1")))
        self.assertIsNotNone(self.context(self.failure(agent_id="a2")))
        self.assertIsNone(self.context(self.failure(agent_id="a1")))

    def test_non_string_agent_id_is_main_agent(self):
        self.failure()
        self.assertIsNotNone(self.context(self.failure(agent_id=5)))

    def test_old_shown_prompt_format_reads(self):
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"counts": {}, "shown_prompt": "p-1"}), encoding="utf-8")
        self.failure()
        self.assertIsNotNone(self.context(self.failure()))

    def test_old_shown_prompt_dropped_on_write(self):
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"counts": {}, "shown_prompt": "p-1"}), encoding="utf-8")
        self.failure()
        self.assertNotIn("shown_prompt", json.loads(self.state_file().read_text(encoding="utf-8")))
        self.failure()
        state = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(sorted(state), ["counts", "shown"])
        self.succeed()
        self.assertNotIn("shown_prompt", json.loads(self.state_file().read_text(encoding="utf-8")))

    def test_bool_count_in_state_ignored(self):
        key, _ = debug_watch.command_key("make test")
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"counts": {key: True}}), encoding="utf-8")
        self.assertIsNone(self.context(self.failure()))

    def test_long_command_truncated_in_line(self):
        command = "make " + "x" * 300
        self.failure(command)
        ctx = self.context(self.failure(command))
        first = ctx.removeprefix(f"{common.ADDRESS},\n\n").split("\n", 1)[0]
        self.assertIn("`" + command[:debug_watch.MAX_SHOWN_COMMAND - 1] + "…`", first)
        self.assertNotIn("x" * debug_watch.MAX_SHOWN_COMMAND, first)

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

    def test_counts_and_shown_bounded(self):
        key, _ = debug_watch.command_key("make test")
        counts = {f"k{i}": 1 for i in range(debug_watch.MAX_COUNTS * 3)}
        counts[key] = 1
        shown = {f"a{i}": "p" for i in range(debug_watch.MAX_SHOWN * 3)}
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"counts": counts, "shown": shown}), encoding="utf-8")
        self.assertIsNotNone(self.context(self.failure()))
        state = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertLessEqual(len(state["counts"]), debug_watch.MAX_COUNTS)
        self.assertLessEqual(len(state["shown"]), debug_watch.MAX_SHOWN)
        self.assertIn(key, state["counts"])
        self.assertIn("", state["shown"])
        self.assertNotIn("k0", state["counts"])
        self.assertNotIn("a0", state["shown"])

    def test_recent_failure_kept_over_old(self):
        self.failure("make a")
        state = {"counts": {f"k{i}": 1 for i in range(debug_watch.MAX_COUNTS - 1)}}
        key, _ = debug_watch.command_key("make a")
        state["counts"][key] = 1
        self.state_file().write_text(json.dumps(state), encoding="utf-8")
        self.failure("make b")
        self.failure("make a")
        kept = json.loads(self.state_file().read_text(encoding="utf-8"))["counts"]
        self.assertEqual(kept[key], 2)
        self.assertNotIn("k0", kept)

    def test_repeated_key_moves_to_end_and_survives_eviction(self):
        self.failure("make a")
        key, _ = debug_watch.command_key("make a")
        counts = {key: 1}
        counts.update({f"k{i}": 1 for i in range(debug_watch.MAX_COUNTS - 1)})
        self.state_file().write_text(json.dumps({"counts": counts}), encoding="utf-8")
        self.failure("make a")
        self.failure("make b")
        kept = json.loads(self.state_file().read_text(encoding="utf-8"))["counts"]
        self.assertEqual(kept[key], 2)
        self.assertNotIn("k0", kept)


class StateLockRaceTest(unittest.TestCase):
    """Параллельные неудачи разных команд одной сессии: каждая доходит до counts файла состояния."""

    PARALLEL = 6
    # Хук подпроцессом: процессы ждут друг друга перед первым common.data_dir (в update — до блокировки
    # состояния), чтение файла состояния задерживается на SLOW секунд. Без блокировки все читают пустой
    # файл, и каждая запись затирает предыдущие.
    SLOW = 0.3
    WRAPPER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import common, debug_watch
arrive, parallel, slow = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
data_dir, read_json = common.data_dir, common.read_json
def rendezvous():
    if not getattr(rendezvous, "done", False):
        rendezvous.done = True
        open(os.path.join(arrive, str(os.getpid())), "w").close()
        deadline = time.monotonic() + 20
        while len(os.listdir(arrive)) < parallel and time.monotonic() < deadline:
            time.sleep(0.01)
    return data_dir()
def delayed(*args):
    value = read_json(*args)
    time.sleep(slow)
    return value
common.data_dir, common.read_json = rendezvous, delayed
common.run_hook(debug_watch.main)
"""

    def setUp(self):
        self.env = Env(rules=dict(RULES, debugging=DEBUGGING))
        self.addCleanup(self.env.close)

    def test_parallel_failures_all_counted(self):
        arrive = self.env.data / "arrive"
        arrive.mkdir()
        procs = []
        for i in range(self.PARALLEL):
            hook_input = self.env.hook_input("PostToolUseFailure", tool_name="Bash",
                                             tool_input={"command": f"make t{i}"}, tool_use_id=f"t{i}",
                                             error="Exit code 1\nboom", is_interrupt=False)
            proc = subprocess.Popen(
                [sys.executable, "-c", self.WRAPPER, str(PLANKA_DIR), str(arrive), str(self.PARALLEL),
                 str(self.SLOW)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.env.environ())
            proc.stdin.write(json.dumps(hook_input).encode("utf-8"))
            proc.stdin.close()
            procs.append(proc)
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
            self.assertEqual((proc.stdout.read(), proc.stderr.read()), (b"", b""))
            proc.stdout.close()
            proc.stderr.close()
        state = json.loads((self.env.data / "state" / "sess-1.debug.json").read_text(encoding="utf-8"))
        expected = {debug_watch.command_key(f"make t{i}")[0]: 1 for i in range(self.PARALLEL)}
        self.assertEqual(state["counts"], expected)


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


class CountsTest(unittest.TestCase):
    def test_filters_non_positive_and_non_int(self):
        counts = {"a": True, "b": 0, "c": -1, "d": "2", "e": 2, "f": 1.5}
        self.assertEqual(debug_watch._counts({"counts": counts}), {"e": 2})
        self.assertEqual(debug_watch._counts({"counts": []}), {})


class ShownMarksTest(unittest.TestCase):
    def test_filters_non_string_values(self):
        self.assertEqual(debug_watch._shown_marks({"shown": {"a": "p", "b": 5, "c": None, "": "q"}}),
                         {"a": "p", "": "q"})
        self.assertEqual(debug_watch._shown_marks({"shown": ["p"]}), {})
        self.assertEqual(debug_watch._shown_marks({}), {})


class ShownTest(unittest.TestCase):
    def test_truncates_at_limit(self):
        limit = debug_watch.MAX_SHOWN_COMMAND
        self.assertEqual(debug_watch._shown("a" * limit), "a" * limit)
        self.assertEqual(debug_watch._shown("a" * (limit + 1)), "a" * (limit - 1) + "…")


class SegmentsTest(unittest.TestCase):
    def test_arithmetic_shift_is_not_heredoc(self):
        self.assertEqual(debug_watch._segments("(( n = x << 2 ))\ngrep foo f"),
                         [("", "(( n = x << 2 ))"), ("\n", "grep foo f")])
        self.assertEqual(debug_watch._segments("echo $((1<<2))\ngrep x f"),
                         [("", "echo $((1<<2))"), ("\n", "grep x f")])

    def test_unclosed_test_brackets_linear(self):
        small, large = ("[[ " * n for n in (25000, 100000))
        assert_linear(self, lambda: debug_watch._segments(small), lambda: debug_watch._segments(large))


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
                        "(cd d && grep x f)", "(make; grep x f)",
                        "[[ -f a && -f b ]]", "[[ -f a || -f b ]]", "make; [[ -f a && -f b ]]",
                        "grep -f - file <<EOF\npat\nEOF", "grep -f - file <<'EOF'\na && b\nEOF\n",
                        "grep -f - file <<-EOF\n\tpat\n\tEOF", "make\ngrep -f - file <<EOF\nmake test\nEOF",
                        "grep x f <<< 'a && b'", "echo $((1<<2))\ngrep x f", "(( n = x << 2 ))\ngrep foo f",
                        "echo $(( (1<<2) + 1 ))\ngrep x f", "for ((i=0;i<1<<2;i++)); do :; done\ngrep x f",
                        "echo [[ -f x\ngrep a f"):
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
                        "grep x f && echo ok < in", "make test;#grep", "grep a#b; make", "{ grep x f; }",
                        "[[ -f a ]] && make test", "[[ -f a ]]; make test",
                        "grep -f - file <<EOF\npat\nEOF\nmake test", "cat <<EOF\ngrep x f\nEOF",
                        "grep -f - file <<-EOF\n\tpat\n\tEOF\nmake test", "grep x f <<< 'a'\nmake test",
                        "cat <<EOF $((1<<2))\ngrep x f\nEOF"):
            with self.subTest(command=command):
                self.assertFalse(debug_watch.code1_is_answer(command))

    def test_long_word_linear(self):
        for prefix in ("grep ", "git diff --quiet ", "echo ok && "):
            with self.subTest(prefix=prefix):
                small, large = (prefix + "a" * n + " f" for n in (100000, 400000))
                assert_linear(self, lambda: debug_watch.code1_is_answer(small),
                              lambda: debug_watch.code1_is_answer(large))

    def test_word_cut_by_head_limit_not_taken_as_flag(self):
        # Слово «--quietzzz», разрезанное пределом на «--quiet», не флаг git diff.
        head = "git diff "
        pad = "x" * (debug_watch._HEAD_LIMIT - len(head) - len("--quiet") - 1)
        self.assertTrue(debug_watch.code1_is_answer(f"{head}{pad} --quiet"))
        self.assertFalse(debug_watch.code1_is_answer(f"{head}{pad} --quietzzz"))

    def test_long_word_keeps_verdict(self):
        self.assertTrue(debug_watch.code1_is_answer("grep " + "a" * 100000 + " f"))
        self.assertTrue(debug_watch.code1_is_answer("grep '" + "a b" * 50000 + "' f"))
        self.assertFalse(debug_watch.code1_is_answer("make " + "a" * 100000))
        self.assertFalse(debug_watch.code1_is_answer("echo " + "a" * 100000 + " && make"))
