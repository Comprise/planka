import hashlib
import json
import sys
import unittest
from unittest import mock

from tests.helpers import (RULES, Env, PLANKA_DIR, assert_not_logged, author_block, fill_budget, messages, output,
                           run_in_process)

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
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

NO_PLAN_PATH = "planka: план не найден: в транскрипте нет planFilePath"

AUTHOR_TURN = "Нужна быстрая заплатка гонки, перестройку владения не предлагай."
AUTHOR_ANSWER = 'User has answered your questions: "Чинить сейчас?"="Да, сегодня".'


def author_entries(model="claude-test-model"):
    """Записи транскрипта: реплика автора, вопрос AskUserQuestion агента и ответ автора на него.

    Форма — Claude Code без origin: реплика — строка content, ответ — tool_result с текстом
    «User has answered your questions: …»."""
    return [
        {"type": "user", "message": {"role": "user", "content": AUTHOR_TURN}},
        {"type": "assistant", "message": {"model": model, "content": [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "q1", "content": AUTHOR_ANSWER}]}},
    ]


PLAN_CLEAN = PLAN_CONFLICT.replace("- Создать: `x.py`\n### Задача 2", "- Создать: `y.py`\n### Задача 2")


class QuestionTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def ask(self, **extra):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT,
            tool_use_id="t1"), **extra)

    def test_ok_passes(self):
        r = self.ask(PLANKA_STUB="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "ok")

    def test_deny(self):
        r = self.ask(PLANKA_STUB="deny", PLANKA_STUB_REASON="рекомендован по трудозатратам")
        out = output(r)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertIn("рекомендован по трудозатратам", out["permissionDecisionReason"])
        self.assertIn("Решения 4", out["permissionDecisionReason"])
        self.assertTrue(out["permissionDecisionReason"].startswith("planka: "))

    def test_judge_gets_rendered_options_and_rubric(self):
        rec = self.env.data / "rec.txt"
        self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertNotIn("## Планы", text)
        self.assertIn("1. Мьютекс (Recommended) — три строки", text)
        self.assertIn("2. Один писатель — перестроить владение", text)

    def test_judge_gets_author_turn_not_logged(self):
        self.env.transcript.write_text("".join(json.dumps(e) + "\n" for e in author_entries()), encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(author_block(rec), "Реплика автора текущего хода:\n" + AUTHOR_TURN + "\n\n"
                                            "Ответы автора на AskUserQuestion после неё:\n" + AUTHOR_ANSWER)
        self.assertIn("Явная просьба автора в <author> побеждает рубрику", rec.read_text(encoding="utf-8"))
        last = self.env.log_lines()[-1]
        self.assertEqual(last["content_len"], len(judge_tool.prompts.render_questions(QUESTION_INPUT)))
        assert_not_logged(self, self.env, AUTHOR_TURN, "быстрая заплатка", "Да, сегодня", "Мьютекс")

    def test_budget_reached_at_deny_time_passes(self):
        # Параллельный вызов исчерпал лимит между проверкой до судьи и отказом.
        fill_budget(self.env, "question")
        with mock.patch.object(common, "deny_budget_left", return_value=True):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT), PLANKA_STUB="deny")
        self.assertEqual(out, {"systemMessage": "planka: лимит отказов, пропущено без проверки"})
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "budget")
        self.assertEqual(last["reason"], "в списке нет варианта, снимающего причину")

    def test_session_model_from_transcript(self):
        rec = self.env.data / "rec.txt"
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n",
                     encoding="utf-8")
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT, transcript_path=str(t)),
            PLANKA_STUB_RECORD=str(rec), CLAUDE_PLUGIN_OPTION_JUDGE_MODEL="session")
        self.assertEqual(r.stdout, "")
        argv = rec.read_text(encoding="utf-8").split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")

    def test_budget_checked_before_judge(self):
        for _ in range(2):
            self.assertEqual(output(self.ask(PLANKA_STUB="deny"))["hookSpecificOutput"]["permissionDecision"], "deny")
        rec = self.env.data / "rec.txt"
        r = self.ask(PLANKA_STUB="deny", PLANKA_STUB_RECORD=str(rec))
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: лимит отказов, пропущено без проверки"])
        self.assertFalse(rec.exists())
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "budget")
        self.assertEqual(last["content_len"], len(judge_tool.prompts.render_questions(QUESTION_INPUT)))

    def test_deny_survives_log_failure(self):
        (self.env.data / "judge.log").mkdir()
        r = self.ask(PLANKA_STUB="deny")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(any("внутренняя ошибка" in m for m in messages(r)))

    def test_judge_failure_passes_with_one_warning(self):
        r = self.ask(PLANKA_STUB="notlogged")
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: судья пропущен: ошибка судьи: Not logged in · Please run /login"])
        last = self.env.log_lines()[-1]
        self.assertEqual((last["verdict"], last["error"]), ("skipped", "ошибка судьи"))
        self.assertNotIn("Not logged in", json.dumps(last, ensure_ascii=False))

    def test_empty_questions_pass_without_judge(self):
        rec = self.env.data / "rec.txt"
        for ti in ({"questions": []}, {}):
            r = self.env.run("judge_tool.py", self.env.hook_input(
                "PreToolUse", tool_name="AskUserQuestion", tool_input=ti), PLANKA_STUB="deny",
                PLANKA_STUB_RECORD=str(rec))
            self.assertEqual(r.stdout, "")
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines(), [])

    def test_other_tool_passes_silently(self):
        r = self.env.run("judge_tool.py", self.env.hook_input("PreToolUse", tool_name="Read", tool_input={}))
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])


class InputGuardTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_barrier_skips_dependency_check(self):
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "npm install x"}), PLANKA_JUDGE="1")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_barrier_skips_question_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT),
            PLANKA_JUDGE="1", PLANKA_STUB="deny", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "")
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines(), [])

    def test_empty_or_garbage_stdin(self):
        for stdin in ("", "   \n", "not json", "[1, 2]", '"x"'):
            r = self.env.run("judge_tool.py", stdin)
            self.assertEqual(r.returncode, 0, stdin)
            self.assertEqual(r.stdout, "", stdin)
            self.assertEqual(r.stderr, "", stdin)
        self.assertEqual(self.env.log_lines(), [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def with_plan(self, text, author=False):
        """Транскрипт с путём к плану text; author — перед ним реплика автора и ответ на AskUserQuestion."""
        plan = self.env.data / "plan.md"
        plan.write_text(text, encoding="utf-8")
        t = self.env.data / "t.jsonl"
        entries = author_entries() if author else [{"type": "assistant", "message": {"model": "claude-test-model"}}]
        entries.append({"type": "attachment", "attachment": {"type": "plan_mode", "planFilePath": str(plan)}})
        t.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return str(t)

    def test_plan_judge_gets_author_turn_not_logged(self):
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CLEAN, author=True), PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        block = author_block(rec)
        self.assertIn(AUTHOR_TURN, block)
        self.assertIn(AUTHOR_ANSWER, block)
        self.assertNotIn("Задача 2: B", block)
        self.assertEqual(self.env.log_lines()[-1]["content_len"], len(PLAN_CLEAN))
        assert_not_logged(self, self.env, AUTHOR_TURN, "Да, сегодня", "Задача 2: B")

    def test_plan_budget_reached_at_deny_time_passes(self):
        fill_budget(self.env, "plan")
        t = self.with_plan(PLAN_CLEAN)
        with mock.patch.object(common, "deny_budget_left", return_value=True):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=t), PLANKA_STUB="deny")
        self.assertEqual(out, {"systemMessage": "planka: лимит отказов, пропущено без проверки"})
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "budget")

    def exit_plan(self, transcript, **extra):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=transcript), **extra)

    def test_conflict_denied_without_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CONFLICT), PLANKA_STUB_RECORD=str(rec))
        out = output(r)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("файл x.py принадлежит задачам 1 и 2 волны 1", out["permissionDecisionReason"])
        self.assertTrue(out["permissionDecisionReason"].startswith("planka: "))
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-files")

    def test_plan_without_waves_goes_to_judge(self):
        rec = self.env.data / "rec.txt"
        plan = PLAN_CONFLICT.replace("## Волна 1\n", "")
        r = self.exit_plan(self.with_plan(plan), PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertTrue(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "ok")

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
        reason = output(r)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("нет схождения после волны 1", reason)
        self.assertTrue(reason.startswith("planka: "))

    def test_missing_transcript_passes_with_warning(self):
        r = self.exit_plan("/nonexistent/t.jsonl")
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), [NO_PLAN_PATH])
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_null_transcript_path_passes_with_warning(self):
        r = self.exit_plan(None)
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), [NO_PLAN_PATH])
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_non_dict_attachment_passes_with_warning(self):
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"attachment": "x"}) + "\n", encoding="utf-8")
        r = self.exit_plan(str(t))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), [NO_PLAN_PATH])
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_unusable_data_dir_passes_without_deny(self):
        blocker = self.env.data / "file"
        blocker.write_text("", encoding="utf-8")
        r = self.exit_plan(self.with_plan(PLAN_CONFLICT), CLAUDE_PLUGIN_DATA=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka: внутренняя ошибка", "\n".join(messages(r)))
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_plan_file_passes_with_warning(self):
        t = self.with_plan(PLAN_CLEAN)
        (self.env.data / "plan.md").unlink()
        r = self.exit_plan(t)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), [f"planka: план не найден: нет файла {self.env.data / 'plan.md'}"])

    def test_unreadable_plan_passes_with_warning(self):
        # Каталог вместо файла — OSError, NUL в пути из транскрипта — ValueError: пропуск, не внутренняя ошибка.
        for path in (self.env.data, str(self.env.data / "plan.md") + "\0x"):
            with self.subTest(path=path):
                t = self.env.data / "t.jsonl"
                t.write_text(json.dumps({"attachment": {"type": "plan_mode", "planFilePath": str(path)}}) + "\n",
                             encoding="utf-8")
                r = self.exit_plan(str(t), PLANKA_STUB="deny")
                self.assertEqual(r.returncode, 0)
                self.assertIsNone(output(r))
                msgs = messages(r)
                self.assertEqual(len(msgs), 1, msgs)
                self.assertTrue(msgs[0].startswith(f"planka: план не прочитан: {path}: "), msgs)
                last = self.env.log_lines()[-1]
                self.assertEqual(last["verdict"], "skipped")
                self.assertTrue(last["error"].startswith("план не прочитан"), last)

    def test_plan_judge_gets_modules(self):
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CLEAN), PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        for needle in ["## Решения", "## Планы", "# Планирование", "# Субагенты", "# Рефакторинг", "# Паттерны", "# Эвристики"]:
            self.assertIn(needle, text)
        self.assertLess(text.index("## Планы"), text.index("# Планирование"))

    def test_plan_judge_skips_without_new_modules(self):
        for name in ("refactoring", "design-patterns", "heuristics"):
            with self.subTest(name=name):
                path = self.env.root / "rules" / f"{name}.md"
                path.unlink()
                r = self.exit_plan(self.with_plan(PLAN_CLEAN))
                self.assertIsNone(output(r))
                self.assertEqual(messages(r), [f"planka: нет модуля правил {path}"])
                path.write_text(RULES[name], encoding="utf-8")

    def test_plan_budget_checked_before_judge(self):
        t = self.with_plan(PLAN_CLEAN)
        for _ in range(2):
            r = self.exit_plan(t, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(t, PLANKA_STUB="deny", PLANKA_STUB_RECORD=str(rec))
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: лимит отказов, пропущено без проверки"])
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "budget")

    def test_plan_judge_failure_one_warning(self):
        r = self.exit_plan(self.with_plan(PLAN_CLEAN), PLANKA_STUB="garbage")
        self.assertIsNone(output(r))
        self.assertEqual(len(messages(r)), 1, messages(r))
        self.assertTrue(messages(r)[0].startswith("planka: судья пропущен: ответ судьи не JSON: "), messages(r))
        self.assertIn("nonsense", messages(r)[0])
        self.assertEqual(self.env.log_lines()[-1]["error"], "ответ судьи не JSON")

    def test_plan_judge_skips_without_module(self):
        (self.env.root / "rules" / "subagents.md").unlink()
        r = self.exit_plan(self.with_plan(PLAN_CLEAN))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), [f"planka: нет модуля правил {self.env.root / 'rules' / 'subagents.md'}"])
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_files_conflict_budget(self):
        t = self.with_plan(PLAN_CONFLICT)
        for _ in range(2):
            r = self.exit_plan(t)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-files")
        r = self.exit_plan(t)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: лимит отказов, пропущено без проверки"])
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "budget")
        self.assertEqual(last["content_len"], len(PLAN_CONFLICT))

    def test_budget_key_is_prompt_and_hook(self):
        for _ in range(2):
            self.env.run("judge_tool.py", self.env.hook_input(
                "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT), PLANKA_STUB="deny")
        t = self.with_plan(PLAN_CONFLICT)
        for _ in range(2):
            self.assertEqual(output(self.exit_plan(t))["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(output(self.exit_plan(t)))
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=t, prompt_id="p-2"))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")


class MissingRubricTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(philosophy="# X\n\n## Планы\n\n1. a\n")

    def tearDown(self):
        self.env.close()

    def check_skipped(self, r):
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: в philosophy.md нет раздела «Решения»"])
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "skipped")
        self.assertEqual(last["error"], "нет раздела рубрики")

    def test_question_without_section_is_logged(self):
        self.check_skipped(self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="AskUserQuestion", tool_input=QUESTION_INPUT)))

    def test_plan_without_section_is_logged(self):
        plan = self.env.data / "plan.md"
        plan.write_text(PLAN_CLEAN, encoding="utf-8")
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"attachment": {"planFilePath": str(plan)}}) + "\n", encoding="utf-8")
        self.check_skipped(self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=str(t))))


class BashTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def bash(self, command, **extra):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1"), **extra)

    def test_dependency_add_denied_without_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.bash("cd app && npm install left-pad", PLANKA_STUB_RECORD=str(rec))
        out = output(r)["hookSpecificOutput"]
        reason = out["permissionDecisionReason"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(reason.startswith("planka: "))
        self.assertIn("вопрос автору", reason)
        self.assertIn("Команда: npm install left-pad", reason)
        self.assertNotIn("cd app &&", reason.split("Команда:")[1])
        self.assertIn("cd app && PLANKA_DEP_OK=1 npm install", reason)
        self.assertIn(str(self.env.root / "rules" / "dependencies.md"), reason)
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-dep")
        last = self.env.log_lines()[-1]
        command = "cd app && npm install left-pad"
        self.assertNotIn("content", last)
        self.assertEqual(last["content_len"], len(command))
        self.assertEqual(last["content_sha256"], hashlib.sha256(command.encode("utf-8")).hexdigest())

    def test_dependency_log_reason_without_command(self):
        self.bash("SECRET_TOKEN=abc npm install left-pad")
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "deny-dep")
        entry = json.dumps(last, ensure_ascii=False)
        self.assertNotIn("left-pad", entry)
        self.assertNotIn("SECRET_TOKEN", entry)

    def test_dependency_deny_survives_log_failure(self):
        data = self.env.data / "as-file"
        data.write_text("x", encoding="utf-8")
        r = self.bash("npm install left-pad", CLAUDE_PLUGIN_DATA=str(data))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(any("внутренняя ошибка" in m for m in messages(r)))

    def test_lone_surrogate_in_command(self):
        r = self.env.run("judge_tool.py", '{"session_id": "s", "tool_name": "Bash", '
                                          '"tool_input": {"command": "npm install lodash\\udcff"}}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_marker_passes(self):
        r = self.bash("PLANKA_DEP_OK=1 npm install left-pad")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_ordinary_command_passes_silently(self):
        r = self.bash("make test")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_no_budget_for_dependency_denies(self):
        for _ in range(4):
            r = self.bash("npm install left-pad")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_missing_or_non_string_command_passes(self):
        for ti in ({}, {"command": None}, {"command": 5}):
            r = self.env.run("judge_tool.py", self.env.hook_input("PreToolUse", tool_name="Bash", tool_input=ti))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertNotIn("Traceback", r.stderr)

