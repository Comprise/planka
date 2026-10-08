"""Контракт кода с настоящими текстами правил: имена разделов ядра, файлы модулей, метки."""
import ast
import json
import re
import sys
import unittest

from tests.helpers import PLANKA_DIR, REPO

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import guard_memory  # noqa: E402
import judge_stop  # noqa: E402
import manifest_watch  # noqa: E402
import remind  # noqa: E402

PLUGIN = REPO / "plugin"
# Обратное направление: разделы и модули, которые код обязан брать (имена выводит из кода _code_names);
# «Границы» называют guard_memory и тексты отказа judge_tool (DEP_REASON, MANIFEST_REASON, COMMAND_REASON),
# dependencies — те же тексты judge_tool.
SECTIONS = ("Решения", "Планы", "Границы")
MODULES = ("planning", "subagents", "verification", "docs", "comments", "dependencies",
           "refactoring", "design-patterns", "heuristics", "debugging", "memory")
# Метки, которые подставляет common.substitute; любая другая дошла бы до агента как есть.
MARKS = {"{RULES}", "{COMMENT_LANG}", "{DOC_LANG}"}


def _strings(node):
    """Строковые литералы в выражении (кортежи, списки, условные выражения)."""
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _code_names():
    """(разделы, модули), которые код plugin/planka/*.py берёт по имени.

    Разделы — литералы в common.rubric (1-й аргумент) и common.philosophy_sections; модули — литералы
    в common.rubric (2-й аргумент) и common.rule_texts, константа MODULE, а для имени-списка в
    rubric(..., tuple(имя)) — всё, что в него кладут: литерал, append, +=."""
    sections, modules = set(), set()
    for path in PLANKA_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lists = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODULE"
                                                    for t in node.targets):
                modules |= _strings(node.value)
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr == "philosophy_sections":
                for arg in node.args:
                    sections |= _strings(arg)
            elif attr == "rule_texts":
                for arg in node.args:
                    modules |= _strings(arg)
            elif attr == "rubric" and len(node.args) >= 2:
                sections |= _strings(node.args[0])
                modules |= _strings(node.args[1])
                lists |= {n.id for n in ast.walk(node.args[1]) if isinstance(n, ast.Name) and n.id != "tuple"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "append", "extend") and isinstance(node.func.value, ast.Name) and node.func.value.id in lists:
                for arg in node.args:
                    modules |= _strings(arg)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id in lists:
                modules |= _strings(node.value)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in lists
                                                      for t in node.targets):
                modules |= _strings(node.value)
    return sections, modules


def _hook_timeouts():
    """{файл хука: [timeout, ...]} из plugin/hooks/hooks.json."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    found = {}
    for event, groups in hooks.items():
        for group in groups:
            for hook in group["hooks"]:
                name = re.search(r"planka/(\w+)\.py", hook["command"]).group(1)
                found.setdefault((event, name), []).append(hook["timeout"])
    return found


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

    def test_names_taken_by_code_exist(self):
        sections, modules = _code_names()
        # Сборщик находит каждое имя ручных списков: пустое множество — сбой разбора.
        self.assertGreaterEqual(sections, set(SECTIONS))
        # dependencies код называет только в текстах judge_tool DEP_REASON, MANIFEST_REASON, COMMAND_REASON, не
        # вызовом.
        self.assertGreaterEqual(modules, set(MODULES) - {"dependencies"})
        headings = set(re.findall(r"^## (.+)$", self.core, re.MULTILINE))
        for name in sorted(sections):
            self.assertIn(name, headings, f"раздел {name!r}, который берёт код, не найден в philosophy.md")
        for name in sorted(modules):
            self.assertTrue((PLUGIN / "rules" / f"{name}.md").is_file(),
                            f"модуль {name!r}, который берёт код, не найден в plugin/rules/")

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


class TimeoutsTest(unittest.TestCase):
    """Сроки внутри хука укладываются в его timeout из hooks.json: иначе Claude Code убьёт хук раньше
    судьи, и предупреждение с журналом пропадут."""

    @classmethod
    def setUpClass(cls):
        cls.timeouts = _hook_timeouts()

    def test_pre_tool_use_judges_fit(self):
        # guard_memory до судьи ещё зовёт git check-ignore для цели под autoMemoryDirectory в проекте.
        spent = {"judge_tool": common.JUDGE_TIMEOUT + common.KILL_WAIT,
                 "guard_memory": guard_memory.CHECK_IGNORE_TIMEOUT + common.JUDGE_TIMEOUT + common.KILL_WAIT}
        for name, total in spent.items():
            for timeout in self.timeouts[("PreToolUse", name)]:
                self.assertLess(total, timeout, name)

    def test_stop_fits(self):
        spent = (common.GIT_ROOT_TIMEOUT + judge_stop.SNAPSHOT_BUDGET + judge_stop.COMMENTS_BUDGET
                 + common.JUDGE_TIMEOUT + common.KILL_WAIT)
        for timeout in self.timeouts[("Stop", "judge_stop")]:
            self.assertLess(spent, timeout)

    def test_user_prompt_submit_fits(self):
        # Срок снимка отсчитывается от старта хука, определение корня входит в него.
        for timeout in self.timeouts[("UserPromptSubmit", "remind")]:
            self.assertLess(remind.SNAPSHOT_BUDGET, timeout)

    def test_bash_manifest_snapshot_fits(self):
        # Снимок манифестов перед командой Bash: корень проекта git rev-parse, затем срок снимка; правка
        # манифеста файловым инструментом — git cat-file версии из HEAD, затем корень проекта и обход
        # манифестов проекта (judge_tool._project_names).
        for timeout in self.timeouts[("PreToolUse", "judge_tool")]:
            self.assertLess(common.GIT_ROOT_TIMEOUT + manifest_watch.SNAPSHOT_BUDGET, timeout)
            self.assertLess(manifest_watch.HEAD_TIMEOUT + common.GIT_ROOT_TIMEOUT + manifest_watch.SNAPSHOT_BUDGET,
                            timeout)

    def test_post_tool_use_fits(self):
        for event in ("PostToolUse", "PostToolUseFailure"):
            for timeout in self.timeouts[(event, "judge_tool")]:
                self.assertLess(manifest_watch.CHECK_BUDGET, timeout, event)

    def test_post_tool_use_has_no_judge(self):
        # PostToolUse на Bash — debug_watch и сравнение манифестов judge_tool.check_command_manifests: ни один
        # не зовёт судью.
        def calls(node):
            return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                    for n in ast.walk(node) if isinstance(n, ast.Call)}
        for name in ("debug_watch", "manifest_watch"):
            tree = ast.parse((PLANKA_DIR / f"{name}.py").read_text(encoding="utf-8"))
            self.assertNotIn("run_judge", calls(tree), name)
        tree = ast.parse((PLANKA_DIR / "judge_tool.py").read_text(encoding="utf-8"))
        post = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}["check_command_manifests"]
        self.assertTrue(calls(post).isdisjoint({"run_judge", "_judge_and_emit", "judge_question", "judge_plan"}))
        for event in ("PostToolUse", "PostToolUseFailure"):
            self.assertIn((event, "debug_watch"), self.timeouts)
            self.assertIn((event, "judge_tool"), self.timeouts)
