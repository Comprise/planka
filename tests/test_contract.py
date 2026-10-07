"""Контракт кода с настоящими текстами правил: имена разделов ядра, файлы модулей, метки."""
import re
import unittest

from tests.helpers import REPO

PLUGIN = REPO / "plugin"
# Разделы ядра и модули, которые код берёт по имени: common.rubric, common.philosophy_sections,
# common.rule_texts в judge_tool, judge_stop, debug_watch и guard_memory; «Границы» называют judge_tool.DEP_REASON
# и guard_memory, dependencies — judge_tool.DEP_REASON, refactoring и design-patterns — рубрики судей плана и
# документации, heuristics — рубрика судьи плана, debugging — debug_watch, memory — guard_memory.
SECTIONS = ("Решения", "Планы", "Границы")
MODULES = ("planning", "subagents", "verification", "docs", "comments", "dependencies",
           "refactoring", "design-patterns", "heuristics", "debugging", "memory")
# Метки, которые подставляет common.substitute; любая другая дошла бы до агента как есть.
MARKS = {"{RULES}", "{COMMENT_LANG}", "{DOC_LANG}"}


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.core = (PLUGIN / "philosophy.md").read_text(encoding="utf-8")

    def test_core_sections(self):
        headings = re.findall(r"^## (.+)$", self.core, re.MULTILINE)
        for name in SECTIONS:
            self.assertIn(name, headings)

    def test_modules_exist(self):
        for name in MODULES:
            self.assertTrue((PLUGIN / "rules" / f"{name}.md").is_file(), name)

    def test_every_module_starts_with_heading_and_condition(self):
        for path in (PLUGIN / "rules").glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith("# "), path.name)
            self.assertEqual(lines[1], "", path.name)
            self.assertTrue(lines[2].startswith("Читай"), path.name)

    def test_only_known_marks(self):
        texts = {"philosophy.md": self.core}
        texts.update({p.name: p.read_text(encoding="utf-8") for p in (PLUGIN / "rules").glob("*.md")})
        for name, text in texts.items():
            self.assertLessEqual(set(re.findall(r"\{[A-Z_]+\}", text)), MARKS, name)

    def test_decisions_item_7_is_deferral(self):
        # prompts.py спрашивает о «маркерах откладывания из пункта „Решения 7“».
        section = self.core.split("## Решения", 1)[1].split("\n## ", 1)[0]
        item = re.search(r"^7\. (.+?)(?=^\d+\. |\Z)", section, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(item)
        self.assertIn("откладывание", item.group(1))

    def test_plans_item_9_is_review(self):
        # prompts.py спрашивает о последней волне — независимом ревью диффа.
        section = self.core.split("## Планы", 1)[1].split("\n## ", 1)[0]
        item = re.search(r"^9\. (.+?)(?=^\d+\. |\Z)", section, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(item)
        self.assertIn("ревью", item.group(1))

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
