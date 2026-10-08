"""Новые имена зависимостей в манифестах: правка файловым инструментом и команда Bash.

Правка — текст манифеста до и после по входу Write, Edit, MultiEdit. Команда — снимок имён всех
манифестов проекта перед ней (state/<session>.manifests.json по tool_use_id) и сравнение после.
Новым не считается имя из текста до правки вместе с транзитивными, из версии манифеста в HEAD и в
источнике незавершённых merge, cherry-pick, rebase, revert, из другого манифеста того же реестра в
проекте и имя пакета самого проекта (workspace, монорепозиторий).
"""
import os
import re
import stat
import subprocess
import time

import common
import depcheck
import manifests
import snapshot

# Срок снимка манифестов перед командой Bash и сравнения после неё, секунды от старта хука; без
# common.GIT_ROOT_TIMEOUT корня проекта (tests/test_contract.py, TimeoutsTest).
SNAPSHOT_BUDGET = 5
CHECK_BUDGET = 5
# Срок `git cat-file` версий из _REPO_REFS при правке файловым инструментом, секунды.
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


def _git(cwd, timeout, *args, input=None):
    """stdout `git -C cwd <args>` с stdin input байтами; None при коде не 0 или сбое; TimeoutError по сроку."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("git не уложился в срок") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


# Версии, чьи имена уже в репозитории: HEAD и источник незавершённых merge (и pull), cherry-pick, rebase и
# revert — при конфликте они не закоммичены, а файл уже с их правкой; revert возвращает версию до
# отменяемого коммита (REVERT_HEAD^).
_REPO_REFS = ("HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD", "REVERT_HEAD^")


def _blobs(out):
    """Содержимое блобов вывода `git cat-file --batch`; отсутствующие и не блобы пропущены."""
    blobs, i = [], 0
    while i < len(out):
        end = out.find(b"\n", i)
        if end < 0:
            break
        header = out[i:end].split()
        i = end + 1
        if len(header) == 3 and header[2].isdigit():
            size = int(header[2])
            if header[1] == b"blob":
                blobs.append(out[i:i + size])
            i += size + 1
    return blobs


def head_names(path, kind, timeout):
    """Имена манифеста path вместе с транзитивными (manifests.known_names) в версиях _REPO_REFS его
    репозитория, объединённые; None — нет репозитория, файла ни в одной версии или ни одна не разобрана.
    `cat-file --batch` отдаёт байты блобов без textconv и фильтров одним вызовом git."""
    name = os.path.basename(path)
    cwd = os.path.dirname(path) or "."
    if "\n" in name or "\r" in name:
        # Ввод --batch построчный: имя с переводом строки — только версия HEAD отдельным вызовом.
        out = _git(cwd, timeout, "cat-file", "blob", f"HEAD:./{name}")
        blobs = [] if out is None else [out]
    else:
        out = _git(cwd, timeout, "cat-file", "--batch",
                   input=os.fsencode("".join(f"{ref}:./{name}\n" for ref in _REPO_REFS)))
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
    """Имена манифестов реестра вида kind (manifests.registry) под root с транзитивными и имена пакетов
    самого проекта этого реестра.
    Не разобранный и больший MAX_MANIFEST_BYTES манифест не даёт ничего. Unavailable и TimeoutError —
    как у list_manifests."""
    _, found = list_manifests(root, deadline)
    out = set()
    for rel in found:
        _remaining(deadline)
        other = watched_kind(rel)
        if manifests.registry(other) != manifests.registry(kind):
            continue
        known, own = _parse(os.path.join(root, rel), other)
        out.update(known or ())
        out.add(own)
    out.discard(None)
    return frozenset(out)


def check_edit(tool, tool_input, path, kind, project=frozenset):
    """Новые имена правки манифеста path; project — как у fresh_names. None — правку не вычислить.
    Unavailable — не проверить: не разобран или манифесты проекта не перечислить."""
    texts = edit_texts(tool, tool_input, path)
    if texts is None:
        return None
    before, after = texts
    return edit_names(kind, before, after, lambda: head_names(path, kind, HEAD_TIMEOUT), project)


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("не уложился в срок")
    return left


def _walk(root, deadline):
    """Манифесты под root обходом каталогов без FOREIGN_DIRS; Unavailable — файлов больше MAX_WALK_FILES."""
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
    """("git" или "walk", [путь манифеста от root]). В git — отслеживаемые и неотслеживаемые не
    исключённые файлы (`ls-files -co --exclude-standard`), вне git — обход; в обоих режимах без каталогов
    FOREIGN_DIRS (watched_kind). Unavailable — манифестов больше MAX_MANIFESTS; TimeoutError — срок."""
    out = _git(root, _remaining(deadline), "ls-files", "-z", "-c", "-o", "--exclude-standard")
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
    """(имена с транзитивными или None, имя пакета самого манифеста или None)."""
    text = _text(path)
    if text is None:
        return None, None
    return manifests.known_names(kind, text), manifests.own_name(kind, text)


def take(root, deadline):
    """Снимок: {"root", "mode", "ts", "files": {путь: [size, mtime_ns, имена с транзитивными или None,
    имя пакета манифеста или None]}}."""
    mode, found = list_manifests(root, deadline)
    files = {}
    for rel in found:
        _remaining(deadline)
        path = os.path.join(root, rel)
        st = _stat(path)
        if st is None:
            continue
        known, own = _parse(path, watched_kind(rel))
        files[rel] = [*st, None if known is None else sorted(known), own]
    return {"root": str(root), "mode": mode, "ts": time.time(), "files": files}


def compare(entry, deadline, skip=frozenset()):
    """({путь: новые имена}, [пути, которые не проверить]) после команды против снимка entry.

    Новый манифест сравнивается с пустым; пропавший — ничего; манифест с тем же size и mtime не
    перечитывается; манифест, чей realpath в skip, не проверяется. Имя из любого манифеста того же реестра
    (manifests.registry) в снимке и имя пакета самого проекта (в снимке или в манифесте после команды) — не
    новое: перенос, копия, член workspace. Unavailable — снимок другого режима."""
    root = entry["root"]
    mode, found = list_manifests(root, deadline)
    if mode != entry["mode"]:
        raise Unavailable("за команду корень проекта " +
                          ("стал git-репозиторием" if mode == "git" else "перестал быть git-репозиторием"))
    before = entry["files"]
    project = {}
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
    added = {}
    for rel, path, kind, new, was in changed:
        if was is None:
            old = frozenset()
        else:
            old = None if was[2] is None else frozenset(was[2])

        def head(path=path, kind=kind):
            return head_names(path, kind, _remaining(deadline)) if mode == "git" else None
        try:
            names = fresh_names(old, new, head, lambda kind=kind: frozenset(project[manifests.registry(kind)]))
        except Unavailable:
            unknown.append(rel)
            continue
        if names:
            added[rel] = names
    return added, unknown


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
                     and (v[3] is None or isinstance(v[3], str)) for v in entry["files"].values()))
    return entry if valid else None
