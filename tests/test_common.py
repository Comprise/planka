import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR, STUB_DIR

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
        with mock.patch("sys.stdin", new=__import__("io").StringIO("")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=__import__("io").StringIO("not json")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=__import__("io").StringIO('{"a":1}')):
            self.assertEqual(common.read_input(), {"a": 1})


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
        with mock.patch("sys.stderr", new=__import__("io").StringIO()) as err:
            self.assertIsNone(common.philosophy_sections("Решения", "Нет такого"))
            self.assertIn("planka:", err.getvalue())

    def test_missing_file_is_none(self):
        (self.env.root / "philosophy.md").unlink()
        with mock.patch("sys.stderr", new=__import__("io").StringIO()) as err:
            self.assertIsNone(common.philosophy_text())
            self.assertIn("planka:", err.getvalue())


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

    def test_flags_stdin_and_env(self):
        rec = self.env.data / "rec.txt"
        self.judge(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec), PLANKA_MODEL="haiku")
        text = rec.read_text(encoding="utf-8")
        for flag in ["-p", "--setting-sources", "--strict-mcp-config",
                     "--no-session-persistence", "--output-format", "json",
                     "--json-schema", "--model", "haiku", "--system-prompt", "SYS"]:
            self.assertIn(flag, text)
        self.assertNotIn("--bare", text)
        self.assertIn("STDIN\nUSER", text)
        self.assertIn("ENV PLANKA_JUDGE=1", text)

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
        with mock.patch.dict(os.environ, env.environ(), clear=True):
            common.log_event("tool", "s", verdict="ok", reason="")
        lines = env.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["hook"], "tool")
        self.assertEqual(lines[0]["session_id"], "s")
        self.assertIn("ts", lines[0])
        env.close()


if __name__ == "__main__":
    unittest.main()
