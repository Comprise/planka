import json
import sys
import unittest

from tests.helpers import Env, PLANKA_DIR, hook_input

sys.path.insert(0, str(PLANKA_DIR))
import judge_tool  # noqa: E402

QUESTION_INPUT = {"questions": [{
    "question": "Как чинить гонку?", "header": "Подход", "multiSelect": False,
    "options": [
        {"label": "Мьютекс (Recommended)", "description": "три строки"},
        {"label": "Один писатель", "description": "перестроить владение"},
    ]}]}

PLAN_CONFLICT = """## Волна 1
### Задача 1: A
**Файлы:**
- Создать: `x.py`
### Задача 2: B
**Файлы:**
- Создать: `x.py`
"""

PLAN_CLEAN = PLAN_CONFLICT.replace("- Создать: `x.py`\n### Задача 2", "- Создать: `y.py`\n### Задача 2")


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def write_transcript(self, lines):
        p = self.env.data / "t.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_last_plan_path_wins(self):
        p = self.write_transcript([
            json.dumps({"type": "attachment", "attachment": {"type": "plan_mode", "planFilePath": "/a.md"}}),
            "garbage line",
            json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}),
            json.dumps({"type": "attachment", "attachment": {"type": "plan_mode", "planFilePath": "/b.md"}}),
        ])
        self.assertEqual(str(judge_tool.plan_file_from_transcript(str(p))), "/b.md")

    def test_malformed_entries_are_ignored(self):
        p = self.write_transcript([
            json.dumps({"type": "attachment", "attachment": {"planFilePath": "/a.md"}}),
            json.dumps({"attachment": "x"}),
            json.dumps({"attachment": {"planFilePath": 5}}),
        ])
        self.assertEqual(str(judge_tool.plan_file_from_transcript(str(p))), "/a.md")
        self.assertIsNone(judge_tool.plan_file_from_transcript(None))
        self.assertIsNone(judge_tool.plan_file_from_transcript(7))

    def test_no_plan_entry(self):
        p = self.write_transcript([json.dumps({"type": "user"})])
        self.assertIsNone(judge_tool.plan_file_from_transcript(str(p)))
        self.assertIsNone(judge_tool.plan_file_from_transcript("/nonexistent"))


class QuestionTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def ask(self, **extra):
        return self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT,
            tool_use_id="t1"), **extra)

    def test_ok_passes(self):
        r = self.ask(PLANKA_STUB="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "ok")

    def test_deny(self):
        r = self.ask(PLANKA_STUB="deny", PLANKA_STUB_REASON="рекомендован по трудозатратам")
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertIn("рекомендован по трудозатратам", out["permissionDecisionReason"])
        self.assertIn("Решения 4", out["permissionDecisionReason"])

    def test_judge_gets_rendered_options_and_rubric(self):
        rec = self.env.data / "rec.txt"
        self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertNotIn("## Планы", text)
        self.assertIn("1. Мьютекс (Recommended) — три строки", text)
        self.assertIn("2. Один писатель — перестроить владение", text)

    def test_budget(self):
        for _ in range(2):
            self.assertIn("deny", self.ask(PLANKA_STUB="deny").stdout)
        r = self.ask(PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        self.assertIn("лимит отказов", r.stderr)

    def test_judge_failure_passes(self):
        r = self.ask(PLANKA_STUB="notlogged")
        self.assertEqual(r.stdout, "")
        self.assertIn("Not logged in", r.stderr)

    def test_other_tool_passes_silently(self):
        r = self.env.run("judge_tool.py", hook_input("PreToolUse", tool_name="Read", tool_input={}))
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def with_plan(self, text):
        plan = self.env.data / "plan.md"
        plan.write_text(text, encoding="utf-8")
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"type": "attachment", "attachment": {
            "type": "plan_mode", "planFilePath": str(plan)}}) + "\n", encoding="utf-8")
        return str(t)

    def exit_plan(self, transcript, **extra):
        return self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=transcript), **extra)

    def test_conflict_denied_without_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CONFLICT), PLANKA_STUB_RECORD=str(rec))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("файл x.py принадлежит задачам 1 и 2 волны 1", out["permissionDecisionReason"])
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-files")

    def test_clean_plan_goes_to_judge_with_plan_rubric(self):
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CLEAN), PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertIn("## Планы", text)
        self.assertIn("Задача 2: B", text)
        self.assertIn("схождени", text)

    def test_plan_deny(self):
        r = self.exit_plan(self.with_plan(PLAN_CLEAN), PLANKA_STUB="deny", PLANKA_STUB_REASON="нет схождения после волны 1")
        self.assertIn("нет схождения после волны 1", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_missing_transcript_passes_with_warning(self):
        r = self.exit_plan("/nonexistent/t.jsonl")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_null_transcript_path_passes_with_warning(self):
        r = self.exit_plan(None)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_non_dict_attachment_passes_with_warning(self):
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"attachment": "x"}) + "\n", encoding="utf-8")
        r = self.exit_plan(str(t))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_unusable_data_dir_passes_without_deny(self):
        blocker = self.env.data / "file"
        blocker.write_text("", encoding="utf-8")
        r = self.exit_plan(self.with_plan(PLAN_CONFLICT), CLAUDE_PLUGIN_DATA=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka: внутренняя ошибка", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_plan_file_passes_with_warning(self):
        t = self.with_plan(PLAN_CLEAN)
        (self.env.data / "plan.md").unlink()
        r = self.exit_plan(t)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)


class MissingRubricTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(philosophy="# X\n\n## Планы\n\n1. a\n")

    def tearDown(self):
        self.env.close()

    def check_skipped(self, r):
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(r.stderr.count("planka:"), 1)
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "skipped")
        self.assertEqual(last["error"], "нет раздела рубрики")

    def test_question_without_section_is_logged(self):
        self.check_skipped(self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT)))

    def test_plan_without_section_is_logged(self):
        plan = self.env.data / "plan.md"
        plan.write_text(PLAN_CLEAN, encoding="utf-8")
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"attachment": {"planFilePath": str(plan)}}) + "\n", encoding="utf-8")
        self.check_skipped(self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=str(t))))


if __name__ == "__main__":
    unittest.main()
