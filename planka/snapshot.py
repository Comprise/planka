"""Снимок дерева проекта: какие файлы и с каким размером и mtime были на реплике."""
import json
import os
import pathlib
import tempfile

IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build",
                          "dist", ".dart_tool", ".superpowers", ".audit", ".data", ".idea", ".vscode"})
# Порог по умолчанию; MAX_FILES — рабочее значение, его подменяют тесты.
DEFAULT_MAX_FILES = 50_000
MAX_FILES = DEFAULT_MAX_FILES


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
    changed = {p for p in old.keys() ^ new.keys()}
    changed |= {p for p in old.keys() & new.keys() if old[p] != new[p]}
    return sorted(changed)
