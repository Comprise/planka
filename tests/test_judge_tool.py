import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.helpers import (PHILOSOPHY, RULES, Env, PLANKA_DIR, assert_not_logged, author_block, fill_budget, messages,
                           output, run_in_process)

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import judge_tool  # noqa: E402
import prompts  # noqa: E402
import manifest_watch  # noqa: E402

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


# Ядро теста с пунктом об опорах рекомендации в «Решениях»: его вырезает prompts.without_premises.
PHILOSOPHY_WITH_PREMISES = PHILOSOPHY.replace("2. Правило решений два.\n", "2. Правило решений два.\n"
                                              + prompts.PREMISES_ITEM + " — факта о коде — назван\n     источник.\n")
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

    def test_question_rubric_without_premises_item(self):
        self.env.close()
        self.env = Env(philosophy=PHILOSOPHY_WITH_PREMISES)
        rec = self.env.data / "rec.txt"
        self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("Правило решений два", text)
        self.assertNotIn("у опоры рекомендации", text)

    def test_judge_gets_rendered_options_and_rubric(self):
        rec = self.env.data / "rec.txt"
        self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertNotIn("## Планы", text)
        self.assertIn("1. Мьютекс (Recommended) — три строки", text)
        self.assertIn("2. Один писатель — перестроить владение", text)

    def test_judge_gets_earlier_author_turns(self):
        earlier = "Рефакторинг владения потом, сейчас только заплатка."
        entries = [{"type": "user", "message": {"role": "user", "content": earlier}},
                   {"type": "assistant", "message": {"model": "claude-test-model", "content": [
                       {"type": "text", "text": "Понял."}]}}] + author_entries()
        self.env.transcript.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.ask(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertTrue(author_block(rec).startswith("Прежние реплики автора, от старых к новым:\n" + earlier + "\n"))
        assert_not_logged(self, self.env, earlier)

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
        self.assertIn("(только чтение)", out["permissionDecisionReason"])
        self.assertIn("Файлы: нет", out["permissionDecisionReason"])
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-files")

    def test_conflict_log_has_metadata_only(self):
        self.exit_plan(self.with_plan(PLAN_CONFLICT))
        last = self.env.log_lines()[-1]
        self.assertEqual(last["verdict"], "deny-files")
        self.assertEqual(last["conflicts"], 1)
        self.assertNotIn("reason", last)
        self.assertNotIn("x.py", json.dumps(last, ensure_ascii=False))

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

    def test_plan_rubric_without_premises_item(self):
        # Судья плана шагов реплики не видит: пункт об опорах рекомендации из рубрики вырезан.
        self.env.close()
        self.env = Env(philosophy=PHILOSOPHY_WITH_PREMISES)
        rec = self.env.data / "rec.txt"
        r = self.exit_plan(self.with_plan(PLAN_CLEAN), PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("Правило решений два", text)
        self.assertNotIn("у опоры рекомендации", text)

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

    def test_doubtful_dependency_add_denied(self):
        # Менеджер и команда установки распознаны, пакет или подкоманда под сомнением: отказ с доводом и маркером.
        for command, segment, why in [
            ("npm --weird val i -g x", "npm --weird val i -g x", "флаг"),
            ("pip install --target --dry-run x", "pip install --target --dry-run x", "--dry-run"),
            ("cd app && echo x | xargs npm install", "xargs npm install", "xargs"),
            ("eval " * 5 + "npm install x", "eval " * 5 + "npm install x", "4 уровней"),
            ("npm install " + "-D " * 1400 + "left-pad", "npm install " + "-D " * 1400 + "left-pad", "4096"),
        ]:
            with self.subTest(command[:40]):
                r = self.bash(command)
                out = output(r)["hookSpecificOutput"]
                reason = out["permissionDecisionReason"]
                self.assertEqual(out["permissionDecision"], "deny")
                self.assertTrue(reason.startswith("planka: "))
                self.assertIn("сомнени", reason)
                self.assertIn(why, reason)
                self.assertIn("PLANKA_DEP_OK=1", reason)
                self.assertIn(str(self.env.root / "rules" / "dependencies.md"), reason)
                self.assertTrue(reason.endswith("Команда: " + segment))
                self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-dep")
        # npm берёт `false` значением флага: установка без сомнения.
        reason = output(self.bash("npm --dry-run false install x"))["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertNotIn("сомнени", reason)

    def test_doubtful_dependency_marker_passes(self):
        r = self.bash("echo x | PLANKA_DEP_OK=1 xargs npm install")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

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



# Манифесты по форме настоящих файлов корпуса tests/fixtures/manifests (npm, pyproject PEP 621, requirements).
PACKAGE_JSON = """{
  "name": "app",
  "version": "1.0.0",
  "scripts": {"test": "jest"},
  "dependencies": {"react": "^18.2.0"},
  "devDependencies": {"jest": "^29.0.0"}
}
"""
PYPROJECT = """[project]
name = "app"
version = "0.1.0"
dependencies = ["httpx>=0.27"]
"""
REQUIREMENTS = "httpx==0.27.0\nrich>=13\n"
SECRET_NAME = "lodash-secret-name"
# Файлы зависимостей PyPI без проверки (manifest_watch._LEGACY) из корпуса tests/fixtures/manifests (источник —
# первая строка файла): имя файла → (текст, два его требования в другой записи — перенос в pyproject.toml).
FIXTURES = PLANKA_DIR.parent.parent / "tests" / "fixtures" / "manifests"
LEGACY_SOURCES = {
    name: ((FIXTURES / rel).read_text(encoding="utf-8"), moved)
    for name, rel, moved in (("setup.py", "legacy-requests/setup.py", ["IDNA>=2", "certifi"]),
                             ("setup.cfg", "legacy-pytest/setup.cfg", ["packaging", "Requests"]),
                             ("Pipfile", "legacy-pipenv/Pipfile", ["Click", "pytz"]))
}


class ManifestEditTest(unittest.TestCase):
    """PreToolUse на Write, Edit, MultiEdit: новое имя зависимости в манифесте отклоняется."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def write(self, rel, text):
        p = self.env.project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def tool(self, tool, tool_input, **extra):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id="e1"), **extra)

    def edit(self, rel, old, new, **fields):
        return self.tool("Edit", {"file_path": str(self.env.project / rel), "old_string": old, "new_string": new,
                                  "replace_all": False, **fields})

    def assert_denied(self, r, *names):
        out = output(r)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertTrue(reason.startswith("planka: "), reason)
        self.assertIn("вопрос автору", reason)
        self.assertIn(str(self.env.root / "rules" / "dependencies.md"), reason)
        self.assertIn("PLANKA_DEP_OK=1", reason)
        for name in names:
            self.assertIn(name, reason)
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "deny-dep"))
        return reason

    def assert_silent(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "", r.stderr)

    def test_edit_package_json_new_dependency_denied(self):
        self.write("package.json", PACKAGE_JSON)
        r = self.edit("package.json", '"react": "^18.2.0"', f'"react": "^18.2.0", "{SECRET_NAME}": "^4.17.21"')
        reason = self.assert_denied(r, SECRET_NAME)
        self.assertIn(str(self.env.project / "package.json"), reason)
        assert_not_logged(self, self.env, SECRET_NAME)

    def test_write_pyproject_new_dependency_denied(self):
        self.write("pyproject.toml", PYPROJECT)
        r = self.tool("Write", {"file_path": str(self.env.project / "pyproject.toml"),
                                "content": PYPROJECT.replace('["httpx>=0.27"]', '["httpx>=0.27", "Requests>=2"]')})
        self.assert_denied(r, "requests")

    def test_multiedit_requirements_denied(self):
        self.write("requirements.txt", REQUIREMENTS)
        r = self.tool("MultiEdit", {"file_path": str(self.env.project / "requirements.txt"), "edits": [
            {"old_string": "rich>=13", "new_string": "rich>=14"},
            {"old_string": "rich>=14\n", "new_string": "rich>=14\nflask\n"}]})
        self.assert_denied(r, "flask")

    def test_write_new_requirements_file_denied(self):
        r = self.tool("Write", {"file_path": str(self.env.project / "requirements-dev.txt"), "content": "pytest\n"})
        self.assert_denied(r, "pytest")

    def test_relative_path_from_cwd(self):
        self.write("app/package.json", PACKAGE_JSON)
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Edit", cwd=str(self.env.project / "app"), tool_input={
                "file_path": "package.json", "old_string": '"jest": "^29.0.0"',
                "new_string": '"jest": "^29.0.0", "vitest": "^1"'}))
        self.assert_denied(r, "vitest")

    def test_replace_all(self):
        self.write("requirements.txt", "rich\n# rich\n")
        r = self.edit("requirements.txt", "rich", "flask", replace_all=True)
        self.assert_denied(r, "flask")

    def test_no_new_name_passes(self):
        cases = [
            ("package.json", PACKAGE_JSON, '"react": "^18.2.0"', '"react": "^19.0.0"'),
            ("package.json", PACKAGE_JSON, '"test": "jest"', '"test": "jest --ci", "lint": "eslint ."'),
            ("package.json", PACKAGE_JSON, ',\n  "devDependencies": {"jest": "^29.0.0"}', ""),
            ("pyproject.toml", PYPROJECT, 'version = "0.1.0"', 'version = "0.2.0"'),
            ("requirements.txt", REQUIREMENTS, "rich>=13\n", ""),
            ("requirements.txt", REQUIREMENTS, "httpx==0.27.0", "HTTPX==0.28.1"),
            ("README.md", "lodash\n", "lodash", "lodash express"),
        ]
        for rel, text, old, new in cases:
            with self.subTest(rel=rel, new=new):
                self.write(rel, text)
                self.assert_silent(self.edit(rel, old, new))
        self.assertEqual(self.env.log_lines(), [])

    GO_MOD = ("module example.com/app\n\ngo 1.22\n\nrequire github.com/a/one v1.0.0\n\n"
              "require golang.org/x/text v0.14.0 // indirect\n")
    PIP_COMPILE = ("#\n# This file is autogenerated by pip-compile with Python 3.11\n#\nidna==3.7\n    # via requests\n"
                   "requests==2.32.3\n    # via -r requirements.in\n")

    def test_transitive_becoming_direct_passes(self):
        # `go mod tidy` после импорта снимает `// indirect`; снятый заголовок оставляет пакеты, что уже были.
        self.write("go.mod", self.GO_MOD)
        self.assert_silent(self.edit("go.mod", " // indirect", ""))
        self.write("requirements.txt", self.PIP_COMPILE)
        self.assert_silent(self.tool("Write", {"file_path": str(self.env.project / "requirements.txt"),
                                               "content": "idna==3.7\nrequests==2.32.3\n"}))
        r = self.tool("Write", {"file_path": str(self.env.project / "requirements.txt"),
                                "content": "idna==3.7\nrequests==2.32.3\nflask\n"})
        self.assert_denied(r, "flask")

    def test_generated_header_does_not_disable_check(self):
        # Заголовок генератора и аннотацию `# via` агент впишет сам: файл с ними проверяется по содержимому.
        self.write("requirements.txt", REQUIREMENTS)
        header = "# This file is autogenerated by pip-compile with Python 3.11\n"
        r = self.tool("Write", {"file_path": str(self.env.project / "requirements.txt"),
                                "content": header + REQUIREMENTS + "flask==3.0\n    # via -r requirements.in\n"})
        self.assert_denied(r, "flask")
        r = self.edit("requirements.txt", "httpx==0.27.0", "# This file is @generated by PDM.\nhttpx==0.27.0\nflask")
        self.assert_denied(r, "flask")
        self.write("requirements.txt", self.PIP_COMPILE)
        r = self.edit("requirements.txt", "requests==2.32.3\n", "requests==2.32.3\nurllib3==2.2\n    # via requests\n")
        self.assert_denied(r, "urllib3")

    def test_crlf_manifest_matched_like_tool(self):
        # Инструмент сопоставляет old_string с текстом, где CRLF приведены к LF.
        self.env.project.joinpath("package.json").write_bytes(PACKAGE_JSON.replace("\n", "\r\n").encode())
        r = self.edit("package.json", '"react": "^18.2.0"},\n  "devDependencies"',
                      '"react": "^18.2.0", "axios": "^1"},\n  "devDependencies"')
        self.assert_denied(r, "axios")
        self.write("requirements.txt", REQUIREMENTS.replace("\n", "\r\n"))
        r = self.tool("MultiEdit", {"file_path": str(self.env.project / "requirements.txt"), "edits": [
            {"old_string": "httpx==0.27.0\nrich>=13\n", "new_string": "httpx==0.27.0\nrich>=13\nflask\n"}]})
        self.assert_denied(r, "flask")

    def test_installed_packages_dirs_not_watched(self):
        for rel in (".venv/lib/python3.12/site-packages/pandas/pyproject.toml",
                    "env/lib/python3.12/site-packages/pandas/pyproject.toml",
                    "vendor/guzzlehttp/guzzle/composer.json", "target/debug/build/x/Cargo.toml"):
            with self.subTest(rel):
                kind_file = rel.rsplit("/", 1)[-1]
                content = {"pyproject.toml": '[project]\ndependencies = ["numpy"]\n',
                           "composer.json": '{"require": {"psr/http-message": "^1"}}',
                           "Cargo.toml": '[dependencies]\nserde = "1"\n'}[kind_file]
                self.assert_silent(self.tool("Write", {"file_path": str(self.env.project / rel), "content": content}))

    def test_workspace_member_not_external(self):
        # npm workspaces: имя пакета самого проекта — не внешняя зависимость.
        self.write("package.json", '{"name": "root", "workspaces": ["packages/*"], "dependencies": {}}')
        self.write("packages/utils/package.json", '{"name": "@acme/utils", "version": "1.0.0"}')
        r = self.edit("package.json", '"dependencies": {}', '"dependencies": {"@acme/utils": "*"}')
        self.assert_silent(r)
        r = self.edit("package.json", '"dependencies": {}', '"dependencies": {"@acme/utils": "*", "axios": "^1"}')
        reason = self.assert_denied(r, "axios")
        self.assertNotIn("@acme/utils", reason)

    def test_name_declared_in_other_manifest_not_new(self):
        # Имя, уже объявленное в другом манифесте того же вида, — не новое для проекта.
        self.write("requirements.txt", REQUIREMENTS)
        r = self.tool("Write", {"file_path": str(self.env.project / "svc" / "requirements.txt"),
                                "content": "httpx==0.27.0\n"})
        self.assert_silent(r)
        # Имя из манифеста другого вида не засчитывается.
        r = self.tool("Write", {"file_path": str(self.env.project / "svc" / "package.json"),
                                "content": '{"dependencies": {"httpx": "1"}}'})
        self.assert_denied(r, "httpx")

    def test_project_scan_failure_warns(self):
        self.write("requirements.txt", REQUIREMENTS)
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 0):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Edit", tool_input={
                    "file_path": str(self.env.project / "requirements.txt"), "old_string": "rich>=13",
                    "new_string": "rich>=13\nflask"}))
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("манифестов больше 0", out["systemMessage"])
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "skipped"))

    def test_hard_link_not_matched_warns(self):
        # Жёсткая ссылка notes.json на манифест: манифесты проекта не перечислены — пропуск с предупреждением.
        self.write("requirements.txt", REQUIREMENTS)
        os.link(self.env.project / "requirements.txt", self.env.project / "notes.json")
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 0):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Edit", tool_input={
                    "file_path": str(self.env.project / "notes.json"), "old_string": "rich>=13",
                    "new_string": "rich>=13\nflask"}))
            self.assertEqual(set(out), {"systemMessage"})
            self.assertIn("жёсткая ссылка не сверена с манифестами проекта: манифестов больше 0", out["systemMessage"])
            last = self.env.log_lines()[-1]
            self.assertEqual((last["hook"], last["verdict"]), ("manifest", "skipped"))
            # Предупреждение — раз на сессию (жёсткие ссылки pnpm в node_modules), пропуск в журнале — на каждую правку.
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Edit", tool_input={
                    "file_path": str(self.env.project / "notes.json"), "old_string": "rich>=13",
                    "new_string": "rich>=13\nflask"}))
        self.assertIsNone(out)
        logged = [(e["hook"], e["verdict"]) for e in self.env.log_lines()]
        self.assertEqual(logged, [("manifest", "skipped")] * 2)

    def test_hard_link_named_as_other_manifest_denied(self):
        # Жёсткая ссылка sub/Gemfile на package.json: файл проверяется и как package.json.
        self.write("package.json", PACKAGE_JSON)
        (self.env.project / "sub").mkdir()
        os.link(self.env.project / "package.json", self.env.project / "sub" / "Gemfile")
        r = self.tool("Write", {"file_path": str(self.env.project / "sub" / "Gemfile"),
                                "content": PACKAGE_JSON.replace('"react"', '"left-pad": "1", "react"', 1)})
        self.assert_denied(r, "left-pad", str(self.env.project / "package.json"))

    def test_hard_link_targets_beyond_budget_skipped(self):
        # Второй манифест того же файла не укладывается в LINKED_BUDGET: пропуск с предупреждением, первый проверен.
        self.write("package.json", PACKAGE_JSON)
        (self.env.project / "sub").mkdir()
        os.link(self.env.project / "package.json", self.env.project / "sub" / "Gemfile")
        content = PACKAGE_JSON.replace('"react"', '"left-pad": "1", "react"', 1)
        with mock.patch.object(judge_tool, "LINKED_BUDGET", 0):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Write", tool_input={
                    "file_path": str(self.env.project / "package.json"), "content": content}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("left-pad", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(out["systemMessage"], f"planka: манифест {self.env.project / 'sub' / 'Gemfile'}: жёсткие "
                                               f"ссылки не проверены за срок, новые зависимости не проверены")

    def test_hard_link_listing_shared(self):
        # Перечень манифестов для жёсткой ссылки и подключения — один за правку (манифесты проекта здесь не читаются).
        self.write("requirements.txt", REQUIREMENTS)
        os.link(self.env.project / "requirements.txt", self.env.project / "notes.txt")
        calls = []
        real = manifest_watch.list_manifests

        def counted(*args):
            calls.append(args)
            return real(*args)
        with mock.patch.object(manifest_watch, "list_manifests", side_effect=counted), \
                mock.patch.object(judge_tool, "_project_names", return_value=frozenset()):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Write", tool_input={
                    "file_path": str(self.env.project / "notes.txt"), "content": REQUIREMENTS + "-r extra.list\n"}))
        self.assertIn("include extra.list", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(calls), 1, calls)

    def test_hard_link_to_plain_file_silent(self):
        self.write("notes.json", "{}")
        os.link(self.env.project / "notes.json", self.env.project / "notes2.json")
        self.assert_silent(self.tool("Write", {"file_path": str(self.env.project / "notes.json"),
                                               "content": '{"dependencies": {"evil": "1"}}'}))

    def test_large_manifest_warns(self):
        self.write("requirements.txt", REQUIREMENTS)
        with mock.patch.object(manifest_watch, "MAX_MANIFEST_BYTES", len(REQUIREMENTS) - 1):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Edit", tool_input={
                    "file_path": str(self.env.project / "requirements.txt"), "old_string": "rich>=13",
                    "new_string": "rich>=13\nflask"}))
        self.assertIn(f"больше {len(REQUIREMENTS) - 1} байт", out["systemMessage"])

    def test_same_registry_other_kind_not_new(self):
        # requirements и pyproject — один реестр PyPI: перенос зависимостей между ними не новые имена.
        self.write("requirements.txt", REQUIREMENTS)
        r = self.tool("Write", {"file_path": str(self.env.project / "pyproject.toml"),
                                "content": '[project]\nname = "app"\ndependencies = ["HTTPX>=0.27", "rich"]\n'})
        self.assert_silent(r)

    def test_standard_build_backend_not_new(self):
        # `uv init --lib` и setuptools: бэкенд сборки — не новая зависимость; другой пакет в requires — новая.
        for requires, backend in (('["uv_build>=0.8.3,<0.9.0"]', "uv_build"), ('["setuptools>=61", "wheel"]',
                                                                                "setuptools.build_meta")):
            with self.subTest(requires):
                content = (f'[project]\nname = "lib"\nversion = "0.1.0"\ndependencies = []\n\n'
                           f'[build-system]\nrequires = {requires}\nbuild-backend = "{backend}"\n')
                self.assert_silent(self.tool("Write", {"file_path": str(self.env.project / "pyproject.toml"),
                                                       "content": content}))
        r = self.tool("Write", {"file_path": str(self.env.project / "pyproject.toml"),
                                "content": '[build-system]\nrequires = ["evil-backend"]\nbuild-backend = "evil"\n'})
        self.assert_denied(r, "evil-backend")

    def test_empty_old_string_fills_empty_file(self):
        self.write("requirements.txt", "")
        self.assert_denied(self.edit("requirements.txt", "", "flask\n"), "flask")

    def test_edit_that_tool_rejects_passes(self):
        # old_string не найден или неоднозначен без replace_all — инструмент сам упадёт.
        self.write("requirements.txt", "rich\nrich\n")
        self.assert_silent(self.edit("requirements.txt", "absent", "flask"))
        self.assert_silent(self.edit("requirements.txt", "rich", "flask"))
        self.assert_silent(self.tool("MultiEdit", {"file_path": str(self.env.project / "requirements.txt"),
                                                   "edits": [{"old_string": "absent", "new_string": "flask"}]}))

    def test_broken_json_warns(self):
        self.write("package.json", PACKAGE_JSON)
        r = self.tool("Write", {"file_path": str(self.env.project / "package.json"),
                                "content": '{"dependencies": {"left-pad": "1"'})
        self.assertIsNone(output(r))
        msgs = messages(r)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("не разобран", msgs[0])
        self.assertIn("package.json", msgs[0])
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "skipped"))
        assert_not_logged(self, self.env, "left-pad")

    def test_broken_old_without_git_compared_as_empty(self):
        # До правки не разобран, версии в git нет — сравнение с пустым манифестом.
        self.write("package.json", "{broken")
        r = self.tool("Write", {"file_path": str(self.env.project / "package.json"), "content": PACKAGE_JSON})
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(messages(r), [])

    def test_fixture_manifest_passes(self):
        r = self.tool("Write", {"file_path": str(self.env.project / "tests" / "fixtures" / "x" / "package.json"),
                                "content": PACKAGE_JSON})
        self.assert_silent(r)

    def test_not_limited_by_budget(self):
        self.write("requirements.txt", REQUIREMENTS)
        fill_budget(self.env, "manifest")
        for _ in range(3):
            r = self.edit("requirements.txt", "rich>=13", "rich>=13\nflask")
            self.assert_denied(r, "flask")

    def test_deny_survives_log_failure(self):
        self.write("requirements.txt", REQUIREMENTS)
        (self.env.data / "judge.log").mkdir()
        r = self.edit("requirements.txt", "rich>=13", "rich>=13\nflask")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(any("внутренняя ошибка" in m for m in messages(r)))

    def test_barrier(self):
        self.write("requirements.txt", REQUIREMENTS)
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Edit", tool_input={
                "file_path": str(self.env.project / "requirements.txt"), "old_string": "rich>=13",
                "new_string": "flask"}), PLANKA_JUDGE="1")
        self.assert_silent(r)
        self.assertEqual(self.env.log_lines(), [])

    def test_garbage_tool_input_passes(self):
        for ti in ({}, {"file_path": None}, {"file_path": str(self.env.project / "requirements.txt"),
                                               "content": 5}, {"file_path": "a\0/requirements.txt"}):
            with self.subTest(ti=ti):
                self.assert_silent(self.tool("Write", ti))
        self.assert_silent(self.tool("MultiEdit", {"file_path": str(self.env.project / "requirements.txt"),
                                                   "edits": "x"}))


@unittest.skipUnless(shutil.which("git"), "нет git")
class ManifestEditGitTest(unittest.TestCase):
    """Версия манифеста в HEAD — тоже база: имя оттуда не новое."""

    def setUp(self):
        self.env = Env()
        self.p = self.env.project
        git("init", "-q", cwd=self.p)
        (self.p / "package.json").write_text(PACKAGE_JSON, encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "i", cwd=self.p)

    def tearDown(self):
        self.env.close()

    def write_tool(self, content):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Write", tool_input={"file_path": str(self.p / "package.json"),
                                                         "content": content}))

    def test_broken_old_compared_with_head(self):
        (self.p / "package.json").write_text("{broken", encoding="utf-8")
        r = self.write_tool(PACKAGE_JSON)
        self.assertEqual(r.stdout, "", r.stderr)
        r = self.write_tool(PACKAGE_JSON.replace('"react": "^18.2.0"', '"react": "^18.2.0", "axios": "^1"'))
        self.assertIn("axios", output(r)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_restore_from_head_passes(self):
        (self.p / "package.json").unlink()
        self.assertEqual(self.write_tool(PACKAGE_JSON).stdout, "")
        (self.p / "package.json").write_text(PACKAGE_JSON.replace(',\n  "devDependencies": {"jest": "^29.0.0"}', ""),
                                             encoding="utf-8")
        self.assertEqual(self.write_tool(PACKAGE_JSON).stdout, "")


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


class ManifestBashTest(unittest.TestCase):
    """Bash: снимок имён манифестов в PreToolUse, сравнение в PostToolUse и PostToolUseFailure."""

    use_git = False

    def setUp(self):
        self.env = Env()
        self.p = self.env.project
        (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
        (self.p / "web").mkdir()
        (self.p / "web" / "package.json").write_text(PACKAGE_JSON, encoding="utf-8")
        if self.use_git:
            git("init", "-q", cwd=self.p)
            git("add", ".", cwd=self.p)
            git("commit", "-qm", "i", cwd=self.p)

    def tearDown(self):
        self.env.close()

    def state(self):
        p = self.env.data / "state" / "sess-1.manifests.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def run_command(self, command, event="PostToolUse", tool_use_id="b1", effect=None, post_cwd=None, **extra):
        """Pre, сама команда (или effect вместо неё — для менеджеров, которых нет на машине) в каталоге
        проекта, Post с cwd post_cwd (по умолчанию проект); ответ Post."""
        pre = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id=tool_use_id), **extra)
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.pre = pre
        subprocess.run(command if effect is None else effect, shell=True, cwd=self.p, capture_output=True)
        fields = {"tool_response": {"stdout": "", "stderr": ""}} if event == "PostToolUse" else {
            "error": "Exit code 1", "is_interrupt": False}
        if post_cwd is not None:
            fields["cwd"] = str(post_cwd)
        return self.env.run("judge_tool.py", self.env.hook_input(
            event, tool_name="Bash", tool_input={"command": command}, tool_use_id=tool_use_id, **fields), **extra)

    # Генераторы requirements без заголовка: хук не знает, выполнился ли генератор и писал ли он файл последним,
    # поэтому файл проверяется по содержимому, как прочие манифесты.
    GENERATORS = ("pip freeze > requirements.txt", "python3 -m pip freeze >requirements.txt",
                  "poetry export -f requirements.txt -o requirements.txt",
                  "poetry export -f requirements.txt > requirements.txt",
                  "pipenv requirements > requirements.txt",
                  "uv export --no-header --no-annotate -o requirements.txt",
                  "uv pip freeze > requirements.txt", "pdm export --output=requirements.txt",
                  "pip freeze | tee requirements.txt")
    GENERATED = "httpx==0.27.0\nrich==13.7.1\nanyio==4.4.0\ncertifi==2024.7.4\n"

    def test_generators_blocked(self):
        for command in self.GENERATORS:
            with self.subTest(command=command):
                (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
                r = self.run_command(command, effect=f"printf '{self.GENERATED}' > requirements.txt")
                self.assert_blocked(r, "requirements.txt: anyio, certifi")

    HEADED = "# This file was autogenerated by uv via the following command:\n" + GENERATED.replace(
        "anyio==4.4.0\n", "anyio==4.4.0\n    # via httpx\n")

    def test_generated_header_does_not_disable_check(self):
        # Заголовок генератора или `# via` в файле не снимает проверку: их пишет и агент.
        for command in ("uv export -o requirements.txt", f"printf '{self.HEADED}' > requirements.txt"):
            with self.subTest(command=command):
                (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
                r = self.run_command(command, effect=f"printf '{self.HEADED}' > requirements.txt")
                self.assert_blocked(r, "requirements.txt: anyio, certifi")

    def test_generated_header_with_marker_not_blocked(self):
        r = self.run_command(f"PLANKA_DEP_OK=1 printf '{self.HEADED}' > requirements.txt")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.state(), {})
        self.assertIn("anyio", (self.p / "requirements.txt").read_text(encoding="utf-8"))

    def test_generator_with_marker_not_blocked(self):
        r = self.run_command("PLANKA_DEP_OK=1 pip freeze > requirements.txt",
                             effect=f"printf '{self.GENERATED}' > requirements.txt")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.state(), {})

    def test_generator_and_handwritten_append_both_blocked(self):
        command = "pip freeze > requirements.txt && printf 'click\\n' >> requirements-dev.txt"
        r = self.run_command(command, effect=f"printf '{self.GENERATED}' > requirements.txt && "
                                             "printf 'click\\n' >> requirements-dev.txt")
        self.assert_blocked(r, "requirements-dev.txt: click", "anyio")

    def assert_blocked(self, r, *names):
        out = output(r)
        self.assertEqual(out["decision"], "block", r.stdout)
        reason = out["reason"]
        self.assertTrue(reason.startswith("planka: "), reason)
        self.assertIn("вопрос автору", reason)
        self.assertIn(str(self.env.root / "rules" / "dependencies.md"), reason)
        self.assertIn("PLANKA_DEP_OK=1", reason)
        for name in names:
            self.assertIn(name, reason)
        # Текст не велит откатывать вслепую: команда могла вернуть работу автора.
        self.assertNotIn("Откати", reason)
        self.assertIn("пожалуйста, откатывайте только свою правку", reason)
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "block-dep"))
        self.assertIsInstance(last["manifests"], int)
        self.assertNotIn("files", last)
        self.assertEqual(self.state(), {})
        return reason

    def test_python_append_blocked(self):
        command = f"python3 -c \"open('requirements.txt', 'a').write('{SECRET_NAME}\\n')\""
        r = self.run_command(command)
        self.assertEqual(self.pre.stdout, "", self.pre.stderr)
        self.assert_blocked(r, SECRET_NAME, "requirements.txt")
        self.assertEqual(self.env.log_lines()[-1]["manifests"], 1)
        assert_not_logged(self, self.env, SECRET_NAME)

    def test_printf_append_blocked(self):
        self.assert_blocked(self.run_command("printf 'flask\\n' >> requirements.txt"), "flask")

    def test_json_edit_in_subdirectory_blocked(self):
        command = ("python3 -c \"import json; p='web/package.json'; d=json.load(open(p)); "
                   "d['dependencies']['axios']='^1'; json.dump(d, open(p, 'w'))\"")
        self.assert_blocked(self.run_command(command), "axios", "web/package.json")

    def test_new_manifest_file_blocked(self):
        self.assert_blocked(self.run_command("printf 'click\\n' > requirements-dev.txt"), "click")

    def test_failure_event_blocked(self):
        self.assert_blocked(self.run_command("printf 'flask\\n' >> requirements.txt; false",
                                             event="PostToolUseFailure"), "flask")

    def test_marker_skips_check(self):
        r = self.run_command("PLANKA_DEP_OK=1 printf 'flask\\n' >> requirements.txt")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.state(), {})
        self.assertEqual(self.env.log_lines(), [])

    def test_marker_only_as_command_assignment(self):
        # Маркер в комментарии или аргументом другой команды проверку не снимает.
        for command in ("printf 'flask\\n' >> requirements.txt # PLANKA_DEP_OK=1",
                        "echo PLANKA_DEP_OK=1; printf 'flask\\n' >> requirements.txt"):
            with self.subTest(command=command):
                (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
                self.assert_blocked(self.run_command(command), "flask")

    def test_transitive_becoming_direct_silent(self):
        (self.p / "go.mod").write_text(ManifestEditTest.GO_MOD, encoding="utf-8")
        (self.p / "requirements-lock.txt").write_text(ManifestEditTest.PIP_COMPILE, encoding="utf-8")
        if self.use_git:
            git("add", ".", cwd=self.p)
            git("commit", "-qm", "go", cwd=self.p)
        r = self.run_command("python3 -c \"import pathlib; p=pathlib.Path('go.mod'); "
                             "p.write_text(p.read_text().replace(' // indirect', ''))\"")
        self.assertEqual(r.stdout, "", r.stderr)
        r = self.run_command("printf 'idna==3.7\\nrequests==2.32.3\\n' > requirements-lock.txt")
        self.assertEqual(r.stdout, "", r.stderr)

    def test_installed_packages_dirs_not_watched(self):
        # Окружение без .gitignore: pip кладёт манифесты пакетов в site-packages.
        command = ("mkdir -p venv2/lib/python3.12/site-packages/pandas vendor/acme/x && "
                   "printf '[project]\\ndependencies = [\"numpy\"]\\n' > venv2/lib/python3.12/site-packages/pandas/pyproject.toml && "
                   "printf '{\"require\": {\"psr/log\": \"1\"}}' > vendor/acme/x/composer.json")
        r = self.run_command(command)
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertTrue((self.p / "vendor/acme/x/composer.json").exists())

    def test_workspace_member_not_external(self):
        # Член workspace корня ставится из каталога по имени; имя свежего манифеста вне workspace — внешнее.
        (self.p / "package.json").write_text('{"name": "root", "workspaces": ["packages/*"]}', encoding="utf-8")
        command = ("mkdir -p packages/utils && printf '{\"name\": \"@acme/utils\"}' > packages/utils/package.json && "
                   "python3 -c \"import json; p='web/package.json'; d=json.load(open(p)); "
                   "d['dependencies']['@acme/utils']='*'; json.dump(d, open(p, 'w'))\"")
        r = self.run_command(command)
        self.assertEqual(r.stdout, "", r.stderr)

    def test_existing_workspace_member_not_external(self):
        # Член workspace есть до команды; команда меняет только зависимый манифест.
        (self.p / "package.json").write_text('{"name": "root", "workspaces": ["packages/*"]}', encoding="utf-8")
        (self.p / "packages" / "b").mkdir(parents=True)
        (self.p / "packages" / "b" / "package.json").write_text('{"name": "pkg-b"}', encoding="utf-8")
        command = ("python3 -c \"import json; p='web/package.json'; d=json.load(open(p)); "
                   "d['dependencies']['pkg-b']='*'; json.dump(d, open(p, 'w'))\"")
        r = self.run_command(command)
        self.assertEqual(r.stdout, "", r.stderr)

    def test_requirements_moved_to_pyproject_not_new(self):
        command = ("printf '[project]\\nname = \"app\"\\ndependencies = [\"httpx\", \"rich\"]\\n' > pyproject.toml"
                   " && rm requirements.txt")
        r = self.run_command(command)
        self.assertTrue((self.p / "pyproject.toml").exists())
        self.assertEqual(r.stdout, "", r.stderr)

    def test_move_and_copy_not_new(self):
        for command in ("mkdir -p app && mv web/package.json app/package.json",
                        "mkdir -p web2 && cp web/package.json web2/package.json",
                        "printf 'httpx\\n' > requirements-dev.txt"):
            with self.subTest(command=command):
                r = self.run_command(command)
                self.assertEqual(r.stdout, "", r.stderr)

    def test_unchanged_manifests_silent(self):
        for command in ("ls", "printf 'x' > notes.txt", "printf 'rich>=14\\n' > requirements.txt",
                        "rm web/package.json"):
            with self.subTest(command=command):
                r = self.run_command(command)
                self.assertEqual(self.pre.stdout, "", self.pre.stderr)
                self.assertEqual(r.stdout, "", r.stderr)
                self.assertEqual(self.state(), {})
        self.assertEqual(self.env.log_lines(), [])

    def test_snapshot_holds_names_not_content(self):
        self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        dumped = json.dumps(self.state()["b1"])
        for name in ("httpx", "rich", "react", "jest", "web/package.json"):
            self.assertIn(name, dumped)
        for content in ("0.27.0", "^18.2.0", "1.0.0", "scripts"):
            self.assertNotIn(content, dumped)

    def mcp_call(self, tool="mcp__filesystem__write_file", event="PostToolUse", edit=True, tool_use_id="m1"):
        """Pre, правка requirements.txt (если edit) вне хука, как делает MCP-инструмент, Post; ответ Post."""
        tool_input = {"path": "requirements.txt", "content": "flask\n"}
        pre = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=tool_use_id))
        self.assertEqual(pre.stdout, "", pre.stderr)
        if edit:
            with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
                f.write("flask\n")
        fields = {"tool_response": {}} if event == "PostToolUse" else {"error": "boom", "is_interrupt": False}
        return self.env.run("judge_tool.py", self.env.hook_input(
            event, tool_name=tool, tool_input=tool_input, tool_use_id=tool_use_id, **fields))

    def test_mcp_tool_manifest_edit_blocked(self):
        for tool in ("mcp__filesystem__write_file", "mcp__serena__replace_content"):
            for event in ("PostToolUse", "PostToolUseFailure"):
                with self.subTest(tool=tool, event=event):
                    (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
                    r = self.mcp_call(tool, event)
                    self.assert_blocked(r, "requirements.txt: flask")
                    self.assertIn(tool, output(r)["reason"])
                    self.assertEqual(self.state(), {})

    def test_mcp_tool_without_manifest_change_silent(self):
        r = self.mcp_call(edit=False)
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.state(), {})

    def test_mcp_pre_takes_snapshot_by_tool_use_id(self):
        self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="mcp__serena__find_symbol", tool_input={}, tool_use_id="m1"))
        self.assertEqual(sorted(self.state()), ["m1"])

    def test_broken_manifest_after_command_warns(self):
        r = self.run_command("printf '{broken' > web/package.json")
        self.assertIsNone(output(r))
        msgs = messages(r)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("web/package.json", msgs[0])
        self.assertIn("не разобран", msgs[0])

    def test_post_without_snapshot_silent(self):
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "printf 'flask\\n' >> requirements.txt"},
            tool_use_id="none"))
        self.assertEqual(r.stdout, "", r.stderr)

    def test_denied_dependency_command_takes_no_snapshot(self):
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "npm install left-pad"}, tool_use_id="b1"))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state(), {})

    def test_parallel_commands_keyed_by_tool_use(self):
        for tid in ("b1", "b2"):
            self.env.run("judge_tool.py", self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id=tid))
        self.assertEqual(sorted(self.state()), ["b1", "b2"])
        self.env.run("judge_tool.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        self.assertEqual(sorted(self.state()), ["b2"])

    def test_barrier(self):
        r = self.run_command("printf 'flask\\n' >> requirements.txt", PLANKA_JUDGE="1")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.state(), {})

    def test_marker_command_takes_no_snapshot(self):
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "PLANKA_DEP_OK=1 printf 'flask\\n' >> requirements.txt"},
            tool_use_id="b1"))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.state(), {})

    def test_block_survives_log_failure(self):
        pre = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "printf 'flask\\n' >> requirements.txt"},
            tool_use_id="b1"))
        self.assertEqual(pre.stdout, "", pre.stderr)
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write("flask\n")
        (self.env.data / "judge.log").mkdir()
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "printf 'flask\\n' >> requirements.txt"},
            tool_use_id="b1", tool_response={"stdout": "", "stderr": ""}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(output(r)["decision"], "block")
        self.assertIn("flask", output(r)["reason"])
        self.assertTrue(any("внутренняя ошибка" in m for m in messages(r)), messages(r))

    def test_unparsed_manifest_without_version_compared_as_empty(self):
        # До команды не разобран, версии в репозитории нет — сравнение с пустым манифестом.
        (self.p / "api").mkdir()
        (self.p / "api" / "package.json").write_text("{broken", encoding="utf-8")
        r = self.run_command("printf '{\"dependencies\": {\"axios\": \"1\"}}' > api/package.json")
        self.assertEqual(output(r)["decision"], "block")
        self.assertIn("api/package.json: axios", output(r)["reason"])
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "block-dep"))

    @staticmethod
    def pyproject_command(deps):
        """Команда, которая пишет pyproject.toml с зависимостями deps."""
        return ("printf '[project]\\nname = \"app\"\\ndependencies = [%s]\\n' > pyproject.toml"
                % ", ".join(f'\\"{d}\\"' for d in deps))

    def test_uncommitted_legacy_source_names_new(self):
        # setup.py, setup.cfg, Pipfile рабочего дерева (вне git — любые, в git — не в HEAD) не проверяются,
        # и их имена не известные: перенос из них в pyproject.toml — новые имена.
        for name, (text, moved) in LEGACY_SOURCES.items():
            for rel in (name, f"scratch/{name}"):
                with self.subTest(path=rel):
                    (self.p / "pyproject.toml").unlink(missing_ok=True)
                    (self.p / rel).parent.mkdir(exist_ok=True)
                    (self.p / rel).write_text(text, encoding="utf-8")
                    r = self.run_command(self.pyproject_command(moved) + f" && rm {rel}")
                    self.assertFalse((self.p / rel).exists())
                    self.assert_blocked(r, "pyproject.toml: " + ", ".join(sorted(
                        m.split(">")[0].lower() for m in moved)))


@unittest.skipUnless(shutil.which("git"), "нет git")
class ManifestBashGitTest(ManifestBashTest):
    """Те же сценарии в git; имена из HEAD не новые: checkout ветки с добавленной автором зависимостью проходит."""

    use_git = True

    def test_checkout_of_committed_dependency_passes(self):
        git("checkout", "-qb", "feature", cwd=self.p)
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write("flask\n")
        git("commit", "-qam", "flask", cwd=self.p)
        git("checkout", "-q", "-", cwd=self.p)
        r = self.run_command("git checkout -q feature")
        self.assertEqual((self.p / "requirements.txt").read_text(encoding="utf-8"), REQUIREMENTS + "flask\n")
        self.assertEqual(r.stdout, "", r.stderr)

    def test_git_mv_not_new(self):
        r = self.run_command("mkdir -p app && git mv web/package.json app/package.json")
        self.assertTrue((self.p / "app/package.json").exists())
        self.assertEqual(r.stdout, "", r.stderr)

    def branch_with(self, name, line):
        """Ветка name: requirements.txt с дописанной строкой line и notes.txt, конфликтующий с текущей веткой."""
        (self.p / "notes.txt").write_text("base\n", encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "notes", cwd=self.p)
        git("checkout", "-qb", name, cwd=self.p)
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write(line)
        (self.p / "notes.txt").write_text("theirs\n", encoding="utf-8")
        git("commit", "-qam", name, cwd=self.p)
        git("checkout", "-q", "-", cwd=self.p)
        (self.p / "notes.txt").write_text("ours\n", encoding="utf-8")
        git("commit", "-qam", "ours", cwd=self.p)

    def test_conflicted_merge_and_cherry_pick_pass(self):
        # Имена из MERGE_HEAD и CHERRY_PICK_HEAD при конфликте — уже в репозитории.
        for i, op in enumerate(("merge", "cherry-pick")):
            with self.subTest(op):
                self.branch_with(f"feat{i}", f"pkg{i}\n")
                r = self.run_command(f"git -c user.email=t@t -c user.name=t {op} feat{i}")
                self.assertIn(f"pkg{i}", (self.p / "requirements.txt").read_text(encoding="utf-8"))
                self.assertEqual(r.stdout, "", r.stderr)
                git(op, "--abort", cwd=self.p)

    def test_conflicted_revert_passes(self):
        # Отменяемая правка убрала пакет: revert возвращает его из версии до неё (REVERT_HEAD^).
        (self.p / "notes.txt").write_text("base\n", encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "notes", cwd=self.p)
        (self.p / "requirements.txt").write_text("httpx==0.27.0\n", encoding="utf-8")
        (self.p / "notes.txt").write_text("drop rich\n", encoding="utf-8")
        git("commit", "-qam", "drop", cwd=self.p)
        (self.p / "notes.txt").write_text("later\n", encoding="utf-8")
        git("commit", "-qam", "later", cwd=self.p)
        r = self.run_command("git -c user.email=t@t -c user.name=t revert --no-edit HEAD~1")
        self.assertIn("rich", (self.p / "requirements.txt").read_text(encoding="utf-8"))
        self.assertEqual(r.stdout, "", r.stderr)

    # Начало сессии в state/sess-1.start.json; коммиты автора — до него, агента — после.
    START = int(time.time()) - 1000
    BEFORE = START - 500
    AFTER = START + 10

    def setUp(self):
        super().setUp()
        state = self.env.data / "state"
        state.mkdir(exist_ok=True)
        (state / "sess-1.start.json").write_text(json.dumps({"start": self.START}), encoding="utf-8")

    def git_at(self, ts, *args):
        """git с датами автора и коммиттера ts."""
        date = f"@{ts} +0000"
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=self.p, check=True,
                       capture_output=True, env={**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date})

    def stash_with(self, ts, line, *flags):
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write(line)
        self.git_at(ts, "stash", "-q", *flags)

    def branch_at(self, ts, name, line):
        """Ветка name с коммитом от ts: requirements.txt с дописанной строкой line; текущая ветка не меняется."""
        git("checkout", "-qb", name, cwd=self.p)
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write(line)
        self.git_at(ts, "commit", "-qam", name)
        git("checkout", "-q", "-", cwd=self.p)

    def test_stash_after_session_start_blocked_with_safe_reason(self):
        # stash, сделанный в сессии, мог заложить агент: блок, текст не велит откатывать.
        self.stash_with(self.AFTER, "flask\n")
        self.assert_blocked(self.run_command("git stash pop -q"), "flask")

    def test_old_patch_by_relative_path_from_subdir_passes(self):
        # Патч автора старше начала сессии, команда из подкаталога с относительным путём: путь — от cwd команды
        # (git apply в подкаталоге меняет только файлы под ним).
        sub = self.p / "sub"
        sub.mkdir()
        (sub / "requirements.txt").write_text("httpx==0.27.0\n", encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "sub", cwd=self.p)
        with open(sub / "requirements.txt", "a", encoding="utf-8") as f:
            f.write("flask\n")
        patch = subprocess.run(["git", "diff"], cwd=self.p, capture_output=True, check=True).stdout
        git("checkout", "--", "sub/requirements.txt", cwd=self.p)
        (self.p / "author.patch").write_bytes(patch)
        # Начало сессии — после записи патча.
        (self.env.data / "state" / "sess-1.start.json").write_text(json.dumps({"start": int(time.time()) + 5}),
                                                                   encoding="utf-8")
        command = "git apply ../author.patch"
        for event, fields in (("PreToolUse", {}), ("PostToolUse", {"tool_response": {"stdout": "", "stderr": ""}})):
            if event == "PostToolUse":
                subprocess.run(["git", "apply", "../author.patch"], cwd=sub, check=True, capture_output=True)
            r = self.env.run("judge_tool.py", self.env.hook_input(event, tool_name="Bash", cwd=str(sub),
                                                                  tool_input={"command": command},
                                                                  tool_use_id="b1", **fields))
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("flask", (sub / "requirements.txt").read_text(encoding="utf-8"))
        self.assertEqual(r.stdout, "", r.stderr)

    def test_author_stash_before_session_passes(self):
        self.stash_with(self.BEFORE, "flask\n")
        r = self.run_command("git stash pop -q")
        self.assertIn("flask", (self.p / "requirements.txt").read_text(encoding="utf-8"))
        self.assertEqual(r.stdout, "", r.stderr)

    def test_stash_index_selects_ref(self):
        # stash@{1} автора старше сессии, stash@{0} агента моложе: время берётся у названного.
        self.stash_with(self.BEFORE, "flask\n")
        self.stash_with(self.AFTER, "django\n")
        for command in ("git stash apply 1", "git stash apply -q stash@{1}", "git stash apply --index 'stash@{1}'"):
            with self.subTest(command=command):
                git("checkout", "-q", "--", ".", cwd=self.p)
                r = self.run_command(command)
                self.assertIn("flask", (self.p / "requirements.txt").read_text(encoding="utf-8"))
                self.assertEqual(r.stdout, "", r.stderr)
        git("checkout", "-q", "--", ".", cwd=self.p)
        self.assert_blocked(self.run_command("git stash apply"), "django")

    def test_author_stash_untracked_manifest_passes(self):
        (self.p / "api").mkdir()
        (self.p / "api" / "requirements.txt").write_text("flask\n", encoding="utf-8")
        self.git_at(self.BEFORE, "stash", "-q", "-u")
        self.assertFalse((self.p / "api" / "requirements.txt").exists())
        r = self.run_command("git stash pop")
        self.assertTrue((self.p / "api" / "requirements.txt").exists())
        self.assertEqual(r.stdout, "", r.stderr)

    def test_restore_from_old_ref_passes(self):
        self.branch_at(self.BEFORE, "old", "flask\n")
        commands = ("git checkout old -- requirements.txt", "git checkout old requirements.txt",
                    "git restore --source=old requirements.txt", "git restore -s old -- requirements.txt",
                    "git restore --source old requirements.txt", "git merge --squash old",
                    "git -c user.email=t@t cherry-pick -n old", "git cherry-pick --no-commit old",
                    "cd web && git checkout old -- ../requirements.txt")
        for command in commands:
            with self.subTest(command=command):
                git("reset", "-q", "--hard", cwd=self.p)
                r = self.run_command(command)
                self.assertIn("flask", (self.p / "requirements.txt").read_text(encoding="utf-8"))
                self.assertEqual(r.stdout, "", r.stderr)

    def test_restore_from_young_ref_blocked(self):
        self.branch_at(self.AFTER, "feat", "flask\n")
        for command in ("git checkout feat -- requirements.txt", "git restore --source=feat requirements.txt",
                        "git merge --squash feat", "git cherry-pick -n feat"):
            with self.subTest(command=command):
                git("reset", "-q", "--hard", cwd=self.p)
                self.assert_blocked(self.run_command(command), "flask")

    def test_ref_in_session_start_second_not_old(self):
        # Время коммиттера — целые секунды: ref той же секунды, что начало сессии, мог создать агент.
        self.branch_at(self.START, "same", "flask\n")
        command = "git checkout same -- requirements.txt"
        self.assertEqual(manifest_watch.restored_names(self.p, command, self.START, time.monotonic() + 30), {})
        self.assertEqual(manifest_watch.restored_names(self.p, command, self.START + 1, time.monotonic() + 30),
                         {"pypi": ["flask", "httpx", "rich"], "npm": ["app", "jest", "react"]})

    def test_old_ref_without_session_start_blocked(self):
        # Начала сессии нет в состоянии (каталог данных был недоступен): время ref не с чем сравнить — блок.
        self.branch_at(self.BEFORE, "old", "flask\n")
        (self.env.data / "state" / "sess-1.start.json").unlink()
        with mock.patch.object(manifest_watch, "mark_start"):
            r = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "git checkout old -- requirements.txt"},
                tool_use_id="b1"))
        self.assertIsNone(r)
        git("checkout", "old", "--", "requirements.txt", cwd=self.p)
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "git checkout old -- requirements.txt"},
            tool_use_id="b1", tool_response={"stdout": "", "stderr": ""}))
        self.assert_blocked(r, "flask")

    def test_ignored_manifest_not_watched(self):
        (self.p / ".gitignore").write_text("vendor/\n", encoding="utf-8")
        r = self.run_command("mkdir -p vendor && printf 'flask\\n' > vendor/requirements.txt")
        self.assertEqual(r.stdout, "", r.stderr)

    def test_ref_age_is_committer_time_not_author_time(self):
        # Дата автора коммита в прошлом (`git commit --date`), коммиттер — в сессии: ref молодой.
        git("checkout", "-qb", "evil", cwd=self.p)
        with open(self.p / "requirements.txt", "a", encoding="utf-8") as f:
            f.write("flask\n")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "evil"], cwd=self.p,
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_DATE": f"@{self.BEFORE} +0000",
                            "GIT_COMMITTER_DATE": f"@{self.AFTER} +0000"})
        git("checkout", "-q", "-", cwd=self.p)
        self.assert_blocked(self.run_command("git checkout evil -- requirements.txt"), "flask")

    def test_committed_legacy_source_moved_to_pyproject_not_new(self):
        # Перенос из закоммиченного setup.py, setup.cfg, Pipfile в pyproject.toml одной командой: имена из HEAD.
        for name, (text, moved) in LEGACY_SOURCES.items():
            with self.subTest(name=name):
                (self.p / "pyproject.toml").unlink(missing_ok=True)
                (self.p / name).write_text(text, encoding="utf-8")
                git("add", name, cwd=self.p)
                git("commit", "-qm", name, cwd=self.p)
                r = self.run_command(self.pyproject_command(moved) + f" && git rm -q {name}")
                self.assertFalse((self.p / name).exists())
                self.assertEqual(r.stdout, "", r.stderr)
                r = self.run_command(self.pyproject_command([*moved, "evilpkg"]), tool_use_id="b2")
                reason = self.assert_blocked(r, "pyproject.toml: evilpkg")
                self.assertNotIn(moved[1].lower(), reason)
                git("commit", "-qm", "rm", cwd=self.p)

    def test_removed_manifest_names_in_head_not_new(self):
        # requirements.txt убран из индекса и дерева, его имена переходят в pyproject.toml следующей командой.
        r = self.run_command("git rm -q requirements.txt")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertFalse((self.p / "requirements.txt").exists())
        r = self.run_command("printf '[project]\\nname = \"app\"\\ndependencies = [\"httpx\", \"Rich>=13\"]\\n' > "
                             "pyproject.toml", tool_use_id="b2")
        self.assertEqual(r.stdout, "", r.stderr)
        r = self.run_command("printf '[project]\\nname = \"app\"\\ndependencies = [\"httpx\", \"evilpkg\"]\\n' > "
                             "pyproject.toml", tool_use_id="b3")
        self.assert_blocked(r, "pyproject.toml: evilpkg")

    def test_restored_names_deadline_and_limit_unavailable(self):
        self.branch_at(self.BEFORE, "old", "flask\n")
        command = "git checkout old -- requirements.txt"
        with self.assertRaisesRegex(manifest_watch.Unavailable, "за срок"):
            manifest_watch.restored_names(self.p, command, self.START, time.monotonic() - 1)
        # В дереве ref requirements.txt и web/package.json.
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 1), \
                self.assertRaisesRegex(manifest_watch.Unavailable, "больше 1"):
            manifest_watch.restored_names(self.p, command, self.START, time.monotonic() + 30)

    def test_restored_names_unreadable_tree_unavailable(self):
        # Дерево старого ref есть в коммите, но объекта дерева нет (повреждённый репозиторий): ls-tree не читает.
        self.branch_at(self.BEFORE, "old", "flask\n")
        tree = run_git(self.p, "rev-parse", "old^{tree}").stdout.strip()
        (self.p / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        with self.assertRaisesRegex(manifest_watch.Unavailable, f"дерево ref {tree} не прочитано"):
            manifest_watch.restored_names(self.p, "git checkout old -- requirements.txt", self.START,
                                          time.monotonic() + 30)

    def test_restored_names_timeout_skips_snapshot_with_warning(self):
        self.branch_at(self.BEFORE, "old", "flask\n")
        with mock.patch.object(manifest_watch, "_old_trees", side_effect=TimeoutError("не уложился в срок")):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "git checkout old -- requirements.txt"},
                tool_use_id="b1"))
        self.assertEqual(out, {"systemMessage": "planka: снимок манифестов не снят (имена ref команды не прочитаны "
                                                "за срок): новые зависимости от команд Bash и MCP-инструментов не проверяются"})
        self.assertEqual(self.state(), {})
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"]), ("manifest", "skipped"))


def run_git(cwd, *args, input=None):
    """git без проверки кода (команда с конфликтом завершается с ошибкой) со строкой stdin input."""
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, capture_output=True,
                          text=True, input=input)


@unittest.skipUnless(shutil.which("git"), "нет git")
class ManifestConflictTest(unittest.TestCase):
    """Конфликт операций, которые ref в _REPO_REFS не оставляют (`git stash pop`, `git merge --squash`,
    `git cherry-pick -n`): имена сторон — в индексе (:1:, :2:, :3:), разрешение конфликта не новое."""

    BASE = '{\n  "name": "app",\n  "dependencies": {\n    "a": "1"\n  }\n}\n'
    OURS = BASE.replace('"a": "1"', '"a": "2"')
    THEIRS = BASE.replace('"a": "1"', '"a": "3",\n    "authordep": "1"')
    RESOLVED = BASE.replace('"a": "1"', '"a": "2",\n    "authordep": "1"')
    OPS = ("stash pop", "merge --squash feat", "cherry-pick -n feat")

    def conflicted(self, op):
        """Env с репозиторием, где op оставила конфликт в package.json: наша сторона — OURS, их — THEIRS."""
        env = Env()
        self.addCleanup(env.close)
        p = env.project
        git("init", "-q", cwd=p)
        (p / "package.json").write_text(self.BASE, encoding="utf-8")
        git("add", ".", cwd=p)
        git("commit", "-qm", "base", cwd=p)
        if op.startswith("stash"):
            (p / "package.json").write_text(self.THEIRS, encoding="utf-8")
            git("stash", "-q", cwd=p)
        else:
            git("checkout", "-qb", "feat", cwd=p)
            (p / "package.json").write_text(self.THEIRS, encoding="utf-8")
            git("commit", "-qam", "feat", cwd=p)
            git("checkout", "-q", "-", cwd=p)
        (p / "package.json").write_text(self.OURS, encoding="utf-8")
        git("commit", "-qam", "ours", cwd=p)
        r = run_git(p, *op.split())
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("<<<<<<<", (p / "package.json").read_text(encoding="utf-8"))
        return env

    def tool(self, env, tool, **tool_input):
        return env.run("judge_tool.py", env.hook_input(
            "PreToolUse", tool_name=tool, tool_input={"file_path": str(env.project / "package.json"), **tool_input}))

    def test_write_and_edit_resolution_pass(self):
        for op in self.OPS:
            with self.subTest(op=op):
                env = self.conflicted(op)
                r = self.tool(env, "Write", content=self.RESOLVED)
                self.assertEqual(r.stdout, "", r.stderr)
                current = (env.project / "package.json").read_text(encoding="utf-8")
                r = self.tool(env, "Edit", old_string=current, new_string=self.RESOLVED)
                self.assertEqual(r.stdout, "", r.stderr)
                # Имя ни одной из сторон — новое.
                r = self.tool(env, "Write", content=self.RESOLVED.replace('"a": "2"', '"a": "2",\n    "evilpkg": "1"'))
                reason = output(r)["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("evilpkg", reason)
                self.assertNotIn("authordep", reason)

    def test_checkout_theirs_passes(self):
        command = "git checkout --theirs package.json"
        for op in self.OPS:
            with self.subTest(op=op):
                env = self.conflicted(op)
                pre = env.run("judge_tool.py", env.hook_input(
                    "PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1"))
                self.assertEqual(pre.stdout, "", pre.stderr)
                run_git(env.project, *command.split()[1:])
                self.assertIn("authordep", (env.project / "package.json").read_text(encoding="utf-8"))
                r = env.run("judge_tool.py", env.hook_input(
                    "PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1",
                    tool_response={"stdout": "", "stderr": ""}))
                self.assertEqual(r.stdout, "", r.stderr)

    def test_rebase_source_after_resolution(self):
        # Конфликт git rebase разрешён нашей стороной и `git add` (сторон в индексе нет); имя из
        # перебазируемого коммита (REBASE_HEAD) возвращается правкой — не новое.
        env = Env()
        self.addCleanup(env.close)
        p = env.project
        git("init", "-q", cwd=p)
        (p / "package.json").write_text(self.BASE, encoding="utf-8")
        git("add", ".", cwd=p)
        git("commit", "-qm", "base", cwd=p)
        git("checkout", "-qb", "feat", cwd=p)
        (p / "package.json").write_text(self.THEIRS, encoding="utf-8")
        git("commit", "-qam", "feat", cwd=p)
        git("checkout", "-q", "-", cwd=p)
        (p / "package.json").write_text(self.OURS, encoding="utf-8")
        git("commit", "-qam", "ours", cwd=p)
        main = run_git(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git("checkout", "-q", "feat", cwd=p)
        self.assertNotEqual(run_git(p, "rebase", main).returncode, 0)
        (p / "package.json").write_text(self.OURS, encoding="utf-8")
        git("add", "package.json", cwd=p)
        self.assertEqual(run_git(p, "ls-files", "-u").stdout, "")
        self.assertTrue((p / ".git" / "REBASE_HEAD").exists())
        r = self.tool(env, "Write", content=self.RESOLVED)
        self.assertEqual(r.stdout, "", r.stderr)


    def test_each_index_stage_read(self):
        # Стадии 1, 2, 3 записаны в индекс в форме `git ls-files -s` конфликтного файла, у каждой своё имя;
        # head_names видит имя каждой стадии, а имя рабочего дерева — нет.
        env = Env()
        self.addCleanup(env.close)
        p = env.project
        git("init", "-q", cwd=p)
        (p / "package.json").write_text(self.BASE, encoding="utf-8")
        git("add", ".", cwd=p)
        git("commit", "-qm", "base", cwd=p)
        lines = []
        for stage in (1, 2, 3):
            blob = run_git(p, "hash-object", "-w", "--stdin", input=self.BASE.replace('"a"', f'"stage{stage}"'))
            lines.append(f"100644 {blob.stdout.strip()} {stage}\tpackage.json\n")
        run_git(p, "rm", "-q", "--cached", "package.json")
        r = run_git(p, "update-index", "--index-info", input="".join(lines))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(run_git(p, "ls-files", "-u").stdout.splitlines()), 3)
        (p / "package.json").write_text(self.BASE.replace('"a"', '"tree"'), encoding="utf-8")
        names = manifest_watch.head_names(str(p / "package.json"), "package.json", 10)
        self.assertEqual(names, {"a", "stage1", "stage2", "stage3"})


@unittest.skipUnless(shutil.which("git"), "нет git")
class ManifestTransferEditTest(unittest.TestCase):
    """Правка файловым инструментом переносит имена из другого файла зависимостей того же реестра: убранного из
    рабочего дерева манифеста версии HEAD, setup.py, setup.cfg, Pipfile."""

    def setUp(self):
        self.env = Env()
        self.p = self.env.project
        git("init", "-q", cwd=self.p)

    def tearDown(self):
        self.env.close()

    def write_pyproject(self, deps):
        content = f'[project]\nname = "app"\ndependencies = [{", ".join(chr(34) + d + chr(34) for d in deps)}]\n'
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Write", tool_input={"file_path": str(self.p / "pyproject.toml"),
                                                         "content": content}))

    def test_removed_manifest_in_head_not_new(self):
        (self.p / "requirements.txt").write_text("requests\nclick\n", encoding="utf-8")
        (self.p / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = []\n', encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "i", cwd=self.p)
        git("rm", "-q", "requirements.txt", cwd=self.p)
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Edit", tool_input={
                "file_path": str(self.p / "pyproject.toml"), "old_string": "dependencies = []",
                "new_string": 'dependencies = ["requests>=2", "click"]'}))
        self.assertEqual(r.stdout, "", r.stderr)
        r = self.write_pyproject(["requests>=2", "evilpkg"])
        reason = output(r)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("evilpkg", reason)
        self.assertNotIn("requests", reason)

    def test_legacy_source_in_head_not_new(self):
        for name, (text, moved) in LEGACY_SOURCES.items():
            with self.subTest(name=name):
                (self.p / name).write_text(text, encoding="utf-8")
                git("add", name, cwd=self.p)
                git("commit", "-qm", name, cwd=self.p)
                git("rm", "-q", name, cwd=self.p)
                self.assertEqual(self.write_pyproject(moved).stdout, "")
                r = self.write_pyproject([moved[0], "evilpkg"])
                reason = output(r)["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("evilpkg", reason)
                self.assertNotIn(moved[0], reason)
                git("reset", "-q", "--hard", cwd=self.p)

    def test_legacy_source_in_tree_only_names_new(self):
        # Обход в два шага: имя в неотслеживаемом или изменённом setup.py, setup.cfg, Pipfile (их правку никто не
        # проверяет), затем в pyproject.toml — новое.
        (self.p / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["requests"]\n',
                                               encoding="utf-8")
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "i", cwd=self.p)
        evil = {"setup.py": "setup(install_requires=['requests', 'evilpkg'])\n",
                "setup.cfg": "[options]\ninstall_requires =\n    requests\n    evilpkg\n",
                "Pipfile": '[packages]\nrequests = "*"\nevilpkg = "*"\n'}
        edit = {"file_path": str(self.p / "pyproject.toml"), "old_string": '["requests"]',
                "new_string": '["requests", "evilpkg"]'}
        for name, (text, moved) in LEGACY_SOURCES.items():
            for rel in (name, f"scratch/{name}"):
                with self.subTest(path=rel):
                    (self.p / rel).parent.mkdir(exist_ok=True)
                    (self.p / rel).write_text(text, encoding="utf-8")
                    r = self.write_pyproject(moved)
                    self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
                    (self.p / rel).write_text(evil[name], encoding="utf-8")
                    r = self.env.run("judge_tool.py", self.env.hook_input(
                        "PreToolUse", tool_name="Edit", tool_input=edit))
                    self.assertIn("evilpkg", output(r)["hookSpecificOutput"]["permissionDecisionReason"])
        # Закоммиченный и затем изменённый setup.py: имена версии HEAD, не рабочего дерева.
        (self.p / "setup.py").write_text(LEGACY_SOURCES["setup.py"][0], encoding="utf-8")
        git("add", "setup.py", cwd=self.p)
        git("commit", "-qm", "setup", cwd=self.p)
        (self.p / "setup.py").write_text(evil["setup.py"], encoding="utf-8")
        r = self.env.run("judge_tool.py", self.env.hook_input("PreToolUse", tool_name="Edit", tool_input=edit))
        self.assertIn("evilpkg", output(r)["hookSpecificOutput"]["permissionDecisionReason"])


    def test_setup_py_syntax_warning_not_on_stderr(self):
        # Разбор setup.py со строкой `"\d"` даёт SyntaxWarning; хук stderr не пишет.
        (self.p / "setup.py").write_text('import re\nPATTERN = re.compile("\\d+")\nsetup(install_requires=["click"])\n',
                                         encoding="utf-8")
        git("add", "setup.py", cwd=self.p)
        git("commit", "-qm", "setup", cwd=self.p)
        r = self.write_pyproject(["click"])
        self.assertEqual((r.stdout, r.stderr), ("", ""))


class ManifestFailureBranchesTest(unittest.TestCase):
    """Сбои проверки манифестов — предупреждение и `skipped` в журнал, не отказ и не блок."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def post(self):
        return run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1",
            tool_response={"stdout": "", "stderr": ""}))

    def assert_skipped(self, out, message):
        self.assertEqual(out, {"systemMessage": message})
        last = self.env.log_lines()[-1]
        self.assertEqual((last["hook"], last["verdict"], last["error"]), ("manifest", "skipped",
                                                                          message.removeprefix("planka: ")))

    def test_pop_failure(self):
        with mock.patch.object(manifest_watch, "pop", side_effect=OSError("диск")):
            out = self.post()
        self.assert_skipped(out, "planka: снимок манифестов не прочитан, новые зависимости после команды не "
                                 "проверены: OSError('диск')")

    def test_compare_failure(self):
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        with mock.patch.object(manifest_watch, "compare", side_effect=PermissionError("нет доступа")):
            out = self.post()
        self.assert_skipped(out, "planka: манифесты после команды не проверены: нет доступа")

    def test_lost_with_names_blocks_and_warns(self):
        # Манифестов после команды не перечислить: манифест снимка с новым именем — блок, новые манифесты —
        # предупреждение.
        path = self.env.project / "requirements.txt"
        path.write_text("rich\n", encoding="utf-8")
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        path.write_text("rich\nflask\n", encoding="utf-8")
        (self.env.project / "new").mkdir()
        (self.env.project / "new" / "requirements.txt").write_text("evilpkg\n", encoding="utf-8")
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 1):
            out = self.post()
        self.assertEqual(out["decision"], "block")
        self.assertIn("requirements.txt: flask", out["reason"])
        self.assertEqual(out["systemMessage"],
                         "planka: новые манифесты после команды не проверены: манифестов больше 1")
        logged = [(e["hook"], e["verdict"]) for e in self.env.log_lines()]
        self.assertEqual(logged[-2:], [("manifest", "skipped"), ("manifest", "block-dep")])

    def test_edit_timeout(self):
        path = self.env.project / "requirements.txt"
        path.write_text("rich\n", encoding="utf-8")
        with mock.patch.object(manifest_watch, "check_edit", side_effect=TimeoutError("не уложился в срок")):
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "flask\n"}))
        self.assert_skipped(out, f"planka: манифест {path}: git или обход проекта не уложились в срок, новые "
                                 f"зависимости не проверены")


class ManifestDeadlineTest(unittest.TestCase):
    """Сроки, которые judge_tool передаёт снимку, сравнению и git cat-file, — константы из TimeoutsTest."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_snapshot_and_compare_budgets(self):
        seen = {}

        def take(root, deadline):
            seen["take"] = deadline - time.monotonic()
            return {"root": str(root), "mode": "walk", "ts": time.time(), "files": {}}

        def compare(entry, deadline):
            seen["compare"] = deadline - time.monotonic()
            return {}, [], None
        with mock.patch.object(manifest_watch, "take", side_effect=take), \
                mock.patch.object(manifest_watch, "compare", side_effect=compare):
            for event in ("PreToolUse", "PostToolUse"):
                run_in_process(self.env, judge_tool.main, self.env.hook_input(
                    event, tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        self.assertTrue(manifest_watch.SNAPSHOT_BUDGET - 1 < seen["take"] <= manifest_watch.SNAPSHOT_BUDGET, seen)
        self.assertTrue(manifest_watch.CHECK_BUDGET - 1 < seen["compare"] <= manifest_watch.CHECK_BUDGET, seen)

    def test_snapshot_budget_counts_after_project_root(self):
        # Корень проекта (git rev-parse, GIT_ROOT_TIMEOUT) срок снимка не съедает.
        seen = {}

        def take(root, deadline):
            seen["take"] = deadline - time.monotonic()
            return {"root": str(root), "mode": "walk", "ts": time.time(), "files": {}}

        def slow_root(cwd):
            time.sleep(1.5)
            return self.env.project
        with mock.patch.object(manifest_watch, "take", side_effect=take), \
                mock.patch.object(common, "project_root", side_effect=slow_root):
            run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        self.assertTrue(manifest_watch.SNAPSHOT_BUDGET - 0.5 < seen["take"] <= manifest_watch.SNAPSHOT_BUDGET, seen)

    def test_restored_ref_within_snapshot_budget(self):
        # git ref, названного командой, — в том же сроке SNAPSHOT_BUDGET, что и снимок.
        def take(root, deadline):
            time.sleep(0.2)
            return {"root": str(root), "mode": "git", "ts": time.time(), "files": {}}
        with mock.patch.object(manifest_watch, "take", side_effect=take), \
                mock.patch.object(manifest_watch, "_git", return_value=None) as git_call:
            run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "git stash pop"}, tool_use_id="b1"))
        self.assertEqual(git_call.call_args.args[2:4], ("cat-file", "--batch"))
        self.assertTrue(0 < git_call.call_args.args[1] <= manifest_watch.SNAPSHOT_BUDGET - 0.2,
                        git_call.call_args)

    def test_head_timeout(self):
        with mock.patch.object(manifest_watch, "_git", return_value=None) as git_call:
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Write", tool_input={
                    "file_path": str(self.env.project / "package.json"), "content": "{}"}))
            self.assertIsNone(out)
            (self.env.project / "package.json").write_text("{broken", encoding="utf-8")
            run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Write", tool_input={
                    "file_path": str(self.env.project / "package.json"), "content": "{}"}))
        self.assertEqual(git_call.call_args.args[1], manifest_watch.HEAD_TIMEOUT)


class ManifestSnapshotFailureTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def bash(self, tid, **extra):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id=tid), **extra)

    def test_too_many_manifests_warns_once(self):
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 1):
            for rel in ("a/requirements.txt", "b/requirements.txt"):
                (self.env.project / rel).parent.mkdir()
                (self.env.project / rel).write_text("rich\n", encoding="utf-8")
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
            self.assertEqual(out, {"systemMessage": "planka: снимок манифестов не снят (манифестов больше 1): "
                                                    "новые зависимости от команд Bash и MCP-инструментов не проверяются"})
            out = run_in_process(self.env, judge_tool.main, self.env.hook_input(
                "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b2"))
            self.assertIsNone(out)
        # Предупреждение — раз на сессию, пропуск в журнале — на каждую команду.
        logged = [(e["hook"], e["verdict"]) for e in self.env.log_lines()]
        self.assertEqual(logged, [("manifest", "skipped")] * 2)
        self.assertIn("манифестов больше 1", self.env.log_lines()[-1]["error"])

    def test_missing_tool_use_id_warns_once(self):
        # Без tool_use_id снимок не сопоставить с вызовом после него: предупреждение раз на сессию, пропуск в журнале
        # на каждый вызов, снимка нет.
        (self.env.project / "requirements.txt").write_text("rich\n", encoding="utf-8")
        outs = [run_in_process(self.env, judge_tool.main,
                               self.env.hook_input("PreToolUse", tool_name=tool, tool_input=inp))
                for tool, inp in (("Bash", {"command": "ls"}), ("mcp__serena__find_symbol", {}))]
        msg = ("planka: снимок манифестов не снят (у вызова нет tool_use_id): новые зависимости от команд Bash и "
               "MCP-инструментов не проверяются")
        self.assertEqual(outs, [{"systemMessage": msg}, None])
        logged = [(e["hook"], e["verdict"]) for e in self.env.log_lines()]
        self.assertEqual(logged, [("manifest", "skipped")] * 2)
        self.assertFalse((self.env.data / "state" / "sess-1.manifests.json").exists())

    def test_unusable_data_dir_warns_without_internal_error(self):
        blocker = self.env.data / "file"
        blocker.write_text("", encoding="utf-8")
        r = self.bash("b1", CLAUDE_PLUGIN_DATA=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        msgs = messages(r)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertTrue(msgs[0].startswith("planka: снимок манифестов не снят"), msgs)


class ManifestProjectUnderFixturesTest(unittest.TestCase):
    def test_foreign_dirs_relative_to_project(self):
        env = Env()
        self.addCleanup(env.close)
        project = env.project / "testdata" / "proj"
        project.mkdir(parents=True)
        r = env.run("judge_tool.py", env.hook_input(
            "PreToolUse", tool_name="Write", cwd=str(project),
            tool_input={"file_path": str(project / "requirements.txt"), "content": "flask\n"}))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")


class ManifestProjectUnderHomeRepoTest(unittest.TestCase):
    """Проект без своего git под домашним каталогом-репозиторием dotfiles (status.showUntrackedFiles=no,
    .gitignore `*`): git о проекте не видит репозиторий `~` — манифесты обходом, как вне git, без имён версий `~`."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.home = self.env.project.parent / "home"
        self.home.mkdir(exist_ok=True)
        git("init", "-q", cwd=self.home)
        git("config", "status.showUntrackedFiles", "no", cwd=self.home)
        (self.home / ".gitignore").write_text("*\n", encoding="utf-8")
        (self.home / "other").mkdir()
        (self.home / "other" / "requirements.txt").write_text("flask\n", encoding="utf-8")
        git("add", "-f", ".gitignore", "other/requirements.txt", cwd=self.home)
        git("commit", "-qm", "dotfiles", cwd=self.home)
        self.p = self.home / "proj"
        self.p.mkdir()
        (self.p / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
        self.extra = {"HOME": str(self.home), "CLAUDE_PROJECT_DIR": str(self.p)}
        with mock.patch.dict(os.environ, self.extra):
            self.assertEqual(common.project_root(str(self.p)), self.p)

    def hook(self, event, **fields):
        return self.env.run("judge_tool.py", self.env.hook_input(event, cwd=str(self.p), **fields), **self.extra)

    def test_list_manifests_walks(self):
        with mock.patch.dict(os.environ, self.extra):
            self.assertEqual(manifest_watch.list_manifests(self.p, time.monotonic() + 10),
                             ("walk", ["requirements.txt"]))

    def test_edit_adding_home_name_denied(self):
        r = self.hook("PreToolUse", tool_name="Write",
                      tool_input={"file_path": str(self.p / "requirements.txt"), "content": REQUIREMENTS + "flask\n"})
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", r.stderr)

    def test_command_adding_name_blocked(self):
        command = "printf 'flask\\n' >> requirements.txt"
        pre = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1")
        self.assertEqual(pre.stdout, "", pre.stderr)
        state = json.loads((self.env.data / "state" / "sess-1.manifests.json").read_text(encoding="utf-8"))
        self.assertEqual(state["b1"]["mode"], "walk")
        subprocess.run(command, shell=True, cwd=self.p, check=True)
        r = self.hook("PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1",
                      tool_response={"stdout": "", "stderr": ""})
        out = output(r)
        self.assertEqual(out["decision"], "block", r.stderr)
        self.assertIn("requirements.txt: flask", out["reason"])

    def test_home_stash_names_not_known(self):
        # stash репозитория `~` до начала сессии — не ref проекта: его имена не снимают блок.
        (self.home / "other" / "requirements.txt").write_text("flask\ndjango\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}):
            git("stash", "-q", cwd=self.home)
        command = "git stash pop; printf 'django\\n' >> requirements.txt"
        pre = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1")
        self.assertEqual(pre.stdout, "", pre.stderr)
        state = json.loads((self.env.data / "state" / "sess-1.manifests.json").read_text(encoding="utf-8"))
        self.assertNotIn("known", state["b1"])
        (self.p / "requirements.txt").write_text(REQUIREMENTS + "django\n", encoding="utf-8")
        r = self.hook("PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1",
                      tool_response={"stdout": "", "stderr": ""})
        self.assertIn("requirements.txt: django", output(r)["reason"], r.stderr)

    def test_file_outside_root_keeps_own_repository(self):
        # Файл вне корня проекта — версии своего репозитория, здесь `~`.
        names = manifest_watch.head_names(str(self.home / "other" / "requirements.txt"), "requirements", 10, self.p)
        self.assertIn("flask", names)
        self.assertIsNone(manifest_watch.head_names(str(self.p / "requirements.txt"), "requirements", 10, self.p))


class HasMarkerTest(unittest.TestCase):
    def test_marker_as_leading_assignment_of_segment(self):
        cases = {
            "PLANKA_DEP_OK=1 python3 -c x": True,
            "cd app && PLANKA_DEP_OK=1 printf x >> requirements.txt": True,
            "sudo PLANKA_DEP_OK=1 pip install -r requirements.txt": True,
            "A=1 PLANKA_DEP_OK=1 npm i": True,
            "printf x >> requirements.txt # PLANKA_DEP_OK=1": False,
            "echo PLANKA_DEP_OK=1; printf x >> requirements.txt": False,
            "printf 'PLANKA_DEP_OK=1 x' >> requirements.txt": False,
            "PLANKA_DEP_OK=10 npm i": False,
            "ls": False,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(manifest_watch.has_marker(command), expected)


class CommandRefsTest(unittest.TestCase):
    """Ref, из которого команда git возвращает файлы, и признак stash."""

    def test_refs(self):
        cases = {
            "git stash pop": [("stash@{0}", True)],
            "git stash pop -q": [("stash@{0}", True)],
            "git stash apply 2": [("stash@{2}", True)],
            "git stash apply --index stash@{1}": [("stash@{1}", True)],
            "git stash pop 'stash@{3}'": [("stash@{3}", True)],
            "git stash": [],
            "git stash list": [],
            "git stash drop stash@{1}": [],
            "git merge --squash feat": [("feat", False)],
            "git merge --squash -m 'msg' feat other": [("feat", False), ("other", False)],
            "git merge feat": [],
            "git checkout feat -- requirements.txt": [("feat", False)],
            "git checkout -q feat requirements.txt": [("feat", False)],
            "git checkout feat": [],
            "git checkout -- requirements.txt": [],
            "git checkout -b x feat": [],
            "git restore --source=feat requirements.txt": [("feat", False)],
            "git restore --source feat -- requirements.txt": [("feat", False)],
            "git restore -s feat requirements.txt": [("feat", False)],
            "git restore -sfeat requirements.txt": [("feat", False)],
            "git restore requirements.txt": [],
            "git cherry-pick -n abc123": [("abc123", False)],
            "git cherry-pick --no-commit -X theirs abc def": [("abc", False), ("def", False)],
            "git cherry-pick abc123": [],
            "git -C sub -c a=b stash pop": [("stash@{0}", True)],
            "cd app && git stash pop; git checkout old -- go.mod": [("stash@{0}", True), ("old", False)],
            "sudo git stash pop": [("stash@{0}", True)],
            "echo git stash pop": [],
            "git apply x.patch": [],
            "git stash pop # git checkout old -- go.mod": [("stash@{0}", True)],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(manifest_watch.command_refs(command), expected)


class ManifestWatchStateTest(unittest.TestCase):
    """Снимок в файле состояния, обход вне git и сравнение по size и mtime."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def in_process(self, fn, *args):
        with mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_DATA": str(self.env.data)}):
            return fn(*args)

    def state_file(self):
        return self.env.data / "state" / "s.manifests.json"

    def test_session_start_written_once_by_any_pre_tool_use(self):
        path = self.env.data / "state" / "sess-1.start.json"
        before = int(time.time())
        edit = self.env.hook_input("PreToolUse", tool_name="Edit", tool_input={
            "file_path": str(self.env.project / "a.py"), "old_string": "a", "new_string": "b"})
        self.assertIsNone(run_in_process(self.env, judge_tool.main, edit))
        start = json.loads(path.read_text(encoding="utf-8"))["start"]
        self.assertTrue(before <= start <= time.time(), start)
        path.write_text(json.dumps({"start": 5}), encoding="utf-8")
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        self.assertEqual(self.in_process(manifest_watch.session_start, "sess-1"), 5)
        # Не той формы — начала нет.
        path.write_text(json.dumps({"start": True}), encoding="utf-8")
        self.assertIsNone(self.in_process(manifest_watch.session_start, "sess-1"))

    def test_session_start_not_written_after_post_tool_use_or_inside_judge(self):
        path = self.env.data / "state" / "sess-1.start.json"
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"))
        run_in_process(self.env, judge_tool.main, self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="b1"), PLANKA_JUDGE="1")
        self.assertFalse(path.exists())

    def test_store_drops_expired_entries(self):
        now = time.time()
        self.in_process(manifest_watch.store, "s", "old", {"ts": now - manifest_watch.ENTRY_TTL - 5})
        self.in_process(manifest_watch.store, "s", "fresh", {"ts": now - manifest_watch.ENTRY_TTL + 60})
        self.in_process(manifest_watch.store, "s", "new", {"ts": now})
        self.assertEqual(sorted(json.loads(self.state_file().read_text(encoding="utf-8"))), ["fresh", "new"])

    def test_pop_removes_empty_state_file(self):
        entry = {"root": "/", "mode": "walk", "ts": time.time(), "files": {}}
        self.in_process(manifest_watch.store, "s", "a", entry)
        self.in_process(manifest_watch.store, "s", "b", entry)
        self.assertEqual(self.in_process(manifest_watch.pop, "s", "a"), entry)
        self.assertTrue(self.state_file().exists())
        self.assertEqual(self.in_process(manifest_watch.pop, "s", "b"), entry)
        self.assertFalse(self.state_file().exists())

    def test_walk_files_limit(self):
        root = self.env.project
        for i in range(3):
            (root / f"f{i}.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(manifest_watch, "MAX_WALK_FILES", 2):
            with self.assertRaisesRegex(manifest_watch.Unavailable, "больше 2 файлов"):
                manifest_watch.list_manifests(root, time.monotonic() + 30)
        with mock.patch.object(manifest_watch, "MAX_WALK_FILES", 3):
            self.assertEqual(manifest_watch.list_manifests(root, time.monotonic() + 30), ("walk", []))

    def test_same_size_mtime_and_ctime_not_reread(self):
        # Манифест с тем же размером, mtime и ctime не перечитывается; правку, вернувшую размер и mtime
        # (`touch -r`), выдаёт ctime.
        path = self.env.project / "requirements.txt"
        path.write_text("rich\n", encoding="utf-8")
        entry = manifest_watch.take(self.env.project, time.monotonic() + 30)
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({}, [], None))
        st = path.stat()
        time.sleep(0.05)
        path.write_text("flsk\n", encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": ["flsk"]}, [], None))

    def test_same_size_new_mtime_reread(self):
        # Правка той же длины (`sed -i s/left-pad/evilpkg1/`) меняет mtime: манифест перечитывается.
        path = self.env.project / "requirements.txt"
        path.write_text("left-pad\n", encoding="utf-8")
        st = path.stat()
        entry = manifest_watch.take(self.env.project, time.monotonic() + 30)
        path.write_text("evilpkg1\n", encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertEqual(path.stat().st_size, st.st_size)
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": ["evilpkg1"]}, [], None))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_mode_change_lost(self):
        root = self.env.project
        (root / "requirements.txt").write_text("rich\n", encoding="utf-8")
        entry = manifest_watch.take(root, time.monotonic() + 30)
        self.assertEqual(entry["mode"], "walk")
        git("init", "-q", cwd=root)
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({}, [], "за команду корень проекта стал git-репозиторием"))
        entry = manifest_watch.take(root, time.monotonic() + 30)
        self.assertEqual(entry["mode"], "git")
        shutil.rmtree(root / ".git")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({}, [], "за команду корень проекта перестал быть git-репозиторием"))

    def test_large_manifest_after_command_unknown(self):
        path = self.env.project / "requirements.txt"
        path.write_text("rich\n", encoding="utf-8")
        entry = manifest_watch.take(self.env.project, time.monotonic() + 30)
        path.write_text("rich\nflask\n", encoding="utf-8")
        with mock.patch.object(manifest_watch, "MAX_MANIFEST_BYTES", 10):
            self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({}, ["requirements.txt"], None))
        with mock.patch.object(manifest_watch, "MAX_MANIFEST_BYTES", 11):
            self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                             ({"requirements.txt": ["flask"]}, [], None))


class ManifestBypassTest(unittest.TestCase):
    """Обходы проверки манифестов: имя свежего манифеста, правка через ссылку, FIFO, каталог FOREIGN_DIRS выше
    репозитория, восстановленные размер и mtime, запись в файл генератора requirements до и после него."""

    ROOT = '{"name": "app", "dependencies": {"left-pad": "^1"}}'

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.p = self.env.project
        (self.p / "package.json").write_text(self.ROOT, encoding="utf-8")

    def commit(self):
        git("init", "-q", cwd=self.p)
        git("add", ".", cwd=self.p)
        git("commit", "-qm", "i", cwd=self.p)

    def tool(self, tool, tool_input, **fields):
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id="e1", **fields))

    def add_evil(self, path):
        return self.tool("Edit", {"file_path": str(path), "old_string": '"left-pad": "^1"',
                                  "new_string": '"left-pad": "^1", "evil-pkg": "^1"'})

    def assert_denied(self, r, name="evil-pkg"):
        out = output(r)
        self.assertIsNotNone(out, r.stderr)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", r.stdout)
        self.assertIn(name, out["hookSpecificOutput"]["permissionDecisionReason"])

    def bash(self, command, effect=None):
        pre = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1"))
        self.assertEqual(pre.stdout, "", pre.stderr)
        subprocess.run(command if effect is None else effect, shell=True, cwd=self.p, capture_output=True)
        return self.env.run("judge_tool.py", self.env.hook_input(
            "PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1",
            tool_response={"stdout": "", "stderr": ""}))

    def assert_blocked(self, r, text):
        out = output(r)
        self.assertIsNotNone(out, r.stderr)
        self.assertEqual(out["decision"], "block", r.stdout)
        self.assertIn(text, out["reason"])

    def fresh_manifest_name_denied(self):
        r = self.tool("Write", {"file_path": str(self.p / "tools" / "fake" / "package.json"),
                                "content": '{"name": "evil-pkg"}'})
        self.assertEqual(r.stdout, "", r.stderr)
        (self.p / "tools" / "fake").mkdir(parents=True)
        (self.p / "tools" / "fake" / "package.json").write_text('{"name": "evil-pkg"}', encoding="utf-8")
        self.assert_denied(self.add_evil(self.p / "package.json"))

    def test_fresh_manifest_name_not_project_package(self):
        self.fresh_manifest_name_denied()

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_fresh_manifest_name_not_project_package_git(self):
        self.commit()
        self.fresh_manifest_name_denied()

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_fresh_manifest_name_in_one_command_blocked(self):
        self.commit()
        command = ("mkdir -p x && echo '{\"name\":\"evil-pkg\"}' > x/package.json && python3 -c \"import json; "
                   "p='package.json'; d=json.load(open(p)); d['dependencies']['evil-pkg']='^1'; "
                   "json.dump(d, open(p, 'w'))\"")
        self.assert_blocked(self.bash(command), "package.json: evil-pkg")

    def test_edit_through_symlinked_dir_denied(self):
        # build — каталог FOREIGN_DIRS, но ссылка ведёт в корень: правится манифест проекта.
        os.symlink(".", self.p / "build")
        self.assert_denied(self.add_evil(self.p / "build" / "package.json"))

    def test_edit_through_symlinked_file_denied(self):
        os.symlink("package.json", self.p / "deps.json")
        self.assert_denied(self.add_evil(self.p / "deps.json"))

    def test_foreign_dir_above_repository_ignored(self):
        repo = self.p / "build" / "mono"
        (repo / "apps" / "web").mkdir(parents=True)
        (repo / "packages" / "ui").mkdir(parents=True)
        for rel in ("apps/web", "packages/ui"):
            (repo / rel / "package.json").write_text(self.ROOT, encoding="utf-8")
        git("init", "-q", cwd=repo)
        git("add", ".", cwd=repo)
        git("commit", "-qm", "i", cwd=repo)
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Edit", cwd=str(repo / "apps" / "web"), tool_use_id="e1", tool_input={
                "file_path": str(repo / "packages" / "ui" / "package.json"), "old_string": '"left-pad": "^1"',
                "new_string": '"left-pad": "^1", "evil-pkg": "^1"'}), CLAUDE_PROJECT_DIR=str(repo / "apps" / "web"))
        self.assert_denied(r)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "нет FIFO")
    def test_fifo_manifest_does_not_hang(self):
        (self.p / "requirements.txt").write_text("requests\n", encoding="utf-8")
        fifo = self.p.parent / "pipe"
        os.mkfifo(fifo)
        (self.p / "sub").mkdir()
        os.symlink(fifo, self.p / "sub" / "requirements-dev.txt")
        os.mkfifo(self.p / "requirements-test.txt")
        r = self.env.run("judge_tool.py", self.env.hook_input(
            "PreToolUse", tool_name="Edit", tool_use_id="e1", tool_input={
                "file_path": str(self.p / "requirements.txt"), "old_string": "requests\n",
                "new_string": "requests\nevilpkgs\n"}))
        self.assert_denied(r, "evilpkgs")

    def test_restored_size_and_mtime_blocked(self):
        (self.p / "requirements.txt").write_text("requests\n", encoding="utf-8")
        ref = self.p.parent / "ref.txt"
        command = (f"cp -p requirements.txt {ref}; sleep 0.05; sed -i 's/requests/evilpkgs/' requirements.txt; "
                   f"touch -r {ref} requirements.txt")
        self.assert_blocked(self.bash(command), "requirements.txt: evilpkgs")

    def test_append_before_skipped_generator_blocked(self):
        (self.p / "requirements.txt").write_text("requests\n", encoding="utf-8")
        command = "echo evilpkgs >> requirements.txt; false && pip freeze > requirements.txt"
        self.assert_blocked(self.bash(command), "requirements.txt: evilpkgs")

    def test_append_after_generator_blocked(self):
        (self.p / "requirements.txt").write_text("requests\n", encoding="utf-8")
        command = "pip freeze > requirements.txt && echo evilpkgs >> requirements.txt"
        self.assert_blocked(self.bash(command, effect="printf 'requests\\nevilpkgs\\n' > requirements.txt"),
                            "requirements.txt: evilpkgs")

    def test_freeze_output_flag_not_generator(self):
        (self.p / "requirements.txt").write_text("requests\n", encoding="utf-8")
        command = "echo evilpkgs >> requirements.txt; uv pip freeze -o requirements.txt"
        self.assert_blocked(self.bash(command, effect="echo evilpkgs >> requirements.txt"),
                            "requirements.txt: evilpkgs")
