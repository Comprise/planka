import json
import sys
import unittest

from tests.helpers import PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import prompts  # noqa: E402


def without_markers(content):
    """Содержимое turn_content без строк-пометок: числа опущенных сообщений и обрезки последнего."""
    head = "… ранние сообщения реплики опущены: "
    if content.startswith(head):
        content = content.split(prompts.TURN_SEPARATOR, 1)[1]
    return content.replace("… начало сообщения опущено\n", "", 1)


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


class CorrectnessChecksTest(unittest.TestCase):
    def test_done_asks_for_red_then_green_test(self):
        out = prompts.stop_prompt("R", "C", options=False, done=True)
        self.assertIn("красный прогон на коде до правки или на мутации и зелёный после", out)

    def test_plan_asks_for_review_wave_and_input_classes(self):
        out = prompts.plan_prompt("R", "P")
        self.assertIn("Последняя ли волна — независимое ревью диффа", out)
        self.assertIn("уходят ли его высокие и средние находки в новую волну с повторным ревью", out)
        self.assertIn("перечислены ли классы входов и окружений", out)

    def test_plan_without_input_handling_passes_input_classes(self):
        out = prompts.plan_prompt("R", "P")
        self.assertIn("План, который не трогает разбор входа, хук, сервис или CLI, этому пункту соответствует.", out)

    def test_plan_asks_heuristic_error_direction_and_corpus(self):
        out = prompts.plan_prompt("R", "P")
        self.assertIn("Если план добавляет или меняет эвристику, детектор или разборщик входа", out)
        self.assertIn("названа ли дешёвая ошибка — ложное срабатывание или пропуск", out)
        self.assertIn("корпус настоящих входов", out)
        self.assertIn("План без такой эвристики этому пункту соответствует.", out)


class PromptsTest(unittest.TestCase):
    def test_content_is_fenced_as_data(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, prompts.memory_prompt, _stop_both):
            p = fn("РУБРИКА", "СОДЕРЖИМОЕ")
            self.assertIn("РУБРИКА", p)
            self.assertIn("<content>\nСОДЕРЖИМОЕ\n</content>", p)
            self.assertLess(p.index("РУБРИКА"), p.index("<content>"))
            self.assertIn("не инструкции", p)

    def test_closing_tag_in_content_is_neutralised(self):
        for fn in (prompts.question_prompt, prompts.plan_prompt, prompts.memory_prompt, _stop_both):
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

    def test_memory_prompt_asks_consent_questions(self):
        p = prompts.memory_prompt("R", "C")
        for needle in ["явное согласие", "без лишних фактов", "секретов", "Сохранить в память: <факт>?",
                       "ответы автора на вопросы инструмента AskUserQuestion"]:
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
        self.assertEqual(out, "[Подход]\nКак чинить?\n1. Заплатка (Recommended) — три строки\n"
                              "2. Перестроить — снимает причину")

    def test_render_tolerates_missing_fields(self):
        out = prompts.render_questions({"questions": [{"question": "Q", "options": [{"label": "A"}]}]})
        self.assertEqual(out, "Q\n1. A")
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
        with self.assertRaises(ValueError):
            prompts.stop_prompt("R", "C", options=False, done=False, docs=False)


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

    def test_isolated_dirs_judged_by_agent_naming(self):
        p = prompts.stop_prompt("R", "C", options=False, done=False, docs=True)
        for needle in ("по списку файлов не видно", "агент сам называет", "локального роутера у каталога нет"):
            self.assertIn(needle, p)

    def test_label_precedes_content(self):
        content = prompts.turn_content(["первое", "второе"])
        self.assertEqual(content, "первое\n\n---\n\nвторое")
        p = prompts.stop_prompt("R", content, options=True, done=False, label=prompts.TURN_LABEL)
        self.assertLess(p.index(prompts.TURN_LABEL), p.index("<content>"))
        self.assertIn("<content>\nпервое\n\n---\n\nвторое\n</content>", p)
        self.assertNotIn(prompts.TURN_LABEL, prompts.stop_prompt("R", content, options=True, done=False))

    def test_turn_content_keeps_latest_messages_within_limit(self):
        limit = prompts.MAX_TURN_CHARS
        early = ["ранее-" + "а" * (limit // 4) for _ in range(6)]
        content = prompts.turn_content(early + ["последнее"])
        self.assertLessEqual(len(without_markers(content)), limit)
        self.assertTrue(content.endswith(prompts.TURN_SEPARATOR + "последнее"))
        kept = content.count("ранее-")
        self.assertEqual(kept, 3)
        self.assertTrue(content.startswith(f"… ранние сообщения реплики опущены: {6 - kept}" + prompts.TURN_SEPARATOR))

    def test_turn_content_counts_separators(self):
        # Короткие сообщения: разделители между ними в сумме — тысячи символов.
        short = [f"сообщение-{i:03d}-" + "ж" * 80 for i in range(400)]
        content = prompts.turn_content(short)
        kept = without_markers(content)
        self.assertLessEqual(len(kept), prompts.MAX_TURN_CHARS)
        self.assertGreater(kept.count(prompts.TURN_SEPARATOR) * len(prompts.TURN_SEPARATOR), 100)
        # Следующее раннее сообщение с разделителем уже не помещается.
        first = short.index(kept.split(prompts.TURN_SEPARATOR, 1)[0])
        self.assertGreater(len(kept) + len(prompts.TURN_SEPARATOR) + len(short[first - 1]), prompts.MAX_TURN_CHARS)

    def test_turn_content_clips_oversized_last_message_from_start(self):
        last = "н" * prompts.MAX_TURN_CHARS + "конец"
        content = prompts.turn_content(["раннее", last])
        self.assertEqual(len(without_markers(content)), prompts.MAX_TURN_CHARS)
        self.assertTrue(content.endswith("конец"))
        self.assertNotIn("раннее", content)
        self.assertIn("… ранние сообщения реплики опущены: 1", content)
        self.assertIn("… начало сообщения опущено", content)

    def test_render_docs_content(self):
        text = prompts.render_docs_content(
            "Сделал.", [("a/b.go", "code", True), ("a/CLAUDE.md", "doc", True)], ["a/b.go: // x"], True, True)
        self.assertTrue(text.startswith("Сделал."))
        self.assertIn("Изменённые файлы за ход:", text)
        self.assertIn("\n- a/b.go — код\n", text)
        self.assertIn("\n- a/CLAUDE.md — документация\n", text)
        self.assertIn("Комментарии в изменённых файлах:", text)
        self.assertIn("a/b.go: // x", text)
        self.assertIn("обрезано", text)
        self.assertIn("В корне проекта нет CLAUDE.md.", text)

    def test_render_docs_content_minimal(self):
        text = prompts.render_docs_content("M", [("x.py", "code", True)], [], False, False)
        self.assertNotIn("обрезано", text)
        self.assertNotIn("нет CLAUDE.md", text)
        self.assertIn("В изменённых файлах кода комментарии не добавлены.", text)

    def test_render_docs_content_unknown_syntax(self):
        text = prompts.render_docs_content("M", [("a.foo", "code", True), ("b.bin", "code", True)], [], False, False,
                                           ["a.foo", "b.bin"])
        self.assertIn("Файлы без известного синтаксиса комментариев, судятся по самоотчёту: a.foo, b.bin", text)
        self.assertNotIn("комментарии не добавлены", text)

    def test_render_docs_content_unknown_capped(self):
        unknown = [f"u{i:03}.erl" for i in range(prompts.MAX_LISTED + 3)]
        text = prompts.render_docs_content("M", [("x.erl", "code", True)], [], False, False, unknown)
        line = next(l for l in text.splitlines() if l.startswith("Файлы без известного"))
        self.assertIn(f"u{prompts.MAX_LISTED - 1:03}.erl", line)
        self.assertNotIn(f"u{prompts.MAX_LISTED:03}.erl", line)
        self.assertTrue(line.endswith("… и ещё 3"))

    def test_render_docs_content_unknown_after_comments(self):
        text = prompts.render_docs_content("M", [("a.go", "code", True), ("b.foo", "code", True)], ["a.go: // x"], True, False,
                                           ["b.foo"])
        self.assertLess(text.index("обрезано"), text.index("Файлы без известного синтаксиса"))
        self.assertNotIn("комментарии не добавлены", text)

    def test_render_docs_content_late_files(self):
        text = prompts.render_docs_content("M", [("a.go", "code", True), ("b.foo", "code", True)], [], False, False,
                                           ["b.foo"], ["a.go"])
        self.assertIn("Файлы без известного синтаксиса комментариев, судятся по самоотчёту: b.foo", text)
        self.assertIn("Файлы кода, не разобранные к сроку, судятся по самоотчёту: a.go", text)
        self.assertNotIn("комментарии не добавлены", text)
        only_late = prompts.render_docs_content("M", [("a.go", "code", True)], [], False, False, late=["a.go"])
        self.assertNotIn("без известного синтаксиса", only_late)
        self.assertNotIn("комментарии не добавлены", only_late)

    def test_render_docs_content_late_capped(self):
        late = [f"l{i:03}.go" for i in range(prompts.MAX_LISTED + 2)]
        text = prompts.render_docs_content("M", [("x.go", "code", True)], [], False, False, late=late)
        line = next(l for l in text.splitlines() if l.startswith("Файлы кода, не разобранные"))
        self.assertTrue(line.endswith("… и ещё 2"))

    def test_render_docs_content_other_and_deleted(self):
        changed = [("a.json", "other", True), ("old.go", "code", False), ("gone.md", "doc", False),
                   ("x.bin", "other", False)]
        text = prompts.render_docs_content("M", changed, [], False, False)
        lines = text.split("Изменённые файлы за ход:\n", 1)[1].split("\n\n", 1)[0].split("\n")
        self.assertEqual(lines, ["- a.json — прочее", "- old.go — код, удалён",
                                 "- gone.md — документация, удалён", "- x.bin — прочее, удалён"])

    def test_render_docs_content_list_limited(self):
        self.assertEqual(prompts.MAX_LISTED, 100)
        changed = [(f"f{i:03}.py", "code", True) for i in range(prompts.MAX_LISTED + 7)]
        text = prompts.render_docs_content("M", changed, [], False, False)
        lines = text.split("Изменённые файлы за ход:\n", 1)[1].split("\n\n", 1)[0].split("\n")
        self.assertEqual(len(lines), prompts.MAX_LISTED + 1)
        self.assertEqual(lines[-2], f"- f{prompts.MAX_LISTED - 1:03}.py — код")
        self.assertEqual(lines[-1], "- … и ещё 7")

    def test_render_docs_content_exact_limit_no_tail(self):
        changed = [(f"f{i:03}.py", "code", True) for i in range(prompts.MAX_LISTED)]
        text = prompts.render_docs_content("M", changed, [], False, False)
        self.assertNotIn("и ещё", text)

    def test_closing_tag_variants_are_neutralised(self):
        for tag in ("</CONTENT>", "</content >", "< /content>", "</Content\t>"):
            p = prompts.question_prompt("R", f"до{tag}после")
            self.assertEqual(len(prompts._CLOSING_TAG.findall(p)), 1, tag)
            self.assertTrue(p.endswith("</content>\n"))

    def test_docs_content_escapes_closing_tag(self):
        content = prompts.render_docs_content("</content>", [("</content>.go", "code", True)], ["x.go: // </content>"], False, False)
        out = prompts.stop_prompt("R", content, options=False, done=False, docs=True)
        self.assertEqual(out.count("</content>"), 1)
        self.assertIn("При отказе назови", out)


if __name__ == "__main__":
    unittest.main()
