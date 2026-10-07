"""Контракт кода с настоящими текстами правил: имена разделов ядра, файлы модулей, метки."""
import re
import unittest

from tests.helpers import REPO

PLUGIN = REPO / "plugin"
# Разделы ядра и модули, которые код берёт по имени: common.rubric, common.philosophy_sections,
# common.rule_texts в judge_tool и judge_stop, judge_tool.DEP_REASON.
SECTIONS = ("Решения", "Планы")
MODULES = ("planning", "subagents", "verification", "docs", "comments", "dependencies")


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.core = (PLUGIN / "philosophy.md").read_text(encoding="utf-8")

    def test_core_sections(self):
        headings = re.findall(r"^## (.+)$", self.core, re.MULTILINE)
        for name in SECTIONS:
            self.assertIn(name, headings)

    def test_modules_exist_and_start_with_condition(self):
        for name in MODULES:
            text = (PLUGIN / "rules" / f"{name}.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# "), name)
            self.assertIn("\n\nЧитай", text, name)

    def test_index_lists_every_module(self):
        index = self.core.split("## Модули", 1)[1]
        listed = set(re.findall(r"^- `([\w-]+)\.md`", index, re.MULTILINE))
        on_disk = {p.stem for p in (PLUGIN / "rules").glob("*.md")}
        self.assertEqual(listed, on_disk)

    def test_language_marks(self):
        self.assertIn("{COMMENT_LANG}", (PLUGIN / "rules" / "comments.md").read_text(encoding="utf-8"))
        self.assertIn("{DOC_LANG}", (PLUGIN / "rules" / "docs.md").read_text(encoding="utf-8"))
        self.assertIn("{RULES}", self.core)


if __name__ == "__main__":
    unittest.main()
