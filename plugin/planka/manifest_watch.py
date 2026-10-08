"""Новые имена зависимостей в манифестах: правка файловым инструментом и команда Bash.

Правка — текст манифеста до и после по входу Write, Edit, MultiEdit. Команда — снимок имён всех
манифестов проекта перед ней (state/<session>.manifests.json по tool_use_id) и сравнение после.
Новым не считается имя из текста до правки вместе с транзитивными, из версии манифеста в HEAD, в
источнике незавершённых merge, cherry-pick, rebase, revert и в сторонах конфликта индекса, из другого манифеста
того же реестра в проекте и в HEAD, из файла _LEGACY в HEAD, имя пакета самого проекта (workspace, монорепозиторий) и,
после команды, имя из манифестов ref, откуда команда git возвращает файлы (stash, ветка, коммит), если ref
создан до начала сессии.
"""
import ast
import configparser
import os
import re
import stat
import subprocess
import time
import tomllib
import warnings

import common
import depcheck
import manifests
import snapshot

# Срок снимка манифестов перед командой Bash и сравнения после неё, секунды от старта хука; без
# common.GIT_ROOT_TIMEOUT корня проекта (tests/test_contract.py, TimeoutsTest).
SNAPSHOT_BUDGET = 5
CHECK_BUDGET = 5
# Срок `git cat-file` версий из _REPO_REFS и _INDEX_STAGES при правке файловым инструментом, секунды.
HEAD_TIMEOUT = 5
# Пределы: больший манифест не разбирается, больше манифестов или файлов обхода вне git — снимка нет.
MAX_MANIFEST_BYTES = 1_048_576
MAX_MANIFESTS = 1000
MAX_WALK_FILES = 20_000
# Запись снимка без PostToolUse (команду отклонил автор или другой хук) удаляется через столько секунд.
ENTRY_TTL = 3600
# Каталоги, чьи манифесты не зависимости проекта: тестовые данные, установленные пакеты (в том числе
# site-packages окружения Python с любым именем, vendor Composer, Bundler и Go), сборка и служебные
# каталоги snapshot.IGNORED_DIRS.
FOREIGN_DIRS = snapshot.IGNORED_DIRS | frozenset({"fixtures", "__fixtures__", "testdata", "node_modules",
                                                  "site-packages", "dist-packages", "vendor", "bower_components"})


class Unavailable(Exception):
    """Снимок или сравнение невозможны; текст — причина для предупреждения."""


def has_marker(command):
    """Маркер согласия depcheck.DEP_OK_MARKER стоит среди ведущих присваиваний команды хоть одного сегмента,
    как у depcheck.dependency_add; в комментарии и аргументом команды — не маркер."""
    return any(depcheck._command(segment)[1] for segment in depcheck._segments(command))


def watched_kind(path):
    """Вид манифеста manifests.kind по пути; None — не манифест или в каталоге FOREIGN_DIRS."""
    parts = re.split(r"[\\/]", path)
    if FOREIGN_DIRS.intersection(parts[:-1]):
        return None
    return manifests.kind(path)


# Файлы зависимостей PyPI, которые не проверяются как манифест: их имена только известные — перенос в
# pyproject.toml или requirements не новое имя — и берутся только из деревьев git (HEAD, ref до начала сессии):
# правку рабочего дерева не проверяет никто. setup.py — код: читаются строковые литералы ключей _SETUP_KEYS.
_LEGACY = frozenset({"setup.py", "setup.cfg", "Pipfile"})
_SETUP_KEYS = ("install_requires", "setup_requires", "tests_require", "extras_require")
# Таблицы Pipfile, ключи которых не пакеты; остальные таблицы — категории пакетов (packages, dev-packages, свои).
_PIPFILE_NOT_PACKAGES = frozenset({"source", "requires", "scripts", "pipenv"})


def source_kind(path):
    """watched_kind или имя файла _LEGACY (вне каталогов FOREIGN_DIRS) — источник известных имён в дереве git;
    None — ни то, ни другое."""
    kind = watched_kind(path)
    if kind is not None:
        return kind
    parts = re.split(r"[\\/]", path)
    return parts[-1] if parts[-1] in _LEGACY and not FOREIGN_DIRS.intersection(parts[:-1]) else None


def _registry(kind):
    return "pypi" if kind in _LEGACY else manifests.registry(kind)


def _setup_cfg(text):
    """Требования `[options]` install_requires, setup_requires, tests_require и `[options.extras_require]`
    setup.cfg строками; None — не разобран. Значение `file:` — ссылка на файл, не требование."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError):
        return None
    values = [parser.get("options", key, fallback="") for key in _SETUP_KEYS[:3]]
    if parser.has_section("options.extras_require"):
        values += [value for _, value in parser.items("options.extras_require")]
    return [line for value in values if not value.strip().startswith("file:") for line in value.splitlines()]


def _literals(roots, assigned):
    """Строки литералов roots: строка, элементы списка, кортежа, множества, значения словаря, обе стороны `+`;
    имя — значение его присваивания на уровне модуля (assigned). Каждый узел и каждое имя обходятся один раз:
    время линейно по размеру дерева, порядок строк не сохраняется."""
    out, stack, seen = [], list(roots), set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                out.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            stack += node.elts
        elif isinstance(node, ast.Dict):
            stack += node.values
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            stack += [node.left, node.right]
        elif isinstance(node, ast.Name) and node.id in assigned:
            stack.append(assigned[node.id])
    return out


def _setup_py(text):
    """Требования setup.py строками: литералы аргументов и ключей словаря _SETUP_KEYS; None — не разобран.
    Требования, собранные кодом (чтение файла, цикл), не видны."""
    try:
        # SyntaxWarning разбора (`"\d"` в строке) печатается в stderr, а хук stderr не пишет.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    assigned = {node.targets[0].id: node.value for node in tree.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in _SETUP_KEYS:
            values.append(node.value)
        elif isinstance(node, ast.Dict):
            values += [v for k, v in zip(node.keys, node.values)
                       if isinstance(k, ast.Constant) and k.value in _SETUP_KEYS]
    return _literals(values, assigned)


def _pipfile(text):
    """Имена пакетов Pipfile — ключи таблиц, кроме _PIPFILE_NOT_PACKAGES; None — не разобран."""
    try:
        data = tomllib.loads(text)
    except (ValueError, RecursionError):
        return None
    return [name for table, value in data.items() if table not in _PIPFILE_NOT_PACKAGES and isinstance(value, dict)
            for name in value]


_LEGACY_PARSERS = {"setup.py": _setup_py, "setup.cfg": _setup_cfg, "Pipfile": _pipfile}


def _known(kind, text):
    """Имена с транзитивными (manifests.known_names) манифеста или файла _LEGACY вида kind; None — не разобран."""
    if kind not in _LEGACY:
        return manifests.known_names(kind, text)
    lines = _LEGACY_PARSERS[kind](text)
    # Строки требований разбирает разбор файла требований: имя PEP 508, нормализация PEP 503.
    return None if lines is None else manifests.known_names("requirements", "\n".join(lines))


def _read(path):
    """Текст файла в UTF-8 с заменой, переводы строк как есть; None — нет файла; Unavailable — больше
    MAX_MANIFEST_BYTES или не прочитан."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unavailable(f"не прочитан: {e!r}") from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise Unavailable(f"больше {MAX_MANIFEST_BYTES} байт")
    return data.decode("utf-8", "replace")


def _replace(text, old, new, replace_all):
    """Текст после замены, как у Edit; None — инструмент откажет: old не найден или неоднозначен."""
    count = text.count(old) if old else 0
    if count == 0 or count > 1 and not replace_all:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def edit_texts(tool, tool_input, path):
    """(текст до или None — файла нет, текст после) правки файловым инструментом; None — правку не
    вычислить или инструмент сам откажет. Unavailable — файл не прочитан.

    Edit и MultiEdit, как Claude Code, сопоставляют old_string с текстом файла, где CRLF приведены к LF;
    текст после — с переводами строк LF."""
    current = _read(path)
    if tool == "Write":
        content = tool_input.get("content")
        return (current, content) if isinstance(content, str) else None
    if tool == "Edit":
        edits = [tool_input]
    elif tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
    else:
        return None
    text = None if current is None else current.replace("\r\n", "\n")
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        # Пустой old_string создаёт файл, которого нет, или заполняет пустой.
        if old == "" and not text:
            text = new
            continue
        if text is None:
            return None
        text = _replace(text, old, new, edit.get("replace_all") is True)
        if text is None:
            return None
    return current, text


def _git(cwd, timeout, *args, root, input=None):
    """stdout `git -C cwd <args>` с stdin input байтами; None при коде не 0 или сбое; TimeoutError по сроку.
    root — корень проекта (common.project_root), cwd — он или каталог под ним: git ищет репозиторий не выше
    root (common.git_env); None — проект неизвестен или cwd вне него, окружение наследуется."""
    env = None if root is None else common.git_env(root)
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True, timeout=timeout,
                              env=env)
    except subprocess.TimeoutExpired:
        raise TimeoutError("git не уложился в срок") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


# Версии, чьи имена уже в репозитории: HEAD и источник незавершённых merge (и pull), cherry-pick, rebase и
# revert — при конфликте они не закоммичены, а файл уже с их правкой; revert возвращает версию до
# отменяемого коммита (REVERT_HEAD^).
_REPO_REFS = ("HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD", "REVERT_HEAD^")
# Стороны конфликта в индексе: база, наша, их. Их пишет git при конфликте любой операции, в том числе
# `git stash pop|apply`, `git merge --squash`, `git cherry-pick -n`, которые ref в _REPO_REFS не оставляют;
# держатся до `git add` или `git rm` файла.
_INDEX_STAGES = (":1:", ":2:", ":3:")


def _objects(out):
    """[(тип, содержимое) или None] вывода `git cat-file --batch` по строке ввода: None — объект не найден
    (заголовок `<ввод> missing` или `ambiguous`)."""
    objects, i = [], 0
    while i < len(out):
        end = out.find(b"\n", i)
        if end < 0:
            break
        header = out[i:end].split()
        i = end + 1
        if len(header) == 3 and header[2].isdigit():
            size = int(header[2])
            objects.append((header[1], out[i:i + size]))
            i += size + 1
        else:
            objects.append(None)
    return objects


def _blobs(out):
    """Содержимое блобов вывода `git cat-file --batch`; отсутствующие и не блобы пропущены."""
    return [data for kind, data in filter(None, _objects(out)) if kind == b"blob"]


def _inside(path, root):
    """Каталог path — root или под ним, символические ссылки разрешены."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Разные диски Windows.
        return False


def head_names(path, kind, timeout, root=None):
    """Имена манифеста path вместе с транзитивными (manifests.known_names) в версиях _REPO_REFS и сторонах
    конфликта _INDEX_STAGES его репозитория, объединённые; None — нет репозитория, файла ни в одной версии или
    ни одна не разобрана. `cat-file --batch` отдаёт байты блобов без textconv и фильтров одним вызовом git.
    root — корень проекта (common.project_root) или None; файл под root ищет репозиторий не выше root (_git),
    файл вне root и при None — свой репозиторий без ограничения."""
    name = os.path.basename(path)
    cwd = os.path.dirname(path) or "."
    if root is not None and not _inside(cwd, root):
        root = None
    if "\n" in name or "\r" in name:
        # Ввод --batch построчный: имя с переводом строки или возвратом каретки — только версия HEAD отдельным
        # вызовом.
        out = _git(cwd, timeout, "cat-file", "blob", f"HEAD:./{name}", root=root)
        blobs = [] if out is None else [out]
    else:
        out = _git(cwd, timeout, "cat-file", "--batch", root=root,
                   input=os.fsencode("".join(f"{ref}:./{name}\n" for ref in _REPO_REFS)
                                     + "".join(f"{stage}./{name}\n" for stage in _INDEX_STAGES)))
        blobs = [] if out is None else _blobs(out)
    found = [manifests.known_names(kind, b.decode("utf-8", "replace")) for b in blobs if len(b) <= MAX_MANIFEST_BYTES]
    found = [names for names in found if names is not None]
    return frozenset().union(*found) if found else None


def fresh_names(old, new, head, project=frozenset):
    """Отсортированные новые имена: из new (объявленные, manifests.names) нет в old, в версии HEAD и в
    проекте. old и head() — имена с транзитивными (manifests.known_names), old None — не разобран, head()
    None — версии нет; project() — имена других манифестов того же реестра и пакетов самого проекта, зовётся,
    только если после old и HEAD что-то осталось. Unavailable — сравнить не с чем: old не разобран и версии
    в HEAD нет."""
    added = None if old is None else new - old
    if added is None or added:
        base = head()
        if added is None:
            if base is None:
                raise Unavailable("до правки не разобран, версии в HEAD нет")
            added = new - base
        elif base is not None:
            added -= base
    if added:
        added -= project()
    return sorted(added)


def edit_names(kind, before, after, head, project=frozenset):
    """Новые имена манифеста вида kind с текстом before (None — файла не было) после правки в after;
    head и project — как у fresh_names. Unavailable — не разобран."""
    new = manifests.names(kind, after)
    if new is None:
        raise Unavailable("после правки не разобран")
    old = frozenset() if before is None else manifests.known_names(kind, before)
    return fresh_names(old, new, head, project)


def project_names(root, kind, deadline):
    """Имена манифестов реестра вида kind (manifests.registry) под root и, в git, манифестов и файлов _LEGACY
    версии HEAD (_tree_names) с транзитивными и имена пакетов самого проекта этого реестра; файлы _LEGACY
    рабочего дерева имён не дают. Не разобранный и больший MAX_MANIFEST_BYTES манифест не даёт ничего.
    Unavailable и TimeoutError — как у list_manifests и _tree_names."""
    mode, found = list_manifests(root, deadline)
    registry = manifests.registry(kind)
    out = set()
    for rel in found:
        _remaining(deadline)
        other = watched_kind(rel)
        if manifests.registry(other) != registry:
            continue
        known, own = _parse(os.path.join(root, rel), other)
        out.update(known or ())
        out.add(own)
    out.discard(None)
    if mode == "git":
        out.update((_tree_names(root, "HEAD", deadline) or {}).get(registry, ()))
    return frozenset(out)


def check_edit(tool, tool_input, path, kind, project=frozenset, root=lambda: None):
    """Новые имена правки манифеста path; project — как у fresh_names; root() — корень проекта для head_names
    или None, зовётся только для версий git. None — правку не вычислить. Unavailable — не проверить: не
    разобран или манифесты проекта не перечислить."""
    texts = edit_texts(tool, tool_input, path)
    if texts is None:
        return None
    before, after = texts
    return edit_names(kind, before, after, lambda: head_names(path, kind, HEAD_TIMEOUT, root()), project)


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("не уложился в срок")
    return left


def _walk(root, deadline):
    """Пути манифестов (watched_kind) под root обходом каталогов без FOREIGN_DIRS; Unavailable — файлов больше
    MAX_WALK_FILES."""
    found, seen = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        _remaining(deadline)
        dirnames[:] = [d for d in dirnames if d not in FOREIGN_DIRS]
        seen += len(filenames)
        if seen > MAX_WALK_FILES:
            raise Unavailable(f"вне git больше {MAX_WALK_FILES} файлов")
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if watched_kind(rel):
                found.append(rel)
    return "walk", found


def list_manifests(root, deadline):
    """("git" или "walk", [путь манифеста от root]); манифест — путь, где watched_kind не None. В git —
    отслеживаемые и неотслеживаемые не исключённые файлы (`ls-files -co --exclude-standard`), вне git — обход;
    в обоих режимах без каталогов FOREIGN_DIRS. Unavailable — манифестов больше MAX_MANIFESTS; TimeoutError —
    срок."""
    out = _git(root, _remaining(deadline), "ls-files", "-z", "-c", "-o", "--exclude-standard", root=root)
    if out is None:
        mode, found = _walk(root, deadline)
    else:
        mode = "git"
        found = list(dict.fromkeys(rel for rel in (os.fsdecode(e) for e in out.split(b"\0") if e)
                                   if watched_kind(rel)))
    if len(found) > MAX_MANIFESTS:
        raise Unavailable(f"манифестов больше {MAX_MANIFESTS}")
    return mode, found


def _stat(path):
    """[size, mtime_ns] обычного файла; None — нет файла или не обычный файл."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return [st.st_size, st.st_mtime_ns] if stat.S_ISREG(st.st_mode) else None


def _text(path):
    """Текст манифеста; None — нет, не прочитан или больше MAX_MANIFEST_BYTES."""
    try:
        return _read(path)
    except Unavailable:
        return None


def _parse(path, kind):
    """(имена с транзитивными или None, имя пакета самого манифеста или None) манифеста."""
    text = _text(path)
    if text is None:
        return None, None
    return _known(kind, text), manifests.own_name(kind, text)


def take(root, deadline):
    """Снимок: {"root", "mode", "ts", "files": {путь: [size, mtime_ns, имена с транзитивными или None,
    имя пакета манифеста или None]}}; поле "known" (restored_names) добавляет вызывающий. Файлы _LEGACY
    рабочего дерева в снимок не входят."""
    mode, found = list_manifests(root, deadline)
    entry = {"root": str(root), "mode": mode, "ts": time.time(), "files": {}}
    for rel in found:
        _remaining(deadline)
        path = os.path.join(root, rel)
        st = _stat(path)
        if st is None:
            continue
        known, own = _parse(path, watched_kind(rel))
        entry["files"][rel] = [*st, None if known is None else sorted(known), own]
    return entry


def compare(entry, deadline, skip=frozenset()):
    """({путь: новые имена}, [пути, которые не проверить]) после команды против снимка entry.

    Новый манифест сравнивается с пустым; пропавший — ничего; манифест с тем же size и mtime не
    перечитывается; манифест, чей realpath в skip, не проверяется. Имя из любого манифеста того же реестра
    (manifests.registry) в снимке, из ref до начала сессии (entry["known"], restored_names), в git — из
    манифестов и файлов _LEGACY версии HEAD (_tree_names, читается, только если после old и версий файла
    что-то осталось) и имя пакета самого проекта (в снимке или в манифесте после команды) — не новое: перенос,
    копия, член workspace, возврат работы автора. Unavailable — снимок другого режима."""
    root = entry["root"]
    mode, found = list_manifests(root, deadline)
    if mode != entry["mode"]:
        raise Unavailable("за команду корень проекта " +
                          ("стал git-репозиторием" if mode == "git" else "перестал быть git-репозиторием"))
    before = entry["files"]
    project = {registry: set(names) for registry, names in entry.get("known", {}).items()}
    for rel, (_, _, known, own) in before.items():
        names = project.setdefault(manifests.registry(watched_kind(rel)), set())
        names.update(known or ())
        if own is not None:
            names.add(own)
    changed, unknown = [], []
    for rel in found:
        _remaining(deadline)
        path = os.path.join(root, rel)
        if os.path.realpath(path) in skip:
            continue
        st = _stat(path)
        was = before.get(rel)
        if st is None or isinstance(was, list) and was[:2] == st:
            continue
        kind = watched_kind(rel)
        text = _text(path)
        new = None if text is None else manifests.names(kind, text)
        if new is None:
            unknown.append(rel)
            continue
        own = manifests.own_name(kind, text)
        project.setdefault(manifests.registry(kind), set()).update(() if own is None else (own,))
        changed.append((rel, path, kind, new, was))
    head_tree = []

    def project_of(kind):
        if mode == "git" and not head_tree:
            head_tree.append(_tree_names(root, "HEAD", deadline) or {})
        registry = manifests.registry(kind)
        return frozenset(project.get(registry, ())).union(head_tree[0].get(registry, ()) if head_tree else ())
    added = {}
    for rel, path, kind, new, was in changed:
        if was is None:
            old = frozenset()
        else:
            old = None if was[2] is None else frozenset(was[2])

        def head(path=path, kind=kind):
            return head_names(path, kind, _remaining(deadline), root) if mode == "git" else None
        try:
            names = fresh_names(old, new, head, lambda kind=kind: project_of(kind))
        except Unavailable:
            unknown.append(rel)
            continue
        if names:
            added[rel] = names
    return added, unknown


# Флаги со значением следующим словом: глобальные git до подкоманды и подкоманд, из чьих операндов берётся ref.
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
_MERGE_VALUE_FLAGS = {"-m", "-F", "--file", "-s", "--strategy", "-X", "--strategy-option", "--cleanup",
                      "--into-name"}
_CHERRY_PICK_VALUE_FLAGS = {"-m", "--mainline", "-X", "--strategy-option", "--strategy", "--cleanup"}
_CHECKOUT_VALUE_FLAGS = {"-b", "-B", "--orphan", "--conflict", "--pathspec-from-file"}


def _operands(args, value_flags):
    """(операнды до `--` без флагов и их значений, стоит ли `--`)."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return out, True
        if a.startswith("-") and a != "-":
            i += 2 if depcheck._takes_value(a, value_flags) else 1
            continue
        out.append(a)
        i += 1
    return out, False


def _restore_source(args):
    """Значение `--source`/`-s` команды `git restore`; None — нет."""
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in ("--source", "-s"):
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--source="):
            return a[len("--source="):]
        if a.startswith("-s") and not a.startswith("--"):
            return a[2:]
    return None


def command_refs(command):
    """[(ref, это stash)] — откуда команда git кладёт файлы в рабочее дерево без коммита, по сегментам
    (depcheck._segments, depcheck._command): `git stash pop|apply [N|stash@{N}]` (по умолчанию stash@{0}),
    `git merge --squash <ref>…`, `git checkout <ref> [--] <пути>`, `git restore --source <ref>`,
    `git cherry-pick -n|--no-commit <ref>…`.

    Остальное — пропуск разбора, имена не добавляются: `git apply` (патч, не ref), merge, cherry-pick и
    checkout ветки с коммитом — их имена в HEAD или источнике незавершённой операции (_REPO_REFS). `git -C`
    не учитывается: ref разрешается в репозитории проекта."""
    refs = []
    for segment in depcheck._segments(command):
        words, _ = depcheck._command(segment.strip())
        if not words or depcheck._basename(words[0]) != "git":
            continue
        sub = depcheck._subcommand(words, _GIT_VALUE_FLAGS)
        if len(sub) < 2:
            continue
        name, args = sub[1], sub[2:]
        if name == "stash" and args[:1] in (["pop"], ["apply"]):
            operands, _ = _operands(args[1:], depcheck._NO_VALUE_FLAGS)
            rev = operands[0] if operands else "0"
            refs.append((f"stash@{{{rev}}}" if rev.isdigit() else rev, True))
        elif name == "merge" and "--squash" in args:
            refs += [(rev, False) for rev in _operands(args, _MERGE_VALUE_FLAGS)[0]]
        elif name == "cherry-pick" and ("-n" in args or "--no-commit" in args):
            refs += [(rev, False) for rev in _operands(args, _CHERRY_PICK_VALUE_FLAGS)[0]]
        elif name == "checkout":
            # Один операнд без `--` — ветка или путь из индекса: файлы не из названного ref.
            operands, dashdash = _operands(args, _CHECKOUT_VALUE_FLAGS)
            if operands and (dashdash or len(operands) > 1):
                refs.append((operands[0], False))
        elif name == "restore":
            rev = _restore_source(args)
            if rev:
                refs.append((rev, False))
    return refs


_COMMITTER = re.compile(rb"^committer .*> (\d+) [+-]\d{4}$", re.M)
_TREE = re.compile(rb"^tree ([0-9a-f]+)$", re.M)


def _old_trees(root, refs, start, deadline):
    """Деревья коммитов refs, созданных до start: время коммиттера коммита (у stash — коммита stash) меньше
    start. У stash дерево коммита — рабочее дерево на момент stash, неотслеживаемые файлы (`stash -u`) — в
    дереве его третьего родителя. Ref не найден — пропуск."""
    lines, primary = [], []
    for rev, stash in refs:
        # Ввод --batch построчный.
        if "\n" in rev or "\r" in rev:
            continue
        lines.append(f"{rev}^{{commit}}")
        primary.append(True)
        if stash:
            lines.append(f"{rev}^3^{{commit}}")
            primary.append(False)
    if not lines:
        return []
    out = _git(root, _remaining(deadline), "cat-file", "--batch", root=root,
               input=os.fsencode("".join(f"{l}\n" for l in lines)))
    if out is None:
        return []
    trees, accepted = [], False
    for is_primary, obj in zip(primary, _objects(out)):
        if is_primary:
            accepted = False
        if obj is None or obj[0] != b"commit":
            continue
        head = obj[1].split(b"\n\n", 1)[0]
        tree = _TREE.search(head)
        if is_primary:
            committed = _COMMITTER.search(head)
            accepted = committed is not None and int(committed.group(1)) < start
        if accepted and tree:
            trees.append(tree.group(1).decode("ascii"))
    return list(dict.fromkeys(trees))


def _tree_names(root, tree, deadline):
    """{реестр: имена с транзитивными} манифестов и файлов _LEGACY (source_kind) дерева tree — ref или SHA
    дерева; None — git не прочитал дерево (нет HEAD, не репозиторий). Пути с переводом строки не ложатся в
    построчный ввод --batch и пропускаются. Unavailable — манифестов в дереве больше MAX_MANIFESTS;
    TimeoutError — срок deadline."""
    listed = _git(root, _remaining(deadline), "ls-tree", "-r", "-z", "--name-only", "--full-tree", tree, root=root)
    if listed is None:
        return None
    paths = [p for p in (os.fsdecode(e) for e in listed.split(b"\0") if e)
             if source_kind(p) and "\n" not in p and "\r" not in p]
    if len(paths) > MAX_MANIFESTS:
        raise Unavailable(f"манифестов в {tree} больше {MAX_MANIFESTS}")
    known = {}
    if not paths:
        return known
    out = _git(root, _remaining(deadline), "cat-file", "--batch", root=root,
               input=os.fsencode("".join(f"{tree}:{p}\n" for p in paths)))
    if out is None:
        return None
    for path, obj in zip(paths, _objects(out)):
        if obj is None or obj[0] != b"blob" or len(obj[1]) > MAX_MANIFEST_BYTES:
            continue
        kind = source_kind(path)
        names = _known(kind, obj[1].decode("utf-8", "replace"))
        if names:
            known.setdefault(_registry(kind), set()).update(names)
    return known


def restored_names(root, command, start, deadline):
    """{реестр: имена с транзитивными} манифестов и файлов _LEGACY ref, откуда command возвращает файлы
    (command_refs), если ref создан до start — начала сессии (session_start); это работа до сессии, после
    команды такие имена не новые. start None, ref моложе или не найден — имена не добавляются: сравнение
    блокирует, как без ref. Unavailable — имена ref не прочитаны: срок deadline вышел, манифестов в дереве больше
    MAX_MANIFESTS или git не прочитал дерево.

    Время ref — время коммиттера: коммит с поддельной датой (GIT_COMMITTER_DATE) проходит как старый."""
    refs = command_refs(command)
    if not refs or start is None:
        return {}
    known = {}
    try:
        for tree in _old_trees(root, refs, start, deadline):
            names = _tree_names(root, tree, deadline)
            if names is None:
                raise Unavailable(f"дерево ref {tree} не прочитано")
            for registry, found in names.items():
                known.setdefault(registry, set()).update(found)
    except TimeoutError:
        raise Unavailable("имена ref команды не прочитаны за срок") from None
    return {registry: sorted(names) for registry, names in known.items()}


# Подкоманды, печатающие установленные или зафиксированные пакеты в формате requirements: у pip и `uv pip` —
# freeze, у uv, poetry, pdm — export, у pipenv — requirements.
_GENERATORS = {"uv": {"export"}, "poetry": {"export"}, "pdm": {"export"}, "pipenv": {"requirements"}}
_OUTPUT_FLAGS = ("--output", "--output-file", "-o")


def _is_generator(words):
    """Слова команды (depcheck._command) — генератор requirements: `pip freeze`, `python -m pip freeze`,
    `uv pip freeze`, `uv export`, `poetry export`, `pdm export`, `pipenv requirements`."""
    if not words:
        return False
    name = depcheck._basename(words[0])
    if depcheck._PYTHON.match(name):
        module = depcheck._python_module(words)
        if not module or module[0] != "pip":
            return False
        words, name = ["pip", *module[1:]], "pip"
    if depcheck._PIP.match(name):
        return depcheck._subcommand(words, depcheck._PIP_GLOBAL_VALUE_FLAGS)[1:2] == ["freeze"]
    if name not in _GENERATORS:
        return False
    sub = depcheck._subcommand(words, depcheck._GLOBAL_FLAGS.get(name, depcheck._NO_VALUE_FLAGS))
    if name == "uv" and sub[1:2] == ["pip"]:
        return depcheck._after_flags(sub[2:], depcheck._UV_PIP_VALUE_FLAGS)[:1] == ["freeze"]
    return sub[1:2] != [] and sub[1] in _GENERATORS[name]


def _outputs(segment, words):
    """Файлы вывода сегмента: цели перенаправлений stdout (`>`, `>>`, `>|`, `&>`) и значения `-o`, `--output`,
    `--output-file`."""
    out = []
    pairs = depcheck._split(segment)
    for i, (word, lead) in enumerate(pairs):
        m = depcheck._REDIRECT.match(lead)
        if not m:
            continue
        op = m.group(0)
        fd = op.rstrip("<>&|")
        if "<" in op or op.endswith(">&") or fd not in ("", "1", "&"):
            continue
        target = word[m.end():] if len(word) > m.end() else (pairs[i + 1][0] if i + 1 < len(pairs) else "")
        out.append(target)
    for i, w in enumerate(words):
        for flag in _OUTPUT_FLAGS:
            if w == flag and i + 1 < len(words):
                out.append(words[i + 1])
            elif w.startswith(flag + "=") or flag == "-o" and w.startswith("-o") and len(w) > 2:
                out.append(w[len(flag) + 1:] if w.startswith(flag + "=") else w[2:])
    return [t for t in out if t]


def _tee_outputs(segment, words):
    """Файлы `tee` сегмента без своего ввода (`<`, heredoc); не tee — []."""
    if not words or depcheck._basename(words[0]) != "tee":
        return []
    for _, lead in depcheck._split(segment):
        m = depcheck._REDIRECT.match(lead)
        if m and "<" in m.group(0):
            return []
    out, flags = [], True
    for w in words[1:]:
        if flags and w == "--":
            flags = False
        elif not (flags and w.startswith("-")):
            out.append(w)
    return out


def generated_requirements(command):
    """Пути файлов requirements, как написаны в команде, куда пишет генератор (_is_generator) своего сегмента
    или `tee` сегмента сразу за генератором (`pip freeze | tee requirements.txt`).

    Генератор, направленный в рукописный файл, снимает с него проверку. tee сразу за генератором через `;`
    или `&&` тоже узнаётся. Конвейер с фильтром (`pip freeze | sort > requirements.txt`) не узнаётся:
    перенаправление не в сегменте генератора и не в tee за ним, файл проверяется."""
    targets = []
    after_generator = False
    for segment in depcheck._segments(command):
        segment = segment.strip()
        if not segment:
            continue
        words, _ = depcheck._command(segment)
        if _is_generator(words):
            outputs = _outputs(segment, words)
            after_generator = True
        else:
            outputs = _tee_outputs(segment, words) if after_generator else []
            after_generator = False
        targets += [t for t in outputs if manifests.kind(t) == "requirements"]
    return targets


def resolve(targets, cwds):
    """realpath каждого пути targets от каждого каталога cwds (относительный путь) или сам (абсолютный)."""
    out = set()
    for target in targets:
        target = os.path.expanduser(target)
        for cwd in cwds:
            if os.path.isabs(target) or isinstance(cwd, str) and cwd:
                out.add(os.path.realpath(os.path.join(cwd or "", target)))
    return frozenset(out)


def _state_path(state_dir, session):
    return state_dir / f"{common.safe_name(session)}.manifests.json"


def store(session, tool_use_id, entry):
    """Снимок entry в state/<session>.manifests.json под ключом tool_use_id; записи старше ENTRY_TTL
    удаляются."""
    state_dir = common.data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = _state_path(state_dir, session)
    cutoff = time.time() - ENTRY_TTL
    with common.state_lock(state_dir):
        state = common.read_json(path, dict)
        state = {k: v for k, v in state.items()
                 if isinstance(v, dict) and isinstance(v.get("ts"), (int, float)) and v["ts"] >= cutoff}
        state[tool_use_id] = entry
        common.atomic_write_json(path, state)
    common.prune_state(state_dir)


def pop(session, tool_use_id):
    """Снимок команды tool_use_id, удалённый из состояния; None — нет или не той формы."""
    state_dir = common.data_dir() / "state"
    path = _state_path(state_dir, session)
    if not path.exists():
        return None
    with common.state_lock(state_dir):
        state = common.read_json(path, dict)
        entry = state.pop(tool_use_id, None)
        if entry is None:
            return None
        if state:
            common.atomic_write_json(path, state)
        else:
            path.unlink(missing_ok=True)
    valid = (isinstance(entry, dict) and isinstance(entry.get("root"), str) and entry.get("mode") in ("git", "walk")
             and isinstance(entry.get("files"), dict)
             and all(isinstance(v, list) and len(v) == 4 and (v[2] is None or isinstance(v[2], list))
                     and (v[3] is None or isinstance(v[3], str)) for v in entry["files"].values())
             and isinstance(entry.get("known", {}), dict)
             and all(isinstance(v, list) for v in entry.get("known", {}).values()))
    return entry if valid else None


def _start_path(state_dir, session):
    return state_dir / f"{common.safe_name(session)}.start.json"


def mark_start(session):
    """Начало сессии — целые секунды вниз — в state/<session>.start.json при первом вызове; дальше файл не
    меняется. Зовётся на каждом PreToolUse judge_tool: до первого вызова агент не запускал ни Bash, ни правок."""
    state_dir = common.data_dir() / "state"
    path = _start_path(state_dir, session)
    if path.exists():
        return
    state_dir.mkdir(exist_ok=True)
    with common.state_lock(state_dir):
        if not path.exists():
            common.atomic_write_json(path, {"start": int(time.time())})


def session_start(session):
    """Начало сессии из mark_start, секунды; None — нет записи или она не той формы."""
    start = common.read_json(_start_path(common.data_dir() / "state", session), dict).get("start")
    return start if type(start) is int else None
