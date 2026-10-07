import concurrent.futures
import hashlib
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
from prompts import JUDGE_SCHEMA  # noqa: E402

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "transcript-shapes.jsonl"


class BarrierTest(unittest.TestCase):
    def test_judge_env_only(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANKA_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(common.barrier_active())
            with mock.patch.dict(os.environ, {"PLANKA_JUDGE": "1"}, clear=False):
                self.assertTrue(common.barrier_active())
            with mock.patch.dict(os.environ, {"PLANKA_OFF": "1"}, clear=False):
                self.assertFalse(common.barrier_active())
            with mock.patch.dict(os.environ, {"PLANKA_JUDGE": ""}, clear=False):
                self.assertFalse(common.barrier_active())


def stdin_of(data):
    """Текстовый stdin поверх байтов, как у процесса хука."""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(raw), encoding="latin-1")


class ReadInputTest(unittest.TestCase):
    def test_empty_and_garbage(self):
        with mock.patch("sys.stdin", new=stdin_of("")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=stdin_of("not json")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=stdin_of('{"a":1}')):
            self.assertEqual(common.read_input(), {"a": 1})
        with mock.patch("sys.stdin", new=stdin_of('[1]')):
            self.assertIsNone(common.read_input())

    def test_non_utf8_is_none(self):
        with mock.patch("sys.stdin", new=stdin_of(b'{"a":"\xff"}')):
            self.assertIsNone(common.read_input())

    def test_utf8_independent_of_locale_encoding(self):
        with mock.patch("sys.stdin", new=stdin_of('{"a":"привет"}')):
            self.assertEqual(common.read_input(), {"a": "привет"})


class PhilosophyTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_sections_in_order(self):
        text = common.philosophy_sections("Планы", "Решения")
        self.assertTrue(text.startswith("## Планы"))
        self.assertIn("## Решения", text)
        self.assertLess(text.index("## Планы"), text.index("## Решения"))
        self.assertNotIn("## Поведение", text)
        self.assertIn("Правило планов два.", text)

    def test_missing_section_is_none(self):
        common._reset()
        self.assertIsNone(common.philosophy_sections("Решения", "Нет такого"))
        self.assertEqual(common._messages, ["planka: в philosophy.md нет раздела «Нет такого»"])

    def test_missing_file_is_none(self):
        (self.env.root / "philosophy.md").unlink()
        common._reset()
        self.assertIsNone(common.philosophy_text())
        self.assertEqual(common._messages, [f"planka: нет файла правил {self.env.root / 'philosophy.md'}"])


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_placeholder_replaced_with_rules_dir(self):
        text = common.philosophy_text()
        self.assertNotIn("{RULES}", text)
        self.assertIn(str(self.env.root / "rules"), text)

    def test_rule_texts_in_order(self):
        text = common.rule_texts("planning", "verification")
        self.assertTrue(text.startswith("# Планирование"))
        self.assertIn("# Доказательство", text)
        self.assertLess(text.index("# Планирование"), text.index("# Доказательство"))

    def test_missing_rule_is_none(self):
        common._reset()
        self.assertIsNone(common.rule_texts("verification", "nope"))
        self.assertEqual(common._messages, [f"planka: нет модуля правил {self.env.root / 'rules' / 'nope.md'}"])

    def test_non_utf8_rule_is_none(self):
        (self.env.root / "rules" / "verification.md").write_bytes(b"\xff\xfe")
        common._reset()
        self.assertIsNone(common.rule_texts("verification"))
        self.assertEqual(common._messages,
                         [f"planka: модуль правил {self.env.root / 'rules' / 'verification.md'} не в UTF-8"])

    def test_rubric_combines(self):
        text = common.rubric(("Решения", "Планы"), ("planning",))
        self.assertTrue(text.startswith("## Решения"))
        self.assertIn("## Планы", text)
        self.assertIn("# Планирование", text)
        self.assertLess(text.index("## Планы"), text.index("# Планирование"))

    def test_rubric_sections_only_and_modules_only(self):
        self.assertTrue(common.rubric(("Решения",), ()).startswith("## Решения"))
        self.assertTrue(common.rubric((), ("verification",)).startswith("# Доказательство"))

    def test_rubric_none_when_part_missing(self):
        common._reset()
        self.assertIsNone(common.rubric(("Решения",), ("nope",)))
        self.assertIsNone(common.rubric(("Нет такого",), ("planning",)))

    def test_rules_dir_missing_is_none(self):
        shutil.rmtree(self.env.root / "rules")
        common._reset()
        self.assertIsNone(common.rule_texts("planning"))
        self.assertEqual(common._messages,
                         [f"planka: нет модуля правил {self.env.root / 'rules' / 'planning.md'}"])
        self.assertIn(str(self.env.root / "rules"), common.philosophy_text())


class RunJudgeTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def judge(self, model="haiku", **extra):
        with mock.patch.dict(os.environ, {**self.base, **extra}, clear=True):
            return common.run_judge("SYS", "USER", model, timeout=3)

    def test_ok(self):
        v = self.judge(PLANKA_STUB="ok")
        self.assertTrue(v.ok)
        self.assertIsNone(v.error)

    def test_deny(self):
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="нет правильного варианта")
        self.assertFalse(v.ok)
        self.assertEqual(v.violated, ["Решения 4"])
        self.assertEqual(v.reason, "нет правильного варианта")

    def test_long_reason_is_capped(self):
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="ы" * 3000)
        self.assertEqual(len(v.reason), 2000)
        self.assertTrue(v.reason.endswith("ы…"))

    def record(self, **extra):
        rec = self.env.data / "rec.txt"
        self.judge(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec), **extra)
        return rec.read_text(encoding="utf-8")

    @staticmethod
    def argv_of(text):
        return text.split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")

    def test_flags_stdin_and_env(self):
        text = self.record()
        argv = self.argv_of(text)
        self.assertEqual(argv[:9], ["-p", "--setting-sources", "", "--strict-mcp-config",
                                    "--no-session-persistence", "--output-format", "json",
                                    "--tools", ""])
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), JUDGE_SCHEMA)
        self.assertNotIn("--bare", argv)
        self.assertIn("STDIN\nUSER", text)
        self.assertIn("ENV PLANKA_JUDGE=1", text)
        self.assertIn(f"CWD {self.env.data.resolve()}", text.splitlines())

    def test_no_model_omits_flag(self):
        rec = self.env.data / "rec.txt"
        self.judge(None, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertNotIn("--model", self.argv_of(rec.read_text(encoding="utf-8")))

    def test_non_executable_binary_is_error(self):
        fake = self.env.root / "bin"
        fake.mkdir()
        (fake / "claude").write_text("#!/bin/sh\n")
        v = self.judge(PATH=str(fake))
        self.assertTrue(v.ok)
        self.assertTrue(v.error.startswith("claude не запущен"), v.error)

    def test_missing_binary_is_error(self):
        v = self.judge(PATH="/nonexistent")
        self.assertTrue(v.ok)
        self.assertEqual(v.error, "claude не найден в PATH")

    def test_preexec_failure_is_skip(self):
        with mock.patch("subprocess.Popen", side_effect=subprocess.SubprocessError("Exception occurred in preexec_fn.")):
            v = self.judge()
        self.assertTrue(v.ok)
        self.assertTrue(v.error.startswith("claude не запущен"), v.error)

    def test_timeout_is_error_and_kills_judge(self):
        rec = self.env.data / "rec.txt"
        v = self.judge(PLANKA_STUB="hang", PLANKA_STUB_RECORD=str(rec))
        self.assertTrue(v.ok)
        self.assertIn("таймаут", v.error)
        pid = int(next(l for l in rec.read_text(encoding="utf-8").splitlines() if l.startswith("PID ")).split()[1])
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("судья жив после таймаута")

    def test_timeout_survives_vanished_group(self):
        real = os.killpg

        def killpg_then_gone(pid, sig):
            real(pid, sig)
            raise ProcessLookupError()
        with mock.patch("os.killpg", side_effect=killpg_then_gone):
            v = self.judge(PLANKA_STUB="hang")
        self.assertTrue(v.ok)
        self.assertIn("таймаут", v.error)

    def test_garbage_is_error_without_model_text(self):
        v = self.judge(PLANKA_STUB="garbage")
        self.assertTrue(v.ok)
        self.assertEqual(v.error, "ответ судьи не JSON")
        self.assertIn("nonsense", v.detail)

    def test_unparsed_is_error(self):
        v = self.judge(PLANKA_STUB="unparsed")
        self.assertTrue(v.ok)
        self.assertEqual(v.violated, [])
        self.assertEqual(v.error, "ответ судьи не разобран")

    def test_no_structured_output_is_error(self):
        v = self.judge(PLANKA_STUB="nostructured")
        self.assertTrue(v.ok)
        self.assertEqual(v.violated, [])
        self.assertEqual(v.error, "в ответе судьи нет structured_output")

    def test_not_logged_in_is_error_and_text_goes_to_detail(self):
        common._reset()
        v = self.judge(PLANKA_STUB="notlogged")
        self.assertTrue(v.ok)
        self.assertEqual(v.error, "ошибка судьи")
        self.assertEqual(v.detail, "Not logged in · Please run /login")
        self.assertEqual(common._messages, [])
        self.assertEqual(common.skip_message(v), "судья пропущен: ошибка судьи: Not logged in · Please run /login")

    def test_error_text_of_model_is_not_in_error(self):
        line = json.dumps({"is_error": True, "result": "СЕКРЕТ" * 100})
        proc = mock.Mock(pid=1)
        proc.communicate.return_value = (line + "\n", "")
        common._reset()
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch.dict(os.environ, self.base, clear=True):
            v = common.run_judge("S", "U", None)
        self.assertNotIn("СЕКРЕТ", v.error)
        self.assertEqual(len(v.detail), common.MAX_DETAIL)
        self.assertEqual(common._messages, [])

    def test_not_json_detail_is_capped(self):
        proc = mock.Mock(pid=1)
        proc.communicate.return_value = ("x" * 500, "y" * 500)
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch.dict(os.environ, self.base, clear=True):
            v = common.run_judge("S", "U", None)
        self.assertEqual(v.error, "ответ судьи не JSON")
        self.assertEqual(len(v.detail), common.MAX_DETAIL)

    def test_skip_message_without_detail(self):
        v = self.judge(PATH="/nonexistent")
        self.assertIsNone(v.detail)
        self.assertEqual(common.skip_message(v), "судья пропущен: claude не найден в PATH")

    def test_violated_must_be_list_of_strings(self):
        cases = {'"Решения 4"': [], '{"a": 1}': [], '["Решения 4", 7, null]': ["Решения 4", "7", "None"], '[]': []}
        for raw, expected in cases.items():
            v = self.judge(PLANKA_STUB="violated", PLANKA_STUB_VIOLATED=raw)
            self.assertFalse(v.ok)
            self.assertEqual(v.violated, expected, raw)

    def test_reason_of_exact_limit_is_kept(self):
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="ы" * common.MAX_REASON)
        self.assertEqual(v.reason, "ы" * common.MAX_REASON)

    def test_reason_with_quotes_and_backslashes(self):
        reason = 'он сказал "нет" \\ и \n всё'
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON=reason)
        self.assertEqual(v.reason, reason)


class JudgeModelTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()
        common._reset()

    def tearDown(self):
        self.env.close()

    def transcript(self, entries):
        p = self.env.data / "t.jsonl"
        p.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n",
                     encoding="utf-8")
        return str(p)

    def model(self, transcript_path, session_id="s", **extra):
        with mock.patch.dict(os.environ, {**self.base, **extra}, clear=True):
            return common.judge_model({"transcript_path": transcript_path, "session_id": session_id})

    def test_given_transcript_is_not_reread(self):
        data = {"transcript_path": self.transcript([{"type": "assistant", "message": {"model": "из файла"}}]),
                "session_id": "s"}
        with mock.patch.dict(os.environ, self.base, clear=True), \
             mock.patch("common.read_transcript", side_effect=AssertionError("повторное чтение")):
            self.assertEqual(common.judge_model(data, common.Transcript(model="claude-opus-5-5")), "claude-opus-5-5")
            self.assertIsNone(common.judge_model(data, common.Transcript()))
        self.assertEqual(len(common._messages), 1)

    def test_explicit_setting_wins(self):
        t = self.transcript([{"type": "assistant", "message": {"model": "claude-opus-5-5"}}])
        self.assertEqual(self.model(t, CLAUDE_PLUGIN_OPTION_JUDGE_MODEL="haiku"), "haiku")
        self.assertEqual(common._messages, [])

    def test_session_is_last_main_assistant_model(self):
        t = self.transcript([
            {"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
            "не json",
            {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
            {"type": "user", "message": {"model": "claude-user-model"}},
            {"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-5-5"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "assistant", "message": "не словарь"},
            {"type": "assistant", "message": {"model": ""}},
            {"type": "assistant", "message": {"model": 7}},
            [1],
        ])
        self.assertEqual(self.model(t), "claude-opus-5-5")
        self.assertEqual(self.model(t, CLAUDE_PLUGIN_OPTION_JUDGE_MODEL="session"), "claude-opus-5-5")
        self.assertEqual(common._messages, [])

    def test_session_not_found_is_none_with_warning(self):
        text = "planka: модель сессии не найдена в транскрипте, судья на модели claude по умолчанию"
        paths = [self.transcript([{"type": "user"}]), "/nonexistent/t.jsonl", None]
        for n, path in enumerate(paths):
            common._reset()
            self.assertIsNone(self.model(path, session_id=f"s{n}"))
            self.assertEqual(common._messages, [text])

    def test_warning_once_per_session(self):
        common._reset()
        self.assertIsNone(self.model(None))
        self.assertIsNone(self.model(None))
        self.assertEqual(len(common._messages), 1)
        self.assertIsNone(self.model(None, session_id="other"))
        self.assertEqual(len(common._messages), 2)


class DenyBudgetTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_two_denies_then_exhausted(self):
        self.assertFalse(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertFalse(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertTrue(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertFalse(common.deny_budget_exhausted("s", "p", "stop"))
        self.assertFalse(common.deny_budget_exhausted("s", "p2", "tool"))

    def test_parallel_calls_lose_nothing(self):
        with concurrent.futures.ThreadPoolExecutor(20) as pool:
            results = list(pool.map(lambda _: common.deny_budget_exhausted("s", "p", "stop"), range(20)))
        self.assertEqual(results.count(False), common.MAX_DENIES)
        state = json.loads((self.env.data / "state" / "s.json").read_text())
        self.assertEqual(state["p:stop"], 20)

    def test_counter_survives_sequential_writes(self):
        for _ in range(5):
            common.deny_budget_exhausted("s", "p", "tool")
        state = json.loads((self.env.data / "state" / "s.json").read_text())
        self.assertEqual(state["p:tool"], 5)
        self.assertEqual(sorted(p.name for p in (self.env.data / "state").iterdir()), [".lock", "s.json"])

    def test_stale_state_is_pruned(self):
        state = self.env.data / "state"
        state.mkdir()
        old = time.time() - 8 * 86400
        for name in ("old.json", "y.snap.json", "z.warned.json", ".tmp-abandoned"):
            p = state / name
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (old, old))
        for name in ("new.json", "fresh.snap.json", ".tmp-fresh"):
            (state / name).write_text("{}", encoding="utf-8")
        common.deny_budget_exhausted("s", "p", "tool")
        self.assertEqual(sorted(p.name for p in state.iterdir()),
                         [".lock", ".tmp-fresh", "fresh.snap.json", "new.json", "s.json"])

    def test_wrong_json_type_is_like_broken(self):
        state = self.env.data / "state"
        state.mkdir()
        for content in ("[]", '"x"', "7", "null", '{"p:tool": "много"}', '{"p:tool": [1]}'):
            (state / "s.json").write_text(content, encoding="utf-8")
            self.assertFalse(common.deny_budget_exhausted("s", "p", "tool"), content)
            self.assertEqual(json.loads((state / "s.json").read_text())["p:tool"], 1, content)

    def test_session_id_is_sanitized(self):
        common.deny_budget_exhausted("../../x/y", "p", "tool")
        common.deny_budget_exhausted("", "p", "tool")
        names = sorted(p.name for p in (self.env.data / "state").glob("*.json"))
        self.assertEqual(names, [".._.._x_y.json", "unknown.json"])

    def test_safe_name_replaces_non_ascii_letters(self):
        self.assertEqual(common.safe_name("сессия/1"), "_" * 7 + "1")
        self.assertEqual(common.safe_name("a.b-c_9"), "a.b-c_9")


class ReadTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "t.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def read(self, entries):
        self.path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n",
                             encoding="utf-8")
        return common.read_transcript(str(self.path))

    @staticmethod
    def user(content, **extra):
        return {"type": "user", "message": {"role": "user", "content": content}, **extra}

    @staticmethod
    def reply(*blocks, model="claude-opus-5-5", **extra):
        return {"type": "assistant", "message": {"model": model, "content": list(blocks)}, **extra}

    @staticmethod
    def text(t):
        return {"type": "text", "text": t}

    def test_turn_messages_after_last_author_turn(self):
        t = self.read([
            self.user("первая реплика"),
            self.reply(self.text("старый ответ")),
            self.user([self.text("вторая реплика")]),
            self.reply({"type": "thinking", "thinking": "мысль"}),
            self.reply(self.text("начало")),
            self.reply({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            self.user([{"type": "tool_result", "tool_use_id": "t1", "content": "вывод"}]),
            self.user([self.text("тело навыка")], isMeta=True),
            self.reply(self.text("часть 1, "), {"type": "tool_use"}, self.text("часть 2")),
            self.reply(self.text("   ")),
            self.reply(self.text("от субагента"), isSidechain=True),
            self.user("реплика субагента", isSidechain=True),
            self.reply(self.text("итог")),
        ])
        self.assertEqual(t.turn_messages, ["начало", "часть 1, часть 2", "итог"])

    def test_author_turn_answers_and_message_before(self):
        ask = {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}
        t = self.read([
            self.user("старая"),
            self.reply(self.text("до реплики")),
            self.reply({"type": "thinking", "thinking": "мысль"}),
            self.user([self.text("строка 1"), self.text("строка 2")]),
            self.reply(self.text("спрашиваю"), ask),
            self.reply({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            self.user([{"type": "tool_result", "tool_use_id": "q1", "content": [self.text("да")]},
                       {"type": "tool_result", "tool_use_id": "t1", "content": "чужой"}]),
            self.user([{"type": "tool_result", "tool_use_id": "q1", "content": "ещё"}], isSidechain=True),
            self.reply(self.text("пишу")),
        ])
        self.assertEqual((t.author_turn, t.author_answers, t.message_before_author),
                         ("строка 1\nстрока 2", ["да"], "до реплики"))
        self.assertEqual(t.turn_messages, ["спрашиваю", "пишу"])

    def test_new_author_turn_resets_answers_and_keeps_message_before(self):
        ask = {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}
        t = self.read([
            self.user("р1"), self.reply(self.text("а1"), ask),
            self.user([{"type": "tool_result", "tool_use_id": "q1", "content": "да"}]),
            self.user("р2"), self.user("р3"),
            self.user([{"type": "tool_result", "tool_use_id": "q1", "content": "поздно"}]),
        ])
        self.assertEqual((t.author_turn, t.author_answers, t.message_before_author), ("р3", [], "а1"))

    def test_author_turn_without_reply_empties_messages(self):
        t = self.read([self.reply(self.text("ответ")), self.user("новая")])
        self.assertEqual(t.turn_messages, [])

    def test_tool_result_with_text_is_not_author_turn(self):
        t = self.read([self.user("р"), self.reply(self.text("до")),
                       self.user([{"type": "tool_result", "content": "x"}, self.text("подпись")])])
        self.assertEqual(t.turn_messages, ["до"])

    def test_model_and_plan_file(self):
        t = self.read([
            {"type": "attachment", "attachment": {"type": "plan_mode", "planFilePath": "/a.md"}},
            self.reply(self.text("x"), model="claude-sonnet-5-5"),
            {"type": "attachment", "attachment": {"planFilePath": "/b.md"}},
            {"type": "attachment", "attachment": {"planFilePath": ""}},
            {"attachment": {"planFilePath": 7}},
            self.reply(model="claude-opus-5-5"),
            self.reply(model="<synthetic>"),
            self.reply(model="claude-haiku-5-5", isSidechain=True),
        ])
        self.assertEqual(t.model, "claude-opus-5-5")
        self.assertEqual(t.plan_file, pathlib.Path("/b.md"))

    def test_malformed_entries_skipped(self):
        t = self.read(["не json", "[1]", "7", {"type": "assistant", "message": "строка"},
                       {"type": "assistant", "message": {"content": "просто строка", "model": 7}},
                       {"type": "user", "message": {"content": None}},
                       {"type": "assistant", "message": {"content": [7, {"type": "text", "text": None}]}}])
        self.assertEqual(t, common.Transcript(turn_messages=["просто строка"]))

    def read_corpus(self, lines=None):
        """Корпус форм записей Claude Code 2.1.293 (содержимое заменено), первые lines строк."""
        rows = CORPUS.read_text(encoding="utf-8").splitlines()
        return self.read(rows if lines is None else rows[:lines])

    def test_corpus_service_entries_are_not_author_turn(self):
        # task-notification, peer, локальные команды и вставки isMeta не сбрасывают реплику; сообщение
        # человека посреди хода (queued_command с origin human) дописывается к ней.
        t = self.read_corpus()
        self.assertEqual((t.author_turn, t.author_answers, t.message_before_author),
                         ("реплика-1\nреплика-1-посреди", ["ответ-автора-1"], "ответ-навык"))
        self.assertEqual(t.turn_messages, ["ответ-1-а", "ответ-1-б", "ответ-1-в", "ответ-пиру", "ответ-последний"])

    def test_corpus_prefixes(self):
        # До первой записи с origin в транскрипте запись пользователя с текстом без tool_result, в том числе
        # локальная команда, считается репликой автора.
        self.assertTrue(self.read_corpus(2).author_turn.startswith("<command-name>"))
        t = self.read_corpus(8)
        self.assertEqual((t.author_turn, t.turn_messages), ("реплика-0", ["ответ-0"]))
        t = self.read_corpus(10)
        self.assertEqual((t.author_turn, t.turn_messages, t.message_before_author),
                         ("<command-message>skill</command-message><command-name>/skill</command-name>",
                          ["ответ-навык"], "ответ-0"))

    def test_queued_human_appends_and_keeps_turn(self):
        def queued(prompt, kind="human", meta=False, **extra):
            att = {"type": "queued_command", "prompt": prompt, "origin": {"kind": kind}}
            return {"type": "attachment", "attachment": {**att, "isMeta": True} if meta else att, **extra}
        ask = {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}
        t = self.read([
            queued("до реплики"),
            self.user("р", origin={"kind": "human"}),
            self.reply(self.text("а1"), ask),
            self.user([{"type": "tool_result", "tool_use_id": "q1", "content": "да"}]),
            queued([self.text("п1"), {"type": "image"}]),
            queued("чужое", kind="task-notification"),
            queued("субагент", isSidechain=True),
            queued("служебное", meta=True),
            queued(7),
            self.reply(self.text("а2")),
        ])
        self.assertEqual((t.author_turn, t.author_answers, t.turn_messages), ("р\nп1", ["да"], ["а1", "а2"]))
        self.assertEqual(self.read([queued("одна")]).author_turn, "одна")

    def test_missing_or_bad_path_is_empty(self):
        for path in (None, "", 7, "/nonexistent/t.jsonl", self.dir.name):
            self.assertEqual(common.read_transcript(path), common.Transcript(), path)

    def test_non_utf8_bytes_do_not_fail(self):
        self.path.write_bytes(json.dumps(self.reply(self.text("ok"))).encode() + b"\n\xff\xfe\n")
        self.assertEqual(common.read_transcript(str(self.path)).turn_messages, ["ok"])


class DenyBudgetLeftTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_left_until_exhausted_without_counting(self):
        for _ in range(5):
            self.assertTrue(common.deny_budget_left("s", "p", "stop"))
        common.deny_budget_exhausted("s", "p", "stop")
        self.assertTrue(common.deny_budget_left("s", "p", "stop"))
        common.deny_budget_exhausted("s", "p", "stop")
        self.assertFalse(common.deny_budget_left("s", "p", "stop"))
        self.assertTrue(common.deny_budget_left("s", "p", "tool"))
        state = json.loads((self.env.data / "state" / "s.json").read_text())
        self.assertEqual(state, {"p:stop": 2})

    def test_broken_state_is_left(self):
        state = self.env.data / "state"
        state.mkdir()
        for content in ("{", "[]", '{"p:stop": "много"}', '{"p:stop": true}'):
            (state / "s.json").write_text(content, encoding="utf-8")
            self.assertTrue(common.deny_budget_left("s", "p", "stop"), content)

    def test_read_failure_is_left(self):
        with mock.patch("common.state_lock", side_effect=PermissionError("нет доступа")):
            self.assertTrue(common.deny_budget_left("s", "p", "stop"))
        (self.env.data / "state").mkdir(exist_ok=True)
        (self.env.data / "state" / "s.json").mkdir()
        self.assertTrue(common.deny_budget_left("s", "p", "stop"))


def _pid_alive(pid):
    """Жив ли процесс; зомби — не жив."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        if os.path.isdir("/proc"):
            return False
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# Процесс «хука»: запускает судью через сторож и ждёт, пока его убьют.
_HOOK = """
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import common
common._start_judge(["claude"], "darwin", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
time.sleep(100)
"""


class WatchdogTest(unittest.TestCase):
    """Сторож судьи — механизм вне Linux; здесь он проверяется на любой POSIX."""

    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def judge(self, **extra):
        with mock.patch.dict(os.environ, {**self.base, **extra}, clear=True), \
             mock.patch.object(sys, "platform", "darwin"):
            return common.run_judge("SYS", "USER", "haiku", timeout=3)

    @staticmethod
    def wait_dead(pid, seconds):
        deadline = time.monotonic() + seconds
        while _pid_alive(pid):
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                return False
            time.sleep(0.05)
        return True

    @staticmethod
    def stub_pid(rec):
        return int(rec.read_text(encoding="utf-8").split("PID ", 1)[1].split()[0])

    def test_verdict_and_arguments_pass_through(self):
        rec = self.env.data / "rec.txt"
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="причина", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual((v.ok, v.reason, v.error), (False, "причина", None))
        text = rec.read_text(encoding="utf-8")
        argv = text.split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")
        self.assertEqual(argv[:9], ["-p", "--setting-sources", "", "--strict-mcp-config",
                                    "--no-session-persistence", "--output-format", "json", "--tools", ""])
        self.assertEqual(argv[-1], "SYS")
        self.assertIn("STDIN\nUSER", text)
        self.assertIn("ENV PLANKA_JUDGE=1", text)

    def test_missing_binary_is_not_found(self):
        v = self.judge(PATH="/nonexistent")
        self.assertEqual(v.error, "claude не найден в PATH")

    def test_timeout_kills_judge(self):
        rec = self.env.data / "rec.txt"
        v = self.judge(PLANKA_STUB="hang", PLANKA_STUB_RECORD=str(rec))
        self.assertIn("таймаут", v.error)
        self.assertTrue(self.wait_dead(self.stub_pid(rec), 5), "судья жив после таймаута")

    def test_judge_killed_with_hook(self):
        rec = self.env.data / "rec.txt"
        hook = subprocess.Popen([sys.executable, "-c", _HOOK, str(PLANKA_DIR)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=self.env.environ(PLANKA_STUB="hang", PLANKA_STUB_RECORD=str(rec)))
        try:
            deadline = time.monotonic() + 10
            while not (rec.exists() and "PID " in rec.read_text(encoding="utf-8")):
                self.assertLess(time.monotonic(), deadline, "судья не запустился")
                time.sleep(0.05)
            judge_pid = self.stub_pid(rec)
            self.assertTrue(_pid_alive(judge_pid))
            hook.kill()
            hook.wait()
            self.assertTrue(self.wait_dead(judge_pid, 5), "судья пережил хук")
        finally:
            hook.kill()
            hook.wait()


def run_captured(main):
    """run_hook с перехваченными stdout и stderr: (stdout, stderr)."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="ascii")
    with mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=io.StringIO()) as err:
        common.run_hook(main)
    out.flush()
    return raw.getvalue().decode("utf-8"), err.getvalue()


class RunHookTest(unittest.TestCase):
    def test_exception_is_system_message(self):
        def main():
            raise PermissionError("нет доступа")
        out, err = run_captured(main)
        self.assertEqual(err, "")
        msg = json.loads(out)
        self.assertEqual(list(msg), ["systemMessage"])
        self.assertTrue(msg["systemMessage"].startswith("planka: внутренняя ошибка: PermissionError"))

    def test_exception_keeps_emitted_output(self):
        def main():
            common.emit(common.block_output("r"))
            raise ValueError("x")
        out, _ = run_captured(main)
        msg = json.loads(out)
        self.assertEqual(msg["decision"], "block")
        self.assertIn("planka: внутренняя ошибка: ValueError", msg["systemMessage"])

    def test_normal_main_returns(self):
        calls = []
        out, err = run_captured(lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_warnings_only(self):
        def main():
            common.warn("раз")
            common.warn("два")
        out, err = run_captured(main)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"systemMessage": "planka: раз\nplanka: два"})

    def test_merge_with_each_output(self):
        for build in (common.deny_output, common.block_output, common.context_output):
            def main():
                common.warn("внимание")
                common.emit(build("текст"))
            out, err = run_captured(main)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out), {**build("текст"), "systemMessage": "planka: внимание"})

    def test_output_without_warnings(self):
        out, _ = run_captured(lambda: common.emit(common.block_output("r")))
        self.assertEqual(json.loads(out), {"decision": "block", "reason": "r"})

    def test_state_reset_between_runs(self):
        def first():
            common.warn("старое")
            common.emit(common.block_output("r"))
        run_captured(first)
        out, _ = run_captured(lambda: None)
        self.assertEqual(out, "")

    def test_warn_does_not_write_stderr(self):
        common._reset()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            common.warn("x")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(common._messages, ["planka: x"])


class RunHookEncodingTest(unittest.TestCase):
    def test_utf8_io_under_latin1_locale(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import common\n"
                "def main():\n"
                "    data = common.read_input()\n"
                "    common.emit(common.context_output(data['t']))\n"
                "common.run_hook(main)\n")
        env = {**os.environ, "PYTHONIOENCODING": "latin-1"}
        r = subprocess.run([sys.executable, "-c", code, str(PLANKA_DIR)], input=json.dumps({"t": "привет ✓"}).encode("utf-8"),
                           capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, b"")
        self.assertEqual(json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"], "привет ✓")


class OutputsTest(unittest.TestCase):
    def test_formats(self):
        d = common.deny_output("r")
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(d["hookSpecificOutput"]["permissionDecisionReason"], "r")
        self.assertEqual(common.block_output("r"), {"decision": "block", "reason": "r"})
        c = common.context_output("t")
        self.assertEqual(c["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(c["hookSpecificOutput"]["additionalContext"], "t")


class LogTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()
        self.log = self.env.data / "judge.log"

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_log_line(self):
        common.log_event("tool", "s", verdict="ok", reason="")
        lines = self.env.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["hook"], "tool")
        self.assertEqual(lines[0]["session_id"], "s")
        self.assertIn("ts", lines[0])
        self.assertNotIn("content_len", lines[0])
        self.assertNotIn("content_sha256", lines[0])

    def test_content_is_hashed(self):
        text = "секрет // comment"
        common.log_event("stop", "s", verdict="ok", content=text)
        entry = self.env.log_lines()[0]
        self.assertNotIn("content", entry)
        self.assertEqual(entry["content_len"], len(text))
        self.assertEqual(entry["content_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertNotIn("секрет", self.log.read_text(encoding="utf-8"))

    def test_lone_surrogate_logged(self):
        common.log_event("stop", "s", verdict="ok", files=["bad\udcff.py"])
        self.assertEqual(self.env.log_lines()[0]["files"], ["bad\udcff.py"])

    def test_rotation_at_threshold(self):
        self.assertEqual(common.LOG_MAX_BYTES, 1_048_576)
        (self.env.data / "judge.log.1").write_text("прежний\n", encoding="utf-8")
        old = "x" * (common.LOG_MAX_BYTES - 1) + "\n"
        self.log.write_text(old, encoding="utf-8")
        common.log_event("tool", "s", verdict="ok")
        self.assertEqual((self.env.data / "judge.log.1").read_text(encoding="utf-8"), old)
        self.assertEqual(len(self.env.log_lines()), 1)

    def test_no_rotation_below_threshold(self):
        old = "x" * (common.LOG_MAX_BYTES - 2) + "\n"
        self.log.write_text(old, encoding="utf-8")
        common.log_event("tool", "s", verdict="ok")
        self.assertFalse((self.env.data / "judge.log.1").exists())
        self.assertTrue(self.log.read_text(encoding="utf-8").startswith(old))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def test_settings_unset_is_ru(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "ru", "doc_lang": "ru"})

    def test_settings_set(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en+ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"}, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "en+ru", "doc_lang": "en"})

    def test_settings_empty_is_ru(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": ""}, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "ru", "doc_lang": "ru"})

    def test_substitute_all_placeholders(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"}, clear=True):
            out = common.substitute("a {RULES} b {COMMENT_LANG} c {DOC_LANG}")
            self.assertEqual(out, f"a {self.env.root / 'rules'} b en c en")

    def test_substitute_unset_is_ru(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.substitute("{COMMENT_LANG}/{DOC_LANG}"), "ru/ru")

    def test_philosophy_and_rules_substituted(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertIn("Язык комментариев: ru; язык документации: ru.", common.philosophy_text())
            self.assertIn("Язык: ru.", common.rule_texts("comments"))
            self.assertNotIn("{COMMENT_LANG}", common.rule_texts("comments"))


class ProjectRootTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_project_dir_wins_over_cwd(self):
        sub = self.env.project / "a" / "b"
        sub.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.env.project)}):
            self.assertEqual(common.project_root(str(sub)), self.env.project)

    def test_non_git_is_cwd(self):
        sub = self.env.project / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(common.project_root(str(sub)), sub)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_toplevel(self):
        subprocess.run(["git", "init", "-q", str(self.env.project)], check=True)
        sub = self.env.project / "pkg"
        sub.mkdir()
        self.assertEqual(common.project_root(str(sub)).resolve(), self.env.project.resolve())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_non_utf8_git_toplevel(self):
        root = pathlib.Path(os.fsdecode(bytes(self.env.project) + b"/\xffrepo"))
        try:
            root.mkdir()
        except OSError:
            self.skipTest("файловая система не принимает имя не в UTF-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        top = common.project_root(str(root))
        self.assertTrue(top.exists(), top)
        self.assertEqual(top.resolve(), root.resolve())

    def test_missing_dir_is_path(self):
        self.assertEqual(common.project_root("/nonexistent/x"), pathlib.Path("/nonexistent/x"))


class DocPathTest(unittest.TestCase):
    def test_doc_paths(self):
        for p in ["README.md", "README", "README.rst", "LICENSE", "CLAUDE.md", "internal/x/CLAUDE.md",
                  "context/a.md", "context/deferred/INDEX.md", "docs/en/x.md", "notes.md"]:
            self.assertTrue(common.is_doc_path(p), p)

    def test_code_paths(self):
        for p in ["main.go", "a/b.py", "Makefile", "docs.py", "context.go", "readme_test.go", "x.toml"]:
            self.assertFalse(common.is_doc_path(p), p)


class PathKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "README.md": "doc", "README": "doc", "LICENSE": "doc", "docs/en/x.md": "doc",
            "internal/CLAUDE.md": "doc", "context/notes.txt": "doc",
            "context/x.go": "code", "docs/docs.go": "code", "READMEParser.java": "code",
            "LICENSE_check.py": "code",
            "a.mjs": "code", "a.cjs": "code", "a.mts": "code", "a.cts": "code", "a.pyi": "code",
            "a.cxx": "code", "a.hh": "code", "a.hxx": "code", "CMakeLists.txt": "code", "m/x.cmake": "code",
            "pkg/x.go": "code", "a/b.py": "code", "Makefile": "code", "Dockerfile": "code",
            "sub/Justfile": "code", "makefile": "code", "GNUmakefile": "code", "Rakefile": "code", "Gemfile": "code", "x.toml": "code",
            "src/App.TSX": "code", "a.php": "code", "infra/main.tf": "code",
            "a.json": "other", "img.png": "other", "LICENSE-third-party.txt": "doc",
            "notes.txt": "other", "bin/tool": "other", "go.sum": "other",
            "a.css": "code", "a.scss": "code", "a.sass": "code", "a.LESS": "code",
            "package.json": "code", "web/tsconfig.json": "code", "jsconfig.json": "code", "composer.json": "code",
            "deno.json": "code", "mod/go.mod": "code", "Cargo.toml": "code", "pyproject.toml": "code",
            "setup.cfg": "code", "pom.xml": "code", "build.gradle": "code",
            "package-lock.json": "other", "tsconfig.base.json": "other", "docs/package.json": "code",
        }
        for path, kind in cases.items():
            self.assertEqual(common.path_kind(path), kind, path)

    def test_code_exts_cover_comment_families(self):
        import comments
        names = {n.lower() for n in comments._NAMES}
        self.assertLessEqual(set(comments._SYNTAX) - names, common.CODE_EXTS)
        self.assertLessEqual(names, set(comments._SYNTAX))
        self.assertEqual(comments._NAMES, common.CODE_NAMES)
        for ext in ("php", "r", "jl", "ex", "exs", "erl", "clj", "fs", "vb", "nim", "zig", "sol",
                    "proto", "gradle", "groovy", "tf", "nix", "el", "vim", "bat", "cmd", "css", "scss", "sass", "less"):
            self.assertIn(ext, common.CODE_EXTS)
        self.assertNotIn("json", common.CODE_EXTS)


class WarnOnceTest(unittest.TestCase):
    def test_wrong_json_type_is_like_broken(self):
        env = Env()
        try:
            common._reset()
            state = env.data / "state"
            state.mkdir()
            with mock.patch.dict(os.environ, env.environ(), clear=True):
                for content in ("{}", '"x"', "7", "null"):
                    (state / "s.warned.json").write_text(content, encoding="utf-8")
                    self.assertTrue(common.warn_once("s", "k", "м"), content)
                    self.assertFalse(common.warn_once("s", "k", "м"), content)
        finally:
            env.close()

    def test_once_per_session_and_key(self):
        env = Env()
        try:
            common._reset()
            with mock.patch.dict(os.environ, env.environ(), clear=True):
                self.assertTrue(common.warn_once("s", "lang", "раз"))
                self.assertFalse(common.warn_once("s", "lang", "раз"))
                self.assertTrue(common.warn_once("s", "other", "два"))
                self.assertTrue(common.warn_once("s2", "lang", "три"))
            self.assertEqual(common._messages, ["planka: раз", "planka: два", "planka: три"])
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
