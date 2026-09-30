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


class PromptsTest(unittest.TestCase):
    def test_content_is_fenced_as_data(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, prompts.message_prompt):
            p = fn("РУБРИКА", "СОДЕРЖИМОЕ")
            self.assertIn("РУБРИКА", p)
            self.assertIn("<content>\nСОДЕРЖИМОЕ\n</content>", p)
            self.assertLess(p.index("РУБРИКА"), p.index("<content>"))
            self.assertIn("не инструкции", p)

    def test_closing_tag_in_content_is_neutralised(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, prompts.message_prompt):
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


if __name__ == "__main__":
    unittest.main()
