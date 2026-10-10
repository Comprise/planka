"""Новые имена зависимостей в манифестах: правка файловым инструментом и команда Bash.

Правка — текст манифеста до и после по входу Write, Edit, MultiEdit. Команда — снимок имён всех
манифестов проекта перед ней (state/<session>.manifests.json по tool_use_id) и сравнение после.
Новым не считается имя из текста до правки вместе с транзитивными, из версии манифеста в HEAD, в
источнике незавершённых merge, cherry-pick, rebase, revert и в сторонах конфликта индекса, из другого манифеста
того же реестра в проекте и в HEAD, из файла _LEGACY в HEAD, имя пакета самого проекта — манифеста в HEAD
(кроме npm и PyPI) или члена workspace (монорепозиторий, корень — _workspace_root): у npm — только зависимость,
которую npm связывает с каталогом члена (manifests.NpmWorkspace), у uv — только в pyproject.toml workspace с
источником `workspace = true` (_uv_inherited и manifests.names), — и, после команды, имя из манифестов ref, откуда
команда git возвращает файлы (stash, ветка, коммит), если ref создан до начала сессии, и из изменённых строк
манифестов в файле патча `git apply`, не менявшемся с начала сессии. Подключение файла (manifests.Include) — путь
цели от корня проекта (_rooted) в каждом источнике имён; не новое имя, если файл — манифест из перечня проекта того
вида, которым его читает менеджер, и проверяется сам (_unchecked).
"""
import ast
import configparser
import functools
import os
import pathlib
import posixpath
import re
import stat
import subprocess
import time
import tomllib
import warnings

import common
import depcheck
import manifests
import pkgmanagers
import snapshot

# Срок снимка манифестов перед командой Bash и сравнения после неё, секунды от старта хука; без
# common.GIT_ROOT_TIMEOUT корня проекта (tests/test_contract.py, TimeoutsTest).
SNAPSHOT_BUDGET = 5
CHECK_BUDGET = 5
# Срок вызовов git версий из _REPO_REFS и _INDEX_STAGES (head_names) при правке файловым инструментом, секунды.
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


def watched_kind(path, fold=False):
    """Вид манифеста manifests.kind по пути (fold — без учёта регистра имени); None — не манифест или в каталоге
    FOREIGN_DIRS."""
    parts = re.split(r"[\\/]", path)
    if FOREIGN_DIRS.intersection(parts[:-1]):
        return None
    return manifests.kind(path, fold)


def _relative(path, root):
    """path от root, если path под root; иначе path как есть."""
    if root is None:
        return path
    rel = os.path.relpath(path, root)
    return path if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def listing(root):
    """Перечень манифестов проекта для жёстких ссылок, подключений и project_names на хук (_Listing)."""
    return _Listing(root)


class _Listing:
    """Перечень манифестов проекта на хук: вызов () → (корень root(), [путь от корня]) или None — проект неизвестен.
    list_manifests зовётся при первом вызове в срок HEAD_TIMEOUT и один раз за хук: результат, режим (mode, "git" или
    "walk") и ошибка (Unavailable, TimeoutError) запоминаются. versions() — один _VersionNpm корня на хук или None —
    проект неизвестен: дерево HEAD читается один раз для head_names и project_names."""
    __slots__ = ("root", "memo", "mode", "shared")

    def __init__(self, root):
        self.root, self.memo, self.mode, self.shared = root, None, None, None

    def __call__(self):
        if self.memo is None:
            try:
                base = self.root()
                if base is None:
                    self.memo = (None, None)
                else:
                    self.mode, found = list_manifests(base, time.monotonic() + HEAD_TIMEOUT)
                    self.memo = ((base, found), None)
            except (Unavailable, TimeoutError) as e:
                self.memo = (None, e)
        result, error = self.memo
        if error is not None:
            raise error
        return result

    def versions(self):
        base = self.root()
        if base is None:
            return None
        if self.shared is None:
            self.shared = _VersionNpm(base)
        return self.shared


def edit_targets(path, root, listed=None):
    """([(путь, вид)] манифестов, которые правит файловый инструмент по пути path, причина или None), причина —
    почему жёсткая ссылка не сверена с манифестами проекта.

    Манифест по имени: вид — watched_kind пути от корня проекта root() (None — путь как есть) и пути с разрешёнными
    символическими ссылками от root() с разрешёнными ссылками: манифест хоть по одному из них — манифест. Путь —
    разрешённый, если манифест он: ссылка `deps.json -> package.json` и каталог-ссылка `build -> .` правят манифест
    проекта. На файловой системе без учёта регистра (common.case_insensitive) пути и корень — с компонентами,
    приведёнными к записям каталогов (_on_disk): `REQUIREMENTS/BASE.TXT` — манифест requirements/base.txt.
    Файл с несколькими жёсткими ссылками — ещё все манифесты проекта с тем же файлом, каждый по своему виду, каким бы
    ни было имя path и его каталог (_linked_targets; listed — перечень listing(root), None — свой). root зовётся,
    только если путь без учёта регистра похож на манифест или у файла больше одной жёсткой ссылки."""
    real = os.path.realpath(path)
    # Приведение к записям каталогов меняет только регистр: манифестом станет лишь путь, который манифест в нижнем
    # регистре; остальным пути проба регистра и обход каталогов не нужны.
    if not any(manifests.kind(p.lower(), fold=True) for p in (path, real)):
        named = None
    elif common.case_insensitive(real):
        named = _named_target(path, real, root, cased=True)
    elif manifests.kind(path) is None and manifests.kind(real) is None:
        named = None
    else:
        named = _named_target(path, real, root)
    targets = [] if named is None else [named]
    try:
        linked = _linked_targets(real, listed or listing(root))
    except Unavailable as e:
        return targets, str(e)
    seen = {(os.path.realpath(p), k) for p, k in targets}
    targets += [(p, k) for p, k in linked if (os.path.realpath(p), k) not in seen]
    return targets, None


def _named_target(path, real, root, cased=False):
    """(путь, вид) манифеста по имени path или real (manifests.kind) вне каталогов FOREIGN_DIRS; None — нет.

    cased — файловая система без учёта регистра: пути и корень — _on_disk, вид — без учёта регистра базового имени
    (fold): нового файла `PACKAGE.JSON` ещё нет в каталоге, а npm прочтёт его как package.json."""
    root = root()
    real_root = None if root is None else os.path.realpath(root)
    if cased:
        path, real = _on_disk(path), _on_disk(real)
        root, real_root = (None, None) if root is None else (_on_disk(root), _on_disk(real_root))
    kind = watched_kind(_relative(real, real_root), fold=cased)
    if kind is not None:
        return real, kind
    kind = watched_kind(_relative(path, root), fold=cased)
    return None if kind is None else (path, kind)


def _on_disk(path):
    """Абсолютный path с компонентами, приведёнными к записям каталогов, для файловой системы без учёта регистра:
    имя в другом регистре там находит ту же запись, а realpath регистр не правит. Компонент — запись своего каталога
    с тем же именем без учёта регистра и тем же файлом (os.path.samefile), другая, чем написано, если такая есть;
    компонента нет на диске (новый файл, нет доступа) — он и остальные как написаны."""
    parts = pathlib.PurePath(os.path.abspath(path)).parts
    current = parts[0]
    for i, name in enumerate(parts[1:], 1):
        try:
            entries = sorted(os.listdir(current))
        except (OSError, ValueError):
            return os.path.join(current, *parts[i:])
        written = os.path.join(current, name)
        found = None
        for entry in entries:
            if entry.casefold() != name.casefold():
                continue
            try:
                if os.path.samefile(os.path.join(current, entry), written):
                    found = entry
            except (OSError, ValueError):
                continue
            if found is not None and found != name:
                break
        if found is None:
            return os.path.join(current, *parts[i:])
        current = os.path.join(current, found)
    return current


def _linked_targets(real, listed):
    """[(путь, вид)] манифестов проекта из перечня listed() (listing), которые — тот же файл, что real (совпали st_dev
    и st_ino), если у real больше одной жёсткой ссылки; каждый — по своему виду (watched_kind). Пусто — нет файла, не
    обычный файл, ссылка одна или проект неизвестен; в первых трёх случаях перечень не нужен. Unavailable —
    манифесты проекта не перечислены (срок HEAD_TIMEOUT или предел list_manifests)."""
    try:
        st = os.stat(real)
    except (OSError, ValueError):
        return []
    if st.st_nlink < 2 or not stat.S_ISREG(st.st_mode):
        return []
    try:
        found = listed()
    except TimeoutError:
        raise Unavailable("жёсткая ссылка не сверена с манифестами проекта: не уложился в срок") from None
    except Unavailable as e:
        raise Unavailable(f"жёсткая ссылка не сверена с манифестами проекта: {e}") from None
    if found is None:
        return []
    root, rels = found
    out = []
    for rel in rels:
        path = os.path.join(root, rel)
        try:
            other = os.stat(path)
        except (OSError, ValueError):
            continue
        if (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino):
            out.append((path, watched_kind(rel)))
    return out


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


def _known(kind, text, workspace=None):
    """Имена с транзитивными (manifests.known_names) манифеста или файла _LEGACY вида kind; None — не разобран.
    workspace — manifests.NpmWorkspace package.json или None."""
    if kind not in _LEGACY:
        return manifests.known_names(kind, text, workspace)
    lines = _LEGACY_PARSERS[kind](text)
    # Строки требований разбирает разбор файла требований: имя PEP 508, нормализация PEP 503.
    return None if lines is None else manifests.known_names("requirements", "\n".join(lines))


def _read(path, kind=None):
    """Текст файла, декодированный, как его читает менеджер вида kind (manifests.decode; None — UTF-8 с заменой),
    переводы строк как есть; None — нет файла; Unavailable — не обычный файл, больше MAX_MANIFEST_BYTES или не
    прочитан. Файл открывается с O_NONBLOCK: FIFO не вешает хук."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unavailable(f"не прочитан: {e!r}") from None
    try:
        with open(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unavailable("не обычный файл")
            data = f.read(MAX_MANIFEST_BYTES + 1)
    except OSError as e:
        raise Unavailable(f"не прочитан: {e!r}") from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise Unavailable(f"больше {MAX_MANIFEST_BYTES} байт")
    return manifests.decode(kind, data)


def _replace(text, old, new, replace_all):
    """Текст после замены, как у Edit; None — инструмент откажет: old не найден или неоднозначен."""
    count = text.count(old) if old else 0
    if count == 0 or count > 1 and not replace_all:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def edit_texts(tool, tool_input, path, kind=None):
    """(текст до или None — файла нет, текст после) правки файловым инструментом файла path вида kind (_read);
    None — правку не вычислить или инструмент сам откажет. Unavailable — файл не прочитан.

    Edit и MultiEdit, как Claude Code, сопоставляют old_string с текстом файла, где CRLF приведены к LF;
    текст после — с переводами строк LF. У текста с другим прочтением (manifests.Decoded.other) правка, которая не
    ложится на первое, вычисляется по второму: какую кодировку видит инструмент, хук не знает."""
    current = _read(path, kind)
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
    other = getattr(current, "other", None)
    for view in [current] if other is None else [current, other]:
        after = _apply_edits(view, edits)
        if after is not None:
            return current, after
    return None


def _apply_edits(current, edits):
    """Текст current после замен edits, как у Edit и MultiEdit; None — инструмент откажет или правку не вычислить."""
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
    return text


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
_VERSIONS = _REPO_REFS + _INDEX_STAGES


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


def _inside(path, root):
    """Каталог path — root или под ним, символические ссылки разрешены."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Разные диски Windows.
        return False


def head_names(path, kind, timeout, root=None, versions=None):
    """Имена манифеста path вместе с транзитивными (manifests.known_names) в версиях _REPO_REFS и сторонах
    конфликта _INDEX_STAGES его репозитория, объединённые; None — нет репозитория, файла ни в одной версии или
    ни одна не разобрана. `cat-file --batch` отдаёт байты блобов без textconv и фильтров одним вызовом git;
    package.json каждой версии разбирается с npm workspace той же версии (_VersionNpm). timeout — срок всех вызовов
    git вместе, секунды.
    root — корень проекта (common.project_root) или None; файл под root ищет репозиторий не выше root (_git),
    файл вне root и при None — свой репозиторий без ограничения и без npm workspace. versions — _VersionNpm корня
    root, общий для манифестов одной правки или сравнения; None — свой."""
    deadline = time.monotonic() + timeout
    name = os.path.basename(path)
    cwd = os.path.dirname(path) or "."
    if root is not None and not _inside(cwd, root):
        root = None
    if "\n" in name or "\r" in name:
        # Ввод --batch построчный: имя с переводом строки или возвратом каретки — только версия HEAD отдельным
        # вызовом.
        out = _git(cwd, _remaining(deadline), "cat-file", "blob", f"HEAD:./{name}", root=root)
        blobs = [] if out is None else [("HEAD", out)]
    else:
        out = _git(cwd, _remaining(deadline), "cat-file", "--batch", root=root,
                   input=os.fsencode("".join(f"{ref}:./{name}\n" for ref in _REPO_REFS)
                                     + "".join(f"{stage}./{name}\n" for stage in _INDEX_STAGES)))
        blobs = [] if out is None else [(version, obj[1]) for version, obj in zip(_VERSIONS, _objects(out))
                                         if obj is not None and obj[0] == b"blob"]
    texts = {version: manifests.decode(kind, data) for version, data in blobs if len(data) <= MAX_MANIFEST_BYTES}
    npm = {}
    if kind == "package.json" and root is not None and texts:
        if versions is None:
            versions = _VersionNpm(root)
        npm = versions.of(_project_rel(path, root), texts, deadline)
    found = [manifests.known_names(kind, text, npm.get(version)) for version, text in texts.items()]
    found = [names for names in found if names is not None]
    return frozenset().union(*found) if found else None


def _spec(version, path):
    """Объект `cat-file` файла path от каталога вызова в версии version: ref — `ref:./path`, сторона индекса —
    `:N:./path`."""
    return f"{version}./{path}" if version.startswith(":") else f"{version}:./{path}"


def _package_text(obj):
    """Текст package.json объекта _objects; None — не блоб, нет объекта или больше MAX_MANIFEST_BYTES."""
    if obj is None or obj[0] != b"blob" or len(obj[1]) > MAX_MANIFEST_BYTES:
        return None
    return manifests.decode("package.json", obj[1])


class _VersionNpm:
    """npm workspace версий _VERSIONS проекта root для разбора package.json в head_names и деревья ref с блобами для
    _tree_names; один на правку (_Listing.versions) или сравнение (compare), чтобы дерево версии читалось один раз, а
    не на каждый изменённый package.json и не второй раз для имён проекта. Дерево версии для npm (_tree) читается,
    только если в этой версии у самого package.json или у package.json его каталога-предка есть `workspaces`; тексты
    предков запоминаются. Пути — от root: `git -C root` с путями `./` в cat-file и ls-files без --full-tree; ls-tree
    (entries) — с --full-tree: git ищет репозиторий не выше root (_git), так что пути от вершины дерева — пути от
    root."""
    __slots__ = ("root", "ancestors", "trees", "index", "blobs", "listed")

    def __init__(self, root):
        # ancestors — {(версия, каталог): текст package.json или None}; trees — {версия: _Npm или None};
        # index — {путь: {стадия: SHA}} `ls-files -s`; blobs — {SHA: байты блоба или None}; listed — {ref: {путь:
        # SHA блоба} или None}.
        self.root, self.ancestors, self.trees, self.index, self.blobs, self.listed = root, {}, {}, None, {}, {}

    def of(self, rel, texts, deadline):
        """{версия: manifests.NpmWorkspace} package.json rel (путь от root) с текстами версий texts {версия: текст}.
        Версии нет в ответе (разбор без workspace), если ни у файла, ни у package.json предков в ней нет
        `workspaces`, её дерево не прочитано или package.json в нём больше MAX_MANIFESTS."""
        rel = rel.replace(os.sep, "/")
        directories = _ancestors(rel)
        wanted = [(version, directory) for version in texts if version not in self.trees
                  for directory in directories if (version, directory) not in self.ancestors]
        if wanted:
            self._read_ancestors(wanted, deadline)
        out = {}
        for version, text in texts.items():
            if version not in self.trees:
                own = [text, *(self.ancestors[(version, directory)] for directory in directories)]
                if not any(manifests.workspace_members("package.json", t) for t in own if t is not None):
                    continue
                self.trees[version] = self._tree(version, deadline)
            npm = self.trees[version]
            workspace = None if npm is None else npm.of("package.json", rel)
            if workspace is not None:
                out[version] = workspace
        return out

    def _read_ancestors(self, wanted, deadline):
        """Тексты package.json каталогов wanted [(версия, каталог)] одним `cat-file --batch`; у стороны конфликта
        индекса файл без этой стороны — его стадия 0. Каталог с переводом строки не ложится в построчный ввод — его
        package.json как нет."""
        specs, fed = [], []
        for version, directory in wanted:
            if "\n" in directory or "\r" in directory:
                self.ancestors[(version, directory)] = None
                continue
            path = posixpath.join(directory, "package.json")
            specs.append(_spec(version, path))
            if version in _INDEX_STAGES:
                specs.append(_spec(":0:", path))
            fed.append((version, directory))
        out = _git(self.root, _remaining(deadline), "cat-file", "--batch", root=self.root,
                   input=os.fsencode("".join(f"{spec}\n" for spec in specs))) if specs else None
        objects = [] if out is None else _objects(out)
        if len(objects) != len(specs):
            objects = [None] * len(specs)
        objects = iter(objects)
        for version, directory in fed:
            obj = next(objects)
            if version in _INDEX_STAGES:
                zero = next(objects)
                obj = zero if obj is None else obj
            self.ancestors[(version, directory)] = _package_text(obj)

    def _tree(self, version, deadline):
        """_Npm дерева версии (_tree_npm): package.json из `ls-tree -r` ref или, у стороны конфликта, из
        `ls-files -s` (запись стороны, иначе стадия 0); None — дерево не прочитано, package.json нет или их больше
        MAX_MANIFESTS."""
        if version in _INDEX_STAGES:
            if self.index is None:
                self.index = _index_entries(_git(self.root, _remaining(deadline), "ls-files", "-s", "-z",
                                                 root=self.root))
            entries = {path: stages.get(version[1], stages.get("0")) for path, stages in self.index.items()}
        else:
            entries = self.entries(version, deadline) or {}
        entries = {path: sha for path, sha in entries.items() if sha and watched_kind(path) == "package.json"}
        if not entries or len(entries) > MAX_MANIFESTS:
            return None
        blobs = self.read(entries.values(), deadline) or {}
        texts = {path: blobs.get(sha) for path, sha in entries.items()}
        return _tree_npm({path: manifests.decode("package.json", data) for path, data in texts.items()
                          if data is not None})

    def entries(self, tree, deadline):
        """{путь от корня: SHA блоба} дерева tree — ref или SHA дерева — из `ls-tree -r --full-tree`; None — git не
        прочитал дерево (нет HEAD, не репозиторий). Дерево читается один раз."""
        if tree not in self.listed:
            out = _git(self.root, _remaining(deadline), "ls-tree", "-r", "-z", "--full-tree", tree, root=self.root)
            self.listed[tree] = None if out is None else _tree_entries(out)
        return self.listed[tree]

    def read(self, shas, deadline):
        """{SHA: байты блоба или None — не блоб, объекта нет или больше MAX_MANIFEST_BYTES} блобов shas: ещё не
        прочитанные — одним `cat-file --batch`; None — git их не прочитал, тогда они не запоминаются."""
        shas = list(dict.fromkeys(shas))
        missing = [sha for sha in shas if sha not in self.blobs]
        if missing:
            out = _git(self.root, _remaining(deadline), "cat-file", "--batch", root=self.root,
                       input=os.fsencode("".join(f"{sha}\n" for sha in missing)))
            if out is None:
                return None
            objects = _objects(out)
            for i, sha in enumerate(missing):
                obj = objects[i] if i < len(objects) else None
                self.blobs[sha] = (obj[1] if obj is not None and obj[0] == b"blob" and len(obj[1]) <= MAX_MANIFEST_BYTES
                                   else None)
        return {sha: self.blobs[sha] for sha in shas}


def _tree_entries(out):
    """{путь: SHA блоба} вывода `git ls-tree -r -z` out; out None (git не прочитал) — {}."""
    entries = {}
    for record in (out or b"").split(b"\0"):
        meta, _, path = record.partition(b"\t")
        meta = meta.split()
        if len(meta) == 3 and meta[1] == b"blob":
            entries[os.fsdecode(path)] = meta[2].decode("ascii")
    return entries


def _index_entries(out):
    """{путь: {стадия: SHA}} вывода `git ls-files -s -z` out; out None (git не прочитал) — {}."""
    entries = {}
    for record in (out or b"").split(b"\0"):
        meta, _, path = record.partition(b"\t")
        meta = meta.split()
        if len(meta) == 3:
            entries.setdefault(os.fsdecode(path), {})[meta[2].decode("ascii")] = meta[1].decode("ascii")
    return entries


def fresh_names(old, new, head, project=frozenset):
    """Отсортированные новые имена: из new (объявленные, manifests.names) нет в old, в версиях git (head()) и в
    проекте. old и head() — имена с транзитивными (manifests.known_names), old None — не разобран, head()
    None — версии нет; project() — имена других манифестов того же реестра и пакетов самого проекта, зовётся,
    только если после old и версий git что-то осталось. old не разобран и версии нет — сравнение с пустым: новы
    все имена new, кроме имён проекта."""
    added = None if old is None else new - old
    if added is None or added:
        base = head()
        if added is None:
            added = new if base is None else new - base
        elif base is not None:
            added -= base
    if added:
        added -= project()
    return sorted(added)


def edit_names(kind, before, after, head, project=frozenset, rel=None, workspaces=(None, None)):
    """Новые имена манифеста вида kind с текстом before (None — файла не было) после правки в after;
    head и project — как у fresh_names; rel — путь манифеста от корня проекта для подключений (_rooted), None —
    подключения как записаны; workspaces — manifests.NpmWorkspace package.json до и после правки или None.
    Unavailable — after не разобран."""
    new = manifests.names(kind, after, workspaces[1])
    if new is None:
        raise Unavailable("после правки не разобран")
    old = frozenset() if before is None else manifests.known_names(kind, before, workspaces[0])
    if rel is not None:
        new, old = _rooted(new, rel), _rooted(old, rel)
    return fresh_names(old, new, head, project)


# Путь подключения со схемой URL (`https:`, `git+ssh:`): не файл проекта.
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def _rooted(names, rel):
    """names манифеста по пути rel от корня проекта (вне проекта — абсолютному), где подключение manifests.Include —
    путь цели от корня проекта: разрешённый от каталога манифеста, как его разрешают pip и Bundler, и нормализованный
    os.path.normpath (без разрешения ссылок); у манифеста вне корня rel абсолютный — и путь цели абсолютный, путь со
    схемой URL — как записан. Так одинаковый текст подключения в разных каталогах — разные имена, а один файл из
    разных каталогов — одно. None — None."""
    if names is None:
        return None
    return frozenset(
        manifests.Include(os.path.normpath(os.path.join(os.path.dirname(rel), name.path)))
        if isinstance(name, manifests.Include) and not _SCHEME.match(name.path) else name for name in names)


def _project_rel(path, root):
    """path от корня проекта root: от root как написан или с разрешёнными ссылками (edit_targets отдаёт и путь с
    разрешёнными ссылками); вне корня и при None — path как есть."""
    if root is None:
        return path
    for base in (root, os.path.realpath(root)):
        rel = _relative(path, base)
        if rel != path:
            return rel
    return path


def _glob(pattern):
    """Регулярное выражение шаблона каталога workspace от корня: `*` и `?` — в пределах части пути, `**` — любые
    части; `./` в начале и `/` в конце не значат."""
    out, i = [], 0
    pattern = pattern.strip().removeprefix("./").rstrip("/")
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            out.append({"*": "[^/]*", "?": "[^/]"}.get(pattern[i], re.escape(pattern[i])))
            i += 1
    return re.compile("".join(out) + r"\Z")


# Корневой манифест реестра, чей workspace (manifests.workspace_members) ставит членов из их каталогов по имени.
# Пакет проекта этих реестров менеджер ставит из каталога только по связи workspace: имя пакета манифеста HEAD и
# ref здесь не прикрывает зависимость (_tree_names), в прочем манифесте npm и pip ставят его из реестра.
_WORKSPACE_ROOTS = {"npm": "package.json", "pypi": "pyproject.toml"}


# Запомнен по тексту: _workspace_root зовёт его с текстом предка на каждого члена.
@functools.lru_cache(maxsize=256)
def _globs(name, text):
    """((шаблон каталога _glob, исключение), …) workspace корневого манифеста name с текстом text; None — пусто."""
    patterns = [] if text is None else manifests.workspace_members(manifests.kind(name), text)
    return tuple((_glob(p.removeprefix("!")), p.startswith("!")) for p in patterns)


def _ancestors(rel):
    """Каталоги-предки манифеста rel (путь от корня с `/`), от ближнего до корня `""`; свой каталог не входит."""
    directory, out = posixpath.dirname(rel), []
    while directory:
        directory = posixpath.dirname(directory)
        out.append(directory)
    return out


def _disk(root, override=None):
    """Функция (путь манифеста от root с `/`) → текст файла рабочего дерева (_text) или None, запомненный;
    override — {путь: текст или None} вместо файла (текст правки)."""
    @functools.cache
    def text_of(rel):
        if override and rel in override:
            return override[rel]
        return _text(os.path.join(root, rel), watched_kind(rel))
    return text_of


def _workspace_root(rel, text_of):
    """Каталог корня workspace (путь от корня проекта с `/`), чей член — package.json или pyproject.toml rel (путь от
    корня проекта), или None; text_of(путь) — текст манифеста или None (нет, не прочитан). Корень ищется, как его
    ищет менеджер из каталога члена, не выше корня проекта: npm — ближайший предок, чей package.json с `workspaces`
    включает каталог rel (@npmcli/config loadLocalPrefix: предок без package.json, без `workspaces` или не
    включающий пропускается); uv — первый предок с pyproject.toml, если его `[tool.uv.workspace]` включает каталог
    rel, иначе не член: проект внутри другого проекта uv членом не считает (uv-workspace find_workspace; проверено
    `uv lock --offline`, uv 0.13.0). Включает — последний подходящий шаблон без `!`. Не прочитанный pyproject.toml —
    как нет."""
    rel = rel.replace(os.sep, "/")
    name = posixpath.basename(rel)
    if os.path.isabs(rel) or name not in _WORKSPACE_ROOTS.values() or watched_kind(rel) is None:
        return None
    directory = posixpath.dirname(rel)
    for ancestor in _ancestors(rel):
        text = text_of(posixpath.join(ancestor, name))
        if text is None:
            continue
        member = False
        for pattern, exclude in _globs(name, text):
            if pattern.match(directory[len(ancestor) + 1:] if ancestor else directory):
                member = not exclude
        if member:
            return ancestor
        if name == "pyproject.toml":
            return None
    return None


def _npm_place(rel, text_of):
    """(место, каталог корня) package.json rel в npm workspace: ("member", каталог корня) — член (_workspace_root),
    ("root", свой каталог) — не член, а у него `workspaces`, (None, None) — прочий. Член со своими `workspaces` —
    член: npm из его каталога поднимается к предку, чей `workspaces` включает каталог (@npmcli/config
    loadLocalPrefix), и `workspaces` члена не читает."""
    rel = rel.replace(os.sep, "/")
    if os.path.isabs(rel) or watched_kind(rel) != "package.json":
        return None, None
    root = _workspace_root(rel, text_of)
    if root is not None:
        return "member", root
    if _globs("package.json", text_of(rel)):
        return "root", posixpath.dirname(rel)
    return None, None


class _Npm:
    """npm workspace проекта или версии git для разбора package.json: text_of — тексты package.json по пути от корня
    (_workspace_root), members — {каталог корня workspace: {имя члена: версия или None}} (_npm_members) и имена
    бывших членов former (до правки или команды): зависимость на бывшего члена npm берёт из реестра."""
    __slots__ = ("text_of", "members", "former")

    def __init__(self, text_of, members, former=()):
        self.text_of, self.members, self.former = text_of, members, frozenset(former)

    def names(self):
        """Имена членов всех корней."""
        return frozenset().union(*self.members.values())

    def of(self, kind, rel):
        """manifests.NpmWorkspace манифеста rel вида kind: члены — своего корня (_npm_place), следимые имена — члены
        всех корней и бывшие; None — не package.json или нет ни членов, ни бывших."""
        watched = self.former | self.names()
        if kind != "package.json" or not watched:
            return None
        place, root = _npm_place(rel, self.text_of)
        return manifests.NpmWorkspace(self.members.get(root, {}), place, watched)


def _npm_member(rel, text_of):
    """Имя пакета манифеста rel — пакет проекта: rel — член npm workspace (_workspace_root). Связь с каталогом члена
    или реестр различает разбор package.json (manifests.NpmWorkspace). Имя члена uv пакетом проекта не считается:
    uv ставит его из каталога только по источнику `workspace = true` (_uv_inherited), pip — из PyPI."""
    return watched_kind(rel) == "package.json" and _workspace_root(rel, text_of) is not None


def _uv_inherited(rel, text, text_of):
    """Имена с местным источником (manifests.uv_sources) `[tool.uv.sources]` корня uv workspace, которые uv ставит из
    каталога для члена rel (_workspace_root; text_of — как там) с текстом text: источник корня действует на члена,
    если член не задал источник этого имени сам (docs.astral.sh/uv, «Workspaces»: «Any tool.uv.sources definitions in
    the workspace root apply to all members, unless overridden»). Не член — пусто; свои источники манифеста разбирает
    manifests.names."""
    if watched_kind(rel) != "pyproject":
        return frozenset()
    root = _workspace_root(rel, text_of)
    if root is None:
        return frozenset()
    own = manifests.uv_sources(text)
    return frozenset(name for name, local in manifests.uv_sources(text_of(posixpath.join(root, "pyproject.toml")))
                     .items() if local and name not in own)


def _npm_members(rels, text_of):
    """{каталог корня: {имя: версия или None}} членов npm workspace (_workspace_root) среди манифестов rels;
    text_of(путь) — текст или None. Имя члена без `name` — имя каталога, под каталогом `@scope` — `@scope/каталог`
    (@npmcli/map-workspaces getPackageName, @npmcli/name-from-folder); не разобранный член пропускается."""
    members = {}
    for rel in rels:
        root = _workspace_root(rel, text_of) if watched_kind(rel) == "package.json" else None
        text = None if root is None else text_of(rel)
        if text is None or manifests._json_object(text.removeprefix("\ufeff")) is None:
            continue
        name = manifests.own_name("package.json", text)
        if name is None:
            directory = posixpath.dirname(rel.replace(os.sep, "/"))
            scope, name = posixpath.basename(posixpath.dirname(directory)), posixpath.basename(directory)
            name = f"{scope}/{name}" if scope.startswith("@") else name
        members.setdefault(root, {})[name] = manifests.npm_version(text)
    return members


def _tree_npm(texts):
    """_Npm версии git по {путь package.json: текст} её дерева: корни workspace и члены — из той же версии; бывших
    членов нет."""
    return _Npm(texts.get, _npm_members(list(texts), texts.get))


def project_names(root, kind, deadline, listed=None):
    """Имена манифестов реестра вида kind (manifests.registry) под root и, в git, манифестов и файлов _LEGACY
    версии HEAD (_tree_names) с транзитивными и имена пакетов самого проекта этого реестра: манифестов версии HEAD
    (кроме npm и PyPI) и членов npm workspace (_npm_member). Имя свежего манифеста вне workspace — не пакет
    проекта: его пишет кто угодно, а проверяется только объявление зависимости. Файлы _LEGACY рабочего дерева имён
    не дают. Не разобранный и больший MAX_MANIFEST_BYTES манифест не даёт ничего. listed — перечень правки (listing)
    корня root: манифесты и режим — из него, дерево HEAD — из его listed.versions(), общего с head_names; None — свой
    list_manifests в срок deadline и своё дерево. Unavailable и TimeoutError — как у list_manifests и _tree_names."""
    if listed is None:
        (mode, found), versions = list_manifests(root, deadline), None
    else:
        found, mode, versions = listed()[1], listed.mode, listed.versions()
    registry = manifests.registry(kind)
    text_of = _disk(root)
    npm = _Npm(text_of, _npm_members(found, text_of) if registry == "npm" else {})
    out = set()
    for rel in found:
        _remaining(deadline)
        other = watched_kind(rel)
        if manifests.registry(other) != registry:
            continue
        known, own = _parse(os.path.join(root, rel), other, npm.of(other, rel))
        out.update(_rooted(known, rel) or ())
        if _npm_member(rel, text_of):
            out.add(own)
    out.discard(None)
    if mode == "git":
        out.update((_tree_names(root, "HEAD", deadline, versions) or {}).get(registry, ()))
    return frozenset(out)


def check_edit(tool, tool_input, path, kind, project=frozenset, root=lambda: None, listed=None):
    """Новые имена правки манифеста path без подключений проверяемых манифестов (_unchecked) и с именами других
    package.json, которые правка членов npm workspace переводит со связи на реестр (_edit_workspaces); project — как
    у fresh_names; root() — корень проекта для head_names и перечня или None, зовётся только для версий git,
    подключений и npm workspace; listed — перечень listing(root), его versions() читают версии git head_names, None —
    свой. None — правку не вычислить.
    Unavailable — не проверить: не разобран или манифесты проекта не перечислить; TimeoutError — перечень манифестов
    для подключения или npm workspace не уложился в HEAD_TIMEOUT."""
    texts = edit_texts(tool, tool_input, path, kind)
    if texts is None:
        return None
    before, after = texts
    if kind == "requirements":
        # Файл пишется в UTF-8, а pip декодирует его по BOM и объявлению PEP 263 (manifests.decode).
        after = manifests.decode(kind, after.encode("utf-8", "surrogatepass"))
    rel = _project_rel(path, root())
    listed = listed or listing(root)
    workspaces, effects = _edit_workspaces(kind, rel, before, after, root(), listed)

    def own_project():
        names = project()
        if kind == "pyproject" and root() is not None:
            names = names | _uv_inherited(rel, after, _disk(root()))
        return names
    names = edit_names(kind, before, after,
                       lambda: _rooted(head_names(path, kind, HEAD_TIMEOUT, root(), listed.versions()), rel),
                       own_project, rel, workspaces)
    if effects:
        effects -= project()
    return _unchecked(sorted(set(names) | effects), listed, kind)


def _edit_workspaces(kind, rel, before, after, root, listed):
    """((manifests.NpmWorkspace до правки или None, после или None), {новые имена других package.json проекта})
    правки package.json rel: корни и члены npm workspace — по файлам проекта listed() (listing), у правленого файла —
    по тексту before и after. Правка, которая меняет членов (имя, версия) или место package.json в workspace
    (шаблоны, `workspaces`), переводит зависимость другого package.json на члена со связи с каталогом на реестр: такие
    имена — второй элемент. Членов нет ни до, ни после — (None, None) и пусто. Перечень не получен (Unavailable) —
    то же, если правленый файл ни до, ни после не корень и не член по предкам на диске: его имена без workspace
    сверяет project() с тем же пределом list_manifests; иначе Unavailable."""
    if kind != "package.json" or root is None or os.path.isabs(rel):
        return (None, None), set()
    rel = rel.replace(os.sep, "/")
    sides = (_disk(root, {rel: before}), _disk(root, {rel: after}))
    try:
        found = listed()
    except Unavailable:
        if any(_npm_place(rel, text_of)[0] for text_of in sides):
            raise
        return (None, None), set()
    if found is None:
        return (None, None), set()
    rels = [r for r in found[1] if r != rel and watched_kind(r) == "package.json"] + [rel]
    npm = _Npm(sides[0], _npm_members(rels, sides[0]))
    npm_after = _Npm(sides[1], _npm_members(rels, sides[1]), npm.names())
    effects = set()
    if (npm.members, [_npm_place(r, sides[0]) for r in rels]) != (npm_after.members,
                                                                  [_npm_place(r, sides[1]) for r in rels]):
        for r in rels[:-1]:
            text = sides[0](r)
            if text is None:
                continue
            old = manifests.names("package.json", text, npm.of("package.json", r))
            new = manifests.names("package.json", text, npm_after.of("package.json", r))
            if old is not None and new is not None:
                effects |= new - old
    return (npm.of(kind, rel), npm_after.of(kind, rel)), effects


# Вид, которым менеджер читает подключённый файл под любым именем: pip — `-r` и `-c`, setuptools и
# hatch-requirements-txt — файлы `dynamic` pyproject (manifests._dynamic_files) как требования, Bundler —
# `eval_gemfile` как Gemfile.
_INCLUDE_KINDS = {"requirements": "requirements", "pyproject": "requirements", "gemfile": "gemfile"}


def _unchecked(names, listed, kind):
    """names манифеста вида kind без подключений manifests.Include файлов, которые проверяются сами: путь без схемы
    URL от корня проекта (_rooted), с разрешёнными ссылками, — манифест из перечня проекта listed() = (корень, [путь
    от корня]) (list_manifests) того вида, которым подключённый файл читает менеджер (_INCLUDE_KINDS); None —
    проект неизвестен, подключения остаются. Манифест другого вида (`-r Gemfile`) проверяется по своему виду, а
    ставится по виду подключающего: его подключение — новое имя. Файл, исключённый git, в перечень не входит и не
    проверяется: его подключение — новое имя. listed() зовётся, только если подключение есть."""
    if not any(isinstance(name, manifests.Include) for name in names):
        return names
    found = listed()
    if found is None:
        return names
    base, rels = found
    read_as = _INCLUDE_KINDS.get(kind)
    checked = {(os.path.realpath(os.path.join(base, rel)), watched_kind(rel)) for rel in rels}
    return [name for name in names if not (
        isinstance(name, manifests.Include) and not _SCHEME.match(name.path)
        and (os.path.realpath(os.path.join(base, name.path)), read_as) in checked)]


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
    в обоих режимах без каталогов FOREIGN_DIRS. Unavailable — манифестов больше MAX_MANIFESTS или вне git файлов
    больше MAX_WALK_FILES (_walk); TimeoutError — срок."""
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
    """[size, mtime_ns, ctime_ns] обычного файла; None — нет файла или не обычный файл. ctime ставит ядро при
    любой записи: размер и mtime возвращает `touch -r`, ctime — нет."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns] if stat.S_ISREG(st.st_mode) else None


def _text(path, kind=None):
    """Текст манифеста вида kind (_read); None — нет, не прочитан или больше MAX_MANIFEST_BYTES."""
    try:
        return _read(path, kind)
    except Unavailable:
        return None


def _parse(path, kind, workspace=None):
    """(имена с транзитивными или None, имя пакета самого манифеста или None) манифеста; workspace — как у _known."""
    text = _text(path, kind)
    if text is None:
        return None, None
    return _known(kind, text, workspace), manifests.own_name(kind, text)


def take(root, deadline):
    """Снимок: {"root", "mode", "ts", "files": {путь: [size, mtime_ns, ctime_ns, имена с транзитивными (_dump_names)
    или None, имя пакета манифеста или None]}, "npm": {каталог корня npm workspace: {имя члена: версия или None}}}
    (_npm_members); package.json разбирается с этими членами (_Npm). Поле "known" (restored_names) добавляет
    вызывающий. Файлы _LEGACY рабочего дерева в снимок не входят."""
    mode, found = list_manifests(root, deadline)
    text_of = _disk(root)
    npm = _Npm(text_of, _npm_members(found, text_of))
    entry = {"root": str(root), "mode": mode, "ts": time.time(), "files": {}, "npm": npm.members}
    for rel in found:
        _remaining(deadline)
        path = os.path.join(root, rel)
        st = _stat(path)
        if st is None:
            continue
        kind = watched_kind(rel)
        known, own = _parse(path, kind, npm.of(kind, rel))
        entry["files"][rel] = [*st, None if known is None else _dump_names(_rooted(known, rel)), own]
    return entry


def _dump_names(names):
    """Отсортированные имена для состояния JSON: manifests.Include — {"include": путь}, прочие — строки как есть."""
    return [{"include": name.path} if isinstance(name, manifests.Include) else name for name in sorted(names)]


def _load_names(items):
    """Имена из состояния JSON (_dump_names)."""
    return frozenset(manifests.Include(item["include"]) if isinstance(item, dict) else item for item in items)


def _valid_names(items):
    """items — список имён формы _dump_names."""
    return isinstance(items, list) and all(
        isinstance(item, str) or isinstance(item, dict) and list(item) == ["include"]
        and isinstance(item["include"], str) for item in items)


def compare(entry, deadline):
    """({путь: новые имена}, [пути, которые не проверить], причина, почему новые манифесты не проверены, или None)
    после команды против снимка entry.

    Новый манифест и манифест, не разобранный в снимке и без версии git, сравниваются с пустым (fresh_names);
    пропавший — ничего; манифест с тем же size, mtime и ctime не перечитывается; не разобранный после команды — в
    списке непроверенных. Имя из любого манифеста того же реестра
    (manifests.registry) в снимке, из ref до начала сессии (entry["known"], restored_names, с именами пакетов их
    манифестов), в git — из манифестов и файлов _LEGACY версии HEAD и имена пакетов её манифестов (_tree_names,
    читается, только если после old и версий файла что-то осталось; без имён пакетов npm и PyPI), имя пакета члена
    npm workspace после команды (_npm_member; в снимке или в манифесте после команды) и, в члене uv workspace,
    имя с местным источником корня (_uv_inherited) — не новое: перенос, копия, член workspace, возврат работы
    автора. Подключение манифеста из перечня после команды — не новое имя (_unchecked). Версии git изменённых
    package.json разбираются с одним _VersionNpm на сравнение. package.json
    разбирается с членами npm workspace после команды (_Npm); члены изменились против снимка (entry["npm"]) — каждый
    package.json перечитывается и с тем же size, mtime и ctime: зависимость на члена могла уйти со связи на реестр.

    Режим сменился за команду (`git init`) или манифестов теперь не перечислить (больше MAX_MANIFESTS, вне git
    файлов больше MAX_WALK_FILES) — сверяются только пути снимка, третий элемент — причина: новые манифесты не
    проверены."""
    root = entry["root"]
    before = entry["files"]
    try:
        mode, found = list_manifests(root, deadline)
        lost = None if mode == entry["mode"] else ("за команду корень проекта " + (
            "стал git-репозиторием" if mode == "git" else "перестал быть git-репозиторием"))
    except Unavailable as e:
        mode, found, lost = entry["mode"], [], str(e)
    if lost is not None:
        found = list(before)
    text_of = _disk(root)
    npm = _Npm(text_of, _npm_members(found, text_of), frozenset().union(*entry["npm"].values()))
    members_changed = npm.members != entry["npm"]
    versions = _VersionNpm(root)
    project = {registry: set(_load_names(names)) for registry, names in entry.get("known", {}).items()}
    for rel, (*_, known, own) in before.items():
        names = project.setdefault(manifests.registry(watched_kind(rel)), set())
        names.update(_load_names(known or ()))
        if own is not None and _npm_member(rel, text_of):
            names.add(own)
    changed, unknown = [], []
    for rel in found:
        _remaining(deadline)
        path = os.path.join(root, rel)
        st = _stat(path)
        was = before.get(rel)
        kind = watched_kind(rel)
        if st is None or isinstance(was, list) and was[:3] == st and not (members_changed and kind == "package.json"):
            continue
        text = text_of(rel)
        new = None if text is None else _rooted(manifests.names(kind, text, npm.of(kind, rel)), rel)
        if new is None:
            unknown.append(rel)
            continue
        own = manifests.own_name(kind, text) if _npm_member(rel, text_of) else None
        project.setdefault(manifests.registry(kind), set()).update(() if own is None else (own,))
        changed.append((rel, path, kind, new, was, _uv_inherited(rel, text, text_of)))
    head_tree = []

    def project_of(kind, inherited):
        if mode == "git" and not head_tree:
            head_tree.append(_tree_names(root, "HEAD", deadline, versions) or {})
        registry = manifests.registry(kind)
        return frozenset(project.get(registry, ())).union(head_tree[0].get(registry, ()) if head_tree else (),
                                                          inherited)
    added = {}
    for rel, path, kind, new, was, inherited in changed:
        if was is None:
            old = frozenset()
        else:
            old = None if was[3] is None else _load_names(was[3])

        def head(path=path, kind=kind, rel=rel):
            return _rooted(head_names(path, kind, _remaining(deadline), root, versions), rel) if mode == "git" else None
        names = _unchecked(fresh_names(old, new, head,
                                       lambda kind=kind, inherited=inherited: project_of(kind, inherited)),
                           lambda: (root, found), kind)
        if names:
            added[rel] = names
    return added, unknown, lost


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
            i += 2 if pkgmanagers._takes_value(a, value_flags) else 1
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
        if not words or pkgmanagers._basename(words[0]) != "git":
            continue
        sub = pkgmanagers._subcommand(words, _GIT_VALUE_FLAGS)
        if len(sub) < 2:
            continue
        name, args = sub[1], sub[2:]
        if name == "stash" and args[:1] in (["pop"], ["apply"]):
            operands, _ = _operands(args[1:], pkgmanagers._NO_VALUE_FLAGS)
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


def _tree_names(root, tree, deadline, versions=None):
    """{реестр: имена с транзитивными и имена пакетов манифестов (кроме npm и PyPI, _WORKSPACE_ROOTS)} манифестов и
    файлов _LEGACY (source_kind) дерева tree — ref или SHA дерева; None — git не прочитал дерево (нет HEAD, не
    репозиторий) или его блобы. package.json разбирается с npm workspace того же дерева (_tree_npm). Пути с переводом
    строки или возвратом каретки пропускаются. Дерево и блобы читает versions — _VersionNpm корня root, общий с
    head_names правки или сравнения; None — свой. Unavailable — манифестов в дереве больше MAX_MANIFESTS;
    TimeoutError — срок deadline."""
    if versions is None:
        versions = _VersionNpm(root)
    entries = versions.entries(tree, deadline)
    if entries is None:
        return None
    paths = [p for p in entries if source_kind(p) and "\n" not in p and "\r" not in p]
    if len(paths) > MAX_MANIFESTS:
        raise Unavailable(f"манифестов в {tree} больше {MAX_MANIFESTS}")
    known = {}
    if not paths:
        return known
    read = versions.read((entries[p] for p in paths), deadline)
    if read is None:
        return None
    blobs = [(path, read[entries[path]]) for path in paths if read[entries[path]] is not None]
    npm = _tree_npm({path: manifests.decode("package.json", data) for path, data in blobs
                     if source_kind(path) == "package.json"})
    for path, data in blobs:
        kind = source_kind(path)
        text = manifests.decode(kind, data)
        names = set(_rooted(_known(kind, text, npm.of(kind, path)), path) or ())
        if kind not in _LEGACY and manifests.registry(kind) not in _WORKSPACE_ROOTS:
            names.add(manifests.own_name(kind, text))
        names.discard(None)
        if names:
            known.setdefault(_registry(kind), set()).update(names)
    return known


_APPLY_VALUE_FLAGS = {"--exclude", "--include", "-p", "-C", "--whitespace", "--directory", "--build-fake-ancestor"}


def command_patches(command):
    """Файлы патчей `git apply` по сегментам: операнды и, если их нет или операнд `-`, цель перенаправления
    `<`. Heredoc, конвейер (`cat p | git apply`) и `git -C` не разбираются: у патча из stdin файла нет."""
    out = []
    for segment in depcheck._segments(command):
        words, _ = depcheck._command(segment.strip())
        if not words or pkgmanagers._basename(words[0]) != "git":
            continue
        sub = pkgmanagers._subcommand(words, _GIT_VALUE_FLAGS)
        if sub[1:2] != ["apply"]:
            continue
        args = sub[2:]
        operands, dashdash = _operands(args, _APPLY_VALUE_FLAGS)
        if dashdash:
            operands += args[args.index("--") + 1:]
        files = [o for o in operands if o != "-"]
        if len(files) < len(operands) or not operands:
            pairs = depcheck._split(segment)
            for i, (word, lead) in enumerate(pairs):
                m = depcheck._REDIRECT.match(lead)
                if m and m.group(0).lstrip("0123456789") == "<" and m.group(0)[:-1] in ("", "0"):
                    target = word[m.end():] or (pairs[i + 1][0] if i + 1 < len(pairs) else "")
                    if target:
                        files.append(target)
        out += files
    return out


_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_TOKEN = re.compile(r"[A-Za-z0-9@_][A-Za-z0-9._/@~-]*")
# Нормализация имён реестра, как в manifests.names: имя из патча сравнивается с именами манифеста после команды.
_NORMALIZE = {"pypi": manifests._pep503, "packagist": str.lower, "crates.io": manifests._crate}


def _patch_names(text):
    """{реестр: слова} изменённых строк (`+` и `-`) блоков патча, чей файл — манифест или файл _LEGACY
    (source_kind по путям заголовков `---`/`+++`). Слова — надмножество имён: строка `"left-pad": "^1"` даёт и
    `left-pad`, и `1`; лишнее слово только пропускает имя, которое и так есть в патче. Строки блока считаются по
    числам заголовка `@@`: строка `--- x` внутри блока — содержимое, не заголовок файла."""
    known, kind, old, new = {}, None, 0, 0
    for line in text.splitlines():
        if old > 0 or new > 0:
            tag = line[:1]
            if tag in (" ", ""):
                old, new = old - 1, new - 1
            elif tag in ("-", "+"):
                old, new = (old - 1, new) if tag == "-" else (old, new - 1)
                if kind is not None:
                    registry = _registry(kind)
                    normalize = _NORMALIZE.get(registry, str)
                    known.setdefault(registry, set()).update(
                        normalize(t.rstrip("._-/")) for t in _TOKEN.findall(line[1:]))
            if tag in (" ", "", "-", "+", "\\"):
                continue
            # Блок оборвался раньше чисел заголовка: строка — заголовок.
            old = new = 0
        if line.startswith("diff "):
            kind = None
        elif line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t")[0].strip().strip('"')
            if path != "/dev/null":
                kind = source_kind(path)
        else:
            m = _HUNK.match(line)
            if m:
                old, new = int(m.group(1) or 1), int(m.group(2) or 1)
    return known


def _old_patch_names(patches, start, base, deadline):
    """{реестр: слова} патчей (_patch_names), чей файл не менялся с start: ctime файла меньше start. ctime
    ставит ядро при любой записи и смене атрибутов, `touch` назад его не ставит. ctime сверяется после чтения:
    правка во время чтения его сдвигает. Не обычный файл (FIFO не открывается в
    блокирующем режиме), больше MAX_MANIFEST_BYTES, не прочитан — пропуск."""
    known = {}
    for patch in patches:
        _remaining(deadline)
        try:
            fd = os.open(os.path.join(base, os.path.expanduser(patch)), os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_MANIFEST_BYTES:
                continue
            data = b""
            while chunk := os.read(fd, MAX_MANIFEST_BYTES + 1 - len(data)):
                data += chunk
                if len(data) > MAX_MANIFEST_BYTES:
                    break
            if os.fstat(fd).st_ctime >= start or len(data) > MAX_MANIFEST_BYTES:
                continue
        except OSError:
            continue
        finally:
            os.close(fd)
        for registry, names in _patch_names(data.decode("utf-8", "replace")).items():
            known.setdefault(registry, set()).update(names)
    return known


def restored_names(root, command, start, deadline, cwd=None):
    """{реестр: имена с транзитивными и имена пакетов (_tree_names, _dump_names)} манифестов и файлов _LEGACY ref,
    откуда command возвращает файлы (command_refs), если ref создан до start — начала сессии (session_start), и
    слова изменённых строк манифестов в файлах патчей `git apply` (command_patches), не менявшихся с start;
    относительный путь патча — от cwd, без него — от root. Это работа до сессии, после команды такие имена не
    новые. start None, ref моложе или не найден, патч изменён в сессии — имена не добавляются: сравнение блокирует,
    как без ref.
    Unavailable — имена ref не прочитаны: срок deadline вышел, манифестов в дереве больше MAX_MANIFESTS или git
    не прочитал дерево.

    Время ref — время коммиттера: коммит с поддельной датой (GIT_COMMITTER_DATE) проходит как старый."""
    refs = command_refs(command)
    patches = command_patches(command)
    if not refs and not patches or start is None:
        return {}
    known = {}
    try:
        known = _old_patch_names(patches, start, root if cwd is None else cwd, deadline)
        for tree in _old_trees(root, refs, start, deadline):
            names = _tree_names(root, tree, deadline)
            if names is None:
                raise Unavailable(f"дерево ref {tree} не прочитано")
            for registry, found in names.items():
                known.setdefault(registry, set()).update(found)
    except TimeoutError:
        raise Unavailable("имена ref команды не прочитаны за срок") from None
    return {registry: _dump_names(names) for registry, names in known.items()}


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
             and all(isinstance(v, list) and len(v) == 5 and (v[3] is None or _valid_names(v[3]))
                     and (v[4] is None or isinstance(v[4], str)) for v in entry["files"].values())
             and isinstance(entry.get("known", {}), dict)
             and all(_valid_names(v) for v in entry.get("known", {}).values())
             and isinstance(entry.get("npm"), dict)
             and all(isinstance(members, dict) and all(v is None or isinstance(v, str) for v in members.values())
                     for members in entry["npm"].values()))
    return entry if valid else None


def _start_path(state_dir, session):
    return state_dir / f"{common.safe_name(session)}.start.json"


def mark_start(session):
    """Начало сессии — целые секунды вниз — в state/<session>.start.json при первом вызове; дальше содержимое не
    меняется, а mtime обновляется на каждом вызове: common.prune_state удаляет файлы старше STATE_TTL по mtime,
    начало сессии, где judge_tool звался в пределах STATE_TTL, он не удаляет. Зовётся на каждом PreToolUse
    judge_tool: до первого вызова агент не запускал ни Bash, ни правок."""
    state_dir = common.data_dir() / "state"
    path = _start_path(state_dir, session)
    try:
        os.utime(path)
        return
    except FileNotFoundError:
        pass
    state_dir.mkdir(exist_ok=True)
    with common.state_lock(state_dir):
        if not path.exists():
            common.atomic_write_json(path, {"start": int(time.time())})


def session_start(session):
    """Начало сессии из mark_start, секунды; None — нет записи или она не той формы."""
    start = common.read_json(_start_path(common.data_dir() / "state", session), dict).get("start")
    return start if type(start) is int else None
