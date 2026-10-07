import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402


class BarrierTest(unittest.TestCase):
    def test_off_and_judge_env(self):
        with mock.patch.dict(os.environ, {"PLANKA_OFF": "1"}, clear=False):
            self.assertTrue(common.barrier_active())
        with mock.patch.dict(os.environ, {"PLANKA_JUDGE": "1"}, clear=False):
            self.assertTrue(common.barrier_active())
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANKA_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(common.barrier_active())


class ReadInputTest(unittest.TestCase):
    def test_empty_and_garbage(self):
        with mock.patch("sys.stdin", new=io.StringIO("")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=io.StringIO("not json")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=io.StringIO('{"a":1}')):
            self.assertEqual(common.read_input(), {"a": 1})
        with mock.patch("sys.stdin", new=io.StringIO('[1]')):
            self.assertIsNone(common.read_input())


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
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.philosophy_sections("Решения", "Нет такого"))
            self.assertIn("planka:", err.getvalue())

    def test_missing_file_is_none(self):
        (self.env.root / "philosophy.md").unlink()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.philosophy_text())
            self.assertIn("planka:", err.getvalue())


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
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("verification", "nope"))
            self.assertIn("planka:", err.getvalue())

    def test_non_utf8_rule_is_none(self):
        (self.env.root / "rules" / "verification.md").write_bytes(b"\xff\xfe")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("verification"))
            self.assertIn("planka:", err.getvalue())

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
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertIsNone(common.rubric(("Решения",), ("nope",)))
            self.assertIsNone(common.rubric(("Нет такого",), ("planning",)))

    def test_rules_dir_missing_is_none(self):
        import shutil
        shutil.rmtree(self.env.root / "rules")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("planning"))
            self.assertIn("planka:", err.getvalue())
        self.assertIn(str(self.env.root / "rules"), common.philosophy_text())


class RunJudgeTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def judge(self, **extra):
        with mock.patch.dict(os.environ, {**self.base, **extra}, clear=True):
            return common.run_judge("SYS", "USER", timeout=3)

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

    def test_flags_stdin_and_env(self):
        rec = self.env.data / "rec.txt"
        self.judge(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec), PLANKA_MODEL="haiku")
        text = rec.read_text(encoding="utf-8")
        argv = text.split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")
        self.assertEqual(argv[:9], ["-p", "--setting-sources", "", "--strict-mcp-config",
                                    "--no-session-persistence", "--output-format", "json",
                                    "--tools", ""])
        for whole in ["--json-schema", "--model", "haiku", "--system-prompt", "SYS"]:
            self.assertIn(whole, argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("STDIN\nUSER", text)
        self.assertIn("ENV PLANKA_JUDGE=1", text)
        self.assertIn(f"CWD {self.env.data.resolve()}", text.splitlines())

    def test_non_executable_binary_is_error(self):
        fake = self.env.root / "bin"
        fake.mkdir()
        (fake / "claude").write_text("#!/bin/sh\n")
        v = self.judge(PATH=str(fake))
        self.assertTrue(v.ok)
        self.assertIn("claude", v.error)

    def test_timeout_is_error(self):
        v = self.judge(PLANKA_STUB="hang")
        self.assertTrue(v.ok)
        self.assertIn("таймаут", v.error)

    def test_garbage_is_error(self):
        v = self.judge(PLANKA_STUB="garbage")
        self.assertTrue(v.ok)
        self.assertIsNotNone(v.error)

    def test_not_logged_in_is_error(self):
        v = self.judge(PLANKA_STUB="notlogged")
        self.assertTrue(v.ok)
        self.assertIn("Not logged in", v.error)

    def test_missing_binary_is_error(self):
        v = self.judge(PATH="/nonexistent")
        self.assertTrue(v.ok)
        self.assertIn("claude", v.error)


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

    def test_counter_survives_sequential_writes(self):
        for _ in range(5):
            common.deny_budget_exhausted("s", "p", "tool")
        state = json.loads((self.env.data / "state" / "s.json").read_text())
        self.assertEqual(state["p:tool"], 5)
        self.assertEqual([p.name for p in (self.env.data / "state").iterdir()], ["s.json"])

    def test_stale_state_is_pruned(self):
        state = self.env.data / "state"
        state.mkdir()
        stale, fresh = state / "old.json", state / "new.json"
        for p in (stale, fresh):
            p.write_text("{}", encoding="utf-8")
        week_ago = time.time() - 8 * 86400
        os.utime(stale, (week_ago, week_ago))
        common.deny_budget_exhausted("s", "p", "tool")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((state / "s.json").exists())

    def test_session_id_is_sanitized(self):
        common.deny_budget_exhausted("../../x/y", "p", "tool")
        common.deny_budget_exhausted("", "p", "tool")
        names = sorted(p.name for p in (self.env.data / "state").iterdir())
        self.assertEqual(names, [".._.._x_y.json", "unknown.json"])


class RunHookTest(unittest.TestCase):
    def test_exception_is_warning_and_exit_zero(self):
        def main():
            raise PermissionError("нет доступа")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                common.run_hook(main)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("planka: внутренняя ошибка: PermissionError", err.getvalue())

    def test_normal_main_returns(self):
        calls = []
        common.run_hook(lambda: calls.append(1))
        self.assertEqual(calls, [1])


class OutputsTest(unittest.TestCase):
    def test_formats(self):
        d = json.loads(common.deny_output("r"))
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(d["hookSpecificOutput"]["permissionDecisionReason"], "r")
        b = json.loads(common.block_output("r"))
        self.assertEqual(b, {"decision": "block", "reason": "r"})
        c = json.loads(common.context_output("t"))
        self.assertEqual(c["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(c["hookSpecificOutput"]["additionalContext"], "t")


class LogTest(unittest.TestCase):
    def test_log_line(self):
        env = Env()
        try:
            with mock.patch.dict(os.environ, env.environ(), clear=True):
                common.log_event("tool", "s", verdict="ok", reason="")
            lines = env.log_lines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["hook"], "tool")
            self.assertEqual(lines[0]["session_id"], "s")
            self.assertIn("ts", lines[0])
        finally:
            env.close()


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def test_settings_unset(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": None, "doc_lang": None})

    def test_settings_set(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en+ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "en+ru", "doc_lang": "ru"})

    def test_substitute_all_placeholders(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"}, clear=True):
            out = common.substitute("a {RULES} b {COMMENT_LANG} c {DOC_LANG}")
            self.assertEqual(out, f"a {self.env.root / 'rules'} b en c en")

    def test_substitute_unset_is_marker(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.substitute("{COMMENT_LANG}/{DOC_LANG}"), "не задан/не задан")

    def test_philosophy_and_rules_substituted(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertIn("Язык комментариев: ru; язык документации: ru.", common.philosophy_text())
            self.assertIn("Язык: ru.", common.rule_texts("comments"))
            self.assertNotIn("{COMMENT_LANG}", common.rule_texts("comments"))


class ProjectRootTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

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


class WarnOnceTest(unittest.TestCase):
    def test_once_per_session_and_key(self):
        env = Env()
        try:
            with mock.patch.dict(os.environ, env.environ(), clear=True), \
                 mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertTrue(common.warn_once("s", "lang", "раз"))
                self.assertFalse(common.warn_once("s", "lang", "раз"))
                self.assertTrue(common.warn_once("s", "other", "два"))
                self.assertTrue(common.warn_once("s2", "lang", "три"))
                self.assertEqual(err.getvalue().count("planka:"), 3)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
