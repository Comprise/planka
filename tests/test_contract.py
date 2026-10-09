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
# «Границы» называют guard_memory и тексты отказа judge_tool (DEP_REASON, DEP_DOUBT_REASON, MANIFEST_REASON,
# COMMAND_REASON, MCP_REASON), dependencies — те же тексты judge_tool.
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


def _assigned(tree, nodes=None):
    """{имя: [выражения]}: всё, что в файле присваивают имени (=, +=, аннотированное) или кладут в него
    (append, extend); nodes — узлы, которые смотреть вместо всех узлов tree."""
    values = {}
    for node in ast.walk(tree) if nodes is None else nodes:
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


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class _Scopes:
    """Присваивания _assigned по областям видимости: модуль и каждая функция без тел вложенных в неё. Имя ищется
    в своей функции, затем в объемлющих, затем в модуле; параметр функции значения из кода не получает."""

    def __init__(self, tree):
        self.values, self.params, self.parent, self.scope_of = {}, {}, {}, {}
        self._fill(tree, None)

    def _fill(self, scope, outer):
        self.parent[id(scope)] = outer
        args = getattr(scope, "args", None)
        names = set()
        if args is not None:
            names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
            names |= {a.arg for a in (args.vararg, args.kwarg) if a}
        self.params[id(scope)] = names
        own = []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            own.append(node)
            self.scope_of[id(node)] = scope
            if isinstance(node, _FUNCTIONS):
                self._fill(node, scope)
            else:
                stack.extend(ast.iter_child_nodes(node))
        self.values[id(scope)] = _assigned(scope, own)

    def lookup(self, name, scope):
        """(область, [выражения]) имени из scope; пустой список — параметр или имя без присваивания."""
        while scope is not None:
            if name in self.params[id(scope)]:
                return scope, []
            if name in self.values[id(scope)]:
                return scope, self.values[id(scope)][name]
            scope = self.parent[id(scope)]
        return None, []


def _resolve(expr, scopes, scope, seen=frozenset()):
    """Строки, которые выражение в области scope даёт значением: литералы, элементы кортежей и списков, ветви
    условного выражения, слагаемые, tuple(...)/list(...), распаковка и всё, что присваивают именам в их области
    (_Scopes), — рекурсивно. Условие ветвления и аргументы прочих вызовов значением не считаются."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.Name):
        owner, exprs = scopes.lookup(expr.id, scope)
        key = (id(owner), expr.id)
        found = set()
        for value in () if key in seen else exprs:
            found |= _resolve(value, scopes, owner, seen | {key})
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
        found |= _resolve(part, scopes, scope, seen)
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
    имени, которому его присваивают в той же функции, объемлющей или в модуле (константа, список с append, +=;
    _Scopes), в позиционном, распакованном (*) или ключевом аргументе; вызов — common.rubric или rubric после
    from common import. Нераспознанные — «файл:строка» аргументов без единой строки, в том числе параметр
    функции; исключение — параметр функции из TAKERS, переданный дальше как есть (common.rubric зовёт
    philosophy_sections и rule_texts)."""
    if sources is None:
        sources = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(PLANKA_DIR.glob("*.py"))]
    found = {"sections": set(), "modules": set()}
    unresolved = []
    for name, text in sources:
        tree = ast.parse(text)
        scopes = _Scopes(tree)
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
                strings = _resolve(expr, scopes, scopes.scope_of[id(node)]) if kind else set()
                if not strings:
                    unresolved.append(f"{name}:{expr.lineno}")
                elif kind:
                    found[kind] |= strings
    return found["sections"], found["modules"], unresolved


# Обращение к модели на «ты»: местоимения и глаголы 2-го лица ед. числа («знаешь»; «лишь» — частица).
INFORMAL = re.compile(r"(?<![\w-])(?:ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих|твоим|твоей|твоего|твою|"
                      r"[а-яё]+(?:ешь|ёшь|ишь)(?:ся)?)(?![\w-])", re.IGNORECASE)
INFORMAL_EXCEPT = {"лишь"}
# Повелительное ед. числа глаголов, которыми пишут указания тексты для модели (ядро, модули, промпты судьи, причины
# отказа): ловит эти формы в любом месте предложения, в том числе там, где их не видит IMPERATIVE_PLACE.
SINGULAR_IMPERATIVES = (
    "бери", "включай", "возрази", "выбери", "выбирай", "выводи", "выдели", "выдумывай", "выполняй", "говори",
    "гоняй", "давай", "делай", "делегируй", "держи", "держись", "добавляй", "добавь", "закрепляй", "заменяй",
    "замкни", "запиши", "запускай", "запусти", "зафиксируй", "заявляй", "зеркаль", "исполняй", "используй",
    "коммить", "комментируй", "лечи", "меняй", "назови", "называй", "найди", "начинай", "обновляй", "объясни",
    "остановись", "отвергай", "отвечай", "ответь", "откатывай", "переверни", "передавай", "перезаписывай",
    "переписывай", "перепиши", "перепроверь", "перепрогони", "перечитай", "печатай", "пиши", "планируй",
    "повтори", "повторяй", "подразумевай", "подтверди", "покажи", "помечай", "правь", "предложи",
    "предпочитай", "придирайся", "принимай", "проверь", "проверяй", "проводи", "проговаривай", "прогони",
    "проси", "прочитай", "прячь", "разведай", "разверни", "раздели", "разреши", "реализуй", "сведи", "сделай",
    "скажи", "сканируй", "следуй", "снижай", "собери", "соглашайся", "сообщи", "спрашивай", "спроси", "сравни",
    "ставь", "трассируй", "уважай", "удаляй", "улучши", "утверждай", "форматируй", "храни", "цитируй", "чини",
    "читай",
)
IMPERATIVE = re.compile(r"(?<![\w-])(?:%s)(?![\w-])" % "|".join(SINGULAR_IMPERATIVES), re.IGNORECASE)
# Повелительное ед. числа по форме: окончание -й, -ь, -и (возвратное -йся, -ься, -ись) там, где стоит указание, —
# в начале предложения, строки или пункта списка, после «.», «!», «?», «:», «;», «,», «—», после «не» и «и» и рядом
# с «пожалуйста». Существительные и прилагательные с теми же окончаниями отсекают NOT_IMPERATIVE и
# IMPERATIVE_EXCEPT.
_IMPERATIVE_FORM = r"[а-яё]+(?:[йьи]|йся|ься|ись)"
IMPERATIVE_PLACE = re.compile(
    r"(?:(?:^|[.!?:;,—])\s*(?:(?:[-*]|\d+\.)[ \t]+)?|(?<![\w-])(?:не|и|пожалуйста,?)\s+)"
    r"(?P<after>" + _IMPERATIVE_FORM + r")(?![\w-])"
    r"|(?<![\w-])(?P<before>" + _IMPERATIVE_FORM + r")(?=,?\s+пожалуйста(?![\w-]))",
    re.IGNORECASE | re.MULTILINE)
# Окончания, которых у повелительного нет (или есть у редких глаголов вне текстов плагина — «пей», «пеки»):
# прилагательные и местоимения -ый/-ий/-ой/-ей, инфинитив -ть/-ться/-чь/-сти/-зти/-йти/-дти, мн. ч. повелительного
# -тесь, прошедшее -ось/-ась, существительные -ии, -тель, -ки, творительный мн. ч. -ами/-ями/-ыми.
NOT_IMPERATIVE = re.compile(r"(?:[ыиое]й|ть|ться|чь|[сзйд]ти|тесь|[оа]сь|ии|тель|ки|[аяы]ми)$", re.IGNORECASE)
# Не глаголы, которые в текстах для модели стоят на месте указания (IMPERATIVE_PLACE) и не отсекаются
# NOT_IMPERATIVE: служебные слова и существительные. Новое такое слово в тексте — сюда.
IMPERATIVE_EXCEPT = {
    "весь", "внутри", "если", "или", "ни", "они", "при", "ради", "три",
    "дубли", "задачи", "запись", "ключи", "конфиги", "логи", "локаль", "модули", "модуль", "перечень",
    "пути", "разборщики", "стиль", "теги", "флаги", "хэши", "цель",
    "разошлись",
}
# Просьба во мн. ч. повелительного (-йте, -ьте, -ите и возвратные): предложение с ней в тексте хука, ядре и модуле
# несёт «пожалуйста». -ите совпадает и с настоящим временем («вы видите»): в этих текстах его нет.
PLURAL_IMPERATIVE = re.compile(r"(?<![\w-])[а-яё]+(?:[йь]те|ите)(?:сь)?(?![\w-])", re.IGNORECASE)
# Функции common, которые отдают текст модели; обращение к ней добавляют они сами.
EMITTERS = {"deny_output", "block_output", "context_output"}
# Начало блока разметки: пустая строка, заголовок, пункт списка. Цитата и код в обратных кавычках не переходят
# его границу: незакрытая «ёлочка» иначе спрятала бы от проверки весь остаток текста.
BLOCK_START = re.compile(r"\n(?=[ \t]*(?:\n|#|[-*][ \t]|\d+\.[ \t]))")


def _unquoted_block(block):
    block = re.sub(r"`[^`]*`", " ", block)
    out, depth = [], 0
    for ch in block:
        if ch == "«":
            depth += 1
        elif ch == "»" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def _unquoted(text):
    """text без кода в обратных кавычках и цитат в «ёлочках» (с вложенными): там слова — чужие, не обращение
    к модели. Цитата и строчный код кончаются на границе блока (BLOCK_START)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return "\n".join(_unquoted_block(b) for b in BLOCK_START.split(text))


def _unbalanced_quotes(text):
    """Блоки text вне блоков кода, где «ёлочка» не закрыта или закрыта без открытия."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    out = []
    for block in BLOCK_START.split(text):
        depth, broken = 0, False
        for ch in re.sub(r"`[^`]*`", " ", block):
            if ch == "«":
                depth += 1
            elif ch == "»":
                depth -= 1
                broken = broken or depth < 0
        if depth or broken:
            out.append(block.strip()[:80])
    return out


def _informal_words(text):
    """Слова обращения на «ты» и повелительного ед. числа в text вне кода и цитат."""
    text = _unquoted(text)
    found = {m.start(): m.group(0) for m in INFORMAL.finditer(text) if m.group(0).lower() not in INFORMAL_EXCEPT}
    found.update({m.start(): m.group(0) for m in IMPERATIVE.finditer(text)})
    for m in IMPERATIVE_PLACE.finditer(text):
        name = "after" if m.group("after") else "before"
        word = m.group(name)
        if not NOT_IMPERATIVE.search(word) and word.lower() not in IMPERATIVE_EXCEPT:
            found[m.start(name)] = word
    return [found[k] for k in sorted(found)]


def _sentences_without_please(text):
    """Предложения text вне кода и цитат с повелительным мн. ч. и без «пожалуйста». Перенос строки внутри
    абзаца предложение не кончает."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|" + BLOCK_START.pattern, _unquoted(text)):
        if PLURAL_IMPERATIVE.search(sentence) and "пожалуйста" not in sentence.lower():
            out.append(sentence.strip())
    return out


def _module_trees(planka_dir):
    """{имя модуля: (дерево, присваивания _assigned, {имя функции: FunctionDef})} plugin/planka/*.py."""
    out = {}
    for path in sorted(planka_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        out[path.stem] = (tree, _assigned(tree), funcs)
    return out


def _flow_strings(expr, mod, modules, seen):
    """Строки, из которых может сложиться значение expr в модуле mod: литералы и части f-строк, значения имён
    (рекурсивно), константы других модулей плагина (`depcheck.X`), возвращаемое функциями плагина, которые expr
    зовёт. Лишнее (ключи словарей, аргументы git) проверке не мешает: в нём нет русских слов."""
    found = set()
    for node in ast.walk(expr):
        target = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Name):
            target = (mod, node.id)
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
              and node.value.id in modules):
            target = (node.value.id, node.attr)
        if target is None or target in seen:
            continue
        seen.add(target)
        tmod, name = target
        _, values, funcs = modules[tmod]
        for value in values.get(name, ()):
            found |= _flow_strings(value, tmod, modules, seen)
        if name in funcs:
            for ret in ast.walk(funcs[name]):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    found |= _flow_strings(ret.value, tmod, modules, seen)
    return found


def _hook_texts(planka_dir=PLANKA_DIR):
    """{"модуль.py": {строка, ...}}: русский текст, который хуки отдают модели через EMITTERS, выведенный из
    кода по потоку значений аргументов (_flow_strings)."""
    modules = _module_trees(planka_dir)
    out = {}
    for mod, (tree, _, _) in modules.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node.func) in EMITTERS and node.args:
                strings = set()
                for arg in node.args:
                    strings |= _flow_strings(arg, mod, modules, set())
                out.setdefault(f"{mod}.py", set()).update(s for s in strings if re.search("[а-яё]", s, re.I))
    return out


def _prompt_strings(planka_dir=PLANKA_DIR):
    """Строковые литералы prompts.py, кроме докстрингов: всё, из чего складываются промпты судьи."""
    tree = ast.parse((planka_dir / "prompts.py").read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings}


def _informal_texts(plugin_dir=PLUGIN):
    """["источник: слово"] обращений на «ты» и повелительного ед. числа в текстах для модели: ядро, модули,
    промпты судьи, тексты хуков."""
    texts = [("philosophy.md", (plugin_dir / "philosophy.md").read_text(encoding="utf-8"))]
    texts += [(f"rules/{p.name}", p.read_text(encoding="utf-8")) for p in sorted((plugin_dir / "rules").glob("*.md"))]
    planka_dir = plugin_dir / "planka"
    texts += [("prompts.py", s) for s in sorted(_prompt_strings(planka_dir))]
    texts += [(name, s) for name, strings in sorted(_hook_texts(planka_dir).items()) for s in sorted(strings)]
    return [f"{name}: {word}" for name, text in texts for word in _informal_words(text)]


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


def _core_part_hooks():
    """{номер части ядра: timeout} хуков remind.py на UserPromptSubmit в plugin/hooks/hooks.json."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    found = {}
    for group in hooks["UserPromptSubmit"]:
        for hook in group["hooks"]:
            m = re.search(r"planka/remind\.py\"?\s*(\S*)$", hook["command"])
            if m:
                found[m.group(1)] = hook["timeout"]
    return found


class CorePartsTest(unittest.TestCase):
    """Ядро уходит частями, каждая — отдельный additionalContext не длиннее remind.CONTEXT_LIMIT."""

    def setUp(self):
        self.core = (PLUGIN / "philosophy.md").read_text(encoding="utf-8")

    def test_parts_fit_context_limit(self):
        # Худший случай подстановки: длинный путь кэша плагина и длинные значения языков.
        rules = "/" + "r" * 199
        core = self.core.replace("{RULES}", rules).replace("{COMMENT_LANG}", "x" * 20).replace("{DOC_LANG}", "x" * 20)
        contexts = remind.core_parts(core)
        self.assertEqual(len(contexts), remind.PARTS)
        contexts[0] += "\n\n" + remind.NO_DOCS_LINE.replace("{RULES}", rules)
        for k, ctx in enumerate(contexts, 1):
            # Мерится то, что уходит агенту: с обращением, которое добавляет context_output.
            sent = common.context_output(ctx)["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(sent.startswith(common.ADDRESS))
            self.assertLessEqual(len(sent), remind.CONTEXT_LIMIT, f"часть {k}")

    def test_parts_keep_every_section_once(self):
        parts = remind.split_core(self.core, remind.PARTS)
        self.assertEqual("".join(parts), self.core)
        self.assertTrue(all(p.strip() for p in parts))
        headings = re.findall(r"^## .+$", self.core, re.MULTILINE)
        self.assertEqual([h for p in parts for h in re.findall(r"^## .+$", p, re.MULTILINE)], headings)
        for p in parts[1:]:
            self.assertTrue(p.startswith("## "), p[:40])

    def test_hooks_json_has_every_part(self):
        timeouts = _core_part_hooks()
        self.assertEqual(sorted(timeouts, key=int), [str(k) for k in range(1, remind.PARTS + 1)])


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
        # dependencies код называет только в текстах judge_tool DEP_REASON, DEP_DOUBT_REASON, MANIFEST_REASON,
        # COMMAND_REASON, MCP_REASON, не вызовом.
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
            self.assertTrue(lines[2].startswith(f"Читайте, {common.ADDRESS[0].lower()}{common.ADDRESS[1:]},"),
                            path.name)

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


class PoliteFormTest(unittest.TestCase):
    """Всё, что плагин пишет модели, — на «вы» и с «пожалуйста»; обращение common.ADDRESS ставят EMITTERS и
    промпты судьи (EMITTERS проверяет test_common)."""

    def test_no_informal_address(self):
        found = _informal_texts()
        if found:
            self.fail("обращение на «ты» или повелительное ед. числа:\n" + "\n".join(found))

    def test_please_in_core_and_every_module(self):
        for name, text in _rule_texts().items():
            self.assertTrue("пожалуйста" in text.lower(), name)

    def test_hook_texts_found_by_code(self):
        # Сборщик видит каждый известный текст хука: пропуск — сбой разбора, а не отсутствие текста.
        import debug_watch
        import depcheck
        import judge_tool
        texts = set().union(*_hook_texts().values())
        known = (judge_tool.DEP_REASON, judge_tool.DEP_DOUBT_REASON, judge_tool.MANIFEST_REASON,
                 judge_tool.COMMAND_REASON, judge_tool.MCP_REASON, judge_tool.FILES_HINT, remind.NO_DOCS_LINE,
                 remind.CONTINUATION, debug_watch.LINE, depcheck._WHY_FLAG, depcheck._WHY_NAME)
        for text in known:
            self.assertIn(text, texts)

    def test_judge_prompts_start_with_address(self):
        # Промпты, которые хуки отдают судье: prompts.SYSTEM_PROMPT и функции prompts.*_prompt из кода хуков.
        used = set()
        for path in PLANKA_DIR.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "prompts" and node.attr.lower().endswith("_prompt")):
                    used.add(node.attr)
        self.assertGreaterEqual(used, {"SYSTEM_PROMPT", "question_prompt", "plan_prompt", "memory_prompt",
                                       "stop_prompt"})
        for name in sorted(used):
            value = getattr(prompts, name)
            if callable(value):
                params = inspect.signature(value).parameters.values()
                args = ["x" for p in params if p.kind is p.POSITIONAL_OR_KEYWORD and p.default is p.empty]
                kwargs = {p.name: True for p in params if p.kind is p.KEYWORD_ONLY and p.default is p.empty}
                value = value(*args, **kwargs)
            self.assertTrue(value.startswith(common.ADDRESS), name)

    def test_informal_words_found(self):
        cases = {
            "Спроси автора.": ["Спроси"],
            "Пожалуйста, не правь чужое и прочитай модуль.": ["правь", "прочитай"],
            "Если ты знаешь ответ, твоя очередь.": ["ты", "твоя", "знаешь"],
            "Спросите автора, пожалуйста; читайте модуль лишь раз.": [],
            "Цитата «не правь» и код `читай` — не обращение.": [],
            "Вложенная «цитата «спроси» внутри» тоже.": [],
            # Формы вне SINGULAR_IMPERATIVES — по месту указания.
            "Сверь файл. Откати правку.\n- Поставь маркер.": ["Сверь", "Откати", "Поставь"],
            "Не трогай тест; поставь маркер — и уходи.": ["трогай", "поставь", "уходи"],
            "Дальше иди, пожалуйста, по модулю; пожалуйста, вернись.": ["иди", "вернись"],
            # Не глаголы на месте указания: окончания NOT_IMPERATIVE и слова IMPERATIVE_EXCEPT.
            "Новый модуль. Правки, ключи и пути — если есть; мой дорогой друг, держитесь.": [],
            # Незакрытая «ёлочка» прячет только свой блок.
            "Это «незакрытая цитата.\n## Границы\nСпроси автора.": ["Спроси"],
            "Это «незакрытая цитата.\n\nСпроси автора.": ["Спроси"],
        }
        for text, words in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sorted(_informal_words(text)), sorted(words))

    def test_rule_quotes_balanced(self):
        # Незакрытая «ёлочка» прячет от test_no_informal_address остаток блока.
        for name, text in _rule_texts().items():
            self.assertEqual(_unbalanced_quotes(text), [], name)

    def test_unbalanced_quotes_found(self):
        self.assertEqual(_unbalanced_quotes("«а» и «б «в»»\n\n`«` код"), [])
        self.assertEqual(len(_unbalanced_quotes("Это «незакрытая.\n## Границы\nа»\n\nи » лишняя")), 3)

    def test_hook_requests_say_please(self):
        found = [f"{name}: {s}" for name, strings in sorted(_hook_texts().items()) for text in sorted(strings)
                 for s in _sentences_without_please(text)]
        if found:
            self.fail("просьба без «пожалуйста»:\n" + "\n".join(found))

    def test_rule_requests_say_please(self):
        found = [f"{name}: {' '.join(s.split())}" for name, text in sorted(_rule_texts().items())
                 for s in _sentences_without_please(text)]
        if found:
            self.fail("просьба без «пожалуйста»:\n" + "\n".join(found))

    def test_sentences_without_please_found(self):
        text = ("Пожалуйста, назовите пакет и\nповторите команду. Прочитайте модуль.\n- Спросите автора, "
                "пожалуйста.\n- Остановитесь.\n\nГраница задачи — то, что поставлено.")
        self.assertEqual(_sentences_without_please(text), ["Прочитайте модуль.", "- Остановитесь."])

    def test_hook_answer_built_only_in_common(self):
        # Ответ хука собирают common.deny_output, block_output, context_output: обращение к модели ставят они, и
        # ответ в обход них ушёл бы без него. Чтение готового ответа (out["hookSpecificOutput"]) — не сборка.
        keys = ("hookSpecificOutput", "permissionDecision", '"decision"')
        found = []
        for path in sorted(PLANKA_DIR.glob("*.py")):
            if path.name == "common.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            loads = {id(n.slice) for n in ast.walk(tree)
                     if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in loads:
                    value = node.value
                elif isinstance(node, ast.keyword) and node.arg:
                    value = node.arg
                else:
                    continue
                if value == "decision" or any(k in value for k in keys):
                    found.append(f"{path.name}:{node.lineno}: {value[:60]}")
        self.assertEqual(found, [])


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

    def test_names_resolved_in_own_scope(self):
        # Параметр функции — не одноимённая переменная другой функции; имя функции без присваивания в ней — имя
        # модуля.
        text = ('def f():\n    mods = ("a",)\ndef g(mods):\n    common.rule_texts(*mods)\n'
                'def h():\n    common.rule_texts(*mods)\nmods = ("b",)')
        self.assertEqual(self.names(text), (set(), {"b"}, ["x.py:4"]))

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

    def test_correct_option_first_checked(self):
        # Правило — plugin/philosophy.md, «Решения» 4: самый правильный вариант есть в списке и идёт первым.
        decisions = self.flat(common.philosophy_sections("Решения"))
        self.assertIn("самый правильный вариант обязан быть в списке, даже если он самый трудный и крупный, и идёт "
                      "первым", decisions)
        self.assertIn("есть ли он в списке и идёт ли он первым?", prompts._CHOICE_CHECKS)

    def test_fact_question_rule_matches_planning(self):
        # Ядро и plugin/rules/planning.md говорят одно: факт ищется, вопрос — только о нескольких кандидатах.
        specs = self.flat(common.philosophy_sections("Спецификации"))
        self.assertIn("спрашивается, только если после поиска осталось несколько кандидатов", specs)
        self.assertIn("спрашивается то, где после поиска осталось несколько кандидатов", self.flat(
            self.rules["planning"]))

    def test_positive_statement_rule_names_its_form(self):
        # Правило запрещает начинать с отрицания; форма «X, а не Y» в ядре — утверждение с контрастом.
        behaviour = self.flat(common.philosophy_sections("Поведение"))
        self.assertIn("без «не X, а Y» — отрицания в начале; «X, а не Y» — утверждение", behaviour)

    def test_affected_tests_only_by_default(self):
        # plugin/philosophy.md («Планы» 6) и шлюз plugin/rules/verification.md требуют прогона тестов затронутого
        # функционала и полного набора только по просьбе автора.
        plans = self.flat(common.philosophy_sections("Планы"))
        self.assertIn("гоняет проверки затронутого волной функционала", plans)
        self.assertIn("Полный набор — только по просьбе автора", plans)
        gate = " ".join(common.rule_texts("verification").split())
        self.assertIn("это прогон тестов затронутого функционала", gate)
        self.assertIn("Полный прогон — только по просьбе автора", gate)

    def test_recommendation_premises_checked(self):
        # Правило — plugin/philosophy.md, «Решения» 4; судья Stop его проверяет (видит шаги реплики), судьям вопроса
        # и плана пункт вырезается из рубрики (prompts.without_premises).
        decisions = common.philosophy_sections("Решения")
        self.assertIn("у опоры рекомендации — факта о коде, данных, поведении — назван источник",
                      " ".join(decisions.split()))
        self.assertIn("назван источник («Решения» 4", prompts._MESSAGE_CHECKS)
        stripped = prompts.without_premises(decisions)
        self.assertNotIn("опоры рекомендации", stripped)
        self.assertEqual(stripped.count("\n") + 4, decisions.count("\n"))
        self.assertIn("после него остаётся недоделанным;", stripped)
        self.assertIn("5. Правильное не значит большее.", stripped)

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


def _readme_missing(readme, name, values):
    """Значения values, которых нет в блоках README (BLOCK_START), где упомянута константа name."""
    blocks = [b for b in BLOCK_START.split(readme) if name in b]
    return sorted(v for v in values
                  if not any(re.search(r"(?<![\w.-])" + re.escape(v) + r"(?![\w-])", b) for b in blocks))


class ReadmeCopiesTest(unittest.TestCase):
    """README перечисляет значения списков кода, которые названы рядом: копию сверяет тест."""

    def test_lists_named_in_readme_complete(self):
        import manifests
        import snapshot
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        lists = {"snapshot.IGNORED_DIRS": snapshot.IGNORED_DIRS,
                 "manifest_watch.FOREIGN_DIRS": set(manifest_watch.FOREIGN_DIRS) - set(snapshot.IGNORED_DIRS),
                 "manifests._BUILD_BACKENDS": manifests._BUILD_BACKENDS, "CODE_NAMES": common.CODE_NAMES}
        for name, values in lists.items():
            with self.subTest(name=name):
                self.assertEqual(_readme_missing(readme, name, values), [])

    def test_readme_missing_found(self):
        readme = "Каталоги `snapshot.IGNORED_DIRS` (`.git`, `build`).\n\nИ `dist` в другом блоке."
        self.assertEqual(_readme_missing(readme, "snapshot.IGNORED_DIRS", {".git", "build", "dist", "git"}),
                         ["dist", "git"])


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
        # Срок снимка отсчитывается от старта хука, определение корня входит в него; снимок снимает только часть 1.
        self.assertLess(remind.SNAPSHOT_BUDGET, _core_part_hooks()["1"])

    def test_bash_manifest_snapshot_fits(self):
        # Снимок манифестов перед командой Bash: корень проекта git rev-parse, затем срок снимка; правка
        # манифеста файловым инструментом — корень проекта, перечень манифестов (manifest_watch.listing,
        # HEAD_TIMEOUT), git cat-file версий из HEAD и обход манифестов проекта (judge_tool._project_names) первого
        # манифеста. Второй и следующий манифест одного файла начинается, только если и он уложится в
        # judge_tool.LINKED_BUDGET от started: started снят до корня и перечня, срок покрывает и их.
        import judge_tool
        for timeout in self.timeouts[("PreToolUse", "judge_tool")]:
            self.assertLess(common.GIT_ROOT_TIMEOUT + manifest_watch.SNAPSHOT_BUDGET, timeout)
            self.assertLess(2 * manifest_watch.HEAD_TIMEOUT + common.GIT_ROOT_TIMEOUT
                            + manifest_watch.SNAPSHOT_BUDGET, timeout)
            self.assertLess(judge_tool.LINKED_BUDGET, timeout)

    def test_linked_budget_counts_from_hook_work(self):
        # LINKED_BUDGET покрывает корень проекта и перечень, только если started снят до manifest_watch.listing и
        # edit_targets в judge_tool.judge_manifest_edit.
        tree = ast.parse((PLANKA_DIR / "judge_tool.py").read_text(encoding="utf-8"))
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "judge_manifest_edit")
        started = min(n.lineno for n in ast.walk(func) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "started" for t in n.targets))
        calls = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("listing", "edit_targets")]
        self.assertTrue(calls)
        self.assertLess(started, min(calls))

    def test_post_tool_use_fits(self):
        for event in ("PostToolUse", "PostToolUseFailure"):
            for timeout in self.timeouts[(event, "judge_tool")]:
                self.assertLess(manifest_watch.CHECK_BUDGET, timeout, event)

    def test_model_watch_fits(self):
        # model_watch не зовёт ни судью, ни git, ни других процессов: срок — запись файла под state_lock.
        tree = ast.parse((PLANKA_DIR / "model_watch.py").read_text(encoding="utf-8"))
        names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(tree)
                 if isinstance(n, (ast.Attribute, ast.Name))}
        self.assertTrue(names.isdisjoint({"run_judge", "subprocess", "project_root", "_git_out"}))
        for event in ("SessionStart", "PostModelSwitch"):
            self.assertEqual(self.timeouts[(event, "model_watch")], [10], event)

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


class SourceWarningsTest(unittest.TestCase):
    """Модули хуков компилируются без предупреждений: SyntaxWarning (escape вроде «\\`» в строке) Python пишет в
    stderr при первой компиляции, а хук stderr не пишет. Компилируется текст, а не импорт: __pycache__ не прячет."""

    def test_modules_compile_without_warnings(self):
        import warnings
        for path in sorted(PLANKA_DIR.glob("*.py")):
            with self.subTest(path.name), warnings.catch_warnings():
                warnings.simplefilter("error")
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
