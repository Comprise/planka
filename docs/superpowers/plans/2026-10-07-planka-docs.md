# planka docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Судья на `Stop` принуждает сверку документации и комментариев после каждой правки кода по снимку дерева; напоминание подставляет языки из настроек плагина и предлагает инициализацию в проекте без `CLAUDE.md`; извлечённые комментарии изменённых файлов попадают судье.

**Architecture:** Тексты (`rules/docs.md`, `rules/comments.md`, ядро, манифест) уже в репозитории (7c7be85). Код: `common.py` — настройки и подстановка меток, корень проекта, классификация путей; новый `snapshot.py` — обход дерева, запись и сравнение; новый `comments.py` — извлечение строк комментариев; `prompts.py` — вопросы «документация» и третий флаг `stop_prompt`; `remind.py` — снимок, строка про отсутствие `CLAUDE.md`, предупреждение о языке; `judge_stop.py` — третий фильтр.

**Tech Stack:** Python 3 stdlib (`os`, `json`, `re`, `subprocess`, `unittest`), подмена `claude` в `tests/stub/claude`, `make test`.

**Spec:** `docs/superpowers/specs/2026-10-07-planka-docs-design.md`

## Global Constraints

- Единственный источник правил — `philosophy.md` и `rules/*.md`; промпты не дублируют их текст. Тексты в этом плане не правятся.
- Барьеры `PLANKA_OFF`/`PLANKA_JUDGE` и `common.run_hook` — как прежде; никакая ошибка хука не роняет сессию и не даёт отказ.
- Снимок: корень — `git rev-parse --show-toplevel` для `cwd` хука, иначе `cwd`; исключения — `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `target`, `build`, `dist`, `.dart_tool`, `.superpowers`, `.audit`, `.data`, `.idea`, `.vscode` плюс записи корневого `.gitignore` без подстановочных знаков и вида `*.ext`; больше 50 000 файлов — снимка нет и строка `planka: дерево больше 50000 файлов, сверка документации не проверяется`.
- Документация по пути: `*.md`, `CLAUDE.md`, `context/**`, `docs/**`, `README*`, `LICENSE*`; остальное — код.
- Извлечение комментариев: в git-репозитории — добавленные строки `git diff --no-color -U0 HEAD -- <файл>` для отслеживаемых и все строки неотслеживаемых; иначе все строки файла; затем фильтр по синтаксису расширения; потолок 300 строк и 16 КБ с пометкой об обрезке.
- Настройки читаются из `CLAUDE_PLUGIN_OPTION_COMMENT_LANG` и `CLAUDE_PLUGIN_OPTION_DOC_LANG`; незаданная подставляется как «не задан» и даёт одно предупреждение на сессию.
- Три фильтра `Stop` — один вызов судьи; лимит отказов общий, ключ `stop`.
- Тесты не зовут настоящий `claude`; тесты, которым нужен git, пропускаются с `unittest.skipUnless(shutil.which("git"))`.
- Коммиты только своих файлов, без трейлеров; комментарии в коде — факт, по-русски.

## Review Focus

1. Хук `Stop` без снимка (первая реплика до установки, другой `prompt_id`, снимок старше) — фильтр «документация» молчит, остальные работают. Тест в задаче 6.
2. Снимок не сделан из-за порога — `remind` предупреждает, `judge_stop` не падает. Тесты в задачах 2 и 5.
3. Проект без git и без `CLAUDE.md` — строка про инициализацию в контексте, снимок по `cwd`. Тест в задаче 5.
4. Изменились только документы — фильтр не срабатывает. Тест в задаче 6.
5. Файл с неизвестным расширением среди изменённых — извлечение даёт пустой список, не исключение. Тест в задаче 3.

## Структура файлов

```text
planka/common.py       — settings, substitute, project_root, is_doc_path, warn_once   (задача 1)
tests/helpers.py       — RULES с docs/comments; Env.project; hook_input с cwd        (задача 1)
tests/test_common.py                                                                   (задача 1)
planka/snapshot.py     — scan, save, load, diff, IGNORED_DIRS, MAX_FILES               (задача 2)
tests/test_snapshot.py                                                                 (задача 2)
planka/comments.py     — comment_lines, extract                                        (задача 3)
tests/test_comments.py                                                                 (задача 3)
planka/prompts.py      — _DOCS_CHECKS, render_docs_content, stop_prompt(docs=)         (задача 4)
tests/test_prompts.py                                                                  (задача 4)
planka/remind.py       — снимок, строка про CLAUDE.md, предупреждение о языке          (задача 5)
tests/test_remind.py                                                                   (задача 5)
planka/judge_stop.py   — фильтр «документация»                                         (задача 6)
tests/test_judge_stop.py                                                               (задача 6)
README.md                                                                              (задача 7)
```

## Волны

| Волна | Задачи | Зависимости |
| --- | --- | --- |
| 1 | тексты, манифест, спека — выполнена координатором (7c7be85) | — |
| 2 | 1 common, 2 snapshot, 3 comments, 4 prompts | контракты ниже; файлы не пересекаются |
| 3 | 5 remind, 6 judge_stop | волна 2 |
| 4 | 7 README; переименование `debt`→`deferred` в проекте-образце — координатор | всё |

Схождение после каждой волны: `make test`, `make validate` координатором. При проблеме исполнитель останавливается и предлагает варианты по «Решениям».

---

## Волна 2

### Task 1: `common.py` — настройки, подстановка, корень проекта, классификация

**Files:**
- Modify: `planka/common.py`
- Modify: `tests/helpers.py`
- Modify: `tests/test_common.py`

**Interfaces:**
- Consumes: существующие `plugin_root`, `rules_dir`, `philosophy_text`, `rule_texts`, `data_dir`, `warn`.
- Produces:

```python
COMMENT_LANG_PLACEHOLDER = "{COMMENT_LANG}"
DOC_LANG_PLACEHOLDER = "{DOC_LANG}"
UNSET_LANG = "не задан"
DOC_PATTERNS: tuple[str, ...]   # см. Global Constraints

def settings() -> dict           # {"comment_lang": str|None, "doc_lang": str|None} из CLAUDE_PLUGIN_OPTION_*
def substitute(text: str) -> str # {RULES}, {COMMENT_LANG}, {DOC_LANG}; None → UNSET_LANG
def philosophy_text() -> str | None   # как прежде, через substitute
def rule_texts(*names) -> str | None  # как прежде, каждый модуль через substitute
def project_root(cwd: str) -> pathlib.Path
    # git rev-parse --show-toplevel в cwd (timeout 5 с); при любой ошибке — Path(cwd)
def is_doc_path(relpath: str) -> bool
    # relpath с прямыми слэшами; True для *.md, CLAUDE.md, context/…, docs/…, README*, LICENSE*
def warn_once(session_id: str, key: str, msg: str) -> bool
    # warn(msg) один раз на (session, key); состояние в data_dir()/state/<session>.warned.json; True если предупредил
```

`tests/helpers.py`: `RULES` дополняется `"docs": "# Документация\n\n## Что сверяется с каждой правкой кода\n\n1. Правило документации.\n\n## Инициализация проекта без документации\n\n1. Правило инициализации.\n"` и `"comments": "# Комментарии\n\n- Язык: {COMMENT_LANG}.\n"`; `PHILOSOPHY` получает строку `Язык комментариев: {COMMENT_LANG}; язык документации: {DOC_LANG}.` после строки с `{RULES}`. `Env` получает `self.project = Path(tmp)/"project"` (создаётся пустым) и `environ()` по умолчанию не задаёт `CLAUDE_PLUGIN_OPTION_*`; `hook_input(event, **fields)` ставит `cwd` по умолчанию в `"/tmp"` как прежде — тесты задач 5–6 передают `cwd=str(env.project)` явно.

- [ ] **Step 1: Красные тесты** — в `tests/test_common.py`:

```python
class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def test_settings_unset(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": None, "doc_lang": None})

    def test_settings_set(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en+ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "en+ru", "doc_lang": "ru"})

    def test_substitute_all_placeholders(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"}, clear=True):
            out = common.substitute("a {RULES} b {COMMENT_LANG} c {DOC_LANG}")
            self.assertEqual(out, f"a {self.env.root / 'rules'} b en c en")

    def test_substitute_unset_is_marker(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.substitute("{COMMENT_LANG}/{DOC_LANG}"), "не задан/не задан")

    def test_philosophy_and_rules_substituted(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertIn("Язык комментариев: ru; язык документации: ru.", common.philosophy_text())
            self.assertIn("Язык: ru.", common.rule_texts("comments"))
            self.assertNotIn("{COMMENT_LANG}", common.rule_texts("comments"))


class ProjectRootTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_non_git_is_cwd(self):
        sub = self.env.project / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(common.project_root(str(sub)), sub)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_toplevel(self):
        subprocess.run(["git", "init", "-q", str(self.env.project)], check=True)
        sub = self.env.project / "pkg"
        sub.mkdir()
        self.assertEqual(common.project_root(str(sub)).resolve(), self.env.project.resolve())

    def test_missing_dir_is_path(self):
        self.assertEqual(common.project_root("/nonexistent/x"), pathlib.Path("/nonexistent/x"))


class DocPathTest(unittest.TestCase):
    def test_doc_paths(self):
        for p in ["README.md", "README", "README.rst", "LICENSE", "CLAUDE.md", "internal/x/CLAUDE.md",
                  "context/a.md", "context/deferred/INDEX.md", "docs/en/x.md", "notes.md"]:
            self.assertTrue(common.is_doc_path(p), p)

    def test_code_paths(self):
        for p in ["main.go", "a/b.py", "Makefile", "docs.py", "context.go", "readme_test.go", "x.toml"]:
            self.assertFalse(common.is_doc_path(p), p)


class WarnOnceTest(unittest.TestCase):
    def test_once_per_session_and_key(self):
        env = Env()
        try:
            with mock.patch.dict(os.environ, env.environ(), clear=True), \
                 mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertTrue(common.warn_once("s", "lang", "раз"))
                self.assertFalse(common.warn_once("s", "lang", "раз"))
                self.assertTrue(common.warn_once("s", "other", "два"))
                self.assertTrue(common.warn_once("s2", "lang", "три"))
                self.assertEqual(err.getvalue().count("planka:"), 3)
        finally:
            env.close()
```

Добавить `import shutil`, `import subprocess`, `import pathlib` в начало файла, если их нет.

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_common.SettingsTest tests.test_common.ProjectRootTest tests.test_common.DocPathTest tests.test_common.WarnOnceTest 2>&1 | tail -3` → `AttributeError`.

- [ ] **Step 3: `helpers.py`** — по описанию выше.

- [ ] **Step 4: `common.py`**

```python
COMMENT_LANG_PLACEHOLDER = "{COMMENT_LANG}"
DOC_LANG_PLACEHOLDER = "{DOC_LANG}"
UNSET_LANG = "не задан"
DOC_PATTERNS = ("README", "LICENSE")
DOC_DIRS = ("context/", "docs/")


def settings():
    return {"comment_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_COMMENT_LANG") or None,
            "doc_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_DOC_LANG") or None}


def substitute(text):
    """Метки каталога модулей и языков заменяются значениями; незаданный язык — UNSET_LANG."""
    s = settings()
    return (text.replace(RULES_PLACEHOLDER, str(rules_dir()))
                .replace(COMMENT_LANG_PLACEHOLDER, s["comment_lang"] or UNSET_LANG)
                .replace(DOC_LANG_PLACEHOLDER, s["doc_lang"] or UNSET_LANG))
```

`philosophy_text`: `return substitute(text)` вместо `text.replace(RULES_PLACEHOLDER, …)`. `rule_texts`: `out.append(substitute(p.read_text(...).strip()))`.

```python
def project_root(cwd):
    """Вершина git-репозитория для cwd; без git или вне репозитория — сам cwd."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return pathlib.Path(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return pathlib.Path(cwd)


def is_doc_path(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name.endswith(".md") or name.startswith(DOC_PATTERNS):
        return True
    return relpath.startswith(DOC_DIRS)


def warn_once(session_id, key, msg):
    """Предупреждение один раз на сессию и ключ; факт записан в state/<session>.warned.json."""
    state_dir = data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{_safe_name(session_id)}.warned.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        seen = []
    if key in seen:
        return False
    seen.append(key)
    _atomic_write_json(path, seen)
    warn(msg)
    return True
```

Вынести санитизацию имени из `deny_budget_exhausted` в `_safe_name(session_id)` и использовать в обоих местах.

- [ ] **Step 5: Зелёные** — Run: `python3 -m unittest tests.test_common -v 2>&1 | tail -3` → `OK`; `python3 -m unittest tests.test_remind tests.test_judge_stop tests.test_judge_tool 2>&1 | tail -2` → `OK`.

- [ ] **Step 6: Commit** — `git add planka/common.py tests/helpers.py tests/test_common.py && git commit -m "feat: настройки языка, подстановка меток, корень проекта и классификация путей"`

---

### Task 2: `snapshot.py` — снимок дерева

**Files:**
- Create: `planka/snapshot.py`
- Create: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: ничего из других задач (данные пишет по переданному каталогу).
- Produces:

```python
IGNORED_DIRS: frozenset[str]   # см. Global Constraints
MAX_FILES = 50_000

def ignore_rules(root: Path) -> tuple[set[str], set[str]]
    # (имена без подстановочных знаков, расширения из строк "*.ext") корневого .gitignore; без файла — пустые
def scan(root: Path) -> dict[str, list[int]] | None
    # {относительный путь с "/": [size, mtime_ns]}; None, если файлов больше MAX_FILES; симлинки не раскрываются
def save(state_dir: Path, session_id: str, prompt_id: str, root: Path, files: dict) -> None
    # state_dir/<safe session>.snap.json, атомарно
def load(state_dir: Path, session_id: str) -> dict | None
    # {"prompt_id", "root", "files"} или None
def diff(old: dict, new: dict) -> list[str]
    # отсортированные пути: добавленные, удалённые, изменившие size или mtime_ns
```

- [ ] **Step 1: Красные тесты** — `tests/test_snapshot.py`:

```python
import json
import os
import pathlib
import sys
import tempfile
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import snapshot  # noqa: E402


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_scan_relpaths_and_ignored_dirs(self):
        self.write("a.py"); self.write("pkg/b.go"); self.write(".git/HEAD"); self.write("node_modules/x.js")
        self.write("build/out"); self.write(".superpowers/x")
        files = snapshot.scan(self.root)
        self.assertEqual(sorted(files), ["a.py", "pkg/b.go"])
        self.assertEqual(len(files["a.py"]), 2)

    def test_gitignore_names_and_exts(self):
        self.write(".gitignore", "secrets/\n*.log\n# comment\nbin\n!keep\nfoo/*.tmp\n")
        self.write("secrets/k"); self.write("run.log"); self.write("bin/tool"); self.write("ok.txt")
        names, exts = snapshot.ignore_rules(self.root)
        self.assertEqual(names, {"secrets", "bin"})
        self.assertEqual(exts, {"log"})
        self.assertEqual(sorted(snapshot.scan(self.root)), [".gitignore", "ok.txt"])

    def test_no_gitignore(self):
        self.assertEqual(snapshot.ignore_rules(self.root), (set(), set()))

    def test_symlink_not_followed(self):
        self.write("real/f")
        os.symlink(self.root / "real", self.root / "link")
        self.assertEqual(sorted(snapshot.scan(self.root)), ["real/f"])

    def test_too_many_files_is_none(self):
        old = snapshot.MAX_FILES
        snapshot.MAX_FILES = 2
        try:
            self.write("a"); self.write("b"); self.write("c")
            self.assertIsNone(snapshot.scan(self.root))
        finally:
            snapshot.MAX_FILES = old


class SaveLoadDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        files = {"a.py": [1, 2], "b/c.go": [3, 4]}
        snapshot.save(self.state, "sess/1", "p-1", self.root, files)
        got = snapshot.load(self.state, "sess/1")
        self.assertEqual(got["prompt_id"], "p-1")
        self.assertEqual(got["root"], str(self.root))
        self.assertEqual(got["files"], files)
        self.assertEqual([p.name for p in self.state.iterdir()], ["sess_1.snap.json"])

    def test_load_missing_or_garbage(self):
        self.assertIsNone(snapshot.load(self.state, "none"))
        (self.state / "bad.snap.json").write_text("{", encoding="utf-8")
        self.assertIsNone(snapshot.load(self.state, "bad"))

    def test_diff(self):
        old = {"a": [1, 1], "b": [1, 1], "c": [1, 1]}
        new = {"a": [1, 1], "b": [2, 1], "d": [1, 1]}
        self.assertEqual(snapshot.diff(old, new), ["b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_snapshot 2>&1 | tail -2` → `ModuleNotFoundError`.

- [ ] **Step 3: `snapshot.py`**

```python
"""Снимок дерева проекта: какие файлы и с каким размером и mtime были на реплике."""
import json
import os
import pathlib
import tempfile

IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build",
                          "dist", ".dart_tool", ".superpowers", ".audit", ".data", ".idea", ".vscode"})
MAX_FILES = 50_000


def ignore_rules(root):
    """Записи корневого .gitignore: имена без подстановочных знаков и расширения из «*.ext»."""
    names, exts = set(), set()
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return names, exts
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if line.startswith("*.") and "/" not in line and not any(c in line[2:] for c in "*?["):
            exts.add(line[2:])
        elif not any(c in line for c in "*?[") and "/" not in line.strip("/"):
            names.add(line.strip("/"))
    return names, exts


def scan(root):
    """{путь: [size, mtime_ns]} для файлов под root; None, если их больше MAX_FILES."""
    names, exts = ignore_rules(root)
    skip = IGNORED_DIRS | names
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name in names or (exts and name.rsplit(".", 1)[-1] in exts and "." in name):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files[rel] = [st.st_size, st.st_mtime_ns]
            if len(files) > MAX_FILES:
                return None
    return files


def _safe(session_id):
    return "".join(c if (c.isalnum() and c.isascii()) or c in "._-" else "_" for c in session_id) or "unknown"


def save(state_dir, session_id, prompt_id, root, files):
    path = state_dir / f"{_safe(session_id)}.snap.json"
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"prompt_id": prompt_id, "root": str(root), "files": files}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def load(state_dir, session_id):
    path = state_dir / f"{_safe(session_id)}.snap.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def diff(old, new):
    changed = {p for p in old.keys() ^ new.keys()}
    changed |= {p for p in old.keys() & new.keys() if old[p] != new[p]}
    return sorted(changed)
```

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_snapshot -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/snapshot.py tests/test_snapshot.py && git commit -m "feat: снимок дерева проекта"`

---

### Task 3: `comments.py` — извлечение комментариев

**Files:**
- Create: `planka/comments.py`
- Create: `tests/test_comments.py`

**Interfaces:**
- Consumes: ничего.
- Produces:

```python
MAX_LINES = 300
MAX_BYTES = 16_384

def comment_lines(text: str, ext: str) -> list[str]
    # строки комментариев для расширения без точки ("go", "py", …); неизвестное расширение → []
def extract(root: Path, relpaths: list[str]) -> tuple[list[str], bool]
    # строки вида "path: <комментарий>", потолок MAX_LINES/MAX_BYTES, второй элемент — была ли обрезка;
    # в git-репозитории (git rev-parse внутри root успешен) — добавленные строки git diff -U0 HEAD для
    # отслеживаемых файлов и все строки неотслеживаемых; иначе все строки файла
```

Синтаксис по расширению: `//` и `/* */` — go, c, h, cc, cpp, hpp, java, kt, kts, swift, js, jsx, ts, tsx, dart, rs, scala, m, mm, cs; `#` — py, sh, bash, zsh, rb, pl, toml, yaml, yml, mk, Makefile (имя файла), cfg, ini, ps1; `--` — sql, lua, hs; docstring `"""`/`'''` — py; `<!-- -->` — html, xml, vue, svelte, md.

- [ ] **Step 1: Красные тесты** — `tests/test_comments.py`:

```python
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402


class CommentLinesTest(unittest.TestCase):
    def test_c_family(self):
        src = "x := 1 // trailing\n// line one\n/* block\n   two */\ns := \"// not\"\n"
        self.assertEqual(comments.comment_lines(src, "go"),
                         ["// trailing", "// line one", "/* block", "two */"])

    def test_hash_and_docstring(self):
        src = "#!/usr/bin/env python3\n# note\ndef f():\n    \"\"\"Doc line.\"\"\"\n    s = '#'\n    return 1  # tail\n"
        self.assertEqual(comments.comment_lines(src, "py"),
                         ["# note", '"""Doc line."""', "# tail"])

    def test_sql_and_html(self):
        self.assertEqual(comments.comment_lines("select 1 -- c\n", "sql"), ["-- c"])
        self.assertEqual(comments.comment_lines("<a>\n<!-- hidden -->\n", "html"), ["<!-- hidden -->"])

    def test_unknown_ext_is_empty(self):
        self.assertEqual(comments.comment_lines("// x\n# y\n", "bin"), [])
        self.assertEqual(comments.comment_lines("", "go"), [])


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_non_git_whole_files(self):
        self.write("a.go", "// one\nx := 1\n")
        self.write("b.py", "# two\n")
        self.write("c.bin", "// ignored\n")
        lines, truncated = comments.extract(self.root, ["a.go", "b.py", "c.bin", "missing.go"])
        self.assertEqual(lines, ["a.go: // one", "b.py: # two"])
        self.assertFalse(truncated)

    def test_truncation(self):
        self.write("a.py", "".join(f"# line {i}\n" for i in range(500)))
        lines, truncated = comments.extract(self.root, ["a.py"])
        self.assertEqual(len(lines), comments.MAX_LINES)
        self.assertTrue(truncated)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_added_lines_only(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("a.go", "// old\nx := 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "a.go"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "init"], check=True)
        self.write("a.go", "// old\nx := 1\n// new\n")
        self.write("u.py", "# untracked\n")
        lines, _ = comments.extract(self.root, ["a.go", "u.py"])
        self.assertEqual(lines, ["a.go: // new", "u.py: # untracked"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_comments 2>&1 | tail -2` → `ModuleNotFoundError`.

- [ ] **Step 3: `comments.py`**

```python
"""Извлечение строк комментариев из изменённых файлов для судьи."""
import os
import re
import subprocess

MAX_LINES = 300
MAX_BYTES = 16_384

_C_FAMILY = {"go", "c", "h", "cc", "cpp", "hpp", "java", "kt", "kts", "swift", "js", "jsx", "ts", "tsx",
             "dart", "rs", "scala", "m", "mm", "cs"}
_HASH = {"py", "sh", "bash", "zsh", "rb", "pl", "toml", "yaml", "yml", "mk", "makefile", "cfg", "ini", "ps1"}
_DASH = {"sql", "lua", "hs"}
_HTML = {"html", "xml", "vue", "svelte", "md"}
_DOCSTRING = {"py"}


def _strip_strings(line):
    """Содержимое строковых литералов заменяется пробелами, чтобы маркер внутри строки не считался комментарием."""
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', lambda m: " " * len(m.group(0)), line)


def comment_lines(text, ext):
    ext = ext.lower()
    out = []
    in_block = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if in_block:
            out.append(line)
            if in_block in line:
                in_block = None
            continue
        probe = _strip_strings(line)
        if ext in _C_FAMILY:
            i = probe.find("//")
            j = probe.find("/*")
            if 0 <= i and (j < 0 or i < j):
                out.append(line[i:])
            elif j >= 0:
                out.append(line[j:])
                if "*/" not in probe[j:]:
                    in_block = "*/"
        elif ext in _HASH:
            i = probe.find("#")
            if i >= 0 and not line.startswith("#!"):
                out.append(line[i:])
            if ext in _DOCSTRING:
                for q in ('"""', "'''"):
                    k = line.find(q)
                    if k >= 0:
                        out.append(line[k:])
                        if line.count(q) == 1:
                            in_block = q
                        break
        elif ext in _DASH:
            i = probe.find("--")
            if i >= 0:
                out.append(line[i:])
        elif ext in _HTML:
            i = probe.find("<!--")
            if i >= 0:
                out.append(line[i:])
                if "-->" not in probe[i:]:
                    in_block = "-->"
    return out


def _ext(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name == "Makefile":
        return "makefile"
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _git(root, *args):
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _source(root, relpath, in_git):
    """Строки файла, подлежащие проверке: добавленные в git diff для отслеживаемого, иначе все."""
    full = os.path.join(root, relpath)
    if in_git and _git(root, "ls-files", "--error-unmatch", "--", relpath) is not None:
        out = _git(root, "diff", "--no-color", "-U0", "HEAD", "--", relpath)
        if out is None:
            return ""
        return "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def extract(root, relpaths):
    in_git = _git(root, "rev-parse", "--show-toplevel") is not None
    lines, size, truncated = [], 0, False
    for rel in relpaths:
        for c in comment_lines(_source(root, rel, in_git), _ext(rel)):
            entry = f"{rel}: {c}"
            if len(lines) >= MAX_LINES or size + len(entry.encode("utf-8")) > MAX_BYTES:
                return lines, True
            lines.append(entry)
            size += len(entry.encode("utf-8"))
    return lines, truncated
```

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_comments -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/comments.py tests/test_comments.py && git commit -m "feat: извлечение комментариев из изменённых файлов"`

---

### Task 4: `prompts.py` — вопросы «документация» и третий флаг

**Files:**
- Modify: `planka/prompts.py`
- Modify: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `_wrap`, `_MESSAGE_CHECKS`, `_DONE_CHECKS`.
- Produces:

```python
def stop_prompt(rubric, content, *, options, done, docs=False) -> str
    # вопросы в порядке options, done, docs; ValueError, если все False
def render_docs_content(message: str, changed: list[tuple[str, bool]], comments: list[str],
                        truncated: bool, no_claude_md: bool) -> str
    # changed — [(путь, is_doc)]; текст: сообщение, затем «Изменённые файлы за ход:» с пометками
    # «код»/«документация», затем «Комментарии в изменённых файлах:» со строками (и строкой
    # «… обрезано» при truncated), затем «В корне проекта нет CLAUDE.md.» при no_claude_md
```

`_DOCS_CHECKS`:

```text
Проверь сверку документации и комментариев по рубрике и ответь:
1. Для каждого каталога с изменённым кодом — назван ли его локальный CLAUDE.md и сверен ли он?
2. Названы ли документы context/ по изменённому механизму; обновлены они или сказано, почему ничего не устарело?
3. Остаток правки записан в context/deferred/ или сказано, что остатка нет?
4. Комментарии в изменённых файлах — на заданном языке или по правилу проектного CLAUDE.md; без истории; без пересказа очевидного; факт, а не обоснование?
5. Если в корне нет CLAUDE.md — предложена ли автору инициализация?
Отказ — указание: какой файл сверить или какую строку комментария переписать.
```

- [ ] **Step 1: Красные тесты** — в `tests/test_prompts.py`:

```python
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
        self.assertIn("Комментарии в изменённых файлах: нет", text)
```

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_prompts.DocsPromptTest 2>&1 | tail -2` → `TypeError`/`AttributeError`.

- [ ] **Step 3: `prompts.py`** — добавить `_DOCS_CHECKS` (текст выше), изменить `stop_prompt`:

```python
def stop_prompt(rubric, content, *, options, done, docs=False):
    """Промпт судьи на Stop: вопросы по совпавшим фильтрам в порядке options, done, docs."""
    checks = [c for flag, c in ((options, _MESSAGE_CHECKS), (done, _DONE_CHECKS), (docs, _DOCS_CHECKS)) if flag]
    if not checks:
        raise ValueError("ни один фильтр Stop не совпал")
    return _wrap(rubric, "\n\n".join(checks), content)


def render_docs_content(message, changed, comments, truncated, no_claude_md):
    parts = [message, "", "Изменённые файлы за ход:"]
    parts += [f"- {path} — {'документация' if is_doc else 'код'}" for path, is_doc in changed]
    if comments:
        parts += ["", "Комментарии в изменённых файлах:"] + [f"- {c}" for c in comments]
        if truncated:
            parts.append("- … обрезано")
    else:
        parts += ["", "Комментарии в изменённых файлах: нет"]
    if no_claude_md:
        parts += ["", "В корне проекта нет CLAUDE.md."]
    return "\n".join(parts)
```

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_prompts -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/prompts.py tests/test_prompts.py && git commit -m "feat: вопросы судьи о документации и содержимое с изменёнными файлами"`

---

**Схождение волны 2:** `make test`, `make validate`.

---

## Волна 3

### Task 5: `remind.py` — снимок, строка про `CLAUDE.md`, предупреждение о языке

**Files:**
- Modify: `planka/remind.py`
- Modify: `tests/test_remind.py`

**Interfaces:**
- Consumes: `common.project_root`, `common.settings`, `common.warn_once`, `common.data_dir`, `snapshot.scan/save`.
- Produces: `NO_DOCS_LINE = "Проект без документации: предложи автору инициализацию по rules/docs.md."`

Поведение `main` после чтения ядра: `root = common.project_root(data["cwd"])`; если `(root / "CLAUDE.md")` нет — к тексту контекста добавляется `"\n\n" + NO_DOCS_LINE`; `files = snapshot.scan(root)`; `None` → `common.warn_once(session, "snapshot", "дерево больше 50000 файлов, сверка документации не проверяется")`, иначе `snapshot.save(data_dir()/"state", session, prompt_id, root, files)` (каталог `state` создаётся); если `settings()` не полон — `warn_once(session, "lang", "задайте comment_lang и doc_lang: claude plugin configure planka --values-stdin")`. Отсутствующий `cwd` → без снимка и без строки. Ошибки снимка — как любые: `run_hook`.

- [ ] **Step 1: Красные тесты** — в `tests/test_remind.py` (используя `self.env.project`):

```python
    def prompt(self, **extra):
        return self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="x", cwd=str(self.env.project)), **extra)

    def test_no_claude_md_line(self):
        r = self.prompt()
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.endswith("Проект без документации: предложи автору инициализацию по rules/docs.md."))

    def test_claude_md_present_no_line(self):
        (self.env.project / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        ctx = json.loads(self.prompt().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Проект без документации", ctx)

    def test_snapshot_written(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        self.prompt()
        snap = json.loads((self.env.data / "state" / "sess-1.snap.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["prompt_id"], "p-1")
        self.assertEqual(sorted(snap["files"]), ["a.py"])

    def test_language_warning_once(self):
        r1 = self.prompt()
        r2 = self.prompt()
        self.assertIn("comment_lang", r1.stderr)
        self.assertNotIn("comment_lang", r2.stderr)
        r3 = self.prompt(CLAUDE_PLUGIN_OPTION_COMMENT_LANG="en", CLAUDE_PLUGIN_OPTION_DOC_LANG="en")
        ctx = json.loads(r3.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Язык комментариев: en; язык документации: en.", ctx)

    def test_too_many_files_warns(self):
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        r = self.prompt(PLANKA_TEST_MAX_FILES="2")
        self.assertIn("50000", r.stderr)
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())
```

Для последнего теста `remind.py` читает `PLANKA_TEST_MAX_FILES` и, если задана, выставляет `snapshot.MAX_FILES = int(...)` до `scan`; это тестовый крючок, документируется комментарием в коде.

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_remind 2>&1 | tail -3` → падения новых тестов.

- [ ] **Step 3: `remind.py`**

```python
"""UserPromptSubmit: подмешивает ядро, снимает снимок дерева, напоминает об инициализации документации."""
import os

import common
import snapshot

NO_DOCS_LINE = "Проект без документации: предложи автору инициализацию по rules/docs.md."


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if data is None:
        return
    text = common.philosophy_text()
    if text is None:
        return
    session = data.get("session_id", "")
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        root = common.project_root(cwd)
        if not (root / "CLAUDE.md").exists():
            text += "\n\n" + NO_DOCS_LINE
        test_limit = os.environ.get("PLANKA_TEST_MAX_FILES")
        if test_limit:
            # Тестовый крючок: порог снимка уменьшается, чтобы проверить ветку отказа.
            snapshot.MAX_FILES = int(test_limit)
        files = snapshot.scan(root)
        if files is None:
            common.warn_once(session, "snapshot",
                             f"дерево больше {snapshot.MAX_FILES} файлов, сверка документации не проверяется")
        else:
            state_dir = common.data_dir() / "state"
            state_dir.mkdir(exist_ok=True)
            snapshot.save(state_dir, session, data.get("prompt_id", ""), root, files)
    s = common.settings()
    if not (s["comment_lang"] and s["doc_lang"]):
        common.warn_once(session, "lang",
                         "задайте comment_lang и doc_lang: claude plugin configure planka --values-stdin")
    common.emit(common.context_output(text))


if __name__ == "__main__":
    common.run_hook(main)
```

Тест `test_too_many_files_warns` ждёт «50000» в stderr: с крючком порог 2, поэтому сообщение обязано называть `50000` константой, а не `snapshot.MAX_FILES`; записать `f"дерево больше {snapshot.DEFAULT_MAX_FILES} файлов…"` — добавить в `snapshot.py` `DEFAULT_MAX_FILES = 50_000` и `MAX_FILES = DEFAULT_MAX_FILES` (задача 2 владеет файлом; задача 5 ждёт её — волна 3 стоит на волне 2). Если константы нет, исполнитель задачи 5 останавливается и сообщает.

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_remind -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/remind.py tests/test_remind.py && git commit -m "feat: напоминание снимает снимок дерева и предлагает инициализацию документации"`

---

### Task 6: `judge_stop.py` — фильтр «документация»

**Files:**
- Modify: `planka/judge_stop.py`
- Modify: `tests/test_judge_stop.py`

**Interfaces:**
- Consumes: `common.project_root`, `common.is_doc_path`, `common.rubric`, `snapshot.load/scan/diff`, `comments.extract`, `prompts.stop_prompt(docs=)`, `prompts.render_docs_content`.
- Produces: `changed_this_turn(data) -> tuple[Path, list[tuple[str, bool]]] | None` — корень и изменённые пути с пометкой документации; `None`, если снимка для текущего `prompt_id` нет или `cwd` отсутствует.

Поведение `main`: `options`, `done` как прежде; `docs_info = changed_this_turn(data)`; `docs = bool(docs_info and any(not is_doc for _, is_doc in docs_info[1]))`; если ни один флаг — return. Рубрика: `sections = ("Решения",) if options else ()`, `modules = [...]`: `"verification"` при done, `"docs"` и `"comments"` при docs; `common.rubric(sections, tuple(modules))`. Содержимое: при docs — `prompts.render_docs_content(message, changed, *comments.extract(root, [p for p, d in changed if not d]), not (root/"CLAUDE.md").exists())`, иначе `message`. Промпт `prompts.stop_prompt(rubric, content, options=options, done=done, docs=docs)`. `filters` получает `"docs"`. Журнал — как прежде, `content` — то, что ушло судье.

- [ ] **Step 1: Красные тесты** — в `tests/test_judge_stop.py`:

```python
class DocsFilterTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.project = self.env.project
        (self.project / "pkg").mkdir()

    def tearDown(self):
        self.env.close()

    def snap(self, prompt_id="p-1"):
        import snapshot  # planka/ в sys.path через helpers
        state = self.env.data / "state"
        state.mkdir(exist_ok=True)
        snapshot.save(state, "sess-1", prompt_id, self.project, snapshot.scan(self.project))

    def stop(self, msg, **extra):
        return self.env.run("judge_stop.py", hook_input(
            "Stop", last_assistant_message=msg, stop_hook_active=False, cwd=str(self.project)), **extra)

    def test_code_change_triggers_docs_judge(self):
        self.snap()
        (self.project / "pkg" / "a.go").write_text("// hello\nx := 1\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("# Документация", text)
        self.assertIn("# Комментарии", text)
        self.assertIn("pkg/a.go — код", text)
        self.assertIn("pkg/a.go: // hello", text)
        self.assertIn("В корне проекта нет CLAUDE.md.", text)
        self.assertIn("локальный CLAUDE.md", text)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["docs"])

    def test_docs_only_change_no_trigger(self):
        self.snap()
        (self.project / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        (self.project / "notes.md").write_text("n\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_no_snapshot_no_trigger(self):
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_stale_snapshot_no_trigger(self):
        self.snap(prompt_id="p-0")
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")

    def test_denied_blocks_with_reason(self):
        self.snap()
        (self.project / "a.py").write_text("# было так, стало эдак\n", encoding="utf-8")
        r = self.stop("Поправил.", PLANKA_STUB="deny", PLANKA_STUB_REASON="сверь pkg/CLAUDE.md")
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("planka: "))
        self.assertIn("сверь pkg/CLAUDE.md", out["reason"])

    def test_three_filters_one_call(self):
        self.snap()
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.stop(OPTIONS_MSG + "\n\nГотово.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n<content>\n"), 1)
        for needle in ["## Решения", "# Доказательство", "# Документация", "# Комментарии"]:
            self.assertIn(needle, text)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["options", "done", "docs"])

    def test_missing_docs_module_skips(self):
        self.snap()
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        (self.env.root / "rules" / "comments.md").unlink()
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
```

Чтобы `import snapshot` в тесте работал, `tests/helpers.py` уже вставляет `PLANKA_DIR` в `sys.path`? Нет — тесты делают это сами в начале файла (`sys.path.insert(0, str(PLANKA_DIR))` уже есть в `test_judge_stop.py`). Хорошо.

- [ ] **Step 2: Красные** — Run: `python3 -m unittest tests.test_judge_stop.DocsFilterTest 2>&1 | tail -3` → падения.

- [ ] **Step 3: `judge_stop.py`** — добавить `import comments`, `import snapshot`:

```python
def changed_this_turn(data):
    """Корень проекта и пути, изменившиеся со снимка текущей реплики; None, если снимка нет."""
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    snap = snapshot.load(common.data_dir() / "state", data.get("session_id", ""))
    if snap is None or snap.get("prompt_id") != data.get("prompt_id"):
        return None
    root = common.project_root(cwd)
    if str(root) != snap.get("root"):
        return None
    now = snapshot.scan(root)
    if now is None:
        return None
    return root, [(p, common.is_doc_path(p)) for p in snapshot.diff(snap["files"], now)]
```

и в `main`:

```python
    options = looks_like_options(message)
    done = claims_done(message)
    docs_info = changed_this_turn(data)
    docs = bool(docs_info and any(not is_doc for _, is_doc in docs_info[1]))
    if not (options or done or docs):
        return
    filters = [name for flag, name in ((options, "options"), (done, "done"), (docs, "docs")) if flag]
    modules = []
    if done:
        modules.append("verification")
    if docs:
        modules += ["docs", "comments"]
    rubric = common.rubric(("Решения",) if options else (), tuple(modules))
    ...
    content = message
    if docs:
        root, changed = docs_info
        lines, truncated = comments.extract(root, [p for p, is_doc in changed if not is_doc])
        content = prompts.render_docs_content(message, changed, lines, truncated,
                                              not (root / "CLAUDE.md").exists())
    verdict = common.run_judge(prompts.SYSTEM_PROMPT,
                               prompts.stop_prompt(rubric, content, options=options, done=done, docs=docs))
```

`log_event(..., content=content)` везде вместо `message`. Докстринг модуля дополнить третьим фильтром.

- [ ] **Step 4: Зелёные** — Run: `python3 -m unittest tests.test_judge_stop -v 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit** — `git add planka/judge_stop.py tests/test_judge_stop.py && git commit -m "feat: сверка документации и комментариев после правки кода судится по снимку дерева"`

---

**Схождение волны 3:** `make test`, `make validate`.

---

## Волна 4

### Task 7: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** Разделы: «Установка» — после `make install` шаг настройки языка: `claude plugin configure planka@skills-dir --values-stdin <<< '{"comment_lang":"en+ru","doc_lang":"ru"}'` с пояснением вариантов; «Как это работает» — два новых модуля, снимок дерева на каждой реплике и фильтр «документация» на `Stop` (что видит судья: список файлов, комментарии, отсутствие `CLAUDE.md`), строка про инициализацию; «Известные ограничения» — порог 50 000 файлов, синтаксисы комментариев, самоотчёт для остальных, судья видит только последнее сообщение, `.gitignore` читается упрощённо. Каждое утверждение сверить с кодом.
- [ ] **Step 2:** Commit — `git add README.md && git commit -m "docs: модули документации и комментариев, настройки языка, снимок дерева в README"`

### Task 8 (координатор, репозиторий проекта-образца): `context/debt` → `context/deferred`

- `git mv context/debt context/deferred`; замена `context/debt` → `context/deferred` и `debt/` → `deferred/` в 21 md-файле и `Makefile`; проверка, что ни одна ссылка не бита (`grep -rn 'context/debt' . --include=*.md --include=Makefile` пуст; все относительные ссылки `deferred/*.md` разрешаются); коммит в проекте-образце по его регламенту.

**Схождение волны 4:** `make test`, `make validate`, живая проверка автором по спеке §9.
