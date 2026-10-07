"""Снимок дерева проекта: какие файлы и с каким размером и mtime были на реплике."""
import json
import os
import pathlib
import subprocess
import tempfile

# Каталоги, которые обход вне git пропускает всегда.
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build",
                          "dist", ".dart_tool", ".data", ".idea", ".vscode"})
# Порог по умолчанию; MAX_FILES — рабочий порог.
DEFAULT_MAX_FILES = 50_000
MAX_FILES = DEFAULT_MAX_FILES


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


def _git_ls(root, *args):
    """Записи `git ls-files -z <args>` в root; None вне git или при сбое."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [os.fsdecode(e) for e in proc.stdout.split(b"\0") if e]


def _git_paths(root):
    """Пути файлов рабочего дерева, которые git не игнорирует (отслеживаемые и новые), включая файлы
    подмодулей; None вне git.

    Подмодуль — запись с режимом 160000 в `ls-files -s` — обходится отдельным вызовом; подмодуль без
    своего `.git` не обходится.
    """
    paths = _git_ls(root, "-co", "--exclude-standard")
    if paths is None:
        return None
    for entry in _git_ls(root, "-s") or []:
        meta, _, sub = entry.partition("\t")
        if meta.startswith("160000 "):
            if sub in paths:
                paths.remove(sub)
            if os.path.exists(os.path.join(root, sub, ".git")):
                paths += [f"{sub}/{p}" for p in _git_paths(os.path.join(root, sub)) or []]
    return paths


def _stat_files(root, relpaths):
    files = {}
    for rel in relpaths:
        full = os.path.join(root, rel)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        files[rel] = [st.st_size, st.st_mtime_ns]
        if len(files) > MAX_FILES:
            return None
    return files


def scan(root):
    """{путь: [size, mtime_ns]} для файлов под root; None, если их больше MAX_FILES.

    В git-репозитории — файлы, которые git не игнорирует: правила .gitignore, .git/info/exclude и
    глобального excludes точные. Вне git — обход каталогов: правила каталога — правила родителя плюс
    его собственный .gitignore, упрощённо по ignore_rules; действуют на его поддерево.
    """
    root = pathlib.Path(root)
    paths = _git_paths(root)
    if paths is not None:
        return _stat_files(root, paths)
    rules_by_dir = {}
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        parent = rules_by_dir.get(os.path.dirname(dirpath), (set(), set()))
        own = ignore_rules(pathlib.Path(dirpath))
        names, exts = parent[0] | own[0], parent[1] | own[1]
        rules_by_dir[dirpath] = (names, exts)
        skip = IGNORED_DIRS | names
        dirnames[:] = [d for d in dirnames if d not in skip and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name in names or any(name.endswith("." + e) for e in exts):
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
    # ValueError покрывает JSONDecodeError и UnicodeDecodeError.
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def diff(old, new):
    changed = old.keys() ^ new.keys()
    changed |= {p for p in old.keys() & new.keys() if old[p] != new[p]}
    return sorted(changed)
