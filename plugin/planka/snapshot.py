"""Снимок дерева проекта: какие файлы и с каким размером и mtime были на реплике."""
import json
import os
import pathlib
import stat
import subprocess
import time

import common

# Каталоги, которые обход вне git пропускает всегда.
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build",
                          "dist", ".dart_tool", ".data", ".idea", ".vscode"})
# Предел путей снимка: вне git — всех файлов, в git — изменённых и неотслеживаемых.
MAX_FILES = 50_000
# Срок проверяется раз в столько файлов при lstat.
STAT_CHECK_EVERY = 256


class TooManyFiles(Exception):
    """Путей для снимка больше MAX_FILES; текст — причина для предупреждения."""


def ignore_rules(directory):
    """Записи .gitignore каталога: имена без подстановочных знаков и расширения из «*.ext».

    Ведущий «/» не привязывает имя к каталогу: имя совпадает на любой глубине под каталогом файла.
    """
    names, exts = set(), set()
    try:
        lines = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
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


GIT_TIMEOUT = 10


def _remaining(deadline):
    """Секунды до срока deadline (time.monotonic), не больше GIT_TIMEOUT; TimeoutError, если срок прошёл."""
    if deadline is None:
        return GIT_TIMEOUT
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("снимок не уложился в срок")
    return min(GIT_TIMEOUT, left)


def _git(root, deadline, *args):
    """stdout `git -C root <args>` байтами; None вне git или при сбое; TimeoutError по сроку deadline."""
    # TimeoutError — подкласс OSError; срок проверяется вне try.
    timeout = _remaining(deadline)
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("git не уложился в срок снимка") from None
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_ls(root, deadline, *args):
    """Записи `git ls-files -z <args>` в root; None вне git или при сбое."""
    out = _git(root, deadline, "ls-files", "-z", *args)
    return None if out is None else [os.fsdecode(e) for e in out.split(b"\0") if e]


def _walk_paths(root, deadline):
    """Пути файлов под root обходом каталогов; TooManyFiles, когда путей больше MAX_FILES: оборванный
    обход не отдаётся как полный.

    Правила каталога — правила родителя плюс его собственный .gitignore, упрощённо по ignore_rules;
    действуют на его поддерево. os.walk не заходит в символические ссылки на каталоги.
    """
    rules_by_dir = {}
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        _remaining(deadline)
        parent = rules_by_dir.get(os.path.dirname(dirpath), (set(), set()))
        own = ignore_rules(pathlib.Path(dirpath))
        names, exts = parent[0] | own[0], parent[1] | own[1]
        rules_by_dir[dirpath] = (names, exts)
        skip = IGNORED_DIRS | names
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if name in names or any(name.endswith("." + e) for e in exts):
                continue
            paths.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
        if len(paths) > MAX_FILES:
            raise TooManyFiles(f"дерево больше {MAX_FILES} файлов")
    return paths


def _stat_files(root, relpaths, deadline=None):
    """{путь: [size, mtime_ns]} для обычных файлов (не ссылок) из relpaths; None, если их больше MAX_FILES;
    TimeoutError по сроку deadline."""
    files = {}
    for i, rel in enumerate(relpaths):
        if i % STAT_CHECK_EVERY == 0:
            _remaining(deadline)
        try:
            st = os.lstat(os.path.join(root, rel))
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        files[rel] = [st.st_size, st.st_mtime_ns]
        if len(files) > MAX_FILES:
            return None
    return files


def _stat_one(root, rel):
    """[size, mtime_ns] обычного файла (не ссылки) root/rel; None — нет файла или не обычный файл."""
    try:
        st = os.lstat(os.path.join(root, rel))
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns] if stat.S_ISREG(st.st_mode) else None


# Число полей до пути в записях `git status --porcelain=v2`: обычная, переименование, конфликт.
_V2_FIELDS = {b"1": 8, b"2": 9, b"u": 10}


def _status(root, deadline):
    """(HEAD или None без коммитов, пути с изменениями и неотслеживаемые, каталоги вложенных репозиториев)
    по `git status` в root; None вне git.

    Пути — от root. Подмодули пропускаются: их обходит _git_state. Переименование даёт оба пути.
    Неотслеживаемый каталог при --untracked-files=all git отдаёт одной записью «dir/» только для
    вложенного репозитория.
    """
    out = _git(root, deadline, "--no-optional-locks", "status", "--porcelain=v2", "-z", "--branch",
               "--untracked-files=all", "--ignore-submodules=all")
    if out is None:
        return None
    head, paths, nested = None, [], []
    tokens = iter(out.split(b"\0"))
    for tok in tokens:
        kind = tok[:1]
        if tok.startswith(b"# branch.oid "):
            oid = tok[len(b"# branch.oid "):].decode("ascii", "replace")
            head = None if oid == "(initial)" else oid
        elif tok.startswith(b"? "):
            rel = os.fsdecode(tok[2:])
            if rel.endswith("/"):
                nested.append(rel.rstrip("/"))
            else:
                paths.append(rel)
        elif kind in _V2_FIELDS and tok[1:2] == b" ":
            parts = tok.split(b" ", _V2_FIELDS[kind])
            # У переименования исходный путь — следующая запись -z.
            orig = next(tokens, b"") if kind == b"2" else None
            if len(parts) <= _V2_FIELDS[kind]:
                continue
            paths.append(os.fsdecode(parts[-1]))
            if orig:
                paths.append(os.fsdecode(orig))
    return head, paths, nested


def _gitlinks(root, deadline):
    """Пути подмодулей root — записи с режимом 160000 в `ls-files -s`, при конфликте без повторов."""
    out = _git(root, deadline, "ls-files", "-s", "-z") or b""
    return list(dict.fromkeys(os.fsdecode(e.partition(b"\t")[2]) for e in out.split(b"\0")
                              if e.startswith(b"160000 ")))


def _git_state(root, deadline, prefix="", repos=None):
    """{префикс репозитория от корня: {"head": HEAD или None, "dirty": [пути от корня]}} для root, его
    инициализированных подмодулей и вложенных репозиториев-не-подмодулей; None, если root вне git.

    Корень — префикс "". Подмодуль без своего `.git` не обходится: git в нём отвечает за родителя.
    Вложенный репозиторий, где git не работает, обходится _walk_paths: его файлы — грязные пути родителя.
    """
    st = _status(root, deadline)
    if st is None:
        return None
    repos = {} if repos is None else repos
    head, dirty, nested = st
    pre = f"{prefix}/" if prefix else ""
    entry = {"head": head, "dirty": [pre + p for p in dirty]}
    repos[prefix] = entry
    for sub in _gitlinks(root, deadline):
        if os.path.exists(os.path.join(root, sub, ".git")):
            _git_state(os.path.join(root, sub), deadline, pre + sub, repos)
    for sub in nested:
        if _git_state(os.path.join(root, sub), deadline, pre + sub, repos) is None:
            entry["dirty"] += [f"{pre}{sub}/{p}" for p in _walk_paths(os.path.join(root, sub), deadline)]
    return repos


def capture(root, deadline=None):
    """Снимок дерева root на старте реплики без prompt_id и root; TooManyFiles — путей больше MAX_FILES;
    TimeoutError — не уложился в срок deadline (time.monotonic).

    В git ("mode": "git"): "repos" — {префикс: HEAD} корня, подмодулей и вложенных репозиториев,
    "dirty" — {путь: [size, mtime_ns] или None} изменённых против HEAD и неотслеживаемых путей; None —
    путь не обычный файл или удалён. Stat всех файлов не снимается: размер снимка не зависит от дерева.
    Вне git ("mode": "walk"): "files" — {путь: [size, mtime_ns]} всех файлов обхода _walk_paths.
    "head" и "sub_heads" — базы для comments.extract.
    """
    root = pathlib.Path(root)
    repos = _git_state(root, deadline)
    if repos is None:
        files = _stat_files(root, _walk_paths(root, deadline), deadline)
        if files is None:
            raise TooManyFiles(f"дерево больше {MAX_FILES} файлов")
        return {"mode": "walk", "head": None, "sub_heads": {}, "files": files}
    # Предел ограничивает запись снимка: она идёт после проверки срока.
    dirty = list(dict.fromkeys(p for r in repos.values() for p in r["dirty"]))
    if len(dirty) > MAX_FILES:
        raise TooManyFiles(f"изменённых и неотслеживаемых файлов больше {MAX_FILES}")
    stats = {}
    for i, rel in enumerate(dirty):
        if i % STAT_CHECK_EVERY == 0:
            _remaining(deadline)
        stats[rel] = _stat_one(root, rel)
    heads = {p: r["head"] for p, r in repos.items()}
    return {"mode": "git", "head": heads[""], "sub_heads": {p: h for p, h in heads.items() if p},
            "repos": heads, "dirty": stats}


def _undetermined(reason):
    common.warn(f"сверка документации не проверена: {reason}")
    return None


def _since_commit(repo_dir, base, deadline):
    """Пути от repo_dir, отличные в рабочем дереве от коммита base, с удалёнными; без base — все
    отслеживаемые; None — base недоступен."""
    if base is None:
        return _git_ls(repo_dir, deadline, "-c")
    out = _git(repo_dir, deadline, "--no-optional-locks", "diff", "--name-only", "-z", "--no-renames",
               "--ignore-submodules=all", base, "--")
    return None if out is None else [os.fsdecode(e) for e in out.split(b"\0") if e]


def changed_since(root, snap, deadline=None):
    """[(путь от root, обычный ли это файл сейчас)] файлов, изменённых со снимка snap (snapshot.load),
    по пути; None с предупреждением, если определить нельзя: сменился режим git/обход, пропал
    репозиторий снимка, недоступен коммит HEAD снимка, вне git дерево больше MAX_FILES.
    TimeoutError — не уложился в срок deadline (time.monotonic).

    В git изменённые — пути, отличные от HEAD снимка (коммиты и правки за реплику), грязные сейчас и
    грязные на старте, кроме тех, чей stat совпал со снимком. Новый репозиторий — все его файлы.
    """
    root = pathlib.Path(root)
    mode = snap.get("mode")
    try:
        repos = _git_state(root, deadline)
    except TooManyFiles as e:
        return _undetermined(str(e))
    if (repos is not None) != (mode == "git"):
        return _undetermined("за реплику корень проекта " +
                             ("стал git-репозиторием" if repos is not None else "перестал быть git-репозиторием"))
    if repos is None:
        try:
            now = _stat_files(root, _walk_paths(root, deadline), deadline)
        except TooManyFiles as e:
            return _undetermined(str(e))
        if now is None:
            return _undetermined(f"дерево больше {MAX_FILES} файлов")
        return [(p, p in now) for p in diff(snap["files"], now)]
    start_heads, start_dirty = snap["repos"], snap["dirty"]
    gone = [p for p in start_heads if p not in repos]
    if gone:
        return _undetermined(f"за реплику пропал репозиторий {gone[0]}")
    candidates = dict.fromkeys(start_dirty)
    for prefix, repo in repos.items():
        candidates.update(dict.fromkeys(repo["dirty"]))
        if prefix in start_heads and repo["head"] == start_heads[prefix]:
            continue
        base = start_heads.get(prefix)
        names = _since_commit(root / prefix if prefix else root, base, deadline)
        if names is None:
            return _undetermined(f"git не отдал изменения против снимка в репозитории {prefix or '.'}")
        pre = f"{prefix}/" if prefix else ""
        candidates.update(dict.fromkeys(pre + n for n in names))
    changed = []
    for i, rel in enumerate(candidates):
        if i % STAT_CHECK_EVERY == 0:
            _remaining(deadline)
        now = _stat_one(root, rel)
        was = start_dirty.get(rel, False)
        if now == was:
            continue
        # Ссылка или каталог на месте пути, который не был обычным файлом, — не файл кода.
        if now is None and not was and os.path.lexists(os.path.join(root, rel)):
            continue
        changed.append((rel, now is not None))
    return sorted(changed)


def _snap_path(state_dir, session_id):
    return state_dir / f"{common.safe_name(session_id)}.snap.json"


def store(state_dir, session_id, prompt_id, root, snap):
    """Снимок capture в state_dir/<session>.snap.json вместе с репликой и корнем."""
    common.atomic_write_json(_snap_path(state_dir, session_id),
                              {**snap, "prompt_id": prompt_id, "root": str(root)})


def load(state_dir, session_id):
    """Снимок сессии; None — нет файла, битый JSON, режим не "git" и не "walk" или нет полей режима."""
    path = _snap_path(state_dir, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # ValueError покрывает JSONDecodeError и UnicodeDecodeError.
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("mode")
    if mode == "git":
        valid = isinstance(data.get("dirty"), dict) and isinstance(data.get("repos"), dict) \
            and "" in data["repos"]
    else:
        valid = mode == "walk" and isinstance(data.get("files"), dict)
    return data if valid else None


def diff(old, new):
    changed = old.keys() ^ new.keys()
    changed |= {p for p in old.keys() & new.keys() if old[p] != new[p]}
    return sorted(changed)
