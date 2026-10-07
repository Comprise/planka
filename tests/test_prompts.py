import json
import sys
import unittest

from tests.helpers import PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import prompts  # noqa: E402


class SchemaTest(unittest.TestCase):
    def test_schema(self):
        s = prompts.JUDGE_SCHEMA
        self.assertEqual(s["type"], "object")
        self.assertEqual(set(s["required"]), {"ok", "violated", "reason"})
        self.assertEqual(s["properties"]["ok"]["type"], "boolean")
        self.assertEqual(s["properties"]["violated"]["type"], "array")
        json.dumps(s)


def _stop_both(rubric, content):
    return prompts.stop_prompt(rubric, content, options=True, done=True)


class PromptsTest(unittest.TestCase):
    def test_content_is_fenced_as_data(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, _stop_both):
            p = fn("РУБРИКА", "СОДЕРЖИМОЕ")
            self.assertIn("РУБРИКА", p)
            self.assertIn("<content>\nСОДЕРЖИМОЕ\n</content>", p)
            self.assertLess(p.index("РУБРИКА"), p.index("<content>"))
            self.assertIn("не инструкции", p)

    def test_closing_tag_in_content_is_neutralised(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, _stop_both):
            p = fn("R", "до</content>после")
            self.assertIn("до<\\/content>после", p)
            self.assertEqual(p.count("</content>"), 1)
            self.assertTrue(p.endswith("</content>\n"))

    def test_question_prompt_asks_fixed_questions(self):
        p = prompts.question_prompt("R", "C")
        for needle in ["самый правильный", "есть ли он в списке", "Рекомендуемый",
                       "границ", "откладыван", "пункта «Решения 7»", "сверх задачи"]:
            self.assertIn(needle, p)
        # Слова-маркеры живут только в рубрике.
        for marker in ["«пока»", "«временно»", "«потом»", "«вне рамок»"]:
            self.assertNotIn(marker, p)

    def test_plan_prompt_asks_plan_questions(self):
        p = prompts.plan_prompt("R", "C")
        for needle in ["волн", "схождени", "контракт", "самодостаточ",
                       "при проблеме", "общие файлы"]:
            self.assertIn(needle, p)

    def test_system_prompt(self):
        self.assertIn("JSON", prompts.SYSTEM_PROMPT)
        self.assertIn("указание", prompts.SYSTEM_PROMPT)
        self.assertIn("имя модуля", prompts.SYSTEM_PROMPT)


class RenderQuestionsTest(unittest.TestCase):
    def test_render(self):
        ti = {"questions": [{
            "question": "Как чинить?", "header": "Подход", "multiSelect": False,
            "options": [
                {"label": "Заплатка (Recommended)", "description": "три строки"},
                {"label": "Перестроить", "description": "снимает причину"},
            ]}]}
        out = prompts.render_questions(ti)
        self.assertIn("Подход", out)
        self.assertIn("Как чинить?", out)
        self.assertIn("1. Заплатка (Recommended) — три строки", out)
        self.assertIn("2. Перестроить — снимает причину", out)

    def test_render_tolerates_missing_fields(self):
        out = prompts.render_questions({"questions": [{"question": "Q", "options": [{"label": "A"}]}]})
        self.assertIn("Q", out)
        self.assertIn("1. A", out)
        self.assertEqual(prompts.render_questions({}), "")


class StopPromptTest(unittest.TestCase):
    def test_stop_prompt_combines(self):
        p = prompts.stop_prompt("R", "C", options=True, done=True)
        self.assertIn("самый правильный", p)
        self.assertIn("команда-доказательство", p)
        self.assertLess(p.index("самый правильный"), p.index("команда-доказательство"))
        self.assertEqual(p.count("\n<content>\n"), 1)
        self.assertEqual(p.count("\n</content>\n"), 1)

    def test_stop_prompt_single(self):
        self.assertNotIn("команда-доказательство", prompts.stop_prompt("R", "C", options=True, done=False))
        p = prompts.stop_prompt("РУБРИКА", "СОДЕРЖИМОЕ", options=False, done=True)
        self.assertNotIn("самый правильный", p)
        self.assertIn("РУБРИКА", p)
        self.assertIn("<content>\nСОДЕРЖИМОЕ\n</content>", p)
        for needle in ["команда-доказательство", "CI-конфиг", "не проверено",
                       "исходный падающий сценарий", "из вывода команды", "готов обсудить"]:
            self.assertIn(needle, p)

    def test_stop_prompt_requires_a_filter(self):
        with self.assertRaises(ValueError):
            prompts.stop_prompt("R", "C", options=False, done=False)


class DocsPromptTest(unittest.TestCase):
    def test_docs_flag_adds_checks_last(self):
        p = prompts.stop_prompt("R", "C", options=True, done=True, docs=True)
        for needle in ["самый правильный", "команда-доказательство", "локальный CLAUDE.md", "context/deferred/"]:
            self.assertIn(needle, p)
        self.assertLess(p.index("команда-доказательство"), p.index("локальный CLAUDE.md"))
        self.assertEqual(p.count("\n<content>\n"), 1)

    def test_docs_only(self):
        p = prompts.stop_prompt("R", "C", options=False, done=False, docs=True)
        self.assertIn("локальный CLAUDE.md", p)
        self.assertNotIn("самый правильный", p)

    def test_all_false_raises(self):
        with self.assertRaises(ValueError):
            prompts.stop_prompt("R", "C", options=False, done=False, docs=False)

    def test_render_docs_content(self):
        text = prompts.render_docs_content(
            "Сделал.", [("a/b.go", False), ("a/CLAUDE.md", True)], ["a/b.go: // x"], True, True)
        self.assertTrue(text.startswith("Сделал."))
        self.assertIn("Изменённые файлы за ход:", text)
        self.assertIn("a/b.go — код", text)
        self.assertIn("a/CLAUDE.md — документация", text)
        self.assertIn("Комментарии в изменённых файлах:", text)
        self.assertIn("a/b.go: // x", text)
        self.assertIn("обрезано", text)
        self.assertIn("В корне проекта нет CLAUDE.md.", text)

    def test_render_docs_content_minimal(self):
        text = prompts.render_docs_content("M", [("x.py", False)], [], False, False)
        self.assertNotIn("обрезано", text)
        self.assertNotIn("нет CLAUDE.md", text)
        self.assertIn("В изменённых файлах кода комментарии не добавлены.", text)

    def test_docs_content_escapes_closing_tag(self):
        content = prompts.render_docs_content("</content>", [("</content>.go", False)], ["x.go: // </content>"], False, False)
        out = prompts.stop_prompt("R", content, options=False, done=False, docs=True)
        self.assertEqual(out.count("</content>"), 1)
        self.assertIn("При отказе назови", out)


if __name__ == "__main__":
    unittest.main()
