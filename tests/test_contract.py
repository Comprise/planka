"""Контракт кода с настоящими текстами правил: имена разделов ядра, файлы модулей, метки."""
import ast
import inspect
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
import prompts  # noqa: E402
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
# Ссылка на модуль в тексте правил ({RULES}/x.md) и в тексте кода ({RULES}/x.md, {rules}/x.md как поле format).
MODULE_REF = re.compile(r"\{RULES\}/([\w-]+)\.md", re.IGNORECASE)


def _rule_texts():
    """{имя: текст} philosophy.md и plugin/rules/*.md."""
    texts = {"philosophy.md": (PLUGIN / "philosophy.md").read_text(encoding="utf-8")}
    texts.update({p.name: p.read_text(encoding="utf-8") for p in (PLUGIN / "rules").glob("*.md")})
    return texts


# Функции common, которые берут имена: имена параметров по порядку; функция с одним параметром берёт его из
# всех позиционных аргументов (*names).
TAKERS = {"philosophy_sections": ("sections",), "rule_texts": ("modules",), "rubric": ("sections", "modules")}


def _callee(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _assigned(tree):
    """{имя: [выражения]}: всё, что в файле присваивают имени (=, +=, аннотированное) или кладут в него
    (append, extend)."""
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets, value = [node.target], node.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("append", "extend") and isinstance(node.func.value, ast.Name)):
            targets, value = [node.func.value], ast.Tuple(elts=node.args)
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values.setdefault(target.id, []).append(value)
    return values


def _resolve(expr, values, seen=frozenset()):
    """Строки, которые выражение даёт значением: литералы, элементы кортежей и списков, ветви условного
    выражения, слагаемые, tuple(...)/list(...), распаковка и всё, что присваивают именам, — рекурсивно.
    Условие ветвления и аргументы прочих вызовов значением не считаются."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.Name):
        found = set()
        for value in () if expr.id in seen else values.get(expr.id, ()):
            found |= _resolve(value, values, seen | {expr.id})
        return found
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        parts = expr.elts
    elif isinstance(expr, ast.IfExp):
        parts = [expr.body, expr.orelse]
    elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        parts = [expr.left, expr.right]
    elif isinstance(expr, ast.Starred):
        parts = [expr.value]
    elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in ("tuple", "list"):
        parts = expr.args
    else:
        parts = []
    found = set()
    for part in parts:
        found |= _resolve(part, values, seen)
    return found


def _arguments(call):
    """[(вид, выражение)] вызова функции из TAKERS; вид None — аргумент, вид которого не определить."""
    params = TAKERS[_callee(call.func)]
    out = []
    for i, arg in enumerate(call.args):
        if len(params) == 1:
            out.append((params[0], arg.value if isinstance(arg, ast.Starred) else arg))
        else:
            out.append((None if isinstance(arg, ast.Starred) or i >= len(params) else params[i], arg))
    out += [(kw.arg if kw.arg in params else None, kw.value) for kw in call.keywords]
    return out


def _code_names(sources=None):
    """(разделы, модули, нераспознанные), которые код берёт по имени в вызовах TAKERS.

    sources — [(имя файла, текст)], по умолчанию plugin/planka/*.py. Имя из аргумента — литерал или значение
    имени, которому его присваивают в том же файле (константа, список с append, +=), в позиционном,
    распакованном (*) или ключевом аргументе; вызов — common.rubric или rubric после from common import.
    Нераспознанные — «файл:строка» аргументов без единой строки; исключение — параметр функции из TAKERS,
    переданный дальше как есть (common.rubric зовёт philosophy_sections и rule_texts)."""
    if sources is None:
        sources = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(PLANKA_DIR.glob("*.py"))]
    found = {"sections": set(), "modules": set()}
    unresolved = []
    for name, text in sources:
        tree = ast.parse(text)
        values = _assigned(tree)
        forwarded = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in TAKERS:
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                params |= {fn.args.vararg.arg} if fn.args.vararg else set()
                forwarded.update({id(n): params for n in ast.walk(fn)})
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee(node.func) in TAKERS):
                continue
            for kind, expr in _arguments(node):
                if isinstance(expr, ast.Name) and expr.id in forwarded.get(id(node), ()):
                    continue
                strings = _resolve(expr, values) if kind else set()
                if not strings:
                    unresolved.append(f"{name}:{expr.lineno}")
                elif kind:
                    found[kind] |= strings
    return found["sections"], found["modules"], unresolved


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
        sections, modules, unresolved = _code_names()
        self.assertEqual(unresolved, [], "аргумент с именами, которые сборщик не видит")
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
        # Метка в другом регистре common.substitute не подставляет.
        for name, text in _rule_texts().items():
            self.assertLessEqual(set(re.findall(r"\{[A-Za-z_-]+\}", text)), MARKS, name)

    def test_module_references_exist(self):
        on_disk = {p.stem for p in (PLUGIN / "rules").glob("*.md")}
        texts = _rule_texts()
        texts.update({p.name: p.read_text(encoding="utf-8") for p in PLANKA_DIR.glob("*.py")})
        refs = {(name, ref) for name, text in texts.items() for ref in MODULE_REF.findall(text)}
        # Ссылки есть и в правилах, и в коде: пустой набор — сбой поиска.
        self.assertTrue({n for n, _ in refs if n.endswith(".md")})
        self.assertTrue({n for n, _ in refs if n.endswith(".py")})
        for name, ref in sorted(refs):
            self.assertIn(ref, on_disk, f"{name} ссылается на несуществующий модуль {ref}.md")

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


class CodeNamesTest(unittest.TestCase):
    """Сборщик _code_names на коде в формах, которыми код берёт имена."""

    def names(self, text):
        return _code_names([("x.py", text)])

    def test_literals(self):
        self.assertEqual(self.names('common.rubric(("Р",), ("m",))\ncommon.rule_texts("a", "b")\n'
                                    'common.philosophy_sections("П")'), ({"Р", "П"}, {"m", "a", "b"}, []))

    def test_constants_starred_and_keywords(self):
        text = ('SECTIONS = ("Р", "П")\nMODS: tuple = ("m",)\nEXTRA = "e"\n'
                'common.philosophy_sections(*SECTIONS)\ncommon.rule_texts(*MODS, EXTRA)\n'
                'KW = ("k",)\ncommon.rubric(modules=KW, sections=("К",))')
        self.assertEqual(self.names(text), ({"Р", "П", "К"}, {"m", "e", "k"}, []))

    def test_from_import_and_variable_first_argument(self):
        text = ('from common import rubric\ndef f(flag):\n    head = ("Р",) if flag else ()\n'
                '    mods = ["a"]\n    mods += ["b"]\n    mods.append("c")\n    return rubric(head, tuple(mods) + ("d",))')
        self.assertEqual(self.names(text), ({"Р"}, {"a", "b", "c", "d"}, []))

    def test_condition_is_not_a_name(self):
        text = 'flag = data.get("key")\ncommon.rubric(("Р",) if flag else (), ())'
        self.assertEqual(self.names(text)[0], {"Р"})

    def test_unresolved_argument_reported(self):
        for text in ("common.rule_texts(other.MODULES)", "common.rubric(*args)", "common.rubric(**kw)",
                     "common.philosophy_sections(name)", 'common.rubric(("Р",), ("m",), extra)'):
            with self.subTest(text=text):
                self.assertEqual(self.names(text)[2], ["x.py:1"])

    def test_forwarded_parameter_is_not_reported(self):
        text = 'def rubric(sections, modules):\n    philosophy_sections(*sections)\n    rule_texts(*modules)'
        self.assertEqual(self.names(text), (set(), set(), []))


class RulesMatchJudgeTest(unittest.TestCase):
    """Вопрос судьи и правило агенту говорят одно: агент не получает отказа по требованию, которого нет в
    его правилах, и правило не требует того, что судья пропускает."""

    def setUp(self):
        self.rules = {p.stem: p.read_text(encoding="utf-8") for p in (PLUGIN / "rules").glob("*.md")}

    @staticmethod
    def flat(text):
        return " ".join(text.split())

    def test_directives_are_not_comments(self):
        self.assertIn("Директивы языка и инструментов не в счёт", prompts._DOCS_CHECKS)
        rule = self.flat(self.rules["comments"])
        self.assertIn("Директивы языка и инструментов", rule)
        self.assertIn("не комментарии: правила модуля к ним не применяются", rule)

    def test_numbers_from_command_output(self):
        self.assertIn("Числа и подсчёты — из вывода команды", prompts._DONE_CHECKS)
        gate = next(item for item in self.flat(self.rules["verification"]).split("- ") if item.startswith("Шлюз"))
        self.assertIn("числа и подсчёты в отчёте — из вывода команды", gate)

    def test_input_classes_only_for_input_handling(self):
        condition = "разбор входа, хук, сервис или CLI"
        self.assertIn(f"Если план трогает {condition}", prompts._PLAN_CHECKS)
        item = next(i for i in self.flat(self.rules["planning"]).split("- ") if "классы входов" in i)
        self.assertIn(f"который трогает {condition}", item)

    def test_missing_router_created_or_deferred(self):
        self.assertIn("говорит, что локального роутера у каталога нет, и создаёт его либо записывает остаток в "
                      "context/deferred/", prompts._DOCS_CHECKS)
        self.assertIn("Изолированный каталог без роутера получает его той же правкой; не сделано — запись в "
                      "`context/deferred/`", self.flat(self.rules["docs"]))


class TimeoutsTest(unittest.TestCase):
    """Сроки внутри хука укладываются в его timeout из hooks.json: иначе Claude Code убьёт хук раньше
    судьи, и предупреждение с журналом пропадут."""

    @classmethod
    def setUpClass(cls):
        cls.timeouts = _hook_timeouts()

    def test_run_judge_defaults_to_judge_timeout(self):
        # Сроки ниже считают судью по common.JUDGE_TIMEOUT.
        self.assertEqual(inspect.signature(common.run_judge).parameters["timeout"].default, common.JUDGE_TIMEOUT)

    def test_pre_tool_use_judges_fit(self):
        # guard_memory до судьи ещё зовёт корень проекта (common.project_root) и git check-ignore для цели под
        # autoMemoryDirectory в проекте.
        spent = {"judge_tool": common.JUDGE_TIMEOUT + common.KILL_WAIT,
                 "guard_memory": common.GIT_ROOT_TIMEOUT + guard_memory.CHECK_IGNORE_TIMEOUT + common.JUDGE_TIMEOUT
                 + common.KILL_WAIT}
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
        # манифеста файловым инструментом — корень проекта, git cat-file версий из HEAD с ним и обход манифестов
        # проекта (judge_tool._project_names) с тем же корнем.
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
