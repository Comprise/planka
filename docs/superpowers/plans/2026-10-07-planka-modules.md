# planka modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Модули правил `rules/*.md` принудительно подгружаются судьями: план судится с модулями планирования и субагентов, заявка «готово» на `Stop` — с модулем проверки, добавление пакета в Bash отклоняется детерминированно; напоминание подставляет путь к модулям.

**Architecture:** Тексты ядра и модулей уже в репозитории (коммит 7f94212). Код: `common.py` получает чтение модулей и подстановку `{RULES}`; новый `depcheck.py` разбирает команду Bash; `prompts.py` получает вопросы судьи «готово» и сборку промпта `Stop` из двух фильтров; `judge_tool.py` обслуживает `Bash` и расширенную рубрику плана; `judge_stop.py` получает второй фильтр; `hooks.json` — матчер с `Bash`.

**Tech Stack:** Python 3 stdlib (`re`, `shlex`, `json`, `unittest`), подмена `claude` в `tests/stub/claude`, `make test` = `python3 -m unittest discover -s tests -t .`.

**Spec:** `docs/superpowers/specs/2026-10-07-planka-modules-design.md` (и базовая `2026-09-30-planka-design.md`).

## Global Constraints

- Единственный источник правил — `philosophy.md` и `rules/*.md`; ни один скрипт и промпт не дублирует их текст. Тексты в этом плане не правятся.
- Все скрипты при `PLANKA_OFF=1` или `PLANKA_JUDGE=1` выходят с кодом 0 без вывода до чтения stdin; любое внутреннее исключение — `planka: внутренняя ошибка` на stderr и код 0 (`common.run_hook`).
- Любая ошибка судьи — пропуск с `planka:` в stderr и записью `verdict: skipped` в журнал; никогда не отказ.
- Отсутствующий модуль или раздел ядра — пропуск судьи с `planka:` и `verdict: skipped`, никогда не отказ.
- Детерминированный отказ по зависимостям не расходует лимит отказов и не зовёт модель.
- Форматы вывода хуков — как в базовой спеке. Комментарии в коде — по факту, на русском, без истории.
- Тесты не зовут настоящий `claude`; `tests/helpers.Env` пишет свои `philosophy.md` и `rules/*.md`.
- Коммиты: только свои файлы по пути; без трейлеров соавторства.

## Review Focus

1. `{RULES}` в ядре, но каталога `rules/` нет на диске — напоминание всё равно подставляет путь, судья плана пропускает с `planka:` и `skipped`. Тесты в задачах 1 и 4.
2. Команда Bash с маркером `PLANKA_DEP_OK=1` после `&&` или в середине — пропуск. Тест в задаче 2.
3. Сообщение `Stop`, совпавшее с обоими фильтрами, — один вызов судьи, обе рубрики, оба набора вопросов в промпте. Тест в задаче 5.
4. `tool_input.command` отсутствует или не строка — пропуск без трассировки. Тест в задаче 4.
5. `shlex.split` падает на незакрытой кавычке — сегмент разбирается грубым `str.split`, не исключением. Тест в задаче 2.

## Структура файлов

```text
planka/common.py        — rules_dir, rule_texts, rubric, подстановка {RULES}   (задача 1)
tests/helpers.py        — Env пишет rules/*.md                                  (задача 1)
tests/test_common.py                                                             (задача 1)
planka/depcheck.py      — dependency_add(command)                                (задача 2)
tests/test_depcheck.py                                                           (задача 2)
planka/prompts.py       — done_prompt, stop_prompt                               (задача 3)
tests/test_prompts.py                                                            (задача 3)
planka/judge_tool.py    — judge_bash, рубрика плана с модулями                  (задача 4)
hooks/hooks.json        — матчер Bash                                            (задача 4)
tests/test_judge_tool.py                                                         (задача 4)
planka/judge_stop.py    — claims_done, объединённая рубрика                      (задача 5)
tests/test_judge_stop.py                                                         (задача 5)
tests/test_remind.py    — путь к rules в контексте                               (задача 6)
README.md                                                                        (задача 7)
```

## Волны

| Волна | Задачи | Зависимости |
| --- | --- | --- |
| 1 | тексты ядра и модулей — выполнена координатором (7f94212) | — |
| 2 | 1 common, 2 depcheck, 3 prompts | контракты ниже; файлы не пересекаются |
| 3 | 4 judge_tool+hooks.json, 5 judge_stop, 6 test_remind | контракты волны 2 |
| 4 | 7 README | всё |

Схождение после каждой волны: `make test` и `make validate` координатором. При проблеме исполнитель останавливается и предлагает варианты по «Решениям».

---

## Волна 2

### Task 1: `common.py` — модули и подстановка `{RULES}`

**Files:**
- Modify: `planka/common.py`
- Modify: `tests/helpers.py`
- Modify: `tests/test_common.py`

**Interfaces:**
- Consumes: существующие `plugin_root`, `philosophy_text`, `philosophy_sections`, `warn`.
- Produces:

```python
RULES_PLACEHOLDER = "{RULES}"

def rules_dir() -> pathlib.Path          # plugin_root() / "rules"
def philosophy_text() -> str | None      # как прежде, но RULES_PLACEHOLDER заменён на str(rules_dir())
def rule_texts(*names: str) -> str | None
    # rules/<name>.md для каждого имени, в порядке names, склеенные пустой строкой;
    # None и warn при отсутствии файла или не-UTF-8.
def rubric(sections: tuple[str, ...], modules: tuple[str, ...]) -> str | None
    # philosophy_sections(*sections) и rule_texts(*modules), склеенные пустой строкой;
    # пустой кортеж — соответствующая часть пропускается; None, если любая часть None.
```

`tests/helpers.py`: `Env(philosophy=PHILOSOPHY, rules=RULES)` — `RULES` это dict имя→текст, по умолчанию
`{"verification": "# Доказательство\n\n- Правило проверки.\n", "planning": "# Планирование\n\n- Правило планирования.\n", "subagents": "# Субагенты\n\n- Правило субагентов.\n"}`; файлы пишутся в `root/rules/<имя>.md`; `rules=None` — каталог не создаётся. `PHILOSOPHY` получает в конец раздел `## Модули\n\nКаталог: {RULES}\n`.

- [ ] **Step 1: Красные тесты** — добавить в `tests/test_common.py`:

```python
class RulesTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_placeholder_replaced_with_rules_dir(self):
        text = common.philosophy_text()
        self.assertNotIn("{RULES}", text)
        self.assertIn(str(self.env.root / "rules"), text)

    def test_rule_texts_in_order(self):
        text = common.rule_texts("planning", "verification")
        self.assertTrue(text.startswith("# Планирование"))
        self.assertIn("# Доказательство", text)
        self.assertLess(text.index("# Планирование"), text.index("# Доказательство"))

    def test_missing_rule_is_none(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("verification", "nope"))
            self.assertIn("planka:", err.getvalue())

    def test_non_utf8_rule_is_none(self):
        (self.env.root / "rules" / "verification.md").write_bytes(b"\xff\xfe")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("verification"))
            self.assertIn("planka:", err.getvalue())

    def test_rubric_combines(self):
        text = common.rubric(("Решения", "Планы"), ("planning",))
        self.assertTrue(text.startswith("## Решения"))
        self.assertIn("## Планы", text)
        self.assertIn("# Планирование", text)
        self.assertLess(text.index("## Планы"), text.index("# Планирование"))

    def test_rubric_sections_only_and_modules_only(self):
        self.assertTrue(common.rubric(("Решения",), ()).startswith("## Решения"))
        self.assertTrue(common.rubric((), ("verification",)).startswith("# Доказательство"))

    def test_rubric_none_when_part_missing(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertIsNone(common.rubric(("Решения",), ("nope",)))
            self.assertIsNone(common.rubric(("Нет такого",), ("planning",)))

    def test_rules_dir_missing_is_none(self):
        import shutil
        shutil.rmtree(self.env.root / "rules")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(common.rule_texts("planning"))
            self.assertIn("planka:", err.getvalue())
        self.assertIn(str(self.env.root / "rules"), common.philosophy_text())
```

Добавить `import io` в начало файла, если его нет (сейчас тесты используют `__import__("io")`; допустимо привести к `import io`).

- [ ] **Step 2: Убедиться, что красные**

Run: `python3 -m unittest tests.test_common.RulesTest 2>&1 | tail -3`
Expected: `AttributeError: module 'common' has no attribute 'rule_texts'` (или `rubric`), и падение `test_placeholder_replaced_with_rules_dir`.

- [ ] **Step 3: `helpers.py`** — `PHILOSOPHY` дополняется:

```python
## Модули

Каталог: {RULES}
```

и:

```python
RULES = {
    "verification": "# Доказательство\n\n- Правило проверки.\n",
    "planning": "# Планирование\n\n- Правило планирования.\n",
    "subagents": "# Субагенты\n\n- Правило субагентов.\n",
}


class Env:
    def __init__(self, philosophy=PHILOSOPHY, rules=RULES):
        ...
        if philosophy is not None:
            (self.root / "philosophy.md").write_text(philosophy, encoding="utf-8")
        if rules is not None:
            (self.root / "rules").mkdir()
            for name, text in rules.items():
                (self.root / "rules" / f"{name}.md").write_text(text, encoding="utf-8")
```

- [ ] **Step 4: `common.py`**

```python
RULES_PLACEHOLDER = "{RULES}"


def rules_dir():
    return plugin_root() / "rules"


def philosophy_text():
    p = plugin_root() / "philosophy.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        warn(f"нет файла правил {p}")
        return None
    except UnicodeDecodeError:
        warn(f"файл правил {p} не в UTF-8")
        return None
    return text.replace(RULES_PLACEHOLDER, str(rules_dir()))


def rule_texts(*names):
    """Модули rules/<name>.md в порядке names; None, если хоть одного нет."""
    out = []
    for name in names:
        p = rules_dir() / f"{name}.md"
        try:
            out.append(p.read_text(encoding="utf-8").strip())
        except OSError:
            warn(f"нет модуля правил {p}")
            return None
        except UnicodeDecodeError:
            warn(f"модуль правил {p} не в UTF-8")
            return None
    return "\n\n".join(out)


def rubric(sections, modules):
    """Рубрика судьи: разделы ядра, затем модули; None, если любая часть недоступна."""
    parts = []
    if sections:
        core = philosophy_sections(*sections)
        if core is None:
            return None
        parts.append(core)
    if modules:
        mods = rule_texts(*modules)
        if mods is None:
            return None
        parts.append(mods)
    return "\n\n".join(parts)
```

- [ ] **Step 5: Зелёные** — Run: `python3 -m unittest tests.test_common -v 2>&1 | tail -3` → `OK`. Затем `python3 -m unittest tests.test_remind tests.test_judge_stop tests.test_judge_tool 2>&1 | tail -3` → `OK` (существующие тесты не ломаются от нового `Env`).

- [ ] **Step 6: Commit** — `git add planka/common.py tests/helpers.py tests/test_common.py && git commit -m "feat: чтение модулей правил и подстановка каталога rules в ядро"`

---

### Task 2: `depcheck.py` — разбор команды на добавление пакета

**Files:**
- Create: `planka/depcheck.py`
- Create: `tests/test_depcheck.py`

**Interfaces:**
- Consumes: ничего.
- Produces:

```python
DEP_OK_MARKER = "PLANKA_DEP_OK=1"

def dependency_add(command: str) -> str | None
    # Сегмент команды, добавляющий пакет, или None. Маркер в команде → None.
```

- [ ] **Step 1: Красные тесты** — `tests/test_depcheck.py`:

```python
import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import depcheck  # noqa: E402


class DependencyAddTest(unittest.TestCase):
    def test_adds_detected(self):
        for cmd in [
            "go get github.com/x/y@v1",
            "npm install left-pad",
            "npm i -D typescript",
            "npm add lodash",
            "pnpm add react",
            "yarn add react",
            "pip install requests",
            "pip3 install --upgrade requests",
            "python -m pip install requests",
            "python3 -m pip install requests",
            "uv add httpx",
            "uv pip install httpx",
            "poetry add httpx",
            "cargo add serde",
            "gem install rails",
            "bundle add rails",
            "composer require monolog/monolog",
            "sudo npm install -g pnpm",
            "CI=1 npm install left-pad",
            "cd app && npm install left-pad",
            "make build; pip install requests",
            "echo x | npm install left-pad",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_returns_offending_segment(self):
        self.assertEqual(depcheck.dependency_add("cd app && npm install left-pad"), "npm install left-pad")

    def test_not_adds(self):
        for cmd in [
            "npm ci",
            "npm install",
            "npm install --frozen-lockfile",
            "pnpm install",
            "pip install -r requirements.txt",
            "pip install --requirement requirements.txt",
            "pip install -e .",
            "pip install .",
            "pip install ./pkg",
            "pip install /abs/pkg",
            "go build ./...",
            "go get",
            "git add -A",
            "npm run build",
            "cargo build",
            "gem list",
            "echo 'npm install left-pad'",
            "",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_marker_passes(self):
        self.assertIsNone(depcheck.dependency_add("PLANKA_DEP_OK=1 npm install left-pad"))
        self.assertIsNone(depcheck.dependency_add("cd app && PLANKA_DEP_OK=1 npm install left-pad"))

    def test_unclosed_quote_does_not_raise(self):
        self.assertIsNotNone(depcheck.dependency_add("npm install left-pad 'oops"))
        self.assertIsNone(depcheck.dependency_add("echo 'oops"))

    def test_non_string(self):
        self.assertIsNone(depcheck.dependency_add(None))
        self.assertIsNone(depcheck.dependency_add(5))
```

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_depcheck 2>&1 | tail -2` → `ModuleNotFoundError: No module named 'depcheck'`.

- [ ] **Step 3: `depcheck.py`**

```python
"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import re
import shlex

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP_REQ_FLAGS = {"-r", "--requirement"}


def _words(segment):
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] == "sudo"):
        words.pop(0)
    return words


def _positional(args):
    """Первое слово-аргумент, похожее на имя пакета, а не на флаг или локальный путь."""
    for w in args:
        if w.startswith("-") or w in (".", "..") or w.startswith(("./", "/")):
            continue
        return w
    return None


def _pip_add(args):
    if _PIP_REQ_FLAGS & set(args):
        return False
    return _positional(args) is not None


def _is_add(words):
    if not words:
        return False
    w = words
    if w[0] == "go" and len(w) > 1 and w[1] == "get":
        return _positional(w[2:]) is not None
    if w[0] == "npm" and len(w) > 1 and w[1] in ("install", "i", "add"):
        return _positional(w[2:]) is not None
    if w[0] == "pnpm" and len(w) > 1 and w[1] in ("add", "install", "i"):
        return _positional(w[2:]) is not None
    if w[0] == "yarn" and len(w) > 1 and w[1] == "add":
        return _positional(w[2:]) is not None
    if w[0] in ("pip", "pip3") and len(w) > 1 and w[1] == "install":
        return _pip_add(w[2:])
    if w[0].startswith("python") and len(w) > 3 and w[1] == "-m" and w[2] == "pip" and w[3] == "install":
        return _pip_add(w[4:])
    if w[0] == "uv" and len(w) > 1:
        if w[1] == "add":
            return _positional(w[2:]) is not None
        if w[1] == "pip" and len(w) > 2 and w[2] == "install":
            return _pip_add(w[3:])
    if w[0] in ("poetry", "cargo", "bundle") and len(w) > 1 and w[1] == "add":
        return _positional(w[2:]) is not None
    if w[0] == "gem" and len(w) > 1 and w[1] == "install":
        return _positional(w[2:]) is not None
    if w[0] == "composer" and len(w) > 1 and w[1] == "require":
        return _positional(w[2:]) is not None
    return False


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет или стоит маркер согласия."""
    if not isinstance(command, str) or DEP_OK_MARKER in command:
        return None
    for segment in _SPLIT.split(command):
        segment = segment.strip()
        if segment and _is_add(_words(segment)):
            return segment
    return None
```

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_depcheck -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/depcheck.py tests/test_depcheck.py && git commit -m "feat: разбор команды на добавление пакета"`

---

### Task 3: `prompts.py` — вопросы судьи «готово» и промпт `Stop`

**Files:**
- Modify: `planka/prompts.py`
- Modify: `tests/test_prompts.py`

**Interfaces:**
- Consumes: существующие `_wrap`, `_QUESTION_CHECKS`.
- Produces:

```python
def done_prompt(rubric: str, content: str) -> str
def stop_prompt(rubric: str, content: str, *, options: bool, done: bool) -> str
    # options и done — какие фильтры совпали; вопросы объединяются в порядке options, done;
    # хотя бы один обязан быть True, иначе ValueError.
```

Текст вопросов «готово» (константа `_DONE_CHECKS`):

```text
Проверь заявку о выполненной работе по рубрике и ответь:
1. Названа ли команда-доказательство и процитирован ли её увиденный вывод?
2. Взята ли команда из CI-конфига, манифеста или task runner, а не восстановлена по памяти?
3. Названо ли, что не проверено и почему, или успех подразумевается?
4. Если это фикс бага — прогнан ли исходный падающий сценарий?
5. Числа и подсчёты — из вывода команды, а не из головы?
Сообщение, которое не заявляет о выполненной работе (например, «готов обсудить»), соответствует рубрике.
```

- [ ] **Step 1: Красные тесты** — добавить в `tests/test_prompts.py`:

```python
class DonePromptTest(unittest.TestCase):
    def test_done_prompt(self):
        p = prompts.done_prompt("РУБРИКА", "СОДЕРЖИМОЕ")
        self.assertIn("РУБРИКА", p)
        self.assertIn("<content>\nСОДЕРЖИМОЕ\n</content>", p)
        for needle in ["команда-доказательство", "CI-конфиг", "не проверено",
                       "исходный падающий сценарий", "из вывода команды", "готов обсудить"]:
            self.assertIn(needle, p)

    def test_stop_prompt_combines(self):
        p = prompts.stop_prompt("R", "C", options=True, done=True)
        self.assertIn("самый правильный", p)
        self.assertIn("команда-доказательство", p)
        self.assertLess(p.index("самый правильный"), p.index("команда-доказательство"))
        self.assertEqual(p.count("<content>"), 1)

    def test_stop_prompt_single(self):
        self.assertNotIn("команда-доказательство", prompts.stop_prompt("R", "C", options=True, done=False))
        self.assertNotIn("самый правильный", prompts.stop_prompt("R", "C", options=False, done=True))

    def test_stop_prompt_requires_a_filter(self):
        with self.assertRaises(ValueError):
            prompts.stop_prompt("R", "C", options=False, done=False)
```

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_prompts.DonePromptTest 2>&1 | tail -2` → `AttributeError: ... 'done_prompt'`.

- [ ] **Step 3: `prompts.py`** — добавить после `_MESSAGE_CHECKS`:

```python
_DONE_CHECKS = """Проверь заявку о выполненной работе по рубрике и ответь:
1. Названа ли команда-доказательство и процитирован ли её увиденный вывод?
2. Взята ли команда из CI-конфига, манифеста или task runner, а не восстановлена по памяти?
3. Названо ли, что не проверено и почему, или успех подразумевается?
4. Если это фикс бага — прогнан ли исходный падающий сценарий?
5. Числа и подсчёты — из вывода команды, а не из головы?
Сообщение, которое не заявляет о выполненной работе (например, «готов обсудить»), соответствует рубрике."""


def done_prompt(rubric, content):
    return _wrap(rubric, _DONE_CHECKS, content)


def stop_prompt(rubric, content, *, options, done):
    """Промпт судьи на Stop: вопросы по совпавшим фильтрам в порядке options, done."""
    checks = [c for flag, c in ((options, _MESSAGE_CHECKS), (done, _DONE_CHECKS)) if flag]
    if not checks:
        raise ValueError("ни один фильтр Stop не совпал")
    return _wrap(rubric, "\n\n".join(checks), content)
```

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_prompts -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/prompts.py tests/test_prompts.py && git commit -m "feat: вопросы судьи о готовности и промпт Stop по двум фильтрам"`

---

**Схождение волны 2:** `make test`, `make validate`.

---

## Волна 3

### Task 4: `judge_tool.py` — Bash и рубрика плана с модулями

**Files:**
- Modify: `planka/judge_tool.py`
- Modify: `hooks/hooks.json`
- Modify: `tests/test_judge_tool.py`

**Interfaces:**
- Consumes: `common.rubric(sections, modules)`, `common.rules_dir()`, `depcheck.dependency_add`, `depcheck.DEP_OK_MARKER`, `prompts.plan_prompt`.
- Produces: `judge_bash(data)`; константа `DEP_REASON` (шаблон с `{rules}` и `{command}`).

- [ ] **Step 1: Красные тесты** — добавить в `tests/test_judge_tool.py`:

```python
class BashTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def bash(self, command, **extra):
        return self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="b1"), **extra)

    def test_dependency_add_denied_without_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.bash("cd app && npm install left-pad", PLANKA_STUB_RECORD=str(rec))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("вопрос автору", out["permissionDecisionReason"])
        self.assertIn("npm install left-pad", out["permissionDecisionReason"])
        self.assertIn("PLANKA_DEP_OK=1", out["permissionDecisionReason"])
        self.assertIn(str(self.env.root / "rules" / "dependencies.md"), out["permissionDecisionReason"])
        self.assertFalse(rec.exists())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny-dep")

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
            self.assertIn("deny", r.stdout)

    def test_missing_or_non_string_command_passes(self):
        for ti in ({}, {"command": None}, {"command": 5}):
            r = self.env.run("judge_tool.py", hook_input("PreToolUse", tool_name="Bash", tool_input=ti))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertNotIn("Traceback", r.stderr)


class PlanRubricTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def with_plan(self, text):
        plan = self.env.data / "plan.md"
        plan.write_text(text, encoding="utf-8")
        t = self.env.data / "t.jsonl"
        t.write_text(json.dumps({"type": "attachment", "attachment": {
            "type": "plan_mode", "planFilePath": str(plan)}}) + "\n", encoding="utf-8")
        return str(t)

    def test_plan_judge_gets_modules(self):
        rec = self.env.data / "rec.txt"
        r = self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=self.with_plan(PLAN_CLEAN)),
            PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        for needle in ["## Решения", "## Планы", "# Планирование", "# Субагенты"]:
            self.assertIn(needle, text)
        self.assertLess(text.index("## Планы"), text.index("# Планирование"))

    def test_plan_judge_skips_without_module(self):
        (self.env.root / "rules" / "subagents.md").unlink()
        r = self.env.run("judge_tool.py", hook_input(
            "PreToolUse", tool_name="ExitPlanMode", tool_input={}, transcript_path=self.with_plan(PLAN_CLEAN)))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
```

`PLAN_CLEAN` уже определён в файле.

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_judge_tool.BashTest tests.test_judge_tool.PlanRubricTest 2>&1 | tail -3` → падения (Bash проходит молча, модули не в промпте).

- [ ] **Step 3: `judge_tool.py`**

Добавить `import depcheck` и:

```python
DEP_REASON = ("Новая зависимость — вопрос автору (ядро, «Границы»): назови пакет, зачем он и что "
              "из stdlib или уже установленного задачу не закрывает; прочитай {rules}/dependencies.md. "
              "После согласия автора повтори команду с префиксом {marker}. Команда: {command}")


def judge_bash(data):
    """Добавление пакета отклоняется детерминированно: без модели и без лимита отказов."""
    command = (data.get("tool_input") or {}).get("command")
    segment = depcheck.dependency_add(command)
    if segment is None:
        return
    session = data.get("session_id", "")
    reason = DEP_REASON.format(rules=common.rules_dir(), marker=depcheck.DEP_OK_MARKER, command=segment)
    common.log_event("bash", session, verdict="deny-dep", reason=reason, content=command)
    common.emit(common.deny_output(reason))
```

В `judge_plan` заменить `rubric = common.philosophy_sections("Решения", "Планы")` на
`rubric = common.rubric(("Решения", "Планы"), ("planning", "subagents"))`; ветка `rubric is None`
остаётся (предупреждение уже выдано `common`). В `main` добавить `elif tool == "Bash": judge_bash(data)`.
Докстринг модуля: «PreToolUse: судья вопросов автору (AskUserQuestion), планов (ExitPlanMode) и
детерминированная проверка команд Bash».

- [ ] **Step 4: `hooks.json`** — матчер `"AskUserQuestion|ExitPlanMode|Bash"`.

- [ ] **Step 5: Зелёные** — Run: `python3 -m unittest tests.test_judge_tool -v 2>&1 | tail -3` → `OK`; `claude plugin validate .` → без ошибок.

- [ ] **Step 6: Commit** — `git add planka/judge_tool.py hooks/hooks.json tests/test_judge_tool.py && git commit -m "feat: отказ на добавление пакета в Bash; план судится с модулями планирования и субагентов"`

---

### Task 5: `judge_stop.py` — фильтр «готово» и объединённая рубрика

**Files:**
- Modify: `planka/judge_stop.py`
- Modify: `tests/test_judge_stop.py`

**Interfaces:**
- Consumes: `common.rubric`, `prompts.stop_prompt`.
- Produces: `claims_done(text) -> bool`.

Регулярное выражение фильтра (без учёта регистра, по границам слов):
`готов[оаы]?|сделан[оаы]?|исправлен[оаы]?|починен[оаы]?|проход[яи]т|прош[её]л|прошли|зел[её]н\w*|done|fixed|passing|passes|completed?`

- [ ] **Step 1: Красные тесты** — добавить в `tests/test_judge_stop.py`:

```python
DONE_MSG = "Готово: тесты зелёные, 82/82."
BOTH_MSG = OPTIONS_MSG + "\n\nПервый вариант уже сделан."


class DoneFilterTest(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(judge_stop.claims_done(DONE_MSG))
        self.assertTrue(judge_stop.claims_done("Fixed, all tests passing."))
        self.assertTrue(judge_stop.claims_done("Исправлено."))
        self.assertFalse(judge_stop.claims_done("Готовлю план."))
        self.assertFalse(judge_stop.claims_done("Смотрю, что сломалось."))
        self.assertFalse(judge_stop.claims_done(""))
        self.assertFalse(judge_stop.claims_done(None))


class DoneHookTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def stop(self, msg, **extra):
        return self.env.run("judge_stop.py",
                            hook_input("Stop", last_assistant_message=msg, stop_hook_active=False), **extra)

    def test_done_judged_with_verification_module(self):
        rec = self.env.data / "rec.txt"
        r = self.stop(DONE_MSG, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("# Доказательство", text)
        self.assertNotIn("## Решения", text)
        self.assertIn("команда-доказательство", text)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["done"])

    def test_done_denied_blocks(self):
        r = self.stop(DONE_MSG, PLANKA_STUB="deny", PLANKA_STUB_REASON="нет команды-доказательства")
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("нет команды-доказательства", out["reason"])

    def test_both_filters_one_call(self):
        rec = self.env.data / "rec.txt"
        r = self.stop(BOTH_MSG, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertIn("# Доказательство", text)
        self.assertIn("самый правильный", text)
        self.assertIn("команда-доказательство", text)
        self.assertEqual(text.count("<content>"), 1)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["options", "done"])

    def test_done_without_module_skips(self):
        (self.env.root / "rules" / "verification.md").unlink()
        r = self.stop(DONE_MSG)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
```

Существующий `test_plain_message_passes_without_judge` использует `PLAIN_MSG = "Готово, тесты зелёные."`, который теперь совпадает с фильтром «готово». Заменить `PLAIN_MSG` на `"Смотрю, что сломалось."` и оставить тест как есть.

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_judge_stop 2>&1 | tail -3` → `AttributeError: ... 'claims_done'`.

- [ ] **Step 3: `judge_stop.py`**

```python
_DONE = re.compile(r"\b(?:готов[оаы]?|сделан[оаы]?|исправлен[оаы]?|починен[оаы]?|проход[яи]т|"
                   r"прош[её]л|прошли|зел[её]н\w*|done|fixed|passing|passes|completed?)\b",
                   re.IGNORECASE)


def claims_done(text):
    return bool(_DONE.search(text or ""))
```

В `main` после чтения `message`:

```python
    options = looks_like_options(message)
    done = claims_done(message)
    if not options and not done:
        return
    filters = [name for flag, name in ((options, "options"), (done, "done")) if flag]
    session = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")
    rubric = common.rubric(("Решения",) if options else (), ("verification",) if done else ())
    if rubric is None:
        common.log_event("stop", session, verdict="skipped", error="нет раздела рубрики", filters=filters)
        return
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT,
                               prompts.stop_prompt(rubric, message, options=options, done=done))
```

и во все четыре `log_event` ниже добавить `filters=filters`. Докстринг модуля: «Stop: сообщение со
списком вариантов судится по «Решениям», заявка о готовности — по модулю проверки; при совпадении обоих
фильтров — один вызов».

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_judge_stop -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/judge_stop.py tests/test_judge_stop.py && git commit -m "feat: заявка о готовности на Stop судится по модулю проверки"`

---

### Task 6: `test_remind.py` — путь к модулям в контексте

**Files:**
- Modify: `tests/test_remind.py`

**Interfaces:** consumes поведение `common.philosophy_text` из задачи 1; `remind.py` не меняется.

- [ ] **Step 1: Тест** — добавить в `RemindTest`:

```python
    def test_rules_dir_substituted(self):
        r = self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="привет"))
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("{RULES}", ctx)
        self.assertIn(str(self.env.root / "rules"), ctx)
```

- [ ] **Step 2: Зелёный** — Run: `python3 -m unittest tests.test_remind -v 2>&1 | tail -3` → `OK` (тест зелёный сразу: поведение дала задача 1; это тест-фиксация, не TDD-дефект).

- [ ] **Step 3: Commit** — `git add tests/test_remind.py && git commit -m "test: напоминание подставляет путь к модулям"`

---

**Схождение волны 3:** `make test`, `make validate`.

---

## Волна 4

### Task 7: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** В разделе «Как это работает» описать: ядро и `rules/`, индекс с путём, привязки судей (таблица из спеки §4), фильтр «готово», хук зависимостей с маркером `PLANKA_DEP_OK=1`. В «Известные ограничения» добавить: модули `debugging`, `memory`, `refactoring`, `design-patterns` без хука — рекомендательные, долг; фильтр «готово» — эвристика по словам; детектор зависимостей знает перечисленные менеджеры и не видит пакет в переменной. В «Переменные» ничего не добавлять (маркер — не переменная окружения, а текст команды). Каждое утверждение сверить с кодом.

- [ ] **Step 2:** Commit — `git add README.md && git commit -m "docs: модули правил, судья готовности и хук зависимостей в README"`

**Схождение волны 4:** `make test`, `make validate`, живая проверка автором по спеке §8.
