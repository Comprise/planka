"""Снимок дерева проекта: какие файлы и с каким размером и mtime были на реплике; отметка проверки снимка
на Stop и неудачи снимка в сессии."""
import base64
import json
import os
import pathlib
import stat
import struct
import subprocess
import time
import zlib

import common

# Каталоги, которые обход вне git пропускает всегда.
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build",
                          "dist", ".dart_tool", ".data", ".idea", ".vscode"})
# Предел изменённых и неотслеживаемых путей снимка в git: они хранятся словарём, объектом на путь, в памяти
# и в JSON файла снимка. Обход вне git ограничен только сроком.
MAX_FILES = 50_000
# Срок проверяется раз в столько файлов при lstat.
STAT_CHECK_EVERY = 256
# Версия формата файла снимка; файл другой версии load отвергает.
FORMAT = 2


class TooManyFiles(Exception):
    """Изменённых и неотслеживаемых путей снимка в git больше MAX_FILES; текст — причина для предупреждения."""


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
    """stdout `git -C root <args>` байтами; None вне git или при сбое; TimeoutError по сроку deadline.
    Репозиторий ищется в root и ниже (common.git_env): root — корень проекта или репозиторий под ним."""
    # TimeoutError — подкласс OSError; срок проверяется вне try.
    timeout = _remaining(deadline)
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout,
                              env=common.git_env(root))
    except subprocess.TimeoutExpired:
        raise TimeoutError("git не уложился в срок снимка") from None
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_ls(root, deadline, *args):
    """Записи `git ls-files -z <args>` в root; None вне git или при сбое."""
    out = _git(root, deadline, "ls-files", "-z", *args)
    return None if out is None else [os.fsdecode(e) for e in out.split(b"\0") if e]


def _ext_ignored(name, exts):
    """Совпадает ли имя файла с записью «*.ext» из exts: часть имени после любой его точки — в exts."""
    i = name.find(".")
    while i != -1:
        if name[i + 1:] in exts:
            return True
        i = name.find(".", i + 1)
    return False


def _walk(root, deadline):
    """(каталог от root через «/», "" — сам root; [DirEntry обычных файлов каталога]) обходом под root;
    TimeoutError по сроку deadline. Каталог без файлов не выдаётся.

    Правила каталога — правила родителя плюс его собственный .gitignore, упрощённо по ignore_rules;
    действуют на его поддерево. Символические ссылки не обходятся и в файлы не попадают; каталог, который
    не прочесть, пропускается.
    """
    stack = [("", os.fspath(root), (frozenset(), frozenset()))]
    while stack:
        _remaining(deadline)
        rel, path, rules = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        if any(e.name == ".gitignore" for e in entries):
            own_names, own_exts = ignore_rules(pathlib.Path(path))
            if own_names or own_exts:
                rules = (rules[0] | own_names, rules[1] | own_exts)
        names, exts = rules
        files = []
        pre = f"{rel}/" if rel else ""
        for e in entries:
            if e.name in names:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in IGNORED_DIRS:
                        stack.append((pre + e.name, e.path, rules))
                elif e.is_file(follow_symlinks=False) and not _ext_ignored(e.name, exts):
                    files.append(e)
            except OSError:
                continue
        if files:
            yield rel, files


def _walk_paths(root, deadline):
    """Пути обычных файлов под root обходом _walk; TimeoutError по сроку deadline."""
    return [f"{rel}/{e.name}" if rel else e.name for rel, files in _walk(root, deadline) for e in files]


# Запись файла в блоке каталога: имя в байтах ФС, NUL, size и mtime_ns. Имя файла не содержит NUL.
_STAT = struct.Struct("<Qq")
# Длина блока каталога в записи файла снимка.
_LEN = struct.Struct("<I")


def _walk_dirs(root, deadline):
    """{каталог от root: блок} обычных файлов обхода _walk; TimeoutError по сроку deadline.

    Блок — записи _STAT файлов каталога, отсортированные по имени: каталог без правок даёт тот же блок
    при любом порядке readdir, и сверка сравнивает каталоги целиком, не разбирая записи. Пути не хранятся
    объектом на файл: память — около размера блоков.
    """
    dirs = {}
    count = 0
    for rel, files in _walk(root, deadline):
        records = []
        for e in files:
            count += 1
            if count % STAT_CHECK_EVERY == 0:
                _remaining(deadline)
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                records.append(os.fsencode(e.name) + b"\0" + _STAT.pack(st.st_size, st.st_mtime_ns))
        if records:
            records.sort()
            dirs[rel] = b"".join(records)
    return dirs


def _records(block):
    """{имя в байтах ФС: упакованные size и mtime_ns} блока _walk_dirs."""
    out = {}
    pos = 0
    while pos < len(block):
        end = block.index(b"\0", pos)
        out[block[pos:end]] = block[end + 1:end + 1 + _STAT.size]
        pos = end + 1 + _STAT.size
    return out


def diff(old, new):
    """[(путь, есть ли файл в new)] файлов, которые отличаются между снимками _walk_dirs old и new, по пути."""
    changed = []
    for rel in old.keys() | new.keys():
        a, b = old.get(rel, b""), new.get(rel, b"")
        if a == b:
            continue
        ra, rb = _records(a), _records(b)
        pre = f"{rel}/" if rel else ""
        names = ra.keys() ^ rb.keys()
        names |= {n for n in ra.keys() & rb.keys() if ra[n] != rb[n]}
        changed += [(pre + os.fsdecode(n), n in rb) for n in names]
    return sorted(changed)


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
    """Снимок дерева root на старте реплики без prompt_id и root; TooManyFiles — в git изменённых и
    неотслеживаемых путей больше MAX_FILES; TimeoutError — не уложился в срок deadline (time.monotonic).

    В git ("mode": "git"): "repos" — {префикс: HEAD} корня, подмодулей и вложенных репозиториев,
    "dirty" — {путь: [size, mtime_ns] или None} изменённых против HEAD и неотслеживаемых путей; None —
    путь не обычный файл или удалён. Stat всех файлов не снимается: размер снимка не зависит от дерева.
    Вне git ("mode": "walk"): "dirs" — {каталог: блок} всех обычных файлов обхода _walk_dirs.
    "head" и "sub_heads" — базы для comments.extract.
    """
    root = pathlib.Path(root)
    repos = _git_state(root, deadline)
    if repos is None:
        return {"mode": "walk", "head": None, "sub_heads": {}, "dirs": _walk_dirs(root, deadline)}
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
    репозиторий снимка, недоступен коммит HEAD снимка.
    TimeoutError — не уложился в срок deadline (time.monotonic).

    В git изменённые — пути, отличные от HEAD снимка (коммиты и правки за реплику), грязные сейчас и
    грязные на старте, кроме тех, чей stat совпал со снимком. Новый репозиторий — все его файлы.
    """
    root = pathlib.Path(root)
    mode = snap.get("mode")
    repos = _git_state(root, deadline)
    if (repos is not None) != (mode == "git"):
        return _undetermined("за реплику корень проекта " +
                             ("стал git-репозиторием" if repos is not None else "перестал быть git-репозиторием"))
    if repos is None:
        return diff(snap["dirs"], _walk_dirs(root, deadline))
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


def _pack_dirs(dirs):
    """Блоки _walk_dirs одной строкой: base64 от zlib записей «каталог в байтах ФС, NUL, длина блока, блок»."""
    # Сжатие по каталогу: несжатая запись целиком в памяти не собирается.
    z = zlib.compressobj(1)
    parts = []
    for rel, block in dirs.items():
        parts.append(z.compress(os.fsencode(rel) + b"\0" + _LEN.pack(len(block))))
        parts.append(z.compress(block))
    parts.append(z.flush())
    return base64.b64encode(b"".join(parts)).decode("ascii")


def _unpack_dirs(text):
    """Обратное _pack_dirs; ValueError, zlib.error или struct.error — запись повреждена."""
    raw = zlib.decompress(base64.b64decode(text, validate=True))
    dirs = {}
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\0", pos)
        (size,) = _LEN.unpack_from(raw, end + 1)
        start = end + 1 + _LEN.size
        if start + size > len(raw):
            raise ValueError("обрезанный блок каталога")
        dirs[os.fsdecode(raw[pos:end])] = raw[start:start + size]
        pos = start + size
    return dirs


def store(state_dir, session_id, prompt_id, root, snap):
    """Снимок capture в state_dir/<session>.snap.json вместе с репликой, корнем, версией формата FORMAT и
    "checked": False; блоки режима walk — строкой _pack_dirs."""
    data = {**snap, "format": FORMAT, "prompt_id": prompt_id, "root": str(root), "checked": False}
    if snap.get("mode") == "walk":
        data["dirs"] = _pack_dirs(snap["dirs"])
    common.atomic_write_json(_snap_path(state_dir, session_id), data)


def load(state_dir, session_id):
    """Снимок сессии; None — нет файла, битый JSON, версия формата не FORMAT, режим не "git" и не "walk",
    нет полей режима или повреждены блоки walk."""
    path = _snap_path(state_dir, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # ValueError покрывает JSONDecodeError и UnicodeDecodeError.
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    mode = data.get("mode")
    if mode == "git":
        valid = isinstance(data.get("dirty"), dict) and isinstance(data.get("repos"), dict) \
            and "" in data["repos"]
        return data if valid else None
    if mode != "walk" or not isinstance(data.get("dirs"), str):
        return None
    try:
        data["dirs"] = _unpack_dirs(data["dirs"])
    except (ValueError, zlib.error, struct.error):
        return None
    return data


def carry_over(state_dir, session_id, prompt_id, root):
    """Перепривязывает к реплике prompt_id снимок сессии, который Stop не отметил проверенным (mark_checked),
    если он снят для корня root; True — снимок перепривязан и остаётся базой сверки.

    Снимок без поля "checked" считается проверенным. Блоки walk не разворачиваются.
    """
    path = _snap_path(state_dir, session_id)
    with common.state_lock(state_dir):
        data = common.read_json(path, dict)
        if data.get("format") != FORMAT or data.get("checked", True) is not False \
                or data.get("root") != str(root):
            return False
        data["prompt_id"] = prompt_id
        common.atomic_write_json(path, data)
    return True


def mark_checked(state_dir, session_id, prompt_id):
    """Отмечает снимок сессии реплики prompt_id проверенным: следующая реплика снимет новый.
    Нет снимка или он другой реплики — ничего."""
    path = _snap_path(state_dir, session_id)
    with common.state_lock(state_dir):
        data = common.read_json(path, dict)
        if data.get("checked", True) is not False or data.get("prompt_id") != prompt_id:
            return
        data["checked"] = True
        common.atomic_write_json(path, data)


def _failed_path(state_dir, session_id):
    return state_dir / f"{common.safe_name(session_id)}.snapfail.json"


def failure(state_dir, session_id, root):
    """Причина неудачи снимка корня root в этой сессии (mark_failed); None — снимок корня не падал."""
    with common.state_lock(state_dir):
        reason = common.read_json(_failed_path(state_dir, session_id), dict).get(str(root))
    return reason if isinstance(reason, str) else None


def mark_failed(state_dir, session_id, root, reason):
    """Запоминает неудачу снимка корня root в сессии: {корень: причина} в state_dir/<session>.snapfail.json."""
    path = _failed_path(state_dir, session_id)
    with common.state_lock(state_dir):
        failed = common.read_json(path, dict)
        failed[str(root)] = reason
        common.atomic_write_json(path, failed)
